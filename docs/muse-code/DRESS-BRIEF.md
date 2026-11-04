# Brief: dress the cut, B-roll by talking (`director dress`: title cards, ElevenLabs music, a ghost that pops with a bing)

Written 2026-11-04 by the project owner with Claude Code for Meta's Muse Code agent. Build it straight from this brief
on Muse Wonder. Budget: about 75 minutes of agent time; the build must be done by 4:00 PM ET (submission is 9:00 PM).
Two sibling builds (ONBOARD: first-run tutorial, Import page, shared design and the ghost mascot; KITCHEN: render loading
screen) run in parallel worktrees; stay inside the files this brief names. **All live API calls (Spark, ElevenLabs, xAI)
happen after the build, run by the owner's agent. You have no network: build against fakes and test offline.**

**Merge contract (all three builds).** The owner's agent pre-applies every `<link>`, `<script>`, button and overlay line for
all three builds to `app/ui/index.html` before you start (stylesheets after `app.css` in the order `ghost.css`,
`import.css`, `kitchen.css`, `dress.css`; scripts between `demo/project.js` and `app.js` in the order `ghost/mascot.js`,
`import.js`, `onboard.js`, `kitchen.js`, `dress.js`; a Dress button with `id="dressBtn"` in `.transport`). Check that yours are
there and add only what is missing, at that spot. Sibling files will not exist in your worktree (expected 404s: never assert
their presence, and every use of `window.GhostMark` or `window.Kitchen` is optional-chained). In `app/engine.py` put your
`do_GET` routes right after the `/api/cutjob/rerender` line and your `_post` routes right after the
`/api/cutjob/rerender` branch; do not edit `Engine.__init__` (keep dress state in `getattr(self, "_dress_state", ...)`),
`_rerender_worker` or `offload_*`. README row: at the end of the "Who built what" table (after "The briefs in
`docs/muse-code/`"). BUILD-LOG: append at the end (the merge keeps all three).

**Scope triage (about 75 minutes, so build in this order and stop when time is up, noting what is unfinished in the
BUILD-LOG).** Tier 1, the visible result: `dress.py` plan + gate + assets + ASS cards + mix + encode, the `director dress
plan|render|show` CLI, their tests. Tier 2, the app: the `cutjob.build_project` link, the `/api/dress*` routes, a *minimal*
`dress.js` panel (plan list with enable toggles and Remove, Plan and Render buttons, progress, Dressed/Plain switch, one
text box). Tier 3, talking to it: the note grammar and its engine/voice wiring (item 4 below). The ghost sprite belongs to
Tier 1 (the owner asked for "a little ghost that pops up, bing") but if it threatens the schedule, ship the bing and the
pill first and the sprite second. Everything under "If time remains" stays out until all three tiers are done. In
`dress.json`, `history`, `design`, `broll` and `limits` may be omitted or reduced to what the code actually uses.

Read `AGENTS.md`, `docs/muse-code/OPEN-BRIEF.md` (its "Rules (hard)" apply), `docs/muse-code/BUILD-LOG.md`, then
`src/director/finish.py` (reuse its pure builders: `extract_voice_command`, `music_mix_command` ~583, `sfx_mix_command`
~604, `duck_mix_command` ~627, `loudnorm_measure_command`, `parse_loudnorm_full`, `loudnorm_apply_command`,
`limiter_command`, constants `DUCK_*`, `MUSIC_GAIN_DB`, `MUSIC_GAIN_LOW_DB`, `SoundCue`, `MusicPlan`, `FinishAsset`,
`_licence`, `_elevenlabs_key`, `_replace_master` ~1168 for the verify-then-replace discipline), `src/director/captions.py`
(`burn_command` ~785, `_clean_source` ~833, `TITLE_END_S`, `ACTIVE_BGR`, `filter_caption_words`, `map_words_to_output`,
`group_lines`, `build_ass`, `stage_fonts_dir`, `propose_title_with_muse` and `title_transcript` for the Spark pattern),
`src/director/elevenlabs.py` (`generate_music`, `generate_sound_effect`, `estimate_music_usd`, `estimate_sfx_usd`),
`src/director/voice.py` (read-only: `normalize_quote_token` ~718, `_blocked`/`_passed` ~1265 for the check-dict shape
`{ok, text, hard}`, `MIX_MESSAGE`, `UNPLACED_MESSAGE`),
`src/director/cli.py` (`command_finish` ~516), `app/cutjob.py` (`build_project` ~358-362, `rerender` ~388), `app/engine.py`
(`_store_voice` ~255, `voice_apply` ~324, `_voice()` ~170 for the lazy-import pattern, `_rerender_worker` ~1050 for the
background-thread pattern), `app/ui/app.js` (`proposalMsg` ~378, `askLive` ~430, `voiceNoteCard` ~560, `applyResult` ~379),
`scripts/director_test_run.py` (`_ping` ~881 and `_whoosh` ~894, a numpy bell and whoosh to copy), `tests/test_finish.py`
(its `RouteTransport` and `FakeGuestlist` patterns), `tests/test_voice.py` (lines ~511-523 and ~614 are pinned; you do not touch `voice.py`, so they stay green).

## What the owner wants

"If they want to add B-roll, they can just talk to the agent. The agent has the B-roll organized. It'll render the B-roll
into the screen. For the demo, the B-roll is just like AI title cards, as title text, fonts or pop-up things, so they can
see, as it's scrolling, oh, there's stuff there. If we can call some music off the ElevenLabs API, so they can see the
features, boom, boom, boom. With GhostCut there's a little ghost that pops up, bing. So it just looks more interactive.
Right now it looks like a cheap, quick video edit that's super simple. But if we have AI video stuff, that's where the
B-roll would go. Myself narrating. There's cards, there's talks, there's voice, there's all of it." Title cards are the
priority; AI video B-roll is optional and last.

## What exists (read from the code, do not guess)

- Real job (read-only, never write to it in tests): `cut.mp4` 1920x1080, 24000/1001 fps, 130.133 s, AAC stereo with
  captions burned in; `cut.clean.mp4` is the same without captions; `captions.json` holds `lines[]` (110 lines, 414 words, each with `words[{text,start_s,end_s}]`) on the **output**
  timeline plus a `title` block whose `file` is the *vertical* `title.png`: the horizontal `title-1920x1080.png` (shown
  0-3.5 s, `TITLE_END_S`) sits next to it in the job folder and is not named in `captions.json`, so use it when the file
  exists and `title` is not null; `captions-horizontal.ass` is the ready-made 1920x1080 caption script; `finish.json` shows voice-only at about
  -14.77 LUFS, `assets: []`. `.cut.mp4.captioned` holds `size:mtime_ns` of the captioned file. `cutjob.rerender` replaces
  `cut.mp4` with an **uncaptioned** render.
- `finish.run_files` is the wrong tool (re-cleans mastered audio, remuxes `cut.mp4` in place, places SFX only in word
  gaps). Reuse its pure command builders and constants, never call `run_files`.
- `/api/connections` reports ElevenLabs `connected:false` today (the engine has no env key; no `keyring` in `.venv`). Not a
  bug to fix: the owner's agent starts the engine with `ELEVENLABS_API_KEY` and `XAI_API_KEY` in the environment.
- The timeline has B-roll and Cards tracks (`#trkBroll`, `#trkCards`) filled from `P.broll` and `P.cards`; both are empty
  for this job. `build_project` symlinks `job/cut.mp4` as `app/ui/demo/video.mp4`.
- Spark pattern: `cache.key`, `cache.guard`, `spark._structured`, `guestlist` imported **inside** the function,
  a `...Unavailable` exception on ImportError. Spark reasons before it answers, so give `max_output_tokens` about 6000.
- The mic does not work inside the pywebview window; typed notes work everywhere, and voice works in a browser tab
  (`http://127.0.0.1:<port>/app/ui/`). The engine routes `/api/voice/record|file|apply` already exist and must keep working.
  **The typed chat is a different path:** `askLive` posts to `/api/muse`, `engine.ask` runs the legacy demo-project Muse and
  `proposalMsg`'s Accept calls `/api/accept`, which cannot accept voice-kind proposals (it indexes `muse.OP_SCHEMA` keys), so
  typed dress notes must not be routed through `engine.ask`; the voice cards (`voiceNoteCard`) call `/api/voice/apply`,
  which is why item 3 builds on `_store_voice`/`voice_apply`.
- Pinned tests that must stay green: `test_voice.py` ~511-523 pins the exact `RESOLVE_SCHEMA` op enum (so **do not add ops
  to the schema**; Spark never learns dress ops), ~614 pins "turn the music down a bit" to an `add_note` with the exact
  `MIX_MESSAGE`; `test_connect.py` ~126/366/415 pins `PROVIDERS == ["elevenlabs","higgsfield"]` (do not touch `connect.py`).

## The job

Two commands, so editing a plan never re-pays for API calls: `director dress plan` (network) writes `dress.json` and caches
assets; `director dress render` (ffmpeg only, offline) writes `cut.dressed.mp4`. It never touches `cut.mp4`,
`cut.clean.mp4`, `finish.json` or `angles.json`. Target runtime for render: 1 to 3 minutes for the 130 s cut, never the
32-minute pipeline.

1. **`src/director/dress.py`** (no app dependency; every pure piece testable; API clients injectable).
   - `source_video(job)`: mirror `captions._clean_source`: if `.cut.mp4.captioned` equals the stamp of `cut.mp4`, the source
     is `cut.clean.mp4`, else `cut.mp4` (a fresh uncaptioned re-render). When the stamp matches (the real job), the captions
for the dressed cut are the existing `captions-horizontal.ass`, copied as-is (its `[Script Info]` header, styles and
events) with the card styles and events appended to it. Only when the stamp does not match (a fresh uncaptioned
re-render, whose timeline may differ) rebuild from `angles.load_timed_words`, `captions.filter_caption_words`,
`map_words_to_output`, `group_lines`, `build_ass(1920,1080)` exactly as `captions.run` does ~880-895 (origin and duration
from `origin_from_shots`/`duration_from_shots`, `cut_times_from_shots`, `cutlist.load_tighten`). Do not change
`captions.py`.
   - **`dress.json` (version 1)**, written atomically:
     `{version, source:{video,stamp,duration_s,captioned}, title:{enabled,file,end_s}, captions:{enabled,preset},
     cards:[{id,t,d,text,pos:"top-left|top-right|top-center",ghost,anchor:{quote,word_start,word_end},
     source:"muse|typed|voice|rule",why,enabled}], ghost:{enabled,size_px,sfx}, music:{enabled,prompt,gain_db,duck,asset},
     sfx:{bing:{prompt,duration_s,gain_db,asset}, pop:{...}}, broll:[], limits:{max_cards:8,min_gap_s:0.6,min_card_s:2.0,
     max_card_s:3.2}, assets:[{id,kind,file,provider,model,prompt,duration_s,cost_usd,licence}], design:{model,cached,
     spend_usd}, provider_spend_usd, notes:[], warnings:[], history:[{op,note_id,at}], rendered:{file,source_stamp,at}}`.
     A render is stale when `rendered.source_stamp` differs from the current source stamp or the plan changed.
   - **Planning `plan(job_dir, *, model, max_usd, max_provider_usd, no_muse)`**: one Spark call (`muse-spark-1.3`, effort
     medium) given timed caption lines `[t] text`; reply schema `{cards:[{quote,text,why}]}`; developer prompt rules: 4 to 8
     cards, 2 to 6 words, sentence case, no end punctuation, `quote` is 2 to 6 contiguous spoken words copied from the
     transcript, no numbers, names or claims that are not spoken. **Muse proposes, code decides** (`check_card`): the quote
     must match the output-timeline words (use `voice.normalize_quote_token`-style matching); `t = first_word.start_s - 0.10`,
     `d` clamped 2.0 to 3.2; `t >= TITLE_END_S + 0.3` (the first card starts at or after 3.8 s); `t + d <= duration - 1.0`; at
     least 0.6 s between cards; at most 8; at least 60% of the card's content tokens appear in the spoken text within
     +-20 s; reject numeric tokens that are not spoken (number words count: "eight hours" allows "8 hours"); reject
     path-like text (copy the `CARD_PATHLIKE` regex idea from `director_test_run.py` ~911). Spark down, no Meta key or
     `--no-muse`: write no auto cards and say so in `notes`; typed and voice cards still work. An optional tiny rule
     lexicon (sync, captions, B-roll, grade) may be added and labelled `source:"rule"`.
   - **Assets `ensure_assets`**, cached in `job/dress/assets/<kind>-<sha8>.mp3` hashed over prompt plus length bucket, so a
     second run makes zero HTTP calls. Music bed: `elevenlabs.generate_music` (instrumental), length is the cut duration
     rounded up to 30 s, estimated about $0.375, default prompt "Minimal modern documentary tech underscore, soft synth pads,
     light pulse, optimistic, no vocals, steady 95 bpm". Bing: `generate_sound_effect`, 1.0 s, "single bright glass bell
     ping, cheerful magical sparkle, clean". Pop: 0.5 s "soft cartoon pop". Offline fallback for bing and pop: a numpy
     synth copied from `_ping`/`_whoosh` with note `dress_sfx_local`; music has no fallback (render continues with no bed
     and note `dress_music_unavailable`). Spend gate: `--max-provider-usd` default 1.00; status handling like finish (401/403
     /402/429 stop with a note such as `finish_provider_auth_failed:elevenlabs`, retry 5xx); never print a key; every error
     string through `connect.redact`; record each asset like finish (`FinishAsset`, `finish._licence`, costs flagged as
     estimates).
   - **Ghost sprite `build_ghost_frames`**: from `docs/brand/ghostcut-icon-1024.png` (already in the repo and served; the
     owner's own ChatGPT-made art, say so in the README row), crop it to its alpha bounding box first (the art is a rounded tile with a transparent margin; use the alpha channel's
`getbbox()`), then Pillow builds a cached RGBA PNG sequence
     `dress/ghost/pop_%03d.png` at 24 fps, 320x320 canvas, icon at 200 px: scale-in 0 to 1.18 to 1.0 (ease-out-back, 0.35 s),
     a bob/tilt hold (about 1.0 s), scale-out (0.2 s), about 38 frames, built once per job. If Pillow or the icon is missing,
     skip the ghost with a warning (cards still render).
   - **Cards `build_ass`**: one extra ASS file appended to the captions events (`PlayRes 1920x1080`). Per card two events on
     `\an5` at the same `\pos`: a rounded-pill vector drawing (`\p1`, dark 70% fill, `\bord3`, border colour from
`captions.ACTIVE_BGR`; draw the path starting at `m 0 0` so the drawing's box begins at the origin and `\an5` centres
the pill and the text on the same point; the live check must look at one rendered frame to confirm) and the text in the shipped Poppins SemiBold (via `stage_fonts_dir`) at 64 px. Pill width =
     `ImageFont.truetype(...).getlength(text)` plus padding; inject a fake measurer in tests. Pop animation
     `\fscx60\fscy60\t(0,140,\fscx108\fscy108)\t(140,260,\fscx100\fscy100)`, `\fad(0,200)`. Positions alternate top-left
     (x about 330, y about 150) and top-right so cards stay clear of the caption lower third (y >= 830); `pos` is
     overridable. The ghost sprite sits beside the pill, its first frame at `card.t`. Use `ass_escape`.
   - **Sound `mix_commands` (pure)**: 1) voice = `finish.extract_voice_command(source, dress/voice.wav)`; 2) bed =
     `finish.music_mix_command(bed, duration, MusicPlan(prompt=..., start_s=0, end_s=duration, gain_db=-20, dropouts=[]), out)`
(`gain_db` is not applied there; the dress graph below applies `music_gain`); 3) a dress
     specific mix that ducks **the bed only**: `[0:a]asplit[vm][key];[vm]pan=stereo|c0=c0|c1=c0[v];[1:a]volume={music_gain}dB[m];
     [m][key]sidechaincompress=threshold={DUCK_THRESHOLD}:ratio={DUCK_RATIO}:attack={DUCK_ATTACK_MS}:release={DUCK_RELEASE_MS}[bed];
     [v][bed][2:a]amix=inputs=3:normalize=0[out]` (reusing finish constants; do not use `finish.duck_mix_command`, it would
duck the bing under speech; the SFX bus gets **no** extra `volume` in this graph, because `finish.sfx_mix_command` already
applies each cue's `gain_db`, so a second gain would count twice); SFX default cue gains -10 dB bing, -16 dB pop; 4) master with
     `loudnorm_measure_command` / `parse_loudnorm_full` / `loudnorm_apply_command` then `limiter_command`, target from
     `finish.json target_lufs` else -14; 5) the bing and pop are placed with `finish.sfx_mix_command` using one `SoundCue(id="bing_01", kind="bing",
at_s=card.t+0.05, duration_s=1.0, prompt=..., gain_db=-10)` per card (`SoundCue` has no defaults). Order of steps: 1, 2, 5,
3, 4.
   - **Encode (one ffmpeg video pass)**: inputs: source video, the title PNG if `captions.json` has one, one
     `-framerate 24 -i dress/ghost/pop_%03d.png` per pop card, the mastered `dress/master.wav` last. Filter: title overlay
     `enable='between(t,0,3.5)'`; per pop `[k:v]format=rgba,setpts=PTS-STARTPTS+{t}/TB[g{i}];[v{i-1}][g{i}]overlay=x=..:y=..:
     eof_action=pass:repeatlast=0`; finally `subtitles=filename=<abs>/dress/overlay_all.ass:fontsdir=<abs>/caption-fonts`, both through
`captions._escape_filter_path` (the real job lives under a path with a space in it, so the tests use a temp job folder whose
name contains a space). Output:
     `-c:v libx264 -preset fast -crf 17 -pix_fmt yuv420p -r 24000/1001 -c:a aac -b:a 192k -movflags +faststart` (no `-c copy` on
     video). Write `cut.dressed.next.mp4`, probe, check duration within +-0.1 s of the source, then `os.replace` to
     `cut.dressed.mp4`. Progress: `-progress pipe:1` parsed into `dress/status.json` (`{state,pct,eta_s,message}`).
   - **Voice/text grammar `parse_note(text, *, job_dir, playhead_s=None, words=None) -> list[dict]|None`** (deterministic
     regex plus quote matching; offline; first match wins, case-insensitive):
     "add (a) card/title/pop-up/text that says X" (optional "where I say Y") -> `dress_add_card` anchored at Y, else the
     playhead, else unplaced (blocked with `UNPLACED_MESSAGE`); "add B-roll/a card/a title where I say Y" (no text) ->
     card text = Y title-cased, at most 6 words; "remove/delete/lose the card (at Y / here)" -> `dress_remove_card` (nearest to
     the quote or playhead); "no cards" / "more cards" / "fewer cards" -> `dress_set_cards` (off / re-plan / keep every other);
     "no music" / "add music" / "more music" or "music louder" (+4 dB) / "less music" or "quieter music" or "turn the music
     down" (-4 dB), gain clamped -30 to -10; "different music: Z" / "make the music Z" -> `dress_set_music` prompt change
     (flag "costs about $0.375" as a soft check; the next plan regenerates); "no ghost" / "no bing" / "no sound effects"
     -> `dress_set_ghost` / `dress_set_sfx` off. `validate_op`: text at most 6 words and not path-like, `t >= 3.8`, `t+d <=
     duration-1.0`, at least 0.6 s from another card, at most 8 cards. `apply_ops(job_dir, ops)` writes `dress.json` and
     appends `history`. `summary(job_dir)` returns the compact state for the UI. Example on the real job: "add B-roll where
     I say synced" -> a card "Synced" at about 14.26 s (the word "synced" is at 14.36 s in `captions.json`).
2. **CLI `src/director/cli.py`**: register `director dress plan|render|show [--job DIR] [--max-provider-usd N] [--no-muse]
   [--ai-video N]`, following `command_finish` (print what it did, the cost estimate and the next command). `--ai-video`
   may be accepted and print "not built, use DRESS stretch" unless you reach the stretch item.
3. **Engine `app/engine.py` and cutjob `app/cutjob.py`.** Every route is a thin `do_GET`/`_post` branch over an `Engine`
   method (`dress_get`, `dress_plan`, `dress_render`, `dress_note`), so tests call the methods and need no socket.
   `dress` is imported lazily through a `_dress()` helper that copies `_voice()` (path guard, `Unavailable` on ImportError).
   - `GET /api/dress` -> `{state, plan, status, stale, fresh}` (plan = `dress.json` or null; `fresh` = a dressed render
     exists for the current source and plan); `POST /api/dress/plan`; `POST /api/dress/render` (background thread like
     `_rerender_worker`, state `idle|running|done|failed`, busy guard raising `Busy` (HTTP 409), status read from
     `dress/status.json`; not allowed while `_cutjob_state` is running). `POST /api/dress/note {text, at}` ->
     `{matched:false}` or `{matched:true, id, reply, ops, usd:0, cached:true, model:"rules"}`: it runs
     `dress.parse_note`, checks each op with `dress.validate_op` (adding `blocked`, `checks:[{ok,text,hard}]`, `label`,
     `why`) and stores one proposal through `_store_voice` (one synthetic resolved note, `raw_text` = the text) so
     `/api/voice/apply` accepts it unchanged.
   - **Voice and typed notes without touching `voice.py`.** (Spark's op schema is pinned by `test_voice.py` and the mic path is
     long; do not edit `voice.py`.) Add `Engine._dress_notes(notes)`, called at the top of `_store_voice`: for each note dict
     whose `raw_text` matches `dress.parse_note(...)` (card, B-roll, ghost, bing notes always; music and level notes only
     when `job_dir/dress.json` exists or the text says "add music"), replace the note's ops with the checked dress ops, set
     `status="resolved"` and `message` to the first label. Whatever Spark or the no-key stub put there for that note is
     dropped (the dress grammar is specific enough to win). This also covers the no-Meta-key case, where the notes arrive as
     unresolved stubs. Then add `Engine._dress_split_apply(notes, pick)`, called in `voice_apply` right before
     `vm.apply_notes` (and for the `ops=` path): it applies the picked, unblocked `dress_*` ops with `dress.apply_ops(job,
     ops)` and marks those ops `blocked=True` in the notes it hands to `vm.apply_notes`, which skips blocked ops, so the flat
     op indexes the UI sends stay valid. Test both helpers directly with synthetic notes (no voice job fixture, no socket).
   - `cutjob.build_project`: `demo/video.mp4` stays the plain cut (`_link_video(render, ...)` and `_stills` unchanged); when
     `cut.dressed.mp4` is **fresh**, also symlink it as `demo/dressed.mp4` (a stale one is ignored and any old link removed)
     and put `{"video":"demo/dressed.mp4","fresh":true}` in the summary; import `dress` lazily with the same `src/` path guard
     as `require_director_angles` and skip all of this on ImportError. Add `project["cutJob"]["dress"] =
     dress.summary(job_dir)` and fill `project["cards"]` from `dress.json` cards as `{id, t, d, lines:[text]}` (the timeline
     does `c.lines.join(...)`, `app.js` ~265) so the Cards track shows them.
4. **The note grammar `parse_note` (Tier 3), exactly the table in item 1, and its wiring above.** `voice.py` and
   `RESOLVE_SCHEMA` stay untouched: Spark never learns dress ops, the pinned `test_voice.py` enum and the "turn the music
   down a bit" `add_note` test stay green by construction, and `connect.py` is not touched either. `dress.validate_op` returns
   the same check dicts as `voice._blocked/_passed`; `dress.op_label(op)` gives the label (for example "Card: Synced by sound @14.3s").
5. **UI `app/ui/dress.js` + `app/ui/dress.css` (small, `GCDress.mount(ctx)`).** `ctx` is passed by `app.js` and carries what the
   script needs (`h`, `$`, `api`, `fmt`, `LIVE()`, `project()`, `seek`, `applyResult`, `ask(text)` = the chat's own `reply`);
   do not rely on `window.GC` (ONBOARD's helper may not exist in your worktree). A "Dress the cut" panel on the Edit view,
   opened by `#dressBtn`: the plan (cards with time, text, an enable toggle and Remove), music and ghost toggles, a "Plan" and a
   "Render" button with a progress bar polling `GET /api/dress`, a "Dressed / Plain" switch that sets `#video`'s `src` to
   `demo/dressed.mp4?v=<now>` or `demo/video.mp4` (keeping the current time), and **one text box "Tell Ghost what to add"**.
   The text box and the existing chat box share one interceptor: a capture-phase `submit` listener on `#askForm` (active only
   when `ctx.LIVE()` and the project is a cut job) calls `preventDefault()` and `stopImmediatePropagation()` synchronously
   (the server answer is async), clears `#askInput`, and posts the text to `/api/dress/note`. When `matched` it renders a
   small proposal card in `#chat` (label, checks, Accept / Reject; Accept calls `/api/voice/apply {id, pick}` then
   `ctx.applyResult(j)` and refreshes the panel); when not matched, or if the request fails, it calls `ctx.ask(text)` so the
   chat behaves exactly as before. It works with the Dress panel closed.
   Voice (mic) notes reach the same grammar through `_store_voice`. **The render shows the kitchen:** on Render click call
   `window.Kitchen?.start({mode:"dress"})`, feed each poll to `window.Kitchen?.update({state:"running", step:"Dressing the
   cut", pct, eta_s})`, and on `done` swap the video source, wait for `canplay` (cap 4 s) and then `await
   window.Kitchen?.finish()`; on `failed` call `window.Kitchen?.fail(message)`. Without `Kitchen` (sibling not merged) the
   panel's own bar is the only progress. Say in the panel that the mic needs a browser tab (the window's mic is unavailable)
   and that typing works everywhere; if `window.GhostMark` exists use it in the header, else a text glyph; tokens with
   fallbacks (`var(--glass, #161a23)`).
6. **Tests `tests/test_dress.py` (offline, fakes, in the style of `test_finish.py`; no socket, no `ps`).** The gate rejects: a
   card inside the 0 to 3.8 s title window, one in the final second, one closer than 0.6 s to another, one with an unspoken
   digit (and accepts "8" for spoken "eight"), a quote not in the words, a path-like text, a ninth card. `locate`/anchor
   matching finds the right words for "synced them together". `build_ass`: `h:mm:ss.cc` times, `\pos`/`\t` strings,
   `ass_escape`, pill width from an injected measurer; appending cards to an existing `captions-horizontal.ass` keeps its
   events untouched. Ghost frames with Pillow: count, size, alpha, cropped to the tile (skip without PIL). Command builders:
   right `-i` count, the title overlay `between(t,0,3.5)`, `eof_action=pass`, `sidechaincompress` only on the music branch
   with the SFX branch after the duck and no second `volume` on it, no `-c copy` on video, absolute escaped paths for a job
   folder with a space in its name. Assets: cache hit means zero HTTP calls, the spend cap stops generation, a 401 stops, an
   empty body skips, a missing key falls back to the local bing and music is skipped with the note. Spark: a fake reply
   passes the gate; a garbage reply, a Spark failure and a missing `guestlist` degrade to no cards. Grammar table: every
   phrase in item 1. Engine, by direct method calls: `dress_note("add B-roll where I say synced")` yields an unblocked
   `dress_add_card` and stores a proposal; `_dress_notes` replaces a Spark-style `add_note` for the same words, leaves
   "turn the music down a bit" alone without `dress.json` and turns it into `dress_set_music` -4 dB with one;
   `_dress_split_apply` writes `dress.json` and marks the ops blocked for `vm.apply_notes`; `dress_render` with a fake
   renderer raises `Busy` when called twice; `build_project` links a fresh `cut.dressed.mp4` as `demo/dressed.mp4`, ignores a
   stale one, and fills `cards` with `lines`. End to end (skip without ffmpeg, libass or Pillow): a 4 s synthetic `lavfi`
   `testsrc2` + `sine` source in a temp folder with a space in its name, one card, pre-seeded `lavfi`-made assets; assert
   duration within +-0.1 s, audio present, frame size unchanged, source files byte-identical. The existing `UiWiringTests`,
   `test_voice.py` and `test_connect.py` stay green untouched; `node --check app/ui/dress.js`. The full suite is green except
   the same 11 pre-existing `ps`/socket errors in `tests/test_engine_render.py` and `tests/test_render_runner.py` (give your
   before/after counts in the BUILD-LOG).

## Files you own and the only edits to shared files

New files (yours alone): `src/director/dress.py`, `app/ui/dress.js`, `app/ui/dress.css`, `tests/test_dress.py`. (Optional,
only for the stretch item: `src/director/xai.py`.)

Hook edits, minimal; mark each with a comment containing `DRESS`:
- `src/director/cli.py`: one `dress` subparser block and its `command_dress`.
- `app/engine.py`: the `/api/dress*` routes and their `Engine` methods (`dress_get`, `dress_plan`, `dress_render`,
  `dress_note`), `_dress()`, and the two helpers `_dress_notes` (called at the top of `_store_voice`) and `_dress_split_apply`
  (called in `voice_apply`). Not `Engine.__init__`, not `_rerender_worker`, not `offload_*`.
- `app/cutjob.py`: `build_project` only.
- `app/ui/index.html`: pre-applied (see the Merge contract); add only what is missing.
- `app/ui/app.js`: (a) one `window.GCDress?.mount({...ctx})` call right after the `loadTaste();` line, and one
  `window.GCDress?.refresh?.()` inside `applyResult`; (b) in `voiceNoteCard`, make the success text
  `"✓ Applied · re-render to watch it"` read `"✓ Applied · open Dress the cut and Render"` when the note's ops start with
  `dress_` (a one-line conditional). Do not touch `startCutjobRerender` / `pollCutjob` (KITCHEN), `PAGES.import` or the start
  line (ONBOARD).
- `README.md`, `docs/muse-code/BUILD-LOG.md`.
- Do **not** touch `voice.py`, `connect.py`, `captions.py`, `finish.py` (import from them only), `ghost.css`, mascot files,
  `kitchen*`, `import*`, `onboard*`.

## Rules

`OPEN-BRIEF.md` "Rules (hard)". Also:
- **No network in the sandbox and none in tests.** Meta is reached only through `guestlist`, imported inside the function
  with a fallback. No footage leaves the machine: only transcript text goes to Spark, only prompt text goes to ElevenLabs
  (and xAI in the stretch).
- Keys: read `ELEVENLABS_API_KEY` through the existing helper; never print, log or write a key; do not read `.env*` files.
- Offline at demo time: the **render** is ffmpeg-only and works without any network once assets are cached; the app shows the
  dressed cut with no remote asset.
- Fast pass: never re-run the cut pipeline, never modify `cut.mp4`, `cut.clean.mp4`, `finish.json`, `angles.json`.
- Rights and terms: ElevenLabs outputs are licensed by the plan on the key that generated them. Say in the BUILD-LOG that the
  owner's agent confirms that plan allows commercial use of generated music and sound effects before the demo uses them
  (the per-asset licence note from `finish._licence` already says "subject to the provider's terms"; do not claim more).
- Rights: generated music and SFX are fine (record provider, model, prompt and licence note per asset in `dress.json` and the
  BUILD-LOG). The ghost sprite is the owner's own brand art (not Muse-built; `docs/brand/NOTICE.md`). No third-party sounds,
  logos or fonts beyond the shipped Poppins (OFL). No committed media: everything generated stays in `job/dress/`.
- No client names, people's names or local footage paths in code, tests or docs; use temp dirs and synthetic media.
- Provenance: a DRESS section in `docs/muse-code/BUILD-LOG.md` in the OFFLOAD format (title, Step/By/What table, "Muse Code
  token use: TBD", build notes, a "Live test" section left for the owner's agent that lists the live checks: restart the engine
  with the keys in the process env only, `director connect test elevenlabs`, `director dress plan`, check the cards against
  the transcript, `director dress render` and time it, check caption collisions at the top corners, the title overlap and the
  bing audibility under speech, then try "add B-roll where I say synced" and "turn the music down") and a README "Who built
  what" row: "Dress the cut: title cards, ElevenLabs music and SFX, ghost pop-up (`director dress`). Built by Muse Code on
  muse-wonder-1.0 from `DRESS-BRIEF.md`, after my review of the app on submission day; music and SFX generated with ElevenLabs
  during the event; the ghost sprite is the owner's own brand art." Add to README "Keys" that XAI_API_KEY is read from the
  environment only (no screen shows a saved key).
- Run the whole test suite before you finish: green apart from the same 11 pre-existing `ps`/socket errors named in item 6.

## If time remains (stretch, in this order)

- Typed/voice "no cards", "more cards", "fewer cards" end to end through the engine.
- `--ai-video N`: `src/director/xai.py` (stdlib HTTP, key from `XAI_API_KEY` env only, errors through `connect.redact`):
  `POST https://api.x.ai/v1/videos/generations` with `{model:"grok-imagine-video-1.5", prompt, duration, resolution}`, poll
  `GET /v1/videos/{request_id}`, take the URL from `.video.url`; estimate about $0.25 per second under `--max-provider-usd`;
  fill `dress.json broll[]` as `{id,t,d,prompt,asset,status}` and add one more `-i clip.mp4` per entry with
  `scale=1920:1080,setpts=PTS-STARTPTS+T/TB,fade=in/out:alpha=1` and `overlay=enable='between(t,T,T+d)'`; no people or crew in
  prompts; label output AI-generated in the BUILD-LOG; keep it out of `connect.PROVIDERS`. The request shape is from the
  owner's notes (verified July), not re-checked today: the owner's agent verifies it live before anything depends on it.
- A rule lexicon fallback for planning (`source:"rule"`) so a no-key demo still has cards.
