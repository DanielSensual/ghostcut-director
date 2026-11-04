# Brief: the ghost kitchen, an interactive loading screen for the Edit tab (`kitchen.js`)

Written 2026-11-04 by the project owner with Claude Code for Meta's Muse Code agent. Build it straight from this brief
on Muse Wonder. Budget: about 75 minutes of agent time; the build must be done by 4:00 PM ET (submission is 9:00 PM).
Two sibling builds (ONBOARD: first-run tutorial, Import page, shared design and the ghost mascot; DRESS: title cards and
music) run in parallel worktrees; stay inside the files this brief names.

**Merge contract (all three builds).** The owner's agent pre-applies every `<link>`, `<script>`, button and overlay line for
all three builds to `app/ui/index.html` before you start (stylesheets after `app.css` in the order `ghost.css`,
`import.css`, `kitchen.css`, `dress.css`; scripts between `demo/project.js` and `app.js` in the order `ghost/mascot.js`,
`import.js`, `onboard.js`, `kitchen.js`, `dress.js`; the kitchen overlay line inside `.player`). Check that yours are there
and add only what is missing, at that spot. Files of the sibling builds will not exist in your worktree (expected 404s: never
assert their presence). README row: directly after the "Card offload" row. BUILD-LOG: append your section at the end (the
merge keeps all three). Your `app/engine.py` edits stay inside `_rerender_worker` and the two helpers named below; add no
`do_GET`/`_post` route.

Read `AGENTS.md`, `docs/muse-code/OPEN-BRIEF.md` (its "Rules (hard)" apply), `docs/muse-code/BUILD-LOG.md`, then
`app/ui/index.html` (`<div class="player">` at about line 43), `app/ui/app.css` (`.player` at about 104, tokens at 1-26),
`app/ui/app.js` (`startCutjobRerender` ~1032, `pollCutjob` ~1052-1075, `applyResult` ~379, `follow/poll/showProgress/ended`
~928-993, the offload-cut poller ~1140-1156, `show()` ~200, `layout()`), `app/engine.py` (`_rerender_worker` ~1050,
`_cutjob_state`, `Engine.project()` ~141, `STATIC` at 48), `app/cutjob.py` (`rerender` ~388, `build_project`),
`src/director/angles.py` (`render()` loop ~1489, parts folder ~1408), `tests/test_engine_render.py` (`UiWiringTests`
~452-510).

## What the owner wants

"Edit should be like a cooking. There should be a little ghost floating around, cooking in the kitchen, almost like a
little 3D kind of ghost. Where my video is right now in the Edit section, while the video is rendering and everything's
processing from FFmpeg, there's got to be some kind of loading screen, and I want to make it interactive and interesting
and fun, where they're like, 'Oh my god, this was hilarious!' Little ghosts flying around, cooking up, mixing your videos.
An interactive 3D loading screen. Then bam, the video pops up there. It loads. The person can see it." Everything runs on
a 16 GB Mac while ffmpeg is already saturating the CPU, so it must be light.

## What exists (read from the code, do not guess)

- The real job is a cut job (`P.cutJob` set). The Render button calls `startCutjobRerender()`; progress comes from
  `GET /api/cutjob/rerender`, which today returns only `{state: idle|running|done|failed, message}`. The UI hard-codes the
  bar to 50% while running (about 1048) and calls `location.reload()` 1.5 s after `done` (about 1066). `location.reload()`
  would destroy a running scene, so the hand-off must replace it.
- `angles.render()` renders one ffmpeg per shot and writes `<out stem>.parts/shot_NNN.mp4`; `rerender()` renders to
  `cut.next.mp4`, so the folder is `<job>/cut.next.parts/`. The real job has 23 shots in `angles.json`
  (`shots[]` each with `camera,start_s,end_s,shot_size,reason`). On a tightened job angles are first re-picked with Muse
  before any part exists (no files to count, phase "plan").
- Legacy `GET /api/render` (demo projects only) returns `{state, version, step, i, n, pct, eta_s, message, log, tail[]}`.
  Offload-cut `GET /api/offload/cut/status` returns `{state, message, stages:{name:status}, cut_status}`; stage names, in
  order, are sync, words, tighten, grade, angles, finish, export, reframe, captions, qc; only finished stages are recorded.
- `_rerender_worker` already runs `_load()` and `build_project` (relinks `video.mp4`, rewrites thumbs and `project.json`)
  before `state` becomes `done`, so `GET /api/project` is fresh when the UI sees `done`.
- `demo/thumbs/%04d.jpg` (192x108) and `demo/poster.jpg` exist for a built project; the video `src` is `demo/video.mp4${VQ}`
  and `VQ` is `""` for a cut job.
- The engine serves only `/app/ui/` and `/docs/brand/`; Python serves `.js` as `text/javascript`, so a dynamic
  `import("./vendor/three.module.min.js")` works from a classic script.

## The job

1. **Real progress from the engine (`app/engine.py`, about 40 lines, additive only).** Facts you must design around
   (read `angles.render()` ~1396-1420, `angles.run()` ~1530-1660 and `cutjob.rerender()` ~388): ffmpeg creates
   `shot_NNN.mp4` the moment it **starts** that shot, so a fresh file means "in progress", not "finished"; shots render one
   at a time in order; after the last shot the per-span audio parts (`audio_NNN.*`, one per kept span when the job is
   tightened) render, then `parts.txt` is written, then the final join; `angles.json` is rewritten only **after** the render
   (so during a tightened re-render it still holds the previous shot list); and a tightened job spends minutes before the
   first part exists (Spark re-pick, gaze and face sampling), with no signal at all.
   - Put the counting in a **pure function** `sample_parts(parts_dir, started_at, total)` returning `{"phase":
     "plan|shots|join", "done": n, "total": N|None}` so tests need no thread: take the `shot_*.mp4` files with
     `mtime >= started_at - 2.0` (the 2 s absorbs coarse file-system clocks on external drives; older files are leftovers of
     a previous run and never count); `fresh = len(those)`; if any `audio_*` file or `parts.txt` is that fresh, every shot is
     finished: `phase="join"`, `done = fresh`, `total = fresh`; elif `fresh > 0`: `phase="shots"`, `done = fresh - 1` (the
     newest file is the one being encoded), `total = max(total or 0, fresh)`; else `phase="plan"`, `done = 0`. No parts
     folder yet is `plan`.
   - In `_rerender_worker` start a daemon sampler thread (ticks every 0.5 s) around `cutjob.rerender(...)`. It reads
     `len(angles.json shots)` once at start for `total` (None when the file is missing or unreadable), calls
     `sample_parts(job / "cut.next.parts", started_at, total)`, and merges `{"phase","done","total","pct","eta_s"}` into
     `self._cutjob_state` **under `self.lock` and only while `state == "running"`**. `pct = round(100*done/total)` in the
     `shots` and `join` phases (cap 99 until the worker finishes), `None` in `plan`; `eta_s = round(avg_seconds_per_finished_shot
     * (total - done))` once `done >= 1`, else `None`. `state` and `message` keep their meaning (existing clients and tests
     stay green).
   - **Stop and join the sampler before the worker writes `done` or `failed`** (set its event and `join(timeout=2)` right
     after `cutjob.rerender` returns or raises, before taking the lock to write the final state), and again in a `finally`.
     Otherwise a late tick overwrites `done` with `running` and the UI never finishes.

2. **`app/ui/kitchen.js`** (plain classic IIFE script) exposing `window.Kitchen = {start(opts), update(st), finish(), fail(msg),
   stop(), preview()}`. `start({mode})` takes `"cutjob"` (the Edit-tab re-render), `"dress"` (the DRESS build's render, which
   calls it from `dress.js` and passes statuses shaped `{state:"running", step, pct, eta_s}`: map `pct` onto the thumbnail
   index so cards drop as the percentage rises) or `"preview"`; `finish()` returns a Promise that resolves when the overlay
   has hidden; every method is a safe no-op when called in the wrong state, a `<div class="kitchen" id="kitchen" hidden>` overlay mounted over the player (position absolute, inset 0,
   z-index 2), containing `<canvas id="kitchenCanvas">`, `#kitchenCaption`, `#kitchenReadout`. It lazy-`import()`s three
   on first start, so the Edit tab's load time is untouched.
   - **Scene (three.js primitives only, no model or texture files, under about 3,000 triangles, Lambert or Toon
     materials, shared geometry/materials, no shadows, no post-processing):** a kitchen counter and back wall in the
     app's colours (`--bg` `#0b0d12`, accent `--orange` `#ff6a1a`, `--ghost` `#9db8ff`, `--teal`, `--violet`), a warm
     point light and a cool rim light; a big pot with an orange emissive glow and rising bubbles; a head-chef ghost (capsule
     body, wavy hem, two eyes, chef hat) plus 2 to 3 smaller ghosts floating with a sine bob; pan and spoon silhouettes on
     the wall.
   - **Driven by real data:** every time `done` increases, a film card (thin 16:9 box textured with the matching
     `demo/thumbs/%04d.jpg` still, falling back to a plain card if the image fails) drops into the pot with a splash. The
     readout shows `shots 7/23 . 31% . about 4 min left` in `var(--mono)`; in `plan` and `join` phases show an
     indeterminate shimmer and the captions "Picking the angles" / "Joining the plates". Show `message` on failure. When
     the stage-style status arrives (offload cut, `stages{}`) drive the choreography by stage: sync stirs, angles peeks
     over cards, grade sprinkles sparkles, captions tosses letter blocks, finish and export plate. With no stage names,
     cycle stir, season, toss, peek on a 4 s loop, faster as `done/total` rises. Accept the legacy `{step,i,n,pct,eta_s}`
     shape too.
   - **Interaction (the fun part):** raycast pointer events. Click a ghost: squash-and-stretch giggle, a floating "hee hee"
     bubble and a short bing. Drag moves it along the counter plane and releases with a spin. Double-click: a little dance.
     Cursor pointer on hover. Captions rotate every 2.5 s with original lines, for example "Folding in a little B-roll",
     "Whisking the sync to soft peaks", "Marinating shot 7 of 23", "Teaching the ghosts to count frames". The bing is a
     Web Audio oscillator (two-note bell) created on the first click, so there is no sound file and no licence question;
     nothing plays on its own because of autoplay policy.
   - **Preview (needed for the owner's check and for filming; a real re-render takes many minutes):** `Kitchen.preview()`
     runs the overlay for about 25 s on a simulated status stream (23 shots, `plan` for 3 s, then one shot per ~0.9 s, then
     `join`), the readout is prefixed "preview ·" (honesty: never present it as a real render),
     then runs the plating beat. `kitchen.js` calls it itself when `location.hash === "#edit/kitchen"` on `load` and on
     `hashchange` (no `app.js` edit needed; app.js ignores the unknown second hash segment). The falling cards use the same
     `demo/thumbs/%04d.jpg` stills as a real run and fall back to plain cards.
   - **Light on the CPU:** manual `requestAnimationFrame` loop capped at 30 fps (33 ms accumulator), `setPixelRatio(min(dpr,
     1.5))`, antialias off, pause on `document.visibilitychange` hidden and whenever `#view-edit` is hidden, a
     `ResizeObserver` on `.player`, and on `finish()` dispose of the geometry, materials and renderer and cancel the loop.
     Create the WebGL context lazily on first use.
   - **Fallbacks:** if `import()` fails, `new WebGLRenderer` throws, or `matchMedia("(prefers-reduced-motion: reduce)")`
     is true, show a CSS kitchen instead: the mascot via `window.GhostMark?.(64,"cook")` (a text glyph if it does not
     exist), bobbing with CSS transforms, plus caption and readout, no animation loop. The page works with no
     `vendor/` folder at all (then three's absence uses the fallback).
3. **Wiring in `app/ui/app.js` (minimal).**
   - `startCutjobRerender()`: after the POST succeeds call `Kitchen.start({mode:"cutjob"})` and `video.pause()`; show the
     poster behind the overlay. Keep `#renderBar` text as a secondary readout.
   - `pollCutjob()`: pass each status to `Kitchen.update(st)` **before** the `if (st.state === "running") return;` line (today
     a running status returns immediately and would never reach the scene). On `done`, **do not reload**: `const j = await api("/api/project");
     ({project:P, edits:EDITS, engine:ENGINE} = j); applyResult(j);` then assign a cache-busted `video.src =
     \`demo/video.mp4?v=${Date.now()}\`` (a separate assignment; leave the `VQ` constant and the literal
     `demo/video.mp4${VQ}` untouched, tests assert it), refresh `video.poster`, `$("#tcDur").textContent = fmt(P.duration)`,
     call `renderWords(); lockEdits();` (the initial render builds the Words list and the edit lock once; `applyResult` does not
   redo them, so without these the Words tab and the locked-edit state go stale), call `layout()`, wait for the video's
   `canplay` (cap at 4 s), then `Kitchen.finish()`. Thumbnail URLs carry no version for cut jobs, so a heuristic browser
   cache can show old timeline thumbs until a reload: accepted, say so in the BUILD-LOG. If anything in that chain
     throws, fall back to the old `location.reload()`. On `failed`, call `Kitchen.fail(st.message)` and keep today's error
     bar.
   - `Kitchen.finish()` plays the plating beat (a ghost lifts a plate with a film card, the card scales to the player
     rectangle while the canvas fades, about 600 ms, then the overlay hides) and leaves the first video frame visible and
     paused (do not rely on autoplay; the user's Render click may allow `video.play()`, wrap it in try/catch).
   - Also call `Kitchen.update(st)` from `follow()/showProgress()` (legacy) and the offload-cut poller only if time
     remains; both are optional.
4. **Tests `tests/test_kitchen.py` (offline, no socket, no `ps`, no live thread needed):** `node --check app/ui/kitchen.js`
   (skip without node); `sample_parts` on a temp dir with `os.utime`-set files: no folder is `plan`; stale files (older than
   `started_at - 2`) never count; 3 fresh shots is `shots` with `done=2`; a fresh `audio_000.m4a` or `parts.txt` is `join` with
   `done=total=fresh`; `fresh` above the stale `total` raises `total` and never yields `done > total`; the worker never
   leaves `running` after it wrote `done` (drive `_rerender_worker` with `cutjob.rerender` monkeypatched to sleep briefly
   and the sampler interval patched small); the worker's new
   status fields are additive (`state`, `message` still present); `index.html` contains `id="kitchen"`, `kitchenCanvas`,
   `kitchenCaption`, `kitchenReadout` and the `kitchen.js` script tag; if `app/ui/vendor/three.module.min.js` exists then
   `app/ui/vendor/three.LICENSE` exists, the module header contains MIT or SPDX, and the module has no `import` statements;
   `kitchen.js` has no external URLs, no `eval`, no `/Users/` or `/Volumes/`, and handles reduced motion; the existing
   `UiWiringTests` stay green (every `$("#id")` in `app.js` exists in `index.html`, `node --check app.js`, no machine
   paths in `engine.py`/`app.js`/`index.html`/`app.css`).

## Files you own and the only edits to shared files

New files (yours alone): `app/ui/kitchen.js`, `app/ui/kitchen.css`, `tests/test_kitchen.py`. (`app/ui/vendor/` is added by
the owner's agent before the build; do not create or modify it.)

Hook edits, minimal; mark each with a comment containing `KITCHEN`:
- `app/ui/index.html`: pre-applied by the owner's agent (see the Merge contract); only if missing, inside `.player`, after `<video id="video">`, one line:
  `<div class="kitchen" id="kitchen" hidden><canvas id="kitchenCanvas"></canvas><div id="kitchenCaption"></div><div id="kitchenReadout"></div></div>`;
  one `<link rel="stylesheet" href="kitchen.css">` after the other stylesheet links; one `<script src="kitchen.js">` before
  the `app.js` tag. Nothing else.
- `app/ui/app.js`: only inside `startCutjobRerender`, `pollCutjob` (every branch, as described) and, optionally,
  `follow/showProgress`. No reformatting of anything else.
- `app/engine.py`: only `_rerender_worker`, plus a new module-level `sample_parts` function and, if you want one, a small
  sampler helper next to it (the extra fields live in `_cutjob_state`). Nothing in `Engine.__init__`, `ask`, `_store_voice`,
  `do_GET` or `_post`.
- `README.md` and `docs/muse-code/BUILD-LOG.md`.
- Do not touch `app/ui/app.css` (put everything in `kitchen.css`), the Import page, `ghost.css` or the mascot files (ONBOARD
  owns them; use whatever exists, and degrade gracefully when `window.GhostMark` or the new tokens are missing, using
  token fallbacks like `var(--glass, #161a23)`), cutjob code, `voice.py`, `cli.py`, or `dress*` anything.

## Rules

`OPEN-BRIEF.md` "Rules (hard)". Also:
- Offline at demo time: no CDN, no web font, no remote asset. **Vendored library:** three.js is vendored MIT code, kept with its LICENSE; the
  BUILD-LOG and README state that it is third-party (not hackathon work) with its version and licence, and that the
  scene, the choreography, the sampler and the hand-off are new.
- Honesty: describe it as an interactive 3D ghost kitchen (it is WebGL when available); if it ran on the CSS fallback in the
  owner's check, the README must say which one the recording shows.
- Sandbox has no GPU: you cannot see WebGL render. Keep the scene code defensive (feature-check, try/catch around the
  setup, fallback on any error), and leave the visual check to the owner's agent. Never start a server or a render.
- Never call Meta, ElevenLabs, xAI or Whisper in tests. No client names or local paths in code, tests or docs.
- Provenance: a KITCHEN section in `docs/muse-code/BUILD-LOG.md` in the OFFLOAD format (title, Step/By/What table, "Muse
  Code token use: TBD", build notes, a "Live test" section left for the owner's agent) and a row in the README "Who built
  what" table: "Ghost kitchen render screen (interactive, offline). Built by Muse Code on muse-wonder-1.0 from
  `KITCHEN-BRIEF.md`, after my review of the app on submission day; three.js (MIT) is vendored third-party code."
- Run the whole test suite before you finish: green apart from the same 11 pre-existing `ps`/socket errors in
  `tests/test_engine_render.py` and `tests/test_render_runner.py` (give your before/after counts in the BUILD-LOG).

## If time remains (stretch, in this order)

- Hook the offload-cut poller on the Import page's "Make the cut" (coordinate by calling `Kitchen` only if it is present on
  `window`; ONBOARD owns that page).
- A "sad ghost" `fail()` beat of about 2 s that restores the poster.
- Letter-block tossing for captions, sparkle particles for grade (cut first if the scene runs long: keep the pot, falling
  film cards, one chef ghost, real progress and the plating hand-off, which are the must-haves).
