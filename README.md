# GhostCut Director

I cut talking video shot on several cameras. GhostCut Director is a Mac app and a command line that do that work: Muse Spark picks the takes and the angle for every spoken line, each camera is graded to match, and SAM 3.1 follows the speaker for the vertical version.

## The pipeline

Four commands, in order. File names below are placeholders.

```bash
director sync a-cam.mp4 b-cam.mp4 lav.mp3 --out sync.json
```

Places every clip on one shared timeline (`sync.json`) from timecode, capture-clock, waveform and Whisper offsets, checked against each other.

```bash
director grade --sync sync.json --out grade.json
```

Writes a per-camera `.cube` from published curves (S-Log3, HLG) and matches exposure and white balance to a reference camera.

```bash
director angles --sync sync.json --words words.json --out angles.json --grade grade.json --render cut.mp4
```

Picks the camera for each spoken line (Muse Spark chooses from one frame per camera; mechanical checks overrule) and renders the cut shot by shot under the uncut audio.

```bash
director reframe cut.mp4 --out reframe.json --shots angles.json --render vertical.mp4
```

Reframes the finished 16:9 cut to 9:16: shot cuts come from `angles.json` or scene detection, SAM 3.1 tracks the speaker, and a smoothed per-shot crop path is written to `reframe.json`.

## The Director app

The app is the same cut with a review step. Muse proposes timeline edits and I accept the ones I want; Render re-renders only what changed into the next version; about 35 checks run before a new version replaces the current one, and revert goes back to the previous version.

```bash
python app/main.py
```

That starts the local engine on a free port and opens the UI in a native window (see [app/main.py](app/main.py); it needs `pywebview`). The app needs a project made by the Director test run (`scripts/`), and this repo does not ship one.

## Meta AI used

- Muse Spark: take picks, proposed timeline edits, the angle choice for every line, and the grade-match check.
- Muse Wonder: the designs for sync, angles, grade and reframe.
- Muse Code: designed and built sync, angles, grade and reframe (see Who built what).
- SAM 3.1: person tracking for the vertical reframe.

The Meta Model API calls go through the optional `guestlist` package: `pip install -e ".[muse]"`, key from the `MODEL_API_KEY` env var. Everything else works without it.

## Who built what

From [docs/muse-code/BUILD-LOG.md](docs/muse-code/BUILD-LOG.md) and the git log, nothing else.

| Piece | Who |
|---|---|
| Multicam sync | Designed by Muse Code on muse-wonder-1.0, built by Muse Code on muse-spark-1.3; four defects found on real audio by the owner and Claude Code and fixed by Muse Code |
| SAM 3.1 vertical reframe | Designed by Muse Code on muse-wonder-1.0, built by Muse Code on muse-spark-1.3, live-tested by Claude Code |
| Angle choice and per-camera grade | Designed and built by Muse Code, reviewed and fixed by Claude Code (in GhostCut, then imported) |
| The Director app, Render and the test-run scripts | Built by Claude Code with the owner (imported) |
| The briefs | The owner with Claude Code |

Sync and reframe were first built earlier in the event by Claude Code, inside GhostCut, where they are still public. The versions here are Muse Code's rewrites: it got only the briefs, never that code, and its run logs show no reads outside this repository and its installed packages. Both were checked against the same real recordings and cut ([BUILD-LOG](docs/muse-code/BUILD-LOG.md), Live test).

Muse Code spend at list rates (cached input at the cached rate): about $5.19 for sync ($2.11 + $2.07 + $0.67 + $0.34) and about $2.34 for reframe ($1.46 + $0.88). Angles and grade cost at most about $6.20 and $9.60, counted with no cache discount.

## Context

Built during the Meta Global AI Developer Hackathon (Oct 26 – Nov 4, 2026), on top of [GhostCut](https://github.com/DanielSensual/ghostcut), installed as a dependency pinned to its release from before the event plus its in-event export fixes. Everything in this repo is in-event work.

## Install and test

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[director]"
pip install -e ".[muse]"                              # optional: the Meta Model API client
python -m unittest discover -s tests -p 'test_*.py'
```

Tests self-skip without numpy, Pillow or ffmpeg. A green run with skips is normal.

## License

MIT (see LICENSE).
