# Brief: multicam sync (`director sync`)

Written 2026-11-02 by the project owner with Claude Code for Meta's Muse Code agent. Muse Wonder writes the design
(`docs/muse-code/SYNC-DESIGN.md`), then Muse Code builds it on Muse Spark. Read `AGENTS.md` first and follow it.

This repo is new. Multicam sync existed in an earlier repo; **do not look for it or read it**. Write this from the
brief. Read only files inside this repository and the installed `ghostcut` package (`.venv`), whose
`ghostcut.ffmpeg_tools` (`run_command`, `probe_media`, `parse_fraction`, `coerce_float`, `snap_frame_rate`,
`ToolingError`, `require_ffmpeg_binaries`) and `ghostcut.config` (`read_json`, `write_json`) you may use.

## The job

A talk filmed on 2–4 cameras (two Sony bodies shooting S-Log3, an iPhone shooting HLG) and usually a separate lav
recorder, each started at a different moment. `director sync clip1 clip2 …` finds where every clip sits on one
shared timeline and writes `sync.json`. Several methods each estimate a clip's offset, then they are checked against
each other, because any single method can be confidently wrong.

Convention: a clip's `offset_s` is where its first sample sits on the timeline, so clip time `t` is timeline time
`t + offset_s`. The reference clip sits at 0. `offset_s` is `null` for a clip that could not be placed.

## The contract `angles.py` and `grade.py` already read (must not change)

```json
{
  "reference": "<path of the reference clip>",
  "methods": ["timecode", "clock", "waveform", "whisper"],
  "clips": [
    {
      "label": "A",
      "offset_s": 0.0,
      "method": "reference",
      "camera": {"path": "...", "make": "...", "model": "...", "lens": "...", "fps": 59.94,
                 "width": 3840, "height": 2160, "duration_s": 312.4, "has_audio": true,
                 "gamma": "s-log3-cine", "timecode": "13:37:55:21", "tc_rate": 60.0,
                 "captured_at": "2026-11-02T14:03:11-05:00"}
    }
  ]
}
```

Labels: the reference is `A`, the others `B`, `C`, … in the order the clips were given. Each clip also reports
every estimate it got (method, offset, confidence 0–1, precision in seconds, which clip it was measured against, a
detail dict), every pairwise check between estimates (methods, delta, agree yes or no), drift in ppm when measured,
and plain-English warnings. Read `src/director/angles.py: load_sync` and `src/director/grade.py: load_sync_for_grade`
to see exactly which fields they use.

## The methods

1. **Timecode.** The start timecode of each file. ffprobe shows it as a `timecode` tag on the format or a stream
   (Sony writes it on its metadata track). Sony also writes an XML sidecar next to the clip, named `<stem>M01.XML`:
   its `LtcChangeTable` has a `tcFps` attribute, and the `LtcChange` with `frameCount="0"` has an 8-hex-digit
   `value` holding BCD bytes in the order frames, seconds, minutes, hours. The high bits of those bytes are flags
   (drop-frame, field), so mask them off before reading the digits. Converting timecode to real seconds:
   - timecode counts frames at the round rate (24, 30, 60);
   - on a 23.976 / 29.97 / 59.94 clip each labelled second lasts 1.001 real seconds;
   - a `;` before the frames means drop-frame: 2 frame labels (4 at 60) are skipped every minute except every tenth;
   - an offset more than half a day wraps around midnight.

   Timecode is frame-exact only when the cameras were jammed to one source; free-running cameras are seconds or
   hours apart. It is never trusted on its own while an audio method has an answer.
2. **Capture clock.** The capture time in the metadata. The iPhone writes `com.apple.quicktime.creationdate`, its own
   network time; prefer it over `creation_time`, which can be the copy time. Sony's sidecar has `CreationDate`. Parse
   time zones like `-0400`. Coarse (±2 s at best): used to narrow a search, or alone only as a flagged last resort.
3. **Waveform.** Cross-correlate the clips' own sound. Needs numpy (the `director` extra), so guard the import. What
   works:
   - decode mono at 8 kHz, band-passed to speech (about 200–3400 Hz) in ffmpeg, so rumble and hiss don't drive it;
   - compare **log RMS envelopes**, not raw samples. Log compression is what lets a chest lav and a camera mic three
     metres away correlate;
   - search every lag at 10 ms hops with an FFT, then refine at 1 ms around the best;
   - require a minimum overlap (10 s, or half the shorter clip);
   - trust a peak only when its correlation is clear (r ≥ 0.45) and it beats the best peak more than 1 s away by a
     margin (0.08). A short clip latches onto any loud section with a confident-looking score, so a lone high peak
     is not proof;
   - measure the lag again in 20 s windows across the overlap and fit a line. The slope is the clock drift in ppm,
     and the intercept is the offset at the clip's first frame. Warn when drift moves the sync more than half a
     frame over the clip.
4. **Whisper.** The same words heard by two cameras. Word-timed transcripts come from `--transcripts DIR` (files
   named `<clip stem>.json`, Whisper JSON with `{"segments": [{"words": [{"word", "start", "end"}]}]}` or
   `{"words": [...]}`). Without it, transcribe locally with `mlx_whisper` if it is on PATH (word timestamps on, no
   API spend, cached per clip next to the output). Otherwise skip the method. Matching:
   - normalise words (lowercase, strip punctuation);
   - every 3-word phrase that occurs **exactly once in each** transcript votes for an offset;
   - the densest 0.3 s cluster of votes wins, so misheard words fail to vote instead of pulling the answer;
   - overlapping phrases from one sentence are one piece of evidence. Count **moments**: separate runs of matched
     phrases. Require at least 2, because one sentence can match by coincidence (the same question asked in two
     different interviews).

## Combining them

- Agreement between two estimates: within the larger of their precisions plus one frame. Precisions: timecode 0
  (one frame), clock 2 s, waveform 2 ms, whisper 0.15 s.
- **When the waveform's global peak disagrees with Whisper** (or, without Whisper, with timecode or clock), search
  the waveform again **only inside the window the other method allows** (its precision plus 0.5 s). Take the guided
  peak when the global one was weak, or when the guided one correlates about as well (within 0.02). Keep the
  unguided result in the details.
- If Whisper's words line up but the sound doesn't (the guided waveform r < 0.45 while both clips have audio), the
  words were probably said twice: halve Whisper's confidence, record it, and don't place the clip on Whisper alone.
- Pick, in order:
  1. a trusted waveform (clear peak, or backed by another non-clock method);
  2. Whisper with enough confidence;
  3. timecode, flagged "right only if the cameras were jammed";
  4. the clock, flagged "check by eye".

  Any timecode or Whisper estimate that disagrees with the pick becomes a warning.
- A clip that can't be placed against the reference is tried against the clips already placed. An iPhone the A-cam
  barely hears may be clear against the B-cam. Stop early once a confident waveform pick is found.

## CLI

`director sync CLIP [CLIP …] [--reference CLIP] [--methods timecode,clock,waveform,whisper]
[--transcripts DIR] [--out sync.json]`

The reference defaults to the longest clip. After writing, print one row per clip: label, file name, camera,
gamma, fps, offset, method. Under each row print its checks (agree / DISAGREE) and warnings.

## Tests (offline: no media files, no network, no Meta API, no Whisper)

Write them in the style of the existing tests. They must cover:
- Sony LTC decode, including flag bits;
- timecode to seconds at 24, 25, 29.97 non-drop and drop-frame, and 59.94;
- the midnight wrap;
- iPhone vs `creation_time` capture time;
- the waveform on synthetic speech-like noise bursts, at a known offset with a different gain and noise on one side;
- drift recovered from a resampled copy;
- a short clip's false peak corrected by a Whisper hint;
- Whisper votes, moments and the one-sentence coincidence refused;
- the selection order and its warnings;
- placing against a non-reference clip;
- the `sync.json` shape, read back through `director.angles.load_sync`.

The whole suite must stay green.

## Limits

- Stdlib-only apart from numpy, which is guarded. No new dependencies.
- Do not change `angles.py`, `grade.py` or `app/`.
- No client names, people's names, footage paths or keys in code, tests or docs.
- Add a SYNC section to `docs/muse-code/BUILD-LOG.md` (who did what, model calls, token use) and a line to
  `AGENTS.md` under Layout.
