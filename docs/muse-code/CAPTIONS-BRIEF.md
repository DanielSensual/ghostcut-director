# Brief: burned-in captions (`captions`)

Written 2026-11-03 by the project owner with Claude Code for Meta's Muse Code agent. Small enough to build straight
from this brief on Muse Spark. Read `AGENTS.md`, `docs/muse-code/OPEN-BRIEF.md` (its Rules apply), then
`src/director/cut.py`, `cutlist.py` (source-to-output time mapping), `tighten.py`, `transcribe.py` (word fields
`speaker`, `heard_by`, `disfluency`), `finish.py`, `reframe.py`, `export.py` and `cli.py`.

## Why

Most of the owner's deliveries are Reels and Shorts, and every one needs burned-in captions. Today they are done by
hand. Director already has exact word times on the shared timeline, the verbatim Muse Voice Transcribe text, and the
take picks that say which words survive into the cut.

## The job

1. A `captions` stage in `director cut` after `reframe` (and a `director captions` command for an existing job):
   - Words on the **output** timeline: only words inside kept spans, mapped through the tighten/cut mapping (or the
     identity when there is no `tighten.json`). Leave out words only Muse heard (`heard_by: "muse"`) and fillers
     marked as disfluencies; captions show what the viewer hears, cleanly.
   - Group into short lines: 1–4 words, never across a cut point, a removed span or a pause over 0.6 s; each line on
     screen from its first word's start to its last word's end (minimum 0.4 s), no gaps under 0.1 s between lines.
   - Write `captions.srt` and `captions.ass` in the job folder (the editor can import SRT into Final Cut or Resolve).
   - Burn them into `vertical.mp4` by default (re-encode video only, copy audio), and into the horizontal master only
     with `--captions-horizontal`. Keep the un-captioned files as `*.clean.mp4`.
   - Style (ASS, libass): a heavy sans available on macOS (e.g. "Avenir Next" Heavy or "Helvetica Neue" Bold, with a
     fallback), white text, thick dark outline, soft shadow; the word being spoken highlighted (karaoke `\k` timing or
     one event per word), sized for 1080×1920 at about 7% of frame height; placed in the 9:16 safe zone (centre at
     about 62% of the height, clear of the bottom 20% where platform UI sits); horizontal: lower third. A
     `--caption-style` flag with two or three named presets is enough.
2. `cut.json` records the stage, `--no-captions` skips it, fingerprints follow the existing pattern, and the QC
   stage checks captions exist and that every caption lies inside the video's duration.

## Rules

`OPEN-BRIEF.md` "Rules (hard)". Tests offline: grouping and timing rules on synthetic words, the output-time mapping
with a synthetic `tighten.json`, SRT/ASS text, the ffmpeg command shape, and one `skipUnless(ffmpeg)` test that burns
captions onto a short `lavfi` clip and checks the output duration and that frames differ where a caption shows. No
Meta API, no Whisper, no media committed. Add a CAPTIONS section to `docs/muse-code/BUILD-LOG.md`, a README line and
a "Who built what" row.
