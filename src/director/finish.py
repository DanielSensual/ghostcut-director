"""Finishing: clean voice, optional sound design, ducked mix, mastered cut.

`director finish` (and the `finish` stage of `director cut`) takes the
rendered `cut.mp4` and its `angles.json` and finishes the audio: the
voice is cleaned locally with ffmpeg (or with ElevenLabs audio isolation
when connected and asked), Muse Spark optionally places a music bed and
a few effects, the provider generates them, Director mixes them ducked
under the voice, masters two-pass to −14 LUFS / −1 dBTP (or −16), and
remuxes the result as `cut.mp4`'s audio — so `vertical.mp4` inherits it.
Nothing mutates `cut.mp4` until the final atomic replace.

With no keys the stage still cleans, levels and masters locally; sound
design is off unless asked. Every generated file stays in `finish/`
with its prompt, provider, cost and licence note.

Designed by Muse Code on muse-wonder-1.0, built by Muse Code on
muse-spark-1.3 from docs/muse-code/FINISH-DESIGN.md. The de-plosive
and the post-loudnorm limiter were added by Muse Code on muse-spark-1.3
from docs/muse-code/AIR-BRIEF.md.
"""
from __future__ import annotations

import math
import os
import re
import time
import wave
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ghostcut.config import read_json, write_json
from ghostcut.ffmpeg_tools import (
    ToolingError,
    extract_media_stats,
    probe_media,
    run_command,
)

from . import angles as angles_mod
from . import connect as connect_mod
from . import cutlist
from . import elevenlabs as elevenlabs_mod

try:
    import numpy as np
except ImportError:  # the director extra is optional
    np = None  # type: ignore[assignment]

AUDIO_SR = 48000
VOICE_HIGHPASS_HZ = 80
VOICE_DENOISE_NR = 8
VOICE_DENOISE_NF = -50
VOICE_COMP_THRESHOLD = 0.125
VOICE_COMP_RATIO = 3
VOICE_COMP_ATTACK_MS = 10
VOICE_COMP_RELEASE_MS = 250
TARGET_LUFS_DEFAULT = -14.0
TARGET_LUFS_ALT = -16.0
TARGET_TP_DBTP = -1.0
TARGET_LRA = 11
MUSIC_GAIN_DB = -20.0
MUSIC_GAIN_LOW_DB = -26.0
SFX_GAIN_MIN_DB = -24.0
SFX_GAIN_MAX_DB = -6.0
DUCK_THRESHOLD = 0.10
DUCK_RATIO = 4
DUCK_ATTACK_MS = 5
DUCK_RELEASE_MS = 400
MAX_SFX_PER_MINUTE = 3
MAX_SFX_TOTAL = 12
EDGE_GUARD_S = 1.0
WORD_OVERLAP_CAP_DB = -12.0
DESIGN_MAX_OUTPUT_TOKENS = 6000
DESIGN_INPUT_TOKENS_PER_S = 0
SPARK_TOKEN_RATES_USD_PER_1M = {
    "muse-spark-1.3": (1.25, 4.25),
    "muse-wonder-1.0": (4.00, 20.00),
}
DEFAULT_MAX_PROVIDER_USD = 2.00
ISOLATION_MAX_S = 3600
CUE_KINDS = ("riser", "whoosh", "impact")
# Mic-air de-plosive (AIR-BRIEF.md): the threshold is measured per
# recording (speech-gated low-band level + 6 dB); the filter values
# below are the recipe as measured on the owner's take.
DEPLOSIVE_MEASURE_FC = 150.0
DEPLOSIVE_SPLIT_FC = 160
DEPLOSIVE_HIGHPASS_HZ = 60
DEPLOSIVE_RATIO = 20
DEPLOSIVE_ATTACK_MS = 0.3
DEPLOSIVE_RELEASE_MS = 40
DEPLOSIVE_KNEE = 2
DEPLOSIVE_OFFSET_DB = 6.0
DEPLOSIVE_FALLBACK_THRESHOLD_DBFS = -36.0
DEPLOSIVE_WINDOW_MS = 30.0
DEPLOSIVE_MERGE_GAP_S = 0.30
# Gentle peak limiter after loudness normalisation: -1 dBFS, ffmpeg's
# default attack/release, no auto level, delay compensated.
LIMITER_LIMIT = 0.891251
LIMITER_ATTACK = 5
LIMITER_RELEASE = 50

DEVELOPER = (
    "You place sound under a finished talking-head cut. You do not edit "
    "picture and you never invent speech. Return only JSON matching the "
    "schema; every other byte is discarded.\n"
    "Propose at most one instrumental music bed and a few sound effects: "
    "a riser into a new section, a soft whoosh at a visual break, an "
    "impact under a cold open. Prefer silence to decoration. Speech is "
    "sacred: music lives under it ducked, effects sit in gaps or land on "
    "boundaries, and nothing musical may have vocals.\n"
    "Times are finish seconds (0 = first frame of the rendered cut). "
    "Music start_s/end_s bound the bed; dropouts are intervals where the "
    "bed must be fully silent (for example under an intimate or crucial "
    "line). Effects carry at_s (start), duration_s, a generation prompt "
    "for a sound-effects model, gain_db (quieter is more professional), "
    "a reason an editor can judge, and confidence 0..1."
)


class FinishError(RuntimeError):
    """A finish step failed. The message always starts with 'finish: '."""


class DesignUnavailable(RuntimeError):
    """guestlist cannot be imported (the --no-muse degradation)."""


@dataclass(slots=True)
class TasteFinish:
    no_music: bool = False
    no_sound: bool = False
    forbid_kinds: frozenset = frozenset()
    music_low: bool = False
    loudness: float | None = None
    allow_edges: bool = False
    isolate: bool = False


@dataclass(slots=True)
class MusicPlan:
    prompt: str
    start_s: float
    end_s: float
    gain_db: float
    dropouts: list[tuple[float, float]] = field(default_factory=list)
    reason: str = ""
    confidence: float = 0.0


@dataclass(slots=True)
class SoundCue:
    id: str  # "sfx_001"… in placement order ("" until verified)
    kind: str  # "riser" | "whoosh" | "impact"
    at_s: float  # finish timeline
    duration_s: float
    prompt: str  # provider prompt (capped)
    gain_db: float
    reason: str = ""
    confidence: float = 0.0


@dataclass(slots=True)
class FinishAsset:
    id: str  # "music" | "sfx_001" | "voice_isolation"
    kind: str  # "music" | "riser" | "whoosh" | "impact" | "voice_isolation"
    file: str  # bare file name inside finish/
    provider: str  # "elevenlabs"
    model: str
    prompt: str  # "" for isolation (no prompt; the input is the voice)
    duration_s: float  # probed returned duration
    cost_usd: float
    licence: str


# ------------------------------------------------------------------ taste (§11.3)


def parse_taste(entries: list[str]) -> TasteFinish:
    """The §11.3 phrase table over lowercased entry texts. Pure."""
    out = TasteFinish()
    forbids: set[str] = set()
    for entry in entries:
        text = entry.lower()
        if ("no sound design" in text or "no sound" in text
                or "no effects" in text):
            out.no_music = True
            out.no_sound = True
        elif "no music" in text:
            out.no_music = True
        if "no whoosh" in text:
            forbids.add("whoosh")
        if "no riser" in text:
            forbids.add("riser")
        if "no impact" in text or "no hits" in text:
            forbids.add("impact")
        if "music low" in text or "quiet music" in text or "music quiet" in text:
            out.music_low = True
        if "master to" in text or "loudness" in text:
            if "-16" in text or "−16" in text:
                out.loudness = -16.0
            elif "-14" in text or "−14" in text:
                out.loudness = -14.0
        if ("isolate" in text and "voice" in text) or "clean voice with elevenlabs" in text:
            out.isolate = True
        if "allow edge effects" in text:
            out.allow_edges = True
    out.forbid_kinds = frozenset(forbids)
    return out


def _taste_asks_sound(entries: list[str]) -> bool:
    """Any entry mentioning music asks for sound design (the §11.3 row)."""
    return any("music" in entry.lower() for entry in entries)


# ------------------------------------------------------- timeline (§11.2)


def finish_origin(angles_payload: dict) -> float:
    """shots[0].start_s, else 0.0. Pure."""
    shots = angles_payload.get("shots", []) or []
    if not shots:
        return 0.0
    try:
        return float(shots[0].get("start_s", 0.0))
    except (TypeError, ValueError):
        return 0.0


def word_intervals(
    angles_payload: dict,
    tighten_payload: dict | None,
    words: list[angles_mod.TimedWord],
) -> list[tuple[float, float, str]]:
    """Word (start, end, text) on the finish timeline. Pure.

    Source words map through cutlist.source_to_output when tightened
    (words in removed spans drop out); either way the result shifts by
    −origin. Words ending at or before 0 drop; starts clamp to 0. The
    caller drops words past the probed duration (only it knows it).
    """
    origin = finish_origin(angles_payload)
    out: list[tuple[float, float, str]] = []
    for word in words:
        start, end = float(word.start_s), float(word.end_s)
        if tighten_payload is not None:
            mapped_start = cutlist.source_to_output(tighten_payload, start)
            mapped_end = cutlist.source_to_output(tighten_payload, end)
            if mapped_start is None or mapped_end is None:
                continue
            start, end = mapped_start, mapped_end
        start -= origin
        end -= origin
        if end <= 0:
            continue
        out.append((round(max(0.0, start), 3), round(end, 3), word.text))
    out.sort(key=lambda item: (item[0], item[1]))
    return out


def beat_sections(
    angles_payload: dict,
    tighten_payload: dict | None,
    words: list[angles_mod.TimedWord],
) -> list[dict]:
    """[{"id", "topic", "start_s", "end_s"}] on the finish timeline. Pure.

    Tightened: each beat spans the union of its kept spans. Otherwise
    one "Cut" section spans the shots. (words is accepted for the
    designed signature; sections derive from beats/shots alone.)
    """
    _ = words
    origin = finish_origin(angles_payload)
    shots = angles_payload.get("shots", []) or []
    if tighten_payload is not None:
        kept = [s for s in tighten_payload.get("kept_spans", []) or []
                if isinstance(s, dict)]
        sections: list[dict] = []
        for beat in tighten_payload.get("beats", []) or []:
            if not isinstance(beat, dict):
                continue
            bid = beat.get("id")
            edges: list[float] = []
            for span in kept:
                if span.get("beat_id") != bid:
                    continue
                try:
                    lo = cutlist.source_to_output(tighten_payload, float(span["start_s"]))
                    hi = cutlist.source_to_output(tighten_payload, float(span["end_s"]))
                except (TypeError, ValueError, KeyError):
                    continue
                if lo is None or hi is None:
                    continue
                edges.extend((lo - origin, hi - origin))
            if not edges:
                continue
            sections.append({
                "id": bid,
                "topic": str(beat.get("topic", "")),
                "start_s": round(max(0.0, min(edges)), 3),
                "end_s": round(max(edges), 3),
            })
        if sections:
            sections.sort(key=lambda s: (s["start_s"], s["end_s"]))
            return sections
    if shots:
        try:
            end = max(float(s.get("end_s", 0.0)) for s in shots) - origin
        except (TypeError, ValueError):
            end = 0.0
    else:
        end = 0.0
    return [{"id": "cut", "topic": "Cut", "start_s": 0.0,
             "end_s": round(max(0.0, end), 3)}]


# ------------------------------------------------- ffmpeg commands (§11.6)


def extract_voice_command(video: Path, wav: Path) -> list[str]:
    """Step 1: cut.mp4 audio to mono 48 kHz WAV. Pure."""
    return ["ffmpeg", "-v", "error", "-y", "-i", str(video),
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(AUDIO_SR),
            "-c:a", "pcm_s16le", str(wav)]


def isolation_input_command(video: Path, wav: Path) -> list[str]:
    """The WAV sent for isolation. Pure (same extraction as step 1)."""
    return ["ffmpeg", "-v", "error", "-y", "-i", str(video),
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(AUDIO_SR),
            "-c:a", "pcm_s16le", str(wav)]


def dbfs_to_linear(db: float) -> float:
    """dBFS to linear amplitude. Pure."""
    return 10.0 ** (float(db) / 20.0)


def clean_voice_command(src: Path, dst: Path, *, isolated: bool,
                        deplosive_threshold: float | None = None) -> list[str]:
    """Step 2: high-pass (+denoise when local) + gentle levelling. Pure.

    When `deplosive_threshold` (linear amplitude) is given, the initial
    high-pass is replaced by the AIR-BRIEF de-plosive block: high-pass at
    60 Hz, split at 160 Hz (two cascaded 2nd-order filters each side),
    the low band through a 20:1 compressor, bands summed. `None` keeps
    the original chain.
    """
    if deplosive_threshold is None:
        if isolated:
            chain = (f"highpass=f={VOICE_HIGHPASS_HZ},"
                     f"acompressor=threshold={VOICE_COMP_THRESHOLD}:"
                     f"ratio={VOICE_COMP_RATIO}:attack={VOICE_COMP_ATTACK_MS}:"
                     f"release={VOICE_COMP_RELEASE_MS}")
        else:
            chain = (f"highpass=f={VOICE_HIGHPASS_HZ},"
                     f"afftdn=nr={VOICE_DENOISE_NR}:nf={VOICE_DENOISE_NF},"
                     f"acompressor=threshold={VOICE_COMP_THRESHOLD}:"
                     f"ratio={VOICE_COMP_RATIO}:attack={VOICE_COMP_ATTACK_MS}:"
                     f"release={VOICE_COMP_RELEASE_MS}")
        return ["ffmpeg", "-v", "error", "-y", "-i", str(src),
                "-af", chain, "-ac", "1", "-ar", str(AUDIO_SR),
                "-c:a", "pcm_s16le", str(dst)]
    thr = min(1.0, max(0.000976563, float(deplosive_threshold)))
    comp = (f"acompressor=threshold={thr:.7g}:ratio={DEPLOSIVE_RATIO}"
            f":attack={DEPLOSIVE_ATTACK_MS}:release={DEPLOSIVE_RELEASE_MS}"
            f":knee={DEPLOSIVE_KNEE}")
    split = (
        f"[0:a]highpass=f={DEPLOSIVE_HIGHPASS_HZ},asplit[lo_in][hi_in];"
        f"[lo_in]lowpass=f={DEPLOSIVE_SPLIT_FC},"
        f"lowpass=f={DEPLOSIVE_SPLIT_FC},{comp}[lo];"
        f"[hi_in]highpass=f={DEPLOSIVE_SPLIT_FC},"
        f"highpass=f={DEPLOSIVE_SPLIT_FC}[hi];"
        f"[lo][hi]amix=inputs=2:normalize=0[mixed]")
    tail = (f"acompressor=threshold={VOICE_COMP_THRESHOLD}:"
            f"ratio={VOICE_COMP_RATIO}:attack={VOICE_COMP_ATTACK_MS}:"
            f"release={VOICE_COMP_RELEASE_MS}")
    if not isolated:
        tail = (f"afftdn=nr={VOICE_DENOISE_NR}:nf={VOICE_DENOISE_NF},{tail}")
    graph = f"{split};[mixed]{tail}[out]"
    return ["ffmpeg", "-v", "error", "-y", "-i", str(src),
            "-filter_complex", graph, "-map", "[out]",
            "-ac", "1", "-ar", str(AUDIO_SR),
            "-c:a", "pcm_s16le", str(dst)]


def limiter_command(src: Path, dst: Path) -> list[str]:
    """Step 6c: gentle peak limiter after loudness normalisation. Pure."""
    af = (f"alimiter=limit={LIMITER_LIMIT}:attack={LIMITER_ATTACK}"
          f":release={LIMITER_RELEASE}:level=0:latency=1")
    return ["ffmpeg", "-v", "error", "-y", "-i", str(src),
            "-af", af, "-ac", "2", "-ar", str(AUDIO_SR),
            "-c:a", "pcm_s16le", str(dst)]


def _lowpass_twice(samples, sample_rate: int,
                   cutoff: float = DEPLOSIVE_MEASURE_FC):
    """Two cascaded 2nd-order Butterworth low-passes, zero-phase via FFT.

    Matches `ffmpeg lowpass=f=<cutoff>,lowpass=f=<cutoff>` magnitude
    (Q=0.707) to 0.1 dB on the owner's take; phase is zeroed, which the
    RMS envelope does not see. Requires numpy. Pure.
    """
    assert np is not None
    x = np.asarray(samples, dtype=np.float64).ravel()
    if x.size == 0:
        return x
    w0 = 2.0 * math.pi * float(cutoff) / float(sample_rate)
    cosw = math.cos(w0)
    alpha = math.sin(w0) / (2.0 * 0.70710678)
    b0 = (1.0 - cosw) / 2.0
    b1 = 1.0 - cosw
    b2 = b0
    a0 = 1.0 + alpha
    a1 = -2.0 * cosw
    a2 = 1.0 - alpha
    b = np.array([b0, b1, b2]) / a0
    a = np.array([1.0, a1 / a0, a2 / a0])
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(x.size, 1.0 / float(sample_rate))
    w = 2.0 * np.pi * freqs / float(sample_rate)
    z = np.exp(-1j * w)
    resp = ((b[0] + b[1] * z + b[2] * z * z)
            / (a[0] + a[1] * z + a[2] * z * z))
    return np.fft.irfft(spectrum * resp * resp, n=x.size)


def speech_gated_level(levels_db) -> float:
    """Median over the louder half of the frames. Pure (needs numpy)."""
    assert np is not None
    levels = np.asarray(list(levels_db), dtype=np.float64).ravel()
    if levels.size == 0:
        return float("-inf")
    median_all = float(np.median(levels))
    louder = levels[levels > median_all]
    if louder.size == 0:
        return median_all
    return float(np.median(louder))


def _count_moments(above, hop_s: float) -> int:
    """Above-threshold frames grouped into bursts. Pure (needs numpy)."""
    assert np is not None
    flags = np.asarray(list(above), dtype=bool).ravel()
    if not bool(np.any(flags)):
        return 0
    gap_frames = max(1, int(round(DEPLOSIVE_MERGE_GAP_S / hop_s)))
    groups = 0
    in_run = False
    gap = 0
    for flag in flags:
        if flag:
            if not in_run:
                groups += 1
                in_run = True
            gap = 0
        elif in_run:
            gap += 1
            if gap > gap_frames:
                in_run = False
                gap = 0
    return groups


def measure_deplosive_threshold_from_samples(samples,
                                             sample_rate: int
                                             ) -> tuple[float, int]:
    """(threshold_dbfs, moments) from mono float samples. Pure.

    Low-passes at 150 Hz (twice), takes 30 ms RMS windows, medians over
    the louder half for the speech-gated low-band level, adds 6 dB.
    Requires numpy.
    """
    assert np is not None
    x = np.asarray(samples, dtype=np.float64).ravel()
    sr = int(sample_rate)
    hop = max(1, int(round(sr * DEPLOSIVE_WINDOW_MS / 1000.0)))
    nblocks = x.size // hop
    if nblocks == 0:
        return DEPLOSIVE_FALLBACK_THRESHOLD_DBFS, 0
    low = _lowpass_twice(x[:nblocks * hop], sr)
    rms = np.sqrt(np.mean(low.reshape(nblocks, hop) ** 2, axis=1))
    with np.errstate(divide="ignore"):
        levels = 20.0 * np.log10(np.maximum(rms, 1e-12))
    gated = speech_gated_level(levels)
    threshold = gated + DEPLOSIVE_OFFSET_DB
    hop_s = hop / float(sr)
    moments = _count_moments(levels > threshold, hop_s)
    return round(float(threshold), 1), moments


def measure_deplosive_threshold(wav_path: Path) -> tuple[float, int, bool]:
    """(threshold_dbfs, moments, fallback) for a voice WAV. Never raises.

    Without numpy, or when the file will not read as PCM, falls back to
    the fixed −36 dBFS threshold with 0 moments and True.
    """
    if np is None:
        return DEPLOSIVE_FALLBACK_THRESHOLD_DBFS, 0, True
    try:
        with wave.open(str(wav_path), "rb") as handle:
            sr = handle.getframerate()
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            frames = handle.getnframes()
            if frames <= 0 or sr <= 0 or channels <= 0:
                return DEPLOSIVE_FALLBACK_THRESHOLD_DBFS, 0, True
            raw = handle.readframes(frames)
        if width == 2:
            data = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
            data = data / 32768.0
        elif width == 4:
            # 32-bit PCM (signed); voice_raw is 16-bit, this is robustness.
            data = np.frombuffer(raw, dtype=np.int32).astype(np.float64)
            data = data / 2147483648.0
        else:
            return DEPLOSIVE_FALLBACK_THRESHOLD_DBFS, 0, True
        if channels > 1:
            data = data.reshape(-1, channels).mean(axis=1)
        threshold, moments = measure_deplosive_threshold_from_samples(
            data, sr)
        return threshold, moments, False
    except (OSError, EOFError, wave.Error, ValueError, AssertionError):
        return DEPLOSIVE_FALLBACK_THRESHOLD_DBFS, 0, True


def loudnorm_measure_command(wav: Path, target_lufs: float) -> list[str]:
    """Step 6a: measure the mix against the target. Pure."""
    return ["ffmpeg", "-nostats", "-nostdin", "-i", str(wav),
            "-vn", "-af",
            f"loudnorm=I={target_lufs}:TP={TARGET_TP_DBTP}:LRA={TARGET_LRA}"
            ":print_format=json",
            "-f", "null", "-"]


def parse_loudnorm_full(stderr: str) -> dict | None:
    """The full measured block, or None on any failure. Pure.

    The applying pass wants `offset`; ffmpeg's JSON names it
    `target_offset`, which is read here (with `offset` as fallback).
    """
    try:
        found = stderr.rfind("[Parsed_loudnorm")
        if found < 0:
            return None
        start = stderr.find("{", found)
        end = stderr.rfind("}")
        if start < 0 or end < start:
            return None
        import json as _json

        data = _json.loads(stderr[start:end + 1])
        offset = data.get("target_offset", data.get("offset"))
        parsed = {
            "input_i": float(data["input_i"]),
            "input_tp": float(data["input_tp"]),
            "input_lra": float(data["input_lra"]),
            "input_thresh": float(data["input_thresh"]),
            "offset": float(offset),
        }
        if not all(math.isfinite(value) for value in parsed.values()):
            return None
        return parsed
    except (ValueError, KeyError, TypeError):
        return None


def loudnorm_apply_command(src: Path, dst: Path, *, target_lufs: float,
                           measured: dict) -> list[str]:
    """Step 6b: apply the measured correction. Pure."""
    af = (f"loudnorm=I={target_lufs}:TP={TARGET_TP_DBTP}:LRA={TARGET_LRA}"
          f":measured_I={measured['input_i']}"
          f":measured_TP={measured['input_tp']}"
          f":measured_LRA={measured['input_lra']}"
          f":measured_thresh={measured['input_thresh']}"
          f":offset={measured['offset']}:linear=true")
    return ["ffmpeg", "-v", "error", "-y", "-i", str(src),
            "-af", af, "-ac", "2", "-ar", str(AUDIO_SR),
            "-c:a", "pcm_s16le", str(dst)]


def music_mix_command(bed: Path, duration_s: float, plan: MusicPlan,
                      out: Path) -> list[str]:
    """Step 4: loop/trim/fade/dropout the bed to full cut length. Pure."""
    span = plan.end_s - plan.start_s
    fade_out_start = max(0.0, span - 1.0)
    vols = "".join(
        f"volume=enable='between(t,{a - plan.start_s:.3f},{b - plan.start_s:.3f})'"
        ":volume=0,"
        for a, b in plan.dropouts)
    chain = (f"aresample={AUDIO_SR},aloop=loop=-1:size=2147483647,"
             f"atrim=0:{span:.3f},asetpts=N/SR/TB,"
             f"{vols}"
             f"afade=t=in:st=0:d=1,afade=t=out:st={fade_out_start:.3f}:d=1")
    delay_ms = round(plan.start_s * 1000)
    full = (f"{chain},adelay={delay_ms}|{delay_ms},"
            f"apad,atrim=0:{duration_s:.3f}")
    return ["ffmpeg", "-v", "error", "-y", "-i", str(bed),
            "-af", full, "-ac", "2", "-ar", str(AUDIO_SR),
            "-c:a", "pcm_s16le", str(out)]


def sfx_mix_command(cues: list[tuple[SoundCue, Path]], duration_s: float,
                    out: Path) -> list[str]:
    """Step 5: place each cue at its time, sum to full length. Pure."""
    args = ["ffmpeg", "-v", "error", "-y"]
    for _cue, path in cues:
        args += ["-i", str(path)]
    graphs, labels = [], []
    for i, (cue, _path) in enumerate(cues):
        d_ms = round(cue.at_s * 1000)
        graphs.append(
            f"[{i}:a]aresample={AUDIO_SR},aformat=channel_layouts=stereo,"
            f"atrim=0:{cue.duration_s:.3f},asetpts=N/SR/TB,"
            f"volume={cue.gain_db:.1f}dB,"
            f"afade=t=in:st=0:d=0.05,adelay={d_ms}|{d_ms}[s{i}]")
        labels.append(f"[s{i}]")
    graph = (";".join(graphs) + ";" + "".join(labels)
             + f"amix=inputs={len(cues)}:duration=longest:normalize=0,"
               f"apad,atrim=0:{duration_s:.3f}[out]")
    return args + ["-filter_complex", graph, "-map", "[out]",
                   "-ac", "2", "-ar", str(AUDIO_SR),
                   "-c:a", "pcm_s16le", str(out)]


def duck_mix_command(voice: Path, bed_mix: Path | None, sfx_mix: Path | None,
                     out: Path, *,
                     music_gain_db: float = MUSIC_GAIN_DB) -> list[str]:
    """Step 5b: duck the bed bus under the voice and sum. Pure.

    `music_gain_db` carries the taste-resolved bed level (default or
    low); effect gains were set in step 5 and the voice stays at unity.
    """
    if bed_mix is None and sfx_mix is None:
        return ["ffmpeg", "-v", "error", "-y", "-i", str(voice),
                "-af", f"aresample={AUDIO_SR},pan=stereo|c0=c0|c1=c0",
                "-c:a", "pcm_s16le", str(out)]
    duck = (f"sidechaincompress=threshold={DUCK_THRESHOLD}:ratio={DUCK_RATIO}"
            f":attack={DUCK_ATTACK_MS}:release={DUCK_RELEASE_MS}")
    tail = "amix=inputs=2:duration=first:normalize=0[out]"
    # The voice is mono and the stems are stereo; amix would upmix the
    # voice implicitly at −3 dB per channel, so it is explicitly
    # duplicated at unity first (measured; the sidechain key stays mono).
    head = "[0:a]asplit=2[vm][key];[vm]pan=stereo|c0=c0|c1=c0[v];"
    if bed_mix is not None and sfx_mix is not None:
        args = ["ffmpeg", "-v", "error", "-y", "-i", str(voice),
                "-i", str(bed_mix), "-i", str(sfx_mix)]
        graph = (f"{head}"
                 f"[1:a]volume={music_gain_db:.1f}dB[m];"
                 f"[m][2:a]amix=inputs=2:duration=longest:normalize=0[bed];"
                 f"[bed][key]{duck}[ducked];[v][ducked]{tail}")
    elif bed_mix is not None:
        args = ["ffmpeg", "-v", "error", "-y", "-i", str(voice),
                "-i", str(bed_mix)]
        graph = (f"{head}"
                 f"[1:a]volume={music_gain_db:.1f}dB[bed];"
                 f"[bed][key]{duck}[ducked];[v][ducked]{tail}")
    else:
        args = ["ffmpeg", "-v", "error", "-y", "-i", str(voice),
                "-i", str(sfx_mix)]
        graph = (f"{head}"
                 f"[1:a][key]{duck}[ducked];[v][ducked]{tail}")
    return args + ["-filter_complex", graph, "-map", "[out]",
                   "-ac", "2", "-ar", str(AUDIO_SR),
                   "-c:a", "pcm_s16le", str(out)]


def mux_command(video: Path, master_wav: Path, out: Path) -> list[str]:
    """Step 7: picture copied, mastered audio as AAC. Pure."""
    return ["ffmpeg", "-v", "error", "-y", "-i", str(video),
            "-i", str(master_wav),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(out)]


# ------------------------------------------------- Spark design (§11.4)


def design_schema() -> dict:
    """The strict finish_design schema. Pure."""
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "music": {"type": ["object", "null"], "additionalProperties": False,
                      "properties": {
                          "prompt": {"type": "string"},
                          "start_s": {"type": "number"},
                          "end_s": {"type": "number"},
                          "gain_db": {"type": "number"},
                          "dropouts": {"type": "array", "items": {
                              "type": "object", "additionalProperties": False,
                              "properties": {"start_s": {"type": "number"},
                                             "end_s": {"type": "number"}},
                              "required": ["start_s", "end_s"]}},
                          "reason": {"type": "string"},
                          "confidence": {"type": "number"}},
                      "required": ["prompt", "start_s", "end_s", "gain_db",
                                   "dropouts", "reason", "confidence"]},
            "effects": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string",
                             "enum": ["riser", "whoosh", "impact"]},
                    "at_s": {"type": "number"},
                    "duration_s": {"type": "number"},
                    "prompt": {"type": "string"},
                    "gain_db": {"type": "number"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "number"}},
                "required": ["kind", "at_s", "duration_s", "prompt",
                             "gain_db", "reason", "confidence"]}}},
        "required": ["music", "effects"],
    }


def _escape_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def design_prompt(*, duration_s: float, voice_stats: dict,
                  taste_entries: list[str], sections: list[dict],
                  shots: list[dict], gaps: list[tuple[float, float]],
                  words: list[tuple[float, float, str]],
                  forbids: list[str]) -> str:
    """The sound-design user prompt: numbers, not pictures. Pure."""
    rms = voice_stats.get("rms_db")
    peak = voice_stats.get("peak_db")
    if rms is None and peak is None:
        voice_line = "Voice (pre-master): loudness unknown"
    else:
        rms_text = "?" if rms is None else f"{rms:.1f} dB"
        peak_text = "?" if peak is None else f"{peak:.1f} dB"
        voice_line = f"Voice (pre-master): RMS {rms_text}, peak {peak_text}"
    lines = [f"Finish cut: {duration_s:.1f}s. {voice_line}."]
    lines.append("Taste:")
    if taste_entries:
        lines.extend(f"- {entry}" for entry in taste_entries)
    else:
        lines.append("(none)")
    lines.append("Sections:")
    for section in sections:
        lines.append(f"- {section.get('id')} \"{section.get('topic', '')}\" "
                     f"{float(section.get('start_s', 0.0)):.3f}-"
                     f"{float(section.get('end_s', 0.0)):.3f}")
    lines.append("Shots:")
    for shot in shots:
        lines.append(f"shot s{shot.get('id')} "
                     f"{float(shot.get('start_s', 0.0)):.3f}-"
                     f"{float(shot.get('end_s', 0.0)):.3f} "
                     f"cam-{shot.get('camera', '')}")
    lines.append("Gaps (>=0.30s):")
    if gaps:
        lines.extend(f"- {a:.3f}-{b:.3f}" for a, b in gaps)
    else:
        lines.append("(none)")
    lines.append("Words:")
    for start, end, text in words:
        lines.append(f"- {start:.3f}-{end:.3f} \"{_escape_text(text)}\"")
    if forbids:
        lines.append(f"forbidden_kinds: [{', '.join(forbids)}]")
    else:
        lines.append("forbidden_kinds: none")
    lines.append("Design the sound for this cut. JSON only.")
    return "\n".join(lines)


def _word_gaps(words: list[tuple[float, float, str]]) -> list[tuple[float, float]]:
    """Inter-word gaps ≥ 0.30 s, the 80 largest, in time order. Pure."""
    gaps = [(words[i][1], words[i + 1][0])
            for i in range(len(words) - 1)
            if words[i + 1][0] - words[i][1] >= 0.30]
    gaps.sort(key=lambda gap: gap[1] - gap[0], reverse=True)
    chosen = gaps[:80]
    chosen.sort()
    return chosen


def _truncate_prompt(prompt: str, limit: int = 400) -> str:
    """Word-boundary cut; a prompt with no space is cut hard. Pure."""
    if len(prompt) <= limit:
        return prompt
    cut = prompt[:limit]
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut


def verify_design(
    music: MusicPlan | None,
    cues: list[SoundCue],
    *,
    duration_s: float,
    words: list[tuple[float, float, str]],
    sections: list[dict],
    taste: TasteFinish,
) -> tuple[MusicPlan | None, list[SoundCue], list[str]]:
    """Mechanical rules overrule Spark, in §11.5 order. Pure.

    (sections is accepted for the designed signature; the rules read
    duration, words and taste.)
    """
    _ = sections
    notes: list[str] = []
    bed = music
    # 1. Schema-hygiene drops.
    if bed is not None:
        if (not math.isfinite(bed.start_s) or not math.isfinite(bed.end_s)
                or bed.end_s <= bed.start_s or not bed.prompt.strip()
                or not isinstance(bed.confidence, (int, float))
                or not 0.0 <= float(bed.confidence) <= 1.0):
            bed = None
            notes.append("finish_music_dropped:invalid_plan")
    survivors: list[SoundCue] = []
    for pos, cue in enumerate(cues, 1):
        if (cue.kind not in CUE_KINDS
                or not math.isfinite(cue.at_s)
                or not math.isfinite(cue.duration_s)
                or not cue.prompt.strip()
                or not isinstance(cue.confidence, (int, float))
                or not 0.0 <= float(cue.confidence) <= 1.0):
            notes.append(f"finish_cue_dropped:{pos}:invalid_cue")
            continue
        survivors.append(cue)
    # 2. Taste forbids.
    if taste.no_sound:
        notes.append("finish_sound_forbidden_by_taste")
        return None, [], notes
    if taste.no_music and bed is not None:
        bed = None
        notes.append("finish_music_forbidden_by_taste")
    if taste.forbid_kinds:
        kept = []
        for cue in survivors:
            if cue.kind in taste.forbid_kinds:
                notes.append(f"finish_cue_dropped:{cue.kind}:{cue.at_s:.3f}s"
                             ":forbidden_by_taste")
            else:
                kept.append(cue)
        survivors = kept
    # 3. Clamp.
    if bed is not None:
        start = max(0.0, bed.start_s)
        end = min(duration_s, bed.end_s)
        if end <= start:
            bed = None
            notes.append("finish_music_dropped:invalid_plan")
        else:
            cleaned: list[tuple[float, float]] = []
            for a, b in bed.dropouts:
                try:
                    lo, hi = max(start, float(a)), min(end, float(b))
                except (TypeError, ValueError):
                    continue
                if hi > lo and math.isfinite(lo) and math.isfinite(hi):
                    cleaned.append((lo, hi))
            cleaned.sort()
            merged: list[tuple[float, float]] = []
            for lo, hi in cleaned:
                if merged and lo <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
                else:
                    merged.append((lo, hi))
            bed.start_s = start
            bed.end_s = end
            bed.dropouts = merged
            bed.gain_db = (MUSIC_GAIN_LOW_DB if taste.music_low
                           else MUSIC_GAIN_DB)
    clamped_cues: list[SoundCue] = []
    for cue in survivors:
        cue.duration_s = min(30.0, max(0.5, cue.duration_s))
        cue.gain_db = min(SFX_GAIN_MAX_DB, max(SFX_GAIN_MIN_DB, cue.gain_db))
        cue.prompt = _truncate_prompt(cue.prompt)
        if not cue.prompt.strip():
            notes.append(f"finish_cue_dropped:{cue.kind}:{cue.at_s:.3f}s"
                         ":invalid_cue")
            continue
        clamped_cues.append(cue)
    survivors = clamped_cues
    # 4. Edge guard.
    if not taste.allow_edges:
        kept = []
        for cue in survivors:
            if cue.at_s < EDGE_GUARD_S or cue.at_s > duration_s - EDGE_GUARD_S:
                notes.append(f"finish_cue_dropped:{cue.kind}:{cue.at_s:.3f}s"
                             ":edge")
            else:
                kept.append(cue)
        survivors = kept
    # 5. Word placement.
    placed: list[SoundCue] = []
    for cue in survivors:
        holder = next((w for w in words if w[0] < cue.at_s < w[1]), None)
        if holder is not None:
            right = holder[1] + 0.05
            left = holder[0] - cue.duration_s
            options = sorted(((abs(right - cue.at_s), right),
                              (abs(left - cue.at_s), left)))
            moved = next((at for dist, at in options if dist <= 0.75), None)
            if moved is None or moved < 0 or moved > duration_s:
                notes.append(f"finish_cue_dropped:{cue.kind}:{cue.at_s:.3f}s"
                             ":word")
                continue
            notes.append(f"finish_cue_moved:{cue.kind}:{cue.at_s:.3f}s"
                         f"->{moved:.3f}s:word")
            cue.at_s = moved
        tail = cue.at_s + cue.duration_s
        if any(w[0] < tail and w[1] > cue.at_s for w in words):
            if cue.gain_db > WORD_OVERLAP_CAP_DB:
                cue.gain_db = WORD_OVERLAP_CAP_DB
                notes.append(f"finish_cue_capped:{cue.kind}:{cue.at_s:.3f}s"
                             f":{WORD_OVERLAP_CAP_DB:.1f}dB:words")
        placed.append(cue)
    survivors = placed
    # 6. Density.
    survivors.sort(key=lambda cue: cue.at_s)
    thinned: list[SoundCue] = []
    for cue in survivors:
        window = [kept for kept in thinned if cue.at_s - kept.at_s < 60.0]
        if len(window) >= MAX_SFX_PER_MINUTE:
            notes.append(f"finish_cue_dropped:{cue.kind}:{cue.at_s:.3f}s"
                         ":density")
            continue
        thinned.append(cue)
    survivors = thinned
    if len(survivors) > MAX_SFX_TOTAL:
        ranked = sorted(survivors,
                        key=lambda cue: (cue.confidence, -cue.at_s))
        dropped = ranked[:len(survivors) - MAX_SFX_TOTAL]
        drop_ids = {id(cue) for cue in dropped}
        for cue in sorted(dropped, key=lambda cue: cue.at_s):
            notes.append(f"finish_cue_dropped:{cue.kind}:{cue.at_s:.3f}s"
                         ":total_cap")
        survivors = [cue for cue in survivors if id(cue) not in drop_ids]
        survivors.sort(key=lambda cue: cue.at_s)
    # 7. Ids.
    for pos, cue in enumerate(survivors, 1):
        cue.id = f"sfx_{pos:03d}"
    if bed is not None and taste.music_low:
        notes.append("finish_music_low:taste")
    return bed, survivors, notes


def estimate_finish_usd(music: MusicPlan | None, cues: list[SoundCue], *,
                        isolate_s: float) -> float:
    """Provider estimate for the gate: isolation + bed + cues. Pure."""
    total = elevenlabs_mod.estimate_isolation_usd(isolate_s)
    if music is not None:
        total += elevenlabs_mod.estimate_music_usd(music.end_s - music.start_s)
    for cue in cues:
        total += elevenlabs_mod.estimate_sfx_usd(cue.duration_s)
    return round(total, 4)


def _coerce_plan(raw: object) -> tuple[MusicPlan | None, list[SoundCue]]:
    """A Spark reply dict to an unverified (plan, cues) pair. Pure.

    Coercion is total: anything malformed becomes values the hygiene
    step drops, never an exception.
    """
    if not isinstance(raw, dict):
        return None, []
    music_raw = raw.get("music")
    plan: MusicPlan | None = None
    if isinstance(music_raw, dict):
        try:
            start = float(music_raw.get("start_s", float("nan")))
            end = float(music_raw.get("end_s", float("nan")))
        except (TypeError, ValueError):
            start, end = float("nan"), float("nan")
        try:
            gain = float(music_raw.get("gain_db", MUSIC_GAIN_DB))
        except (TypeError, ValueError):
            gain = MUSIC_GAIN_DB
        try:
            confidence = float(music_raw.get("confidence", -1.0))
        except (TypeError, ValueError):
            confidence = -1.0
        dropouts: list[tuple[float, float]] = []
        for item in music_raw.get("dropouts", []) or []:
            if not isinstance(item, dict):
                continue
            try:
                dropouts.append((float(item["start_s"]), float(item["end_s"])))
            except (TypeError, ValueError, KeyError):
                continue
        plan = MusicPlan(prompt=str(music_raw.get("prompt", "")), start_s=start,
                         end_s=end, gain_db=gain, dropouts=dropouts,
                         reason=str(music_raw.get("reason", "")),
                         confidence=confidence)
    cues: list[SoundCue] = []
    effects = raw.get("effects", [])
    if not isinstance(effects, list):
        effects = []
    for item in effects:
        if not isinstance(item, dict):
            cues.append(SoundCue("", "", float("nan"), float("nan"), "",
                                 0.0, "", -1.0))
            continue
        try:
            at = float(item.get("at_s", float("nan")))
        except (TypeError, ValueError):
            at = float("nan")
        try:
            duration = float(item.get("duration_s", float("nan")))
        except (TypeError, ValueError):
            duration = float("nan")
        try:
            gain_cue = float(item.get("gain_db", SFX_GAIN_MIN_DB))
        except (TypeError, ValueError):
            gain_cue = SFX_GAIN_MIN_DB
        try:
            conf = float(item.get("confidence", -1.0))
        except (TypeError, ValueError):
            conf = -1.0
        cues.append(SoundCue(
            "", str(item.get("kind", "")), at, duration,
            str(item.get("prompt", "")), gain_cue,
            str(item.get("reason", "")), conf))
    return plan, cues


def design_with_muse(*, duration_s: float, voice_stats: dict,
                     taste_entries: list[str], sections: list[dict],
                     shots: list[dict], words: list[tuple[float, float, str]],
                     taste: TasteFinish, model: str, max_usd: float,
                     job_name: str
                     ) -> tuple[MusicPlan | None, list[SoundCue], float, bool,
                                list[str]]:
    """The one Spark sound-design call plus §11.5 verification.

    Returns (music, cues, meta_usd, cached, notes). Raises
    DesignUnavailable only on ImportError; every other Meta failure
    degrades to a finish_design_unavailable note with no generation.
    """
    prompt = design_prompt(
        duration_s=duration_s, voice_stats=voice_stats,
        taste_entries=taste_entries, sections=sections, shots=shots,
        gaps=_word_gaps(words), words=words,
        forbids=sorted(taste.forbid_kinds))
    try:
        from guestlist.meta import cache, spark
    except ImportError as error:
        raise DesignUnavailable(str(error)) from error
    schema = design_schema()
    key = cache.key("finish", model, DEVELOPER, prompt, schema, "medium")
    hit = cache.get("spark", key)
    prefix: list[str] = []
    if hit:
        data = hit["data"]
        spend = 0.0
        cached = True
    else:
        rates = SPARK_TOKEN_RATES_USD_PER_1M.get(model)
        if rates is None:
            rates = SPARK_TOKEN_RATES_USD_PER_1M["muse-spark-1.3"]
            prefix.append(f"finish_estimate_default_rates:{model}")
        in_rate, out_rate = rates
        estimate = (len(DEVELOPER + prompt) / 4 * in_rate / 1e6
                    + DESIGN_MAX_OUTPUT_TOKENS * out_rate / 1e6)
        if estimate > max_usd:
            return (None, [], 0.0, False,
                    prefix + ["finish_design_unavailable:exceeds --max-usd"])
        try:
            cache.guard(0.25)
            reply = spark._structured(
                key, [{"type": "input_text", "text": prompt}],
                schema=schema, name="finish_design",
                developer=DEVELOPER, model=model, effort="medium",
                max_output_tokens=DESIGN_MAX_OUTPUT_TOKENS,
                record={}, detail={"job": job_name})
        except Exception as error:
            cause = f"{type(error).__name__}: {error}"[:160]
            return (None, [], 0.0, False,
                    prefix + [f"finish_design_unavailable:{cause}"])
        data = reply.data
        try:
            spend = round(float(reply.usd or 0.0), 4)
        except (TypeError, ValueError):
            spend = 0.0
        cached = False
    if not isinstance(data, dict):
        return (None, [], spend, cached,
                prefix + ["finish_design_unavailable:invalid reply"])
    plan, cues = _coerce_plan(data)
    bed, placed, notes = verify_design(
        plan, cues, duration_s=duration_s, words=words,
        sections=sections, taste=taste)
    if any("no ducking" in entry.lower() for entry in taste_entries):
        notes.append("finish_taste_ignored_ducking")
    return bed, placed, spend, cached, prefix + notes


# ---------------------------------------------------- internal helpers (§4)


def _voice_stats(voice_wav: Path) -> dict:
    """RMS/peak dB of the extracted voice, for the Spark prompt.

    From `volumedetect` stderr; unparseable or missing values are None.
    Never raises.
    """
    out: dict = {"rms_db": None, "peak_db": None}
    try:
        proc = run_command(
            ["ffmpeg", "-v", "info", "-i", str(voice_wav),
             "-af", "volumedetect", "-f", "null", "-"],
            check=False)
    except OSError:
        return out
    stderr = proc.stderr or ""
    for key, label in (("rms_db", "mean_volume"), ("peak_db", "max_volume")):
        match = re.search(rf"{label}:\s*([-\d.]+|n/a|-inf)\s*dB", stderr)
        if not match:
            continue
        try:
            out[key] = float(match.group(1))
        except ValueError:
            continue
    return out


def _licence(provider_display: str, model: str) -> str:
    return (f"Generated with {provider_display} ({model}) on "
            f"{date.today().isoformat()}; use is subject to "
            f"{provider_display}'s terms for your plan.")


def _probe_audio(wav_or_mp3: Path) -> float | None:
    """Probed audio duration, or None when it will not probe as audio."""
    try:
        stats = extract_media_stats(probe_media(Path(wav_or_mp3)))
    except (ToolingError, OSError, ValueError):
        return None
    if not stats.get("has_audio"):
        return None
    duration = float(stats.get("duration_s") or 0.0)
    return duration if duration > 0 else None


def _download_asset(*, work_dir: Path, asset_id: str, kind: str,
                    filename: str, provider_display: str, model: str,
                    prompt: str, data: bytes, requested_s: float,
                    estimator) -> tuple[FinishAsset | None, list[str]]:
    """Write provider bytes, probe, and build the asset (or skip it)."""
    notes: list[str] = []
    target = work_dir / filename
    target.write_bytes(bytes(data))
    probed = _probe_audio(target)
    if probed is None:
        try:
            target.unlink()
        except OSError:
            pass
        notes.append(f"finish_cue_skipped:{asset_id}:empty audio")
        return None, notes
    if abs(probed - requested_s) > 0.5:
        notes.append(f"finish_asset_duration:{asset_id}:{probed:.3f}s")
    return FinishAsset(
        id=asset_id, kind=kind, file=filename, provider="elevenlabs",
        model=model, prompt=prompt, duration_s=probed,
        cost_usd=estimator(probed), licence=_licence(provider_display, model),
    ), notes


def _replace_master(video: Path, master_wav: Path, work_dir: Path,
                    expected_s: float) -> None:
    """Mux to a temp file, probe it, atomically replace the video."""
    tmp = work_dir / "cut_finished.mp4"
    try:
        run_command(mux_command(video, master_wav, tmp))
    except ToolingError as error:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise FinishError("finish: mastered audio could not be muxed; "
                          "cut.mp4 is unchanged") from error
    try:
        stats = extract_media_stats(probe_media(tmp))
    except (ToolingError, OSError, ValueError) as error:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise FinishError("finish: mastered audio could not be muxed; "
                          "cut.mp4 is unchanged") from error
    duration = float(stats.get("duration_s") or 0.0)
    if (not stats.get("has_audio") or int(stats.get("width") or 0) <= 0
            or abs(duration - expected_s) > 0.1):
        try:
            tmp.unlink()
        except OSError:
            pass
        raise FinishError("finish: mastered audio could not be muxed; "
                          "cut.mp4 is unchanged")
    os.replace(tmp, video)


# ------------------------------------------------------------- loading


def _load_angles(path: Path) -> dict:
    try:
        data = read_json(Path(path))
    except (OSError, ValueError) as error:
        raise FinishError(f"finish: not an angles.json: {path}") from error
    if (not isinstance(data, dict) or data.get("version") != 1
            or not isinstance(data.get("shots"), list)):
        raise FinishError(f"finish: not an angles.json: {path}")
    return data


def _load_tighten(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        return cutlist.load_tighten(Path(path))
    except (OSError, ValueError) as error:
        raise FinishError(f"finish: not a tighten.json: {path}") from error


def _load_words(words_path: Path | None,
                tighten_payload: dict | None) -> list[angles_mod.TimedWord]:
    """Explicit words, else one word per tighten utterance, else []."""
    if words_path is not None:
        return angles_mod.load_timed_words(Path(words_path))
    if tighten_payload is not None:
        out: list[angles_mod.TimedWord] = []
        for entry in tighten_payload.get("utterances", []) or []:
            if not isinstance(entry, dict):
                continue
            try:
                start = float(entry.get("start_s", 0.0))
                end = float(entry.get("end_s", 0.0))
            except (TypeError, ValueError):
                continue
            text = str(entry.get("text", "")).strip()
            if end <= start or not text:
                continue
            out.append(angles_mod.TimedWord(text, start, end))
        out.sort(key=lambda word: word.start_s)
        return out
    return []


def _load_taste_entries(taste_path: Path | None) -> tuple[list[str], bool]:
    """(entry texts, unreadable). Missing reads as empty, never an error."""
    if taste_path is None or not Path(taste_path).exists():
        return [], False
    try:
        data = read_json(Path(taste_path))
    except (OSError, ValueError):
        return [], True
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return [], True
    entries = [str(entry.get("text", ""))
               for entry in data["entries"]
               if isinstance(entry, dict) and str(entry.get("text", "")).strip()]
    return entries, False


def _elevenlabs_key() -> str | None:
    """The ElevenLabs key, or None when unconnected. Never logged."""
    try:
        status = connect_mod.key_status("elevenlabs")
    except connect_mod.ConnectError:
        return None
    if not status.connected:
        return None
    parts = connect_mod.get_provider_key("elevenlabs")
    if not parts:
        return None
    return parts[0]


def _provider_category(error: elevenlabs_mod.ElevenLabsError) -> str:
    """The §7.6 mapped category for a generation failure."""
    status = getattr(error, "status", None)
    if status is None:
        if "empty audio" in str(error):
            return "empty audio"
        return "elevenlabs unavailable"
    if status in (400, 422) or 400 <= status < 500 and status not in (401, 403, 402, 429):
        return "elevenlabs rejected the request"
    return "elevenlabs unavailable"


def _run_step(command: list[str], step: str) -> None:
    try:
        run_command(command)
    except ToolingError as error:
        raise FinishError(f"finish: {step} failed ({error})") from error


# ------------------------------------------------------- the pipeline (§11)


def run_files(*, video_path: Path, angles_path: Path,
              tighten_path: Path | None, words_path: Path | None,
              taste_path: Path | None, out_path: Path,
              model: str, no_muse: bool, sound: bool, isolate: bool,
              loudness: float | None, max_provider_usd: float,
              max_usd: float, deplosive: bool = True) -> dict:
    """Finish one cut: clean → design → generate → mix → master → remux.

    `out_path` is the finish.json path; the finish/ work dir is its
    sibling. Returns the finish.json payload (also written). The mic-air
    de-plosive is on by default; pass `deplosive=False` to skip it.
    """
    if loudness is not None and not -20.0 <= float(loudness) <= -10.0:
        raise ValueError("--loudness must be between -20 and -10")
    if max_provider_usd < 0:
        raise ValueError("--max-provider-usd must be >= 0")
    if max_usd < 0:
        raise ValueError("--max-usd must be >= 0")
    video_path, angles_path, out_path = (
        Path(video_path), Path(angles_path), Path(out_path))
    angles_payload = _load_angles(angles_path)
    tighten_payload = _load_tighten(Path(tighten_path) if tighten_path else None)
    words = _load_words(Path(words_path) if words_path else None, tighten_payload)
    taste_entries, taste_unreadable = _load_taste_entries(
        Path(taste_path) if taste_path else None)
    notes: list[str] = []
    warnings: list[str] = []
    if taste_unreadable:
        notes.append("finish_taste_unreadable")
    taste = parse_taste(taste_entries)
    target = (float(loudness) if loudness is not None
              else (taste.loudness if taste.loudness is not None
                    else TARGET_LUFS_DEFAULT))

    try:
        stats = extract_media_stats(probe_media(video_path))
    except (ToolingError, OSError, ValueError) as error:
        raise FinishError(f"finish: extract failed ({error})") from error
    if not stats.get("has_audio"):
        raise FinishError("finish: cut.mp4 has no audio stream to finish")
    duration_s = float(stats.get("duration_s") or 0.0)

    work_dir = out_path.parent / "finish"
    work_dir.mkdir(parents=True, exist_ok=True)
    voice_raw = work_dir / "voice_raw.wav"
    voice_clean = work_dir / "voice_clean.wav"

    # Step 1 — extract the voice.
    _run_step(extract_voice_command(video_path, voice_raw), "extract")
    voice_stats = _voice_stats(voice_raw)
    # De-plosive threshold, measured per recording on the extracted
    # voice (the isolated voice, when asked for, is cleaned with the
    # same threshold: isolation runs before the de-plosive).
    if deplosive:
        deplosive_db, deplosive_moments, deplosive_fallback = (
            measure_deplosive_threshold(voice_raw))
        deplosive_linear: float | None = dbfs_to_linear(deplosive_db)
        if deplosive_fallback:
            notes.append("finish_deplosive_fallback:fixed -36.0 dBFS")
    else:
        deplosive_db, deplosive_moments, deplosive_fallback = None, 0, False
        deplosive_linear = None
    # Step 2 — the local clean is always rendered first, so the
    # isolation fallback is a file that already exists.
    _run_step(clean_voice_command(voice_raw, voice_clean, isolated=False,
                                  deplosive_threshold=deplosive_linear),
              "clean")

    provider_key = _elevenlabs_key()
    assets: list[FinishAsset] = []
    provider_spent = 0.0
    provider_dead = False
    cap_note = f"finish_provider_cap_reached:${max_provider_usd:.2f}"

    def _cap_allows(estimate: float) -> bool:
        return provider_spent + estimate <= max_provider_usd

    # Step 2 (isolation half): extract → isolate → decode-clean.
    method = "local"
    isolate_requested = bool(isolate or taste.isolate)
    if isolate_requested:
        if provider_key is None:
            notes.append("finish_isolate_unavailable:elevenlabs not connected")
        elif duration_s > ISOLATION_MAX_S:
            notes.append(f"finish_isolate_skipped_long:{int(duration_s)}s")
        else:
            estimate = elevenlabs_mod.estimate_isolation_usd(duration_s)
            if not _cap_allows(estimate):
                notes.append(cap_note)
            else:
                try:
                    generated = elevenlabs_mod.isolate_audio(
                        key=provider_key, wav_path=voice_raw)
                except elevenlabs_mod.ElevenLabsError as error:
                    status = getattr(error, "status", None)
                    if status in (401, 403):
                        notes.append("finish_provider_auth_failed:elevenlabs")
                        provider_dead = True
                    elif status in (402, 429):
                        notes.append("finish_provider_quota:elevenlabs")
                        provider_dead = True
                    else:
                        notes.append("finish_isolate_fallback:local "
                                     f"({_provider_category(error)})")
                else:
                    asset, asset_notes = _download_asset(
                        work_dir=work_dir, asset_id="voice_isolation",
                        kind="voice_isolation", filename="voice_isolated.mp3",
                        provider_display="ElevenLabs", model="audio-isolation",
                        prompt="", data=generated.data,
                        requested_s=duration_s,
                        estimator=elevenlabs_mod.estimate_isolation_usd)
                    notes.extend(asset_notes)
                    if asset is None:
                        notes.append("finish_isolate_fallback:local "
                                     "(empty audio)")
                    else:
                        isolated_clean = work_dir / "voice_clean_iso.wav"
                        try:
                            run_command(clean_voice_command(
                                work_dir / "voice_isolated.mp3",
                                isolated_clean, isolated=True,
                                deplosive_threshold=deplosive_linear))
                        except ToolingError:
                            try:
                                isolated_clean.unlink()
                            except OSError:
                                pass
                            notes.append("finish_isolate_fallback:local "
                                         "(could not decode)")
                        else:
                            os.replace(isolated_clean, voice_clean)
                            assets.append(asset)
                            provider_spent = round(
                                provider_spent + asset.cost_usd, 4)
                            method = "isolated"
                            notes.append("finish_voice_isolated:elevenlabs")
    clean_chain = ("highpass+compressor" if method == "isolated"
                   else "highpass+afftdn+compressor")

    # Sound design gate (§11.1, §11.4).
    intervals = [(s, e, t) for s, e, t in word_intervals(
        angles_payload, tighten_payload, words) if s < duration_s]
    sections = beat_sections(angles_payload, tighten_payload, words)
    origin = finish_origin(angles_payload)
    finish_shots = []
    for shot in angles_payload.get("shots", []) or []:
        try:
            start = float(shot.get("start_s", 0.0)) - origin
            end = float(shot.get("end_s", start)) - origin
        except (TypeError, ValueError):
            continue
        finish_shots.append({"id": shot.get("id"),
                             "start_s": round(max(0.0, start), 3),
                             "end_s": round(max(0.0, end), 3),
                             "camera": str(shot.get("camera", ""))})
    sound_requested = bool(sound or _taste_asks_sound(taste_entries))
    design_record: dict | None = None
    bed: MusicPlan | None = None
    cues: list[SoundCue] = []
    if sound_requested:
        if taste.no_sound:
            notes.append("finish_sound_forbidden_by_taste")
        elif provider_key is None:
            notes.append("finish_sound_off_no_provider")
        elif no_muse:
            notes.append("finish_design_unavailable:guestlist not installed "
                         '(pip install -e ".[muse]")')
        else:
            try:
                bed, cues, meta_usd, cached, design_notes = design_with_muse(
                    duration_s=duration_s, voice_stats=voice_stats,
                    taste_entries=taste_entries, sections=sections,
                    shots=finish_shots, words=intervals, taste=taste,
                    model=model, max_usd=max_usd,
                    job_name=out_path.parent.name)
            except DesignUnavailable as error:
                cause = str(error) or "guestlist not installed"
                notes.append(f"finish_design_unavailable:{cause}")
            else:
                notes.extend(design_notes)
                design_record = {"model": model, "cached": cached,
                                 "meta_usd": round(meta_usd, 4),
                                 "taste": list(taste_entries)}

    # Step 3 — generate (surviving, gated cues): bed, then cues in order.
    bed_asset: FinishAsset | None = None
    cue_assets: dict[str, FinishAsset] = {}

    def _generate(label: str, prompt_text: str, length_s: float,
                  filename: str, kind: str, model_name: str,
                  estimator, caller) -> FinishAsset | None:
        estimate = estimator(length_s)
        if not _cap_allows(estimate):
            notes.append(cap_note)
            return "cap"  # type: ignore[return-value]
        assert provider_key is not None
        try:
            generated = caller(key=provider_key, prompt=prompt_text,
                               duration_s=length_s)
        except elevenlabs_mod.ElevenLabsError as error:
            status = getattr(error, "status", None)
            if status in (401, 403):
                notes.append("finish_provider_auth_failed:elevenlabs")
                return "stop"  # type: ignore[return-value]
            if status in (402, 429):
                notes.append("finish_provider_quota:elevenlabs")
                return "stop"  # type: ignore[return-value]
            if getattr(error, "status", None) is None and "empty audio" in str(error):
                notes.append(f"finish_cue_skipped:{label}:empty audio")
                return None
            if status is None or status >= 500:
                time.sleep(2)
                try:
                    generated = caller(key=provider_key, prompt=prompt_text,
                                       duration_s=length_s)
                except elevenlabs_mod.ElevenLabsError as retry_error:
                    retry_status = getattr(retry_error, "status", None)
                    if retry_status in (401, 403):
                        notes.append("finish_provider_auth_failed:elevenlabs")
                        return "stop"  # type: ignore[return-value]
                    if retry_status in (402, 429):
                        notes.append("finish_provider_quota:elevenlabs")
                        return "stop"  # type: ignore[return-value]
                    notes.append(f"finish_cue_skipped:{label}:"
                                 "elevenlabs unavailable")
                    return None
            else:
                notes.append(f"finish_cue_skipped:{label}:"
                             "elevenlabs rejected the request")
                return None
        asset, asset_notes = _download_asset(
            work_dir=work_dir, asset_id=label, kind=kind, filename=filename,
            provider_display="ElevenLabs", model=model_name,
            prompt=prompt_text, data=generated.data, requested_s=length_s,
            estimator=estimator)
        notes.extend(asset_notes)
        return asset

    if design_record is not None and not provider_dead and provider_key is not None:
        halted = False
        if bed is not None:
            span = bed.end_s - bed.start_s
            result = _generate(
                "music", bed.prompt, span, "music_bed.mp3", "music",
                elevenlabs_mod.MUSIC_MODEL,
                elevenlabs_mod.estimate_music_usd,
                elevenlabs_mod.generate_music)
            if result == "cap" or result == "stop":
                halted = True
            elif isinstance(result, FinishAsset):
                bed_asset = result
                assets.append(result)
                provider_spent = round(provider_spent + result.cost_usd, 4)
        if not halted:
            for cue in cues:
                result = _generate(
                    cue.id, cue.prompt, cue.duration_s,
                    f"{cue.id}.mp3", cue.kind, elevenlabs_mod.SFX_MODEL,
                    elevenlabs_mod.estimate_sfx_usd,
                    elevenlabs_mod.generate_sound_effect)
                if result == "cap" or result == "stop":
                    break
                if isinstance(result, FinishAsset):
                    cue_assets[cue.id] = result
                    cue.duration_s = result.duration_s
                    assets.append(result)
                    provider_spent = round(provider_spent + result.cost_usd, 4)

    # Steps 4–5b — render the bed/cue timelines, duck and sum.
    bed_mix: Path | None = None
    sfx_mix: Path | None = None
    if bed is not None and bed_asset is not None:
        bed_mix = work_dir / "music_mix.wav"
        _run_step(music_mix_command(work_dir / "music_bed.mp3", duration_s,
                                    bed, bed_mix), "music mix")
    rendered_cues = [(cue, work_dir / f"{cue.id}.mp3") for cue in cues
                     if cue.id in cue_assets]
    if rendered_cues:
        sfx_mix = work_dir / "sfx_mix.wav"
        _run_step(sfx_mix_command(rendered_cues, duration_s, sfx_mix),
                  "effects mix")
    premaster = work_dir / "mix_premaster.wav"
    music_gain = MUSIC_GAIN_LOW_DB if taste.music_low else MUSIC_GAIN_DB
    _run_step(duck_mix_command(voice_clean, bed_mix, sfx_mix, premaster,
                               music_gain_db=music_gain), "duck mix")

    # Step 6 — two-pass master, then the gentle limiter.
    master = work_dir / "master.wav"
    unlimited = work_dir / "master_unlimited.wav"
    measure = run_command(loudnorm_measure_command(premaster, target),
                          check=False)
    measured = parse_loudnorm_full(measure.stderr or "")
    loudness_before: dict | None = None
    if measured is None:
        fallback = ["ffmpeg", "-v", "error", "-y", "-i", str(premaster),
                    "-af",
                    f"loudnorm=I={target}:TP={TARGET_TP_DBTP}:LRA={TARGET_LRA}",
                    "-ac", "2", "-ar", str(AUDIO_SR),
                    "-c:a", "pcm_s16le", str(unlimited)]
        try:
            run_command(fallback)
        except ToolingError as error:
            raise FinishError("finish: could not measure loudness for "
                              "mastering") from error
    else:
        loudness_before = {"integrated_lufs": round(measured["input_i"], 2),
                           "true_peak_dbtp": round(measured["input_tp"], 2)}
        _run_step(loudnorm_apply_command(premaster, unlimited,
                                         target_lufs=target,
                                         measured=measured), "master")
    try:
        run_command(limiter_command(unlimited, master))
    except ToolingError:
        try:
            os.replace(unlimited, master)
        except OSError as error:
            raise FinishError("finish: master failed "
                              f"({error})") from error
        warnings.append("finish_limiter_failed")
    else:
        try:
            unlimited.unlink()
        except OSError:
            pass
    remeasure = run_command(loudnorm_measure_command(master, target),
                            check=False)
    remeasured = parse_loudnorm_full(remeasure.stderr or "")
    loudness_after: dict | None = None
    if remeasured is not None:
        loudness_after = {
            "integrated_lufs": round(remeasured["input_i"], 2),
            "true_peak_dbtp": round(remeasured["input_tp"], 2)}
        if abs(remeasured["input_i"] - target) > 1.0:
            notes.append(f"finish_loudness_off_target:"
                         f"{remeasured['input_i']:.1f} LUFS")
    else:
        warnings.append("finish_loudness_unmeasured")

    # Step 7 — remux (the only mutation of the video).
    _replace_master(video_path, master, work_dir, duration_s)

    if bed is not None:
        plan_music = {"prompt": bed.prompt,
                      "start_s": round(bed.start_s, 3),
                      "end_s": round(bed.end_s, 3),
                      "gain_db": bed.gain_db,
                      "dropouts": [{"start_s": round(a, 3),
                                    "end_s": round(b, 3)}
                                   for a, b in bed.dropouts],
                      "reason": bed.reason, "confidence": bed.confidence}
    else:
        plan_music = None
    if deplosive:
        deplosive_record: dict = {
            "enabled": True,
            "threshold_dbfs": deplosive_db,
            "moments": deplosive_moments,
            "fallback": deplosive_fallback,
        }
    else:
        deplosive_record = {
            "enabled": False,
            "threshold_dbfs": None,
            "moments": 0,
            "fallback": False,
        }
    payload = {
        "version": 1,
        "video": video_path.name,
        "duration_s": round(duration_s, 3),
        "voice": {"method": method, "clean_chain": clean_chain,
                  "isolate_requested": isolate_requested},
        "deplosive": deplosive_record,
        "provider_costs_are_estimates": True,
        "target_lufs": target,
        "loudness_before": loudness_before,
        "loudness_after": loudness_after,
        "design": design_record,
        "plan": {
            "music": plan_music,
            "effects": [{"id": cue.id, "kind": cue.kind,
                         "at_s": round(cue.at_s, 3),
                         "duration_s": round(cue.duration_s, 3),
                         "prompt": cue.prompt, "gain_db": cue.gain_db,
                         "reason": cue.reason, "confidence": cue.confidence}
                        for cue in cues],
        },
        "assets": [{"id": asset.id, "kind": asset.kind, "file": asset.file,
                    "provider": asset.provider, "model": asset.model,
                    "prompt": asset.prompt,
                    "duration_s": round(asset.duration_s, 3),
                    "cost_usd": round(asset.cost_usd, 4),
                    "licence": asset.licence}
                   for asset in assets],
        "provider_spend_usd": round(provider_spent, 4),
        "notes": notes,
        "warnings": warnings,
    }
    write_json(out_path, payload)
    return payload


def run(*, job_dir: Path, model: str, no_muse: bool, sound: bool,
        isolate: bool, loudness: float | None, max_provider_usd: float,
        max_usd: float, deplosive: bool = True) -> dict:
    """Finish a job directory. Returns the finish.json payload (written)."""
    job_dir = Path(job_dir)
    video = job_dir / "cut.mp4"
    angles = job_dir / "angles.json"
    if not video.exists():
        raise FinishError(f"finish: no cut.mp4 in {job_dir} "
                          "(run director cut or angles first)")
    if not angles.exists():
        raise FinishError(f"finish: no angles.json in {job_dir} "
                          "(run director cut or angles first)")
    tighten = job_dir / "tighten.json"
    words = job_dir / "words.json"
    taste = job_dir / "taste.json"
    return run_files(
        video_path=video, angles_path=angles,
        tighten_path=tighten if tighten.exists() else None,
        words_path=words if words.exists() else None,
        taste_path=taste if taste.exists() else None,
        out_path=job_dir / "finish.json", model=model, no_muse=no_muse,
        sound=sound, isolate=isolate, loudness=loudness,
        max_provider_usd=max_provider_usd, max_usd=max_usd,
        deplosive=deplosive)
