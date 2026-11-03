# Brief: a slow push-in on every shot (`--push-in`)

Written 2026-11-03 by the project owner with Claude Code for Meta's Muse Code agent. Build it straight from this
brief on Muse Spark. Read `AGENTS.md`, `docs/muse-code/OPEN-BRIEF.md` (its Rules apply), then `src/director/angles.py`
(the shot-by-shot render and `lut_filter_chain` usage), `src/director/grade.py` (`lut_filter_chain`, `FILL_OR_FIT`),
`src/director/cut.py`, `cli.py` and their tests.

## Why

The owner watched the final take (v3): "the video looks good, need a slight push-in effect on it." Static tripod
angles feel flat; a slow push-in (a creep toward the speaker) is what an editor adds by hand on every shot.

## The job

1. `director cut --push-in PERCENT` (and `director angles --push-in`), default **0** (off), suggested 3–6. Each
   rendered camera shot scales smoothly from 100% at its first frame to (100 + PERCENT)% at its last frame, centred
   on the speaker: use the face box when the job has one (the gaze/lips data or a face sample at the shot's middle),
   else the frame centre. Black shots are untouched.
2. Smooth: no stair-stepping or jitter. Do it after the grade and the fill-or-fit framing, in the same per-shot
   filter chain, at the output size. Measure smoothness: render a synthetic shot, compute frame-to-frame scale from
   the rendered frames and show it rises monotonically with no reversals. Prefer an approach that interpolates
   sub-pixel (e.g. render at 2× and downscale, or `zoompan` on an upscaled input, or `scale` with `eval=frame` plus
   `crop` with fractional offsets); choose by measurement and say why.
3. Shot boundaries stay frame-exact (`frame_grid`), durations unchanged; QC's duration checks must still pass.
4. Record `push_in` in `angles.json` and `cut.json`; it is part of the angles render fingerprint.

## Rules

`OPEN-BRIEF.md` "Rules (hard)". Tests offline: the filter text for on/off, centring on a face box vs centre, and one
`skipUnless(ffmpeg)` test that renders a short synthetic shot with a push-in and checks duration, that the last frame
is zoomed relative to the first, and monotonic smoothness. Never call the Meta API or Whisper. Add a PUSH section to
`docs/muse-code/BUILD-LOG.md` and a README line.
