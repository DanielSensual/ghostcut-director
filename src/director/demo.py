"""Try it in one command, no footage needed: `director demo`.

Builds a tiny synthetic shoot in the output folder — a lav WAV plus three
cameras, all carrying the same speech with known offsets — runs the real
`director cut` pipeline on it, and checks the result against the known
answers: the sync offsets (within one frame), the restart that take picking
should remove, and the files a finished job should contain. Offline by
default (`--no-muse --no-sam`, the script's own word timings unless local
Whisper is available); `--muse` with `MODEL_API_KEY` set runs the
Muse/SAM path instead.

The speech is rendered with macOS `say` when available, else
`espeak-ng`/`espeak`, else a tone-burst stand-in with exact word timings
(and a printed note that the real voice needs `say` or espeak).

Built by Muse Code on muse-spark-1.3 from docs/muse-code/DEMO-BRIEF.md.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path

from ghostcut.config import read_json, write_json

# ------------------------------------------------------------------ the script
#
# Eight utterances, about 39 s at the tone cadence. Utterance FLUB_UTTERANCE
# is the deliberate restart: it carries no terminal period, so take picking
# (the rule fallback or Muse) reads it as the abandoned first pass of the
# next utterance's beat and removes it. Every utterance stays under
# tighten.MAX_UTTERANCE_S (6 s) so none is split mid-thought.

SCRIPT: tuple[tuple[str, ...], ...] = (
    ("This", "is", "the", "GhostCut", "Director", "demo."),
    ("Two", "cameras", "and", "a", "lav."),
    ("GhostCut", "Director", "syncs", "every", "camera",
     "sorry,", "let", "me", "do", "that", "again"),
    ("GhostCut", "Director", "syncs", "every", "camera",
     "on", "your", "shoot."),
    ("It", "picks", "your", "best", "take,",
     "and", "hands", "you", "a", "timeline."),
    ("Every", "line", "gets", "the", "angle", "it", "deserves."),
    ("In", "horizontal", "and", "vertical."),
    ("Open", "the", "timeline", "and", "make", "it", "yours."),
)
FLUB_UTTERANCE = 2

LEAD_S = 3.0
TAIL_S = 2.0
WORD_DUR_S = 0.46
WORD_GAP_S = 0.12
FLUB_WORD_DUR_S = 0.40
FLUB_WORD_GAP_S = 0.08
UTTERANCE_PAUSE_S = 1.0

SAY_RATE_WPM = 130
ESPEAK_WPM = 130
# A rendered voice shorter than this is treated as a failed backend (a
# sandboxed `say` exits 0 with an empty file) and the next voice is tried.
MIN_VOICE_S = 5.0

SAMPLE_RATE = 44100
TONE_AMPLITUDE = 0.7
TONE_RAMP_S = 0.010
TONE_FREQS = (392.00, 523.25, 440.00, 659.25, 493.88,
              587.33, 349.23, 415.30, 554.37, 466.16)

FPS = 30.0
FRAME_S = 1.0 / FPS

CAMERA_OFFSETS = {"cam-a.mp4": 1.0, "cam-b.mp4": 4.0}
SILENT_CAMERAS = {"cam-c.mp4": 2.0}
CAMERA_GAIN = 0.5

RESTART_KINDS = frozenset({"flub", "restart", "alternate"})

LABEL_FONTS = (
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)


class DemoError(RuntimeError):
    """The demo shoot cannot be built or checked (message is user-facing)."""


@dataclass(slots=True)
class PlannedWord:
    text: str
    start_s: float
    end_s: float


@dataclass(slots=True)
class Shoot:
    dir: Path
    lav: Path
    cameras: list[Path]
    silent: list[Path]
    words_path: Path
    voice_backend: str
    known_offsets: dict[str, float]
    lav_duration_s: float


@dataclass(slots=True)
class OffsetCheck:
    name: str
    label: str
    known_s: float | None
    found_s: float | None
    method: str | None
    passed: bool
    note: str


# ------------------------------------------------------- script and word times


def script_text() -> str:
    """The whole script as one string, with natural sentence breaks for speech."""
    parts = [" ".join(utterance) for utterance in SCRIPT]
    parts[FLUB_UTTERANCE] += "."
    return " ".join(parts)


def script_hash() -> str:
    """Short hash of the script; a changed script invalidates a kept shoot."""
    digest = hashlib.sha256(
        json.dumps(SCRIPT, separators=(",", ":")).encode("utf-8")).hexdigest()
    return digest[:16]


def plan_word_times() -> list[PlannedWord]:
    """Lav-local word times for the script: speech starts after LEAD_S. Pure."""
    out: list[PlannedWord] = []
    time_s = LEAD_S
    for index, utterance in enumerate(SCRIPT):
        if index == FLUB_UTTERANCE:
            duration, gap = FLUB_WORD_DUR_S, FLUB_WORD_GAP_S
        else:
            duration, gap = WORD_DUR_S, WORD_GAP_S
        for pos, text in enumerate(utterance):
            out.append(PlannedWord(text, time_s, time_s + duration))
            time_s += duration + (gap if pos + 1 < len(utterance)
                                  else UTTERANCE_PAUSE_S)
    return out


def speech_span_s(words: list[PlannedWord]) -> float:
    """First word start to last word end. Pure."""
    return words[-1].end_s - words[0].start_s if words else 0.0


def lav_duration_s(words: list[PlannedWord]) -> float:
    """Lead + speech + tail for the master timeline. Pure."""
    return words[-1].end_s + TAIL_S if words else LEAD_S + TAIL_S


def fit_word_times(planned: list[PlannedWord], measured_s: float) -> list[PlannedWord]:
    """Scale the planned grid onto a rendered voice of measured_s seconds.

    `say`/espeak speak at their own rate, so the plan's speech span maps
    linearly onto [LEAD_S, LEAD_S + measured_s]. Pure.
    """
    span = speech_span_s(planned)
    if span <= 0 or measured_s <= 0:
        raise DemoError("demo: cannot fit word times to an empty voice track")
    scale = measured_s / span
    first = planned[0].start_s
    return [PlannedWord(word.text, LEAD_S + (word.start_s - first) * scale,
                        LEAD_S + (word.end_s - first) * scale)
            for word in planned]


# ------------------------------------------------------------------ voice


def find_voice_backend() -> str:
    """'say', 'espeak-ng', 'espeak', or 'tone' — the first available. Pure-ish."""
    if shutil.which("say") is not None:
        return "say"
    if shutil.which("espeak-ng") is not None:
        return "espeak-ng"
    if shutil.which("espeak") is not None:
        return "espeak"
    return "tone"


def say_command(text: str, out_path: Path) -> list[str]:
    return ["say", "-r", str(SAY_RATE_WPM), "-o", str(out_path), text]


def espeak_command(exe: str, text: str, out_path: Path) -> list[str]:
    return [exe, "-s", str(ESPEAK_WPM), "-w", str(out_path), text]


def synth_tone_speech(words: list[PlannedWord]) -> list[float]:
    """One sine burst per word at lav-local times, silence elsewhere. Pure."""
    import math

    total = int(round(lav_duration_s(words) * SAMPLE_RATE))
    pcm = [0.0] * total
    ramp = max(1, int(round(TONE_RAMP_S * SAMPLE_RATE)))
    for index, word in enumerate(words):
        freq = TONE_FREQS[index % len(TONE_FREQS)]
        start = int(round(word.start_s * SAMPLE_RATE))
        end = int(round(word.end_s * SAMPLE_RATE))
        count = end - start
        for i in range(count):
            env = min(1.0, i / ramp, (count - i) / ramp)
            pcm[start + i] = (math.sin(2 * math.pi * freq * (i / SAMPLE_RATE))
                              * TONE_AMPLITUDE * max(0.0, env))
    return pcm


def decode_pcm_mono(path: Path, sample_rate: int = SAMPLE_RATE) -> list[float]:
    """Any audio file to mono float samples. Raises DemoError when ffmpeg fails."""
    try:
        done = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1",
             "-ar", str(sample_rate), "-f", "s16le", "-"],
            capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DemoError(f"demo: cannot decode {path.name}: {exc}") from exc
    if done.returncode != 0 or not done.stdout:
        raise DemoError(f"demo: cannot decode {path.name}: ffmpeg exited "
                        f"{done.returncode}")
    raw = array("h")
    raw.frombytes(done.stdout)
    scale = 1.0 / 32768.0
    return [sample * scale for sample in raw]


def trim_silence(pcm: list[float], *, keep_s: float = 0.05) -> list[float]:
    """Drop leading/trailing near-silence, keeping a short pad. Pure."""
    threshold = 200.0 / 32768.0
    keep = int(round(keep_s * SAMPLE_RATE))
    start = next((i for i, sample in enumerate(pcm)
                  if abs(sample) >= threshold), len(pcm))
    end = next((i for i in range(len(pcm) - 1, -1, -1)
                if abs(pcm[i]) >= threshold), -1) + 1
    if end <= start:
        return list(pcm)
    return pcm[max(0, start - keep):min(len(pcm), end + keep)]


def write_wav(path: Path, pcm: list[float],
              sample_rate: int = SAMPLE_RATE) -> None:
    """Mono 16-bit WAV from float samples. Stdlib only."""
    data = array("h", (max(-32768, min(32767, int(round(sample * 32767))))
                       for sample in pcm))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(data.tobytes())


def render_speech(planned: list[PlannedWord], tmp: Path, *,
                  first_choice: str) -> tuple[list[PlannedWord], list[float], str]:
    """(words, speech PCM, backend used), falling through broken voices.

    Tries first_choice, then any other installed speech backend, then the
    tone-burst stand-in, which always succeeds. A backend that errors or
    produces under MIN_VOICE_S of audio (a sandboxed `say` exits 0 with an
    empty file) is skipped with a printed note.
    """
    candidates = [first_choice]
    if first_choice != "tone":
        for name in ("say", "espeak-ng", "espeak"):
            if name != first_choice and shutil.which(name) is not None:
                candidates.append(name)
        candidates.append("tone")
    for candidate in candidates:
        if candidate == "tone":
            print("demo: voice: tone-burst stand-in (the real voice needs "
                  "`say` on macOS or espeak)")
            return planned, synth_tone_speech(planned), "tone"
        try:
            if candidate == "say":
                rendered = tmp / "voice.aiff"
                _run_or_demo_error(say_command(script_text(), rendered),
                                   "say voice render")
            else:
                rendered = tmp / "voice.wav"
                _run_or_demo_error(
                    espeak_command(candidate, script_text(), rendered),
                    f"{candidate} voice render")
            decoded = trim_silence(decode_pcm_mono(rendered))
        except DemoError as exc:
            print(f"demo: voice {candidate} failed ({exc}); "
                  f"trying the next voice")
            continue
        if len(decoded) / SAMPLE_RATE < MIN_VOICE_S:
            print(f"demo: voice {candidate} produced no usable audio; "
                  f"trying the next voice")
            continue
        print(f"demo: voice: {candidate}")
        words = fit_word_times(planned, len(decoded) / SAMPLE_RATE)
        return words, decoded, candidate
    raise AssertionError("tone never fails")  # unreachable


def probe_voice(backend: str, tmp: Path) -> bool:
    """Whether `backend` renders one spoken word to usable audio."""
    try:
        if backend == "say":
            rendered = tmp / "probe.aiff"
            _run_or_demo_error(say_command("demo", rendered), "say probe")
        else:
            rendered = tmp / "probe.wav"
            _run_or_demo_error(espeak_command(backend, "demo", rendered),
                               f"{backend} probe")
        decoded = decode_pcm_mono(rendered)
    except DemoError:
        return False
    return len(decoded) / SAMPLE_RATE >= 0.3


def effective_voice_backend(first_choice: str, tmp: Path) -> str:
    """The first voice that renders the one-word probe, else tone."""
    if first_choice == "tone":
        return "tone"
    candidates = [first_choice]
    for name in ("say", "espeak-ng", "espeak"):
        if name != first_choice and shutil.which(name) is not None:
            candidates.append(name)
    for candidate in candidates:
        if probe_voice(candidate, tmp):
            return candidate
        print(f"demo: voice {candidate} produced no usable audio; "
              f"trying the next voice")
    return "tone"


def slice_master(master: list[float], offset_s: float, gain: float = 1.0) -> list[float]:
    """The master timeline from offset_s on, at gain. Pure."""
    start = int(round(offset_s * SAMPLE_RATE))
    return [sample * gain for sample in master[start:]]


# ------------------------------------------------------------------ video


def find_label_font() -> Path | None:
    """A drawtext fontfile, or None when no known font exists. Pure-ish."""
    for candidate in LABEL_FONTS:
        if Path(candidate).exists():
            return Path(candidate)
    try:
        done = subprocess.run(["fc-match", "-f", "%{file}\n", "sans"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode == 0:
        for line in (done.stdout or "").splitlines():
            if line.strip() and Path(line.strip()).exists():
                return Path(line.strip())
    return None


def _filter_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def drawtext_filter(label: str, font: Path) -> str:
    """Bottom-centred label plate for a camera picture. Pure."""
    return ("drawtext=fontfile='{}':text='{}':fontsize=84:fontcolor=white:"
            "box=1:boxcolor=black@0.6:boxborderw=16:"
            "x=(w-text_w)/2:y=h-170".format(_filter_escape(str(font)),
                                            _filter_escape(label)))


def camera_filter(name: str, font: Path) -> str:
    """The lavfi video filter for one camera file: colour, framing, label. Pure."""
    label = "CAMERA " + name.removeprefix("cam-").removesuffix(".mp4").upper()
    plate = drawtext_filter(label, font)
    if name == "cam-a.mp4":
        return plate
    if name == "cam-b.mp4":
        return f"hue=h=90,crop=1280:720:320:180,scale=1920:1080,{plate}"
    return f"hue=h=180,crop=960:540:480:270,scale=1920:1080,{plate}"


def camera_command(*, duration_s: float, audio_path: Path | None,
                   video_filter: str, out_path: Path) -> list[str]:
    """One camera file: testsrc2 picture, the speech slice, metadata stripped."""
    command = ["ffmpeg", "-v", "error", "-y",
               "-f", "lavfi", "-i",
               f"testsrc2=s=1920x1080:r=30:d={duration_s:.3f}"]
    if audio_path is not None:
        command += ["-i", str(audio_path)]
    command += ["-vf", video_filter, "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio_path is not None:
        command += ["-c:a", "aac"]
    else:
        command += ["-an"]
    return command + ["-map_metadata", "-1", "-shortest", str(out_path)]


# ------------------------------------------------------------------ the shoot


def read_shoot_spec(shoot_dir: Path) -> dict | None:
    """The stored shoot spec, or None when missing/unreadable."""
    try:
        data = read_json(Path(shoot_dir) / "demo-shoot.json")
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def shoot_clips_present(shoot_dir: Path) -> bool:
    """All four clips exist."""
    names = ["lav.wav", *CAMERA_OFFSETS, *SILENT_CAMERAS]
    return all((Path(shoot_dir) / name).exists() for name in names)


def _run_or_demo_error(command: list[str], what: str) -> None:
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DemoError(f"demo: {what} failed: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or "").strip().splitlines()
        tail = f": {detail[-1]}" if detail else ""
        raise DemoError(f"demo: {what} failed (exit {done.returncode}){tail}")


def build_shoot(shoot_dir: Path, *, voice_backend: str,
                keep: bool) -> Shoot:
    """Render the lav + three cameras, or reuse them under --keep."""
    shoot_dir = Path(shoot_dir)
    shoot_dir.mkdir(parents=True, exist_ok=True)
    tmp = shoot_dir / "tmp"
    tmp.mkdir(exist_ok=True)
    if keep and shoot_clips_present(shoot_dir):
        effective = effective_voice_backend(voice_backend, tmp)
        spec = read_shoot_spec(shoot_dir)
        if (spec is not None and spec.get("script") == script_hash()
                and spec.get("voice_backend") == effective
                and (shoot_dir / "demo-words.json").exists()):
            print(f"demo: reusing the shoot in {shoot_dir} (--keep)")
            return Shoot(
                dir=shoot_dir, lav=shoot_dir / "lav.wav",
                cameras=[shoot_dir / name for name in CAMERA_OFFSETS],
                silent=[shoot_dir / name for name in SILENT_CAMERAS],
                words_path=shoot_dir / "demo-words.json",
                voice_backend=effective,
                known_offsets={**CAMERA_OFFSETS, **SILENT_CAMERAS},
                lav_duration_s=float(spec.get("lav_duration_s") or 0.0))
        print(f"demo: rebuilding the shoot in {shoot_dir} "
              f"(the kept one is from another voice or script)")
    try:
        import numpy  # noqa: F401 (waveform sync needs it; fail fast, not in QC)
    except ImportError as exc:
        raise DemoError("demo: needs the 'director' extra (numpy): "
                        "pip install -e \".[director]\"") from exc
    font = find_label_font()
    if font is None:
        raise DemoError("demo: no label font found (looked for Helvetica, "
                        "Arial and DejaVuSans, then fc-match sans)")
    planned = plan_word_times()
    words, speech, used_backend = render_speech(
        planned, tmp, first_choice=voice_backend)
    master = ([0.0] * int(round(LEAD_S * SAMPLE_RATE)) + speech
              + [0.0] * int(round(TAIL_S * SAMPLE_RATE)))
    lav_duration = len(master) / SAMPLE_RATE
    write_wav(shoot_dir / "lav.wav", master)
    from .cut import words_file_payload
    from .angles import TimedWord

    write_json(shoot_dir / "demo-words.json", words_file_payload(
        [TimedWord(word.text, word.start_s, word.end_s) for word in words]))
    (shoot_dir / "script.txt").write_text(script_text() + "\n", encoding="utf-8")
    for name, offset in CAMERA_OFFSETS.items():
        track = tmp / f"{Path(name).stem}.wav"
        write_wav(track, slice_master(master, offset, CAMERA_GAIN))
        _run_or_demo_error(camera_command(
            duration_s=lav_duration - offset, audio_path=track,
            video_filter=camera_filter(name, font),
            out_path=shoot_dir / name), f"camera {name} render")
    for name, offset in SILENT_CAMERAS.items():
        _run_or_demo_error(camera_command(
            duration_s=lav_duration - offset, audio_path=None,
            video_filter=camera_filter(name, font),
            out_path=shoot_dir / name), f"camera {name} render")
    write_json(shoot_dir / "demo-shoot.json", {
        "script": script_hash(), "voice_backend": used_backend,
        "lav_duration_s": round(lav_duration, 3),
        "known_offsets": {**CAMERA_OFFSETS, **SILENT_CAMERAS},
        "words_file": "demo-words.json"})
    return Shoot(
        dir=shoot_dir, lav=shoot_dir / "lav.wav",
        cameras=[shoot_dir / name for name in CAMERA_OFFSETS],
        silent=[shoot_dir / name for name in SILENT_CAMERAS],
        words_path=shoot_dir / "demo-words.json",
        voice_backend=used_backend,
        known_offsets={**CAMERA_OFFSETS, **SILENT_CAMERAS},
        lav_duration_s=lav_duration)


# ------------------------------------------------------------------ the pipeline


def whisper_available() -> bool:
    """Local Whisper can transcribe: on PATH with a visible Metal GPU.

    Mirrors the guard `cut` itself uses, so the demo chooses script words
    exactly when the pipeline would have no Whisper to run.
    """
    from . import sync as sync_mod

    return (shutil.which("mlx_whisper") is not None
            and sync_mod.metal_visible())


def muse_available() -> bool:
    """A Meta key is set for the --muse path."""
    return bool(os.environ.get("MODEL_API_KEY"))


def run_pipeline(*, shoot: Shoot, job_dir: Path, use_muse: bool,
                 allow_whisper: bool) -> tuple[dict, str]:
    """The real `director cut` on the shoot; (cut.json payload, words note)."""
    from . import cut as cut_mod
    from . import sync as sync_mod

    if allow_whisper and whisper_available():
        words_path: Path | None = None
        words_note = "Whisper words from the local mlx_whisper"
    else:
        words_path = shoot.words_path
        words_note = "script word timings (no local Whisper here)"
    common = dict(
        clips=[shoot.dir], out_dir=Path(job_dir),
        reference_path=shoot.lav, audio_path=None, transcripts_dir=None,
        methods=sync_mod.parse_methods(None), no_grade=False,
        grade_check=False, no_muse=not use_muse, model="muse-spark-1.3",
        min_shot_s=2.0, no_vertical=False, no_sam=not use_muse,
        no_export=False, no_qc=False, max_usd=5.0, force=False,
        project_name="Director demo", no_tighten=False, pause_limit_s=0.8,
        target_s=None, no_transcribe=False, no_finish=False,
        finish_sound=False, finish_isolate=False, finish_loudness=None,
        max_provider_usd=2.0, lut_patterns=[])
    try:
        payload = cut_mod.run(words_path=words_path, **common)  # type: ignore[arg-type]
    except cut_mod.CutError as exc:
        if words_path is None and str(exc).startswith("words: "):
            print("demo: local Whisper failed; falling back to the script timings")
            payload = cut_mod.run(words_path=shoot.words_path, **common)  # type: ignore[arg-type]
            words_note = ("script word timings "
                          "(local Whisper failed, see cut.json)")
        else:
            raise
    return payload, words_note


# ------------------------------------------------------------------ the checks


def sync_clip_by_name(sync_payload: dict, name: str) -> dict | None:
    """The sync.json clip whose file name is `name`, or None. Pure."""
    for clip in sync_payload.get("clips", []) or []:
        cam = clip.get("camera") or {}
        if Path(str(cam.get("path") or "")).name == name:
            return clip
    return None


def check_offsets(sync_payload: dict, known: dict[str, float],
                  *, frame_s: float = FRAME_S) -> list[OffsetCheck]:
    """Found vs known offsets; silent cameras must be clearly unplaced. Pure."""
    checks: list[OffsetCheck] = []
    for name in [*CAMERA_OFFSETS, *SILENT_CAMERAS]:
        clip = sync_clip_by_name(sync_payload, name)
        label = str((clip or {}).get("label") or "?")
        found = (clip or {}).get("offset_s")
        found_f = None if found is None else float(found)
        method = (clip or {}).get("method")
        method_s = None if method is None else str(method)
        if name in SILENT_CAMERAS:
            warnings = (clip or {}).get("warnings", []) or []
            clear = any("could not be placed on the timeline" in str(note)
                        for note in warnings)
            if found_f is None and clear:
                checks.append(OffsetCheck(
                    name, label, known[name], None, method_s, True,
                    "silent camera, unplaced and clearly reported"))
            elif (found_f is not None
                    and abs(found_f - known[name]) <= frame_s + 1e-9):
                checks.append(OffsetCheck(
                    name, label, known[name], found_f, method_s, True,
                    "silent camera, placed within a frame"))
            else:
                checks.append(OffsetCheck(
                    name, label, known[name], found_f, method_s, False,
                    "silent camera, mis-synced without a clear report"))
            continue
        if found_f is None:
            checks.append(OffsetCheck(
                name, label, known[name], None, method_s, False,
                "not placed on the timeline"))
        elif abs(found_f - known[name]) <= frame_s + 1e-9:
            checks.append(OffsetCheck(
                name, label, known[name], found_f, method_s, True, ""))
        else:
            checks.append(OffsetCheck(
                name, label, known[name], found_f, method_s, False,
                f"off by {abs(found_f - known[name]):.3f}s"))
    return checks


def restart_removed_s(tighten_payload: dict) -> tuple[float, float]:
    """(Take-removal seconds, all removed seconds) from tighten.json. Pure."""
    attempt_s = 0.0
    total_s = 0.0
    for span in tighten_payload.get("removed_spans", []) or []:
        if not isinstance(span, dict):
            continue
        seconds = max(0.0, float(span.get("end_s", 0.0))
                      - float(span.get("start_s", 0.0)))
        total_s += seconds
        if str(span.get("kind", "")) in RESTART_KINDS:
            attempt_s += seconds
    return attempt_s, total_s


def check_files(job_dir: Path) -> list[tuple[str, bool]]:
    """(file, present and non-empty) for the finished job's outputs."""
    out = []
    for name in ("cut.mp4", "vertical.mp4", "timeline.fcpxml",
                 "timeline.otio.json", "cut.json", "tighten.json",
                 "sync.json", "angles.json"):
        try:
            ok = (Path(job_dir) / name).stat().st_size > 0
        except OSError:
            ok = False
        out.append((name, ok))
    return out


# ------------------------------------------------------------------ the report


def format_report(*, out_dir: Path, job_dir: Path, shoot: Shoot,
                  use_muse: bool, words_note: str,
                  offset_checks: list[OffsetCheck],
                  attempt_s: float, total_removed_s: float,
                  kept_s: float, file_checks: list[tuple[str, bool]],
                  job_status: str) -> tuple[str, bool]:
    """The short friendly report plus its pass/fail. Pure."""
    lines = ["GhostCut Director demo", ""]
    if shoot.voice_backend == "tone":
        lines.append("Voice: tone-burst stand-in (the real voice needs "
                     "`say` on macOS or espeak).")
    elif shoot.voice_backend == "say":
        lines.append("Voice: macOS say.")
    else:
        lines.append(f"Voice: {shoot.voice_backend}.")
    if use_muse:
        lines.append("Pipeline: director cut with Muse + SAM.")
    else:
        lines.append("Pipeline: director cut --no-muse --no-sam (offline, "
                     "no keys, no network).")
    lines.append(f"Words: {words_note}.")
    lines.append("")
    lines.append(f"Sync offsets (one frame = {FRAME_S:.3f} s):")
    passed = True
    for check in offset_checks:
        if check.found_s is None and check.name in SILENT_CAMERAS and check.passed:
            lines.append(f"  {check.name}: silent camera, left unplaced "
                         f"and clearly reported — PASS")
        elif check.found_s is None:
            passed = False
            lines.append(f"  {check.name}: not placed "
                         f"(known {check.known_s:+.3f} s) — FAIL")
        else:
            verdict = "PASS" if check.passed else "FAIL"
            if not check.passed:
                passed = False
            lines.append(f"  {check.name}: found {check.found_s:+.3f} s, "
                         f"known {check.known_s:+.3f} s — {verdict}")
    lines.append("")
    if attempt_s > 0:
        lines.append(f"Tightening took the restart out: {attempt_s:.1f} s "
                     f"removed, {kept_s:.1f} s kept.")
    else:
        lines.append(f"Tightening kept every take ({total_removed_s:.1f} s "
                     f"of trims, {kept_s:.1f} s kept).")
    lines.append("")
    lines.append("Open:")
    lines.append(f"  {job_dir}/cut.mp4 — the horizontal master")
    lines.append(f"  {job_dir}/vertical.mp4 — the 9:16 version")
    lines.append(f"  {job_dir}/timeline.fcpxml — the same cut for Final Cut Pro")
    lines.append(f"  {job_dir}/cut.json — what ran, what it cost, what QC found")
    lines.append("")
    lines.append("Next:")
    lines.append("  director cut your-folder --out-dir job1")
    lines.append(f"  director voice NOTE.m4a --job {job_dir}")
    lines.append(f"  python app/main.py --job {job_dir}")
    lines.append("")
    missing = [name for name, ok in file_checks if not ok]
    if missing:
        passed = False
        lines.append(f"Missing outputs: {', '.join(missing)}.")
    if job_status not in ("complete", "complete_with_warnings"):
        passed = False
        lines.append(f"Job status: {job_status}.")
    lines.append(f"Demo {'PASS' if passed else 'FAIL'}")
    return "\n".join(lines), passed


# ------------------------------------------------------------------ run


def run(*, out_dir: Path, muse_requested: bool = False, keep: bool = False,
        voice_backend: str | None = None,
        allow_whisper: bool = True) -> dict:
    """Build the shoot, cut it, check it; the demo.json payload (also written)."""
    from ghostcut.ffmpeg_tools import require_ffmpeg_binaries

    require_ffmpeg_binaries()
    out_dir = Path(out_dir)
    shoot_dir = out_dir / "shoot"
    job_dir = out_dir / "job"
    backend = voice_backend or find_voice_backend()
    use_muse = bool(muse_requested and muse_available())
    if muse_requested and not muse_available():
        print("demo: --muse needs MODEL_API_KEY; running offline instead")
    print(f"demo: building the shoot in {shoot_dir}")
    shoot = build_shoot(shoot_dir, voice_backend=backend, keep=keep)
    print(f"demo: running director cut in {job_dir} "
          f"({'with Muse + SAM' if use_muse else 'offline: --no-muse --no-sam'})")
    cut_payload, words_note = run_pipeline(
        shoot=shoot, job_dir=job_dir, use_muse=use_muse,
        allow_whisper=allow_whisper)
    print("demo: checking the result")
    sync_payload = read_json(job_dir / "sync.json")
    tighten_payload = read_json(job_dir / "tighten.json")
    offset_checks = check_offsets(sync_payload, shoot.known_offsets)
    attempt_s, total_removed_s = restart_removed_s(tighten_payload)
    kept_s = float(tighten_payload.get("duration_out_s", 0.0))
    file_checks = check_files(job_dir)
    job_status = str(cut_payload.get("status", ""))
    words_source = str((cut_payload.get("audio") or {}).get("words_source", ""))
    report, passed = format_report(
        out_dir=out_dir, job_dir=job_dir, shoot=shoot, use_muse=use_muse,
        words_note=words_note, offset_checks=offset_checks,
        attempt_s=attempt_s, total_removed_s=total_removed_s, kept_s=kept_s,
        file_checks=file_checks, job_status=job_status)
    # The deterministic offline path must remove the restart; with live
    # Whisper words or Muse picks the take choice is reported, not gated.
    removal_gated = words_source == "explicit" and not use_muse
    if removal_gated and attempt_s <= 0:
        passed = False
        report = report.replace("Demo PASS", "Demo FAIL").replace(
            "Tightening kept every take",
            "Tightening should have removed the restart but kept every take")
    payload = {
        "version": 1,
        "out_dir": str(out_dir),
        "job_dir": str(job_dir),
        "voice_backend": backend,
        "use_muse": use_muse,
        "words_source": words_source,
        "passed": passed,
        "offsets": [
            {"clip": check.name, "label": check.label,
             "known_s": check.known_s, "found_s": check.found_s,
             "method": check.method, "passed": check.passed,
             "note": check.note}
            for check in offset_checks],
        "restart_removed_s": round(attempt_s, 3),
        "removed_s": round(total_removed_s, 3),
        "kept_s": round(kept_s, 3),
        "removal_gated": removal_gated,
        "files": {name: ok for name, ok in file_checks},
        "job_status": job_status,
        "report": report,
    }
    write_json(out_dir / "demo.json", payload)
    return payload
