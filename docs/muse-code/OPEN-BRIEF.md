# Open brief: make Director usable by a working editor

Written 2026-11-03 by the project owner with Claude Code for Meta's Muse Code agent. Unlike the other briefs in this
folder, this one does not say what to build. **Muse Code decides.** Muse Wonder chooses the feature or features and
writes the design (`docs/muse-code/OPEN-DESIGN.md`); Muse Code then builds that design on Muse Spark. Read
`AGENTS.md` first and follow it.

## Who uses this

A video editor who shoots talking video (interviews, founder talks, podcasts, real-estate walkthroughs) on two or
three cameras: typically a Sony A7S III or FX3 in S-Log3, a second Sony, an iPhone shooting HLG, sometimes a separate
lav or recorder. They dump the cards into one folder. They want a finished cut, horizontal and vertical, that they can
publish or finish in Final Cut Pro, DaVinci Resolve or Premiere. They have never seen this repository.

The owner will use whatever you build tomorrow, on camera, to cut the 3-minute demo video for the Meta Global AI
Developer Hackathon: a founder talk shot on two or three cameras, cut by Director. So it has to work on real footage,
not only in tests.

## What exists

Read the code; it is all in this repository. In short:

- `director sync` puts every clip on one timeline (`sync.json`).
- `director grade` writes a per-camera `.cube` and matches cameras (`grade.json`).
- `director angles` picks the camera for each spoken line with Muse Spark and renders the cut (`angles.json`, MP4).
- `director reframe` makes the 9:16 version with SAM 3.1 (`reframe.json`, MP4).
- `app/` is a Mac app (pywebview) where Muse proposes timeline edits and the editor accepts or rejects them, then
  Render re-renders what changed. It was built for an earlier single-camera pipeline.
- The installed `ghostcut` package (pinned) has `export_fcpxml`, `export_otio`, `ffmpeg_tools`, transcriber adapters
  and more. Reuse it where it fits; read it, do not modify it.
- The installed `guestlist` package is the Meta Model API client (Muse Spark, Muse Wonder, SAM 3.1, Voice
  Transcribe, Muse Image). Its public calls are what the existing modules already use; read its source for the rest.

## What we noticed (observations, not instructions)

Walking through it as that editor, the owner and Claude Code hit these. You may pick any of them, several, or
something else you find more important. Judge for yourself.

- There is no one-step path. The editor has to chain four commands and know the order and the JSON files between
  them.
- `director angles --words` needs a word-timed Whisper JSON for the audio clip, and no command makes one. `sync`
  can transcribe internally (mlx_whisper) and caches it, but that is invisible to the user.
- The multicam cut only comes out as a rendered MP4. There is no Final Cut / Resolve / Premiere timeline, so an
  editor cannot take Director's choices and refine them in the tool they already use.
- The Mac app cannot open the output of `sync` + `angles`. It reads projects made by the earlier single-camera test
  run, which this repo does not ship.
- There is no way to try any of it without your own multicam footage.
- Muse is used for one decision per line (which camera). An editor also decides what to cut out entirely: flubs,
  restarts, long pauses, "can we do that again", the crew talking. None of that happens today.
- Nobody checks the finished cut as a whole before it is handed over.

## Freedom

- Choose what to build. Aim for the change that makes the biggest difference to that editor getting from a folder of
  cards to a cut they would publish or finish. Prefer one complete, working, well-tested thing over several half
  things.
- Use the Meta models where they genuinely make the result better, in ways a judge would not expect from a basic
  integration (video and image input, structured output, SAM 3.1, Voice Transcribe, tool use). Do not add a Meta call
  for show.
- You may add new modules, commands and app screens, and change existing modules where your feature needs it. Keep
  every existing command and file format working, and keep the existing tests green.
- Scope it so it can be built and tested in one build run on Muse Spark.

## Rules (hard)

- Everything stays in this repository. Do not read anything outside it except the installed packages (`ghostcut`,
  `guestlist`, the standard library, numpy, Pillow).
- Tests are offline: no network, no Meta API, no Whisper. A test may make short synthetic media at run time with
  ffmpeg's `lavfi` sources (`testsrc2`, `sine`, `anullsrc`) into a temp directory, behind
  `@unittest.skipUnless(shutil.which("ffmpeg"), ...)`. Never commit media files.
- Meta API calls go through `guestlist`, imported inside the function that needs it, guarded, with a clear fallback
  or message when it is missing. Key from `MODEL_API_KEY` only. Use non-contributor model ids (`muse-spark-1.3`,
  `muse-wonder-1.0`, `sam-3.1`), never a `-contributor` id. Estimate spend before any per-frame or per-minute call and
  refuse above a `--max-usd` limit, like `reframe` does.
- Never send footage anywhere except the Meta Model API, and only when the user runs a command that says so.
- No client names, people's names or local footage paths in code, tests or docs.
- Provenance: add a section to `docs/muse-code/BUILD-LOG.md` and a row to the README's "Who built what" table that
  say exactly what Muse Code designed and built here. Leave model-call and token counts as TBD; the owner fills them
  from the run log.

## What done looks like

- The design (`OPEN-DESIGN.md`) opens with a short section, "Why this", naming the problem chosen and why it matters
  most to the editor above, then gives modules, data contracts, the command line, how Meta models are used and why,
  failure and edge cases, and a test list.
- The build adds the code, tests and docs, keeps the full suite green with
  `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`, and updates the README so a stranger can use the
  new feature from a fresh clone.
