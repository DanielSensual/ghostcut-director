# Brief: the Import page becomes Ghost, an offload agent (`director offload` + the app's Import tab)

Written 2026-11-04 by the project owner with Claude Code for Meta's Muse Code agent. Build it straight from this brief
on Muse Wonder. Read `AGENTS.md`, `docs/muse-code/OPEN-BRIEF.md` (its Rules apply), then `app/engine.py`,
`app/ui/index.html`, `app/ui/app.js` (the `import(el)` view is a placeholder that says "NOT BUILT YET"),
`app/ui/app.css`, `app/muse.py`, `src/director/voice.py` and `src/director/transcribe.py` (how a spoken note is heard),
`src/director/cli.py`, `src/director/cut.py` (`director cut` inputs and the job folder), the installed
`ghostcut/ingest.py` (`discover_media`, `probe_media`), and the tests for the app engine.

## What the owner wants

"The import part — I would like it to be an agent as well. There's a little agent tab, Ghost, and I can tell Muse:
'the footage is on the SD card, transfer it to my SSD'. It does that, and then it goes to Story: it knows the shots.
Then Edit and Deliver." This is the opening of the hackathon demo video, so it must work on the first try with real
cards on a Mac, and look finished, not like a prototype.

The owner's real cards when mounted look like this (names vary; never hard-code them):

- Sony camera SD card: `/Volumes/Untitled 1/` with `PRIVATE/M4ROOT/CLIP/*.MP4` plus `*M01.XML` sidecars (timecode,
  capture gamma), and `AVF_INFO/`, `SONY/`.
- A second Sony-style card: `/Volumes/Untitled/` with `PRIVATE/`, `AVF_INFO/`.
- DJI drone / Osmo: `DCIM/DJI_*/DJI_*.MP4` (+ `.SRT`/`.LRF`), GoPro: `DCIM/1xxGOPRO/*.MP4`, Rode Wireless PRO /
  recorders: `*.WAV` (32-bit float), iPhone AirDrops land in `~/Downloads`.
- The destination SSD: `/Volumes/Extreme SSD/` (4 TB, mostly full: free space matters).

## The job

1. **`src/director/offload.py`** (no app dependency, fully testable):
   - `list_volumes()`: mounted volumes under `/Volumes` (skip the boot volume, installers/DMGs, hidden), each with
     free/total bytes and a guess of what it is: camera card (by layout above, any brand you can recognise), a big
     destination drive, or other.
   - `scan_card(volume)`: every camera clip and audio recording on it (skip proxies like `.LRF`, thumbnails, system
     folders), with size, duration and recorded time (ffprobe `creation_time`, Sony XML sidecar, file mtime as the last
     resort, and say which), camera make/model when the metadata says, and a suggested camera label.
   - `plan_offload(cards, destination, project=None)`: a plan into `<destination>/<YYYY-MM-DD> <project or "Shoot">/
     <camera label>/` keeping original file names and copying sidecars next to their clips, skipping files already
     there with the same size and checksum, and refusing when the destination lacks the space (with the numbers).
   - `run_offload(plan, progress)`: copy with progress (bytes, file, ETA), verify every copy with a streamed checksum
     (xxhash if importable, else BLAKE2b/SHA-256 from hashlib) against the source, write `offload.json` (sources,
     destinations, sizes, checksums, verified, skipped) into the shoot folder, and resume cleanly after an interrupt
     (partial files go to `*.part` and are renamed only after they verify). **Never delete, move, rename or write
     anything on the source card.** No eject.
   - `director offload [--to DEST] [--project NAME] [--yes] [CARD ...]` on the command line: prints the plan, asks to
     confirm unless `--yes`, runs it, prints the verify result and the `director cut` command for the new folder.
2. **Ghost, the agent** (`app/` + engine endpoints): the Import tab becomes a chat with Ghost, with a text box and the
   same hold-to-talk mic the Edit tab uses (spoken requests are heard through the existing transcribe path).
   - Muse Spark turns the request into a structured intent (which cards, which destination, project name, whether to
     start the cut afterwards) using the live `list_volumes()` facts it is given; deterministic code does all file work.
     With no Meta key, a small rule-based parser handles the obvious phrasings ("SD card", "drone", "to my SSD",
     "extreme") so the demo never depends on the network.
   - Ghost answers in a sentence, then shows a **plan card**: each card found (name, camera guess, clip count, GB,
     recorded date range), the destination folder, the space check, and **Transfer** / **Change** buttons. Nothing is
     copied until the user clicks Transfer (or says "yes, go").
   - While copying: a progress bar per file and overall, live from the engine; then "Verified N files, X GB" with any
     failure named. The Import page then shows the shoot: bins per camera with thumbnails (one frame per clip, cached
     in the shoot folder) and durations.
   - Then Ghost offers **"Make the cut"**: it starts `director cut` on the new shoot folder as a background job (the
     same way the engine re-renders today), streams its stage progress, and when it finishes opens that job so
     Story/Edit/Deliver show it. It must also be able to just open an existing job folder ("open yesterday's job").
   - Remove the "NOT BUILT YET" placeholder text. Keep the page's visual style (app.css tokens).
   - Engine endpoints are localhost-only like the existing ones; paths from the client are re-resolved and must be
     under `/Volumes` or the user's home; the copy runs in a background thread with a status endpoint.
3. **Check on the real cards, read-only.** `list_volumes()` and `scan_card()` on the cards mounted now (read-only), and
   `plan_offload` into `/tmp/offload-test/` with a small subset (two clips) so you can run a real copy + verify without
   touching the SSD. Report what it found. If your sandbox hides `/Volumes`, test against a synthetic card layout in a
   temp dir and say so; the owner's agent runs the live check.

## Rules

`OPEN-BRIEF.md` "Rules (hard)". Tests offline: card-layout detection for Sony/DJI/GoPro/recorder trees built in a temp
dir, sidecar pairing, skip-if-identical, the space refusal, checksum mismatch → not verified and the `.part` file
kept, resume after interrupt, the source never written (check mtimes and a read-only source dir), the rule-based
intent parser on the owner's phrasing, the engine endpoints (plan, start, status) with a fake copy. Never call the
Meta API or Whisper in tests. Add an OFFLOAD section to `docs/muse-code/BUILD-LOG.md`, a README section "Import with
Ghost", and a row in the README's "Who built what" table.
