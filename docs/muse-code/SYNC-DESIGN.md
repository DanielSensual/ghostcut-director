# Design: multicam sync (`director sync`)

Implements [SYNC-BRIEF.md](SYNC-BRIEF.md) under [AGENTS.md](../../AGENTS.md). This is not a special mode; reply `go` to execute this plan, or tell me what to change. No code is written by this document.

Conventions that bind the build: Python ≥3.11, `from __future__ import annotations`, type annotations, `@dataclass(slots=True)` at module boundaries, stdlib only apart from guarded numpy (the `director` extra), no guestlist/Meta API anywhere in this feature, API keys never used, no media/client/person/path literals from real footage in code or tests. Existing readers are the contract and are not changed: [load_sync in angles.py](../../src/director/angles.py:193) reads `reference`, `clips[].label`, `clips[].offset_s`, `clips[].camera.{path,fps,width,height,duration_s}`; [load_sync_for_grade in grade.py](../../src/director/grade.py:461) additionally reads `clips[].camera.gamma`. Both ignore every other key, so all estimate/check detail is additive.

## 1. Module layout

| File | Change |
|---|---|
| `src/director/sync.py` | New. All sync logic: probe, Sony sidecar, timecode, clock, waveform, Whisper, combining, `sync.json` writer, `run()`. |
| `src/director/cli.py` | New `command_sync(args)` and a `sync` subparser. Thin: parse, require binaries, call `sync.run`, print rows. No logic moves into `cli.py`. |
| `tests/test_sync.py` | New. Offline `unittest` (§13). |
| `docs/muse-code/BUILD-LOG.md` | New SYNC section at build time: who designed/built/reviewed, any model calls and token use, live-test result. |
| `AGENTS.md` | One bullet under Layout for `sync.py`, at build time. |

`src/director/angles.py`, `src/director/grade.py` and `app/` are unchanged.

Imports in `sync.py`: stdlib (`argparse`-free; `json`, `math`, `re`, `shutil`, `subprocess`, `xml.etree.ElementTree`, `dataclasses`, `datetime`, `pathlib`, `fractions` only via ghostcut) plus `ghostcut.config.read_json/write_json` (only `write_json` is needed) and `ghostcut.ffmpeg_tools` (`run_command`, `probe_media` is **not** used — §3 needs entries it does not request — `parse_fraction`, `coerce_float`, `snap_frame_rate`, `ToolingError`, `require_ffmpeg_binaries` used by `cli.py` only). numpy is imported once, guarded:

```python
try:
    import numpy as np
except ImportError:  # the director extra is optional
    np = None  # type: ignore[assignment]
```

Every numpy entry point checks `np is None` first. `mlx_whisper` is never imported; it is an external binary found with `shutil.which`.

## 2. Constants and data classes

```python
METHODS = ("timecode", "clock", "waveform", "whisper")  # canonical order everywhere
PRECISION_S = {"timecode": 0.0, "clock": 2.0, "waveform": 0.002, "whisper": 0.15}
CONFIDENCE = {"timecode": 0.5, "clock": 0.2}  # waveform/whisper derive theirs (§6, §7)

SAMPLE_RATE = 8000            # waveform decode rate, Hz
BAND_HZ = (200, 3400)         # speech band-pass in ffmpeg
COARSE_HOP_S = 0.010          # 80 samples at 8 kHz
REFINE_HOP_S = 0.001          # 8 samples at 8 kHz
LOG_EPS = 1e-4                # added to RMS before log (§6.2)
MIN_OVERLAP_FLOOR_S = 10.0    # required overlap unless the shorter clip is shorter (§6.3)
R_CLEAR = 0.45                # a peak below this correlation is never clear
MARGIN_CLEAR = 0.08           # best must beat the best peak >1 s away by this
SECOND_PEAK_EXCLUSION_S = 1.0
GUIDE_EXTRA_S = 0.5           # guided window = guide precision + this
GUIDE_R_TOLERANCE = 0.02      # guided peak "about as good" as the global peak
WHISPER_PHRASE_WORDS = 3
WHISPER_CLUSTER_S = 0.3
WHISPER_MIN_MOMENTS = 2
WHISPER_MIN_CONFIDENCE = 0.6
DRIFT_WINDOW_S = 20.0
DRIFT_MIN_WINDOWS = 3
DRIFT_WINDOW_MIN_R = 0.30
DRIFT_SEARCH_S = 0.25         # local re-measure searches ±this around the predicted lag
DAY_S = 86400.0
```

Why these precisions: timecode is frame-exact when jammed, so its *precision* is 0 and the one-frame term in the agreement rule (§8.1) is what gives it width; the clock is a wall-clock stamp good to about ±2 s; the waveform refine step is 1 ms so 2 ms is an honest claim; Whisper word starts wobble by roughly a word onset, 0.15 s. Why `LOG_EPS = 1e-4`: RMS of quiet 8 kHz speech-band audio sits near 1e-3–1e-2; 1e-4 keeps silence finite (`log(1e-4)`) without compressing real level differences away.

```python
class SyncError(RuntimeError):
    """A sync step failed (unprobeable clip, no usable streams)."""

@dataclass(slots=True)
class CameraInfo:
    label: str
    path: str
    make: str = ""
    model: str = ""
    lens: str = ""
    fps: float = 0.0            # float(snap_frame_rate(probed fps)); 0.0 for audio-only
    width: int = 0
    height: int = 0
    duration_s: float = 0.0
    has_audio: bool = False
    gamma: str = ""             # s-log3-cine | s-log3 | hlg | pq | rec709 | "" (§3.3)
    timecode: str | None = None # "HH:MM:SS:FF" or "HH:MM:SS;FF", normalised separators
    tc_rate: float | None = None  # rate the timecode counts at (60.0 for a 59.94 clip)
    captured_at: str | None = None  # ISO 8601 with offset, normalised by parse_captured_at

@dataclass(slots=True)
class Estimate:
    method: str                 # timecode | clock | waveform | whisper
    offset_s: float             # absolute timeline offset of the target clip
    confidence: float           # 0..1
    precision_s: float          # from PRECISION_S
    against: str                # label of the clip it was measured against
    detail: dict = field(default_factory=dict)

@dataclass(slots=True)
class EstimateCheck:
    a: str                      # method of the first estimate
    b: str                      # method of the second estimate
    against_a: str
    against_b: str
    delta_s: float              # abs(offset_a - offset_b)
    threshold_s: float          # §8.1
    agree: bool

@dataclass(slots=True)
class ClipResult:
    label: str
    camera: CameraInfo
    offset_s: float | None      # None = unplaced
    method: str | None          # "reference" | a method name | None
    estimates: list[Estimate] = field(default_factory=list)
    checks: list[EstimateCheck] = field(default_factory=list)
    drift_ppm: float | None = None
    warnings: list[str] = field(default_factory=list)

@dataclass(slots=True)
class SonySidecar:
    gamma: str = ""
    captured_raw: str | None = None
    tc_fps: float | None = None
    timecode: str | None = None  # decoded from the frameCount="0" LtcChange

@dataclass(slots=True)
class TranscriptWord:
    word: str                   # normalised (§7.2); empty words are dropped before this
    start_s: float
    end_s: float

@dataclass(slots=True)
class WaveformPeak:
    offset_s: float             # target offset relative to the anchor (add anchor offset for absolute)
    r: float
    second_r: float             # best r more than SECOND_PEAK_EXCLUSION_S away; -1.0 if none exists
    margin: float               # r - second_r
    overlap_s: float
    clear: bool                 # r >= R_CLEAR and margin >= MARGIN_CLEAR and overlap ok
```

Internal caches (`dict[Path, object]` for envelopes and transcripts) live in `run()` and are passed down; they are not dataclasses and never serialised.

## 3. Probe and `CameraInfo`

### 3.1 ffprobe

`probe_clip(path: Path) -> dict` runs exactly this via `run_command` and returns `json.loads(result.stdout)`; a non-zero exit raises `ToolingError` from `run_command`, malformed JSON or a payload with neither a video nor an audio stream raises `SyncError(f"no usable streams: {path}")`:

```python
["ffprobe", "-v", "error", "-show_entries",
 "format=duration:stream=index,codec_type,codec_name,width,height,"
 "r_frame_rate,avg_frame_rate,sample_rate,channels,color_transfer:stream_tags:format_tags",
 "-of", "json", str(path)]
```

`ghostcut.ffmpeg_tools.probe_media` is not used: it requests no tags, no `color_transfer` and no per-stream tags, and timecode/make/model live only there. Tag access is case-insensitive: build one merged dict per scope by lower-casing keys. Stream tags appear in the JSON as a `tags` dict on each stream; format tags as `format.tags`.

### 3.2 Field extraction — `camera_from_probe(label, path, probe, sidecar) -> CameraInfo`

Pure (tests feed dicts). Steps in order:

1. `video` = first stream with `codec_type == "video"`; `audio` = first with `codec_type == "audio"`. `has_audio = audio is not None`. An audio-only clip (a lav recorder file) is legal: `fps=0.0`, `width=height=0`, `gamma=""`.
2. `duration_s = coerce_float(format.duration)`; if 0 and the video stream carries no duration either, keep 0.0 (reference choice and overlap math tolerate it; a 0-duration non-reference clip simply cannot overlap and will not place by waveform).
3. `fps`: `parse_fraction(avg_frame_rate or r_frame_rate)`; stored value is `float(snap_frame_rate(raw))` when `raw > 0`, else 0.0. So 59.940059… is stored (and written, after §10 rounding) as 59.94, and `snap_frame_rate` remains the single source of rate truth shared with `angles`/`grade`.
4. `width`/`height`: `int(video.width or 0)` etc.
5. Tags: merged search order is (a) the video stream's tags, (b) format tags, (c) every other stream's tags in stream order — first hit wins, key match is exact after lower-casing:
   - `make`: `make`, `manufacturer`, `com.apple.quicktime.make`
   - `model`: `model`, `com.apple.quicktime.model`
   - `lens`: `lens`, `lensmodel`, `lens_model`, `com.apple.quicktime.lensmodel`
   Values are `str(value).strip()`; missing → `""`.
6. `timecode`: first non-empty `timecode` tag found in the same search order (Sony writes it on a data/metadata stream, so step (c) matters). Normalise by stripping whitespace; keep the `;` if present (it is the drop-frame signal, §4). If none, fall back to `sidecar.timecode`.
7. `tc_rate`: `sidecar.tc_fps` if present, else derived from the snapped rate: if `snap_frame_rate(fps)` has denominator 1001, `tc_rate = numerator / 1000` (24000/1001 → 24.0, 60000/1001 → 60.0); else `tc_rate = float(snapped)` when `fps > 0`. `None` when there is no timecode and no rate, or `fps == 0`.
8. `captured_at`: first parseable (§5.1) of, in order: format/stream tag `com.apple.quicktime.creationdate`, `sidecar.captured_raw`, tag `creation_time`. The iPhone's QuickTime creation date is its own network time and is preferred over `creation_time`, which can be the copy time; Sony's sidecar date likewise beats the generic tag. Stored as the normalised `datetime.isoformat()` of the parsed value, or `None`. The raw tag is not kept.

### 3.3 Gamma

Priority: sidecar `CaptureGammaEquation` (§3.4) first, then the video stream's `color_transfer`:

| Source value (lower-cased, stripped) | `gamma` |
|---|---|
| sidecar contains `s-log3` and `cine` | `s-log3-cine` |
| sidecar contains `s-log3` | `s-log3` |
| sidecar contains `hlg` / transfer `arib-std-b67` | `hlg` |
| sidecar contains `pq` or `2084` / transfer `smpte2084` | `pq` |
| sidecar contains `709` / transfer `bt709` | `rec709` |
| anything else | the lower-cased transfer string, or `""` |

This is exactly the vocabulary [grade.py](../../src/director/grade.py:195) maps to gamuts, so `director grade` works on the output unchanged.

### 3.4 Sony sidecar — `find_sidecar(clip_path)`, `parse_sony_sidecar(path)`, `decode_ltc_hex(value)`

The sidecar sits next to the clip and is named `<stem>M01.XML` (clip `C0001.MP4` → `C0001M01.XML`, exact case). `find_sidecar` returns that path if it exists, else `None`. `parse_sony_sidecar` parses with `ElementTree`, matches elements by local name (tag after `}`), and never raises: unreadable or malformed XML returns an empty `SonySidecar` and the caller adds no warning (a missing sidecar is the normal iPhone case). It extracts:

- `CreationDate`: the element's `value` attribute if present, else its text → `captured_raw`.
- `CaptureGammaEquation`: `value` attribute or text → mapped by §3.3.
- The `LtcChangeTable` element's `tcFps` attribute → `tc_fps` via `coerce_float` (0 → `None`). Among its `LtcChange` children, the one with `frameCount="0"`; its `value` attribute goes to `decode_ltc_hex`. If several claim `frameCount="0"`, the first wins.

`decode_ltc_hex(value: str) -> str | None`: `value` is 8 hex digits = 4 bytes in the order frames, seconds, minutes, hours. Each byte is BCD (high nibble = tens, low nibble = units) after masking off the flag bits that share the byte: frames `& 0x3F`, seconds `& 0x7F`, minutes `& 0x7F`, hours `& 0x3F` (drop-frame and field/colour flags live in the masked bits). Decode as `tens * 10 + units` per byte. Return `None` (no timecode) when the string is not exactly 8 hex digits, when any units nibble exceeds 9, or when minutes/seconds exceed 59. Otherwise return `f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"` (never `;` — the sidecar does not say drop-frame; §4 treats it as non-drop).

## 4. Timecode

### 4.1 `timecode_to_seconds(tc: str, fps: float, tc_rate: float | None = None) -> float`

Converts one start timecode to real seconds since midnight. Pure.

1. Parse with `^(\d{2}):(\d{2}):(\d{2})([:;])(\d{2})$` after stripping. No match, minutes/seconds ≥ 60 → `ValueError`. `drop = (separator == ";")`.
2. Nominal rate `R`: snap `fps` with `snap_frame_rate`; if `tc_rate` is given and > 0 it wins and `R = int(round(tc_rate))` unless `tc_rate` itself snaps to a 1001-denominator rate, in which case `R = numerator // 1000`. Without `tc_rate`: denominator 1001 → `R = numerator // 1000` (24000/1001 → 24); otherwise `R = int(snapped)`. For an audio-only clip (`fps == 0`) with a `tc_rate`, `R = int(round(tc_rate))`; with neither, `ValueError`. Frames field must be `< R` (or `< 2*R` is never accepted) else `ValueError`.
3. Frame count. Non-drop: `frames = ((hh*3600 + mm*60 + ss) * R) + ff`. Drop (only legal for `R` 30 or 60, else `ValueError`): skipped labels per minute `D = 2` at 30, `4` at 60 (i.e. `R // 15`); `total_minutes = hh*60 + mm`; `dropped = D * (total_minutes - total_minutes // 10)`; `frames = ((hh*3600 + mm*60 + ss) * R) + ff - dropped`. Drop-frame therefore keeps labelled time aligned with wall time at every tenth minute.
4. Real seconds: the code counts at the round rate `R`, but on a 23.976/29.97/59.94 clip (`snap_frame_rate` denominator 1001) each labelled second lasts 1.001 real seconds: `seconds = frames / R * 1001 / 1000`. On any other rate `seconds = frames / R`.

Worked values the tests pin (§13): `00:01:00:12` at 24 → 60.5; at 25 → 60.48; `00:01:00:00` at 29.97 non-drop → 60.06; `00:01:00;02` at 29.97 drop → 60.06; `00:10:00;00` at 29.97 drop → 599.9994 (≈600, the tenth minute drops nothing); `00:01:00;04` at 59.94 drop → 60.06; `00:00:01:30` at 59.94 non-drop → 1.5015.

### 4.2 From timecodes to an offset — `wrap_day_delta(delta_s)` and `timecode_estimate(target, anchor)`

`wrap_day_delta(delta_s: float) -> float` returns `((delta_s + DAY_S/2) % DAY_S) - DAY_S/2`, i.e. the same difference in (−12 h, +12 h]. An offset of more than half a day is a midnight wrap, not a 23-hour shoot.

`timecode_estimate(target: ClipResult, anchor: ClipResult) -> Estimate | None`: `None` when either clip lacks a timecode or its rate cannot be determined. Otherwise `offset_s = anchor.offset_s + wrap_day_delta(timecode_to_seconds(target) - timecode_to_seconds(anchor))`, each side converted with its own `fps`/`tc_rate` (cameras may run different rates). `confidence = 0.5`, `precision_s = 0.0`, `against = anchor.label`, `detail = {"target_timecode": ..., "anchor_timecode": ..., "target_tc_rate": ..., "anchor_tc_rate": ...}`. Timecode is never trusted on its own while an audio method has an answer; that is enforced by the pick order (§8.4), not here.

## 5. Capture clock

### 5.1 `parse_captured_at(value: str | None) -> datetime | None`

Never raises; `None` in → `None` out; unparseable → `None`. Normalisation, in order: strip; replace a trailing `Z` with `+00:00`; replace a space between date and time with `T`; rewrite a trailing numeric zone `([+-]\d{2})(\d{2})$` to `\1:\2` (so `-0400` parses — `datetime.fromisoformat` on 3.11 already accepts it, but the rewrite keeps one code path for every shape, including Sony's); then `datetime.fromisoformat`. A naive result gets `timezone.utc` attached (stamps are compared, never displayed, and mixing naive with aware must not raise). Two stamps are only ever subtracted, so a wrong UTC assumption on both sides cancels; on one side it is a clock error the ±2 s precision already understates — accepted, and §5.2's warning covers clock-only placement.

### 5.2 `clock_estimate(target, anchor) -> Estimate | None`

`None` when either `captured_at` is missing/unparseable. `offset_s = anchor.offset_s + (target_dt - anchor_dt).total_seconds()` — no day wrap (the stamp carries a date). `confidence = 0.2`, `precision_s = 2.0`, `detail = {"target_captured_at": ..., "anchor_captured_at": ...}`. The clock's job is to narrow a waveform search (§8.2) or, flagged, to place a clip nothing else could (§8.4 step 4).

## 6. Waveform

Needs numpy; every function in this section returns `None` (no estimate) when `np is None`, and `run()` adds the warning `"Waveform sync unavailable: numpy is not installed."` once per non-reference clip when the method was requested but numpy is missing.

### 6.1 Decode — `decode_command(path)`, `decode_samples(path)`

```python
["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1",
 "-ar", "8000", "-af", "highpass=f=200,lowpass=f=3400", "-f", "f32le", "pipe:1"]
```

Band-passing to roughly 200–3400 Hz in ffmpeg keeps rumble and hiss from driving the correlation. `decode_samples` runs it with `subprocess.run(capture_output=True)` (binary pipe, so not the text-mode `run_command` — the same reason [grade.decode_centre_frame](../../src/director/grade.py:588) bypasses it), and returns `np.frombuffer(stdout, dtype=np.float32).copy()`, or `None` on non-zero exit, empty output, or a clip with `has_audio == False`. Decoded sample arrays are cached per path inside `run()`; envelopes (§6.2) are cached per `(path, hop)`.

### 6.2 Log RMS envelopes — `log_envelope(samples, hop_samples) -> np.ndarray`

Split the samples into non-overlapping blocks of `hop_samples` (drop the trailing partial block); per block `rms = sqrt(mean(x**2))`; the envelope is `log(rms + LOG_EPS)`. Log compression is what lets a chest lav and a camera mic three metres away correlate: it squeezes the level difference between a close and a distant mic into an additive shift, which the per-lag normalisation (§6.3) then removes. Coarse envelopes use 80 samples (10 ms), refine envelopes 8 samples (1 ms). An envelope shorter than 2 samples, or with zero variance, yields no estimate.

### 6.3 Peak search — `find_peak(ref_env, tgt_env, hop_s, *, window_s=None) -> WaveformPeak | None`

Sign convention, fixed once here: an event at timeline `T` sits at ref-local `T - anchor.offset_s` and target-local `T - target.offset_s`, so with `lag = ref_index - target_index`, `relative_offset_s = lag * hop_s` is `target.offset_s - anchor.offset_s`. Every lag, offset and vote in this design uses that identity.

1. Z-score each envelope whole (`(e - mean) / std`); `std == 0` → `None`.
2. Full cross-correlation at every lag by FFT: zero-pad both to the next power of two ≥ `len(ref) + len(tgt) - 1`, correlate (FFT of one times the conjugate FFT of the other, inverse FFT, real part), and divide each lag's sum by its overlap sample count so the result is a per-lag correlation `r` in approximately [−1, 1]. Lag range is `-(len(tgt)-1) .. len(ref)-1` in envelope samples. (The implementation may use the equivalent `np.correlate` identity to fix indexing, but the FFT path is what runs; the §13 synthetic tests pin the sign.)
3. Overlap gate: for a lag, `overlap_s = (number of overlapping envelope samples) * hop_s`. Required overlap is `min(MIN_OVERLAP_FLOOR_S, 0.5 * min(ref_duration_s, tgt_duration_s))` where durations are `len(env) * hop_s` — 10 s normally, half the shorter clip when that is less (a 12 s clip needs 6 s). Lags failing the gate are excluded (treated as `r = -inf`). If no lag passes, `None`.
4. Window gate (guided searches, §8.2): when `window_s = (centre_s, radius_s)` is given, lags whose `relative_offset_s` falls outside `[centre_s - radius_s, centre_s + radius_s]` are likewise excluded.
5. Best lag = argmax `r`. `second_r` = max `r` over lags at least `SECOND_PEAK_EXCLUSION_S / hop_s` samples away from the best (still passing both gates); `-1.0` when no such lag exists. `margin = r - second_r`; `clear = r >= R_CLEAR and margin >= MARGIN_CLEAR`. A short clip latching onto any loud section can produce one high peak, which is why a lone peak is not proof: `clear` demands both the height and the margin.

`waveform_estimate` (below) calls `find_peak` twice: coarse at 10 ms over all lags, then refine — 1 ms envelopes, `find_peak` again with the lag window restricted to coarse_best ± 15 ms (implemented as `window_s=(coarse_offset_s, 0.015)` on the fine lag axis plus the same overlap gate). The refined peak is the estimate's peak. Precision stays 2 ms.

### 6.4 Drift — `measure_drift(ref_env1ms, tgt_env1ms, coarse_offset_s, target_duration_s) -> tuple[float, float] | None`

Returns `(offset_at_target_start_s, drift_ppm)` (both relative to the anchor) or `None`.

1. The overlap in target-local time is `[max(0, -coarse_offset_s), min(tgt_duration, ref_duration - coarse_offset_s))` (from the §6.3 identity). Cut it into consecutive non-overlapping `DRIFT_WINDOW_S` (20 s) windows; a trailing window shorter than 10 s is dropped (merged into nothing — too short to trust), exactly the §6.3 floor logic at window scale.
2. Per window, take the 1 ms envelopes for the window on both sides and run the §6.3 correlation restricted to ±`DRIFT_SEARCH_S` (0.25 s) around the coarse lag for that window. Keep the window's `(target_local_centre_s, measured_relative_offset_s)` only when its local `r >= DRIFT_WINDOW_MIN_R`.
3. Fewer than `DRIFT_MIN_WINDOWS` kept windows, or kept centres spanning less than 40 s → `None` (drift unmeasured; the global offset stands).
4. Least-squares line `offset = intercept + slope * target_local_centre_s` (`np.polyfit(..., 1)`). `drift_ppm = slope * 1e6`; `intercept` is the offset at the target's first frame by construction (target-local time 0). This intercept **replaces** the global refined offset as the waveform estimate's offset whenever drift measurement succeeds: a drifting pair's single global lag is an average over the overlap and is wrong at both ends, the intercept is right where the clip starts, which is what `sync.json` records.

Warning (added in §8.5, not here): drift moves the sync over the clip by `abs(slope) * target_duration_s`; when that exceeds half a frame (`0.5 / frame_rate`, §8.1) the clip gets a drift warning, because `sync.json` carries one offset and the far end of the clip will be off by that much.

### 6.5 `waveform_estimate(target, anchor, envelopes, *, guide=None) -> Estimate | None`

`guide` is `None` or `(centre_relative_offset_s, radius_s, guide_method)`. The `envelopes` argument is the run-level cache `dict[tuple[str, int], np.ndarray]` keyed by `(clip_path, hop_samples)` (80 coarse, 8 refine), filled on demand through §6.1/§6.2; tests pre-fill it or patch `decode_samples`. It runs §6.3 (windowed when guided), then §6.4. `None` when either side has no usable envelope or no lag passes the gates. On success:

- `offset_s = anchor.offset_s + relative_offset_s` (intercept-relative when drift succeeded, else refined-lag-relative).
- `confidence = min(1.0, max(0.0, r))` of the refined (or guided) peak.
- `precision_s = 0.002`.
- `detail = {"r": r, "second_r": second_r, "margin": margin, "clear": clear, "overlap_s": overlap_s, "guided": bool, "guide_method": guide_method or None, "drift_ppm": drift_ppm or None, "drift_windows": n_windows_kept}`. When a guided search replaces a global one, the caller (§8.2) additionally stores the unguided peak under `detail["unguided"] = {"offset_s": ..., "r": ..., "margin": ...}` — the brief requires the unguided result to stay in the details.

## 7. Whisper

No Meta API, no network. Word-timed transcripts come from, in order: `--transcripts DIR`, a cache next to the output, or a local `mlx_whisper` run. Without any of those the method is skipped for that pair (never an error).

### 7.1 Getting words — `load_transcript_words(path)`, `get_transcript_words(clip, transcripts_dir, cache_dir)`

`load_transcript_words(path: Path) -> list[TranscriptWord]`: parse Whisper JSON in either shape — `{"segments": [{"words": [{"word", "start", "end"}]}]}` flattened in order, or a top-level `{"words": [...]}`. Keep entries with non-empty normalised text (§7.2) and a numeric `start`; missing `end` → next word's `start`, else `start + 0.30` (the [angles.load_timed_words](../../src/director/angles.py:108) fallback, reused in spirit). Sort by `start`. Malformed JSON or a missing file → `[]`, never an exception (this mirrors `load_sync`'s strictness being reserved for `sync.json` itself).

`get_transcript_words(clip: CameraInfo, transcripts_dir: Path | None, cache_dir: Path) -> list[TranscriptWord]`:

1. If `transcripts_dir` is given: load `transcripts_dir / f"{Path(clip.path).stem}.json"`; a missing file means no Whisper estimate for pairs involving this clip (no fallback to transcribing — the owner pointed at a directory deliberately).
2. Else the cache file `cache_dir / f"{stem}.json"` where `cache_dir = out_path.parent / "sync-transcripts"`: if it exists and is non-empty, load it.
3. Else if `shutil.which("mlx_whisper")`: run `["mlx_whisper", clip.path, "--output-format", "json", "--output-dir", str(cache_dir), "--word-timestamps", "True", "--verbose", "False"]` via `subprocess.run(capture_output=True)`; word timestamps on, no API spend. Non-zero exit or no output file → `[]` for this clip. If `mlx_whisper` is absent → `[]`.

Results are cached per clip inside `run()`, so a clip is transcribed at most once per run and re-runs of `director sync` with the same `--out` reuse the cache directory.

### 7.2 Normalising and voting — `normalize_word(word)`, `whisper_estimate(target, anchor, target_words, anchor_words)`

`normalize_word(word: str) -> str`: lower-case, then strip leading/trailing characters that are not letters, digits or an internal apostrophe (`re.sub(r"^[^\w']+|[^\w']+$", "", word.lower()).strip("'")` is the shape; the test pins `"Hello," -> "hello"`, `'"world!"' -> "world"`). Internal punctuation stays (`"don't"` keeps its apostrophe on both sides, so it still matches itself). Empty after normalising → the word is dropped in §7.1.

`whisper_estimate(...) -> Estimate | None`, with both word lists already normalised:

1. Build every consecutive 3-word phrase in each transcript: key = the tuple of 3 normalised words; value = list of `(start_s of first word, first-word index)`. A phrase **votes only when it occurs exactly once in each transcript** — a repeated phrase ("thank you very much" twice) cannot say which occurrence matches, so it abstains rather than guessing.
2. Each qualifying phrase votes `vote = anchor_start - target_start` (relative offset, §6.3 identity; absolute = `anchor.offset_s + vote`).
3. Densest cluster: sort the votes; slide a window of width `WHISPER_CLUSTER_S` (0.3 s) and keep the window holding the most votes; ties go to the window with the smallest internal spread `max-min`, then the earliest. Misheard words simply fail to match a phrase and never vote, which is why this beats averaging all votes. The estimate's relative offset is the **median** of the winning window's votes.
4. Moments: take the winning phrases (those whose vote is in the window), sort by anchor first-word index; a new **moment** starts when the next phrase's index is more than 1 greater than the previous phrase's index **or** its anchor start is more than 2.0 s after the previous phrase's anchor start. Overlapping phrases from one sentence (indices i, i+1, i+2 all matching) are therefore one moment, not three. `moments` = number of such runs.
5. Refusal: `moments < WHISPER_MIN_MOMENTS` (2) → `None`. One sentence matching by coincidence — the same question asked in two different interviews — is exactly one moment and is refused. Also `None` when there are no qualifying phrases at all.
6. On success: `confidence = min(1.0, moments / 3.0)` (2 moments → 0.667, 3+ → 1.0; §8.4 requires ≥ 0.6, so 2 clean moments just pass and anything thinner does not), `precision_s = 0.15`, `detail = {"votes": total_qualifying_votes, "cluster_votes": len(winning_window), "moments": moments}`.

## 8. Combining: checks, guided re-search, halving, pick

All of this section is pure given estimate objects plus, where noted, a waveform re-search callable, so §13 tests it without media.

### 8.1 Agreement — `frame_duration_s(target, anchor)`, `estimates_agree(a, b, frame_s)`, `build_checks(estimates, frame_s)`

`frame_s = 1 / target.camera.fps` when that is > 0, else `1 / anchor.camera.fps` when > 0, else `1/30`. Two estimates **agree** when `abs(a.offset_s - b.offset_s) <= max(a.precision_s, b.precision_s) + frame_s`. Timecode's precision is 0, so against the waveform it agrees within 2 ms + one frame; clock against anything agrees within 2 s + one frame (the clock can corroborate, never veto). `build_checks` emits one `EstimateCheck` per unordered pair of estimates, in estimate-list order, with `delta_s = abs(...)` and that threshold. Checks are recomputed after any estimate is replaced or penalised (§8.2, §8.3); deltas do not change under halving, but the rebuild keeps one invariant — `clip.checks` always describes `clip.estimates` as written.

### 8.2 Guided re-search — `resolve_guided(global_est, guide_est, guided_est)`

Trigger (per target/anchor attempt, before picking): a waveform estimate exists **and** disagrees (§8.1) with a guide estimate, where the guide is Whisper if present, else timecode, else clock — the brief's order ("with Whisper, or without Whisper with timecode or clock"). Then `run()` re-runs `waveform_estimate` with `guide = (guide.offset_s - anchor.offset_s, guide.precision_s + GUIDE_EXTRA_S, guide.method)`: the search is confined to the window the other method allows, its precision plus 0.5 s (Whisper: ±0.65 s; timecode: ±0.5 s; clock: ±2.5 s).

`resolve_guided(global_est, guide_est, guided_est) -> tuple[Estimate, bool]` decides, purely:

- `guided_est is None` → keep global, `False`.
- Global was **weak** — `not global.detail["clear"]` (r < 0.45 or margin < 0.08) — → take guided, `True`.
- Else guided correlates about as well: `guided.detail["r"] >= global.detail["r"] - GUIDE_R_TOLERANCE` (0.02) → take guided, `True`.
- Else keep global, `False` (a strong, clear global peak is not moved by a weaker guided one).

When taken, the guided estimate replaces the waveform entry in the list and gains `detail["unguided"]` (§6.5); when not taken, the global stays and the guided peak is recorded only inside the attempt's warnings via §8.5 (the disagreement is real either way). If there was no global waveform estimate at all, no guided search runs — there is nothing to re-search, and Whisper stands or falls on its own (§8.3 still applies only when a guided result exists).

### 8.3 Halving Whisper — `penalise_whisper(whisper_est, guided_est, both_have_audio) -> bool`

When the words line up but the sound does not: a guided search was run against Whisper (§8.2), the guided peak still has `r < R_CLEAR` (0.45), and both clips have audio — then the same words were probably said twice. Mutate the Whisper estimate: `detail["confidence_before_halving"] = confidence`, `confidence = confidence / 2`, `detail["confidence_halved"] = True`, add the clip warning `"Whisper matches words that do not match in sound (guided waveform r=<r:.2f>); Whisper confidence halved."`, return `True`. A halved Whisper (0.667 → 0.333) falls below `WHISPER_MIN_CONFIDENCE`, so the clip is not placed on Whisper alone — and, per §8.4, a halved Whisper also cannot act as the "backing" that makes a waveform trusted. When `both_have_audio` is false, or no guided result exists, nothing is halved: absent sound is not evidence against the words.

### 8.4 Pick — `choose_pick(estimates, checks) -> tuple[float | None, str | None]`

Fixed order; the first applicable step wins. `frame_s` is the §8.1 value for the attempt.

1. **Trusted waveform**: a waveform estimate that is clear (`detail["clear"]`), **or** backed by another non-clock method — some estimate with method in (`whisper`, `timecode`), not confidence-halved, whose check against the waveform agrees. Clock backing does not count (the clock agrees with almost everything within 2 s). → pick the waveform.
2. **Whisper**: a Whisper estimate with `detail["moments"] >= 2` and `confidence >= WHISPER_MIN_CONFIDENCE` (0.6). → pick Whisper.
3. **Timecode**: → pick timecode, flagged (§8.5) "right only if the cameras were jammed".
4. **Clock**: → pick clock, flagged "check by eye".
5. Otherwise `(None, None)` — unplaced for this anchor.

Confidence is otherwise not compared across methods: the order *is* the trust model, because any single method can be confidently wrong (a free-running timecode is exact-looking and hours off; only the cross-checks in steps 1–2 earn trust).

### 8.5 Warnings — `attempt_warnings(target, estimates, checks, pick_method, pick_offset)`

Plain English, exact templates (`<x>` = `:.3f` seconds unless shown):

- Pick is timecode: `"Placed by timecode only: right only if the cameras were jammed."`
- Pick is clock: `"Placed by capture clock only: check by eye."`
- For every timecode or Whisper estimate (deduplicated by `(method, against)`) whose method differs from the pick and which disagrees with the pick: `"<Method> disagrees with the chosen offset by <delta> s."` — with `<Method>` capitalised (`Timecode`/`Whisper`), plus for timecode only the suffix `" It is right only if the cameras were jammed."` when the pick is not itself timecode. Clock disagreements never warn (2 s precision disagrees by design).
- Drift: when any waveform estimate for the clip (the picked one first, else the first with drift) has `drift_ppm`, and `abs(drift_ppm) * 1e-6 * target.camera.duration_s > 0.5 * frame_s`: `"Clock drift <ppm:+.0f> ppm moves sync by <ms:.0f> ms over this clip (more than half a frame); the offset is exact at the clip start."`
- No pick from any anchor (§9): `"Could not be placed on the timeline."`
- Method-availability warnings from §6/§7 (numpy missing, no audio, no transcript) are appended by `run()`, once per clip per cause.

`ClipResult.drift_ppm` is the picked waveform's `detail["drift_ppm"]` when the pick is waveform, else the first waveform estimate's drift for that clip, else `None` — drift is reported whenever measured, even when another method placed the clip.

## 9. Orchestration — `run(*, clip_paths, reference_path, methods, transcripts_dir, out_path) -> dict`

Signature mirrors [grade.run](../../src/director/grade.py:836) keyword style: `run(*, clip_paths: list[Path], reference_path: Path | None, methods: list[str], transcripts_dir: Path | None, out_path: Path) -> dict`. Steps in order; nothing reorders them:

1. **Validate**: fewer than 2 clips → `ValueError("need at least two clips")`; a path that does not exist → `FileNotFoundError`; the same resolved path given twice → `ValueError`. `methods` is already canonicalised by `parse_methods` (§11).
2. **Probe** every clip with §3 (`probe_clip`, `find_sidecar` + `parse_sony_sidecar`, `camera_from_probe` with a placeholder label). A clip whose probe raises `SyncError`/`ToolingError` aborts the run — a `sync.json` describing an unreadable clip would poison `angles` later, and `main()` already turns these into `Error: ...` + exit 1.
3. **Reference**: `reference_path` given → the clip whose resolved path matches it (no match → `ValueError`); else the longest `duration_s`, ties to the first given. Labels: the reference is `A`; the rest are `B`, `C`, … in the order given (so with clips `[x, y, z]` and `y` the reference: `y=A, x=B, z=C`). Labels past `Z` continue `AA`, `AB`, … (`_label_for_index`). `CameraInfo.label` is rewritten to the final label here, before any estimate is built.
4. **Reference result**: `ClipResult(label="A", offset_s=0.0, method="reference", estimates=[], checks=[], drift_ppm=None, warnings=[])`.
5. **Per non-reference clip, in label order — anchor attempts.** Anchors are tried in the order: the reference, then each already-placed non-reference clip in label order. For each anchor, build the attempt's estimates in canonical method order, including only requested methods:
   - timecode (§4.2) and clock (§5.2) against that anchor (both are differences of absolute stamps plus the anchor's offset, so they are valid against any placed anchor);
   - waveform (§6.5) against that anchor, envelopes cached across anchors;
   - Whisper (§7.2) against that anchor, transcripts cached across anchors.
   Then §8.2 guided re-search (if triggered), §8.3 halving (if applicable), `build_checks`, `choose_pick`.
   - **Stop early**: if the pick is a trusted waveform (step 1 of §8.4 — clear or backed), accept this attempt immediately and try no further anchors. This is the common good case and avoids decoding against every camera.
   - Otherwise remember the attempt; if it produced any pick and no earlier attempt did, remember it as the fallback. Continue to the next anchor looking for a trusted waveform (an iPhone the A-cam barely hears may be clear against the B-cam).
   - After all anchors: the accepted trusted-waveform attempt wins; else the fallback (first attempt, i.e. against the reference, that produced any pick); else unplaced (`offset_s=None, method=None`).
6. **Assemble the clip result**: `estimates` = the union of all attempts' estimates in attempt order (timecode/clock deduplicated by `(method, against)` — they are anchor-independent in value once absolute, and repeating them per anchor would double-count in `build_checks`); `checks = build_checks(estimates, frame_s)` against the reference frame rate (the target's own `fps` first, §8.1); warnings = the chosen attempt's §8.5 warnings (recomputed against the final pick) plus availability warnings; `drift_ppm` per §8.5. Because later anchors' waveform/Whisper estimates are checked against the final pick too, a B-cam waveform that disagrees with an A-cam pick still surfaces as a §8.5 disagreement warning — that is intended, not noise.
7. **Payload and write** (§10) via `write_json(out_path, payload)`; return the payload.

One clip's failure to place never fails the run: it is written with `offset_s: null` and `angles`/`grade` already treat unplaced clips as covering nothing.

## 10. `sync.json`

Built by `build_payload(results: list[ClipResult], reference_path: str, methods: list[str]) -> dict` (pure; §13 test 22 calls it directly) and written with `write_json` (sorted keys, indent 2) to `--out`. Internals keep full precision; rounding happens only in `build_payload`: offsets/deltas/durations/fps to 3 decimals (`round(x, 3)`), confidence to 3, `drift_ppm` to 1. `offset_s: null` and `method: null` for an unplaced clip.

```json
{
  "reference": "<path of the reference clip, as given>",
  "methods": ["timecode", "clock", "waveform", "whisper"],
  "clips": [
    {
      "label": "A",
      "offset_s": 0.0,
      "method": "reference",
      "camera": {"path": "...", "make": "...", "model": "...", "lens": "...", "fps": 59.94,
                 "width": 3840, "height": 2160, "duration_s": 312.4, "has_audio": true,
                 "gamma": "s-log3-cine", "timecode": "13:37:55:21", "tc_rate": 60.0,
                 "captured_at": "2026-11-02T14:03:11-05:00"},
      "estimates": [
        {"method": "waveform", "offset_s": 6.0, "confidence": 0.82, "precision_s": 0.002,
         "against": "A", "detail": {"r": 0.82, "second_r": 0.31, "margin": 0.51, "clear": true,
         "overlap_s": 120.0, "guided": false, "guide_method": null, "drift_ppm": null, "drift_windows": 0}}
      ],
      "checks": [
        {"a": "waveform", "b": "whisper", "against_a": "A", "against_b": "A",
         "delta_s": 0.004, "threshold_s": 0.167, "agree": true}
      ],
      "drift_ppm": null,
      "warnings": []
    }
  ]
}
```

`reference` is the reference clip's path string exactly as passed on the command line (`angles` matches clips by this string, falling back to label `A`). `methods` is the canonicalised requested list (§11), in `METHODS` order — it records what was attempted, not what succeeded. The reference clip's `estimates`/`checks` are empty and its `method` is `"reference"`. `camera.timecode`/`tc_rate`/`captured_at` are `null` when absent. This shape is pinned by the §13 round-trip test through `angles.load_sync`.

## 11. CLI

```
director sync CLIP [CLIP …] [--reference CLIP] [--methods timecode,clock,waveform,whisper]
              [--transcripts DIR] [--out sync.json]
```

`parse_methods(text: str | None) -> list[str]`: `None` → all of `METHODS`; else split on commas, strip, lower-case, drop empties, deduplicate, return in `METHODS` order. An unknown name or an empty result → `ValueError(f"unknown sync method: {name}")` / `ValueError("no sync methods")`, which `main()` already reports as `Error: ...` with exit 1.

Subparser (after `grade` in `build_parser`): positional `clips` (`nargs="+"`), `--reference` (default `None`), `--methods` (default `None`), `--transcripts` (default `None`), `--out` (default `"sync.json"`). `command_sync(args)`: `require_ffmpeg_binaries()` unconditionally (probing needs ffprobe, waveform needs ffmpeg), then `sync.run(clip_paths=[Path(p) for p in args.clips], reference_path=Path(args.reference) if args.reference else None, methods=parse_methods(args.methods), transcripts_dir=Path(args.transcripts) if args.transcripts else None, out_path=Path(args.out))`.

Print, one row per clip in label order, then its checks and warnings indented under it, then the wrote line:

```
A clipA.mp4 Sony A7SIII s-log3-cine 59.94fps offset +0.000s method reference
B clipB.mp4 Sony A7IV s-log3-cine 59.94fps offset +6.000s method waveform
  check waveform vs whisper: agree (delta 0.004s)
  check timecode vs waveform: DISAGREE (delta 1.250s)
  ! Timecode disagrees with the chosen offset by 1.250 s. It is right only if the cameras were jammed.
Wrote sync.json
```

Row format exactly: `f"{label} {filename} {make} {model} {gamma or '-'} {fps:.2f}fps offset {offset} method {method}"` with single spaces dropped when a field is empty (build from non-empty parts joined by spaces, so an audio-only lav row reads `C lav.wav - 0.00fps offset ...` — `fps` prints `0.00` there); `filename` is `Path(camera.path).name`; `offset` is `f"{offset_s:+.3f}s"` or `"--"` when unplaced; `method` is the clip's method or `"unplaced"`. Check lines: two spaces + `f"check {a} vs {b}: {'agree' if agree else 'DISAGREE'} (delta {delta_s:.3f}s)"`. Warnings: two spaces + `f"! {warning}"`.

## 12. Edge cases (decided)

| Case | Behaviour |
|---|---|
| Fewer than 2 clips; duplicate resolved path; `--reference` not among the clips | `ValueError` before any probe; exit 1 via `main()`. |
| Clip file missing | `FileNotFoundError` from §9 step 1. A file that exists but will not probe aborts the run (`SyncError`/`ToolingError`) — no partial `sync.json`. |
| Audio-only clip (lav recorder) | Legal clip: `fps/width/height` 0, `gamma ""`, places by waveform/Whisper/clock like any other; `frame_s` falls back per §8.1. A clip with neither audio nor video streams is the only probe-level `SyncError`. |
| No timecode on one side; malformed LTC hex; sidecar XML malformed | That method yields no estimate for the pair; sidecar problems are silent (absent sidecar is normal). No warning — absence is not disagreement. |
| Drop-frame `;` at a non-30/60 nominal rate; frames field ≥ rate; unparseable timecode string | `timecode_to_seconds` raises `ValueError`; `timecode_estimate` catches it per pair and yields no estimate (one camera's odd stamp must not kill the run). |
| Midnight wrap | Timecode differences wrap via `wrap_day_delta` (§4.2) in both directions; clock differences do not wrap (they carry dates). Exactly +12 h maps to −12 h by the modulo formula; accepted (a 12-hour-old stamp is a wrap in practice). |
| Naive vs aware capture stamps; `-0400`-style zones; `Z` | All parse to aware datetimes (§5.1); naive is assumed UTC. |
| numpy not installed | Waveform yields nothing; per-clip warning; timecode/clock/Whisper still place clips. Tests for §6 skip via `skipUnless`. |
| No audio, decode failure, silent/zero-variance audio, overlap below the §6.3 floor | No waveform estimate for that pair. `has_audio == False` on either side also blocks the §8.3 halving (no sound is not counter-evidence). |
| Ambiguous waveform peak (r high, margin < 0.08) | Estimate exists with `clear=False`; it can still be guided (§8.2) or backed (§8.4), and alone it does not place the clip at step 1. |
| Drift measured but < 3 windows / < 40 s span | `drift_ppm: null`, global offset stands, no drift warning. Drift ≥ half a frame over the clip warns (§8.5) but the recorded offset stays the intercept. |
| `--transcripts` file missing for a clip; transcript JSON malformed; `mlx_whisper` absent/failing | No Whisper estimate for pairs with that clip; never an error. With `--transcripts` given there is no transcribe fallback (§7.1). |
| A phrase repeated in either transcript; only one moment matches | Repeated phrases abstain; one moment is refused (§7.2 steps 1, 5). |
| Guided window containing no valid lag; guided r still < 0.45 with Whisper guide | Guided result is `None` (keep global) or, when taken and weak, triggers §8.3 halving. The unguided peak stays in `detail["unguided"]` whenever a guided estimate is written. |
| Clip starts before the reference | Negative `offset_s` is correct (the timeline origin is the reference's first sample, not the earliest clip); `angles.covers` already handles it. |
| Non-reference anchor already placed, reference attempt weak | Later anchors are tried for a trusted waveform; the first (reference) pick is the fallback (§9 step 5). A clip placed against `B` records `against: "B"` on those estimates. |
| More than 26 clips | Labels continue `AA`, `AB`, …; §13 does not test this, the helper is one line. |
| `--methods` excluding waveform/whisper | The pick order simply has fewer steps; a timecode-only run places by timecode with its jam warning. |

## 13. Tests (`tests/test_sync.py`, unittest, offline: no media, no network, no Meta API, no Whisper binary)

Style follows [test_angles.py](../../tests/test_angles.py) and [test_grade.py](../../tests/test_grade.py): small synthetic builders (`_camera(...) -> CameraInfo`, `_clip(label, offset, ...) -> ClipResult`, `_estimate(method, offset, **detail) -> Estimate`), `tempfile` for JSON, `skipUnless(np is not None)` on waveform classes. One line per test — what it builds, what it asserts:

1. `test_ltc_decode_plain` — hex for 13:37:55:21 as 4 BCD bytes (frames first) → `decode_ltc_hex` returns `"13:37:55:21"`.
2. `test_ltc_decode_masks_flag_bits` — the same bytes with drop-frame/field flag bits set in the masked positions → identical string; non-hex, wrong length, and a units nibble of 0x0A each return `None`.
3. `test_sony_sidecar_parse` — a temp `<stem>M01.XML` with `LtcChangeTable tcFps="60"`, a `frameCount="0"` and a `frameCount="1"` `LtcChange`, `CreationDate`, `CaptureGammaEquation` → sidecar carries the frameCount-0 timecode, `tc_fps` 60.0, the creation string and `s-log3-cine`; a missing sidecar yields an empty `SonySidecar`.
4. `test_timecode_seconds_24_and_25` — `00:01:00:12` at fps 24 → 60.5 and at 25 → 60.48; `00:00:00:00` → 0.0.
5. `test_timecode_seconds_2997_non_drop_and_drop` — at 29.97: `00:01:00:00` non-drop → 60.06; `00:01:00;02` drop → 60.06; `00:10:00;00` drop → ≈600 within 0.001.
6. `test_timecode_seconds_5994` — at 59.94: `00:01:00;04` drop → 60.06; `00:00:01:30` non-drop → 1.5015.
7. `test_timecode_rejects_bad_input` — `;` at 25 fps, frames == rate, and a malformed string each raise `ValueError`.
8. `test_midnight_wrap` — timecode estimates for a target stamped 23:59:58 against a reference stamped 00:00:05 (same rates) → offset ≈ −7 s worth of wrap (i.e. `wrap_day_delta` maps +86393 → −7), and the reverse direction maps to +7.
9. `test_clock_prefers_quicktime_creationdate` — a probe dict with both `com.apple.quicktime.creationdate` (earlier) and `creation_time` (later copy time) → `camera_from_probe.captured_at` is the QuickTime value; `parse_captured_at` accepts `-0400`, `Z` and a space separator, and returns `None` for garbage.
10. `test_camera_from_probe_fields` — a Sony-shaped probe dict + sidecar → make/model/lens, snapped fps 59.94, `tc_rate` 60.0, timecode from the metadata stream's tags, gamma from the sidecar overriding the stream transfer; an audio-only probe dict → `fps/width/height` 0 and `has_audio` true.
11. `test_waveform_known_offset_with_gain_and_noise` *(numpy)* — synthetic speech-like bursts (random burst envelopes at seeded positions, 60 s at 8 kHz) with the target the same bursts delayed 6.25 s, at 0.3× gain plus independent noise → `waveform_estimate` recovers the offset within 0.005 s, `clear` true, confidence > 0.8.
12. `test_waveform_sign_and_short_overlap_refused` *(numpy)* — the target delayed −3 s (starts before the anchor) recovers a negative offset; a pair overlapping by less than the §6.3 floor (e.g. 4 s of a 60 s pair) yields `None`.
13. `test_waveform_drift_from_resampled_copy` *(numpy)* — the target is the anchor's burst train resampled by 1.00015 (150 ppm fast) over ≥120 s → recovered `drift_ppm` within ±40 ppm of the truth and the intercept offset within 0.02 s of the true start offset; a drift-free pair reports `drift_ppm` near 0 or `None` per §6.4's window rules.
14. `test_short_clip_false_peak_corrected_by_whisper_hint` *(numpy)* — a short target whose envelope repeats a loud section so the global peak lands on the wrong lag with `clear=False`, plus a Whisper estimate at the true offset disagreeing with it → the guided re-search (window = Whisper precision + 0.5 s) is taken by `resolve_guided`, the final pick is the guided offset, and `detail["unguided"]` still holds the false peak.
15. `test_resolve_guided_keeps_strong_global` — estimate objects only: a clear global (r 0.9, margin 0.4) against a guided peak at r 0.5 → global kept; the same global at r 0.6 (weak by margin) against guided r 0.59 → guided taken (within 0.02).
16. `test_whisper_votes_and_moments` — two hand-built word lists sharing two separated sentences (≥2 moments) with jitter ≤0.1 s → `whisper_estimate` returns the median vote within 0.05 s, `detail["moments"] == 2`, confidence ≈ 0.667; a phrase occurring twice in one list casts no vote.
17. `test_whisper_one_sentence_coincidence_refused` — word lists sharing exactly one sentence (one moment), however many overlapping phrases → `None`.
18. `test_whisper_halved_when_sound_disagrees` — a Whisper estimate plus a guided waveform estimate with r 0.2, both clips with audio → `penalise_whisper` halves the confidence, sets `confidence_halved`, and `choose_pick` then refuses Whisper-alone placement; with `both_have_audio=False` nothing is halved.
19. `test_agreement_thresholds` — `build_checks`/`estimates_agree`: waveform vs timecode 0.01 s apart agree (≤2 ms + 1 frame at 30 fps), 0.05 s apart disagree; clock vs waveform 1.9 s apart agree, 2.1 s apart disagree (frame 1/30).
20. `test_selection_order_and_warnings` — estimate objects only, one case per §8.4 step: clear waveform beats agreeing Whisper; unclear waveform backed by Whisper picks the waveform; unbacked unclear waveform falls to Whisper; Whisper below threshold falls to timecode with the jam warning; timecode absent falls to clock with the check-by-eye warning; a disagreeing timecode/Whisper against the pick produces the §8.5 disagreement warnings; nothing usable → `(None, None)`.
21. `test_placing_against_non_reference_clip` — three clips where the target has no usable estimate against `A` but a clear waveform against the already-placed `B` (offset 5.0) → `run` with `probe_clip` and `decode_samples` stubbed via `unittest.mock.patch` (in the `FakeMuseTests` stubbing spirit) places it at `5.0 + relative` with `against == "B"` on the winning estimate, and a trusted waveform against `A` stops before `B` is ever tried (assert the stub's call list).
22. `test_sync_json_shape_round_trips_through_angles` — build `ClipResult`s (reference, one placed, one unplaced), write via the §10 payload builder to a temp `sync.json` → `director.angles.load_sync` returns the reference path and clips with the right labels/offsets (`None` preserved), and `director.grade.load_sync_for_grade` resolves the reference label and gammas.
23. `test_parse_methods` — `None` → all four in canonical order; `"whisper,timecode,whisper"` → `["timecode", "whisper"]`; `"bogus"` and `""` raise `ValueError`.
24. `test_cli_parser_and_print` — `build_parser` parses `sync a.mp4 b.mp4 --out s.json` with defaults (`methods/transcripts/reference` None, out `sync.json`); `command_sync` with `sync.run` stubbed prints one row per clip with its checks/warnings and the `Wrote` line.

Gate: `python -m unittest discover -s tests -p 'test_*.py'` green (skips for missing numpy/ffmpeg are normal per AGENTS.md). For the ffmpeg-facing parts (decode command, probe), AGENTS.md requires running the built command on a short real clip and looking at the output before claiming done; the owner runs that live check.

## 14. What the build also updates (not this design)

At build time, per the brief's Limits: a SYNC section in `docs/muse-code/BUILD-LOG.md` (who did what, model calls, token use — this design is one Muse Wonder design pass, the build is Muse Spark, review is Claude Code with the owner, matching the existing sections' shape), one `sync.py` bullet in `AGENTS.md` under Layout, and the README provenance row if the README is present in the tree at build time. No commit is made by the design or build step; the owner reviews and commits.

This is a plan, not a special mode; I haven't started implementation. Reply `go` to execute this plan, or tell me what to change.


