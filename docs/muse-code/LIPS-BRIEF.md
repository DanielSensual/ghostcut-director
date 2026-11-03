# Brief: lip-reading sync for cameras with no audio (`lips`)

Written 2026-11-03 by the project owner with Claude Code for Meta's Muse Code agent, from a prototype Claude Code
validated on the owner's real shoot the same afternoon. Small enough to build straight from this brief on Muse Spark.
Read `AGENTS.md`, `docs/muse-code/OPEN-BRIEF.md` (its Rules apply), `docs/muse-code/SYNC-BRIEF.md` and
`docs/muse-code/FLASH-BRIEF.md`, then `src/director/sync.py` and `tests/test_sync.py` in full.

## Why

Drones, action cameras and many B-cameras record no usable audio. Syncing them to the main audio by hand is the
worst part of a multicam edit: a realtor on a rooftop saying "let me show you this house" on the drone, the voice on
the lav. A slate is often missing (today's shoot had none), and camera clocks lie: today the drone's clock was
**476 s** off (no GPS indoors). But a camera that sees the speaker's face can be synced from the lips.

## What was measured (2026-11-03, real footage: A7S III front, Mavic 3 at 45°, D-Log; lav recorder)

- Mouth opening per frame = Apple Vision `VNDetectFaceLandmarksRequest` inner-lip height (max − min y of
  `innerLips.normalizedPoints`) on the largest face. Face found in 99–100% of frames on both cameras.
- Signal prep (both lips and audio): 3-frame moving average, minus a 1 s (24-frame) moving average, z-score.
- Audio: band-pass 300–3000 Hz, RMS per video frame, log, then the same prep.
- Score: mean product (Pearson on z-scored series) at each lag; take the best lag; confidence = best / best score
  more than 0.5 s away ("ratio").
- Results over ~270 s windows:
  - A7S III lips vs lav: offset within **0.1 frame** of the waveform truth, ratio **1.95**. On 60 s windows the
    ratio was only 1.07: use at least ~3 minutes of speech.
  - Mavic lips vs lav, after cropping to the face at full resolution and applying the camera LUT: ratio **2.41**.
    On the downscaled full 5.1K frame and searched in the wrong place it failed.
  - **Mavic lips vs A7S III lips: r = 0.93, ratio 9.70**, agreeing with lips-vs-lav to 0.021 s. Two views of the same
    mouth are the strongest signal; prefer it whenever another placed camera sees the same face.
- Lesson: the clock-based search window was the reason for every early failure. Search the whole plausible overlap
  (both clips' full lengths), using the clock only as a tie-breaker, never as a hard window.

## The job

Add a sync method `lips` to `src/director/sync.py`:

1. A small Swift helper (`src/director/lips_helper.swift`, shipped as source) that reads JPEG frames and prints one
   line per frame: index, inner-lip opening, face box (x, y, w, h normalised), or a no-face marker. Build it on first
   use with `swiftc -O` into a cache directory (e.g. `~/Library/Caches/ghostcut-director/`) keyed by the source hash;
   if `swiftc` or Vision is unavailable (not macOS, no Xcode command-line tools), the method is skipped with a plain
   warning. No pyobjc dependency.
2. Per clip that has picture: a coarse pass on a low-resolution sample to find the face box; then frames at the clip's
   own rate, cropped around the median face box (with margin) at full resolution, with the camera's LUT applied when
   `grade.json` or a supplied `.cube` exists for it, scaled so the face is ~400 px tall, written to a temp dir.
   Limit the analysed span (default 300 s of the clip with the most speech), configurable.
3. Correlate as measured above: lips vs lips with every placed camera that has a face, and lips vs the audio of every
   placed clip with audio. Search lags over the whole possible overlap. Report each as an estimate with confidence
   from the ratio (below 1.5 = low, ≥ 2 = good), precision one frame.
4. It joins the existing estimates and cross-checks like `flash`. Resolution order: waveform → whisper → lips → flash
   → timecode → clock (lips is more robust than flash in daylight and needs no slate). A disagreement over two frames
   with another method becomes a warning.
5. Add `lips` to `METHODS` after `waveform` (it runs only for clips audio cannot place, plus as a cross-check on one
   audio-placed camera when cheap), make `--methods` accept it, and say in `cut.json` warnings which clips were
   placed by lips and with what ratio.
6. Spend nothing: this is local (Vision on the Mac). Time budget: print a progress line; ~1 ms per frame for Vision at
   400 px faces is typical.

## Rules

`OPEN-BRIEF.md` "Rules (hard)". Tests: pure functions on synthetic series (signal prep, lag search over the full
overlap, ratio, choosing lips-vs-lips over lips-vs-audio, the clock as tie-breaker only), the helper-output parser,
the skip path when `swiftc` is missing (mock `shutil.which`), and one macOS-only test (`skipUnless` darwin and
`swiftc`) that builds the helper and runs it on a synthetic image with no face (expects the no-face marker). Never
call the Meta API or Whisper. No real footage in tests. Touch only `src/director/sync.py`, the new helper,
`tests/test_sync.py`, the SYNC section of `docs/muse-code/BUILD-LOG.md` (a "lips method" subsection: brief and
validated recipe by the owner with Claude Code, built by Muse Code on muse-spark-1.3, counts TBD) and the README
pipeline line.
