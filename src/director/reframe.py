"""Vertical reframe: follow the speaker of a finished 16:9 cut in 9:16.

Pipeline: probe the cut, build a small 5 fps proxy, find shot cuts
(``--shots angles.json`` or ffmpeg scene detection refined at full rate),
track every person with SAM 3.1 (``guestlist``, optional), pick the subject
per shot, smooth a crop path inside each shot, and optionally render one
ffmpeg pass that moves a named crop filter with ``sendcmd``.

Designed by Muse Code on muse-wonder-1.0, built by Muse Code on
muse-spark-1.3 from docs/muse-code/REFRAME-BRIEF.md.
"""

from __future__ import annotations

import bisect
import json
import math
from dataclasses import dataclass
from pathlib import Path

from ghostcut.config import write_json
from ghostcut.ffmpeg_tools import (
    ToolingError,
    extract_scene_change_pts,
    probe_media,
    run_command,
)

PROXY_FPS = 5
PROXY_SHORT_SIDE = 540
SCENE_THRESHOLD = 0.30
REFINE_WINDOW_S = 0.5
REFINE_HEIGHT = 360
MIN_CUT_GAP_S = 0.25
SIGMA_S = 0.6
GAUSSIAN_RADIUS_SIGMAS = 3
SUBJECT_MIN_PRESENCE = 0.5
SUBJECT_MIN_HEIGHT_FRAC = 0.25
COST_USD_PER_1000_FRAMES = 0.20
MIN_BILLED_FRAMES_PER_CALL = 50
SAM_WINDOW_S = 10.0
SAM_OVERLAP_S = 1.0
DEFAULT_MAX_USD = 2.0
OUT_W, OUT_H = 1080, 1920
EPS = 1e-9


class ReframeError(RuntimeError):
    """A reframe step failed in a way the operator must see."""


class SamUnavailable(RuntimeError):
    """guestlist cannot be imported; every shot falls back to centre."""


@dataclass(slots=True)
class MediaInfo:
    path: Path
    width: int
    height: int
    fps: float
    duration_s: float


@dataclass(slots=True)
class ShotSpan:
    id: int
    start_s: float
    end_s: float


@dataclass(slots=True)
class Detection:
    frame: int
    object_id: str
    box: tuple[float, float, float, float]
    confidence: float


@dataclass(slots=True)
class TrackOutcome:
    detections: list[Detection]
    usd: float
    frames_billed: int
    saturated_spans: list[tuple[int, int]]
    warnings: list[str]


@dataclass(slots=True)
class SubjectChoice:
    object_id: str | None
    presence: float
    reason: str
    centres: dict[int, float]


@dataclass(slots=True)
class ShotPlan:
    id: int
    start_s: float
    end_s: float
    method: str
    object_id: str | None
    presence: float
    reason: str
    sample_start: int
    sample_end: int
    centres: list[float]


def _parse_rate(value: str | None) -> float:
    if not value:
        return 0.0
    text = value.strip()
    if "/" in text:
        parts = text.split("/")
        if len(parts) != 2:
            return 0.0
        try:
            num, den = float(parts[0]), float(parts[1])
        except ValueError:
            return 0.0
        if den == 0.0:
            return 0.0
        return num / den
    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_probe(probe: dict, path: Path) -> MediaInfo:
    streams = probe.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError(f"No video stream in {path}")
    try:
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
    except (TypeError, ValueError):
        raise ValueError(f"Bad frame size in {path}") from None
    if width <= 0 or height <= 0:
        raise ValueError(f"Bad frame size in {path}")
    if width <= height:
        raise ValueError(
            f"Already vertical (or square): {width}x{height}; "
            "reframe needs a landscape cut"
        )
    fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
    if fps <= 0:
        raise ValueError(f"Bad frame rate in {path}")
    try:
        duration_s = float((probe.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        raise ValueError(f"Bad duration in {path}") from None
    if duration_s <= 0:
        raise ValueError(f"Bad duration in {path}")
    return MediaInfo(path=path, width=width, height=height, fps=fps, duration_s=duration_s)


def probe_cut(path: Path) -> MediaInfo:
    return parse_probe(probe_media(path), path)


def proxy_frame_count(duration_s: float) -> int:
    return max(1, int(round(duration_s * PROXY_FPS)))


def proxy_command(video_path: Path, proxy_path: Path) -> list[str]:
    return [
        "ffmpeg", "-v", "error", "-y", "-i", str(video_path),
        "-map", "0:v:0",
        "-vf", f"fps={PROXY_FPS},scale=-2:{PROXY_SHORT_SIDE}",
        "-an", "-map_metadata", "-1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(proxy_path),
    ]


def make_proxy(video_path: Path, proxy_path: Path) -> Path:
    proxy_path.parent.mkdir(parents=True, exist_ok=True)
    run_command(proxy_command(video_path, proxy_path))
    return proxy_path


def load_shot_cuts(path: Path) -> list[float]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except json.JSONDecodeError:
        raise ValueError(f"Not an angles.json: {path}") from None
    except OSError as error:
        raise ValueError(f"Not an angles.json: {path}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("shots"), list):
        raise ValueError(f"Not an angles.json: {path}")
    shots = payload["shots"]
    if not shots:
        raise ValueError(f"angles.json has no shots: {path}")
    cuts: list[float] = []
    for entry in shots[1:]:
        if not isinstance(entry, dict):
            raise ValueError(f"angles.json shot without a numeric start_s: {path}")
        try:
            cuts.append(float(entry["start_s"]))
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"angles.json shot without a numeric start_s: {path}") from None
    return cuts


def scene_cuts(proxy_path: Path) -> list[float]:
    command = [
        "ffmpeg", "-v", "info", "-i", str(proxy_path),
        "-map", "0:v:0",
        "-vf", f"select='gt(scene,{SCENE_THRESHOLD:.6f})',showinfo",
        "-an", "-fps_mode", "passthrough", "-f", "null", "-",
    ]
    process = run_command(command)
    cuts = sorted({t for t in extract_scene_change_pts(process.stderr) if t > EPS})
    return cuts


def refine_cut(video_path: Path, proxy_cut_s: float, duration_s: float) -> float:
    lo = max(0.0, proxy_cut_s - REFINE_WINDOW_S)
    hi = min(duration_s, proxy_cut_s + REFINE_WINDOW_S)
    if hi - lo <= EPS:
        return proxy_cut_s
    command = [
        "ffmpeg", "-v", "info",
        "-ss", f"{lo:.3f}", "-t", f"{hi - lo:.3f}", "-i", str(video_path),
        "-map", "0:v:0",
        "-vf", f"scale=-2:{REFINE_HEIGHT},select='gt(scene,{SCENE_THRESHOLD:.6f})',showinfo",
        "-an", "-fps_mode", "passthrough", "-f", "null", "-",
    ]
    process = run_command(command)
    candidates = [lo + pts for pts in extract_scene_change_pts(process.stderr)]
    candidates = [c for c in candidates if lo - EPS <= c <= hi + EPS]
    if not candidates:
        return proxy_cut_s
    return min(candidates, key=lambda c: (abs(c - proxy_cut_s), c))


def refine_cuts(video_path: Path, proxy_cuts: list[float], duration_s: float) -> list[float]:
    return [refine_cut(video_path, cut, duration_s) for cut in proxy_cuts]


def merge_close_cuts(cuts: list[float], duration_s: float) -> list[float]:
    ordered = sorted(t for t in cuts if isinstance(t, (int, float)) and math.isfinite(t))
    ordered = [t for t in ordered if EPS < t < duration_s - EPS]
    kept: list[float] = []
    for cut in ordered:
        if kept and cut - kept[-1] < MIN_CUT_GAP_S - EPS:
            continue
        kept.append(cut)
    if kept and kept[0] < MIN_CUT_GAP_S - EPS:
        kept.pop(0)
    if kept and duration_s - kept[-1] < MIN_CUT_GAP_S - EPS:
        kept.pop()
    return kept


def build_shots(cuts: list[float], duration_s: float) -> list[ShotSpan]:
    edges = [0.0] + merge_close_cuts(cuts, duration_s) + [duration_s]
    return [ShotSpan(id=i, start_s=edges[i], end_s=edges[i + 1]) for i in range(len(edges) - 1)]


def shot_sample_ranges(shots: list[ShotSpan], n_samples: int) -> list[tuple[int, int]]:
    starts = [s.start_s for s in shots]
    counts = [0] * len(shots)
    for k in range(n_samples):
        idx = bisect.bisect_right(starts, k / PROXY_FPS + EPS) - 1
        idx = min(max(idx, 0), len(shots) - 1)
        counts[idx] += 1
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for count in counts:
        ranges.append((cursor, cursor + count))
        cursor += count
    return ranges


def shot_sample_range(shots: list[ShotSpan], index: int, n_samples: int) -> tuple[int, int]:
    return shot_sample_ranges(shots, n_samples)[index]


def sam_window_spans(n_frames: int, fps: float = PROXY_FPS) -> list[tuple[int, int]]:
    if n_frames <= 0:
        return []
    w = int(round(SAM_WINDOW_S * fps))
    o = int(round(SAM_OVERLAP_S * fps))
    if n_frames <= w:
        return [(0, n_frames)]
    spans: list[tuple[int, int]] = []
    s = 0
    while s + w < n_frames:
        spans.append((s, s + w))
        s += w - o
    if n_frames - s < w // 2:
        start, _ = spans.pop()
        spans.append((start, n_frames))
    else:
        spans.append((s, n_frames))
    return spans


def estimate_cost_usd(n_frames: int) -> float:
    billed = sum(
        max(end - start, MIN_BILLED_FRAMES_PER_CALL)
        for start, end in sam_window_spans(n_frames)
    )
    return billed * COST_USD_PER_1000_FRAMES / 1000.0


def ensure_within_budget(estimate_usd: float, max_usd: float, n_frames: int) -> None:
    if estimate_usd > max_usd:
        raise ValueError(
            f"Estimated SAM cost ${estimate_usd:.2f} exceeds --max-usd ${max_usd:.2f} "
            f"({n_frames}-frame proxy at $0.20/1000 frames); "
            "raise --max-usd or pass --no-sam"
        )


def detections_from_result(
    result,
    *,
    frame_w: int,
    frame_h: int,
    n_samples: int,
    fallback_frame_size: tuple[int, int],
) -> list[Detection]:
    size = getattr(result, "frame_size", None) or fallback_frame_size
    sw, sh = size
    if sw <= 0 or sh <= 0:
        sw, sh = frame_w, frame_h
    sx, sy = frame_w / sw, frame_h / sh
    detections: list[Detection] = []
    for raw in getattr(result, "detections", []):
        frame = int(raw.frame)
        if frame < 0 or frame >= n_samples:
            continue
        x0, y0, x1, y1 = raw.box
        x0, x1 = min(max(x0 * sx, 0.0), float(frame_w)), min(max(x1 * sx, 0.0), float(frame_w))
        y0, y1 = min(max(y0 * sy, 0.0), float(frame_h)), min(max(y1 * sy, 0.0), float(frame_h))
        if x1 <= x0 or y1 <= y0:
            continue
        detections.append(
            Detection(
                frame=frame,
                object_id=str(raw.object_id),
                box=(x0, y0, x1, y1),
                confidence=float(raw.confidence or 0.0),
            )
        )
    detections.sort(key=lambda d: (d.frame, d.object_id))
    return detections


def track(
    proxy_path: Path,
    *,
    n_frames: int,
    frame_w: int,
    frame_h: int,
    max_usd: float,
    workdir: Path,
) -> TrackOutcome:
    try:
        from guestlist.windows import segment_windowed
    except ImportError as error:
        raise SamUnavailable(str(error)) from error
    try:
        result = segment_windowed(
            proxy_path,
            "person",
            fps=PROXY_FPS,
            frames=n_frames,
            max_usd_per_call=max_usd,
            workdir=workdir,
        )
    except Exception as error:
        raise ReframeError(f"SAM tracking failed: {error}") from error
    try:
        proxy_info = probe_cut(proxy_path)
        fallback = (proxy_info.width, proxy_info.height)
    except Exception:
        fallback = (frame_w, frame_h)
    return TrackOutcome(
        detections=detections_from_result(
            result,
            frame_w=frame_w,
            frame_h=frame_h,
            n_samples=n_frames,
            fallback_frame_size=fallback,
        ),
        usd=float(result.usd),
        frames_billed=int(result.frames_billed),
        saturated_spans=[(int(a), int(b)) for a, b in result.saturated_spans()],
        warnings=[],
    )


def subject_for_shot(
    detections: list[Detection],
    *,
    n_samples: int,
    frame_w: int,
    frame_h: int,
    fallback_reason: str | None = None,
) -> SubjectChoice:
    if n_samples == 0:
        return SubjectChoice(None, 0.0, "no_samples", {})
    if not detections:
        return SubjectChoice(None, 0.0, fallback_reason or "no_detections", {})
    by_id: dict[str, dict[int, list[Detection]]] = {}
    for det in detections:
        by_id.setdefault(det.object_id, {}).setdefault(det.frame, []).append(det)
    ranked: list[tuple[int, float, float, str]] = []
    stats: dict[str, tuple[float, float, dict[int, float]]] = {}
    for object_id, per_frame in by_id.items():
        frames_seen = set(per_frame)
        centres: dict[int, float] = {}
        heights: list[float] = []
        areas: list[float] = []
        for frame, dets in per_frame.items():
            centres[frame] = sum((d.box[0] + d.box[2]) / 2 for d in dets) / len(dets)
            heights.append(sum(d.box[3] - d.box[1] for d in dets) / len(dets) / frame_h)
            areas.append(
                sum((d.box[2] - d.box[0]) * (d.box[3] - d.box[1]) for d in dets)
                / len(dets)
                / (frame_w * frame_h)
            )
        presence = len(frames_seen) / n_samples
        mean_height = sum(heights) / len(heights)
        mean_area = sum(areas) / len(areas)
        stats[object_id] = (presence, mean_height, centres)
        ranked.append((len(frames_seen), mean_height, mean_area, object_id))
    ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
    for _count, _height, _area, object_id in ranked:
        presence, mean_height, centres = stats[object_id]
        if presence >= SUBJECT_MIN_PRESENCE and mean_height >= SUBJECT_MIN_HEIGHT_FRAC:
            return SubjectChoice(object_id, presence, "ok", centres)
    top_presence = stats[ranked[0][3]][0]
    if any(stats[object_id][0] >= SUBJECT_MIN_PRESENCE for _, _, _, object_id in ranked):
        return SubjectChoice(None, top_presence, "too_small", {})
    return SubjectChoice(None, top_presence, "low_presence", {})


def fill_gaps(values: list[float | None]) -> list[float]:
    known = [i for i, v in enumerate(values) if v is not None]
    if not known:
        raise ValueError("fill_gaps: no known values")
    out: list[float] = [0.0] * len(values)
    for i in range(known[0]):
        out[i] = values[known[0]]  # type: ignore[index]
    out[known[-1]] = values[known[-1]]  # type: ignore[index]
    for i in range(known[-1] + 1, len(values)):
        out[i] = values[known[-1]]  # type: ignore[index]
    for a, b in zip(known, known[1:]):
        va = values[a]
        vb = values[b]
        assert va is not None and vb is not None
        for i in range(a, b + 1):
            out[i] = va + (vb - va) * (i - a) / (b - a)
    return out


def _reflect_index(j: int, n: int) -> int:
    if n <= 1:
        return 0
    while True:
        if j < 0:
            j = -j
        elif j >= n:
            j = 2 * (n - 1) - j
        else:
            return j


def gaussian_smooth(
    values: list[float], *, sigma_s: float = SIGMA_S, fps: float = PROXY_FPS
) -> list[float]:
    n = len(values)
    if n <= 1:
        return list(values)
    sigma = sigma_s * fps
    if sigma <= 0:
        return list(values)
    radius = int(math.ceil(GAUSSIAN_RADIUS_SIGMAS * sigma))
    weights = [math.exp(-0.5 * (d / sigma) ** 2) for d in range(-radius, radius + 1)]
    total = sum(weights)
    weights = [w / total for w in weights]
    out: list[float] = []
    for i in range(n):
        acc = 0.0
        for offset, w in enumerate(weights, start=-radius):
            acc += w * values[_reflect_index(i + offset, n)]
        out.append(acc)
    return out


def plan_shots(
    shots: list[ShotSpan],
    detections: list[Detection],
    *,
    n_samples: int,
    frame_w: int,
    frame_h: int,
    fallback_reason: str | None,
) -> list[ShotPlan]:
    ranges = shot_sample_ranges(shots, n_samples)
    plans: list[ShotPlan] = []
    for shot, (sample_start, sample_end) in zip(shots, ranges):
        m = sample_end - sample_start
        local = [d for d in detections if sample_start <= d.frame < sample_end]
        choice = subject_for_shot(
            local,
            n_samples=m,
            frame_w=frame_w,
            frame_h=frame_h,
            fallback_reason=fallback_reason,
        )
        if choice.object_id is None:
            centres = [frame_w / 2.0] * m
            plans.append(
                ShotPlan(
                    id=shot.id,
                    start_s=shot.start_s,
                    end_s=shot.end_s,
                    method="centre",
                    object_id=None,
                    presence=choice.presence,
                    reason=choice.reason,
                    sample_start=sample_start,
                    sample_end=sample_end,
                    centres=centres,
                )
            )
            continue
        raw = [choice.centres.get(k) for k in range(sample_start, sample_end)]
        centres = gaussian_smooth(fill_gaps(raw))
        plans.append(
            ShotPlan(
                id=shot.id,
                start_s=shot.start_s,
                end_s=shot.end_s,
                method="subject",
                object_id=choice.object_id,
                presence=choice.presence,
                reason=choice.reason,
                sample_start=sample_start,
                sample_end=sample_end,
                centres=centres,
            )
        )
    return plans


def crop_size(frame_w: int, frame_h: int) -> tuple[int, int]:
    crop_h = frame_h
    crop_w = int(round(frame_h * 9 / 16 / 2)) * 2
    if crop_w > frame_w:
        crop_w = (frame_w // 2) * 2
    if crop_w <= 0:
        raise ValueError(f"Cannot cut a 9:16 crop from {frame_w}x{frame_h}")
    return crop_w, crop_h


def clamp_crop_x(centre_x: float, frame_w: int, crop_w: int) -> int:
    limit = max(0, frame_w - crop_w)
    if crop_w >= frame_w:
        return 0
    x = int(math.floor(centre_x - crop_w / 2 + 0.5))
    return min(max(x, 0), limit)


def per_frame_crop_xs(
    plans: list[ShotPlan],
    *,
    fps: float,
    duration_s: float,
    frame_w: int,
    crop_w: int,
) -> list[int]:
    n_out = max(1, int(round(duration_s * fps)))
    starts = [p.start_s for p in plans]
    xs: list[int] = []
    for f in range(n_out):
        t = f / fps
        idx = bisect.bisect_right(starts, t + EPS) - 1
        idx = min(max(idx, 0), len(plans) - 1)
        plan = plans[idx]
        m = plan.sample_end - plan.sample_start
        if m <= 0:
            centre = frame_w / 2.0
        elif m == 1:
            centre = plan.centres[0]
        else:
            p = t * PROXY_FPS
            ss = plan.sample_start
            if p <= ss:
                centre = plan.centres[0]
            elif p >= plan.sample_end - 1:
                centre = plan.centres[-1]
            else:
                k0 = int(math.floor(p))
                frac = p - k0
                centre = plan.centres[k0 - ss] * (1 - frac) + plan.centres[k0 - ss + 1] * frac
        xs.append(clamp_crop_x(centre, frame_w, crop_w))
    return xs


def sendcmd_text(crop_xs: list[int], *, fps: float) -> tuple[str, int]:
    lines: list[str] = []
    for f in range(1, len(crop_xs)):
        if crop_xs[f] != crop_xs[f - 1]:
            lines.append(f"{f / fps:.6f} crop@rf x {crop_xs[f]};")
    text = "".join(line + "\n" for line in lines)
    return text, len(lines)


def _escape_filter_path(path: Path) -> str:
    text = str(path)
    for char in ("\\", ":", ",", "'"):
        text = text.replace(char, "\\" + char)
    return text


def render_command(
    video_path: Path,
    render_path: Path,
    *,
    crop_w: int,
    crop_h: int,
    first_x: int,
    script_path: Path,
) -> list[str]:
    filters = (
        f"sendcmd=f={_escape_filter_path(script_path)},"
        f"crop@rf=w={crop_w}:h={crop_h}:x={first_x}:y=0,"
        f"scale={OUT_W}:{OUT_H}:flags=lanczos,"
        "setsar=1,"
        "format=yuv420p"
    )
    return [
        "ffmpeg", "-v", "error", "-y", "-i", str(video_path),
        "-map", "0:v:0", "-map", "0:a?",
        "-vf", filters,
        "-c:v", "libx264", "-preset", "medium", "-crf", "16",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(render_path),
    ]


def render(
    video_path: Path,
    render_path: Path,
    *,
    crop_w: int,
    crop_h: int,
    crop_xs: list[int],
    fps: float,
    script_path: Path,
) -> int:
    text, moves = sendcmd_text(crop_xs, fps=fps)
    if not text.strip():
        # A static crop path (every centred shot) emits no commands, which
        # sendcmd refuses ("No commands were specified"). Pin the single
        # crop position at t=0; moves stays 0, which is what happened.
        text = f"{0.0:.6f} crop@rf x {crop_xs[0] if crop_xs else 0};\n"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(text, encoding="utf-8")
    Path(render_path).parent.mkdir(parents=True, exist_ok=True)
    run_command(
        render_command(
            video_path,
            render_path,
            crop_w=crop_w,
            crop_h=crop_h,
            first_x=crop_xs[0] if crop_xs else 0,
            script_path=script_path,
        )
    )
    return moves


def run(
    *,
    video_path: Path,
    out_path: Path,
    render_path: Path | None,
    shots_path: Path | None,
    no_sam: bool,
    max_usd: float,
) -> dict:
    video_path = Path(video_path)
    out_path = Path(out_path)
    if max_usd < 0:
        raise ValueError("--max-usd must be >= 0")
    info = probe_cut(video_path)
    if info.width <= info.height:
        raise ValueError(
            f"Already vertical (or square): {info.width}x{info.height}; "
            "reframe needs a landscape cut"
        )
    crop_w, crop_h = crop_size(info.width, info.height)
    n_samples = proxy_frame_count(info.duration_s)
    work = out_path.parent / f"{video_path.stem}.reframe"

    cuts: list[float] | None = None
    if shots_path is not None:
        cuts = load_shot_cuts(Path(shots_path))

    estimate = estimate_cost_usd(n_samples) if not no_sam else 0.0
    if not no_sam:
        ensure_within_budget(estimate, max_usd, n_samples)

    proxy_path = work / "proxy.mp4"
    need_proxy = shots_path is None or not no_sam
    if need_proxy:
        make_proxy(video_path, proxy_path)
    if cuts is None:
        cuts = refine_cuts(video_path, scene_cuts(proxy_path), info.duration_s)
    shots = build_shots(cuts, info.duration_s)

    warnings: list[str] = []
    detections: list[Detection] = []
    spend = 0.0
    billed = 0
    saturated: list[tuple[int, int]] = []
    if no_sam:
        method = "centre"
        fallback_reason: str | None = "no_sam"
        warnings.append("no_sam: --no-sam given; every shot is centred")
    else:
        try:
            outcome = track(
                proxy_path,
                n_frames=n_samples,
                frame_w=info.width,
                frame_h=info.height,
                max_usd=max_usd,
                workdir=work / "windows",
            )
        except SamUnavailable as error:
            method = "centre"
            fallback_reason = "sam_unavailable"
            warnings.append(f"sam_unavailable: {error}; every shot is centred")
        else:
            method = "sam"
            fallback_reason = None
            detections = outcome.detections
            spend = outcome.usd
            billed = outcome.frames_billed
            saturated = outcome.saturated_spans
            for a, b in saturated:
                warnings.append(
                    f"sam_saturated:{a / PROXY_FPS:.1f}-{b / PROXY_FPS:.1f}s: "
                    "window hit SAM's 16-id cap; a person may be missing there"
                )
            if spend > estimate:
                warnings.append(f"sam_spend_over_estimate:${spend:.4f}>${estimate:.4f}")

    plans = plan_shots(
        shots,
        detections,
        n_samples=n_samples,
        frame_w=info.width,
        frame_h=info.height,
        fallback_reason=fallback_reason,
    )
    crop_xs = per_frame_crop_xs(
        plans, fps=info.fps, duration_s=info.duration_s, frame_w=info.width, crop_w=crop_w
    )

    render_payload: dict | None = None
    if render_path is not None:
        render_path = Path(render_path)
        moves = render(
            video_path,
            render_path,
            crop_w=crop_w,
            crop_h=crop_h,
            crop_xs=crop_xs,
            fps=info.fps,
            script_path=work / "crop.sendcmd",
        )
        render_payload = {
            "path": str(render_path),
            "width": OUT_W,
            "height": OUT_H,
            "frames": len(crop_xs),
            "crop_moves": moves,
        }

    ranges = shot_sample_ranges(shots, n_samples)
    samples: list[dict] = []
    for plan, (sample_start, sample_end) in zip(plans, ranges):
        for k in range(sample_start, sample_end):
            samples.append(
                {
                    "frame": k,
                    "t_s": round(k / PROXY_FPS, 3),
                    "shot": plan.id,
                    "centre_x": round(plan.centres[k - sample_start], 3),
                }
            )
    payload = {
        "version": 1,
        "video": str(video_path),
        "width": info.width,
        "height": info.height,
        "fps": round(info.fps, 6),
        "duration_s": round(info.duration_s, 3),
        "crop": {"width": crop_w, "height": crop_h},
        "method": method,
        "proxy_fps": PROXY_FPS,
        "estimate_usd": round(estimate, 4),
        "spend_usd": round(spend, 4),
        "frames_billed": billed,
        "max_usd": max_usd,
        "saturated_spans": [
            {"start_s": round(a / PROXY_FPS, 3), "end_s": round(b / PROXY_FPS, 3)}
            for a, b in saturated
        ],
        "warnings": warnings,
        "shots": [
            {
                "id": plan.id,
                "start_s": round(plan.start_s, 3),
                "end_s": round(plan.end_s, 3),
                "method": plan.method,
                "object_id": plan.object_id,
                "presence": round(plan.presence, 4),
                "reason": plan.reason,
                "samples": plan.sample_end - plan.sample_start,
            }
            for plan in plans
        ],
        "samples": samples,
        "render": render_payload,
    }
    write_json(out_path, payload)
    return payload
