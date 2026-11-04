"""A `director cut` job directory becomes an app project — and back.

`build_project` maps the job's JSON (cut/sync/words/tighten/angles,
voice notes) onto the `project.json` shape `app/ui/` already renders:
beats become sections, shots become talk clips, take picks fill the Why
panel, kept words become the Words tab, and voice/taste state rides
along. `rerender` re-runs `angles` over an edited `tighten.json` —
the same render `director cut` performs — so app edits and CLI re-runs
interleave freely.

Stdlib plus `ghostcut.ffmpeg_tools` only: this module must import under
the app's plain Python without `src/` on the path, so the source/output
mapping is re-derived from the JSON contracts. The one exception is
`rerender`, which adds `src/` to `sys.path` itself to reach
`director.angles`.

Designed by Muse Code on muse-wonder-1.0, built by Muse Code on
muse-spark-1.3 from docs/muse-code/VOICE-DESIGN.md.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from ghostcut.ffmpeg_tools import run_command

THUMB_EVERY_S = 2.0


class CutJobError(ValueError):
    """The directory is not a usable cut job, or the render cannot run."""


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _spans_of(tighten: dict | None) -> list[tuple[float, float]]:
    if not tighten:
        return []
    out = []
    for span in tighten.get("kept_spans", []) or []:
        if not isinstance(span, dict):
            continue
        try:
            out.append((float(span.get("start_s", 0.0)),
                        float(span.get("end_s", 0.0))))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def _source_to_output(spans: list[tuple[float, float]],
                      t_s: float) -> float | None:
    """Output time of a kept source moment; None when removed."""
    position = 0.0
    for start, end in spans:
        if start <= t_s <= end:
            return position + (t_s - start)
        position += end - start
    return None


def _source_to_output_or_edge(spans: list[tuple[float, float]],
                              t_s: float) -> float:
    """Output time, or the output edge where a removed moment would be."""
    mapped = _source_to_output(spans, t_s)
    if mapped is not None:
        return mapped
    position = 0.0
    for start, end in spans:
        if start > t_s:
            return position
        position += end - start
    return position


def _link_video(video: Path, out: Path) -> bool:
    """Symlink the job's render as video.mp4 (copy when that fails)."""
    link = out / "video.mp4"
    if link.is_symlink() or link.exists():
        link.unlink()
    try:
        link.symlink_to(os.path.abspath(video))
    except OSError:
        shutil.copy2(video, link)
    return True


def _stills(video: Path, out: Path) -> None:
    """Poster plus one thumbnail every THUMB_EVERY_S, like make_demo."""
    (out / "thumbs").mkdir(parents=True, exist_ok=True)
    run_command(["ffmpeg", "-v", "error", "-y", "-ss", "0.00", "-i",
                 str(video), "-frames:v", "1", "-q:v", "3",
                 str(out / "poster.jpg")])
    run_command(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf",
                 f"fps=1/{THUMB_EVERY_S:g},scale=192:108", "-q:v", "5",
                 str(out / "thumbs" / "%04d.jpg")])


def build_project(job_dir: Path, out_dir: Path) -> dict:
    """Cut job -> app/ui/demo project.json/project.js + media links.

    Returns the project dict. Raises CutJobError with a plain message
    before writing anything when the directory is not a cut job. A job
    whose render is missing still builds (reviewing takes and notes
    must not require the render); the player shows its empty state.
    """
    job_arg = str(job_dir)
    job_dir, out_dir = Path(job_dir), Path(out_dir)
    angles = _read(job_dir / "angles.json")
    if angles is None or not isinstance(angles.get("shots"), list):
        raise CutJobError(
            f"Not a Director cut job: {job_arg} has no angles.json "
            f"(run director cut there first)")
    shots = [s for s in angles["shots"] if isinstance(s, dict)]
    tighten = _read(job_dir / "tighten.json")
    if tighten is not None and not isinstance(
            tighten.get("beats"), list):
        tighten = None
    tightened = tighten is not None
    words_doc = _read(job_dir / "words.json") or {}
    raw_words = [w for w in words_doc.get("words", []) or []
                 if isinstance(w, dict)]
    cut = _read(job_dir / "cut.json")
    voice = _read(job_dir / "voice.json")

    duration = 0.0
    for shot in shots:
        try:
            duration = max(duration, float(shot.get("end_s", 0.0)))
        except (TypeError, ValueError):
            continue
    spans = _spans_of(tighten)

    beats = [b for b in (tighten.get("beats", []) if tighten else []) or []
             if isinstance(b, dict)]
    by_beat = {b.get("id"): b for b in beats}
    shots_of: dict[int, list[dict]] = {}
    for shot in shots:
        bid = shot.get("beat_id", -1)
        if isinstance(bid, int) and bid in by_beat:
            shots_of.setdefault(bid, []).append(shot)
    if tightened:
        sections = []
        for beat in beats:
            owned = shots_of.get(beat.get("id", -1), [])
            if not owned:
                continue
            try:
                start = min(float(s.get("start_s", 0.0)) for s in owned)
                end = max(float(s.get("end_s", 0.0)) for s in owned)
            except (TypeError, ValueError):
                continue
            sections.append({"name": str(beat.get("topic", "")),
                             "t": round(start, 3),
                             "d": round(end - start, 3)})
        if not sections:
            sections = [{"name": "Cut", "t": 0.0,
                         "d": round(duration, 3)}]
    else:
        try:
            first = min(float(s.get("start_s", 0.0)) for s in shots)
        except (TypeError, ValueError):
            first = 0.0
        sections = [{"name": "Cut", "t": round(first, 3),
                     "d": round(duration - first, 3)}]

    lines_by_id: dict[int, dict] = {}
    for line in angles.get("lines", []) or []:
        if isinstance(line, dict) and isinstance(line.get("id"), int):
            lines_by_id[line["id"]] = line
    talk = []
    for num, shot in enumerate(shots, 1):
        try:
            start, end = float(shot.get("start_s", 0.0)), float(
                shot.get("end_s", 0.0))
            clip_in = float(shot.get("clip_in_s", 0.0))
        except (TypeError, ValueError):
            continue
        camera = str(shot.get("camera", ""))
        bid = shot.get("beat_id", -1)
        beat = by_beat.get(bid) if isinstance(bid, int) else None
        if beat is not None:
            topic = str(beat.get("topic", ""))
            section = topic
        else:
            first_line = next(
                (lines_by_id[i] for i in shot.get("line_ids", []) or []
                 if i in lines_by_id), None)
            topic = str((first_line or {}).get("text", ""))[:80]
            section = sections[0]["name"] if sections else "Cut"
        talk.append({
            "id": f"t{num}", "t": round(start, 3),
            "d": round(end - start, 3),
            "src": f"camera {camera}" if camera != "black" else "black",
            "in": round(clip_in, 3), "topic": topic, "section": section,
            "ref": [shot.get("id")]})

    takes: list[dict] = []
    take_meta: dict = {}
    if tighten is not None:
        for beat in beats:
            attempts = [a for a in beat.get("attempts", []) or []
                        if isinstance(a, dict)]
            pick = beat.get("pick_attempt", -1)
            for pos, attempt in enumerate(attempts):
                is_pick = isinstance(pick, int) and pos == pick
                try:
                    start = float(attempt.get("start_s", 0.0))
                    end = float(attempt.get("end_s", 0.0))
                    conf = float(attempt.get("confidence", 0.0))
                except (TypeError, ValueError):
                    start, end, conf = 0.0, 0.0, 0.0
                takes.append({
                    "section": str(beat.get("topic", "")),
                    "kind": ("keeper_take" if is_pick
                             else str(attempt.get("kind", ""))),
                    "topic": str(beat.get("topic", "")),
                    "group": f"B{beat.get('id')}",
                    "aid": attempt.get("id"),
                    "pick": is_pick,
                    "reason": str(attempt.get("reason", "")),
                    "conf": round(min(1.0, max(0.0, conf)), 4),
                    "clip": f"B{beat.get('id')}",
                    "in": round(start, 3), "d": round(end - start, 3),
                    "text": str(attempt.get("text", ""))[:400]})
        take_meta = {"model": tighten.get("model"),
                     "method": tighten.get("method"),
                     "usd": tighten.get("spend_usd")}

    words: list[tuple[str, float, float]] = []
    for entry in raw_words:
        try:
            text = str(entry.get("word") or entry.get("text") or "")
            start, end = float(entry.get("start", 0.0)), float(
                entry.get("end", 0.0))
        except (TypeError, ValueError):
            continue
        if not text.strip():
            continue
        words.append((text.strip(), start, max(end, start)))
    words.sort(key=lambda w: w[1])
    kept: list[tuple[str, float, float, float, float]] = []
    for text, start, end in words:
        if not tightened:
            kept.append((text, start, end, start, end))
            continue
        out_start = _source_to_output(spans, start)
        out_end = _source_to_output(spans, min(end, start + max(
            0.0, end - start)))
        if out_start is None:
            continue
        kept.append((text, start, end, out_start,
                     out_start + max(0.0, end - start)))
    transcript = []
    ordered_lines = sorted(
        lines_by_id.values(),
        key=lambda l: float(l.get("start_s", 0.0) or 0.0))
    pos = 0
    for line in ordered_lines:
        try:
            line_end = float(line.get("end_s", 0.0))
        except (TypeError, ValueError):
            continue
        group = []
        while pos < len(kept) and kept[pos][1] <= line_end + 1e-9:
            group.append(kept[pos])
            pos += 1
        if not group:
            continue
        transcript.append({
            "t": round(group[0][3], 2), "e": round(group[-1][4], 2),
            "text": " ".join(w[0] for w in group),
            "w": [[w[0], round(w[3], 2), round(w[4], 2)]
                  for w in group]})
    if pos < len(kept):
        # Words past the last line (a hand-run angles file): keep them.
        group = kept[pos:]
        transcript.append({
            "t": round(group[0][3], 2), "e": round(group[-1][4], 2),
            "text": " ".join(w[0] for w in group),
            "w": [[w[0], round(w[3], 2), round(w[4], 2)]
                  for w in group]})

    findings = []
    if tighten is not None:
        for note in tighten.get("editor_notes", []) or []:
            if not isinstance(note, dict):
                continue
            text = str(note.get("text", ""))
            try:
                at = float(note.get("at_s", 0.0))
            except (TypeError, ValueError):
                at = 0.0
            findings.append({
                "t": round(_source_to_output_or_edge(spans, at), 3),
                "src": "you", "state": "open", "title": text[:70],
                "body": text, "check": "Voice note"})
    if voice is not None:
        for note in voice.get("notes", []) or []:
            if not isinstance(note, dict):
                continue
            if note.get("status") != "unresolved":
                continue
            raw = str(note.get("raw_text", ""))
            heard = str(note.get("heard", ""))
            message = str(note.get("message", ""))
            try:
                at = note.get("at_s")
                at = float(at) if at is not None else 0.0
            except (TypeError, ValueError):
                at = 0.0
            body = raw + (f" — {message}" if message else "")
            findings.append({
                "t": round(at, 3), "src": "you", "state": "open",
                "title": (heard or raw)[:70], "body": body,
                "check": "Voice note"})

    if cut is not None:
        stages = cut.get("stages", {}) or {}
        rows = [[name, 1, round(float((record or {}).get("spend_usd")
                                     or 0.0), 4)]
                for name, record in stages.items()]
        try:
            total = round(float(cut.get("spend_usd") or 0.0), 4)
        except (TypeError, ValueError):
            total = round(sum(row[2] for row in rows), 4)
        spend = {"total": total, "rows": rows}
        title = (cut.get("project_name") or cut.get("name")
                 or (cut.get("export") or {}).get("project_name")
                 or "Director cut")
    else:
        try:
            total = round(float(angles.get("spend_usd") or 0.0), 4)
        except (TypeError, ValueError):
            total = 0.0
        spend = {"total": total, "rows": [["angles", 1, total]]}
        title = "Director cut"

    project = {
        "title": str(title), "client": "", "shootDate": "",
        "duration": round(duration, 3),
        "sections": sections, "talk": talk, "broll": [], "cards": [],
        "takes": takes, "takeMeta": take_meta, "library": [],
        "transcript": transcript, "findings": findings,
        "spend": spend, "env": {}, "envStep": 0.5,
        "cutJob": {"dir": job_arg, "tightened": tightened},
    }
    # Loudness Director's QC measured on cut.mp4, for the Deliver page.
    for check in ((cut or {}).get("qc") or {}).get("checks") or []:
        detail = check.get("detail") if isinstance(check, dict) else None
        if isinstance(detail, dict) and "integrated_lufs" in detail:
            project["loudness"] = {"lufs": detail["integrated_lufs"],
                                   "tp": detail.get("true_peak_dbtp"),
                                   "measured": "Director's QC on cut.mp4"}
            break

    out_dir.mkdir(parents=True, exist_ok=True)
    render = job_dir / "cut.mp4"
    if render.exists():
        _link_video(render, out_dir)
        _stills(render, out_dir)
    else:
        stale = out_dir / "video.mp4"
        if stale.is_symlink() or stale.exists():
            stale.unlink()
    _dress_project(job_dir, out_dir, project)  # DRESS
    (out_dir / "project.json").write_text(json.dumps(project))
    (out_dir / "project.js").write_text(
        "window.PROJECT = " + json.dumps(project) + ";\n")
    return project


def _dress_project(job_dir: Path, out_dir: Path, project: dict) -> None:
    """DRESS: link a fresh cut.dressed.mp4 and show the dress cards.

    `demo/video.mp4` stays the plain cut; a fresh dressed render is
    linked beside it as `demo/dressed.mp4` (a stale one is ignored and
    its old link removed). Skipped when director.dress cannot import.
    """
    link = out_dir / "dressed.mp4"
    root = Path(__file__).resolve().parents[1]
    src = str(root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        from director import dress as dress_mod
    except ImportError:
        return
    try:
        summary = dress_mod.summary(job_dir)
    except Exception:  # noqa: BLE001 - a broken dress.json never blocks the project
        return
    if summary.get("fresh"):
        if link.is_symlink() or link.exists():
            link.unlink()
        try:
            link.symlink_to(os.path.abspath(job_dir / "cut.dressed.mp4"))
        except OSError:
            shutil.copy2(job_dir / "cut.dressed.mp4", link)
        summary["video"] = "demo/dressed.mp4"
    elif link.is_symlink() or link.exists():
        link.unlink()
    if not summary.get("exists"):
        return  # never dressed: the project stays exactly as before
    project["cutJob"]["dress"] = summary
    project["cards"] = [
        # caption: muse.context and muse.validate index it (review fix).
        {"id": c["id"], "t": c["t"], "d": c["d"], "lines": [c["text"]], "caption": ""}
        for c in summary.get("cards", [])
        if c.get("enabled", True) and c.get("t") is not None]


def require_director_angles():  # type: ignore[no-untyped-def]
    """`director.angles`, with `src/` put on the path here."""
    root = Path(__file__).resolve().parents[1]
    src = str(root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        from director import angles as angles_mod
    except ImportError as error:
        raise CutJobError(
            "Re-render needs the Director package: run the engine from "
            "the repo venv") from error
    return angles_mod


def rerender(job_dir: Path, demo: Path | None) -> dict:
    """Angles re-run over the edited tighten.json -> new cut.mp4.

    Tightened jobs re-run `director.angles.run` with the job's own
    settings; untightened jobs re-render the current angles.json shots
    as they stand (no re-pick, so editor angle choices survive). On
    success cut.next.mp4 replaces cut.mp4 (the old file moves to
    cut.prev.mp4, one generation kept); on failure cut.mp4 is untouched
    and the .next file is discarded. With `demo` given, the app
    project rebuilds from the new angles. Returns {"duration_s",
    "cut_mp4", "prev_mp4", "shots", "tightened"}.
    """
    job_dir = Path(job_dir)
    angles_mod = require_director_angles()
    angles = _read(job_dir / "angles.json")
    if angles is None or not isinstance(angles.get("shots"), list):
        raise CutJobError(
            f"Not a Director cut job: {job_dir} has no angles.json "
            f"(run director cut there first)")
    tighten_path = job_dir / "tighten.json"
    tightened = tighten_path.exists()
    audio_label = (angles.get("audio") or {}).get("label")
    try:
        min_shot_s = float(angles.get("min_shot_s", 2.0))
    except (TypeError, ValueError):
        min_shot_s = 2.0
    try:
        push_in = float(angles.get("push_in", 0.0))
    except (TypeError, ValueError):
        push_in = 0.0
    model = str(angles.get("model") or "muse-spark-1.3")
    grade_path = None
    declared = angles.get("grade")
    if declared and Path(declared).exists():
        grade_path = Path(declared)
    elif (job_dir / "grade.json").exists():
        grade_path = job_dir / "grade.json"
    nxt = job_dir / "cut.next.mp4"
    try:
        if tightened:
            payload = angles_mod.run(
                sync_path=job_dir / "sync.json",
                words_path=job_dir / "words.json",
                out_path=job_dir / "angles.json",
                audio_label=audio_label, render_path=nxt,
                min_shot_s=min_shot_s, model=model, no_muse=False,
                grade_path=grade_path, tighten_path=tighten_path,
                push_in=push_in)
            shots = payload.get("shots", [])
        else:
            _render_untightened(angles_mod, job_dir, audio_label,
                                grade_path, nxt, push_in)
            shots = angles.get("shots", [])
    except Exception:
        nxt.unlink(missing_ok=True)
        raise
    prev = job_dir / "cut.prev.mp4"
    current = job_dir / "cut.mp4"
    kept_prev: str | None = None
    if current.exists():
        prev.unlink(missing_ok=True)
        current.rename(prev)
        kept_prev = prev.name
    nxt.rename(current)
    if demo is not None:
        build_project(job_dir, Path(demo))
    ends = []
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        try:
            ends.append(float(shot.get("end_s", 0.0)))
        except (TypeError, ValueError):
            continue
    return {"duration_s": round(max(ends) if ends else 0.0, 3),
            "cut_mp4": current.name, "prev_mp4": kept_prev,
            "shots": len(shots), "tightened": tightened}


def _render_untightened(angles_mod, job_dir: Path,  # type: ignore[no-untyped-def]
                        audio_label: str | None, grade_path: Path | None,
                        out: Path, push_in: float = 0.0) -> None:
    """Re-render the current angles.json shots as they stand."""
    reference, clips = angles_mod.load_sync(job_dir / "sync.json")
    by_label = {c.label: c for c in clips}
    if audio_label is not None:
        audio = by_label.get(audio_label)
        if audio is None:
            raise ValueError(f"Unknown --audio label: {audio_label}")
    else:
        audio = next((c for c in clips if c.path == reference), None)
        if audio is None:
            audio = by_label.get("A")
        if audio is None and clips:
            audio = clips[0]
    if audio is None:
        raise ValueError("sync.json has no clips")
    angles = _read(job_dir / "angles.json") or {}
    shots = []
    for entry in angles.get("shots", []) or []:
        if not isinstance(entry, dict):
            continue
        shots.append(angles_mod.Shot(
            id=int(entry.get("id", 0)),
            start_s=float(entry.get("start_s", 0.0)),
            end_s=float(entry.get("end_s", 0.0)),
            camera=str(entry.get("camera", "")),
            clip_path=str(entry.get("clip_path", "")),
            clip_in_s=float(entry.get("clip_in_s", 0.0)),
            clip_out_s=float(entry.get("clip_out_s", 0.0)),
            line_ids=list(entry.get("line_ids", []) or []),
            shot_size=str(entry.get("shot_size", "")),
            reason=str(entry.get("reason", "")),
            method=str(entry.get("method", "")),
            overrules=list(entry.get("overrules", []) or [])))
    if not shots:
        raise ValueError("nothing to render")
    grade = (angles_mod.load_grade(grade_path)
             if grade_path is not None else None)
    angles_mod.render(shots, by_label, audio, out, grade=grade,
                      kept_spans=None, push_in=push_in)
