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
  - `grade.py`: per-camera `.cube` from published curves (S-Log3, HLG) → probed decode range → exposure and white
    balance matched to a reference → optional Muse check.
  - `sync.py`: timecode, capture-clock, waveform and Whisper offsets per clip → checked against each other →
    one shared timeline in `sync.json`.
  - `reframe.py`: finished 16:9 cut → shot cuts (angles.json or scene detection) → SAM 3.1 person tracks
    (guestlist, optional) → smoothed per-shot crop path → 9:16 `reframe.json` + optional render.
  - `cut.py`: one command from files or a folder to the whole job (`sync` → words → `grade` → `angles` → timeline export → `reframe` → whole-cut QC) in one output directory, resumable via `cut.json` fingerprints.
  - `export.py`: `angles.json` shots → FCPXML and OTIO timelines through the installed `ghostcut` exporters.
- `app/` — the Director Mac app: `main.py` (pywebview window), `engine.py` (local server), `muse.py` (Muse edits),
  `render*.py` (re-render accepted edits, verify, promote, revert), `make_demo.py` (timeline from a cut).
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
