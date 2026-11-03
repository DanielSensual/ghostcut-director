# GhostCut Director — Agent Guide

Everything in this repo was built during the Meta Global AI Developer Hackathon (Oct 26 – Nov 4, 2026). It builds
on [GhostCut](https://github.com/DanielSensual/ghostcut), which is installed as a dependency pinned to the commit
before this work began, plus its in-event export fixes (`be52bc8`). Python ≥3.11, ffmpeg/ffprobe on PATH.

## Zero to running

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[director]"                          # numpy + Pillow for the app and its tests
pip install -e ".[muse]"                              # optional: the Meta Model API client (guestlist)
python -m unittest discover -s tests -p 'test_*.py'
```

Tests self-skip when their prerequisites (numpy, Pillow, ffmpeg, the macOS card font) are absent. A green run with
skips is normal.

## Layout

- `src/director/` — the command line (`director …`, `cli.py`):
  - `angles.py`: lines from the audio clip's words → covering cameras per line → Muse Spark picks from one frame
    per camera (guestlist, optional) → mechanical checks overrule → shot-by-shot render under the uncut audio.
    Consumes `tighten.json` when given (`--tighten`): lines per kept span, shots on the output timeline.
  - `cutlist.py`: the shared "what stays" contract: `tighten.json` load/write, span recomputation, source/output
    time mapping, and `apply_operations`, the one edit path.
  - `tighten.py`: words → utterances → windows → Muse Spark take picks (guestlist, optional) or the rule fallback
    (last full pass per beat) → `tighten.json` with kept spans, removed spans with reasons, beats and alternates.
  - `grade.py`: per-camera `.cube` from published curves (S-Log3, HLG) → probed decode range → exposure and white
    balance matched to a reference → optional Muse check.
  - `sync.py`: timecode, capture-clock, waveform and Whisper offsets per clip → checked against each other →
    one shared timeline in `sync.json`.
  - `reframe.py`: finished 16:9 cut → shot cuts (angles.json or scene detection) → SAM 3.1 person tracks
    (guestlist, optional) → smoothed per-shot crop path → 9:16 `reframe.json` + optional render.
  - `cut.py`: one command from files or a folder to the whole job (`sync` → words → `grade` → `angles` → timeline export → `reframe` → whole-cut QC) in one output directory, resumable via `cut.json` fingerprints. The words stage enriches with Voice Transcribe when available.
- `transcribe.py`: the production-audio clip → Voice Transcribe turns (16 kHz WAV, silence chunks, speakers reconciled) aligned onto Whisper word times → enriched `words.json` + `transcript.json` (`director transcribe`, the words stage of `director cut`).
- `captions.py`: heard words on the output timeline → short karaoke lines → `captions.srt` + house-style `captions.ass` (burned into `vertical.mp4`, `*.clean.mp4` kept) and a serif title PNG for the first 3.5 s, explicit or one checked Muse Spark proposal (`director captions`, the `captions` stage of `director cut`).
- `voice.py`: a spoken note → Meta transcription (local Whisper fallback) → notes anchored by playhead, spoken timecode or quoted words → Muse Spark resolves each to checked `cutlist` operations → `voice.json`, applied with `--apply` (`director voice`, the app's mic and memo drop).
  - `export.py`: `angles.json` shots → FCPXML and OTIO timelines through the installed `ghostcut` exporters.
  - `connect.py` + `elevenlabs.py` + `higgsfield.py`: the outside-service registry — env-or-Keychain keys, cheap no-generation test calls, key-free messages. ElevenLabs is sound effects, music and voice isolation over stdlib HTTP; Higgsfield stores a key and tests it only (no finishing capability yet).
  - `finish.py`: the finished cut's voice cleaned locally (or isolated when connected and asked) → Muse Spark places a bed and effects (guestlist, optional) → the provider generates them → ducked under the voice → two-pass master → remuxed as `cut.mp4`'s audio. `finish/` keeps every asset with its licence note; `finish.json` records the plan, the measured loudness and every fallback.
- `app/` — the Director Mac app: `main.py` (pywebview window), `engine.py` (local server), `muse.py` (Muse edits), `cutjob.py` (opens a `director cut` job as a project, re-renders it),
  `render*.py` (re-render accepted edits, verify, promote, revert), `make_demo.py` (timeline from a cut), the Connections panel (outside-service keys, per machine).
- `scripts/` — the first end-to-end Director test run and the day-1 take-picking spike.
- `docs/` — design docs, Muse Code briefs and build logs, outside reviews.

## Conventions

- Optional features go behind extras with guarded imports and `@unittest.skipUnless` tests.
- The Meta API client is `guestlist`: import it inside the function that calls Muse or SAM, guarded, so the rest
  works without it. Never call the Meta API from tests.
- API keys come from env vars only; never write keys into code, docs or examples.
- No media files, client names, people's names or local footage paths in code, tests or docs.
- Type-annotated, `from __future__ import annotations`, dataclasses at module boundaries.

## Provenance

Every piece names who wrote it. Keep that true: `docs/muse-code/BUILD-LOG.md` and the README's table record which
parts Meta's Muse Code designed and built, and which were built by Claude Code with the owner. Add a row when you
add a piece.

## Verify before you claim done

`python -m unittest discover -s tests -p 'test_*.py'` must be green. For ffmpeg-facing changes, run the command
on a short real clip and look at the output: filter-graph regressions rarely show up in unit tests.
