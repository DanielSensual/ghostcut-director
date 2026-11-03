"""One-step multicam cut: a folder of cards in, a finished cut out.

`director cut` runs the whole Director pipeline in one output directory:
`sync` puts every clip on one timeline, the words bridge makes the audio
clip's word timings explicit in the shared timebase, `tighten` picks the
take to keep for every beat (unless --no-tighten), `grade` matches the
cameras, `angles` picks and renders the horizontal master, the timelines
export to FCPXML and OTIO, `reframe` renders the 9:16 vertical, and a
whole-cut check (mechanical, then one Muse Spark pass over a proxy of the
finished cut) verifies the result. A manifest, `cut.json`, records every
stage, its spend, its warnings and its fingerprint, so re-running the same
command resumes what already ran.

Designed by Muse Code on muse-wonder-1.0, built by Muse Code on
muse-spark-1.3 from docs/muse-code/OPEN-DESIGN.md.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
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
from . import grade as grade_mod
from . import reframe as reframe_mod
from . import sync as sync_mod
from . import tighten as tighten_mod
from . import transcribe as transcribe_mod
from .export import ExportError, export_timelines

MEDIA_EXTENSIONS = {".mov", ".mp4", ".m4v", ".mp3", ".wav", ".m4a"}
DEFAULT_OUT_DIR = "director-cut"
DEFAULT_MODEL = "muse-spark-1.3"
DEFAULT_MIN_SHOT_S = 2.0
DEFAULT_MAX_USD = 5.0
STAGES = ("sync", "words", "tighten", "grade", "angles", "finish",
          "export", "reframe", "captions", "qc")
WHISPER_TIMEOUT_S = 3600
DURATION_TOLERANCE_S = 0.10
BLACK_SHARE_WARN = 0.20
LOUDNESS_WARN_LOW_LUFS = -23.0
LOUDNESS_WARN_HIGH_LUFS = -12.0
TRUE_PEAK_WARN_DBTP = -0.5
QC_PROXY_FPS = 5
QC_PROXY_SHORT_SIDE = 540
QC_INPUT_TOKENS_PER_S = 300
QC_MAX_OUTPUT_TOKENS = 4000
TOKEN_RATES_USD_PER_1M = {
    "muse-spark-1.3": (1.25, 4.25),
    "muse-wonder-1.0": (4.00, 20.00),
}

QC_DEVELOPER = (
    "You are Muse, the finishing checker inside GhostCut Director. You are "
    "watching a low-frame-rate proxy of a finished multicam talking-head "
    "cut, with its soundtrack. The cut was assembled by choosing, for each "
    "spoken line, one of several cameras that recorded the same moment, and "
    "grading every camera to match. Judge only what an editor would catch "
    "on a first watch of an export: a cut to a camera where the speaker is "
    "clearly not the person talking or is badly framed, a visible colour "
    "or exposure jump between cameras that the grade did not fix, picture "
    "that is black, frozen or the wrong shot for the words, a join where "
    "the words are audibly clipped or the voice jumps against the picture, "
    "and framing that loses the speaker. Do not judge the content of what "
    "is said, do not ask for different words, and do not invent problems "
    "to fill a quota: an empty findings list on a clean cut is the correct "
    "answer. Give times in seconds from the start of this proxy, which "
    "starts at the same moment as the finished cut. Never invent a person, "
    "a camera, or an event you cannot see or hear."
)


class CutError(RuntimeError):
    """A cut stage failed in a way the operator must see.

    The message always starts with the stage name, e.g. 'words: …'.
    `stage_warnings` carries warnings gathered before the failure (only
    the words stage ever sets it); the manifest records them alongside
    the error so a failed stage still explains what it tried.
    """

    def __init__(self, message: str, *, stage_warnings: list[str] | None = None):
        super().__init__(message)
        self.stage_warnings = list(stage_warnings or [])


class QcUnavailable(RuntimeError):
    """guestlist (the Meta API client) cannot be imported."""


@dataclass(slots=True)
class JobOptions:
    clips: list[Path]
    out_dir: Path
    reference_path: Path | None
    audio_path: Path | None
    words_path: Path | None
    transcripts_dir: Path | None
    methods: list[str]
    no_grade: bool
    grade_check: bool
    no_muse: bool
    model: str
    min_shot_s: float
    no_vertical: bool
    no_sam: bool
    no_export: bool
    no_qc: bool
    max_usd: float
    force: bool
    project_name: str
    # Take picking (TIGHTEN-DESIGN.md §11). no_tighten defaults True so
    # existing programmatic callers keep their 7-stage job; the CLI passes
    # False unless --no-tighten, so `director cut` tightens by default.
    no_tighten: bool = True
    pause_limit_s: float = 0.8
    target_s: float | None = None
    # Voice Transcribe enrichment of the words stage (TRANSCRIBE-DESIGN.md
    # §8). no_transcribe defaults False so `director cut` transcribes when
    # it can; existing callers keep working through the default.
    no_transcribe: bool = False
    whisper_model: str = sync_mod.WHISPER_MODEL
    # Finishing (FINISH-DESIGN.md §13). no_finish defaults True so
    # existing programmatic callers keep their old job; the CLI passes
    # False unless --no-finish, so `director cut` finishes by default.
    no_finish: bool = True
    finish_sound: bool = False
    finish_isolate: bool = False
    finish_loudness: float | None = None
    max_provider_usd: float = 2.00
    # --lut PATTERN=CUBE entries: a supplied .cube for every clip whose file name (or path)
    # matches PATTERN, for cameras Director can't read (DJI D-Log, Apple Log, ...).
    lut_patterns: list[str] = field(default_factory=list)
    # Burned-in captions (CAPTIONS-BRIEF.md). no_captions defaults True so
    # existing programmatic callers keep their old job; the CLI passes
    # False unless --no-captions, so `director cut` captions by default.
    no_captions: bool = True
    captions_preset: str = "house"
    captions_horizontal: bool = False
    title: str | None = None
    no_title: bool = False
    fonts_dir: str | None = None


@dataclass(slots=True)
class StageRecord:
    name: str
    status: str
    fingerprint: str
    outputs: list[str]
    spend_usd: float
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(slots=True)
class QcCheck:
    id: str
    source: str
    status: str
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------- discovery


def discover_clips(inputs: list[Path], out_dir: Path) -> list[Path]:
    """Files and folders to the deduplicated clip list, in job order.

    Pure path logic: files pass through as given, folders expand
    non-recursively to their media entries, missing paths are left for
    `sync.run` to refuse with its own message, and anything under the
    job directory (the job's own outputs on a re-run) is dropped.
    """
    expanded: list[Path] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            entries = sorted(
                (entry for entry in path.iterdir()
                 if entry.suffix.lower() in MEDIA_EXTENSIONS
                 and not entry.name.startswith(".")),
                key=lambda entry: (entry.name.lower(), entry.name),
            )
            expanded.extend(entries)
        else:
            expanded.append(path)
    out_resolved = Path(out_dir).resolve()
    clips: list[Path] = []
    seen: set[str] = set()
    for path in expanded:
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            resolved = path.absolute()
        if resolved == out_resolved or out_resolved in resolved.parents:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        clips.append(Path(path))
    if len(clips) < 2:
        raise CutError("cut: need at least two clips (files, or a folder of media)")
    return clips


# -------------------------------------------------------------- words bridge


def match_luts(patterns: list[str], clips: list[Path]) -> dict[str, Path]:
    """--lut PATTERN=CUBE entries to {clip path: cube}; later entries win.

    PATTERN is a shell glob matched case-insensitively against each clip's file name and
    full path, so `DJI_*=dlog.cube` or the exact file name both work.
    """
    out: dict[str, Path] = {}
    for item in patterns:
        pattern, sep, cube = item.partition("=")
        if not sep or not pattern or not cube:
            raise CutError(f"--lut {item!r}: want PATTERN=CUBE, e.g. 'DJI_*=dlog.cube'")
        cube_path = Path(cube).expanduser()
        if not cube_path.is_file():
            raise CutError(f"--lut {item!r}: no such .cube file: {cube_path}")
        hits = [clip for clip in clips
                if fnmatch.fnmatch(clip.name.lower(), pattern.lower())
                or fnmatch.fnmatch(str(clip).lower(), pattern.lower())]
        if not hits:
            raise CutError(f"--lut {item!r}: no input clip matches {pattern!r}")
        for clip in hits:
            out[str(clip)] = cube_path.resolve()
    return out


def camera_coverage(sync_payload: dict) -> list[tuple[float, float]]:
    """Merged timeline spans where at least one placed clip with picture is rolling. Pure."""
    spans = sorted(
        (float(clip["offset_s"]),
         float(clip["offset_s"]) + float((clip.get("camera") or {}).get("duration_s") or 0.0))
        for clip in sync_payload.get("clips", [])
        if clip.get("offset_s") is not None
        and float((clip.get("camera") or {}).get("width") or 0) > 0)
    merged: list[tuple[float, float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def words_on_camera(words: list, spans: list[tuple[float, float]]) -> list:
    """The words spoken while some camera was rolling (by each word's midpoint). Pure.

    A lav recorder usually rolls before and after the cameras; a cut can only show what a
    camera saw. With no camera coverage at all every word is kept.
    """
    if not spans:
        return list(words)
    return [word for word in words
            if any(start <= (word.start_s + word.end_s) / 2 <= end for start, end in spans)]


def resolve_audio(sync_payload: dict, audio_path: Path | None) -> tuple[str, dict]:
    """(label, sync clip dict) of the clip angles will lay under the cut."""
    clips = sync_payload.get("clips", [])
    if audio_path is not None:
        wanted = Path(audio_path).resolve()
        chosen = None
        for item in clips:
            cam = item.get("camera") or {}
            if Path(str(cam.get("path") or "")).resolve() == wanted:
                chosen = item
                break
        if chosen is None:
            raise CutError(f"words: --audio is not one of the synced clips: {audio_path}")
    else:
        reference = sync_payload.get("reference")
        chosen = next((item for item in clips
                       if (item.get("camera") or {}).get("path") == reference), None)
        if chosen is None:
            chosen = next((item for item in clips if item.get("label") == "A"), None)
        if chosen is None:
            raise CutError("words: sync.json has no clips")
    label = str(chosen.get("label") or "")
    if chosen.get("offset_s") is None:
        raise CutError(f"words: audio clip {label} is not placed on the timeline; "
                       f"cannot align its words — check the sync warnings in cut.json")
    return label, chosen


def shift_words(words: list[angles_mod.TimedWord],
                offset_s: float) -> list[angles_mod.TimedWord]:
    """Clip-local to timeline: start and end both move by offset_s. Pure."""
    return [angles_mod.TimedWord(word.text, word.start_s + offset_s,
                                 word.end_s + offset_s, word.speaker,
                                 word.heard_by, word.disfluency)
            for word in words]


def words_file_payload(words: list[angles_mod.TimedWord]) -> dict:
    """{"words": [{"word": text, "start": …, "end": …}, …]} at 3 decimals.

    Words carrying transcript enrichment gain the optional speaker,
    heard_by and disfluency keys; plain Whisper words keep the
    pre-feature shape byte-for-byte.
    """
    entries = []
    for word in words:
        entry: dict = {"word": word.text, "start": round(word.start_s, 3),
                       "end": round(word.end_s, 3)}
        if word.speaker:
            entry["speaker"] = word.speaker
        if word.speaker or word.heard_by != "whisper":
            entry["heard_by"] = word.heard_by
        if word.disfluency:
            entry["disfluency"] = True
        entries.append(entry)
    return {"words": entries}


def mlx_whisper_command(clip_path: Path, out_dir: Path,
                        whisper_model: str = sync_mod.WHISPER_MODEL
                        ) -> list[str]:
    return ["mlx_whisper", str(clip_path), "--model", whisper_model,
            "--output-format", "json",
            "--output-dir", str(out_dir), "--word-timestamps", "True",
            *sync_mod.WHISPER_GUARD_ARGS, "--verbose", "False"]


def _tolerant_word_load(path: Path) -> list[angles_mod.TimedWord]:
    """load_timed_words, with malformed input giving [] (never raising)."""
    try:
        return angles_mod.load_timed_words(Path(path))
    except Exception:
        return []


def resolve_words(*, audio_label: str, audio_clip: dict,
                  options: JobOptions) -> tuple[Path, str, list[str]]:
    """(canonical words path, source, warnings) for the audio clip.

    The first source yielding at least one word wins: the explicit
    --words file (timeline times, used in place), the --transcripts
    candidate, the sync transcript cache, or a local mlx_whisper run.
    Shifted sources are time-shifted into the shared timebase and
    written to <out_dir>/words.json.
    """
    warnings: list[str] = []
    cam = audio_clip.get("camera") or {}
    clip_path = Path(str(cam.get("path") or ""))
    stem = clip_path.stem
    offset_s = float(audio_clip.get("offset_s") or 0.0)
    out_dir = Path(options.out_dir)

    def _shift_and_write(words: list[angles_mod.TimedWord]) -> Path:
        dest = out_dir / "words.json"
        write_json(dest, words_file_payload(shift_words(words, offset_s)))
        return dest

    if options.words_path is not None:
        words = angles_mod.load_timed_words(Path(options.words_path))
        if not words:
            raise CutError(f"words: no words in --words file: {options.words_path}",
                           stage_warnings=warnings)
        return Path(options.words_path), "explicit", warnings
    if options.transcripts_dir is not None:
        candidate = Path(options.transcripts_dir) / f"{stem}.json"
        if candidate.exists():
            words = _tolerant_word_load(candidate)
            if words:
                return _shift_and_write(words), "transcripts", warnings
            warnings.append(f"words_empty_candidate:{candidate.name}")
    cached = out_dir / "sync-transcripts" / f"{stem}.json"
    try:
        cached_ok = cached.exists() and cached.stat().st_size > 0
    except OSError:
        cached_ok = False
    if cached_ok:
        words = _tolerant_word_load(cached)
        if words:
            return _shift_and_write(words), "sync_cache", warnings
        warnings.append(f"words_empty_candidate:{cached.name}")
    whisper_on_path = shutil.which("mlx_whisper") is not None
    no_gpu = whisper_on_path and not sync_mod.metal_visible()
    if whisper_on_path and not no_gpu:
        dest_dir = out_dir / "transcripts"
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / f"{stem}.json"
        try:
            done = subprocess.run(
                mlx_whisper_command(clip_path, dest_dir,
                                    options.whisper_model),
                capture_output=True, timeout=WHISPER_TIMEOUT_S)
            produced = done.returncode == 0
        except (subprocess.SubprocessError, OSError):
            produced = False
        if produced:
            try:
                produced = target.exists() and target.stat().st_size > 0
            except OSError:
                produced = False
        if produced:
            words = _tolerant_word_load(target)
            if words:
                return _shift_and_write(words), "mlx_whisper", warnings
        warnings.append(f"words_mlx_whisper_failed:{clip_path.name}")
    elif no_gpu:
        # MLX aborts the process without a visible GPU; never run it blind.
        warnings.append(f"words_no_gpu_visible:{clip_path.name}")
    if no_gpu:
        reason = "no GPU visible for mlx_whisper"
    elif whisper_on_path:
        reason = f"mlx_whisper failed for {clip_path.name}"
    else:
        reason = "mlx_whisper is not on PATH; install mlx-whisper"
    raise CutError(
        f"words: no word-timed transcript for clip {audio_label} "
        f"({clip_path.name}) and {reason}, "
        "or pass --words WORDS.json, or --transcripts DIR",
        stage_warnings=warnings)


# -------------------------------------------------- fingerprints and resume


def file_fingerprint(path: Path) -> dict:
    """{"path", "size", "mtime_ns"}; a missing file gives -1s. Never raises."""
    try:
        stat = Path(path).stat()
    except OSError:
        return {"path": str(path), "size": -1, "mtime_ns": -1}
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def fingerprint_of(payload: object) -> str:
    """sha256 hex of json.dumps(payload, sort_keys=True, default=str)."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def stage_fingerprint(stage: str, options: dict, files: list[Path]) -> str:
    """fingerprint_of the stage name, its options and its input files."""
    return fingerprint_of({"stage": stage, "options": options,
                           "files": [file_fingerprint(path) for path in files]})


def _write_manifest(out_dir: Path, payload: dict) -> None:
    write_json(Path(out_dir) / "cut.json", payload)


# ------------------------------------------------------------------ QC tools


def qc_proxy_command(video: Path, proxy: Path) -> list[str]:
    return ["ffmpeg", "-v", "error", "-y", "-i", str(video), "-map", "0:v:0",
            "-vf", f"fps={QC_PROXY_FPS},scale=-2:{QC_PROXY_SHORT_SIDE}",
            "-an", "-map_metadata", "-1", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(proxy)]


def loudnorm_command(path: Path) -> list[str]:
    return ["ffmpeg", "-v", "info", "-i", str(path), "-vn",
            "-af", "loudnorm=print_format=json", "-f", "null", "-"]


def parse_loudnorm(stderr: str) -> tuple[float | None, float | None]:
    """(integrated LUFS, true peak dBTP) from loudnorm stderr; (None, None)."""
    try:
        found = stderr.rfind("[Parsed_loudnorm")
        if found < 0:
            return None, None
        start = stderr.find("{", found)
        end = stderr.rfind("}")
        if start < 0 or end < start:
            return None, None
        data = json.loads(stderr[start:end + 1])
        return float(data["input_i"]), float(data["input_tp"])
    except (ValueError, KeyError, TypeError):
        return None, None


def _probe_stats(path: Path) -> dict | None:
    """extract_media_stats for a file, or None when it will not probe."""
    try:
        return extract_media_stats(probe_media(Path(path)))
    except (ToolingError, OSError, ValueError):
        return None


def mechanical_qc(*, out_dir: Path, sync_payload: dict, angles_payload: dict,
                  reframe_payload: dict | None,
                  export_section: dict | None,
                  captions_section: dict | None = None) -> list[QcCheck]:
    """Deterministic whole-cut checks, in record order. Check 9 never fails."""
    out_dir = Path(out_dir)
    checks: list[QcCheck] = []
    shots = angles_payload.get("shots", []) or []
    if shots:
        span_s = float(shots[-1].get("end_s", 0.0)) - float(shots[0].get("start_s", 0.0))
    else:
        span_s = 0.0
    span_s = max(0.0, span_s)

    master_stats = _probe_stats(out_dir / "cut.mp4")
    master_missing = master_stats is None
    checks.append(QcCheck("master_exists", "mechanical",
                          "fail" if master_missing else "pass", {"file": "cut.mp4"}))
    master_dur = float((master_stats or {}).get("duration_s") or 0.0)

    if master_missing:
        checks.append(QcCheck("duration_match", "mechanical", "warn",
                              {"skipped": "master_exists"}))
    else:
        delta = abs(master_dur - span_s)
        checks.append(QcCheck(
            "duration_match", "mechanical",
            "pass" if delta <= DURATION_TOLERANCE_S else "fail",
            {"rendered_s": round(master_dur, 3), "shots_s": round(span_s, 3),
             "delta_s": round(delta, 3)}))
    if master_missing:
        checks.append(QcCheck("audio_present", "mechanical", "warn",
                              {"skipped": "master_exists"}))
    else:
        has_audio = bool(master_stats.get("has_audio"))
        checks.append(QcCheck("audio_present", "mechanical",
                              "pass" if has_audio else "warn", {"has_audio": has_audio}))
    if master_missing:
        checks.append(QcCheck("black_share", "mechanical", "warn",
                              {"skipped": "master_exists"}))
    else:
        black_s = sum(max(0.0, float(shot.get("end_s", 0.0))
                                    - float(shot.get("start_s", 0.0)))
                      for shot in shots if shot.get("camera") == "black")
        share = (black_s / span_s) if span_s > 0 else 0.0
        checks.append(QcCheck(
            "black_share", "mechanical",
            "pass" if share <= BLACK_SHARE_WARN else "warn",
            {"black_s": round(black_s, 3), "span_s": round(span_s, 3),
             "share": round(share, 4)}))

    long_ids = [shot.get("id") for shot in shots
                if any(str(note).startswith("long_shot:")
                       for note in (shot.get("overrules") or []))]
    checks.append(QcCheck("long_shots", "mechanical",
                          "pass" if not long_ids else "warn",
                          {"count": len(long_ids), "shot_ids": long_ids}))
    unplaced = [clip.get("label") for clip in (sync_payload.get("clips") or [])
                if clip.get("offset_s") is None]
    checks.append(QcCheck("unplaced_cameras", "mechanical",
                          "pass" if not unplaced else "warn", {"labels": unplaced}))

    if reframe_payload is not None:
        vertical_stats = _probe_stats(out_dir / "vertical.mp4")
        if master_missing:
            checks.append(QcCheck("vertical_shape", "mechanical", "warn",
                                  {"skipped": "master_exists"}))
        elif vertical_stats is None:
            checks.append(QcCheck(
                "vertical_shape", "mechanical", "fail",
                {"width": 0, "height": 0, "rendered_s": 0.0,
                 "master_s": round(master_dur, 3), "delta_s": round(master_dur, 3)}))
        else:
            width = int(vertical_stats.get("width") or 0)
            height = int(vertical_stats.get("height") or 0)
            rendered = float(vertical_stats.get("duration_s") or 0.0)
            delta = abs(rendered - master_dur)
            ok = width == 1080 and height == 1920 and delta <= DURATION_TOLERANCE_S
            checks.append(QcCheck(
                "vertical_shape", "mechanical", "pass" if ok else "fail",
                {"width": width, "height": height, "rendered_s": round(rendered, 3),
                 "master_s": round(master_dur, 3), "delta_s": round(delta, 3)}))

    if export_section is not None:
        try:
            ET.parse(out_dir / "timeline.fcpxml")
            fcpxml_ok = True
        except (OSError, ET.ParseError):
            fcpxml_ok = False
        try:
            otio = json.loads((out_dir / "timeline.otio.json").read_text(encoding="utf-8"))
            otio_ok = isinstance(otio, dict) and otio.get("OTIO_SCHEMA") == "Timeline.1"
        except (OSError, ValueError):
            otio_ok = False
        checks.append(QcCheck(
            "timelines_parse", "mechanical",
            "pass" if (fcpxml_ok and otio_ok) else "fail",
            {"fcpxml": fcpxml_ok, "otio": otio_ok}))

    if captions_section is not None:
        caption_files = ["captions.srt", "captions.ass", "captions.json"]
        present = {}
        for name in caption_files:
            try:
                present[name] = (out_dir / name).exists() \
                    and (out_dir / name).stat().st_size > 0
            except OSError:
                present[name] = False
        checks.append(QcCheck(
            "captions_exist", "mechanical",
            "pass" if all(present.values()) else "fail",
            {name: present[name] for name in caption_files}))
        target = out_dir / "vertical.mp4"
        if not target.exists():
            target = out_dir / "cut.mp4"
        target_stats = _probe_stats(target)
        if target_stats is None:
            checks.append(QcCheck("captions_in_range", "mechanical", "warn",
                                  {"skipped": target.name}))
        else:
            video_dur = float(target_stats.get("duration_s") or 0.0)
            try:
                saved = json.loads(
                    (out_dir / "captions.json").read_text(encoding="utf-8"))
                saved_lines = saved.get("lines", [])
            except (OSError, ValueError):
                saved_lines = None
            if not isinstance(saved_lines, list):
                checks.append(QcCheck("captions_in_range", "mechanical",
                                      "warn", {"skipped": "captions.json"}))
            else:
                bad: list[int] = []
                for entry in saved_lines:
                    if not isinstance(entry, dict):
                        continue
                    try:
                        start = float(entry.get("start_s", 0.0))
                        end = float(entry.get("end_s", 0.0))
                    except (TypeError, ValueError):
                        bad.append(int(entry.get("id", -1)))
                        continue
                    if start < -1e-6 or end > video_dur + DURATION_TOLERANCE_S \
                            or end < start:
                        try:
                            bad.append(int(entry.get("id", -1)))
                        except (TypeError, ValueError):
                            bad.append(-1)
                checks.append(QcCheck(
                    "captions_in_range", "mechanical",
                    "pass" if not bad else "fail",
                    {"lines": len(saved_lines),
                     "video_s": round(video_dur, 3),
                     "outside_ids": bad[:12]}))

    try:
        process = run_command(loudnorm_command(out_dir / "cut.mp4"), check=False)
        integrated, peak = parse_loudnorm(process.stderr or "")
    except OSError:
        integrated, peak = None, None
    if integrated is None or peak is None:
        checks.append(QcCheck("loudness", "mechanical", "warn", {"measured": None}))
    else:
        ok = (LOUDNESS_WARN_LOW_LUFS <= integrated <= LOUDNESS_WARN_HIGH_LUFS
              and peak <= TRUE_PEAK_WARN_DBTP)
        checks.append(QcCheck(
            "loudness", "mechanical", "pass" if ok else "warn",
            {"integrated_lufs": round(integrated, 2),
             "true_peak_dbtp": round(peak, 2)}))
    return checks


# ------------------------------------------------------- the Muse whole-cut pass


def estimate_qc_usd(duration_s: float, model: str) -> float:
    """Conservative pre-call estimate for the QC video call. Pure."""
    in_rate, out_rate = TOKEN_RATES_USD_PER_1M.get(
        model, TOKEN_RATES_USD_PER_1M["muse-spark-1.3"])
    return (duration_s * QC_INPUT_TOKENS_PER_S * in_rate / 1e6
            + QC_MAX_OUTPUT_TOKENS * out_rate / 1e6)


def qc_schema() -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["overall", "summary", "findings"],
        "properties": {
            "overall": {"type": "string",
                        "enum": ["publish", "review", "not_ready"]},
            "summary": {"type": "string"},
            "findings": {
                "type": "array", "maxItems": 12,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["start_s", "end_s", "kind", "severity",
                                 "description", "confidence"],
                    "properties": {
                        "start_s": {"type": "number"},
                        "end_s": {"type": "number"},
                        "kind": {"type": "string", "enum": [
                            "angle_choice", "grade_mismatch", "av_sync_suspect",
                            "black_or_frozen", "framing", "audio_issue", "other"]},
                        "severity": {"type": "string",
                                     "enum": ["low", "medium", "high"]},
                        "description": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                },
            },
        },
    }


def qc_prompt(duration_s: float, shots: list[dict]) -> str:
    """The whole-cut check prompt: verdict request plus the shot map."""
    lines = [f"Check this finished cut ({duration_s:.1f}s). Reply with the overall "
             f"verdict and at most 12 findings."]
    for shot in shots:
        start = float(shot.get("start_s", 0.0))
        end = float(shot.get("end_s", 0.0))
        if shot.get("camera") == "black":
            lines.append(f"shot {shot.get('id')} {start:.1f}-{end:.1f}s BLACK "
                         f"(no camera covered this span)")
        else:
            lines.append(f"shot {shot.get('id')} {start:.1f}-{end:.1f}s "
                         f"camera {shot.get('camera')}")
    lines.append("Cameras are the letters used in angles.json; times below are seconds "
                 "on the finished cut.")
    lines.append("If the picture disagrees with this map, trust the picture and say so "
                 "in the finding.")
    return "\n".join(lines)


def muse_qc(*, proxy: Path, duration_s: float, model: str,
            angles_payload: dict) -> tuple[list[QcCheck], float, list[str]]:
    """One spark.video_json call over the finished cut. Muse proposes; the
    mechanical verification below decides what is recorded. Raises
    QcUnavailable only when guestlist cannot be imported; every other
    failure propagates to the caller, which degrades to mechanical-only."""
    try:
        from guestlist.meta import cache, spark
    except ImportError as error:
        raise QcUnavailable(str(error)) from error
    estimate = estimate_qc_usd(duration_s, model)
    cache.guard(estimate)
    shots = angles_payload.get("shots", []) or []
    reply = spark.video_json(proxy, schema=qc_schema(), name="cut_qc",
                             developer=QC_DEVELOPER,
                             prompt=qc_prompt(duration_s, shots),
                             model=model, effort="medium",
                             max_output_tokens=QC_MAX_OUTPUT_TOKENS,
                             max_usd=estimate)
    data = reply.data
    if not isinstance(data, dict):
        raise ValueError(f"Muse QC reply is not a dict: {type(data).__name__}")
    findings = data.get("findings", [])
    if not isinstance(findings, list):
        raise ValueError("Muse QC reply findings are not a list")
    black_spans = [(float(shot.get("start_s", 0.0)), float(shot.get("end_s", 0.0)))
                   for shot in shots if shot.get("camera") == "black"]
    checks: list[QcCheck] = []
    warnings: list[str] = []
    dropped = 0
    for finding in findings:
        if not isinstance(finding, dict):
            dropped += 1
            continue
        start, end = finding.get("start_s"), finding.get("end_s")
        if (isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or not math.isfinite(start) or not math.isfinite(end)):
            dropped += 1
            continue
        start_f, end_f = float(start), float(end)
        if end_f <= start_f or end_f < 0 or start_f > duration_s:
            dropped += 1
            continue
        start_f = max(0.0, start_f)
        end_f = min(duration_s, end_f)
        try:
            confidence = float(finding.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if not math.isfinite(confidence):
            confidence = 0.0
        confidence = min(1.0, max(0.0, confidence))
        kind = str(finding.get("kind", "other"))
        length = end_f - start_f
        corroborated = (kind == "black_or_frozen" and length > 0 and any(
            max(0.0, min(end_f, black_end) - max(start_f, black_start))
            >= 0.5 * length for black_start, black_end in black_spans))
        checks.append(QcCheck(
            id=f"muse:{kind}", source="muse", status="warn",
            detail={"start_s": round(start_f, 3), "end_s": round(end_f, 3),
                    "severity": str(finding.get("severity", "low")),
                    "description": str(finding.get("description", "")),
                    "confidence": confidence, "corroborated": corroborated}))
    if dropped:
        warnings.append(f"qc_muse_dropped_findings:{dropped}")
    overall = data.get("overall")
    if overall not in ("publish", "review", "not_ready"):
        overall = "review"
    checks.append(QcCheck(
        id="muse:overall", source="muse",
        status="pass" if overall == "publish" else "warn",
        detail={"overall": overall, "summary": str(data.get("summary", ""))}))
    return checks, float(getattr(reply, "usd", 0.0) or 0.0), warnings


# ------------------------------------------------------------- orchestration


def _spend_of(payload: dict) -> float:
    return float(payload.get("spend_usd") or 0.0)


def _captions_section(captions_payload: dict) -> dict:
    """The cut.json captions section from a captions.json payload."""
    title = captions_payload.get("title") or {}
    return {
        "file": "captions.json",
        "preset": captions_payload.get("preset", "house"),
        "lines": len(captions_payload.get("lines", []) or []),
        "title": title.get("text") if isinstance(title, dict) else None,
        "burned": list(captions_payload.get("burned", []) or []),
    }


def _tighten_section(tighten_payload: dict) -> dict:
    """The cut.json tighten section from a tighten.json payload."""
    kept = [s for s in tighten_payload.get("kept_spans", []) or []]
    removed = [s for s in tighten_payload.get("removed_spans", []) or []]
    return {
        "file": "tighten.json",
        "beats": len(tighten_payload.get("beats", []) or []),
        "kept_s": round(sum(float(s.get("end_s", 0.0))
                            - float(s.get("start_s", 0.0)) for s in kept), 3),
        "removed_s": round(sum(float(s.get("end_s", 0.0))
                               - float(s.get("start_s", 0.0))
                               for s in removed), 3),
        "duration_in_s": tighten_payload.get("duration_in_s", 0.0),
        "duration_out_s": tighten_payload.get("duration_out_s", 0.0),
        "method": tighten_payload.get("method", ""),
        "target": tighten_payload.get("target"),
    }


def run(*, clips: list[Path], out_dir: Path,
        reference_path: Path | None, audio_path: Path | None,
        words_path: Path | None, transcripts_dir: Path | None,
        methods: list[str], no_grade: bool, grade_check: bool,
        no_muse: bool, model: str, min_shot_s: float,
        no_vertical: bool, no_sam: bool, no_export: bool,
        no_qc: bool, max_usd: float, force: bool,
        project_name: str, no_tighten: bool = True,
        pause_limit_s: float = 0.8,
        target_s: float | None = None,
        no_transcribe: bool = False,
        whisper_model: str = sync_mod.WHISPER_MODEL,
        no_finish: bool = True, finish_sound: bool = False,
        finish_isolate: bool = False, finish_loudness: float | None = None,
        max_provider_usd: float = 2.00,
        lut_patterns: list[str] | None = None,
        no_captions: bool = True, captions_preset: str = "house",
        captions_horizontal: bool = False, title: str | None = None,
        no_title: bool = False, fonts_dir: str | None = None) -> dict:
    """One job: discover → sync → words → tighten → grade → angles → … → qc.

    The tighten stage runs unless no_tighten (the CLI enables it unless
    --no-tighten). Returns the cut.json payload (also written,
    incrementally, to out_dir).
    """
    if max_usd < 0:
        raise ValueError("--max-usd must be >= 0")
    if max_provider_usd < 0:
        raise ValueError("--max-provider-usd must be >= 0")
    if finish_loudness is not None and not -20.0 <= float(finish_loudness) <= -10.0:
        raise ValueError("--finish-loudness must be between -20 and -10")
    if min_shot_s < 0:
        raise ValueError("--min-shot must be >= 0")
    out_dir = Path(out_dir)
    discovered = discover_clips([Path(clip) for clip in clips], out_dir)
    options = JobOptions(
        clips=discovered, out_dir=out_dir,
        reference_path=Path(reference_path) if reference_path is not None else None,
        audio_path=Path(audio_path) if audio_path is not None else None,
        words_path=Path(words_path) if words_path is not None else None,
        transcripts_dir=Path(transcripts_dir) if transcripts_dir is not None else None,
        methods=list(methods), no_grade=no_grade, grade_check=grade_check,
        no_muse=no_muse, model=model, min_shot_s=min_shot_s,
        no_vertical=no_vertical, no_sam=no_sam, no_export=no_export,
        no_qc=no_qc, max_usd=max_usd, force=force, project_name=project_name,
        no_tighten=no_tighten, pause_limit_s=pause_limit_s,
        target_s=target_s, no_transcribe=no_transcribe,
        whisper_model=whisper_model, no_finish=no_finish,
        finish_sound=finish_sound, finish_isolate=finish_isolate,
        finish_loudness=finish_loudness, max_provider_usd=max_provider_usd,
        lut_patterns=list(lut_patterns or []),
        no_captions=no_captions, captions_preset=captions_preset,
        captions_horizontal=captions_horizontal, title=title,
        no_title=no_title, fonts_dir=fonts_dir)
    graded = not no_grade
    tightened = not no_tighten

    top_warnings: list[str] = []
    prev: dict | None = None
    manifest_path = out_dir / "cut.json"
    if manifest_path.exists():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            prev = loaded if isinstance(loaded, dict) \
                and isinstance(loaded.get("stages"), dict) else None
        except (OSError, ValueError):
            prev = None
        if prev is None:
            top_warnings.append("previous_manifest_unreadable")
    prev_stages: dict = prev.get("stages", {}) if prev else {}

    enabled = ["sync", "words"]
    if tightened:
        enabled.append("tighten")
    if graded:
        enabled.append("grade")
    enabled.append("angles")
    if not no_finish:
        enabled.append("finish")
    if not no_export:
        enabled.append("export")
    if not no_vertical:
        enabled.append("reframe")
    if not options.no_captions:
        enabled.append("captions")
    if not no_qc:
        enabled.append("qc")

    records: dict[str, StageRecord] = {}
    if no_qc:
        records["qc"] = StageRecord("qc", "skipped", "", [], 0.0,
                                    ["disabled by --no-qc"], None)
    sync_payload: dict | None = None
    audio_section: dict | None = None
    words_file_current: Path | None = None
    tighten_section: dict | None = None
    grade_payload: dict | None = None
    angles_payload: dict | None = None
    finish_payload: dict | None = None
    finish_provider_spend = 0.0
    export_section: dict | None = None
    reframe_payload: dict | None = None
    captions_section: dict | None = None
    qc_section: dict | None = None
    final_checks: list[QcCheck] = []
    spent_usd = 0.0
    provider_spent_usd = 0.0
    dirty = False

    def _manifest(status: str, failed_stage: str | None) -> dict:
        stages_dict = {}
        for name in STAGES:
            if name in records:
                record = records[name]
                entry: dict = {
                    "status": record.status, "fingerprint": record.fingerprint,
                    "outputs": list(record.outputs),
                    "spend_usd": round(record.spend_usd, 4),
                    "warnings": list(record.warnings), "error": record.error}
                if name == "finish":
                    entry["provider_spend_usd"] = round(finish_provider_spend, 4)
                stages_dict[name] = entry
        return {
            "version": 1,
            "status": status,
            "failed_stage": failed_stage,
            "out_dir": str(options.out_dir),
            "model": options.model,
            "max_usd": options.max_usd,
            "spend_usd": round(spent_usd, 4),
            "provider_spend_usd": round(provider_spent_usd, 4),
            "warnings": list(top_warnings),
            "inputs": [Path(clip).name for clip in options.clips],
            "stages": stages_dict,
            "audio": audio_section,
            "tighten": tighten_section,
            "export": export_section,
            "captions": captions_section,
            "qc": qc_section,
        }

    def _fail(stage: str, fingerprint: str, exc: Exception) -> None:
        pre_warnings = list(getattr(exc, "stage_warnings", None) or [])
        records[stage] = StageRecord(stage, "failed", fingerprint, [], 0.0,
                                     pre_warnings, str(exc))
        _write_manifest(out_dir, _manifest("failed", stage))
        if isinstance(exc, CutError) and str(exc).startswith(f"{stage}: "):
            raise exc
        raise CutError(f"{stage}: {exc}") from exc

    luts = match_luts(list(options.lut_patterns), list(options.clips))

    def _grade_lut_specs() -> tuple[str, ...]:
        """Supplied cubes as grade's LABEL=PATH, once sync has named the clips."""
        if not luts or sync_payload is None:
            return ()
        specs = []
        for clip in sync_payload.get("clips", []):
            path = str((clip.get("camera") or {}).get("path") or "")
            if path in luts:
                specs.append(f"{clip.get('label')}={luts[path]}")
        return tuple(specs)

    def _fingerprint(stage: str) -> str:
        if stage == "sync":
            return stage_fingerprint(
                "sync",
                {"methods": options.methods,
                 "reference": str(options.reference_path)
                 if options.reference_path is not None else None,
                 "whisper_model": options.whisper_model,
                 "luts": sorted(options.lut_patterns)},
                list(options.clips) + sorted(set(luts.values())))
        if stage == "words":
            assert sync_payload is not None
            label, clip = resolve_audio(sync_payload, options.audio_path)
            cam = clip.get("camera") or {}
            files = [Path(str(cam.get("path") or ""))]
            if options.words_path is not None:
                files.append(Path(options.words_path))
            elif options.transcripts_dir is not None:
                stem = Path(str(cam.get("path") or "")).stem
                files.append(Path(options.transcripts_dir) / f"{stem}.json")
            return stage_fingerprint(
                "words",
                {"audio": label, "no_transcribe": options.no_transcribe,
                 "no_muse": options.no_muse,
                 "whisper_model": options.whisper_model},
                files)
        if stage == "tighten":
            assert audio_section is not None and words_file_current is not None
            return stage_fingerprint(
                "tighten",
                {"audio": audio_section["label"],
                 "pause_limit_s": options.pause_limit_s,
                 "target_s": options.target_s, "model": options.model,
                 "no_muse": options.no_muse},
                [words_file_current, out_dir / "sync.json"])
        if stage == "grade":
            return stage_fingerprint(
                "grade",
                {"reference_label": None,
                 "check": bool(options.grade_check and not options.no_muse),
                 "model": options.model, "no_match": False,
                 "luts": sorted(options.lut_patterns)},
                [out_dir / "sync.json"] + sorted(set(luts.values())))
        if stage == "angles":
            assert audio_section is not None and words_file_current is not None
            files = [words_file_current, out_dir / "sync.json"]
            if tightened:
                files.append(out_dir / "tighten.json")
            if graded:
                files.append(out_dir / "grade.json")
            return stage_fingerprint(
                "angles",
                {"audio": audio_section["label"], "min_shot_s": options.min_shot_s,
                 "model": options.model, "no_muse": options.no_muse,
                 "graded": graded},
                files)
        if stage == "finish":
            taste_file = out_dir / "taste.json"
            try:
                taste_fp = fingerprint_of(read_json(taste_file)
                                          if taste_file.exists() else [])
            except (OSError, ValueError):
                taste_fp = fingerprint_of([])
            finish_files: list[Path] = [out_dir / "cut.mp4",
                                        out_dir / "angles.json"]
            if words_file_current is not None:
                finish_files.append(words_file_current)
            if tightened:
                finish_files.append(out_dir / "tighten.json")
            if taste_file.exists():
                finish_files.append(taste_file)
            return stage_fingerprint(
                "finish",
                {"model": options.model, "no_muse": options.no_muse,
                 "sound": options.finish_sound,
                 "isolate": options.finish_isolate,
                 "loudness": options.finish_loudness,
                 "max_provider_usd": options.max_provider_usd,
                 "taste": taste_fp,
                 "elevenlabs_connected": connect_mod.key_status(
                     "elevenlabs").connected,
                 "higgsfield_connected": connect_mod.key_status(
                     "higgsfield").connected},
                finish_files)
        if stage == "export":
            files = [out_dir / "angles.json", out_dir / "sync.json"]
            if (out_dir / "grade.json").exists():
                files.append(out_dir / "grade.json")
            if (out_dir / "finish.json").exists():
                files.append(out_dir / "finish.json")
            return stage_fingerprint("export", {"project_name": options.project_name},
                                     files)
        if stage == "reframe":
            return stage_fingerprint("reframe", {"no_sam": options.no_sam},
                                     [out_dir / "cut.mp4", out_dir / "angles.json"])
        if stage == "captions":
            assert audio_section is not None and words_file_current is not None
            files = [words_file_current, out_dir / "angles.json"]
            if tightened:
                files.append(out_dir / "tighten.json")
            if (out_dir / "vertical.mp4").exists():
                files.append(out_dir / "vertical.mp4")
            if options.captions_horizontal and (out_dir / "cut.mp4").exists():
                files.append(out_dir / "cut.mp4")
            if options.fonts_dir is not None:
                files.append(Path(options.fonts_dir))
            return stage_fingerprint(
                "captions",
                {"preset": options.captions_preset,
                 "horizontal": options.captions_horizontal,
                 "title": options.title, "no_title": options.no_title,
                 "model": options.model, "no_muse": options.no_muse},
                files)
        if stage == "qc":
            files = [out_dir / "cut.mp4"]
            if not no_vertical:
                files.append(out_dir / "vertical.mp4")
            return stage_fingerprint("qc", {"model": options.model,
                                            "no_muse": options.no_muse}, files)
        raise AssertionError(f"unknown stage: {stage}")

    def _reload(stage: str) -> None:
        """Reload a skipped stage's state; ValueError/OSError re-executes it."""
        nonlocal sync_payload, audio_section, words_file_current, grade_payload
        nonlocal angles_payload, export_section, reframe_payload
        nonlocal qc_section, final_checks, tighten_section
        nonlocal finish_payload, finish_provider_spend, provider_spent_usd
        nonlocal captions_section
        if stage == "sync":
            loaded = read_json(out_dir / "sync.json")
            if not isinstance(loaded, dict):
                raise ValueError(f"Not a sync.json: {out_dir / 'sync.json'}")
            sync_payload = loaded
        elif stage == "words":
            previous_audio = (prev or {}).get("audio")
            if not isinstance(previous_audio, dict):
                raise ValueError("previous manifest has no audio section")
            if previous_audio.get("words_source") == "explicit":
                words_file = Path(str(previous_audio.get("words_file") or ""))
            else:
                words_file = out_dir / "words.json"
            recount = angles_mod.load_timed_words(words_file)
            audio_section = dict(previous_audio)
            audio_section["words"] = len(recount)
            if not isinstance(audio_section.get("transcribe"), dict):
                audio_section["transcribe"] = {
                    "method": "whisper",
                    "model": transcribe_mod.TRANSCRIBE_MODEL,
                    "spend_usd": 0.0, "transcript_file": None,
                    "speakers": [], "chunks": 0, "matched_share": None,
                    "muse_only": 0, "whisper_only": 0}
            words_file_current = words_file
        elif stage == "tighten":
            loaded = read_json(out_dir / "tighten.json")
            if not isinstance(loaded, dict):
                raise ValueError(
                    f"Not a tighten.json: {out_dir / 'tighten.json'}")
            tighten_section = _tighten_section(loaded)
        elif stage == "grade":
            loaded = read_json(out_dir / "grade.json")
            if not isinstance(loaded, dict):
                raise ValueError(f"Not a grade.json: {out_dir / 'grade.json'}")
            grade_payload = loaded
        elif stage == "angles":
            loaded = read_json(out_dir / "angles.json")
            if not isinstance(loaded, dict):
                raise ValueError(f"Not an angles.json: {out_dir / 'angles.json'}")
            angles_payload = loaded
        elif stage == "finish":
            loaded = read_json(out_dir / "finish.json")
            if not isinstance(loaded, dict):
                raise ValueError(f"Not a finish.json: {out_dir / 'finish.json'}")
            finish_payload = loaded
            # A skipped stage contributes this run's spend (0.0), like Meta.
            finish_provider_spend = 0.0
        elif stage == "export":
            section = (prev or {}).get("export")
            if not isinstance(section, dict):
                raise ValueError("previous manifest has no export section")
            export_section = section
        elif stage == "reframe":
            loaded = read_json(out_dir / "reframe.json")
            if not isinstance(loaded, dict):
                raise ValueError(f"Not a reframe.json: {out_dir / 'reframe.json'}")
            reframe_payload = loaded
        elif stage == "captions":
            loaded = read_json(out_dir / "captions.json")
            if not isinstance(loaded, dict):
                raise ValueError(
                    f"Not a captions.json: {out_dir / 'captions.json'}")
            captions_section = _captions_section(loaded)
        elif stage == "qc":
            section = (prev or {}).get("qc")
            if not isinstance(section, dict) or not isinstance(section.get("checks"), list):
                raise ValueError("previous manifest has no qc section")
            restored = []
            for item in section["checks"]:
                if not isinstance(item, dict):
                    raise ValueError("previous manifest has no qc section")
                restored.append(QcCheck(str(item.get("id")), str(item.get("source")),
                                        str(item.get("status")),
                                        dict(item.get("detail") or {})))
            qc_section = section
            final_checks = restored

    def _trailing_gate(stage: str) -> None:
        if spent_usd > options.max_usd:
            raise CutError(
                f"{stage}: estimated spend ${spent_usd:.2f} already exceeds "
                f"--max-usd ${options.max_usd:.2f}; raise --max-usd or pass --no-muse")

    def _execute(stage: str, fingerprint: str) -> None:
        nonlocal sync_payload, audio_section, words_file_current, grade_payload
        nonlocal angles_payload, export_section, reframe_payload
        nonlocal qc_section, final_checks, spent_usd, tighten_section
        nonlocal finish_payload, finish_provider_spend, provider_spent_usd
        nonlocal captions_section
        if stage == "sync":
            sync_kwargs: dict = {}
            if luts:
                sync_kwargs["lips_cubes"] = {path: str(cube) for path, cube in luts.items()}
            payload = sync_mod.run(
                clip_paths=list(options.clips), reference_path=options.reference_path,
                methods=list(options.methods), transcripts_dir=options.transcripts_dir,
                out_path=out_dir / "sync.json",
                whisper_model=options.whisper_model, **sync_kwargs)
            sync_payload = payload
            stage_warnings = [f"{clip.get('label')}: {note}"
                              for clip in payload.get("clips", [])
                              for note in clip.get("warnings", [])]
            records["sync"] = StageRecord("sync", "done", fingerprint, ["sync.json"],
                                          0.0, stage_warnings, None)
        elif stage == "words":
            assert sync_payload is not None
            label, clip = resolve_audio(sync_payload, options.audio_path)
            words_file, source, stage_warnings = resolve_words(
                audio_label=label, audio_clip=clip, options=options)
            validated = angles_mod.load_timed_words(words_file)
            assert validated, "words: resolved words file has no words"
            offset = float(clip.get("offset_s") or 0.0)
            shift = 0.0 if source == "explicit" else offset
            cam = clip.get("camera") or {}
            clip_path = str(cam.get("path") or "")
            duration = float(cam.get("duration_s") or 0.0)
            outputs = [] if source == "explicit" else ["words.json"]
            words_spend = 0.0
            eligible = (source != "explicit" and not options.no_muse
                        and not options.no_transcribe
                        and bool(cam.get("has_audio")))
            if not eligible:
                transcribe_record: dict = {
                    "method": "skipped", "model": None, "spend_usd": 0.0,
                    "transcript_file": None, "speakers": [], "chunks": 0,
                    "matched_share": None, "muse_only": 0, "whisper_only": 0}
                (out_dir / "transcript.json").unlink(missing_ok=True)
            else:
                remaining = max(0.0, options.max_usd - spent_usd)
                clip_local = shift_words(validated, -offset)
                try:
                    transcribed = transcribe_mod.transcribe_audio_clip(
                        clip_path=Path(clip_path),
                        whisper_words=clip_local,
                        duration_s=duration,
                        work_dir=out_dir / "transcribe",
                        max_usd=remaining)
                except transcribe_mod.TranscribeError as exc:
                    transcribed = transcribe_mod.TranscribeResult(
                        method="whisper", words=clip_local, turns=[],
                        speakers=[], chunks=[], stats=None, spend_usd=0.0,
                        warnings=[f"transcribe_failed:TranscribeError: {exc}"])
                if transcribed.method == "muse":
                    shifted = shift_words(transcribed.words, offset)
                    write_json(out_dir / "words.json",
                               words_file_payload(shifted))
                    shifted_result = replace(
                        transcribed, words=shifted,
                        turns=[replace(turn, start_s=turn.start_s + offset,
                                       end_s=turn.end_s + offset)
                               for turn in transcribed.turns])
                    write_json(out_dir / "transcript.json",
                               transcribe_mod.build_transcript_payload(
                                   audio_label=label, clip_path=clip_path,
                                   offset_s=offset, duration_s=duration,
                                   result=shifted_result))
                    outputs = ["words.json", "transcript.json"]
                    words_spend = transcribed.spend_usd
                    validated = shifted
                    stats = transcribed.stats
                    transcribe_record = {
                        "method": "muse",
                        "model": transcribe_mod.TRANSCRIBE_MODEL,
                        "spend_usd": round(words_spend, 4),
                        "transcript_file": "transcript.json",
                        "speakers": [entry["id"]
                                     for entry in transcribed.speakers],
                        "chunks": len(transcribed.chunks),
                        "matched_share": (round(stats.matched_share, 4)
                                          if stats is not None else None),
                        "muse_only": stats.muse_only if stats is not None else 0,
                        "whisper_only": (stats.whisper_only
                                         if stats is not None else 0)}
                else:
                    (out_dir / "transcript.json").unlink(missing_ok=True)
                    transcribe_record = {
                        "method": "whisper",
                        "model": transcribe_mod.TRANSCRIBE_MODEL,
                        "spend_usd": 0.0, "transcript_file": None,
                        "speakers": [], "chunks": 0, "matched_share": None,
                        "muse_only": 0, "whisper_only": 0}
                stage_warnings.extend(transcribed.warnings)
            if source != "explicit":
                kept_words = words_on_camera(validated, camera_coverage(sync_payload))
                if 0 < len(kept_words) < len(validated):
                    stage_warnings.append(
                        f"words_outside_cameras_dropped:{len(validated) - len(kept_words)}")
                    validated = kept_words
                    write_json(out_dir / "words.json", words_file_payload(validated))
            spent_usd += words_spend
            audio_section = {
                "label": label,
                "clip_path": clip_path,
                "offset_s": round(offset, 3),
                "words_source": source,
                "words_file": str(words_file) if source == "explicit" else "words.json",
                "words": len(validated),
                "words_shifted_s": round(shift, 3),
                "transcribe": transcribe_record,
            }
            words_file_current = words_file
            records["words"] = StageRecord("words", "done", fingerprint, outputs,
                                           words_spend, stage_warnings, None)
        elif stage == "tighten":
            assert audio_section is not None and words_file_current is not None
            try:
                payload = tighten_mod.run(
                    sync_path=out_dir / "sync.json",
                    words_path=words_file_current,
                    out_path=out_dir / "tighten.json",
                    audio_label=audio_section["label"],
                    pause_limit_s=options.pause_limit_s,
                    target_s=options.target_s, model=options.model,
                    no_muse=options.no_muse,
                    max_usd=max(0.0, options.max_usd - spent_usd))
            except Exception as exc:
                if isinstance(exc, tighten_mod.TightenError):
                    # Already stage-prefixed; CutError carries it through
                    # _fail unchanged.
                    raise CutError(str(exc)) from exc
                raise CutError(f"tighten: {exc}") from exc
            spend = _spend_of(payload)
            spent_usd += spend
            tighten_section = _tighten_section(payload)
            records["tighten"] = StageRecord(
                "tighten", "done", fingerprint, ["tighten.json"],
                spend, list(payload.get("warnings", [])), None)
        elif stage == "grade":
            do_check = bool(options.grade_check and not options.no_muse)
            if do_check:
                _trailing_gate("grade")
            payload = grade_mod.run(
                sync_path=out_dir / "sync.json", out_path=out_dir / "grade.json",
                reference_label=None, lut_specs=_grade_lut_specs(), no_match=False,
                check=do_check, model=options.model)
            grade_payload = payload
            spend = _spend_of(payload)
            spent_usd += spend
            outputs = ["grade.json"] + [str(item["lut"])
                                        for item in payload.get("cameras", [])
                                        if item.get("lut") is not None]
            records["grade"] = StageRecord("grade", "done", fingerprint, outputs,
                                           spend, list(payload.get("warnings", [])), None)
        elif stage == "angles":
            _trailing_gate("angles")
            assert audio_section is not None and words_file_current is not None
            angles_kwargs: dict = {
                "sync_path": out_dir / "sync.json",
                "words_path": words_file_current,
                "out_path": out_dir / "angles.json",
                "audio_label": audio_section["label"],
                "render_path": out_dir / "cut.mp4",
                "min_shot_s": options.min_shot_s, "model": options.model,
                "no_muse": options.no_muse,
                "grade_path": out_dir / "grade.json" if graded else None,
            }
            if tightened:
                angles_kwargs["tighten_path"] = out_dir / "tighten.json"
            try:
                payload = angles_mod.run(**angles_kwargs)
            except Exception as exc:
                if str(exc) == "nothing to render":
                    raise CutError(
                        "angles: no shots: no camera covers any spoken line; "
                        "check that the words belong to the audio clip and the sync "
                        "offsets in cut.json") from exc
                raise
            angles_payload = payload
            spend = _spend_of(payload)
            spent_usd += spend
            records["angles"] = StageRecord(
                "angles", "done", fingerprint, ["angles.json", "cut.mp4"],
                spend, list(payload.get("warnings", [])), None)
        elif stage == "finish":
            from . import finish as finish_mod

            payload = finish_mod.run(
                job_dir=options.out_dir, model=options.model,
                no_muse=options.no_muse, sound=options.finish_sound,
                isolate=options.finish_isolate,
                loudness=options.finish_loudness,
                max_provider_usd=max(0.0, options.max_provider_usd
                                     - provider_spent_usd),
                max_usd=max(0.0, options.max_usd - spent_usd))
            finish_payload = payload
            meta_spend = float((payload.get("design") or {}).get("meta_usd") or 0.0)
            finish_provider_spend = float(payload["provider_spend_usd"])
            provider_spent_usd += finish_provider_spend
            spent_usd += meta_spend
            # Finish rewrites cut.mp4, so the pre-run fingerprint could
            # never match on resume; the stored print is post-run.
            fingerprint = _fingerprint("finish")
            records["finish"] = StageRecord(
                "finish", "done", fingerprint, ["finish.json", "cut.mp4"],
                meta_spend, list(payload["warnings"]), None)
        elif stage == "export":
            finish_file = out_dir / "finish.json"
            export_kwargs: dict = {
                "angles_path": out_dir / "angles.json",
                "sync_path": out_dir / "sync.json",
                "grade_path": out_dir / "grade.json" if graded else None,
                "fcpxml_path": out_dir / "timeline.fcpxml",
                "otio_path": out_dir / "timeline.otio.json",
                "project_name": options.project_name,
            }
            if (records.get("finish") is not None
                    and records["finish"].status == "done"
                    and finish_file.exists()):
                export_kwargs["finish_path"] = finish_file
            section = export_timelines(**export_kwargs)
            export_section = section
            records["export"] = StageRecord(
                "export", "done", fingerprint,
                ["timeline.fcpxml", "timeline.otio.json"],
                0.0, list(section.get("warnings", [])), None)
        elif stage == "reframe":
            if options.no_sam:
                _trailing_gate("reframe")
            else:
                stats = extract_media_stats(probe_media(out_dir / "cut.mp4"))
                horizontal_s = float(stats.get("duration_s") or 0.0)
                estimate = reframe_mod.estimate_cost_usd(
                    reframe_mod.proxy_frame_count(horizontal_s))
                if spent_usd + estimate > options.max_usd:
                    raise CutError(
                        f"reframe: estimated SAM cost ${estimate:.2f} would push "
                        f"spend ${spent_usd:.2f} over --max-usd ${options.max_usd:.2f}; "
                        "raise --max-usd or pass --no-vertical")
            payload = reframe_mod.run(
                video_path=out_dir / "cut.mp4", out_path=out_dir / "reframe.json",
                render_path=out_dir / "vertical.mp4",
                shots_path=out_dir / "angles.json", no_sam=options.no_sam,
                max_usd=max(0.0, options.max_usd - spent_usd))
            reframe_payload = payload
            spend = _spend_of(payload)
            spent_usd += spend
            records["reframe"] = StageRecord(
                "reframe", "done", fingerprint, ["reframe.json", "vertical.mp4"],
                spend, list(payload.get("warnings", [])), None)
        elif stage == "captions":
            from . import captions as captions_mod

            assert audio_section is not None and words_file_current is not None
            tighten_file = out_dir / "tighten.json" if tightened else None
            try:
                payload = captions_mod.run(
                    words_path=words_file_current,
                    angles_path=out_dir / "angles.json",
                    tighten_path=tighten_file,
                    out_dir=out_dir,
                    preset=options.captions_preset,
                    title=options.title, no_title=options.no_title,
                    fonts_dir=Path(options.fonts_dir)
                    if options.fonts_dir is not None else None,
                    burn_horizontal=options.captions_horizontal,
                    model=options.model, no_muse=options.no_muse,
                    max_usd=max(0.0, options.max_usd - spent_usd))
            except Exception as exc:
                if isinstance(exc, captions_mod.CaptionsError):
                    raise CutError(str(exc)) from exc
                raise CutError(f"captions: {exc}") from exc
            spend = _spend_of(payload)
            spent_usd += spend
            captions_section = _captions_section(payload)
            outputs = ["captions.srt", "captions.ass", "captions.json"]
            if payload.get("ass_horizontal"):
                outputs.append(str(payload["ass_horizontal"]))
            if payload.get("title"):
                outputs.append("title.png")
                if "cut.mp4" in (payload.get("burned") or []) \
                        and (out_dir / "title-1920x1080.png").exists():
                    outputs.append("title-1920x1080.png")
            outputs.extend(str(name) for name in (payload.get("burned") or []))
            # Captions rewrites the mp4s, so the pre-run fingerprint could
            # never match on resume; the stored print is post-run.
            fingerprint = _fingerprint("captions")
            records["captions"] = StageRecord(
                "captions", "done", fingerprint, outputs,
                spend, list(payload.get("warnings", [])), None)
        elif stage == "qc":
            assert sync_payload is not None and angles_payload is not None
            checks = mechanical_qc(
                out_dir=out_dir, sync_payload=sync_payload,
                angles_payload=angles_payload, reframe_payload=reframe_payload,
                export_section=export_section,
                captions_section=captions_section)
            stage_warnings: list[str] = []
            muse_spend = 0.0
            if options.no_muse:
                stage_warnings.append("muse checks disabled by --no-muse")
            else:
                master_stats = _probe_stats(out_dir / "cut.mp4")
                if master_stats is not None:
                    duration_s = float(master_stats.get("duration_s") or 0.0)
                    if options.model not in TOKEN_RATES_USD_PER_1M:
                        top_warnings.append(f"qc_estimate_default_rates:{options.model}")
                    estimate = estimate_qc_usd(duration_s, options.model)
                    remaining = options.max_usd - spent_usd
                    if spent_usd + estimate > options.max_usd:
                        note = ("qc_muse_skipped_over_budget:"
                                f"est ${estimate:.2f} over remaining ${remaining:.2f}")
                        top_warnings.append(note)
                        stage_warnings.append(note)
                    else:
                        proxy = out_dir / "qc" / "proxy.mp4"
                        try:
                            proxy.parent.mkdir(parents=True, exist_ok=True)
                            run_command(qc_proxy_command(out_dir / "cut.mp4", proxy))
                            muse_checks, muse_spend, muse_warnings = muse_qc(
                                proxy=proxy, duration_s=duration_s,
                                model=options.model, angles_payload=angles_payload)
                        except QcUnavailable as exc:
                            note = f"qc_muse_unavailable:{exc}"
                            top_warnings.append(note)
                            stage_warnings.append(note)
                            muse_spend = 0.0
                        except Exception as exc:
                            note = (f"qc_muse_failed:{type(exc).__name__}: "
                                    f"{str(exc)[:160]}")
                            top_warnings.append(note)
                            stage_warnings.append(note)
                            muse_spend = 0.0
                        else:
                            checks.extend(muse_checks)
                            stage_warnings.extend(muse_warnings)
                            spent_usd += muse_spend
            status = "pass"
            if any(check.status == "fail" for check in checks):
                status = "fail"
            elif any(check.status == "warn" for check in checks):
                status = "warn"
            qc_section = {
                "status": status,
                "checks": [{"id": check.id, "source": check.source,
                            "status": check.status, "detail": check.detail}
                           for check in checks],
                "spend_usd": round(muse_spend, 4),
            }
            final_checks = checks
            records["qc"] = StageRecord("qc", "done", fingerprint, [], muse_spend,
                                        stage_warnings, None)

    _write_manifest(out_dir, _manifest("running", None))
    for stage in enabled:
        try:
            fingerprint = _fingerprint(stage)
        except Exception as exc:
            _fail(stage, "", exc)
            raise AssertionError("unreachable")
        prev_record = prev_stages.get(stage)
        skippable = (not options.force and not dirty
                     and isinstance(prev_record, dict)
                     and prev_record.get("status") in ("done", "skipped")
                     and prev_record.get("fingerprint") == fingerprint
                     and all((out_dir / name).exists()
                             for name in (prev_record.get("outputs") or [])))
        if skippable:
            assert isinstance(prev_record, dict)
            try:
                _reload(stage)
            except (OSError, ValueError):
                skippable = False
            else:
                records[stage] = StageRecord(
                    stage, "skipped", fingerprint,
                    list(prev_record.get("outputs") or []), 0.0,
                    list(prev_record.get("warnings") or []), None)
                _write_manifest(out_dir, _manifest("running", None))
                continue
        try:
            _execute(stage, fingerprint)
        except Exception as exc:
            if stage == "finish":
                # A failed finish degrades: the job continues on the
                # untouched cut.mp4 and ships a video anyway (§13.3).
                message = str(exc)
                if not message.startswith("finish: "):
                    message = f"finish: {exc}"
                records["finish"] = StageRecord(
                    "finish", "failed", fingerprint, [], 0.0, [], message)
                _write_manifest(out_dir, _manifest("running", None))
                continue
            _fail(stage, fingerprint, exc)
            raise AssertionError("unreachable")
        dirty = True
        _write_manifest(out_dir, _manifest("running", None))

    if not no_qc:
        failed_ids = [check.id for check in final_checks
                      if check.source == "mechanical" and check.status == "fail"]
        if failed_ids:
            _write_manifest(out_dir, _manifest("failed", "qc"))
            raise CutError(
                f"qc: finished cut failed mechanical checks: {', '.join(failed_ids)} "
                f"(files kept in {out_dir})")
    status = "complete"
    if (any(record.warnings for record in records.values())
            or any(record.status == "failed" for record in records.values())
            or any(check.status == "warn" for check in final_checks)
            or any(check.source == "muse" and check.id != "muse:overall"
                   for check in final_checks)):
        status = "complete_with_warnings"
    _write_manifest(out_dir, _manifest(status, None))
    return _manifest(status, None)
