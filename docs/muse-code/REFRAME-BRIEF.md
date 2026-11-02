# Brief: vertical reframe with SAM 3.1 (`director reframe`)

Written 2026-11-02 by the project owner with Claude Code for Meta's Muse Code agent. Muse Wonder writes the design
(`docs/muse-code/REFRAME-DESIGN.md`), then Muse Code builds it on Muse Spark. Read `AGENTS.md` first and follow it.

This repo is new. A reframe existed in an earlier repo; **do not look for it or read it**. Write this from the
brief. Read only files inside this repository, the installed `ghostcut` package (`ghostcut.ffmpeg_tools`:
`run_command`, `probe_media`, `extract_scene_change_pts`, `ToolingError`; `ghostcut.config.write_json`) and the
installed `guestlist` package's public call described below.

## The job

`director reframe cut.mp4 --out reframe.json [--render vertical.mp4]` turns a finished 16:9 talking cut into a 9:16
one that keeps the speaker in frame. SAM 3.1 (Meta's segmentation model, through the Meta Model API) tracks every
person, and a smoothed crop follows the right one. A cut is allowed to jump the crop; a shot never pans abruptly.

## Pipeline

1. **Probe** the cut: width, height, fps, duration. Refuse a cut that is already vertical.
2. **Proxy.** SAM bills per frame, so track a small constant-rate proxy: 5 fps, short side 540 px, video only, no
   metadata. 5 fps follows a seated or walking speaker fine.
3. **Shots.** With `--shots angles.json`, the cut times are the `start_s` of every shot after the first (exact; that
   file comes from `director angles`). Otherwise use ffmpeg scene detection (score > 0.30) on the proxy. Proxy
   times are only good to its 0.2 s frame, so re-run detection on the full-rate cut in a ±0.5 s window around each
   one (scaled down, for speed) and take the nearest hit. A cut that lands a frame late shows the old shot's crop
   on the new shot. Cuts closer together than 0.25 s merge.
4. **Track** (unless `--no-sam`), with `guestlist` imported inside the function and guarded:
   ```python
   from guestlist.windows import segment_windowed
   result = segment_windowed(proxy, "person", fps=5, frames=n_proxy_frames,
                             max_usd_per_call=max_usd, workdir=work / "windows")
   # result.detections: each has .frame (proxy frame index), .object_id (stable across the cut),
   #                    .box (x0, y0, x1, y1 in SAM's frame pixels), .confidence
   # result.frame_size: (w, h) of SAM's frames, to scale boxes back to the cut's pixels
   # result.usd, result.frames_billed, result.saturated_spans() (windows that hit SAM's 16-id cap)
   ```
   It runs SAM in 10 s windows with 1 s overlaps and stitches ids, because one SAM request holds at most 16 object
   ids. Without guestlist, or with `--no-sam`, every shot is centred and a warning says why.
5. **Cost guard.** Estimate before calling: $0.20 per 1,000 SAM frames, each request billed at least 50 frames,
   windows of 10 s overlapping by 1 s (about 7 cents for a 60 s cut). Refuse with a clear error above `--max-usd`
   (default 2.0).
6. **Subject per shot:** the person on screen the most and the largest. Follow them only if they appear in at least
   half the shot's samples and stand at least a quarter of the frame tall. Otherwise the shot is centred, with the
   reason recorded (a passer-by, a tiny background figure, B-roll).
7. **Path.** The subject's box centre at each sample. Fill gaps by straight-line interpolation inside the shot and
   hold the ends. Smooth with a **zero-phase** Gaussian (sigma 0.6 s; zero-phase, so the crop never lags the
   speaker), inside each shot only, so the path never smears across a cut.
8. **Crop.** 9:16 from the full height, width rounded to an even number (608 px at 1080p, 1216 at 2160p), x clamped
   inside the frame. One crop x per **output frame**, interpolated between proxy samples, and jumping exactly at
   each cut frame.
9. **Render** (`--render`): one ffmpeg pass. A `sendcmd` script changes the x of a named crop filter
   (`crop@rf`), writing a line only when x changes, then `scale=1080:1920` (lanczos), `setsar=1`, `yuv420p`. The
   audio is stream-copied untouched.

## Output `reframe.json`

The cut's path, size, fps and duration; the crop size; every shot (start, end, method `subject` or `centre`, the
followed object id, its presence share, the reason); the smoothed centre per proxy sample; the method (`sam` or
`centre`); spend; frames billed; warnings. With a render, also the render path, output size, frame count and the
number of crop moves. Work files (proxy, crop script, SAM window cache) go in `<out dir>/<cut stem>.reframe/`.

## CLI

`director reframe VIDEO --out reframe.json [--render vertical.mp4] [--shots angles.json] [--no-sam]
[--max-usd 2.0]`

Print the shots (method and reason), the spend and where it wrote.

## Tests (offline: no media files, no network, no Meta API)

Write them in the style of the existing tests. They must cover:
- the crop size and clamping;
- cuts from an `angles.json`;
- shot building and near-duplicate cuts;
- sample ranges per shot;
- subject choice: the most present, a passer-by refused, a tiny background figure refused, an empty shot;
- gap filling;
- the smoothing: no lag on a ramp, a constant kept, jitter removed;
- each shot planned on its own;
- the per-frame crop jumping exactly at a cut and never inside a shot;
- interpolation between samples;
- the sendcmd script writing only changes;
- the render command (crop, scale, audio copy), built as a list and not run;
- the cost estimate;
- the guard refusing above the cap;
- a stand-in `segment_windowed` result through the planner.

The whole suite must stay green.

## Limits

- Stdlib-only. guestlist is optional and imported inside the tracking function only.
- Do not change `angles.py`, `grade.py`, `sync.py` or `app/`.
- No client names, people's names, footage paths or keys in code, tests or docs.
- Add a REFRAME section to `docs/muse-code/BUILD-LOG.md` and a line to `AGENTS.md` under Layout.
