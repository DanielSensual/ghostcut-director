"""Speaker gaze: cut to the camera the speaker is addressing.

A talking head turns to the camera it is addressing, so the addressed
camera sees a near-frontal face (small effective Vision `yaw`) while the
others see a turned head. `angles` samples frames densely (about one
every 0.5 s, capped per line) per covering camera through the camera's
LUT, reads yaw per frame with the lips helper, and when exactly one
camera sees the speaker addressed for most of the line a mechanical rule
picks it that Muse Spark cannot overrule. A turn of address inside a
line, held for at least `GAZE_HOLD_S`, becomes a gaze segment; `angles`
turns a change of segment into a cut on the nearest word gap. On
ambiguous lines Spark decides as before, with the gaze numbers in its
prompt. The effective yaw is the helper's continuous landmark estimate
when it reports one, with the coarse Vision yaw as the fallback. Like
the lips method this needs macOS with Apple Vision; without a helper
every line simply has no gaze evidence and behaviour is as before.
"""
from __future__ import annotations

import math
import shutil
import statistics
from dataclasses import dataclass, field
from pathlib import Path

YAW_ADDRESSED_DEG = 18.0
SAMPLES_PER_LINE = 3
DENSE_SAMPLE_INTERVAL_S = 0.5
MAX_SAMPLES_PER_LINE = 16
GAZE_HOLD_S = 1.0
FRAME_W = 640


@dataclass(slots=True)
class CameraGaze:
    label: str
    yaws: list[float | None]  # effective yaw per sample, None when no face
    addressed: int  # samples with |yaw| under the threshold
    samples: int  # frames successfully analysed
    median_abs_yaw: float | None  # over the samples with a face
    coarse_yaws: list[float | None] = field(default_factory=list)
    continuous_yaws: list[float | None] = field(default_factory=list)
    sample_times_s: list[float] = field(default_factory=list)


@dataclass(slots=True)
class GazeSegment:
    """One held address to one camera inside a line. Pure."""

    line_id: int
    camera: str
    start_s: float
    end_s: float
    reason: str = ""

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)


@dataclass(slots=True)
class LineGaze:
    line_id: int
    cameras: dict[str, CameraGaze] = field(default_factory=dict)
    pick: str | None = None  # the addressed camera, when exactly one
    reason: str = ""
    sample_times_s: list[float] = field(default_factory=list)
    segments: list[GazeSegment] = field(default_factory=list)


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


def effective_yaw(coarse: float | None,
                  continuous: float | None) -> float | None:
    """The yaw used for gaze decisions: continuous when present, else coarse. Pure."""
    return continuous if continuous is not None else coarse


def summarize_camera(label: str, yaws: list[float | None],
                     threshold: float = YAW_ADDRESSED_DEG,
                     *,
                     coarse_yaws: list[float | None] | None = None,
                     continuous_yaws: list[float | None] | None = None,
                     sample_times_s: list[float] | None = None) -> CameraGaze:
    """Per-camera evidence from one line's yaw samples. Pure."""
    return CameraGaze(
        label=label,
        yaws=list(yaws),
        addressed=sum(1 for y in yaws if is_addressed(y, threshold)),
        samples=len(yaws),
        median_abs_yaw=median_abs_yaw(yaws),
        coarse_yaws=list(coarse_yaws) if coarse_yaws is not None else list(yaws),
        continuous_yaws=(list(continuous_yaws) if continuous_yaws is not None
                         else [None] * len(yaws)),
        sample_times_s=list(sample_times_s) if sample_times_s is not None else [],
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
                    threshold: float = YAW_ADDRESSED_DEG,
                    *,
                    sample_times_s: list[float] | None = None,
                    start_s: float | None = None,
                    end_s: float | None = None,
                    coarse_per_camera: dict[str, list[float | None]] | None = None,
                    continuous_per_camera: dict[str, list[float | None]] | None = None,
                    ) -> LineGaze:
    """Evidence plus the addressed-camera decision for one line. Pure.

    `per_camera` holds the effective yaws (continuous when the helper
    reported one, else coarse). The coarse/continuous lists are kept
    only as evidence. When the line's span is given, held gaze segments
    are computed as well.
    """
    times = list(sample_times_s) if sample_times_s is not None else []
    gaze = LineGaze(line_id=line_id, sample_times_s=times)
    gaze.cameras = {
        label: summarize_camera(
            label, yaws, threshold,
            coarse_yaws=(coarse_per_camera or {}).get(label),
            continuous_yaws=(continuous_per_camera or {}).get(label),
            sample_times_s=times)
        for label, yaws in per_camera.items()}
    gaze.pick, gaze.reason = decide_addressed_camera(per_camera, threshold)
    if start_s is not None and end_s is not None and times:
        gaze.segments = gaze_segments_for_line(
            line_id, start_s, end_s, times, per_camera, threshold=threshold)
    return gaze


def addressed_camera_at_sample(
        sample_yaws: dict[str, float | None],
        threshold: float = YAW_ADDRESSED_DEG) -> str | None:
    """The one camera addressed in a single sample, else None. Pure."""
    winners = [label for label, yaw in sample_yaws.items()
               if is_addressed(yaw, threshold)]
    return winners[0] if len(winners) == 1 else None


def _sample_edges(start_s: float, end_s: float,
                  sample_times: list[float]) -> list[float]:
    edges = [float(start_s)]
    for first, second in zip(sample_times, sample_times[1:]):
        edges.append((float(first) + float(second)) / 2)
    edges.append(float(end_s))
    return edges


def addressed_durations(start_s: float, end_s: float,
                        sample_times: list[float],
                        per_camera: dict[str, list[float | None]],
                        threshold: float = YAW_ADDRESSED_DEG) -> dict[str, float]:
    """Seconds addressed to each camera, sample-weighted by duration. Pure.

    Each sample owns the interval halfway to its neighbours (clipped to
    the line). Only samples with exactly one addressed camera count.
    """
    if not sample_times or end_s <= start_s:
        return {}
    edges = _sample_edges(start_s, end_s, sample_times)
    totals: dict[str, float] = {}
    for k in range(len(sample_times)):
        label = addressed_camera_at_sample(
            {cam: (yaws[k] if k < len(yaws) else None)
             for cam, yaws in per_camera.items()}, threshold)
        if label is not None:
            totals[label] = totals.get(label, 0.0) + max(
                0.0, edges[k + 1] - edges[k])
    return totals


def gaze_segments_for_line(
        line_id: int, start_s: float, end_s: float,
        sample_times: list[float],
        per_camera: dict[str, list[float | None]],
        *,
        min_hold_s: float = GAZE_HOLD_S,
        threshold: float = YAW_ADDRESSED_DEG) -> list[GazeSegment]:
    """Held addressed-camera runs inside one line. Pure.

    Samples with no single addressed camera inherit the previous
    addressed camera (a faceless sample is not a turn). A run shorter
    than `min_hold_s` is a glance: it folds into its longer neighbour
    (the previous one on a tie, the only neighbour at an edge), so a
    0.4 s look away never makes a segment. The surviving runs cover the
    whole line; a single run is the line's one address, not a turn.
    """
    if not sample_times or end_s <= start_s or not per_camera:
        return []
    edges = _sample_edges(start_s, end_s, sample_times)
    labels: list[str | None] = []
    for k in range(len(sample_times)):
        labels.append(addressed_camera_at_sample(
            {cam: (yaws[k] if k < len(yaws) else None)
             for cam, yaws in per_camera.items()}, threshold))
    known = [label for label in labels if label is not None]
    if not known:
        return []
    filled: list[str] = []
    current = known[0]
    for label in labels:
        if label is not None:
            current = label
        filled.append(current)
    # runs as [camera, edge_start, edge_end]
    runs: list[list] = []
    for k, label in enumerate(filled):
        if runs and runs[-1][0] == label:
            runs[-1][2] = edges[k + 1]
        else:
            runs.append([label, edges[k], edges[k + 1]])
    while len(runs) > 1:
        short_at = next((i for i, run in enumerate(runs)
                         if run[2] - run[1] < min_hold_s - 1e-9), None)
        if short_at is None:
            break
        if short_at == 0:
            target = 1
        elif short_at == len(runs) - 1:
            target = short_at - 1
        else:
            prev_dur = runs[short_at - 1][2] - runs[short_at - 1][1]
            next_dur = runs[short_at + 1][2] - runs[short_at + 1][1]
            target = short_at - 1 if prev_dur >= next_dur else short_at + 1
        runs[target][1] = min(runs[target][1], runs[short_at][1])
        runs[target][2] = max(runs[target][2], runs[short_at][2])
        runs.pop(short_at)
        merged: list[list] = []
        for run in runs:
            if merged and merged[-1][0] == run[0]:
                merged[-1][2] = run[2]
            else:
                merged.append(list(run))
        runs = merged
    return [GazeSegment(line_id, str(run[0]), float(run[1]), float(run[2]),
                        f"speaker addresses camera {run[0]}")
            for run in runs]


def segments_for_gaze(gaze: LineGaze, start_s: float, end_s: float,
                      *,
                      min_hold_s: float = GAZE_HOLD_S,
                      threshold: float = YAW_ADDRESSED_DEG) -> list[GazeSegment]:
    """Held segments for a `LineGaze`, recomputed when not stored. Pure."""
    if len(gaze.segments) > 1:
        return list(gaze.segments)
    times = list(gaze.sample_times_s)
    if not times and gaze.cameras:
        count = max((cam.samples for cam in gaze.cameras.values()), default=0)
        times = line_sample_times(start_s, end_s, count)
    if not times:
        return list(gaze.segments)
    per_camera = {label: list(cam.yaws) for label, cam in gaze.cameras.items()}
    return gaze_segments_for_line(gaze.line_id, start_s, end_s, times,
                                  per_camera, min_hold_s=min_hold_s,
                                  threshold=threshold)


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
    per_camera: dict[str, dict] = {}
    for label, cam in sorted(gaze.cameras.items()):
        entry: dict = {
            "yaw_deg": [None if y is None else round(y, 2) for y in cam.yaws],
            "addressed": cam.addressed,
            "samples": cam.samples,
            "median_abs_yaw": (None if cam.median_abs_yaw is None
                               else round(cam.median_abs_yaw, 2)),
        }
        if any(y is not None for y in cam.continuous_yaws):
            entry["yaw_continuous_deg"] = [
                None if y is None else round(y, 2) for y in cam.continuous_yaws]
            entry["yaw_coarse_deg"] = [
                None if y is None else round(y, 2) for y in cam.coarse_yaws]
        per_camera[label] = entry
    return per_camera | {
        "pick": gaze.pick,
        "reason": gaze.reason,
        "sample_times_s": [round(t, 3) for t in gaze.sample_times_s],
        "segments": [{"camera": seg.camera,
                      "start_s": round(seg.start_s, 3),
                      "end_s": round(seg.end_s, 3)}
                     for seg in gaze.segments],
    }


def line_sample_times(start_s: float, end_s: float,
                      n: int = SAMPLES_PER_LINE) -> list[float]:
    """n evenly spaced sampling times across the line. Pure."""
    if n <= 0 or end_s <= start_s:
        return []
    return [start_s + (i + 0.5) / n * (end_s - start_s) for i in range(n)]


def dense_line_sample_times(
        start_s: float, end_s: float,
        interval_s: float = DENSE_SAMPLE_INTERVAL_S,
        max_samples: int = MAX_SAMPLES_PER_LINE,
        min_samples: int = SAMPLES_PER_LINE) -> list[float]:
    """About one sample every `interval_s`, capped per line. Pure.

    Short lines keep `min_samples` so a brief address is still seen;
    long lines stop at `max_samples`, which widens the spacing instead
    of sampling without bound.
    """
    if end_s <= start_s or interval_s <= 0 or max_samples <= 0:
        return []
    count = int(math.ceil((end_s - start_s) / interval_s - 1e-9))
    count = min(max_samples, max(min_samples, count))
    return line_sample_times(start_s, end_s, count)


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
    # Zero-padded so the helper's sorted read order is the sample order
    # once a line has ten or more dense samples.
    return f"G{line_id:03d}_{label}_{sample:02d}.jpg"


def sample_line_gaze(lines, clips_by_label: dict,
                     luts: dict[str, Path | None],
                     frames_dir: Path) -> dict[int, LineGaze]:
    """Per-line gaze evidence, sampling densely per line per camera.

    About one frame every 0.5 s (capped per line; the times are recorded
    on the returned gaze). Frames go to `frames_dir` (kept, like the
    angle frames) with the camera's LUT applied, and one helper run
    reads them all. The effective yaw per sample is the helper's
    continuous estimate when present, else its coarse yaw. Anything that
    stops the sampling — no ffmpeg, no helper, no covering cameras —
    gives no gaze evidence rather than raising; per-frame failures only
    shrink that camera's sample count. Never raises.
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
    wanted: dict[str, tuple[int, str, int]] = {}
    line_times: dict[int, list[float]] = {}
    for line in coverable:
        times = dense_line_sample_times(line.start_s, line.end_s)
        line_times[line.id] = times
        for label in sorted(line.cameras):
            clip = clips_by_label.get(label)
            offset = getattr(clip, "offset_s", None) if clip is not None else None
            path = getattr(clip, "path", "") if clip is not None else ""
            if clip is None or offset is None or not path:
                continue
            lut = luts.get(label)
            for k, at in enumerate(times):
                name = gaze_frame_name(line.id, label, k)
                out = Path(frames_dir) / name
                if not (out.exists() and out.stat().st_size > 0):
                    try:
                        run_command(gaze_frame_command(path, at - offset, out, lut))
                    except Exception:
                        continue
                if out.exists() and out.stat().st_size > 0:
                    wanted[name] = (line.id, label, k)
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
    # (line, camera) -> sample index -> (coarse, continuous)
    seen: dict[tuple[int, str], dict[int, tuple[float | None, float | None]]] = {}
    for pos, name in enumerate(ordered):
        if name not in wanted:
            continue
        line_id, label, sample = wanted[name]
        frame = by_index.get(pos)
        seen.setdefault((line_id, label), {})[sample] = (
            frame.yaw if frame is not None else None,
            getattr(frame, "yaw_continuous", None) if frame is not None else None)
    out: dict[int, LineGaze] = {}
    for line in coverable:
        times = line_times.get(line.id, [])
        effective: dict[str, list[float | None]] = {}
        coarse: dict[str, list[float | None]] = {}
        continuous: dict[str, list[float | None]] = {}
        for label in line.cameras:
            by_sample = seen.get((line.id, label), {})
            if not by_sample:
                continue
            count = max(by_sample) + 1
            coarse[label] = [by_sample.get(k, (None, None))[0] for k in range(count)]
            continuous[label] = [by_sample.get(k, (None, None))[1] for k in range(count)]
            effective[label] = [effective_yaw(c, f) for c, f in
                                zip(coarse[label], continuous[label])]
        if not any(effective.get(label) for label in line.cameras):
            continue
        out[line.id] = build_line_gaze(
            line.id,
            {label: effective.get(label, []) for label in line.cameras},
            sample_times_s=times, start_s=line.start_s, end_s=line.end_s,
            coarse_per_camera=coarse, continuous_per_camera=continuous)
    return out
