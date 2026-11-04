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

## Lips method (Muse Code on `muse-spark-1.3`)

Brief and validated recipe by the owner with Claude Code (`docs/muse-code/LIPS-BRIEF.md`), built by Muse Code on
`muse-spark-1.3`: the sixth sync method, `lips`, for cameras with no usable audio. A shipped Swift helper reads
Apple Vision inner-lip openings per frame (built once with `swiftc -O` into the cache, keyed by the source hash;
the method skips with a warning when swiftc or Vision is unavailable). Each clip with picture gets a coarse
low-resolution pass to find the median face box, then full-rate frames cropped around it with margin at full
resolution (the camera's `.cube` from `grade.json` or a supplied cube applied first), scaled so the face is ~400 px
tall, over the 300 s window with the most speech. The measured signal is kept as validated: 3-frame average minus
the ~1 s average, z-scored, for lips and for band-passed log-RMS audio alike; Pearson correlation at each lag over
the whole overlap; confidence from the best-over-rival-more-than-0.5-s-away ratio (below 1.5 cross-checks only,
2 and up good); the clock only breaks near ties. Lips-vs-lips is reported first and preferred; resolution runs
waveform → whisper → lips → flash → timecode → clock, and a lips placement is named with its ratio in the
warnings (which is also how it reaches `cut.json`). Touched `src/director/sync.py`, the new
`src/director/lips_helper.swift`, `tests/test_sync.py`, this section and one README line.

Muse Code token use: 65 calls; 9.07M input (8.84M cached) + 80k output on muse-spark-1.3.

### Live test (2026-11-03, Claude Code, the owner's shoot)

A lav recorder (28:35, reference A), the A7S III facing the speaker (B), and a Mavic 3 at 45° with no audio, in
D-Log with the DJI cube supplied (C). The drone's clock disagreed with the camera's by 476 s.

- B by waveform at +899.146 s (r 0.979). Its lips-vs-audio cross-check read +899.215 s (ratio 1.17): mouths move
  ~70 ms before the sound, a steady bias, which is why lips-vs-audio only cross-checks.
- **C placed by lips-vs-lips against B at +895.408 s, ratio 5.86** (r 0.926 over 104 s). Its lips-vs-audio estimates
  read +895.475 to +895.478 s (ratios 1.15–1.28); minus the 69 ms bias measured on B they land at +895.409 s, 1 ms from
  the lips-vs-lips placement.
- 21.6 minutes of wall time for the two cameras (300 s each, full-resolution crops of 5.1K HEVC).
- Fixed by Claude Code after the run: supplied cubes were keyed by label only; `sync.run` now also accepts clip
  paths, and `director cut --lut PATTERN=CUBE` feeds them.

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

# Build log: burned-in captions + cinematic title (`captions`) (2026-11-03)

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `CAPTIONS-BRIEF.md`: karaoke captions in the house style, the serif title hook, the `captions` stage. |
| Build | Meta **Muse Code** on **`muse-spark-1.3`** | `src/director/captions.py`, the `captions` stage in `cut.py`, `director captions` in `cli.py`, `tests/test_captions.py` (41 tests), `assets/fonts/` committed. Small enough to build straight from the brief; no separate design. |
| Code review and live test | Claude Code, piloted by the owner | Real footage, below. |

Muse Code token use: TBD (the owner fills this from the run log).
Live test: TBD (the owner fills this from the real-footage run).

## Build notes (Muse Code)

- `cut.run` keeps `no_captions=True` as its default so existing callers
  (and `tests/test_cut.py`, including the 7-stage end-to-end run) behave
  unchanged; the CLI passes `no_captions=False` unless `--no-captions`,
  so `director cut` captions by default exactly as briefed.
- The output timeline follows `finish`/`export`: tighten-mapped when a
  `tighten.json` is present (identity otherwise), shifted by the first
  shot's start, clamped to the cut length. Muse-only and disfluency
  words drop before mapping; a word straddling a kept edge drops whole.
- Lines are 2–4 words (`bold`: 3–4, caps) with hard breaks at kept-span
  joins, shot cuts in an inter-word gap and pauses over 0.6 s. A
  remainder below the minimum joins back (5 words split 2+3, never
  4+1); a run that cannot split cleanly (5 words at 3–4) stays one
  over-long line rather than strand a tail, and is the only way a line
  leaves its word-count range.
- The vertical house style line is the brief's line byte-for-byte
  (asserted); horizontal scales every size by frame height, and the
  horizontal burn reads its own `captions-horizontal.ass`.
- A requested title renders as a PNG card (vertical, plus a scaled
  card when the horizontal master burns); `--title auto` degrades to
  no title with a warning on any failure, like the finish design call.
  Playfair is searched but never shipped; Georgia Bold, then the
  shipped Poppins, are the fallbacks.
- libass reads a job-local `caption-fonts` staging dir (shipped Poppins
  plus `--fonts-dir`), never `assets/fonts` directly: the shipped
  folder also holds the OFL text, which libass otherwise tries to open
  as a font. A verbose burn confirms it loads the staged Poppins file.
- The captions fingerprint is stored post-run (the stage rewrites the
  mp4s, as finish does), and re-runs burn from the kept `*.clean.mp4`,
  never from captioned.
- Full suite: 621 tests (41 new), green except the same 11 pre-existing
  `ps`-sandbox errors in `tests/test_engine_render.py` and
  `tests/test_render_runner.py`.
- By hand: `director captions` on a 6 s white 1080x1920 lavfi job with
  an explicit title burned `vertical.mp4` (clean kept, durations
  equal); the t=1 s frame shows the serif title over its gradient and
  the caption with the champagne word, both readable on white, inside
  the safe zone; at t=5 s the title is gone and captions continue; a
  `--captions-horizontal` re-run burned `cut.mp4` from clean too.

# Build log: `director demo` (2026-11-03)

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `DEMO-BRIEF.md`: try it in one command, no footage needed. |
| Build | Meta **Muse Code** on **`muse-spark-1.3`** | `src/director/demo.py`, the `demo` command in `cli.py`, `tests/test_demo.py` (25 tests), the CI step, plus the two pipeline fixes below. Built straight from the brief (no separate design step). |
| Code review and live test | Claude Code, piloted by the owner | Real footage, below. |

Muse Code token use: TBD (the owner fills this from the run log).
Live test: TBD (the owner fills this from the real-footage run).

## Build notes (Muse Code)

- Layout: `<out-dir>/shoot/` holds the four clips plus `demo-words.json`, `script.txt` and
  `demo-shoot.json`; `<out-dir>/job/` holds the real `director cut` job (the shoot cannot live
  inside the job directory: `cut` drops anything under its own output from discovery).
  `demo.json` at the root records the checks beside the printed report.
- The 58-word script is eight utterances, all under the 6 s utterance split, with the restart as
  utterance 2 carrying no terminal period; the rule fallback removes exactly it (5.2 s of words,
  ~6.2 s out with the half-pauses), verified in `test_demo.py` against `rule_beats` directly.
- `say`/espeak voices are fitted to the planned grid proportionally per run (real word times come
  from local Whisper when it can run); the tone stand-in is sample-exact. A backend that errors or
  renders under 5 s of audio is skipped with a printed note — a sandboxed `say` exits 0 with an
  empty file — and `--keep` probes one spoken word first so it reuses a tone shoot instead of
  rebuilding one every run.
- Words pass to `cut` as explicit `--words` (timeline times; the lav is the reference at 0) only
  when local Whisper cannot run; when it can, the pipeline transcribes, retrying with the script
  timings if the words stage fails. The removal check is gated only on the deterministic path
  (script words, offline); with live Whisper words or `--muse` the take choice is reported, not
  gated.
- Two pipeline defects the demo exposed, fixed in this build:
  - `angles.covers` counted audio-only clips as covering cameras, so any job with a lav reference
    opened on the lav and the render failed ("Stream map '' matches no streams"). Covering now
    needs picture (`width > 0`), matching `tighten`'s own convention; `voice._compute_covers`
    passes widths through (its two failures caught this).
  - Tightened audio rendered one AAC part per kept span and stream-copy joined them, stacking
    ~21 ms of encoder priming per span: the decoded cut ran ~0.2 s long on the demo and `finish`
    failed its mux check on any cut with more than a few spans. Parts are PCM now and the final
    join encodes AAC once, like the un-tightened path; `test_audio_part_command` pins the
    corrected command and the synthetic render test asserts decoded length.
- Full suite: 608 tests, green except the same 11 pre-existing `ps`-sandbox errors in
  `tests/test_engine_render.py` and `tests/test_render_runner.py`.
- By hand: `director demo --out-dir /tmp/director-demo-check` reports PASS (offsets +1.000/+4.000
  exact by waveform, the silent camera clearly unplaced, 6.2 s removed, all nine stages done,
  finish at −14.0 LUFS). Frames checked per camera: labels render, pictures differ.

# Build log: gaze angle choice + skin match (`look`) (2026-11-03)

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `LOOK-BRIEF.md`: cut to the camera the speaker looks at, match skin not walls. Written after the owner watched the final take's cut (v2). |
| Build | Meta **Muse Code** on **`muse-spark-1.3`** | The lips helper pose output, `src/director/gaze.py`, the `angles` gaze rule, prompt numbers and `angles.json` evidence, the `grade` face-region match (`solve_skin_match`, `skin_stats`, whole-frame fallback), `tests/test_gaze.py` (21 tests), new `test_grade.py`/`test_sync.py` tests. Built straight from the brief (no separate design). |
| Code review and live test | Claude Code, piloted by the owner | Real footage, below. |

Muse Code token use: TBD (the owner fills this from the run log).
Live test: TBD (the owner fills this from the real-footage run).

## Build notes (Muse Code)

- The helper prints Vision yaw/pitch/roll in degrees after the face box
  (`<index> <opening> <box> <yaw>,<pitch>,<roll>`, each `NA` when
  unreported); `LipsFrame` carries the pose and the parser keeps old-format
  lines working, dropping only the pose (never the frame) when the pose
  token is malformed.
- `gaze.py` samples 3 frames per line per covering camera through the
  camera's LUT (one helper run per job) and calls a frame addressed at
  |yaw| ≤ 18°. Exactly one majority-addressed camera wins the line
  (`speaker addresses camera B: |yaw| 6° vs 41° on C`); anything else
  leaves the line to Spark with the numbers in its prompt
  (`[gaze: B |yaw| 6° (3/3 frontal); …]`). Gaze overrides run after picks
  (Muse or rule) and before editor overrides and the checks, so coverage,
  merging and cut points still decide; each `angles.json` line carries its
  gaze evidence (`null` when the helper is unavailable, e.g. Linux).
- `grade` measures skin inside the central 60% of the Vision box at the
  shared moments (paired: both cameras need a face) through the conversion
  cubes, and solves gain as before plus contrast about the matched mean
  (k = ref_std / matched_std clamped to 0.8–1.25, lift holding the mean,
  final gain to 0.7–1.4, lift to ±0.10). No paired face falls back to the
  whole-frame match with a `no_face:<label>` warning (`region: frame` vs
  `skin` in `grade.json`); a missing helper warns once per run.
- Full suite: 689 tests, green except the same 11 pre-existing `ps`-sandbox
  errors in `tests/test_engine_render.py` and `tests/test_render_runner.py`.
- By hand on the owner's final-take cards (lav + front Sony + 45° drone,
  read in place, all output under `/tmp/muse-look-test`, never `job/` or
  `cards/`): `grade --reference C` with the DJI cube falls back cleanly
  and reproduces the owner's gains exactly (1.111/1.252/1.071); `angles
  --no-muse --tighten` samples all 30 lines (177 frames, one helper run)
  and records no-face evidence without changing a pick. Vision itself was
  sandbox-blocked here (ANE model fails to load, so no numeric yaw); the
  18° threshold follows the brief's 15–20° range and the viewed geometry
  (addressed ≈ frontal, other ≈ 40° turn). A hand-boxed 4-moment skin
  probe through the real pipeline closes exactly (remeasured after-stats
  match predicted to 0.001) and confirms the premise: frontal skin
  already agrees (B 0.34 vs C 0.28) while the whole-frame gains overshoot
  it to 0.38 (+0.10 on blue).

# Build log: mic-air de-plosive + peak limiter (`finish` air) (2026-11-03)

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `AIR-BRIEF.md`: tame mic air on the peaks, from the recipe measured on the owner's take. |
| Build | Meta **Muse Code** on **`muse-spark-1.3`** | The de-plosive + limiter in `src/director/finish.py`, `--no-deplosive` in `cli.py`/`cut.py`, `tests/test_air.py` (14 tests). Built straight from the brief (no separate design). |
| Code review and live test | Claude Code, piloted by the owner | Real take, below. |

Muse Code token use: TBD (the owner fills this from the run log).
Live test: TBD (the owner fills this from the real-footage run).

## Build notes (Muse Code)

- Threshold is measured per recording in pure numpy: two cascaded 2nd-order Butterworth
  low-passes at 150 Hz (zero-phase via FFT, matching `ffmpeg lowpass=f=150,lowpass=f=150`
  magnitude to 0.1 dB on the take), 30 ms RMS windows, median over the louder half, +6 dB.
  On the take that is −42.8 dBFS gated / −36.8 dBFS threshold. Without numpy, or when the
  WAV will not read, the threshold falls back to fixed −36.0 dBFS with a
  `finish_deplosive_fallback` note.
- `clean_voice_command` takes an optional linear `deplosive_threshold` (`None` keeps the
  original `-af` chain, so every existing command test stands): with it, the initial
  high-pass becomes the recipe block over `-filter_complex` (high-pass 60, split at 160
  doubled each side, low band through `acompressor` 20:1 attack 0.3 release 40 knee 2,
  `amix=inputs=2:normalize=0`), then afftdn/levelling as before. Isolation, when asked
  for, is cleaned with the same threshold: isolation runs before the de-plosive.
- The record lives at top-level `finish.json` `deplosive` (`enabled`, `threshold_dbfs`,
  `moments`, `fallback`), not under `voice`: a re-run measures the louder mastered file,
  so a voice-nested threshold would break the rerun-voice-equality test. `clean_chain`
  is unchanged for the same reason.
- `moments` counts above-threshold 30 ms frames grouped with a 0.30 s merge gap (52 on
  the take): all threshold crossings, where the brief's seven are the loud audible ones.
- The limiter (`alimiter=limit=0.891251:attack=5:release=50:level=0:latency=1`, −1 dBFS,
  ffmpeg defaults, no auto level, delay compensated) runs after loudness normalisation
  (two-pass and single-pass silence fallback alike) and the re-measure records the final
  true peak in `loudness_after`. If the limiter fails, the unlimited master stands with
  a `finish_limiter_failed` warning.
- Full suite: 667 tests (14 new), green except the same 11 pre-existing `ps`-sandbox
  errors in `tests/test_engine_render.py` and `tests/test_render_runner.py`.
- By hand, on the owner's take (`finish/voice_raw.wav`, read only) through the shipped
  code: threshold −36.8 dBFS, 52 moments; low band at the seven moments −9.6/−11.8/−9.8/
  −11.9/−13.7/−10.1/−14.2 dB (mean −11.6 dB), normal speech low band −0.6 dB, speech
  level −0.4 dB; mastered −14.82 LUFS, final true peak −0.99 dBTP (limiter transparent).
  Test outputs went to `/tmp/muse-air-test/`: this sandbox forbids writes to the named
  `/Volumes/.../muse-air-test/` folder (reads worked).

# Build log: slow push-in on every shot (`--push-in`) (2026-11-03)

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `PUSH-BRIEF.md`: a slow push-in on every shot, from the owner's note on the final take ("the video looks good, need a slight push-in effect on it"). |
| Build | Meta **Muse Code** on **`muse-spark-1.3`** | The push-in segment in `src/director/angles.py`, `--push-in` in `cli.py`/`cut.py`/`cutjob.py`, `tests/test_push_in.py` (22 tests) plus 2 cut tests. Built straight from the brief (no separate design). |

Muse Code token use: TBD (the owner fills this from the run log).

## Build notes (Muse Code)

- Each rendered camera shot scales from 100% on its first frame to
  (100 + PERCENT)% on its last, in the same per-shot filter chain after the
  grade and the fill-or-fit framing, at the output size. Black shots are
  untouched, shot boundaries stay on `frame_grid`, durations are unchanged.
- The zoom centre eases from the frame centre to the speaker: one face
  sample per shot at the shot's middle, rendered at the output size through
  the shot's own grade and framing chain (so the box needs no coordinate
  mapping) and read by one lips-helper run. The gaze/lips data carries no
  persisted face boxes, so the shot-middle sample is the face source; when
  it has no face the shot pushes about the frame centre. At 3-6% only
  near-centre faces are reached; zoompan clamps the window at the frame
  edges and the clamped drift stays smooth (measured, no jumps).
- Smoothing was chosen by measurement on a synthetic bar pattern (black
  bars on white, per-frame centroids against the ideal path; static noise
  floor 0.00 px), hard case 3% over 120 frames: `scale` with `eval=frame`
  plus `crop` with `iw`-based offsets is broken (crop x/y only re-evaluate
  when the input size changes, and `iw` is then init-stale: the zoom pivots
  about the top-left corner, +29 px error); zoompan with no upscale
  stair-steps (rms 0.58 px, holds on half the frames); zoompan on a 2x
  upscale still sawtooths (rms 0.29 px, max 1.13 px). The two 4x finalists
  tie (zoompan rms 0.05 px, scale/crop rms 0.05 px, both with clamped drift
  clean); zoompan won on render speed (2.3 s vs 4.0 s per 120 frames) and
  on mechanism (documented per-frame z/x/y rather than the crop re-config
  quirk). The committed render test asserts the brief's metric on the real
  chain: frame-to-frame scale rises monotonically with no reversals and
  lands at 1.05 for a 5% push.
- `push_in` is recorded in `angles.json` and top-level in `cut.json`, and
  is part of the angles render fingerprint, so changing it re-renders. The
  app's re-render keeps the job's push-in on both paths.
- Full suite: 727 tests (24 new: 22 in `tests/test_push_in.py`, 2 in
  `tests/test_cut.py`), green except the same 11 pre-existing `ps`-sandbox
  errors in `tests/test_engine_render.py` and `tests/test_render_runner.py`.
- By hand, on a synthetic two-camera shoot (`testsrc2` + sine, all output
  under `/tmp/pushcli/`): `director angles --no-muse --push-in 5 --render`
  records `push_in: 5.0`, samples one face frame per shot, and renders
  1920x1080 with the exact frame count and duration (102 frames, 3.4 s);
  the pushed render differs from `--push-in 0` at the same duration.

# Build log: voice-note phrase cuts (`voice` fixes) (2026-11-04)

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `VOICEFIX-BRIEF.md`: phrase-level cuts, quote-to-range resolution, angle-note anchors, after the owner tested voice notes on the final take. |
| Build | Meta **Muse Code** on **`muse-spark-1.3`** | The `drop_words`/`restore_words` ops in `cutlist.py`, quote ranges + take-all + cut-edge anchors in `voice.py`, the override-survives-merge rule in `angles.py`, 28 tests. Built straight from the brief (no separate design). |
| Code review and live test | Claude Code, piloted by the owner | Real footage, below. |

Muse Code token use: TBD (the owner fills this from the run log).
Live test: TBD (the owner fills this from the real-footage run).

## Build notes (Muse Code)

- `drop_words`/`restore_words` go through `apply_operations` like every
  other op. Drops live in `payload["word_exclusions"]` as word-index
  ranges, which are stable across re-picks (unlike span ids), so they
  also survive take swaps and carry over verbatim on `tighten` re-runs
  with their history, exactly like span exclusions. `recompute_spans`
  splits kept runs at dropped words; the new edges sit mid-gap beside
  the dropped words under the edge-silence rule (0.5 s cap), and the
  hole classifies as `editor_drop`. The checks: whole-number indices
  ("cuts inside a word"), all words in one kept span, and — by
  simulating the drop on a copy — no new kept piece under 0.35 s and
  no beat emptied silently ("That is the whole of beat B<n> — drop
  the beat instead"). A duplicate drop passes as an already-dropped
  no-op; a partial overlap blocks.
- Quote resolution is mechanical: Spark sends the quoted words with
  `word_start`/`word_end` -1 (it never invents numbers) and Director
  finds the ranges. Hyphens split on both sides, so "B roll" and
  "B-roll" match either way, and a lone "B-roll" clears the two-token
  floor as two content tokens. One match fills the op; "both"/"all"/
  "every" expand one op per match; several matches without them ask,
  with candidates carrying their word ranges so the editor's pick
  applies that range. A `drop_span` whose span id names nothing but
  whose quote lands inside kept lines converts to `drop_words` — that
  was the real run's shape ("Drop span: No kept span in this cut").
- Cut-edge anchors sit after quote matching and before camera
  references: "at the very start / the opening / the first shot" lands
  on output 0 and the first kept beat, "at the end / the last shot /
  closing" on the duration and the last kept beat. Bare "the end"
  anchors; "the end of &lt;that bit&gt;" does not. Shot lines in the
  resolve prompt now name each camera's make, model and file stem, so
  Spark can map "the drone camera" to the DJI file.
- `_merge_short`: a short shot carrying an `angle_override` whose
  surviving neighbour has none gives the merged shot the override's
  camera (which must cover the merged lines; `clip_path` follows the
  camera), with the override note carried onto the merged shot. When
  the override camera cannot cover the merge, the short shot stays
  (`min_shot_kept`) instead of destroying the explicit choice; when
  the survivor has its own override, its camera wins as before.
- `apply_notes` now honours ambiguous candidate picks: a blocked op on
  an ambiguous note with a picked candidate falls through to retarget
  and re-validation (word picks also fill the candidate's range).
  Before, the blocked-skip ran first, so candidate picks never applied
  — this affected beat-op picks too, and both now have tests.
- Edge behaviour kept as found: a drop at a span edge shrinks the span
  instead of splitting it, and the neighbouring gap keeps its previous
  classification — only mid-span drops yield `editor_drop` spans.
- Real-job check on a `/tmp` copy of the final-take job (this sandbox
  blocks network, so stand-in Spark replies built from the two real
  transcripts drove `verify_resolved` + `apply_notes`; no Meta or
  Whisper calls, nothing written to the real job): note 1 resolves to
  `set_angle` B0 → C via the start anchor with no usable quote (and
  via the quote when one is given); note 2 resolves to `drop_words`
  74–75 ("B-roll,") on B2 and 279–281 ("B roll") on B5, through both
  the `drop_words` and the `drop_span` reply shapes, and asks with
  both candidates when "both" is absent. Applied e1–e3, duration
  130.14 → 129.18 s; `angles --no-muse` on the copy opens on C with
  `angle_override:B0`, and the dropped words sit in no kept span. The
  proposal labels show the exact phrases: the stutter "roll," after
  "B-roll," and "get in" around "B roll" remain for the editor to
  judge — the match is the quote, never extended.
- Full suite: 758 tests (28 new: 7 cutlist, 3 angles, 18 voice),
  green except the same 11 pre-existing `ps`-sandbox errors in
  `tests/test_engine_render.py` and `tests/test_render_runner.py`
  (730 on the pristine tree, same 11).

# Build log: OFFLOAD — Ghost, the import agent (2026-11-04)

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `OFFLOAD-BRIEF.md`: the Import page becomes Ghost (`director offload` + the app's Import tab), after the owner's review of the app on submission day. |
| Build | Meta **Muse Code** on **`muse-wonder-1.0`** | `src/director/offload.py` (`list_volumes` / `scan_card` / `plan_offload` / `run_offload`, Ghost intent with a Spark path and a rule fallback), the `director offload` command, the Ghost endpoints and background cut in `app/engine.py`, the Ghost chat on the Import tab (plan card, live progress, shoot bins, Make the cut), `tests/test_offload.py` (14 tests). Built straight from the brief (no separate design). |

Muse Code token use: TBD (the owner fills this from the run log).

## Build notes (Muse Code)

- `list_volumes()` skips hidden volumes, the boot volume and
  installer/DMG drops, then guesses from layout (Sony
  `PRIVATE/M4ROOT` or top-level `M4ROOT`, `DCIM/DJI_*`, `DCIM/*GOPRO*`,
  `DCIM/100APPLE`, WAV-only recorder roots), then volume name, then
  size. A multi-terabyte archive drive that merely contains a `DCIM`
  backup folder still reads as the destination.
- `scan_card()` walks read-only, skipping `.LRF` proxies, thumbnail
  and system trees and dotfiles. Recorded time is ffprobe
  `creation_time`, then the Sony `M01.XML` `CreationDate`, then file
  mtime, and each clip says which (`time_source`); make/model come
  from the probe or the XML `Device`. One card holding two cameras'
  clips yields both camera labels (`FX3`, `7SM3` live), and
  `plan_offload` numbers duplicate labels across cards (`FX3`,
  `FX3 2`) into `<YYYY-MM-DD> <project>/<label>/`.
- Copies stream through `*.part` and rename only after the streamed
  checksum (xxhash when importable, else BLAKE2b) matches the source;
  a mismatch stays unverified with its `.part` kept and named in the
  result. `offload.json` records sources, destinations, sizes,
  checksums, verified and skipped per file. Nothing writes to the
  source card and nothing ejects.
- Ghost: Spark turns the request into a checked intent against the
  live volume facts (guarded import, `MODEL_API_KEY` only); without
  it the rule parser covers "SD card", "drone", "to my SSD" /
  "extreme", project naming, "make the cut", "yes, go" and "open
  yesterday's job". Deterministic code does all file work either way,
  and nothing copies before Transfer.

## Live test (2026-11-04, read-only on the mounted cards)

- `list_volumes()`: the destination SSD (536.6 GB free of 4000.7 GB),
  and two Sony camera cards (246.3 GB free of 256.5 GB; 432.5 GB free
  of 512.0 GB); boot, installer and hidden volumes skipped.
- `scan_card()`: card one — 4 clips, 10.135 GB, all Sony `ILME-FX3`;
  card two — 111 clips, 79.429 GB, 10× `ILME-FX3` + 101× `ILCE-7SM3`,
  recorded 2026-10-19 to 2026-11-03. Every clip's time came from
  ffprobe with its `M01.XML` sidecar paired.
- Two smallest clips (67,384,204 B and 67,386,559 B) plus sidecars
  planned into `/tmp/offload-test/` and copied for real: 4 files,
  134,775,032 B, all verified (independent BLAKE2b re-hash matches),
  `offload.json` written, source mtimes and sizes unchanged. The SSD
  was not written.

## First live run fixes (2026-11-04, `OFFLOAD-FIX-BRIEF.md`)

Built by Muse Code on `muse-wonder-1.0` from
`docs/muse-code/OFFLOAD-FIX-BRIEF.md`, after the owner's review of
the app on submission day. Muse Code token use: TBD (the owner fills
this from the run log).

- **Today's shoot, not the whole card history.** The first live run
  planned all 115 clips (89.56 GB) because cards hold weeks of shoots.
  `plan_offload` now plans only the most recent shoot day on the
  cards (the local date of each clip's recorded time) and names the
  folder by that day; `shoot_date` plans a named day and
  `include_all` the whole cards, honoured by the rule parser, the
  Spark intent (`date` / `include_all` in the schema) and the CLI
  (`--date` / `--all`). The plan carries what was left out
  (`older_count`, `older_summary`, per-card `cards_info` with a
  `skipped` flag), the plan card shows "Also on the cards: … (not
  copied)" with an **Include them** button that replans over the
  whole cards, a card with no clips from the day is listed as
  skipped, and Ghost's sentence names the day and camera: "Found
  today's shoot (Nov 3): 8 Sony clips, 22.0 GB on Untitled 1."
- **Readable Transfer button.** The plan card's `.acts button` rule
  set the muted grey (`rgb(138,147,168)`) over `.primary`'s white,
  leaving grey text on the orange background. Primary buttons in the
  plan card now keep their white label (`.ghost-plan .primary`,
  `.acts button.primary`); Change, Include them, Make the cut and
  Open job were checked the same way.
- **Fast answers.** `scan_card` caches per volume, keyed by the clip
  files' names, sizes and mtimes (`card_fingerprint`,
  `clear_scan_cache`): a repeat scan probes nothing, and after a
  change only new or changed clips are probed. The engine scans
  mounted cards in background threads when the Import tab opens (and
  when a card appears), reports live progress on
  `GET /api/offload/scans`, and the Import tab shows "Scanning
  Untitled 1 · 40/111" until each card is ready, so a request is
  answered from the warm cache. Nine new offline tests cover the
  latest-day default, "everything" and a named date, the left-out
  summary, the skipped card and the cache hit/miss.
- Live check (read-only, nothing copied): the owner's request now
  plans only Nov 3 — 8 Sony clips, 22.02 GB on Untitled 1 into
  `2026-11-03 Shoot`, with Untitled (4 clips, Oct 29) skipped and
  "Also on the cards: 107 older clips, Oct 19 – Oct 29 (not copied)".
  A cached replan in the same process took 0.05 s.

# Build log: gaze cuts inside a line (`angles` gazecut) (2026-11-04)

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `GAZECUT-BRIEF.md`: cut to the camera I'm facing, inside a line. Written after the owner watched the final take in the app and saw the cut open on tripod camera B while he was facing the drone C. |
| Build | Meta **Muse Code** on **`muse-wonder-1.0`** | Dense gaze sampling, held-turn gaze segments and word-gap splits, duration-weighted minimum-shot merges and one reason per shot in `src/director/gaze.py` + `src/director/angles.py`, the continuous landmark yaw in `src/director/lips_helper.swift` + `src/director/sync.py`, 13 new tests (11 in `tests/test_gaze.py`, 2 in `tests/test_sync.py`; the dense-sampling test updated). Built straight from the brief (no separate design). |
| Code review and live test | Claude Code, piloted by the owner | Real footage, below. |

Muse Code token use: TBD (the owner fills this from the run log).
Live test: TBD (the owner fills this from the real-footage run).

## Build notes (Muse Code)

- Sampling is dense now: about one frame every 0.5 s per covering
  camera across each line, at least the old 3 and capped at 16 per
  line (`dense_line_sample_times`); frame names are zero-padded so
  the helper's sorted order stays the sample order past nine samples.
  The sample times are recorded per line in `angles.json`
  (`sample_times_s`, with the held `segments` beside them), and the
  per-line pick still needs a majority of addressed samples.
- A turn inside a line becomes a cut: per sample, the addressed
  camera is the one camera with |yaw| ≤ 18°; samples with no single
  addressed camera inherit the previous address, and a run held for
  under 1.0 s is a glance that folds into its longer neighbour
  (`gaze_segments_for_line`), so a 0.4 s look away makes no cut. A
  surviving change of address splits the line on the word gap nearest
  the turn (`_gaze_units` in `angles.py`, noted `gaze_split:L<n>` on
  both shots). A side that would be folded away despite being short
  is kept when folding would show the wrong camera for more than
  1 s (`gaze_split_kept`); at or under a second it folds.
- Merges keep the gaze: a minimum-shot merge now takes the camera
  addressed for the most time in the merged span — each sample owns
  the interval halfway to its neighbours, summed over the span
  (`addressed_durations`) — not a per-line vote and not simply the
  surviving neighbour's camera. Adjacent pieces on one camera group
  first, so a short split side that continues the previous shot's
  camera is already part of that shot before the rule runs.
- Overrides win: a line pinned by an editor (`set_angle` /
  `angle_override`, method `editor`) never gaze-splits, and a
  neighbour carrying an override keeps its camera against the
  duration-weighted winner, exactly as the short-shot override rule
  already worked.
- One reason per shot: shots no longer concatenate their lines'
  reasons. A shot keeps the first reason for its camera (a split
  shot's own `speaker addresses camera <label>`), through same-camera
  grouping, minimum-shot merges and same-camera joins alike; the
  repeated four-copy reasons on the final take's opening shots are
  gone (see the job check below).
- Better yaw, and what was measured: Vision's yaw is quantised
  (0°, ±45°), so the helper now also prints a fifth token, a
  continuous yaw in degrees from the face landmarks — the nose tip
  (the lowest `nose` landmark) sideways from the midpoint of the two
  eye centroids, as `atan2(offset, 0.55 × eyeDistance)` clamped to
  ±75°, its sign aligned with the coarse yaw when that is non-zero;
  `NA` when the eye or nose landmarks are missing. The nose tip sits
  roughly 0.55 inter-eye distances in front of the eye plane, which
  is what the 0.55 encodes. The gaze rule uses this estimate when
  present and the coarse yaw otherwise (`effective_yaw`); both are
  recorded (`yaw_continuous_deg` / `yaw_coarse_deg`) when the
  continuous one exists. Measured here: the formula's Python mirror
  (`continuous_yaw_deg` in `sync.py`, tested in `test_sync.py`) gives
  0° for a centred nose tip and 45.0° for an offset of
  0.55 eye-distances, rising monotonically between; the helper still
  builds with `swiftc -O` as today (rebuilt through
  `build_lips_helper` and directly). It was **not** measured on real
  faces in this sandbox: Vision returns NOFACE here (the ANE model
  fails to load, as on the `look` build), so the 0.55 depth ratio is
  geometry, not a fit to the owner's footage — the live check below
  is the owner's agent's to run with the real helper.
- Job check, on copies in `/tmp/gazecut-job/` only (the JSON files
  copied out of the read-only job; nothing written into it, no
  `--render`): the planner ran `--no-muse` with stand-in gaze, since
  this sandbox can run neither Vision nor new ffmpeg samples. The
  stand-in samples are the job's existing three samples per line held
  over their thirds at the new dense times, except line 1, where the
  brief's frame reading (faces C for "my drone has no microphone",
  B for "but this software lined it up here") puts the samples on C
  before the microphone|but gap and B after it. First 30 s, output
  timeline. Before (the job's `angles.json`): B 0.00–5.73 (lines
  0–1, with the four-times-repeated reason), B 5.73–10.53, B
  10.53–11.34, C 11.34–15.22, B 15.22–26.64, C 26.64–29.33, C
  29.33–30.86. After: C 0.00–3.69 (lines 0–1: "My mic, my drone has
  no microphone"), B 3.69–5.73, B 5.73–10.53, B 10.53–12.80 (lines
  3–4), C 12.80–17.12 (lines 4–5), B 17.12–26.64, C 26.64–29.33, C
  29.33–30.86 — the opening now opens on the drone and cuts to B at
  the word gap after "microphone", and every reason appears once.
  The extra mid-line cuts after 10 s come from those held thirds of
  sparse samples, not from live dense sampling; with the real helper
  the owner's agent re-runs this check before trusting them.
- Full suite: 771 tests (13 new), green except the same 11
  pre-existing `ps`/socket-sandbox errors in
  `tests/test_engine_render.py` and `tests/test_render_runner.py`
  (758 on the tree before this build, same 11).

### Live check (owner's agent, Claude Code, 2026-11-04)

Run outside the sandbox with the real Vision helper on a copy of the job (`director angles`, Muse picks on, no
render): 29 lines → 22 shots, $0.073. The dense samples for line 1 ("my drone has no microphone but this software lined
it up here.", 8 samples per camera) give continuous yaw C ≈ 10–14° and B ≈ 19–37° for the first four samples, then the
reverse; the held-turn segments are C 160.62–162.51 and B 162.51–164.40, and the shot list opens C 0.00–3.69 ("My mic,
my drone has no microphone") then B — the cut the owner asked for. Every shot carries one reason. Full suite outside
the sandbox: 771 tests, OK.

## VOICE-OFFLINE (2026-11-04, Claude Code)

Built by Claude Code (Opus) after the Muse credit ran out: from the afternoon of submission day the Meta Model API
answered every call (Muse Spark, Muse Voice Transcribe) with HTTP 402 `billing_not_configured`. Voice notes ("hold
to talk" in the Edit tab, `director voice`) now keep working end to end without Muse. Muse stays the primary path;
nothing below runs while it answers.

- **Hearing.** `transcribe_wav` already fell through to local `mlx_whisper` on any Meta failure; a 402 comes back at
  once and the transcribe call has no retries. It now also says why, in the payload's `warnings`: "Muse unavailable
  (402), heard with local Whisper" (`muse_unavailable_reason` maps 402/billing, 401/403/429/5xx, connection errors,
  a missing key, the spend hard stop and a missing guestlist to a short reason).
- **Understanding.** When the one Spark call in `resolve_notes` fails for one of those reasons, or guestlist is not
  installed, `resolve_offline` reads the notes with rules (`offline_reply_notes`) that emit reply notes in exactly
  the `RESOLVE_SCHEMA` shape, so `_assemble_entries` → `verify_resolved` still do the real work (quoted-phrase
  ranges, "both/all/every", cut-edge and camera anchors, every cutlist check). Covered: "cut where I say …",
  "cut the part where I say …", "cut both places / every mention of …", "remove …", typed quotes (`cut "…"`),
  "at the very start / at the end use the drone / the Sony / camera B", "use camera B here" (playhead),
  "tighten the pauses", "always/never …" (preference), and a note that does several of these at once. Camera names
  come from the clip files (DJI_ → drone, C0001 → Sony, IMG_ → iPhone) as well as labels; the B in "B-roll" is never
  camera B. A misheard edge in a quote falls back to the longest run of its words that is in the cut. Anything
  else comes back unresolved with "I couldn't understand that note without Muse; try: cut where I say …" — never an
  exception. Each offline note says "Resolved offline by rules (Muse unavailable: 402)"; spend is $0. Any other Spark
  failure (a bad reply) still raises as before. `resolve_typed` runs a typed note through the same rules.
- **Frame check.** `confirm_visual_notes` skips its Spark vision call on a billing/network failure (or when the notes
  were resolved offline): the angle change is kept with a soft "Frame check skipped (Muse unavailable: 402)" check
  and one warning, instead of being downgraded to a note.
- **App.** `Engine._store_voice` returns the warnings, appends the "Muse unavailable" lines to the card's reply, and
  names the model "offline rules (Muse unavailable)" when the rules resolved the note; Ghost's voice request
  (`offload_voice`) carries the Whisper warning too. `app/ui` is unchanged.
- **Not covered offline:** "use the other take" (swap_take), "make it under three minutes", and the typed chat box in
  the Edit tab (`/api/muse`), which is a separate Spark call on the B-roll/card project and still needs Muse.
- Tests: `tests/test_voice_offline.py`, 27 new, offline (the guestlist stand-ins from `test_voice` raise the 402;
  Whisper is stubbed): every phrasing above on a fixture shaped like the hackathon job, the 402 fall-through in
  `transcribe_wav` (one Meta call, no retries) and `resolve_notes`, Muse still primary when it answers, other failures
  still raising, the skipped frame check, `run()` end to end with both calls returning 402, and the app's card text.

# Build log: ONBOARD — first-run tour, the Import page and Ghost's face (2026-11-04)

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `ONBOARD-BRIEF.md`: a first-run tutorial and a finished-looking Import page, after the owner's review of the app on submission day. |
| Build (partial) | Meta **Muse Code** on **`muse-wonder-1.0`** | Commit `d108cab`, untested: the settings file and `GET`/`POST /api/settings` (`settings_get` / `settings_post`, lazy, atomic write, `Engine(settings_path=…)` / `GHOSTCUT_SETTINGS`), the live briefing (`offload_briefing`, `GET /api/offload/briefing`) and the help branch at the top of `offload_ask`, `app/ui/ghost.css` (tokens, glass surfaces, colour wash, motion), the mascot (`app/ui/ghost/mascot.js` `window.GhostMark(size, mood)`, `app/ui/ghost/ghost.svg`) and the six-step tour (`app/ui/onboard.js` `window.GCTour.open(ctx)`). The run stopped with Meta API error 402 (credit exhausted) before `is_help`, the Import page, the app.js hooks, the scan enrichment, tests or docs. |
| Finish | **Claude Code** (Opus) | Kept Muse's files and fixed them in place; built the rest (below). |

Muse Code token use: TBD (the owner fills this from the run log).

## What Claude Code finished or fixed after the credit ran out

- `src/director/offload.py`: `GHOST_BRIEFING` (the workflow, what Ghost understands, what it never does), appended to
  `GHOST_DEVELOPER`; `is_help(text)`. `INTENT_SCHEMA` unchanged. Muse's help branch called `is_help` through
  `getattr`, so until this it silently never fired.
- `app/engine.py`: scan entries carry `camera_labels`, `date_start`, `date_end` and `sample` (file name only), copied
  through `_scan_public`; `offload_ask` now reports `planner` (`muse` or `rules`) and, when a key is set but Spark did not
  answer, `note: "Muse unavailable, using the built-in planner."` (`MUSE_UNAVAILABLE`). The old body of `offload_ask`
  moved unchanged into `_offload_ask_intent`.
- `app/ui/import.js` + `app/ui/import.css` (new): the Import view moved out of `app.js` and rebuilt —
  `window.GCImport.mount(el, ctx)`, two columns above 1100 px, pipeline strip, card tiles (SVG glyph by camera,
  clips, GB, mini scan bar, Ready chip, camera and date range, select to plan by click), destination tile with a
  free-space meter and a "to copy" segment (red when short), Ghost panel (mascot, live status, Help, starter chips),
  plan card (same `ghost-plan` / `.acts button.primary` classes, Include them, Transfer, Change), one persistent
  transfer progress element (percent, file, bytes, ETA, client-side throughput), shoot bins with skeletons, duration
  badges and `encodeURIComponent` thumb URLs, a Resume-a-job card, friendly cut-stage chips, `#edit` before the reload
  on done, the scan poll stopped while the view is hidden (and restarted when shown), empty states (insert a card,
  connect your SSD, scan error), resume of a finished or running transfer/cut on mount, and a Make-the-cut offer
  after the copy when the request asked for the cut (never started automatically). Tiles redraw only when the
  poll's data changes, so they keep focus and do not re-animate every 1.2 s.
- `app/ui/app.js` hooks (marked `ONBOARD`): `window.GC = Object.assign(window.GC || {}, {…})` after `api()`;
  `PAGES.import` → `window.GCImport.mount(el, window.GC)`; the start line opens on Import and starts the tour when
  `/api/settings` says first run (an explicit view hash wins; a failed settings call counts as first run),
  `#import/tour` forces it, `#import/notour` suppresses it.
- `app/ui/onboard.js` fixes: the `?` Help button (`#helpBtn`) is wired; step 5 pre-fills the chat input; the card
  step's auto-advance no longer jumps if the user already moved; the tour sits under `.modal` so Connections (opened
  from step 2) shows above it, and its key handler steps aside while a modal is open; the Muse line says honestly that
  Ghost falls back to the built-in planner. Tour styles live in `import.css` (Muse had left them unstyled).
- `tests/test_onboard.py` (new, 17 tests) and `GhostUiTests` now reads `app.js` + `import.js` (assertions unchanged).

## Muse unavailable

The Meta API returns 402 for this account on submission day, so nothing on this page depends on it. Ghost's request
reading tries Spark only when `MODEL_API_KEY` is set; any error (402, billing, network) returns to the rule parser,
which handles the demo phrasings, and the reply carries the note above, shown under Ghost's answer. Help, the briefing,
the tour, the plan, the transfer and the shoot bins never call Muse. A failed voice request that looks like a billing
error tells the editor to type instead. The Muse path stays primary whenever a working key exists.

## Build notes (Claude Code)

- Hook edits in shared files only: `app/ui/app.js` (three, above), `app/engine.py` (Muse's settings/briefing/help code,
  the scan keys, the `offload_ask` planner note), `src/director/offload.py`, `tests/test_offload.py`, `README.md`, this
  log. `index.html` was not edited (its lines were pre-applied in `5407a3a`).
- Not done (stretch): real card thumbnails, cheaper `offload_shoot`, recent-jobs list, per-card transfer breakdown,
  mascot moods reacting to events, Reset tour.
- Not verified: the page and tour were not opened in a browser or the Mac window (no servers on this machine during the
  build), so layout, glass/backdrop rendering and motion are unchecked; no live ElevenLabs or Meta call (the 402 path is
  tested with a mocked client raising); the background `director cut` itself was not run, and its own Muse stages'
  behaviour on a 402 is outside this brief.
- Full suite, run outside the Muse sandbox: 811 tests, OK, no errors (794 before this build + 17 new in
  `tests/test_onboard.py`; the 11 `ps`/socket errors the brief names only occur inside the sandbox). After a last
  one-line guard in `_scan_worker` (`date_range` errors never fail a ready scan) `test_offload`, `test_onboard` and
  `test_engine_render` were re-run: 70 tests, OK.

## Live test

Left for the owner's agent.
