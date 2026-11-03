"""Speaker gaze: cut to the camera the speaker is addressing.

A talking head turns to the camera it is addressing, so the addressed
camera sees a near-frontal face (small Vision `yaw`) while the others see
a turned head. `angles` samples a few frames per spoken line per covering
camera through the camera's LUT, reads yaw per frame with the lips helper,
and when exactly one camera sees the speaker addressed for most of the
line a mechanical rule picks it that Muse Spark cannot overrule. On
ambiguous lines Spark decides as before, with the gaze numbers in its
prompt. Like the lips method this needs macOS with Apple Vision; without
a helper every line simply has no gaze evidence and behaviour is as
before.
"""
from __future__ import annotations

import shutil
import statistics
from dataclasses import dataclass, field
from pathlib import Path

YAW_ADDRESSED_DEG = 18.0
SAMPLES_PER_LINE = 3
FRAME_W = 640


@dataclass(slots=True)
class CameraGaze:
    label: str
    yaws: list[float | None]  # per sampled frame, None when no face
    addressed: int  # samples with |yaw| under the threshold
    samples: int  # frames successfully analysed
    median_abs_yaw: float | None  # over the samples with a face


@dataclass(slots=True)
class LineGaze:
    line_id: int
    cameras: dict[str, CameraGaze] = field(default_factory=dict)
    pick: str | None = None  # the addressed camera, when exactly one
    reason: str = ""


def is_addressed(yaw: float | None,
                 threshold: float = YAW_ADDRESSED_DEG) -> bool:
    """A frame is addressed to a camera when the face is near-frontal to it. Pure."""
    return yaw is not None and abs(yaw) <= threshold


def median_abs_yaw(yaws: list[float | None]) -> float | None:
    """Median |yaw| over the samples with a face; None when there are none. Pure."""
    seen = sorted(abs(y) for y in yaws if y is not None)
    if not seen:
        return None
    return float(statistics.median(seen))


def summarize_camera(label: str, yaws: list[float | None],
                     threshold: float = YAW_ADDRESSED_DEG) -> CameraGaze:
    """Per-camera evidence from one line's yaw samples. Pure."""
    return CameraGaze(
        label=label,
        yaws=list(yaws),
        addressed=sum(1 for y in yaws if is_addressed(y, threshold)),
        samples=len(yaws),
        median_abs_yaw=median_abs_yaw(yaws),
    )


def decide_addressed_camera(
        per_camera: dict[str, list[float | None]],
        threshold: float = YAW_ADDRESSED_DEG) -> tuple[str | None, str]:
    """(winner, reason): exactly one majority-addressed camera wins. Pure.

    A camera is addressed for the line when most of its sampled frames are
    addressed to it. Anything else — no faces, no majority, two majorities —
    leaves the line to Muse Spark with the numbers in its prompt.
    """
    summaries = {label: summarize_camera(label, yaws, threshold)
                 for label, yaws in per_camera.items()}
    winners = [label for label, gaze in summaries.items()
               if gaze.samples > 0 and gaze.addressed > gaze.samples / 2]
    if len(winners) == 1:
        winner = winners[0]
        median = summaries[winner].median_abs_yaw or 0.0
        others = []
        for label in sorted(summaries):
            if label == winner:
                continue
            other = summaries[label].median_abs_yaw
            others.append(f"{other:.0f}° on {label}" if other is not None
                          else f"no face on {label}")
        vs = f" vs {', '.join(others)}" if others else ""
        return winner, (f"speaker addresses camera {winner}: "
                        f"|yaw| {median:.0f}°{vs}")
    if not any(gaze.median_abs_yaw is not None for gaze in summaries.values()):
        return None, "no face detected on any camera"
    detail = ", ".join(f"{label} {summaries[label].addressed}/"
                       f"{summaries[label].samples} frontal"
                       for label in sorted(summaries))
    return None, f"gaze ambiguous: {detail}"


def build_line_gaze(line_id: int, per_camera: dict[str, list[float | None]],
                    threshold: float = YAW_ADDRESSED_DEG) -> LineGaze:
    """Evidence plus the addressed-camera decision for one line. Pure."""
    gaze = LineGaze(line_id=line_id)
    gaze.cameras = {label: summarize_camera(label, yaws, threshold)
                    for label, yaws in per_camera.items()}
    gaze.pick, gaze.reason = decide_addressed_camera(per_camera, threshold)
    return gaze


def format_gaze_note(gaze: LineGaze) -> str:
    """One line's per-camera gaze numbers for the Muse prompt. Pure."""
    parts = []
    for label in sorted(gaze.cameras):
        cam = gaze.cameras[label]
        if cam.median_abs_yaw is None:
            parts.append(f"{label} no face ({cam.addressed}/{cam.samples} frontal)")
        else:
            parts.append(f"{label} |yaw| {cam.median_abs_yaw:.0f}° "
                         f"({cam.addressed}/{cam.samples} frontal)")
    return "; ".join(parts)


def line_gaze_json(gaze: LineGaze) -> dict:
    """The `angles.json` per-line gaze evidence. Pure."""
    return {
        label: {
            "yaw_deg": [None if y is None else round(y, 2) for y in cam.yaws],
            "addressed": cam.addressed,
            "samples": cam.samples,
            "median_abs_yaw": (None if cam.median_abs_yaw is None
                               else round(cam.median_abs_yaw, 2)),
        }
        for label, cam in sorted(gaze.cameras.items())
    } | {"pick": gaze.pick, "reason": gaze.reason}


def line_sample_times(start_s: float, end_s: float,
                      n: int = SAMPLES_PER_LINE) -> list[float]:
    """n evenly spaced sampling times across the line. Pure."""
    if n <= 0 or end_s <= start_s:
        return []
    return [start_s + (i + 0.5) / n * (end_s - start_s) for i in range(n)]


def gaze_frame_command(clip_path: str, at_s: float, out: Path,
                       lut: Path | None) -> list[str]:
    """One gaze still through the camera's LUT (as the lips frames do)."""
    if lut is not None and ("'" in str(lut) or ":" in str(lut)):
        raise ValueError(f"LUT path cannot contain ' or :: {lut}")
    scale = f"scale={FRAME_W}:-2"
    vf = f"lut3d=file='{lut}':interp=tetrahedral,{scale}" if lut is not None else scale
    return ["ffmpeg", "-v", "error", "-ss", f"{at_s:.3f}", "-i", clip_path,
            "-frames:v", "1", "-vf", vf, "-q:v", "4", "-y", str(out)]


def gaze_frame_name(line_id: int, label: str, sample: int) -> str:
    return f"G{line_id:03d}_{label}_{sample}.jpg"


def sample_line_gaze(lines, clips_by_label: dict,
                     luts: dict[str, Path | None],
                     frames_dir: Path) -> dict[int, LineGaze]:
    """Per-line gaze evidence, sampling a few frames per line per camera.

    Frames go to `frames_dir` (kept, like the angle frames) with the
    camera's LUT applied, and one helper run reads them all. Anything that
    stops the sampling — no ffmpeg, no helper, no covering cameras — gives
    no gaze evidence rather than raising; per-frame failures only shrink
    that camera's sample count. Never raises.
    """
    try:
        from ghostcut.ffmpeg_tools import run_command

        from .sync import build_lips_helper, run_lips_helper
    except ImportError:
        return {}
    coverable = [line for line in lines if getattr(line, "cameras", None)]
    if not coverable or shutil.which("ffmpeg") is None:
        return {}
    try:
        helper, _ = build_lips_helper()
    except Exception:
        return {}
    if helper is None:
        return {}
    try:
        Path(frames_dir).mkdir(parents=True, exist_ok=True)
    except OSError:
        return {}
    wanted: dict[str, tuple[int, str]] = {}
    for line in coverable:
        for label in sorted(line.cameras):
            clip = clips_by_label.get(label)
            offset = getattr(clip, "offset_s", None) if clip is not None else None
            path = getattr(clip, "path", "") if clip is not None else ""
            if clip is None or offset is None or not path:
                continue
            lut = luts.get(label)
            for k, at in enumerate(line_sample_times(line.start_s, line.end_s)):
                name = gaze_frame_name(line.id, label, k)
                out = Path(frames_dir) / name
                if not (out.exists() and out.stat().st_size > 0):
                    try:
                        run_command(gaze_frame_command(path, at - offset, out, lut))
                    except Exception:
                        continue
                if out.exists() and out.stat().st_size > 0:
                    wanted[name] = (line.id, label)
    if not wanted:
        return {}
    try:
        found = run_lips_helper(helper, Path(frames_dir))
    except Exception:
        return {}
    if found is None:
        return {}
    try:
        ordered = sorted(p.name for p in Path(frames_dir).iterdir()
                         if not p.name.startswith(".")
                         and p.suffix.lower() in (".jpg", ".jpeg"))
    except OSError:
        return {}
    by_index = {frame.index: frame for frame in found}
    per_line: dict[int, dict[str, list[float | None]]] = {}
    for pos, name in enumerate(ordered):
        if name not in wanted:
            continue
        line_id, label = wanted[name]
        frame = by_index.get(pos)
        per_line.setdefault(line_id, {}).setdefault(label, []).append(
            frame.yaw if frame is not None else None)
    out: dict[int, LineGaze] = {}
    for line in coverable:
        cams = per_line.get(line.id, {})
        if not any(cams.get(label) for label in line.cameras):
            continue
        out[line.id] = build_line_gaze(
            line.id, {label: cams.get(label, []) for label in line.cameras})
    return out
