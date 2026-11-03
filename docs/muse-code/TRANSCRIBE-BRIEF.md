# Brief: Muse Voice Transcribe as the transcript of record (`transcribe`)

Written 2026-11-03 by the project owner with Claude Code for Meta's Muse Code agent. Muse Wonder writes the design
(`docs/muse-code/TRANSCRIBE-DESIGN.md`), then Muse Code builds it on Muse Spark. Read `AGENTS.md`,
`docs/muse-code/OPEN-BRIEF.md`, `OPEN-DESIGN.md`, `TIGHTEN-DESIGN.md` and `VOICE-DESIGN.md` first; this feature
feeds all three.

## Why

Every word time in Director comes from local Whisper today. Whisper writes what the speaker *meant*: it smooths
"the the", "will will", "um" and half-started words out of the text. Those are exactly the signals that a take is
flubbed. It also has no idea who is talking, so the crew's "rolling", "one more time" and side talk look like the
speaker's lines.

Meta's Voice Transcribe (`muse-voice-transcribe-1.0`, `guestlist.meta.transcribe.diarize`) keeps disfluencies
verbatim and labels speakers. The owner wants it to be the transcript Director reasons over.

## What it returns (measured 2026-11-03 on 60 s of real single-speaker talking-head audio)

- Response keys: `audioDurationMs`, `sessionId`, `transcript`, `turns[]` with `turnId`, `startMs`, `endMs`,
  `transcript`, `speaker`.
- **No word timestamps.** 60 s of one speaker came back as a single turn (0 to 59 580 ms).
- The text kept repetitions and fillers that Whisper drops ("will will", "the the", "um", "uh").
- Input: WAV, mono, 16-bit PCM, 16 or 24 kHz. Price $0.18 per audio hour. `diarize` caches by file hash.
- The maximum audio length per request is unknown. A 60 s request worked. Design for chunking.

## The job

1. Transcribe the production-audio clip with Voice Transcribe: extract 16 kHz mono WAV, chunk on silence (aim for
   chunks of a few minutes, cut only in a pause, never inside speech), call `diarize` per chunk, offset the turns
   back to clip time, and reconcile speaker labels across chunks (speaker ids may restart per request).
2. Keep Whisper as the clock only. Word start/end times still come from local Whisper (mlx_whisper). Claude Code is
   adding a `--whisper-model` option to `sync` and `cut` today, defaulting to
   `mlx-community/whisper-large-v3-turbo`; assume it exists.
3. Align. Map Voice Transcribe's tokens onto Whisper's timed words with a sequence alignment (normalised text,
   tolerant of punctuation, numbers and casing). Every Whisper word gets the speaker of the turn it falls in. Tokens
   only Voice Transcribe heard (the repeats and fillers Whisper dropped) get a time inside the gap they belong to,
   are marked `heard_by: "muse"`, and are never used as a cut point by themselves.
4. Write it so existing readers keep working: `words.json` keeps its current shape (`{"words": [{"word", "start",
   "end"}]}`) with optional extra fields (`speaker`, `heard_by`, `disfluency`), plus a `transcript.json` with the
   turns, speakers, chunks and the alignment stats (matched share, Muse-only tokens, Whisper-only words).
5. Feed it in:
   - `director cut`'s words stage uses it by default when the Meta key and guestlist are available.
   - `tighten` uses disfluencies inside an attempt as evidence against it, and removes turns by a speaker who is not
     the main speaker (most speech by duration), each with a reason.
   - Voice notes keep their own design; reuse the chunking and WAV extraction if it fits.
6. Speaker naming: the main speaker is `talent`, others `other-1`, `other-2`. No real names.

## Fallbacks

No key, no guestlist, a refused request or an over-limit chunk: Whisper alone, same output shape, a warning in
`cut.json` naming why. `--no-muse` also skips it. Spend estimated before the call and counted under `--max-usd`.

## Rules

Everything in `OPEN-BRIEF.md` "Rules (hard)". Tests offline: a stand-in `diarize` returning fixed turns, synthetic
Whisper words, no network, no Whisper, no media unless a test truly needs a lavfi file. No transcript text from real
recordings in tests or docs. Add a TRANSCRIBE section to `docs/muse-code/BUILD-LOG.md` and a README row.

## What done looks like

`TRANSCRIBE-DESIGN.md`: the chunker, the alignment algorithm with worked examples (a repeat Whisper dropped, a
number written as digits by one and words by the other, crew talk in a second speaker), data contracts with example
values, exactly which functions in `cut.py` and the tighten module change and how, failure cases, and a numbered test
list. It must be buildable in one Muse Spark run after tighten is built.
