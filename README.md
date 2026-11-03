# GhostCut Director

I cut talking video shot on several cameras. GhostCut Director is a Mac app and a command line that do that work: Muse Spark picks the takes and the angle for every spoken line, each camera is graded to match, and SAM 3.1 follows the speaker for the vertical version.

## One command: `director cut`

```bash
director cut /path/to/card-dump --out-dir job1
# or, explicitly:
director cut cam-a.mp4 cam-b.mp4 lav.wav --out-dir job1
```

That runs the whole job in one output directory: sync puts every clip on one timeline, the audio clip's word timings are made explicit in the shared timebase, take picking keeps the best take of every beat and drops the flubs, each camera is graded to match, Muse Spark picks the angle for every spoken line and renders the horizontal master `cut.mp4` to publish, the same shots export as `timeline.fcpxml` for Final Cut Pro and `timeline.otio.json` for OTIO/Resolve tools, SAM 3.1 follows the speaker for the 9:16 `vertical.mp4`, and a whole-cut check verifies the result. `cut.json` records what ran, what it cost and what QC found. `director cut --no-muse --no-sam` runs fully offline (rule picks, centred vertical, mechanical QC only), and re-running the same command resumes: stages whose inputs did not change are skipped.

```bash
director cut /path/to/card-dump --out-dir job1 --pause-limit 0.8 --target 3:00
director cut /path/to/card-dump --out-dir job1 --no-tighten   # keep the whole recording
```

`--target` shortens the kept cut to a length by dropping whole beats only, never mid-sentence; `--pause-limit` shortens pauses inside kept takes to 0.35 s.

Two things to know before finishing in an NLE: the timelines reference the camera originals, so they open showing log/HLG picture — the per-camera `.cube` files `grade` wrote sit beside them in the job directory (`cut.json` names which cube belongs to which camera). And each timeline clip carries its own camera's audio; the rendered `cut.mp4`/`vertical.mp4` remain the masters with the production audio laid under them, so an editor rebuilding the mix re-lays the audio clip at the offset recorded in `angles.json`/`cut.json` (`audio.offset_s`).

## Running the stages by hand

The same stages `director cut` runs, one command at a time. File names below are placeholders.

```bash
director sync a-cam.mp4 b-cam.mp4 lav.mp3 --out sync.json
```

Places every clip on one shared timeline (`sync.json`) from timecode, capture-clock, waveform and Whisper offsets, checked against each other.

```bash
director grade --sync sync.json --out grade.json
```

Writes a per-camera `.cube` from published curves (S-Log3, HLG) and matches exposure and white balance to a reference camera.

```bash
director tighten --sync sync.json --words words.json --out tighten.json
```

Director keeps one take per beat, lists every removed span with its reason in `tighten.json`, shortens pauses over the limit to 0.35 s, and both picture and sound are cut at word boundaries with a short fade so joins do not click. Muse Spark picks the takes by watching a proxy beside the numbered transcript (the rule fallback with `--no-muse` keeps the last full pass of each beat); pass the result to `angles` with `--tighten tighten.json`.

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
| One-step multicam cut (`director cut`) | Designed by Muse Code on muse-wonder-1.0, built by Muse Code on muse-spark-1.3 |
| Take picking (`director tighten`, the `tighten` stage of `director cut`) | Designed by Muse Code on muse-wonder-1.0, built by Muse Code on muse-spark-1.3 |

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
