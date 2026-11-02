# GhostCut Director — Meta Global AI Developer Hackathon plan

> **Historical Day-1 plan (2026-10-27).** Superseded by [APP-DESIGN.md](APP-DESIGN.md) and the README's hackathon
> section. The `ghostcut organize/cut/remaster` commands below were never added to the CLI: those steps live in
> `scripts/director_test_run.py`, and remastering was not built.

**Entry:** Startup track as Ghost AI Systems (confirmed on the Cerebral Valley platform
2026-10-30), theme **Build for Your World**. Submission due **2026-11-04 9:00 PM ET**.
**Replaces:** Guestlist (parked 2026-10-26; its `guestlist.meta` clients and spend ledger are reused).
**What predates the event (state it in the submission):**
- GhostCut itself: first commit 2026-02-11 (private history, not published), public v0.2.0 on 2026-07-27
  (`8472e7b`), last pre-event commit 2026-08-27 (`53004e6`). That covers ingest, proxies, silence analysis,
  the FCPXML/OTIO exporter and its data classes, and the recap engine. Director reuses the exporter and data classes;
  a 2026-10-27 commit fixed dissolves, NTSC rates and OTIO gaps in them.
- The demo footage was recorded before the event (2026-10-02 and 2026-10-13).

**Built during the event:** `guestlist.meta` (2026-10-26, public repo DanielSensual/guestlist), the take-log spike
and `scripts/director_test_run.py` (committed 2026-10-27/28), the fact-card step and the Mac app in `app/`
(2026-10-28/29).

## The pitch

**Dump a card. Get an organized project, the best takes cut together, studio-clean audio, and an editable
timeline — and see why every decision was made.**

A solo creator (me) films 20 minutes to get five posts: restarts, stumbles, looking off-camera, talking to the
editor, gear checks. Whisper-only GhostCut can find silence; it can't tell a keeper from a flub or hear that the
room hum ruins the take. Meta's models can watch and listen.

## What each Meta model does (judged: Use of Meta AI Tooling, 25%)

| Step | Meta model | Job | Non-Meta part kept |
|---|---|---|---|
| **Organize** | `muse-spark-1.3` (video + audio in, strict JSON out) | Segments a raw recording into keeper takes, alternates (grouped, best picked with a reason), flubs, off-camera pauses, asides to the editor, setup, dead air | — |
| **Who's talking** | `muse-voice-transcribe-1.0` (diarization) | Speaker turns for multi-person footage; labels interviewer vs subject. **Not built as a feature (as of 2026-10-29):** one ad-hoc call on 2026-10-27 checked a QC claim. | Whisper stays for **word** timestamps (Voice Transcribe is turn-level only) |
| **Cut** | Spark picks + order | Assemble picked takes, drop flubs/pauses inside them | GhostCut snaps every edge to measured silence between Whisper words; FCPXML/OTIO export |
| **Remaster audio** | **SAM Audio** (open weights, Dec 2025) | Isolate the voice by text prompt ("a man speaking") → clean target stem + residual (hum, room, music). **Not built (as of 2026-10-29).** The sample film's audio cleanup used ElevenLabs voice isolation (non-Meta). | ffmpeg loudness master (−16 LUFS / −1 dBTP), de-ess/EQ chain |
| **Click the speaker** (stretch) | **SAM 3.1** masks → **SAM Audio** visual prompt (`-tv` model) | Click a person in a two-person shot → hear only them. **Not built (as of 2026-10-29).** | — |
| **Reframe** (stretch) | **SAM 3.1** tracking | 16:9 → 9:16 crop that follows the speaker. **Not built (as of 2026-10-29).** | GhostCut render |
| **QA** | Spark (audio in) | Listens to before/after audio and to the cut's joins; flags clicks, clipped words, bad joins — labeled as model judgment, not proof | Mechanical checks (LUFS, clipping, duration, source timecodes) labeled as mechanical |

Rule kept from Guestlist: mechanical checks and model judgments are shown separately; a model agreeing with
itself is never presented as proof. `-contributor` model IDs are never used (they allow training on inputs).

## Demo (judged: Demo 25%) — one 2:30 film

1. **0:00–0:30** — A 22-minute raw talking-head card scrubbed fast: restarts, looking away, "cut that there",
   gear checks. "Five posts are in here. Finding them used to take me an hour."
2. **0:30–1:15** — `ghostcut organize`: the take log appears — groups of attempts, the pick and *why*, flubs marked.
   Accuracy vs my own hand log shown on screen (real numbers from the eval).
3. **1:15–2:00** — `ghostcut cut` + `remaster`: before/after audio on the same line (AC hum gone, voice forward),
   then the FCPXML opening in Final Cut with every cut editable.
4. **2:00–2:30** — stretch beat: two people talking, click one, hear only them.

## Day plan (today = Day 1, 2026-10-27)

| Day | Date | Build | Gate |
|---|---|---|---|
| 1 | Oct 27 | Spike: Spark take log on C2764 vs TAKES.md; SAM Audio access + cost route | Spark recall/precision measured; SAM Audio route known |
| 2 | Oct 28 | `ghostcut organize`: Spark take log → `organize/take_log.json`, bins, groups, picks, `review_queue.json`; snap take edges to Whisper silence; long-recording chunking if needed | Re-run eval: boundary error ≤ 1 s after snapping |
| 3 | Oct 29 | `ghostcut cut`: picked takes → timeline, flub removal inside takes, FCPXML/OTIO via existing exporters; per-take renders | Opens in FCP; joins audible-clean |
| 4 | Oct 30 | `ghostcut remaster`: SAM Audio voice stem (GPU route from Day 1) + loudness chain; fallback chain if SAM Audio is blocked | Before/after on C2764 takes; LUFS within ±0.5 |
| 5 | Oct 31 | Spark audio QA + mechanical checks; HTML report (take log, picks with reasons, before/after players) | Report renders from cache with $0 re-run |
| 6 | Nov 1 | **Held-out eval** on a second batch (Sep 14 talking heads, never used for tuning) — hand-check its takes | Numbers reported as they come out, good or bad |
| 7 | Nov 2 | Stretch: SAM 3.1 + SAM Audio "click the speaker"; reframe | Only if Days 1–6 are green |
| 8 | Nov 3 | Demo film shoot/edit, README, submission copy, public repo pass | Fresh-clone test passes |
| 9 | Nov 4 | Buffer; submit **before 9 PM ET** | Submitted |

## Budget

$150 Meta credit, ~$3.67 spent on Guestlist. Spark video measured at ≈272 input tokens/s → a 22-min recording ≈
360k tokens ≈ **$0.45 per full pass**. Whole plan (two batches, re-runs, QA) stays under ~$15. SAM Audio is not
on the Meta API: its cost is GPU time (see spike). Hard stop: `GUESTLIST_BUDGET_USD` (115) in the shared ledger.

## Risks

- **SAM Audio access** — gated on Hugging Face (Daniel must request access himself: the form asks for personal
  details and license acceptance) and wants a CUDA GPU. Without it the remaster step falls back to
  ffmpeg/Demucs and loses the strongest Meta audio story. Decide by end of Day 2.
- **Spark timestamps on long recordings** — measured in the Day-1 spike; chunk + overlap if drift is large.
- **Take-pick taste** — the "best take" is a judgment. Evaluate against Daniel's own picks; never claim more.
- **Prior work** — GhostCut predates the event. Disclose it plainly (see the honesty line above).

## Day-1 spike results (2026-10-27)

### Spark take log on C2764 (22:10 raw talking head, scored against the hand-checked TAKES.md)

Script: `scripts/director_spike_takes.py`; raw outputs `docs/spike-takes-day1.json` (times mode) and
`docs/spike-takes-day1-lines.json` (lines mode). Proxy: 360×640, 12 fps, mono AAC 64k, 1330 s.

| | A. Spark gives times | **B. Spark picks transcript lines, Whisper gives times** |
|---|---|---|
| Cost / wall clock | $0.149 / 51 s | **$0.171 / 70 s** |
| Input tokens | 95k (≈72 tok/s of video+audio, not the 272 measured on Guestlist's dance clip) | 101k |
| Duration-weighted recall of hand-picked take seconds | 0.76 | **0.85** |
| Duration-weighted precision (picked seconds that are real takes) | 0.74 | **0.94** |
| Removed spans wrongly kept | 60 s (off-camera look, stumble, 42 s of thumbnail stills) | **8 s** (line-boundary spill at two flubs) |
| Typical take-edge error | 6–70 s; drifts later through the recording | withdrawn until rescored (the scorer could match the wrong take; review ledger R09) |

All numbers are duration-weighted results on one 22-minute development recording; there is no held-out set
yet (review ledger R10).

**What A proved:** Spark understands the recording. Unprompted, it found the "No my bad, cut that one" restart, the
abandoned Spanish take, every aside to the editor, the thumbnail still-holds, the receiver troubleshooting and the
silent tail. **What it can't do is keep time** over 22 minutes: segments snap to round numbers and drift up to
70 s late in the middle. So Spark must not be the clock.

**What B proved:** with numbered Whisper lines beside the video, Spark judges delivery from picture + sound and
Whisper supplies the clock (per-take edge distances came from the same scorer as the withdrawn row above). It also caught things the hand index
handled implicitly: a trailing first Visto Azul attempt (grouped, second picked), a stuttered website opener
before the real take, and "cut and rethink here" inside T07.

**Misses in B (honest list):**
- T01 (Rode recap) was called an *aside to the editor* — it is addressed to the AI, and the hand edit kept it
  anyway. A judgment call; the reviewer UI must make reclassifying one click.
- T03 lost its second half (coverage 0.64): one Whisper line spans the off-camera look *and* the restart, so the
  line granularity is too coarse there. Fix: split lines at silences/words before giving them to Spark.
- T05a vs T05b: Spark picked **the first** BAD update ("cleanest delivery, second has a stuttered opening"). The
  hand index kept both and noted the second starts cleaner. **Daniel decides which is right** — this becomes
  the first entry of the taste eval.
- It kept 6 s of the abandoned Spanish attempt as a pick (the hand edit dropped all of it).
- T09's end runs 16 s long into the T10 stumble.

**Decision:** Director uses **mode B** — Spark for judgment, Whisper words for time, then GhostCut snaps every edge
to measured silence. Day 2 adds: lines split at long pauses (≤ ~6 s each), and the same eval re-run.
Total spike spend: **$0.32** (shared Guestlist ledger).

### SAM Audio — availability and cost route

- **Not on the Meta Model API.** `GET /v1/models` on our key (2026-10-27): muse-wonder-1.0, sam-3.1,
  muse-spark-1.1/1.2/1.3 (+ `-contributor`), muse-voice-transcribe-1.0, muse-image-1.0. No audio separation.
- **Open weights, gated.** `facebook/sam-audio-{small,base,large}` and `-tv` variants (better target correctness
  and visual prompting) on Hugging Face, under Meta's custom **SAM License** (not a standard OSS license; read it
  before shipping). Access needs a request form (name, date of birth, country, affiliation, job title) and
  license acceptance — the account holder has to do this.
- **Hardware:** the repo recommends a CUDA GPU; the model card's example falls back to CPU. No official VRAM or
  speed numbers. Third-party guidance: `base` ≈ `large` quality on speech at much less memory, fp16 recommended.
  This Mac (M1 Pro, 16 GB) can at best try `small`/`base` on CPU/MPS for short clips — not a demo path.
- **No hosted endpoint found** (searched fal.ai / Replicate). So the cost is GPU time on a host we choose
  (Modal, Baseten, a rented GPU). Modal CLI is not installed; the hackathon's Modal/Baseten credits go only to
  selected participants. **Cost is unmeasured until weights + a GPU exist.**
- Model-card speech scores (subjective, higher is better): small 3.99, **base 4.25**, large 4.03 → start with `base`.
- **Fallback if blocked by end of Day 2:** Demucs (Meta, MIT) vocal stem + ffmpeg cleanup chain. Works on this
  Mac, but it's a 2019-era Meta model, so the "Meta tooling" story for remastering gets weaker.
