# Design: talk to Director (voice notes that edit the cut)

Designed by Muse Code on `muse-wonder-1.0` from
[VOICE-BRIEF.md](VOICE-BRIEF.md), under [AGENTS.md](../../AGENTS.md),
[OPEN-DESIGN.md](OPEN-DESIGN.md) and
[TIGHTEN-DESIGN.md](TIGHTEN-DESIGN.md). The build (Muse Spark) writes
the code from this document without further decisions. No code is
written by this document.

Conventions that bind the build: Python ≥3.11, `from __future__ import
annotations`, type annotations throughout, `@dataclass(slots=True)` at
module boundaries, `guestlist` imported only inside the functions that
call Meta models and guarded there, API key from `MODEL_API_KEY` only,
no `-contributor` model ids, tests offline with `unittest` (no Meta
API, no Whisper, no microphone — stand-in clients and synthetic `lavfi`
media only), no media files committed, no client names, people's names
or local footage paths in code, tests or docs.

## Build order: this feature is built second

[TIGHTEN-DESIGN.md](TIGHTEN-DESIGN.md) is built first; this design is
built second, on top of it. The two features share one thing — **the
list of what stays in the cut** — and tighten creates it:
`tighten.json`, owned by `src/director/cutlist.py`, mutated only
through `cutlist.apply_operations`, rendered by `angles`. Voice notes
are then purely a new *input* to that existing contract: a spoken note
becomes the same operations an editor could apply by hand, checked by
the same mechanical checks, applied through the same function,
re-rendered by the same render. This build therefore adds no second
edit path, no second span list, and no fork of `tighten.json`. If the
tighten build has not landed, this build does not start; its first test
fixtures are tighten payloads.

What the editor gets, concretely: open a finished `director cut` job in
the Mac app (§9 — the app learns to open one), watch `cut.mp4`, hear
why Director kept each take in the Why panel, hold a key and *say*
"use the other take here" or "lose the crew talk", and get back
checked proposals grouped by note — "I heard: …" — to accept, reject or
accept all, then re-render. Or drop a 20-minute voice memo on the app,
or run `director voice memo.m4a --job demo-job --apply` (§8) with no
app at all.

## 1. Inputs as they actually are

Verified against this repository and the installed packages.

### 1.1 The job this feature opens

A `director cut` job directory (OPEN-DESIGN §10) contains `cut.json`,
`sync.json`, `words.json`, `grade.json`, `angles.json`, `cut.mp4`,
optionally `vertical.mp4`, the timelines, and — after the tighten
build — `tighten.json`. `angles.json` shots carry output-timeline
`start_s`/`end_s` plus, in a tightened job, `source_start_s`,
`source_end_s`, `kept_span_id`, `beat_id` (TIGHTEN-DESIGN §10.2).
`cutlist.source_to_output` / `output_to_source` / `beat_at_output` map
between the playhead's timeline and the beats. A job *without*
`tighten.json` (a `--no-tighten` cut, or a hand-run `angles` job the
editor points at) still opens and still takes angle and note
operations; take operations report the plain message in §10 rather
than guessing at spans that do not exist. Voice never invents a
`tighten.json` for such a job.

### 1.2 Voice Transcribe (verified)

```python
from guestlist.meta.transcribe import diarize  # inside the function only
diarize(wav: str | Path, *, language_bias: list[str] | None = None) -> dict
# WAV in (mono, 16-bit PCM, 16 or 24 kHz only); returns
# {"transcript": str, "turns": [{"startMs", "endMs", "transcript",
# "speaker"}], "audioDurationMs", ...}
# Turn-level times only — no word timestamps. Content-cached by file
# hash; cost via guestlist.meta.pricing.transcribe_cost
# ($0.18 per hour = $0.00005 per second).
```

Two consequences the whole design follows from: (a) a note's own audio
has no word clock, so **anchors come from elsewhere** — the recorded
playhead, a spoken timecode, or words quoted from the cut (§6);
(b) anything uploaded for transcription is first extracted to a local
16 kHz mono WAV (§4.1), and the original recording never leaves the
machine in any other form.

### 1.3 The Spark text call (the app's existing precedent)

Splitting and resolving notes is text work: the cut's transcript, shot
list, beats and taste file are all text, and no picture is needed until
a note explicitly points at one. The repository already has exactly
this call shape in `app/muse.py:ask` — a text prompt in,
schema-constrained JSON out, through `guestlist.meta.cache` (content
key, `guard(0.25)`, spend ledger) and
`spark._structured(k, [{"type": "input_text", "text": prompt}], …)`.
`voice.py` follows that precedent exactly (same cache discipline, same
guard, `name="voice_resolve"`), wrapped so tests substitute a stand-in
module. Where seeing the frame genuinely matters, §6.5 adds **one**
bounded `spark.image_json` confirmation — the public image call
`angles.pick_with_muse` already uses — never a video upload of the cut
for a spoken note.

### 1.4 The app today

`app/engine.py` serves `app/ui/` on 127.0.0.1, JSON bodies only,
localhost Host check, and exposes `/api/project`, `/api/muse`,
`/api/accept`, `/api/undo`, `/api/frame`, `/api/render*`. Its project
is `app/ui/demo/project.json` (built by `app/make_demo.py` from a
single-camera Render `timeline.json` or FCPXML work folders) with
`sections`, `talk`, `broll`, `cards`, `library`, `transcript`
(`{t, e, text, w: [[word, start, end], …]}`), `findings`, `takes`.
`app/muse.py` is the typed path: Spark proposes ops
(`OP_SCHEMA`), `muse.validate` attaches mechanical `checks`
(`{ok, text, hard}`) and a `blocked` flag, `Engine.accept` applies
unblocked ops into `edits.json`, and the UI renders proposals with
per-op checkboxes, seek links and Accept/Reject. `app/render.py` +
`render_plan.py` re-render *that* pipeline's timeline; they cannot
re-render a multicam `angles` cut and are **not modified**. The
microphone caveat in the brief is real: `app/main.py` is a pywebview
(WKWebView) window where `getUserMedia` may be refused; the same engine
served to a normal browser at its localhost address records fine. §9.4
designs for both, with detection and a plain instruction.

## 2. Files changed

| File | Change |
|---|---|
| `src/director/voice.py` | New. Note transcription (Meta + local fallback), the estimate, split-and-resolve Spark call, time resolution, op validation glue, `voice.json` / `taste.json` IO, `run()`. The CLI and the engine both call this module; neither re-implements any of it. |
| `src/director/cli.py` | New `command_voice(args)` + `voice` subparser (§8). |
| `app/cutjob.py` | New. A `director cut` job directory → the app's `project.json` shape (§9.1), plus the cut-job re-render (§9.5). Pure mapping + ffmpeg stills; no engine changes needed to *build* a project. |
| `app/engine.py` | Cut-job mode: `--job` startup, `POST /api/job/open`, the voice endpoints and taste endpoints (§9.3), `/api/cutjob/rerender`. Legacy demo mode byte-compatible when no job is open. |
| `app/main.py` | Optional `--job DIR` argument passed to `engine.serve`; the window title gains the job name. No other change. |
| `app/make_demo.py` | New `--cut-job DIR` mode delegating to `app/cutjob.build_project` (so a project can be built from the terminal without starting the engine). |
| `app/ui/index.html` | The mic button, the voice-note list container, the drop overlay, the Taste panel (§9.4). |
| `app/ui/app.js` | Recording (hold-to-talk), upload, proposal rendering for voice responses (reusing the `/api/muse` proposal renderer), taste UI, cut-job re-render button state. |
| `app/ui/app.css` | Styles for the above, following the existing panel idioms. |
| `tests/test_voice.py` | New. §11 items 1–16. |
| `tests/test_cutjob.py` | New. §11 items 17–24 (engine tests use the existing engine test harness style with a temp job dir). |
| `README.md` | At build time: the voice paragraph, the app-opens-a-cut-job quickstart (§12). One "Who built what" row. |
| `AGENTS.md` | At build time: one Layout bullet (`voice.py`) and the `app/` line gains "opens `director cut` jobs". |
| `docs/muse-code/BUILD-LOG.md` | At build time: one VOICE section (§12). |

`src/director/cutlist.py`, `tighten.py`, `angles.py`, `sync.py`,
`grade.py`, `reframe.py`, `cut.py`, `export.py` and `app/muse.py`,
`app/render*.py` are **not modified**. The typed Muse path keeps
working for legacy projects exactly as today; voice proposals for cut
jobs travel a parallel endpoint into the same *rendering* idiom
(checkboxed, checked ops) but land in `cutlist.apply_operations`.

Imports: `voice.py` takes stdlib (`json`, `re`, `shutil`,
`subprocess`, `dataclasses`, `pathlib`, `datetime`) plus
`ghostcut.config` (`read_json`, `write_json`),
`ghostcut.ffmpeg_tools` (`ToolingError`, `probe_media`,
`extract_media_stats`, `run_command`), `from . import angles as
angles_mod`, `from . import cutlist`. `guestlist` appears only inside
`transcribe_wav` and `resolve_notes` (§1.2, §1.3). `app/cutjob.py`
imports stdlib + `ghostcut.ffmpeg_tools` only, and reads job JSON with
`json.loads` — it must import under the app's plain Python without
`src/` on the path, so it never imports `director.*` (the mapping is
re-derived from the JSON contracts, which §9.1 pins).

## 3. Constants

All in `voice.py` unless stated. Decided; the build does not tune them.

| Constant | Value | Why |
|---|---|---|
| `TRANSCRIBE_SR` | `16000` | The sample rate `diarize` accepts (16/24 kHz mono 16-bit WAV). |
| `MAX_NOTE_S` | `1200` | 20 minutes, the brief's long-note bound. Longer files are refused with the §10 message before any spend. |
| `ESTIMATE_CONFIRM_S` | `60` | Notes longer than this show a spend estimate and need confirmation (app dialog / CLI `--yes`) before transcription. |
| `DEFAULT_MAX_USD` | `2.00` | `--max-usd` default for `director voice`; the app passes the same cap per request. Transcription of 20 min is ≈$0.06; the Spark resolution dominates and is estimated in §4.3. |
| `RESOLVE_MAX_OUTPUT_TOKENS` | `8000` | Same output budget as `app/muse.ask`. |
| `QUOTE_MIN_WORDS` | `2` | A quoted phrase needs at least two content words to match against the cut's transcript (§6.3); one word is not an anchor, it is a coincidence. |
| `ANCHOR_WINDOW_S` | `6.0` | A playhead anchor `at_s` resolves to the beat/shot overlapping `[at_s - 1.0, at_s + 6.0]` on the output timeline (the editor reacts slightly *after* the moment). |
| `CONFIDENCE_FLOOR` | `0.55` | A resolved note below this confidence is reported unresolved (§6.4), never guessed. |
| `TASTE_MAX_ENTRIES` | `50` | The taste file stays a page an editor will actually read; adding the 51st refuses with the §10 message. |
| In `app/cutjob.py`: `THUMB_EVERY_S` | `2.0` | Poster/thumbnail cadence, same as `make_demo.stills`. |

## 4. Transcription: from a recording to turns

### 4.1 Extract the audio — `extract_audio_command`

Any input (voice memo `.m4a`/`.mp3`/`.wav`, phone video `.mp4`/`.mov`,
an in-app webm recording) first becomes the WAV `diarize` requires.
The original file is never uploaded.

```python
def extract_audio_command(src: Path, wav: Path) -> list[str]:
    return ["ffmpeg", "-v", "error", "-y", "-i", str(src),
            "-vn", "-ac", "1", "-ar", str(TRANSCRIBE_SR),
            "-c:a", "pcm_s16le", str(wav)]
```

Run via `run_command` into the job's `voice/` work directory
(`<job>/voice/<note stem>.wav` for files; the engine's
`<demo>/voice/` for in-app recordings). Duration is probed first
(`probe_media` + `extract_media_stats`): over `MAX_NOTE_S` →
`VoiceError(f"voice: note is {mins:.0f} min; the limit is 20 min — "
"split it or trim the file")` before extraction or spend. A file with
no audio stream → `VoiceError("voice: no audio in <file name>; "
"record a voice memo or a video with sound")`.

### 4.2 Transcribe — `transcribe_wav`

```python
def transcribe_wav(wav: Path, *, allow_whisper: bool = True) -> tuple[list[Turn], str, float]:
    """(turns, engine, spend_usd); engine is "meta" or "whisper"."""
```

Primary: `guestlist.meta.transcribe.diarize(wav)` (the only
`guestlist.meta.transcribe` import), turns mapped to the local
`Turn(start_s, end_s, text, speaker)` from `startMs`/`endMs` (÷1000),
`transcript` per turn stripped; empty turns dropped. Spend is
`pricing.transcribe_cost(duration)` recomputed locally (the diarizer's
cache makes re-runs $0, and the function returns the same estimate the
gate used). Any failure of the Meta path — `ImportError` (no
guestlist), `RuntimeError` from `api_key()` (no `MODEL_API_KEY`), or an
HTTP error — falls to the local fallback when `allow_whisper`:

Local fallback: `shutil.which("mlx_whisper")`; run
`["mlx_whisper", str(wav), "--output-format", "json", "--output-dir",
<tmp>, "--word-timestamps", "True", "--verbose", "False"]` (the shape
`sync` already uses) and build turns from the JSON's `segments`
(`{start, end, text}`, one turn per segment, speaker `""`). The caller
always reports which engine ran, in the exact words of §10 — a
Whisper-transcribed note is labelled, never passed off as Meta's.
Neither available → `VoiceError("voice: cannot transcribe: the Meta
client is unavailable (<error>) and mlx_whisper is not on PATH; "
"install one of them, or type the note instead")`. That message is the
degradation the brief asks for: clear, named, with the way forward.

### 4.3 The estimate, shown before spend

```python
def estimate_note_usd(duration_s: float, transcript_chars: int = 0) -> float:
    """Transcribe (per-second rate) + one resolve call estimated at
    (fixed 1200 tokens of cut context per note-minute + transcript
    chars/4) input tokens at spark rates. Pure."""
```

Rates mirror `guestlist.meta.pricing` without importing it
(`TRANSCRIBE_PER_SECOND = 0.18/3600`; spark input $1.25/1M, output
$4.25/1M with `RESOLVE_MAX_OUTPUT_TOKENS` as the output term — the same
mirror discipline as OPEN-DESIGN §3). The CLI prints the estimate to
stderr before transcribing (`voice: ≈$0.07 to transcribe and resolve
(3.2 min)`) and proceeds without asking unless the estimate exceeds
`--max-usd`, which refuses with `voice: estimated $X exceeds
--max-usd $Y; raise --max-usd`. The app shows the estimate in the
confirm dialog for notes over `ESTIMATE_CONFIRM_S` (§9.4). The gate is
checked again before the resolve call with the real transcript length;
over the cap there, resolution is refused and the transcript is still
returned (what was *heard* is never held hostage to what it would cost
to *interpret*).

## 5. The shared operations (what a note can ask for)

A voice note resolves to operations from **exactly** the
`cutlist.apply_operations` vocabulary (TIGHTEN-DESIGN §5.2) —
`swap_take`, `drop_beat`, `restore_beat`, `drop_span`, `restore_span`,
`set_angle`, `clear_angle`, `set_pause_limit`, `add_note` — plus two
that live only in this feature:

| `op` | Fields | Effect |
|---|---|---|
| `add_taste` | `text` | Appends a standing preference to `taste.json` (§7). Not a timeline op; shown in the Taste panel, removable there. Applied immediately on accept, never rendered. |
| `retime_target` | `target_s` | Records the editor's target length in `voice.json` and proposes nothing renderable yet: on accept, the engine/CLI runs tighten's `apply_target` (whole beats only, TIGHTEN-DESIGN §9) and reports which beats it dropped. ("Make it under three minutes" is a target, not a guess at which beats to lose.) |

And three honest refusals, which are operations only in the sense that
the editor sees them: a note about **music level, colour, or loudness**
("the music is too loud under her voice") resolves to `add_note` with
the understood text and the message `Director can't change the mix —
noted for the edit` (no stage in this repo masters audio; OPEN-DESIGN
§15 already keeps that a non-goal). A note asking for footage that is
not in the cut ("use the drone shot") resolves to `add_note` likewise
(`No such shot in this cut — noted`). A note Director cannot parse
into any of the above resolves to no ops at all with status
`unresolved` (§6.4). Plainly saying so is the designed behaviour, not a
failure path.

## 6. Split and resolve: from turns to checked proposals

### 6.1 The resolve call — `resolve_notes`

```python
def resolve_notes(*, turns: list[Turn], job: JobContext,
                  anchors: list[float | None], model: str
                  ) -> tuple[list[ResolvedNote], float, list[str]]:
    """One Spark text call -> (resolved notes, spend_usd, warnings).
    Raises VoiceUnavailable only when guestlist cannot be imported."""
```

`JobContext` (§6.2) is built once per job by
`build_job_context(job_dir)`. The call follows §1.3's precedent:
`cache.key("voice", model, DEVELOPER, prompt, RESOLVE_SCHEMA,
"medium")`, `cache.get("spark", k)`, `cache.guard(0.25)`,
`spark._structured(...)` with `name="voice_resolve"`,
`max_output_tokens=RESOLVE_MAX_OUTPUT_TOKENS`. A cached reply costs $0
and says so. Any exception from the call (network, no answer) →
`VoiceError(f"voice: Muse could not resolve the notes "
f"({ExceptionClass}: <message>); the transcript is saved in "
f"voice.json — check the key and try again")` with the heard transcript
already persisted by the caller. No partial proposals are invented from
a failed call.

Developer prompt (verbatim, stored as `DEVELOPER` in `voice.py`):

> You are Muse, the editing assistant inside GhostCut Director. The
> editor has watched the cut and spoken notes about it; the notes were
> transcribed and may mishear words. Below are the cut's beats (the
> takes Director kept, with their alternates), its shots, its
> transcript with output times, the editor's standing preferences, and
> the spoken notes, each with any anchor time we recorded. Split the
> speech into separate notes and turn each into the operations listed
> — nothing else exists. "Use the other take", "the second version",
> "go back to the first one" mean swap_take to the named alternate of
> the beat the note is anchored to. "Lose", "cut", "drop" a part mean
> drop_beat or drop_span for the beat it belongs to. "Go to the wide /
> the close-up / the iPhone angle" mean set_angle for that beat to the
> camera whose shot size or label matches. "That pause is too long" or
> "tighten the pauses" mean set_pause_limit (0.5 if a number is not
> given, or the number of seconds spoken). "Make it under three
> minutes" means retime_target. "Always", "never", "this client
> hates/loves" mean add_taste as well as any concrete change. When a
> note only comments, asks a question, or asks for something no
> operation does (music level, colour, new footage), return it as a
> plain note with no operations and say what you heard. Give for every
> note the anchor you used and a one-line "I heard" paraphrase. Never
> invent a beat, a take, a camera or words that are not below; if a
> note could mean two beats, say ambiguous and list both — do not pick
> one quietly.

User prompt (`resolve_prompt(job, turns, anchors)`): a `CUT` block
(`duration`, per beat: `B<id> "<topic>" out <start>-<end>s pick
A<pick> (<reason>) alternates: A<n> … removed: <kinds>`; per shot:
`shot <id> out <start>-<end>s camera <label> (<shot_size>) beat
B<id>`; transcript lines `[out <start>-<end>] <text>` mapped through
`cutlist.source_to_output`, words in removed material omitted), a
`PREFERENCES` block (taste entries or `none`), and a `NOTES` block:
one entry per spoken note as `N<n> (anchor: playhead <t>s | spoken
timecode <t>s | none): "<transcript of the note>"`. Note segmentation
into `N<n>` is mechanical before the call: turns are grouped into
notes at turn boundaries and at pauses over 1.5 s inside the note
recording, and a live recording (§9.4) is always exactly one note —
the editor held the key once, that is one thought. Spark may further
split a file note's entry; its reply's `note_index` refers to these
`N<n>` ids and §6.4 merges faithfully.

Schema (`RESOLVE_SCHEMA`, strict, `additionalProperties: false`).
Ops use the app's sentinel discipline (`""`/`-1` for unused fields):

```json
{
  "type": "object", "additionalProperties": false,
  "required": ["notes"],
  "properties": {"notes": {"type": "array", "items": {
    "type": "object", "additionalProperties": false,
    "required": ["note_index", "heard", "anchor_kind", "at_s", "quote",
                 "beat_id", "status", "message", "confidence", "ops", "taste"],
    "properties": {
      "note_index": {"type": "integer"},
      "heard": {"type": "string"},
      "confidence": {"type": "number"},
      "anchor_kind": {"type": "string", "enum": ["playhead",
        "spoken_timecode", "quote", "beat", "global", "none"]},
      "at_s": {"type": "number"},
      "quote": {"type": "string"},
      "beat_id": {"type": "integer"},
      "status": {"type": "string", "enum": ["resolved", "ambiguous",
        "unresolved", "preference"]},
      "message": {"type": "string"},
      "ops": {"type": "array", "items": {
        "type": "object", "additionalProperties": false,
        "required": ["op", "beat_id", "attempt_id", "span_id", "camera",
                     "pause_limit_s", "target_s", "text", "why"],
        "properties": {
          "op": {"type": "string", "enum": ["swap_take", "drop_beat",
            "restore_beat", "drop_span", "restore_span", "set_angle",
            "clear_angle", "set_pause_limit", "add_note", "add_taste",
            "retime_target"]},
          "beat_id": {"type": "integer"}, "attempt_id": {"type": "integer"},
          "span_id": {"type": "string"}, "camera": {"type": "string"},
          "pause_limit_s": {"type": "number"}, "target_s": {"type": "number"},
          "text": {"type": "string"}, "why": {"type": "string"}
        }
      }},
      "taste": {"type": "array", "items": {"type": "string"}}
    }
  }}}
}
```

### 6.2 The job context

```python
@dataclass(slots=True)
class JobContext:
    job_dir: Path
    cut: dict | None          # cut.json payload, None for a bare angles job
    tighten: dict | None      # tighten.json payload, None when untightened
    angles: dict              # angles.json payload
    sync: dict                # sync.json payload
    words: list               # angles.TimedWord list (timeline times)
    covers: dict[int, list[str]]  # beat id -> cameras covering its pick's lines
```

`covers` is computed once from sync clips + `angles.split_lines` over
each pick's kept words (the `angles.covering` rule), so `set_angle`
validation (§6.4) is a lookup, not a re-derivation per note. For an
untightened job, `tighten` is `None`, beats are absent, and only
`set_angle` (against shots, via shot `beat_id = -1` → resolved against
the shot's lines instead) and `add_note`/`add_taste` can resolve —
take ops return the §10 untightened message.

### 6.3 How times resolve (the anchor priority)

Spark proposes an anchor; these mechanical steps verify and place it,
in this priority order. The first that lands wins, and the winner is
recorded as the note's `anchor_kind`:

1. **Playhead** (live notes): the recorded `at_s` is on the output
   timeline by definition. `cutlist.beat_at_output(job.tighten, at_s)`
   (or the shot containing `at_s` when untightened) is the beat. This
   is the strongest anchor and is never second-guessed — it is what
   "here", "this", "that shot" meant when spoken.
2. **Spoken timecode**: parsed mechanically *before* Spark from the
   note text (`parse_spoken_timecode`: `1:20`, `a minute twenty`,
   `at ninety seconds`, `two minutes in` → seconds on the **output**
   timeline — the editor was watching the cut, so their clock is the
   cut's). A parse is offered to Spark as the anchor and verified to
   land inside the cut; a timecode past the end is reported
   (`You said <t>, the cut is <dur> long`) and the note falls through
   to quote matching rather than clamping silently.
3. **Quoted words**: the note's quoted or repeated phrase
   (`where I say 'three cameras'`) is matched by
   `match_quote(words, quote)` — normalised (lower-case, punctuation
   stripped) consecutive-token search over the kept words, needing
   `>= QUOTE_MIN_WORDS` content tokens. Exactly one match → that
   word's beat (via its output time) is the anchor. Several matches →
   `ambiguous` with every candidate listed by output time and text;
   the editor picks in the UI (§9.4), and picking re-runs validation
   for that candidate only.
4. **Camera/shot reference**: "the iPhone angle", "the wide" — matched
   against shot `camera` labels and `shot_size` values near any weaker
   anchor, or, with no other anchor, the first shot matching after the
   previous note's position (notes in a memo are usually in viewing
   order; the resolver tracks a monotonically advancing cursor and
   says `advanced from the previous note` in the message when it uses
   it). No match → unresolved with `No shot matches '<phrase>' in this
   cut`.
5. **Global**: "tighten every pause", "make it under three minutes",
   standing preferences — no beat anchor; the op carries `beat_id -1`
   and applies job-wide.

### 6.4 Verification (Muse proposes, these decide)

In `verify_resolved(job, notes) -> list[ResolvedNote]`, in order:

1. **Ops are real.** Every op goes through
   `cutlist.validate_operation(job.tighten, op, covers=job.covers)`
   (or the untightened subset). Ops are returned with `checks`,
   `blocked` and a readable `label` (the `app/muse.validate` shape —
   `Swap B3 → A2`, `Drop beat B5`, `Camera B for beat B2`,
   `Pauses over 0.5s shortened`) so both UIs render one idiom. An op
   naming a vanished beat/attempt is blocked with cutlist's text,
   never dropped from the list: the editor sees what was understood
   *and* why it cannot happen.
2. **Anchors are real.** A `beat_id` Spark names must exist and must
   agree with the mechanical anchor (§6.3) when one exists; disagreement
   → the mechanical anchor wins, the op is re-targeted, and the note's
   message gains `Heard as beat B<x>; the words/timecode point to
   B<y>, used B<y>`. A note whose beat cannot be established by any
   step becomes `unresolved` with message `I couldn't place that note
   in the cut` and no ops — instead of a guess (the brief's rule).
3. **Confidence.** The note's schema `confidence`, clamped to
   [0, 1] on assembly, below `CONFIDENCE_FLOOR` → `unresolved`, same
   plain message, heard text shown.
4. **Taste is separated.** `taste[]` strings become `add_taste` ops
   with status `preference` on the note; they never ride along inside a
   timeline op, so accepting a take swap cannot silently accept a
   standing rule the editor did not notice.

`ResolvedNote` (dataclass): `id` (`"n1"…` per voice session/file),
`source` (`"live" | "file" | "cli"`), `raw_text` (the note's
transcript, verbatim), `heard`, `anchor_kind`, `at_s` (output seconds
or `None`), `beat_id`, `status`, `message`, `ops` (checked),
`candidates` (for `ambiguous`: `[{"beat_id", "at_s", "label"}]`),
`engine` (`"meta" | "whisper"`), `usd`.

### 6.5 When a frame matters (one bounded image call)

When Spark's note carries a visual reference ("when she points at the
kitchen", "the shot where he holds up the card") and its anchor landed
by quote or timecode, `resolve_notes` extracts stills at the anchored
output time ±2 s from `cut.mp4` (the `angles.frame_command` shape,
384 px) and makes **one** `spark.image_json` call per such note
(schema `{"type":"object","additionalProperties":false,"required":
["confirmed","camera","description"],"properties":{"confirmed":
{"type":"boolean"},"camera":{"type":"string"},"description":
{"type":"string"}}}`; developer: `You are checking one editing note
against stills from the finished cut. Say whether the moment the note
describes is visible, and which camera label (shown on each still) it
belongs to. Never invent an action you cannot see.`). Confirmed with a
camera → the `set_angle` op is kept and gains the check `Frame check:
<description>`. Not confirmed → the op is replaced by `add_note` with
message `I couldn't see that moment at <t> — noted instead of
changed`. Guestlist unavailable → the op is kept but gains a soft
(unblocking) check `Frame check unavailable (no Meta client)`. At most
the first such note per memo pays for stills beyond four frames total;
the rest become notes. This is the only picture a voice note ever
uploads, and never the camera originals.

## 7. The taste file

`<job>/taste.json` — per project (the job directory *is* the project;
a client with several jobs gets one file per job, and the README says
to copy it forward — no cross-job store is invented in this build):

```json
{
  "version": 1,
  "entries": [
    {"id": "t1", "text": "this client hates jump cuts",
     "source": "voice", "note_id": "n4", "at": "2026-11-03T21:20:11"},
    {"id": "t2", "text": "always open on the wide",
     "source": "typed", "note_id": null, "at": "2026-11-03T21:31:40"}
  ]
}
```

Functions in `voice.py`: `load_taste(job_dir) -> dict` (missing file →
`{"version": 1, "entries": []}`, never an error),
`add_taste(job_dir, text, *, source, note_id=None) -> dict` (ids
`t<n>` sequential, never reused; the `TASTE_MAX_ENTRIES` refusal is
`VoiceError("voice: the taste file already has 50 entries; remove one
in the app before adding more")`), `remove_taste(job_dir, entry_id)`.
Read into: every `tighten` window prompt (TIGHTEN-DESIGN §7.3 already
reserves the lines), every voice resolve prompt (§6.1), and shown in
the app (§9.4) where each entry has a remove button. The file is plain
JSON the editor can open; nothing in it is ever sent anywhere except as
prompt text to Spark on a call the editor initiated.

## 8. Command line: `director voice`

```
director voice NOTE [--job DIR] [--sync SYNC.json --words WORDS.json
                     --tighten TIGHTEN.json --angles ANGLES.json]
                 [--apply] [--render] [--model muse-spark-1.3]
                 [--max-usd 2.00] [--yes] [--no-whisper]
```

`NOTE` is an audio or video file (memo, phone video, in-app export).
`--job DIR` names a `director cut` job and implies the four files from
it; the explicit-file form serves a hand-run pipeline. `cli.command_
voice(args)` calls `voice.run(...)`, prints **what it heard first**
(each note: `N1 [engine meta] "<raw text>"`), then per note the
resolution: `→ B3 "the timecode problem": swap to A2 — <why>` with
blocked ops prefixed `✗ <check text>`, ambiguous notes listing their
candidates, unresolved notes printing their plain message (§10). This
is how a stranger tries the feature with no app: record a memo on a
phone, run one command, read what Director understood.

- Without `--apply`: nothing is written except `voice.json` beside the
  job (`<job>/voice.json`, or `./voice.json` in file mode) recording
  notes, proposals, engine and spend — the audit the app also reads.
- With `--apply`: every unblocked op is applied through
  `cutlist.apply_operations(..., source="voice")` into `tighten.json`
  (taste ops into `taste.json`), and the command prints
  `Applied <n> op(s); re-render with: director cut <job inputs>` — or,
  with `--render`, immediately re-runs `angles` for the job (the
  `app/cutjob.rerender` code path, §9.5) and prints the new duration.
  Blocked and ambiguous ops are never applied by `--apply`; they are
  printed and counted (`2 proposal(s) need a decision in the app`).
- `--yes` skips the over-`ESTIMATE_CONFIRM_S` confirmation; without it
  on a long note the CLI prints the estimate and asks `Transcribe and
  resolve for ≈$X? [y/N]` on stderr (a non-tty stdin counts as No and
  exits 0 having written nothing but the estimate line).

`voice.run(*, note_path, job_dir, model, max_usd, apply, allow_whisper)
-> dict` returns the `voice.json` payload:
`{"version": 1, "note_file": <name only>, "engine": "meta"|"whisper",
"duration_s": …, "spend_usd": …, "notes": [<ResolvedNote as dict>],
"applied": [<edit ids>]}`. File names only, never full local paths, in
anything written (the OPEN-DESIGN manifest rule).

## 9. The Mac app: open a cut job, hear the takes, talk back

### 9.1 `app/cutjob.py`: a cut job becomes an app project

```python
def build_project(job_dir: Path, out_dir: Path) -> dict:
    """cut job -> app/ui/demo project.json/project.js + media links.
    Returns the project dict. Raises CutJobError with a §10 message."""
```

Mapping (every app field the UI reads gets a value; fields with no
multicam meaning get the honest empty value, not a fake):

| App field | From the job |
|---|---|
| `title` | `cut.json` project name (`--name`, default `Director cut`); `client`/`shootDate` empty |
| `duration` | last shot `end_s` of `angles.json` (output timeline) |
| `video.mp4` | symlink to the job's `cut.mp4` (copy when symlink fails); `poster.jpg`/thumbs via `make_demo.stills` on it |
| `sections` | one per beat: `{name: topic, t: first shot out-start of the beat, d: span}`, or a single `Cut` section when untightened |
| `talk` | one per shot: `{id: "t<n>", t: start_s, d, src: "camera <label>" (or "black"), in: clip_in_s, topic: beat topic or line text, section, ref: [shot_id]}` — clicking a talk clip seeks and the Why panel (§9.2) fills |
| `takes` / `takeMeta` | from `tighten.json` beats: one row per attempt `{section: beat topic, kind: pick→"keeper_take" else its kind, topic, group: "B<id>", pick: bool, reason, conf: confidence, clip: "B<id>", in: source start, d, text}` — the exact row shape `app.js:renderWhy` already renders ("Spark · keeper take · confidence …", the alternates list) |
| `transcript` | words mapped through `source_to_output`: segments are `angles` lines restricted to kept words, each `{t, e, text, w: [[word, out_start, out_end], …]}` on the output timeline — the Words tab follows the *tightened* picture |
| `broll`, `cards`, `library` | `[]` (a multicam cut has none; the tracks render empty, which the UI already tolerates) |
| `findings` | `editor_notes` from `tighten.json` + unresolved voice notes, as `{t (output), src: "you", state: "open", title, body, check: "Voice note"}` |
| `spend` | from `cut.json`: `{total: spend_usd, rows: [[stage, 1, spend] per stage]}` so the spend popover works |
| `cutJob` | `{"dir": <job path as given>, "tightened": bool}` — the flag the UI uses to show voice/taste/re-render controls and hide Render-v1 controls that cannot apply |

`build_project` validates before writing anything: missing
`angles.json` → `CutJobError("Not a Director cut job: <dir> has no
angles.json (run director cut there first)")`; missing `cut.mp4` when
a render exists → the project still builds with `video.mp4` absent and
the player shows its existing empty state (reviewing takes and notes
must not require the render). `app/make_demo.py --cut-job DIR [--out]`
exposes it from the terminal.

### 9.2 Hearing Director's take picks

No new panel is invented: the existing Why tab already renders a talk
clip's Spark reason and its takes with alternates (`app.js:renderWhy`,
the `P.takes` rows). With a cut-job project, clicking any talk clip
shows `Director kept A1 — "second pass, first one stumbles on
'timecode'" (confidence 0.95)` and lists the alternates and removed
attempts of that beat under "Takes of this moment", each row gaining a
**Use this take** button when it is a usable alternate (the button
issues the §9.3 apply call with a single `swap_take` op — a click and
a voice note are the same operation through the same endpoint, which
is the shared edit path made visible). Beat topics label the sections
track, so the timeline itself reads as the list of beats. The takes
are heard *before* any voice work: opening the job and clicking
through is the review, voice is the accelerator.

### 9.3 Engine endpoints (the delta)

Legacy endpoints are untouched. Cut-job mode is entered by
`python app/engine.py --job DIR` / `app/main.py --job DIR` (builds the
project via `cutjob.build_project` into `app/ui/demo`, then serves as
today) or at runtime:

| Endpoint | Body / behaviour |
|---|---|
| `POST /api/job/open` | `{path}` → validates + builds the project, reloads the engine, returns `GET /api/project`'s payload. A bad dir → 400 with the `CutJobError` text. The path is the editor's own machine and the engine is localhost-only (§1.4); no other path-accepting endpoint is added. |
| `POST /api/voice/record` | `{audio_b64, mime, at_s}` → saves the recording under `demo/voice/`, extracts, transcribes (anchor = `at_s`), resolves, stores the proposal under an id like `/api/muse`'s, returns `{id, notes: [ResolvedNote…], ops: <flattened checked ops>, engine, usd, cached}` — a superset of the `/api/muse` response shape, so `proposalMsg` renders it with only the §9.4 additions. |
| `POST /api/voice/file` | `{name, audio_b64}` → the long-note path: probes duration, returns `{needs_confirm: true, estimate_usd, duration_s}` when over `ESTIMATE_CONFIRM_S` and `confirm` is not set; with `{…, confirm: true}` transcribes and resolves (no playhead anchors; §6.3 steps 2–5). |
| `POST /api/voice/apply` | `{id, pick?: [op indices], candidates?: {note_id: beat_id}}` → mirrors `Engine.accept`: applies the picked unblocked ops through `cutlist.apply_operations` (re-validated at apply time against the *current* `tighten.json` — a proposal made before another apply may be stale, and staleness blocks with `This cut changed since the proposal — ask again`, the engine's existing generation rule), writes `tighten.json` + `voice.json`, rebuilds the project (takes/transcript shift immediately; the video shifts on re-render), returns the project payload. 409 while a cut-job re-render runs. |
| `GET /api/taste` | `{entries}` for the open job. |
| `POST /api/taste/add` / `POST /api/taste/remove` | `{text}` / `{id}` → the updated entries. Typed taste entry exists so a preference does not have to be spoken to be recorded. |
| `POST /api/cutjob/rerender` | §9.5. 409 while one runs; `GET` returns `{state, message}` like `/api/render`. |

Proposals live in memory exactly like `Engine.proposals`, keyed by id
with the engine generation; a re-render or a project reload forgets
them, and applying a forgotten id gets the existing "That proposal is
gone" 400. Accept-all is the UI sending every unblocked index — no
separate endpoint, so there is one apply path, not two.

### 9.4 UI behaviour

All in the Muse tab and transport; the app's layout does not grow a
new view.

- **Mic button + hold `V`.** A mic button sits beside the play button.
  Press-and-hold (pointerdown/up, or holding `V` while the app has
  focus and no text field does) starts `MediaRecorder`
  (`audio/webm;codecs=opus` when offered, else the browser default);
  `at_s = video.currentTime` is captured at pointerdown. The button
  shows a live recording state with elapsed time; release stops,
  uploads to `/api/voice/record`, and the note appears as a card at
  the top of the Muse chat headed `Voice note · 0:42 · Meta
  transcription` (or `· local Whisper`), the `I heard: …` line, its
  ops with the existing checkboxes/checks rendering, and Accept /
  Reject / Accept-all buttons. Several recordings in one viewing are
  normal: cards stack in spoken order, each with its own playhead.
- **Microphone refused / unavailable.** `getUserMedia` missing
  (`navigator.mediaDevices` undefined — the WKWebView case) or a
  `NotAllowedError`: the mic button disables and a banner appears in
  the Muse tab with this exact text: `The microphone is not
  available in this window. Open this page in your browser at
  <url> to record by voice, or drop a voice memo file here.` — where
  `<url>` is `location.origin + "/app/ui/"`, shown as a copyable line
  (a WKWebView link click cannot be relied on to open a browser, so
  the address itself is the instruction). The banner and the drop
  target appear *before* any recording attempt fails silently: the
  `mediaDevices` check runs on load in cut-job mode.
- **Drop a memo.** Dragging an audio/video file anywhere onto the Edit
  view shows the existing modal idiom as a drop overlay (`Drop the
  voice note — Director will transcribe and resolve it`); on drop,
  `/api/voice/file` runs; long files get the confirm dialog (`This
  note is 12:40. Transcribing and resolving it costs about $0.11.
  Continue?`); the result renders as one card per resolved note
  grouped under a `Voice memo · <file name>` header, ambiguous notes
  listing their candidate beats as radio choices that feed the apply
  call's `candidates`.
- **Taste panel.** Under the Muse tab's chat, a `Taste` section lists
  the job's entries with a × per entry (remove endpoint) and a small
  add box. Accepting a note with `add_taste` ops adds them here
  immediately; the panel header reads `Standing preferences — read by
  Director on the next pick or note`.
- **Re-render.** In cut-job mode the Render button reads `Re-render
  cut`; it is enabled exactly when `edits_applied` has grown since the
  last render (the engine tracks the count at build/reload), and
  §9.5's progress replaces the Render-v1 dialog. Legacy Render
  controls are hidden in cut-job mode, never half-wired.

### 9.5 Re-rendering a cut job — `cutjob.rerender`

```python
def rerender(job_dir: Path, demo: Path) -> dict:
    """angles re-run over the edited tighten.json -> new cut.mp4."""
```

Reads the job's `sync.json`, canonical `words.json`, `angles.json`
(for `audio_label`, `min_shot_s`, `model`, grade path) and
`tighten.json`, then calls `director.angles.run(..., tighten_path=
job/tighten.json, render_path=<job>/cut.next.mp4)` — imported by
adding `src/` to `sys.path` inside `cutjob` (the engine already runs
beside the repo; when the import fails the endpoint answers 503
`Re-render needs the Director package: run the engine from the repo
venv`). On success `cut.next.mp4` replaces `cut.mp4` (the old file
moves to `cut.prev.mp4`, one generation kept), `angles.json` is the
re-run's own output written back to the job, the project rebuilds, and
the engine reloads — the app's existing "cut swapped under the UI"
path. The render runs on a worker thread behind the same busy gate as
`/api/accept` (409 while running). Crucially this is the *same* render
`director cut` performs (its angles stage re-runs by fingerprint when
`tighten.json` changes, TIGHTEN-DESIGN §11), so app edits and CLI
re-runs can be interleaved freely — one contract, one render, two
front doors.

## 10. Failure and edge cases (decided)

Exact user-visible strings; the builder does not paraphrase them.

| Case | Behaviour |
|---|---|
| No `MODEL_API_KEY`, no guestlist | Transcription falls back to local Whisper (§4.2) and every surface labels it: CLI `engine: whisper (local — nothing uploaded)`, app card `· local Whisper`. Resolution (Spark) unavailable → notes are returned heard-but-unresolved with `Muse is unavailable, so these notes are transcribed only. Type the change, or run the engine with the Meta client installed.` — heard text is never thrown away. |
| Neither Meta nor `mlx_whisper` | §4.2 `VoiceError`: `voice: cannot transcribe: the Meta client is unavailable (<error>) and mlx_whisper is not on PATH; install one of them, or type the note instead`. CLI exit 1; app shows it in the note card, no spinner left hanging. |
| Microphone refused in the app window | §9.4 banner verbatim, mic button disabled, drop target offered. Nothing records, nothing pretends to. |
| Note over 20 min | §4.1 refusal before spend: `voice: note is <n> min; the limit is 20 min — split it or trim the file`. |
| Estimate over `--max-usd` | §4.3 refusal (`voice: estimated $X exceeds --max-usd $Y; raise --max-usd`); in the app the confirm dialog *is* the gate and its button names the estimate. |
| A note Director cannot do (music, colour, new footage) | §5: `add_note` with `Director can't change the mix — noted for the edit` / `No such shot in this cut — noted`. Shown as a note, counted as understood, never as an applied edit. |
| A note that cannot be placed | `unresolved`, `I couldn't place that note in the cut`, heard text shown, zero ops. The editor can still type the same instruction into the Muse box. |
| A note that could be two beats | `ambiguous`, candidates listed with output times; apply waits for the editor's pick. CLI prints candidates and applies nothing for that note under `--apply`. |
| Job has no `tighten.json` | Opens fine; take ops resolve to the blocked message `This cut was not tightened, so there are no alternate takes to swap. Re-run director cut without --no-tighten.` Angle and note ops still work. |
| Stale proposal (cut changed since) | Apply re-validates; stale ops block with `This cut changed since the proposal — ask again` and nothing from that proposal is applied. |
| Applying would empty the cut | cutlist's batch refusal stands (`That would empty the cut`); the app shows it against the proposal, edits unchanged. |
| Recording with no speech | Transcription returns no turns → one unresolved note `I didn't hear any speech in that recording` (live) or the file equivalent; no Spark call is paid for. |
| Video note with no audio stream | §4.1 `voice: no audio in <file name>; record a voice memo or a video with sound`. |
| Spark resolve call fails | §6.1 `VoiceError`; transcript already in `voice.json`; app card shows the heard text with the error, CLI prints heard notes then the error. |
| Re-render import/runtime failure | 503 with §9.5's message, or the angles error verbatim; `cut.mp4` is untouched (the `.next` file is discarded) and the job's `tighten.json` edits remain applied and visible. |
| Taste file full / corrupt | Full: §7's refusal. Corrupt JSON on load: treated as empty with the warning `taste file unreadable, starting empty` surfaced in the app panel and CLI stderr — preferences are never a reason to lose a note. |

## 11. Tests

`tests/test_voice.py` (items 1–16) and `tests/test_cutjob.py` (items
17–24); numbering is continuous. Offline: stand-in `guestlist.meta`
modules in `sys.modules` returning fixed transcripts and fixed Spark
replies (the `test_angles.FakeMuseTests` pattern), synthetic tighten/
angles/sync fixtures built as dicts, `lavfi` media only where a test
truly needs a file (items 3, 24). No microphone is ever opened: the
recording endpoints are tested at the engine boundary with a tiny
base64 WAV made by ffmpeg at test time or written as raw PCM.

1. `test_extract_audio_command` — equals §4.1 element-for-element;
   a 21-minute probe stub refuses with the §10 limit message before
   any command runs.
2. `test_transcribe_meta_turns` — a fake `diarize` returning two turns
   → `Turn`s in seconds with speakers kept, spend from the per-second
   rate, `engine == "meta"`.
3. `test_transcribe_whisper_fallback_labels` *(ffmpeg for the wav)* —
   fake Meta raising `ImportError`, a stubbed `mlx_whisper` run
   writing segments JSON → turns built, `engine == "whisper"`; with
   neither available, the §4.2 `VoiceError` text is exact.
4. `test_estimate_note_usd` — 1200 s costs the transcribe rate plus the
   §4.3 resolve estimate; an over-cap run refuses before the fake
   diarizer is called.
5. `test_parse_spoken_timecode` — `at about a minute twenty` → 80.0,
   `1:20` → 80.0, `two minutes in` → 120.0, no timecode → `None`;
   a timecode past the cut's end falls through to quote matching with
   the §6.3 message.
6. `test_match_quote_unique_ambiguous_none` — a phrase said once
   anchors its beat; said twice → both candidates in order; said
   nowhere or one content word → no anchor.
7. `test_resolve_prompt_and_schema` — `RESOLVE_SCHEMA` op enum equals
   the §6.1 list; the prompt's CUT block carries beat pick/alternates,
   shot cameras, output-time transcript and taste entries; the
   DEVELOPER text contains "do not pick one quietly".
8. `test_resolve_with_fake_spark` — a fake `_structured` asserting
   `name="voice_resolve"` returns a swap + a taste string → the swap
   is checked through cutlist (unblocked, labelled), the taste is a
   separate `preference` op, spend is the fake's; a raising fake →
   `VoiceError` with the transcript preserved by the caller.
9. `test_verify_anchor_disagreement` — Spark names B2 while the quote
   matches B5's words → the op is re-targeted to B5 with the §6.4
   message; a note under the confidence floor and a note with no
   anchor both come back `unresolved` with zero ops.
10. `test_music_note_becomes_plain_note` — a fixed reply asking for a
    mix change is coerced per §5 to `add_note` with the exact
    can't-change-the-mix message; same for an unknown-footage ask.
11. `test_apply_through_cutlist_only` — `voice.run(..., apply=True)`
    with a fake pipeline over a synthetic job → `tighten.json`'s pick
    changed, `edits_applied` gained the voice entry, `voice.json`
    records the applied edit id; a blocked op in the same batch is
    reported and not applied.
12. `test_taste_add_remove_cap` — add/list/remove round-trip with
    sequential ids; the 51st add raises the §7 text; a corrupt taste
    file loads empty with the §10 warning.
13. `test_untightened_job_take_ops_blocked` — a job context with
    `tighten=None` → a swap proposal blocks with the §10
    not-tightened message while a `set_angle` against a shot still
    validates.
14. `test_global_ops` — "tighten every pause over a second" resolves
    to `set_pause_limit 1.0` with `beat_id -1`; "make it under three
    minutes" resolves to `retime_target 180` and applying it drops
    whole beats only (tighten's `apply_target` result recorded).
15. `test_visual_confirmation_downgrades` — a fake image call
    returning `confirmed: false` → the angle op becomes `add_note`
    with the §6.5 message; guestlist missing → the op survives with
    the soft frame-check-unavailable check.
16. `test_cli_voice_prints_heard_first` — `command_voice` with
    `voice.run` stubbed → stdout's first note line is the heard text,
    blocked ops print with `✗`, and without `--apply` no tighten file
    is touched (fixture mtime/content unchanged).
17. `test_cutjob_build_project_mapping` — a synthetic cut job →
    sections are the beats, talk rows carry shot ids and camera
    labels, `takes` rows mark the pick `keeper_take` with its reason,
    transcript words sit on output times (a removed pass's words
    absent), `cutJob.tightened` true.
18. `test_cutjob_untightened_and_bad_dir` — no `tighten.json` → one
    `Cut` section, empty takes, project still builds; a dir without
    `angles.json` raises the §9.1 `CutJobError` and writes nothing.
19. `test_engine_open_job` — engine with a temp job: `POST
    /api/job/open` returns the project with `cutJob` set; the legacy
    `/api/project` shape is unchanged for a demo project opened the
    old way.
20. `test_engine_voice_record_apply` — `POST /api/voice/record` with a
    tiny base64 WAV and fakes at the `voice` module boundary returns a
    stored proposal; `POST /api/voice/apply` swaps the pick in the
    job's `tighten.json`; applying the same (now consumed) id again
    gets the proposal-gone 400.
21. `test_engine_voice_file_confirm_gate` — a long fake file →
    `needs_confirm` with the estimate and no transcription call;
    with `confirm: true` the notes resolve.
22. `test_engine_taste_endpoints` — add via endpoint, list, remove;
    entries survive an engine reload (they live in the job, not the
    engine).
23. `test_engine_apply_stale_blocks` — a proposal made, then
    `tighten.json` edited on disk, then apply → stale message, no op
    applied.
24. `test_cutjob_rerender_synthetic` *(ffmpeg)* —
    `@unittest.skipUnless(shutil.which("ffmpeg"), ...)`. A synthetic
    tightened job (the tighten suite's two-pass fixture): swap the
    pick through the engine apply path, `cutjob.rerender` → new
    `cut.mp4` probes to the swapped take's duration, `cut.prev.mp4`
    kept, project rebuilt with the new pick shown. No Meta call, no
    microphone.

Gate: `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`
green (with the tighten build's tests). The UI's recording itself is
not unit-tested — no microphone in CI by design; it is exercised in
§12's live check in a real browser window, which is the only honest
place to test `getUserMedia`.

## 12. What the build also updates, non-goals, live check

**Provenance (at build time).** `docs/muse-code/BUILD-LOG.md` gains:

```markdown
# Build log: voice notes (`director voice`, app voice) (2026-11-03)

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `VOICE-BRIEF.md`: voice notes, taste file, app recording. |
| Design | Meta **Muse Code** on **`muse-wonder-1.0`** | `VOICE-DESIGN.md`: transcription + resolution, the shared cutlist ops, the cut-job app. |
| Build | Meta **Muse Code** on **`muse-spark-1.3`** | `src/director/voice.py`, `app/cutjob.py`, engine/UI voice, `tests/test_voice.py`, `tests/test_cutjob.py`. |
| Code review and live test | Claude Code, piloted by the owner | Real footage, below. |

Muse Code token use: TBD (the owner fills this from the run log).
Live test: TBD (the owner fills this from the real-footage run).
```

and the README's "Who built what" table gains:

```markdown
| Voice notes (`director voice`, talking to the app) | Designed by Muse Code on muse-wonder-1.0, built by Muse Code on muse-spark-1.3 |
```

The README also gains: how to open a cut job in the app
(`python app/main.py --job demo-job`, or Open in the running app), the
hold-`V`/mic flow with the browser fallback sentence, and the
`director voice memo.m4a --job demo-job --apply` quickstart. `AGENTS.md`
gains the §2 bullets. Model-call and token counts stay TBD until the
owner fills them from the run log.

**Non-goals (explicit).** *A second render engine* (cut jobs re-render
through `angles`; Render-v1 is untouched and legacy projects never see
a voice endpoint change). *Acting on music/colour/loudness* (noted,
never faked — §5). *Live transcription while speaking* (a note is
resolved when the key is released; streaming partials are a different
feature). *A cross-job taste store* (per-job file, copied forward by
hand — §7). *Speaker-attributed editing* (diarization speakers label
turns in `voice.json` for the editor's reading; no op keys off them).

**Live check (before done is claimed).** On the demo job from the
tighten live check: open it in the app (`python app/main.py --job
demo-job`); click three talk clips and read Director's pick reasons in
Why; hold `V` and say "use the other take here" on one beat and "lose
the part where I talk to the crew" on another; accept, re-render, and
watch the swapped beat play. Then open the same engine URL in a
browser, drop a 3-minute memo mentioning a timecode and a quoted
phrase, and confirm both notes land on the right beats. Finally run
`director voice memo.m4a --job demo-job` from a terminal and read the
heard-first output. Record the outcome, the transcription engine that
actually ran, and any defect in the BUILD-LOG section above.


