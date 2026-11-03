# Brief: cut to the camera I'm looking at, and match skin, not walls (`look`)

Written 2026-11-03 by the project owner with Claude Code for Meta's Muse Code agent, after the owner watched the
final take's cut (v2). Build it straight from this brief on Muse Spark. Read `AGENTS.md`,
`docs/muse-code/OPEN-BRIEF.md` (its Rules apply), then `src/director/angles.py`, `grade.py`, `sync.py` (the `lips`
method and `src/director/lips_helper.swift`), `cut.py` and their tests.

## What the owner saw (real footage: A7S III facing him, Mavic 3 at ~45°, lav)

1. **Wrong camera.** "When I look at one camera it should cut to that camera. Sometimes I look at one camera and it
   cuts to the other." Today Muse Spark picks the angle from one still per camera and judges framing; it does not
   know which lens the speaker is addressing. A speaker turning to a camera is the strongest editorial cue in a
   talking-head multicam.
2. **Colour still off on the DJI.** With `--grade-reference DJI_*`, matching gains were 1.11 / 1.25 / 1.07 and frame
   means matched exactly, yet the angles still read differently. Reason, measured: matching uses whole-frame
   statistics, and the two angles frame different things (the Mavic sees a bright wall and kitchen, the Sony is
   tight on the speaker), so equal frame means do not give equal skin. Matching is also gain-only (no contrast), and
   the Sony's S-Log3 conversion is a flat technical transform next to DJI's punchier LUT.

## The job

1. **Gaze per camera.** Extend the lips helper (or add a sibling helper with the same build-and-cache pattern) to
   print, per frame, the largest face's box plus Vision's `yaw` and `pitch` (and `roll`) where available. A frame
   is "addressed" to a camera when the face is near-frontal to that camera (small |yaw|, e.g. under 15–20°; set the
   threshold from the data, see below). Sample the frames each `angles` line covers (a few per line is enough), per
   camera, with the camera's LUT applied as `lips` does.
2. **Angle choice uses gaze.** Per spoken line: if exactly one camera sees the speaker clearly addressed for most of
   the line, choose it (a mechanical rule that Spark cannot overrule, recorded with its reason, e.g. "speaker
   addresses camera B: |yaw| 6° vs 41° on C"); when gaze is ambiguous, Spark decides as today, but its prompt now
   includes the per-camera gaze numbers. Keep every existing check (no cut inside a word, minimum shot length, merge
   rules). Add the gaze evidence to `angles.json` per line.
3. **Face-region colour match.** In `grade`, sample matching moments as today but measure statistics inside the
   speaker's face box (inner region, e.g. the central 60% of the box, so hair and background stay out) on each
   camera, through the camera's conversion cube. Match each camera to the reference on those skin statistics:
   gain per channel as today, plus contrast (a lift/gain pair from mean and standard deviation, clamped to
   sensible bounds), so skin brightness, warmth and contrast agree. Fall back to whole-frame matching with a
   warning when no face is found. Record before/after skin stats in `grade.json`.
4. **Check it on real footage.** The owner's job is at
   `/Volumes/Extreme SSD/Meta-Hackathon-2026-11-03/final/` (cards in `cards/`, the last job in `job/`). You may read
   that folder's media and run `director cut` on a COPY into a new folder under
   `/Volumes/Extreme SSD/Meta-Hackathon-2026-11-03/final/muse-look-test/` (never write into `job/` or `cards/`), with
   `--lut "DJI_*=/Volumes/Extreme SSD/Meta-Hackathon-2026-11-03/final/dji-dlog-to-rec709.cube" --grade-reference
   "DJI_*" --no-muse --no-sam --no-captions` if you need it offline. Note your sandbox may hide the GPU (Whisper
   skips) and the network (Muse calls fail); the job's existing `words.json`/`sync.json` can be reused with
   `--words`/the sync cache. Report the measured face yaw ranges per camera and the before/after skin stats.

## Rules

`OPEN-BRIEF.md` "Rules (hard)". Tests offline: helper-output parsing with yaw, the addressed-camera rule (clear
winner, ambiguous, no face), the Spark prompt carrying gaze numbers, face-box skin sampling on synthetic frames, the
gain+contrast solve and its clamps, and the whole-frame fallback. Never call the Meta API or Whisper in tests; never
commit media. Add a LOOK section to `docs/muse-code/BUILD-LOG.md` and a README line.
