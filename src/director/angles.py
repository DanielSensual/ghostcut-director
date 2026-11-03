"""Multicam angle choice: lines of speech become shots from the best camera.

Words from the audio clip are split into lines (sentence breaks, pauses over
0.6 s, 8 s cap at the longest pause). Each line is covered by the cameras
whose clip spans the whole line; one frame per line x covering camera goes to
Muse Spark, which picks the angle. Mechanical checks overrule Muse when its
camera does not cover the line, merge same-camera lines, merge short shots,
and pin cuts to inter-word gaps. Without guestlist (--no-muse) a deterministic
rule opens on the reference camera and alternates.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from ghostcut.config import read_json, write_json
from ghostcut.ffmpeg_tools import ToolingError, run_command, snap_frame_rate
from . import cutlist
from .grade import GradeError, GradeShotInfo, SourceColour, lut_filter_chain

PAUSE_S = 0.6
MAX_LINE_S = 8.0
BATCH_LINES = 4
FRAME_W = 512
OUT_W, OUT_H = 1920, 1080
LONG_SHOT_S = 15.0
SHOT_SIZES = ("wide", "medium", "close", "side")
CROSSFADE_S = 0.008
AUDIO_SR = 48000

DEVELOPER = (
    "You are Muse, the multicam angle picker inside GhostCut. You are given, for each of "
    "several consecutive spoken lines, the line text and duration and one frame from every "
    "camera that covers the line, labelled by camera letter (A, B, C, ...). The frames for a "
    "line all show the same moment from different angles. For each line, pick the one camera "
    "whose angle best serves that line, name the shot size you see in that frame (wide, "
    "medium, close, or side), and give a one-sentence reason grounded in what is visible "
    "(who is framed, gaze, gesture, obstructions). Prefer the camera where the current "
    "speaker is clearly framed and looking toward lens; avoid frames where the speaker is "
    "turned away, blocked, mid-blink as the only flaw-free alternative exists, or where the "
    "framing cuts off the subject. You may only choose among the camera letters listed for "
    "that line. Return one entry for every line id given, in order. Never invent a camera, "
    "a person, or an action you cannot see."
)


class MuseUnavailable(RuntimeError):
    """guestlist (the Meta API client) cannot be imported."""


@dataclass(slots=True)
class TimedWord:
    text: str
    start_s: float
    end_s: float


@dataclass(slots=True)
class SyncClip:
    label: str
    path: str
    offset_s: float | None
    fps: float = 0.0
    width: int = 0
    height: int = 0
    duration_s: float = 0.0


@dataclass(slots=True)
class Line:
    id: int
    start_s: float
    end_s: float
    text: str
    word_start: int
    word_end: int
    cameras: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Pick:
    line_id: int
    camera: str | None
    shot_size: str
    reason: str
    method: str
    overruled: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Shot:
    id: int
    start_s: float
    end_s: float
    camera: str
    clip_path: str
    clip_in_s: float
    clip_out_s: float
    line_ids: list[int]
    shot_size: str
    reason: str
    method: str
    overrules: list[str] = field(default_factory=list)
    # Tightened jobs only: start_s/end_s are output-timeline times and
    # these record the source span the shot renders. Un-tightened jobs
    # leave the defaults and the fields stay out of angles.json.
    source_start_s: float = 0.0
    source_end_s: float = 0.0
    kept_span_id: str = ""
    beat_id: int = -1


# ---------------------------------------------------------------- words and lines

def load_timed_words(path: Path) -> list[TimedWord]:
    """(text, start, end) with raw text; missing end falls back to next start."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data.get("words"), list):
        raw = data["words"]
    else:
        raw = [w for seg in data.get("segments", []) for w in seg.get("words", [])]
    parsed: list[TimedWord] = []
    for w in raw:
        if not isinstance(w, dict):
            continue
        text = str(w.get("word") or w.get("text") or "")
        start = w.get("start")
        if not text.strip() or start is None:
            continue
        end = w.get("end")
        parsed.append(TimedWord(text.strip(), float(start),
                                float(end) if end is not None else -1.0))
    parsed.sort(key=lambda t: t.start_s)
    out: list[TimedWord] = []
    for i, t in enumerate(parsed):
        end = t.end_s
        if end < 0:
            if i + 1 < len(parsed):
                end = parsed[i + 1].start_s
            else:
                end = t.start_s + 0.30
        out.append(TimedWord(t.text, t.start_s, max(end, t.start_s)))
    return out


def _sentence_end(text: str) -> bool:
    s = text.strip().rstrip("\"'’”`)]}>")
    return bool(s) and s[-1] in ".!?"


def _longest_pause_split(words: list[TimedWord]) -> int:
    """Index after which the intra-line gap is largest (ties go earliest)."""
    best, best_gap = 1, words[1].start_s - words[0].end_s
    for k in range(2, len(words)):
        gap = words[k].start_s - words[k - 1].end_s
        if gap > best_gap:
            best, best_gap = k, gap
    return best


def split_lines(words: list[TimedWord]) -> list[Line]:
    """Walk words once; sentence ends and long pauses start a line, long runs split inside."""
    lines: list[Line] = []

    def emit(part: list[TimedWord], start_idx: int) -> None:
        lines.append(Line(len(lines), part[0].start_s, part[-1].end_s,
                          " ".join(w.text for w in part),
                          start_idx, start_idx + len(part)))

    cur: list[TimedWord] = []
    cur_idx = 0
    for i, w in enumerate(words):
        if not cur:
            cur, cur_idx = [w], i
            continue
        prev = cur[-1]
        if _sentence_end(prev.text) or w.start_s - prev.end_s > PAUSE_S + 1e-9:
            emit(cur, cur_idx)
            cur, cur_idx = [w], i
            continue
        if w.end_s - cur[0].start_s > MAX_LINE_S:
            acc = cur + [w]
            gaps = [acc[k + 1].start_s - acc[k].end_s for k in range(len(acc) - 1)]
            if not gaps or max(gaps) - min(gaps) < 1e-9 or max(gaps) <= 1e-9:
                emit(cur, cur_idx)
                cur, cur_idx = [w], i
                continue
            k = _longest_pause_split(acc)
            emit(acc[:k], cur_idx)
            cur, cur_idx = acc[k:], cur_idx + k
            continue
        cur.append(w)
    if cur:
        emit(cur, cur_idx)
    return lines


# ---------------------------------------------------------------- sync views and coverage

def load_sync(path: Path) -> tuple[str, list[SyncClip]]:
    """sync.json reference path plus one SyncClip view per clip."""
    data = read_json(Path(path))
    if not isinstance(data, dict) or not isinstance(data.get("clips"), list):
        raise ValueError(f"Not a sync.json: {path}")
    reference = str(data.get("reference") or "")
    clips: list[SyncClip] = []
    seen: set[str] = set()
    for item in data["clips"]:
        label = str(item.get("label") or "")
        if not label:
            raise ValueError(f"Sync clip without a label in {path}")
        if label in seen:
            raise ValueError(f"Duplicate label {label} in {path}")
        seen.add(label)
        cam = item.get("camera") or {}
        offset = item.get("offset_s")
        clips.append(SyncClip(
            label=label,
            path=str(cam.get("path") or ""),
            offset_s=None if offset is None else float(offset),
            fps=float(cam.get("fps") or 0.0),
            width=int(cam.get("width") or 0),
            height=int(cam.get("height") or 0),
            duration_s=float(cam.get("duration_s") or 0.0),
        ))
    return reference, clips


def load_grade(path: Path) -> dict[str, GradeShotInfo]:
    """grade.json LUTs by camera label; lut:null renders the legacy look."""
    data = read_json(Path(path))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError(f"Not a grade.json: {path}")
    base = Path(path).parent
    out: dict[str, GradeShotInfo] = {}
    for item in data.get("cameras", []):
        label = str(item.get("label") or "")
        lut = item.get("lut")
        seen = item.get("source_colour") or {}
        colour = SourceColour(
            range=str(seen.get("range") or "tv"),
            space=str(seen.get("space") or "bt709"),
            primaries=str(seen.get("primaries") or "bt709"),
            transfer=str(seen.get("transfer") or ""),
            pix_fmt=str(seen.get("pix_fmt") or ""),
            bits=int(seen.get("bits") or 8),
        )
        if lut is None:
            out[label] = GradeShotInfo(label, None, colour)
            continue
        if "'" in str(lut) or ":" in str(lut):
            raise GradeError(f"Grade LUT path cannot contain ' or :: {lut}")
        resolved = base / str(lut)
        if not resolved.exists():
            raise ValueError(f"Grade LUT missing for camera {label}: {resolved}")
        out[label] = GradeShotInfo(label, resolved, colour)
    return out


def covers(clip: SyncClip, line: Line) -> bool:
    """The clip spans the whole line; unplaced clips never cover."""
    return (clip.offset_s is not None
            and clip.offset_s <= line.start_s + 1e-9
            and line.end_s <= clip.offset_s + clip.duration_s + 1e-9)


def covering(clips: list[SyncClip], line: Line) -> list[SyncClip]:
    """Covering clips in label order."""
    return sorted((c for c in clips if covers(c, line)), key=lambda c: c.label)


def frame_time(clip: SyncClip, line: Line) -> float:
    """Clip-local time of the line midpoint."""
    mid = (line.start_s + line.end_s) / 2
    return mid - (clip.offset_s or 0.0)


def frame_path(frames_dir: Path, line: Line, clip: SyncClip) -> Path:
    return Path(frames_dir) / f"L{line.id:03d}_{clip.label}.jpg"


def frame_command(clip_path: str, at_s: float, out: Path) -> list[str]:
    return ["ffmpeg", "-v", "error", "-ss", f"{at_s:.3f}", "-i", clip_path,
            "-frames:v", "1", "-vf", f"scale={FRAME_W}:-2", "-q:v", "3", "-y", str(out)]


def grab_frames(lines: list[Line], clips: list[SyncClip],
                frames_dir: Path) -> dict[tuple[int, str], Path]:
    """One midpoint still per line x covering camera, cached by file existence."""
    Path(frames_dir).mkdir(parents=True, exist_ok=True)
    frames: dict[tuple[int, str], Path] = {}
    for line in lines:
        for clip in covering(clips, line):
            out = frame_path(frames_dir, line, clip)
            if out.exists() and out.stat().st_size > 0:
                frames[(line.id, clip.label)] = out
                continue
            try:
                run_command(frame_command(clip.path, frame_time(clip, line), out))
            except Exception:
                # the camera is simply not offered for this line
                if clip.label in line.cameras:
                    line.cameras.remove(clip.label)
                line.warnings.append(f"frame_failed:{clip.label}")
                continue
            frames[(line.id, clip.label)] = out
    return frames


# ---------------------------------------------------------------- Muse picks

def batch_schema(batch: list[Line], coverage: dict[int, list[str]]) -> dict:
    """Strict tuple-style schema: each position admits only its line's cameras."""
    items = []
    for line in batch:
        items.append({
            "type": "object",
            "additionalProperties": False,
            "required": ["line_id", "camera", "shot_size", "reason"],
            "properties": {
                "line_id": {"type": "integer", "enum": [line.id]},
                "camera": {"type": "string", "enum": list(coverage[line.id])},
                "shot_size": {"type": "string", "enum": list(SHOT_SIZES)},
                "reason": {"type": "string"},
            },
        })
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["picks"],
        "properties": {
            "picks": {
                "type": "array",
                "prefixItems": items,
                "items": False,
                "minItems": len(batch),
                "maxItems": len(batch),
            }
        },
    }


def batch_prompt(batch: list[Line], coverage: dict[int, list[str]]) -> str:
    out = ["Pick the angle for each line. Reply with one entry per line, in order."]
    for line in batch:
        letters = ", ".join(coverage[line.id])
        out.append(f'L{line.id} ({line.end_s - line.start_s:.1f}s) cameras {letters}: "{line.text}"')
    return "\n".join(out)


def pick_with_muse(lines: list[Line], clips: list[SyncClip],
                   frames: dict[tuple[int, str], Path], *,
                   model: str, frames_dir: Path) -> tuple[list[Pick], float, list[str]]:
    """Batched spark.image_json calls; per-batch failure falls back to the rule."""
    try:
        from guestlist.meta import cache, spark
    except ImportError as error:
        raise MuseUnavailable(str(error)) from error
    coverable = [line for line in lines if line.cameras]
    picks_by_id: dict[int, Pick] = {}
    for line in lines:
        if not line.cameras:
            picks_by_id[line.id] = Pick(line.id, None, "", "rule: no covering camera", "rule", [])
    warnings: list[str] = []
    spend = 0.0
    cached = 0
    prev_label: str | None = None
    batches = [coverable[i:i + BATCH_LINES] for i in range(0, len(coverable), BATCH_LINES)]
    for bi, batch in enumerate(batches):
        coverage = {line.id: list(line.cameras) for line in batch}
        shown = [(line.id, label) for line in batch for label in coverage[line.id] if (line.id, label) in frames]
        images = [frames[k] for k in shown]
        schema = batch_schema(batch, coverage)
        # the images carry no labels of their own, so say which is which
        prompt = batch_prompt(batch, coverage) + "\nImages, in order: " + ", ".join(
            f"{n} = L{lid} camera {label}" for n, (lid, label) in enumerate(shown, 1))
        try:
            cache.guard(0.25)
            r = spark.image_json(images, schema=schema, name="angles_pick",
                                 developer=DEVELOPER, prompt=prompt, model=model,
                                 effort="medium", max_output_tokens=4000)
            spend += float(r.usd or 0.0)
            if getattr(r, "cached", False):
                cached += 1
            entries = (r.data or {}).get("picks", []) if isinstance(r.data, dict) else []
            seen: set[int] = set()
            for entry in entries:
                try:
                    lid = int(entry["line_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                if lid not in coverage:
                    continue
                if lid in seen:  # the first answer stands
                    picks_by_id[lid].overruled.append("duplicate_reply")
                    continue
                seen.add(lid)
                picks_by_id[lid] = Pick(lid, entry.get("camera"),
                                        str(entry.get("shot_size") or ""),
                                        str(entry.get("reason") or ""),
                                        "muse", [])
            for line in batch:
                if line.id not in seen:
                    picks_by_id[line.id] = Pick(line.id, None, "", "rule: no reply",
                                                "rule", ["missing_from_reply"])
            for line in batch:
                if picks_by_id[line.id].camera is not None:
                    prev_label = picks_by_id[line.id].camera
        except Exception:
            for line in batch:
                chosen = rule_pick_for(line, clips, prev_label)
                picks_by_id[line.id] = Pick(line.id, chosen, "medium" if chosen else "",
                                            "rule: batch failed", "rule", ["batch_failed"])
                if chosen is not None:
                    prev_label = chosen
            warnings.append(f"batch {bi} failed; rule picks used for those lines")
    if cached:
        warnings.append(f"{cached} batch(es) served from cache")
    picks = [picks_by_id[line.id] for line in lines]
    return picks, round(spend, 4), warnings


# ---------------------------------------------------------------- rule fallback

def rule_pick_for(line: Line, clips: list[SyncClip], previous_label: str | None) -> str | None:
    """Next covering label after the previous one (wrapping), else the first."""
    labels = [c.label for c in covering(clips, line)]
    if not labels:
        return None
    if previous_label in labels:
        return labels[(labels.index(previous_label) + 1) % len(labels)]
    return labels[0]


def rule_picks(lines: list[Line], clips: list[SyncClip],
               reference_label: str, min_shot_s: float) -> list[Pick]:
    """Open on the reference, then rotate; a forced cut beats min-shot."""
    picks: list[Pick] = []
    cur: str | None = None
    run_start = 0.0
    run_end = 0.0
    for i, line in enumerate(lines):
        cov = [c.label for c in covering(clips, line)]
        if not cov:
            picks.append(Pick(line.id, None, "", "rule: no covering camera", "rule", []))
            cur = None
            continue
        if i == 0 or cur is None:
            chosen = reference_label if reference_label in cov else cov[0]
            picks.append(Pick(line.id, chosen, "medium", "rule: reference opening", "rule", []))
            cur, run_start, run_end = chosen, line.start_s, line.end_s
            continue
        dur = run_end - run_start
        if cur in cov and dur < min_shot_s:
            picks.append(Pick(line.id, cur, "medium", "rule: alternation", "rule", []))
            run_end = line.end_s
            continue
        chosen = rule_pick_for(line, clips, cur)
        picks.append(Pick(line.id, chosen, "medium", "rule: alternation", "rule", []))
        if chosen == cur:
            run_end = line.end_s
        else:
            cur, run_start, run_end = chosen, line.start_s, line.end_s
            cur = chosen
    return picks


# ---------------------------------------------------------------- checks (Muse is advisory; these decide)

def _merge_same_camera(picks: list[Pick], lines: list[Line],
                       clips: list[SyncClip]) -> list[Shot]:
    """Maximal runs of equal non-None cameras; interior gaps become black shots."""
    by_label = {c.label: c for c in clips}
    order = sorted(lines, key=lambda l: l.id)
    first = next((l for l in order if _pick_cam(picks, l.id) is not None), None)
    last = next((l for l in reversed(order) if _pick_cam(picks, l.id) is not None), None)
    if first is None or last is None:
        return []
    span = [l for l in order if first.id <= l.id <= last.id]
    picks_by_id = {p.line_id: p for p in picks}
    shots: list[Shot] = []
    i = 0
    while i < len(span):
        line, pick = span[i], picks_by_id[span[i].id]
        if pick.camera is None:
            j = i
            while j < len(span) and picks_by_id[span[j].id].camera is None:
                j += 1
            run = span[i:j]
            lids = [l.id for l in run]
            over: list[str] = []
            for l in run:
                for o in picks_by_id[l.id].overruled:
                    if o not in over:
                        over.append(o)
            if "no_camera" not in over:
                over.append("no_camera")
            shots.append(Shot(len(shots), run[0].start_s, run[-1].end_s, "black", "",
                              0.0, run[-1].end_s - run[0].start_s, lids, "", " ".join(
                                  picks_by_id[l.id].reason for l in run).strip(),
                              "rule", over))
            i = j
            continue
        j = i
        while j < len(span) and picks_by_id[span[j].id].camera == pick.camera:
            j += 1
        run = span[i:j]
        lids = [l.id for l in run]
        clip = by_label.get(pick.camera or "")
        over = []
        for l in run:
            for o in picks_by_id[l.id].overruled:
                if o not in over:
                    over.append(o)
        shots.append(Shot(len(shots), run[0].start_s, run[-1].end_s, pick.camera or "",
                          clip.path if clip else "", 0.0, 0.0, lids,
                          picks_by_id[run[0].id].shot_size,
                          " ".join(picks_by_id[l.id].reason for l in run).strip(),
                          "muse" if all(picks_by_id[l.id].method == "muse" for l in run) else "rule",
                          over))
        i = j
    for s in shots:
        if s.camera != "black":
            clip = by_label.get(s.camera)
            off = clip.offset_s if clip and clip.offset_s is not None else 0.0
            s.clip_in_s, s.clip_out_s = s.start_s - off, s.end_s - off
    return shots


def _pick_cam(picks: list[Pick], line_id: int) -> str | None:
    for p in picks:
        if p.line_id == line_id:
            return p.camera
    return None


def _merge_short(shots: list[Shot], clips: list[SyncClip],
                 lines_by_id: dict[int, Line], min_shot_s: float) -> list[Shot]:
    """Fold each short shot into a neighbour that covers its lines; else keep it."""
    by_label = {c.label: c for c in clips}
    if len(shots) <= 1:
        return shots
    kept: set[int] = set()
    while True:
        idx = next((i for i, s in enumerate(shots)
                    if s.id not in kept and s.camera != "black"
                    and s.end_s - s.start_s < min_shot_s), None)
        if idx is None:
            break
        short = shots[idx]
        options: list[tuple[str, Shot]] = []
        if idx > 0:
            options.append(("prev", shots[idx - 1]))
        if idx < len(shots) - 1:
            options.append(("next", shots[idx + 1]))
        eligible: list[tuple[str, Shot]] = []
        for side, nb in options:
            if nb.camera == "black":
                continue
            clip = by_label.get(nb.camera)
            if clip is None:
                continue
            if all(covers(clip, lines_by_id[lid]) for lid in short.line_ids):
                eligible.append((side, nb))
        if not eligible:
            short.overrules.append("min_shot_kept:" + "+".join(f"L{lid}" for lid in short.line_ids))
            kept.add(short.id)
            continue
        if len(eligible) == 1:
            side, nb = eligible[0]
        else:
            prev, nxt = shots[idx - 1], shots[idx + 1]
            side, nb = ("prev", prev) if prev.end_s - prev.start_s >= nxt.end_s - nxt.start_s else ("next", nxt)
        if side == "prev":
            nb.line_ids = sorted(nb.line_ids + short.line_ids)
            nb.start_s = min(nb.start_s, short.start_s)
            nb.end_s = max(nb.end_s, short.end_s)
            nb.reason = f"{nb.reason} {short.reason}".strip()
            nb.overrules.append("min_shot_merge:" + "+".join(f"L{lid}" for lid in short.line_ids) + f"->{nb.camera}")
            if short.method != "muse" or nb.method != "muse":
                nb.method = "rule"
            shots.pop(idx)
        else:
            nb.line_ids = sorted(short.line_ids + nb.line_ids)
            nb.start_s = min(nb.start_s, short.start_s)
            nb.end_s = max(nb.end_s, short.end_s)
            nb.reason = f"{short.reason} {nb.reason}".strip()
            nb.overrules.append("min_shot_merge:" + "+".join(f"L{lid}" for lid in short.line_ids) + f"->{nb.camera}")
            if short.method != "muse" or nb.method != "muse":
                nb.method = "rule"
            shots.pop(idx)
    return _join_same_camera(shots)


def _join_same_camera(shots: list[Shot]) -> list[Shot]:
    """A merge can leave two neighbours on one camera (A, short B, A); a cut between them changes nothing."""
    out: list[Shot] = []
    for shot in shots:
        prev = out[-1] if out else None
        if prev is not None and prev.camera == shot.camera and shot.camera != "black":
            prev.line_ids = prev.line_ids + shot.line_ids
            prev.end_s = shot.end_s
            prev.reason = f"{prev.reason} {shot.reason}".strip()
            prev.overrules += [o for o in shot.overrules if o not in prev.overrules]
            if shot.method != "muse":
                prev.method = "rule"
            continue
        out.append(shot)
    return out


def _shot_warnings(shots: list[Shot]) -> None:
    for s in shots:
        dur = s.end_s - s.start_s
        if dur > LONG_SHOT_S:
            s.overrules.append(f"long_shot:{dur:.1f}s")


def apply_checks(picks: list[Pick], lines: list[Line], clips: list[SyncClip], *,
                 min_shot_s: float) -> list[Shot]:
    """Coverage overrules, same-camera merge, min-shot merge, cut points, long-shot notes."""
    known = {c.label for c in clips}
    lines_by_id = {line.id: line for line in lines}
    picks_by_id = {p.line_id: p for p in picks}
    checked: list[Pick] = []
    prev_kept: str | None = None
    for line in sorted(lines, key=lambda l: l.id):
        pick = picks_by_id.get(line.id)
        if pick is None:
            pick = Pick(line.id, None, "", "", "rule", ["missing_from_reply"])
        bad = pick.camera
        if not line.cameras:
            pick.camera, pick.method = None, "rule"
            if "no_camera" not in pick.overruled:
                pick.overruled.append("no_camera")
        elif bad is None or bad not in known or bad not in line.cameras:
            new = rule_pick_for(line, clips, prev_kept)
            if bad is None:
                pick.overruled.append(f"not_covering:none->{new or 'none'}")
            elif bad not in known:
                pick.overruled.append(f"unknown_camera:{bad}->{new or 'none'}")
            else:
                pick.overruled.append(f"not_covering:{bad}->{new or 'none'}")
            pick.camera, pick.method = new, "rule"
        if pick.camera is not None and pick.shot_size not in SHOT_SIZES:
            pick.overruled.append(f"bad_shot_size:{pick.shot_size}")
            pick.shot_size = "medium"
        if pick.camera is not None and not pick.shot_size:
            pick.shot_size = "medium"
        prev_kept = pick.camera
        checked.append(pick)
    shots = _merge_same_camera(checked, lines, clips)
    shots = _merge_short(shots, clips, lines_by_id, min_shot_s)
    by_label = {c.label: c for c in clips}
    for i, shot in enumerate(shots):
        first = lines_by_id[shot.line_ids[0]]
        last = lines_by_id[shot.line_ids[-1]]
        start = first.start_s if i == 0 else (
            lines_by_id[shots[i - 1].line_ids[-1]].end_s + first.start_s) / 2
        end = last.end_s if i == len(shots) - 1 else (
            last.end_s + lines_by_id[shots[i + 1].line_ids[0]].start_s) / 2
        shot.start_s, shot.end_s = start, end
        if shot.camera == "black":
            shot.clip_in_s, shot.clip_out_s = 0.0, end - start
        else:
            clip = by_label.get(shot.camera)
            off = clip.offset_s if clip and clip.offset_s is not None else 0.0
            shot.clip_in_s, shot.clip_out_s = start - off, end - off
    _shot_warnings(shots)
    for i, shot in enumerate(shots):
        shot.id = i
    return shots


def cut_points(lines: list[Line], words: list[TimedWord]) -> list[float]:
    """One midpoint per adjacent line pair, always inside an inter-word gap."""
    out = []
    for i in range(len(lines) - 1):
        prev_end = words[lines[i].word_end - 1].end_s
        next_start = words[lines[i + 1].word_start].start_s
        out.append((prev_end + next_start) / 2)
    return out


# ---------------------------------------------------------------- tightened jobs (TIGHTEN-DESIGN.md §10)


def _tightened_lines(tighten: dict, words: list[TimedWord]) -> list[Line]:
    """split_lines per kept span, concatenated with fresh ids (source time).

    Kept words are the loaded words whose indices fall inside kept spans'
    utterances; word indices stay global so cut_points keeps working.
    """
    utterances = {u.get("id"): u for u in tighten.get("utterances", []) or []
                  if isinstance(u, dict)}
    lines: list[Line] = []
    for span in tighten.get("kept_spans", []) or []:
        if not isinstance(span, dict):
            continue
        indices: list[int] = []
        for uid in span.get("utterance_ids", []) or []:
            entry = utterances.get(uid)
            if entry is None:
                continue
            try:
                start, end = int(entry.get("word_start", 0)), int(
                    entry.get("word_end", 0))
            except (TypeError, ValueError):
                continue
            indices.extend(range(start, end))
        indices = sorted({i for i in indices if 0 <= i < len(words)})
        if not indices:
            continue
        for line in split_lines([words[i] for i in indices]):
            lines.append(Line(
                len(lines), line.start_s, line.end_s, line.text,
                indices[line.word_start], indices[line.word_end - 1] + 1))
    return lines


def _apply_angle_overrides(tighten: dict, lines: list[Line],
                           picks: list[Pick]) -> None:
    """Editor angle overrides from tighten.json onto picks (method "editor").

    Runs after picks are made and before apply_checks, which overrules an
    override camera that does not cover the line exactly as it overrules
    anything else.
    """
    beats = {b.get("id"): b for b in tighten.get("beats", []) or []
             if isinstance(b, dict)}
    utterances = {u.get("id"): u for u in tighten.get("utterances", []) or []
                  if isinstance(u, dict)}
    picks_by_id = {p.line_id: p for p in picks}
    for override in tighten.get("angle_overrides", []) or []:
        if not isinstance(override, dict):
            continue
        beat = beats.get(override.get("beat_id"))
        if beat is None:
            continue
        attempts = beat.get("attempts", []) or []
        index = beat.get("pick_attempt", -1)
        if not isinstance(index, int) or not 0 <= index < len(attempts):
            continue
        pick = attempts[index]
        if not isinstance(pick, dict):
            continue
        bounds: list[int] = []
        for uid in pick.get("utterance_ids", []) or []:
            entry = utterances.get(uid)
            if isinstance(entry, dict):
                bounds += [entry.get("word_start", 0),
                           entry.get("word_end", 0)]
        if not bounds:
            continue
        low, high = min(bounds), max(bounds)
        note = f"angle_override:B{beat.get('id')}"
        for line in lines:
            if low <= line.word_start and line.word_end <= high:
                found = picks_by_id.get(line.id)
                if found is None:
                    continue
                found.camera = override.get("camera")
                found.method = "editor"
                if note not in found.overruled:
                    found.overruled.append(note)


def _map_shots_to_output(shots: list[Shot], lines: list[Line],
                         clips_by_label: dict[str, SyncClip],
                         tighten: dict) -> list[Shot]:
    """Source-time shots -> output-timeline shots through the kept spans.

    Each shot is split at kept-span boundaries (no shot crosses removed
    material), edge parts extend to the span edges they cover, and times
    convert through the span: out = span_out_start + (source - span.start).
    """
    spans = [s for s in tighten.get("kept_spans", []) or []
             if isinstance(s, dict)]
    if not spans or not shots:
        return []
    line_span: dict[int, int] = {}
    for line in lines:
        for pos, span in enumerate(spans):
            if (float(span.get("start_s", 0.0)) <= line.start_s
                    and line.end_s <= float(span.get("end_s", 0.0))):
                line_span[line.id] = pos
                break
    starts: list[float] = []
    total = 0.0
    for span in spans:
        starts.append(total)
        total += float(span.get("end_s", 0.0)) - float(span.get("start_s", 0.0))
    out: list[Shot] = []
    for pos, span in enumerate(spans):
        span_start = float(span.get("start_s", 0.0))
        span_end = float(span.get("end_s", 0.0))
        parts: list[tuple[float, float, Shot, list[int]]] = []
        for shot in shots:
            low = max(shot.start_s, span_start)
            high = min(shot.end_s, span_end)
            if high <= low:
                continue
            owned = [lid for lid in shot.line_ids
                     if line_span.get(lid) == pos]
            if not owned:
                continue
            parts.append((low, high, shot, owned))
        if not parts:
            continue
        parts.sort(key=lambda part: part[0])
        span_line_ids = {lid for lid, owned_pos in line_span.items()
                         if owned_pos == pos}
        first_lid = min(span_line_ids) if span_line_ids else -1
        last_lid = max(span_line_ids) if span_line_ids else -1
        for number, (low, high, shot, owned) in enumerate(parts):
            # Extend to the span edge only over covered lines: a span whose
            # head or tail lines were dropped (no camera) starts/ends at
            # the first/last rendered line, exactly like an un-tightened cut.
            if number == 0 and first_lid in owned:
                low = span_start
            if number == len(parts) - 1 and last_lid in owned:
                high = span_end
            out_low = starts[pos] + (low - span_start)
            out_high = starts[pos] + (high - span_start)
            if shot.camera == "black":
                clip_in, clip_out = 0.0, out_high - out_low
            else:
                clip = clips_by_label.get(shot.camera)
                offset = (clip.offset_s if clip is not None
                          and clip.offset_s is not None else 0.0)
                clip_in, clip_out = low - offset, high - offset
            mapped = Shot(
                len(out), out_low, out_high, shot.camera, shot.clip_path,
                clip_in, clip_out, sorted(owned), shot.shot_size,
                shot.reason, shot.method, list(shot.overrules),
                low, high, str(span.get("id", "")), span.get("beat_id", -1))
            out.append(mapped)
    return out


# ---------------------------------------------------------------- render (list only; run_command does the work)

def _out_fps(audio: SyncClip) -> str:
    if audio.fps and audio.fps > 0:
        fr = snap_frame_rate(audio.fps)
        return str(fr.numerator) if fr.denominator == 1 else f"{fr.numerator}/{fr.denominator}"
    return "30"


def frame_grid(shots: list[Shot], fps: float) -> list[float]:
    """Shot boundaries snapped to whole output frames from the first cut, so concat never drifts off the audio."""
    rate = snap_frame_rate(fps) if fps > 0 else Fraction(30)
    t0 = shots[0].start_s
    edges = [s.start_s for s in shots] + [shots[-1].end_s]
    return [t0 + float(round(Fraction(t - t0).limit_denominator(1_000_000) * rate) / rate) for t in edges]


def audio_part_command(audio_path: str, audio_in_s: float,
                       duration_s: float, out: Path) -> list[str]:
    """One tightened audio part: the kept span's sound with click-killing fades."""
    fade_out_start = max(0.0, duration_s - CROSSFADE_S)
    return ["ffmpeg", "-v", "error", "-y",
            "-ss", f"{audio_in_s:.6f}", "-t", f"{duration_s:.6f}",
            "-i", audio_path, "-map", "0:a:0",
            "-af", (f"afade=t=in:st=0:d={CROSSFADE_S},"
                    f"afade=t=out:st={fade_out_start:.6f}:d={CROSSFADE_S},"
                    f"aresample={AUDIO_SR}"),
            "-c:a", "aac", "-b:a", "192k", str(out)]


def _snapped_span_audio(shots: list[Shot], kept_spans: list[dict],
                        audio: SyncClip) -> list[tuple[float, float, int]]:
    """(clip-local audio_in_s, snapped output duration_s, span index).

    One part per kept span clipped to the rendered shot range (leading or
    trailing lines with no camera are dropped from the picture, exactly as
    in an un-tightened cut, and the audio follows the picture). Span
    boundaries snap to the same frame grid the picture uses, anchored at
    the first shot, so the joined audio is exactly as long as the video.
    """
    bounds = [0.0]
    for span in kept_spans:
        bounds.append(bounds[-1] + float(span["end_s"]) - float(span["start_s"]))
    first, last = shots[0].start_s, shots[-1].end_s
    rate = snap_frame_rate(audio.fps) if audio.fps > 0 else Fraction(30)

    def snap(t: float) -> float:
        return first + float(
            round(Fraction(t - first).limit_denominator(1_000_000) * rate)
            / rate)

    portions: list[tuple[float, float, int]] = []
    for pos, span in enumerate(kept_spans):
        low, high = max(bounds[pos], first), min(bounds[pos + 1], last)
        if high <= low + 1e-12:
            continue
        source_low = float(span["start_s"]) + (low - bounds[pos])
        portions.append((source_low - (audio.offset_s or 0.0),
                         snap(high) - snap(low), pos))
    return portions


def render_commands(shots: list[Shot], clips_by_label: dict[str, SyncClip], audio: SyncClip,
                    out: Path, *,
                    grade: dict[str, GradeShotInfo] | None = None,
                    kept_spans: list[dict] | None = None) -> tuple[list[list[str]], str, list[str]]:
    """One ffmpeg run per shot, then a stream-copy join with the uncut audio laid under it.

    Shot by shot keeps one camera decoder open at a time: a single filtergraph over every 4K input
    peaked at 2.3 GB for 7 shots. Each part is cut to a whole number of frames on the shared grid,
    so the joined picture is exactly as long as the audio span.
    With a grade, each camera shot renders through its LUT; black shots and
    cameras without a cube keep the legacy look. Without one, every command
    is byte-identical to the ungraded render.
    With kept_spans (a tightened job: shots carry output times), the video
    parts are the same shape, the audio is cut to one part per kept span
    with short fades at the joins, and the final command concats both. The
    returned commands are then the video parts (one per shot, in order)
    followed by the audio parts (one per rendered span portion, in span
    order); render() splits them back apart by len(shots).
    Returns (part commands, concat list text, final command).
    """
    if not shots:
        raise ValueError("nothing to render")
    if grade is not None:
        unknown = sorted(set(grade) - set(clips_by_label))
        if unknown:
            raise ValueError(f"Grade has cameras not in sync.json: {', '.join(unknown)}")
    fps_s = _out_fps(audio)
    rate = snap_frame_rate(audio.fps) if audio.fps > 0 else Fraction(30)
    edges = frame_grid(shots, audio.fps)
    parts_dir = Path(out).with_name(Path(out).stem + ".parts")
    look = lut_filter_chain(SourceColour(), None, fps_s)
    parts: list[list[str]] = []
    listing: list[str] = []
    for i, shot in enumerate(shots):
        dur = edges[i + 1] - edges[i]
        frames = round(Fraction(dur).limit_denominator(1_000_000) * rate)
        part = parts_dir / f"shot_{i:03d}.mp4"
        cmd = ["ffmpeg", "-v", "error", "-y"]
        if shot.camera == "black":
            cmd += ["-f", "lavfi", "-i", f"color=c=black:s={OUT_W}x{OUT_H}:r={fps_s}:d={dur + 1:.6f}"]
        else:
            clip_in = shot.clip_in_s + (edges[i] - shot.start_s)
            # a second of slack after the cut; -frames:v makes the exact length
            cmd += ["-ss", f"{clip_in:.6f}", "-t", f"{dur + 1:.6f}", "-i", clips_by_label[shot.camera].path]
        info = grade.get(shot.camera) if grade is not None else None
        vf = lut_filter_chain(info.colour, info.lut, fps_s) if info and info.lut else look
        cmd += ["-map", "0:v:0", "-vf", vf, "-frames:v", str(frames), "-an",
                "-c:v", "libx264", "-preset", "medium", "-crf", "16"]
        if grade is not None:
            # the join is stream copy, so part tags are the output tags
            cmd += ["-color_primaries", "bt709", "-color_trc", "bt709",
                    "-colorspace", "bt709", "-color_range", "tv"]
        cmd += [str(part)]
        parts.append(cmd)
        listing.append(f"file '{part.resolve().as_posix()}'")
    if kept_spans is None:
        span = edges[-1] - edges[0]
        audio_in = edges[0] - (audio.offset_s or 0.0)
        final = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(parts_dir / "parts.txt"),
                 "-ss", f"{audio_in:.6f}", "-t", f"{span:.6f}", "-i", audio.path,
                 "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                 "-movflags", "+faststart", str(out)]
        return parts, "\n".join(listing) + "\n", final
    for audio_in_s, duration_s, pos in _snapped_span_audio(shots, kept_spans, audio):
        parts.append(audio_part_command(
            audio.path, audio_in_s, duration_s,
            parts_dir / f"audio_{pos:03d}.m4a"))
    final = ["ffmpeg", "-v", "error", "-y",
             "-f", "concat", "-safe", "0", "-i", str(parts_dir / "parts.txt"),
             "-f", "concat", "-safe", "0", "-i", str(parts_dir / "audio_parts.txt"),
             "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
             "-movflags", "+faststart", str(out)]
    return parts, "\n".join(listing) + "\n", final


def render(shots: list[Shot], clips_by_label: dict[str, SyncClip],
           audio: SyncClip, out: Path,
           grade: dict[str, GradeShotInfo] | None = None,
           kept_spans: list[dict] | None = None) -> None:
    for shot in shots:
        if shot.camera == "black":
            continue
        clip = clips_by_label.get(shot.camera)
        if clip is None or not Path(clip.path).exists():
            raise ToolingError(f"Clip file missing for camera {shot.camera}")
    if not Path(audio.path).exists():
        raise ToolingError(f"Audio file missing: {audio.path}")
    parts, listing, final = render_commands(shots, clips_by_label, audio, out,
                                            grade=grade, kept_spans=kept_spans)
    parts_dir = Path(out).with_name(Path(out).stem + ".parts")
    parts_dir.mkdir(parents=True, exist_ok=True)
    for cmd in parts:
        run_command(cmd)
    (parts_dir / "parts.txt").write_text(listing, encoding="utf-8")
    if kept_spans is not None:
        audio_cmds = parts[len(shots):]
        audio_listing = "".join(
            f"file '{Path(cmd[-1]).resolve().as_posix()}'\n" for cmd in audio_cmds)
        (parts_dir / "audio_parts.txt").write_text(audio_listing, encoding="utf-8")
    run_command(final)


# ---------------------------------------------------------------- orchestrator

def run(*, sync_path: Path, words_path: Path, out_path: Path, audio_label: str | None,
        render_path: Path | None, min_shot_s: float, model: str, no_muse: bool,
        grade_path: Path | None = None,
        tighten_path: Path | None = None) -> dict:
    """Load, pick angles, check, optionally render, and write angles.json.

    With tighten_path, lines are built per kept span, shots are emitted on
    the output timeline (with their source times recorded), and the render
    cuts audio per kept span. Without it, behaviour is exactly as before.
    """
    sync_path, words_path, out_path = Path(sync_path), Path(words_path), Path(out_path)
    reference, clips = load_sync(sync_path)
    grade = load_grade(grade_path) if grade_path is not None else None
    if grade is not None:
        unknown = sorted(set(grade) - {c.label for c in clips})
        if unknown:
            raise ValueError(f"Grade has cameras not in {sync_path}: {', '.join(unknown)}")
    words = load_timed_words(words_path)
    by_label = {c.label: c for c in clips}
    if audio_label is not None:
        audio = by_label.get(audio_label)
        if audio is None:
            raise ValueError(f"Unknown --audio label: {audio_label}")
        if audio.offset_s is None and audio.path != reference:
            raise ValueError(f"Audio clip {audio_label} is not placed on the timeline")
    else:
        audio = next((c for c in clips if c.path == reference), None)
        if audio is None:
            audio = by_label.get("A")
        if audio is None and clips:
            audio = clips[0]
    if audio is None:
        raise ValueError("sync.json has no clips")
    tighten: dict | None = None
    if tighten_path is not None:
        tighten = cutlist.load_tighten(Path(tighten_path))
        tighten_audio = (tighten.get("audio") or {}).get("label")

        def _in_range(entry: object) -> bool:
            if not isinstance(entry, dict):
                return False
            try:
                start = int(entry.get("word_start", -1))
                end = int(entry.get("word_end", -1))
            except (TypeError, ValueError):
                return False
            return 0 <= start <= end <= len(words)

        utterances_ok = all(_in_range(u)
                            for u in tighten.get("utterances", []) or [])
        if tighten_audio != audio.label or not utterances_ok:
            raise ValueError(
                "tighten.json does not match this job's words/audio: "
                f"{tighten_path}")
    if tighten is None:
        lines = split_lines(words)
    else:
        lines = _tightened_lines(tighten, words)
    for line in lines:
        line.cameras = [c.label for c in covering(clips, line)]
    warnings: list[str] = []
    frames_dir = out_path.parent / "angle-frames"
    reference_label = next((c.label for c in clips if c.path == reference), "A")
    if not lines:
        warnings.append("no words: zero shots")
        shots: list[Shot] = []
        picks: list[Pick] = []
        spend = 0.0
        method = "rule"
    elif no_muse:
        picks = rule_picks(lines, clips, reference_label, min_shot_s)
        spend, method = 0.0, "rule"
        if tighten is not None:
            _apply_angle_overrides(tighten, lines, picks)
        shots = apply_checks(picks, lines, clips, min_shot_s=min_shot_s)
    else:
        try:
            from guestlist.meta import cache, spark  # noqa: F401 (availability probe; the call lives in pick_with_muse)
        except ImportError as error:
            warnings.append(f"muse unavailable: {error}")
            picks = rule_picks(lines, clips, reference_label, min_shot_s)
            spend, method = 0.0, "rule"
            if tighten is not None:
                _apply_angle_overrides(tighten, lines, picks)
            shots = apply_checks(picks, lines, clips, min_shot_s=min_shot_s)
        else:
            frames = grab_frames(lines, clips, frames_dir)
            picks, spend, muse_warnings = pick_with_muse(lines, clips, frames,
                                                         model=model, frames_dir=frames_dir)
            warnings.extend(muse_warnings)
            method = "muse" if any(p.method == "muse" for p in picks) else "rule"
            if tighten is not None:
                _apply_angle_overrides(tighten, lines, picks)
            shots = apply_checks(picks, lines, clips, min_shot_s=min_shot_s)
    if tighten is not None:
        shots = _map_shots_to_output(shots, lines, by_label, tighten)
    for line in lines:
        if not line.cameras:
            warnings.append(f"line L{line.id} has no covering camera")
    if grade is not None:
        for missing in sorted({s.camera for s in shots if s.camera != "black"} - set(grade)):
            warnings.append(f"grade_missing_camera:{missing}")
    if render_path is not None:
        if not shots:
            raise ValueError("nothing to render")
        render(shots, by_label, audio, Path(render_path), grade=grade,
               kept_spans=(tighten["kept_spans"] if tighten is not None
                           else None))

    def shot_lut(shot: Shot) -> str | None:
        info = grade.get(shot.camera) if grade is not None else None
        return info.lut.name if info and info.lut else None

    def shot_json(shot: Shot) -> dict:
        entry = {
            "id": shot.id, "start_s": round(shot.start_s, 3),
            "end_s": round(shot.end_s, 3),
            "camera": shot.camera, "clip_path": shot.clip_path,
            "clip_in_s": round(shot.clip_in_s, 3),
            "clip_out_s": round(shot.clip_out_s, 3),
            "line_ids": list(shot.line_ids), "shot_size": shot.shot_size,
            "reason": shot.reason, "method": shot.method,
            "overrules": list(shot.overrules), "lut": shot_lut(shot)}
        if tighten is not None:
            entry["source_start_s"] = round(shot.source_start_s, 3)
            entry["source_end_s"] = round(shot.source_end_s, 3)
            entry["kept_span_id"] = shot.kept_span_id
            entry["beat_id"] = shot.beat_id
        return entry

    payload = {
        "version": 1,
        "sync": str(sync_path),
        "words": str(words_path),
        "reference": reference,
        "grade": str(grade_path) if grade_path is not None else None,
        "audio": {"label": audio.label, "clip_path": audio.path,
                  "offset_s": None if audio.offset_s is None else round(audio.offset_s, 3)},
        "method": method,
        "model": model,
        "min_shot_s": round(min_shot_s, 3),
        "spend_usd": round(spend, 3),
        "warnings": warnings,
        "lines": [{"id": l.id, "start_s": round(l.start_s, 3), "end_s": round(l.end_s, 3),
                   "text": l.text, "cameras": list(l.cameras)} for l in lines],
        "shots": [shot_json(s) for s in shots],
    }
    if tighten is not None:
        assert tighten_path is not None
        kept = [s for s in tighten.get("kept_spans", []) or []]
        removed = [s for s in tighten.get("removed_spans", []) or []]
        payload["tighten"] = {
            "file": Path(tighten_path).name,
            "kept_s": round(sum(float(s.get("end_s", 0.0))
                                 - float(s.get("start_s", 0.0)) for s in kept), 3),
            "removed_s": round(sum(float(s.get("end_s", 0.0))
                                    - float(s.get("start_s", 0.0))
                                    for s in removed), 3),
            "beats": len(tighten.get("beats", []) or []),
        }
    write_json(out_path, payload)
    return payload
