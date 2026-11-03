"""Editable timelines for a Director cut: angles.json shots to FCPXML and OTIO.

Pure mapping from the shots `angles` already decided onto the installed
`ghostcut` exporters' data classes. It makes no editorial decision: every
cut in the timeline is an `angles` shot, at the same time, from the same
camera. Black spans (no camera covered them) become gaps in both formats.

Designed by Muse Code on muse-wonder-1.0, built by Muse Code on
muse-spark-1.3 from docs/muse-code/OPEN-DESIGN.md.
"""
from __future__ import annotations

from pathlib import Path

from ghostcut.config import read_json
from ghostcut.export_fcpxml import export_fcpxml
from ghostcut.export_otio import export_otio
from ghostcut.models import MediaClip, TimelineSegment

SECTION = "Director cut"


class ExportError(RuntimeError):
    """The angles/sync/grade inputs cannot be mapped to a timeline."""


def load_angles(path: Path) -> dict:
    """angles.json payload; anything else is an ExportError."""
    data = read_json(Path(path))
    if not isinstance(data, dict) or data.get("version") != 1 \
            or not isinstance(data.get("shots"), list):
        raise ExportError(f"Not an angles.json: {path}")
    return data


def sync_clips_by_label(path: Path) -> dict[str, dict]:
    """sync.json clips keyed by label; duplicates are an ExportError."""
    data = read_json(Path(path))
    if not isinstance(data, dict) or not isinstance(data.get("clips"), list):
        raise ExportError(f"Not a sync.json: {path}")
    out: dict[str, dict] = {}
    for item in data["clips"]:
        label = str(item.get("label") or "")
        if label in out:
            raise ExportError(f"Duplicate label {label} in {path}")
        out[label] = item
    return out


def _grade_luts(grade_path: Path | None) -> dict[str, str | None]:
    """Cube file name by camera label, {} when no grade was run."""
    if grade_path is None:
        return {}
    data = read_json(Path(grade_path))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ExportError(f"Not a grade.json: {grade_path}")
    cameras = data.get("cameras", [])
    if not isinstance(cameras, list):
        cameras = []
    luts: dict[str, str | None] = {}
    for item in cameras:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "")
        lut = item.get("lut")
        luts[label] = None if lut is None else str(lut)
    return luts


def build_media_clips(sync_payload: dict, used_labels: list[str]) -> list[MediaClip]:
    """One MediaClip per used label, in used_labels order. Pure."""
    by_label: dict[str, dict] = {}
    for item in sync_payload.get("clips", []):
        if isinstance(item, dict):
            by_label[str(item.get("label") or "")] = item
    clips: list[MediaClip] = []
    for label in used_labels:
        if label not in by_label:
            raise ExportError(f"angles camera {label} is not in sync.json")
        cam = by_label[label].get("camera") or {}
        clips.append(MediaClip(
            id=label,
            source_path=str(cam.get("path") or ""),
            proxy_path="",
            audio_path="",
            duration_s=float(cam.get("duration_s") or 0.0),
            fps=float(cam.get("fps") or 0.0),
            has_audio=bool(cam.get("has_audio")),
            width=int(cam.get("width") or 0),
            height=int(cam.get("height") or 0),
            layer="a-roll",
        ))
    return clips


def build_segments(angles_payload: dict) -> tuple[list[TimelineSegment], list[dict], list[str]]:
    """(segments, gaps, warnings) from angles shots. Pure.

    Exported time 0 is the first shot's timeline start, so the timeline
    and the rendered master share an origin; black shots become gaps.
    """
    shots = angles_payload.get("shots", [])
    t0 = float(shots[0].get("start_s", 0.0)) if shots else 0.0
    segments: list[TimelineSegment] = []
    gaps: list[dict] = []
    for shot in shots:
        start_s = float(shot.get("start_s", 0.0))
        end_s = float(shot.get("end_s", start_s))
        camera = str(shot.get("camera") or "")
        if camera == "black":
            gaps.append({
                "start_s": round(start_s - t0, 3),
                "end_s": round(end_s - t0, 3),
                "line_ids": list(shot.get("line_ids", [])),
            })
        else:
            segments.append(TimelineSegment(
                section=SECTION,
                clip_id=camera,
                src_in_s=float(shot.get("clip_in_s", 0.0)),
                src_out_s=float(shot.get("clip_out_s", 0.0)),
                timeline_in_s=round(start_s - t0, 3),
            ))
    warnings: list[str] = []
    audio = angles_payload.get("audio") or {}
    audio_label = audio.get("label") if isinstance(audio, dict) else None
    if audio_label and not any(seg.clip_id == audio_label for seg in segments):
        warnings.append(f"audio_clip_not_in_timeline:{audio_label}")
    return segments, gaps, warnings


def finish_stems(finish_path: Path | None) -> list[str]:
    """Music/effect stem file names from finish.json, in id order. Pure-ish.

    [] when no finish ran, the file is missing, or it lists no stems.
    Voice isolation is the voice, not a stem, and is never listed.
    """
    if finish_path is None:
        return []
    try:
        data = read_json(Path(finish_path))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or not isinstance(data.get("assets"), list):
        return []
    stems = [str(asset.get("file", ""))
             for asset in data["assets"]
             if isinstance(asset, dict)
             and asset.get("kind") in ("music", "riser", "whoosh", "impact")
             and str(asset.get("file", ""))]
    return stems


def export_timelines(*, angles_path: Path, sync_path: Path, grade_path: Path | None,
                     fcpxml_path: Path, otio_path: Path, project_name: str,
                     finish_path: Path | None = None) -> dict:
    """Write both timelines; return the cut.json export section."""
    angles_payload = load_angles(Path(angles_path))
    sync_payload = read_json(Path(sync_path))
    if not isinstance(sync_payload, dict) or not isinstance(sync_payload.get("clips"), list):
        raise ExportError(f"Not a sync.json: {sync_path}")
    by_label = sync_clips_by_label(Path(sync_path))
    shots = angles_payload.get("shots", [])
    used_labels = sorted({str(s.get("camera") or "") for s in shots
                          if str(s.get("camera") or "") != "black"})
    for label in used_labels:
        if label not in by_label:
            raise ExportError(f"angles camera {label} is not in sync.json: {sync_path}")
    clips = build_media_clips(sync_payload, used_labels)
    segments, gaps, warnings = build_segments(angles_payload)
    luts: dict[str, str | None] = {}
    if grade_path is not None:
        grade_luts = _grade_luts(Path(grade_path))
        luts = {label: grade_luts.get(label) for label in used_labels}
        warnings.append("timelines_reference_original_media")
        for label in used_labels:
            if luts[label] is not None:
                warnings.append(f"lut:{label}:{luts[label]}")
    # FCPXML first: Final Cut Pro is the most likely finishing tool, and if
    # only one write can succeed the .fcpxml is the one the demo opens.
    export_fcpxml(clips, segments, Path(fcpxml_path), project_name=project_name)
    export_otio(clips, segments, Path(otio_path), project_name=project_name)
    section: dict = {
        "fcpxml": Path(fcpxml_path).name,
        "otio": Path(otio_path).name,
        "project_name": project_name,
        "shots_exported": len(segments),
        "gaps": gaps,
        "clips_used": used_labels,
        "luts": luts,
        "warnings": warnings,
    }
    if finish_path is not None and Path(finish_path).exists():
        # The installed exporters have no audio lanes: the timelines keep
        # referencing the camera originals byte-identically, and the stems
        # are listed here for the editor to re-lay (FINISH-DESIGN §1.3).
        stems = finish_stems(Path(finish_path))
        section["finish_stems"] = stems
        if stems:
            warnings.append(f"finish_audio_not_in_timelines:{','.join(stems)}")
    return section
