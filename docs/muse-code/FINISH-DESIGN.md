# Design: connections and finishing (`director connect`, `finish`)

Designed by Muse Code on `muse-wonder-1.0` from
[FINISH-BRIEF.md](FINISH-BRIEF.md), under [AGENTS.md](../../AGENTS.md),
[OPEN-DESIGN.md](OPEN-DESIGN.md), [TIGHTEN-DESIGN.md](TIGHTEN-DESIGN.md),
[TRANSCRIBE-DESIGN.md](TRANSCRIBE-DESIGN.md) and
[VOICE-DESIGN.md](VOICE-DESIGN.md). `OPEN-DESIGN.md` is the contract for
`cut.py`, `export.py` and `cut.json`; `TIGHTEN-DESIGN.md` is the contract
for `cutlist.py`, `tighten.json` and the tightened `angles` render;
`VOICE-DESIGN.md` is the contract for the app's cut-job view, the taste
file (`<job>/taste.json`) and `src/director/voice.py`, which is being
built in another checkout and is assumed landed when this build starts.
This document is the complete design; the build (Muse Spark) writes the
code from it without further decisions. No code is written by this
document.

Conventions that bind the build: Python ≥3.11, `from __future__ import
annotations`, type annotations throughout, `@dataclass(slots=True)` at
module boundaries, `guestlist` imported only inside the functions that
call Meta models and guarded there, Meta key from `MODEL_API_KEY` only
(handled inside `guestlist`), no `-contributor` model ids, provider keys
from environment variables or the macOS Keychain only (§5), tests offline
with `unittest` and never touching ElevenLabs, Higgsfield, the Meta API
or Whisper, no media files or generated assets committed, no client
names, people's names or local footage paths in code, tests or docs.

## Build order: one build, after voice notes

**This feature is one Muse Spark build, run after the voice-notes build
([VOICE-DESIGN.md](VOICE-DESIGN.md)).** It is not split in two. The
reason it holds together as one build is that the two halves share
exactly one seam — `connect.get_provider_key` / `connect.test_provider`
(§4) — and the finishing half is fully useful with no key at all (§11.1:
clean, level and master are local ffmpeg). Splitting connections from
finishing would ship a key panel with nothing to spend a key on, then a
second build whose first act is re-proving the seam. Inside the one
build the order is fixed: (1) `connect.py` + the two provider modules
and their offline tests, (2) `director connect` and the app panel,
(3) `finish.py` local path (clean/master, no provider call), (4) Spark
sound design + ElevenLabs generation + ducked mix, (5) the `cut` stage
wiring and the `director finish` command. If the run must stop early,
it stops after (3): the editor still gets connections and a publishable
local master, and (4)–(5) land as a follow-up without changing any
contract in §5–§12. Higgsfield generation is not a deferred second
build; it is a non-goal of this design (§8, §19) because the verified
catalogue offers no finishing capability to wire (§1.6).

## Why this

`director cut` stops at picture. `angles` renders `cut.mp4` by joining
graded picture parts under the production audio exactly as recorded —
one uncut span, or one part per kept span in a tightened job
([angles.py](../../src/director/angles.py) `render_commands`). Nothing
cleans the voice, levels it, masters loudness, or adds sound. On the
brief's real shoot the lav recorded at −32 to −41 dB RMS (hand-held):
usable, but the rendered master is tens of decibels under publishable,
and the whole-cut QC loudness check (`cut.mechanical_qc` check 9) can
only warn about it — `cut` offers no re-master (§9.2 of OPEN-DESIGN:
"loudness is a publishing judgement … and `cut` offers no re-master to
fix it"). The owner finishes every job by hand in another tool.

Editors also already pay for generation services and want Director to
use their own keys. Today there is nowhere to put such a key: the only
secret in the repo's world is `MODEL_API_KEY` (env or `guestlist`'s
private file), provider HTTP would need a new home, and the Mac app —
which already tells the editor in its spend popover that "the optional
voice-isolation step (mix --isolate) sends that audio to ElevenLabs"
(`app/ui/app.js`) — has no panel to manage that key.

The feature is therefore two things behind one seam:

1. **Connections** — a provider registry (`src/director/connect.py`)
   with one module per service (`elevenlabs.py`, `higgsfield.py`), keys
   in the environment or the macOS Keychain (`keyring`, optional extra),
   `director connect` on the command line, and a Connections panel in
   the app. A service is data plus three functions (test, estimate,
   call); adding one never touches `finish.py`'s pipeline.
2. **Finish** — a `finish` stage in `director cut` after `angles` and
   before `export`/`reframe`, and a standalone `director finish`
   command. It cleans and levels the voice locally (or with ElevenLabs
   audio isolation when connected and asked), optionally asks Muse
   Spark where music and effects belong and generates them with the
   connected provider, mixes them ducked under the voice, masters to
   −14 LUFS / −1 dBTP (or −16), and remuxes the result as `cut.mp4`'s
   audio so `vertical.mp4` inherits it. Every generated file stays in
   the job folder with its prompt, provider, cost and licence note.

## 1. Inputs as they actually are

Verified against this repository and the installed packages. The build
must not re-derive it differently. Web verification (§1.5–§1.6) used
only the official ElevenLabs and Higgsfield documentation, as the
brief allows; every endpoint below names the page it was verified on.

### 1.1 The pipeline finish slots into (unchanged entry points)

```python
# src/director/angles.py
run(*, sync_path, words_path, out_path, audio_label, render_path,
    min_shot_s, model, no_muse, grade_path=None, tighten_path=None) -> dict
# writes angles.json; renders cut.mp4 shot-by-shot under the production
# audio (one span, or per-kept-span parts with 8 ms fades when tightened)

# src/director/reframe.py
run(*, video_path, out_path, render_path, shots_path, no_sam, max_usd) -> dict
# render_command maps "0:v:0" + "0:a?" with "-c:a copy": the vertical
# inherits whatever audio cut.mp4 carries, bit-for-bit. Finish therefore
# never touches reframe.py; finishing cut.mp4 first is the whole wiring.

# src/director/export.py
export_timelines(*, angles_path, sync_path, grade_path,
                 fcpxml_path, otio_path, project_name) -> dict
# pure mapping of angles shots onto ghostcut's exporters (§1.3)
```

Stage order in `cut.py` today is `STAGES = ("sync", "words", "tighten",
"grade", "angles", "export", "reframe", "qc")`. Finish inserts one
stage (§13): `sync → words → tighten → grade → angles → finish →
export → reframe → qc`. `finish` sits before `export` so the export
section can list the finish stems (§15), and before `reframe` so the
vertical is rendered from the finished master. `cli.main` already turns
`FileNotFoundError`, `ValueError`, `RuntimeError` and
`ghostcut.ffmpeg_tools.ToolingError` into `Error: …` on stderr with exit
code 1; every new error type in this design is a `RuntimeError`
subclass, so no CLI error-handling change is needed beyond the new
subcommands.

Payload facts finish relies on (and nothing more):

- `angles.json`: `{"version": 1, "audio": {"label", "clip_path",
  "offset_s"}, "shots": [{"id", "start_s", "end_s", "camera",
  "clip_in_s", "clip_out_s", "line_ids", …}], …}`. Shot times are
  output-timeline seconds. In a tightened job each shot also carries
  `source_start_s`, `source_end_s`, `kept_span_id`, `beat_id`.
  `cut.mp4` time 0 is the first shot's start: `angles.frame_grid`
  anchors the grid at `shots[0].start_s` and the audio span starts
  there too, so finish time `t` = timeline time − `shots[0].start_s`
  in an untightened job, and = shot `start_s` directly in a tightened
  job (whose output timeline already starts at 0). §11.2 defines the
  one conversion; nothing else converts by hand.
- `tighten.json` (when present): beats with `topic`, `pick_attempt`,
  attempts, `kept_spans`/`removed_spans` in source seconds, mapped with
  `cutlist.source_to_output` / `output_to_source` (the only mapping
  functions; finish imports `cutlist`, never re-derives the map).
- `words.json`: `{"words": [{"word", "start", "end", …}]}` in shared
  (source) timeline seconds, loaded with `angles.load_timed_words`.
  Finish maps each word through the same timeline conversion as shots
  to obtain finish-timeline word intervals (§11.2).
- `cut.json`: `{"version": 1, "status", "stages": {…}, "audio": {…},
  "spend_usd": <Meta total>, …}`. Stage spends sum to the top level;
  §12 keeps that true for Meta money and adds provider money as a
  separate total so existing assertions keep their meaning.

### 1.2 The Spark text call (the precedent finish follows)

Sound placement is text work — beats, shots, word intervals, measured
loudness — so finish adds exactly one Meta call shape, the text one the
repo already has twice: `app/muse.py:ask` and VOICE-DESIGN §6.1's
`voice_resolve`. The call is `guestlist.meta.cache` + 
`guestlist.meta.spark._structured`, never `video_json`:

```python
from guestlist.meta import cache, spark   # inside the calling function only
k = cache.key("finish", model, DEVELOPER, prompt, DESIGN_SCHEMA, "medium")
hit = cache.get("spark", k)               # hit -> $0, reported cached
cache.guard(0.25)                         # guestlist's own backstop too
r = spark._structured(k, [{"type": "input_text", "text": prompt}],
                      schema=DESIGN_SCHEMA, name="finish_design",
                      developer=DEVELOPER, model=model, effort="medium",
                      max_output_tokens=DESIGN_MAX_OUTPUT_TOKENS,
                      record={}, detail={"job": <job dir name>})
# r.data: dict, r.usd: float; spark.check_model refuses "-contributor"
```

No proxy of the cut is uploaded for a spoken-sound decision, and no
camera original ever is. The soundtrack facts Spark needs — where the
words are, where the gaps are, how loud the voice measures — are passed
as numbers (§11.4). A picture cannot tell Spark that a lav sits at
−38 dB RMS; `astats`/`loudnorm` measurements in the prompt can. Pricing
mirrors `guestlist.meta.pricing.TOKEN_RATES` without importing it
(spark $1.25 input / $4.25 output per 1M; wonder $4.00 / $20.00), the
OPEN-DESIGN §3 mirror discipline. The design call is estimated before
it runs and gated by the job's `--max-usd` (§13); a cached reply costs
$0 and says so.

### 1.3 The installed `ghostcut` exporters cannot carry finish audio

Read from the installed source (`ghostcut/export_fcpxml.py`,
`ghostcut/export_otio.py`, `ghostcut/models.py`); the build must state
this in the README rather than pretend otherwise:

- `MediaClip(id, source_path, proxy_path, audio_path, duration_s, fps,
  has_audio, width, height, layer)` and `TimelineSegment(section,
  clip_id, src_in_s, src_out_s, timeline_in_s)` are video-spine types.
  Neither exporter reads `proxy_path` or `audio_path`.
- FCPXML writes one `<spine>` of `asset-clip`s (video with its own
  camera audio, `audioRole` `dialogue` for `layer="a-roll"` else
  `effects`) plus B-roll as connected clips on `lane="1"`. There is no
  audio-only clip, no separate audio lane, and no stem concept.
- OTIO writes `Track.1` `V1` (`kind: "Video"`) and, only when
  `broll_overlays` is given, `V2` (also `kind: "Video"`). There is no
  audio track builder at all.

Consequence (decided, §15): the timelines keep referencing the camera
originals exactly as today. Finish does **not** modify `ghostcut` (the
brief forbids it) and does not fake audio lanes the formats would drop
on import. Instead `export.py` learns to list the finish stems in the
`cut.json` export section with the warning
`finish_audio_not_in_timelines:<file>,…`, and the editor re-lays
`finish/music_bed.mp3` and the effect stems under the timeline in the
NLE. The rendered `cut.mp4`/`vertical.mp4` remain the finished masters;
the timelines remain the picture edit. This is the same honesty rule
OPEN-DESIGN §8.3 already applies to the separate lav
(`audio_clip_not_in_timeline:<label>`).

### 1.4 The taste file (VOICE-DESIGN §7 is the contract)

`<job>/taste.json` is per-job, written by the voice build:

```json
{"version": 1, "entries": [
  {"id": "t1", "text": "this client hates jump cuts",
   "source": "voice", "note_id": "n4", "at": "2026-11-03T21:20:11"}]}
```

`tighten._load_taste` already reads it as a list of entry texts and
treats a missing file as `[]`. Finish reads the same file the same way
(§11.3): missing → no preferences, never an error; corrupt → empty with
the warning `finish_taste_unreadable` in `finish.json` (the voice
build's rule: preferences are never a reason to lose work). No new
taste store is invented, and finish never writes `taste.json` — the
app's Taste panel and `voice.add_taste` remain its only writers.
`cut.py` today passes no `taste_path` to `tighten.run` inside a cut
job; finish does not depend on that changing. It reads
`<out_dir>/taste.json` directly.

### 1.5 ElevenLabs (verified in the official documentation)

Base URL `https://api.elevenlabs.io` (US/EU/India/Singapore residency
hosts exist; Director always uses the default host — no residency flag
in this build). Auth on every request: header `xi-api-key: <key>`
([API Authentication](https://elevenlabs.io/docs/api-reference/authentication);
that page's own first example is `GET /v1/models` with this header).
Keys are secrets, scope-restrictable, and never sent in a URL. The
three capabilities finish uses:

| Capability | Endpoint (verified page) | Request | Response |
|---|---|---|---|
| Test (cheap, no generation) | `GET /v1/models` ([List models](https://elevenlabs.io/docs/api-reference/models/list)) | Header only | JSON list of models |
| Sound effects | `POST /v1/sound-generation` ([Create sound effect](https://elevenlabs.io/docs/api-reference/text-to-sound-effects/convert)) | JSON (§7.2) | Audio bytes in `output_format` |
| Music | `POST /v1/music` ([Compose music](https://elevenlabs.io/docs/api-reference/music/compose)) | JSON (§7.3) | Audio bytes; Director never uses `/v1/music/stream` (streaming adds a second code path for the same audio) |
| Voice isolation | `POST /v1/audio-isolation` ([Audio isolation](https://elevenlabs.io/docs/api-reference/audio-isolation/convert)) | `multipart/form-data` (§7.4) | Audio bytes (the reference response schema is an empty `{}` placeholder; Director treats the body as bytes and probes it — never parses it as JSON) |

Limits and models (same pages, plus [Sound effects](https://elevenlabs.io/docs/overview/capabilities/sound-effects) and [Eleven Music](https://elevenlabs.io/docs/overview/capabilities/music)):

- Sound effects: `duration_seconds` 0.5–30 (omit to let the model
  choose; Director always sets it), `loop` bool, `prompt_influence`
  0–1 (default 0.3). Models `eleven_text_to_sound_v2` (default on the
  wire, **deprecated, removed 2027-03-01**) and
  `eleven_text_to_sound_v3`. Director pins `eleven_text_to_sound_v3`
  explicitly (§3) so the deprecation cannot change behaviour under it.
- Music: `prompt` XOR `composition_plan` (Director uses `prompt`
  only), `music_length_ms` 3000–600000, `force_instrumental` bool.
  Models `music_v1` (wire default, **deprecated, removed
  2027-01-31**), `music_v2`, `music_v2_5`. Director pins `music_v2_5`
  with `force_instrumental: true` (a music bed with surprise vocals
  under speech is a defect, not a variation).
- Isolation: form field `audio` (file, required); optional
  `file_format` `pcm_s16le_16` (16 kHz mono PCM only) or `other`.
  Director sends a WAV as `other` (field omitted) — the 16 kHz
  fast path would force a resample the mix then has to undo.
- Output formats: Director requests `mp3_44100_128` for effects and
  `mp3_48000_192` for music (the v2-family default). WAV/PCM at
  44.1 kHz needs a Pro tier (Create sound effect page), so no WAV is
  ever requested; everything is decoded to 48 kHz WAV locally.

Pricing ( [API pricing](https://elevenlabs.io/pricing/api), "What are
the per-product API rates?": "API usage is billed in US dollars, not
credits"): Music **$0.15 per minute** of generated audio, Sound
Effects **$0.12 per minute**, Voice Isolator **$0.12 per minute** of
source audio. The same page's metering note ("Music and Sound Effects
are billed per generation") means the per-minute rate is applied to
the generated duration — Director's estimate is
`duration_s / 60 × rate` per asset (§7.5) and the recorded spend is
that list-price figure computed from the probed returned duration.
It is an estimate of the invoice, labelled as such in `finish.json`,
not a figure read back from the provider (these endpoints return
audio bytes, not a cost object).

### 1.6 Higgsfield (verified in the official documentation)

Higgsfield **does** have a key-based HTTP API — the brief's MCP-only
branch does not apply — but its catalogue offers no finishing
capability for a talking cut. Verified on:

- [Quickstart](https://docs.higgsfield.ai/docs/quickstart) and
  [Authentication](https://docs.higgsfield.ai/docs/authentication.md):
  credentials are a key ID + secret created in Higgsfield Console,
  sent server-side as `Authorization: Key <ID>:<SECRET>` (legacy
  `hf-api-key`/`hf-secret` headers exist; Director uses only the
  `Authorization` form). Env names in the docs' own examples are
  `HF_API_KEY_ID` / `HF_API_KEY_SECRET`. Never in URLs, logs or
  browser code.
- [How the API works](https://docs.higgsfield.ai/docs/how-to/introduction.md)
  and [Requests and lifecycle](https://docs.higgsfield.ai/docs/concepts/requests.md):
  asynchronous lifecycle — `POST https://api.higgsfield.ai/<model-path>`
  with JSON + `Idempotency-Key` → `{status: "queued", request_id,
  status_url, cancel_url}` → poll `status_url` to `completed` /
  `failed` / `nsfw` / `canceled` → download output URLs before
  `expires_at` (7-day retention, [Billing and retention](https://docs.higgsfield.ai/docs/concepts/billing-and-retention.md)).
- [Model API Reference](https://docs.higgsfield.ai/docs/models.md):
  "The catalog documents 84 entries: 17 Image and 67 Video."
  [Video Generation API](https://docs.higgsfield.ai/docs/models/video-generation.md)
  lists 22 video families (Genjutsu, Seedance, Kling, Wan, Cinema
  Studio, …); [Image Generation API](https://docs.higgsfield.ai/docs/models/image-generation.md)
  lists still-image families. There is no audio generation, no music,
  no voice isolation, no captions endpoint and no dedicated upscale
  endpoint in the catalogue. The nearest "effects" workflow,
  [Genjutsu](https://docs.higgsfield.ai/docs/models/genjutsu.md)
  (`POST /higgsfield/genjutsu/restyle/v1.0`, motion-transfer,
  object-swap), restyles or replaces the picture itself — running it
  on a graded multicam cut would discard the grade, the angle picks
  and lip-sync it exists to finish. It is not a finishing effect.
- [Billing and retention](https://docs.higgsfield.ai/docs/concepts/billing-and-retention.md):
  `POST /estimate/<model-path>` with the same JSON returns
  `{"usd": "0.094"}` (a string) without generating — the cheap test
  call §8 uses. Failed/`nsfw` requests are not charged.
  [Errors and retries](https://docs.higgsfield.ai/docs/concepts/errors.md):
  `401` invalid credentials, `403` insufficient balance, `429` queue
  full with `Retry-After`, `5xx` retryable. [File uploads](https://docs.higgsfield.ai/docs/concepts/file-uploads.md)
  (`POST /files/generate-upload-url` → presigned PUT → `public_url`)
  is documented but unused here, since no generation is wired.

Design consequence (decided): Higgsfield is a full registry citizen —
key storage, `connect` verbs and a real test call — with
`capabilities: []` and an explicit not-available path (§8). Finish
never calls it in this build. No endpoint for captions, upscale or
audio is guessed, and the README says why in one sentence (§19).
When Higgsfield ships a capability that fits, it becomes one new
provider function plus one `finish` call site, which is what the
registry is for.

### 1.7 ffmpeg audio filters (verified on this machine's ffmpeg 8.1)

Every filter below was checked with `ffmpeg -h filter=<name>` on the
repo's ffmpeg (8.1). No filter is used with an option this build does
not document:

- `highpass=f=80` — 3 dB point at 80 Hz; removes hand-held handling
  rumble without thinning speech.
- `afftdn` — FFT denoiser; `nr` (noise reduction, 0.01–97, default 12)
  and `nf` (noise floor, −80…−20, default −50). Finish uses the gentle
  end (`nr=8`) — the brief's lav is quiet, not noisy, and aggressive
  FFT reduction warbles under loudnorm gain.
- `acompressor` — threshold is linear (0.00098…1, default 0.125 ≈
  −18 dBFS), `ratio` 1–20, `attack`/`release` in ms, `makeup` linear
  gain. Finish levels with loudnorm, not makeup (§11.6).
- `sidechaincompress` — same options; input 0 is the bed, input 1 is
  the voice key. Values in §3 are set for 3–6 dB of gentle ducking
  under speech, not radio-style pumping (long release, moderate ratio).
- `loudnorm` — two-pass EBU R128: `I`, `TP`, `LRA` targets plus
  `measured_I`, `measured_LRA`, `measured_TP`, `measured_thresh`,
  `offset`, `linear`, `print_format=json`. `cut.loudnorm_command` /
  `cut.parse_loudnorm` already run the measuring pass for QC check 9;
  finish parses the full measured block (§4) and runs the applying
  pass QC never had.
- Placement/mix primitives: `adelay` (ms per channel), `atrim` +
  `asetpts`, `afade` (`t=in/out`, `st`, `d`), `volume` (dB), `amix`
  (`normalize=0` — Director sets gains itself), `amerge`/`pan` for
  mono-voice centring, `aresample=48000`, `alimiter` is deliberately
  **not** used as the peak control (loudnorm `TP` plus a verification
  measurement is; a limiter alone does not hold true peak after AAC).

All finish intermediates are 48 kHz WAV (`pcm_s16le`), the rate
`angles.AUDIO_SR` already standardises on, so no stage resamples
twice. Provider files arrive as MP3 and are decoded once.

## 2. Files changed

| File | Change |
|---|---|
| `src/director/connect.py` | New. Provider registry (data only), env/Keychain key storage, redaction, list/test/remove logic, `director connect` helpers. No HTTP generation code. |
| `src/director/elevenlabs.py` | New. ElevenLabs test + three generation calls over stdlib HTTP (§7). The only module that knows ElevenLabs URLs, models and rates. |
| `src/director/higgsfield.py` | New. Higgsfield key handling + estimate test call (§8). No generation functions — deliberately. |
| `src/director/finish.py` | New. Voice clean (local/isolation), Spark sound design, mechanical verification, generation orchestration, ducked mix, two-pass master, remux, `finish.json`, `run()` (§11). |
| `src/director/cut.py` | The `finish` stage (§13): `STAGES`, `JobOptions` finish fields, fingerprint/execute/reload, manifest `finish` + `provider_spend_usd`, provider gate. No existing stage's behaviour changes. |
| `src/director/cli.py` | New `command_connect(args)` (+ nested `connect` subparsers), `command_finish(args)` + `finish` subparser; `cut` gains the §13.3 flags; `_cut_stage_line` gains the `finish` line. |
| `src/director/export.py` | `export_timelines` gains optional `finish_path`; the export section lists finish stems with the §1.3 warning. Timeline bytes are unchanged. |
| `pyproject.toml` | New optional extra `connect = ["keyring>=24"]`. Nothing moves between extras; `director`/`muse` installs keep working without it. |
| `app/engine.py` | Connections endpoints (§10.1). Localhost/JSON behaviour inherited; legacy and cut-job modes share them (keys are per-machine, not per-job). |
| `app/ui/index.html` | The Connections button + dialog (§10.2), following the `renderDlg` modal idiom. |
| `app/ui/app.js` | Connections dialog logic (§10.2): list, save, test, remove; password fields cleared on close, never prefilled. |
| `app/ui/app.css` | Dialog rows, following the existing `.pop`/`.dlg` idioms. No layout change. |
| `tests/test_connect.py` | New. §18 items 1–14. |
| `tests/test_finish.py` | New. §18 items 15–38. |
| `README.md` | At build time: `director connect` + finishing section (§19), including the exporter limitation and provider-terms sentence. One "Who built what" row. |
| `AGENTS.md` | At build time: Layout bullets for `connect.py`, the two provider modules and `finish.py`; the `cut.py` bullet gains "finish" in the stage list. |
| `docs/muse-code/BUILD-LOG.md` | At build time: one FINISH section (§19). |

`src/director/angles.py`, `tighten.py`, `cutlist.py`, `transcribe.py`,
`sync.py`, `grade.py`, `reframe.py`, `src/director/voice.py` (voice
build) and `app/muse.py`, `app/cutjob.py`, `app/render*.py` are **not
modified**. `reframe` needs no change (§1.1); `angles` needs no change
(finish consumes its render); the voice build's `taste.json` writers
need no change (finish only reads).

Imports:

- `connect.py`: stdlib (`dataclasses`, `os`, `re`, `pathlib`) only.
  `keyring` is imported inside `keychain_available()` /
  `_keychain()` (§5.2), guarded, so the module imports on a machine
  with no Keychain backend. It never imports a provider module at top;
  `test_provider` imports the provider inside the function (§4), so
  registry data can be read without any HTTP code loading.
- `elevenlabs.py` / `higgsfield.py`: stdlib (`json`, `urllib.request`,
  `urllib.error`, `dataclasses`, `pathlib`) only, plus
  `ghostcut.ffmpeg_tools` (`probe_media`, `extract_media_stats`) where
  a returned duration must be probed. No `httpx`, no `elevenlabs`
  SDK, no new required dependency: tests stub the single transport
  seam (§4) and never need a vendor package. Each imports
  `from . import connect` for key lookup at call time (connect never
  imports them back at top, so there is no cycle).
- `finish.py`: stdlib plus `ghostcut.config` (`read_json`,
  `write_json`), `ghostcut.ffmpeg_tools` (`ToolingError`,
  `extract_media_stats`, `probe_media`, `run_command`),
  `from . import angles as angles_mod`, `from . import cutlist`,
  `from . import connect`, `from . import elevenlabs as elevenlabs_mod`.
  `guestlist` appears only inside `design_with_muse` (§11.4);
  `higgsfield` is never imported by `finish.py` in this build.

## 3. Constants

All decided here; the build does not tune them. Provider values live
in their provider module; pipeline values live in `finish.py` unless
stated.

| Constant | Value | Why |
|---|---|---|
| `CONNECT_SERVICE` (connect.py) | `"ghostcut-director"` | The Keychain service name for every provider account (§5.2). One service, provider-named accounts, so `connect list` can enumerate what Director owns. |
| `ELEVENLABS_BASE` (elevenlabs.py) | `"https://api.elevenlabs.io"` | Default host (§1.5). No residency flag in this build. |
| `SFX_MODEL` | `"eleven_text_to_sound_v3"` | Pinned past the `eleven_text_to_sound_v2` deprecation (removed 2027-03-01). |
| `MUSIC_MODEL` | `"music_v2_5"` | Pinned past the `music_v1` deprecation (removed 2027-01-31); v2-family default format applies. |
| `SFX_OUTPUT_FORMAT` | `"mp3_44100_128"` | Works on every paid plan; WAV 44.1 kHz needs Pro (§1.5). |
| `MUSIC_OUTPUT_FORMAT` | `"mp3_48000_192"` | The v2-family default on the Compose page. |
| `SFX_PROMPT_INFLUENCE` | `0.7` | Precise one-shots (riser/impact) follow the prompt closely; ambience is not a finish cue kind in this build (§11.4). |
| `MUSIC_USD_PER_MIN` | `0.15` | API pricing page (§1.5), mirrored so estimates need no network. |
| `SFX_USD_PER_MIN` | `0.12` | Same page. Applied to generated duration. |
| `ISOLATION_USD_PER_MIN` | `0.12` | Same page (Voice Isolator). Applied to source duration. |
| `HIGGSFIELD_BASE` (higgsfield.py) | `"https://api.higgsfield.ai"` | Docs base URL (§1.6). |
| `HIGGSFIELD_TEST_MODEL` | `"higgsfield-ai/soul/v2/standard"` | The Quickstart/Billing example model; used only for the no-generation estimate test. |
| `HTTP_TIMEOUT_S` (both providers) | `300` | Generation of a 10-minute bed is the slowest call; test calls time out at the same bound and report (§16), never hang the app. |
| `AUDIO_SR` (finish.py) | `48000` | Every intermediate; matches `angles.AUDIO_SR`. |
| `VOICE_HIGHPASS_HZ` | `80` | §1.7. |
| `VOICE_DENOISE_NR` / `VOICE_DENOISE_NF` | `8` / `-50` | Gentle `afftdn` (§1.7). Isolation replaces this filter, not the whole chain (§11.6). |
| `VOICE_COMP_THRESHOLD` / `_RATIO` / `_ATTACK_MS` / `_RELEASE_MS` | `0.125` / `3` / `10` / `250` | One gentle levelling pass before loudnorm; makeup stays 1 (loudnorm owns level). |
| `TARGET_LUFS_DEFAULT` | `-14.0` | Brief default; web/social target. |
| `TARGET_LUFS_ALT` | `-16.0` | The `--loudness -16` / taste value. Any value in −20…−10 is accepted by flag validation (§14); these two are the documented choices. |
| `TARGET_TP_DBTP` | `-1.0` | Brief true-peak ceiling. |
| `TARGET_LRA` | `11` | Speech loudness range for the applying pass; preserves delivery dynamics instead of squashing to broadcast LRA 7. |
| `MUSIC_GAIN_DB` | `-20.0` | Bed under voice before ducking. |
| `MUSIC_GAIN_LOW_DB` | `-26.0` | Taste "music low" (§11.3): 6 dB under the default bed. |
| `SFX_GAIN_MIN_DB` / `SFX_GAIN_MAX_DB` | `-24.0` / `-6.0` | Spark's per-cue gain is clamped into this window (§11.5). |
| `DUCK_THRESHOLD` / `DUCK_RATIO` / `DUCK_ATTACK_MS` / `DUCK_RELEASE_MS` | `0.10` / `4` / `5` / `400` | 3–6 dB gentle duck (§1.7); long release is the anti-pumping choice. |
| `MAX_SFX_PER_MINUTE` | `3` | Brief's "never more than N per minute" with N decided: 3. A fourth cue in any rolling 60 s window is dropped (§11.5). |
| `MAX_SFX_TOTAL` | `12` | A talking cut with more placed effects than this is a trailer, not a finish. Excess drops lowest-confidence first. |
| `EDGE_GUARD_S` | `1.0` | No effect starts in the first or last second unless taste/flag explicitly allows edges (§11.5). |
| `WORD_OVERLAP_CAP_DB` | `-12.0` | An effect overlapping speech is capped here even when ducked (§11.5). |
| `DESIGN_MAX_OUTPUT_TOKENS` | `6000` | `finish_design` reply budget and the output term of its estimate. |
| `DESIGN_INPUT_TOKENS_PER_S` | `0` | No video is sent; the estimate is prompt-chars/4 + output (§11.4), not a per-second video rate. |
| `SPARK_TOKEN_RATES_USD_PER_1M` | `{"muse-spark-1.3": (1.25, 4.25), "muse-wonder-1.0": (4.00, 20.00)}` | Mirrored list rates (OPEN-DESIGN §3 discipline); unlisted model falls back to spark rates with warning `finish_estimate_default_rates:<model>`. |
| `DEFAULT_MAX_PROVIDER_USD` | `2.00` | Per-job / per-command provider cap (§17). A 3-minute demo finish estimates under $1; $2 refuses accidents, not real jobs. |
| `LICENCE_NOTE` template (§11.7) | `"Generated with {provider_name} ({model}) on {date}; use is subject to {provider_name}'s terms for your plan."` | Stored per asset. Director asserts no rights conclusion of its own (§19 README sentence). |

## 4. Data classes and every public function

All `@dataclass(slots=True)`. Times are finish-timeline seconds
(§11.2) inside `finish.py`, provider durations are seconds, money is
USD (float). JSON stores times rounded to 3 decimals and money to 4,
matching every other stage. Keys never appear in a dataclass that is
serialised; `KeyStatus` carries booleans and source names only.

In `connect.py` — the registry. `PROVIDERS` is data; provider behaviour
is dispatched lazily so this module stays importable and testable
without HTTP:

```python
class ConnectError(RuntimeError):
    """A connection operation failed in a way the operator must see.
    The message always starts with 'connect: ' and never contains a key."""

@dataclass(slots=True)
class ProviderInfo:
    id: str                    # "elevenlabs" | "higgsfield"
    display_name: str          # "ElevenLabs" | "Higgsfield"
    env_vars: tuple[str, ...]  # in key-part order (§5.1)
    keychain_accounts: tuple[str, ...]  # parallel to env_vars
    capabilities: tuple[str, ...]       # elevenlabs: ("sfx", "music",
                                        # "isolation"); higgsfield: ()
    available: bool            # False = registry-only (§8)
    unavailable_reason: str    # "" when available

PROVIDERS: dict[str, ProviderInfo]  # insertion order = display order

@dataclass(slots=True)
class KeyStatus:
    provider: str
    connected: bool
    source: str                # "env" | "keychain" | "none"
    env_var: str               # first env var name (for messages); "" if none

@dataclass(slots=True)
class TestResult:
    provider: str
    ok: bool
    message: str               # editor-readable, key-free (§5.3)
    detail: str = ""           # e.g. "3 models visible" — never a key

def provider_info(provider: str) -> ProviderInfo: ...
def key_status(provider: str) -> KeyStatus: ...
def get_provider_key(provider: str) -> tuple[str, ...] | None: ...
def set_provider_key(provider: str, parts: tuple[str, ...]) -> KeyStatus: ...
def remove_provider_key(provider: str) -> KeyStatus: ...
def list_providers() -> list[dict]: ...
def test_provider(provider: str) -> TestResult: ...
def redact(text: str, secrets: tuple[str, ...]) -> str: ...
```

| Signature | Job |
|---|---|
| `provider_info(provider)` | `PROVIDERS[provider]`, else `ConnectError(f"connect: unknown provider '{provider}' (known: elevenlabs, higgsfield)")`. Pure. |
| `key_status(provider)` | Env first, then Keychain (§5.1). Never returns key material. Pure except `keychain_available()` probe. |
| `get_provider_key(provider)` | The key parts in `env_vars` order, or `None` when unconnected. The only function that returns secrets; callers must not log, store or serialise the result. |
| `set_provider_key(provider, parts)` | Validates part count/non-empty (§5.2), writes Keychain, returns status. Raises `ConnectError` with a §16 message when Keychain is unavailable. |
| `remove_provider_key(provider)` | Deletes Keychain entries (missing entries are success). Returns status; when source is `env` the status stays `connected` and the caller prints the §16 unset-it message. |
| `list_providers()` | One dict per provider for CLI/engine: `{"id", "name", "connected", "source", "capabilities", "available", "unavailable_reason"}`. No key, no mask, no fingerprint. |
| `test_provider(provider)` | Dispatches inside the function: `from . import elevenlabs` / `higgsfield`, fetches the key, calls its `test_connection`. No key → the §16 not-connected `TestResult` (`ok: False`, not an exception — listing and testing are reports, not operations). |
| `redact(text, secrets)` | Replaces every non-empty secret substring with `••••`. Every provider error path passes its message through this before raising (§5.3). Pure. |

In `elevenlabs.py` — one transport seam so tests fake HTTP without a
socket. `http_request` is the only function that touches the network;
tests replace it with `mock.patch.object(elevenlabs_mod, "http_request")`:

```python
class ElevenLabsError(RuntimeError):
    """An ElevenLabs call failed. Starts with 'elevenlabs: ', key-free."""

@dataclass(slots=True)
class GeneratedAudio:
    data: bytes                # MP3 bytes as returned
    model: str
    requested_s: float
    cost_usd: float            # list-price estimate from probed duration

def http_request(method: str, url: str, *, headers: dict,
                 body: bytes | None, timeout: float) -> tuple[int, dict, bytes]: ...
def test_connection(key: str, *, transport=http_request) -> TestResult-like dict: ...
def estimate_sfx_usd(duration_s: float) -> float: ...
def estimate_music_usd(duration_s: float) -> float: ...
def estimate_isolation_usd(duration_s: float) -> float: ...
def generate_sound_effect(*, key: str, prompt: str, duration_s: float,
                          transport=http_request) -> GeneratedAudio: ...
def generate_music(*, key: str, prompt: str, duration_s: float,
                   transport=http_request) -> GeneratedAudio: ...
def isolate_audio(*, key: str, wav_path: Path,
                  transport=http_request) -> GeneratedAudio: ...
def isolation_body(wav_bytes: bytes, filename: str) -> tuple[bytes, str]: ...
```

`test_connection` returns a plain dict `{"ok", "message", "detail"}`
(connect.py wraps it into `TestResult`) so the provider modules never
import connect's dataclasses at top — `connect` is imported lazily
only where a key must be fetched by finish (§11), keeping the import
graph acyclic: `finish → elevenlabs → (stdlib)`, `finish → connect`,
`connect → (lazy) elevenlabs/higgsfield`.

In `higgsfield.py`:

```python
class HiggsfieldError(RuntimeError):
    """A Higgsfield call failed. Starts with 'higgsfield: ', key-free."""

def http_request(...) -> tuple[int, dict, bytes]: ...  # same seam shape
def estimate_request(model_path: str, prompt: str) -> tuple[str, dict]: ...
def test_connection(key_id: str, key_secret: str,
                    *, transport=http_request) -> dict: ...
```

No `generate_*` function exists in `higgsfield.py`. Its absence is
the not-available path made structural (§8): there is no endpoint a
caller could guess at.

In `finish.py`:

```python
class FinishError(RuntimeError):
    """A finish step failed. The message always starts with 'finish: '."""

class DesignUnavailable(RuntimeError):
    """guestlist cannot be imported (the --no-muse degradation)."""

@dataclass(slots=True)
class TasteFinish:
    no_music: bool = False
    no_sound: bool = False     # no music and no effects
    forbid_kinds: frozenset = frozenset()  # subset of {"riser","whoosh","impact"}
    music_low: bool = False
    loudness: float | None = None          # taste loudness, e.g. -16.0
    allow_edges: bool = False
    isolate: bool = False      # taste asked for provider isolation

@dataclass(slots=True)
class MusicPlan:
    prompt: str
    start_s: float
    end_s: float
    gain_db: float
    dropouts: list[tuple[float, float]] = field(default_factory=list)
    reason: str = ""
    confidence: float = 0.0

@dataclass(slots=True)
class SoundCue:
    id: str                    # "sfx_001"… in placement order
    kind: str                  # "riser" | "whoosh" | "impact"
    at_s: float                # finish timeline
    duration_s: float
    prompt: str                # provider prompt (capped, §11.5)
    gain_db: float
    reason: str = ""
    confidence: float = 0.0

@dataclass(slots=True)
class FinishAsset:
    id: str                    # "music" | "sfx_001" | "voice_isolation"
    kind: str                  # "music" | "riser" | "whoosh" | "impact" | "voice_isolation"
    file: str                  # bare file name inside finish/ (§12)
    provider: str              # "elevenlabs" ("" never occurs in JSON)
    model: str
    prompt: str                # "" for isolation (no prompt; the input is the voice)
    duration_s: float          # probed returned duration
    cost_usd: float
    licence: str
```

| Signature (finish.py) | Job |
|---|---|
| `parse_taste(entries: list[str]) -> TasteFinish` | §11.3 phrase table. Pure. |
| `finish_origin(angles_payload: dict) -> float` | `shots[0].start_s` (0.0 with no shots). Pure. |
| `word_intervals(angles_payload, tighten_payload_or_none, words) -> list[tuple[float, float, str]]` | Finish-timeline word intervals (§11.2). Pure. |
| `beat_sections(angles_payload, tighten_payload_or_none, words) -> list[dict]` | `[{"id", "topic", "start_s", "end_s"}]` on the finish timeline (§11.4). Pure. |
| `extract_voice_command(video: Path, wav: Path) -> list[str]` | §11.6 step 1. Pure. |
| `clean_voice_command(src: Path, dst: Path, *, isolated: bool) -> list[str]` | §11.6 step 2. Pure. |
| `isolation_input_command(video: Path, wav: Path) -> list[str]` | The WAV sent for isolation (same extraction as step 1; separate name so tests pin both). Pure. |
| `loudnorm_measure_command(wav: Path, target_lufs: float) -> list[str]` | §11.6 step 6a. Pure. |
| `parse_loudnorm_full(stderr: str) -> dict | None` | Full measured block (`input_i`, `input_tp`, `input_lra`, `input_thresh`, `offset`); `None` on any failure. Pure. |
| `loudnorm_apply_command(src: Path, dst: Path, *, target_lufs: float, measured: dict) -> list[str]` | §11.6 step 6b. Pure. |
| `music_mix_command(bed: Path, duration_s: float, plan: MusicPlan, out: Path) -> list[str]` | §11.6 step 4 (loop/trim/fade/dropouts). Pure. |
| `sfx_mix_command(cues: list[tuple[SoundCue, Path]], duration_s: float, out: Path) -> list[str]` | §11.6 step 5. Pure. |
| `duck_mix_command(voice: Path, bed_mix: Path | None, sfx_mix: Path | None, out: Path) -> list[str]` | §11.6 step 5b (sidechain bus). Pure. |
| `mux_command(video: Path, master_wav: Path, out: Path) -> list[str]` | §11.6 step 7 (video copy + AAC). Pure. |
| `design_schema() -> dict` | §11.4 strict schema. Pure. |
| `design_prompt(...) -> str` | §11.4 user prompt. Pure. |
| `design_with_muse(...) -> tuple[MusicPlan | None, list[SoundCue], float, list[str]]` | The one Spark call + §11.5 verification. Raises `DesignUnavailable` only on `ImportError`. |
| `verify_design(music, cues, *, duration_s, words, sections, taste) -> tuple[MusicPlan | None, list[SoundCue], list[str]]` | §11.5 mechanical rules. Pure; notes are returned, never printed. |
| `estimate_finish_usd(music, cues, *, isolate_s: float) -> float` | Provider estimate for the gate (§17). Pure. |
| `run(*, job_dir: Path, model: str, no_muse: bool, sound: bool, isolate: bool, loudness: float | None, max_provider_usd: float, max_usd: float) -> dict` | Job-directory finish (§11–§12); returns the `finish.json` payload (also written). Standalone `--job` and `cut` call this one function. |
| `run_files(*, video_path: Path, angles_path: Path, tighten_path: Path | None, words_path: Path | None, taste_path: Path | None, out_path: Path, model: str, no_muse: bool, sound: bool, isolate: bool, loudness: float | None, max_provider_usd: float, max_usd: float) -> dict` | The explicit-file form; `run` resolves job files (`cut.mp4`, `angles.json`, optional `tighten.json`/`taste.json`, `words.json`) and delegates here, so there is one pipeline, not two. `out_path` is the `finish.json` path; the `finish/` work dir is its sibling. |

Internal helpers the build implements but no other module calls
(listed so the builder does not invent others): `_voice_stats`
(`astats`-free: RMS/peak from `volumedetect` stderr, for the Spark
prompt), `_download_asset` (write bytes, probe duration, build
`FinishAsset`), `_replace_master` (mux to temp then `os.replace`,
§11.6 step 7).

## 5. Key storage and the exact security rules

These rules bind every surface (`connect` CLI, engine, providers,
`finish`). A build that violates one fails review even if it works.

### 5.1 Resolution order and names

| Provider | Env vars (checked first, in order) | Keychain accounts (`CONNECT_SERVICE` + account) |
|---|---|---|
| `elevenlabs` | `ELEVENLABS_API_KEY` (1 part) | `elevenlabs` |
| `higgsfield` | `HF_API_KEY_ID`, `HF_API_KEY_SECRET` (2 parts, both required) | `higgsfield-id`, `higgsfield-secret` |

1. **Environment beats Keychain.** `key_status` checks the env vars
   first: all present and non-empty → `source: "env"`. A partially set
   Higgsfield pair (one var present) counts as **not connected**,
   never as half a key, and `list` reports the missing var by name.
2. **Keychain second.** All accounts present and non-empty →
   `source: "keychain"`; otherwise `source: "none"`.
3. Empty or whitespace-only values count as absent everywhere.
   Values are stripped once at read; they are never written anywhere
   in stripped or unstripped form except the Keychain itself.
4. `MODEL_API_KEY` is untouched by this feature. Provider keys never
   satisfy a Meta call and the Meta key never satisfies a provider
   call; `guestlist` keeps owning the Meta credential exactly as
   `AGENTS.md` already states.

### 5.2 `keyring` access (optional extra, guarded)

```toml
# pyproject.toml — new extra; nothing else moves
connect = ["keyring>=24"]
```

```python
def keychain_available() -> bool:
    try:
        import keyring
        return keyring.get_keyring() is not None
    except Exception:
        return False
```

- Every Keychain touch goes through `_keychain()` (same probe, then
  `keyring.get_password` / `set_password` / `delete_password` with
  `(CONNECT_SERVICE, account)`). `keyring.errors.PasswordDeleteError`
  on a missing entry is success (removing what is not there).
- Without the package or backend, `connect list` / `connect test`
  still work for env keys and report Keychain providers as
  `not connected`, and only `connect add` / Remove-of-keychain-key /
  the app's Save/Remove fail, with the §16 install message. The app
  must never crash the engine over a missing Keychain (§10).
- The app engine never imports `keyring` at module top;
  `connect.py`'s guarded import is the only one.

### 5.3 Never-leak rules (tested, §18 items 5–6)

1. **Never in the repo, a job file, `cut.json`/`finish.json`, a log
   line, a URL, or an error message.** Keys travel only in the
   `xi-api-key` / `Authorization` headers. Sound/music/isolation URLs
   carry no query-string key (verified shapes in §7–§8 need none).
2. **Never shown back.** No surface prints, returns or prefills any
   character of a saved key — not masked, not last-four, not a hash.
   `list` payloads carry `connected` + `source` + names only. The app
   password inputs are `type="password"`, `autocomplete="off"`, never
   prefilled, and cleared on dialog close (§10.2).
3. **Redaction on every error path.** Provider modules pass every
   outbound error string through `connect.redact(msg, key_parts)`
   before raising, because a provider may echo request material.
   CLI/engine code never interpolates a key into its own messages
   either; the §16 messages are the only connection strings the editor
   sees.
4. **Engine stays local.** Connections endpoints inherit
   `engine.py`'s existing guards unchanged: bound to `127.0.0.1`,
   `Host` must be local, JSON bodies only (≤ 64 KiB for these routes),
   `send_json` masking. Add-keys POST bodies are never logged —
   `Handler.log_message` already logs the request line only, and keys
   ride in the body, not the path. No CORS, no new port, no auth
   bypass: anyone who can already drive the local app can already
   render footage; the panel adds no remote surface.
5. **Tests use fake keys** (`"test-key-…"`) and a fake Keychain (§18).
   No real key appears in a test, fixture, doc example or commit.

## 6. The provider registry

```python
PROVIDERS = {
    "elevenlabs": ProviderInfo(
        id="elevenlabs", display_name="ElevenLabs",
        env_vars=("ELEVENLABS_API_KEY",),
        keychain_accounts=("elevenlabs",),
        capabilities=("sfx", "music", "isolation"),
        available=True, unavailable_reason=""),
    "higgsfield": ProviderInfo(
        id="higgsfield", display_name="Higgsfield",
        env_vars=("HF_API_KEY_ID", "HF_API_KEY_SECRET"),
        keychain_accounts=("higgsfield-id", "higgsfield-secret"),
        capabilities=(),
        available=False,
        unavailable_reason="no finishing capability yet (image/video "
                           "generation only) — see FINISH-DESIGN §1.6"),
}
```

Adding a service = one provider module + one `PROVIDERS` entry + one
`test_provider` dispatch arm. `finish.py` asks the registry two
questions only: *is a provider with capability X connected?*
(`key_status` + `capabilities`) and — via the provider module, never
via `connect` — *generate this*. It therefore compiles and runs
identically when Higgsfield later gains a capability: the diff is a
`capabilities` tuple change plus the new call site, not a pipeline
rewrite. Provider preference needs no flag in this build: ElevenLabs
is the only provider with `sfx`/`music`/`isolation`, so "the
connected provider" is unambiguous; the first future tie is broken by
`PROVIDERS` insertion order and a `--finish-provider` flag is added
only then (§19).

## 7. ElevenLabs provider (`elevenlabs.py`)

### 7.1 Transport seam and headers

`http_request(method, url, *, headers, body, timeout)` builds a
`urllib.request.Request`, always sets `Accept: application/json,
audio/mpeg` and the provider's auth header at the call site (never
inside minting a URL), sends `body` bytes, and returns
`(status, headers_dict, body_bytes)`. `urllib.error.HTTPError` is
caught and returned as `(status, headers, error_body)` so callers
branch on status; `URLError`/timeout raise `ElevenLabsError` with the
§16 network message after redaction. `timeout=HTTP_TIMEOUT_S` on every
call. The default is the only networking implementation; each public
function takes `transport=http_request` so tests pass a fake that
records `(method, url, headers, body)` and returns canned bytes —
this mirrors how `tests/test_transcribe.py` fakes `guestlist` and how
`tests/test_finish.py` will fake generation (§18).

Auth header on every call: `{"xi-api-key": key}`. JSON calls add
`{"Content-Type": "application/json"}`. Isolation sends
`multipart/form-data` with a boundary minted per call (§7.4).

### 7.2 Sound effects — `POST /v1/sound-generation`

Verified on [Create sound effect](https://elevenlabs.io/docs/api-reference/text-to-sound-effects/convert).

```
POST https://api.elevenlabs.io/v1/sound-generation?output_format=mp3_44100_128
xi-api-key: <key>
Content-Type: application/json

{"text": "<cue prompt>", "duration_seconds": 2.0, "loop": false,
 "prompt_influence": 0.7, "model_id": "eleven_text_to_sound_v3"}
```

- `text` is the cue's `prompt` after §11.5 capping (≤ 400 chars,
  never empty — an empty prompt is a §11.5 drop, not a call).
- `duration_seconds` is the cue duration clamped to 0.5–30 (§11.5);
  `loop` is always `false` (a looping effect under speech is a bed,
  and beds are the music call's job).
- A non-2xx status raises `ElevenLabsError` mapped by §7.6. On 2xx
  the body must be non-empty audio bytes; an empty body or a body
  that fails to probe (§11.7) is `elevenlabs: empty audio returned
  for '<cue id>'`, and only that cue is skipped.

### 7.3 Music — `POST /v1/music`

Verified on [Compose music](https://elevenlabs.io/docs/api-reference/music/compose).
Director uses the non-streaming endpoint only.

```
POST https://api.elevenlabs.io/v1/music?output_format=mp3_48000_192
xi-api-key: <key>
Content-Type: application/json

{"prompt": "<bed prompt>", "music_length_ms": 180000,
 "model_id": "music_v2_5", "force_instrumental": true}
```

- `music_length_ms = round(bed_length_s × 1000)` clamped to
  3000–600000. A bed longer than 600 s is generated at 600000 and
  looped by the mix (§11.6 step 4) — never by a second generation
  call for the tail (one bed, one cost line, one licence entry).
- `composition_plan` is never sent in this build: the bed's structure
  under speech is governed by gain and dropouts in the mix (§11.6),
  not by song sections, and `prompt` is the shape Spark can propose
  and an editor can read in `finish.json`.
- Same non-2xx/empty-body handling as §7.2 with cue id `"music"`.

### 7.4 Voice isolation — `POST /v1/audio-isolation`

Verified on [Audio isolation](https://elevenlabs.io/docs/api-reference/audio-isolation/convert).

```
POST https://api.elevenlabs.io/v1/audio-isolation
xi-api-key: <key>
Content-Type: multipart/form-data; boundary=<minted>

--<minted>
Content-Disposition: form-data; name="audio"; filename="voice.wav"
Content-Type: audio/wav

<WAV bytes>
--<minted>--
```

- `isolation_body(wav_bytes, filename)` is pure: it returns
  `(body_bytes, content_type_header)` with the boundary embedded, so
  tests pin the field name (`audio`), the filename and the absence of
  any `file_format` part (the `other` path, §1.5) without sending
  anything. The input is the mono 48 kHz WAV from
  `isolation_input_command` (§11.6); `requested_s`/`cost` use its
  probed duration (source minutes, §7.5).
- Duration/size guard (decided, no chunking in this build): a voice
  longer than 3600 s is not sent. Finish keeps the local clean and
  records `finish_isolate_skipped_long:<s>s` (§11.1). 48 kHz mono WAV
  keeps even that hour at ~350 MB, under the documented 500 MB /
  1-hour file limit.
- The response body is bytes (the reference response schema is an
  empty placeholder object, §1.5); it is saved as
  `finish/voice_isolated.mp3` and must probe as audio, else the §16
  isolation-failed fallback applies. It is never parsed as JSON. The
  endpoint takes no `model_id`; the asset's `model` is Director's
  literal label `"audio-isolation"` so the manifest names what ran
  without inventing a wire parameter.

### 7.5 Estimates (pure, list-price mirrors)

```python
def estimate_sfx_usd(duration_s):       return round(duration_s / 60 * 0.12, 4)
def estimate_music_usd(duration_s):     return round(duration_s / 60 * 0.15, 4)
def estimate_isolation_usd(duration_s): return round(duration_s / 60 * 0.12, 4)
```

Estimates gate before the call (§17); the recorded cost is the same
formula applied to the **probed returned duration** (generation does
not promise sample-exact lengths), written to the asset's `cost_usd`
with `finish.json`'s top-level `"provider_costs_are_estimates": true`.
No function reads a balance or an invoice — the endpoints return
audio, not billing data, and the design does not invent one.

### 7.6 Status mapping (ElevenLabs)

The provider modules translate statuses once; `finish` only sees
"asset" or "skip this cue with this note":

| Status | Behaviour |
|---|---|
| 2xx | Bytes → asset (§11.7). |
| 400 / 422 | Bad request (prompt/length rejected): skip that cue, note `finish_cue_skipped:<id>:elevenlabs rejected the request`. Never retry the same prompt. |
| 401 / 403 | Key rejected or lacking scope: stop provider work for this finish — one note `finish_provider_auth_failed:elevenlabs`, voice continues locally, remaining cues skipped without further calls. (Retrying every cue against a dead key would burn time and could burn quota.) |
| 402 / 429 | Quota/rate limit: same stop-provider shape, note `finish_provider_quota:elevenlabs`. |
| 5xx / network / timeout | One retry after 2 s for that cue only; second failure skips the cue with `finish_cue_skipped:<id>:elevenlabs unavailable`. Isolation is never retried (it is one long call; the local clean is already rendered, §11.6). |

## 8. Higgsfield provider (`higgsfield.py`) — registry-only by design

Key handling is identical to ElevenLabs' (§5.1: `HF_API_KEY_ID` +
`HF_API_KEY_SECRET`, Keychain accounts `higgsfield-id` /
`higgsfield-secret`, one `Authorization: Key <ID>:<SECRET>` header).
The only network function is the test:

```
POST https://api.higgsfield.ai/estimate/higgsfield-ai/soul/v2/standard
Authorization: Key <ID>:<SECRET>
Content-Type: application/json

{"prompt": "test"}
→ 200 {"usd": "0.094"}   (a string; parsed with float(), never assumed)
```

`estimate_request(model_path, prompt) -> (url, body_dict)` is pure so
tests pin the URL/body without HTTP. `test_connection` maps statuses
per [Errors and retries](https://docs.higgsfield.ai/docs/concepts/errors.md):
200 → ok ("key accepted"); 401 → the §16 check-the-key message; 403 →
insufficient-balance message (the key itself was accepted — the
message says so, because "fix your key" would send the editor to the
wrong screen); 404/503 for the test model → `ok: False` with the
model-unavailable message (the key may be fine; Director reports what
it knows, §16). No generation, polling, upload or webhook code exists
in this build, and `connect list` shows the provider with its
§6 `unavailable_reason` so the editor learns Higgsfield is connected
but unusable for finishing **before** a job, not during one.

## 9. The `director connect` command

```text
director connect                             # = connect list
director connect list [--json]
director connect add <provider> [--key KEY | --key-id ID --key-secret SECRET]
director connect test [<provider>]
director connect remove <provider>
```

Providers are `elevenlabs` / `higgsfield`. Structure: `build_parser`
adds `connect` with its own `add_subparsers(dest="connect_verb")`;
`command_connect(args)` dispatches on `args.connect_verb` (`None` →
list). It is the only command with nested subparsers; every other
command keeps the flat repo pattern.

Behaviour, decided exactly:

- **`list`** prints one line per provider, in registry order, then
  exits 0 (listing never fails the shell):
  `elevenlabs: connected (env ELEVENLABS_API_KEY) — sfx, music, isolation`,
  `elevenlabs: not connected — set ELEVENLABS_API_KEY or: director connect add elevenlabs`,
  `higgsfield: connected (keychain) — no finishing capability yet (image/video generation only)`.
  Never prints a key, a mask or a test result. `--json` prints
  `{"providers": [<list_providers() dicts>]}` instead.
- **`add`** prompts without echo when the key flags are absent:
  `getpass.getpass("ElevenLabs API key (input hidden): ")` (Higgsfield:
  two prompts, `Key ID` then `Key secret`). `--key` / `--key-id` +
  `--key-secret` exist for scripting; the command never echoes the
  flag value in errors or output (a traceback-free `ConnectError`
  path handles bad Keychain). After a successful Keychain save it
  immediately runs the cheap test (§7.1/§8) and prints its line, so
  "saved but dead" is visible in one interaction:
  `Saved ElevenLabs key to the macOS Keychain.` then
  `elevenlabs: connected — test ok (3 models visible)`. A failed test
  leaves the key saved (it may be a scope/balance state the editor
  will fix provider-side) and exits 1 with the §16 message. Adding
  while an env var shadows the Keychain prints first:
  `Note: ELEVENLABS_API_KEY is set, so it will be used instead of the saved key.`
- **`test [<provider>]`** runs the cheap call (ElevenLabs
  `GET /v1/models`; Higgsfield estimate, §8) — never a generation.
  With no argument it tests every connected provider and exits 1 if
  any tested provider fails. With an argument it tests that one (even
  unconnected → the §16 not-connected message, exit 1). Test costs
  $0: both calls are documented non-generating reads/estimates.
- **`remove`** deletes the Keychain entries and prints
  `Removed the saved ElevenLabs key.`; when the provider is connected
  via env it prints instead `ElevenLabs is connected through
  ELEVENLABS_API_KEY; unset it to disconnect.` and exits 0 (there was
  nothing Director owned to delete). Removing an unconnected provider
  prints `<Name>: not connected` and exits 0.

## 10. The Connections panel (Mac app)

Keys are per-machine, so the panel is global: it renders identically
in legacy mode and cut-job mode, and the engine endpoints do not take
a job or project. No finish controls are added to the app in this
build (§19): the app's player automatically plays the finished
`cut.mp4` once the stage has run (cut-job mode symlinks it), and
`finish.json` needs no app reader. The panel is the only app surface
this feature adds.

### 10.1 Engine endpoints (`app/engine.py`)

Four routes, added to the existing `do_GET`/`do_POST` dispatch
alongside `/api/taste`; all go through `send_json`/`_json_body` so the
§5.3 local-only rules hold. Each returns the **full providers list**
after the operation so the UI re-renders from one shape. No response
ever contains key material; the engine resolves `director.connect`
with the voice build's sys.path pattern (try the installed package,
then the repo's `src/`), and when the import fails every Connections
route returns `503 {"err": "Connections are unavailable in this app
build (director package not found)."}` while the rest of the app keeps
working — a missing optional feature never takes the editor down.

| Route | Request | Success response |
|---|---|---|
| `GET /api/connections` | — | `200 {"providers": [<list_providers() dicts>]}` |
| `POST /api/connections/test` | `{"provider": "elevenlabs"}` (`provider` optional → test all connected) | `200 {"providers": […], "tests": [{"provider", "ok", "message", "detail"}]}` (test failures are `ok: false` in the payload, still HTTP 200 — a dead key is a report, not a server error) |
| `POST /api/connections/add` | `{"provider": "elevenlabs", "key": "…"}` or `{"provider": "higgsfield", "key_id": "…", "key_secret": "…"}` | `200 {"providers": […]}` after a Keychain save (the engine runs **no** test here; the UI calls test next so a slow provider cannot hold the save open) |
| `POST /api/connections/remove` | `{"provider": "elevenlabs"}` | `200 {"providers": […]}` |

Request errors are `400 {"err": …}` with the §16 strings (unknown
provider, missing field, Keychain unavailable). Implementation is
four thin `Engine` methods (`connections_list`, `connections_test`,
`connections_add`, `connections_remove`) delegating to
`director.connect`; the Handler stays a router. Engine methods never
log request bodies (the existing handler never did; the build keeps
it that way) and never put a key in an exception message (§5.3.3).

### 10.2 UI behaviour (`index.html`, `app.js`, `app.css`)

Following the spend-popover and `renderDlg` idioms already in the app:

- A **Connections** text-button sits in the top bar next to the spend
  pill (`#connBtn`), labelled with state at a glance fetched once on
  load: `Connections · 1 connected` / `Connections · none connected`.
  It opens a modal (`#connDlg`, `<dialog>`-style overlay, backdrop
  click and ✕ both close) titled "Connections".
- The dialog lists one block per provider: display name; status line
  (`Connected — from ELEVENLABS_API_KEY` / `Connected — saved in
  Keychain` / `Not connected`); capability line (`Sound effects,
  music, voice isolation` or, for Higgsfield, its
  `unavailable_reason` verbatim); then the verbs. ElevenLabs shows one
  password input (`Paste ElevenLabs API key`); Higgsfield shows two
  (`Key ID`, `Key secret`). Buttons: **Save**, **Test**, **Remove**
  (Remove disabled while `source` is `env`, with the unset-it hint as
  its `title` and status line).
- **Save** POSTs add, clears that provider's inputs immediately
  (before the response returns they are already empty), re-renders,
  then auto-runs Test for that provider and shows the result line
  (`Connected — test ok` / the §16 failure in red). **Test** POSTs
  test and shows the result line without touching stored keys.
  **Remove** POSTs remove and re-renders. A result message area per
  provider holds exactly the engine/`connect` message — the UI
  invents no connection strings of its own beyond labels.
- Password inputs are never prefilled (including for a stored key),
  closing the dialog clears every input, and `Esc`/backdrop close
  behaves like every other dialog. The global `esc()` helper covers
  all interpolated provider text. While a test is in flight its
  button reads `Testing…` and disables (the same busy handling as the
  Muse Send button), because a 300 s-timeout call must not stack.
- No provider key, prompt or cost is rendered from `finish.json` in
  this build: the Deliver view is unchanged. (If a later build shows
  finish assets in the app, §5.3 still forbids key display; prompts
  and licence notes are editor content and may be shown.)

## 11. Finish

One pipeline runs from two entry points (`run` for a job directory,
`run_files` for explicit paths; `run` resolves the files and delegates,
§4). Steps, in order. Nothing in this section mutates `cut.mp4` until
step 7's atomic replace: a finish that fails early leaves the angles
render exactly as it was, and only step 7's replace failure can leave
the job unfinished (§16).

### 11.1 What runs with which keys (degradation contract)

| Situation | Voice clean | Sound design | Outcome |
|---|---|---|---|
| No provider connected (default) | Local (§11.6 step 2, `isolated: false`) | Off — Spark not called for sound (§11.4 gate), no warning beyond one summary note `finish_sound_off_no_provider` **only when sound was requested** (§16) | Job completes; publishable master; the demo path. |
| ElevenLabs connected, neither `--finish-sound` nor `--finish-isolate` (nor taste asking) | Local | Off | Job completes; $0 provider spend. Connecting a key spends nothing by itself. |
| Connected + isolate asked, isolation fails/over-long | Local fallback (already rendered) | Per its own switch | Job completes with `finish_isolate_fallback:local (…)`. |
| Connected + sound asked, one cue fails | — | That cue skipped (§7.6); bed + survivors still mixed | Job completes with `finish_cue_skipped:…` notes. |
| Connected + sound asked, key dead (401/403/quota) | Local (or isolated if it succeeded before the failure) | Provider work stops (§7.6); Spark plan retained in `finish.json` for the editor to see | Job completes with `finish_provider_auth_failed:elevenlabs` / `finish_provider_quota:elevenlabs`. |
| `--no-muse` or guestlist missing, sound asked | Normal | Spark unavailable → **no generation at all** (Director never invents placements), note `finish_design_unavailable:…` | Job completes; clean + master only. |
| Higgsfield only connected | Local | Off (no capability, §8) with `finish_sound_off_no_provider` when requested | Job completes; Higgsfield is never called by finish. |

Sound design is therefore **off unless asked or set in taste** on
every surface: `cut` runs it only with `--finish-sound` or a taste
entry that asks (§11.3); standalone `finish` only with `--sound`.
Isolation is likewise only with `--finish-isolate` / `--isolate` or
taste. Clean + master always run (unless `--no-finish` skips the whole
stage, §13.3).

### 11.2 The finish timeline (the one conversion)

All finish times — Spark prompt, cues, `adelay`s, `finish.json` — are
seconds into `cut.mp4` (`0` = its first frame). Derived once:

```python
origin = finish_origin(angles_payload)   # shots[0].start_s, else 0.0

def _to_finish(t_output: float) -> float:
    return t_output - origin             # output-timeline → finish time
```

- **Shots/sections** already live on the output timeline (tightened
  shots carry output `start_s`/`end_s`), so they shift by `−origin`.
  Untouched jobs have `origin == shots[0].start_s`; tightened jobs
  have `origin == 0.0` (output starts at 0). No branch on "tightened"
  is needed anywhere — the shift is the branch.
- **Words** live on the shared (source) timeline. With a
  `tighten.json`, each word is mapped with
  `cutlist.source_to_output(tighten_payload, t)` (words inside
  removed spans map to `None` and drop out — speech the audience
  never hears cannot be ducked under); without one, source time is
  output time. Either way the result shifts by `−origin`. Words
  landing outside `[0, duration]` drop. This reuses `angles` word
  loading (`load_words`) and `cutlist` mapping; finish contains no
  second time-map implementation (TIGHTEN-DESIGN §6.1's rule).
- **Beats** (tightened only): a beat's finish span is the union of its
  kept spans mapped through `source_to_output`, expressed as
  `[min, max]`. Untightened jobs get one section
  `{"id": "cut", "topic": "Cut", …}` spanning the whole cut, exactly
  as QC's un-beated sections already degrade.

Duration is the probed `cut.mp4` duration (`probe_cut`-style via
`ghostcut.ffmpeg_tools.probe_media`), never the angles arithmetic —
the mux must fit the file that exists.

### 11.3 Taste rules for sound (`parse_taste`, decided phrase table)

`parse_taste` lowercases each entry's text and applies this table
(first match per rule wins; unmatched entries are still passed
verbatim to the Spark prompt in §11.4, where Spark may honour them
but mechanical verification never pretends it enforced prose):

| Entry contains (case-insensitive) | Effect |
|---|---|
| `no music` / `no sound` / `no effects` / `no sound design` | `no_music` (+ `no_sound` for the last three): Spark is told, verification drops the bed (and all cues for `no_sound`). Stronger than `--finish-sound`: a job whose taste forbids sound never generates, even when the flag is passed — the editor removes the taste entry to override, so the preference cannot be silently out-flagged. |
| `no whoosh` / `no whooshes` | `forbid_kinds += {"whoosh"}` |
| `no riser` / `no risers` | `forbid_kinds += {"riser"}` |
| `no impact` / `no impacts` / `no hits` | `forbid_kinds += {"impact"}` |
| `music low` / `quiet music` / `music quiet` | `music_low: true` → bed gain `MUSIC_GAIN_LOW_DB`. |
| `no ducking` | **Ignored by design**, with note `finish_taste_ignored_ducking` if it reaches verification: an unducked bed under speech buries the voice (the failure this stage exists to prevent). The taste file governs taste, not audibility. |
| `master to -16` / `master to −16` / `loudness -16` (also `-14` forms) | `loudness: -16.0` (or `-14.0`). Precedence: explicit `--loudness` flag > taste > `TARGET_LUFS_DEFAULT`. |
| `isolate` + `voice` (both in one entry) / `clean voice with elevenlabs` | `isolate: true` (only honoured when ElevenLabs is connected; otherwise the §11.1 note). |
| `music` (any other entry mentioning it) | Asks for sound: sound design is enabled for this job even without `--finish-sound` (the brief's "or set in the taste file"). |
| `edge` + (`allow` or `first second` exception phrasing is **not** inferred) | Only the explicit phrase `allow edge effects` sets `allow_edges: true`. Vague prose never disables the edge guard. |

Taste can forbid and lower; it can never raise a gain, lift the
density cap, or disable ducking. Those are §3 constants and flags,
not preferences.

### 11.4 Spark sound design (one text call)

Gate: sound design runs only when (sound was asked via flag/taste)
**and** `--no-muse` is off **and** a provider with `music`+`sfx`
capability is connected. Failing any gate skips straight to the local
pipeline with the §11.1 note — Spark is never called to design sounds
nobody can generate, and generation is never attempted from an
undesigned plan. Spark itself failing follows the repo's degradation
pattern (angles/QC): `DesignUnavailable` or any Meta error → note
`finish_design_unavailable:<cause>`, local master only, job stands.

Developer prompt (verbatim in `finish.py:DEVELOPER`, name
`finish_design`):

```text
You place sound under a finished talking-head cut. You do not edit picture and you never invent speech. Return only JSON matching the schema; every other byte is discarded.
Propose at most one instrumental music bed and a few sound effects: a riser into a new section, a soft whoosh at a visual break, an impact under a cold open. Prefer silence to decoration. Speech is sacred: music lives under it ducked, effects sit in gaps or land on boundaries, and nothing musical may have vocals.
Times are finish seconds (0 = first frame of the rendered cut). Music start_s/end_s bound the bed; dropouts are intervals where the bed must be fully silent (for example under an intimate or crucial line). Effects carry at_s (start), duration_s, a generation prompt for a sound-effects model, gain_db (quieter is more professional), a reason an editor can judge, and confidence 0..1.
```

User prompt (`design_prompt(...)`, pure): cut duration and measured
voice stats (`_voice_stats`: RMS/peak dB of the extracted voice); the
taste entries verbatim (or `(none)`); sections with finish times and
topics; shots as `shot s3 12.400-18.200 cam-b`; word-gap summary (every
inter-word gap ≥ 0.30 s with its finish interval, capped at the 80
largest); the transcript as `words:` lines (escaped text, finish
times); the forbids resolved from taste (`forbidden_kinds: […]`, or
`none`); and the closing line

```text
Design the sound for this cut. JSON only.
```

No frames, no proxy, no file upload (§1.2). Estimate:
`(len(prompt + DEVELOPER) / 4 + DESIGN_MAX_OUTPUT_TOKENS)` tokens at
`SPARK_TOKEN_RATES_USD_PER_1M[model]`; the estimate gates via
`cache.guard(0.25)` and the caller's remaining `--max-usd` (§13/§17)
before any network use.

Strict schema (`design_schema()`; `strict: true`,
`additionalProperties: false` throughout, every property required —
the guestlist structured-outputs contract):

```json
{"type": "object", "additionalProperties": false,
 "properties": {
  "music": {"type": ["object", "null"], "additionalProperties": false,
   "properties": {
    "prompt": {"type": "string"},
    "start_s": {"type": "number"}, "end_s": {"type": "number"},
    "gain_db": {"type": "number"},
    "dropouts": {"type": "array", "items": {"type": "object",
      "additionalProperties": false,
      "properties": {"start_s": {"type": "number"},
                     "end_s": {"type": "number"}},
      "required": ["start_s", "end_s"]}},
    "reason": {"type": "string"}, "confidence": {"type": "number"}},
   "required": ["prompt", "start_s", "end_s", "gain_db", "dropouts",
                "reason", "confidence"]},
  "effects": {"type": "array", "items": {"type": "object",
    "additionalProperties": false,
    "properties": {
     "kind": {"type": "string", "enum": ["riser", "whoosh", "impact"]},
     "at_s": {"type": "number"}, "duration_s": {"type": "number"},
     "prompt": {"type": "string"}, "gain_db": {"type": "number"},
     "reason": {"type": "string"}, "confidence": {"type": "number"}},
    "required": ["kind", "at_s", "duration_s", "prompt", "gain_db",
                 "reason", "confidence"]}}},
 "required": ["music", "effects"]}
```

Generation prompts are written for the provider by Spark inside this
schema (`music.prompt` example: `"warm minimal piano and soft pads,
slow, hopeful, instrumental, no vocals, seamless underscore"`;
effect prompt example: `"short soft whoosh, airy sweep, subtle"`).
Director never templates provider prompts from topics — Spark speaks
to the sound model, Director speaks about timing.

### 11.5 Mechanical verification (overrules Spark, in this order)

`verify_design` runs before any provider call and returns notes
(`list[str]`, later visible in `finish.json`); drops are the default,
moves need a note. Spark's `reason`/`confidence` survive verbatim
onto surviving cues for the editor to judge.

1. **Schema-hygiene drops.** Music with `end_s ≤ start_s`, non-finite
   times, empty/whitespace prompt, or `confidence` outside 0..1 →
   bed dropped (`finish_music_dropped:invalid_plan`). Same per effect
   (`finish_cue_dropped:<n>:invalid_cue`, numbered by Spark order,
   before ids are assigned).
2. **Taste forbids.** `no_sound` → bed + all effects dropped
   (`finish_sound_forbidden_by_taste`). `no_music` → bed dropped
   (`finish_music_forbidden_by_taste`). `kind ∈ forbid_kinds` → cue
   dropped (`finish_cue_dropped:<kind>:<at_s>s:forbidden_by_taste`).
3. **Clamp.** Music span clamped to `[0, duration]`; dropouts clamped,
   inverted dropouts discarded, overlapping dropouts merged. Effect
   `duration_s` clamped to 0.5–30 (the provider window), `gain_db`
   clamped to `[SFX_GAIN_MIN_DB, SFX_GAIN_MAX_DB]` (music gain fixed
   at `MUSIC_GAIN_DB`/`MUSIC_GAIN_LOW_DB` — Spark's music `gain_db`
   is recorded but not honoured, because bed level is a loudness
   decision measured in §11.6, not a model guess). `prompt` truncated
   to 400 chars (word-boundary cut, never mid-URL — prompts are not
   URLs, and a prompt that becomes empty drops the cue).
4. **Edge guard.** Effect with `at_s < EDGE_GUARD_S` or
   `at_s > duration − EDGE_GUARD_S` drops
   (`finish_cue_dropped:<kind>:<at_s>s:edge`) unless
   `taste.allow_edges`. Brief: "never in the first or last second
   unless asked" — asked means the explicit §11.3 phrase, not a
   flag default.
5. **Word placement.** An effect *starting* strictly inside a word
   interval is moved to the nearest gap edge (previous word end +
   0.05 s, or next word start − duration) when that move is ≤ 0.75 s
   (`finish_cue_moved:<kind>:<from>s-><to>s:word`, using pre-id
   kind+time naming since ids are assigned in step 7), else dropped
   (`finish_cue_dropped:<kind>:<at_s>s:word`). An effect merely
   *overlapping* speech (start in a gap, tail over words) is kept
   only because the mix ducks the whole bed (§11.6 step 5b); its gain
   is capped at `WORD_OVERLAP_CAP_DB` with note
   `finish_cue_capped:<kind>:<at_s>s:-12.0dB:words`. An effect fully
   inside a ≥ 0.30 s gap needs neither move nor cap. ("Never under a
   spoken word unless ducked" is thus enforced as: starts never sit
   on words, tails sit on words only ducked and capped.)
6. **Density.** Sorted by `at_s`: any rolling 60 s window holding
   more than `MAX_SFX_PER_MINUTE` keeps the earliest three, drops the
   rest (`finish_cue_dropped:<kind>:<at_s>s:density`). Then
   `MAX_SFX_TOTAL`: excess drops lowest-confidence first (ties: latest
   `at_s` first), note `finish_cue_dropped:<kind>:<at_s>s:total_cap`.
7. **Ids.** Survivors, in `at_s` order, become `sfx_001`, `sfx_002`, ….
   (Voice-design rule, reused: ids are assigned only after filtering,
   so a dropped cue never leaves a hole in the numbering that lands
   on disk.)

### 11.6 The ffmpeg pipeline (stepwise commands, each pure-built)

Working directory is the job directory; intermediates live in
`finish/` (§12). `SR` is `AUDIO_SR`, voice is mono until step 5b
centres it in stereo. Each command runs through
`ghostcut.ffmpeg_tools.run_command` (same runner every stage uses);
any `ToolingError` aborts finish **before step 7**, leaving `cut.mp4`
untouched and the stage failed (§13.3) — a half-finished master is
never presented as finished.

**Step 1 — extract the voice.** The only read of `cut.mp4`'s audio:

```python
def extract_voice_command(video, wav):
    return ["ffmpeg", "-v", "error", "-y", "-i", str(video),
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(AUDIO_SR),
            "-c:a", "pcm_s16le", str(wav)]
    # -> finish/voice_raw.wav
```

No audio stream → `ToolingError` → `FinishError("finish: cut.mp4 has
no audio stream to finish")` (§16). Stripping any baked-in
clip/guide audio is not a finish concern: `cut.mp4` carries exactly
the production-audio mix `angles` rendered, nothing else.

**Step 2 — clean (local) or isolate-then-clean.** Exactly one of:

```python
def clean_voice_command(src, dst, *, isolated):
    chain = (f"highpass=f={VOICE_HIGHPASS_HZ},"
             f"acompressor=threshold={VOICE_COMP_THRESHOLD}:"
             f"ratio={VOICE_COMP_RATIO}:attack={VOICE_COMP_ATTACK_MS}:"
             f"release={VOICE_COMP_RELEASE_MS}")
    if not isolated:   # isolation replaces the denoiser, nothing else
        chain = (f"highpass=f={VOICE_HIGHPASS_HZ},"
                 f"afftdn=nr={VOICE_DENOISE_NR}:nf={VOICE_DENOISE_NF},"
                 f"acompressor=threshold={VOICE_COMP_THRESHOLD}:"
                 f"ratio={VOICE_COMP_RATIO}:attack={VOICE_COMP_ATTACK_MS}:"
                 f"release={VOICE_COMP_RELEASE_MS}")
    return ["ffmpeg", "-v", "error", "-y", "-i", str(src),
            "-af", chain, "-ac", "1", "-ar", str(AUDIO_SR),
            "-c:a", "pcm_s16le", str(dst)]
    # local: voice_raw.wav -> voice_clean.wav (isolated=False)
    # isolated: voice_isolated.mp3 decoded -> voice_clean.wav (isolated=True)
```

When isolation is honoured, step 2 runs **after** the isolation call
in pipeline order (extract → isolate → decode-clean → …) even though
it is numbered as one step: the local clean of `voice_raw.wav` is
still rendered first, so the §7.6 fallback is a file that already
exists, and `voice_clean.wav` always exists before step 5 whatever
happened provider-side. Isolation input is `voice_raw.wav` itself
(mono WAV, `other` format, §7.4).

**Step 3 — generate (only surviving, gated cues).** Provider calls in
fixed order: isolation (if not already done for step 2), music bed,
effects in id order. Each result goes through `_download_asset`:
write bytes to `finish/music_bed.mp3` / `finish/sfx_001.mp3` / …,
`probe_media` must show an audio stream with duration > 0 else the
asset is deleted and the cue skipped (§7.6 empty-audio rule), and a
duration drift `|probed − requested| > 0.5 s` records
`finish_asset_duration:<id>:<probed>s` (placement uses the probed
duration — the file that exists beats the length that was asked
for). The provider cap is re-checked cumulatively before **each**
call (§17): the call that would cross the cap, and everything after
it, is skipped with `finish_provider_cap_reached:$<cap>`.

**Step 4 — render the music bed timeline** (`music_mix.wav`, stereo,
full cut length; skipped entirely when there is no bed):

```python
def music_mix_command(bed, duration_s, plan, out):
    # loop the returned file if generation was capped at 600 s,
    # trim to the plan span, silence dropouts, fade the ends.
    fade_out_start = max(0.0, plan.end_s - plan.start_s - 1.0)
    vols = "".join(
        f"volume=enable='between(t,{a - plan.start_s:.3f},{b - plan.start_s:.3f})':volume=0,"
        for a, b in plan.dropouts)
    chain = (f"aresample={AUDIO_SR},aloop=loop=-1:size=2147483647,"
             f"atrim=0:{plan.end_s - plan.start_s:.3f},asetpts=N/SR/TB,"
             f"{vols}"
             f"afade=t=in:st=0:d=1,afade=t=out:st={fade_out_start:.3f}:d=1")
    delay_ms = round(plan.start_s * 1000)
    full = (f"{chain},adelay={delay_ms}|{delay_ms},"
            f"apad,atrim=0:{duration_s:.3f}")
    return ["ffmpeg", "-v", "error", "-y", "-i", str(bed),
            "-af", full, "-ac", "2", "-ar", str(AUDIO_SR),
            "-c:a", "pcm_s16le", str(out)]
```

Gain is **not** applied here: bed gain + ducking live in step 5b, so
re-levelling never requires re-looping the bed.

**Step 5 — render the effects timeline** (`sfx_mix.wav`, stereo,
full cut length; skipped when no cue survived):

```python
def sfx_mix_command(cues, duration_s, out):
    # cues: [(SoundCue, asset_path), …] in id order
    args = ["ffmpeg", "-v", "error", "-y"]
    for _cue, path in cues:
        args += ["-i", str(path)]
    graphs, labels = [], []
    for i, (cue, _path) in enumerate(cues):
        d_ms = round(cue.at_s * 1000)
        graphs.append(
            f"[{i}:a]aresample={AUDIO_SR},aformat=channel_layouts=stereo,"
            f"atrim=0:{cue.duration_s:.3f},asetpts=N/SR/TB,"
            f"volume={cue.gain_db:.1f}dB,"
            f"afade=t=in:st=0:d=0.05,adelay={d_ms}|{d_ms}[s{i}]")
        labels.append(f"[s{i}]")
    graph = (";".join(graphs) + ";" + "".join(labels)
             + f"amix=inputs={len(cues)}:duration=longest:normalize=0,"
               f"apad,atrim=0:{duration_s:.3f}[out]")
    return args + ["-filter_complex", graph, "-map", "[out]",
                   "-ac", "2", "-ar", str(AUDIO_SR),
                   "-c:a", "pcm_s16le", str(out)]
```

Placed start is the file start (cues are one-shots whose energy
begins at the file head for the generated kinds; no transient-offset
measurement is invented in this build — Spark places `at_s`, the
`afade` prevents click, and §18 listens on the live check (§19)).

**Step 5b — duck and sum** (`mix_premaster.wav`, stereo). The bed bus
(music + effects, or whichever exists) is sidechain-ducked by the
voice; with no bus the voice is simply centred:

```python
def duck_mix_command(voice, bed_mix, sfx_mix, out):
    if bed_mix is None and sfx_mix is None:
        return ["ffmpeg", "-v", "error", "-y", "-i", str(voice),
                "-af", f"aresample={AUDIO_SR},pan=stereo|c0=c0|c1=c0",
                "-c:a", "pcm_s16le", str(out)]
    # inputs: 0=voice(mono) 1=bed? 2=sfx? (order fixed: voice, music, sfx;
    # missing beds are omitted and indices rebuilt by the builder)
    # [voice] asplit -> [v_mix][v_key]
    # beds amix(normalize=0) -> volume=MUSIC_GAIN (music part only, see below)
    # -> sidechaincompress with [v_key] -> amix with [v_mix]
```

Concretely the builder emits (both beds present):

```text
[0:a]asplit=2[v][key];
[1:a]volume=-20.0dB[m]; [m][2:a]amix=inputs=2:duration=longest:normalize=0[bed];
[bed][key]sidechaincompress=threshold=0.10:ratio=4:attack=5:release=400[ducked];
[v][ducked]amix=inputs=2:duration=first:normalize=0[out]
```

Music gain (`MUSIC_GAIN_DB` or `MUSIC_GAIN_LOW_DB`) is applied to the
music leg **before** the bus sum so an effect's own `gain_db` is
never ducked twice; effect gains were set in step 5. Edge cases are
builder branches, not new graphs: music-only (no `[2:a]`, bed =
`[m]`), effects-only (no volume leg, bed = sfx mix), neither (the
`pan` command above). Voice stays at unity throughout — level is
loudnorm's job, once, in step 6.

**Step 6 — two-pass master** (`master.wav`, stereo):

```python
def loudnorm_measure_command(wav, target_lufs):
    return ["ffmpeg", "-nostats", "-nostdin", "-i", str(wav),
            "-vn", "-af",
            f"loudnorm=I={target_lufs}:TP={TARGET_TP_DBTP}:LRA={TARGET_LRA}"
            ":print_format=json",
            "-f", "null", "-"]
    # target_lufs resolved (flag > taste > -14) before building; stderr
    # carries the measured JSON block parsed by parse_loudnorm_full

def loudnorm_apply_command(src, dst, *, target_lufs, measured):
    af = (f"loudnorm=I={target_lufs}:TP={TARGET_TP_DBTP}:LRA={TARGET_LRA}"
          f":measured_I={measured['input_i']}"
          f":measured_TP={measured['input_tp']}"
          f":measured_LRA={measured['input_lra']}"
          f":measured_thresh={measured['input_thresh']}"
          f":offset={measured['offset']}:linear=true")
    return ["ffmpeg", "-v", "error", "-y", "-i", str(src),
            "-af", af, "-ac", "2", "-ar", str(AUDIO_SR),
            "-c:a", "pcm_s16le", str(dst)]
```

`parse_loudnorm_full` extracts the same `[Parsed_loudnorm_…]` JSON
block `cut.parse_loudnorm` reads, requiring all five keys; `None`
(including pure silence, which loudnorm cannot measure) retries once
with `linear=false` dropped — i.e. a single-pass applying call with
targets only — and failure there is `FinishError("finish: could not
measure loudness for mastering")`. After applying, the master is
re-measured; `finish.json` records `loudness_before` (step-5b mix),
`loudness_after` (master) and `target_lufs`. An after-measurement
more than ±1.0 LU from target records
`finish_loudness_off_target:<after> LUFS` but does not fail the stage
(platforms normalise playback anyway; the editor is told, the file
ships — QC check 9 will independently re-warn, §15).

**Step 7 — remux** (the only mutation of `cut.mp4`):

```python
def mux_command(video, master_wav, out):
    return ["ffmpeg", "-v", "error", "-y", "-i", str(video),
            "-i", str(master_wav),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(out)]
```

`_replace_master` muxes to `finish/cut_finished.mp4`, probes it (video
+ audio streams, duration within 0.1 s of the pre-finish `cut.mp4`),
and only then `os.replace`s it over `cut.mp4`. Picture bytes are
stream-copied — finish can never re-grade, re-crop or re-pick an
angle. A probe failure deletes the temp file and raises
`FinishError("finish: mastered audio could not be muxed; cut.mp4 is
unchanged")`.

### 11.7 Assets on disk (nothing generated is ever thrown away)

```
<job>/finish/
  voice_raw.wav        step 1 (always; the lav as rendered)
  voice_clean.wav      step 2 (always; what the mix actually uses pre-isolation A/B)
  voice_isolated.mp3   step 3, only when isolation ran (probed asset)
  music_bed.mp3        step 3, only when a bed was generated
  sfx_001.mp3 …        step 3, one per generated cue (survivor ids only)
  music_mix.wav        step 4 (rendered bed timeline, when a bed exists)
  sfx_mix.wav          step 5 (rendered effects timeline, when cues exist)
  mix_premaster.wav    step 5b (always)
  master.wav           step 6 (always; the exact audio now in cut.mp4)
  cut_finished.mp4     step 7 temp (replaced away; never left behind
                       on success, deleted on failure)
```

Every step-3 file gets a `FinishAsset` entry in `finish.json`
(§12) with provider, model, prompt (`""` for isolation), probed
duration, list-price cost and the §3 licence line dated with the
job's local date (`date.today().isoformat()`). Local files
(`voice_raw`/`voice_clean`/mixes/master) are **not** assets — they
have no provider, prompt or licence, and listing them as if they did
would be the fastest way to make the manifest untrustworthy. The
Spark plan (including dropped cues and §11.5 notes) is stored in
full in `finish.json` so a rejected riser can be judged later without
re-running anything.

## 12. `finish.json` and the `cut.json` addition

`finish.json` (version 1), written by `finish.run`/`run_files` and
embedded as the `finish` stage record's payload. One real example
(dropped cues appear only in `notes`, never in `plan.effects` or
`assets`):

```json
{
  "version": 1,
  "video": "cut.mp4",
  "duration_s": 182.400,
  "voice": {"method": "isolated", "clean_chain": "highpass+compressor",
            "isolate_requested": true},
  "provider_costs_are_estimates": true,
  "target_lufs": -14.0,
  "loudness_before": {"integrated_lufs": -31.2, "true_peak_dbtp": -18.4},
  "loudness_after": {"integrated_lufs": -14.1, "true_peak_dbtp": -1.2},
  "design": {"model": "muse-spark-1.3", "cached": false,
             "meta_usd": 0.0210,
             "taste": ["no whooshes", "music low"]},
  "plan": {
    "music": {"prompt": "warm minimal piano and soft pads, slow, hopeful, instrumental, no vocals, seamless underscore",
              "start_s": 0.000, "end_s": 182.400, "gain_db": -26.0,
              "dropouts": [{"start_s": 95.000, "end_s": 108.000}],
              "reason": "One bed for the whole talk; out under the closing story.",
              "confidence": 0.82},
    "effects": [
      {"id": "sfx_001", "kind": "riser", "at_s": 41.200,
       "duration_s": 2.500,
       "prompt": "soft rising synth swell, gentle, resolves cleanly",
       "gain_db": -14.0, "reason": "Into the second section.",
       "confidence": 0.77}
    ]
  },
  "assets": [
    {"id": "voice_isolation", "kind": "voice_isolation",
     "file": "voice_isolated.mp3", "provider": "elevenlabs",
     "model": "audio-isolation", "prompt": "",
     "duration_s": 182.400, "cost_usd": 0.3648,
     "licence": "Generated with ElevenLabs (audio-isolation) on 2026-11-03; use is subject to ElevenLabs's terms for your plan."},
    {"id": "music", "kind": "music", "file": "music_bed.mp3",
     "provider": "elevenlabs", "model": "music_v2_5",
     "prompt": "warm minimal piano and soft pads, slow, hopeful, instrumental, no vocals, seamless underscore",
     "duration_s": 182.400, "cost_usd": 0.4560,
     "licence": "Generated with ElevenLabs (music_v2_5) on 2026-11-03; use is subject to ElevenLabs's terms for your plan."},
    {"id": "sfx_001", "kind": "riser", "file": "sfx_001.mp3",
     "provider": "elevenlabs", "model": "eleven_text_to_sound_v3",
     "prompt": "soft rising synth swell, gentle, resolves cleanly",
     "duration_s": 2.480, "cost_usd": 0.0050,
     "licence": "Generated with ElevenLabs (eleven_text_to_sound_v3) on 2026-11-03; use is subject to ElevenLabs's terms for your plan."}
  ],
  "provider_spend_usd": 0.8258,
  "notes": ["finish_voice_isolated:elevenlabs",
            "finish_music_low:taste",
            "finish_cue_dropped:whoosh:88.000s:forbidden_by_taste"],
  "warnings": []
}
```

Field rules (violating any is a wrong build):

- `voice.method` ∈ `"local"`, `"isolated"`. `clean_chain` names the
  filters actually run (`"highpass+afftdn+compressor"` local,
  `"highpass+compressor"` isolated) so a later reader never has to
  infer it from `method`. `voice_isolated.mp3` exists iff
  `method == "isolated"`; the local `voice_clean.wav` always exists.
- `design` is `null` when sound design did not run (not asked, no
  Muse, or no provider). When it ran, `plan.music` is the verified
  bed (gains already resolved to §3 values) or `null`, and
  `plan.effects` lists **survivors only** — dropped cues live only in
  `notes`. `assets` lists files that exist and probed; a cue with a
  note but no asset has no file and must not be re-laid by hand.
- `loudness_before`/`loudness_after` use the keys QC check 9 knows
  so the same numbers can be eyeballed across both reports; either
  is `null` only when the single-pass fallback measured nothing, in
  which case `warnings` carries `finish_loudness_unmeasured`.
- `provider_spend_usd` is the sum of `assets[].cost_usd` (0.0 with no
  provider work). `provider_costs_are_estimates` is always present
  (top level) and `true` in this build — §7.5 has no invoiced figure
  to put there instead. Meta design money is `design.meta_usd`, never
  folded into provider spend.
- `notes` are the ordered §11.5/§11.6 strings (design → verify →
  generate → mix order). `warnings` are the stage-facing subset that
  `cut` also surfaces (§13.3); notes never duplicate warnings.

The `cut.json` addition (§13) mirrors this without duplicating the
plan: top level gains `"provider_spend_usd": <sum of stage provider
spends>` and the `finish` stage record gains
`"provider_spend_usd"`. `spend_usd` stays Meta-only, preserving
OPEN-DESIGN's summing rule and every existing assertion about it.

## 13. Inside `director cut`

`cut.py`'s real mechanics (read from the current file): `STAGES` at
module top, an `enabled` list built in `run()`, per-stage arms in the
nested `_fingerprint` / `_reload` / `_execute` closures, `StageRecord`
rows, and `_manifest` assembling `cut.json`. Finish is one more arm in
each place — no runner rewrite.

### 13.1 Stage placement, options, manifest

```python
STAGES = ("sync", "words", "tighten", "grade", "angles", "finish",
          "export", "reframe", "qc")

# JobOptions gains (defaults keep programmatic callers on the old
# job exactly as no_tighten=True does; the CLI opts in, §13.3):
#   no_finish: bool = True
#   finish_sound: bool = False
#   finish_isolate: bool = False
#   finish_loudness: float | None = None
#   max_provider_usd: float = 2.00
# cut.run(...) gains the same five keyword arguments and forwards them.
```

- `enabled` construction in `run()`: `enabled.append("angles")`, then
  `if not no_finish: enabled.append("finish")`, then `export` /
  `reframe` / `qc` as today. `records["qc"]`-style pre-seeding is not
  used for finish: with `no_finish` the stage simply never appears in
  `records` or `cut.json` (the `--no-grade` precedent — disabled-by-flag
  leaves no record, so an old manifest can never claim a finish that
  did not happen; `cli` prints nothing for an absent stage).
- `_manifest` gains `"provider_spend_usd": round(provider_spent_usd,
  4)` at top level and, inside each `finish` stage dict, the stage's
  `"provider_spend_usd"` alongside the existing `spend_usd` (Meta).
  Existing manifests without these keys reload fine (readers use
  `payload.get("provider_spend_usd", 0.0)`).
- A new `provider_spent_usd` total lives beside `spent_usd` in
  `run()`; the `nonlocal` lists of `_reload`/`_execute` gain
  `finish_payload` (the `finish.json` dict, for export/QC readers)
  in the same style as `angles_payload`.

### 13.2 Fingerprint, reload, execute arms

```python
# _fingerprint arm
if stage == "finish":
    taste_file = out_dir / "taste.json"
    return stage_fingerprint(
        "finish",
        {"model": options.model, "no_muse": options.no_muse,
         "sound": options.finish_sound,
         "isolate": options.finish_isolate,
         "loudness": options.finish_loudness,
         "max_provider_usd": options.max_provider_usd,
         "taste": fingerprint_of(read_json(taste_file)
                                 if taste_file.exists() else []),
         "elevenlabs_connected": key_status("elevenlabs").connected,
         "higgsfield_connected": key_status("higgsfield").connected},
        [out_dir / "cut.mp4", out_dir / "angles.json",
         *( [words_file_current] if words_file_current else []),
         *( [out_dir / "tighten.json"] if tightened else []),
         *( [taste_file] if taste_file.exists() else [])])
```

Connected **booleans**, never key bytes: editing taste, toggling
sound, or connecting a key re-runs finish; rotating a key's value
does not (identical options and plan cache key — the assets would be
re-bought for nothing). The `export` fingerprint arm appends
`out_dir / "finish.json"` when it exists, so re-finishing refreshes
the stem listing without re-picking angles. Reframe/QC arms are
unchanged: their existing `cut.mp4` file fingerprint changes when
finish rewrites it, and dirty propagation does the rest.

```python
# _reload arm
elif stage == "finish":
    loaded = read_json(out_dir / "finish.json")
    if not isinstance(loaded, dict):
        raise ValueError(f"Not a finish.json: {out_dir / 'finish.json'}")
    finish_payload = loaded
```

Outputs existence (the runner's generic `outputs` check) is
`["finish.json", "cut.mp4"]`.

```python
# _execute arm
elif stage == "finish":
    from . import finish as finish_mod

    payload = finish_mod.run(
        job_dir=options.out_dir, model=options.model,
        no_muse=options.no_muse, sound=options.finish_sound,
        isolate=options.finish_isolate,
        loudness=options.finish_loudness,
        max_provider_usd=max(0.0, options.max_provider_usd
                             - provider_spent_usd),
        max_usd=max(0.0, options.max_usd - spent_usd))
    finish_payload = payload
    meta_spend = float((payload.get("design") or {}).get("meta_usd") or 0.0)
    provider_spent_usd += float(payload["provider_spend_usd"])
    spent_usd += meta_spend
    records["finish"] = StageRecord(
        "finish", "done", fingerprint, ["finish.json", "cut.mp4"],
        meta_spend, list(payload["warnings"]), None)
```

`FinishError` propagates to the existing `_fail` path (stage
`failed`, job continues on the untouched `cut.mp4`, final status
`complete_with_warnings` when downstream succeeds — §13.3). There is
no pre-call Meta refusal specific to finish beyond the §11.4
estimate + `cache.guard(0.25)`; the trailing `_trailing_gate`
semantics stay as they are for `angles`/`qc`.

### 13.3 Console, status, flags

`cli._cut_stage_line` gains one arm (the formatter's existing
`"{name}: {status}"` + spend shape is kept; finish adds its payload
summary from the stage record's warnings/outputs and `finish.json`):
`finish` done → `finish: done ($0.0210) (local, -14.1 LUFS)` /
`(isolated, 1 bed + 2 effects, provider $0.83 est., -14.1 LUFS)`;
`finish` failed → the runner's generic `finish: failed` line plus the
recorded `error` (the exact §16 string). With `--no-finish` there is
no record and no line at all (§13.1), matching `--no-grade`.

New `cut` flags (CLI → `command_cut` → `cut.run` → `JobOptions`;
`store_true` defaults mean the CLI finishes by default while
programmatic `cut.run` callers keep the old job until they opt in,
the `no_tighten` precedent):

| Flag | Default | Meaning |
|---|---|---|
| `--no-finish` | off | Omit the finish stage entirely (old behaviour: picture + recorded audio). The only way to get an unfinished `cut.mp4` from this build on. |
| `--finish-isolate` | off | Use ElevenLabs isolation when connected (§11.1). |
| `--finish-sound` | off | Ask Spark for sound design and generate it when ElevenLabs is connected. |
| `--finish-loudness LUFS` | unset (`None`) | Master target −20…−10; taste overrides unset, flag overrides taste (§11.3). `-14` default, `-16` the documented alternative. Outside the range `command_cut` raises `ValueError("--finish-loudness must be between -20 and -10")` → `Error: …` exit 1 via `cli.main`, matching `--pause-limit` validation. |
| `--max-provider-usd USD` | `2.00` | Job-wide ElevenLabs/Higgsfield cap (§17). Validated ≥ 0 like `--max-usd`. |

`cut`'s summary block gains two lines after the existing spend line,
only when finish ran: `Provider spend: $0.83 (estimated list prices)`
and, when the timelines cannot carry finish audio,
`Note: timeline files contain picture only — re-lay finish stems from
<out>/finish/ (see finish.json)`. Status semantics: a finish that
degraded (local fallbacks, skipped cues, off-target loudness) leaves
the job `complete` with issues; only a `FinishError` (§16 hard
failures) makes it `complete_with_warnings` via the stage-failure
path (job continues to `export`/`reframe`/`qc` on the unfinished
`cut.mp4`, exactly as a failed `grade` degrades today). This matters:
the demo must ship a video even when mastering breaks.

## 14. The standalone `director finish` command

```text
director finish --job DIR [options]
director finish --video CUT.mp4 --angles ANGLES.json
                [--tighten TIGHTEN.json] [--words WORDS.json]
                [--taste TASTE.json] [options]
options: [--model M] [--no-muse] [--sound] [--isolate]
         [--loudness LUFS] [--max-usd USD] [--max-provider-usd USD]
```

- `--job DIR` resolves `cut.mp4`, `angles.json`, `tighten.json` (only
  if the file exists), `words.json`, `taste.json` (only if it exists)
  inside `DIR` and writes `DIR/finish.json` + `DIR/finish/`. The
  explicit form requires `--video` + `--angles`; `--words` defaults
  to the sibling `words.json` when present, `tighten` supplied words
  otherwise (the refine/angles fallback chain, reused); `--taste`
  defaults to the sibling `taste.json` when present. Output goes
  beside `--video` (`finish.json` + `finish/`), never inside the
  input video's directory tree beyond that. The video is rewritten
  in place by step 7 (same atomic replace), after printing
  `This will replace the audio of <video> (video stream copied).`
- Defaults mirror `cut`: `--model muse-spark-1.3`, `--no-muse` off,
  `--sound`/`--isolate` off, `--loudness` unset, `--max-usd 5.00`
  (Meta design-call gate), `--max-provider-usd 2.00`. Validation
  errors (`--video must exist`, loudness range, negative caps) go
  through `cli.main`'s `ValueError` handling (stderr + exit 1).
- Console on success (one line, then the loudness):
  `Finished <video>: local master, -14.1 LUFS (target -14), 0 assets, $0.00 provider`
  / with sound: `Finished <video>: isolated voice, 1 bed + 2 effects ($0.83 provider est.), -14.1 LUFS (target -14)`.
  `finish.json` path printed second. Exit 1 with the §16 message on a
  `FinishError`; degraded finishes (skipped cues, local fallback)
  print their notes after the success line and still exit 0.

## 15. How `angles`, `reframe`, `export` and QC consume finish

- **`angles`**: unmodified. It keeps rendering the unfinished master;
  finish is a separate stage precisely so re-picking one angle
  re-renders picture and re-finishes audio through dirty propagation
  instead of entangling the two spends.
- **`reframe`**: unmodified. Its `render_command` copies `cut.mp4`'s
  audio (`-c:a copy`), so `vertical.mp4` inherits the finished AAC
  bit-for-bit. The README states this in one sentence so nobody
  "fixes" reframe to re-master (§19).
- **`export`**: `export_timelines` gains keyword `finish_path:
  Path | None = None`. When given and the file exists, the returned
  section gains `"finish_stems": ["music_bed.mp3", "sfx_001.mp3"]`
  (asset files in id order) and one warning
  `finish_audio_not_in_timelines:music_bed.mp3,sfx_001.mp3`; the
  FCPXML/OTIO bytes are identical to a no-finish run (asserted in
  §18). When absent, the section is byte-identical to today. `cut`
  passes `finish_path` only when the finish stage ran. This is the
  §1.3 limitation surfaced where the editor downloads the timelines,
  not buried in a log.
- **QC** (`mechanical_qc` + `whole_cut_review`): unmodified. Check 9
  now measures the finished master, so a correct finish makes the old
  `cut_too_quiet`/`cut_loudness_unusual` warnings disappear without a
  QC change — the design notes this so the builder does not "relax"
  QC. The Spark whole-cut review hears the finished mix through its
  proxy (its soundtrack is the cut's), which is correct: reviewing
  the unfinished audio after finishing would approve a file nobody
  ships.

## 16. Failure and edge cases (every message is exact)

CLI errors print `Error: <message>` to stderr and exit 1 (`cli.main`'s
existing handling). Engine errors return `{"err": "<message>"}` with
the noted status. Stage failures inside `cut` keep the job alive via
the stage-failure path (§13.3). Connection test failures are reports
(`ok: false`), never exceptions. No message in this table contains a
key, a file's contents, or a provider response body beyond the mapped
category.

| Case | Exact behaviour |
|---|---|
| Unknown provider anywhere | `connect: unknown provider '<x>' (known: elevenlabs, higgsfield)` (`ConnectError`; CLI exit 1, engine 400). |
| `connect add` without `keyring` / backend | `connect: the macOS Keychain is unavailable; install the connection extra: pip install -e ".[connect]" — or set ELEVENLABS_API_KEY instead` (provider var name substituted). Nothing is written. |
| `connect add` with empty/whitespace key | `connect: that key was empty; nothing was saved` (exit 1). The prompt is re-asked zero times — one empty entry is an abort, not a loop. |
| Higgsfield half-pair (env or save) | `connect: Higgsfield needs both HF_API_KEY_ID and HF_API_KEY_SECRET (missing: HF_API_KEY_SECRET)` (missing name substituted; same shape for Keychain saves). Treated as not connected everywhere else. |
| Test with no key (one provider) | `elevenlabs: not connected — set ELEVENLABS_API_KEY or: director connect add elevenlabs` (provider names/vars substituted), `ok: false`, CLI exit 1. |
| ElevenLabs test 401/403 | `elevenlabs: ElevenLabs rejected this key (check it, or its scopes) — nothing was generated` `ok: false`. |
| ElevenLabs test 5xx/network/timeout | `elevenlabs: could not reach ElevenLabs (network or provider error) — try again` `ok: false`. Body text never included. |
| ElevenLabs test success | `elevenlabs: connected — test ok (<n> models visible)` where `<n>` is `len(models)` from `GET /v1/models`; `detail` carries the same count, no model ids that could fingerprint the plan beyond the count. |
| Higgsfield test 401 | `higgsfield: Higgsfield rejected this key pair (check the ID and secret) — nothing was generated` `ok: false`. |
| Higgsfield test 403 | `higgsfield: key accepted, but the account balance cannot run models — add funds, then test again` `ok: false`. |
| Higgsfield test 404/503 (test model) | `higgsfield: key accepted, but the test model is unavailable right now — try again later` `ok: false`. |
| Higgsfield test success | `higgsfield: connected — test ok (estimate $0.094)` with the estimate parsed from the string `usd` (formatted here only). |
| Engine import of `director.connect` fails | `503 {"err": "Connections are unavailable in this app build (director package not found)."}`; rest of app unaffected. |
| Engine add with missing field | `400 {"err": "connect: missing 'key' for elevenlabs"}` (field/provider substituted; value never echoed). |
| Sound requested, no suitable provider | Note `finish_sound_off_no_provider` in `finish.json`; clean + master only; job completes. Never a failure. |
| Sound requested, taste forbids sound | Note `finish_sound_forbidden_by_taste`; Spark not called (no Meta spend to design what taste already refused); job completes. |
| Sound requested, `--no-muse`/no guestlist | Note `finish_design_unavailable:guestlist not installed (pip install -e ".[muse]")` (or the ImportError text); no generation; job completes. |
| Spark design output not valid | Strict-schema failure proportion surfaces as note `finish_design_unavailable:<cause>`; no generation; job completes. (One retry inside `spark._structured` is guestlist's business, not finish's.) |
| Isolation requested, not connected | Note `finish_isolate_unavailable:elevenlabs not connected`; local clean; job completes. |
| Isolation requested, voice > 3600 s | Note `finish_isolate_skipped_long:<s>s`; local clean; job completes. |
| Isolation call fails (any §7.6 status) | Note `finish_isolate_fallback:local (<mapped category>)`; local clean (already rendered) is used; job completes. |
| One generation cue 400/422 | Note `finish_cue_skipped:<id>:elevenlabs rejected the request`; other cues continue; job completes. |
| Generation 401/403 mid-plan | Note `finish_provider_auth_failed:elevenlabs`; remaining calls skipped; already-generated assets are kept and mixed; job completes. |
| Generation 402/429 mid-plan | Note `finish_provider_quota:elevenlabs`; same keep-and-continue shape. |
| Generation 5xx/network | One retry after 2 s, then `finish_cue_skipped:<id>:elevenlabs unavailable`; job completes. |
| Empty/unprobeable returned audio | Asset deleted; note `finish_cue_skipped:<id>:empty audio`; job completes. (Music uses id `music`.) |
| Provider cap would be crossed | This and all later calls skipped; note `finish_provider_cap_reached:$<cap>` (cap formatted `%.2f`); job completes with what exists. |
| Corrupt/missing `taste.json` | Treated as `[]`; note `finish_taste_unreadable` when the file existed but did not parse; job completes. |
| `cut.mp4` has no audio stream | `finish: cut.mp4 has no audio stream to finish` (`FinishError`; standalone exit 1; `cut` stage failed → job `complete_with_warnings`, downstream runs on the untouched file). |
| Any ffmpeg step fails (pre-mux) | The `ToolingError` reason wrapped as `finish: <step> failed (<reason>)` where `<step>` ∈ `extract`, `clean`, `music mix`, `effects mix`, `duck mix`, `master`; `cut.mp4` untouched; same stage-failure handling. |
| Loudness unmeasurable (both passes) | `finish: could not measure loudness for mastering` (`FinishError`); `cut.mp4` untouched. |
| Master off target (> ±1.0 LU) | Note `finish_loudness_off_target:<after> LUFS` (1 decimal); file ships; job completes. |
| Mux probe/duration failure | `finish: mastered audio could not be muxed; cut.mp4 is unchanged` (`FinishError`); temp file deleted. |
| `--finish-loudness` outside −20…−10, negative caps, missing standalone files | `ValueError` with the offending flag named (e.g. `--finish-loudness must be between -20 and -10`); `Error: …` exit 1. No file is touched. |

## 17. Spend handling (both monies, both gates)

- **Meta money** (the §11.4 design call) uses the repo's existing
  rules unchanged: estimated from mirrored rates before the call,
  gated by `cache.guard(0.25)` plus the caller's remaining
  `--max-usd`, actual `usd` from the `SparkResult`, cached replies
  $0 and labelled `cached`. It lands in `finish.json: design.meta_usd`
  and in `cut.json`'s Meta `spend_usd` via the stage record. There is
  exactly one design call per finish run; nothing retries it at
  Director level.
- **Provider money** (ElevenLabs) is gated separately because it is a
  different bill the editor reconciles elsewhere:
  `estimate_finish_usd` sums isolation (source minutes) + bed
  (plan length) + cues (clamped durations) at §7.5 rates. The
  cumulative estimate is compared to the remaining
  `--max-provider-usd` **before the first generation call**; per-call
  re-checks in step 3 enforce it incrementally so a long bed cannot
  strand the cap before effects run. Recorded spend is the probed-
  duration recomputation (§7.5) in `finish.json:
  provider_spend_usd` **and** `cut.json: provider_spend_usd` — a second
  total, never merged into `spend_usd` (§12). Console and README call
  it "estimated list prices" every time, without exception.
- **Higgsfield spends $0** in this build (estimate test is free and is
  a `connect` operation, never charged to a job). If a future
  capability wires generation, it joins the provider cap and the same
  `provider_spend_usd` total — no third money is introduced.
- Neither gate prompts. Both refuse by skipping/noting (provider) or
  the existing pre-call refusal (Meta `cut` path); a standalone
  `finish` Meta-gate refusal surfaces as
  `finish_design_unavailable:exceeds --max-usd` and a local master,
  never as a half-generated sound design.

## 18. Tests

`python -m unittest discover -s tests -p 'test_*.py'` must stay green.
New tests live in `tests/test_connect.py` (items 1–14) and
`tests/test_finish.py` (items 15–38), numbered continuously below.
They self-skip when prerequisites are absent (ffmpeg, numpy/Pillow
where inherited tests need them), run offline, and fake Keychain, HTTP
and Spark exactly at the seams §4 defines. Test keys are fake
(`"test-key-…"`) throughout; no test reads a real environment key —
`mock.patch.dict(os.environ, …, clear=False)` with provider vars
explicitly removed/added per test.

Local-audio unit tests synthesise with ffmpeg `lavfi`:
`aevalsrc=0.3*sin(2*PI*440*t):d=2` (tone), `anullsrc` (silence),
`aevalsrc=…:d=…` concatenated for speech-like bursts — the angles/
transcribe synthetic-media precedent. `CutJob`-shaped job trees are
built with `angles.render_commands`-style parts or a single
`testsrc2` + tone MP4 via `cut_test_media` helpers mirrored from
`tests/test_cut.py`.

`tests/test_connect.py` (`TestConnect`):

1. **Registry shape:** `PROVIDERS` keys/order, ElevenLabs
   capabilities `("sfx", "music", "isolation")` + `available`,
   Higgsfield `capabilities == ()` + `available is False` +
   non-empty `unavailable_reason`; `provider_info("nope")` raises
   the §16 unknown-provider string.
2. **Env beats Keychain:** with a fake Keychain holding one key and
   env holding another, `key_status` reports `env` and
   `get_provider_key` returns the env value; env removed →
   `keychain`; both removed → `none`. Whitespace env counts as absent.
3. **Higgsfield half-pair:** only `HF_API_KEY_ID` set →
   `connected is False` and `list_providers` names the missing var;
   both set → connected.
4. **Set/remove round-trip (fake Keychain):** an in-memory fake
   `keyring` module in `sys.modules` (get/set/delete dict) proves
   `set_provider_key` writes under
   `("ghostcut-director", "elevenlabs")`, `key_status` then reports
   keychain, `remove_provider_key` deletes it (twice = still success),
   and env-connected remove leaves status connected.
5. **No-leak list payloads:** with env + Keychain keys set, assert the
   key strings appear nowhere in `json.dumps(list_providers())`,
   `json.dumps` of engine-shaped payloads, or `str(TestResult)` —
   including that no `mask`/`last4`/`fingerprint` key exists.
6. **Redaction:** `redact("…test-key-abc…", ("test-key-abc",))`
   removes it; a fake ElevenLabs 401 whose body deliberately contains
   the key string still yields a key-free `ElevenLabsError`.
7. **ElevenLabs test call (fake transport):** records method `GET`,
   URL `https://api.elevenlabs.io/v1/models`, header `xi-api-key`,
   no key in URL; 200 with 3 models → ok + `"3 models visible"`;
   401 → §16 rejected message; 500 → unreachable message. No
   generation URL is ever requested.
8. **Higgsfield test call (fake transport):** `estimate_request`
   pins `POST …/estimate/higgsfield-ai/soul/v2/standard` + body
   `{"prompt": "test"}`; header is `Authorization: Key <id>:<secret>`
   with no key in the URL; 200 `{"usd": "0.094"}` → ok; 401/403/404
   map to their §16 strings (403 must not say "rejected key").
9. **`connect` CLI:** fake Keychain + `getpass` patched: `add` saves
   and tests; `add` with env set prints the shadowing note; `test`
   with no key exits 1 with the not-connected line; `remove` of an
   env connection prints the unset-it line and exits 0; `list --json`
   parses and contains no key material. (`cli.main([...])` shape,
   stdout captured.)
10. **Keychain-absent degradation:** `keyring` import made to fail →
    `connect list` still reports env connections,
    `set_provider_key` raises the §16 install message, and engine
    `connections_add` returns 400 with it (no traceback).
11. **Engine endpoints:** `Engine.connections_*` against the fake
    Keychain: list shape, add→list reflects keychain, test payload
    carries `tests[].ok`, remove clears; unknown provider → 400
    §16 string; no response body contains the posted fake key.
12. **Multipart body:** `isolation_body(b"WAVBYTES", "voice.wav")`
    contains `name="audio"`, `filename="voice.wav"`, the bytes, a
    `multipart/form-data; boundary=` content type, and **no**
    `file_format` part and no key.
13. **Request builders:** SFX JSON pins `model_id
    eleven_text_to_sound_v3`, `loop: false`,
    `prompt_influence: 0.7`, `output_format=mp3_44100_128` in the URL;
    music JSON pins `music_v2_5`, `force_instrumental: true`,
    `music_length_ms` clamping (2000 → 3000, 700000 → 600000).
14. **Estimates:** `estimate_sfx_usd(30) == 0.06`,
    `estimate_music_usd(60) == 0.15`,
    `estimate_isolation_usd(120) == 0.24`; zero/negative durations
    estimate 0.0, never negative spend.

`tests/test_finish.py` (`TestFinishCommands`, `TestFinishDesign`,
`TestFinishRun`, `TestFinishInCut`):

15. **Command builders, element-for-element:** `extract`,
    `clean` (both `isolated` branches — `afftdn` present iff local),
    `loudnorm_measure`/`apply` (all five `measured_*` interpolated,
    `linear=true`), `music_mix` (loop + dropout `between` + fades +
    `adelay` — asserted as one `-af`, never two), `sfx_mix`
    (per-cue `adelay`, `normalize=0`), `duck_mix` (four graph
    branches: both/music-only/sfx-only/neither-`pan`), `mux`
    (`-c:v copy`, AAC 192k, `+faststart`). Numbers formatted exactly
    as §11.6 (`%.3f`, `round(…*1000)`).
16. **`parse_loudnorm_full`:** a captured two-pass stderr block parses
    to all five keys; truncated/garbage/silence output → `None`;
    `cut.parse_loudnorm`-style input-only blocks still parse (shared
    fixture string, not a second capture).
17. **Timeline conversion:** untightened job (origin = first shot
    start) and tightened job (kept/removed spans) both map words,
    beats and shots onto finish times with `cutlist` — a word inside
    a removed span drops, a word at the cut head lands at 0.0, and
    `beat_sections` degrades to the single `Cut` section without a
    `tighten.json`.
18. **`parse_taste` table:** every §11.3 row (forbids, `music low`,
    loudness `-16`/`-14`, isolate-ask, music-ask, `allow edge
    effects`) plus negatives: `no ducking` only notes, vague edge
    prose does not set `allow_edges`, unmatched prose changes nothing
    mechanically.
19. **Loudness precedence:** flag > taste > default; unset + no taste
    → `-14.0`; flag `-16` beats taste `-14`.
20. **`verify_design` gauntlet:** one plan exercising every step —
    invalid bed, forbidden whoosh, clamped gains/durations, edge
    drops (and `allow_edges` rescue), word-start move ≤ 0.75 s kept
    vs > 0.75 s dropped, overlap cap at −12 dB, density (4th in a
    minute dropped), total cap (lowest-confidence dropped), final
    ids gapless `sfx_001…`. Notes asserted as exact strings in order.
21. **`design_prompt` hygiene:** contains sections/shots/gaps/
    transcript/taste/forbids, contains no camera file path and no
    word `"contributor"`; `design_schema()` is strict with enums
    `["riser", "whoosh", "impact"]` and all fields required.
22. **Sound gate matrix (§11.1):** `design_with_muse` (fake
    guestlist `_structured`, cache cleared per test) is not called
    when sound is off / no provider / taste-forbidden, is called
    when asked+connected, and a fake Meta failure degrades to the
    `finish_design_unavailable` note with zero provider calls.
23. **Provider orchestration (fake transport + lavfi job):**
    isolation → bed → cues order; cap crossing skips the tail with
    `finish_provider_cap_reached:$2.00`; a 401 on cue 1 stops later
    calls (`finish_provider_auth_failed:elevenlabs`) but keeps cue
    1's file when it succeeded before a later failure variant;
    empty-body cue skipped and its file absent.
24. **Local finish end-to-end (ffmpeg, no keys):** 6 s tone-voice cut
    with `voice=dB` quiet synth → `finish.run` masters it: probed
    `master.wav`/remuxed `cut.mp4` integrated within ±1 LU of −14,
    TP ≤ −0.8, video stream present, duration within 0.1 s;
    `finish.json` records `method: "local"`, empty `assets`,
    `provider_spend_usd: 0.0`, top-level
    `provider_costs_are_estimates: true`.
25. **Isolated finish (fake transport):** fake isolation returns
    tone MP3 bytes → `voice_isolated.mp3` exists, `method:
    "isolated"`, clean chain without `afftdn`, asset cost =
    source minutes × 0.12; fake isolation 500 → fallback note and
    `method: "local"` with the same loudness assertions as 24.
26. **Bed + effects mix (fake transport):** bed MP3 + two cue MP3s
    (lavfi-generated fixtures written by the test, returned by the
    fake) → `music_mix.wav`/`sfx_mix.wav` full-length, dropout
    interval measures near-silent (RMS via `volumedetect` on the
    trimmed interval), ducked mix's bed-only tail is hotter than its
    under-speech span, final master still within ±1 LU of target.
27. **Edge cases:** silent `cut.mp4` audio → single-pass fallback or
    the §16 unmeasurable error, never a crash; video-only `cut.mp4`
    → `finish: cut.mp4 has no audio stream to finish`; a pre-existing
    `finish/` directory from a previous run is reused/overwritten
    file-by-file (resume shape), and a failed run leaves the original
    `cut.mp4` bytes untouched (hash compared).
28. **`cut` integration:** `CutJob` fixture (mirroring
    `tests/test_cut.py`) with `finish` in `STAGES` order; a no-key
    run completes with `finish.json` present, `cut.json` carrying
    top-level + stage `provider_spend_usd: 0.0` while Meta
    `spend_usd` is unchanged in kind; `--no-finish` omits the stage
    and its manifest record entirely.
29. **`cut` re-run/resume:** identical options reload `skipped`
    without re-mastering (mtime of `cut.mp4` stable); edited
    `taste.json` (`music low`) re-runs finish only (+ downstream);
    connecting a (fake-Keychain) key re-runs finish; rotating the key
    value does not (fingerprint holds booleans, §13.1).
30. **Export honesty:** with `finish_path`, export section lists
    stems + `finish_audio_not_in_timelines:…` and the FCPXML/OTIO
    files hash-identical to the no-finish run; without it, the
    section is identical to today's fixtures.
31. **CLI `finish` standalone:** `--job` form finishes a fixture job
    and prints the §14 summary; explicit `--video/--angles` form
    writes beside the video; `--loudness -16` masters within ±1 LU
    of −16; bad loudness/negative cap → `Error: …` exit 1 with no
    file touched.
32. **No-key-in-output sweep:** run items 22–26 with fake keys set;
    assert the key strings appear in no `finish.json`, `cut.json`,
    captured stdout/stderr, or generated filename.
33. **Docs pin (provider constants):** `SFX_MODEL`,
    `MUSIC_MODEL`, base URLs, output formats and rates equal §3 —
    the drift guard if a provider page changes (update the design
    first, then this test).
34. **Connections in cut-job mode:** `projectFor` legacy/cut-job both
    expose `connections` via `GET /api/connections` (per-machine, no
    job param accepted or required).
35. **Save never echoes:** POST add with a fake key → response JSON
    stringified contains neither the key nor any 4-char substring
    field; inputs-clearing is a UI behaviour pinned by asserting the
    engine stores via `connect` only (Keychain dict holds it, engine
    payload does not).
36. **Test route is non-generating:** fake transport records show
    `GET /v1/models` / estimate only after
    `POST /api/connections/test`.
37. **`finish.json` needs no app reader:** a finished job's
    `GET /api/project` (voice contract) still validates, and the
    cut-job player's `cut.mp4` is the finished file (same path —
    finish added no second video for the app to choose between).
38. **Panel copy comes from the registry:** engine list payload's
    Higgsfield `unavailable_reason` equals `connect.PROVIDERS`
    verbatim, so CLI, panel and `finish.json` notes can never disagree
    about why Higgsfield cannot finish.

## 19. What the build also updates; non-goals; live check

Docs shipped with the code (same commit series, per `AGENTS.md`
provenance):

- `README.md`: `director connect list|add|test|remove` and
  `director finish` in the command surface; a "Finishing" section
  stating in plain language — clean/level/master needs no key; sound
  design needs ElevenLabs connected and `--finish-sound`; the
  finished mix replaces `cut.mp4`'s audio and feeds `vertical.mp4`;
  **"The FCPXML/OTIO timelines contain the picture edit only: the
  installed exporters have no audio lanes, so re-lay the stems in
  `finish/` (listed in `finish.json`) under the timeline in your
  editor."**; generated audio's licence line means the editor works
  under their own ElevenLabs plan's terms (linked, not summarised
  into legal advice); Higgsfield is connectable for key testing but
  "offers no finishing capability yet (image/video generation only)".
  One "Who built what" row: connections + finishing — Muse Code.
- `AGENTS.md`: Layout bullets for `connect.py` ("registry, Keychain,
  `director connect`"), `elevenlabs.py`/`higgsfield.py` ("one module
  per provider; stdlib HTTP; keys never leave headers"), `finish.py`
  ("clean/isolate → Spark design → ducked mix → two-pass loudnorm
  master → remux; `finish.json` + provider stems"), and `cut.py`'s
  stage list gains `finish`.
- `docs/muse-code/BUILD-LOG.md`: one FINISH section in the
  BUILD-LOG's shape — verify table (fake-HTTP/fake-Keychain/lavfi
  evidence per §18), deviations (empty), live-check result.

**Non-goals (explicit, so nobody builds them):** Higgsfield
generation of any kind; ElevenLabs text-to-speech/voiceover, dubbing,
stems separation, `/v1/music/stream`, composition plans; per-provider
`--finish-provider` selection; key entry anywhere except `connect`/
the panel; DSP-synth effects; transient-offset placement; loudness
targets per platform beyond the −14/−16 choice; finish controls
inside the Mac app beyond Connections; modifying `ghostcut`,
`guestlist`, `angles`, `reframe` or QC; chunked isolation for
> 1-hour voices; reading a provider balance/invoice.

**Live check the design is written against** (the builder runs it
once ElevenLabs is connected; keys come from the environment, never
from this document): a short multicam job whose lav measures near
−38 dB RMS — `director cut … --finish-isolate --finish-sound` (or the
standalone `director finish --job … --isolate --sound` on an existing
job) — and verify by measurement, not by trust: `cut.mp4` integrated
loudness within ±1 LU of −14 with TP ≤ −1; `vertical.mp4` carries the
same finished AAC; one bed + ≤ 3 effects/min in `finish.json`, each
with an existing probed file, prompt, cost and licence; a taste entry
`no whooshes` visibly removes the whoosh between the two runs; then
listen once in the app player, because placement and ducking are
judged by ear exactly once after the numbers pass.
