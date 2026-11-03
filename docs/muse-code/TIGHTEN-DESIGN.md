# Design: pick the takes, lose the flubs (`tighten`)

Designed by Muse Code on `muse-wonder-1.0` from
[TIGHTEN-BRIEF.md](TIGHTEN-BRIEF.md), under [AGENTS.md](../../AGENTS.md) and
[OPEN-DESIGN.md](OPEN-DESIGN.md). `OPEN-DESIGN.md` is the contract for
`cut.py`, `export.py` and `cut.json` even though that build is still in
flight in another checkout; this design slots into it at exactly the place
its §15 reserves. The build (Muse Spark) writes the code from this
document without further decisions. No code is written by this document.

Conventions that bind the build: Python ≥3.11, `from __future__ import
annotations`, type annotations throughout, `@dataclass(slots=True)` at
module boundaries, `guestlist` imported only inside the functions that call
Meta models and guarded there, API key from `MODEL_API_KEY` only (handled
inside `guestlist`), no `-contributor` model ids, tests offline with
`unittest` and never touching the Meta API or Whisper, no media files
committed, no client names, people's names or local footage paths in code,
tests or docs.

## Build order: this feature is built first

Two features are designed in this round — this one and voice notes
([VOICE-DESIGN.md](VOICE-DESIGN.md)). **Tighten is built first, voice
second.** The reason is a dependency, not a preference: both features are,
at bottom, editors of one list — *what stays in the cut*. This design
creates that list (`tighten.json`), the module that owns it
(`src/director/cutlist.py`, §4), and the one edit path that mutates it
(`cutlist.apply_operations`, §5.4). Voice notes then become a way of
*proposing* operations against a contract that already exists and is
already rendered by `angles`; without tighten there is nothing for a voice
note about takes to act on, and the app has no take picks to show. Each
design is still buildable in a single Muse Spark run: this one touches
five files and two test files (§2), and its last test (§14 item 24) is the
synthetic end-to-end proof the voice build then relies on.

## Why this

A talking recording is never the video. The speaker restarts sentences,
does two or three full passes, pauses to think, talks to the crew. Today
`angles` cuts the whole shared timeline under the uncut audio, so every
flub ends up in the cut, and the owner's hackathon demo — a 3-minute
founder talk recorded as 2–3 full passes on 2–3 cameras — cannot be cut by
Director at all without this stage. Choosing which take to keep is also
where an editor's taste lives, and the slowest part of the owner's week.

The day-1 spike (`docs/DIRECTOR-PLAN.md`, `scripts/director_spike_takes.py`,
`docs/spike-takes-day1-lines.json`) already measured how to do this: Muse
Spark given **numbered Whisper lines beside the video** scored
duration-weighted recall 0.85 / precision 0.94 against hand picks; asked
for its own times it scored 0.76 / 0.74 and drifted up to 70 s. Its one
structural failure was granularity — a coarse Whisper line spanning a flub
(T03 lost its second half). This design therefore keeps the spike's winning
shape — *Spark judges, Whisper words keep time* — and fixes the
granularity (§6.1), the clock (§8), and the render (§10).

## 1. Inputs as they actually are

Verified against this repository and the installed packages. The build
must not re-derive it differently.

### 1.1 What tighten consumes and produces in the pipeline

`director cut` (OPEN-DESIGN) runs stages in the order `sync → words →
grade → angles → export → reframe → qc`. Tighten inserts **one new stage,
`tighten`, between `words` and `grade`**, so the order becomes:

```
sync → words → tighten → grade → angles → export → reframe → qc
```

It needs only the words bridge's output, so placing it before `grade`
fails the job fast — before any paid grade/angle work — when no usable
picks can be made. It consumes:

- `sync.json` — exactly as OPEN-DESIGN §1.1 specifies. Clip time `t` is
  timeline time `t + offset_s`; the reference clip is label `A` at
  offset 0. Unplaced clips (`offset_s: null`) cover nothing.
- `words.json` — the job's canonical word file from the words bridge
  (OPEN-DESIGN §6): `{"words": [{"word", "start", "end"}, …]}` in
  **shared-timeline seconds**, loaded with `angles.load_timed_words`,
  which returns `list[angles.TimedWord(text, start_s, end_s)]` with raw
  punctuated text kept. When the editor passed `--words` explicitly, that
  file is in timeline times by the existing `angles` contract and is used
  in place.

Standalone (§12), the same inputs are named directly:
`director tighten --sync sync.json --words words.json`.

### 1.2 How `angles` renders today, and what that forces

`angles.run(*, sync_path, words_path, out_path, audio_label, render_path,
min_shot_s, model, no_muse, grade_path=None)` splits words into lines
(sentence breaks, pauses over 0.6 s, 8 s cap), picks a camera per line,
and `angles.render_commands` renders one video part per shot, then joins
the parts by stream copy under **one uncut audio span**
(`-ss <audio_in> -t <span>` from the audio clip). Shot times are
shared-timeline seconds; `angles.frame_grid` snaps shot edges to whole
output frames from the first cut so the joined picture is exactly as long
as the audio span. Every edge sits inside an inter-word gap by
construction (midpoints between lines). A tightened cut breaks the "one
uncut audio span" assumption and nothing else — §10 keeps the per-shot
video parts, the frame grid and the grade LUT chain, and replaces only
the audio side.

### 1.3 The installed `guestlist` call this feature adds

One new Meta call shape, the same one the spike used and the one
OPEN-DESIGN §9.3 uses for whole-cut QC:

```python
from guestlist.meta import cache, spark   # inside the calling function only
spark.video_json(video: str | Path, *, schema: dict, name: str,
                 developer: str, prompt: str,
                 model: str = "muse-spark-1.3", effort: str = "medium",
                 max_output_tokens: int = 16000,
                 max_usd: float | None = None) -> SparkResult
# SparkResult.data: dict, .usd: float, .cached: bool, .seconds, .usage, .meta
# Content-cached by (video hash, model, developer, prompt, schema, effort):
# re-running the same window proxy with the same prompt costs $0.
# spark.check_model refuses any model id ending in "-contributor".
```

Pricing facts (from `guestlist.meta.pricing`, 2026-10-26 list, already
mirrored in OPEN-DESIGN §3): `muse-spark-1.3` $1.25 input / $0.15 cached /
$4.25 output per 1M tokens; SAM is not used here. `guestlist.meta.cache`
also enforces a global hard stop (`cache.guard`, `BUDGET_USD`); that is a
backstop, never this feature's per-job gate (§7.4).
`guestlist.meta.transcribe.diarize` is **not** used: it returns
turn-level times with no word clock, and §1.1's words already are the
clock.

### 1.4 The two timelines (read this before §8–§10)

Tightening creates a second timeline, and every later section names which
one it means:

- **Source timeline** — the shared sync timeline. Words, utterances,
  beats, attempts, kept and removed spans, and camera coverage all live
  here. `tighten.json` is written entirely in source seconds.
- **Output timeline** — the tightened cut the editor watches. Output
  second 0 is the first kept span's start; each kept span contributes
  exactly its (frame-snapped, §10.2) duration, in order, with nothing
  between spans. `angles.json` shot `start_s`/`end_s`, the rendered
  `cut.mp4`, the exported timelines and the app's playhead all live here
  when a job is tightened.

`cutlist.source_to_output` / `output_to_source` (§4) are the only mapping
functions; nothing else in the codebase converts between the two by hand.
OPEN-DESIGN's QC check 2 (`duration_match`: rendered duration vs
last-shot-end − first-shot-start) keeps working unchanged, because in a
tightened job the shots and the render are both on the output timeline.

## 2. Files changed

| File | Change |
|---|---|
| `src/director/cutlist.py` | New. The shared "what stays" contract: dataclasses, `tighten.json` load/write, span recomputation, time mapping, and `apply_operations` — the one edit path that tighten, `angles` and the voice feature all go through. Pure (no ffmpeg, no guestlist). |
| `src/director/tighten.py` | New. Utterances, windows, proxies, the Spark pick call, the rule fallback, target-length drops, `run()`. |
| `src/director/angles.py` | `run()` gains `tighten_path: Path \| None = None`; when set, lines are built per kept span, shots are emitted on the output timeline with source times recorded, angle overrides from `tighten.json` are honoured, and the render cuts audio per kept span (§10). Without it, behaviour and ffmpeg commands are byte-identical to today. |
| `src/director/cut.py` | The `tighten` stage (OPEN-DESIGN §7 gains one stage; §11 of this design gives the exact delta): stage order, fingerprint, manifest section, CLI flags, console line. |
| `src/director/cli.py` | New `command_tighten(args)` + `tighten` subparser; `angles` gains `--tighten`; `cut` gains the §11.3 flags. |
| `tests/test_cutlist.py` | New. §14 items 1–8. |
| `tests/test_tighten.py` | New. §14 items 9–24 (items 21–23 extend `angles` behaviour; they live here so the feature's tests are one file). |
| `README.md` | At build time: the tighten paragraph and quickstart lines (§15). One "Who built what" row. |
| `AGENTS.md` | At build time: two Layout bullets (`cutlist.py`, `tighten.py`) and the `angles.py` bullet gains "consumes `tighten.json` when given". |
| `docs/muse-code/BUILD-LOG.md` | At build time: one TIGHTEN section (§15). |

`sync.py`, `grade.py`, `reframe.py`, `export.py` (as designed in
OPEN-DESIGN) and `app/` are **not modified** by this build. The voice
build is the one that touches `app/`.

Imports:

- `cutlist.py`: stdlib only (`dataclasses`, `json`, `pathlib`) plus
  `ghostcut.config` (`read_json`, `write_json`). It never imports
  `angles` (see the cycle note below): word lists arrive as objects
  with `text`/`start_s`/`end_s` attributes. No ffmpeg, no guestlist,
  no numpy — tests import it anywhere.
- `tighten.py`: stdlib (`difflib`, `json`, `math`, `re`, `shutil`,
  `subprocess`, `dataclasses`, `pathlib`) plus
  `ghostcut.config` (`read_json`, `write_json`),
  `ghostcut.ffmpeg_tools` (`ToolingError`, `probe_media`,
  `extract_media_stats`, `run_command`), `from . import angles as
  angles_mod`, `from . import cutlist`. `guestlist` appears only inside
  `pick_window_with_muse` (§7.3).
- `angles.py` gains `from . import cutlist` at module top (cutlist imports
  `TimedWord` from angles — to avoid a cycle, `cutlist` must therefore
  **not** import angles at module top: it takes words as
  `Sequence[TimedWord]`-shaped `(text, start_s, end_s)` triples via a
  local `WordLike` protocol, and `tighten.py` passes `angles.TimedWord`
  objects in. The build keeps this exact arrangement.)

## 3. Constants

All in `tighten.py` unless stated. Every value is decided here; the build
does not tune them.

| Constant | Value | Why |
|---|---|---|
| `MAX_UTTERANCE_S` | `6.0` | The spike's granularity fix: utterances cap at 6 s (angles lines cap at 8 s), split at the longest inter-word pause. |
| `UTTERANCE_PAUSE_S` | `1.2` | An inter-word gap at or above this always ends an utterance, even mid-sentence — it is where restarts and breaths live. |
| `WINDOW_MAX_S` | `240.0` | One Spark window covers at most 4 minutes of source timeline. The spike's 22-minute single call is chunked at this size so a 3-minute demo is one call and a 30-minute talk is eight. |
| `WINDOW_MAX_UTTERANCES` | `60` | The other window bound, whichever comes first. |
| `WINDOW_TAIL_S` | `15.0` | When a window would end mid-flow, its end moves back to the largest inter-word gap inside the last 15 s (§6.3), so beats rarely straddle windows. |
| `PROXY_FPS` | `5` | Mirrors `reframe.PROXY_FPS`. Delivery is judged mostly by ear; 5 fps still shows eyes-on-lens and a sneeze. |
| `PROXY_SHORT_SIDE` | `540` | Mirrors `reframe.PROXY_SHORT_SIDE` and OPEN-DESIGN's QC proxy. |
| `PROXY_AUDIO_KBPS` | `64` | Mono AAC for the proxy soundtrack (the spike used 64k). |
| `INPUT_TOKENS_PER_S` | `300` | Conservative input-token rate for the pre-call estimate; above the ≈272 tok/s measured for Spark video in `docs/DIRECTOR-PLAN.md`, so the estimate errs high. Same value as OPEN-DESIGN's `QC_INPUT_TOKENS_PER_S`. |
| `MAX_OUTPUT_TOKENS` | `16000` | `max_output_tokens` for the pick call and the output term of its estimate. |
| `TOKEN_RATES_USD_PER_1M` | `{"muse-spark-1.3": (1.25, 4.25), "muse-wonder-1.0": (4.00, 20.00)}` | (input, output) list rates mirrored from `guestlist.meta.pricing.TOKEN_RATES` so the estimate needs no guestlist import. An unlisted `--model` falls back to the spark rates with the warning `tighten_estimate_default_rates:<model>`. |
| `DEFAULT_PAUSE_LIMIT_S` | `0.8` | `--pause-limit` default: pauses longer than this inside a kept attempt are shortened (§8.2). |
| `PAUSE_KEEP_S` | `0.35` | What a shortened pause keeps: 0.18 s after the previous word, 0.17 s before the next. A breath survives; the wait does not. |
| `HEAD_PAD_S` | `0.10` | Kept-span head pad when there is no previous word (start of the recording). |
| `TAIL_PAD_S` | `0.15` | Kept-span tail pad when there is no next word. |
| `MIN_KEPT_SPAN_S` | `0.35` | Below this a kept sub-span is not readable; §8.3 merges or warns. |
| `DEAD_AIR_S` | `2.0` | Word-free gaps at or above this are labelled `dead_air` in the removed list even between two kept attempts. |
| `BEAT_SIM_MIN` | `0.45` | Rule-fallback beat boundary: normalised-text similarity below this starts a new beat (§7.5). |
| `WINDOW_RULE_FALLBACK_SHARE` | `0.25` | If mechanical repair would have to fix more than this share of a window's utterances, the whole window falls back to the rule (§7.4 step 6). |
| `CROSSFADE_S` (in `angles.py`) | `0.008` | Audio fade in/out at every tightened join (§10.3). Long enough to kill a click, short enough to never eat a word tail — and edges are in pauses by construction. |
| `AUDIO_SR` (in `angles.py`) | `48000` | Sample rate every tightened audio part is resampled to, so the concat join is sample-consistent. |

## 4. Data classes

All `@dataclass(slots=True)`. Times are source-timeline seconds (float),
money is USD (float). JSON stores times rounded to 3 decimals and money
to 4, matching every other stage.

In `cutlist.py` — the shared contract. The voice design constructs and
mutates these only through this module's functions:

```python
@dataclass(slots=True)
class WordSpan:            # one kept or removed piece of the source timeline
    id: str                # "K0".. for kept, "R0".. for removed (see id rule below)
    start_s: float
    end_s: float
    beat_id: int           # -1 when the span belongs to no beat (dead air between beats)
    attempt_id: int        # -1 likewise; attempts are numbered per beat (A0.. within the beat in JSON)
    utterance_ids: list[int] = field(default_factory=list)
    kind: str = "keeper"   # kept: "keeper" | removed: a REASON_KIND below
    reason: str = ""

@dataclass(slots=True)
class Attempt:
    id: int                # per-beat index, 0-based, in source order
    utterance_ids: list[int]
    start_s: float
    end_s: float
    text: str
    verdict: str           # "pick" | "alternate" | "remove"
    kind: str              # ATTEMPT_KINDS below
    reason: str
    confidence: float      # 0..1, clamped on load

@dataclass(slots=True)
class Beat:
    id: int                # B0.. in source order (JSON stores the int)
    topic: str
    attempts: list[Attempt] = field(default_factory=list)
    pick_attempt: int = -1  # index into attempts; -1 = no usable attempt
    status: str = "kept"   # "kept" | "dropped_target" | "dropped_editor" | "no_usable_attempt"
    keep_priority: int = 3 # 1..5 from Spark (5 = essential); rule mode: 3, first/last beat 5
    reason: str = ""       # why the pick won, one line, editor-readable
```

Id rule (so spans can be rebuilt without renumbering chaos): kept spans
are `K<position>` and removed spans `R<position>` *by their order in
their own lists*, reassigned on every `recompute_spans` call. Voice and
app code therefore never store span ids across a recompute; they store
**beat ids and attempt ids**, which are stable (§5.4 rule 1).

```python
ATTEMPT_KINDS = ("keeper", "alternate", "flub", "restart", "crew_talk",
                 "aside", "setup", "dead_air")
REASON_KINDS = ("alternate", "flub", "restart", "crew_talk", "aside",
                "setup", "dead_air", "pause_trim", "target_drop",
                "editor_drop", "too_short", "unclassified")
```

In `tighten.py`:

```python
class TightenError(RuntimeError):
    """A tighten step failed in a way the operator must see.
    The message always starts with 'tighten: '."""

class MuseUnavailable(RuntimeError):
    """guestlist (the Meta API client) cannot be imported."""

@dataclass(slots=True)
class Utterance:
    id: int                # U0.. in source order (JSON stores the int)
    start_s: float
    end_s: float
    text: str
    word_start: int        # index into the words list
    word_end: int          # exclusive

@dataclass(slots=True)
class Window:
    id: int
    utterance_ids: list[int]   # core utterances this window owns
    start_s: float             # proxy span (padded, §7.2)
    end_s: float
```

## 5. `tighten.json`: the one list of what stays

Written by `tighten.run` and by `cutlist.write_tighten`; read by
`cutlist.load_tighten`. This file is the shared data contract for the
whole round of work: `angles` renders from it, `cut` manifests it, and
the voice feature (§5.4, VOICE-DESIGN) edits it. Nothing else writes it.

```json
{
  "version": 1,
  "sync": "sync.json",
  "words": "words.json",
  "audio": {"label": "C", "clip_path": "lav.wav", "offset_s": 178.746},
  "method": "muse",
  "model": "muse-spark-1.3",
  "pause_limit_s": 0.8,
  "target_s": 180.0,
  "spend_usd": 0.1712,
  "warnings": ["window 2 used rule picks: Spark reply covered 31% badly"],
  "duration_in_s": 612.400,
  "duration_out_s": 181.240,
  "utterances": [
    {"id": 0, "start_s": 12.040, "end_s": 15.320,
     "text": "So the problem with multicam is timecode.", "word_start": 0, "word_end": 7}
  ],
  "beats": [
    {"id": 0, "topic": "the timecode problem", "status": "kept",
     "keep_priority": 4, "pick_attempt": 1,
     "reason": "second pass, first one stumbles on 'timecode'",
     "attempts": [
       {"id": 0, "utterance_ids": [4, 5], "start_s": 40.100, "end_s": 47.900,
        "text": "So the problem with multicam is time— let me start again",
        "verdict": "remove", "kind": "restart",
        "reason": "abandons the line and restarts", "confidence": 0.92},
       {"id": 1, "utterance_ids": [6, 7], "start_s": 48.300, "end_s": 56.100,
        "text": "So the problem with multicam is timecode drifting between cameras.",
        "verdict": "pick", "kind": "keeper",
        "reason": "clean delivery, eyes on lens", "confidence": 0.95},
       {"id": 2, "utterance_ids": [9], "start_s": 57.000, "end_s": 61.200,
        "text": "So the problem with multicam is timecode drifting between cameras.",
        "verdict": "alternate", "kind": "alternate",
        "reason": "usable, but flatter energy than the pick", "confidence": 0.81}
     ]}
  ],
  "kept_spans": [
    {"id": "K0", "start_s": 48.210, "end_s": 51.020, "beat_id": 0, "attempt_id": 1,
     "utterance_ids": [6], "kind": "keeper", "reason": "clean delivery, eyes on lens"},
    {"id": "K1", "start_s": 51.370, "end_s": 56.190, "beat_id": 0, "attempt_id": 1,
     "utterance_ids": [7], "kind": "keeper", "reason": "clean delivery, eyes on lens"}
  ],
  "removed_spans": [
    {"id": "R0", "start_s": 40.055, "end_s": 48.210, "beat_id": 0, "attempt_id": 0,
     "utterance_ids": [4, 5], "kind": "restart", "reason": "abandons the line and restarts"},
    {"id": "R1", "start_s": 51.020, "end_s": 51.370, "beat_id": 0, "attempt_id": 1,
     "utterance_ids": [], "kind": "pause_trim", "reason": "pause 1.42s shortened to 0.35s"},
    {"id": "R2", "start_s": 56.190, "end_s": 57.090, "beat_id": 0, "attempt_id": 2,
     "utterance_ids": [], "kind": "dead_air", "reason": "no words for 2.3s"}
  ],
  "angle_overrides": [
    {"beat_id": 0, "camera": "B", "reason": "editor: go to the wide here"}
  ],
  "edits_applied": [
    {"id": "v3", "source": "voice", "op": "swap_take", "beat_id": 0,
     "attempt_id": 2, "at": "2026-11-03T21:14:00", "why": "use the other take"}
  ],
  "target": {"requested_s": 180.0, "before_s": 204.500, "after_s": 181.240,
             "met": true, "dropped_beat_ids": [7]}
}
```

Field rules (decided; `version` bumps on any incompatible change):

- `method` is `"muse"`, `"rule"` or `"mixed"` (per-window fallbacks,
  §7.4). `target_s` is `null` when no `--target` was given; `target` is
  then `null` too. `angle_overrides` and `edits_applied` start empty and
  are owned by the voice feature; `tighten.run` preserves both verbatim
  when it re-runs over an existing file (§11.2), and never invents them.
- Every utterance belongs to exactly one attempt; every attempt to
  exactly one beat; beats are in source order and never overlap. A beat
  with `pick_attempt >= 0` has `status "kept"` unless the editor or the
  target pass dropped it. No beat is ever dropped silently: a dropped
  beat's status and its attempts' kinds say why, and its spans appear in
  `removed_spans`.
- `kept_spans` are in source order, non-overlapping, and every edge sits
  in an inter-word gap (§8.1). `removed_spans` plus `kept_spans` tile
  `[first kept edge, last kept edge]` exactly — the removed list is the
  complement, classified (§8.4), not a second opinion.
- `span_exclusions` and `editor_notes` (the §5.2 `drop_span`/`add_note`
  state) are present with `[]` defaults when empty; older readers may
  ignore both. `duration_out_s` is the sum of kept-span durations
  *before* frame snapping; the rendered duration may differ by under
  one frame per span (§10.2) and `cut`'s QC measures the real thing.

### 5.1 `cutlist` functions (every public function)

| Signature | Job |
|---|---|
| `load_tighten(path: Path) -> dict` | `read_json`; require a dict with `version == 1`, list `beats`, `kept_spans`, `removed_spans`, else `ValueError(f"Not a tighten.json: {path}")`. Returns the payload dict (payloads stay dicts at rest, dataclasses in flight — the repo's stage convention). |
| `write_tighten(path: Path, payload: dict) -> None` | Round money/times per §4 and `write_json`. |
| `utterances_of(payload: dict) -> list[Utterance-like]` | The payload's utterances as `(id, start_s, end_s, text, word_start, word_end)` triples the span builder consumes. |
| `recompute_spans(payload: dict, words, *, pause_limit_s: float) -> dict` | Pure. Rebuild `kept_spans`, `removed_spans`, `duration_out_s` from the beats/picks/statuses in `payload` and the word list, per §8. Returns the mutated payload. This is the function every edit path calls after changing a pick. |
| `source_to_output(payload: dict, t_s: float) -> float \| None` | Output time of a source time inside a kept span; `None` when the moment was removed. |
| `output_to_source(payload: dict, t_s: float) -> float` | Source time of an output (playhead) time: the kept span containing it, offset within the span. Past the end → the last kept span's end. |
| `beat_at_source(payload: dict, t_s: float) -> int \| None` | Beat whose pick (or, failing that, whose attempts) contains the source time. |
| `beat_at_output(payload: dict, t_s: float) -> int \| None` | `beat_at_source(output_to_source(...))`. The app and voice resolver use this one. |
| `apply_operations(payload: dict, words, ops: list[dict], *, source: str, covers: dict[int, list[str]] \| None = None) -> tuple[dict, list[dict]]` | **The one edit path** (§5.4). Validates each op, applies the unblocked ones in order, recomputes spans once at the end, appends to `edits_applied`, returns `(payload, checked_ops)`. |
| `validate_operation(payload: dict, op: dict, *, covers=None) -> list[dict]` | The checks for one op: `[{"ok": bool, "text": str, "hard": bool}, …]`, the same shape `app/muse.check` returns, so the app renders both identically. |

### 5.2 Operation vocabulary (shared with voice)

`apply_operations` accepts exactly these ops. The voice design proposes
them; tighten itself emits none. Fields not used by an op take the
sentinels `""` (strings), `-1` (numbers), matching `app/muse.OP_SCHEMA`.

| `op` | Fields | Effect |
|---|---|---|
| `swap_take` | `beat_id`, `attempt_id` | The named attempt becomes the beat's pick; the old pick becomes `alternate`. Beat status → `kept`. |
| `drop_beat` | `beat_id` | Beat status → `dropped_editor`; its pick's spans move to removed as `editor_drop`. |
| `restore_beat` | `beat_id` | Status `dropped_editor`/`dropped_target` → `kept` (a `no_usable_attempt` beat cannot be restored — there is nothing to restore). |
| `drop_span` | `span_id` (`"K3"`) | That one kept span is removed (`editor_drop`). Implemented as a per-span exclusion list `payload["span_exclusions"]` (span source-range keyed, see rule 4 below), because spans are rebuilt on every recompute. |
| `restore_span` | `span_id` | Removes the matching exclusion. |
| `set_angle` | `beat_id`, `camera` | Upserts `angle_overrides` for the beat (§10.4). |
| `clear_angle` | `beat_id` | Removes the beat's override. |
| `set_pause_limit` | `pause_limit_s` | Changes the job's pause limit and re-trims every kept attempt. |
| `add_note` | `at_s` (source), `text` | No timeline effect; appended to `payload["editor_notes"]` (`{"at_s", "text", "source"}`) for the app's review list. |

### 5.3 Checks (mechanical; these decide, the model proposes)

`validate_operation` runs, in order, and any `hard` failure blocks the op
(the op is returned in `checked_ops` with `"blocked": true` and is not
applied):

1. The op name is in the table above, else `Unknown operation '<op>'`.
2. `swap_take`/`drop_beat`/`restore_beat`/`set_angle`/`clear_angle`: the
   beat exists, else `No beat B<id> in this cut`. `swap_take`: the attempt
   exists in that beat (`No attempt A<id> in beat B<id>`), its verdict is
   not `remove` and its kind not in (`flub`, `restart`, `crew_talk`,
   `aside`, `setup`) — swapping *to* a flub is blocked with
   `Attempt A<id> is marked <kind>: '<reason>'`, because the editor
   almost certainly means the alternate; the UI offers alternates first.
   Swapping to the current pick is a no-op that passes with the check
   text `Already the pick`.
3. `set_angle`: when `covers` (beat id → camera labels covering every
   line of the beat's pick, computed by the caller from sync + lines) is
   given, the camera must be in it, else
   `Camera <X> does not cover beat B<id>`. Without `covers` the check is
   skipped and `angles` re-validates at render (§10.4) — the CLI voice
   path always passes `covers`; a caller that cannot must say so in its
   own output.
4. `drop_span`: the span exists and is a kept span. Dropping the *last*
   kept span of a beat is blocked (`That is the whole of beat B<id> —
   drop the beat instead`): a beat is kept whole or dropped whole, never
   hollowed out by accident. (Trimming *inside* a take is the pause
   rule's job, not a span drop.)
5. `set_pause_limit`: `0.30 ≤ pause_limit_s ≤ 3.00`, else
   `Pause limit <x>s is outside 0.30–3.00s`.
6. After all ops: at least one kept span remains, else the whole batch is
   refused and nothing is applied (`That would empty the cut`).

### 5.4 The one-edit-path rules

1. **Beat and attempt ids are stable; span ids are not.** Edits name
   beats/attempts (or, for `drop_span`, a span whose *source range* is
   recorded in `span_exclusions` as `{"start_s", "end_s", "reason"}` and
   re-matched after each recompute by exact rounded range). A recompute
   that finds an exclusion matching no kept span drops it with the
   warning `span_exclusion_stale:<start>-<end>` in the returned checks —
   never silently.
2. Every successful `apply_operations` appends one entry per applied op
   to `edits_applied` (`{"id": "e<n>" sequential, "source", "op", …op
   fields, "at": ISO-8601 local, "why"}`) and bumps nothing else. Undo in
   the app is "remove that entry's effect by applying the inverse op"
   (swap back, restore), which is itself recorded — the file is an audit
   trail, not a stack.
3. `recompute_spans` is the *only* function that writes `kept_spans` /
   `removed_spans`. Tighten's Spark path, the rule fallback, target
   drops and editor edits all funnel through it, so the render can never
   disagree with the file about what stays.

## 6. Grouping: words → utterances → beats

### 6.1 Utterances — `build_utterances`

```python
def build_utterances(words: list[angles_mod.TimedWord]) -> list[Utterance]:
    """Sentence/pause units of at most MAX_UTTERANCE_S, numbered U0... Pure."""
```

Walk the words once (the `angles.split_lines` walk, with two changes).
Start a new utterance when any holds: the previous word ends a sentence
(`angles._sentence_end`); the inter-word gap is `> angles.PAUSE_S` (0.6 s,
the existing line rule); the gap is `>= UTTERANCE_PAUSE_S` (1.2 s — the
same condition, stated separately because it is the restart boundary the
spike missed); or adding the word would exceed `MAX_UTTERANCE_S`, in
which case split the accumulated run at its longest inter-word pause
(`angles._longest_pause_split`), exactly as `split_lines` does for its
8 s cap. The result is strictly finer than `angles` lines: no utterance
spans a flub that begins after a breath, which is the T03 failure. Text
is the words joined with single spaces, punctuation kept — sentence
detection and the Spark prompt both need it.

Zero words → `TightenError("tighten: no words in <words file name>; "
"the words stage produced an empty transcript")`. (In a `cut` job the
words stage already guarantees ≥1 word; standalone tighten checks
because the editor named the file.)

### 6.2 Beats and attempts (the vocabulary)

A **beat** is one thing the speaker meant to say once: a sentence or a
short run of sentences forming one point. An **attempt** is one go at a
beat — a contiguous run of utterances. A beat has one or more attempts;
exactly one is the pick, the other usable ones are alternates, the rest
are removed with a kind. Two full passes over the whole talk therefore
appear as the same beats in the same order, each with (usually) two
keeper-grade attempts — which is precisely the demo footage's shape, and
why attempts, not utterances, are the unit an editor swaps.

### 6.3 Windows — `build_windows`

Spark is called per window, never on the whole recording at once (the
spike's single 22-minute call is the cost and drift profile to avoid):

```python
def build_windows(utterances: list[Utterance]) -> list[Window]:
    """Greedy windows of <= WINDOW_MAX_S and <= WINDOW_MAX_UTTERANCES. Pure."""
```

Greedy from the first unowned utterance; a window ends at the first of
the two bounds. Then the end is *retreated*: if the gap after the last
core utterance is `< DEAD_AIR_S` and an inter-word gap of at least 0.6 s
exists inside the window's last `WINDOW_TAIL_S`, the window ends at the
utterance before that gap instead (those utterances start the next
window). Each window's prompt also carries, as context only (§7.2), the
previous window's last beat topic and pick text, so a beat that does
straddle a boundary is recognised as a continuation rather than a new
topic. Window proxy spans pad the core span by 1.0 s on each side,
clamped to the video clip's coverage (§7.2).

## 7. Picking the takes

### 7.1 What Spark sees, and why (decided)

Per window, Spark gets **the window's proxy video (picture + production
audio) and the numbered utterances as text** — the spike's mode B,
unchanged in shape. It judges delivery from sound and picture (a stumble
Whisper smoothed over, eyes off lens, a sneeze), and locates everything
by utterance number; the words keep time. It does **not** get: camera
originals (never uploaded), per-line stills (the `angles` input — a
still cannot show a stumble), or the other cameras (which angle is a
later stage's question; the take is the same event on every camera). One
call per window, `spark.video_json`, name `tighten_picks`.

### 7.2 The window proxy — `window_proxy_command`, `make_window_proxy`

The proxy's picture comes from the **video source clip** and its sound
from the **production audio clip**, aligned on the shared timeline:

- Video source: the sync reference clip when it has picture
  (`width > 0` in `sync.json`), else the first placed clip in label order
  with picture. None → the window cannot be judged by Spark and falls
  back to the rule (§7.5) with the warning
  `tighten_window_no_video:<window id>`; the job still completes.
- A window the video source does not cover (its placed span does not
  contain the padded window span) likewise falls back, per window.

```python
def window_proxy_command(*, video_path: str, video_in_s: float,
                         audio_path: str, audio_in_s: float,
                         duration_s: float, out: Path) -> list[str]:
    return ["ffmpeg", "-v", "error", "-y",
            "-ss", f"{video_in_s:.3f}", "-i", video_path,
            "-ss", f"{audio_in_s:.3f}", "-i", audio_path,
            "-t", f"{duration_s:.3f}",
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", f"fps={PROXY_FPS},scale=-2:{PROXY_SHORT_SIDE}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "1",
            "-b:a", f"{PROXY_AUDIO_KBPS}k",
            "-movflags", "+faststart", str(out)]
```

where `video_in_s = max(0, window_start - video_clip.offset_s)` and
`audio_in_s = max(0, window_start - audio_clip.offset_s)` (clip-local
ins on the shared timeline). When the audio clip *is* the video source,
the same list is used with both ins from that clip (two inputs, one file
— deliberate, so there is exactly one proxy code path). Written to
`<out parent>/tighten-windows/window-<id:03d>.mp4`; `make_window_proxy`
runs it through `ghostcut.ffmpeg_tools.run_command`. A `ToolingError`
here degrades that window to the rule with
`tighten_window_proxy_failed:<window id>` — a proxy defect must never
masquerade as an editorial judgement or kill the job.

Portrait sources: `scale=-2:540` sizes by height; a 9:16 phone clip
yields a 540-wide proxy. That is accepted (Spark reads it fine) and no
second scale expression is introduced.

### 7.3 The Spark call — developer prompt, user prompt, schema

Developer prompt (verbatim, stored as `DEVELOPER` in `tighten.py`):

> You are Muse, the assistant editor inside GhostCut Director. You are
> watching one window of a raw talking-head recording — the speaker
> records the same points several times, restarts sentences, talks to
> the crew, and pauses to think. Below the video is the window's
> transcript as numbered utterances with times; the transcript may
> mishear words, so judge what was meant from the audio and picture,
> and use the numbers only to locate things. Group the utterances into
> beats: one beat is one point the speaker meant to make once, and each
> attempt at it is a run of consecutive utterances. For every beat pick
> exactly one attempt — the one an editor would publish — and give a
> one-sentence reason an editor would accept, naming what beat the
> others ("clean delivery", "second pass, first one stumbles on
> 'timecode'"). Mark every other attempt as an alternate (usable, not
> chosen) or as removed with its kind: a flub or false start, a restart
> that abandons the line, crew talk, an aside to the editor or crew,
> setup or technical talk, or dead air. Judge delivery from the sound
> and the picture together: stumbles and trailing off, eyes on the
> lens, a clean start and a finished ending. Prefer a complete attempt
> over a slightly livelier incomplete one. Never invent words, people
> or events you cannot hear or see, and never leave an utterance out:
> every numbered utterance in the window belongs to exactly one attempt.

User prompt (`window_prompt(window, utterances, prev_topic, taste)`):
first line `Pick the takes in this window (utterances U<a>–U<b>).`,
then, when a previous beat exists, `Previous beat (already decided):
"<topic>" — pick: "<pick text, truncated to 160 chars>". If the first
utterances continue that point, group them as a new attempt of a beat
with the same topic.`, then, when the job has a taste file (voice
feature; empty in this build), one line per entry as
`Editor preference: <text>`, then one line per core utterance:
`U<id> [<start:.1f>-<end:.1f>] <text>`. Times are source seconds; the
proxy starts 1.0 s before the first core utterance, and the prompt's
last line says so: `The video starts 1.0s before U<a>; utterance times
are on the recording's timeline, not the video's.`

Schema (`window_schema()`, strict, `additionalProperties: false`
throughout). Utterance references are global ids, range-based like the
spike's `first_line`/`last_line`:

```json
{
  "type": "object", "additionalProperties": false,
  "required": ["summary", "beats"],
  "properties": {
    "summary": {"type": "string"},
    "beats": {"type": "array", "items": {
      "type": "object", "additionalProperties": false,
      "required": ["topic", "keep_priority", "pick_attempt", "attempts"],
      "properties": {
        "topic": {"type": "string"},
        "keep_priority": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "pick_attempt": {"type": "integer"},
        "attempts": {"type": "array", "minItems": 1, "items": {
          "type": "object", "additionalProperties": false,
          "required": ["first_utterance", "last_utterance", "verdict",
                       "kind", "reason", "confidence"],
          "properties": {
            "first_utterance": {"type": "integer"},
            "last_utterance": {"type": "integer"},
            "verdict": {"type": "string", "enum": ["pick", "alternate", "remove"]},
            "kind": {"type": "string", "enum": ["keeper", "alternate", "flub",
                       "restart", "crew_talk", "aside", "setup", "dead_air"]},
            "reason": {"type": "string"},
            "confidence": {"type": "number"}
          }
        }}
      }
    }}
  }
}
```

Call (inside `pick_window_with_muse`, the only `guestlist` import in
`tighten.py`):

```python
from guestlist.meta import cache, spark   # raises MuseUnavailable on ImportError
cache.guard(estimate)                     # guestlist's own backstop too
r = spark.video_json(proxy, schema=window_schema(), name="tighten_picks",
                     developer=DEVELOPER, prompt=window_prompt(...),
                     model=model, effort="medium",
                     max_output_tokens=MAX_OUTPUT_TOKENS, max_usd=estimate)
```

### 7.4 Mechanical verification and repair (Muse proposes, these decide)

In `verify_window_reply(reply, window, utterances) -> tuple[list[Beat],
list[str]]`, in this order. The reply is never trusted past this list:

1. **Shape.** A reply that is not a dict with a list `beats` raises
   `WindowReplyError` (caught by the caller → rule fallback for that
   window, warning `tighten_window_reply_invalid:<window id>`). A reply
   that *is* shaped but wrong is repaired below, not raised.
2. **Ranges.** Attempt ranges are clamped to the window's core ids;
   `first > last` attempts are dropped; overlapping attempts: the first
   claim (in reply order) keeps the overlap, later ones are trimmed and
   gain the overrule note `overlap_trimmed`. Gaps and ordering are fixed
   by sorting beats/attempts by first utterance.
3. **Coverage.** Every core utterance must end up in exactly one
   attempt. An unclaimed utterance joins the preceding attempt as
   `remove`/`unclassified` material (it extends that attempt's *span*
   but never its *pick* text: §8 builds kept spans from pick attempts
   only, and an unclassified utterance inside a pick attempt is excluded
   from the kept words, splitting the span). The count is remembered.
4. **Verdicts.** Exactly one `pick` per beat: zero picks → the
   highest-confidence `alternate` is promoted (note `pick_promoted`),
   or, when all attempts are `remove`, `pick_attempt = -1` and the beat
   status is `no_usable_attempt`. Multiple picks → the first stands, the
   rest become `alternate` (note `extra_pick_demoted`). A `pick` whose
   kind is a removed kind is coerced to `keeper` (note
   `pick_kind_coerced`); an `alternate` with a removed kind, or a
   `remove` with kind `keeper`/`alternate`, is coerced to match its
   verdict (removed → `flub`). `confidence` is clamped to [0, 1];
   `keep_priority` outside 1–5 is clamped.
5. **Minimum pick length.** A pick attempt whose word span is shorter
   than `MIN_KEPT_SPAN_S` and which is not its beat's only usable attempt
   is demoted to `alternate` and the longest other usable attempt is
   promoted (note `pick_too_short`). When every attempt is that short,
   the longest is kept and the payload gains the warning
   `tighten_beat_below_min:B<id>` — a beat is never dropped silently for
   being short (§5 field rules).
6. **Fallback threshold.** If steps 2–4 had to touch more than
   `WINDOW_RULE_FALLBACK_SHARE` of the window's utterances, the repaired
   reply is discarded wholesale and the window uses the rule (§7.5),
   with the warning `tighten_window_rule_fallback:<window id>` in the
   payload. A model that cannot follow the numbering has not judged
   this window, and a quarter-repaired judgement is worse than the
   honest rule.

Any exception from the call itself (network, `SparkNoAnswer`, budget)
is caught per window: that window falls back to the rule with
`tighten_window_failed:<window id>:<ExceptionClass>` and spend $0 for
that window; other windows are unaffected. Only `ImportError` is
distinct (`MuseUnavailable`), because it is the expected no-guestlist
case and fails *every* window at once: `run` then uses the rule
everywhere with the single warning `tighten_muse_unavailable:<error>`.
`method` in the payload is `"muse"` when no window fell back, `"rule"`
when all did, `"mixed"` otherwise.

### 7.5 The rule fallback — `rule_beats`

Used for `--no-muse`, for unavailable/failed windows, and as the
degradation everywhere a clear message is owed. It keeps **the last
full pass of each beat** (the brief's rule) and is deliberately
conservative: it never removes speech as crew talk or an aside, because
text alone cannot know who was addressed.

```python
def normalize_tokens(text: str) -> list[str]:
    """Lower-case, punctuation stripped, fillers (um, uh, erm) dropped. Pure."""
def text_similarity(a: str, b: str) -> float:
    """difflib.SequenceMatcher ratio over the normalised token strings. Pure."""
def rule_beats(utterances: list[Utterance]) -> list[Beat]:
    """Last complete pass per beat; restart markers and similarity group. Pure."""
```

Algorithm, in order: (1) each utterance is its own attempt; (2) walk in
order, starting a new beat when the gap from the previous utterance is
over 20 s, or when `text_similarity(utterance, beat_representative) <
BEAT_SIM_MIN` where the representative is the beat's longest attempt
text so far — attempts at the same point resemble each other far more
than neighbouring points do, and restarts are temporally adjacent;
(3) within a beat, an attempt is *complete* when its last word ends a
sentence or it is the beat's last attempt; the pick is the **last
complete** attempt (reason `rule: last complete pass`); (4) non-pick
complete attempts are `alternate`/`rule: complete alternate pass`;
incomplete ones are `restart` when they match the restart markers
(`again`, `one more time`, `let me redo`, `start again`, `cut that` —
case-insensitive, first three words) and `flub` otherwise, reason
`rule: incomplete attempt`. `keep_priority` is 3, except the first and
last beats get 5 (openings and closings are never target-dropped by a
rule that cannot judge importance). The payload warning
`tighten_rule_mode_keeps_asides` is added once in rule/mixed jobs: crew
talk the model would have caught stays in, and the editor is told so
rather than the file pretending otherwise.

### 7.6 Spend estimate and the `--max-usd` gate

```python
def estimate_window_usd(duration_s: float, model: str) -> float:
    """duration_s * INPUT_TOKENS_PER_S * in_rate / 1e6
    + MAX_OUTPUT_TOKENS * out_rate / 1e6. Pure."""
```

Before each window's call: if `spent + estimate > max_usd`, the window
is **not** called and not silently rule-picked either — `run` raises
`TightenError(f"tighten: estimated Spark cost ${estimate:.2f} for "
f"window {id} would push spend ${spent:.2f} over --max-usd "
f"${max_usd:.2f}; raise --max-usd or pass --no-muse")`, matching
`reframe.ensure_within_budget`'s refusal shape. A budget refusal is a
stopped job with a remedy in the message, never a quietly worse cut.
Cached windows report their stored `usd` through `SparkResult` exactly
as the stages do today, so a re-run of an unchanged job costs $0 and
the manifest's spend sums check (§11) still holds.

## 8. From picks to spans: pause rule, edges, the complement

All of this section is `cutlist.recompute_spans` — pure, word-driven,
identical for Spark picks, rule picks, target drops and editor edits.

### 8.1 Edges never cut a word

For a kept word run (a maximal run of kept words with no trimmed pause
inside, §8.2): the start edge is the midpoint between the previous word
in the recording (kept or not) and the run's first word —
`(prev.end_s + first.start_s) / 2` — or `first.start_s - HEAD_PAD_S`
(clamped at 0) when there is no previous word. The end edge is the
symmetric midpoint, or `last.end_s + TAIL_PAD_S` at the end of the
recording. Every edge therefore sits strictly inside an inter-word gap:
no kept span can include half of a removed word, and no removed word's
tail bleeds into the picture. This is the same midpoint rule `angles`
already uses between lines, extended to removed material.

### 8.2 The pause rule (inside a kept attempt)

Walk the pick attempt's kept words in order. An inter-word gap
`> pause_limit_s` (default `DEFAULT_PAUSE_LIMIT_S`, flag-driven) ends
the current kept span: the span closes at `prev.end_s + 0.18` and the
next opens at `next.start_s - 0.17` — the pause keeps `PAUSE_KEEP_S`
(0.35 s) straddling where the breath sits, and the removed middle
becomes a `pause_trim` removed span (§8.4) with the reason
`pause <gap:.2f>s shortened to 0.35s`. A gap at or under the limit is
kept whole, breaths and word tails untouched — the rule only ever
removes the middle of a pause that is already longer than the limit,
never speech. Unclassified utterances (§7.4 step 3) inside a pick
attempt contribute no words: the span splits there exactly as if the
pause rule had fired, and the gap is classified `unclassified`.

### 8.3 The readable minimum

A kept span shorter than `MIN_KEPT_SPAN_S` is merged with the following
kept span of the same attempt when the only thing between them is a
trimmed pause (the trim is undone for that pause; the pause stays
long — a readable take beats a tight one). A span still below the
minimum after merging (a genuinely tiny pick) is kept, and the payload
gains `tighten_span_below_min:K<id>`; §7.4 step 5 has already ensured
this can only happen when no longer attempt exists.

### 8.4 Removed spans are the complement, classified

After kept spans are placed, the removed list tiles everything between
the first kept edge and the last kept edge that is not kept. Each
removed span is classified by what it overlaps, in this priority order:
a trimmed pause → `pause_trim`; words of a `remove` attempt → that
attempt's kind; words of a non-pick usable attempt → `alternate`; words
of a target-dropped or editor-dropped beat → `target_drop` /
`editor_drop`; a word-free gap `>= DEAD_AIR_S` → `dead_air` with reason
`no words for <gap:.1f>s`; anything else → `unclassified` with reason
`not in the picked take`. Before the first kept edge and after the last,
nothing is listed: tighten does not editorialize the recording's head
and tail beyond the first and last kept words, and `duration_in_s`
(words span) vs the tiled span in the manifest tells the editor how
much that was.

### 8.5 The output-time map

`source_to_output` sums kept-span durations in order: a source time in
kept span *i* maps to `(sum of durations of spans < i) + (t -
span.start_s)`. `output_to_source` inverts it span by span. Both are
exact inverses on kept material; a source time in removed material
maps to `None`, never to a guess. These are the functions the voice
feature uses to turn a playhead position into a beat (§5.1), and the
functions `angles` uses to place shots (§10.2) — one map, three users,
no divergent copies.

## 9. Optional target length (`--target`)

`--target` accepts `M:SS`, `MM:SS`, `H:MM:SS` or bare seconds
(`parse_target(text) -> float`, `ValueError(f"Bad --target {text!r}: "
"use M:SS or seconds")` otherwise). It is applied *after* spans are
first recomputed, and it only ever drops **whole beats** — never a
sentence, never part of an attempt:

```python
def apply_target(payload: dict, words, target_s: float) -> dict:
    """Drop whole beats (Spark keep_priority, longest first) until the
    kept duration fits or only priority-5 beats remain. Pure."""
```

While `duration_out_s > target_s`: among beats with status `kept` and
`keep_priority < 5`, drop the one with the lowest `keep_priority`
(ties: the longest first, so fewest beats are lost), set its status to
`dropped_target`, and recompute. Priority-5 beats (the opening, the
closing, anything Spark marked essential) are never dropped; if only
those remain and the cut is still long, the payload keeps them and
`target.met` is `false` with the warning
`tighten_target_unmet:<after:.1f>s over <target:.1f>s`. The result is
recorded in `payload["target"]` (`requested_s`, `before_s`, `after_s`,
`met`, `dropped_beat_ids`) — what was dropped is reported in the file,
on the console (§12) and in `cut.json` (§11), never just implied by a
shorter runtime. Re-running with a different `--target` starts from the
Spark picks again (target drops are recomputed from `kept` in
`tighten.run`, not accumulated), while `dropped_editor` beats stay
dropped: the editor's hand outranks the clock.

## 10. The render change (`angles`)

### 10.1 `angles.run` gains one optional input

```python
def run(*, sync_path, words_path, out_path, audio_label, render_path,
        min_shot_s, model, no_muse, grade_path=None,
        tighten_path: Path | None = None) -> dict:
```

With `tighten_path=None` every existing caller, test and ffmpeg command
is unchanged — this is a hard requirement, verified by §14 item 21.
With a path: `cutlist.load_tighten` loads it (a bad file raises its
`ValueError`, surfaced as-is), and the payload must belong to the same
job: `payload["audio"]["label"]` must equal the resolved audio label
and every utterance word index must be inside the loaded words, else
`ValueError(f"tighten.json does not match this job's words/audio: "
f"{tighten_path}")`. A tighten file from another recording must fail
loudly here, not cut the wrong spans quietly.

### 10.2 Lines and shots on two timelines

Kept words are the loaded words whose indices fall inside kept spans'
utterances (span membership comes from the payload, not from re-testing
word times — the file is the contract). Lines are then built **per kept
span**: `split_lines` runs on each span's words separately and the
results are concatenated with fresh sequential ids, so no line ever
crosses a removed gap or a trimmed pause. Coverage, Muse picks, the
rule fallback and every mechanical check in `apply_checks` run exactly
as today, in source time — cameras cover source moments, and that does
not change because the audio will be cut.

Only at shot emission do times convert: each shot's source
`start_s`/`end_s` (the `apply_checks` result) maps through the span it
belongs to — `out = span_out_start + (source - span.start_s)`, where
`span_out_start` is the cumulative frame-snapped output start (§10.3).
The emitted shot keeps `start_s`/`end_s` as **output** times and gains
three fields: `source_start_s`, `source_end_s`, `kept_span_id` (plus
`beat_id`, from the span). `lines` in `angles.json` stay in source time
(they describe speech, and `export`/QC never read them); the payload
gains `"tighten": {"file": <name>, "kept_s": <float>, "removed_s":
<float>, "beats": <int>}` and stays `version: 1` — every added field is
optional for readers, and OPEN-DESIGN's `export.py` (which reads only
shot `start_s`/`clip_in_s`/`clip_out_s`/`camera`) works on a tightened
job unmodified, producing a timeline on the output timeline with no
gaps for removed material. Black shots keep their meaning (a kept span
no camera covers) and their source/output pair.

### 10.3 Rendering: picture as today, audio cut per kept span

`render_commands` gains the kept spans as an optional argument; the
video parts are **identical commands to today** except each shot's
`-ss` uses its source `clip_in_s` (already source-based) and its frame
count comes from the output frame grid: `frame_grid` is fed the shots
in output order with output times, anchored at output 0, so the joined
picture is exactly the sum of the snapped shot durations. Grade LUT
chains, black-shot `lavfi` parts and the parts directory
(`<render stem>.parts/`) are unchanged.

The audio changes from one span to one part per kept span:

```python
def audio_part_command(audio_path: str, audio_in_s: float,
                       duration_s: float, out: Path) -> list[str]:
    fade_out_start = max(0.0, duration_s - CROSSFADE_S)
    return ["ffmpeg", "-v", "error", "-y",
            "-ss", f"{audio_in_s:.6f}", "-t", f"{duration_s:.6f}",
            "-i", audio_path, "-map", "0:a:0",
            "-af", (f"afade=t=in:st=0:d={CROSSFADE_S},"
                    f"afade=t=out:st={fade_out_start:.6f}:d={CROSSFADE_S},"
                    f"aresample={AUDIO_SR}"),
            "-c:a", "aac", "-b:a", "192k", str(out)]
```

`audio_in_s = span.start_s - audio.offset_s` (clip-local); the part's
duration is the span's **frame-snapped output duration**, not its
source duration — the difference is under half a frame and the span
edges sit in pauses (§8.1), so trimming or padding there is inaudible,
and picture and audio parts then agree frame-for-frame by construction.
Each part fades in and out over `CROSSFADE_S` (8 ms): joins are
pause-to-pause, so a short fade kills the click a hard splice can make
without ever touching a word. Parts are written as
`<render stem>.parts/audio_000.m4a`, … and joined with the picture in
one final command — two concat inputs, stream copy, the existing
faststart:

```python
["ffmpeg", "-v", "error", "-y",
 "-f", "concat", "-safe", "0", "-i", str(parts_dir / "parts.txt"),
 "-f", "concat", "-safe", "0", "-i", str(parts_dir / "audio_parts.txt"),
 "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
 "-movflags", "+faststart", str(out)]
```

with `audio_parts.txt` in the same `file '<abs path>'` format as
`parts.txt`. `render()` runs the video parts, then the audio parts,
then this final command. When `tighten_path` is `None`, `render` and
`render_commands` produce byte-identical command lists to today
(§14 item 21 asserts the lists, not just the behaviour).

### 10.4 Angle overrides

`angles.run` reads `payload["angle_overrides"]` (`{beat_id, camera}`):
after picks are made (Muse or rule) and before `apply_checks`, any
line whose words lie inside the overridden beat's pick attempt has its
pick's camera set to the override camera with method `"editor"` and the
overrule note `angle_override:B<beat_id>`. `apply_checks` then does its
normal work: an override camera that does not cover the line is
overruled by the existing coverage check and recorded — the editor's
instruction is honoured exactly as the footage allows, and the
file shows where it was not. Overrides never change *what* is said,
only which camera shows it; they are the voice feature's angle path
(VOICE-DESIGN §5) and tighten's own runs never create them.

## 11. Inside `director cut`

The delta to OPEN-DESIGN, kept minimal so the two builds compose:

- `STAGES` becomes `("sync", "words", "tighten", "grade", "angles",
  "export", "reframe", "qc")`. `tighten` is absent from the manifest
  entirely under `--no-tighten` (the §3 rule for opt-out stages).
- `tighten.run(*, sync_path, words_path, out_path, audio_label,
  pause_limit_s, target_s, model, no_muse, max_usd, taste_path=None)`
  is the stage entry point (standalone CLI in §12 calls the same
  function). Outputs `["tighten.json"]` plus the `tighten-windows/`
  work directory. Spend is the payload's `spend_usd`.
- Stage fingerprint (OPEN-DESIGN §7.1 table gains one row): options
  `{"audio", "pause_limit_s", "target_s", "model", "no_muse"}`; files
  the canonical words file and `sync.json`. The `angles` stage's files
  gain `tighten.json` when the stage is enabled, so any tighten change
  — including an editor edit (§11.2) — re-runs `angles` and everything
  downstream on the next `director cut`, while `tighten` itself skips.
- Manifest: `cut.json` gains `"tighten": {"file": "tighten.json",
  "beats": <int>, "kept_s": <float>, "removed_s": <float>,
  "duration_out_s": <float>, "method": <str>, "target": <payload
  target or null>}` (null under `--no-tighten`), and the stage record
  carries tighten's warnings.
- `angles` in a tightened job is called with
  `tighten_path=out_dir / "tighten.json"`. A `--no-tighten` job calls
  it exactly as OPEN-DESIGN §7.4 specifies. The QC stage needs no
  change (§1.4).

### 11.1 CLI flags on `cut`

| Flag | Default | Effect |
|---|---|---|
| `--no-tighten` | off | Skip the tighten stage; the cut keeps everything (today's behaviour). |
| `--pause-limit` | `0.8` | Pauses longer than this inside a kept attempt are shortened to 0.35 s. Passed to `tighten.run`; validated `0.30–3.00` there with the §5.3 message shape (`tighten: pause limit <x>s is outside 0.30–3.00s`). |
| `--target` | none | Target kept length, `M:SS` or seconds (§9). |

One gate, no second flag: `cut` passes the job's remaining `--max-usd`
budget (the job cap minus spend so far) as `tighten.run`'s `max_usd`,
and §7.6's per-window estimate is checked against it. A standalone
`director tighten` has its own `--max-usd` (§12.1) with the same check.

Console line (OPEN-DESIGN §11.3 format): `tighten: done ($0.1712)` plus
` (14 beats, kept 612.4s → 181.2s)`; with a target, `…, target 3:00
met` or `…, target 3:00 missed by 12.3s`. Under `--no-muse`: `tighten:
done (rule, 14 beats, kept 612.4s → 190.0s)` — the method is on the
console, not buried in JSON.

### 11.2 Editor edits survive re-runs (the shared-contract rule)

A `cut` re-run whose tighten fingerprint matches skips the stage and
the editor-edited `tighten.json` stands — the common voice workflow.
When the stage *does* re-run (words or options changed) over an
existing `tighten.json` with a non-empty `edits_applied`, `tighten.run`
must not silently erase the editor's hand: it preserves
`angle_overrides` and `editor_notes` verbatim, re-applies every
`edits_applied` op whose beat/attempt still exists through
`cutlist.apply_operations` (ops naming vanished beats are dropped with
the warning `tighten_edit_stale:<edit id>`), and only then recomputes
spans. Target drops from the previous run are *not* preserved (they
are recomputed, §9); editor drops are ops, so they are.

## 12. Command line

### 12.1 Standalone `director tighten`

```
director tighten --sync sync.json --words words.json --out tighten.json
                 [--audio LABEL] [--pause-limit 0.8] [--target 3:00]
                 [--model muse-spark-1.3] [--no-muse] [--max-usd 5.00]
```

`cli.command_tighten(args)`: requires ffmpeg (proxies may be built),
resolves the audio label exactly as `angles.run` does (explicit label,
else the reference-path clip, else `A`), calls `tighten.run`, prints
one line per beat — `B<id> <topic>: kept A<pick> (<dur>s) — <reason>`
with alternates counted (`+1 alternate`) and dropped beats shown as
`B<id> <topic>: dropped (<kind of best attempt>)` — then the summary
`<in>s → <out>s, <n> beats, <m> removed spans ($<spend>, <method>)`
and `Wrote <out>`. This is how a stranger tries take-picking without
the one-step command, and how the owner re-picks one recording by hand.

### 12.2 `director angles --tighten`

`angles` gains `--tighten TIGHTEN.json` (default none), threaded to
`run(tighten_path=…)`. Nothing else about the command changes; the
console summary gains `tightened <in>s → <out>s` when the flag is set.

### 12.3 `tighten.run` signature

```python
def run(*, sync_path: Path, words_path: Path, out_path: Path,
        audio_label: str | None, pause_limit_s: float,
        target_s: float | None, model: str, no_muse: bool,
        max_usd: float, taste_path: Path | None = None) -> dict:
    """Words -> utterances -> windows -> picks -> spans -> tighten.json.
    Returns the payload (also written)."""
```

`taste_path` is read when it exists (`{"entries": [{"text": …}]}`, the
voice feature's file) and its entries are added to every window prompt
(§7.3); in this build nothing writes it, and a missing file is normal,
never an error. Steps in order: validate money/pause limit → load sync
views (reuse `angles.load_sync`) and resolve audio/video clips → load
words → utterances → windows → per window: proxy (unless rule mode),
estimate/gate, Spark or rule, verify (§7.4) → assemble beats → preserve
editor state (§11.2) → recompute spans → target (§9) → write.

## 13. Failure and edge cases (decided)

| Case | Behaviour |
|---|---|
| No words / empty `--words` file | §6.1 `TightenError`; in `cut`, the `tighten` stage is marked failed, manifest written, `Error: tighten: …`, exit 1. `sync.json`/`words.json` stay for diagnosis. |
| `MODEL_API_KEY` missing, guestlist missing | Not an error: `tighten_muse_unavailable:<error>` warning, rule picks everywhere, job completes. The console line says `rule`. Same for per-window call failures (`tighten_window_failed:…`). |
| Spark replies garbage for a window | §7.4: repaired if minor, whole-window rule fallback past the 25% threshold, warning names the window. Never a crash, never silently kept flubs presented as Muse picks (`method` says `mixed`). |
| Budget would be exceeded | §7.6 `TightenError` before the window's call, naming the remedy. In `cut` the stage fails with files from earlier stages kept (OPEN-DESIGN §7.2 step 4e applies). |
| One camera + lav (audio clip has no picture) | Video source is the camera (§7.2); audio is the lav. Normal case, no warning. |
| Audio clip unplaced (`offset_s null`) | `TightenError("tighten: audio clip <label> is not placed on the timeline; cannot align its words — check the sync warnings")` — mirroring the words bridge's refusal (OPEN-DESIGN §6.1). |
| Every attempt of a beat is a flub | Beat `no_usable_attempt`, `pick_attempt -1`, contributes no kept span, listed in removed spans. The beat is visible in the file and the console output; nothing vanishes silently. |
| Whole recording is one take, no restarts | One attempt per beat, all picks, removed list holds only pause trims/dead air. `duration_out_s` close to `duration_in_s`; correct, not an error. |
| `--target` below the priority-5 floor | §9: target missed, warning + `met: false`, cut kept long. Never a mid-sentence cut to hit a number. |
| `--target` longer than the kept cut | No-op; `target.met: true`, `dropped_beat_ids: []`. |
| Words and tighten from different jobs | §10.1 `ValueError` at `angles` time; standalone, tighten writes only from the words it was given, so the mismatch cannot arise there. |
| Proxy ffmpeg fails / no video covers a window | Per-window rule fallback + warning (§7.2). The job completes; `method` degrades to `mixed`/`rule` honestly. |
| Very long recordings (hours) | Windows bound every call; proxies are per-window files under `tighten-windows/` and guestlist caches replies by content, so a re-run re-sends nothing answered. No duration cap is imposed — the budget gate is the cap. |
| Re-run after voice edits | §11.2: edits re-applied or named stale; never silently erased. |

## 14. Tests

`tests/test_cutlist.py` (items 1–8) and `tests/test_tighten.py` (items
9–24); numbering is continuous. Offline throughout: no network, no Meta
API, no Whisper. Style follows `tests/test_angles.py` — small synthetic
word/utterance builders, `tempfile.TemporaryDirectory`, and the
`FakeMuseTests` pattern (a stand-in `guestlist.meta` in `sys.modules`)
for Spark paths. One line per item: what it builds, what it asserts.

1. `test_recompute_edges_never_cut_words` — words with a removed
   attempt between two kept ones → kept edges are the exact inter-word
   midpoints; no kept span contains any part of a removed word's
   `[start, end]`.
2. `test_pause_trim_splits_and_keeps_breath` — a kept attempt with a
   1.42 s internal gap at limit 0.8 → two kept spans, one `pause_trim`
   removed span of 1.07 s, retained pause exactly 0.35 s; a 0.7 s gap
   is untouched.
3. `test_removed_complement_tiles` — kept + removed tile
   `[first kept edge, last kept edge]` with no gap or overlap, and each
   removed kind follows the §8.4 priority (pause before attempt kind
   before dead air).
4. `test_min_span_merges_across_trim` — a 0.3 s kept fragment after a
   trimmed pause merges with the next span (trim undone); a lone tiny
   pick survives with `tighten_span_below_min` in the payload warnings.
5. `test_time_mapping_round_trip` — `source_to_output` then
   `output_to_source` is the identity inside every kept span; a removed
   moment maps to `None`; `beat_at_output` finds the right beat for a
   playhead time in the second kept span.
6. `test_apply_operations_swap_and_audit` — `swap_take` changes the
   pick, demotes the old pick to `alternate`, recomputes spans, and
   appends the `edits_applied` entry; a second swap back restores the
   original kept spans exactly.
7. `test_operation_checks_block` — swapping to a `flub` attempt, an
   unknown beat, a span-drop of a beat's last span, and a 5 s pause
   limit each return a blocked op with the §5.3 texts, and the payload
   is unchanged.
8. `test_apply_operations_never_empties` — dropping every beat in one
   batch applies none of them (`That would empty the cut`).
9. `test_build_utterances_granularity` — a word stream with a 1.3 s
   breath mid-sentence and a 7 s sentence → utterances split at the
   breath and at the longest pause under the 6 s cap; punctuation and
   word indices preserved; equal to or finer than `angles.split_lines`
   on the same words.
10. `test_build_windows_bounds_and_retreat` — 130 utterances over
    500 s → windows within both bounds, every utterance owned once,
    and a window that would end 0.4 s before a 1 s gap retreats to end
    before that gap.
11. `test_window_prompt_and_schema` — `window_schema()` requires
    exactly `summary`/`beats` with the §7.3 enums;
    `window_prompt` numbers utterances `U<id> [a-b] text`, carries the
    previous-beat line, and ends with the video-offset sentence;
    `DEVELOPER` contains "never leave an utterance out".
12. `test_verify_window_reply_repairs` — a reply with an overlap, an
    unclaimed utterance, two picks and kind/verdict mismatches is
    repaired per §7.4 steps 2–4 with the named overrule notes; the
    unclaimed utterance is excluded from the pick's kept words.
13. `test_verify_window_reply_fallback_threshold` — a reply misnumbering
    over 25% of the window → the window's beats are the rule beats and
    the warning is `tighten_window_rule_fallback:<id>`; a malformed
    (non-dict) reply raises into the same fallback via
    `tighten_window_reply_invalid:<id>`.
14. `test_rule_beats_keeps_last_full_pass` — two passes over three
    beats plus a restart-marker fragment → the rule picks the second
    pass where it is complete, labels the fragment `restart`, and adds
    `tighten_rule_mode_keeps_asides` (via `run`-level assembly).
15. `test_estimate_and_budget_gate` — `estimate_window_usd` matches the
    §7.6 formula under spark and wonder rates; with a fake Spark that
    must not be called, a window over budget raises the §7.6
    `TightenError` and the fake records zero calls.
16. `test_pick_window_with_fake_guestlist` — a fake `spark.video_json`
    asserts it received the proxy path, `name="tighten_picks"`, the
    §7.3 schema and `max_output_tokens == MAX_OUTPUT_TOKENS`, returns a
    two-beat reply → beats land with spend = the fake's `usd`; a fake
    that raises yields rule beats for that window and the
    `tighten_window_failed:` warning.
17. `test_proxy_command` — `window_proxy_command` equals §7.2
    element-for-element, including both `-ss` ins and the 64k mono AAC.
18. `test_target_drops_whole_beats_only` — beats of known durations and
    priorities with a target below the total → lowest-priority,
    longest-first beats drop whole, priority-5 beats survive, `target`
    records `before/after/dropped_beat_ids`; an unreachable target
    reports `met: false` with `tighten_target_unmet:`.
19. `test_parse_target` — `3:00` → 180.0, `1:02:03` → 3723.0, `90` →
    90.0; `abc` and `1:99` raise the §9 `ValueError`.
20. `test_run_end_to_end_words_only` — synthetic words for a two-pass
    talk, `no_muse=True`, into a temp dir → `tighten.json` parses,
    kept spans cover only second-pass words, `duration_out_s <
    duration_in_s`, and re-running preserves a hand-added
    `angle_overrides` entry and re-applies a recorded `swap_take`
    (§11.2).
21. `test_angles_without_tighten_unchanged` — `render_commands`
    without spans returns command lists equal to the pre-change
    implementation's shape (one audio span, no `audio_parts.txt`), and
    `run(tighten_path=None)` on the existing angles fixtures produces
    the same shots as before.
22. `test_angles_with_tighten_mapping` — a hand-built `tighten.json`
    over the existing angles fixtures → shots carry output
    `start_s`/`end_s`, `source_*`, `kept_span_id`, `beat_id`; no shot
    crosses a removed span; a beat's `angle_overrides` camera appears
    with overrule `angle_override:B<id>`, and a non-covering override
    is overruled by the coverage check.
23. `test_audio_part_command` — `audio_part_command` equals §10.3
    element-for-element, fade-out start clamped at 0 for a span shorter
    than the fade.
24. `test_tightened_render_synthetic` *(ffmpeg)* —
    `@unittest.skipUnless(shutil.which("ffmpeg"), ...)`. Two synthetic
    clips and a two-pass word stream (kept pass says the line once,
    cleanly) rendered through `angles.run` with a rule-built
    `tighten.json`: the output probes to the kept duration ±1 frame,
    its audio duration matches its video duration within 0.05 s, and a
    word from the removed pass is absent from the kept-span map
    (`source_to_output` of its midpoint is `None`). No Meta call.

Gate: `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`
green. Per `AGENTS.md`, the ffmpeg-facing functions
(`window_proxy_command`, `audio_part_command`, the tightened render)
are additionally exercised on real footage in §15's live check —
filter-graph regressions rarely show in unit tests.

## 15. What the build also updates, non-goals, live check

**Provenance (at build time).** `docs/muse-code/BUILD-LOG.md` gains:

```markdown
# Build log: take picking (`tighten`) (2026-11-03)

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `TIGHTEN-BRIEF.md`: take picking, pause trim, `tighten.json`. |
| Design | Meta **Muse Code** on **`muse-wonder-1.0`** | `TIGHTEN-DESIGN.md`: `cutlist.py` contract, windowed Spark picks, rule fallback, tightened render. |
| Build | Meta **Muse Code** on **`muse-spark-1.3`** | `src/director/tighten.py`, `src/director/cutlist.py`, the `angles`/`cut` changes, `tests/test_tighten.py`, `tests/test_cutlist.py`. |
| Code review and live test | Claude Code, piloted by the owner | Real footage, below. |

Muse Code token use: TBD (the owner fills this from the run log).
Live test: TBD (the owner fills this from the real-footage run).
```

and the README's "Who built what" table gains:

```markdown
| Take picking (`director tighten`, the `tighten` stage of `director cut`) | Designed by Muse Code on muse-wonder-1.0, built by Muse Code on muse-spark-1.3 |
```

The README also gains, under the pipeline section: the
`director tighten --sync … --words … --out tighten.json` standalone
form, the `director cut --pause-limit/--target/--no-tighten` flags in
the `cut` quickstart, and one paragraph stating the contract in
editor's terms — Director keeps one take per beat, lists every removed
span with its reason in `tighten.json`, shortens pauses over the limit
to 0.35 s, and both picture and sound are cut at word boundaries with a
short fade so joins do not click. `AGENTS.md` gains the §2 bullets.

**Non-goals (explicit).** *Voice editing* (VOICE-DESIGN — the next
build; this one only defines the contract it edits). *Re-picking
inside `angles`* (angles picks cameras, tighten picks takes; neither
does the other's job). *Audio re-mastering* (fades at joins only; no
loudness or de-noise stage is introduced). *Sub-word edits* (every edge
is inter-word by construction; removing a stumble *inside* a word is a
reshoot note, which the voice feature can record, not a cut this stage
will fake).

**Live check (before done is claimed).** On the owner's real footage —
the 3-minute founder talk, 2–3 passes on 2–3 cameras — run
`director cut <clips> --out-dir demo-job` and: listen to `cut.mp4` end
to end for clipped words and clicks at joins; open `tighten.json` and
check every beat's pick against what the passes actually contain
(swap one pick by hand-editing `pick_attempt` and re-running —
`angles` must follow); confirm the removed list names the restarts
the owner remembers. Record the outcome and the stage spend in the
BUILD-LOG section above.




