# Brief: try it in one command, no footage needed (`director demo`)

Written 2026-11-03 by the project owner with Claude Code for Meta's Muse Code agent. Build it straight from this
brief on Muse Spark. Read `AGENTS.md`, `docs/muse-code/OPEN-BRIEF.md` (its Rules apply), `README.md`, then
`src/director/cli.py`, `cut.py`, `sync.py`, `tighten.py` and `export.py`.

## Why

Judges and first-time users get a GitHub link, not the owner's camera cards. Today they can install Director and run
the tests, but they cannot see it do anything without their own multicam footage. One command has to show the whole
idea on any machine in a couple of minutes, without API keys.

## The job

`director demo [--out-dir director-demo] [--muse] [--keep]` that:

1. **Makes a tiny shoot** in the output folder with ffmpeg and nothing committed:
   - A spoken script of about 25–40 s with a deliberate restart in it, e.g. "GhostCut Director syncs every camera
     on your shoot. Sorry, let me do that again. GhostCut Director syncs every camera on your shoot, picks your best
     take, and hands you a timeline." Voice: macOS `say -o …` when available; else `espeak-ng`/`espeak` when installed;
     else a tone-burst stand-in with a supplied words file (and a printed note that the real voice needs `say` or
     espeak).
   - A "lav" WAV of that speech starting a few seconds early (as real recorders do).
   - Camera A: 1920×1080 `testsrc2`-style picture with a clearly drawn label "CAMERA A", with the speech at lower
     level, starting at a known offset.
   - Camera B: a differently coloured, differently framed picture labelled "CAMERA B", with the speech, starting at a
     different known offset.
   - Optionally camera C with no audio (picture only), to show how a silent camera is reported when no face/flash is
     available (it must be reported clearly, not silently mis-synced).
2. **Runs the real pipeline** on it: `director cut` with `--no-muse --no-sam` by default (no keys, no network), or
   with Muse models when `--muse` is given and `MODEL_API_KEY` is set. Whisper words come from local mlx_whisper when
   present; otherwise from the script's own word timings written by the demo (so it works on Linux and CI).
3. **Checks itself and prints a short, friendly report**: the offsets it found against the known ones (pass/fail
   within one frame), how many seconds the restart removal took out, the files to open (`cut.mp4`, `vertical.mp4`,
   `timeline.fcpxml`, `cut.json`), and the next commands to try (`director cut your-folder`, `director voice`, the app).
4. Exits non-zero when a check fails, so CI can run it.

## Rules

`OPEN-BRIEF.md` "Rules (hard)". Tests: the script/word-timing generator, the offset checks, the report text, and one
`skipUnless(ffmpeg)` end-to-end test that runs `director demo` (with the supplied-words fallback, no `say`, no
Whisper) in a temp dir and asserts it passes. Add a CI step that runs `director demo` with the fallback. Never call
the Meta API or Whisper in tests. Put "Try it in two minutes: `director demo`" near the top of the README, and add a
DEMO section to `docs/muse-code/BUILD-LOG.md` and a "Who built what" row.
