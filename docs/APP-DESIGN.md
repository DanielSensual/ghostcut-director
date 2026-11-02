# GhostCut Director — app design

The Mac app a creator opens to turn a shoot day into a finished, editable video. The Python pipeline
(`director_test_run.py` today, `ghostcut director` later) is the engine; this document is the window around it.

## Who it's for

A solo creator or a two-person crew who shot a talking-head video with B-roll: a realtor tour, a
coach's explainer, a founder update. They can edit, but the first assembly (finding takes, cutting
flubs, placing B-roll, levels) eats a day. They finish in Final Cut, Resolve or Premiere.

## Principles

1. **Camera originals stay on the Mac.** Meta's models see small proxies and transcripts, never the camera
   originals; the optional `mix --isolate` step sends that audio span to ElevenLabs.
2. **Every decision has a reason you can read.** Click any clip: why it was picked, what it beat, what
   was said there.
3. **Measured vs model judgment vs you.** Loudness, Whisper and frame checks are *measured*. Spark's
   notes are *model judgment*, and a model agreeing with itself is never shown as proof. What you
   approved is *confirmed*.
4. **Not a full editor.** Trim, swap, reorder and ask. Anything finer goes out as an FCPXML/XML that
   opens with every cut editable.
5. **Chat edits are diffs.** Muse proposes a change, shows exactly what moves, re-renders that
   section only and verifies it. Undo is one click. (Status 2026-10-30: proposals, checks, accept, undo and
   Render v1 work. Render re-renders the sections that accepted insert, card and note edits touch and swaps the
   new version in only after its checks pass. Cutting talk is not built, so Muse blocks it.)

## Window

```
┌────────────────────────────────────────────────────────────────────────────────┐
│ ◉ GhostCut  Project ▾   Import · Story · Edit · Deliver        $0.84 Meta   ⤴ │
├───────────┬────────────────────────────────────────────┬───────────────────────┤
│ SECTIONS  │                                            │ Muse | Review | Why   │
│ Intro     │                 PLAYER                     │                       │
│ Community │                                            │  chat / findings /    │
│ Explainer │                                            │  clip inspector       │
│ Tour      ├────────────────────────────────────────────┤                       │
│ MEDIA     │ sections ▕ intro ▕ community ▕ explainer ▕ │                       │
│ A-cam 31  │ B-roll   ▕▬▬  ▬▬▬ ▬  ▬▬▬▬ ▬▬ ▬▬▬  ▬▬▬    ▕ │                       │
│ B-cam  6  │ Cards    ▕  ◆   ◆◆   ◆  ◆◆◆◆   ◆        ▕ │                       │
│ Drone 42  │ Talk     ▕▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▕ │                       │
│ Web    10 │ Voice    ▕∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿∿▕ │                       │
│           │ Music    ▕~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~▕ │                       │
└───────────┴────────────────────────────────────────────┴───────────────────────┘
```

## Screens

| Screen | What you do | What the engine does |
|---|---|---|
| **Import** | Drop a folder or card; confirm the shoot date | **Planned (Next #2), not built:** scan every mounted volume for clips recorded that day, group by camera, make proxies, run Whisper on every talking take. The prototype page shows hand-written sample data; in the sample project the explainer takes on another job's card were found by hand during development. |
| **Story** | Read the take log; reclassify or swap a pick in one click; drag sections into order | Spark watches proxies with numbered transcript lines: keepers, alternates (best picked, with a reason), flubs, asides, setup. Proposes sections (intro, community, explainer, tour). (Status 2026-10-29: the take log is real for the house section only; reclassify is not saved yet, see Next #3. Sections are given by hand today, with `make_demo.py --section`; Spark does not propose them yet.) |
| **Edit** | Watch, ask Muse for changes, work the Review list, inspect any clip | Cuts snapped to measured silences; B-roll placed under the words it shows; fact cards on spoken facts; music fitted to length; loudness master. (Status 2026-10-29: fact-card text and timing are written by hand from the transcript and rendered by the `facts` step; Muse-added cards are checked against the transcript. Some B-roll placements were set by hand with `--insert`/`--drop-at`.) |
| **Deliver** | Pick outputs | Master (−14 LUFS / −1 dBTP), phone preview, FCPXML / Premiere XML / Resolve, SRT, reshoot list. (Status 2026-10-29: prototype; the Export button is not wired yet, see Next #4.) |

## Right panel

- **Muse** — natural-language edits. Each reply: what it understood → the change list (clip, in/out,
  before → after) → re-render of that section → verification (Whisper reads clean, loudness). Accept / Undo.
  Accepted edits wait as pending changes until you press Render (see Render below).
- **Review** — findings with a source badge (*Measured*, *Spark*, *You*) and a state (open, fixed,
  rejected-with-reason). Clicking one jumps the player there.
- **Why** — the selected clip: source file and range, Spark's reason, the alternates it beat, the
  words spoken over it, and for B-roll Spark's placement reason (an automatic frame check is not built yet).

## Shell

**pywebview** (Python opens a native WKWebView window) for the hackathon: one process, the engine is
already Python, no Rust toolchain, packaged with PyInstaller later. The UI is plain HTML/CSS/JS with no
build step, so it can move to Tauri unchanged.

## Brand

Dark UI (#0b0d12 base), GhostCut orange #ff6a1a for actions and the playhead, ghost blue #9db8ff for
model output, teal #2dd4bf for audio. Assets in `docs/brand/`.

## Running it

```bash
python app/main.py      # the Mac window (needs pywebview)
python app/engine.py    # the same app in a browser at http://127.0.0.1:7791/
```

Use a Python that has the Meta client (`guestlist.meta`) installed, or Muse answers with an error and
everything else still works. Opened as a plain file, `app/ui/index.html` falls back to the static demo
(`demo/project.js`) with scripted Muse replies.

The project comes from a finished job: `app/make_demo.py` reads each section's FCPXML, B-roll map and
plan, the take log, the fact cards and a Whisper transcript of the finished cut, and writes
`app/ui/demo/` (gitignored: demo footage, names and edits stay local). A fresh clone has no project, so the window
opens empty until you build one; `make_demo.py --poster-at` picks the poster frame.

**Hand-written demo content.** The opening Muse conversation, the Review findings, the media bins, the
Import flag, the spend rows and the Deliver loudness figures come from a hand-written `--extra` file, not
from the engine. They describe work done during development (much of it by hand or with an AI coding
assistant), and the UI labels them as scripted examples. Only messages sent while the engine is connected
are live Muse/Spark output.

## Engine API (`app/engine.py`)

| Call | Does |
|---|---|
| `GET /api/project` | The cut with accepted edits applied, the edit list, Muse spend |
| `POST /api/muse {text, at}` | Spark's proposal for a typed request; every operation comes back with its checks |
| `POST /api/accept {id, pick}` | Adds the proposal's unblocked operations to the edit list |
| `POST /api/undo {edit}` | Removes an edit (default: the last) |
| `GET /api/frame?tag=&t=` | A still from a library shot, at the frame Render will centre an insert on |
| `GET /api/render/plan` | What the next render would do: changes, base fixes, refusals, time estimate |
| `POST /api/render` | Render the accepted edits into the next version (409 while one runs) |
| `GET /api/render` | Its progress: state, step, percent, ETA, message |
| `POST /api/render/cancel` | Stop it; the current cut stays |
| `POST /api/render/revert` | Go back one version (409 while rendering or while accepted edits wait) |

## Muse (`app/muse.py`)

- **In:** the timeline as text with an id per item, the Whisper transcript of the finished cut, the
  unused shot library with descriptions, open review notes, the playhead and the request.
- **Out (schema-constrained):** a short reply, timestamps for questions, and operations: drop / move /
  swap / add an insert, drop / move / edit / add a card, cut a span of talk, add a review note.
- **Checks (measured, before anything can be accepted):** the item exists; the range is inside the
  video; inserts keep ≥ 0.05 s from neighbours; a library shot exists, isn't marked skip by the
  inventory and isn't on the editor's never-use list; every number on a card is spoken within ±20 s;
  an insert is at least 1.0 s; a card has 1–3 lines and ends 0.5 s before the end; a talk cut is blocked
  (Render v1 can't cut talk yet). A failed hard check blocks the operation.
- **Edit layer:** accepted operations go to `edits.json` and show on the timeline as pending changes
  (dashed outline, struck-through drops). Undo removes an edit. The video changes when you press Render.
- **Cost:** about $0.02–0.03 and 10–15 s per request on `muse-spark-1.3`; repeating the same request on
  the same cut is cached and free. Every call goes through the shared ledger with its hard budget stop.

## Render (`app/render.py`)

- **Plan:** the shown project plus `edits.json` become explicit placements on the current version's timeline
  (frame-exact section starts, each insert's source in-point centred on the frame the swap preview showed). An
  operation Render can't do is refused with a reason and stays pending.
- **Run:** only the sections an edit touches are re-encoded; clean sections reuse the previous version's file. The
  voice is rebuilt sample-exact from each section's audio by packet timestamp, with the cached voice isolation
  re-aligned piece by piece (nothing is sent anywhere). Then the fact cards over all sections, the music mix to
  −14 LUFS, the phone copy, and a Whisper transcript.
- **Checks (measured):** frame counts, section joins, overlay cut times in the master and the phone copy, voice
  sync against the assembled sections and the camera files (including each section's last seconds), loudness and
  true peak, audio timestamp gaps, card text and visibility, and the transcript against the previous version's
  (spoken amounts match their digits; a join inside a word that the original dialogue edit already has is a
  warning). Any failed check stops the render: the current version stays live.
- **Versions:** each render is a new `rNNN` folder that is never reused; promote moves the old app files to
  `.history/` so Revert can bring them back. Revert is refused while accepted edits wait to render.

## Next

1. Render v2: cut talk (`cut_span`) with re-checked word edges.
2. Import: the shoot-date scan across every mounted volume, live.
3. Story: persist reclassified takes and re-cut from them.
4. Deliver: wire the buttons to the FCPXML / Premiere XML / SRT exporters.
5. Package as a `.app` with the icon.
