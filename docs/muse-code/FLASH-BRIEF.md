# Brief: light-flash sync for cameras with no audio (`flash`)

Written 2026-11-03 by the project owner with Claude Code for Meta's Muse Code agent. Small enough to build straight
from this brief on Muse Spark (no separate design). Read `AGENTS.md` and `docs/muse-code/SYNC-BRIEF.md` first, then
`src/director/sync.py` and `tests/test_sync.py` in full.

## Why

The owner's demo shoot uses three cameras: a Sony A7S III with the lav, an iPhone, and a DJI Mavic 3 drone sitting on a
table as a static wide. The drone records no audio, so `waveform` and `whisper` cannot place it, and `clock` is only
good to about 2 s. Lip sync needs a frame. A drone, action camera or any silent camera has the same problem.

The shoot will start with a slate: every camera rolling, the room light switched **off for about a second and back on**
(maybe twice), then a clap. Every camera sees the light change at the same instant.

## The job

Add a fifth sync method, `flash`, to `src/director/sync.py`:

1. Per clip, measure mean luma per frame over the first `FLASH_WINDOW_S` seconds (default 120) of the clip, from a tiny
   grey proxy decoded by ffmpeg (e.g. `scale=64:-2,format=gray`, raw frames on a pipe, with each frame's real
   timestamp, so variable-frame-rate phone clips are timed correctly). numpy is optional in this repo: guard it the way
   the waveform method does.
2. Find light-switch edges: frame-to-frame luma steps that are large against the clip's own frame-to-frame noise
   (a robust z-score), signed (off = down, on = up). The edge time is the timestamp of the first frame after the step.
3. Match edge sequences between a target clip and its anchor: same signs in the same order with the same gaps
   (tolerance one frame of the slower clip plus a small margin). The offset is the median of the matched edge time
   differences. Several matched edges raise confidence; a single edge is low confidence. Where a `clock` estimate
   exists, prefer the edge match consistent with it.
4. Precision: one frame of the slower clip. It joins the existing estimate list, the pairwise cross-checks and the
   resolution order exactly like the other methods. Read how `resolve` or its equivalent ranks methods: audio methods
   still win when they have an answer; `flash` places clips the audio methods cannot, and cross-checks the ones they
   can (a disagreement over two frames becomes a warning).
5. Add `flash` to `METHODS` so it runs by default, after `waveform` and before `whisper` in the canonical order if that
   order matters for resolution, and make `--methods` accept it. Keep every existing JSON field and test passing;
   update tests that assert the exact METHODS tuple.
6. Warnings in plain English: "Clip C has no audio and no light flash was found: placed by capture clock only (±2 s);
   lip sync on its shots will be off." and the like.

## Rules

`OPEN-BRIEF.md` "Rules (hard)" apply. Tests: synthetic luma sequences for the edge finder and matcher (pure functions),
plus one ffmpeg test behind `skipUnless(ffmpeg)` that makes two short `lavfi` clips with a brightness drop at different
times (e.g. `color` sources with an `eq`/`geq` or `drawbox` step via `enable='between(t,a,b)'`), one with no audio,
and checks the placed offset to within a frame. Never call Whisper or the Meta API. Touch only `src/director/sync.py`,
`tests/test_sync.py`, the SYNC section of `docs/muse-code/BUILD-LOG.md` (add a "flash method" subsection: built by
Muse Code on muse-spark-1.3 from this brief, counts TBD) and one line in the README pipeline section.
