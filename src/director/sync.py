"""Multicam sync: place every clip on one shared timeline.

Each clip started at a different moment, so `director sync` finds where every
clip sits on one timeline and writes `sync.json`. Several methods each estimate
a clip's offset (timecode, capture clock, waveform cross-correlation, lip
movement, light-flash edges, Whisper word matches), then they are checked against each
other, because any single method can be confidently wrong. Convention: a clip's
`offset_s` is where its first sample sits on the timeline, so clip time `t` is
timeline time `t + offset_s`; the reference clip sits at 0.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ghostcut.config import write_json
from ghostcut.ffmpeg_tools import (
    ToolingError,
    coerce_float,
    parse_fraction,
    run_command,
    snap_frame_rate,
)

try:
    import numpy as np
except ImportError:  # the director extra is optional
    np = None  # type: ignore[assignment]

METHODS = ("timecode", "clock", "waveform", "lips", "flash", "whisper")
PRECISION_S = {"timecode": 0.0, "clock": 2.0, "waveform": 0.002,
               "lips": 1.0 / 30, "flash": 1.0 / 30, "whisper": 0.15}
CONFIDENCE = {"timecode": 0.5, "clock": 0.2}

SAMPLE_RATE = 8000
BAND_HZ = (200, 3400)
COARSE_HOP_S = 0.010
REFINE_HOP_S = 0.001
LOG_EPS = 1e-4
MIN_OVERLAP_FLOOR_S = 10.0
R_CLEAR = 0.45
MARGIN_CLEAR = 0.08
SECOND_PEAK_EXCLUSION_S = 1.0
GUIDE_EXTRA_S = 0.5
GUIDE_R_TOLERANCE = 0.02
# mlx_whisper's own default is whisper-tiny, too loose for word edges that place cuts.
WHISPER_MODEL = os.environ.get("DIRECTOR_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
# On long recordings with silent stretches (a recorder rolling before the cameras) Whisper invents
# "Thank you." in the silence and then loops on its own text. Measured 2026-11-03 on a 28-minute lav
# file with a 15-minute silent lead-in: 70 segments, 45 invented; with these, 2,361 real words.
WHISPER_GUARD_ARGS = ("--condition-on-previous-text", "False",
                      "--hallucination-silence-threshold", "2")
WHISPER_PHRASE_WORDS = 3
WHISPER_CLUSTER_S = 0.3
WHISPER_MIN_MOMENTS = 2
WHISPER_MIN_CONFIDENCE = 0.6
DRIFT_WINDOW_S = 20.0
DRIFT_MIN_WINDOWS = 3
DRIFT_WINDOW_MIN_R = 0.30
DRIFT_SEARCH_S = 0.25
DAY_S = 86400.0
FLASH_WINDOW_S = 120.0
FLASH_WIDTH = 64
FLASH_HEIGHT = 64
FLASH_MIN_Z = 8.0
FLASH_MIN_STEP = 10.0
FLASH_MIN_GAP_S = 0.25
FLASH_GAP_MARGIN_S = 0.05
# Lip sync, measured 2026-11-03 on real footage (A7S III front, Mavic 3 at 45
# degrees, lav recorder): inner-lip opening per frame from Apple Vision,
# prepped like the audio and correlated over the whole overlap.
LIPS_SPAN_S = 300.0
LIPS_RATIO_LOW = 1.5
LIPS_RATIO_GOOD = 2.0
LIPS_RIVAL_EXCLUSION_S = 0.5
LIPS_TIE_TOLERANCE = 0.02
LIPS_MIN_OVERLAP_S = 60.0
LIPS_SMOOTH_FRAMES = 3
LIPS_AUDIO_BAND_HZ = (300, 3000)
LIPS_AUDIO_RATE = 16000
LIPS_COARSE_FPS = 2.0
LIPS_COARSE_WIDTH = 320
LIPS_CROP_MARGIN = 0.5
LIPS_FACE_TARGET_PX = 400.0
LIPS_HELPER_NAME = "lips-helper"
LIPS_CACHE_DIRNAME = "ghostcut-director"


class SyncError(RuntimeError):
    """A sync step failed (unprobeable clip, no usable streams)."""


@dataclass(slots=True)
class CameraInfo:
    label: str
    path: str
    make: str = ""
    model: str = ""
    lens: str = ""
    fps: float = 0.0
    width: int = 0
    height: int = 0
    duration_s: float = 0.0
    has_audio: bool = False
    gamma: str = ""
    timecode: str | None = None
    tc_rate: float | None = None
    captured_at: str | None = None


@dataclass(slots=True)
class Estimate:
    method: str
    offset_s: float
    confidence: float
    precision_s: float
    against: str
    detail: dict = field(default_factory=dict)


@dataclass(slots=True)
class EstimateCheck:
    a: str
    b: str
    against_a: str
    against_b: str
    delta_s: float
    threshold_s: float
    agree: bool


@dataclass(slots=True)
class ClipResult:
    label: str
    camera: CameraInfo
    offset_s: float | None
    method: str | None
    estimates: list[Estimate] = field(default_factory=list)
    checks: list[EstimateCheck] = field(default_factory=list)
    drift_ppm: float | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SonySidecar:
    gamma: str = ""
    captured_raw: str | None = None
    tc_fps: float | None = None
    timecode: str | None = None


@dataclass(slots=True)
class TranscriptWord:
    word: str
    start_s: float
    end_s: float


@dataclass(slots=True)
class WaveformPeak:
    offset_s: float
    r: float
    second_r: float
    margin: float
    overlap_s: float
    clear: bool


@dataclass(slots=True)
class FlashEdge:
    time_s: float
    sign: int
    magnitude: float


@dataclass(slots=True)
class LipsFrame:
    index: int
    opening: float | None
    box: tuple[float, float, float, float] | None


@dataclass(slots=True)
class LipsTrack:
    times: list[float]
    opening: list[float]
    fps: float
    face_frac: float


@dataclass(slots=True)
class LipsLag:
    relative_s: float
    r: float
    rival_r: float
    ratio: float
    overlap_s: float


def _label_for_index(i: int) -> str:
    """A, …, Z, AA, AB, … for the i-th non-reference clip (0-based)."""
    s = ""
    n = i
    while True:
        s = chr(65 + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            return s


# ---------------------------------------------------------------- probe


def probe_clip(path: Path) -> dict:
    """ffprobe format duration, stream kinds/rates/tags; SyncError if unusable."""
    result = run_command(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=index,codec_type,codec_name,width,height,"
         "r_frame_rate,avg_frame_rate,sample_rate,channels,color_transfer"
         ":stream_tags:format_tags",
         "-of", "json", str(path)]
    )
    try:
        probe = json.loads(result.stdout)
    except ValueError:
        raise SyncError(f"no usable streams: {path}")
    streams = probe.get("streams") or []
    kinds = {s.get("codec_type") for s in streams if isinstance(s, dict)}
    if "video" not in kinds and "audio" not in kinds:
        raise SyncError(f"no usable streams: {path}")
    return probe


def _merged_tags(streams: list[dict], format_tags: dict, skip: int) -> list[dict]:
    """Tag dicts in search order: the video stream, format, then other streams."""
    ordered = [s for s in streams if s.get("codec_type") == "video"]
    rest = [s for s in streams if s.get("codec_type") != "video"]
    scopes: list[dict] = []
    for scope in ordered[:1]:
        tags = scope.get("tags")
        if isinstance(tags, dict):
            scopes.append({str(k).lower(): v for k, v in tags.items()})
    if isinstance(format_tags, dict):
        scopes.append({str(k).lower(): v for k, v in format_tags.items()})
    for scope in rest[skip:]:
        tags = scope.get("tags")
        if isinstance(tags, dict):
            scopes.append({str(k).lower(): v for k, v in tags.items()})
    return scopes


def _first_tag(scopes: list[dict], *names: str) -> str | None:
    for scope in scopes:
        for name in names:
            value = scope.get(name)
            if value is not None and str(value).strip():
                return str(value).strip()
    return None


def map_gamma(value: str | None) -> str:
    """Sidecar equation or transfer string to the grade.py gamma vocabulary."""
    s = (value or "").strip().lower()
    if not s:
        return ""
    if "s-log3" in s and "cine" in s:
        return "s-log3-cine"
    if "s-log3" in s:
        return "s-log3"
    if "hlg" in s or "arib-std-b67" in s:
        return "hlg"
    if "pq" in s or "2084" in s or "smpte2084" in s:
        return "pq"
    if "709" in s:
        return "rec709"
    return s


def find_sidecar(clip_path: Path) -> Path | None:
    """The Sony XML next to the clip (`<stem>M01.XML`, exact case), if present."""
    candidate = clip_path.parent / f"{clip_path.stem}M01.XML"
    return candidate if candidate.exists() else None


def decode_ltc_hex(value: str | None) -> str | None:
    """8 hex digits (frames, seconds, minutes, hours) to `HH:MM:SS:FF`.

    Each byte is BCD after masking off the flag bits that share the byte.
    """
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{8}", value.strip()):
        return None
    raw = bytes(int(value.strip()[i:i + 2], 16) for i in (0, 2, 4, 6))
    masked = (raw[0] & 0x3F, raw[1] & 0x7F, raw[2] & 0x7F, raw[3] & 0x3F)
    parts = []
    for byte in masked:
        tens, units = byte >> 4, byte & 0x0F
        if units > 9:
            return None
        parts.append(tens * 10 + units)
    frames, seconds, minutes, hours = parts
    if seconds > 59 or minutes > 59:
        return None
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def parse_sony_sidecar(path: Path | None) -> SonySidecar:
    """Creation date, gamma, tc rate and frame-0 timecode; never raises."""
    empty = SonySidecar()
    if path is None:
        return empty
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return empty
    try:
        out = SonySidecar()
        for elem in root.iter():
            name = _local(elem.tag)
            if name == "CreationDate" and out.captured_raw is None:
                raw = elem.get("value") or (elem.text or "").strip() or None
                out.captured_raw = raw.strip() if raw else None
            elif name == "CaptureGammaEquation" and not out.gamma:
                raw = elem.get("value") or (elem.text or "")
                out.gamma = map_gamma(raw)
            elif (name == "Item" and elem.get("name") == "CaptureGammaEquation"
                  and not out.gamma):
                # Real Sony cards: <Item name="CaptureGammaEquation" value="s-log3-cine"/>
                out.gamma = map_gamma(elem.get("value") or "")
        for table in root.iter():
            if _local(table.tag) != "LtcChangeTable":
                continue
            rate = coerce_float(table.get("tcFps"))
            out.tc_fps = rate if rate > 0 else None
            for change in list(table):
                if _local(change.tag) != "LtcChange":
                    continue
                if str(change.get("frameCount")) != "0":
                    continue
                decoded = decode_ltc_hex(change.get("value"))
                if decoded is not None:
                    out.timecode = decoded
                break
            break
        return out
    except Exception:
        return empty


def camera_from_probe(label: str, path: str, probe: dict, sidecar: SonySidecar) -> CameraInfo:
    """CameraInfo from an ffprobe payload plus the Sony sidecar. Pure."""
    streams = [s for s in (probe.get("streams") or []) if isinstance(s, dict)]
    fmt = probe.get("format") or {}
    format_tags = fmt.get("tags") if isinstance(fmt.get("tags"), dict) else {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    scopes = _merged_tags(streams, format_tags, 0)
    duration_s = coerce_float(fmt.get("duration"))
    if not duration_s and isinstance(video, dict):
        duration_s = coerce_float(video.get("duration"))
    raw_fps = 0.0
    width = height = 0
    if video is not None:
        raw_fps = parse_fraction(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
    fps = float(snap_frame_rate(raw_fps)) if raw_fps > 0 else 0.0
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    timecode = _first_tag(scopes, "timecode") or sidecar.timecode
    if sidecar.tc_fps:
        tc_rate: float | None = sidecar.tc_fps
    elif timecode and raw_fps > 0:
        snapped = snap_frame_rate(raw_fps)
        tc_rate = snapped.numerator / 1000 if snapped.denominator == 1001 else float(snapped)
    else:
        tc_rate = None
    gamma = sidecar.gamma or map_gamma(
        video.get("color_transfer") if isinstance(video, dict) else None)
    captured_at: str | None = None
    quicktime = _first_tag(scopes, "com.apple.quicktime.creationdate")
    generic = _first_tag(scopes, "creation_time")
    for raw in (quicktime, sidecar.captured_raw, generic):
        parsed = parse_captured_at(raw)
        if parsed is not None:
            captured_at = parsed.isoformat()
            break
    return CameraInfo(
        label=label, path=path,
        make=_first_tag(scopes, "make", "manufacturer",
                        "com.apple.quicktime.make") or "",
        model=_first_tag(scopes, "model", "com.apple.quicktime.model") or "",
        lens=_first_tag(scopes, "lens", "lensmodel", "lens_model",
                        "com.apple.quicktime.lensmodel") or "",
        fps=fps, width=width, height=height, duration_s=duration_s,
        has_audio=has_audio, gamma=gamma, timecode=timecode,
        tc_rate=tc_rate, captured_at=captured_at,
    )


# ---------------------------------------------------------------- timecode

_TC_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})([:;])(\d{2})$")


def timecode_to_seconds(tc: str, fps: float, tc_rate: float | None = None) -> float:
    """A start timecode to real seconds since midnight. Pure."""
    match = _TC_RE.match((tc or "").strip())
    if not match:
        raise ValueError(f"bad timecode: {tc!r}")
    hh, mm, ss = int(match.group(1)), int(match.group(2)), int(match.group(3))
    drop = match.group(4) == ";"
    ff = int(match.group(5))
    if mm >= 60 or ss >= 60:
        raise ValueError(f"bad timecode: {tc!r}")
    if tc_rate is not None and tc_rate > 0:
        snapped_tc = snap_frame_rate(float(tc_rate))
        rate = snapped_tc.numerator // 1000 if snapped_tc.denominator == 1001 else int(round(tc_rate))
    elif fps > 0:
        snapped = snap_frame_rate(fps)
        rate = snapped.numerator // 1000 if snapped.denominator == 1001 else int(snapped)
    else:
        raise ValueError(f"timecode rate unknown: {tc!r}")
    if ff >= rate:
        raise ValueError(f"timecode frames beyond rate: {tc!r}")
    if drop:
        if rate not in (30, 60):
            raise ValueError(f"drop-frame needs a 30 or 60 rate: {tc!r}")
        skipped = rate // 15
        total_minutes = hh * 60 + mm
        dropped = skipped * (total_minutes - total_minutes // 10)
        frames = (hh * 3600 + mm * 60 + ss) * rate + ff - dropped
    else:
        frames = (hh * 3600 + mm * 60 + ss) * rate + ff
    if fps > 0 and snap_frame_rate(fps).denominator == 1001:
        return frames / rate * 1001 / 1000
    return frames / rate


def wrap_day_delta(delta_s: float) -> float:
    """The same time difference inside (-12 h, +12 h]: a midnight wrap."""
    return (delta_s + DAY_S / 2) % DAY_S - DAY_S / 2


def _tc_seconds(camera: CameraInfo) -> float:
    if not camera.timecode:
        raise ValueError("clip has no timecode")
    return timecode_to_seconds(camera.timecode, camera.fps, camera.tc_rate)


def timecode_estimate(target: ClipResult, anchor: ClipResult) -> Estimate | None:
    """Offset from the two start timecodes, each in its own rate. Pure."""
    if anchor.offset_s is None:
        return None
    try:
        delta = wrap_day_delta(_tc_seconds(target.camera) - _tc_seconds(anchor.camera))
    except ValueError:
        return None
    return Estimate(
        method="timecode", offset_s=anchor.offset_s + delta,
        confidence=CONFIDENCE["timecode"], precision_s=PRECISION_S["timecode"],
        against=anchor.label,
        detail={"target_timecode": target.camera.timecode,
                "anchor_timecode": anchor.camera.timecode,
                "target_tc_rate": target.camera.tc_rate,
                "anchor_tc_rate": anchor.camera.tc_rate},
    )


# ---------------------------------------------------------------- capture clock

_ZONE_RE = re.compile(r"([+-]\d{2})(\d{2})$")


def parse_captured_at(value: str | None) -> datetime | None:
    """A capture stamp to an aware datetime; never raises."""
    if not value or not str(value).strip():
        return None
    s = str(value).strip()
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    if "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)
    s = _ZONE_RE.sub(r"\1:\2", s)
    try:
        parsed = datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def clock_estimate(target: ClipResult, anchor: ClipResult) -> Estimate | None:
    """Offset from the two capture stamps. Pure; coarse (±2 s at best)."""
    if anchor.offset_s is None:
        return None
    target_dt = parse_captured_at(target.camera.captured_at)
    anchor_dt = parse_captured_at(anchor.camera.captured_at)
    if target_dt is None or anchor_dt is None:
        return None
    return Estimate(
        method="clock",
        offset_s=anchor.offset_s + (target_dt - anchor_dt).total_seconds(),
        confidence=CONFIDENCE["clock"], precision_s=PRECISION_S["clock"],
        against=anchor.label,
        detail={"target_captured_at": target.camera.captured_at,
                "anchor_captured_at": anchor.camera.captured_at},
    )


# ---------------------------------------------------------------- waveform

Envelopes = dict  # (clip_path, hop_samples) -> np.ndarray; (clip_path, "samples") cached audio


def decode_command(path: Path) -> list[str]:
    """Mono 8 kHz speech-band audio on stdout as f32le."""
    low, high = BAND_HZ
    return ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1",
            "-ar", str(SAMPLE_RATE), "-af", f"highpass=f={low},lowpass=f={high}",
            "-f", "f32le", "pipe:1"]


def decode_samples(path: Path, *, has_audio: bool = True):
    """Decoded mono samples, or None when there is nothing to decode."""
    if np is None or not has_audio:
        return None
    try:
        done = subprocess.run(decode_command(Path(path)), capture_output=True, timeout=600)
    except Exception:
        return None
    if done.returncode != 0 or not done.stdout:
        return None
    return np.frombuffer(done.stdout, dtype=np.float32).copy()


def log_envelope(samples, hop_samples: int):
    """Log RMS per non-overlapping block; what near and far mics share."""
    blocks = len(samples) // int(hop_samples)
    if blocks < 1:
        return np.zeros(0) if np is not None else []
    shaped = np.asarray(samples[:blocks * hop_samples], dtype=np.float64)
    shaped = shaped.reshape(blocks, int(hop_samples))
    rms = np.sqrt(np.mean(shaped * shaped, axis=1))
    return np.log(rms + LOG_EPS)


def _get_envelope(path: str, has_audio: bool, hop_samples: int, envelopes: dict):
    cached = envelopes.get((path, hop_samples))
    if cached is not None:
        return cached
    samples = envelopes.get((path, "samples"))
    if samples is None and (path, "samples") not in envelopes:
        samples = decode_samples(Path(path), has_audio=has_audio)
        envelopes[(path, "samples")] = samples
    if samples is None:
        return None
    env = log_envelope(samples, hop_samples)
    if env is None or len(env) < 2:
        return None
    envelopes[(path, hop_samples)] = env
    return env


N_EXACT_RESCORE = 8


def _overlap_pearson(ref, tgt, lag: int) -> float:
    """Exact Pearson correlation over the two segments' actual overlap.

    With `lag = ref_index - target_index`, only the samples where both
    envelopes exist take part; each segment is centred and scaled by its own
    overlap statistics, so the result always lies within [-1, 1].
    """
    n, m = len(ref), len(tgt)
    start = max(0, lag)
    stop = min(n, m + lag)
    if stop <= start:
        return float("-inf")
    x = np.asarray(ref[start:stop], dtype=np.float64)
    y = np.asarray(tgt[start - lag:stop - lag], dtype=np.float64)
    xc = x - float(np.mean(x))
    yc = y - float(np.mean(y))
    denom = float(np.sqrt(float(np.sum(xc * xc)) * float(np.sum(yc * yc))))
    if denom == 0.0:
        return float("-inf")
    return float(max(-1.0, min(1.0, float(np.sum(xc * yc)) / denom)))


def find_peak(ref_env, tgt_env, hop_s: float, *,
              window_s: tuple[float, float] | None = None) -> WaveformPeak | None:
    """Best lag of the target against the reference, as a relative offset.

    With `lag = ref_index - target_index`, `lag * hop_s` is
    `target.offset_s - anchor.offset_s`.

    The FFT pass only proposes candidate lags; the reported r, second_r,
    margin and clear come from the exact Pearson correlation computed over
    each candidate's actual overlap, so short edge overlaps cannot inflate
    past 1 and every search (coarse, refinement, guided, drift) compares
    like with like.
    """
    if np is None:
        return None
    ref = np.asarray(ref_env, dtype=np.float64)
    tgt = np.asarray(tgt_env, dtype=np.float64)
    n, m = len(ref), len(tgt)
    if n < 2 or m < 2:
        return None
    ref_std = float(np.std(ref))
    tgt_std = float(np.std(tgt))
    if ref_std == 0 or tgt_std == 0:
        return None
    ref_z = (ref - float(np.mean(ref))) / ref_std
    tgt_z = (tgt - float(np.mean(tgt))) / tgt_std
    size = 1
    while size < n + m - 1:
        size <<= 1
    corr = np.fft.irfft(np.fft.rfft(ref_z, size) * np.conj(np.fft.rfft(tgt_z, size)), size)
    lags = np.arange(-(m - 1), n)
    raw = corr[np.where(lags >= 0, lags, size + lags)]
    counts = np.where(lags >= 0, np.minimum(n, m + lags) - lags, np.minimum(n, m + lags))
    counts = np.maximum(counts, 0)
    approx = np.full(n + m - 1, -np.inf)
    ok = counts > 0
    approx[ok] = raw[ok] / counts[ok]
    overlap_s = counts * hop_s
    required = min(MIN_OVERLAP_FLOOR_S, 0.5 * min(n, m) * hop_s)
    approx[overlap_s + 1e-9 < required] = -np.inf
    if window_s is not None:
        centre, radius = window_s
        approx[np.abs(lags * hop_s - centre) > radius + 1e-12] = -np.inf
    order = np.argsort(-approx, kind="stable")
    # Spread the re-scored candidates across distinct peaks: neighbouring
    # lags of one inflated hump must not crowd out a rival seconds away.
    suppressed: list[int] = []
    for i in order:
        i = int(i)
        if not np.isfinite(approx[i]):
            continue
        if all(abs(float(lags[i]) - float(lags[j])) * hop_s
               >= SECOND_PEAK_EXCLUSION_S - 1e-9 for j in suppressed):
            suppressed.append(i)
        if len(suppressed) >= N_EXACT_RESCORE:
            break
    if not suppressed:
        return None
    exact = {i: _overlap_pearson(ref, tgt, int(lags[i])) for i in suppressed}
    best = max(suppressed, key=lambda i: (exact[i], float(approx[i])))
    best_r = float(exact[best])
    if not np.isfinite(best_r):
        return None
    best_lag = int(lags[best])
    far = np.abs(lags - best_lag) * hop_s >= SECOND_PEAK_EXCLUSION_S - 1e-9
    rivals = [i for i in suppressed
              if i != best and bool(far[i]) and np.isfinite(exact[i])]
    second_r = -1.0
    if rivals:
        second_r = float(max(exact[i] for i in rivals))
    else:
        far_candidates = [int(i) for i in order
                          if bool(far[int(i)])
                          and np.isfinite(approx[int(i)])][:N_EXACT_RESCORE]
        if far_candidates:
            far_exact = [_overlap_pearson(ref, tgt, int(lags[i]))
                         for i in far_candidates]
            far_finite = [v for v in far_exact if np.isfinite(v)]
            if far_finite:
                second_r = float(max(far_finite))
    margin = best_r - second_r
    return WaveformPeak(
        offset_s=float(best_lag) * hop_s, r=best_r, second_r=second_r,
        margin=margin, overlap_s=float(overlap_s[best]),
        clear=best_r >= R_CLEAR and margin >= MARGIN_CLEAR,
    )


def measure_drift(ref_env_1ms, tgt_env_1ms, coarse_offset_s: float,
                  target_duration_s: float) -> tuple[float, float, int] | None:
    """Re-measure the lag in 20 s windows; slope is drift, intercept is the offset."""
    if np is None:
        return None
    ref_dur = len(ref_env_1ms) * REFINE_HOP_S
    tgt_dur = target_duration_s if target_duration_s > 0 else len(tgt_env_1ms) * REFINE_HOP_S
    lo = max(0.0, -coarse_offset_s)
    hi = min(tgt_dur, ref_dur - coarse_offset_s)
    centres: list[float] = []
    offsets: list[float] = []
    start = lo
    while hi - start >= MIN_OVERLAP_FLOOR_S - 1e-9:
        end = min(start + DRIFT_WINDOW_S, hi)
        if end - start < MIN_OVERLAP_FLOOR_S:
            break
        ref_a = int(round((start + coarse_offset_s) / REFINE_HOP_S))
        ref_b = int(round((end + coarse_offset_s) / REFINE_HOP_S))
        tgt_a = int(round(start / REFINE_HOP_S))
        tgt_b = int(round(end / REFINE_HOP_S))
        ref_seg = np.asarray(ref_env_1ms[max(0, ref_a):ref_b])
        tgt_seg = np.asarray(tgt_env_1ms[max(0, tgt_a):tgt_b])
        peak = find_peak(ref_seg, tgt_seg, REFINE_HOP_S,
                         window_s=(0.0, DRIFT_SEARCH_S))
        if peak is not None and peak.r >= DRIFT_WINDOW_MIN_R:
            centres.append((start + end) / 2)
            offsets.append(coarse_offset_s + peak.offset_s)
        if end >= hi - 1e-9:
            break
        start = end
    if len(centres) < DRIFT_MIN_WINDOWS or max(centres) - min(centres) < 40.0:
        return None
    slope, intercept = (float(v) for v in np.polyfit(centres, offsets, 1))
    return intercept, slope * 1e6, len(centres)


def waveform_estimate(target: ClipResult, anchor: ClipResult, envelopes: dict, *,
                      guide: tuple[float, float, str] | None = None) -> Estimate | None:
    """Offset from log-envelope correlation, coarse then refined, drift-corrected."""
    if np is None or anchor.offset_s is None:
        return None
    if not target.camera.has_audio or not anchor.camera.has_audio:
        return None
    coarse_hop = int(round(COARSE_HOP_S * SAMPLE_RATE))
    refine_hop = int(round(REFINE_HOP_S * SAMPLE_RATE))
    ref_c = _get_envelope(anchor.camera.path, True, coarse_hop, envelopes)
    tgt_c = _get_envelope(target.camera.path, True, coarse_hop, envelopes)
    if ref_c is None or tgt_c is None:
        return None
    coarse = find_peak(ref_c, tgt_c, COARSE_HOP_S)
    if coarse is None:
        return None
    ref_f = _get_envelope(anchor.camera.path, True, refine_hop, envelopes)
    tgt_f = _get_envelope(target.camera.path, True, refine_hop, envelopes)
    if ref_f is None or tgt_f is None:
        return None
    if guide is None:
        fine = find_peak(ref_f, tgt_f, REFINE_HOP_S,
                         window_s=(coarse.offset_s, 0.015))
        if fine is None:
            return None
        # The refinement only moves the offset to the 1 ms grid: r,
        # second_r, margin and clear stay with the coarse global search,
        # where the competing peaks exist.
        peak_offset, peak_overlap = fine.offset_s, coarse.overlap_s
        peak_r, second_r, margin, clear = (coarse.r, coarse.second_r,
                                          coarse.margin, coarse.clear)
        drift_centre = coarse.offset_s
    else:
        centre, radius, _method = guide
        guided = find_peak(ref_f, tgt_f, REFINE_HOP_S,
                           window_s=(centre, radius))
        if guided is None:
            return None
        peak_offset, peak_overlap = guided.offset_s, guided.overlap_s
        peak_r, second_r, margin, clear = (guided.r, guided.second_r,
                                          guided.margin, guided.clear)
        drift_centre = guided.offset_s
    relative = peak_offset
    drift_ppm: float | None = None
    drift_windows = 0
    measured = measure_drift(ref_f, tgt_f, drift_centre, target.camera.duration_s)
    if measured is not None:
        relative, drift_ppm, drift_windows = measured
    return Estimate(
        method="waveform", offset_s=anchor.offset_s + relative,
        confidence=min(1.0, max(0.0, peak_r)), precision_s=PRECISION_S["waveform"],
        against=anchor.label,
        detail={"r": peak_r, "second_r": second_r, "margin": margin,
                "clear": clear, "overlap_s": peak_overlap,
                "guided": guide is not None,
                "guide_method": guide[2] if guide is not None else None,
                "drift_ppm": drift_ppm, "drift_windows": drift_windows},
    )


# ---------------------------------------------------------------- whisper

_WORD_EDGE_RE = re.compile(r"^[^\w']+|[^\w']+$")


def normalize_word(word: str) -> str:
    """Lower-case and strip edge punctuation; internal apostrophes stay."""
    return _WORD_EDGE_RE.sub("", (word or "").lower()).strip("'")


def load_transcript_words(path: Path) -> list[TranscriptWord]:
    """Word-timed Whisper JSON in either shape; malformed input gives []."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("words"), list):
        raw = data["words"]
    else:
        raw = [w for seg in data.get("segments", []) if isinstance(seg, dict)
               for w in seg.get("words", [])]
    parsed: list[TranscriptWord] = []
    for w in raw:
        if not isinstance(w, dict):
            continue
        text = normalize_word(str(w.get("word") or w.get("text") or ""))
        start = w.get("start")
        if not text or not isinstance(start, (int, float)):
            continue
        end = w.get("end")
        parsed.append(TranscriptWord(text, float(start),
                                    float(end) if isinstance(end, (int, float)) else -1.0))
    parsed.sort(key=lambda t: t.start_s)
    out: list[TranscriptWord] = []
    for i, t in enumerate(parsed):
        end = t.end_s
        if end < 0:
            if i + 1 < len(parsed):
                end = parsed[i + 1].start_s
            else:
                end = t.start_s + 0.30
        out.append(TranscriptWord(t.word, t.start_s, max(end, t.start_s)))
    return out


def metal_visible() -> bool:
    """Whether this process can see a Metal GPU, asked the way MLX asks (MTLCopyAllDevices).

    Inside a sandbox that hides the GPU the list is empty, and MLX indexes it anyway and aborts:
    mlx_whisper dies and macOS shows a crash dialog. This check cannot crash.
    """
    if sys.platform != "darwin":
        return False
    try:
        metal = ctypes.CDLL("/System/Library/Frameworks/Metal.framework/Metal")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        metal.MTLCopyAllDevices.restype = ctypes.c_void_p
        cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        devices = metal.MTLCopyAllDevices()
        if not devices:
            return False
        count = cf.CFArrayGetCount(devices)
        cf.CFRelease(devices)
        return count > 0
    except (OSError, AttributeError):
        return False


def get_transcript_words(clip: CameraInfo, transcripts_dir: Path | None,
                         cache_dir: Path,
                         whisper_model: str = WHISPER_MODEL
                         ) -> list[TranscriptWord]:
    """Words for one clip: `--transcripts`, the run cache, or local mlx_whisper."""
    stem = Path(clip.path).stem
    if transcripts_dir is not None:
        return load_transcript_words(transcripts_dir / f"{stem}.json")
    cache_dir = Path(cache_dir)
    cached = cache_dir / f"{stem}.json"
    try:
        if cached.exists() and cached.stat().st_size > 0:
            return load_transcript_words(cached)
    except OSError:
        pass
    if shutil.which("mlx_whisper") is None or not metal_visible():
        return []
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(
            ["mlx_whisper", clip.path, "--model", whisper_model, "--output-format", "json",
             "--output-dir", str(cache_dir), "--word-timestamps", "True",
             *WHISPER_GUARD_ARGS, "--verbose", "False"],
            capture_output=True, timeout=3600)
    except Exception:
        return []
    if done.returncode != 0:
        return []
    return load_transcript_words(cached)


def whisper_estimate(target: ClipResult, anchor: ClipResult,
                     target_words: list[TranscriptWord],
                     anchor_words: list[TranscriptWord]) -> Estimate | None:
    """Offset from once-each 3-word phrases; one matching sentence is refused."""
    if anchor.offset_s is None:
        return None

    def phrases(words: list[TranscriptWord]) -> dict[tuple[str, ...], list[tuple[float, int]]]:
        seq = [(w.word, w.start_s) for w in words if w.word]
        out: dict[tuple[str, ...], list[tuple[float, int]]] = {}
        for i in range(len(seq) - WHISPER_PHRASE_WORDS + 1):
            key = tuple(seq[i + k][0] for k in range(WHISPER_PHRASE_WORDS))
            out.setdefault(key, []).append((seq[i][1], i))
        return out

    target_phrases = phrases(target_words)
    anchor_phrases = phrases(anchor_words)
    votes: list[tuple[float, int, float]] = []
    for key, tlist in target_phrases.items():
        alist = anchor_phrases.get(key)
        if alist is None or len(tlist) != 1 or len(alist) != 1:
            continue
        (target_start, _ti), (anchor_start, anchor_idx) = tlist[0], alist[0]
        votes.append((anchor_start - target_start, anchor_idx, anchor_start))
    if not votes:
        return None
    votes.sort(key=lambda v: v[0])
    vals = [v[0] for v in votes]
    best_i, best_j, best_spread = 0, 0, 0.0
    best_count = 1
    for i in range(len(votes)):
        j = i
        while (j + 1 < len(votes)
               and vals[j + 1] - vals[i] <= WHISPER_CLUSTER_S + 1e-9):
            j += 1
        count, spread = j - i + 1, vals[j] - vals[i]
        if count > best_count or (count == best_count and spread < best_spread - 1e-12):
            best_i, best_j, best_spread, best_count = i, j, spread, count
    winning = votes[best_i:best_j + 1]
    by_anchor = sorted(winning, key=lambda v: v[1])
    moments = 1
    for prev, cur in zip(by_anchor, by_anchor[1:]):
        if cur[1] - prev[1] > 1 or cur[2] - prev[2] > 2.0:
            moments += 1
    if moments < WHISPER_MIN_MOMENTS:
        return None
    ordered = sorted(v[0] for v in winning)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return Estimate(
        method="whisper", offset_s=anchor.offset_s + median,
        confidence=min(1.0, moments / 3.0), precision_s=PRECISION_S["whisper"],
        against=anchor.label,
        detail={"votes": len(votes), "cluster_votes": len(winning), "moments": moments},
    )


# ---------------------------------------------------------------- flash

_PTS_TIME_RE = re.compile(r"pts_time:(-?[0-9]+(?:\.[0-9]+)?)")


def flash_decode_command(path: Path, window_s: float = FLASH_WINDOW_S) -> list[str]:
    """Tiny grey proxy on stdout as rawvideo, per-frame times via showinfo."""
    filt = f"scale={FLASH_WIDTH}:{FLASH_HEIGHT},format=gray,showinfo"
    return ["ffmpeg", "-v", "info", "-i", str(path), "-t", str(window_s),
            "-map", "0:v:0", "-vf", filt, "-f", "rawvideo", "pipe:1"]


def decode_flash_luma(path: Path,
                      window_s: float = FLASH_WINDOW_S
                      ) -> tuple[list[float], list[float]] | None:
    """Mean luma per frame with real timestamps, or None when unavailable."""
    if np is None:
        return None
    try:
        done = subprocess.run(flash_decode_command(Path(path), window_s),
                              capture_output=True, timeout=600)
    except Exception:
        return None
    if done.returncode != 0 or not done.stdout:
        return None
    frame_bytes = FLASH_WIDTH * FLASH_HEIGHT
    n_frames = len(done.stdout) // frame_bytes
    if n_frames < 2:
        return None
    try:
        stderr = done.stderr.decode("utf-8", errors="replace")
    except Exception:
        stderr = ""
    pts = [float(m) for m in _PTS_TIME_RE.findall(stderr)]
    if len(pts) == n_frames:
        t0 = pts[0]
        times = [p - t0 for p in pts]
    else:
        # showinfo parsing failed; fall back to a 30 fps grid so CFR test
        # clips still work (VFR timing needs the real timestamps above).
        times = [i / 30.0 for i in range(n_frames)]
    buf = np.frombuffer(done.stdout[:n_frames * frame_bytes], dtype=np.uint8)
    try:
        means = buf.reshape(n_frames, frame_bytes).mean(axis=1)
    except ValueError:
        return None
    return times, [float(v) for v in means]


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2


def find_flash_edges(times: list[float], luma: list[float]) -> list[FlashEdge]:
    """Light-switch steps: large frame-to-frame moves, signed. Pure.

    A step counts when it beats both the clip's own frame-to-frame noise
    (a robust z-score from the median absolute deviation) and an absolute
    floor, so clean synthetic clips and noisy rooms both work. Same-sign
    steps within FLASH_MIN_GAP_S merge into one (the total move, timed at
    its onset). The edge time is the first frame after the step.
    """
    n = min(len(times), len(luma))
    if n < 2:
        return []
    diffs = [float(luma[i + 1]) - float(luma[i]) for i in range(n - 1)]
    edge_times = [float(times[i + 1]) for i in range(n - 1)]
    mad = _median([abs(d - _median(diffs)) for d in diffs])
    sigma = 1.4826 * mad
    threshold = max(FLASH_MIN_Z * sigma, FLASH_MIN_STEP)
    candidates: list[FlashEdge] = []
    for t, d in zip(edge_times, diffs):
        if abs(d) >= threshold:
            candidates.append(FlashEdge(t, 1 if d > 0 else -1, abs(d)))
    merged: list[FlashEdge] = []
    for edge in candidates:
        if (merged and edge.sign == merged[-1].sign
                and edge.time_s - merged[-1].time_s <= FLASH_MIN_GAP_S + 1e-9):
            merged[-1] = FlashEdge(merged[-1].time_s, merged[-1].sign,
                                   merged[-1].magnitude + edge.magnitude)
        else:
            merged.append(edge)
    return merged


def flash_confidence(n_matched: int) -> float:
    """More matched edges is more proof; one edge is a hint. Pure."""
    if n_matched >= 4:
        return 0.9
    if n_matched == 3:
        return 0.85
    if n_matched == 2:
        return 0.75
    return 0.35


def match_flash_edges(target_edges: list[FlashEdge], anchor_edges: list[FlashEdge],
                      tolerance_s: float,
                      clock_relative_s: float | None = None
                      ) -> tuple[float, list[tuple[int, int]]] | None:
    """Best same-sign, same-gap alignment of two edge sequences. Pure.

    Every same-sign pair proposes a candidate offset; the candidate's
    support is the greedy in-order match within tolerance. Most matched
    edges wins, then closest to the clock hint, then tightest gaps. Returns
    the median anchor-minus-target difference and the matched index pairs.
    """
    if not target_edges or not anchor_edges:
        return None
    seen: set[frozenset[tuple[int, int]]] = set()
    scored: list[tuple[int, float, float, float,
                       list[tuple[int, int]]]] = []
    for t0 in target_edges:
        for a0 in anchor_edges:
            if t0.sign != a0.sign:
                continue
            candidate = a0.time_s - t0.time_s
            used: set[int] = set()
            pairs: list[tuple[int, int]] = []
            for k, te in enumerate(target_edges):
                best_j: int | None = None
                for j, ae in enumerate(anchor_edges):
                    if j in used or ae.sign != te.sign:
                        continue
                    if abs((ae.time_s - te.time_s) - candidate) <= tolerance_s + 1e-9:
                        best_j = j
                        break
                if best_j is not None:
                    used.add(best_j)
                    pairs.append((k, best_j))
            if not pairs:
                continue
            key = frozenset(pairs)
            if key in seen:
                continue
            seen.add(key)
            diffs = sorted(anchor_edges[j].time_s - target_edges[k].time_s
                           for k, j in pairs)
            mid = len(diffs) // 2
            median = diffs[mid] if len(diffs) % 2 else (diffs[mid - 1] + diffs[mid]) / 2
            gap_err = sum(abs(d - median) for d in diffs) / len(diffs)
            if clock_relative_s is None:
                clock_dist = 0.0
            else:
                clock_dist = abs(median - clock_relative_s)
            scored.append((-len(pairs), clock_dist, gap_err, median, pairs))
    if not scored:
        return None
    scored.sort(key=lambda s: (s[0], s[1], s[2]))
    _neg, _clock, _gap, median, pairs = scored[0]
    pairs.sort(key=lambda p: p[0])
    return median, pairs


def flash_precision_s(target: ClipResult, anchor: ClipResult) -> float:
    """One frame of the slower clip, else 1/30. Pure."""
    frames: list[float] = []
    for cam in (target.camera, anchor.camera):
        frames.append(1.0 / cam.fps if cam.fps > 0 else 1.0 / 30)
    return max(frames)


def _get_flash_edges(camera: CameraInfo, flash_cache: dict) -> list[FlashEdge] | None:
    """Cached edges for one clip; None means no video or undecodable."""
    key = (camera.path, "flash_edges")
    if key in flash_cache:
        return flash_cache[key]
    if camera.width <= 0 or camera.height <= 0:
        flash_cache[key] = None
        return None
    decoded = decode_flash_luma(Path(camera.path))
    if decoded is None:
        flash_cache[key] = None
        return None
    times, luma = decoded
    edges = find_flash_edges(times, luma)
    flash_cache[key] = edges
    return edges


def flash_estimate(target: ClipResult, anchor: ClipResult, flash_cache: dict, *,
                   clock_offset_s: float | None = None) -> Estimate | None:
    """Offset from matched light-switch edges, clock-preferred. Pure apart from decode."""
    if np is None or anchor.offset_s is None:
        return None
    target_edges = _get_flash_edges(target.camera, flash_cache)
    anchor_edges = _get_flash_edges(anchor.camera, flash_cache)
    if not target_edges or not anchor_edges:
        return None
    precision = flash_precision_s(target, anchor)
    tolerance = precision + FLASH_GAP_MARGIN_S
    clock_relative: float | None = None
    if clock_offset_s is not None:
        clock_relative = clock_offset_s - anchor.offset_s
    matched = match_flash_edges(target_edges, anchor_edges, tolerance, clock_relative)
    if matched is None:
        return None
    median, pairs = matched
    gaps_target = [target_edges[pairs[i + 1][0]].time_s - target_edges[pairs[i][0]].time_s
                   for i in range(len(pairs) - 1)]
    gaps_anchor = [anchor_edges[pairs[i + 1][1]].time_s - anchor_edges[pairs[i][1]].time_s
                   for i in range(len(pairs) - 1)]
    return Estimate(
        method="flash", offset_s=anchor.offset_s + median,
        confidence=flash_confidence(len(pairs)), precision_s=precision,
        against=anchor.label,
        detail={"matched_edges": len(pairs),
                "target_edge_s": [round(target_edges[k].time_s, 3) for k, _j in pairs],
                "anchor_edge_s": [round(anchor_edges[j].time_s, 3) for _k, j in pairs],
                "signs": [target_edges[k].sign for k, _j in pairs],
                "gap_target_s": [round(g, 3) for g in gaps_target],
                "gap_anchor_s": [round(g, 3) for g in gaps_anchor],
                "tolerance_s": round(tolerance, 3),
                "clock_relative_s": (None if clock_relative is None
                                     else round(clock_relative, 3))},
    )


# ---------------------------------------------------------------- lips

def parse_lips_output(text: str) -> list[LipsFrame]:
    """Helper stdout to per-frame openings and face boxes. Pure; skips bad lines."""
    frames: list[LipsFrame] = []
    for raw in (text or "").splitlines():
        parts = raw.split()
        if len(parts) < 2:
            continue
        try:
            index = int(parts[0])
        except ValueError:
            continue
        if parts[1] == "NOFACE":
            frames.append(LipsFrame(index, None, None))
            continue
        try:
            opening = None if parts[1] == "NA" else float(parts[1])
        except ValueError:
            continue
        box: tuple[float, float, float, float] | None = None
        if len(parts) >= 3:
            try:
                x, y, w, h = (float(v) for v in parts[2].split(","))
            except ValueError:
                continue
            box = (x, y, w, h)
        frames.append(LipsFrame(index, opening, box))
    frames.sort(key=lambda f: f.index)
    return frames


def median_box(frames: list[LipsFrame]) -> tuple[float, float, float, float] | None:
    """Median face box over the frames that have one. Pure."""
    boxes = [f.box for f in frames if f.box is not None]
    if not boxes:
        return None
    return (float(_median([b[0] for b in boxes])),
            float(_median([b[1] for b in boxes])),
            float(_median([b[2] for b in boxes])),
            float(_median([b[3] for b in boxes])))


def _moving_average(values, window: int):
    """Trailing mean over `window` samples, shrunk at the start. Pure."""
    vals = np.asarray(values, dtype=np.float64).ravel()
    width = max(1, int(window))
    n = len(vals)
    if n == 0:
        return vals
    cumsum = np.concatenate(([0.0], np.cumsum(vals)))
    span = np.minimum(np.arange(n) + 1, width)
    start = np.arange(n) + 1 - span
    return (cumsum[1:] - cumsum[start]) / span


def lips_prep(openings, fps: float):
    """3-frame average minus the ~1 s average, z-scored. Pure; None when flat.

    The measured recipe, applied to both the lip openings and the per-frame
    audio: no-face gaps interpolate, ends extend.
    """
    if np is None:
        return None
    vals = np.asarray([np.nan if v is None else v for v in openings],
                      dtype=np.float64).ravel()
    if vals.size < 2:
        return None
    valid = np.isfinite(vals)
    if not np.any(valid):
        return None
    if not np.all(valid):
        xp = np.flatnonzero(valid)
        vals = np.interp(np.arange(vals.size), xp, vals[valid])
    baseline_window = int(round(fps)) if fps > 0 else 24
    detrended = _moving_average(vals, LIPS_SMOOTH_FRAMES) \
        - _moving_average(vals, baseline_window)
    std = float(np.std(detrended))
    if std == 0.0:
        return None
    return (detrended - float(np.mean(detrended))) / std


def lips_audio_per_frame(samples, sample_rate: float, times: list[float]):
    """Log RMS of band-passed audio inside each frame interval. Pure."""
    if np is None or sample_rate <= 0 or len(times) < 1:
        return None
    audio = np.asarray(samples, dtype=np.float64).ravel()
    edges = list(times)
    dts = np.diff(np.asarray(times, dtype=np.float64))
    edges.append(float(times[-1]) + (float(np.median(dts)) if dts.size else 1.0 / 30))
    out = np.empty(len(times))
    for k, (a, b) in enumerate(zip(edges[:-1], edges[1:])):
        i0 = max(0, int(round(a * sample_rate)))
        i1 = max(i0 + 1, min(len(audio), int(round(b * sample_rate))))
        if i0 >= len(audio):
            out[k] = np.log(LOG_EPS)
            continue
        seg = audio[i0:i1]
        out[k] = float(np.log(np.sqrt(np.mean(seg * seg)) + LOG_EPS))
    return out


def lips_resample(times, values, grid_hz: float):
    """(observed start, values on a uniform grid) by linear interpolation. Pure."""
    if np is None or grid_hz <= 0:
        return None
    t = np.asarray(times, dtype=np.float64).ravel()
    v = np.asarray(values, dtype=np.float64).ravel()
    if t.size < 2 or v.size != t.size:
        return None
    t0, t1 = float(t[0]), float(t[-1])
    if not t1 > t0:
        return None
    grid = np.arange(t0, t1, 1.0 / float(grid_hz))
    if grid.size < 2:
        return None
    return t0, np.interp(grid, t, v)


def lips_best_lag(ref_times, ref_vals, tgt_times, tgt_vals, grid_hz: float, *,
                  clock_relative_s: float | None = None) -> LipsLag | None:
    """Best anchor-minus-target lag over the whole overlap, with the rival ratio.

    The measured recipe: Pearson correlation of the z-scored series at each
    lag, best wins, confidence is best over the best score more than 0.5 s
    away. Like the waveform search, the FFT pass only proposes candidate lags
    and the reported r is the exact Pearson over each candidate's actual
    overlap. The clock never narrows the search: it only picks between near
    ties (within 0.02 of the best). Pure.
    """
    if np is None:
        return None
    ref_grid = lips_resample(ref_times, ref_vals, grid_hz)
    tgt_grid = lips_resample(tgt_times, tgt_vals, grid_hz)
    if ref_grid is None or tgt_grid is None:
        return None
    ref_t0, ref = ref_grid
    tgt_t0, tgt = tgt_grid
    hop_s = 1.0 / float(grid_hz)
    n, m = len(ref), len(tgt)
    ref_std = float(np.std(ref))
    tgt_std = float(np.std(tgt))
    if ref_std == 0 or tgt_std == 0:
        return None
    ref_z = (ref - float(np.mean(ref))) / ref_std
    tgt_z = (tgt - float(np.mean(tgt))) / tgt_std
    size = 1
    while size < n + m - 1:
        size <<= 1
    corr = np.fft.irfft(np.fft.rfft(ref_z, size) * np.conj(np.fft.rfft(tgt_z, size)), size)
    lags = np.arange(-(m - 1), n)
    raw = corr[np.where(lags >= 0, lags, size + lags)]
    counts = np.where(lags >= 0, np.minimum(n, m + lags) - lags, np.minimum(n, m + lags))
    counts = np.maximum(counts, 0)
    approx = np.full(n + m - 1, -np.inf)
    ok = counts > 0
    approx[ok] = raw[ok] / counts[ok]
    overlap_s = counts * hop_s
    required = min(LIPS_MIN_OVERLAP_S, 0.5 * min(n, m) * hop_s)
    approx[overlap_s + 1e-9 < required] = -np.inf
    order = np.argsort(-approx, kind="stable")
    # Spread the re-scored candidates across distinct peaks, as in find_peak.
    suppressed: list[int] = []
    for i in order:
        i = int(i)
        if not np.isfinite(approx[i]):
            continue
        if all(abs(float(lags[i]) - float(lags[j])) * hop_s
               >= LIPS_RIVAL_EXCLUSION_S - 1e-9 for j in suppressed):
            suppressed.append(i)
        if len(suppressed) >= N_EXACT_RESCORE:
            break
    if not suppressed:
        return None
    exact = {i: _overlap_pearson(ref, tgt, int(lags[i])) for i in suppressed}
    best = max(suppressed, key=lambda i: (exact[i], float(approx[i])))
    best_r = float(exact[best])
    if not np.isfinite(best_r) or best_r <= 0:
        return None
    base = ref_t0 - tgt_t0
    if clock_relative_s is not None:
        tied = [i for i in suppressed
                if np.isfinite(exact[i])
                and best_r - float(exact[i]) <= LIPS_TIE_TOLERANCE + 1e-12]
        if len(tied) > 1:
            best = min(tied, key=lambda i: abs(
                base + float(lags[i]) * hop_s - clock_relative_s))
            best_r = float(exact[best])
    best_lag = int(lags[best])
    far = np.abs(lags - best_lag) * hop_s >= LIPS_RIVAL_EXCLUSION_S - 1e-9
    rivals = [i for i in suppressed
              if i != best and bool(far[i]) and np.isfinite(exact[i])]
    rival_r = -1.0
    if rivals:
        rival_r = float(max(exact[i] for i in rivals))
    else:
        far_candidates = [int(i) for i in order
                          if bool(far[int(i)])
                          and np.isfinite(approx[int(i)])][:N_EXACT_RESCORE]
        if far_candidates:
            far_exact = [_overlap_pearson(ref, tgt, int(lags[i]))
                         for i in far_candidates]
            far_finite = [v for v in far_exact if np.isfinite(v)]
            if far_finite:
                rival_r = float(max(far_finite))
    if rival_r > 1e-9:
        ratio = best_r / rival_r
    else:
        ratio = float("inf") if best_r > 0 else 0.0
    return LipsLag(relative_s=base + best_lag * hop_s, r=best_r,
                   rival_r=rival_r, ratio=ratio,
                   overlap_s=float(overlap_s[best]))


def lips_confidence(ratio: float) -> float:
    """Ratio bands from the measured recipe: below 1.5 is low, 2 and up good."""
    if ratio >= LIPS_RATIO_GOOD:
        return 0.85
    if ratio >= LIPS_RATIO_LOW:
        return 0.65
    return 0.35


def lips_precision_s(target: ClipResult, anchor: ClipResult) -> float:
    """One frame, like the flash precision. Pure."""
    return frame_duration_s(target, anchor)


def lips_helper_source() -> Path:
    """The shipped Swift helper source next to this module."""
    return Path(__file__).parent / "lips_helper.swift"


def lips_cache_dir() -> Path:
    """Where the built helper lives."""
    return Path.home() / "Library" / "Caches" / LIPS_CACHE_DIRNAME


def build_lips_helper(cache_dir: Path | None = None) -> tuple[Path | None, str | None]:
    """(helper binary, None), building with `swiftc -O` when needed.

    The binary is cached by the source hash. Anything that stops the build
    (not macOS, no swiftc, unreadable source) returns (None, reason) instead;
    the caller skips the method with a warning. Never raises.
    """
    if sys.platform != "darwin":
        return None, "not running on macOS (Apple Vision is required)"
    if shutil.which("swiftc") is None:
        return None, "swiftc not found (install the Xcode command-line tools)"
    src = lips_helper_source()
    if not src.exists():
        return None, f"helper source missing: {src}"
    try:
        digest = hashlib.sha256(src.read_bytes()).hexdigest()[:16]
    except OSError:
        return None, f"helper source unreadable: {src}"
    dest_dir = Path(cache_dir) if cache_dir is not None else lips_cache_dir()
    dest = dest_dir / f"{LIPS_HELPER_NAME}-{digest}"
    if dest.exists():
        return dest, None
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None, f"cannot write the helper cache: {dest_dir}"
    tmp = dest_dir / f"{LIPS_HELPER_NAME}-{digest}.tmp"
    try:
        done = subprocess.run(["swiftc", "-O", str(src), "-o", str(tmp)],
                              capture_output=True, timeout=600)
    except Exception:
        return None, "swiftc failed to run"
    if done.returncode != 0:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None, "the Vision helper could not be built"
    try:
        os.replace(tmp, dest)
    except OSError:
        return None, f"cannot write the helper cache: {dest_dir}"
    return dest, None


def run_lips_helper(helper: Path, frames_dir: Path,
                    timeout_s: float = 3600) -> list[LipsFrame] | None:
    """Parsed helper output, or None when Vision failed. Never raises."""
    try:
        done = subprocess.run([str(helper), str(frames_dir)],
                              capture_output=True, timeout=timeout_s)
    except Exception:
        return None
    if done.returncode != 0:
        return None
    try:
        text = done.stdout.decode("utf-8", errors="replace")
    except Exception:
        return None
    return parse_lips_output(text)


def lips_audio_decode_command(path: Path) -> list[str]:
    """Mono speech-band audio on stdout as f32le for the lips-vs-audio signal."""
    low, high = LIPS_AUDIO_BAND_HZ
    return ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1",
            "-ar", str(LIPS_AUDIO_RATE), "-af", f"highpass=f={low},lowpass=f={high}",
            "-f", "f32le", "pipe:1"]


def decode_lips_audio(path: Path, *, has_audio: bool = True):
    """Decoded mono samples for lips-vs-audio, or None when there are none."""
    if np is None or not has_audio:
        return None
    try:
        done = subprocess.run(lips_audio_decode_command(Path(path)),
                              capture_output=True, timeout=600)
    except Exception:
        return None
    if done.returncode != 0 or not done.stdout:
        return None
    return np.frombuffer(done.stdout, dtype=np.float32).copy()


def lips_coarse_command(path: Path, start_s: float, span_s: float,
                        out_pattern: str) -> list[str]:
    """Low-resolution, low-rate frames to find the face box."""
    return ["ffmpeg", "-v", "error", "-ss", str(start_s), "-i", str(path),
            "-t", str(span_s), "-map", "0:v:0",
            "-vf", f"fps={LIPS_COARSE_FPS},scale={LIPS_COARSE_WIDTH}:-2",
            "-f", "image2", "-q:v", "4", str(out_pattern)]


def _escape_filter_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:") \
        .replace(",", "\\,").replace("'", "\\'")


def lips_crop_filter(box: tuple[float, float, float, float],
                     width: int, height: int,
                     *, cube: Path | None = None) -> str:
    """Crop around the face box (with margin), scaled so the face is ~400 px tall.

    The box is Vision-normalised (origin lower-left); ffmpeg's origin is
    upper-left. The camera LUT, when one was found, converts first. Pure.
    """
    x, y, w, h = (float(v) for v in box)
    w = min(max(w, 0.0), 1.0)
    h = min(max(h, 0.0), 1.0)
    cx = min(max(x + w / 2, 0.0), 1.0)
    cy = min(max(1.0 - y - h / 2, 0.0), 1.0)
    cw = min(w * (1.0 + LIPS_CROP_MARGIN), 1.0)
    ch = min(h * (1.0 + LIPS_CROP_MARGIN), 1.0)
    x0 = min(max(cx - cw / 2, 0.0), 1.0 - cw)
    y0 = min(max(cy - ch / 2, 0.0), 1.0 - ch)
    px = min(max(int(round(x0 * width)), 0), max(width - 2, 0))
    py = min(max(int(round(y0 * height)), 0), max(height - 2, 0))
    pw = min(max(int(round(cw * width)), 2), width - px)
    ph = min(max(int(round(ch * height)), 2), height - py)
    px -= px % 2
    py -= py % 2
    pw -= pw % 2
    ph -= ph % 2
    face_h = max(h * height, 1.0)
    scale = LIPS_FACE_TARGET_PX / face_h
    out_w = max(2, 2 * int(round(pw * scale / 2)))
    out_h = max(2, 2 * int(round(ph * scale / 2)))
    filt = f"crop={pw}:{ph}:{px}:{py},scale={out_w}:{out_h}"
    if cube is not None:
        filt = f"lut3d='{_escape_filter_path(Path(cube))}'," + filt
    return filt


def lips_frames_command(path: Path, start_s: float, span_s: float, crop: str,
                        out_pattern: str) -> list[str]:
    """Full-rate cropped frames (with the LUT in `crop`) for the Vision pass."""
    return ["ffmpeg", "-v", "info", "-ss", str(start_s), "-i", str(path),
            "-t", str(span_s), "-map", "0:v:0", "-vf", f"{crop},showinfo",
            "-vsync", "0", "-f", "image2", "-q:v", "4", str(out_pattern)]


def choose_lips_span(duration_s: float, span_s: float = LIPS_SPAN_S,
                     samples=None, sample_rate: float = 0) -> tuple[float, float]:
    """(start, length): the `span_s` window with the most speech energy. Pure."""
    duration = max(0.0, float(duration_s))
    if duration <= 0:
        return (0.0, 0.0)
    span = min(max(1.0, float(span_s)), duration)
    if span >= duration or samples is None or sample_rate <= 0 or np is None:
        return (0.0, span)
    audio = np.asarray(samples, dtype=np.float64).ravel()
    if audio.size < 2:
        return (0.0, span)
    bins = max(1, int(duration))
    energy = np.zeros(bins)
    idx = np.minimum((np.arange(audio.size) / sample_rate).astype(int), bins - 1)
    np.add.at(energy, idx, audio * audio)
    width = max(1, int(round(span)))
    if width >= bins:
        return (0.0, span)
    cumsum = np.concatenate(([0.0], np.cumsum(energy)))
    totals = cumsum[width:] - cumsum[:-width]
    start = float(np.argmax(totals))
    return (start, min(span, duration - start))


def _lips_helper_cached(lips_cache: dict) -> Path | None:
    """The built helper, building once; None with a cached reason when not."""
    if ("helper",) not in lips_cache:
        helper, reason = build_lips_helper()
        lips_cache[("helper",)] = helper
        lips_cache[("helper_reason",)] = reason
    return lips_cache[("helper",)]


def get_lips_audio(camera: CameraInfo, lips_cache: dict):
    """Cached full-length speech-band samples for one clip, or None."""
    key = (camera.path, "lips_audio")
    if key in lips_cache:
        return lips_cache[key]
    samples = decode_lips_audio(Path(camera.path), has_audio=camera.has_audio)
    lips_cache[key] = samples
    return samples


def get_lips_track(camera: CameraInfo, lips_cache: dict, *,
                   span_s: float = LIPS_SPAN_S, cube: Path | None = None,
                   audio_samples=None) -> LipsTrack | None:
    """Inner-lip openings over the analysed span; a coarse pass finds the box.

    Frames go to a temp dir at the clip's own rate, cropped around the median
    face box with margin at full resolution (the camera LUT first when given),
    scaled so the face is ~400 px tall. Cached per clip; None when there is no
    face, no helper, or no decodable video.
    """
    key = (camera.path, "lips_track")
    if key in lips_cache:
        return lips_cache[key]
    track = _build_lips_track(camera, lips_cache, span_s=span_s, cube=cube,
                              audio_samples=audio_samples)
    lips_cache[key] = track
    return track


def _build_lips_track(camera: CameraInfo, lips_cache: dict, *,
                      span_s: float, cube: Path | None,
                      audio_samples) -> LipsTrack | None:
    if np is None or camera.width <= 0 or camera.fps <= 0:
        return None
    helper = _lips_helper_cached(lips_cache)
    if helper is None:
        return None
    duration = camera.duration_s if camera.duration_s > 0 else 0.0
    if duration <= 0:
        return None
    start, span = choose_lips_span(duration, span_s, audio_samples,
                                   LIPS_AUDIO_RATE)
    if span <= 0:
        return None
    with tempfile.TemporaryDirectory(prefix="lips-") as tmp:
        coarse_dir = Path(tmp) / "coarse"
        coarse_dir.mkdir()
        try:
            coarse = subprocess.run(
                lips_coarse_command(Path(camera.path), start, span,
                                    str(coarse_dir / "c%06d.jpg")),
                capture_output=True, timeout=600)
        except Exception:
            return None
        if coarse.returncode != 0:
            return None
        found = run_lips_helper(helper, coarse_dir)
        if found is None:
            lips_cache[("helper",)] = None
            lips_cache[("helper_reason",)] = "the Vision helper failed to run"
            return None
        box = median_box(found)
        if box is None or not found:
            return None
        face_frac = sum(1 for f in found if f.box is not None) / len(found)
        print(f"lips: clip {camera.label}: face in {face_frac:.0%} of coarse "
              f"frames; analysing ~{int(span * camera.fps)} frames over "
              f"{span:.0f} s", flush=True)
        fine_dir = Path(tmp) / "fine"
        fine_dir.mkdir()
        crop = lips_crop_filter(box, camera.width, camera.height, cube=cube)
        try:
            fine = subprocess.run(
                lips_frames_command(Path(camera.path), start, span, crop,
                                    str(fine_dir / "f%06d.jpg")),
                capture_output=True, timeout=3600)
        except Exception:
            return None
        if fine.returncode != 0:
            return None
        files = sorted(fine_dir.glob("f*.jpg"))
        if len(files) < 2:
            return None
        frames = run_lips_helper(helper, fine_dir)
        if frames is None:
            lips_cache[("helper",)] = None
            lips_cache[("helper_reason",)] = "the Vision helper failed to run"
            return None
        by_index = {f.index: f for f in frames}
        try:
            stderr = fine.stderr.decode("utf-8", errors="replace")
        except Exception:
            stderr = ""
        pts = [float(m) for m in _PTS_TIME_RE.findall(stderr)]
        if len(pts) == len(files):
            t0 = pts[0]
            times = [start + (p - t0) for p in pts]
        else:
            times = [start + k / camera.fps for k in range(len(files))]
        openings = []
        for k in range(len(files)):
            frame = by_index.get(k)
            openings.append(float("nan") if frame is None or frame.opening is None
                            else float(frame.opening))
        faces = sum(1 for o in openings if o == o)
        if faces == 0:
            return None
        return LipsTrack(times=times, opening=openings, fps=camera.fps,
                         face_frac=faces / len(openings))


def lips_pair_estimates(target: ClipResult, anchor: ClipResult, lips_cache: dict, *,
                        clock_offset_s: float | None = None,
                        span_s: float = LIPS_SPAN_S,
                        cubes: dict[str, Path] | None = None) -> list[Estimate]:
    """Target lips vs anchor lips and anchor audio over the whole overlap.

    Two views of the same mouth are the stronger signal (measured r 0.93,
    ratio 9.7), so lips-vs-lips is reported first; the clock only breaks near
    ties, never narrows the search. Pure apart from decode and Vision.
    """
    if np is None or anchor.offset_s is None:
        return []
    cubes = cubes or {}
    target_audio = get_lips_audio(target.camera, lips_cache) \
        if target.camera.has_audio else None
    track = get_lips_track(target.camera, lips_cache, span_s=span_s,
                            cube=cubes.get(target.label),
                            audio_samples=target_audio)
    if track is None:
        return []
    tgt_prep = lips_prep(track.opening, track.fps)
    if tgt_prep is None:
        return []
    anchor_audio = get_lips_audio(anchor.camera, lips_cache) \
        if anchor.camera.has_audio else None
    anchor_track = get_lips_track(anchor.camera, lips_cache, span_s=span_s,
                                  cube=cubes.get(anchor.label),
                                  audio_samples=anchor_audio)
    clock_relative = None if clock_offset_s is None \
        else clock_offset_s - anchor.offset_s
    precision = lips_precision_s(target, anchor)
    estimates: list[Estimate] = []
    if anchor_track is not None:
        anch_prep = lips_prep(anchor_track.opening, anchor_track.fps)
        if anch_prep is not None:
            lag = lips_best_lag(anchor_track.times, anch_prep,
                                track.times, tgt_prep,
                                max(track.fps, anchor_track.fps),
                                clock_relative_s=clock_relative)
            if lag is not None:
                estimates.append(Estimate(
                    method="lips", offset_s=anchor.offset_s + lag.relative_s,
                    confidence=lips_confidence(lag.ratio),
                    precision_s=precision, against=anchor.label,
                    detail={"kind": "lips-lips", "r": lag.r,
                            "rival_r": lag.rival_r, "ratio": lag.ratio,
                            "overlap_s": lag.overlap_s,
                            "face_frac": round(track.face_frac, 3),
                            "anchor_face_frac": round(anchor_track.face_frac, 3)},
                ))
    if anchor_audio is not None and len(anchor_audio) > 0:
        duration = anchor.camera.duration_s
        if duration <= 0:
            duration = len(anchor_audio) / LIPS_AUDIO_RATE
        n_frames = max(2, int(duration * track.fps))
        audio_times = [k / track.fps for k in range(n_frames)]
        per_frame = lips_audio_per_frame(anchor_audio, LIPS_AUDIO_RATE,
                                         audio_times)
        audio_prep = lips_prep(per_frame, track.fps) \
            if per_frame is not None else None
        if audio_prep is not None:
            lag = lips_best_lag(audio_times, audio_prep,
                                track.times, tgt_prep, track.fps,
                                clock_relative_s=clock_relative)
            if lag is not None:
                estimates.append(Estimate(
                    method="lips", offset_s=anchor.offset_s + lag.relative_s,
                    confidence=lips_confidence(lag.ratio),
                    precision_s=precision, against=anchor.label,
                    detail={"kind": "lips-audio", "r": lag.r,
                            "rival_r": lag.rival_r, "ratio": lag.ratio,
                            "overlap_s": lag.overlap_s,
                            "face_frac": round(track.face_frac, 3)},
                ))
    return estimates


def _lips_cube_map(lips_grade: Path | None,
                   lips_cubes: dict[str, Path | str] | None) -> dict[str, Path]:
    """Clip label to .cube path: grade.json's luts, overridden by supplied cubes."""
    out: dict[str, Path] = {}
    if lips_grade is not None:
        try:
            data = json.loads(Path(lips_grade).read_text(encoding="utf-8"))
        except Exception:
            data = {}
        base = Path(lips_grade).parent
        cameras = data.get("cameras") if isinstance(data, dict) else None
        for cam in cameras if isinstance(cameras, list) else []:
            if not isinstance(cam, dict):
                continue
            label, lut = cam.get("label"), cam.get("lut")
            if label and lut:
                candidate = base / str(lut)
                if candidate.exists():
                    out[str(label)] = candidate
    for label, cube in (lips_cubes or {}).items():
        candidate = Path(cube)
        if candidate.exists():
            out[str(label)] = candidate
    return out


# ---------------------------------------------------------------- combining

_BACKING_METHODS = ("whisper", "timecode", "flash", "lips")
_PICK_RANK = {"waveform": 0, "whisper": 1, "lips": 2, "flash": 3,
              "timecode": 4, "clock": 5}


def frame_duration_s(target: ClipResult, anchor: ClipResult) -> float:
    """One frame on the target, else the anchor, else 1/30. Pure."""
    if target.camera.fps > 0:
        return 1.0 / target.camera.fps
    if anchor.camera.fps > 0:
        return 1.0 / anchor.camera.fps
    return 1.0 / 30


def estimates_agree(a: Estimate, b: Estimate, frame_s: float) -> bool:
    """Within the larger precision plus one frame. Pure."""
    return abs(a.offset_s - b.offset_s) <= max(a.precision_s, b.precision_s) + frame_s


def build_checks(estimates: list[Estimate], frame_s: float) -> list[EstimateCheck]:
    """One check per unordered pair, in estimate-list order. Pure."""
    checks: list[EstimateCheck] = []
    for i in range(len(estimates)):
        for j in range(i + 1, len(estimates)):
            a, b = estimates[i], estimates[j]
            delta = abs(a.offset_s - b.offset_s)
            threshold = max(a.precision_s, b.precision_s) + frame_s
            checks.append(EstimateCheck(
                a=a.method, b=b.method, against_a=a.against, against_b=b.against,
                delta_s=delta, threshold_s=threshold, agree=delta <= threshold,
            ))
    return checks


def resolve_guided(global_est: Estimate, guide_est: Estimate,
                   guided_est: Estimate | None) -> tuple[Estimate, bool]:
    """Keep the global waveform peak or take the guided re-search. Pure."""
    if guided_est is None:
        return global_est, False
    if not global_est.detail.get("clear", False):
        return guided_est, True
    if guided_est.detail.get("r", 0.0) >= global_est.detail.get("r", 0.0) - GUIDE_R_TOLERANCE:
        return guided_est, True
    return global_est, False


def penalise_whisper(whisper_est: Estimate, guided_est: Estimate | None,
                     both_have_audio: bool) -> bool:
    """Halve Whisper when the words match where the sound does not. Pure."""
    if guided_est is None or not both_have_audio:
        return False
    if guided_est.detail.get("r", 0.0) >= R_CLEAR:
        return False
    whisper_est.detail["confidence_before_halving"] = whisper_est.confidence
    whisper_est.confidence = whisper_est.confidence / 2
    whisper_est.detail["confidence_halved"] = True
    return True


def _waveform_trusted(estimates: list[Estimate],
                      checks: list[EstimateCheck]) -> Estimate | None:
    """The first waveform estimate that is clear or backed. Pure."""
    by_key = {(e.method, e.against): e for e in estimates}
    for wave in (e for e in estimates if e.method == "waveform"):
        if wave.detail.get("clear", False):
            return wave
        for check in checks:
            sides = [((check.a, check.against_a), (check.b, check.against_b)),
                     ((check.b, check.against_b), (check.a, check.against_a))]
            for (mine, theirs) in sides:
                if mine != (wave.method, wave.against):
                    continue
                other = by_key.get(theirs)
                if (other is not None and other.method in _BACKING_METHODS
                        and not other.detail.get("confidence_halved", False)
                        and (other.method != "lips"
                             or other.detail.get("ratio", 0.0) >= LIPS_RATIO_LOW)
                        and check.agree):
                    return wave
    return None


def _lips_backed_waveform(estimates: list[Estimate],
                          checks: list[EstimateCheck]) -> Estimate | None:
    """First unclear waveform that a qualifying lips estimate agrees with. Pure."""
    by_key = {(e.method, e.against): e for e in estimates}
    for wave in (e for e in estimates if e.method == "waveform"):
        if wave.detail.get("clear", False):
            continue
        for check in checks:
            sides = [((check.a, check.against_a), (check.b, check.against_b)),
                     ((check.b, check.against_b), (check.a, check.against_a))]
            for (mine, theirs) in sides:
                if mine != (wave.method, wave.against):
                    continue
                other = by_key.get(theirs)
                if (other is not None and other.method == "lips"
                        and other.detail.get("ratio", 0.0) >= LIPS_RATIO_LOW
                        and check.agree):
                    return wave
    return None


def _best_lips_pick(estimates: list[Estimate]) -> Estimate | None:
    """First qualifying lips-vs-lips, else first qualifying lips-vs-audio. Pure.

    Two views of the same mouth are the stronger signal, so lips-vs-lips is
    preferred whenever it qualifies; below a ratio of 1.5 lips only
    cross-checks, never places.
    """
    qualifying = [e for e in estimates
                  if e.method == "lips"
                  and e.detail.get("ratio", 0.0) >= LIPS_RATIO_LOW]
    for kind in ("lips-lips", "lips-audio"):
        for est in qualifying:
            if est.detail.get("kind") == kind:
                return est
    return None


def choose_pick(estimates: list[Estimate],
                checks: list[EstimateCheck]) -> tuple[float | None, str | None]:
    """Waveform, Whisper, lips, flash, timecode, clock, else unplaced. Pure; order is trust."""
    trusted = _waveform_trusted(estimates, checks)
    if trusted is not None:
        return trusted.offset_s, "waveform"
    for est in estimates:
        if (est.method == "whisper" and est.detail.get("moments", 0) >= WHISPER_MIN_MOMENTS
                and est.confidence >= WHISPER_MIN_CONFIDENCE):
            return est.offset_s, "whisper"
    lips_pick = _best_lips_pick(estimates)
    if lips_pick is not None:
        return lips_pick.offset_s, "lips"
    for est in estimates:
        if est.method == "flash":
            return est.offset_s, "flash"
    for est in estimates:
        if est.method == "timecode":
            return est.offset_s, "timecode"
    for est in estimates:
        if est.method == "clock":
            return est.offset_s, "clock"
    return None, None


def attempt_warnings(target: ClipResult, estimates: list[Estimate],
                     checks: list[EstimateCheck], pick_method: str | None,
                     pick_offset: float | None) -> list[str]:
    """Plain-English warnings for one clip against its final pick. Pure."""
    warnings: list[str] = []
    if pick_method == "timecode":
        warnings.append("Placed by timecode only: right only if the cameras were jammed.")
    elif pick_method == "clock":
        warnings.append("Placed by capture clock only: check by eye.")
    if pick_method is None:
        warnings.append("Could not be placed on the timeline.")
        return warnings
    picked = next((e for e in estimates
                   if e.method == pick_method and e.offset_s == pick_offset), None)
    if (pick_method == "flash" and picked is not None
            and picked.detail.get("matched_edges", 0) <= 1):
        warnings.append("Placed by a single light flash: check by eye.")
    if pick_method == "lips" and picked is not None:
        kind = picked.detail.get("kind", "lips")
        ratio = picked.detail.get("ratio")
        if isinstance(ratio, (int, float)):
            warnings.append(
                f"Placed by lip movement ({kind} against {picked.against}, "
                f"ratio {ratio:.2f}).")
        else:
            warnings.append(
                f"Placed by lip movement ({kind} against {picked.against}).")
    seen: set[tuple[str, str]] = set()
    for est in estimates:
        if est.method not in ("timecode", "whisper", "flash", "lips"):
            continue
        if (est.method, est.against) in seen:
            continue
        seen.add((est.method, est.against))
        if est is picked:
            continue
        matched: EstimateCheck | None = None
        if picked is not None:
            for check in checks:
                pairs = {(check.a, check.against_a), (check.b, check.against_b)}
                if (picked.method, picked.against) in pairs \
                        and (est.method, est.against) in pairs:
                    matched = check
                    break
        if matched is not None:
            disagreeing = not matched.agree
        else:
            frame_s = 1.0 / target.camera.fps if target.camera.fps > 0 else 1.0 / 30
            precision = max(est.precision_s,
                            picked.precision_s if picked is not None else 0.0)
            disagreeing = pick_offset is None or \
                abs(est.offset_s - pick_offset) > precision + frame_s
        if disagreeing and pick_offset is not None:
            name = est.method.capitalize()
            line = f"{name} disagrees with the chosen offset by {abs(est.offset_s - pick_offset):.3f} s."
            if est.method == "timecode" and pick_method != "timecode":
                line += " It is right only if the cameras were jammed."
            warnings.append(line)
    frame_s = 1.0 / target.camera.fps if target.camera.fps > 0 else 1.0 / 30
    drift_sources = [e for e in estimates if e.method == "waveform" and e.detail.get("drift_ppm")]
    if picked is not None and picked.method == "waveform" and picked.detail.get("drift_ppm"):
        drift_sources = [picked]
    elif drift_sources:
        drift_sources = drift_sources[:1]
    for source in drift_sources:
        ppm = float(source.detail["drift_ppm"])
        moved_s = abs(ppm) * 1e-6 * target.camera.duration_s
        if moved_s > 0.5 * frame_s:
            warnings.append(
                f"Clock drift {ppm:+.0f} ppm moves sync by {moved_s * 1000:.0f} ms "
                "over this clip (more than half a frame); "
                "the offset is exact at the clip start.")
    return warnings


# ---------------------------------------------------------------- orchestration

WAVEFORM_NO_NUMPY = "Waveform sync unavailable: numpy is not installed."
FLASH_NO_NUMPY = "Flash sync unavailable: numpy is not installed."
LIPS_NO_NUMPY = "Lip sync unavailable: numpy is not installed."


def parse_methods(text: str | None) -> list[str]:
    """Requested method names in canonical order; None means all of them."""
    if text is None:
        return list(METHODS)
    names = [part.strip().lower() for part in text.split(",")]
    names = [name for name in names if name]
    for name in names:
        if name not in METHODS:
            raise ValueError(f"unknown sync method: {name}")
    if not names:
        raise ValueError("no sync methods")
    return [m for m in METHODS if m in set(names)]


def _round(value: float | None, places: int):
    return None if value is None else round(float(value), places)


def _estimate_payload(est: Estimate) -> dict:
    detail = dict(est.detail)
    for key in ("r", "second_r", "margin", "overlap_s", "rival_r", "ratio",
                "confidence_before_halving"):
        if isinstance(detail.get(key), (int, float)):
            detail[key] = round(float(detail[key]), 3)
    if isinstance(detail.get("drift_ppm"), (int, float)):
        detail["drift_ppm"] = round(float(detail["drift_ppm"]), 1)
    unguided = detail.get("unguided")
    if isinstance(unguided, dict):
        fixed = dict(unguided)
        for key in ("offset_s", "r", "margin"):
            if isinstance(fixed.get(key), (int, float)):
                fixed[key] = round(float(fixed[key]), 3)
        detail["unguided"] = fixed
    return {"method": est.method, "offset_s": _round(est.offset_s, 3),
            "confidence": _round(est.confidence, 3),
            "precision_s": _round(est.precision_s, 3),
            "against": est.against, "detail": detail}


def build_payload(results: list[ClipResult], reference_path: str,
                  methods: list[str]) -> dict:
    """The sync.json payload; rounding happens only here. Pure."""
    clips = []
    for res in results:
        cam = res.camera
        clips.append({
            "label": res.label,
            "offset_s": _round(res.offset_s, 3),
            "method": res.method,
            "camera": {"path": cam.path, "make": cam.make, "model": cam.model,
                       "lens": cam.lens, "fps": _round(cam.fps, 3),
                       "width": cam.width, "height": cam.height,
                       "duration_s": _round(cam.duration_s, 3),
                       "has_audio": cam.has_audio, "gamma": cam.gamma,
                       "timecode": cam.timecode, "tc_rate": cam.tc_rate,
                       "captured_at": cam.captured_at},
            "estimates": [_estimate_payload(e) for e in res.estimates],
            "checks": [{"a": c.a, "b": c.b, "against_a": c.against_a,
                        "against_b": c.against_b, "delta_s": _round(c.delta_s, 3),
                        "threshold_s": _round(c.threshold_s, 3), "agree": c.agree}
                       for c in res.checks],
            "drift_ppm": _round(res.drift_ppm, 1),
            "warnings": list(res.warnings),
        })
    return {"reference": reference_path, "methods": list(methods), "clips": clips}


def _attempt_estimates(target: ClipResult, anchor: ClipResult, methods: list[str],
                       envelopes: dict, words: dict[str, list[TranscriptWord]],
                       transcripts_dir: Path | None, cache_dir: Path,
                       whisper_model: str = WHISPER_MODEL,
                       flash_cache: dict | None = None) -> list[Estimate]:
    """One anchor's estimates in canonical method order."""
    estimates: list[Estimate] = []
    if "timecode" in methods:
        est = timecode_estimate(target, anchor)
        if est is not None:
            estimates.append(est)
    if "clock" in methods:
        est = clock_estimate(target, anchor)
        if est is not None:
            estimates.append(est)
    if "waveform" in methods and np is not None:
        est = waveform_estimate(target, anchor, envelopes)
        if est is not None:
            estimates.append(est)
    if "flash" in methods and np is not None:
        cache = flash_cache if flash_cache is not None else {}
        clock_hint = next((e.offset_s for e in estimates if e.method == "clock"), None)
        est = flash_estimate(target, anchor, cache, clock_offset_s=clock_hint)
        if est is not None:
            estimates.append(est)
    if "whisper" in methods:
        if target.camera.path not in words:
            words[target.camera.path] = get_transcript_words(
                target.camera, transcripts_dir, cache_dir, whisper_model)
        if anchor.camera.path not in words:
            words[anchor.camera.path] = get_transcript_words(
                anchor.camera, transcripts_dir, cache_dir, whisper_model)
        est = whisper_estimate(target, anchor, words[target.camera.path],
                               words[anchor.camera.path])
        if est is not None:
            estimates.append(est)
    return estimates


def run(*, clip_paths: list[Path], reference_path: Path | None,
        methods: list[str], transcripts_dir: Path | None, out_path: Path,
        whisper_model: str = WHISPER_MODEL,
        lips_span_s: float = LIPS_SPAN_S,
        lips_grade: Path | None = None,
        lips_cubes: dict[str, Path | str] | None = None) -> dict:
    """Place every clip on the reference's timeline; write sync.json."""
    paths = [Path(p) for p in clip_paths]
    if len(paths) < 2:
        raise ValueError("need at least two clips")
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(f"clip not found: {path}")
    resolved = [p.resolve() for p in paths]
    if len(set(resolved)) != len(resolved):
        raise ValueError("the same clip was given twice")
    cameras: list[CameraInfo] = []
    for path in paths:
        probe = probe_clip(path)
        sidecar = parse_sony_sidecar(find_sidecar(path))
        cameras.append(camera_from_probe("", str(path), probe, sidecar))
    if reference_path is not None:
        wanted = Path(reference_path).resolve()
        ref_idx = next((i for i, r in enumerate(resolved) if r == wanted), None)
        if ref_idx is None:
            raise ValueError(f"--reference is not among the clips: {reference_path}")
    else:
        ref_idx = max(range(len(cameras)),
                      key=lambda i: (cameras[i].duration_s, -i))
    labels: dict[int, str] = {ref_idx: "A"}
    others = [i for i in range(len(paths)) if i != ref_idx]
    for order, i in enumerate(others):
        labels[i] = _label_for_index(order + 1)
    for i, cam in enumerate(cameras):
        cam.label = labels[i]
    reference = ClipResult(label="A", camera=cameras[ref_idx], offset_s=0.0,
                           method="reference")
    results = [reference]
    envelopes: dict = {}
    words: dict[str, list[TranscriptWord]] = {}
    flash_cache: dict = {}
    lips_cache: dict = {}
    # Supplied cubes may name a clip by its file path (callers don't know the labels yet).
    path_to_label = {str(Path(p).resolve()): labels[i] for i, p in enumerate(paths)}
    cubes_by_label = {path_to_label.get(str(Path(key).resolve()), key): cube
                      for key, cube in (lips_cubes or {}).items()}
    lips_cube_map = _lips_cube_map(lips_grade, cubes_by_label)
    lips_crosscheck_done = False
    out_path = Path(out_path)
    cache_dir = out_path.parent / "sync-transcripts"
    order_of = {labels[i]: i for i in range(len(paths))}
    for i in sorted(others, key=lambda k: order_of[labels[k]]):
        target = ClipResult(label=labels[i], camera=cameras[i],
                            offset_s=None, method=None)
        anchors = [reference] + [res for res in results[1:] if res.offset_s is not None]
        attempts: list[tuple[ClipResult, list[Estimate], list[EstimateCheck]]] = []
        accepted: tuple[list[Estimate], list[EstimateCheck],
                        float | None, str | None] | None = None
        fallback: tuple[list[Estimate], list[EstimateCheck],
                        float | None, str | None] | None = None
        halved_notes: list[str] = []
        for anchor in anchors:
            estimates = _attempt_estimates(target, anchor, methods, envelopes,
                                           words, transcripts_dir, cache_dir,
                                           whisper_model, flash_cache)
            frame_s = frame_duration_s(target, anchor)
            global_wave = next((e for e in estimates if e.method == "waveform"), None)
            whispers = [e for e in estimates if e.method == "whisper"]
            guide = whispers[0] if whispers else next(
                (e for e in estimates if e.method in ("timecode", "clock")), None)
            if global_wave is not None and guide is not None \
                    and not estimates_agree(global_wave, guide, frame_s):
                guided = waveform_estimate(
                    target, anchor, envelopes,
                    guide=(guide.offset_s - anchor.offset_s,
                           guide.precision_s + GUIDE_EXTRA_S, guide.method))
                chosen, taken = resolve_guided(global_wave, guide, guided)
                if guided is not None and guide.method == "whisper" \
                        and guided.detail.get("r", 0.0) < R_CLEAR \
                        and target.camera.has_audio and anchor.camera.has_audio:
                    whispered = next(e for e in estimates if e.method == "whisper")
                    if penalise_whisper(whispered, guided, True):
                        halved_notes.append(
                            "Whisper matches words that do not match in sound "
                            f"(guided waveform r={guided.detail.get('r', 0.0):.2f}); "
                            "Whisper confidence halved.")
                if taken and guided is not None:
                    chosen.detail["unguided"] = {
                        "offset_s": global_wave.offset_s,
                        "r": global_wave.detail.get("r"),
                        "margin": global_wave.detail.get("margin")}
                    estimates[estimates.index(global_wave)] = chosen
            checks = build_checks(estimates, frame_s)
            pick_offset, pick_method = choose_pick(estimates, checks)
            attempts.append((anchor, estimates, checks))
            if pick_method == "waveform" and _waveform_trusted(estimates, checks) is not None:
                accepted = (estimates, checks, pick_offset, pick_method)
                break
            if fallback is None and pick_method is not None:
                fallback = (estimates, checks, pick_offset, pick_method)
        # Lips runs only where the audio methods did not place the clip (plus
        # one cross-check on an audio-placed camera): Vision over hundreds of
        # seconds is the dearest step in the run.
        lips_added: list[Estimate] = []
        lips_tried = False
        if ("lips" in methods and target.camera.width > 0
                and target.camera.fps > 0):
            if accepted is not None:
                provisional = accepted[3]
            elif fallback is not None:
                provisional = fallback[3]
            else:
                provisional = None
            if provisional in ("waveform", "whisper"):
                lips_anchors = [] if lips_crosscheck_done else [reference]
                crosscheck = bool(lips_anchors)
            else:
                lips_anchors = list(anchors)
                crosscheck = False
            lips_tried = bool(lips_anchors)
            if lips_anchors and np is not None:
                clock_by_anchor: dict[str, float | None] = {}
                for anchor, estimates, _checks in attempts:
                    clock_est = next((e for e in estimates
                                      if e.method == "clock"), None)
                    clock_by_anchor[anchor.label] = clock_est.offset_s \
                        if clock_est is not None else None
                for anchor in lips_anchors:
                    lips_added.extend(lips_pair_estimates(
                        target, anchor, lips_cache,
                        clock_offset_s=clock_by_anchor.get(anchor.label),
                        span_s=lips_span_s, cubes=lips_cube_map))
                if crosscheck:
                    lips_crosscheck_done = True
        union: list[Estimate] = []
        seen_keys: set[tuple[str, str]] = set()
        for _anchor, estimates, _checks in attempts:
            for est in estimates:
                if est.method in ("timecode", "clock"):
                    if (est.method, est.against) in seen_keys:
                        continue
                    seen_keys.add((est.method, est.against))
                union.append(est)
        union.extend(lips_added)
        ref_frame = frame_duration_s(target, reference)
        final_checks = build_checks(union, ref_frame)
        if accepted is not None:
            _est, _chk, pick_offset, pick_method = accepted
        else:
            upgraded = _lips_backed_waveform(union, final_checks) \
                if lips_added else None
            lips_pick = _best_lips_pick(union) if lips_added else None
            if upgraded is not None:
                pick_offset, pick_method = upgraded.offset_s, "waveform"
            elif lips_pick is not None and (
                    fallback is None or _PICK_RANK["lips"]
                    < _PICK_RANK.get(fallback[3], 99)):
                pick_offset, pick_method = lips_pick.offset_s, "lips"
            elif fallback is not None:
                _est, _chk, pick_offset, pick_method = fallback
            else:
                pick_offset, pick_method = None, None
        target.estimates = union
        target.checks = final_checks
        target.offset_s = pick_offset
        target.method = pick_method
        target.warnings = attempt_warnings(target, union, final_checks,
                                           pick_method, pick_offset) + halved_notes
        picked_wave = next((e for e in union if e.method == "waveform"
                            and e.offset_s == pick_offset and pick_method == "waveform"),
                           None)
        if picked_wave is not None and picked_wave.detail.get("drift_ppm") is not None:
            target.drift_ppm = picked_wave.detail["drift_ppm"]
        else:
            drifted = next((e for e in union if e.method == "waveform"
                            and e.detail.get("drift_ppm") is not None), None)
            target.drift_ppm = drifted.detail["drift_ppm"] if drifted else None
        if "waveform" in methods and np is None:
            target.warnings.append(WAVEFORM_NO_NUMPY)
        elif ("waveform" in methods and not target.camera.has_audio
                and not any(e.method == "waveform" for e in union)):
            target.warnings.append(
                f"Clip {target.label} has no audio: waveform sync skipped.")
        if "flash" in methods and np is None:
            target.warnings.append(FLASH_NO_NUMPY)
        elif ("flash" in methods and target.camera.width <= 0
                and not any(e.method == "flash" for e in union)):
            target.warnings.append(
                f"Clip {target.label} has no video: flash sync skipped.")
        if "lips" in methods and lips_tried and np is None:
            target.warnings.append(LIPS_NO_NUMPY)
        elif ("lips" in methods and lips_tried
                and lips_cache.get(("helper",)) is None):
            reason = lips_cache.get(("helper_reason",)) \
                or "the Vision helper is unavailable"
            target.warnings.append(f"Lip sync unavailable: {reason}.")
        has_flash = any(e.method == "flash" for e in union)
        if not target.camera.has_audio and not has_flash:
            if pick_method == "clock":
                target.warnings.append(
                    f"Clip {target.label} has no audio and no light flash was found: "
                    "placed by capture clock only (±2 s); "
                    "lip sync on its shots will be off.")
            elif pick_method == "timecode":
                target.warnings.append(
                    f"Clip {target.label} has no audio and no light flash was found: "
                    "placed by timecode only (right only if the cameras were jammed); "
                    "lip sync on its shots will be off.")
            elif pick_method is None:
                target.warnings.append(
                    f"Clip {target.label} has no audio and no light flash was found: "
                    "could not be placed on the timeline.")
        if ("whisper" in methods
                and not any(e.method == "whisper" for e in union)
                and not words.get(target.camera.path)
                and not any(words.get(a.camera.path) for a, _, _ in attempts)):
            target.warnings.append("No word-timed transcript available: Whisper sync skipped.")
        results.append(target)
    payload = build_payload(results, str(paths[ref_idx]), methods)
    write_json(out_path, payload)
    return payload
