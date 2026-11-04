"""Voice notes that edit the cut: spoken notes become checked cutlist operations.

Two ways in, one result: the editor talks while watching the cut in the
app (each recording anchored by the playhead time it was spoken at), or
drops a long voice memo / phone video on the app or on `director voice`.
Notes are transcribed (Meta Voice Transcribe, local Whisper fallback),
split into separate notes, resolved to the timeline from playhead times,
spoken timecodes or quoted words, and turned into the same operations
`cutlist.apply_operations` already takes — checked by the same mechanical
checks, applied through the same function, re-rendered by the same render.
A note Director cannot do says so plainly instead of guessing.

WAV extraction, long-note chunking and the diarizer wrapper are reused
from `transcribe.py` (TRANSCRIBE-DESIGN.md §10); this module adds no
second copy of any of them.

Designed by Muse Code on muse-wonder-1.0, built by Muse Code on
muse-spark-1.3 from docs/muse-code/VOICE-DESIGN.md.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ghostcut.config import read_json, write_json
from ghostcut.ffmpeg_tools import (
    ToolingError,
    extract_media_stats,
    probe_media,
    run_command,
)

from . import angles as angles_mod
from . import cutlist
from . import transcribe as transcribe_mod

TRANSCRIBE_SR = 16000
MAX_NOTE_S = 1200
ESTIMATE_CONFIRM_S = 60
DEFAULT_MAX_USD = 2.00
RESOLVE_MAX_OUTPUT_TOKENS = 8000
QUOTE_MIN_WORDS = 2
ANCHOR_WINDOW_S = 6.0
CONFIDENCE_FLOOR = 0.55
TASTE_MAX_ENTRIES = 50

TRANSCRIBE_PER_SECOND_USD = 0.18 / 3600
SPARK_INPUT_PER_1M_USD = 1.25
SPARK_OUTPUT_PER_1M_USD = 4.25
RESOLVE_MODEL = "muse-spark-1.3"
RESOLVE_CONTEXT_TOKENS_PER_NOTE_MIN = 1200
NOTE_SPLIT_PAUSE_S = 1.5
VISUAL_STILLS = 3
VISUAL_FRAME_BUDGET = 4
STILL_W = 384

DEVELOPER = (
    "You are Muse, the editing assistant inside GhostCut Director. The "
    "editor has watched the cut and spoken notes about it; the notes were "
    "transcribed and may mishear words. Below are the cut's beats (the "
    "takes Director kept, with their alternates), its shots, its "
    "transcript with output times, the editor's standing preferences, and "
    "the spoken notes, each with any anchor time we recorded. Split the "
    "speech into separate notes and turn each into the operations listed "
    "— nothing else exists. \"Use the other take\", \"the second version\", "
    "\"go back to the first one\" mean swap_take to the named alternate of "
    "the beat the note is anchored to. \"Lose\", \"cut\", \"drop\" a part mean "
    "drop_beat or drop_span for the beat it belongs to. \"Cut the part "
    "where I say X\", \"cut every/both mention(s) of X\" mean drop_words "
    "with the quoted words in quote and word_start/word_end -1 (Director "
    "finds the words — never invent word numbers). \"Go to the wide / "
    "the close-up / the iPhone angle\" mean set_angle for that beat to the "
    "camera whose shot size or label matches. \"At the very start / the "
    "opening / the first shot\" and \"at the end\" anchor to the opening "
    "or closing beat. \"That pause is too long\" or "
    "\"tighten the pauses\" mean set_pause_limit (0.5 if a number is not "
    "given, or the number of seconds spoken). \"Make it under three "
    "minutes\" means retime_target. \"Always\", \"never\", \"this client "
    "hates/loves\" mean add_taste as well as any concrete change. When a "
    "note only comments, asks a question, or asks for something no "
    "operation does (music level, colour, new footage), return it as a "
    "plain note with no operations and say what you heard. Give for every "
    "note the anchor you used and a one-line \"I heard\" paraphrase. Never "
    "invent a beat, a take, a camera or words that are not below; if a "
    "note could mean two beats, say ambiguous and list both — do not pick "
    "one quietly."
)

VISUAL_DEVELOPER = (
    "You are checking one editing note against stills from the finished "
    "cut. Say whether the moment the note describes is visible, and which "
    "camera label (shown on each still) it belongs to. Never invent an "
    "action you cannot see."
)

RESOLVE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["notes"],
    "properties": {"notes": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["note_index", "heard", "anchor_kind", "at_s", "quote",
                     "beat_id", "status", "message", "confidence", "ops",
                     "taste"],
        "properties": {
            "note_index": {"type": "integer"},
            "heard": {"type": "string"},
            "confidence": {"type": "number"},
            "anchor_kind": {"type": "string", "enum": ["playhead",
                                                       "spoken_timecode",
                                                       "quote", "beat",
                                                       "global", "none"]},
            "at_s": {"type": "number"},
            "quote": {"type": "string"},
            "beat_id": {"type": "integer"},
            "status": {"type": "string", "enum": ["resolved", "ambiguous",
                                                 "unresolved",
                                                 "preference"]},
            "message": {"type": "string"},
            "ops": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["op", "beat_id", "attempt_id", "span_id",
                             "camera", "pause_limit_s", "target_s",
                             "word_start", "word_end", "text", "why"],
                "properties": {
                    "op": {"type": "string", "enum": [
                        "swap_take", "drop_beat", "restore_beat",
                        "drop_span", "restore_span", "drop_words",
                        "restore_words", "set_angle",
                        "clear_angle", "set_pause_limit", "add_note",
                        "add_taste", "retime_target"]},
                    "beat_id": {"type": "integer"},
                    "attempt_id": {"type": "integer"},
                    "span_id": {"type": "string"},
                    "camera": {"type": "string"},
                    "pause_limit_s": {"type": "number"},
                    "target_s": {"type": "number"},
                    "word_start": {"type": "integer"},
                    "word_end": {"type": "integer"},
                    "text": {"type": "string"},
                    "why": {"type": "string"},
                },
            }},
            "taste": {"type": "array", "items": {"type": "string"}},
        },
    }}}}

VISUAL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["confirmed", "camera", "description"],
    "properties": {
        "confirmed": {"type": "boolean"},
        "camera": {"type": "string"},
        "description": {"type": "string"},
    },
}

CUTLIST_OPS = ("swap_take", "drop_beat", "restore_beat", "drop_span",
               "restore_span", "drop_words", "restore_words", "set_angle",
               "clear_angle", "set_pause_limit", "add_note")
VOICE_ONLY_OPS = ("add_taste", "retime_target")
BEAT_OPS = ("swap_take", "drop_beat", "restore_beat", "set_angle",
            "clear_angle")

MIX_MESSAGE = "Director can't change the mix — noted for the edit"
NO_SHOT_MESSAGE = "No such shot in this cut — noted"
UNPLACED_MESSAGE = "I couldn't place that note in the cut"
NO_SPEECH_LIVE = "I didn't hear any speech in that recording"
NO_SPEECH_FILE = "I didn't hear any speech in that file"
NOT_TIGHTENED_MESSAGE = (
    "This cut was not tightened, so there are no alternate takes to swap. "
    "Re-run director cut without --no-tighten.")
STALE_MESSAGE = "This cut changed since the proposal — ask again"
FRAME_UNAVAILABLE = "Frame check unavailable (no Meta client)"
TASTE_CORRUPT_WARNING = "taste file unreadable, starting empty"
MUSE_UNAVAILABLE_MESSAGE = (
    "Muse is unavailable, so these notes are transcribed only. Type the "
    "change, or run the engine with the Meta client installed.")

MIX_WORDS = frozenset({
    "music", "musical", "mix", "mixed", "mixing", "volume", "loud",
    "louder", "loudness", "quieter", "quiet", "colour", "color",
    "colours", "colors", "grade", "grading", "saturation", "exposure",
    "contrast", "bright", "brighter", "dark", "darker", "white balance",
    "soundtrack", "song", "mastering", "master",
})

VISUAL_WORDS = frozenset({
    "point", "points", "pointing", "pointed", "holds", "holding",
    "holds up", "holding up", "hold up", "wearing", "wears", "gesture",
    "gestures", "wave", "waving", "waves", "when she", "when he",
    "where she", "where he", "the shot where",
})


class VoiceError(RuntimeError):
    """A voice step failed in a way the operator must see.

    The message always starts with 'voice: ', except for the plain
    per-note messages (§10) carried on notes and ops.
    """


class VoiceUnavailable(RuntimeError):
    """guestlist (the Meta API client) cannot be imported."""


@dataclass(slots=True)
class Turn:
    start_s: float
    end_s: float
    text: str
    speaker: str = ""


@dataclass(slots=True)
class JobContext:
    job_dir: Path
    cut: dict | None
    tighten: dict | None
    angles: dict
    sync: dict
    words: list
    covers: dict[int, list[str]] = field(default_factory=dict)
    tighten_path: Path | None = None
    angles_path: Path | None = None


@dataclass(slots=True)
class ResolvedNote:
    id: str
    source: str
    raw_text: str
    heard: str
    anchor_kind: str
    at_s: float | None
    beat_id: int
    status: str
    message: str
    ops: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    engine: str = ""
    usd: float = 0.0

    def as_dict(self) -> dict:
        return {
            "id": self.id, "source": self.source,
            "raw_text": self.raw_text, "heard": self.heard,
            "anchor_kind": self.anchor_kind,
            "at_s": None if self.at_s is None else round(self.at_s, 3),
            "beat_id": self.beat_id, "status": self.status,
            "message": self.message, "ops": [dict(op) for op in self.ops],
            "candidates": [dict(c) for c in self.candidates],
            "engine": self.engine, "usd": round(self.usd, 4),
        }


# ------------------------------------------------- transcription (§4)


def extract_audio_command(src: Path, wav: Path) -> list[str]:
    """The note to the 16 kHz mono WAV `diarize` requires. Pure.

    One command list in the repo: this delegates to
    `transcribe.extract_wav_command` (TRANSCRIBE-DESIGN.md §10).
    """
    return transcribe_mod.extract_wav_command(src, wav)


def probe_note_duration(note_path: Path) -> float:
    """Note length in seconds; the §4.1 refusals fire here, before spend."""
    name = Path(note_path).name
    try:
        stats = extract_media_stats(probe_media(Path(note_path)))
    except (ToolingError, OSError, ValueError) as error:
        raise VoiceError(
            f"voice: no audio in {name}; record a voice memo or a video "
            f"with sound") from error
    if not stats.get("has_audio"):
        raise VoiceError(f"voice: no audio in {name}; record a voice memo "
                         f"or a video with sound")
    duration = float(stats.get("duration_s") or 0.0)
    if duration > MAX_NOTE_S:
        mins = duration / 60.0
        raise VoiceError(
            f"voice: note is {mins:.0f} min; the limit is 20 min — split "
            f"it or trim the file")
    return duration


def estimate_note_usd(duration_s: float, transcript_chars: int = 0) -> float:
    """Transcribe (per-second rate) + one resolve call. Pure.

    The resolve term estimates the fixed cut context per note-minute plus
    the transcript at 4 chars/token on the input side, and
    RESOLVE_MAX_OUTPUT_TOKENS on the output side.
    """
    transcribe = duration_s * TRANSCRIBE_PER_SECOND_USD
    input_tokens = ((duration_s / 60.0)
                    * RESOLVE_CONTEXT_TOKENS_PER_NOTE_MIN
                    + transcript_chars / 4.0)
    resolve = (input_tokens * SPARK_INPUT_PER_1M_USD / 1e6
               + RESOLVE_MAX_OUTPUT_TOKENS * SPARK_OUTPUT_PER_1M_USD / 1e6)
    return transcribe + resolve


def _diarize_result_to_turns(result: dict, offset_s: float = 0.0
                             ) -> list[Turn]:
    """A `diarize` result dict to note turns. Pure."""
    out: list[Turn] = []
    for turn in result.get("turns", []) or []:
        if not isinstance(turn, dict):
            continue
        try:
            text = str(turn.get("transcript") or "").strip()
            start = offset_s + float(turn.get("startMs") or 0) / 1000.0
            end = offset_s + float(turn.get("endMs") or 0) / 1000.0
        except (TypeError, ValueError):
            continue
        if not text or end <= start:
            continue
        out.append(Turn(start, end, text, str(turn.get("speaker") or "")))
    return out


def _note_pauses(wav: Path, duration_s: float) -> list[tuple[float, float]]:
    """Silence spans in the note WAV as chunking pauses. Never raises."""
    try:
        proc = run_command(transcribe_mod.silence_detect_command(wav),
                           check=False)
        return transcribe_mod.parse_silence_spans(proc.stderr or "",
                                                  duration_s)
    except OSError:
        return []


def _split_oversize(chunks: list[transcribe_mod.Chunk],
                    duration_s: float) -> list[transcribe_mod.Chunk]:
    """Split planned chunks still over CHUNK_MAX_S evenly. Pure.

    Reached only when the note has no usable pauses (the planner never
    cuts inside speech, so with no pauses it plans one chunk).
    """
    out: list[transcribe_mod.Chunk] = []
    for chunk in chunks:
        core = chunk.core_end_s - chunk.core_start_s
        if core <= transcribe_mod.CHUNK_MAX_S:
            out.append(chunk)
            continue
        pieces = math.ceil(core / transcribe_mod.CHUNK_TARGET_S)
        edges = [chunk.core_start_s + core * i / pieces
                 for i in range(pieces + 1)]
        edges[-1] = min(edges[-1], duration_s)
        for pos in range(pieces):
            start = edges[pos]
            out.append(transcribe_mod.Chunk(
                len(out), start, edges[pos + 1],
                start if not out else max(
                    0.0, start - transcribe_mod.CHUNK_OVERLAP_S),
                edges[pos + 1]))
    for pos, chunk in enumerate(out):
        chunk.id = pos
    return out


def _diarize_long_note(wav: Path, duration_s: float, work_dir: Path
                       ) -> tuple[list[Turn], float]:
    """Chunked Meta transcription for notes over CHUNK_MAX_S.

    Silence-chunked exactly like footage (TRANSCRIBE-DESIGN.md §10);
    note speakers stay diarizer labels and overlap turns are deduped by
    chunk core, never reconciled.
    """
    pauses = _note_pauses(wav, duration_s)
    chunks = _split_oversize(
        transcribe_mod.plan_chunks(duration_s, pauses), duration_s)
    turns: list[Turn] = []
    spend = 0.0
    for chunk in chunks:
        chunk_wav = work_dir / f"note-{chunk.id:03d}.wav"
        run_command(transcribe_mod.chunk_wav_command(
            wav, chunk.start_s, chunk.end_s - chunk.start_s, chunk_wav))
        result, chunk_spend, _cached = transcribe_mod.diarize_chunk(
            chunk_wav)
        spend += chunk_spend
        last = chunk.id == len(chunks) - 1
        for turn in _diarize_result_to_turns(result, chunk.start_s):
            mid = (turn.start_s + turn.end_s) / 2
            if mid < chunk.core_start_s:
                continue
            if last:
                if mid > chunk.core_end_s:
                    continue
            elif mid >= chunk.core_end_s:
                continue
            turns.append(turn)
    turns.sort(key=lambda turn: (turn.start_s, turn.end_s))
    return turns, spend


def _whisper_turns(wav: Path) -> list[Turn]:
    """Local mlx_whisper fallback: segments JSON to turns."""
    binary = shutil.which("mlx_whisper")
    if binary is None:
        raise VoiceError("mlx_whisper is not on PATH")
    with tempfile.TemporaryDirectory(prefix="director-voice-") as tmp:
        run_command([binary, str(wav), "--output-format", "json",
                     "--output-dir", tmp, "--word-timestamps", "True",
                     "--verbose", "False"])
        payload = json.loads(
            (Path(tmp) / f"{wav.stem}.json").read_text(encoding="utf-8"))
    out: list[Turn] = []
    for segment in payload.get("segments", []) or []:
        if not isinstance(segment, dict):
            continue
        try:
            text = str(segment.get("text") or "").strip()
            start, end = float(segment.get("start", 0.0)), float(
                segment.get("end", 0.0))
        except (TypeError, ValueError):
            continue
        if not text or end <= start:
            continue
        out.append(Turn(start, end, text, ""))
    return out


def transcribe_wav(wav: Path, *, duration_s: float,
                   allow_whisper: bool = True,
                   work_dir: Path | None = None,
                   warnings: list[str] | None = None
                   ) -> tuple[list[Turn], str, float]:
    """(turns, engine, spend_usd); engine is "meta" or "whisper".

    The Meta path reuses `transcribe.diarize_chunk` (single call up to
    CHUNK_MAX_S, silence-chunked above that). Any Meta failure falls to
    local Whisper when `allow_whisper` — at once, no retries (a 402 is
    answered immediately and the transcribe call has none) — and, when
    a `warnings` list is passed, says why: "Muse unavailable (402),
    heard with local Whisper". With neither available the §4.2
    VoiceError names both.
    """
    wav = Path(wav)
    try:
        if duration_s > transcribe_mod.CHUNK_MAX_S:
            scratch = Path(work_dir) if work_dir is not None else wav.parent
            scratch.mkdir(parents=True, exist_ok=True)
            turns, spend = _diarize_long_note(wav, duration_s, scratch)
        else:
            result, spend, _cached = transcribe_mod.diarize_chunk(wav)
            turns = _diarize_result_to_turns(result)
        return turns, "meta", spend
    except Exception as error:
        if isinstance(error, (VoiceError, KeyboardInterrupt)):
            raise
        meta_error = error
    if not allow_whisper:
        raise VoiceError(
            "voice: cannot transcribe: the Meta client is unavailable "
            f"({meta_error}); drop --no-whisper for the local Whisper "
            "fallback, or type the note instead") from meta_error
    try:
        heard = _whisper_turns(wav)
    except VoiceError as error:
        if "not on PATH" in str(error):
            raise VoiceError(
                "voice: cannot transcribe: the Meta client is unavailable "
                f"({meta_error}) and mlx_whisper is not on PATH; install "
                "one of them, or type the note instead") from meta_error
        raise VoiceError(
            f"voice: local Whisper failed ({error}); check the recording "
            "or type the note instead") from error
    except (ToolingError, OSError, ValueError) as error:
        raise VoiceError(
            f"voice: local Whisper failed ({error}); check the recording "
            "or type the note instead") from error
    if warnings is not None:
        reason = (muse_unavailable_reason(meta_error)
                  or type(meta_error).__name__)
        warnings.append(WHISPER_FALLBACK_WARNING.format(reason=reason))
    return heard, "whisper", 0.0


# ------------------------------------------------- job context (§6.2)


def _read_json(path: Path) -> dict | None:
    try:
        data = read_json(Path(path))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _compute_covers(sync: dict, words: list,
                    tighten: dict | None) -> dict[int, list[str]]:
    """Beat id → labels covering every line of the beat's pick.

    The `angles.covering` rule over each pick's kept words; a beat with
    no lines keeps every placed label (coverage cannot be disproved).
    Pure.
    """
    clips: list[angles_mod.SyncClip] = []
    for item in sync.get("clips", []) or []:
        if not isinstance(item, dict):
            continue
        cam = item.get("camera") or {}
        offset = item.get("offset_s")
        clips.append(angles_mod.SyncClip(
            label=str(item.get("label") or ""),
            path=str(cam.get("path") or ""),
            offset_s=None if offset is None else float(offset),
            fps=float(cam.get("fps") or 0.0),
            width=int(cam.get("width") or 0),
            height=int(cam.get("height") or 0),
            duration_s=float(cam.get("duration_s") or 0.0)))
    placed = sorted({c.label for c in clips
                     if c.label and c.offset_s is not None})
    if tighten is None:
        return {}
    utterance_by_id: dict[int, dict] = {}
    for entry in tighten.get("utterances", []) or []:
        if isinstance(entry, dict):
            utterance_by_id[int(entry.get("id", -1))] = entry
    covers: dict[int, list[str]] = {}
    for beat in tighten.get("beats", []) or []:
        if not isinstance(beat, dict):
            continue
        bid = beat.get("id", -1)
        attempts = [a for a in beat.get("attempts", []) or []
                    if isinstance(a, dict)]
        pick = beat.get("pick_attempt", -1)
        if not isinstance(pick, int) or not 0 <= pick < len(attempts):
            covers[bid] = list(placed)
            continue
        chosen = attempts[pick]
        excluded = {int(uid) for uid in
                    chosen.get("excluded_utterance_ids", []) or []}
        indices: list[int] = []
        for uid in chosen.get("utterance_ids", []) or []:
            if int(uid) in excluded:
                continue
            entry = utterance_by_id.get(int(uid))
            if entry is None:
                continue
            try:
                start, end = int(entry.get("word_start", 0)), int(
                    entry.get("word_end", 0))
            except (TypeError, ValueError):
                continue
            indices.extend(range(start, end))
        kept = [words[i] for i in sorted(set(indices))
                if 0 <= i < len(words)]
        if not kept:
            covers[bid] = list(placed)
            continue
        labels: set[str] | None = None
        for line in angles_mod.split_lines(kept):
            covering = {c.label for c in angles_mod.covering(clips, line)}
            labels = covering if labels is None else labels & covering
        covers[bid] = sorted(labels or set())
    return covers


def build_job_context(job_dir: Path) -> JobContext:
    """The resolve/verify context for a `director cut` job directory."""
    job_dir = Path(job_dir)
    angles = _read_json(job_dir / "angles.json")
    if angles is None or not isinstance(angles.get("shots"), list):
        raise VoiceError(
            f"voice: {job_dir} has no angles.json; run director cut "
            "there first")
    sync = _read_json(job_dir / "sync.json")
    if sync is None or not isinstance(sync.get("clips"), list):
        raise VoiceError(
            f"voice: {job_dir} has no sync.json; run director cut "
            "there first")
    words_path = job_dir / "words.json"
    if not words_path.exists():
        raise VoiceError(
            f"voice: {job_dir} has no words.json; run director cut "
            "there first")
    words = angles_mod.load_timed_words(words_path)
    tighten: dict | None = None
    if (job_dir / "tighten.json").exists():
        try:
            tighten = cutlist.load_tighten(job_dir / "tighten.json")
        except ValueError as error:
            raise VoiceError(f"voice: {error}") from error
    return JobContext(
        job_dir=job_dir,
        cut=_read_json(job_dir / "cut.json"),
        tighten=tighten,
        angles=angles,
        sync=sync,
        words=words,
        covers=_compute_covers(sync, words, tighten),
        tighten_path=job_dir / "tighten.json"
        if tighten is not None else None,
        angles_path=job_dir / "angles.json",
    )


def build_file_context(*, work_dir: Path, sync_path: Path,
                       words_path: Path, angles_path: Path,
                       tighten_path: Path | None = None) -> JobContext:
    """The resolve/verify context for a hand-run pipeline (no job dir)."""
    angles = _read_json(angles_path)
    if angles is None or not isinstance(angles.get("shots"), list):
        raise VoiceError(f"voice: not an angles.json: {angles_path}")
    sync = _read_json(sync_path)
    if sync is None or not isinstance(sync.get("clips"), list):
        raise VoiceError(f"voice: not a sync.json: {sync_path}")
    words = angles_mod.load_timed_words(Path(words_path))
    tighten: dict | None = None
    if tighten_path is not None:
        try:
            tighten = cutlist.load_tighten(Path(tighten_path))
        except ValueError as error:
            raise VoiceError(f"voice: {error}") from error
    work_dir = Path(work_dir)
    return JobContext(
        job_dir=work_dir, cut=None, tighten=tighten, angles=angles,
        sync=sync, words=words,
        covers=_compute_covers(sync, words, tighten),
        tighten_path=Path(tighten_path) if tighten_path else None,
        angles_path=Path(angles_path))


def output_duration(job: JobContext) -> float:
    """The finished cut's length on the output timeline. Pure."""
    if job.tighten is not None:
        try:
            return float(job.tighten.get("duration_out_s", 0.0))
        except (TypeError, ValueError):
            return 0.0
    ends = [float(s.get("end_s", 0.0)) for s in job.angles.get("shots", [])
            if isinstance(s, dict)]
    return max(ends) if ends else 0.0


# ------------------------------------------------- anchors (§6.3)

_NUMBER_ONES = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
_NUMBER_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}


def _number_value(tokens: list[str]) -> float | None:
    """One to three number words (or digits) to a value. Pure."""
    if not tokens:
        return None
    words: list[str] = []
    for token in tokens:
        words.extend(token.replace("-", " ").split())
    if len(words) == 1 and re.fullmatch(r"\d+(?:\.\d+)?", words[0]):
        return float(words[0])
    total: float | None = None
    for word in words:
        if word in _NUMBER_ONES:
            value = _NUMBER_ONES[word]
        elif word in _NUMBER_TENS:
            value = _NUMBER_TENS[word]
        elif re.fullmatch(r"\d+(?:\.\d+)?", word):
            value = float(word)
        else:
            return None
        total = value if total is None else total + value
    return total


def parse_spoken_timecode(text: str) -> float | None:
    """A spoken timecode to output seconds (`1:20`, `a minute twenty`,
    `at ninety seconds`, `two minutes in`). Pure; None when absent."""
    cleaned = re.sub(r"[^\w:.\s]", " ", (text or "").lower())
    match = re.search(r"\b(\d+):(\d{2})\b", cleaned)
    if match:
        return float(match.group(1)) * 60.0 + float(match.group(2))
    tokens = cleaned.split()

    def _before(pos: int) -> float | None:
        for size in (3, 2, 1):
            if pos - size < 0:
                continue
            value = _number_value(tokens[pos - size:pos])
            if value is not None:
                return value
        return None

    def _after(pos: int) -> float:
        for size in (2, 1):
            chunk = tokens[pos + 1:pos + 1 + size]
            if not chunk:
                continue
            value = _number_value(chunk)
            if value is not None:
                return value
        return 0.0

    for pos, token in enumerate(tokens):
        if token in ("minute", "minutes", "min", "mins"):
            minutes = _before(pos)
            if minutes is None:
                continue
            # "a minute twenty": a bare number after minutes is seconds.
            return minutes * 60.0 + _after(pos)
        if token in ("second", "seconds", "sec", "secs"):
            seconds = _before(pos)
            if seconds is not None:
                return seconds
    return None


def normalize_quote_token(text: str) -> str:
    """Lower-case, punctuation stripped. Pure."""
    return re.sub(r"[^a-z0-9']+", "", (text or "").lower()).strip("'")


_TOKEN_SPLIT_RE = re.compile(r"[-\u2010-\u2015]")


def _quote_want_tokens(quote: str) -> list[str]:
    """The quote's normalised tokens; hyphens split ("B-roll" -> b, roll).

    Pure. Both sides split, so "B roll" and "B-roll" match either way.
    """
    want: list[str] = []
    for chunk in (quote or "").split():
        for piece in _TOKEN_SPLIT_RE.split(chunk):
            token = normalize_quote_token(piece)
            if token:
                want.append(token)
    return want


def match_quote_ranges(words: list, quote: str) -> list[tuple[int, int]]:
    """(word_start, word_end) of every consecutive-token match. Pure.

    Needs QUOTE_MIN_WORDS content (non-filler) tokens; one word is not
    an anchor, it is a coincidence.
    """
    stream: list[tuple[str, int]] = []
    for index, word in enumerate(words):
        text = word.text if hasattr(word, "text") else word
        for chunk in str(text or "").split():
            for piece in _TOKEN_SPLIT_RE.split(chunk):
                token = normalize_quote_token(piece)
                if token:
                    stream.append((token, index))
    want = _quote_want_tokens(quote)
    content = [t for t in want if t not in transcribe_mod.FILLER_TOKENS]
    if len(content) < QUOTE_MIN_WORDS:
        return []
    out: list[tuple[int, int]] = []
    for i in range(len(stream) - len(want) + 1):
        if [token for token, _ in stream[i:i + len(want)]] == want:
            found = (stream[i][1], stream[i + len(want) - 1][1] + 1)
            if found not in out:
                out.append(found)
    return out


def match_quote(words: list, quote: str) -> list[int]:
    """Start word indices where the quote matches consecutively. Pure.

    Needs QUOTE_MIN_WORDS content (non-filler) tokens; one word is not
    an anchor, it is a coincidence.
    """
    return [start for start, _ in match_quote_ranges(words, quote)]


def match_kept_ranges(job: JobContext, quote: str) -> list[tuple[int, int]]:
    """Global word ranges where the quote matches inside one kept span.

    Pure. A match straddling a cut is not contiguous in the cut and is
    dropped; untightened jobs keep every word.
    """
    ranges = match_quote_ranges(job.words, quote)
    if job.tighten is None or not ranges:
        return ranges
    spans = [(float(s.get("start_s", 0.0)), float(s.get("end_s", 0.0)))
             for s in job.tighten.get("kept_spans", []) or []
             if isinstance(s, dict)]
    out: list[tuple[int, int]] = []
    for word_start, word_end in ranges:
        mids = [(job.words[j].start_s + job.words[j].end_s) / 2
                for j in range(word_start, word_end)]
        if any(all(start <= mid <= end for mid in mids)
               for start, end in spans):
            out.append((word_start, word_end))
    return out


TAKE_ALL_RE = re.compile(r"\b(both|all|every)\b", re.IGNORECASE)

_CUT_START_RE = re.compile(
    r"\b(?:very start|at the start|from the start|"
    r"start of the (?:cut|video|film)|the opening|opening|opening shot|"
    r"first shot|first line|open on|opens on|(?:at )?the beginning)\b")
_CUT_END_RE = re.compile(
    r"\b(?:at the (?:very )?end|very end|the last shot|last shot|"
    r"the final shot|final shot|closing|end of the (?:cut|video|film)|"
    r"the end\b(?! of\b))")


def match_cut_edge(text: str) -> str | None:
    """A "where in the cut" phrase to "start", "end" or None. Pure.

    Bare "the end" anchors; "the end of <that bit>" does not — it is
    relative to something else, not to the cut.
    """
    lowered = (text or "").lower()
    if _CUT_START_RE.search(lowered):
        return "start"
    if _CUT_END_RE.search(lowered):
        return "end"
    return None


def kept_words(job: JobContext) -> list:
    """Job words inside kept spans (all words when untightened). Pure."""
    if job.tighten is None:
        return list(job.words)
    spans = [(float(s.get("start_s", 0.0)), float(s.get("end_s", 0.0)))
             for s in job.tighten.get("kept_spans", []) or []
             if isinstance(s, dict)]
    return [w for w in job.words
            if any(start <= w.start_s <= end for start, end in spans)]


def _beat_in_window(job: JobContext, at_s: float) -> int | None:
    """The beat at the playhead, else the first overlapping
    [at_s - 1.0, at_s + ANCHOR_WINDOW_S]. Pure."""
    if job.tighten is not None:
        beat = cutlist.beat_at_output(job.tighten, at_s)
        if beat is not None:
            return beat
        lo = cutlist.output_to_source(job.tighten,
                                      max(0.0, at_s - 1.0))
        hi = cutlist.output_to_source(job.tighten, at_s + ANCHOR_WINDOW_S)
        for bid in sorted({b.get("id") for b in
                           job.tighten.get("beats", []) or []
                           if isinstance(b, dict)}):
            if bid is None:
                continue
            beat = next((b for b in job.tighten.get("beats", [])
                         if isinstance(b, dict) and b.get("id") == bid),
                        None)
            if beat is None:
                continue
            for attempt in beat.get("attempts", []) or []:
                if not isinstance(attempt, dict):
                    continue
                try:
                    start, end = float(attempt.get("start_s", 0.0)), float(
                        attempt.get("end_s", 0.0))
                except (TypeError, ValueError):
                    continue
                if start <= hi and end >= lo:
                    return bid
        return None
    shots = [s for s in job.angles.get("shots", []) if isinstance(s, dict)]
    for shot in shots:
        try:
            start, end = float(shot.get("start_s", 0.0)), float(
                shot.get("end_s", 0.0))
        except (TypeError, ValueError):
            continue
        if start <= at_s <= end:
            return -1
    lo, hi = at_s - 1.0, at_s + ANCHOR_WINDOW_S
    for shot in shots:
        try:
            start, end = float(shot.get("start_s", 0.0)), float(
                shot.get("end_s", 0.0))
        except (TypeError, ValueError):
            continue
        if start <= hi and end >= lo:
            return -1
    return None


def shot_at_output(job: JobContext, at_s: float) -> dict | None:
    """The shot containing the output time (window fallback). Pure."""
    shots = [s for s in job.angles.get("shots", []) if isinstance(s, dict)]

    def _span(shot: dict) -> tuple[float, float] | None:
        try:
            return (float(shot.get("start_s", 0.0)),
                    float(shot.get("end_s", 0.0)))
        except (TypeError, ValueError):
            return None

    for shot in shots:
        span = _span(shot)
        if span is not None and span[0] <= at_s <= span[1]:
            return shot
    lo, hi = at_s - 1.0, at_s + ANCHOR_WINDOW_S
    for shot in shots:
        span = _span(shot)
        if span is not None and span[0] <= hi and span[1] >= lo:
            return shot
    return None


def _shot_phrases(job: JobContext) -> dict[int, list[str]]:
    """Shot index → matchable phrases (labels, makes, models, sizes)."""
    by_label: dict[str, list[str]] = {}
    for item in job.sync.get("clips", []) or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "")
        cam = item.get("camera") or {}
        phrases = [label.lower()] if label else []
        for key in ("make", "model"):
            value = str(cam.get(key) or "").lower()
            if value:
                phrases.append(value)
        if label:
            by_label[label] = phrases
    out: dict[int, list[str]] = {}
    for pos, shot in enumerate(job.angles.get("shots", []) or []):
        if not isinstance(shot, dict):
            continue
        phrases = list(by_label.get(str(shot.get("camera") or ""), []))
        size = str(shot.get("shot_size") or "").lower()
        if size:
            phrases.append(size)
        phrases.append(str(shot.get("camera") or "").lower())
        out[pos] = [p for p in phrases if p]
    return out


def match_camera_reference(job: JobContext, text: str, *,
                           after_s: float = 0.0,
                           near_s: float | None = None
                           ) -> tuple[dict | None, bool]:
    """(shot, advanced) for a camera/shot phrase. Pure.

    The first shot whose label, make, model or size is named, after the
    cursor (wrapping once); near another anchor, the nearest match wins.
    """
    lowered = (text or "").lower()
    phrases = _shot_phrases(job)
    shots = [s for s in job.angles.get("shots", []) or []
             if isinstance(s, dict)]
    hits: list[int] = []
    for pos, shot in enumerate(shots):
        for phrase in phrases.get(pos, []):
            if len(phrase) == 1:
                if re.search(rf"\bcamera {re.escape(phrase)}\b", lowered):
                    hits.append(pos)
                    break
            elif phrase in lowered:
                hits.append(pos)
                break
    if not hits:
        return None, False
    if near_s is not None:
        def _dist(pos: int) -> float:
            try:
                mid = (float(shots[pos].get("start_s", 0.0))
                       + float(shots[pos].get("end_s", 0.0))) / 2
            except (TypeError, ValueError):
                return float("inf")
            return abs(mid - near_s)

        return shots[min(hits, key=_dist)], False

    def _start(pos: int) -> float:
        try:
            return float(shots[pos].get("start_s", 0.0))
        except (TypeError, ValueError):
            return 0.0

    ordered = sorted(hits, key=_start)
    for pos in ordered:
        if _start(pos) >= after_s:
            return shots[pos], after_s > 0
    return shots[ordered[0]], after_s > 0


# ------------------------------------------------- the taste file (§7)


def load_taste_with_warnings(job_dir: Path
                             ) -> tuple[dict, list[str]]:
    """(taste payload, warnings); missing or corrupt reads as empty."""
    path = Path(job_dir) / "taste.json"
    empty = {"version": 1, "entries": []}
    if not path.exists():
        return {"version": 1, "entries": []}, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "entries": []}, [TASTE_CORRUPT_WARNING]
    if (not isinstance(data, dict)
            or not isinstance(data.get("entries"), list)):
        return {"version": 1, "entries": []}, [TASTE_CORRUPT_WARNING]
    entries = [e for e in data["entries"] if isinstance(e, dict)]
    return {"version": 1, "entries": entries}, []


def load_taste(job_dir: Path) -> dict:
    """The taste payload; missing or corrupt reads as empty, never errors."""
    payload, _warnings = load_taste_with_warnings(job_dir)
    return payload


def taste_entries(job_dir: Path) -> list[str]:
    """Entry texts for prompts."""
    return [str(e.get("text", "")) for e in
            load_taste(job_dir).get("entries", [])]


def add_taste(job_dir: Path, text: str, *, source: str,
              note_id: str | None = None) -> dict:
    """Append a standing preference; ids t<n>, never reused."""
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        raise VoiceError("voice: empty taste entry")
    payload, _warnings = load_taste_with_warnings(job_dir)
    entries = payload["entries"]
    for entry in entries:
        if " ".join(str(entry.get("text", "")).split()).casefold() == cleaned.casefold():
            return payload
    if len(entries) >= TASTE_MAX_ENTRIES:
        raise VoiceError(
            "voice: the taste file already has 50 entries; remove one in "
            "the app before adding more")
    taken = 0
    for entry in entries:
        raw = str(entry.get("id", ""))
        if raw.startswith("t") and raw[1:].isdigit():
            taken = max(taken, int(raw[1:]))
    entries.append({
        "id": f"t{taken + 1}", "text": cleaned, "source": source,
        "note_id": note_id,
        "at": datetime.now().isoformat(timespec="seconds")})
    write_json(Path(job_dir) / "taste.json", payload)
    return payload


def remove_taste(job_dir: Path, entry_id: str) -> dict:
    """Remove one taste entry by id."""
    payload, _warnings = load_taste_with_warnings(job_dir)
    entries = payload["entries"]
    kept = [e for e in entries if e.get("id") != entry_id]
    if len(kept) == len(entries):
        raise VoiceError(f"voice: no taste entry {entry_id}")
    payload["entries"] = kept
    write_json(Path(job_dir) / "taste.json", payload)
    return payload


# ------------------------------------------------- split and resolve (§6)


def group_turns(turns: list[Turn]) -> list[list[Turn]]:
    """Turns to spoken notes: split at pauses over NOTE_SPLIT_PAUSE_S.

    A live recording is always exactly one note — the caller passes it
    through ungrouped. Pure.
    """
    groups: list[list[Turn]] = []
    for turn in turns:
        if (groups and turn.start_s - groups[-1][-1].end_s
                > NOTE_SPLIT_PAUSE_S):
            groups.append([])
        if not groups:
            groups.append([])
        groups[-1].append(turn)
    return groups


def _beat_output_range(job: JobContext, bid: int
                       ) -> tuple[float, float] | None:
    """Output range of a beat's kept spans; None when it keeps nothing."""
    if job.tighten is None:
        return None
    outs: list[float] = []
    for span in job.tighten.get("kept_spans", []) or []:
        if not isinstance(span, dict) or span.get("beat_id") != bid:
            continue
        for edge in (float(span.get("start_s", 0.0)),
                     float(span.get("end_s", 0.0))):
            mapped = cutlist.source_to_output(job.tighten, edge)
            if mapped is not None:
                outs.append(mapped)
    if not outs:
        return None
    return min(outs), max(outs)


def _transcript_lines(job: JobContext) -> list[str]:
    """`[out <start>-<end>] <text>` over kept words on output time."""
    kept = kept_words(job)
    if not kept:
        return []
    lines = angles_mod.split_lines(kept)
    out: list[str] = []
    for line in lines:
        words = kept[line.word_start:line.word_end] if (
            0 <= line.word_start <= line.word_end <= len(kept)) else []
        if job.tighten is None:
            start, end = line.start_s, line.end_s
        else:
            mapped = [cutlist.source_to_output(job.tighten, w.start_s)
                      for w in words]
            mapped = [m for m in mapped if m is not None]
            if not mapped:
                continue
            start = min(mapped)
            end = max(mapped)
            if words:
                end = max(end, start + (words[-1].end_s
                                       - words[-1].start_s))
        text = " ".join(w.text for w in words) if words else line.text
        out.append(f"[out {start:.1f}-{end:.1f}] {text}")
    return out


def resolve_prompt(job: JobContext, note_turns: list[list[Turn]],
                   anchors: list[float | None]) -> str:
    """The user prompt: CUT block, PREFERENCES block, NOTES block."""
    lines = [f"CUT (finished cut, {output_duration(job):.1f}s, "
             f"times below are output seconds):"]
    if job.tighten is not None:
        for beat in job.tighten.get("beats", []) or []:
            if not isinstance(beat, dict):
                continue
            bid = beat.get("id", -1)
            attempts = [a for a in beat.get("attempts", []) or []
                        if isinstance(a, dict)]
            pick = beat.get("pick_attempt", -1)
            if isinstance(pick, int) and 0 <= pick < len(attempts):
                chosen = attempts[pick]
                pick_text = (f"A{chosen.get('id')} "
                             f"({chosen.get('reason', '')})")
            else:
                pick_text = "none"
            alternates = " ".join(
                f"A{a.get('id')}" for a in attempts
                if a.get("verdict") == "alternate") or "none"
            removed = ", ".join(
                sorted({str(a.get("kind", "")) for a in attempts
                        if a.get("verdict") == "remove"})) or "none"
            if beat.get("status") != "kept":
                lines.append(
                    f"B{bid} \"{beat.get('topic', '')}\" "
                    f"{beat.get('status')} pick {pick_text} alternates: "
                    f"{alternates} removed: {removed}")
                continue
            span = _beat_output_range(job, bid)
            where = (f"out {span[0]:.1f}-{span[1]:.1f}s"
                     if span else "out -")
            lines.append(
                f"B{bid} \"{beat.get('topic', '')}\" {where} pick "
                f"{pick_text} alternates: {alternates} removed: {removed}")
    else:
        lines.append("(not tightened: no takes, angles only)")
    clip_hints: dict[str, str] = {}
    for item in job.sync.get("clips", []) or []:
        if not isinstance(item, dict) or not item.get("label"):
            continue
        cam = item.get("camera") or {}
        parts = [str(cam.get("make") or ""), str(cam.get("model") or "")]
        stem = Path(str(cam.get("path") or "")).name
        if stem:
            parts.append(stem)
        hint = " ".join(p for p in parts if p)
        if hint:
            clip_hints[str(item["label"])] = hint
    for shot in job.angles.get("shots", []) or []:
        if not isinstance(shot, dict):
            continue
        try:
            start, end = float(shot.get("start_s", 0.0)), float(
                shot.get("end_s", 0.0))
        except (TypeError, ValueError):
            continue
        beat = shot.get("beat_id", -1)
        beat_text = f" beat B{beat}" if isinstance(beat, int) and beat >= 0 else ""
        size = str(shot.get("shot_size", ""))
        hint = clip_hints.get(str(shot.get("camera") or ""), "")
        if hint:
            size = f"{size}; {hint}" if size else hint
        lines.append(
            f"shot {shot.get('id')} out {start:.1f}-{end:.1f}s camera "
            f"{shot.get('camera')} ({size})"
            f"{beat_text}")
    lines.append("TRANSCRIPT:")
    lines.extend(_transcript_lines(job) or ["(no words kept)"])
    taste = taste_entries(job.job_dir)
    lines.append("PREFERENCES:")
    lines.extend(f"- {entry}" for entry in taste) or lines.append("none")
    lines.append("NOTES:")
    for num, group in enumerate(note_turns, 1):
        raw = " ".join(t.text for t in group)
        anchor: float | None = (anchors[num - 1]
                                if num - 1 < len(anchors) else None)
        if anchor is not None:
            detail = f"playhead {anchor:.1f}s"
        else:
            spoken = parse_spoken_timecode(raw)
            detail = (f"spoken timecode {spoken:.1f}s"
                      if spoken is not None else "none")
        lines.append(f"N{num} (anchor: {detail}): \"{raw}\"")
    return "\n".join(lines)


# ------------------------------------------------- verification (§6.4)


def op_label(op: dict) -> str:
    """A readable label in the `app/muse.validate` shape. Pure."""
    name = op.get("op", "")
    bid = op.get("beat_id", -1)
    tag = f"B{bid}" if isinstance(bid, int) and bid >= 0 else "B?"
    if name == "swap_take":
        return f"Swap {tag} → A{op.get('attempt_id', -1)}"
    if name == "drop_beat":
        return f"Drop beat {tag}"
    if name == "restore_beat":
        return f"Restore beat {tag}"
    if name == "drop_span":
        return f"Drop span {op.get('span_id', '')}"
    if name == "restore_span":
        return f"Restore span {op.get('span_id', '')}"
    if name in ("drop_words", "restore_words"):
        verb = "Drop" if name == "drop_words" else "Restore"
        label = (f"{verb} words {op.get('word_start', -1)}–"
                 f"{op.get('word_end', -1)}")
        text = str(op.get("text", "")).strip()
        if text:
            label += f" (“{text[:60]}”)"
        return label
    if name == "set_angle":
        return f"Camera {op.get('camera', '')} for beat {tag}"
    if name == "clear_angle":
        return f"Clear the angle override on beat {tag}"
    if name == "set_pause_limit":
        try:
            value = float(op.get("pause_limit_s", 0.0))
        except (TypeError, ValueError):
            value = 0.0
        return f"Pauses over {value:g}s shortened"
    if name == "add_note":
        return f"Note: {str(op.get('text', ''))[:60]}"
    if name == "add_taste":
        return f"Remember: {str(op.get('text', ''))[:60]}"
    if name == "retime_target":
        try:
            target = float(op.get("target_s", 0.0))
        except (TypeError, ValueError):
            target = 0.0
        return f"Make it under {int(target // 60)}:{int(target % 60):02d}"
    return str(name or "unknown operation")


def _blocked(text: str) -> dict:
    return {"ok": False, "text": text, "hard": True}


def _passed(text: str) -> dict:
    return {"ok": True, "text": text, "hard": False}


def _mentions_mix(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in MIX_WORDS)


def _mentions_visual(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in VISUAL_WORDS)


def _shot_cameras(job: JobContext) -> set[str]:
    return {str(s.get("camera", "")) for s in
            job.angles.get("shots", []) or [] if isinstance(s, dict)}


def _coerced_add_note(text: str, at_s: float | None, message: str,
                      why: str = "") -> dict:
    return {
        "op": "add_note", "beat_id": -1, "attempt_id": -1, "span_id": "",
        "camera": "", "pause_limit_s": -1, "target_s": -1,
        "at_s": round(at_s, 3) if at_s is not None else 0.0,
        "text": text, "why": why or message,
        "blocked": False, "checks": [_passed(message)],
        "label": f"Note: {text[:60]}",
    }


def _validate_untightened(job: JobContext, op: dict, at_s: float | None
                          ) -> list[dict]:
    """Checks for one op against a job with no tighten.json. Pure."""
    name = op.get("op", "")
    if name in ("swap_take", "drop_beat", "restore_beat", "drop_span",
                "restore_span", "drop_words", "restore_words",
                "clear_angle", "retime_target"):
        return [_blocked(NOT_TIGHTENED_MESSAGE)]
    if name == "set_pause_limit":
        value = op.get("pause_limit_s", -1)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not 0.30 <= float(value) <= 3.00):
            return [_blocked(
                f"Pause limit {value}s is outside 0.30–3.00s")]
        return [_blocked(
            "This cut was not tightened, so pauses were never trimmed. "
            "Re-run director cut without --no-tighten.")]
    if name == "set_angle":
        camera = op.get("camera", "")
        if not camera:
            return [_blocked("set_angle needs a camera")]
        if camera not in _shot_cameras(job):
            return [_blocked(f"No camera {camera} in this cut")]
        shot = shot_at_output(job, at_s) if at_s is not None else None
        if shot is None:
            return [_blocked(UNPLACED_MESSAGE)]
        return [_passed(f"Set the shot at {at_s:.1f}s to camera {camera}")]
    if name == "add_note":
        return [_passed("Add an editor note")]
    if name == "add_taste":
        if not str(op.get("text", "")).strip():
            return [_blocked("Empty preference")]
        return [_passed(f"Remember: {op.get('text', '')[:60]}")]
    return [_blocked(f"Unknown operation '{name}'")]


def _validate_voice_op(job: JobContext, op: dict) -> list[dict]:
    """Checks for the two voice-only ops on a tightened job.

    Reads the taste file for the add_taste capacity check.
    """
    name = op.get("op", "")
    if name == "add_taste":
        if not str(op.get("text", "")).strip():
            return [_blocked("Empty preference")]
        if len(load_taste(job.job_dir).get("entries", [])) >= TASTE_MAX_ENTRIES:
            return [_blocked(
                "voice: the taste file already has 50 entries; remove one "
                "in the app before adding more")]
        return [_passed(f"Remember: {op.get('text', '')[:60]}")]
    if name == "retime_target":
        try:
            target = float(op.get("target_s", -1))
        except (TypeError, ValueError):
            target = -1.0
        if target <= 0:
            return [_blocked("Target length needs a number of seconds")]
        return [_passed(
            f"Retarget the cut to {target:.0f}s (whole beats only)")]
    return [_blocked(f"Unknown operation '{name}'")]


def _matches_to_candidates(job: JobContext, matches: list[int]) -> list[dict]:
    """Quote word indices to ambiguous candidates. Pure."""
    words = kept_words(job)
    out: list[dict] = []
    for start in matches:
        if not 0 <= start < len(words):
            continue
        word = words[start]
        if job.tighten is not None:
            at = cutlist.source_to_output(job.tighten, word.start_s)
            bid = cutlist.beat_at_source(job.tighten, word.start_s)
        else:
            at, bid = word.start_s, -1
        if at is None:
            continue
        snippet = " ".join(w.text for w in words[start:start + 8])
        out.append({"beat_id": bid if bid is not None else -1,
                    "at_s": round(at, 3),
                    "label": f"{at:.1f}s — {snippet}"})
    out.sort(key=lambda c: c["at_s"])
    return out


def _fmt_clock(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    return f"{total // 60}:{total % 60:02d}"


def _range_candidates(job: JobContext,
                      ranges: list[tuple[int, int]]) -> list[dict]:
    """Word ranges to ambiguous candidates (carrying their ranges). Pure."""
    out: list[dict] = []
    for word_start, word_end in ranges:
        if not 0 <= word_start < word_end <= len(job.words):
            continue
        word = job.words[word_start]
        if job.tighten is not None:
            at = cutlist.source_to_output(job.tighten, word.start_s)
            bid = cutlist.beat_at_source(job.tighten, word.start_s)
        else:
            at, bid = word.start_s, -1
        if at is None:
            continue
        snippet = " ".join(w.text for w in
                           job.words[word_start:word_start + 8])
        out.append({"beat_id": bid if bid is not None else -1,
                    "at_s": round(at, 3),
                    "label": f"{at:.1f}s — {snippet}",
                    "word_start": word_start, "word_end": word_end})
    out.sort(key=lambda c: c["at_s"])
    return out


def _fill_word_op(job: JobContext, op: dict,
                  found: tuple[int, int]) -> dict:
    """A drop_words op with its resolved range (and phrase). Pure."""
    op = dict(op)
    op["word_start"], op["word_end"] = found
    if not str(op.get("text", "")).strip():
        op["text"] = " ".join(
            w.text for w in job.words[found[0]:found[1]])
    return op


def _resolve_word_op_ranges(job: JobContext, *, raw_text: str, heard: str,
                            quote: str, ops: list[dict]
                            ) -> tuple[list[dict], list[dict], str,
                                       tuple[float, int | None] | None]:
    """Quote to word ranges for drop_words ops. Pure.

    Returns (ops, word_candidates, message, anchor). Spark cannot know
    span ids or word numbers, so it sends the quoted words and Director
    finds them: one match fills the op; "both"/"all"/"every" expand one
    op per match; several matches without them ask. A drop_span whose
    span id names nothing but whose quote lands inside kept lines is a
    phrase cut mislabelled, and is converted. The anchor is (output
    time, beat) of the first resolved range, for the note's own anchor.
    """
    if job.tighten is None:
        return list(ops), [], "", None
    cleaned = quote.strip()
    ranges = match_kept_ranges(job, cleaned) if cleaned else []
    take_all = bool(TAKE_ALL_RE.search(f"{raw_text} {heard}"))
    kept_ids = {str(s.get("id")) for s in
                job.tighten.get("kept_spans", []) or []
                if isinstance(s, dict)}
    out: list[dict] = []
    word_candidates: list[dict] = []
    notes: list[str] = []
    anchor: tuple[float, int | None] | None = None
    for op in ops:
        name = op.get("op", "")
        if (name == "drop_span" and ranges
                and str(op.get("span_id", "")) not in kept_ids):
            op = {**op, "op": "drop_words", "span_id": "",
                  "word_start": -1, "word_end": -1}
            name = "drop_words"
            notes.append(f"Matched “{cleaned}” inside kept lines — "
                         f"dropping the words")
        if name != "drop_words":
            out.append(op)
            continue
        word_start, word_end = op.get("word_start", -1), op.get("word_end", -1)
        if (word_start != -1 and word_end != -1):
            # Indices already filled (the app re-sends them); the checks
            # decide, here or in cutlist.
            out.append(op)
            continue
        if not ranges:
            if cleaned:
                text = f"No words match “{cleaned}” in this cut"
                label = f"Drop “{cleaned[:60]}”"
            else:
                text = "I couldn't tell which words to cut"
                label = "Drop words"
            out.append({**op, "blocked": True, "checks": [_blocked(text)],
                        "label": label})
            continue
        if len(ranges) > 1 and not take_all:
            for cand in _range_candidates(job, ranges):
                key = (cand["word_start"], cand["word_end"])
                if key not in {(c["word_start"], c["word_end"])
                               for c in word_candidates}:
                    word_candidates.append(cand)
            word_candidates.sort(key=lambda c: c["at_s"])
            out.append({**op, "blocked": True,
                        "checks": [_blocked(
                            f"Ambiguous — pick one of "
                            f"{len(word_candidates)} moments")],
                        "label": f"Drop “{cleaned[:60]}”"})
            continue
        for found in ranges:
            out.append(_fill_word_op(job, op, found))
        if anchor is None:
            # The note's own anchor: the first range's output time, and
            # its beat when exactly one range resolved (several ranges
            # place no single beat).
            first = _range_candidates(job, ranges[:1])
            if first:
                bid = first[0]["beat_id"]
                anchor = (first[0]["at_s"],
                          bid if len(ranges) == 1 and bid >= 0 else None)
    return out, word_candidates, " ".join(notes), anchor


def verify_resolved(job: JobContext, notes: list[dict],
                    *, source: str = "file") -> list[ResolvedNote]:
    """Muse proposes, these decide: anchors, checks, retargets. Pure
    except for the taste-file read behind `add_taste` validation.

    Each input note carries the Spark reply fields (`note_index`,
    `heard`, `confidence`, `anchor_kind`, `at_s`, `quote`, `beat_id`,
    `status`, `message`, `ops`, `taste`) plus `raw_text` (the note's
    verbatim transcript) and `playhead_at` (float or None).
    """
    resolved: list[ResolvedNote] = []
    cursor = 0.0
    for position, entry in enumerate(notes):
        raw = str(entry.get("raw_text", ""))
        heard = str(entry.get("heard", ""))
        try:
            confidence = float(entry.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = min(1.0, max(0.0, confidence))
        playhead = entry.get("playhead_at")
        if isinstance(playhead, bool) or not isinstance(
                playhead, (int, float)):
            playhead = None
        ops_in = [dict(op) for op in entry.get("ops", []) or []]
        taste_in = [str(t) for t in entry.get("taste", []) or []]
        message = str(entry.get("message", ""))
        spark_beat = entry.get("beat_id", -1)
        if not isinstance(spark_beat, int):
            try:
                spark_beat = int(spark_beat)
            except (TypeError, ValueError):
                spark_beat = -1

        def _finish(status: str, kind: str, at: float | None, bid: int,
                    ops: list[dict], candidates: list[dict],
                    text: str) -> ResolvedNote:
            return ResolvedNote(
                id=f"n{position + 1}", source=source, raw_text=raw,
                heard=heard, anchor_kind=kind, at_s=at, beat_id=bid,
                status=status, message=text, ops=ops,
                candidates=candidates,
                engine=str(entry.get("engine", "")),
                usd=float(entry.get("usd", 0.0) or 0.0))

        # Step 0 (§5): a note about the mix/colour with nothing doable
        # becomes a plain note, never a guessed edit.
        substantive = [op for op in ops_in if op.get("op") != "add_note"]
        if _mentions_mix(f"{raw} {heard} {message}") and not substantive:
            at = playhead
            ops = [_coerced_add_note(heard or raw, at, MIX_MESSAGE)]
            resolved.append(_finish(
                "resolved", "playhead" if at is not None else "global",
                at, -1, ops, [], MIX_MESSAGE))
            if at is not None:
                cursor = at
            continue

        # Word ranges before anchors: Spark sends quoted words, never
        # word numbers, so the quote resolves to ranges here.
        quote_text = str(entry.get("quote", ""))
        (ops_in, word_candidates, word_note,
         word_anchor) = _resolve_word_op_ranges(
            job, raw_text=raw, heard=heard, quote=quote_text, ops=ops_in)
        if word_note:
            message = f"{message} {word_note}".strip() if message \
                else word_note
        word_ambiguous = bool(word_candidates)

        needs_beat = any(op.get("op") in BEAT_OPS for op in ops_in)
        needs_place = needs_beat or any(op.get("op") == "add_note"
                                        for op in ops_in)

        # The anchor priority (§6.3). The playhead is never second-guessed.
        kind = "none"
        at: float | None = None
        bid: int | None = None
        candidates: list[dict] = []
        past_end_note = ""
        if playhead is not None:
            dur = output_duration(job)
            if 0 <= playhead <= dur + 0.5:
                kind, at = "playhead", float(playhead)
                found = _beat_in_window(job, float(playhead))
                bid = found if found is not None else -1
            elif needs_place:
                past_end_note = (
                    f"You said {_fmt_clock(playhead)}, the cut is "
                    f"{_fmt_clock(dur)} long")
        spoken_tc: float | None = None
        if kind == "none" and needs_place:
            spoken_tc = parse_spoken_timecode(raw)
            if spoken_tc is not None:
                dur = output_duration(job)
                if 0 <= spoken_tc <= dur:
                    kind, at = "spoken_timecode", spoken_tc
                    found = _beat_in_window(job, spoken_tc)
                    bid = found if found is not None else -1
                else:
                    past_end_note = (
                        f"You said {_fmt_clock(spoken_tc)}, the cut is "
                        f"{_fmt_clock(dur)} long")
                    spoken_tc = None
        if kind == "none" and needs_place:
            quote = str(entry.get("quote", ""))
            if quote.strip():
                matches = match_quote(kept_words(job), quote)
                if len(matches) == 1:
                    words = kept_words(job)
                    word = words[matches[0]]
                    if job.tighten is not None:
                        placed = cutlist.source_to_output(
                            job.tighten, word.start_s)
                        placed_bid = cutlist.beat_at_source(
                            job.tighten, word.start_s)
                    else:
                        placed, placed_bid = word.start_s, -1
                    if placed is not None:
                        kind, at = "quote", placed
                        bid = placed_bid if placed_bid is not None else -1
                elif len(matches) > 1:
                    candidates = _matches_to_candidates(job, matches)
        if kind == "none" and needs_place and not candidates:
            edge = match_cut_edge(f"{raw} {heard}")
            if edge is not None:
                at_edge = 0.0 if edge == "start" else output_duration(job)
                kind, at = "beat", at_edge
                found = _beat_in_window(job, at_edge)
                bid = found if found is not None else -1
        if kind == "none" and needs_place and not candidates:
            shot, advanced = match_camera_reference(
                job, f"{raw} {heard}",
                after_s=cursor if playhead is None and spoken_tc is None
                else 0.0,
                near_s=at)
            if shot is not None:
                try:
                    shot_at = float(shot.get("start_s", 0.0))
                except (TypeError, ValueError):
                    shot_at = 0.0
                kind, at = "beat", shot_at
                shot_bid = shot.get("beat_id", -1)
                if (job.tighten is not None and isinstance(shot_bid, int)
                        and shot_bid >= 0):
                    bid = shot_bid
                elif job.tighten is not None:
                    found = _beat_in_window(job, shot_at)
                    bid = found if found is not None else -1
                else:
                    bid = -1
                if advanced and not message:
                    message = ("Matched after the previous note "
                               "(advanced from the previous note)")
                elif advanced:
                    message = (message.rstrip()
                               + " (advanced from the previous note)")

        if word_candidates:
            candidates = sorted(word_candidates + candidates,
                                key=lambda c: c["at_s"])
        if kind == "none" and word_anchor is not None:
            kind, at = "quote", word_anchor[0]
            if bid is None and word_anchor[1] is not None:
                bid = word_anchor[1]

        # Confidence floor (§6.4 step 3): report, never guess.
        if confidence < CONFIDENCE_FLOOR:
            resolved.append(_finish("unresolved", kind, at,
                                    bid if bid is not None else -1,
                                    [], [], UNPLACED_MESSAGE))
            if at is not None:
                cursor = at
            continue

        # Ambiguity: candidates listed, apply waits for the editor's pick.
        if candidates and (needs_beat or word_ambiguous):
            ordered: list[dict] = []
            for op in ops_in:
                if op.get("op") in BEAT_OPS:
                    op = dict(op)
                    op["beat_id"] = -1
                    op["blocked"] = True
                    op["checks"] = [_blocked(
                        f"Ambiguous — pick one of {len(candidates)} "
                        "moments")]
                    op["label"] = op_label(op)
                    ordered.append(op)
                elif op.get("checks") is not None:
                    # The word-range step already decided this op.
                    ordered.append(op)
                else:
                    ordered.extend(_check_ops(job, [op], at))
            text = (message or
                    f"That could be {len(candidates)} moments — pick one")
            if past_end_note:
                text = f"{past_end_note}. {text}" if text else past_end_note
            resolved.append(_finish("ambiguous", "quote", at,
                                    bid if bid is not None else -1,
                                    ordered, candidates, text))
            continue

        # Agreement (§6.4 step 2): the mechanical anchor wins.
        beat_ids = {b.get("id") for b in
                    (job.tighten.get("beats", []) or [])
                    if isinstance(b, dict)} if job.tighten else set()
        spark_valid = spark_beat in beat_ids if job.tighten else False
        anchor_bid = bid
        if needs_beat and job.tighten is None:
            # Untightened: no beats exist; set_angle resolves against the
            # anchored shot instead and carries beat_id -1.
            if at is None:
                resolved.append(_finish(
                    "unresolved", kind, None, -1, [], [],
                    message or UNPLACED_MESSAGE))
                continue
            for op in ops_in:
                if op.get("op") in BEAT_OPS:
                    op["beat_id"] = -1
            anchor_bid = -1
            if kind == "none":
                kind = "beat"
        if needs_beat and job.tighten is not None:
            if anchor_bid is not None and anchor_bid >= 0:
                if (spark_beat != anchor_bid and isinstance(spark_beat, int)
                        and spark_beat >= 0):
                    note_text = (
                        f"Heard as beat B{spark_beat}; the words/timecode "
                        f"point to B{anchor_bid}, used B{anchor_bid}")
                    message = f"{message} {note_text}".strip() if message \
                        else note_text
                for op in ops_in:
                    if op.get("op") in BEAT_OPS:
                        op["beat_id"] = anchor_bid
                if kind == "none":
                    kind = "beat"
            elif spark_valid:
                anchor_bid = spark_beat
                if kind == "none":
                    kind = "beat"
            else:
                text = message or UNPLACED_MESSAGE
                if past_end_note and not message:
                    text = past_end_note
                resolved.append(_finish(
                    "unresolved", kind, at, -1, [], [], text))
                if at is not None:
                    cursor = at
                continue
        if kind == "none" and not needs_place and (
                ops_in or taste_in):
            kind = "global"
        if past_end_note and not message and kind != "none":
            message = past_end_note

        # Defaults and coercions before the checks run.
        final_ops: list[dict] = []
        for op in ops_in:
            op = {**op}
            name = op.get("op", "")
            if name == "set_pause_limit" and op.get(
                    "pause_limit_s", -1) in (None, -1, 0):
                op["pause_limit_s"] = 0.5
            if name == "add_note":
                op["at_s"] = round(at, 3) if at is not None else 0.0
                if not str(op.get("text", "")).strip():
                    op["text"] = heard or raw
            if name == "set_angle" and job.tighten is not None:
                camera = op.get("camera", "")
                if camera and camera not in _shot_cameras(job):
                    final_ops.append(_coerced_add_note(
                        heard or raw, at, NO_SHOT_MESSAGE,
                        why=str(op.get("why", ""))))
                    if not message:
                        message = NO_SHOT_MESSAGE
                    continue
            final_ops.append(op)
        # Taste is separated (§6.4 step 4): standing rules never ride
        # along inside a timeline op.
        for text in taste_in:
            if not text.strip():
                continue
            final_ops.append({
                "op": "add_taste", "beat_id": -1, "attempt_id": -1,
                "span_id": "", "camera": "", "pause_limit_s": -1,
                "target_s": -1, "text": text,
                "why": str(entry.get("message", "")),
            })

        checked = []
        for op in final_ops:
            if op.get("checks") is not None:
                # The word-range step already decided this op.
                checked.append(op)
            else:
                checked.extend(_check_ops(job, [op], at))
        # An op naming a vanished beat/attempt stays visible, blocked
        # with cutlist's text — it is never dropped from the list.
        if not checked and not taste_in:
            text = message or UNPLACED_MESSAGE
            resolved.append(_finish(
                "unresolved", kind, at,
                anchor_bid if anchor_bid is not None else -1,
                [], [], text))
            if at is not None:
                cursor = at
            continue
        timeline_ops = [op for op in checked
                        if op.get("op") != "add_taste"]
        if not timeline_ops and any(op.get("op") == "add_taste"
                                    for op in checked):
            status = "preference"
        else:
            status = "resolved"
        if kind == "none" and status == "resolved":
            kind = "global"
        resolved.append(_finish(
            status, kind, at,
            anchor_bid if anchor_bid is not None else -1,
            checked, [], message))
        if at is not None:
            cursor = at
    return resolved


def _check_ops(job: JobContext, ops: list[dict],
               at_s: float | None) -> list[dict]:
    """Every op with checks, blocked and a label. Reads the taste file
    only for the add_taste capacity check."""
    out: list[dict] = []
    for op in ops:
        op = dict(op)
        name = op.get("op", "")
        if job.tighten is None:
            checks = _validate_untightened(job, op, at_s)
        elif name in VOICE_ONLY_OPS:
            checks = _validate_voice_op(job, op)
        elif name in CUTLIST_OPS:
            checks = cutlist.validate_operation(
                job.tighten, op, covers=job.covers, words=job.words)
        else:
            checks = [_blocked(f"Unknown operation '{name}'")]
        op["blocked"] = any(not c["ok"] and c["hard"] for c in checks)
        op["checks"] = checks
        op["label"] = op_label(op)
        out.append(op)
    return out


def _assemble_entries(groups: list[list[Turn]], anchors: list[float | None],
                      reply_notes: list[dict]) -> list[dict]:
    """Spark reply entries joined to their note groups. Pure.

    `note_index` is 1-based (N<n>); a 0-based index is accepted when no
    1-based reading fits. Out-of-range entries are dropped.
    """
    raws = [" ".join(t.text for t in group) for group in groups]
    out: list[dict] = []
    for item in reply_notes:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("note_index", -1))
        except (TypeError, ValueError):
            continue
        pos: int | None = None
        if 1 <= index <= len(groups):
            pos = index - 1
        elif 0 <= index < len(groups):
            pos = index
        if pos is None:
            continue
        playhead = anchors[pos] if pos < len(anchors) else None
        out.append({
            "note_index": index,
            "heard": str(item.get("heard", "")),
            "confidence": item.get("confidence", 0.0),
            "anchor_kind": str(item.get("anchor_kind", "none")),
            "at_s": item.get("at_s", -1),
            "quote": str(item.get("quote", "")),
            "beat_id": item.get("beat_id", -1),
            "status": str(item.get("status", "unresolved")),
            "message": str(item.get("message", "")),
            "ops": [dict(op) for op in item.get("ops", []) or []],
            "taste": [str(t) for t in item.get("taste", []) or []],
            "raw_text": raws[pos],
            "playhead_at": playhead,
        })
    return out


# ------------------------------------------------- offline resolver (VOICE-OFFLINE)
#
# Built by Claude Code (Opus) after the Meta API credit ran out. When
# the Spark call fails for billing or network reasons (or guestlist is
# not installed), notes are read by the rules below instead. They emit
# reply notes in exactly the RESOLVE_SCHEMA shape, so `_assemble_entries`
# and `verify_resolved` do the real work: quoted-phrase matching,
# "both/all/every", word ranges, cut-edge and camera anchors, checks.
# Muse stays primary; this runs only when it cannot be reached.

OFFLINE_HELP_MESSAGE = (
    "I couldn't understand that note without Muse; try: cut where I say "
    "… / remove … / use camera B here / use the drone at the very start")
OFFLINE_WARNING = ("Muse unavailable ({reason}), resolved offline by rules "
                   "— check each change before accepting")
WHISPER_FALLBACK_WARNING = "Muse unavailable ({reason}), heard with local Whisper"
OFFLINE_CONFIDENCE = 0.8

_HTTP_CODE_RE = re.compile(r"\b(?:HTTP|status(?: code)?|Error code:?)\s*(\d{3})\b",
                           re.IGNORECASE)
_BILLING_RE = re.compile(
    r"billing|payment required|insufficient[_ ](?:quota|funds|credit)|"
    r"credit balance|out of credit|quota exceeded", re.IGNORECASE)
_NETWORK_NAMES = ("connect", "timeout", "timedout", "network", "dns",
                  "remoteprotocol", "readerror", "writeerror")


def muse_unavailable_reason(error: BaseException) -> str | None:
    """Why Muse cannot be reached ("402", "HTTP 503", "network", ...), or
    None when the failure is something else (a bad reply, a bug). Pure.

    Billing, auth, rate-limit, server and connection failures, a missing
    key, the spend hard stop and a missing guestlist all count: none of
    them gets better by asking again during a demo.
    """
    if isinstance(error, (ImportError, VoiceUnavailable)):
        return "no Meta client"
    if type(error).__name__ == "MuseUnavailable":
        return "no Meta client"
    text = f"{type(error).__name__}: {error}"
    code = getattr(error, "status_code", None)
    if not isinstance(code, int):
        response = getattr(error, "response", None)
        code = getattr(response, "status_code", None)
    if not isinstance(code, int):
        found = _HTTP_CODE_RE.search(text)
        code = int(found.group(1)) if found else None
    if code == 402 or _BILLING_RE.search(text):
        return "402"
    if isinstance(code, int) and (code in (401, 403, 408, 429)
                                  or code >= 500):
        return f"HTTP {code}"
    if type(error).__name__ == "BudgetExceeded":
        return "spend hard stop"
    if "MODEL_API_KEY" in text:
        return "no API key"
    name = type(error).__name__.lower()
    if isinstance(error, (ConnectionError, TimeoutError)) or any(
            part in name for part in _NETWORK_NAMES):
        return "network"
    if isinstance(error, OSError):
        return "network"
    return None


_ALIAS_BY_STEM = (
    (re.compile(r"^dji[_-]", re.IGNORECASE), ("drone", "dji")),
    (re.compile(r"^(?:c\d{4}|dsc\d+|a\d{3}_c\d+)", re.IGNORECASE),
     ("sony",)),
    (re.compile(r"^img[_-]?\d+", re.IGNORECASE), ("iphone", "phone")),
    (re.compile(r"^(?:gopr|gx\d+|gh\d+)", re.IGNORECASE), ("gopro",)),
)
_ALIAS_BY_MAKE = {
    "dji": ("drone", "dji"), "sony": ("sony",), "apple": ("iphone", "phone"),
    "gopro": ("gopro",), "canon": ("canon",), "blackmagic": ("blackmagic",),
}


def camera_aliases(job: JobContext) -> dict[str, list[str]]:
    """Camera label (only labels that appear in the cut's shots) to the
    phrases an editor uses for it: "camera B", make, model, and what the
    file name gives away (DJI_ -> drone, C0001 -> Sony, IMG_ -> iPhone).
    Pure."""
    used = _shot_cameras(job)
    out: dict[str, list[str]] = {}
    for item in job.sync.get("clips", []) or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "")
        if not label or label not in used:
            continue
        cam = item.get("camera") or {}
        names: list[str] = []
        for key in ("make", "model"):
            value = str(cam.get(key) or "").strip().lower()
            if value:
                names.append(value)
                names.extend(_ALIAS_BY_MAKE.get(value, ()))
        stem = Path(str(cam.get("path") or "")).name
        for pattern, aliases in _ALIAS_BY_STEM:
            if pattern.search(stem):
                names.extend(aliases)
        if len(label) > 1:
            names.append(label.lower())
        out[label] = list(dict.fromkeys(n for n in names if n))
    for label in used:
        out.setdefault(label, [label.lower()] if len(label) > 1 else [])
    return out


def _camera_in_text(job: JobContext, text: str) -> str | None:
    """The camera label a clause names ("camera B", "the drone", "the
    Sony"), or None. A single-letter label only counts as "camera B",
    "angle B", "cam B" or "B camera" — never the B in "B-roll". Pure."""
    lowered = (text or "").lower()
    for label, names in camera_aliases(job).items():
        if len(label) <= 2:
            letter = re.escape(label.lower())
            if re.search(rf"\b(?:camera|cam|angle|shot)\s+{letter}\b"
                         rf"|\b{letter}[\s-](?:camera|cam|angle)\b",
                         lowered):
                return label
        for name in names:
            if re.search(rf"\b{re.escape(name)}\b", lowered):
                return label
    return None


_SAY_RE = re.compile(
    r"\b(?:where|when|the (?:part|bit|line|places?|spots?|moments?|times?) "
    r"(?:where|when)|places? where|times? where)?\s*"
    r"(?:i|we|you|he|she)\s+(?:say|said|says|mention|mentioned|"
    r"talk about|talked about|talking about)\b\s*[:,]?\s*(.+)$",
    re.IGNORECASE)
_MENTION_RE = re.compile(
    r"\b(?:every|each|all|both)?\s*(?:mentions?|instances?|times?) of\s+"
    r"(.+)$", re.IGNORECASE)
_CUT_VERB_RE = re.compile(
    r"\b(?:cut|remove|drop|lose|delete|trim|take out|get rid of|kill)\b"
    r"\s*(?:out\s+)?(.*)$", re.IGNORECASE)
_ANGLE_VERB_RE = re.compile(
    r"\b(?:use|switch|go|cut to|change to|show|put|give me|open on|"
    r"start on|end on|stay on|camera|angle)\b", re.IGNORECASE)
_PAUSE_RE = re.compile(r"\bpauses?\b|\bgaps?\b|\bdead air\b", re.IGNORECASE)
_TASTE_RE = re.compile(r"^\s*(?:always|never|this client (?:hates|loves))\b",
                       re.IGNORECASE)
_QUOTED_RE = re.compile(
    r"[\"“”]([^\"“”]{2,})[\"“”]"
    r"|(?<![A-Za-z])['‘]([^'‘’]{2,})['’](?![A-Za-z])")
_CLAUSE_SPLIT_RE = re.compile(
    r"(?<=[.!?;])\s+|\s+(?:and then|and also|then|and)\s+"
    r"(?=(?:please\s+)?(?:cut|remove|drop|lose|delete|trim|take out|"
    r"get rid of|use|switch|go to|change to|tighten)\b)",
    re.IGNORECASE)
_LEAD_FILLER_RE = re.compile(
    r"^(?:(?:um|uh|so|okay|ok|please|and|the words?|the line|the phrase|"
    r"that|this|it says)\b[\s,:]*)+", re.IGNORECASE)
_TRAIL_FILLER_RE = re.compile(
    r"(?:[\s,]+(?:please|out of (?:the|this) (?:cut|video)|from (?:the|this) "
    r"(?:cut|video)|out|again|there|in the cut|thanks|thank you))+[.!?]*$",
    re.IGNORECASE)
_PART_RE = re.compile(
    r"^(?:the\s+)?(?:(?:part|bit|line|section|places?|spots?|moments?|"
    r"times?)\s+)(?:where|when)\s+", re.IGNORECASE)


def _clean_quote(text: str) -> str:
    quote = (text or "").strip().strip("\"“”‘’'").strip()
    quote = _LEAD_FILLER_RE.sub("", quote)
    quote = _TRAIL_FILLER_RE.sub("", quote)
    return quote.strip(" ,.:;!?\"“”‘’'")


def _spoken_phrase(text: str) -> str:
    """The words a clause quotes: typed quotes first, then "where I say
    X", "every mention of X", then whatever follows the cut verb. Pure."""
    quoted = [a or b for a, b in _QUOTED_RE.findall(text or "")]
    if quoted:
        return _clean_quote(max(quoted, key=len))
    found = _SAY_RE.search(text or "")
    if found:
        return _clean_quote(found.group(1))
    found = _MENTION_RE.search(text or "")
    if found:
        return _clean_quote(found.group(1))
    found = _CUT_VERB_RE.search(text or "")
    if found:
        rest = _PART_RE.sub("", found.group(1).strip())
        rest = re.sub(r"^(?:where\s+)?(?:i|we)\s+(?:say|said)\s*[:,]?\s*",
                      "", rest, flags=re.IGNORECASE)
        return _clean_quote(rest)
    return ""


def _best_quote(job: JobContext, phrase: str, *, unique: bool = False,
                min_cover: float = 0.0) -> str:
    """The phrase itself when it matches the cut's words, else the longest
    run of its words that does (a misheard edge or a stutter in the note
    should not lose the cut). `unique` prefers a run matching once (an
    anchor); otherwise any match. `min_cover` is the smallest share of the
    phrase's words a shorter run must keep: a cut asks for 0.6, so "my drone
    flies to Neptune" never shrinks to cutting "my drone". Returns "" when
    nothing matches. Pure."""
    tokens = [t for t in re.split(r"\s+", phrase.strip()) if t]
    if not tokens:
        return ""
    if match_kept_ranges(job, phrase):
        if not unique or len(match_kept_ranges(job, phrase)) == 1:
            return phrase
    fallback = ""
    for size in range(len(tokens) - 1, 0, -1):
        if size < min_cover * len(tokens):
            break
        for start in range(0, len(tokens) - size + 1):
            window = " ".join(tokens[start:start + size])
            ranges = match_kept_ranges(job, window)
            if not ranges:
                continue
            if not unique or len(ranges) == 1:
                return window
            fallback = fallback or window
    return fallback


def _offline_op(name: str, **over: object) -> dict:
    op: dict = {"op": name, "beat_id": -1, "attempt_id": -1, "span_id": "",
                "camera": "", "pause_limit_s": -1, "target_s": -1,
                "word_start": -1, "word_end": -1, "text": "",
                "why": "offline rules (Muse unavailable)"}
    op.update(over)
    return op


def _offline_note(index: int, *, heard: str, kind: str = "none",
                  quote: str = "", status: str = "resolved",
                  message: str = "", ops: list[dict] | None = None,
                  taste: list[str] | None = None,
                  confidence: float = OFFLINE_CONFIDENCE) -> dict:
    return {"note_index": index, "heard": heard, "confidence": confidence,
            "anchor_kind": kind, "at_s": -1, "quote": quote, "beat_id": -1,
            "status": status, "message": message, "ops": list(ops or []),
            "taste": list(taste or [])}


def _clauses(text: str) -> list[str]:
    """One note's commands. A clause with no command in it ("At the very
    start, where I say X.") is context for the command after it. Pure."""
    parts = [p.strip(" ,") for p in _CLAUSE_SPLIT_RE.split(text or "")
             if p and p.strip(" ,.")]
    out: list[str] = []
    context = ""
    for part in parts:
        is_command = bool(
            _CUT_VERB_RE.search(part) or _ANGLE_VERB_RE.search(part)
            or _PAUSE_RE.search(part) or _TASTE_RE.search(part))
        if not is_command:
            context = f"{context} {part}".strip()
            continue
        out.append(f"{context} {part}".strip() if context else part)
        context = ""
    if context:
        if out:
            out[-1] = f"{out[-1]} {context}"
        else:
            out.append(context)
    return out


def _offline_clause(job: JobContext, index: int, clause: str
                    ) -> dict | None:
    """One clause to one reply note, or None when no rule reads it."""
    if _TASTE_RE.search(clause):
        return _offline_note(index, heard=f"Remember: {clause}",
                             kind="global", status="preference",
                             taste=[clause.strip()])
    camera = _camera_in_text(job, clause)
    cut = _CUT_VERB_RE.search(clause)
    quoting = bool(_SAY_RE.search(clause) or _QUOTED_RE.search(clause)
                   or _MENTION_RE.search(clause))
    if _PAUSE_RE.search(clause) and not quoting and re.search(
            r"\b(?:tighten|shorten|too long|shorter|trim|cut)\b", clause,
            re.IGNORECASE):
        number = re.search(r"(\d+(?:\.\d+)?)\s*(?:s\b|sec|second)", clause)
        limit = float(number.group(1)) if number else 0.5
        op = _offline_op("set_pause_limit", pause_limit_s=limit)
        return _offline_note(index, heard=f"Shorten pauses over {limit:g}s",
                             kind="global", ops=[op])
    cut_to = re.search(r"\bcut (?:to|over to)\b", clause, re.IGNORECASE)
    if camera and (cut_to or not cut or _ANGLE_VERB_RE.search(
            clause[:cut.start()] if cut else clause)):
        where = ""
        said = _SAY_RE.search(clause)
        anchor = ""
        if said:
            phrase = _clean_quote(re.split(
                r"[.!?]|\b(?:use|switch|go to|cut to)\b", said.group(1),
                maxsplit=1, flags=re.IGNORECASE)[0])
            anchor = _best_quote(job, phrase, unique=True) if phrase else ""
            if anchor:
                where = f" where you say “{anchor}”"
        edge = match_cut_edge(clause)
        if not where and edge == "start":
            where = " at the very start"
        elif not where and edge == "end":
            where = " at the end"
        elif not where and re.search(r"\bhere\b", clause, re.IGNORECASE):
            where = " here"
        op = _offline_op("set_angle", camera=camera,
                         why=f"offline rules: “{clause[:80]}”")
        return _offline_note(
            index, heard=f"Use camera {camera}{where}",
            kind="quote" if anchor else "beat", quote=anchor, ops=[op])
    if cut and not cut_to:
        phrase = _spoken_phrase(clause)
        if not phrase:
            return None
        # "both/all/every" outside the quoted words ("all the things" is
        # a phrase, not a request for every match).
        take_all = bool(TAKE_ALL_RE.search(clause.replace(phrase, " ")))
        quote = _best_quote(job, phrase, min_cover=0.6) or phrase
        every = " (every mention)" if take_all else ""
        op = _offline_op("drop_words", why=f"offline rules: “{clause[:80]}”")
        return _offline_note(index, heard=f"Cut “{quote}”{every}",
                             kind="quote", quote=quote, ops=[op])
    return None


def offline_reply_notes(job: JobContext, groups: list[list[Turn]]
                        ) -> list[dict]:
    """Rule-based stand-in for the Spark reply: RESOLVE_SCHEMA `notes`.
    Pure. Every group yields at least one entry; what no rule reads comes
    back as a plain note with OFFLINE_HELP_MESSAGE (no ops), and a note
    about the mix or colour is left to verify_resolved's mix rule."""
    out: list[dict] = []
    for index, group in enumerate(groups, 1):
        raw = " ".join(t.text for t in group).strip()
        made = [note for note in (_offline_clause(job, index, clause)
                                  for clause in _clauses(raw))
                if note is not None]
        if made:
            out.extend(made)
        elif _mentions_mix(raw):
            out.append(_offline_note(index, heard=raw, kind="none"))
        else:
            out.append(_offline_note(index, heard=raw, kind="none",
                                     message=OFFLINE_HELP_MESSAGE))
    return out


def resolve_offline(*, turns: list[Turn], job: JobContext,
                    anchors: list[float | None], reason: str
                    ) -> tuple[list[ResolvedNote], float, list[str]]:
    """The offline twin of `resolve_notes`: same grouping, same
    verification, no API call, $0. Typed notes come in as one Turn."""
    if not turns:
        return [], 0.0, []
    groups = [turns] if len(anchors) == 1 else group_turns(turns)
    entries = _assemble_entries(groups, anchors,
                                offline_reply_notes(job, groups))
    notes = verify_resolved(job, entries)
    tag = f"Resolved offline by rules (Muse unavailable: {reason})"
    for note in notes:
        if note.message != OFFLINE_HELP_MESSAGE:
            note.message = (f"{note.message} · {tag}" if note.message
                            else tag)
    _, visual_warnings = confirm_visual_notes(job, notes,
                                              skip_reason=reason)
    return notes, 0.0, [OFFLINE_WARNING.format(reason=reason),
                        *visual_warnings]


def resolve_typed(job: JobContext, text: str, at_s: float | None = None,
                  *, reason: str = "offline") -> list[ResolvedNote]:
    """A typed note through the offline rules (one note, optional playhead).
    Pure apart from the taste-file read in verification."""
    turn = Turn(0.0, 1.0, str(text or "").strip())
    notes, _spend, _warnings = resolve_offline(
        turns=[turn] if turn.text else [], job=job, anchors=[at_s],
        reason=reason)
    return notes


def resolve_notes(*, turns: list[Turn], job: JobContext,
                  anchors: list[float | None], model: str
                  ) -> tuple[list[ResolvedNote], float, list[str]]:
    """One Spark text call -> (resolved notes, spend_usd, warnings).

    Pass `[at_s]` for a live recording (always exactly one note);
    pass `[]` for a file note (grouped mechanically at 1.5 s pauses).
    When guestlist cannot be imported, or the Spark call fails for
    billing, auth or network reasons (`muse_unavailable_reason`), the
    notes are resolved by the offline rules instead (`resolve_offline`,
    $0, warned); any other Spark failure raises VoiceError.
    """
    try:
        from guestlist.meta import cache, spark
    except ImportError:
        return resolve_offline(turns=turns, job=job, anchors=anchors,
                               reason="no Meta client")
    if not turns:
        return [], 0.0, []
    groups = [turns] if len(anchors) == 1 else group_turns(turns)
    prompt = resolve_prompt(job, groups, anchors)
    key = cache.key("voice", model, DEVELOPER, prompt, RESOLVE_SCHEMA,
                    "medium")
    hit = cache.get("spark", key)
    warnings: list[str] = []
    if hit:
        data = hit["data"]
        spend = 0.0
        warnings.append("resolve served from cache ($0)")
    else:
        try:
            cache.guard(0.25)
            result = spark._structured(
                key, [{"type": "input_text", "text": prompt}],
                schema=RESOLVE_SCHEMA, name="voice_resolve",
                developer=DEVELOPER, model=model, effort="medium",
                max_output_tokens=RESOLVE_MAX_OUTPUT_TOKENS,
                record={}, detail={"notes": len(groups)})
        except Exception as error:
            reason = muse_unavailable_reason(error)
            if reason is not None:
                return resolve_offline(turns=turns, job=job,
                                       anchors=anchors, reason=reason)
            raise VoiceError(
                "voice: Muse could not resolve the notes "
                f"({type(error).__name__}: {error}); the transcript is "
                "saved in voice.json — check the key and try again"
            ) from error
        data = result.data
        spend = round(float(result.usd or 0.0), 4)
    reply_notes = data.get("notes", []) if isinstance(data, dict) else []
    if not isinstance(reply_notes, list):
        reply_notes = []
    entries = _assemble_entries(groups, anchors, reply_notes)
    notes = verify_resolved(job, entries)
    visual_spend, visual_warnings = confirm_visual_notes(job, notes)
    warnings.extend(visual_warnings)
    return notes, round(spend + visual_spend, 4), warnings


# ------------------------------------------------- visual confirmation (§6.5)


def still_command(cut_path: Path, at_s: float, out: Path) -> list[str]:
    """One 384 px still from the finished cut. Pure."""
    return ["ffmpeg", "-v", "error", "-ss", f"{at_s:.3f}", "-i",
            str(cut_path), "-frames:v", "1", "-vf",
            f"scale={STILL_W}:-2", "-q:v", "3", "-y", str(out)]


def _camera_on_screen(job: JobContext, at_s: float) -> str:
    shot = shot_at_output(job, at_s)
    return str(shot.get("camera", "")) if shot else ""


def confirm_visual_notes(job: JobContext, notes: list[ResolvedNote],
                         *, skip_reason: str | None = None
                         ) -> tuple[float, list[str]]:
    """The one bounded image call per visual note. Mutates notes' ops.

    A note whose text points at the picture ("when she points at the
    kitchen") and whose anchor landed by quote or timecode gets its
    set_angle ops confirmed against stills from cut.mp4. Unconfirmed —
    or over the four-frame budget — the angle becomes a note. Without
    the Meta client the ops survive with a soft check; so they do when
    Muse is unavailable (`skip_reason`, or a billing/network failure on
    the first call), with one warning and no further calls.
    """
    targets = [note for note in notes
               if note.status == "resolved"
               and note.anchor_kind in ("quote", "spoken_timecode")
               and note.at_s is not None
               and any(op.get("op") == "set_angle" and not op.get("blocked")
                       for op in note.ops)
               and _mentions_visual(f"{note.raw_text} {note.heard}")]
    if not targets:
        return 0.0, []

    def _skip(reason: str, todo: list[ResolvedNote]) -> str:
        for note in todo:
            for op in note.ops:
                if op.get("op") == "set_angle" and not op.get("blocked"):
                    op["checks"] = list(op.get("checks", [])) + [{
                        "ok": True,
                        "text": (f"Frame check skipped (Muse unavailable: "
                                 f"{reason})"),
                        "hard": False}]
        return f"Muse unavailable ({reason}), frame check skipped"

    if skip_reason:
        return 0.0, [_skip(skip_reason, targets)]
    try:
        from guestlist.meta import cache, spark
    except ImportError:
        for note in targets:
            for op in note.ops:
                if op.get("op") == "set_angle" and not op.get("blocked"):
                    op["checks"] = list(op.get("checks", [])) + [{
                        "ok": True, "text": FRAME_UNAVAILABLE,
                        "hard": False}]
        return 0.0, []
    cut = job.job_dir / "cut.mp4"
    if not cut.exists():
        for note in targets:
            for op in note.ops:
                if op.get("op") == "set_angle" and not op.get("blocked"):
                    op["checks"] = list(op.get("checks", [])) + [{
                        "ok": True,
                        "text": ("Frame check unavailable (no finished "
                                 "cut to check against)"),
                        "hard": False}]
        return 0.0, []
    spend = 0.0
    warnings: list[str] = []
    used = 0
    stills_dir = job.job_dir / "voice" / "stills"
    for num, note in enumerate(targets):
        assert note.at_s is not None
        if used + VISUAL_STILLS > VISUAL_FRAME_BUDGET:
            _downgrade_visual(note)
            continue
        times = sorted({max(0.0, note.at_s - 2.0), note.at_s,
                        note.at_s + 2.0})
        stills_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        try:
            for pos, moment in enumerate(times):
                out = stills_dir / f"n{num}_{pos}.jpg"
                run_command(still_command(cut, moment, out))
                paths.append(out)
        except (ToolingError, OSError) as error:
            warnings.append(f"voice_stills_failed:{error}")
            _downgrade_visual(note)
            continue
        used += len(paths)
        detail = "; ".join(
            f"still {pos + 1} at {moment:.1f}s "
            f"(camera {_camera_on_screen(job, moment) or '?'} on screen)"
            for pos, moment in enumerate(times))
        prompt = (f"Note: \"{note.raw_text}\"\nStills from the finished "
                  f"cut, in order: {detail}.")
        try:
            cache.guard(0.25)
            result = spark.image_json(
                paths, schema=VISUAL_SCHEMA, name="voice_visual",
                developer=VISUAL_DEVELOPER, prompt=prompt,
                model=RESOLVE_MODEL, effort="medium",
                max_output_tokens=500)
        except Exception as error:
            reason = muse_unavailable_reason(error)
            if reason is not None:
                warnings.append(_skip(reason, targets[num:]))
                break
            warnings.append(
                f"voice_visual_failed:{type(error).__name__}")
            _downgrade_visual(note)
            continue
        spend += float(result.usd or 0.0)
        data = result.data if isinstance(result.data, dict) else {}
        if data.get("confirmed") is True and data.get("camera"):
            description = str(data.get("description", ""))
            for op in note.ops:
                if op.get("op") == "set_angle" and not op.get("blocked"):
                    op["checks"] = list(op.get("checks", [])) + [{
                        "ok": True, "text": f"Frame check: {description}",
                        "hard": False}]
        else:
            _downgrade_visual(note)
    return round(spend, 4), warnings


def _downgrade_visual(note: ResolvedNote) -> None:
    """A set_angle that cannot be confirmed becomes a note. Mutates."""
    at = note.at_s or 0.0
    message = (f"I couldn't see that moment at {at:.1f}s — noted "
               f"instead of changed")
    kept: list[dict] = []
    for op in note.ops:
        if op.get("op") == "set_angle" and not op.get("blocked"):
            kept.append(_coerced_add_note(
                note.heard or note.raw_text, note.at_s, message,
                why=str(op.get("why", ""))))
        else:
            kept.append(op)
    note.ops = kept
    note.message = f"{note.message} {message}".strip() if note.message \
        else message


# ------------------------------------------------- applying (§8)

_CUTLIST_FIELDS = ("op", "beat_id", "attempt_id", "span_id", "camera",
                   "pause_limit_s", "at_s", "word_start", "word_end",
                   "text", "why")


def _strip_for_cutlist(op: dict) -> dict:
    """A checked op to the fields `apply_operations` audits. Pure."""
    out = {key: op[key] for key in _CUTLIST_FIELDS if key in op}
    if out.get("op") not in ("drop_words", "restore_words"):
        out.pop("word_start", None)
        out.pop("word_end", None)
    return out


def _next_audit_id(payload: dict) -> str:
    taken = 0
    for entry in payload.get("edits_applied", []) or []:
        if not isinstance(entry, dict):
            continue
        raw = str(entry.get("id", ""))
        if raw.startswith("e") and raw[1:].isdigit():
            taken = max(taken, int(raw[1:]))
    return f"e{taken + 1}"


def _retime(payload: dict, words: list, target_s: float, why: str,
            source: str) -> tuple[str, list[int]]:
    """Whole-beat retarget plus its audit entry. Mutates the payload."""
    from . import tighten as tighten_mod

    before = [b.get("id") for b in payload.get("beats", []) or []
              if isinstance(b, dict)
              and b.get("status") == "dropped_target"]
    tighten_mod.apply_target(payload, words, target_s)
    after = [b.get("id") for b in payload.get("beats", []) or []
             if isinstance(b, dict)
             and b.get("status") == "dropped_target"]
    dropped = [b for b in after if b not in before]
    edit_id = _next_audit_id(payload)
    payload.setdefault("edits_applied", []).append({
        "id": edit_id, "source": source, "op": "retime_target",
        "target_s": target_s, "dropped_beat_ids": dropped,
        "at": datetime.now().isoformat(timespec="seconds"), "why": why})
    return edit_id, dropped


def _patch_untightened_angle(job: JobContext, op: dict,
                             at_s: float | None) -> str | None:
    """A set_angle against a shot: patch angles.json. Returns an id."""
    camera = op.get("camera", "")
    shot = shot_at_output(job, at_s) if at_s is not None else None
    if shot is None or not camera:
        return None
    shot["camera"] = camera
    shot["method"] = "editor"
    if op.get("why"):
        shot["reason"] = str(op.get("why"))
    write_json(job.angles_path or (job.job_dir / "angles.json"),
               job.angles)
    return f"shot{shot.get('id')}:{camera}"


def apply_notes(*, job: JobContext, notes: list[dict],
                pick: list[int] | None = None,
                candidates: dict[str, int] | None = None,
                source: str = "voice") -> dict:
    """Apply picked ops from resolved-note dicts.

    `pick` indexes the flattened ops (all notes, in order); None picks
    every unblocked op. `candidates` retargets ambiguous notes'
    beat ops ({note_id: beat_id}); ambiguous notes without a pick are
    skipped. Blocked ops are never applied. Writes tighten.json /
    taste.json / angles.json as needed. Returns {"applied": [...],
    "notes": [...], "need_decision": n}.
    """
    candidates = dict(candidates or {})
    flat: list[tuple[int, int, dict]] = []
    for note_pos, note in enumerate(notes):
        for op_pos, op in enumerate(note.get("ops", []) or []):
            flat.append((note_pos, op_pos, op))
    if pick is None:
        chosen = {i for i, (_, _, op) in enumerate(flat)
                  if not op.get("blocked")}
    else:
        chosen = {i for i in pick if 0 <= i < len(flat)}
    notes = [dict(note, ops=[dict(op) for op in note.get("ops", []) or []])
             for note in notes]
    applied: list[str] = []
    cutlist_ops: list[tuple[int, dict, float | None]] = []
    taste_ops: list[dict] = []
    retime_ops: list[dict] = []
    for index in sorted(chosen):
        note_pos, op_pos, _ = flat[index]
        note = notes[note_pos]
        op = note["ops"][op_pos]
        if op.get("blocked"):
            # Ambiguous ops are always blocked; a picked candidate
            # retargets and re-validates below, so only picked ones
            # fall through.
            if not (note.get("status") == "ambiguous"
                    and candidates.get(note.get("id", "")) is not None):
                continue
        if note.get("status") == "ambiguous":
            choice = candidates.get(note.get("id", ""))
            if choice is None:
                continue
            op = dict(op)
            op["beat_id"] = choice
            picked = next((c for c in note.get("candidates", []) or []
                           if c.get("beat_id") == choice), None)
            if (picked is not None
                    and op.get("op") in ("drop_words", "restore_words")
                    and isinstance(picked.get("word_start"), int)
                    and isinstance(picked.get("word_end"), int)):
                op["word_start"] = picked["word_start"]
                op["word_end"] = picked["word_end"]
                if not str(op.get("text", "")).strip():
                    op["text"] = " ".join(
                        w.text for w in
                        job.words[op["word_start"]:op["word_end"]])
            at = picked.get("at_s") if picked else note.get("at_s")
            fresh = _check_ops(job, [op], at)
            note["ops"][op_pos] = fresh[0]
            op = fresh[0]
            if op.get("blocked"):
                continue
            note["beat_id"] = choice
            if picked:
                note["at_s"] = picked.get("at_s")
            note["status"] = "resolved"
        name = op.get("op", "")
        if name in CUTLIST_OPS:
            cutlist_ops.append((index, op, note.get("at_s")))
        elif name == "add_taste":
            taste_ops.append(op)
        elif name == "retime_target":
            retime_ops.append(op)
    if cutlist_ops and job.tighten is not None:
        stripped: list[dict] = []
        for _, op, _ in cutlist_ops:
            clean = _strip_for_cutlist(op)
            if clean.get("op") == "add_note":
                # Anchors are output times; editor notes are source times.
                try:
                    at = float(clean.get("at_s", 0.0))
                except (TypeError, ValueError):
                    at = 0.0
                clean["at_s"] = round(
                    cutlist.output_to_source(job.tighten, at), 3)
            stripped.append(clean)
        before = len(job.tighten.get("edits_applied", []) or [])
        cutlist.apply_operations(job.tighten, job.words, stripped,
                                 source=source, covers=job.covers)
        for entry in (job.tighten.get("edits_applied", []) or [])[before:]:
            if isinstance(entry, dict) and entry.get("id"):
                applied.append(str(entry["id"]))
        cutlist.write_tighten(
            job.tighten_path or (job.job_dir / "tighten.json"),
            job.tighten)
    elif cutlist_ops:
        for _, op, note_at in cutlist_ops:
            name = op.get("op", "")
            if name == "set_angle":
                made = _patch_untightened_angle(job, op, note_at)
                if made is not None:
                    applied.append(made)
            elif name == "add_note":
                applied.append(f"note:{op.get('text', '')[:24]}")
    for op in taste_ops:
        try:
            payload = add_taste(job.job_dir, str(op.get("text", "")),
                                source=source,
                                note_id=op.get("note_id"))
        except VoiceError as error:
            op["blocked"] = True
            op["checks"] = list(op.get("checks", [])) + [_blocked(
                str(error))]
            continue
        entries = payload.get("entries", [])
        if entries:
            applied.append(str(entries[-1].get("id", "")))
    if retime_ops and job.tighten is not None:
        for op in retime_ops:
            try:
                target = float(op.get("target_s", -1))
            except (TypeError, ValueError):
                continue
            edit_id, dropped = _retime(job.tighten, job.words, target,
                                       str(op.get("why", "")), source)
            applied.append(edit_id)
            op["checks"] = list(op.get("checks", [])) + [_passed(
                f"Dropped beats {', '.join(f'B{b}' for b in dropped)}"
                if dropped else "Already within the target length")]
        cutlist.write_tighten(
            job.tighten_path or (job.job_dir / "tighten.json"),
            job.tighten)
    need_decision = sum(1 for _, _, op in flat if op.get("blocked"))
    need_decision += sum(
        1 for note in notes
        if note.get("status") == "ambiguous"
        and note.get("id") not in candidates)
    return {"applied": applied, "notes": notes,
            "need_decision": need_decision}


def _unresolved_stub(raw: str, message: str, *, source: str,
                     engine: str) -> dict:
    return ResolvedNote(
        id="n?", source=source, raw_text=raw, heard="", anchor_kind="none",
        at_s=None, beat_id=-1, status="unresolved", message=message,
        ops=[], candidates=[], engine=engine, usd=0.0).as_dict()


def _renumber(notes: list[dict]) -> list[dict]:
    for pos, note in enumerate(notes):
        note["id"] = f"n{pos + 1}"
    return notes


def run(*, note_path: Path, job_dir: Path | None = None,
        model: str = RESOLVE_MODEL, max_usd: float = DEFAULT_MAX_USD,
        apply: bool = False, allow_whisper: bool = True,
        anchors: list[float | None] | None = None,
        sync_path: Path | None = None, words_path: Path | None = None,
        tighten_path: Path | None = None, angles_path: Path | None = None,
        source: str = "cli") -> dict:
    """Transcribe a note, resolve it, optionally apply. Returns the
    `voice.json` payload (also written).

    `anchors=[at_s]` marks a live recording (one note); file notes pass
    None and are grouped mechanically. Without `job_dir`, the explicit
    `*_path` files serve a hand-run pipeline and `voice.json` lands in
    the working directory. Refusals that happen before anything is heard
    raise VoiceError; once a transcript exists it is returned (and
    written) with the error instead of raising.
    """
    note_path = Path(note_path)
    if job_dir is not None:
        job = build_job_context(Path(job_dir))
    else:
        if sync_path is None or words_path is None or angles_path is None:
            raise VoiceError(
                "voice: pass --job DIR or --sync/--words/--angles")
        job = build_file_context(
            work_dir=Path("."), sync_path=Path(sync_path),
            words_path=Path(words_path), angles_path=Path(angles_path),
            tighten_path=Path(tighten_path) if tighten_path else None)
    duration = probe_note_duration(note_path)
    estimate = estimate_note_usd(duration)
    if estimate > max_usd:
        raise VoiceError(
            f"voice: estimated ${estimate:.2f} exceeds --max-usd "
            f"${max_usd:.2f}; raise --max-usd")
    work = job.job_dir / "voice"
    work.mkdir(parents=True, exist_ok=True)
    wav = work / f"{note_path.stem}.wav"
    run_command(extract_audio_command(note_path, wav))
    heard_warnings: list[str] = []
    turns, engine, transcribe_spend = transcribe_wav(
        wav, duration_s=duration, allow_whisper=allow_whisper,
        work_dir=work, warnings=heard_warnings)
    transcript = " ".join(t.text for t in turns)
    payload: dict = {
        "version": 1, "note_file": note_path.name, "engine": engine,
        "duration_s": round(duration, 3), "spend_usd": 0.0,
        "notes": [], "applied": [],
        "turns": [{"start_s": round(t.start_s, 3),
                   "end_s": round(t.end_s, 3), "text": t.text,
                   "speaker": t.speaker} for t in turns],
        "warnings": list(heard_warnings),
    }
    payload_path = job.job_dir / "voice.json"

    def _write() -> dict:
        write_json(payload_path, payload)
        return payload

    if not turns:
        message = NO_SPEECH_LIVE if source == "live" else NO_SPEECH_FILE
        payload["notes"] = _renumber([_unresolved_stub("", message,
                                                       source=source,
                                                       engine=engine)])
        payload["spend_usd"] = round(transcribe_spend, 4)
        return _write()
    estimate = estimate_note_usd(duration, len(transcript))
    if estimate > max_usd:
        refused = (f"voice: estimated ${estimate:.2f} exceeds --max-usd "
                   f"${max_usd:.2f}; raise --max-usd")
        groups = ([turns] if anchors is not None and len(anchors) == 1
                  else group_turns(turns))
        payload["notes"] = _renumber([
            _unresolved_stub(" ".join(t.text for t in group), refused,
                             source=source, engine=engine)
            for group in groups])
        payload["spend_usd"] = round(transcribe_spend, 4)
        payload["error"] = refused
        return _write()
    try:
        notes, resolve_spend, warnings = resolve_notes(
            turns=turns, job=job, anchors=list(anchors or []),
            model=model)
    except VoiceUnavailable:
        groups = ([turns] if anchors is not None and len(anchors) == 1
                  else group_turns(turns))
        payload["notes"] = _renumber([
            _unresolved_stub(" ".join(t.text for t in group),
                             MUSE_UNAVAILABLE_MESSAGE, source=source,
                             engine=engine)
            for group in groups])
        payload["spend_usd"] = round(transcribe_spend, 4)
        return _write()
    except VoiceError as error:
        groups = ([turns] if anchors is not None and len(anchors) == 1
                  else group_turns(turns))
        payload["notes"] = _renumber([
            _unresolved_stub(" ".join(t.text for t in group), str(error),
                             source=source, engine=engine)
            for group in groups])
        payload["spend_usd"] = round(transcribe_spend, 4)
        payload["error"] = str(error)
        return _write()
    total = round(transcribe_spend + resolve_spend, 4)
    share = round(total / max(1, len(notes)), 4)
    note_dicts: list[dict] = []
    for note in notes:
        note.source = source
        note.engine = engine
        note.usd = share
        note_dicts.append(note.as_dict())
    payload["notes"] = note_dicts
    payload["spend_usd"] = total
    payload["warnings"] = list(heard_warnings) + list(warnings)
    payload["resolver"] = ("rules" if any(
        "resolved offline by rules" in str(w) for w in warnings)
        else "muse")
    _, taste_warnings = load_taste_with_warnings(job.job_dir)
    payload["warnings"].extend(taste_warnings)
    if apply:
        result = apply_notes(job=job, notes=note_dicts, source="voice")
        payload["notes"] = result["notes"]
        payload["applied"] = result["applied"]
        payload["need_decision"] = result["need_decision"]
    return _write()
