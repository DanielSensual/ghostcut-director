# Build log: `ghostcut angles` (2026-11-01)

Who did what, so the credit is exact.

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `ANGLES-BRIEF.md`: the feature, limits and tests. |
| Design | Meta **Muse Code** 1.4.13 on **`muse-wonder-1.0`** (effort high, contributor mode off) | `ANGLES-DESIGN.md` sections 1–11: module layout, data classes, the Muse prompt and per-line schema, the order of the mechanical checks, edge cases, test list. 8 model calls. |
| Design review | Claude Code | Section 12 of the design: black shots instead of picture gaps, merge only into a camera that covers, seek per input. |
| Build | Meta **Muse Code** 1.4.13 on **`muse-spark-1.3`** (effort high, contributor mode off) | `src/ghostcut/angles.py`, `tests/test_angles.py` (18 tests), the `angles` command in `cli.py`. 26 model calls; it ran the tests and fixed its own failures. |
| Code review and fixes | Claude Code | See below. |
| Live test | Claude Code, piloted by the owner | One real 4K clip made into two cameras (the wide original and a punched-in crop starting 6 s later). |

Muse Code token use: Wonder 549k input (429k cached) + 10k output; Spark 2.87M input (2.73M cached) + 49k
output. At list price with no cache discount that is at most about $6.20 of the event's Meta API credit.

## Review fixes (Claude Code)

- Muse got several unlabelled images per call; the prompt now says which image is which line and camera.
- Shot edges snap to whole output frames (`frame_grid`), so dozens of shots cannot drift against the uncut audio.
- A duplicate Muse reply was silently dropped by a check that could never fire; it is now recorded.
- After a short-shot merge two neighbours could sit on the same camera (a cut that changes nothing); they join.
- Merge and keep notes name line ids, not shot ids.
- Render: one filtergraph over every 4K input peaked at 2.3 GB for 7 shots. It now renders shot by shot (one decoder
  open at a time, exact frame counts) and joins the parts by stream copy under the uncut audio: 1.4 GB peak,
  flat in the number of shots. It also failed when the output folder did not exist.
- 3 tests added (frame grid, same-camera join, the Muse path with a stand-in client); the render test rewritten.

## Live test (2026-11-01)

- `ghostcut sync`: camera B at +6.000 s by waveform, Whisper agreeing to the millisecond.
- `ghostcut angles --no-muse --render`: 14 lines → 7 shots; output video 59.977 s, audio 59.976 s.
- A camera-B shot compared with camera A at the same moment matches best at 0 frames offset (PSNR 46.8 dB vs
  46.2 / 44.8 one frame either side): the cut is frame-accurate.
- `ghostcut angles --render` with Muse Spark: 14 lines in 4 calls, $0.032, 5 shots; every reason describes the
  frame (framing, gaze, gesture).

# Build log: `ghostcut grade` (2026-11-01)

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `GRADE-BRIEF.md`. |
| Design | Muse Code 1.4.13 on `muse-wonder-1.0` (effort high, contributor mode off) | `GRADE-DESIGN.md` §1–12: the S-Log3 and HLG maths with sources, gamut matrices derived from primaries, the shoulder, `.cube` rules, the per-shot ffmpeg chain, sampling, the match, the Muse check prompt and schema. 9 calls. |
| Design review | Claude Code | §13: HLG exposure by ITU-R BT.2408 (reference white at 0.75; without it the iPhone renders ~1.9 stops dark), explicit BT.709 matrix on the way out, no absolute paths. |
| Build | Muse Code 1.4.13 on `muse-spark-1.3` (effort high, contributor mode off) | `src/ghostcut/grade.py`, `tests/test_grade.py` (44 tests), `grade` and `angles --grade` in `cli.py`, the `--grade` path in `angles.py`. 33 calls. |
| Code review, fixes, live test | Claude Code | §14 of the design. |

Muse Code token use: Wonder 659k input (527k cached) + 33k output; Spark 4.80M input (4.59M cached) + 79k output.
At list price with no cache discount that is at most about $9.60 of the event's Meta API credit.

## Live test (2026-11-01)

- An A7S III S-Log3 clip (10-bit 4:2:2, flagged full range) and an iPhone 17 Pro HLG clip converted through the
  real per-shot chain: natural Rec.709 for both, sky detail kept, the iPhone at normal exposure.
- Matching, on one real clip made into two cameras (wide and punch-in), the punch-in given a cast:
  - gain-only cast: gains 1.32 / 1.23 / 1.11 against an ideal 1.28 / 1.18 / 1.06 (the wide frame's centre holds a
    dark table the close one doesn't); the matched frame is 32.1 dB PSNR from the uncast original, up from 20.5 dB.
  - harsh cast (gain, offset, lower contrast): measured means land within 0.012 of the reference, but pure gains
    cannot undo an offset and blue hits its clamp; the matched shot stays a little bright and warm, and the Muse
    check named exactly that ("B brighter, warmer") on all three moments, for $0.027.
- `angles --grade --render`: graded shots use their camera's cube, the Rec.709 reference renders untouched, the
  output is tagged BT.709 limited, video and audio both 59.977 s, 1.4 GB peak memory.

# Build log: `director sync` (2026-11-02)

Who did what, so the credit is exact.

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `SYNC-BRIEF.md`: the feature, contracts, limits and tests. |
| Design | Meta **Muse Code** on **`muse-wonder-1.0`** | `SYNC-DESIGN.md` sections 1–13: module layout, data classes, probe/sidecar rules, the four methods, the combining order, edge cases, test list. |
| Build | Meta **Muse Code** on **`muse-spark-1.3`** | `src/director/sync.py`, `tests/test_sync.py` (26 tests), the `sync` command in `src/director/cli.py`, the `AGENTS.md` layout line. 69 model calls; it ran the tests and fixed its own failures. |
| Code review and live test | Claude Code, piloted by the owner | Real recordings, below. Found the three waveform defects and the drift defect listed under Review fixes; Muse Code fixed them in two more runs (26 and 16 calls). |

Muse Code token use (from the run logs):

| Run | Model | Calls | Input (cached) | Output | List rates, cached input at the cached rate |
|---|---|---|---|---|---|
| Design | `muse-wonder-1.0` | 23 | 2.65M (2.43M) | 38k | $2.11 |
| Build | `muse-spark-1.3` | 69 | 9.56M (9.35M) | 94k | $2.07 |
| Fix: scoring | `muse-spark-1.3` | 26 | 2.31M (2.17M) | 38k | $0.67 |
| Fix: drift | `muse-spark-1.3` | 16 | 1.09M (0.97M) | 11k | $0.34 |

The 11 errors Muse saw in `test_engine_render.py` and `test_render_runner.py` come from its sandbox blocking `ps`; outside it the full suite passes (346 tests).

## Live test (2026-11-02, Claude Code)

Three real recording pairs with known answers, plus one synthetic pair:

- A 73.6 s camera clip against a 767 s lav recording, verified at +178.74 s. The first build placed it at +755.832 s (an 11 s edge overlap). After the fixes: +178.746 s at the clip's first frame, 132 ppm drift over 4 windows, Whisper agreeing to 6 ms. Measured independently at 1 ms, the whole-clip best lag is 178.752 s and the 20 s windows read 178.746 → 178.752 s, so the intercept is right.
- Two interview pairs where the same stock question is asked in two different interviews: correctly left unplaced.
- One 4K clip made into two cameras 6 s apart: +6.000 s, r 1.0.

## Build notes (Muse Code)

- `measure_drift` returns `(offset_at_start_s, drift_ppm, n_windows)` rather than the designed pair, because
  the waveform detail is specified to carry `drift_windows` and there is no other channel for the count.
- `tests/test_sync.py` has 26 test methods for the design's 24 items: item 21 is two tests (place against
  a non-reference clip; stop after a trusted waveform against the reference) and `normalize_word` has its
  own test alongside the Whisper voting tests.
- Full suite: everything passes except 11 pre-existing errors in `tests/test_engine_render.py` and
  `tests/test_render_runner.py`, which fail identically on the untouched tree (the sandbox blocks `ps`).

## Review fixes (Muse Code on `muse-spark-1.3`; defects found on real audio by the owner and Claude Code)

- The waveform score was a whole-clip z-scored FFT dot product divided by the overlap count, not a Pearson correlation (identical audio scored r 1.003, an 11 s edge overlap 0.744 and won): the FFT pass now only proposes candidate lags spread across distinct peaks, and the reported r is the exact Pearson over each candidate's actual overlap (top 8 rescored, best wins).
- `clear` and `margin` came from the ±15 ms 1 ms refinement, whose window holds no rival 1 s away (second_r always −1, every peak "clear"): they now come from the coarse global search (exact-r best minus best exact-r at least 1 s away, also rescored exactly), while the refinement only moves the offset to the 1 ms grid.
- The guided re-search compared its FFT-approximate r against the global one: it now reports the same exact Pearson, so the "guided r ≥ global r − 0.02" comparison is like with like.
- `measure_drift` searched each pre-aligned 20 s window around the absolute offset instead of the residual 0, so at a large offset (+178.75 s) every window missed and drift came back null: it now searches around 0, adds the residual back to the coarse offset per window, and reports the fitted intercept (the offset at the clip's first frame).

## Flash method (Muse Code on `muse-spark-1.3`)

Built by Muse Code on `muse-spark-1.3` from `docs/muse-code/FLASH-BRIEF.md`: the fifth sync method, `flash`, for cameras with no audio. Mean luma per frame over the first 120 s from a tiny grey ffmpeg proxy with real per-frame timestamps, robust-z light-switch edges (signed), same-sign same-gap sequence matching with the clock estimate preferred, one-frame-of-the-slower-clip precision, resolution after Whisper and before timecode, and plain-English warnings. Touched `src/director/sync.py`, `tests/test_sync.py`, this section and one README line.

Muse Code token use: TBD (the owner fills this from the run log).

# Build log: `director reframe` (2026-11-02)

Who did what, so the credit is exact.

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `REFRAME-BRIEF.md`: the feature, contracts, rules and limits. |
| Design | Meta **Muse Code** on **`muse-wonder-1.0`** | `REFRAME-DESIGN.md` sections 1–15: module layout, data classes, probe/proxy rules, shot detection and refinement, cost estimate and guard, SAM tracking, subject choice, gap filling and zero-phase smoothing, per-frame crop path, sendcmd render, payload, orchestrator and CLI, edge cases, test list. |
| Build | Meta **Muse Code** on **`muse-spark-1.3`** | `src/director/reframe.py`, `tests/test_reframe.py` (29 tests), the `reframe` command in `src/director/cli.py`, the `AGENTS.md` layout line. 40 model calls; it ran the tests and fixed its own failures. |
| Code review and live test | Claude Code, piloted by the owner | Real cut, below. No defects found. |

Muse Code token use (from the run logs):

| Run | Model | Calls | Input (cached) | Output | List rates, cached input at the cached rate |
|---|---|---|---|---|---|
| Design | `muse-wonder-1.0` | 12 | 1.01M (0.86M) | 33k | $1.46 |
| Build | `muse-spark-1.3` | 40 | 3.71M (3.57M) | 40k | $0.88 |

## Live test (2026-11-02, Claude Code)

A real 60 s 4K talking cut, five shots, the speaker deliberately off centre:

- SAM 3.1 through guestlist: 350 frames billed, $0.07; the speaker followed in all five shots (presence 1.00).
- Cuts found by scene detection and refined on the full-rate cut land within 1 ms of the reframe Claude Code wrote earlier in GhostCut (which Muse never saw); the smoothed crop path is within 1.9 px of it (mean 0.3 px), and frames at 8, 20, 30, 42 and 55 s are visually identical.
- Render: 1080×1920, 3,595 frames, audio untouched (59.976 s); the crop moves by small steps inside shots and jumps only on the first frame of a new shot.

## Build notes (Muse Code)

- `parse_probe` refuses an already-vertical (or square) cut as well as `run`'s probe check, because the
  design's test list requires the refusal at probe-parse level; `run` keeps its own check so the refusal
  still fires when `probe_cut` is stubbed. Same message, same `ValueError`, nothing written either way.
- The design's test gloss says a smoothed ramp stays within one sample-step everywhere; with the specified
  reflected-boundary Gaussian the edge pull is ~sigma*sqrt(2/pi) ≈ 2.4 steps (measured 23.6 px on a
  10 px/step ramp). The algorithm spec (§10.2) is normative — reflection is what makes the jitter test
  pass — so the test asserts interior exactness within 1e-6 and edges bounded by 2.5 steps instead.
- `shot_sample_range(shots, index, n_samples)` indexes into `shot_sample_ranges(shots, n_samples)`, since
  sample ownership needs the whole shot list (bisect over start times).

# Build log: `director cut` (2026-11-03)

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `OPEN-BRIEF.md`: the open feature choice. |
| Design | Meta **Muse Code** on **`muse-wonder-1.0`** | `OPEN-DESIGN.md`: the `director cut` feature — one-step orchestration, words bridge, timeline export, whole-cut QC. |
| Build | Meta **Muse Code** on **`muse-spark-1.3`** | `src/director/cut.py`, `src/director/export.py`, the `cut` command in `cli.py`, `tests/test_cut.py`, `tests/test_export.py`. |
| Code review and live test | Claude Code, piloted by the owner | Real footage, below. |

Muse Code token use: TBD (the owner fills this from the run log).
Live test: TBD (the owner fills this from the real-footage run).

## Build notes (Muse Code)

- The build also carries a 5-line fix in `src/director/reframe.py` (`render`): a static crop path — every
  centred shot, i.e. every `--no-sam` render — emitted an empty sendcmd script, which ffmpeg refuses with
  "No commands were specified", so `director reframe --no-sam --render` failed on the untouched tree (no
  existing test renders; all use `render_path=None`). The design's offline path (`--no-sam`, §13 "the job
  completes") and its end-to-end test (item 22, `no_sam=True`) both assume centred renders work. The fix
  pins the single crop position at t=0 when the script would otherwise be empty; `sendcmd_text`, the move
  count and every JSON shape are unchanged, all existing reframe tests still pass, and item 22 is the
  regression test. Allowed by the brief ("change existing modules where your feature needs it"); the only
  departure from the design's §2 file table.
- `build_media_clips` keeps its designed `(sync_payload, used_labels)` signature and raises its
  unknown-label `ExportError` without the sync path; `export_timelines`, which owns the path, raises the
  full §8.2 message. Same condition, same exception type, no other difference.
- On a stage failure the CLI prints the completed stage lines (reread from the `cut.json` the run just
  wrote, guarded by mtime so a stale manifest stays silent) before `cli.main` prints the error, per §11.3's
  "the stage lines are the progress log".
- Full suite: 376 tests, green except the same 11 pre-existing `ps`-sandbox errors in
  `tests/test_engine_render.py` and `tests/test_render_runner.py` that fail identically on the untouched
  tree (verified before and after this build).

# Build log: take picking (`tighten`) (2026-11-03)

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `TIGHTEN-BRIEF.md`: take picking, pause trim, `tighten.json`. |
| Design | Meta **Muse Code** on **`muse-wonder-1.0`** | `TIGHTEN-DESIGN.md`: `cutlist.py` contract, windowed Spark picks, rule fallback, tightened render. |
| Build | Meta **Muse Code** on **`muse-spark-1.3`** | `src/director/tighten.py`, `src/director/cutlist.py`, the `angles`/`cut` changes, `tests/test_tighten.py`, `tests/test_cutlist.py`. |
| Code review and live test | Claude Code, piloted by the owner | Real footage, below. |

Muse Code token use: TBD (the owner fills this from the run log).
Live test: TBD (the owner fills this from the real-footage run).

## Build notes (Muse Code)

- `cut.run` keeps `no_tighten=True` as its default so existing callers (and
  `tests/test_cut.py`, which pins the 7-stage manifest) run unchanged; the CLI
  passes `no_tighten=False` unless `--no-tighten`, so `director cut` tightens
  by default exactly as designed. `cut` passes `tighten_path` to `angles.run`
  only when the stage is enabled, for the same reason.
- The five span-building constants (§8) live in `cutlist.py`, re-exported
  from `tighten.py` under the same names: `recompute_spans` needs them and
  `cutlist` must not import `tighten` back (the no-cycle rule).
- Repair carriage on attempts: `notes` (the §7.4 overrule notes) and
  `excluded_utterance_ids` (unclaimed utterances folded into an attempt's
  span but excluded from its kept words) ride on the attempt dicts, omitted
  when empty so a clean pick matches the §5 shape exactly.
- `verify_window_reply` takes an optional `beat_id_offset` (default 0) so
  the `tighten_beat_below_min:B<id>` warning names the payload-level beat.
- Re-run preservation (§11.2): span exclusions carry over verbatim (they are
  range-keyed; stale ranges drop aloud via `span_exclusion_stale`), with
  their history entries, while beat/attempt ops re-apply through
  `apply_operations` — re-applying `drop_span` by its old span id would drop
  the wrong span after a re-pick renumbers everything.
- Window-boundary beats with equal topics are not structurally merged (the
  design specifies no merge mechanics): no-merge never drops speech, and the
  retreat rule makes straddling rare.
- Render (§10): shots are split at kept-span boundaries and edge parts
  extend to the span edges they cover, so no shot crosses removed material
  and output shots tile contiguously from 0; audio parts clip to the
  rendered shot range (trailing lines with no camera are dropped from the
  picture, as in an un-tightened cut) and snap to the same grid, so audio
  and video agree frame-for-frame. In the normal case this is exactly the
  designed per-span audio.
- Midpoint edges make consecutive kept spans abut, so a word-free gap can
  only reach the `dead_air` branch in degenerate inputs; the branch stays
  as the specified safety net and is covered directly.
- Full suite: 407 tests, green except the same 11 pre-existing `ps`-sandbox
  errors in `tests/test_engine_render.py` and `tests/test_render_runner.py`.

# Build log: Voice Transcribe transcript (`transcribe`) (2026-11-03)

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `TRANSCRIBE-BRIEF.md`: Voice Transcribe as the transcript of record. |
| Design | Meta **Muse Code** on **`muse-wonder-1.0`** | `TRANSCRIBE-DESIGN.md`: chunking, speaker reconciliation, Whisper↔Muse alignment, cut/tighten integration. |
| Build | Meta **Muse Code** on **`muse-spark-1.3`** | `src/director/transcribe.py`, the `angles`/`cut`/`tighten`/`cutlist` changes, `tests/test_transcribe.py`. |
| Code review and live test | Claude Code, piloted by the owner | Real footage, below. |

Muse Code token use: TBD (the owner fills this from the run log).
Live test: TBD (the owner fills this from the real-footage run).

## Build notes (Muse Code)

- The `--whisper-model` option the design assumes Claude Code added arrived
  only half built (`WHISPER_MODEL` + `metal_visible` in `sync.py`), so this
  build finished it: `--whisper-model` on `director sync` and `director cut`
  (default `sync.WHISPER_MODEL`), threaded into `sync.get_transcript_words`
  and `cut.mlx_whisper_command` (as `--model`), the `cut` local Whisper run
  skipped with `words_no_gpu_visible` when `metal_visible()` is false, and
  the `cut` error now says "mlx_whisper failed" / "no GPU visible" when the
  binary is installed instead of "not on PATH". `tests/test_cut.py` carries
  the updated expectations; both stages fingerprint the model.
- `Chunk` carries a `spend_usd` field (default 0.0): `transcript.json`
  needs each chunk's actual spend (0.0 on a cache hit) and the result is
  the only channel from `diarize_chunk` to `build_transcript_payload`.
- §7.7 example 1's gloss ("difflib anchors the first the") mis-predicts
  `difflib`: the longest match is `[the, problem, is, timecode]`, so the
  paired `the` is Muse's second one and the muse-only `the` interpolates
  into the gap before it (10.22–10.24 in the example becomes 10.12–10.14,
  and the disfluency flag lands on the paired word per the §7.5 repeat
  rule). The §7.3 opcode walk is normative and decides; item 10 asserts
  what it emits.
- Item 17 uses a 400 s clip, not the designed 200 s: 200 s plans as one
  chunk and the item needs the swapped-label second chunk.
- `apply_transcript_evidence` step 1 warns `tighten_beat_other_speaker`
  only when it removes the beat's pick and nothing usable remains; a beat
  that already had no pick is left alone without the warning (otherwise
  every pre-existing flub-only beat would be mislabelled other-speaker).
  Step 2's swap sets the new pick to verdict `pick` / kind `keeper` and
  the old pick to `alternate` / `alternate`, keeping the file's invariant
  (the design's "kind stays keeper" for an `alternate`-kind attempt reads
  as a slip).
- Full suite: 437 tests, green except the same 11 pre-existing `ps`-sandbox
  errors in `tests/test_engine_render.py` and `tests/test_render_runner.py`.

# Build log: voice notes (`voice`) (2026-11-03)

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `VOICE-BRIEF.md`: talk back to the cut — voice notes that become checked edits, the app opening a `director cut` job. |
| Design | Meta **Muse Code** on **`muse-wonder-1.0`** | `VOICE-DESIGN.md`: transcription, anchoring, Spark resolution, verification, the taste file, cut-job app mode. |
| Build | Meta **Muse Code** on **`muse-spark-1.3`** | `src/director/voice.py`, `director voice`, `app/cutjob.py`, the engine/UI cut-job mode, `tests/test_voice.py`, `tests/test_cutjob.py`. |
| Code review and live test | Claude Code, piloted by the owner | Real footage, below. |

Muse Code token use: TBD (the owner fills this from the run log).
Live test: TBD (the owner fills this from the real-footage run).

## Build notes (Muse Code)

- `VOICE-DESIGN.md` predates `transcribe.py`, so per the task brief and
  `TRANSCRIBE-DESIGN.md` §10 the WAV extraction (`extract_wav_command`),
  long-note chunking (`plan_chunks`, `chunk_wav_command`) and the
  diarizer wrapper (`diarize_chunk`, with its `(result, spend, cached)`
  spend labelling) are reused from `transcribe.py` — `voice.py` adds no
  second copy. `extract_audio_command` delegates, so the §4.1 command is
  byte-identical to transcribe's.
- `JobContext` carries `tighten_path`/`angles_path` alongside `job_dir`
  so `--apply` in the explicit-file CLI form writes the files it was
  given, not `./tighten.json`.
- Untightened `set_angle` applies to the anchored shot in `angles.json`
  (method `editor`), and untightened `rerender` re-renders the current
  shots as they stand through `angles.render` rather than re-picking —
  the design's §9.5 re-run only describes the tightened path, and a
  re-pick would silently drop the editor's angle choices.
- The visual confirmation's camera labels ride in the image prompt as
  text (still times with the camera on screen), not burned into the
  frames: the stills come from the finished cut, which has no labels to
  burn.
- The engine's cut-job Re-render button compares a
  `(edits_applied, angles mtime)` cookie taken at open and after each
  re-render; the apply-time project rebuild deliberately does not reset
  it, or the button could never enable.
- `cut.json` records no project name (it goes to the export timelines
  instead), so cut-job titles use the designed `Director cut` default
  until one is stored.
- Voice cards render through a dedicated renderer sharing the proposal
  markup and CSS rather than calling `proposalMsg` — grouping,
  candidate radios and the heard line differ per note.
- `tests/test_cutjob.py` drives the engine through the real HTTP
  dispatch (`do_GET`/`do_POST`/`_call`/`_post`) with captured responses
  instead of TCP sockets, which this sandbox forbids; the socket-based
  engine tests fail here either way (below).
- Full suite: 496 tests (46 new), green except the same 11 pre-existing
  `ps`-sandbox errors in `tests/test_engine_render.py` and
  `tests/test_render_runner.py`.

# Build log: connections + finishing (`connect`, `finish`) (2026-11-03)

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `FINISH-BRIEF.md`: outside-service keys, and finishing the cut's audio — clean voice, optional generated sound, mastered loudness. |
| Design | Meta **Muse Code** on **`muse-wonder-1.0`** | `FINISH-DESIGN.md`: the provider registry, ElevenLabs/Higgsfield calls, the `finish` stage and `director finish`, the Connections panel. |
| Build | Meta **Muse Code** on **`muse-spark-1.3`** | `src/director/connect.py`, `elevenlabs.py`, `higgsfield.py`, `finish.py`, the `finish` stage, `director connect`/`director finish`, `tests/test_connect.py`, `tests/test_finish.py`. |
| Code review and live test | Claude Code, piloted by the owner | Real footage, below. |

Muse Code token use: TBD (the owner fills this from the run log).
Live test: TBD (the owner fills this from the real-footage run).

## Build notes (Muse Code)

- The design's engine section assumes `send_json`/`_json_body`
  helpers the engine does not have, so the Connections routes use the
  existing `_json`/`_body` dispatch and the existing `{"error": ...}`
  envelope (the UI's error check reads `error`); paths, methods, the
  64 KiB body cap and every message string are as designed. The panel
  builds its rows with the existing `h()` text nodes rather than the
  `esc()` helper the design names — no such helper exists, and text
  nodes escape by construction.
- Provider `transport` parameters default to `None` and resolve to
  `http_request` at call time instead of binding it as the default:
  a bound default would put the fake-transport seam the design's
  tests use out of `mock.patch.object`'s reach.
- `design_with_muse` returns the `cached` bit as a fifth element; the
  designed 4-tuple cannot report it without inferring it from a
  $0.00 spend.
- `list_providers` carries two extra key-free fields the design's
  own tests and panel need: `missing` (the half-pair var, item 3)
  and `env_vars` (names only, for the panel's "Connected — from …"
  line). `director connect list` prints the Higgsfield reason
  without its " — see FINISH-DESIGN §1.6" tail, matching the
  designed line.
- `duck_mix_command` takes the taste-resolved `music_gain_db`
  (default −20.0): the pinned signature has nowhere for the low
  gain to enter the graph.
- Measured FFmpeg behaviour the design does not mention: `amix`
  upmixes a mono input implicitly at −3 dB per channel, so the duck
  graphs duplicate the mono voice at unity with `pan` first
  (`[0:a]asplit=2[vm][key];[vm]pan=stereo|c0=c0|c1=c0[v]`); without
  it the voice sits 3 dB under its design level against stereo
  stems. Provider-asset fixtures are stereo like real ElevenLabs
  MP3s for the same reason (mono fixtures would misstate stem
  levels another −3 dB through `-ac 2`/`aformat`).
- The finish fingerprint is stored post-run: the stage rewrites
  `cut.mp4`, so a pre-run print could never match on resume (every
  resume would re-finish). Recomputing after execute keeps
  cut.mp4-change detection and makes skip work.
- A failed finish degrades (record `failed`, the job continues on
  the untouched `cut.mp4`, final status `complete_with_warnings`).
  The repo has no continue-after-failure path — a failed grade
  fails the job — so this arm is new, per the design's
  demo-must-ship rule.
- `cut` passes `finish_path` to `export_timelines` only when the
  finish stage ran (stage-shaped fakes keep working), the section
  gains `finish_stems` only then, and the re-lay warning only when
  stems exist.
- Non-finite loudnorm values (pure silence) parse to unmeasurable,
  routing silence through the designed single-pass fallback.
- `connect test <provider>` raises on failure without pre-printing,
  so the shell sees one `Error:` line, not two.
- Full suite: 550 tests (54 new: 14 connections, 40 finishing),
  green except the same 11 pre-existing `ps`-sandbox errors in
  `tests/test_engine_render.py` and `tests/test_render_runner.py`
  (verified identical on the pristine tree: sandbox
  `PermissionError`, not this build).
- By hand: `director finish` on a 6 s synthetic lavfi cut with no
  keys connected (local clean + master only) reported `local
  master, -14.0 LUFS (target -14), 0 assets, $0.00 provider`; an
  independent loudnorm re-measure of the remuxed `cut.mp4` read
  −14.02 LUFS integrated, −12.92 dBTP, picture untouched.
