# Design: vertical reframe with SAM 3.1 (`director reframe`)

Implements `docs/muse-code/REFRAME-BRIEF.md`. Conventions from `AGENTS.md`:
Python ≥3.11, stdlib-only (the brief's Limits section overrides the numpy/Pillow
`director` extra — `reframe.py` imports neither), `from __future__ import
annotations`, type annotations throughout, `@dataclass(slots=True)` at module
boundaries, `guestlist` imported inside the one function that calls it and
nowhere else, tests offline with `unittest` and never touching the Meta API.
ffmpeg/ffprobe are assumed on PATH (the CLI enforces it).

Everything below is decided. An engineer building this should not need to
choose a constant, a field name, a reason string, or an edge-case outcome.

---

## 1. Inputs as they actually are

### 1.1 The installed `ghostcut` functions (the only ones used)

From `ghostcut.ffmpeg_tools`:

- `probe_media(path: Path) -> dict` — runs
  `ffprobe -v error -show_entries format=duration,bit_rate:stream=index,codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,sample_rate,channels -of json <path>`
  and returns the parsed JSON dict:
  `{"streams": [{"codec_type": "video", "width": int, "height": int,
  "avg_frame_rate": "30000/1001", "r_frame_rate": "...", ...}, ...],
  "format": {"duration": "12.340000", ...}}`. Durations and rates arrive as
  strings; a missing/unparseable rate is `"0/0"`.
- `run_command(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]`
  — text mode, captures stdout/stderr, raises `ToolingError` on a non-zero exit
  when `check` is true. This is how every ffmpeg invocation runs.
- `extract_scene_change_pts(stderr: str) -> list[float]` — extracts every
  `pts_time:<float>` from ffmpeg's stderr (the `showinfo` filter's output).
  It returns them in log order, unsorted, with no deduplication.
- `ToolingError(RuntimeError)` — already caught by `cli.main`.

From `ghostcut.config`:

- `write_json(path: Path, payload: dict | list) -> None` — creates the parent
  directory, writes `json.dumps(payload, indent=2, sort_keys=True)`. Keys in
  `reframe.json` are therefore sorted; payload construction order is free.

`reframe.py` does **not** import `read_json`, `snap_frame_rate`, or anything
else from `ghostcut`. Probe parsing (§3) is local because `probe_media`'s
fixed `-show_entries` set is all the brief permits, and `angles.json` is
parsed with stdlib `json` for the same reason.

### 1.2 The installed `guestlist` call (verified against the package)

```python
from guestlist.windows import segment_windowed
segment_windowed(proxy: str | Path, prompt: str = "person", *,
                 fps: float, frames: int,
                 window_s: float = 10.0, overlap_s: float = 1.0,
                 min_window_s: float = 5.0, workers: int = 4,
                 max_usd_per_call: float | None = None,
                 workdir: str | Path | None = None,
                 keep_uploads: bool = False) -> WindowedResult
```

`WindowedResult` (in `guestlist.windows`):

- `.detections: list[Detection]` where `Detection` (in `guestlist.meta.sam`)
  has `.frame: int` (global proxy-frame index), `.object_id: str` (stable
  across the whole cut, stitched), `.box: tuple[int, int, int, int]`
  `(x0, y0, x1, y1)` with right/bottom **exclusive**, in SAM's frame pixels,
  and `.confidence: float | None`. Detections are sorted by
  `(frame, int(object_id))`. The `.mask` field exists and is always ignored
  by this feature.
- `.frame_size: tuple[int, int] | None` — `(w, h)` of SAM's frames, `None`
  only when no window produced a size.
- `.usd` — a **property**, total dollars including superseded (split) windows.
- `.frames_billed` — a **property**, total billed frames likewise.
- `.saturated_spans()` — a **method** returning `list[tuple[int, int]]` of
  `[start_frame, end_frame)` global frame spans whose final window still held
  the 16-id cap; people may be missing there.

The brief's call shape is used verbatim (§8), with `window_s`/`overlap_s`
left at their defaults so the cost estimate (§7) and the real windowing agree.

### 1.3 `angles.json` (the `--shots` input)

Written by `director angles` (`src/director/angles.py:run`). Only these facts
are relied on: the top level is a JSON object with `"version": 1` and
`"shots": [{"id": int, "start_s": float, "end_s": float, ...}, ...]`, shots
in time order, times already rounded to 3 decimals. Reframe reads `start_s`
only; it re-derives shot ends from the cut's own duration (§5), so an
`angles.json` made from a longer timeline still tiles the cut it is given.

---

## 2. Files changed

| File | Change |
|---|---|
| `src/director/reframe.py` | New. All logic in this document. |
| `src/director/cli.py` | New `command_reframe(args)` and a `reframe` subparser. Nothing else in the file changes. |
| `tests/test_reframe.py` | New. §15. |
| `docs/muse-code/BUILD-LOG.md` | New REFRAME section (brief §Limits; written at build time, not by this design). |
| `AGENTS.md` | One `reframe.py` bullet under Layout (same). |

`angles.py`, `grade.py`, `sync.py` (which does not exist in this repo — the
brief names it defensively) and `app/` are not touched.

`reframe.py` imports, in full: stdlib (`bisect`, `json`, `math`, `dataclasses`,
`pathlib`) plus `ghostcut.config.write_json` and
`ghostcut.ffmpeg_tools` (`ToolingError` re-exported by use, `extract_scene_change_pts`,
`probe_media`, `run_command`). No `guestlist`, `numpy`, or Pillow import at
module level or anywhere except inside `track()` (§8).

---

## 3. Constants

| Constant | Value | Why |
|---|---|---|
| `PROXY_FPS` | `5` | Brief §2: 5 fps follows a seated or walking speaker; SAM bills per frame, so this is the cost knob. Proxy frame period is exactly 0.2 s. |
| `PROXY_SHORT_SIDE` | `540` | Brief §2: short side 540 px. For a landscape cut the short side is the height, so the proxy is `scale=-2:540` (960×540 at 16:9). |
| `SCENE_THRESHOLD` | `0.30` | Brief §3: ffmpeg `select='gt(scene,0.30)'` on the proxy. |
| `REFINE_WINDOW_S` | `0.5` | Brief §3: full-rate re-detection in a ±0.5 s window around each proxy hit (proxy times are only good to 0.2 s). |
| `REFINE_HEIGHT` | `360` | The refinement pass is scaled to 360 px high (`scale=-2:360`) for speed; scene scores at 360p rank the same cut first in practice, and only the *nearest* hit is taken. |
| `MIN_CUT_GAP_S` | `0.25` | Brief §3: cuts closer together than 0.25 s merge. Also the minimum distance a kept cut may sit from 0 or from the end. |
| `SIGMA_S` | `0.6` | Brief §7: Gaussian sigma, in seconds. At 5 fps `sigma_samples = 3.0`. |
| `GAUSSIAN_RADIUS_SIGMAS` | `3` | Kernel truncated at ±3σ: radius `R = ceil(3 * 3.0) = 9` samples. The discarded tail mass is < 0.3 % and is removed by normalisation. |
| `SUBJECT_MIN_PRESENCE` | `0.5` | Brief §6: follow only a person present in at least half the shot's samples. |
| `SUBJECT_MIN_HEIGHT_FRAC` | `0.25` | Brief §6: …and standing at least a quarter of the frame tall (mean box height / frame height). |
| `COST_USD_PER_1000_FRAMES` | `0.20` | Brief §5: $0.20 per 1,000 SAM frames. |
| `MIN_BILLED_FRAMES_PER_CALL` | `50` | Brief §5: each SAM request bills at least 50 frames. |
| `SAM_WINDOW_S` | `10.0` | Mirrors `guestlist.windows.WINDOW_S`, the default actually passed to SAM. Used only by the estimate (§7). |
| `SAM_OVERLAP_S` | `1.0` | Mirrors `guestlist.windows.OVERLAP_S`. Same use. |
| `DEFAULT_MAX_USD` | `2.0` | Brief §5 / CLI default for `--max-usd`. |
| `OUT_W, OUT_H` | `1080, 1920` | Brief §9: the render is always `scale=1080:1920`. |
| `EPS` | `1e-9` | Float-dust guard on time comparisons only (cut merging, shot assignment). Never applied to the presence/height gates, which are exact `>=` comparisons on rationals of small integers. |

---

## 4. Data classes

All `@dataclass(slots=True)` in `reframe.py`. Times are seconds (float),
positions are source-frame pixels (float unless named `_x` as an int crop
offset), frame indices are ints.

```python
@dataclass(slots=True)
class MediaInfo:
    path: Path
    width: int          # coded pixels of the first video stream
    height: int
    fps: float          # avg_frame_rate if parseable else r_frame_rate
    duration_s: float   # format.duration

@dataclass(slots=True)
class ShotSpan:
    id: int             # 0..n-1 in time order
    start_s: float
    end_s: float        # shots tile [0, duration_s]: shot[i].end_s == shot[i+1].start_s

@dataclass(slots=True)
class Detection:
    frame: int                       # proxy/sample frame index, 0-based
    object_id: str
    box: tuple[float, float, float, float]  # (x0, y0, x1, y1), source pixels, clipped to the frame
    confidence: float                # 0.0 when SAM reported None; carried, never gated on

@dataclass(slots=True)
class TrackOutcome:
    detections: list[Detection]      # scaled to source pixels (§8)
    usd: float
    frames_billed: int
    saturated_spans: list[tuple[int, int]]  # [start_frame, end_frame) in sample frames
    warnings: list[str]

@dataclass(slots=True)
class SubjectChoice:
    object_id: str | None            # None when the shot is centred
    presence: float                  # winner's (or best candidate's) share of the shot's samples; 0.0 when none
    reason: str                      # "ok" for a followed subject; a centre reason from §9.2 otherwise
    centres: dict[int, float]        # sample frame -> raw box-centre x (source px), winner only; {} when centred

@dataclass(slots=True)
class ShotPlan:
    id: int
    start_s: float
    end_s: float
    method: str                      # "subject" | "centre"
    object_id: str | None
    presence: float
    reason: str                      # "ok" when method == "subject"
    sample_start: int                # first sample frame of this shot (inclusive)
    sample_end: int                  # one past the last sample frame
    centres: list[float]             # smoothed centre x per sample, aligned to
                                     # range(sample_start, sample_end); the frame
                                     # centre (frame_w / 2) for centre shots
```

Invariants the rest of the module (and the tests) rely on:

- `ShotSpan`s are sorted, non-overlapping, and tile `[0, duration_s]`
  exactly: first starts at 0.0, last ends at `duration_s`.
- Sample frame `k` shows time `k / PROXY_FPS`. Every sample belongs to
  exactly one shot (§6.3); `ShotPlan.centres` concatenated in shot order is
  the per-sample path written to `reframe.json`.
- `Detection.box` is always clipped into `[0, frame_w] × [0, frame_h]` with
  `x1 > x0` and `y1 > y0` (degenerate boxes are dropped at the border, §8).

Two exceptions:

```python
class ReframeError(RuntimeError):
    """A reframe step failed in a way the operator must see (e.g. SAM errored mid-run)."""

class SamUnavailable(RuntimeError):
    """guestlist cannot be imported; every shot falls back to centre."""
```

Both subclass `RuntimeError`, so `cli.main`'s existing
`except (FileNotFoundError, ValueError, RuntimeError, ToolingError)` handles
them with no CLI change beyond the new subcommand.

---

## 5. Shots

### 5.1 From `angles.json`: `load_shot_cuts(path: Path) -> list[float]`

1. Read and `json.loads` the file. Any `OSError` propagates as
   `FileNotFoundError` (CLI prints it); any `json.JSONDecodeError` is
   re-raised as `ValueError(f"Not an angles.json: {path}")`.
2. Require a dict with a `"shots"` list, else the same `ValueError`.
   (`"version"` is not checked — only `start_s` is consumed, and a future
   version that keeps `start_s` stays readable.)
3. For each entry require a dict whose `"start_s"` converts with `float()`;
   a missing or non-numeric `start_s` raises
   `ValueError(f"angles.json shot without a numeric start_s: {path}")`.
4. Return `[float(s["start_s"]) for s in shots[1:]]` — the brief: the cut
   times are the `start_s` of every shot after the first. An empty shots
   list raises `ValueError(f"angles.json has no shots: {path}")`; a
   one-shot file is valid and returns `[]`.

The returned cuts are *not* trusted: they go through the same
`merge_close_cuts` + `build_shots` as detected cuts (§5.3), which sorts,
clamps, and merges them.

### 5.2 From scene detection (no `--shots`)

`scene_cuts(proxy_path: Path) -> list[float]`:

```
ffmpeg -v info -i <proxy> -map 0:v:0
       -vf select='gt(scene,0.300000)',showinfo
       -an -fps_mode passthrough -f null -
```

run via `run_command`. `extract_scene_change_pts(process.stderr)` yields the
proxy-time cut candidates (showinfo preserves source timestamps because
`select` does not reset PTS and `-fps_mode passthrough` forbids duplication).
Drop any `t <= EPS` (a "cut" at the first frame is the stream start), sort,
deduplicate exact repeats. This list is quantised to the proxy's 0.2 s grid.

`refine_cut(video_path: Path, proxy_cut_s: float, duration_s: float) -> float`
— one full-rate re-check per proxy hit:

1. `lo = max(0.0, proxy_cut_s - REFINE_WINDOW_S)`,
   `hi = min(duration_s, proxy_cut_s + REFINE_WINDOW_S)`. If `hi - lo <= EPS`
   return `proxy_cut_s` unchanged.
2. Run (input seek, so showinfo timestamps restart at 0 and `lo` is added
   back; input seek is exact here because the output is transcoded):

   ```
   ffmpeg -v info -ss <lo:.3f> -t <hi-lo:.3f> -i <video> -map 0:v:0
          -vf scale=-2:360,select='gt(scene,0.300000)',showinfo
          -an -fps_mode passthrough -f null -
   ```

3. Candidates = `[lo + pts for pts in extract_scene_change_pts(stderr)]`
   filtered to `[lo, hi]`. If there are none, return `proxy_cut_s` (the
   proxy saw *something*; keeping its estimate loses less than dropping a
   real cut). Otherwise return the candidate minimising
   `(abs(c - proxy_cut_s), c)` — nearest wins, ties go to the earlier time.

`refine_cuts(video_path, proxy_cuts, duration_s) -> list[float]` maps
`refine_cut` over the list in order. Cost is one short decode per cut.

Why this order matters (brief §3): a cut that lands a frame late shows the
old shot's crop on the new shot. The proxy pass finds *that* a cut exists;
only the full-rate pass may say *when*, and it is never allowed to move a
cut by more than the ±0.5 s window or to invent one outside it.

### 5.3 Merging and building: `merge_close_cuts`, `build_shots`

`merge_close_cuts(cuts: list[float], duration_s: float) -> list[float]`:

1. Keep finite cuts with `0.0 < t < duration_s` (strict, modulo `EPS`:
   drop `t <= EPS` and `t >= duration_s - EPS`), sort ascending.
2. Greedy left-to-right: keep the first; drop any later cut whose distance
   to the **last kept** cut is `< MIN_CUT_GAP_S - EPS`. (Distance is measured
   to the last *kept* cut, not the last seen cut, so a chain 0.0/0.2/0.4
   keeps 0.0 and 0.4 — two real cuts 0.4 s apart survive, a burst collapses
   to its first member. Keeping the *earliest* of a burst is deliberate:
   combined with §5.2's refinement, the crop jumps at or just before the
   true cut, never a frame after it.)
3. Boundary merge: if the first kept cut is `< MIN_CUT_GAP_S` from 0, drop
   it; if the last kept cut is within `MIN_CUT_GAP_S` of `duration_s`, drop
   it. A shot shorter than 0.25 s at either edge is absorbed by its only
   neighbour.

`build_shots(cuts: list[float], duration_s: float) -> list[ShotSpan]`:
edges = `[0.0] + merge_close_cuts(cuts, duration_s) + [duration_s]`;
shots are consecutive edge pairs with ids `0..n-1`. With no cuts this is
one shot `[0, duration_s]`. Every internal shot is at least
`MIN_CUT_GAP_S` long (step 2/3 above), except that a cut list from
`angles.json` for a cut shorter than 0.5 s can still yield a single shot —
handled by the no-samples path in §9.

---

## 6. Probe, proxy, samples

### 6.1 Probe

`_parse_rate(value: str | None) -> float`: `"30000/1001"` → `30000/1001`;
`"0/0"`, empty, missing, or unparseable → `0.0` (mirrors
`ghostcut.ffmpeg_tools.parse_fraction` semantics without importing it —
the brief names only the four functions in §1.1).

`parse_probe(probe: dict, path: Path) -> MediaInfo`:

- First stream with `codec_type == "video"`; none →
  `ValueError(f"No video stream in {path}")`.
- `width`/`height` are `int(stream.get(...) or 0)`; either `<= 0` →
  `ValueError(f"Bad frame size in {path}")`.
- `fps = _parse_rate(avg_frame_rate) or _parse_rate(r_frame_rate)`;
  `fps <= 0` → `ValueError(f"Bad frame rate in {path}")`.
- `duration_s = float(probe["format"]["duration"])` guarded: missing or
  non-numeric → `ValueError(f"Bad duration in {path}")`; `<= 0` → same.
- `path` is stored as given (the payload records what the operator passed).

`probe_cut(path: Path) -> MediaInfo` is `parse_probe(probe_media(path), path)`.

Vertical refusal (in `run`, immediately after probing):
`if info.width <= info.height: raise ValueError(f"Already vertical (or square): {w}x{h}; reframe needs a landscape cut")`.
Square counts as vertical: the 9:16 crop from full height would need width
`> height`, i.e. more than the frame holds, for any `width <= height`.

### 6.2 Proxy

`proxy_frame_count(duration_s: float) -> int` =
`max(1, int(round(duration_s * PROXY_FPS)))`. This is the `frames=` argument
to SAM and the length of every per-sample structure. (The proxy file itself
may hold one frame more or fewer at the tail due to fps-filter rounding;
sample `k` is *defined* as time `k / PROXY_FPS`, and SAM reports detections
in the same indexing, so a ±1 tail discrepancy only ever affects the last
sample, which §9's hold-the-ends rule absorbs.)

`proxy_command(video_path: Path, proxy_path: Path) -> list[str]` (pure,
tested, the single source of the proxy's shape):

```
ffmpeg -v error -y -i <video> -map 0:v:0
       -vf fps=5,scale=-2:540
       -an -map_metadata -1
       -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p
       -movflags +faststart <proxy>
```

`fps=5` before `scale` (fewer frames scaled), video only (`-map 0:v:0`,
`-an`), no metadata (`-map_metadata -1`) — all per brief §2. `libx264
veryfast crf 20 yuv420p` matches the window-cut encoding `guestlist` itself
uses, so SAM sees the same pixel format it sees for window slices.

`make_proxy(video_path: Path, proxy_path: Path) -> Path`:
`proxy_path.parent.mkdir(parents=True, exist_ok=True)` then
`run_command(proxy_command(...))`, returning `proxy_path`. No caching: the
work directory is per-run (§11) and a stale proxy for a replaced cut would
be worse than the seconds a re-encode costs.

### 6.3 Sample ranges per shot

`shot_sample_range(shot: ShotSpan, n_samples: int) -> tuple[int, int]`:
samples are assigned by time: sample `k` belongs to the shot with
`start_s - EPS <= k / PROXY_FPS < end_s - EPS`, the last shot owning any
sample with `k / PROXY_FPS < duration_s` (i.e. all `k < n_samples`).
Implementation: binary search (`bisect`) over shot start times, then clamp
to `[0, n_samples]` and to monotonic non-decreasing boundaries so ranges
are contiguous and partition `range(n_samples)`:

```
starts = [s.start_s for s in shots]
for k in range(n_samples):
    shot_idx = bisect_right(starts, k / PROXY_FPS + EPS) - 1   # in run/planner, computed incrementally
```

The returned `(sample_start, sample_end)` per shot satisfy:
`shots[0].sample_start == 0`, `shots[-1].sample_end == n_samples`,
`shots[i].sample_end == shots[i+1].sample_start`. A shot may own **zero**
samples (shorter than 0.2 s and falling between two proxy frames); §9.2
gives it `method="centre", reason="no_samples"`. This function is the only
place sample ownership is decided; subject choice, smoothing, and the
per-frame path all consume its output, which is what guarantees the crop
can never smear or jump inside the wrong shot.

---

## 7. Cost estimate and guard

`sam_window_spans(n_frames: int, fps: float = PROXY_FPS) -> list[tuple[int, int]]`
replicates `guestlist.windows.plan_windows` at the proxy rate (it is
re-implemented, not imported: `guestlist` may be absent, and the estimate
must run before anyone decides to import it):

- `w = round(10.0 * fps)` (= 50), `o = round(1.0 * fps)` (= 5).
- `n_frames <= 0` → `[]`; `n_frames <= w` → `[(0, n_frames)]`.
- Otherwise step `w - o` from 0 appending `(s, s + w)` while `s + w < n_frames`;
  then if the remaining tail `n_frames - s < w // 2` extend the last span to
  `n_frames`, else append `(s, n_frames)`.

`estimate_cost_usd(n_frames: int) -> float`:

```
billed = sum(max(end - start, MIN_BILLED_FRAMES_PER_CALL)
             for start, end in sam_window_spans(n_frames))
return billed * COST_USD_PER_1000_FRAMES / 1000.0
```

Sanity anchor from the brief: a 60 s cut → `n = 300` → spans
`(0,50),(45,95),(90,140),(135,185),(180,230),(225,275),(270,300)` → billed
`6*50 + 50 = 350` (the 30-frame tail bills at the 50-frame minimum) →
`350 * 0.20 / 1000 = $0.07`. The estimate deliberately ignores saturated-
window splits (unknowable before the run); if the actual `result.usd`
exceeds the estimate, that is reported, not hidden (§8, §12).

`ensure_within_budget(estimate_usd: float, max_usd: float) -> None`:
if `estimate_usd > max_usd` raise
`ValueError(f"Estimated SAM cost ${estimate_usd:.2f} exceeds --max-usd ${max_usd:.2f} ({n}-frame proxy at $0.20/1000 frames); raise --max-usd or pass --no-sam")`
— the message is built by the caller-adjacent helper `budget_error(...)`
or, equivalently, `ensure_within_budget(estimate_usd, max_usd, n_frames)`
with `n_frames` used only for the message. Pick one shape and keep it:
**`ensure_within_budget(estimate_usd: float, max_usd: float, n_frames: int) -> None`**,
raising `ValueError` with exactly that text. `max_usd < 0` is a
`ValueError("--max-usd must be >= 0")` raised by `run` before anything else.

Order (decided): the guard runs **before** the proxy is built and before
SAM is imported or called (§13 step 5). Refusing late would have spent the
operator's time; refusing after a SAM call would have spent their money.
The guard applies whenever SAM is requested (i.e. not `--no-sam`), whether
or not `guestlist` turns out to be importable — the cap is a statement
about the cut, not about the environment.

---

## 8. Tracking (SAM)

```python
def track(proxy_path: Path, *, n_frames: int, frame_w: int, frame_h: int,
          max_usd: float, workdir: Path) -> TrackOutcome
```

The **only** function in the module that imports `guestlist`, and it does
so inside its body:

```python
try:
    from guestlist.windows import segment_windowed
except ImportError as error:
    raise SamUnavailable(str(error)) from error
result = segment_windowed(proxy_path, "person", fps=PROXY_FPS,
                          frames=n_frames, max_usd_per_call=max_usd,
                          workdir=workdir)
```

(`max_usd` is passed as `max_usd_per_call` exactly as the brief's snippet
shows; the run-level guard in §7 is what bounds the total estimate. Any
non-`ImportError` exception from the call is wrapped:
`raise ReframeError(f"SAM tracking failed: {error}") from error` — a
half-finished paid run must fail loudly, not silently centre every shot.)

`detections_from_result(result, *, frame_w: int, frame_h: int,
n_samples: int, fallback_frame_size: tuple[int, int]) -> list[Detection]`
is the pure adapter (this is what the stand-in-result test drives):

1. `(sw, sh) = result.frame_size or fallback_frame_size`; if either is
   `<= 0`, fall back again to `(frame_w, frame_h)` (boxes then pass
   through unscaled). `sx, sy = frame_w / sw, frame_h / sh`.
2. For each raw detection: skip if `int(d.frame)` is outside
   `[0, n_samples)`; scale its box by `(sx, sy)`; clip x into
   `[0, frame_w]`, y into `[0, frame_h]`; skip if the clipped box is
   degenerate (`x1 <= x0` or `y1 <= y0`); emit
   `Detection(frame=int(d.frame), object_id=str(d.object_id),
   box=(x0, y0, x1, y1), confidence=float(d.confidence or 0.0))`.
3. Sort by `(frame, object_id)`. Duplicate `(frame, object_id)` entries
   are kept here and averaged later (§9.1), so this function stays a
   faithful transcription of what SAM said.

`track()` obtains `fallback_frame_size` by probing the proxy
(`probe_cut(proxy_path)` → `(width, height)`; on any exception use
`(frame_w, frame_h)`) — SAM's frames *are* the proxy frames, so this is
the right fallback when `result.frame_size is None`. It then returns
`TrackOutcome(detections=detections_from_result(...), usd=float(result.usd),
frames_billed=int(result.frames_billed),
saturated_spans=[(int(a), int(b)) for a, b in result.saturated_spans()],
warnings=[])`. Saturated spans become run-level warnings in `run` (§13),
one per span: `f"sam_saturated:{a / PROXY_FPS:.1f}-{b / PROXY_FPS:.1f}s: window hit SAM's 16-id cap; a person may be missing there"` (frame numbers converted to seconds of cut time; `b` is exclusive as returned).

---

## 9. Subject choice and the per-shot path

### 9.1 `subject_for_shot`

```python
def subject_for_shot(detections: list[Detection], *, n_samples: int,
                     frame_h: int) -> SubjectChoice
```

`detections` is already restricted to the shot's samples
(`d.frame` in the shot's range — the caller slices by `shot_sample_range`,
the function does not re-check shot bounds).

1. If `n_samples == 0`: `SubjectChoice(None, 0.0, "no_samples", {})`.
   If `detections` is empty: `SubjectChoice(None, 0.0, <fallback>, {})`
   where `<fallback>` is decided by the caller context (§9.2) — see the
   `fallback_reason` parameter below.
2. Group by `object_id`. Per object: `frames_seen` = the set of sample
   frames it appears in; `presence = len(frames_seen) / n_samples`. If an
   object has several detections in one sample (possible after clipping),
   that sample contributes the **mean** of their centre xs and the **mean**
   of their heights — deterministic and order-independent. Per object also
   compute `mean_height_frac` = mean over its samples of
   `(y1 - y0) / frame_h`, and `mean_area_frac` = mean of
   `(x1 - x0) * (y1 - y0) / (frame_w * frame_h)` — the caller passes
   `frame_w` as well; the signature is therefore
   `subject_for_shot(detections, *, n_samples, frame_w, frame_h, fallback_reason)`.
3. Rank candidates by `(count desc, mean_height_frac desc,
   mean_area_frac desc, object_id asc)` — "on screen the most" first,
   "the largest" as the tie-break (brief §6's order), id ascending so the
   result never depends on dict order.
4. Walk the ranked list; the first candidate with
   `presence >= SUBJECT_MIN_PRESENCE` **and**
   `mean_height_frac >= SUBJECT_MIN_HEIGHT_FRAC` is the subject:
   `SubjectChoice(object_id, presence, "ok", centres)` where `centres`
   maps each of its sample frames to that sample's (possibly averaged)
   box-centre x `(x0 + x1) / 2` in source pixels.
5. If no candidate passes both gates:
   - if any candidate has `presence >= SUBJECT_MIN_PRESENCE` (so someone
     was around enough but everyone was small): reason `"too_small"`;
   - elif there was at least one candidate: reason `"low_presence"`
     (the passer-by case: the most-present person is still under half);
   - `presence` reported is the top-ranked candidate's presence, so the
     JSON shows *how close* the best candidate came.

`confidence` is never consulted: the brief gates on presence and size only,
and SAM's per-box confidence is not calibrated across windows.

### 9.2 `plan_shots`

```python
def plan_shots(shots: list[ShotSpan], detections: list[Detection], *,
               n_samples: int, frame_w: int, frame_h: int,
               fallback_reason: str | None) -> list[ShotPlan]
```

`fallback_reason` is `"no_sam"` when `--no-sam` was given,
`"sam_unavailable"` when `track` raised `SamUnavailable`, and `None` when
SAM ran. It only changes the *reason string* of shots that end up centred
for lack of a subject; it never changes a `subject` outcome (there are no
detections at all in the first two cases, so no subject outcome is
possible). Per shot, in order:

1. `(sample_start, sample_end) = shot_sample_range(shot, n_samples)`;
   `m = sample_end - sample_start`.
2. `local = [d for d in detections if sample_start <= d.frame < sample_end]`
   (detections are frame-sorted, so this is a slice in practice).
3. `choice = subject_for_shot(local, n_samples=m, frame_w=frame_w,
   frame_h=frame_h, fallback_reason=fallback_reason)`, where inside
   `subject_for_shot` an empty `local` yields reason
   `fallback_reason or "no_detections"` (and `m == 0` yields
   `"no_samples"` regardless of fallback — the shot had no samples to
   judge anyone by).
4. If `choice.object_id is None`: `method="centre"`,
   `centres = [frame_w / 2.0] * m`, `object_id=None`,
   `presence=choice.presence`, `reason=choice.reason`.
5. If a subject was chosen: `method="subject"`, build the raw per-sample
   series `[choice.centres.get(k) for k in range(sample_start, sample_end)]`
   (`float | None` per sample), `fill_gaps` it (§10.1), then
   `gaussian_smooth` it (§10.2) — **inside this shot only**. The series
   never sees a neighbouring shot's values: this is the step that makes
   "the path never smears across a cut" true.
6. `presence` is stored unrounded; rounding happens only in the payload.

Reason strings, the complete set (these exact tokens appear in
`reframe.json` and in the tests):

| Token | Meaning |
|---|---|
| `ok` | A subject is followed (`method="subject"`). |
| `no_sam` | `--no-sam`: no tracking was requested. |
| `sam_unavailable` | `guestlist` could not be imported. |
| `no_detections` | SAM ran; this shot contains no detections at all (empty shot / B-roll). |
| `no_samples` | The shot owns zero proxy samples (§6.3). |
| `low_presence` | People appear, but nobody reaches half the shot's samples (passer-by). |
| `too_small` | Somebody is present enough, but nobody followed stands a quarter of the frame tall (tiny background figure). |

### 9.3 From samples to output frames

```python
def per_frame_crop_xs(plans: list[ShotPlan], *, fps: float, duration_s: float,
                      frame_w: int, crop_w: int) -> list[int]
```

- `n_out = max(1, int(round(duration_s * fps)))` output frames, frame `f`
  at time `t = f / fps`. The source is treated as constant-rate at the
  probed `fps` (see VFR note in §14).
- The owning shot is found by bisecting shot start times at `t`
  (`bisect_right(starts, t + EPS) - 1`, clamped) — so the first frame with
  `t >= cut` belongs to the **new** shot. The jump lands exactly on the
  cut frame: frame `ceil(cut * fps)` at the latest, and never a frame
  early, because `t < cut - EPS` still belongs to the old shot.
- Within the shot, the smoothed centre at time `t` is linear interpolation
  over the shot's own samples: `p = t * PROXY_FPS`; let `k0` be the sample
  index clamped into `[sample_start, sample_end - 1]`; interpolate between
  samples `k0` and `k0 + 1` **of the same shot** with fraction
  `p - k0`, holding the shot's first value for `p < sample_start` and its
  last value for `p > sample_end - 1`. For a centre shot every sample is
  `frame_w / 2`, so this degenerates to a constant. For a zero-sample shot
  the centre is `frame_w / 2`.
- `crop_xs[f] = clamp_crop_x(centre_x, frame_w, crop_w)`.

`clamp_crop_x(centre_x: float, frame_w: int, crop_w: int) -> int`:
`x = int(math.floor(centre_x - crop_w / 2 + 0.5))` (round-half-up, chosen
over `round()` so results don't depend on banker's rounding), clamped to
`[0, max(0, frame_w - crop_w)]`. If `crop_w >= frame_w` the only legal
value is 0.

`crop_size(frame_w: int, frame_h: int) -> tuple[int, int]`:
`crop_h = frame_h`; `crop_w = int(round(frame_h * 9 / 16 / 2)) * 2`
(nearest even); if `crop_w > frame_w`, `crop_w` becomes the largest even
number `<= frame_w`; if that is `<= 0`, raise `ValueError`. Anchors:
1080p → `round(607.5/2)*2 = 608×1080`; 2160p → `1216×2160` (brief §8).

---

## 10. Gap filling and smoothing (pure, per shot)

### 10.1 `fill_gaps(values: list[float | None]) -> list[float]`

Straight-line interpolation inside the shot, ends held (brief §7):

- Known indices `i0 < i1 < …`. Before `i0`: hold `values[i0]`. After the
  last known index: hold its value. Between consecutive known `a < b`:
  `v[i] = v[a] + (v[b] - v[a]) * (i - a) / (b - a)`.
- A single known value fills the whole series with that constant.
- An all-`None` list cannot occur here (§9.2 only calls `fill_gaps` for a
  chosen subject, which has ≥1 detection); if it ever did, raise
  `ValueError("fill_gaps: no known values")` rather than inventing a centre.

### 10.2 `gaussian_smooth(values: list[float], *, sigma_s: float = SIGMA_S, fps: float = PROXY_FPS) -> list[float]`

Zero-phase Gaussian, implemented in pure Python (stdlib-only):

- `sigma = sigma_s * fps` (= 3.0 samples), `R = ceil(GAUSSIAN_RADIUS_SIGMAS * sigma)` (= 9).
- Weights `w[d] = exp(-0.5 * (d / sigma) ** 2)` for `d in -R..R`, normalised
  to sum 1 over the full kernel (edges use reflected values, below, so the
  full kernel always applies and a constant series is preserved exactly,
  up to float error).
- Output `out[i] = Σ_d w[d] * values[reflect(i + d, n)]` where `reflect`
  mirrors about the end samples without repeating them:
  `reflect(j, n)`: if `n == 1` return 0; repeatedly map `j < 0 → -j` and
  `j >= n → 2*(n-1) - j` until in range.
- `len(values) <= 1` returns a copy unchanged.

Why zero-phase and why this is the right discretisation: the kernel is
symmetric, so a linear ramp passes through its interior **exactly**
(`Σ w[d]·(a·(i+d)+b) = a·i + b` when `Σ w[d]·d = 0`), i.e. the crop never
lags a steadily moving speaker — the failure mode of a causal (one-sided)
filter, which would trail the subject by ~σ seconds on every move.
Reflection (rather than truncating and renormalising the kernel at the
edges) keeps the kernel symmetric at the boundaries too; the only edge
artefact is a slight pull toward the interior within `R` samples of a
shot's ends, which is the correct price for never borrowing values across
a cut. High-frequency jitter (period ≪ σ) is attenuated by the Gaussian's
frequency response; the smoothing test (§15) asserts a ±10 px alternating
series comes out within 2 px of its mean.

**Order of operations (fixed):** choose subject → raw centres per sample →
`fill_gaps` → `gaussian_smooth` (per shot) → per-frame interpolation →
`clamp_crop_x`. Smoothing raw detections before filling gaps would let
`None`s leak into the kernel; interpolating to output frames before
smoothing would smooth across cuts. No other order is used anywhere.

---

## 11. Render

### 11.1 `sendcmd_text(crop_xs: list[int], *, fps: float) -> tuple[str, int]`

One line per **change** of x (brief §9): for each frame `f > 0` with
`crop_xs[f] != crop_xs[f-1]`, the line

```
{f / fps:.6f} crop@rf x {crop_xs[f]};
```

Frame 0 never gets a line — the crop filter's initial `x=` is
`crop_xs[0]` (§11.2), so a line at 0.0 would be a no-op write. Returns
`(text, moves)` where `moves` is the number of lines. An unchanging crop
yields `("", 0)` and an empty script file, which `sendcmd` accepts.
(The line format `time target command arg;` with target `crop@rf` was
verified against the ffmpeg on PATH: the command takes effect on the
first frame whose PTS is ≥ the timestamp.)

### 11.2 `render_command` / `render`

```python
def render_command(video_path: Path, render_path: Path, *, crop_w: int,
                   crop_h: int, first_x: int, script_path: Path) -> list[str]
```

(pure, tested, not run in tests):

```
ffmpeg -v error -y -i <video>
       -map 0:v:0 -map 0:a?
       -vf sendcmd=f=<script>,crop@rf=w=<crop_w>:h=<crop_h>:x=<first_x>:y=0,scale=1080:1920:flags=lanczos,setsar=1,format=yuv420p
       -c:v libx264 -preset medium -crf 16
       -c:a copy
       -movflags +faststart <render>
```

Notes on the decided details:

- One ffmpeg pass over the **full-rate cut** (brief §9) — the proxy never
  reaches the render. `sendcmd` precedes the named `crop@rf` so commands
  address it; `crop` y is 0 because the crop is the full height
  (`crop_h == frame_h`, §9.3).
- `scale=1080:1920:flags=lanczos` then `setsar=1` then `format=yuv420p`,
  in that order, exactly as the brief lists them (brief writes the pixel
  format as `yuv420p`; the filter is `format=yuv420p`).
- `-map 0:a?` (optional audio): a cut without an audio stream still
  renders; when audio exists it is **stream-copied untouched**
  (`-c:a copy`) — never re-encoded, never trimmed separately, so sync is
  the source's own.
- Video encodes `libx264 -preset medium -crf 16`, matching the per-shot
  render settings in `angles.py` (`-crf 16` there); the output is a
  delivery file, not an intermediate, and 16 is visually lossless for
  talking-head content at 1080×1920.
- `<script>` inside the filtergraph is escaped for ffmpeg's filter parser:
  backslash-escape `\`, `:`, `,`, and `'` in the script path (in that
  order). The script *file* itself needs no escaping.
- If `crop_w == frame_w` the crop is a no-op horizontally but the filter
  chain is unchanged (the scale still applies).

`render(video_path, render_path, *, crop_w, crop_h, crop_xs, fps,
script_path) -> int`: writes `sendcmd_text` to `script_path` (creating
the work directory), creates `render_path.parent`, runs
`render_command(...)` via `run_command`, returns the move count. A failed
encode raises `ToolingError` and no `reframe.json` is written (§13 order).

---

## 12. `reframe.json`

Written with `write_json` (sorted keys) **after** any render succeeds.
Floats are rounded only here: times to 3 decimals, `presence` to 4,
`centre_x` to 3, `fps` to 6, money to 4.

```json
{
  "version": 1,
  "video": "<video path as given>",
  "width": 1920, "height": 1080, "fps": 29.97003, "duration_s": 61.200,
  "crop": {"width": 608, "height": 1080},
  "method": "sam",
  "proxy_fps": 5,
  "estimate_usd": 0.0720, "spend_usd": 0.0713, "frames_billed": 350,
  "max_usd": 2.0,
  "saturated_spans": [{"start_s": 20.0, "end_s": 30.0}],
  "warnings": ["sam_saturated:20.0-30.0s: window hit SAM's 16-id cap; a person may be missing there"],
  "shots": [
    {"id": 0, "start_s": 0.0, "end_s": 12.400, "method": "subject",
     "object_id": "3", "presence": 0.9355, "reason": "ok", "samples": 62},
    {"id": 1, "start_s": 12.400, "end_s": 20.000, "method": "centre",
     "object_id": null, "presence": 0.2105, "reason": "low_presence", "samples": 38}
  ],
  "samples": [
    {"frame": 0, "t_s": 0.0, "shot": 0, "centre_x": 941.250}
  ],
  "render": {"path": "<render path as given>", "width": 1080, "height": 1920,
             "frames": 1835, "crop_moves": 212}
}
```

Field decisions:

- `"method"` is the **run** method: `"sam"` if `segment_windowed`
  returned (even if every shot then centred for lack of a subject — SAM
  ran and its answer was "nobody qualifies"), `"centre"` if `--no-sam`
  or `guestlist` was unavailable. Per-shot method lives on each shot.
- `"samples"` is the brief's "smoothed centre per proxy sample": one entry
  per sample frame `0..n_samples-1`, `centre_x` in **source pixels**
  (pre-clamp; the clamp is a crop concern, and the unclamped path is the
  useful diagnostic), `shot` the owning shot id. Centre shots contribute
  `width / 2`.
- `"shots[].samples"` is the shot's sample count (`sample_end - sample_start`);
  `"presence"` is the followed subject's share, or the best candidate's
  share for a centred shot (0.0 when there was no candidate), so a refused
  passer-by is visible in the output, not just in the reason token.
- `"saturated_spans"` are seconds (frames / `PROXY_FPS`, rounded 3),
  `[start, end)` — present (possibly `[]`) whenever SAM ran, `[]` when it
  did not.
- `"estimate_usd"` is the §7 estimate (0.0 under `--no-sam`);
  `"spend_usd"`/`"frames_billed"` are the actuals from the
  `WindowedResult` (0 / 0 when SAM did not run). A warning
  `f"sam_spend_over_estimate:${spend:.4f}>${estimate:.4f}"` is appended
  when splits pushed the actual spend past the estimate — the guard (§7)
  bounds the *estimate*, and this keeps the overage honest.
- `"render"` is `null` without `--render`; with one, `"frames"` is
  `len(crop_xs)` (the planned output-frame count) and `"crop_moves"` is
  the `sendcmd` line count.

Work files live in `<out dir>/<cut stem>.reframe/` (brief): the proxy at
`<work>/proxy.mp4`, SAM's window cache at `<work>/windows/` (passed as
`workdir`), the crop script at `<work>/crop.sendcmd` (only written when
rendering). `<cut stem>` is `Path(video).stem`. `reframe.json` itself is
at `--out`, wherever the operator put it. Nothing else is written anywhere.

---

## 13. Orchestrator and CLI

### 13.1 `run`

```python
def run(*, video_path: Path, out_path: Path, render_path: Path | None,
        shots_path: Path | None, no_sam: bool, max_usd: float) -> dict
```

Steps in this exact order (each step's failure aborts before the next
spends time or money; nothing is written to `--out` until step 11):

1. Normalise paths to `Path`. If `max_usd < 0`:
   `ValueError("--max-usd must be >= 0")`. If the video does not exist,
   `probe_media` raises `ToolingError` at step 2 — no separate existence
   check (one source of truth, matching `angles.py`'s clip handling).
2. `info = probe_cut(video_path)`. Vertical/square refusal (§6.1).
   `(crop_w, crop_h) = crop_size(info.width, info.height)`.
3. `n_samples = proxy_frame_count(info.duration_s)`.
   `work = out_path.parent / f"{video_path.stem}.reframe"`.
4. Shots source: if `shots_path is not None`, `cuts = load_shot_cuts(shots_path)`
   (skip to step 6's `build_shots` after step 5 — no proxy is needed for
   shot *times* in this branch).
5. Cost guard (only when `not no_sam`):
   `estimate = estimate_cost_usd(n_samples)`;
   `ensure_within_budget(estimate, max_usd, n_samples)`.
6. Proxy: build it if it will be used — for scene detection (no
   `--shots`) or for SAM (not `--no-sam`). If neither (i.e. `--shots`
   with `--no-sam`), skip the proxy entirely and set
   `proxy_path = work / "proxy.mp4"` without creating the file.
   `shots = build_shots(cuts_or_detected, info.duration_s)` where the
   detected branch is
   `refine_cuts(video_path, scene_cuts(proxy_path), info.duration_s)`.
7. Tracking: if `no_sam`: `detections = []`, `method = "centre"`,
   `fallback_reason = "no_sam"`, warning
   `"no_sam: --no-sam given; every shot is centred"`, spend/billed 0.
   Else `try: outcome = track(proxy_path, n_frames=n_samples,
   frame_w=info.width, frame_h=info.height, max_usd=max_usd,
   workdir=work / "windows")` — on `SamUnavailable as error`:
   `detections = []`, `method = "centre"`,
   `fallback_reason = "sam_unavailable"`, warning
   `f"sam_unavailable: {error}; every shot is centred"`, spend/billed 0.
   On success: `method = "sam"`, `fallback_reason = None`, detections/
   spend/billed/saturated from the outcome, plus one `sam_saturated:…`
   warning per saturated span (§8) and the over-estimate warning (§12)
   when applicable.
8. `plans = plan_shots(shots, detections, n_samples=n_samples,
   frame_w=info.width, frame_h=info.height, fallback_reason=fallback_reason)`.
9. `crop_xs = per_frame_crop_xs(plans, fps=info.fps,
   duration_s=info.duration_s, frame_w=info.width, crop_w=crop_w)`.
10. If `render_path is not None`:
    `moves = render(video_path, render_path, crop_w=crop_w,
    crop_h=crop_h, crop_xs=crop_xs, fps=info.fps,
    script_path=work / "crop.sendcmd")`.
11. Build the §12 payload, `write_json(out_path, payload)`, return it.

### 13.2 CLI

In `cli.py`, mirroring `command_grade`/`command_angles` (lazy import of
`.reframe` inside the command function; `require_ffmpeg_binaries()`
unconditionally, because even the `--shots --no-sam` path needs `ffprobe`):

```
director reframe VIDEO --out reframe.json [--render vertical.mp4]
                   [--shots angles.json] [--no-sam] [--max-usd 2.0]
```

- `reframe` subparser: positional `video`; `--out` required; `--render`
  default `None`; `--shots` default `None`; `--no-sam` `store_true`;
  `--max-usd` `type=float` default `2.0` (i.e. `DEFAULT_MAX_USD`).
- `command_reframe(args) -> dict` calls `run(...)` with
  `Path(args.video)`, `Path(args.out)`, etc., then prints, in order:
  - one line per shot:
    `f"shot {s['id']} {s['start_s']:.3f}-{s['end_s']:.3f}s {s['method']} {subject} {s['reason']}"`
    where `subject` is `f"object {s['object_id']} presence {s['presence']:.2f}"`
    for a subject shot and `"-"` for a centred one — so the printed line
    always carries the method and the reason (brief §CLI) and, for
    followed shots, who and how much;
  - one `f"! {warning}"` per warning (the `command_grade` convention);
  - `f"spend ${spend:.4f} ({frames_billed} frames billed, method {method})"`
    — under `--no-sam`/unavailable this prints `$0.0000 (0 frames billed, …)`,
    which is the honest number, not a placeholder;
  - if rendered: `f"Rendered {render_path} ({moves} crop moves)"`
    (moves read back from `payload["render"]["crop_moves"]`);
  - `f"Wrote {args.out}"`.
- Returns the payload, like the other commands. All error types raised by
  `run` (`ValueError`, `ReframeError`, `SamUnavailable`, `ToolingError`,
  `FileNotFoundError` from a missing `--shots` file) are already in
  `main`'s `except` tuple; `main` prints `Error: …` and returns 1.

---

## 14. Edge cases (decided behaviour)

- **Already vertical or square** (`width <= height`): refused at probe
  time (§6.1) before any file is created. A *rotated* portrait phone clip
  stored as landscape pixels with rotation metadata is out of scope:
  `probe_media`'s fixed entry set carries no rotation, ffmpeg applies
  autorotation consistently in proxy, detection, and render, and the crop
  follows the rotated picture the editor sees. Recorded here so nobody
  "fixes" it by adding probe entries the brief did not name.
- **Zero/negative/unparseable duration, fps, or frame size**: `ValueError`
  from `parse_probe`; nothing is written.
- **Cut shorter than one proxy frame** (`duration_s < 0.2`): `n_samples`
  is 1 (§6.2), one shot, that shot owns sample 0; everything downstream is
  the constant path. A cut shorter than 0.5 s with an interior cut from
  `--shots` can produce a zero-sample shot → `centre` / `no_samples`
  (§9.2), and the per-frame path uses the frame centre for its frames.
- **`--shots` with 0 or 1 shots**: 0 → `ValueError`; 1 → no cuts, single
  shot over the whole cut.
- **`--shots` cuts outside the cut** (angles made from a longer edit):
  `merge_close_cuts` drops cuts `<= 0` or `>= duration_s` and merges cuts
  within 0.25 s of the edges, so the cut it is given always tiles cleanly.
- **Scene detection finds nothing**: one shot, `[0, duration_s]`. This is
  a normal outcome for a single locked-off take, not a warning.
- **Refinement finds nothing in a window**: the proxy time stands (§5.2).
  Refinement never *removes* a cut — removal decisions belong to the
  0.25 s merge, which runs after refinement on refined times.
- **Cuts closer than 0.25 s after refinement** (two proxy hits refining to
  nearly the same instant): merged by §5.3, earliest kept.
- **`guestlist` absent**: `SamUnavailable` → all shots centred,
  `reason="sam_unavailable"`, `method="centre"`, a warning names the
  import error. The run still writes `reframe.json` and still renders
  (a centred 9:16 is a valid deliverable).
- **SAM errors mid-run** (API failure after the import succeeded):
  `ReframeError`, the run fails, no JSON. Deliberately different from the
  absent-guestlist case: money may have been spent, and a silently
  centred file would disguise that.
- **Saturated windows**: recorded as `saturated_spans` + one warning per
  span. Shots overlapping a saturated span are planned from whatever
  detections exist; if the subject is missing there, the presence gate
  centres the shot — the warning is how the operator learns why.
- **Empty shot / B-roll** (SAM ran, zero detections in the shot):
  `centre` / `no_detections`, `presence 0.0`.
- **Subject visible in exactly one sample**: presence will normally fail
  the 0.5 gate; if the shot has ≤2 samples it can pass, and then
  `fill_gaps` holds that one centre across the shot and smoothing is the
  identity on a constant — a static crop, which is the only defensible
  read of one observation.
- **Subject leaves and returns within one shot**: gaps up to the whole
  inter-appearance span are straight-line interpolated (§10.1). There is
  deliberately **no maximum gap**: the alternative (recentring mid-shot)
  would introduce a jump the brief forbids inside a shot. The presence
  gate is the guard against following someone who is mostly absent.
- **Two people, one qualifying**: the ranked walk (§9.1 step 4) follows
  the highest-ranked qualifier; the other is ignored for this shot even
  if larger, when the higher-ranked one is more present. Presence is the
  identity signal; size only orders candidates and gates.
- **Box hanging off the frame edge**: clipped before centre/size are
  computed (§8), so a half-out-of-frame speaker's centre is the centre of
  the *visible* box — the crop then frames what is actually visible.
- **`crop_w >= frame_w`** (near-square landscape, e.g. 1000×900):
  `crop_w` is reduced to the largest even width `<= frame_w`, `x` is
  pinned to 0, and `scale=1080:1920` upscales. Rare and lossy, but the
  alternative (refusing) would break the "any landscape cut" contract.
- **VFR source**: `per_frame_crop_xs` indexes frames by the probed average
  fps, so for a strongly variable-rate cut the nominal frame grid and the
  real frame PTS drift apart. The render is unaffected in kind —
  `sendcmd` acts on *time*, so the crop still changes at the right
  moments and jumps at cuts; only the JSON's implied per-frame x values
  are nominal. Finished talking cuts (the feature's stated input) are
  constant-rate in practice. Documented, not solved.
- **No audio stream**: `-map 0:a?` makes the render succeed silently;
  `reframe.json` does not record audio presence (it is not this feature's
  concern).
- **Render fails** (bad codec, unwritable path): `ToolingError`
  propagates, `reframe.json` is **not** written (§13 step 10 before 11),
  so a `reframe.json` on disk always describes a render that exists.
- **Same stem, two cuts** (`a/cut.mp4`, `b/cut.mp4`, same `--out`): the
  work directory is derived from `--out`'s directory plus the video stem,
  so both runs share `<out dir>/cut.reframe/` and the second proxy
  overwrites the first. Accepted (same behaviour as `angles.py`'s fixed
  `angle-frames` directory); operators separate runs by `--out`.
- **`--max-usd 0`** with SAM requested: any non-empty cut estimates above
  $0, so the guard refuses; with `--no-sam` the flag is inert. Both are
  the operator's explicit intent.

---

## 15. Tests (`tests/test_reframe.py`)

Offline `unittest`, in the style of `tests/test_angles.py` /
`tests/test_grade.py`: small synthetic builders (`_media(...)`,
`_shot(...)`, `_det(frame, oid, box)`), no media files, no network, no
`guestlist` import (the stand-in result is a `types.SimpleNamespace`;
`director.reframe` never imports `guestlist` at module level, so the whole
file runs where `guestlist` is absent). Command builders are asserted as
lists and never executed; the one integration-style test patches
`reframe.probe_cut`/`reframe.track` at the module namespace. One line per
test — what it builds, what it asserts:

- `test_crop_size_1080p_and_2160p` — `crop_size(1920, 1080) == (608, 1080)` and `crop_size(3840, 2160) == (1216, 2160)`; odd heights round to the nearest even width; a near-square `crop_size(1000, 900)` returns an even width `<= 1000`.
- `test_clamp_crop_x_edges_and_centre` — centre of a 1920-wide frame gives `(1920-608)//2`; centres near the left/right edges clamp to `0` / `1920-608`; `crop_w >= frame_w` always gives 0; half-pixel centres round half-up.
- `test_parse_probe_rejects_vertical_inputs` — hand-built probe dicts: square and portrait raise `ValueError`, `"0/0"` rates raise, a `30000/1001` rate parses to `29.970…`, missing video stream raises.
- `test_proxy_command_shape` — `proxy_command` is a `list[str]` containing `fps=5,scale=-2:540`, `-an`, `-map_metadata -1`, `-map 0:v:0`, ending at the proxy path.
- `test_proxy_frame_count` — 60 s → 300, 0.1 s → 1, 61.2 s → 306.
- `test_load_shot_cuts_from_angles_json` — a temp `angles.json` with 3 shots yields the last two `start_s` values as cuts; a 1-shot file yields `[]`; missing `shots`/missing `start_s`/malformed JSON each raise `ValueError`.
- `test_build_shots_tiles_and_merges_near_duplicates` — cuts `[1.0, 1.15, 5.0]` over 10 s build shots `[0,1),[1,5),[5,10]` (the 0.15 s pair merged, earliest kept); cuts within 0.25 s of 0 or of the end are dropped; no cuts → one shot; shots always tile `[0, duration]`.
- `test_merge_close_cuts_chain_keeps_earliest` — `[0.0-ish…]` chain `t, t+0.2, t+0.4` keeps the first and third (gap measured to the last *kept* cut).
- `test_shot_sample_ranges_partition` — shots `[0,1),[1,3)` over 15 samples give ranges `(0,5),(5,15)`; ranges are contiguous, cover `range(n_samples)` exactly, and a shot between two samples owns zero.
- `test_subject_most_present_wins` — two objects in one shot, the more-present-but-smaller one is followed (`object_id`, `presence`, `reason "ok"`).
- `test_subject_passerby_refused` — best candidate at 30 % presence → `centre`, reason `low_presence`, reported presence ≈ 0.3.
- `test_subject_tiny_background_refused` — candidate at 90 % presence with mean height 10 % of frame → `centre`, reason `too_small`.
- `test_subject_empty_shot` — no detections → `centre`, reason `no_detections`, presence 0.0; with `fallback_reason="no_sam"` the same empty shot reports `no_sam`.
- `test_subject_duplicate_detections_in_one_sample_averaged` — two boxes for one object in one sample count once for presence and contribute their mean centre/height.
- `test_fill_gaps_interpolates_and_holds_ends` — `[None, 10, None, None, 40, None]` → `[10, 10, 20, 30, 40, 40]`; a single known value fills constantly; all-`None` raises `ValueError`.
- `test_gaussian_smooth_constant_kept` — a constant series is returned equal within 1e-9 at every index, including the ends.
- `test_gaussian_smooth_no_lag_on_ramp` — a linear ramp's interior samples (≥ kernel radius from both ends) equal the ramp within 1e-6 (zero phase); the whole series stays within one sample-step of the ramp.
- `test_gaussian_smooth_removes_jitter` — a ±10 px alternating series around 500 comes out within 2 px of 500 everywhere past the first/last sample.
- `test_each_shot_planned_on_its_own` — two shots with subjects at opposite sides: each `ShotPlan.centres` derives only from its own shot (no value in shot 2's plan is pulled toward shot 1's centre), and a centred shot between two subject shots does not bridge them.
- `test_per_frame_crop_jumps_exactly_at_cut` — plans with constant but different centres per shot, cut at 1.0 s, fps 30: frames 0–29 carry shot 1's x, frame 30 onward carries shot 2's x, and the change happens at exactly frame 30.
- `test_per_frame_crop_interpolates_between_samples` — a subject whose smoothed centres move linearly across samples: the per-frame x at a quarter/half/three-quarter point between two samples is the linear interpolation of the bracketing samples' clamped xs (within rounding), and within a shot consecutive frames never jump by more than the inter-sample step.
- `test_sendcmd_writes_only_changes` — xs `[5,5,5,9,9,12]` at fps 10 → exactly two lines, for frames 3 and 5, formatted `<t:.6f> crop@rf x <n>;`, moves == 2; constant xs → `("", 0)`.
- `test_render_command_shape` — `render_command` is a `list[str]`: one `-i`, `sendcmd=f=` before `crop@rf=w=608:h=1080:x=<first>:y=0`, `scale=1080:1920:flags=lanczos`, `setsar=1`, `format=yuv420p`, `-map 0:a?` with `-c:a copy`, output path last; it is built, not run.
- `test_estimate_cost_60s_anchor` — `estimate_cost_usd(300) == 0.07` (within 1e-9); a 30-frame proxy bills the 50-frame minimum (`0.01`); `sam_window_spans(300)` matches the §7 span list.
- `test_cost_guard_refuses_above_cap` — `ensure_within_budget(0.07, 0.05, 300)` raises `ValueError` naming both amounts; `ensure_within_budget(0.07, 2.0, 300)` returns `None`; negative `--max-usd` is rejected by `run` before probing (asserted with a patched `probe_cut` that fails the test if called).
- `test_detections_from_result_scales_and_clips` — a stand-in result (`frame_size (960, 540)`, boxes in SAM pixels) against a 1920×1080 cut scales boxes ×2, clips an off-frame box into bounds, drops a degenerate box and an out-of-range frame, and stringifies a non-string `object_id`.
- `test_plan_from_stand_in_segment_windowed_result` — a `SimpleNamespace` result (detections, `frame_size`, `usd`, `frames_billed`, `saturated_spans()` lambda) fed through `detections_from_result` + `plan_shots` follows the synthetic speaker across two shots and centres a third, empty shot — the full planner path SAM would feed, with no `guestlist` import.
- `test_run_end_to_end_centred_without_sam` — temp dir, patched `probe_cut` (fake 1920×1080/30 fps/4 s media) and a patched `run_command` that records but does not execute: `run(no_sam=True, shots_path=<two-shot angles.json>)` writes a parseable `reframe.json` with `method "centre"`, both shots `centre`/`no_sam`, `spend_usd 0`, and no proxy command was issued (shots + no-SAM needs no proxy).
- `test_run_refuses_vertical_before_any_command` — patched `probe_cut` returning portrait media: `run` raises `ValueError` and neither `run_command` nor `track` is called.

Suite gate per the brief and `AGENTS.md`: the full
`python -m unittest discover -s tests -p 'test_*.py'` stays green
(skips for absent numpy/Pillow/ffmpeg/font prerequisites are normal).

---

## 16. Build order for the implementer

1. `reframe.py` pure core: constants, dataclasses, `crop_size`,
   `clamp_crop_x`, `fill_gaps`, `gaussian_smooth`, `sam_window_spans`,
   `estimate_cost_usd`, `ensure_within_budget`, `merge_close_cuts`,
   `build_shots`, `shot_sample_range`, `subject_for_shot`, `plan_shots`,
   `per_frame_crop_xs`, `sendcmd_text` — with their tests.
2. Borders: `parse_probe`/`probe_cut`, `load_shot_cuts`,
   `detections_from_result` — with their tests.
3. ffmpeg lists: `proxy_command`, `scene_cuts`/`refine_cut(s)`,
   `render_command`/`render` — command-shape tests only.
4. `track` (the sole `guestlist` import) and `run`; the CLI subcommand;
   the end-to-end centred test.
5. `BUILD-LOG.md` REFRAME section and the `AGENTS.md` Layout line;
   full-suite run.
