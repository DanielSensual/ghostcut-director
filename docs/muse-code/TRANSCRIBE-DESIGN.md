# Design: Voice Transcribe as the transcript of record (`transcribe`)

Designed by Muse Code on `muse-wonder-1.0` from
[TRANSCRIBE-BRIEF.md](TRANSCRIBE-BRIEF.md), under [AGENTS.md](../../AGENTS.md),
[OPEN-DESIGN.md](OPEN-DESIGN.md), [TIGHTEN-DESIGN.md](TIGHTEN-DESIGN.md) and
[VOICE-DESIGN.md](VOICE-DESIGN.md). `OPEN-DESIGN.md` is the contract for
`cut.py`, `export.py` and `cut.json`; `TIGHTEN-DESIGN.md` is the contract for
`cutlist.py`, `tighten.py`, `tighten.json` and the tightened `angles` render,
even though that build lands in another checkout first. This document is the
complete design; the build (Muse Spark) writes the code from it without
further decisions. No code is written by this document.

Conventions that bind the build: Python ≥3.11, `from __future__ import
annotations`, type annotations throughout, `@dataclass(slots=True)` at module
boundaries, `guestlist` imported only inside the function that calls it and
guarded there, API key from `MODEL_API_KEY` only (handled inside
`guestlist`), no `-contributor` model ids, tests offline with `unittest` and
never touching the Meta API or Whisper, no media files committed, no client
names, people's names or local footage paths in code, tests or docs.

## Build order: after tighten, before voice notes

**Tighten is built first, this feature second, voice notes third.** Tighten
creates the one list of what stays (`tighten.json`, owned by
`src/director/cutlist.py`, mutated only through `cutlist.apply_operations`,
rendered by `angles`). This feature changes *what the words in that pipeline
mean* — verbatim text, a speaker per word, disfluencies kept — and tighten is
the first consumer that can act on the difference (§9). Voice notes are built
last and reuse this module's WAV extraction and chunking for their long
recordings (§10); nothing in this build writes voice code.

The build assumes the `--whisper-model` option Claude Code adds to `sync`
and `cut` today is present in the tree it lands on (default
`mlx-community/whisper-large-v3-turbo`, threaded into
`sync.get_transcript_words` and `cut.mlx_whisper_command`). This design does
not add a second Whisper-model flag and does not change that option's name,
default or plumbing; it only consumes whichever word timings that option
produced (§1.2).

## Why this

Every word time in Director comes from local Whisper today. Whisper writes
what the speaker *meant*: it smooths "the the", "will will", "um" and
half-started words out of the text, and it has no idea who is talking, so the
crew's "rolling", "one more time" and side talk look like the speaker's
lines. Those smoothed-away repeats and fillers are exactly the signals that
a take is flubbed, and the crew talk is exactly what tighten must remove —
but the transcript tighten reasons over has already erased both.

Meta's Voice Transcribe (`muse-voice-transcribe-1.0`,
`guestlist.meta.transcribe.diarize`) keeps disfluencies verbatim and labels
speakers. It returns turn-level times only — measured 2026-11-03, 60 s of
one speaker came back as a single turn (0 to 59 580 ms) with no word
timestamps (TRANSCRIBE-BRIEF). So neither engine alone is the transcript
Director needs: Muse has the words and the speakers but no clock; Whisper
has the clock but neither the words nor the speakers.

The feature is therefore **one alignment**: transcribe the production-audio
clip with Voice Transcribe (16 kHz mono WAV, chunked on silence, speakers
reconciled across chunks), keep Whisper's word times as the clock only, and
map Muse's tokens onto Whisper's timed words with a tolerant sequence
alignment. The job's `words.json` keeps its current shape and gains three
optional fields per word (`speaker`, `heard_by`, `disfluency`); a new
`transcript.json` records the turns, speakers, chunks and alignment stats.
`director cut`'s words stage does this by default when the Meta key and
`guestlist` are available, and falls back to Whisper alone — same output
shape, warning in `cut.json` naming why — when they are not. Tighten then
uses the new fields in exactly two mechanical ways (§9): a non-talent
speaker's attempt is removed as crew/other talk, and disfluencies inside an
attempt count against it when a cleaner alternate exists.

## 1. Inputs as they actually are

Verified against this repository and the installed packages. The build must
not re-derive it differently.

### 1.1 Voice Transcribe (verified in the installed `guestlist`)

```python
# guestlist.meta.transcribe — the only import is inside diarize_chunk (§6.4)
MODEL = "muse-voice-transcribe-1.0"
def diarize(wav: str | Path, *, language_bias: list[str] | None = None) -> dict:
    """WAV (mono, 16-bit PCM, 16 or 24 kHz) ->
    {"transcript", "turns": [{"startMs", "endMs", "transcript", "speaker"}], ...}"""
# Response keys (brief, measured): audioDurationMs, sessionId, transcript,
# turns[] with turnId, startMs, endMs, transcript, speaker.
# Turn-level times only; no word timestamps. Content-cached by file hash
# (cache key over sha256_file(wav), MODEL, request dict); a cache hit
# returns the stored result without charging the ledger.
# Cost: guestlist.meta.pricing.transcribe_cost(seconds)
#     = seconds * TRANSCRIBE_PER_SECOND, TRANSCRIBE_PER_SECOND = 0.18/3600
#     = $0.00005 per second ($0.18 per audio hour).
# Non-200 raises RuntimeError(f"transcribe HTTP {status}: ...");
# a missing key raises RuntimeError from guestlist.meta.client.api_key().
```

Two consequences the whole design follows from: (a) Muse token times must
be *derived* from Whisper's clock (§7), never read from the response; there
are none. (b) The maximum audio length per request is unknown (60 s
measured), so the design chunks (§6) and never sends a whole long recording
in one request.

### 1.2 Whisper is the clock only (verified in this repository)

- `sync.load_transcript_words(path)` and `angles.load_timed_words(path)`
  both parse Whisper JSON in either shape — `{"words": [{"word"|"text",
  "start", "end"}, …]}` or `{"segments": [{"words": [...]}, …]}`.
  `sync`'s copy normalises word text for offset voting; `angles`' copy
  keeps the raw (punctuated) text, which line splitting needs.
- `angles.TimedWord(text, start_s, end_s)` is the word type every later
  stage consumes. `angles.load_timed_words` drops entries without text or
  a numeric `start`, fills a missing `end` from the next word's start
  (last word: `start + 0.30`), sorts by `start`, clamps `end >= start`.
- `sync.get_transcript_words(clip, transcripts_dir, cache_dir)` obtains
  per-clip words from `--transcripts`, the `sync-transcripts/` cache, or a
  local `mlx_whisper` run (`["mlx_whisper", clip.path, "--output-format",
  "json", "--output-dir", …, "--word-timestamps", "True", "--verbose",
  "False"]`, plus `"--model", <whisper-model>` once the assumed option
  lands). `cut.resolve_words` resolves the audio clip's words from the
  same sources in the order `explicit → transcripts → sync_cache →
  mlx_whisper` (OPEN-DESIGN §6.2) and `cut.shift_words` /
  `cut.words_file_payload` shift clip-local times into shared-timeline
  seconds and write `<out_dir>/words.json` as
  `{"words": [{"word", "start", "end"}, …]}` at 3 decimals.
- All Whisper times in this design are **clip-local seconds** until the
  single shift at the end (§8.2). Alignment, chunking and speaker
  assignment happen in clip-local time, where the Muse turns live; the
  shift is applied once, to words and turns together, so the two can never
  be shifted by different amounts.

### 1.3 What `angles` and tighten read today (the compatibility contract)

- `angles.run` treats word times as **shared-timeline** seconds, splits
  them into lines with `angles.split_lines` (sentence breaks, pauses over
  `angles.PAUSE_S` = 0.6 s, 8 s cap), and places cut points at inter-word
  midpoints (`angles.cut_points`). Any word list this feature emits must
  keep `start_s`/`end_s` monotonic and edges inside inter-word gaps, or
  every downstream guarantee breaks.
- Tighten (TIGHTEN-DESIGN) groups words into utterances
  (`tighten.build_utterances`, ≤6 s, split at 1.2 s pauses), groups
  utterances into beats/attempts, picks one attempt per beat with Spark
  (`tighten.pick_window_with_muse`, `spark.video_json`, name
  `tighten_picks`) or the rule (`tighten.rule_beats`, last complete pass),
  and `cutlist.recompute_spans(payload, words, pause_limit_s=…)` rebuilds
  `kept_spans`/`removed_spans` with every edge at an inter-word midpoint
  (TIGHTEN-DESIGN §8.1). `tighten.json` attempt kinds already include
  `crew_talk`, `flub`, `restart`, `aside`, `setup`, `dead_air`
  (TIGHTEN-DESIGN §4); §9 of this design feeds those kinds evidence, it
  does not add new ones.
- `cutlist` never imports `angles` at module top (TIGHTEN-DESIGN §2 cycle
  rule): words arrive as objects with `text`/`start_s`/`end_s`
  attributes. Every new word field in §4 is therefore read with
  `getattr(word, "speaker", "")`-style access in `cutlist`, never with a
  hard attribute requirement, so hand-built word triples in existing
  tests keep working.

### 1.4 The installed `guestlist` cache and pricing (verified)

`guestlist.meta.cache` is a content-addressed disk cache plus a spend
ledger with a global hard stop (`cache.guard`, `BUDGET_USD`, $115 in the
installed package). `diarize` keys its cache on
`cache.key(cache.sha256_file(wav), MODEL, request)` in namespace
`"transcribe"`; a hit returns `hit["result"]` and charges nothing. Because
`diarize` returns only the result dict (no `cached` flag, unlike
`spark.SparkResult`), §6.4's wrapper recomputes that same key before the
call to learn whether the chunk is already paid for — the one place this
design mirrors a `guestlist` internal, using only its public
`cache.key` / `cache.sha256_file` / `cache.get` functions, with the
request dict built exactly as `guestlist.meta.transcribe` builds it when
`language_bias` is `None` (`{"mode": "DIARIZATION", "model": MODEL,
"audioEncoding": "WAV"}`). If a future `guestlist` changes that request
shape, the pre-check simply always misses and spend is reported at list
price on a cache hit — accounting drifts, results do not.

## 2. Files changed

| File | Change |
|---|---|
| `src/director/transcribe.py` | New. WAV extraction, silence chunking, the `diarize` wrapper, speaker reconciliation, the Whisper↔Muse alignment, `transcript.json` payload, `transcribe_audio_clip` and the standalone `run()`. The only new module. |
| `src/director/angles.py` | `TimedWord` gains `speaker`/`heard_by`/`disfluency` with defaults; `load_timed_words` parses and preserves them. Nothing else changes: line splitting, picks, checks and render are byte-identical for words without the new fields. |
| `src/director/cut.py` | The words stage (§8): `JobOptions.no_transcribe`, transcribe-aware `words_file_payload` and `shift_words`, the words-stage execution delta, fingerprint and manifest deltas, spend accounting. No stage is added; `STAGES` is unchanged from the post-tighten list. |
| `src/director/cli.py` | `cut` gains `--no-transcribe`; new `command_transcribe(args)` and `transcribe` subparser (§11). |
| `src/director/tighten.py` | Transcript evidence (§9): `Utterance` gains speaker/disfluency aggregates, `window_prompt` annotates them when present, `DEVELOPER` gains one sentence, `rule_beats` gains the transcript tie-break, new `attempt_transcript_stats` and `apply_transcript_evidence`, called from `run()` before spans are recomputed. |
| `src/director/cutlist.py` | `recompute_spans` only: Muse-only words (`heard_by == "muse"`) never anchor a span edge (§9.4). All other functions unchanged. |
| `tests/test_transcribe.py` | New. §13 items 1–28. Tighten-side items live here too, so the feature's tests are one file; existing `test_tighten.py`/`test_cutlist.py`/`test_cut.py` expectations are untouched except where §13 says a prompt-format assertion is extended. |
| `README.md` | At build time: the transcribe paragraph and flags (§14). One "Who built what" row. |
| `AGENTS.md` | At build time: one Layout bullet (`transcribe.py`) and the `cut.py` bullet gains "words stage enriches with Voice Transcribe when available". |
| `docs/muse-code/BUILD-LOG.md` | At build time: one TRANSCRIBE section (§14). |

`sync.py`, `grade.py`, `reframe.py`, `export.py` and `app/` are **not
modified**. `sync`'s Whisper voting keeps using its own normalised
`TranscriptWord`; the enriched words are a words-stage product, never fed
back into sync. The voice build is the one that touches `app/`.

Imports:

- `transcribe.py`: stdlib (`dataclasses`, `difflib`, `json`, `math`,
  `re`, `shutil`, `subprocess`, `pathlib`) plus
  `ghostcut.config` (`read_json`, `write_json`),
  `ghostcut.ffmpeg_tools` (`ToolingError`, `extract_media_stats`,
  `probe_media`, `run_command`), `from . import angles as angles_mod`.
  It never imports `cut`, `tighten` or `cutlist` (the dependency runs the
  other way). `guestlist` appears only inside `diarize_chunk` (§6.4).
- `cut.py` gains `from . import transcribe as transcribe_mod` at module
  top (transcribe imports only stdlib, ghostcut and `angles`, so tests
  can stub `director.cut.transcribe_mod.transcribe_audio_clip`).
- `tighten.py` and `cutlist.py` gain no imports; the new word fields are
  read with `getattr` (§1.3).

## 3. Constants

All in `transcribe.py` unless stated. Every value is decided here; the
build does not tune them.

| Constant | Value | Why |
|---|---|---|
| `TRANSCRIBE_MODEL` | `"muse-voice-transcribe-1.0"` | The `guestlist.meta.transcribe.MODEL` value, mirrored so payloads and messages can name it without importing `guestlist`. Never a `-contributor` id. |
| `TRANSCRIBE_SR` | `16000` | One of the two sample rates `diarize` accepts (16/24 kHz mono 16-bit WAV). The same value VOICE-DESIGN §3 uses, so the voice build's extraction is identical. |
| `CHUNK_TARGET_S` | `180.0` | Aim: chunks of a few minutes (brief). 3 min per request is far above the 60 s measured-good length and far below any plausible per-request cap. |
| `CHUNK_MIN_S` | `60.0` | A chunk shorter than this is merged forward rather than sent alone, unless it is the recording's tail. |
| `CHUNK_MAX_S` | `300.0` | Hard planning bound: the planner searches for a pause up to here before extending (§6.2 step 4). |
| `CHUNK_SEARCH_S` | `45.0` | The window around the target point where the planner prefers the longest pause (§6.2 step 3). |
| `CHUNK_PAUSE_S` | `0.6` | Minimum inter-word gap that counts as a pause for chunking. The same value as `angles.PAUSE_S`: a gap Director already treats as a break. |
| `SILENCE_NOISE_DB` | `"-40dB"` | `silencedetect` noise floor (§6.1). Room tone on a lav sits below this; speech does not. |
| `SILENCE_MIN_S` | `0.40` | `silencedetect` minimum duration: shorter dips are breaths and mouth noise, not chunk boundaries. |
| `CHUNK_OVERLAP_S` | `8.0` | Audio overlap between consecutive chunks (§6.5): the next chunk's WAV starts this far before its core starts, so the same speech is diarized twice and speaker labels can be voted across the seam. |
| `SPEAKER_MATCH_MIN_OVERLAP_S` | `0.30` | Minimum time overlap for two turns from adjacent chunks to vote that their raw speakers are the same person (§6.5 step 3). |
| `SPEAKER_MATCH_MIN_SIM` | `0.60` | Minimum normalised-text similarity for that vote (`difflib` ratio over normalised tokens, the `tighten.text_similarity` measure). |
| `ALIGN_MIN_SHARE` | `0.40` | Below this matched share the alignment is discarded and Whisper alone is kept (§7.6): the Muse transcript is probably of different audio. |
| `ALIGN_WARN_SHARE` | `0.70` | Below this (but at or above `ALIGN_MIN_SHARE`) the result is kept with the warning `transcribe_alignment_low:<share>`. |
| `TRANSCRIBE_PER_SECOND_USD` | `0.18 / 3600` | Mirrored from `guestlist.meta.pricing.TRANSCRIBE_PER_SECOND` so the pre-call estimate needs no `guestlist` import (the OPEN-DESIGN §3 mirror discipline). |
| `FILLER_TOKENS` | `{"um", "uh", "umm", "uhh", "erm", "er", "ah", "hmm", "mm", "mhm"}` | The filler set for `disfluency` (§7.5). Deliberately small and closed: anything not in it is disfluent only by immediate repetition. |
| `MAIN_SPEAKER` | `"talent"` | The main speaker's public name (brief §6). Others are `other-1`, `other-2`, … (§6.5 step 5). No real names ever. |

In `angles.py`: `TimedWord` gains three fields with defaults
(`speaker: str = ""`, `heard_by: str = "whisper"`,
`disfluency: bool = False`), so every existing construction
(`TimedWord(text, start, end)`) keeps working positionally.

In `tighten.py` (§9): `DISFLUENCY_SWAP_MIN` = `2` (a pick must have at
least this many more disfluencies than an alternate before the mechanical
swap fires) and `SPEAKER_TALENT_MIN_SHARE` = `0.50` (an attempt whose
talent share is below this, with a different dominant speaker, is not
talent speaking).

## 4. Data classes

All `@dataclass(slots=True)`. Times are seconds (float), clip-local in
`transcribe.py` until the shift in §8.2, money is USD (float).

In `transcribe.py`:

```python
class TranscribeError(RuntimeError):
    """A transcribe step failed in a way the operator must see.
    The message always starts with 'transcribe: '."""

class MuseUnavailable(RuntimeError):
    """guestlist (the Meta API client) cannot be imported."""

@dataclass(slots=True)
class Chunk:
    id: int                # C0.. in clip order (JSON stores the int)
    core_start_s: float    # the span this chunk owns; cores tile [0, duration]
    core_end_s: float
    start_s: float         # WAV start actually sent (core_start - overlap)
    end_s: float           # WAV end actually sent (== core_end)

@dataclass(slots=True)
class RawTurn:
    chunk_id: int
    raw_speaker: str       # the speaker string exactly as returned
    start_s: float         # clip-local, after the chunk offset (§6.4)
    end_s: float
    text: str              # turn transcript, stripped

@dataclass(slots=True)
class MuseTurn:
    id: int                # T0.. in clip order (JSON stores the int)
    start_s: float
    end_s: float
    speaker: str           # public name: "talent" | "other-1" | ...
    raw_speaker: str       # the chunk-local raw label it came from
    text: str
    chunk_id: int

@dataclass(slots=True)
class MuseToken:
    text: str              # original token, punctuation/casing kept
    norm: str              # normalize_token(text); "" for pure punctuation
    turn_id: int           # index into the MuseTurn list it came from
    speaker: str           # that turn's public speaker name

@dataclass(slots=True)
class AlignStats:
    whisper_words: int     # whisper units (§7.2), not raw words
    muse_tokens: int       # muse units (§7.2)
    matched: int           # units paired equal/replace
    muse_only: int         # unpaired muse units
    whisper_only: int      # unpaired whisper units
    matched_share: float   # matched / whisper_words, 0.0 when no whisper units

@dataclass(slots=True)
class TranscribeResult:
    method: str            # "muse" | "whisper" (fallback; words are Whisper's)
    words: list[angles_mod.TimedWord]  # aligned, in timeline order (§7)
    turns: list[MuseTurn]              # [] on fallback
    speakers: list[dict]               # §5.2 speakers array; [] on fallback
    chunks: list[Chunk]                # [] on fallback
    stats: AlignStats | None           # None on fallback
    spend_usd: float                   # 0.0 on fallback and on full cache hits
    warnings: list[str] = field(default_factory=list)
```

`TranscribeResult.words`/`turns` are returned in **clip-local** time;
the caller shifts (§8.2). A fallback result carries the input Whisper
words object-for-object (same texts, times, default fields), so a caller
that writes it out produces byte-identical words to the pre-feature
pipeline.

In `angles.py`, `TimedWord` becomes:

```python
@dataclass(slots=True)
class TimedWord:
    text: str
    start_s: float
    end_s: float
    speaker: str = ""          # "talent" | "other-1" | ... ; "" = unknown
    heard_by: str = "whisper"  # "whisper" | "both" | "muse"
    disfluency: bool = False
```

In `tighten.py`, `Utterance` gains (TIGHTEN-DESIGN §4 is otherwise
unchanged):

```python
    speaker: str = ""              # majority speaker by word duration
    disfluency_count: int = 0      # words in the slice with disfluency
    muse_only_count: int = 0       # words in the slice with heard_by muse
```

No new dataclasses in `cut.py` or `cutlist.py`. `cut.JobOptions` gains
one field, `no_transcribe: bool` (§8.1).

### 4.1 Every public function in `transcribe.py`

| Signature | Job |
|---|---|
| `normalize_token(text: str) -> str` | §7.1 normalisation. Pure. |
| `tokenize_turn(text: str) -> list[str]` | Whitespace split, empties dropped. Pure. |
| `extract_wav_command(src: Path, wav: Path) -> list[str]` | §6.1 full-WAV command. Pure. |
| `chunk_wav_command(full_wav: Path, start_s: float, duration_s: float, out: Path) -> list[str]` | §6.3 chunk-WAV command. Pure. |
| `silence_detect_command(wav: Path) -> list[str]` | §6.1 silencedetect command. Pure. |
| `parse_silence_spans(stderr: str, duration_s: float) -> list[tuple[float, float]]` | §6.1 stderr → silence spans; a trailing unpaired start closes at `duration_s`. Pure. |
| `pauses_from_words(words, min_gap_s: float = CHUNK_PAUSE_S) -> list[tuple[float, float]]` | Inter-word gaps ≥ `min_gap_s` as `(prev.end_s, next.start_s)` pauses. Pure. Takes `TimedWord`-shaped triples. |
| `plan_chunks(duration_s: float, pauses: list[tuple[float, float]]) -> list[Chunk]` | §6.2 planner. Pure. |
| `estimate_transcribe_usd(duration_s: float) -> float` | `duration_s * TRANSCRIBE_PER_SECOND_USD`. Pure. |
| `diarize_chunk(wav: Path) -> tuple[dict, float, bool]` | §6.4: `(diarize result, spend_usd, cached)`. The only `guestlist` import. Raises `MuseUnavailable` on `ImportError`; every other failure propagates. |
| `reconcile_speakers(chunk_turns: dict[int, list[RawTurn]], chunks: list[Chunk]) -> tuple[list[MuseTurn], list[dict], list[str]]` | §6.5: `(turns, speakers, warnings)` in clip-local time with public speaker names. Pure. |
| `speaker_for_time(turns: list[MuseTurn], t_s: float) -> str` | §7.4: public speaker of the turn a moment falls in; `""` when no turn is within 0.5 s. Pure. |
| `align_words(whisper_words: list[angles_mod.TimedWord], turns: list[MuseTurn]) -> tuple[list[angles_mod.TimedWord], AlignStats]` | §7: the alignment. Pure. |
| `build_transcript_payload(*, audio_label: str, clip_path: str, offset_s: float, duration_s: float, result: TranscribeResult) -> dict` | §5.2 payload from a result already shifted to timeline time. Pure. |
| `transcribe_audio_clip(*, clip_path: Path, whisper_words: list[angles_mod.TimedWord], duration_s: float, work_dir: Path, max_usd: float) -> TranscribeResult` | §6–§7 orchestrator: extract → chunk → diarize → reconcile → align. Never raises for a Meta-side failure; it returns a `method="whisper"` result with the §12 warning instead. Raises `TranscribeError` only when no words can be produced at all (no audio, unprobeable clip). |
| `run(*, sync_path: Path, words_path: Path, out_path: Path, transcript_path: Path, audio_label: str \| None, max_usd: float, no_muse: bool) -> dict` | Standalone (§11.2): the same orchestrator over named files. Returns the `transcript.json` payload (also written), or the whisper fallback payload of §5.2 when transcription is skipped/unavailable. |

Internal helpers the build implements but no other module calls (listed
so the builder does not invent others): `_number_units` (§7.2 number
grouping), `_parse_number_phrase` (§7.2 grammar), `_is_disfluency`
(§7.5), `_subdivide` (§7.3 interval splitting), `_merged_pauses`
(§6.1 step 3), `_text_similarity` (`difflib.SequenceMatcher` ratio
over normalised token strings — the same measure
`tighten.text_similarity` defines; duplicated here because
`transcribe` must not import `tighten`, §2).

## 5. Data contracts

Times in both files are **shared-timeline seconds** (post-shift, §8.2),
rounded to 3 decimals; money to 4. File names, never full local paths,
in anything that points inside the job directory (the OPEN-DESIGN §10
manifest rule applies to `transcript.json` too).

### 5.1 `words.json` — unchanged shape, three optional fields

The current shape is kept exactly: `{"words": [{"word", "start",
"end"}, …]}`. When (and only when) a word carries transcript
information, three optional keys are added per entry; a reader that
ignores them — including every pre-feature reader — sees the file it
already understands.

```json
{
  "words": [
    {"word": "So", "start": 190.786, "end": 190.906,
     "speaker": "talent", "heard_by": "both"},
    {"word": "the", "start": 190.926, "end": 191.006,
     "speaker": "talent", "heard_by": "both"},
    {"word": "the", "start": 191.006, "end": 191.026,
     "speaker": "talent", "heard_by": "muse", "disfluency": true},
    {"word": "problem", "start": 191.026, "end": 191.386,
     "speaker": "talent", "heard_by": "both"},
    {"word": "three", "start": 192.100, "end": 192.240,
     "speaker": "talent", "heard_by": "both"},
    {"word": "rolling", "start": 187.500, "end": 187.820,
     "speaker": "other-1", "heard_by": "both"}
  ]
}
```

(In a real file the words are in timeline order; the last entry above
is shown out of order only to keep the example short.)

Field rules (decided):

- `word` is the **Muse token's original text** for `both`/`muse`
  words (verbatim, punctuation and casing as Muse returned it) and
  the Whisper word's text for `whisper` words. The transcript of
  record is Muse's text on Whisper's clock; Whisper's spelling of a
  matched word survives only in the alignment stats' `whisper_only`
  count, never as a competing text.
- `speaker` is present on every word of a Muse-enriched file
  (`"talent"`, `"other-1"`, …; §6.5 naming). It is omitted only when
  no turn lies within 0.5 s of the word (§7.4) — in practice, words in
  long silences at the head/tail. Tighten treats a missing/`""`
  speaker as unknown, never as talent (§9.2).
- `heard_by` is `"both"` (aligned Muse token on a Whisper time),
  `"muse"` (Muse-only token on an interpolated time, §7.3), or
  `"whisper"` (Whisper-only word Muse did not return). It is omitted
  in a Whisper-only fallback file, where every word would say
  `"whisper"` and the pre-feature shape is preserved byte-for-byte.
- `disfluency` is present only when `true` (§7.5). Omitting `false`
  keeps the common word at its current size.
- A Muse-only word's `start`/`end` lie inside the inter-word gap it
  belongs to (§7.3 step 6) and may be zero-length when that gap is
  closed. Muse-only words are **never used as a cut point by
  themselves**: `cutlist.recompute_spans` never anchors an edge on
  one (§9.4), and `angles` cut points remain inter-*line* midpoints
  over a file whose line-internal gaps can only shrink, never move
  outside the original pause.

`cut.words_file_payload(words)` is the one writer of this shape. It is
extended, not replaced: each entry starts as today's
`{"word", "start", "end"}` and gains `speaker` when
`word.speaker` is non-empty, `heard_by` when `word.speaker` is
non-empty or `word.heard_by != "whisper"`, and
`"disfluency": True` when `word.disfluency` is true.
`angles.load_timed_words` is the one reader: it reads the three keys
with `w.get("speaker") or ""`, `w.get("heard_by") or "whisper"`,
`bool(w.get("disfluency"))`, carries them through its missing-`end`
fill and sort unchanged, and ignores unknown keys as today.
`cut.shift_words` constructs the shifted `TimedWord` with all six
fields, so a shift can never strip the enrichment.

### 5.2 `transcript.json` — turns, speakers, chunks, stats

Written beside `words.json` in the job directory (`<out_dir>/
transcript.json` in a `cut` job; `--transcript` path standalone).
Written **only** on a Muse success (including a low-but-kept
alignment, §7.6). On any Whisper fallback no file is written, so
"does `transcript.json` exist" is the test every later stage needs;
a stale file from a previous run is removed by the words stage when
the new run falls back, and the stage record's outputs say which case
happened (§8.3). The example below abbreviates `chunks` and `turns`
to their first entries; a real file lists every chunk and turn.

```json
{
  "version": 1,
  "audio": {"label": "C", "clip_path": "lav.wav", "offset_s": 178.746},
  "method": "muse",
  "model": "muse-voice-transcribe-1.0",
  "duration_s": 612.400,
  "spend_usd": 0.0306,
  "speakers": [
    {"id": "talent", "raw_speakers": ["SPEAKER_00", "SPEAKER_01"],
     "duration_s": 540.120, "turns": 14, "words": 798},
    {"id": "other-1", "raw_speakers": ["SPEAKER_01"],
     "duration_s": 18.340, "turns": 3, "words": 21}
  ],
  "chunks": [
    {"id": 0, "core_start_s": 178.746, "core_end_s": 359.946,
     "start_s": 178.746, "end_s": 359.946, "turns": 5, "spend_usd": 0.0090},
    {"id": 1, "core_start_s": 359.946, "core_end_s": 541.200,
     "start_s": 351.946, "end_s": 541.200, "turns": 6, "spend_usd": 0.0095}
  ],
  "turns": [
    {"id": 0, "start_s": 186.246, "end_s": 190.700,
     "speaker": "other-1", "raw_speaker": "SPEAKER_01",
     "text": "rolling", "chunk_id": 0},
    {"id": 1, "start_s": 190.786, "end_s": 372.100,
     "speaker": "talent", "raw_speaker": "SPEAKER_00",
     "text": "So the the problem is timecode drifting between cameras.",
     "chunk_id": 0}
  ],
  "alignment": {"whisper_words": 812, "muse_tokens": 835,
                "matched": 790, "muse_only": 45, "whisper_only": 22,
                "matched_share": 0.9729},
  "warnings": []
}
```

Field rules (decided; `version` bumps on any incompatible change):

- `speakers` is ordered `talent` first, then `other-1`, `other-2`, …
  by total turn duration descending (ties: first appearance).
  `raw_speakers` lists every chunk-local raw label that was mapped to
  the public name, in first-mapped order — the audit trail for §6.5.
  `words` counts aligned words carrying that speaker (words with no
  speaker are counted nowhere).
- `chunks[].core_*` are the owned spans (tiling the clip, shifted);
  `start_s`/`end_s` are the WAV spans actually sent, so
  `start_s < core_start_s` on every chunk after the first shows the
  8 s overlap. `turns` counts core turns kept from that chunk (§6.5
  step 4); `spend_usd` is that chunk's actual spend (0.0 on a cache
  hit, §6.4).
- `turns[].text` is the diarizer's turn text verbatim (stripped).
  Turn times are the diarizer's, shifted — they are *not* snapped to
  word times; a 60 s single-speaker turn stays a 60 s turn, and §7 is
  what reconciles that coarseness with the word clock.
- `alignment` is the `AlignStats` of §4 serialised, `matched_share`
  rounded to 4. The counts are in **units** (§7.2): a spoken "twenty
  four" matched against a Whisper "24" is one matched unit.
- On fallback there is no file; the standalone `run()` return value
  for that case is the in-memory equivalent with `"method":
  "whisper"`, `"turns": []`, `"speakers": []`, `"chunks": []`,
  `"alignment": null`, `"spend_usd": 0.0` and the §12 warning in
  `"warnings"` — the same shape, so a caller can log it uniformly.

## 6. Transcription: WAV, chunks, speakers

All of this section is `transcribe_audio_clip` in `transcribe.py`.
Its inputs are the production-audio clip file, that clip's Whisper
words in **clip-local** time, the clip's probed duration, a work
directory and the remaining `--max-usd` budget. Its output is a
`TranscribeResult` in clip-local time (§4). Steps run in this order;
nothing reorders them.

### 6.1 Extract the audio and find the silences

The clip (a camera file, an iPhone clip, a lav `.wav`/`.mp3`) first
becomes the WAV `diarize` requires. The original file is never
uploaded; only the derived chunk WAVs of §6.3 are.

```python
def extract_wav_command(src: Path, wav: Path) -> list[str]:
    return ["ffmpeg", "-v", "error", "-y", "-i", str(src),
            "-vn", "-ac", "1", "-ar", str(TRANSCRIBE_SR),
            "-c:a", "pcm_s16le", str(wav)]

def silence_detect_command(wav: Path) -> list[str]:
    return ["ffmpeg", "-v", "info", "-i", str(wav),
            "-af", f"silencedetect=noise={SILENCE_NOISE_DB}:d={SILENCE_MIN_S}",
            "-f", "null", "-"]
```

The full WAV is written to `<work_dir>/audio.wav` (`<out_dir>/
transcribe/audio.wav` in a `cut` job) via `run_command`. A clip with
no audio stream never reaches here from `cut` (the sync payload's
`has_audio` is checked first, §8.2 step 2); standalone, an ffmpeg
failure with no WAV produced becomes
`TranscribeError(f"transcribe: no audio could be extracted from
{clip_path.name}")`. Duration comes from the caller
(`probe_media` + `extract_media_stats` in `cut`; the sync payload's
`camera.duration_s` standalone); `duration_s <= 0` becomes
`TranscribeError("transcribe: audio clip has no duration")`.

Silences are found two ways and merged, because each alone has a
hole: `silencedetect` hears every quiet gap, including gaps inside a
Whisper word it mis-timed; the Whisper word gaps know exactly where
words are, including quiet speech `silencedetect` misses.

1. Run `silence_detect_command` with `check=False`;
   `parse_silence_spans(stderr, duration_s)` pairs each
   `silence_start: <t>` line with the next `silence_end: <t>` line
   (ffmpeg prints them in order on stderr; a trailing unpaired start
   closes at `duration_s`). Garbage or a failed run gives `[]`,
   never an error.
2. `pauses_from_words(whisper_words)` gives every inter-word gap
   `(prev.end_s, next.start_s)` with `next.start_s - prev.end_s >=
   CHUNK_PAUSE_S`.
3. `_merged_pauses(silences, word_pauses, whisper_words)` builds
   the pause list the planner sees: keep a silence span only when
   it contains no part of any Whisper word's `[start_s, end_s]`
   (a silence that swallows a word is a mis-timing, dropped), trim
   each kept span by 0.10 s at each edge and drop it if that
   leaves `start >= end`; then add every word gap from step 2 that
   overlaps no kept silence span. Sorted by start, duplicates
   removed. If step 1 gave nothing (stubbed ffmpeg in tests,
   unusual build), the list is step 2 alone and the result gains
   the warning `transcribe_silence_fallback:words`. Either way,
   every pause in the list lies strictly between two words or in
   word-free audio, so a boundary placed inside it (§6.2) can
   never fall inside speech — the brief's rule, enforced by
   construction rather than by checking afterwards. The work
   directory is created (`mkdir(parents=True, exist_ok=True)`)
   before the full WAV is written.

### 6.2 Plan the chunks — `plan_chunks`

```python
def plan_chunks(duration_s: float, pauses: list[tuple[float, float]]) -> list[Chunk]:
    """Cores tiling [0, duration_s]; boundaries only inside pauses. Pure."""
```

Algorithm, in order. `pauses` are `(start_s, end_s)` as in §6.1,
sorted; a pause's midpoint is `(start + end) / 2` and its length is
`end - start`.

1. If `duration_s <= CHUNK_MAX_S`, return one chunk:
   `Chunk(0, 0.0, duration_s, 0.0, duration_s)`. A 60 s talking head
   (the brief's measured case) is always exactly one request.
2. Otherwise walk `core_start = 0.0`, emitting one chunk per step:
   - Desired boundary: `core_start + CHUNK_TARGET_S`.
   - **Step 3 (preferred):** among pauses whose midpoint lies in
     `[desired - CHUNK_SEARCH_S, desired + CHUNK_SEARCH_S]`, pick
     the longest (ties: closest midpoint to desired, then
     earliest). Boundary = its midpoint.
   - **Step 4 (extended):** if step 3 found none, among pauses
     whose midpoint lies in `[core_start + CHUNK_MIN_S,
     core_start + CHUNK_MAX_S]`, pick the longest (same ties).
     Boundary = its midpoint.
   - **Step 5 (no pause in reach):** take the first pause whose
     midpoint is after `core_start + CHUNK_MAX_S`; boundary = its
     midpoint, even though the chunk is now longer than
     `CHUNK_MAX_S`. If there is no later pause at all, the chunk
     runs to `duration_s` and the walk ends. A long chunk is a
     known risk against the unknown per-request cap (§12), but
     cutting inside speech is a certain defect; the design chooses
     the risk, and §6.4's per-chunk failure makes even that a
     Whisper fallback, not a lost job.
3. The chunk's core is `[core_start, boundary]`; its WAV span is
   `[max(0.0, core_start - CHUNK_OVERLAP_S) if id > 0 else
   core_start, boundary]`. Set `core_start = boundary` and repeat
   from step 2 until the remaining span is `<= CHUNK_MAX_S`, when
   it becomes the final chunk with `core_end = end = duration_s`.
4. A final core shorter than `CHUNK_MIN_S` is merged into the
   previous chunk (previous `core_end`/`end` become `duration_s`),
   unless it is the only chunk. Merging can only lengthen the
   previous chunk past `CHUNK_MAX_S` by under a minute; one
   slightly long request beats a tiny one whose diarizer turns are
   mostly overlap.

Worked example (decided numbers, asserted in §13 item 4): a
612.400 s clip with word-gap pauses at 178.2–179.1, 355.0–356.4,
470.0–470.8 and 590.0–591.0 plans as cores `[0, 178.65]`,
`[178.65, 355.70]`, `[355.70, 612.40]` — the third pause at 470.4
is inside the last core, not a boundary, because the remaining
256.7 s after 355.70 is within `CHUNK_MAX_S` and becomes the final
chunk whole. WAV spans are `[0, 178.65]`, `[170.65, 355.70]`,
`[347.70, 612.40]`.

### 6.3 Slice and send each chunk

```python
def chunk_wav_command(full_wav: Path, start_s: float,
                      duration_s: float, out: Path) -> list[str]:
    return ["ffmpeg", "-v", "error", "-y",
            "-ss", f"{start_s:.3f}", "-i", str(full_wav),
            "-t", f"{duration_s:.3f}",
            "-ac", "1", "-ar", str(TRANSCRIBE_SR),
            "-c:a", "pcm_s16le", str(out)]
```

Each chunk's WAV is `<work_dir>/chunk-<id:03d>.wav`, cut from the
full WAV of §6.1 (PCM seeking is sample-accurate; cutting from the
derived WAV instead of the camera original is also what guarantees
the sent audio is 16 kHz mono regardless of the source container).
`-ss` precedes `-i` deliberately: for PCM WAV every frame is a
keyframe, so fast seek loses nothing. Chunks are extracted and sent
**in order, one at a time** — the speaker vote in §6.5 needs the
previous chunk's turns, and serial calls keep the spend gate of
§6.4 exact.

### 6.4 The `diarize` wrapper and the spend gate — `diarize_chunk`

```python
def diarize_chunk(wav: Path) -> tuple[dict, float, bool]:
    """(result, spend_usd, cached). The only guestlist import."""
    try:
        from guestlist.meta import cache, pricing
        from guestlist.meta import transcribe as meta_transcribe
    except ImportError as error:
        raise MuseUnavailable(str(error)) from error
    request = {"mode": "DIARIZATION", "model": meta_transcribe.MODEL,
               "audioEncoding": "WAV"}
    key = cache.key(cache.sha256_file(wav), meta_transcribe.MODEL, request)
    cached = cache.get("transcribe", key) is not None
    result = meta_transcribe.diarize(wav)
    if cached:
        return result, 0.0, True
    seconds = float(result.get("audioDurationMs") or 0) / 1000.0
    return result, pricing.transcribe_cost(seconds), False
```

Notes the builder must keep: `language_bias` is never passed (the
request dict above is then exactly the one `diarize` builds, §1.4);
the result is validated by the caller, not here; spend on a miss is
recomputed from the response's own `audioDurationMs` via the public
`pricing.transcribe_cost`, matching what `diarize` charges.

The gate runs in `transcribe_audio_clip` **before any chunk is
extracted or sent**, over the whole clip:

```python
estimate = estimate_transcribe_usd(duration_s)  # duration * $0.00005
```

If `estimate > max_usd` (the remaining job budget handed in by the
caller), no WAV is extracted, no chunk is sent, and the result is
the Whisper fallback with the warning
`f"transcribe_over_budget:est ${estimate:.2f} over remaining
${max_usd:.2f}"`. Transcription is all-or-nothing: a transcript
covering half the clip with speakers and half without would make
tighten's speaker evidence silently change meaning mid-recording,
which is worse than no enrichment. (A 3-minute demo clip estimates
at $0.0090; an hour at $0.18. The gate exists for the `--max-usd 0`
offline form and for pathological durations, not for real talks.)

Per chunk, in order: extract (§6.3), call `diarize_chunk`, offset
its turns to clip time (`chunk.start_s + startMs/1000`,
`chunk.start_s + endMs/1000`), drop turns with empty text or
`end <= start`, collect as `RawTurn`s under the chunk id. **Any**
exception from any chunk — `MuseUnavailable`, the `RuntimeError`
of a missing key, an HTTP `RuntimeError`, a `ToolingError` from
extraction — aborts the whole transcription: partial turns are
discarded, and the result is the Whisper fallback with the warning
`f"transcribe_failed:{type(exc).__name__}: {str(exc)[:160]}"`
(`transcribe_unavailable:<error>` instead when the exception is
`MuseUnavailable`, the expected no-guestlist case, mirroring
OPEN-DESIGN's `qc_muse_unavailable` split). Zero usable turns
across all chunks (silent audio, or a diarizer that returns only
empties) is likewise a fallback with `transcribe_no_turns`.

### 6.5 Reconcile speakers across chunks — `reconcile_speakers`

Speaker ids may restart per request (brief): chunk 1's
`"SPEAKER_01"` is not necessarily chunk 0's. Reconciliation maps
every chunk-local raw label to a provisional global id, then names
the globals. In order:

1. **Chunk 0.** Each distinct raw label gets a provisional id
   (`G0`, `G1`, …) in order of its total turn duration in the
   chunk, longest first (ties: first appearance). The longest
   first guess matters: in talking video the talent dominates
   almost every window, so `G0` is the talent candidate from the
   start.
2. **Overlap turns.** For chunk `n > 0`, the overlap interval is
   `[chunk.start_s, chunk.core_start_s)` — audio the previous
   chunk also sent (§6.2). Collect the previous chunk's kept turns
   and this chunk's raw turns that intersect that interval.
3. **Votes.** For every (previous turn, current turn) pair whose
   time overlap is `>= SPEAKER_MATCH_MIN_OVERLAP_S` (0.30 s) and
   whose `_text_similarity` (normalised-token `difflib` ratio) is
   `>= SPEAKER_MATCH_MIN_SIM` (0.60), add the overlap duration as
   a vote that the current raw label maps to the previous turn's
   provisional id. The text condition is what stops two people
   talking over the seam from voting for each other on time alone.
4. **Assignment.** Sort all votes descending (ties: larger text
   similarity, then earlier current turn). Greedily assign each
   current raw label to its highest-voted provisional id not
   already taken by another raw label of this chunk (the mapping
   within a chunk is one-to-one: one raw label is one person in
   that chunk's own diarization). Raw labels with no surviving
   vote get fresh provisional ids, in duration order as in step 1.
   If the chunk produced **no votes at all** (no speech in the
   overlap, or every similarity failed), fall back for the whole
   chunk to duration-rank mapping — raw labels sorted by duration
   mapped onto existing provisionals sorted by cumulative duration
   — and add the warning `f"transcribe_speaker_fallback:{chunk_id}"`.
   The fallback is labelled because it is a guess; the overlap vote
   is not, because it is evidence.
5. **Keep core turns only.** After mapping, a turn is kept when
   its midpoint lies in its chunk's core `[core_start_s,
   core_end_s)` (the final chunk includes its `core_end_s`). This
   deduplicates the overlap: overlapped speech is kept from the
   earlier chunk's transcription, and the later chunk's copy
   served only as voting evidence. Kept turns are sorted by start
   and renumbered `T0…` as `MuseTurn`s with provisional ids still
   attached.
6. **Naming.** Sum kept-turn durations per provisional id. The
   largest becomes `"talent"`; the rest become `"other-1"`,
   `"other-2"`, … in descending duration (ties: first
   appearance). `MuseTurn.speaker` and the §5.2 `speakers` array
   (with each public name's `raw_speakers` audit list, durations,
   turn counts) are built from this mapping. A single-speaker
   recording has exactly one entry, `talent`.

A chunk boundary placed in a pause (§6.2) is what makes step 5
safe: no turn should straddle a core boundary, because the
boundary sits inside word-free audio. If a diarizer nonetheless
returns a turn crossing one, the midpoint rule keeps it whole in
exactly one chunk — it is never split, and its text is never
duplicated.

## 7. Alignment: Muse's tokens on Whisper's clock

`align_words(whisper_words, turns)` is pure and runs entirely in
clip-local time. It takes the Whisper words as loaded (raw text,
times) and the reconciled `MuseTurn`s, and returns the aligned
word list plus `AlignStats`. Steps run in this order.

### 7.1 Normalisation — `normalize_token`

```python
def normalize_token(text: str) -> str:
    """Lower-case, strip edge punctuation, fold apostrophes. Pure."""
```

In order: lower-case; strip leading/trailing characters that are
not letters, digits or an internal apostrophe (`"Timecode."` →
`"timecode"`, `"'quoted'"` → `"quoted"`, `"don't"` → `"don't"`);
a token of pure punctuation normalises to `""` and is dropped from
both streams before alignment (it carries no word). Casing,
punctuation and possessives therefore never block a match.
Hyphenated tokens (`"twenty-four"`) keep their hyphen here; §7.2's
number grouping splits on it.

### 7.2 Units and the number rule

Alignment operates on **units**, not raw tokens, so that a number
spoken once can be written two ways without breaking the match.

- A **Whisper unit** is one Whisper word, or a maximal run of
  consecutive Whisper words forming one number phrase (§7.2
  grammar). It records its word indices and the interval
  `[first.start_s, last.end_s]`.
- A **Muse unit** is one Muse token, or a maximal run of
  consecutive tokens *within one turn* forming one number phrase.
  It records its token indices (turn, position) and its turn's
  speaker. A unit never crosses a turn boundary: numbers do not
  span speakers.

`_parse_number_phrase(norms: list[str], i: int) -> tuple[str, int]
| None` recognises a number starting at position `i` in a
normalised token list (hyphenated tokens pre-split on `"-"` for
this test only; the original tokens are untouched) and returns
`(canonical, consumed_count)`. Canonical form is the digit string
(`"3"`, `"24"`, `"2026"`, `"3.5"`). Grammar, exactly:

- Word values: ones/teens `zero`–`nineteen` (0–19), tens
  `twenty`–`ninety` (20–90), scales `hundred` (×100 on the current
  group, requires a preceding group), `thousand`/`million`
  (flush `(current or 1) * scale` into the total), and `point`
  followed by single-digit words/digits for the fraction.
  Accumulation is the standard English rule: `current` sums
  ones/tens, `hundred` multiplies `current`, a scale flushes.
  `"two thousand twenty six"` → total 2000, current 26 →
  `"2026"`. `"twenty four"` → `"24"`. `"three"` → `"3"`.
- A digit token (`^\d[\d,]*(\.\d+)?$`, commas stripped) is a
  complete phrase of length 1 with canonical form `str(int(...))`
  for integers (leading zeros dropped) or the comma-stripped
  string for decimals.
- Anything else is not a number phrase. The parser is deliberately
  small: it covers counts, years, times and prices as actually
  spoken in talking video. A phrase it cannot parse (e.g. `"twenty
  twenty six"` for 2026) simply stays as separate non-number units
  and §7.3's replace-pairing still aligns it positionally — a
  number the grammar misses degrades to a substitution, never to
  a lost word.

Each unit's **norm key** is its canonical digit string for a
number unit, else its single token's `normalize_token`. Two units
match for alignment when their norm keys are equal strings.

### 7.3 The sequence alignment

Let `A` be the Whisper units' norm keys and `B` the Muse units',
both in time/text order.

1. `matcher = difflib.SequenceMatcher(a=A, b=B, autojunk=False)`;
   `opcodes = matcher.get_opcodes()`. `difflib` is stdlib, already
   the repo's similarity tool (TIGHTEN-DESIGN §7.5), deterministic
   for equal inputs, and its longest-match blocks are exactly the
   anchors a transcript alignment wants: long verbatim runs align
   first, and repeats/fillers fall out as small inserts between
   them.
2. Walk the opcodes in order, emitting words (§7.4–§7.5 decide
   each emitted word's speaker and disfluency):
   - **`equal`** — pair units index-for-index. Each pair emits the
     Muse unit's tokens over the Whisper unit's interval:
     `_subdivide(muse_tokens, start, end)` splits the interval
     among the tokens proportionally to `len(token) + 1` (a
     one-token unit keeps the whole interval). Emitted words are
     `heard_by="both"`. This is also the number case: Whisper
     `"24"` (one word, 20.80–21.00) against Muse `"twenty four"`
     (one unit, two tokens) emits `twenty` ≈20.80–20.92 and
     `four` ≈20.92–21.00, each on a subdivided slice of the one
     Whisper time.
   - **`replace`** — pair units index-for-index up to
     `min(len_a, len_b)` exactly as `equal` (a mishearing is still
     the same spoken word: Muse's text wins, Whisper's time
     wins, `heard_by="both"`). Surplus Whisper units emit as
     `whisper`-only; surplus Muse units as `muse`-only (§7.3
     step 3).
   - **`delete`** (in `A`, not `B`) — Whisper-only units: emit
     each original Whisper word with its own time and text,
     `heard_by="whisper"`, `disfluency=False`.
   - **`insert`** (in `B`, not `A`) — Muse-only units: emit each
     token with an interpolated time (step 3 below),
     `heard_by="muse"`.
3. **Muse-only interpolation.** For each maximal run of
   consecutive Muse-only tokens (an `insert` block, plus any
   `replace` surplus): let `prev_end` be the end of the last
   anchor word emitted before the run (a `both` or `whisper`
   word), or the run's first token's turn start when there is
   none; let `next_start` be the start of the next anchor word,
   or the run's last token's turn end when there is none. Split
   `[prev_end, max(next_start, prev_end)]` into equal slices, one
   per token, in order. When the gap is closed (`next_start <=
   prev_end`), every token in the run gets `start == end ==
   prev_end`. The run's tokens therefore always sit inside the
   gap they belong to, in spoken order, and never move an anchor.
4. Output order is emission order. It is **not** re-sorted by
   time: anchor times are non-decreasing by construction, Muse-
   only times lie between their anchors, and a stable time sort
   would scramble zero-length Muse-only tokens against their
   neighbours. The list `angles.load_timed_words` will reload is
   already sorted in the sense that loader checks.

### 7.4 Speaker assignment — `speaker_for_time`

Every emitted word gets its speaker from **time**, not from the
alignment pairing (brief: "every Whisper word gets the speaker of
the turn it falls in"). For an anchor word (`both`/`whisper`), the
speaker is `speaker_for_time(turns, midpoint)` where midpoint is
the Whisper interval's centre: the turn containing the midpoint;
if none contains it, the turn with the greatest overlap with the
interval; if still none, the nearest turn whose edge is within
0.5 s; otherwise `""`. For a Muse-only word, the speaker is its
own token's turn speaker (its interpolated time is synthetic, and
the turn it was heard in is the only honest attribution). In
practice the two agree — a matched token's turn is the turn its
Whisper time falls in — and when chunk timing makes them disagree
by a turn boundary, time wins for anchors, because the render and
tighten both reason over times, not over pairings.

### 7.5 Disfluency — `_is_disfluency`

A Muse token (emitted as `both` or `muse`) is
`disfluency=True` when either holds, evaluated over the flat
Muse token stream in turn order:

- its norm is in `FILLER_TOKENS` (`um`, `uh`, …), or
- its norm is non-empty and equals the immediately previous Muse
  token's norm (an immediate repeat: the second `the` in "the
  the", the second `will` in "will will").

The first token of a repeat is not marked; the repeat is the
evidence. Whisper-only words are never disfluent (Whisper already
smoothed them; marking them would invent evidence). This is the
whole rule: half-started words Muse writes with a trailing
hyphen (`"th-"`) normalise to `"th-"`→`"th"`, match nothing, and
surface as Muse-only tokens — visible in the transcript, but not
labelled disfluency unless they also repeat or are fillers,
because a hyphen alone is Muse's spelling choice, not proof.

### 7.6 Stats and the alignment floor

`AlignStats` counts units: `matched` = units paired in `equal` +
`replace`; `muse_only`/`whisper_only` = unpaired units;
`matched_share = matched / whisper_words` (0.0 when there are no
Whisper units). Back in `transcribe_audio_clip`, after alignment:

- `matched_share < ALIGN_MIN_SHARE` (0.40): the Muse transcript
  is discarded (the result is the Whisper fallback) with the
  warning `f"transcribe_alignment_low:{matched_share:.2f}"`. Two
  transcripts of the same speech cannot agree on under 40% of
  units; keeping speakers from such an alignment would attribute
  crew talk by coin flip.
- `ALIGN_MIN_SHARE <= matched_share < ALIGN_WARN_SHARE` (0.70):
  kept, with the same `transcribe_alignment_low:<share>` warning.
- Otherwise kept silently. The stats are recorded in
  `transcript.json` either way it is kept (§5.2).

### 7.7 Worked examples (asserted in §13 items 9–11)

All times clip-local. Whisper words are `(text, start–end)`;
Muse turns give `(speaker, text)`.

**Example 1 — a repeat Whisper dropped.** Whisper: `So
10.00–10.12`, `the 10.14–10.22`, `problem 10.24–10.60`, `is
10.62–10.70`, `timecode 10.72–11.10`. Muse turn (talent,
9.98–11.20): "So the the problem is timecode." Units: Whisper
`[so, the, problem, is, timecode]`, Muse `[so, the, the, problem,
is, timecode]`. Opcodes: `equal so`, `insert the`, `equal
the…timecode` (difflib anchors the first `the`; which `the` is
"the repeat" is undecidable and does not matter — exactly one
`the` is Muse-only). Output, in order: `So` both 10.00–10.12;
`the` both 10.14–10.22; `the` **muse** 10.22–10.24 (the whole
0.02 s gap, one token), `disfluency=true` (immediate repeat of
the previous Muse token), speaker talent (its turn); `problem`
both 10.24–10.60; `is`, `timecode` both. Stats: 5 whisper units,
6 Muse units, matched 5, muse_only 1, whisper_only 0,
matched_share 1.00.

**Example 2 — a number as digits and as words.** Whisper: `We
20.00–20.10`, `shot 20.12–20.35`, `3 20.37–20.45`, `cameras
20.47–20.80`, `at 20.82–20.90`, `24 20.92–21.10`, `frames
21.12–21.40`. Muse turn (talent): "We shot three cameras at
twenty four frames". Number grouping: Whisper units `[we, shot,
3, cameras, at, 24, frames]` with canonical keys `[we, shot, "3",
cameras, at, "24", frames]`; Muse units `[We, shot, three,
cameras, at, twenty four, frames]` with keys `[we, shot, "3",
cameras, at, "24", frames]` (`three` → `"3"`; `twenty`+`four` →
one unit → `"24"`). All `equal`. Output: `three` both over
20.37–20.45 (Muse's word on Whisper's time); `twenty` both
≈20.92–21.01 and `four` both ≈21.01–21.10 (the 0.18 s Whisper
interval subdivided by token length); the rest one-to-one.
matched_share 1.00, no muse-only or whisper-only units.

**Example 3 — crew talk in a second speaker.** Whisper: `rolling
5.00–5.32`, `So 5.60–5.72`, `we 5.74–5.84`, `built 5.86–6.10`,
`three 6.12–6.30`, `cameras 6.32–6.66`. Muse turns:
(other-raw, 4.90–5.40) "rolling"; (talent-raw, 5.55–6.70) "So we
built three cameras". After §6.5 naming the first raw label is
the shorter speaker → `other-1`, the second → `talent`. Every
unit matches `equal`; speakers come from §7.4 time: `rolling`'s
midpoint 5.16 lies in the 4.90–5.40 turn → `other-1`; the rest
→ `talent`. Tighten (§9.2) sees a first utterance whose words
are 100% `other-1` and removes it as `crew_talk` with the reason
of §9.2; the talent sentence is untouched. Had Whisper *also*
dropped the crew word entirely, it would appear as a Muse-only
token at an interpolated time with speaker `other-1` — still
visible, still attributed, still never a cut anchor (§9.4).

## 8. Inside `director cut`: the words stage learns to transcribe

No stage is added. The post-tighten stage order stays
`sync → words → tighten → grade → angles → export → reframe → qc`
(TIGHTEN-DESIGN §11), and the words stage — which already resolves
the audio clip, obtains its Whisper words and shifts them — gains
one enrichment step between obtaining and shifting. Keeping it
inside `words` is deliberate: the stage's fingerprint (§8.3),
manifest section and console line already answer "where did the
words come from", and a separate `transcribe` stage would have to
re-answer all three for the same file.

### 8.1 Exactly which `cut.py` functions change, and how

| Function / place | Change |
|---|---|
| `JobOptions` | Gains `no_transcribe: bool` (after `no_muse`). `cli.command_cut` fills it from `--no-transcribe` (§11). The assumed `whisper_model: str` field (build-order note) is untouched. |
| `words_file_payload(words)` | Extended per §5.1: optional `speaker`/`heard_by`/`disfluency` keys from the new `TimedWord` fields. Signature unchanged; every existing caller and test keeps working. |
| `shift_words(words, offset_s)` | Constructs `TimedWord(word.text, start+off, end+off, word.speaker, word.heard_by, word.disfluency)`. Signature and pure-shift semantics unchanged. |
| `resolve_words(...)` | **Unchanged.** It still returns `(canonical_words_path, source, warnings)` with source in `{"explicit", "transcripts", "sync_cache", "mlx_whisper"}`, still shifts auto sources and writes `<out_dir>/words.json`, still uses an explicit file in place. Transcription is layered over its result by the stage (below), so OPEN-DESIGN §14 items 6–8 keep their exact meaning. |
| `mlx_whisper_command(...)` | Unchanged by this design (the `--whisper-model` threading is the assumed concurrent change). |
| `_fingerprint("words")` (§8.3) | Options dict gains `"no_transcribe"` and `"no_muse"`. |
| `_execute("words")` | Gains steps 2–5 of §8.2. Stage spend becomes the transcribe spend (was always 0.0). |
| `_reload("words")` | Re-reads the manifest's `audio.transcribe` (§8.3) with the word count; a missing key (pre-feature manifest) reloads as `{"method": "whisper", …}` defaults rather than failing the skip. |
| `_manifest(...)` | The `audio` section gains the `transcribe` key of §8.3. No other section changes. |
| Stage spend accounting (`spent_usd`) | The words record's `spend_usd` is added to `spent_usd` exactly as grade/angles/reframe spends are today, before the `tighten` and `grade` trailing gates read it. |

`sync.py` is not changed by this feature. In particular
`transcribe` never feeds enriched words back into `sync`'s Whisper
voting: sync's words are an offset-estimation input obtained before
any of this exists, and its `TranscriptWord` normalisation is a
different contract.

### 8.2 The words-stage execution, step by step

In `_execute("words")`, after the existing
`resolve_audio` + `resolve_words` (steps unchanged):

1. **Resolve Whisper words (existing).** `words_file, source,
   warnings = resolve_words(...)`; `validated =
   angles_mod.load_timed_words(words_file)`; `offset =
   float(audio_clip.offset_s)`. As today, `validated` is non-empty
   by construction.
2. **Eligibility.** Transcription is attempted only when **all**
   hold: `source != "explicit"` (an editor-supplied timeline file
   is used in place and never sent anywhere, OPEN-DESIGN §6.2 —
   enriching it would require unshifting their file and writing
   over the job's canonical copy, breaking the in-place contract);
   `not options.no_muse`; `not options.no_transcribe`; the audio
   clip's sync entry has `has_audio` true. When eligibility fails
   the stage records `transcribe.method` `"skipped"` with no
   warning (an explicit opt-out is not a defect and must not turn
   the job `complete_with_warnings`), deletes any stale
   `<out_dir>/transcript.json`, and continues exactly as today.
   No warning is added for `--no-muse`, `--no-transcribe` or
   `explicit`; the manifest records the method.
3. **Budget.** `remaining = options.max_usd - spent_usd` (spend
   so far is $0 at this point in the stage order, but the code
   uses the general expression). The estimate/gate itself lives
   in `transcribe_audio_clip` (§6.4) and returns a fallback result
   rather than raising, so this step never fails the stage.
4. **Transcribe in clip-local time.**
   `clip_local = shift_words(validated, -offset)` recovers the
   clip-local Whisper words `resolve_words` shifted (the 3-decimal
   round-trip is exact at the precision both files use).
   Then:
   ```python
   result = transcribe_mod.transcribe_audio_clip(
       clip_path=Path(str((audio_clip.get("camera") or {}).get("path") or "")),
       whisper_words=clip_local,
       duration_s=float((audio_clip.get("camera") or {}).get("duration_s") or 0.0),
       work_dir=out_dir / "transcribe",
       max_usd=remaining)
   ```
   `transcribe_audio_clip` internally returns a Whisper-fallback
   result for every Meta-side failure (§6.4), so no `try` wraps
   this call except for `TranscribeError` (no audio/duration —
   cannot arise from a placed, probed audio clip, but if it does
   it is caught and converted to the same fallback with warning
   `f"transcribe_failed:TranscribeError: {exc}"`, keeping the
   words stage's never-fail-on-transcription rule absolute).
5. **Write the enriched words and the transcript (Muse case).**
   `shifted = shift_words(result.words, offset)`; overwrite
   `<out_dir>/words.json` with `words_file_payload(shifted)`
   (the same file `resolve_words` wrote, now enriched — one
   canonical words file, as today). Build
   `payload = transcribe_mod.build_transcript_payload(
   audio_label=label, clip_path=<sync path string>,
   offset_s=offset, duration_s=<duration>,
   result=<shifted result>)` and
   `write_json(out_dir / "transcript.json", payload)`, where
   `<shifted result>` is
   `replace(result, words=shifted,
   turns=[replace(t, start_s=t.start_s + offset,
   end_s=t.end_s + offset) for t in result.turns])`
   (`replace` added to `cut.py`'s existing
   `from dataclasses import dataclass, field` import).
   `transcribe.py` itself never sees timeline time.
   Stage outputs become `["words.json", "transcript.json"]`;
   stage spend is `result.spend_usd`.
6. **Fallback case.** `words.json` stays exactly what
   `resolve_words` wrote (Whisper shape, §5.1); any stale
   `transcript.json` is unlinked (`missing_ok=True`); stage
   outputs stay `["words.json"]` (or `[]` for `explicit`); stage
   spend $0; `result.warnings` are appended to the stage warnings
   (this is what names the reason in `cut.json`, §8.3). The stage
   **succeeds**. There is no path where a transcription problem
   fails a `cut` job whose Whisper words are good.

The console line (OPEN-DESIGN §11.3 format, extended):
`words: done (812 words, sync_cache, shifted +178.746s)` becomes,
on a Muse success,
`words: done (835 words, sync_cache, shifted +178.746s, muse 97.3% matched, $0.0306)`
and on a fallback gains nothing (the warning prints as
`! words: transcribe_failed:…` with the other stage warnings).
`_cut_stage_line` in `cli.py` reads the parenthetical from the
manifest's `audio.transcribe` exactly as it reads today's fields;
counts come from the enriched file, so the words line names the
file `angles` will actually read.

### 8.3 Fingerprint, manifest and resume

- **Fingerprint.** The words stage's options dict (§8.1) gains
  the two flags, so toggling `--no-transcribe` (or `--no-muse`)
  re-runs `words` and, by dirty propagation, `tighten` and
  everything downstream — while an unchanged re-run skips and
  costs $0 twice over (stage skip, plus `guestlist`'s per-chunk
  cache if it ever does re-run). Input files are unchanged (the
  audio clip file's size/mtime already keys the transcription:
  replacing the lav re-transcribes).
- **Manifest.** `cut.json`'s `audio` section gains one key:
  ```json
  "transcribe": {"method": "muse", "model": "muse-voice-transcribe-1.0",
                 "spend_usd": 0.0306, "transcript_file": "transcript.json",
                 "speakers": ["talent", "other-1"], "chunks": 3,
                 "matched_share": 0.9729, "muse_only": 45, "whisper_only": 22}
  ```
  On fallback: `"method": "whisper"`, `"spend_usd": 0.0`,
  `"transcript_file": null`, `"speakers": []`, `"chunks": 0`,
  `"matched_share": null`, `"muse_only": 0, "whisper_only": 0`.
  On an explicit/opt-out skip: `"method": "skipped"`, the rest as
  fallback. `model` is `TRANSCRIBE_MODEL` for `muse`/`whisper`,
  `null` for `skipped`. Top-level `warnings` gain nothing; the
  fallback reason lives in the words stage record's `warnings`
  (brief: "a warning in `cut.json` naming why" — the stage record
  is in `cut.json`, and §11.3 prints it). Top-level `spend_usd`
  includes the transcribe spend by the stage-sum rule (§8.1), so
  the existing "per-stage spends sum to the top level" assertion
  keeps holding.
- **Resume.** A skipped words stage reloads `audio` (including
  `transcribe`) from the previous manifest (§8.1 `_reload`) and
  re-validates the canonical words file as today. A job completed
  before this feature (no `transcribe` key) reloads with the
  fallback defaults and, because its words fingerprint differs
  (new options keys), re-runs `words` once on the next invocation
  — the intended one-time migration, not a loop.

## 9. Inside tighten: speakers and disfluencies as evidence

Tighten consumes the enriched words through the file it already
reads: `tighten.run` loads words with `angles.load_timed_words`,
which now carries the three new fields (§5.1). No signature
changes: `tighten.run(*, sync_path, words_path, out_path,
audio_label, pause_limit_s, target_s, model, no_muse, max_usd,
taste_path=None)` is exactly the TIGHTEN-DESIGN §12.3 signature,
and `cut` calls it exactly as TIGHTEN-DESIGN §11 specifies. When
the words carry no enrichment (Whisper fallback, `--no-tighten`
jobs, hand-run pipelines), every rule in this section reduces to
today's behaviour — the checks below all key off non-empty
speakers / true disfluencies, which a fallback file never has.

### 9.1 Exactly which tighten functions change, and how

| Function / place | Change |
|---|---|
| `Utterance` dataclass | Gains `speaker`, `disfluency_count`, `muse_only_count` (§4). Serialised into `tighten.json` utterances as `"speaker"`, `"disfluency_count"`, `"muse_only_count"` (omitted when `""`/`0`, matching §5.1's omission rule). |
| `build_utterances(words)` | Computes the three aggregates over each utterance's word slice (§9.2). Signature and splitting rules unchanged. |
| `window_prompt(window, utterances, prev_topic, taste)` | When an utterance has a speaker or disfluencies, its line gains an annotation (§9.2). Otherwise the line is byte-identical to today. |
| `DEVELOPER` | Gains exactly one appended sentence (§9.2). |
| `rule_beats(utterances)` | Gains the transcript tie-break of §9.3 step R. With no speaker/disfluency data the result is identical to today. |
| `verify_window_reply(...)` | **Unchanged.** Transcript evidence is not applied per window; it is applied once over the assembled beats (§9.3), so Spark and rule beats pass through the same mechanical gate. |
| New `attempt_transcript_stats(attempt, utterances_by_id, words)` | §9.2 stats for one attempt. Pure. |
| New `apply_transcript_evidence(beats, utterances, words)` | §9.3 mechanical gate over assembled beats. Pure; returns `(beats, warnings)`. |
| `run(...)` | Calls `apply_transcript_evidence` after beats are assembled (Spark windows + rule windows merged) and before editor-state preservation / `recompute_spans` (TIGHTEN-DESIGN §11.2/§12.3 order gains this one step between "assemble beats" and "preserve editor state"). Its warnings extend the payload warnings. The payload gains `"transcript_speakers"` (§9.5). |

### 9.2 Aggregates, prompt annotation, attempt stats

`build_utterances` computes, over the utterance's word slice
`words[word_start:word_end]`:

- `speaker`: the public speaker with the greatest summed word
  duration (`end_s - start_s`) among words with a non-empty
  speaker; ties go to the speaker of the earliest such word;
  `""` when no word in the slice has a speaker.
- `disfluency_count`: words in the slice with `disfluency` true.
- `muse_only_count`: words in the slice with
  `heard_by == "muse"`.

`window_prompt` renders each core utterance as today —
`U<id> [<start:.1f>-<end:.1f>] <text>` — plus, when
`utterance.speaker` is non-empty or `utterance.disfluency_count
> 0`, the suffix ` (speaker <speaker or "unknown">` +
`, <n> disfluency/disfluencies` when the count is non-zero +
`)` appended after the text. Example:
`U12 [48.3-51.0] So the the problem is timecode (speaker talent, 1 disfluency)`.
With unenriched words no line changes, so TIGHTEN-DESIGN §14
item 11's prompt assertions keep passing unmodified.

`DEVELOPER` (TIGHTEN-DESIGN §7.3) gains this sentence verbatim at
the end:

> Utterances may carry a speaker tag and a disfluency count from
> the verbatim transcript: a speaker other than talent, and
> repeated or filler words, count against an attempt, but what
> you can hear in the delivery still matters more than the tag.

`attempt_transcript_stats(attempt, utterances_by_id, words) ->
tuple[float, str, int]` returns `(talent_share, dominant_speaker,
disfluency_count)` for one attempt: over every word of every
utterance in `attempt.utterance_ids` (via the utterances'
`word_start`/`word_end` slices into `words`), sum durations per
speaker; `talent_share` is the `talent` duration divided by the
total duration of words that have any speaker (0.0 when no word
has one — unknown is not talent); `dominant_speaker` is the
speaker with the greatest duration (`""` likewise);
`disfluency_count` sums the utterances' counts. Duration, not
word count, is the measure: it is the same "most speech by
duration" the brief defines the main speaker by (§6.5 step 6).

### 9.3 The mechanical gate — `apply_transcript_evidence`

Called once over the assembled beat list, in beat order. Muse
proposes (and the rule proposes); these steps decide, in this
order per beat. Both steps only ever move a beat *away* from a
pick the transcript contradicts; neither invents a beat, drops a
beat silently, or touches `angle_overrides`/`edits_applied`.

1. **Speaker removal.** For each attempt of the beat, compute
   §9.2 stats. An attempt with at least one speaker-tagged word
   whose `talent_share < SPEAKER_TALENT_MIN_SHARE` (0.50) and
   whose `dominant_speaker` is non-empty and not `"talent"` is
   **not talent speaking**: set `verdict="remove"`,
   `kind="crew_talk"`, and
   `reason=f"speaker {dominant_speaker} is not talent"` (the
   attempt's previous reason is discarded; the new one names the
   evidence). This applies to attempts Spark labelled `pick`,
   `alternate` and `remove` alike — a crew attempt Spark kept is
   removed, and a crew attempt Spark removed keeps a precise
   reason. Then repair the beat's pick: if the pick attempt was
   just removed (or the beat had no pick), promote the surviving
   usable attempt (`verdict` in `pick`/`alternate`, kind
   `keeper`/`alternate`) with the highest confidence, then the
   longest duration; set the beat's `pick_attempt` to it and
   `status="kept"`. When a previous pick was removed in this
   step, append to the promoted attempt's reason
   `f" (promoted: previous pick was {removed_dominant} speech)"`
   naming that removed pick's dominant speaker; when the beat
   simply had no pick, promote without the suffix. If no usable
   attempt survives, `pick_attempt=-1`,
   `status="no_usable_attempt"`,
   and the payload gains the warning
   `f"tighten_beat_other_speaker:B{beat.id}"`. The beat stays in
   the file with its attempts and reasons (TIGHTEN-DESIGN §5
   field rules: no beat is ever dropped silently), contributes
   no kept span, and its spans appear in `removed_spans`
   classified by the attempts' kinds through the existing
   §8.4 complement — `crew_talk` needs no new classification
   code because the attempt already carries the kind.
2. **Disfluency preference.** Only when the beat still has a
   pick and at least one usable alternate: let `p` be the pick's
   disfluency count and `a` the best alternate's (fewest
   disfluencies; ties: highest confidence, then longest). If
   `p - a >= DISFLUENCY_SWAP_MIN` (2), swap: the alternate
   becomes the pick (`verdict="pick"`, kind stays `keeper`), the
   old pick becomes `alternate`, and the new pick's reason gains
   `f" (cleaner take: {a} vs {p} disfluencies)"`. If no alternate
   qualifies, the pick is kept untouched except that its
   `confidence` is reduced by `min(0.20, 0.05 * p)` (clamped to
   [0, 1]) — a sole take with fillers is still the take; the
   confidence records the evidence instead of punishing the only
   option. Rule beats (**step R**, inside `rule_beats`): when
   choosing the pick among complete attempts (TIGHTEN-DESIGN
   §7.5 step 3), sort candidates by `(talent_dominant,
   -disfluency_count)` before the existing last-complete
   preference, where `talent_dominant` is 1 when the attempt has
   speaker data and its dominant speaker is `talent`, else 0.
   With no speaker data anywhere, every key is `(0, 0)` and the
   existing rule decides alone, unchanged.

An attempt that is *both* other-speaker and disfluent is handled
by step 1 alone; step 2 never sees a removed attempt as a swap
candidate. `apply_transcript_evidence` returns the warnings from
step 1; step 2's swaps are visible in the attempts' reasons and
verdicts, not in warnings — a swap between two talent takes is a
judgement the file explains per beat, not a job-level concern.

### 9.4 `cutlist.recompute_spans`: Muse-only words never anchor

One change, in the edge computation of TIGHTEN-DESIGN §8.1/§8.2.
Define an **anchor word** as a word with
`getattr(word, "heard_by", "whisper") != "muse"`. Kept word runs,
pause gaps and span edges are computed over anchor words only:
a kept span's start edge is the midpoint between the previous
anchor word in the recording and the run's first anchor word
(`first.start_s - HEAD_PAD_S` when there is none), and likewise
at the end. Muse-only words falling between a run's anchors are
inside the span by their interpolated times and ride along (they
are printed in the attempt text and in `words.json`); a Muse-only
word in a trimmed pause belongs to the pause, and the pause's
measured gap is the anchor-to-anchor gap, so a filler in a long
pause can never split it into two sub-limit gaps and defeat the
trim. `source_to_output`/`output_to_source` are unchanged (they
operate on spans, not words). With no Muse-only words present,
the anchor set is the whole word list and every span is
byte-identical to today — TIGHTEN-DESIGN §14 items 1–4 keep
passing unmodified.

### 9.5 `tighten.json` additions

The payload gains exactly two top-level keys, both always present:

- `"transcript_speakers"`: the distinct non-empty word speakers
  in the words file, `talent` first then `other-N` in numeric
  order; `[]` for unenriched words. This is how a reader (the
  app, the voice build) knows speaker evidence existed without
  opening `transcript.json`.
- Utterance entries gain `"speaker"`/`"disfluency_count"`/
  `"muse_only_count"` per §9.1's omission rule. Attempts gain
  nothing new: the speaker/disfluency outcomes are already
  expressed as the existing `verdict`/`kind`/`reason`/`confidence`
  fields, so `apply_operations`, the app and the voice resolver
  read a tightened file from this feature exactly as before.

`duration_in_s`, beats, spans, target handling and editor-state
preservation are unchanged. A `tighten.json` written before this
feature (no `transcript_speakers`) loads fine: `cutlist` never
requires the key, and `tighten.run` rewrites it on its next run.

## 10. What voice notes reuse (no voice code in this build)

VOICE-DESIGN §4 transcribes the editor's notes (not the footage)
with the same `diarize`: it extracts a 16 kHz mono WAV, calls
`diarize`, and maps turns from `startMs`/`endMs`. This build
exports the pieces that feature needs, with names the voice build
must call rather than re-implement:

- `transcribe.extract_wav_command(src, wav)` is byte-identical
  to VOICE-DESIGN §4.1's `extract_audio_command`. At voice build
  time, `voice.extract_audio_command` delegates to it (one
  command list in the repo, not two).
- `transcribe.plan_chunks` / `transcribe.Chunk` give the voice
  build its long-note chunking for free: a note over
  `CHUNK_MAX_S` is planned exactly as footage is. Voice notes
  need no alignment (§1.2 of VOICE-DESIGN: anchors come from the
  playhead/timecodes/quotes), so the voice build uses only the
  extraction/chunk/diarize layers, and `transcribe_audio_clip`
  itself (which aligns against Whisper) is never called for a
  note. `diarize_chunk` is likewise reused instead of a second
  wrapper; its `(result, spend, cached)` triple gives voice the
  spend labelling VOICE-DESIGN §4.2 requires.
- Speaker *names* do not transfer: footage speakers are
  `talent`/`other-N` (§6.5); note speakers stay diarizer labels
  in `voice.json` as VOICE-DESIGN specifies. No function renames
  note speakers, and `reconcile_speakers` (which assumes one
  continuous recording with overlap) must not be run over
  separate note files.

Nothing in this section is built now; it is a stability promise
about four function signatures, recorded here because
TRANSCRIBE-BRIEF asks the design to say whether reuse fits. It
does, at the extraction/chunk/diarize layers, and not at the
alignment layer.

## 11. Command line

### 11.1 `director cut` gains one flag

```
director cut INPUT [INPUT …] [--out-dir DIR] [--reference CLIP]
             [--audio CLIP] [--words WORDS.json] [--transcripts DIR]
             [--methods …] [--whisper-model MODEL]
             [--no-grade] [--grade-check] [--no-muse] [--no-transcribe]
             [--model muse-spark-1.3] [--min-shot 2.0]
             [--no-tighten] [--pause-limit 0.8] [--target 3:00]
             [--no-vertical] [--no-sam] [--no-export] [--no-qc]
             [--max-usd 5.00] [--force] [--name "Director cut"]
```

(`--whisper-model` and the tighten flags are shown in their
post-tighten positions; this design adds only the flagged row
below. `cli.build_parser` gains the argument on the existing
`cut` subparser and `cli.command_cut` passes
`no_transcribe=bool(args.no_transcribe)` into `cut.run` — whose
signature gains `no_transcribe: bool` after `no_muse` in the
same position as `JobOptions`.)

| Flag | Default | Effect |
|---|---|---|
| `--no-transcribe` | off | Skip Voice Transcribe in the words stage; keep Whisper words exactly as today. `angles`/tighten Spark calls, SAM and QC are unaffected (this is the surgical opt-out; `--no-muse` skips transcription too, §8.2 step 2). Help text exactly: `Skip Muse Voice Transcribe; keep Whisper words only`. |

There is no `--transcribe-model`: the diarizer's model is fixed
by `guestlist.meta.transcribe.MODEL` (§1.1), and `--model`
continues to mean the Spark model for angles/grade/QC only. It
is never passed to `diarize`, and this design adds no model
allow-list for it to be confused with.

Console output: OPEN-DESIGN §11.3's stage lines plus
TIGHTEN-DESIGN §11.1's tighten line, with the words line extended
as §8.2 step 6 specifies. No new stage line is printed, because
no stage was added. Example (Muse path, tightened job):

```
sync: done
words: done (835 words, sync_cache, shifted +178.746s, muse 97.3% matched, $0.0306)
tighten: done ($0.1712) (14 beats, kept 612.4s → 181.2s)
grade: done
angles: done ($0.0312)
export: done (21 shots, 1 gap)
reframe: done ($0.0419)
qc: warn (9 checks, 1 muse findings)
! words: transcribe_speaker_fallback:2
Wrote director-cut/cut.json (complete_with_warnings, $0.2439)
cut: director-cut/cut.mp4
```

On a Whisper fallback the words line is today's line unchanged
and the reason prints as a `! words: transcribe_…` warning.

### 11.2 Standalone `director transcribe`

```
director transcribe --sync sync.json --words whisper.json
                    --out words.json [--transcript transcript.json]
                    [--audio LABEL] [--no-muse] [--max-usd 2.00]
```

`cli.command_transcribe(args)` requires ffmpeg (extraction may
run), resolves the audio clip from the sync payload exactly as
`angles.run` does (explicit `--audio LABEL`, else the clip whose
path equals the sync reference, else label `A`), loads
`--words` as the **clip-local** Whisper JSON (either shape,
via `angles.load_timed_words`), and calls
`transcribe.run(sync_path=…, words_path=…, out_path=…,
transcript_path=…, audio_label=…, max_usd=…, no_muse=…)`.

- `--words` here is clip-local — the file `sync` cached or
  `mlx_whisper` wrote — **not** a timeline file. This command is
  the bridge the words stage performs internally, exposed so a
  stranger can transcribe one recording by hand and inspect the
  alignment without running a cut. The distinction from
  `director cut --words` (timeline times, used in place, never
  transcribed, §8.2) is stated in both flags' help text.
- `--out` (default `words.json`) receives the **timeline** words
  (clip-local words shifted by the audio clip's `offset_s`, the
  §8.2 shift), in the §5.1 enriched shape on success and the
  plain Whisper shape on fallback. `--transcript` (default:
  `transcript.json` beside `--out`) receives §5.2 on success
  only.
- `--no-muse` skips transcription and simply shifts the Whisper
  words (exit 0), so the command is also the offline
  clip-local→timeline converter. `--max-usd` defaults to `2.00`
  (the VOICE-DESIGN voice-command default; an hour of audio is
  $0.18, so the cap refuses accidents, not real clips).
- Print order: one line per speaker
  (`talent: 540.1s, 14 turns`), then the alignment line
  (`aligned 790/812 words (97.3%), 45 muse-only, 22 whisper-only,
  $0.0306`), then any warnings as `! <warning>`, then
  `Wrote <out>` and, on success, `Wrote <transcript>`. On
  fallback the speaker/alignment lines are replaced by the
  single warning line and the `Wrote <out>` still prints —
  the shifted Whisper file is a valid result, and exit code
  stays 0. Bad sync/words files (missing, unparseable, unknown
  `--audio` label, unplaced audio clip) raise
  `ValueError`/`FileNotFoundError` and `cli.main` exits 1 with
  `Error: …`, as for every other command.

`transcribe.run` mirrors `tighten.run`'s contract: it returns
the §5.2 payload (written to `transcript_path` on success), and
on fallback returns the §5.2 in-memory whisper payload while
writing only the words file. A placed audio clip is required:
`offset_s is None` raises
`ValueError(f"transcribe: audio clip {label} is not placed on
the timeline")` (the same refusal the words bridge makes,
OPEN-DESIGN §6.1, reworded with this command's prefix).

## 12. Failure and edge cases (decided)

Exact user-visible strings; the builder does not paraphrase
them. In a `cut` job every `transcribe_…` warning lands in the
words stage record's `warnings` in `cut.json` and prints as
`! words: <warning>`; no row of this table fails a `cut` job
unless it says it does.

| Case | Behaviour |
|---|---|
| No `MODEL_API_KEY`, `guestlist` not installed | `diarize_chunk` raises `MuseUnavailable`; whole transcription falls back. Warning `transcribe_unavailable:<error>`. `words.json` is the Whisper file; job completes. This together with `--no-muse` is the fully offline form. |
| Diarizer HTTP refusal / error on any chunk | Partial turns discarded. Warning `transcribe_failed:RuntimeError: <message truncated to 160 chars>`. Whisper fallback. Never a half-enriched transcript. |
| Estimate over remaining `--max-usd` | Nothing extracted or sent. Warning `transcribe_over_budget:est $<est> over remaining $<rem>`. Whisper fallback. In `cut` this includes the `--max-usd 0` form. |
| `--no-muse` / `--no-transcribe` / explicit `--words` | Not attempted. Manifest `transcribe.method` `"skipped"`; no warning; stale `transcript.json` removed. Job status unaffected. |
| Audio clip has no audio / zero duration | From `cut`, guarded by `has_audio`/probed duration before the call; standalone raises `TranscribeError` (§6.1). In `cut`, a `TranscribeError` escaping the guard is caught into the fallback with `transcribe_failed:TranscribeError: …`. |
| WAV extraction fails (`ToolingError`, missing file) | Fallback with `transcribe_failed:ToolingError: …`; the work dir keeps `audio.wav` if partially written, for diagnosis. |
| `silencedetect` yields nothing | Word-gap pauses alone; warning `transcribe_silence_fallback:words`. Chunking still never cuts inside a Whisper word. |
| No pause within reach on a long clip | §6.2 step 5: chunk extends to the next pause (or the end). A request longer than the unknown API cap may then fail and fall back per the HTTP row — accepted, and visible, rather than a cut inside speech. |
| Chunk returns no usable turns; all chunks empty | Fallback with `transcribe_no_turns`. (A silent production clip also has no Whisper words and will already have failed the words stage on its own terms.) |
| Speaker vote finds nothing across a seam | Duration-rank fallback for that chunk only; warning `transcribe_speaker_fallback:<chunk_id>`. Other seams keep their voted mappings. |
| One speaker in the whole recording | `speakers` is `[talent]`; tighten's §9.3 step 1 can never fire (no non-talent dominant exists). Normal talking-head case. |
| Alignment below 0.40 matched | Muse result discarded; warning `transcribe_alignment_low:<share with 2 decimals>`. Between 0.40 and 0.70: kept with the same warning (§7.6). |
| Muse returns a turn of minutes (measured: 60 s as one turn) | Normal. Turns are not split; §7 attributes words by time within the turn, and `transcript.json` records the coarse turn honestly. |
| Repeated/filler tokens at a span edge in tighten | §9.4: Muse-only words never anchor edges, so a filler can be inside a kept span or inside a trimmed pause but can never create, move or split a span edge by itself. |
| Attempt entirely `other-1` that Spark picked (crew talk Spark missed) | §9.3 step 1 removes it as `crew_talk` with `speaker other-1 is not talent`; an alternate talent attempt is promoted, or the beat becomes `no_usable_attempt` with `tighten_beat_other_speaker:B<id>`. |
| Sole talent attempt full of fillers (no cleaner alternate) | Kept. Only its confidence drops (§9.3 step 2); disfluency alone never removes the only take. |
| Words file with speakers but a beat with no speaker-tagged words | `talent_share` 0.0 and dominant `""`: step 1 does not fire (unknown is not crew). The attempt stands on Spark/rule judgement alone. |
| Re-run of a pre-feature `cut` job | Words fingerprint differs (new options keys) → `words` re-runs once, enriching if available; §8.3 reload defaults keep the skip logic safe. Pre-feature `tighten.json` (no `transcript_speakers`) loads and is rewritten on the next tighten run (§9.5). |
| Re-run with the same audio, stage re-executed (e.g. `--force`) | `guestlist` returns each chunk from its content cache; `diarize_chunk` reports `cached=True`, spend $0 per chunk, and `transcript.json` is rewritten identical. |
| Very long recordings (hours) | Chunks bound every request; per-chunk WAVs live under `transcribe/` and guestlist caches replies by content. No duration cap is imposed — the estimate gate of §6.4 is the cap, and its refusal is a labelled Whisper fallback, not a failed job. |
| Footage privacy | Only derived 16 kHz mono chunk WAVs of the production-audio clip are sent, only through `diarize`, only on a run whose words stage is eligible (§8.2). Camera originals and other clips' audio are never opened by this module. `--words` supplied by the editor disables eligibility, so a hand-supplied transcript is also a privacy opt-out. |

## 13. Tests

`tests/test_transcribe.py`, items 1–28. Offline throughout: no
network, no Meta API, no real Whisper. The Meta seam is
`transcribe.diarize_chunk` patched with
`unittest.mock.patch.object` (or a stand-in returning fixed
per-chunk results keyed by WAV file name), and `guestlist` is
never imported by the suite. Style follows
`tests/test_angles.py`/`test_tighten.py`: small synthetic
word/turn builders, `tempfile.TemporaryDirectory`, exact
command-list and exact message assertions. Synthetic example
sentences in fixtures are invented for the test (no transcript
text from real recordings, per the brief). One line per item:
what it builds, what it asserts.

1. `test_normalize_token_and_tokenize` — `"Timecode."` →
   `"timecode"`, `"'quoted'"` → `"quoted"`, `"don't"` →
   `"don't"`, `"…"` → `""`; `tokenize_turn` splits on runs of
   whitespace and drops empties.
2. `test_extract_and_chunk_commands` — `extract_wav_command`,
   `chunk_wav_command` and `silence_detect_command` equal §6
   element-for-element, including `-ss` before `-i` and
   `pcm_s16le` at 16000 Hz.
3. `test_parse_silence_spans` — a captured-shaped stderr fixture
   (progress lines, two `silence_start`/`silence_end` pairs, one
   trailing unpaired start) parses to the two pairs plus the
   trailing start closed at the duration argument; garbage
   gives `[]`.
4. `test_plan_chunks_worked_example` — the §6.2 612.400 s
   example: cores `[0, 178.65]`, `[178.65, 355.70]`,
   `[355.70, 612.40]`, WAV starts 8 s before cores 2–3, every
   boundary strictly inside its pause. A 59.58 s duration plans
   as exactly one chunk.
5. `test_plan_chunks_never_inside_speech` — pauses dense early
   and absent for 400 s mid-clip → the long middle chunk extends
   to the next pause (step 5) and no boundary lies inside any
   synthetic word interval; a final core under `CHUNK_MIN_S`
   merges into its predecessor.
6. `test_pauses_from_words_and_merge` — word gaps below 0.6 s
   excluded by `pauses_from_words`; `_merged_pauses` drops a
   silence span containing part of a word, trims a kept silence
   by 0.10 s per edge, and keeps a word gap overlapping no
   silence (all three asserted on the helper's output list).
7. `test_estimate_and_over_budget_gate` — `estimate_transcribe_usd(3600)`
   is $0.18; with `max_usd` below the estimate for a 600 s clip,
   `transcribe_audio_clip` (ffmpeg runner and `diarize_chunk`
   both stubbed to record calls) returns the Whisper fallback
   with `transcribe_over_budget:est $0.03 over remaining $0.01`
   and records zero extraction/diarize calls.
8. `test_reconcile_speakers_overlap_vote` — two chunks whose raw
   labels are swapped (`SPEAKER_00/01` in chunk 0,
   `SPEAKER_01/00` in chunk 1) with overlapping turns of
   matching text in the 8 s seam → one provisional mapping,
   `talent` is the longer speaker across both chunks, kept
   turns come from cores only (no duplicated overlap text),
   raw labels appear in `speakers[].raw_speakers`.
9. `test_reconcile_speakers_fallback_and_naming` — a second
   chunk with no overlap speech → duration-rank mapping plus
   `transcribe_speaker_fallback:1`; three provisionals name as
   `talent`, `other-1`, `other-2` by total duration, ties by
   first appearance. (§13 items 10–12 below are the §7.7
   worked examples, asserted field-for-field.)
10. `test_align_repeat_whisper_dropped` — §7.7 example 1: the
    second `the` is `heard_by "muse"`, `disfluency` true, at
    10.22–10.24, speaker `talent`; anchors keep Whisper times;
    stats 5/6/5/1/0, share 1.00.
11. `test_align_number_digits_vs_words` — §7.7 example 2:
    `three` is `both` over the `3` interval; `twenty`/`four`
    are both `both` and subdivide the `24` interval in order
    without overlap; no muse-only/whisper-only units; same
    assertions reversed (Whisper words, Muse digits) emit
    Muse's digit token over the combined Whisper interval.
12. `test_align_crew_talk_second_speaker` — §7.7 example 3:
    `rolling` gets `other-1` by time, the sentence gets
    `talent`; with the crew word deleted from the Whisper
    fixture it instead appears muse-only, interpolated,
    still `other-1`.
13. `test_align_replace_and_surplus` — a misheard pair
    (`their`/`there`) aligns as `both` with Muse's text on
    Whisper's time; a 3-for-1 replace surplus emits the extra
    Whisper word as `whisper`-only and the extra Muse token
    as `muse`-only, stats counting each.
14. `test_align_muse_only_closed_gap` — an insert between two
    touching Whisper words (gap 0) gives zero-length muse-only
    times at the shared edge, order preserved, and no anchor
    time moves.
15. `test_align_low_share_discards` — fixtures sharing under
    40% of units → `align_words` still returns its alignment
    (the pure function does not judge), and
    `transcribe_audio_clip` with a stubbed `diarize_chunk`
    returning those turns discards it: method `whisper`,
    words object-equal to the input, warning
    `transcribe_alignment_low:0.32` (share per fixture).
16. `test_speaker_for_time` — midpoint inside a turn, a word
    straddling two turns (greatest overlap wins), a word 0.4 s
    past a turn end (nearest within 0.5 s), and a word in a
    long silence (`""`).
17. `test_transcribe_audio_clip_end_to_end_fake` — a synthetic
    200 s clip (duration stubbed; ffmpeg runner stubbed to
    create the WAV files), `diarize_chunk` patched to return
    fixed turns per chunk file name (including a swapped-label
    second chunk and one filler/repeat) → result words carry
    speakers/heard_by/disfluency, `transcript` payload via
    `build_transcript_payload` matches §5.2's shape and sums
    (per-chunk spends add to `spend_usd`; a chunk reported
    `cached=True` contributes 0.0).
18. `test_transcribe_fallbacks` — `diarize_chunk` raising
    `MuseUnavailable` → `transcribe_unavailable:…`; raising
    `RuntimeError("transcribe HTTP 500: …")` on chunk 2 →
    `transcribe_failed:RuntimeError: …` with no partial turns
    retained; returning no turns → `transcribe_no_turns`. In
    each case the returned words equal the input Whisper words
    field-for-field (including default `heard_by`).
19. `test_words_payload_round_trip` — `cut.words_file_payload`
    over enriched words written to a temp file reloads through
    the extended `angles.load_timed_words` with text, times,
    speaker, heard_by and disfluency preserved; a plain
    pre-feature words file loads with `""/"whisper"/False`
    defaults; `cut.shift_words` preserves all three fields
    through a +178.746 shift.
20. `test_cut_words_stage_enriches_and_falls_back` — `cut.run`
    with stubbed stage modules (the `test_cut.py` pattern) and
    `transcribe_mod.transcribe_audio_clip` patched: (a) Muse
    result → `words.json` enriched, `transcript.json` written,
    manifest `audio.transcribe.method "muse"`, words stage
    spend equals the fake's spend and the stage spends sum to
    the top-level spend; (b) whisper result → no
    `transcript.json` (a pre-seeded stale one is removed), the
    stage warning names the fake's warning, job still
    completes; (c) `no_transcribe=True` and (d) explicit
    `--words` source → the fake is never called and method is
    `"skipped"`.
21. `test_cut_words_fingerprint_transcribe_flags` — toggling
    `no_transcribe` changes the words stage fingerprint
    (re-runs `words` on the next `cut.run`); an unchanged
    re-run skips it (fake call count stays at its first-run
    total).
22. `test_build_utterances_transcript_aggregates` — words with
    mixed speakers/disfluencies → the utterance's majority
    speaker is by summed duration (a long `other-1` word beats
    two short `talent` words), counts match the slice; words
    with defaults give `""/0/0` and `window_prompt` lines are
    byte-identical to the pre-feature format, while an enriched
    utterance's line ends with the §9.2 annotation and
    `DEVELOPER` ends with the §9.2 sentence.
23. `test_attempt_stats_and_speaker_removal` —
    `attempt_transcript_stats` shares are duration-based;
    `apply_transcript_evidence` removes a 100% `other-1` pick
    as `crew_talk` with reason `speaker other-1 is not talent`,
    promotes the talent alternate with the §9.3 promoted-note,
    and when no alternate exists leaves
    `no_usable_attempt` plus `tighten_beat_other_speaker:B<id>`.
    A beat whose words carry no speakers is untouched.
24. `test_disfluency_swap_and_sole_take` — a pick with 3
    disfluencies against a clean alternate swaps with reason
    `(cleaner take: 0 vs 3 disfluencies)`; a 1-vs-0 difference
    does not swap (below `DISFLUENCY_SWAP_MIN`); a sole
    disfluent pick is kept with confidence reduced by exactly
    `min(0.20, 0.05 * count)`; `rule_beats` over the same
    fixtures prefers the clean talent complete pass, and with
    speaker fields blanked picks the last complete pass as
    before.
25. `test_muse_only_never_anchors_span` — `cutlist.recompute_spans`
    over a kept attempt containing a muse-only filler inside a
    1.42 s anchor gap at pause limit 0.8 still trims the pause
    (gap measured anchor-to-anchor), and a kept span's edges
    are the anchor midpoints of TIGHTEN-DESIGN §14 item 1 with
    the filler present or removed — identical edges either way.
26. `test_tighten_run_with_enriched_words` — `tighten.run`
    with `no_muse=True` over synthetic two-pass words where the
    first pass is `other-1` and the second `talent` (plus a
    disfluent first talent pass in a second beat) →
    `tighten.json` has `transcript_speakers ["talent",
    "other-1"]`, the crew beat's pick is the talent attempt,
    utterance entries carry the §9.5 keys, and kept spans cover
    only talent words. No Meta call.
27. `test_cli_transcribe_parser_and_cut_flag` — `build_parser`
    parses `cut a.mp4 b.mp4` with `no_transcribe` defaulting
    false and true with the flag; `transcribe --sync … --words
    …` parses with `max_usd 2.00` default; `command_cut` with
    `cut.run` stubbed asserts `no_transcribe` is forwarded;
    `command_transcribe` with `transcribe.run` stubbed prints
    the speaker line before the alignment line (§11.2 order).
28. `test_transcribe_extract_synthetic` *(ffmpeg)* —
    `@unittest.skipUnless(shutil.which("ffmpeg"), ...)`. A 2 s
    `sine` lavfi file in a temp dir: `extract_wav_command` run
    for real produces a probeable 16 kHz mono WAV of ≈2 s, and
    `plan_chunks` over its duration is one chunk. No diarizer
    is called (this tests the ffmpeg contract, not the API).

Gate: `.venv/bin/python -m unittest discover -s tests -p
'test_*.py'` green (with the tighten build's tests). Existing
suites are the regression contract for this feature's
compatibility claims: `test_angles` for the `TimedWord`/
`load_timed_words` extension (§13 item 19 covers the new
fields), OPEN-DESIGN items 6–8 for `resolve_words`, and
TIGHTEN-DESIGN items 1–4/11 for the `recompute_spans` anchor
rule and prompt stability. Per `AGENTS.md`, the ffmpeg-facing
commands (`extract_wav_command`, `chunk_wav_command`,
`silence_detect_command`) are additionally exercised on real
footage in §14's live check.

## 14. What the build also updates, non-goals, live check

**Provenance (at build time).** `docs/muse-code/BUILD-LOG.md`
gains:

```markdown
# Build log: Voice Transcribe transcript (`transcribe`) (2026-11-03)

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `TRANSCRIBE-BRIEF.md`: Voice Transcribe as the transcript of record. |
| Design | Meta **Muse Code** on **`muse-wonder-1.0`** | `TRANSCRIBE-DESIGN.md`: chunking, speaker reconciliation, Whisper↔Muse alignment, cut/tighten integration. |
| Build | Meta **Muse Code** on **`muse-spark-1.3`** | `src/director/transcribe.py`, the `angles`/`cut`/`tighten`/`cutlist` changes, `tests/test_transcribe.py`. |
| Code review and live test | Claude Code, piloted by the owner | Real footage, below. |

Muse Code token use: TBD (the owner fills this from the run log).
Live test: TBD (the owner fills this from the real-footage run).
```

and the README's "Who built what" table gains:

```markdown
| Voice Transcribe transcript (`director transcribe`, the transcribe step of `director cut`) | Designed by Muse Code on muse-wonder-1.0, built by Muse Code on muse-spark-1.3 |
```

The README also gains, in the `director cut` section: one
paragraph stating the contract in editor's terms — Director
transcribes the production audio with Muse Voice Transcribe
when a Meta key is available, keeps every repetition and filler,
labels the main speaker `talent`, writes the turns to
`transcript.json` next to `words.json`, and falls back to
Whisper alone (named in `cut.json`) when it cannot — plus the
`--no-transcribe` flag line in the `cut` flag list and the
`director transcribe --sync … --words …` standalone form.
`AGENTS.md` gains the §2 bullets. Model-call and token counts
stay TBD in both files until the owner fills them from the run
log; the build does not estimate them into the record.

**Non-goals (explicit).** *Word timestamps from Muse* (the API
returns none; §7 derives token times from Whisper and any future
word-timed endpoint replaces the alignment's interpolation, not
its outputs). *Feeding enriched words back into sync* (§8.1:
sync's voting contract is separate and earlier). *Speaker
identification* (speakers are `talent`/`other-N` by duration,
never named, never enrolled). *Acting on speakers outside
tighten* (angles still picks cameras per line regardless of
speaker; a line spoken by `other-1` that survives tighten is
shown like any other). *Re-diarizing voice notes* (§10: the
voice build reuses extraction/chunking only). *A second
transcript file format* (`words.json` stays the one word file
every reader already opens; `transcript.json` is additive).

**Live check (before done is claimed).** On the owner's real
footage — the 3-minute founder talk, 2–3 passes on 2–3 cameras,
from the tighten live check's `demo-job`: run
`director cut <clips> --out-dir demo-job` with the Meta key set
and open `transcript.json`: read the turns against the
recording (crew talk attributed to `other-1`, the "the the" /
"will will" moments present in `words.json` with
`heard_by: "muse"`), check `matched_share` is above 0.90, and
spot-check five muse-only times against the audio. Then open
`tighten.json` and confirm the crew-talk attempts are removed
with the §9.3 reason and no talent attempt was removed by
§9.3 step 1. Finally re-run with `--no-transcribe` and confirm
the words stage reports `skipped`, `transcript.json` is gone,
and the cut still completes. Record the outcome, the transcribe
spend and the matched share in the BUILD-LOG section above.
