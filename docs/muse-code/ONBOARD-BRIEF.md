# Brief: first-run tutorial and a real Import page (Ghost gets a face, a briefing and a tour)

Written 2026-11-04 by the project owner with Claude Code for Meta's Muse Code agent. Build it straight from this brief
on Muse Wonder. Budget: about 75 minutes of agent time; the build must be done by 4:00 PM ET (submission is 9:00 PM).
Two sibling builds (KITCHEN: render loading screen, DRESS: title cards and music) run in parallel worktrees; stay inside
the files this brief names so the merge is trivial.

**Merge contract (all three builds, read before editing any shared file).** The owner's agent pre-applies every
`<link>`, `<script>`, button and overlay line for all three builds to `app/ui/index.html` before you start, so
`index.html` should already contain, in this order: `ghost.css`, `import.css`, `kitchen.css`, `dress.css` after `app.css`;
`ghost/mascot.js`, `import.js`, `onboard.js`, `kitchen.js`, `dress.js` between `demo/project.js` and `app.js`; a `?`
Help button (`id="helpBtn"`) in `.top-right`; the kitchen overlay in `.player`; a Dress button in `.transport`. Check
that the lines for your own files are there and add only what is missing, at that spot. Files for the sibling builds will
not exist in your worktree: the resulting 404s are expected, so never assert their presence. In `app/engine.py` put your
`do_GET` routes right after the `/api/offload/cut/status` block and your `_post` routes right before the final
`raise FileNotFoundError("unknown endpoint")`, and in `README.md`/`BUILD-LOG.md` append at the end of your own section or
row group only (the BUILD-LOG sections are append-only; the merge keeps all three). The README "Who built what" row goes
directly after the "Phrase-level voice-note cuts" row.

Read `AGENTS.md`, `docs/muse-code/OPEN-BRIEF.md` (its "Rules (hard)" apply), `docs/muse-code/BUILD-LOG.md` (the OFFLOAD
section, for the format you will add to), then `app/ui/app.js` (`PAGES.import`, about lines 1106-1301; `show()` at about
200; the default-view line at about 1341; `api()` near line 25), `app/ui/index.html` (nav at 12-34, empty
`<main id="view-import">` at about 131, Connections dialog 140-146, scripts at about 169), `app/ui/app.css` (tokens at
1-26, `.ghost-*` at about 392-424, `.box/.vol` at about 271-278), `app/engine.py` (`offload_ask` ~750, `_make_plan` ~718,
`_plan_card` ~661, `_scan_public` ~514, `_scan_worker` ~562, `offload_shoot` ~953, `offload_thumb` ~976, `do_GET` routes
~1260-1280, `_post` ~1385-1408), `src/director/offload.py` (`GHOST_DEVELOPER` ~1200, `INTENT_SCHEMA` ~1183,
`parse_intent` ~1495, `parse_intent_rule` ~1334, `ClipInfo` ~181), `src/director/connect.py` and `tests/test_offload.py`
(`GhostUiTests`, ~618, which reads `app.js` and pins strings).

## What the owner wants

After playing with the app: "Right now, it looks super basic on the import thing. I like that it can see the camera.
Goes to footage. I want to work on the import tab a little bit more. Develop that." And: "I think there should be a slight
tutorial at the beginning. Whenever the software installs, it kind of walks it through, or walks the agent through, at the
beginning in the import phase. Tell us what you need to know." Story and Edit are liked; do not disturb them. This page
opens the demo video, so it has to look finished, and it must keep working fully offline (only the optional Muse
call needs a network).

## What exists (read from the code, do not guess)

- `PAGES.import` is one column of text: a lede, a `.box` "Mounted" with a text row per volume, a chat holding one canned
  line, the plan card, a bare 8 px progress bar, small thumbnails, and an unstyled "Open job" input. Its state is in
  closures. Every completion path calls `location.reload()` (about 1152, 1216, 1273), which wipes the chat.
- `scanTimer` polls `GET /api/offload/scans` every 1.2 s and never stops when the view is hidden.
- `GET /api/offload/volumes` returns volumes with `name, kind (destination|camera_card|other), detail (Sony...), free/total
  bytes`. Scan entries carry only `{volume,name,state,done,total,clip_count,total_bytes,message}`: the camera model, date
  range and a sample clip exist in `ClipInfo` but `_scan_public` and `_scan_worker` drop them.
- The plan shape from `_plan_card` is `cards[{volume,name,kind,detail,camera,clip_count,total_clips,total_bytes,gb,
  date_start,date_end,skipped}]`, `destination, shoot_dir, project, bytes_to_copy, free_bytes, enough_space, older_count,
  older_summary, include_all, entries, volumes, reply`. Transfer status from `GET /api/offload/status` is `{state:
  planned|running|done|done_with_failures|failed, message, shoot_dir, progress:{file,bytes_done,bytes_total,eta_s}}`.
  Cut status from `GET /api/offload/cut/status` is `{state, message, shoot_dir, out_dir, stages:{name:status}, cut_status}`.
- Help-style text ("what can you do") falls through to "Tell me which card to copy and where to put it." The intent
  field `make_cut` is parsed and never used.
- The app serves from a random port every launch (`app/main.py` `free_port`), so `localStorage` does not survive in the
  Mac window. First-run state must live on disk, in the engine.
- `Engine._offload_root` already lets tests use a fake volumes root.

## The job

Priority order, with a timebox per item (about 75 minutes in all). If time runs short, stop after the item you are on and
note what is unfinished in the BUILD-LOG. Items 1 and 2 together get at most 10 minutes: they are enablers (the other two
builds reuse the mascot), the owner's two real asks are the tour (5) and the Import page (6). Timeboxes: 1+2 ten minutes,
3 five, 4 eight, 5 fifteen, 6 twenty-two, 7 five (the scan enrichment only; see "If time remains" for the thumbnails), 8 five.
Order of work: 3, 1, 2, 5, 6, 4, 7, 8 (the settings route comes first because the tour and the start line need it).

1. **Shared design upgrade (this brief owns it; KITCHEN and DRESS only reuse it).** New `app/ui/ghost.css`, loaded after
   `app.css`. It only adds or overrides, never renames or removes a class, and never touches `.view`, `.panel`,
   `.timeline` or grid rules.
   - Tokens: `--dim:#8791a5` (readable), define the missing `--warn:#e6b35a` and `--blue:#9db8ff`, `--glass:rgba(22,26,35,.62)`,
     `--ghost-glow:0 0 24px #9db8ff55`, and a type scale `--t-xs:11px --t-sm:12.5px --t-md:14px --t-lg:20px --t-xl:32px`.
     `.primary` background `#e8590c` so white text passes contrast (hover keeps `--orange2`).
   - Page titles and `.lede`: bigger display heading (`.page h2` at `--t-xl`, tight tracking), lede at `--t-md`. Use the
     system font stack; do **not** add a web font (no rights work to do).
   - Glass surface for `.box`, `.ghost-plan`, `.dlg`, `.pop`, `.msg.muse`: border `1px solid #ffffff14`, soft shadow,
     `backdrop-filter: blur(14px) saturate(1.2)`. A faint radial colour wash behind `#view-import`, `#view-story`,
     `#view-deliver` (orange top-left, ghost-blue top-right).
   - Motion only inside `@media (prefers-reduced-motion: no-preference)`: `@keyframes gfloat` (mascot bob), `gpop`
     (entry for `.msg`, `.box`, thumbnails with a stagger), a small hover lift on `.primary`.
2. **Ghost mascot.** New `app/ui/ghost/mascot.js` (plain classic script, defines `window.GhostMark(size, mood)` returning an
   inline SVG element, moods `idle|scan|cook|bing` change the eyes and a CSS class only) and `app/ui/ghost/ghost.svg`
   (the same drawing as a file). Hand-draw it in SVG: rounded-top body with a wavy hem, two oval eyes, a soft `#9db8ff`
   gradient and an orange rim slash echoing the brand icon. No external images, no network. The other two builds call
   `window.GhostMark?.(...)` and fall back to a text glyph, so keep the global name and signature exactly.
3. **Settings endpoints and first-run state (engine).** A per-machine file
   `~/Library/Application Support/GhostCut Director/settings.json`, written atomically (temp file then `os.replace`,
   `mkdir(parents=True)`), shape `{"onboarding":{"seen":bool,"version":1,"step":int,"skipped":bool,"completed_at":str|null}}`.
   - Engine methods `settings_get()` / `settings_post(body)` back the routes.
   - `GET /api/settings` -> `{onboarding:{...}, first_run:bool, meta_key:bool}` (`meta_key` = `bool(os.environ.get("MODEL_API_KEY"))`).
   - `POST /api/settings {onboarding:{...}}` merges and returns the same shape. Unknown keys are ignored, the file is
     never given anything but the onboarding block.
   - Tests inject the path: `Engine(settings_path=...)` or env `GHOSTCUT_SETTINGS`. Tests never touch the real home.
   - Do **not** read `~/.config/ghostai/meta-api.env` or any key file. The owner's agent starts the engine with the key
     in the environment; Ghost simply reports `meta_key` honestly.
4. **Ghost's briefing and a help branch.**
   - `GHOST_BRIEFING` in `src/director/offload.py` (about 12 lines): the workflow (cards, destination, project name,
     the "older footage" rule, transfer, verify, make the cut), what Ghost understands, and what it will never do (copy
     without a confirm, invent a volume, write to a card). Append it to `GHOST_DEVELOPER`. Do **not** change
     `INTENT_SCHEMA` (it is `additionalProperties:false` with every key required).
   - `is_help(text)` in `offload.py`: matches `help`, `what can you do`, `how does this work`, `tutorial`, `walk me
     through`, `get started`. `Engine.offload_ask` checks it before `parse_intent` and replies from live state, for
     example "I can see 2 Sony cards (Untitled, 4 clips; Untitled 1, 111 clips) and Extreme SSD with 535 GB free. Say
     'transfer to my SSD' to begin." Return `{reply, help:true, steps, live:{cards,destination}}`.
   - `GET /api/offload/briefing` (`Engine.offload_briefing()`) returns the same live data so the tour and Ghost share one source.
   - Ghost's first chat message on mount is that live line, not the canned sentence.
5. **The tour: `app/ui/onboard.js` (+ styles in `import.css`).** Exposes `window.GCTour.open(ctx)`. A coach-card modal over
   the Import page, 6 steps, Back/Next, **Skip on every step**, a step dots row, focus-trapped, Esc closes (counts as skip),
   all text original and short:
   1. Welcome: "Cards in, finished cut out. You talk; Ghost and Muse do the work." with the four-stage strip Import, Story,
      Edit, Deliver and the mascot.
   2. Connect your tools (optional): reads `GET /api/connections`, shows ElevenLabs connected or not ("music and sound
      effects") and the `meta_key` status ("Ghost is running on rules" if false). Button opens the existing Connections
      dialog via `ctx.openConn()`.
   3. Pick your destination: lists destination drives from `/api/offload/volumes` with a free-space meter; none found says
      "Connect your SSD".
   4. Insert a camera card: live-polls `/api/offload/scans`; empty state is an animated "waiting for a card" line; when a
      card appears it auto-advances saying "Found a Sony card, 111 clips".
   5. Ask Ghost: pre-fills the chat input with "the footage is on the SD card, transfer it to my SSD" and a "Try it" button
      that submits it through the same function the form uses. If a job is already open (`P.cutJob`), this step says "Open
      a job" and points at the Resume card instead.
   6. Transfer, then Make the cut: checksum verification, what the cut does (sync, angles, takes, grade), and honestly that
      a full cut takes about half an hour.
   - First run: if `first_run` is true and the URL hash names no view, the app opens on Import (not Edit) and the tour starts
     after the page paints (an explicit `#edit` or `#story` hash always wins and starts no tour).
     Finishing or skipping POSTs `/api/settings`. Escape hatches: URL hash `#import/tour` forces it, `#import/notour`
     suppresses it (needed for recording), and a `?` Help button in the top bar reopens it any time. Help in the chat
     header does the same.
   - The tour needs no network and no engine job; if `/api/settings` fails it still opens (treat as first run, never block).
6. **Import page rebuild: `app/ui/import.js` + `app/ui/import.css`.** Move the whole view out of `app.js` and expose
   `window.GCImport.mount(el, ctx)`. Keep the data flow and endpoints; replace the look:
   - Two columns above 1100 px (list column left, Ghost column right), one column below. A header with the pipeline strip
     (Import, Story, Edit, Deliver, current one lit).
   - **Card tiles** per camera volume: inline SVG glyph chosen by `detail` (Sony, DJI, GoPro, iPhone, recorder, generic),
     name, clip count, GB, a mini scan bar with `done/total`, a teal "Ready" chip at `state==="ready"`, camera model and date
     range when known, a checkbox to select it. Selecting cards and a destination plans by click through the existing
     `POST /api/offload/plan {cards,destination}`.
   - **Destination tile**: free-space meter from `total_bytes`/`free_bytes`, a "to copy" segment once a plan exists
     (`bytes_to_copy`), red when `enough_space` is false.
   - **Ghost chat panel**: header with the mascot, the live status line and a Help button; starter chips ("Transfer my SD
     card to the SSD", "What can you do?", "Open the last job") that submit text; the plan card and progress below it.
   - **Plan card**: per-card rows (glyph, clips, day range, GB), total line, keep "Include them", Transfer and Change, and keep
     the class names `ghost-plan` and `.acts button.primary`.
   - **Transfer progress**: one persistent element updated in place (no `replaceWith`), determinate bar with percent, file
     name, bytes, ETA and a client-side throughput figure.
   - **Shoot bins**: header per camera (label, clip count, total duration), thumbnail grid with a duration badge, skeleton
     tiles while loading, `encodeURIComponent` on the thumb URL params (they break on `&` and `#` today). Durations come
     from the existing response (`duration_s` may be `null`: hide the badge then).
   - **Resume a job** card replaces the bare "Open job" input: a styled path input plus primary "Open job" (this is the
     practical demo path because a full cut takes about 30 minutes).
   - **Cut progress**: friendly labels per stage from `cut/status.stages` along the strip instead of `k: v . k: v`; on done set
     `location.hash="#edit"` before the reload so the page lands on Edit by design.
   - **Fixes while in there**: stop `scanTimer` when the view is hidden (check `el.hidden` in the poll); guard empty states
     (no card -> an "Insert a card" panel with a calm animation, no SSD, scan error); on mount, read
     `/api/offload/status` so a finished transfer resumes (bins visible) instead of an empty chat; if the parsed intent has
     `make_cut` true, offer the cut right after the transfer completes (an offer button, never an automatic 30-minute job).
7. **Engine enrichment for the tiles (small).**
   - `_scan_worker` / `_scan_public` also return `camera_labels:[...]` (sorted unique `clip.camera_label`), `date_start`,
     `date_end` (ISO strings from `scan.date_range`, `None` when unknown) and `sample` (the first clip's file name only, not
     a path), taken from `scan.clips` (about 6 lines; `_scan_public` must copy the new keys, it builds its dicts by hand).
   - Real card thumbnails (`GET /api/offload/card-thumb`) and the cheaper `offload_shoot` are in "If time remains". Until they
     exist the tiles show the glyph and the shoot bins keep today's `/api/offload/thumb` URLs (with `encodeURIComponent`).

8. **Docs.** BUILD-LOG section, README row and a two-sentence README update to "Import with Ghost" (see Rules).

## Files you own and the only edits to shared files

New files (yours alone): `app/ui/ghost.css`, `app/ui/import.js`, `app/ui/import.css`, `app/ui/onboard.js`,
`app/ui/ghost/mascot.js`, `app/ui/ghost/ghost.svg`, `tests/test_onboard.py`.

Hook edits, kept minimal and mark each with a one-line comment containing `ONBOARD` so merges stay trivial:
- `app/ui/index.html`: your `ghost.css`, `import.css`, `ghost/mascot.js`, `import.js`, `onboard.js` lines and the `?` Help
  button (`id="helpBtn"`, in `.top-right`) are pre-applied (see the Merge contract); add any that are missing. Nothing else.
- `app/ui/app.js`: (a) after `api()`, expose `window.GC = Object.assign(window.GC || {}, {h,$,$$,api,fmt, LIVE:()=>LIVE,
  show:(v)=>show(v), openConn:()=>{renderConn(); connDlg.hidden=false}, project:()=>P})`, about 3 lines, merge with
  `Object.assign` because other builds may also set `window.GC`; (b) replace the body of `PAGES.import` with
  `import(el){ window.GCImport.mount(el, window.GC); }`; (c) the start line (about 1341): default to `import` when
  first-run is pending, honour `#import/tour` and `#import/notour`. Keep the literal `demo/video.mp4${VQ}` and keep every
  `$("#id")` used in `app.js` present in `index.html`.
- `app/engine.py`: the settings, briefing and card-thumb routes beside the existing `/api/` routes; the help branch at the
  top of `offload_ask`; the scan enrichment. No other change.
- `src/director/offload.py`: `GHOST_BRIEFING`, `is_help`, the `GHOST_DEVELOPER` paragraph.
- `tests/test_offload.py`: `GhostUiTests` reads `app.js`; since the view moves, make it read `app.js` **and** `import.js`
  (concatenate) so the pinned strings ("Include them", "Scanning ", "/api/offload/scans", "older_summary") still match, or
  keep those strings in `import.js`. Do not weaken the assertions.
- `README.md`, `docs/muse-code/BUILD-LOG.md`.
- Do **not** touch the Edit view, `#view-edit`, `startCutjobRerender`, `pollCutjob`, or anything render-related (KITCHEN),
  `src/director/voice.py`, `cli.py`, `muse.py`, or cutjob code (DRESS).

## Rules

`OPEN-BRIEF.md` "Rules (hard)". Also:
- Tests need no socket (the sandbox blocks them, and `ps`): add the new routes as thin `do_GET`/`_post` branches that call
  `Engine` methods (`settings_get`, `settings_post`, `offload_briefing`, and the scan keys through `_scan_public`), and test
  those methods directly, like `tests/test_offload.py` ~326 ("Handler._post without a socket"). The settings file is read
  and written lazily, never in `Engine.__init__`, so every existing test that builds an `Engine` stays off the real home.
  The full suite is green except the same 11 pre-existing `ps`/socket errors in `tests/test_engine_render.py` and
  `tests/test_render_runner.py` (say so, with your before/after counts, in the BUILD-LOG).
- Tests offline, in `tests/test_onboard.py` (and the updated `GhostUiTests`): settings GET/POST round trip with a temp
  path (first_run true, then false after completion, corrupt file treated as first run, unknown keys ignored);
  `is_help` on the phrasings above and negatives ("transfer to my SSD" is not help); the live help reply built from a fake
  volumes root; `/api/offload/briefing` shape; scan enrichment keys present (`camera_labels`, `date_start`, `date_end`, `sample`); `node --check` on `import.js`, `onboard.js`,
  `mascot.js` (skip when node is missing, like the existing tests); no `/Users/` or `/Volumes/` string in any new
  shipped file; `index.html` contains every new `<script>`/`<link>`; every `$("#id")` in `app.js` and `import.js` exists.
  Never call Meta, ElevenLabs, xAI or Whisper; never call `parse_intent` with a key set.
- Offline at demo time: no CDN, no web font, no remote image. Everything under `app/ui/` (the engine only serves
  `/app/ui/` and `/docs/brand/`).
- Keep the app's visual tokens; extend them only in `ghost.css`.
- Never write to a source card. No client names, people's names or local footage paths in code, tests or docs; fakes live
  under a temp `volumes_root`.
- Provenance: add an ONBOARD section to `docs/muse-code/BUILD-LOG.md` in the OFFLOAD format (title, Step/By/What table,
  "Muse Code token use: TBD", build notes, then a "Live test" section left for the owner's agent) and a row in the README
  "Who built what" table: "First-run tutorial, Import page and Ghost mascot. Built by Muse Code on muse-wonder-1.0 from
  `ONBOARD-BRIEF.md`, after my review of the app on submission day." Say plainly that the brand icon is the owner's own
  ChatGPT-made art, not Muse's, and that the mascot SVG is new.
- Run the whole test suite before you finish (green apart from the 11 pre-existing sandbox errors named above).

## If time remains (stretch, in this order)

- Real card thumbnails: `Engine.offload_card_thumb(volume, file) -> Path` plus `GET /api/offload/card-thumb?volume=&file=`
  (JPEG). The file must resolve with `_allowed_path` inside a mounted camera volume **and** be a clip in that volume's scan
  (`_get_scan`); cache to a `tempfile` directory, never into the card (the card is never written); ffmpeg missing or failing
  returns 404 and the tile keeps its glyph; a test refuses a path outside a camera volume and a non-clip without ffmpeg.
- Make `offload_shoot` cheap: reuse durations from the scan, or cache probe results in `offload.json`, so 111 clips do not
  stall the request.
- A recent-jobs list on the Resume card (last 5 job folders the engine opened, stored in the same settings file under
  `recent_jobs`).
- Per-card breakdown inside the transfer progress.
- Mascot moods reacting to events (`scan` while scanning, `bing` on verified).
- A "Reset tour" action in the Connections dialog.
