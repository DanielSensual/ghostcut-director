# Brief: cut to the camera I'm facing, inside a line (`angles` gaze cuts)

Written 2026-11-04 by the project owner with Claude Code for Meta's Muse Code agent, after the owner watched the final
take in the app. Build it straight from this brief on Muse Wonder. Read `AGENTS.md`, `docs/muse-code/OPEN-BRIEF.md`
(its Rules apply), the gaze sections of `docs/muse-code/BUILD-LOG.md`, then `src/director/gaze.py`,
`src/director/angles.py` (line → shot planning, the minimum-shot merge, overrides), `src/director/sync.py`
(`LipsFrame`, `run_lips_helper`), `src/director/lips_helper.swift`, and their tests.

## What the owner saw

"In this shot I'm looking at the wrong camera. You have the drone shot — I don't know why you don't just use that one
when we start, and then cut to the other one right away."

The cut opens on the tripod camera B while he is plainly facing the drone C. What the job's `angles.json` says
(read-only job: `/Volumes/Extreme SSD/Meta-Hackathon-2026-11-03/final/job/`, files `angles.json`, `gaze-frames/`,
`tighten.json`, `words.json`):

- Line 0 "My mic" (0.7 s): 3 samples each camera; C yaw [0,0,0], B yaw [-45,-45,-45] → pick C. Correct.
- Line 1 "my drone has no microphone but this software lined it up here." (3.8 s): C yaw [0,45,45], B [-45,0,0] →
  majority B. The frames (`gaze-frames/G001_*`) show he faces the drone for "my drone has no microphone" and turns to
  B for "but this software lined it up here".
- Line 0 is shorter than the minimum shot, so it was merged with line 1 into shot 0 (0–5.73 s), which took the
  majority camera B. The owner wanted C for "My mic, my drone has no microphone", then B.
- Bug: shot `reason` strings are concatenated across merged lines and repeat ("speaker addresses camera C … speaker
  addresses camera B … camera B … camera B"). One clear reason per shot.

## The job

1. **Gaze changes inside a line become cut points.** Sample gaze densely enough to see a turn: about one frame every
   0.5 s per covering camera across the line (cap the count per line; record the sample times). Find where the
   addressed camera changes and hold for ≥ 1.0 s (hysteresis, no flicker), and split the shot there **on a word
   boundary** (words from `words.json`/the angles words, nearest gap between words to the turn). Both sides must still
   respect the minimum shot length; if a side would be too short, fold it into its neighbour on the same camera rather
   than overruling the gaze of the longer side.
2. **Merges keep the gaze.** When the minimum-shot rule merges short lines, the merged shot's camera is the camera the
   speaker addresses for the most *time* (sample-weighted by duration), not a per-line vote, and a split from (1) is
   preferred to a merge that would put the speaker on the wrong camera for more than 1 s.
3. **Better yaw.** Vision's face `yaw` comes back quantised (0°, ±45°). If the helper can give a continuous estimate
   (e.g. from the face landmarks: nose tip relative to the eye/cheek midline inside the face box), add it to the
   helper output and use it, keeping the coarse yaw as a fallback. Say what you measured. Swift changes must keep the
   helper building with `swiftc` as today.
4. **Overrides win.** Voice-note / `set_angle` overrides still beat gaze, including on a split shot.
5. **Check on the real job.** Copy the JSON files you need from the job into `/tmp/gazecut-job/` (never write into the
   real job) and run the shot planning (no `--render`). Report the first 30 s of shots before and after. If your
   sandbox cannot run the Vision helper or ffmpeg for new samples, test the planner on the existing three samples per
   line plus synthetic denser samples built from what the frames show, and say so; the owner's agent will run the live
   check.

## Rules

`OPEN-BRIEF.md` "Rules (hard)". Tests offline with stand-in samples: a turn mid-line splits on the word gap nearest the
turn; hysteresis ignores a 0.4 s glance; a short opening line merged with the next keeps the camera addressed longest;
an override beats gaze on a split shot; one reason per shot. Never call the Meta API or Whisper in tests. Add a
GAZECUT section to `docs/muse-code/BUILD-LOG.md` and update the README line about gaze.
