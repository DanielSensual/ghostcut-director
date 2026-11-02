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
| Build | Meta **Muse Code** on **`muse-spark-1.3`** | `src/director/sync.py`, `tests/test_sync.py` (26 tests), the `sync` command in `src/director/cli.py`, the `AGENTS.md` layout line. TBD model calls; it ran the tests and fixed its own failures. |
| Code review and live test | Claude Code, piloted by the owner | TBD by the owner (design section 13 gate: run the built command on a short real clip and look at the output). |

Muse Code token use: TBD (the owner fills them from the run log).

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

# Build log: `director reframe` (2026-11-02)

Who did what, so the credit is exact.

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `REFRAME-BRIEF.md`: the feature, contracts, rules and limits. |
| Design | Meta **Muse Code** on **`muse-wonder-1.0`** | `REFRAME-DESIGN.md` sections 1–15: module layout, data classes, probe/proxy rules, shot detection and refinement, cost estimate and guard, SAM tracking, subject choice, gap filling and zero-phase smoothing, per-frame crop path, sendcmd render, payload, orchestrator and CLI, edge cases, test list. |
| Build | Meta **Muse Code** on **`muse-spark-1.3`** | `src/director/reframe.py`, `tests/test_reframe.py` (29 tests), the `reframe` command in `src/director/cli.py`, the `AGENTS.md` layout line. TBD model calls; it ran the tests and fixed its own failures. |
| Code review and live test | Claude Code, piloted by the owner | TBD by the owner (design section 14 gate: run the built command on a short real clip and look at the output). |

Muse Code token use: TBD (the owner fills them from the run log).

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
