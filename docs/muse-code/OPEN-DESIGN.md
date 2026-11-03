# Design: one-step multicam cut (`director cut`)

Chosen and designed by Muse Code on `muse-wonder-1.0` from
[OPEN-BRIEF.md](OPEN-BRIEF.md), under [AGENTS.md](../../AGENTS.md). This document
is the complete design; the build (Muse Spark) writes the code from it without
further decisions. No code is written by this document.

Conventions that bind the build: Python ≥3.11, `from __future__ import
annotations`, type annotations throughout, `@dataclass(slots=True)` at module
boundaries, `guestlist` imported only inside the functions that call Meta models
and guarded there, API key from `MODEL_API_KEY` only (handled inside
`guestlist`), no contributor model ids, tests offline with `unittest` and never
touching the Meta API or Whisper, no media files committed, no client names,
people's names or local footage paths in code, tests or docs.

## Why this

The editor in the brief shoots talking video on two or three cameras (Sony
S-Log3, a second Sony, an iPhone in HLG, sometimes a separate lav), dumps the
cards into one folder, and wants a finished cut — horizontal and vertical —
that they can publish or finish in Final Cut Pro, DaVinci Resolve or Premiere.

Today every stage of that exists and is tested, and the editor still cannot
get there without becoming an operator of this repository:

1. There is no one-step path. The editor must run `sync`, `grade`, `angles`,
   `reframe` in order and know which JSON file feeds which command
   ([cli.py](../../src/director/cli.py) exposes exactly four subcommands).
2. The middle step is secretly blocked. `director angles --words` requires a
   word-timed Whisper JSON, and no command produces one. `sync` can obtain
   word timings internally (its Whisper method, via a local `mlx_whisper`
   binary or a `--transcripts` directory) and caches them beside `sync.json`,
   but that cache is invisible: nothing tells the editor it exists, which clip
   it belongs to, or that its times are clip-local while `angles` reads word
   times as shared-timeline times (§1.4). On the common setup where the audio
   clip is not the reference clip, a hand-made cache file is not merely hard
   to find, it is in the wrong timebase.
3. The only multicam output is a rendered MP4. An editor who wants to refine
   Director's choices in the tool they already use gets nothing, even though
   the installed `ghostcut` package already ships tested FCPXML and OTIO
   exporters ([§1.5](#15-the-installed-ghostcut-exporters)) that no Director
   command calls.
4. Nobody checks the finished cut as a whole. Each stage validates its own
   step; nothing verifies that the rendered master is as long as the shots
   say, that black (uncovered) picture is a small share of it, or that a
   vertical render really is 1080×1920 — and no model ever watches the cut
   the way an editor watches a first export.

The feature is therefore **one command, `director cut`, that takes files
and/or a card-dump folder and produces the whole job in one output
directory**: `sync.json`, the word timings made explicit and time-shifted
into the shared timebase, `grade.json` plus per-camera `.cube` files,
`angles.json` and the graded horizontal master `cut.mp4`, editable
`timeline.fcpxml` and `timeline.otio.json` built from the same shots, the
vertical `vertical.mp4` with `reframe.json`, a whole-cut check (`qc`,
mechanical first, then one Muse Spark pass that watches a proxy of the
finished cut), and a single manifest `cut.json` that records every stage,
its spend, its warnings and whether it was skipped on a re-run.

This is one feature, not three. The unit an editor buys is "folder in,
publishable and editable cut out"; discovery, the words bridge, resume and
the spend guard are what make that unit trustworthy, the timeline export is
what makes its output finishable, and the whole-cut check is what lets the
editor trust a first run on real footage — including the owner's 3-minute
hackathon demo talk, shot on two or three cameras and cut with exactly this
command. Flub/restart removal ("tighten") is deliberately not in this
feature (§15): it is an editorial-judgement feature with its own eval, and
bolting it onto the first one-step path would put unproven cuts inside the
one command the demo depends on.

## 1. Inputs as they actually are

Everything in this section was verified against the repository and the
installed packages. The build must not re-derive it differently.

### 1.1 The four stage entry points (unchanged, called as-is)

```python
# src/director/sync.py
run(*, clip_paths: list[Path], reference_path: Path | None,
    methods: list[str], transcripts_dir: Path | None, out_path: Path) -> dict
# methods comes from sync.parse_methods(text: str | None) -> list[str];
# canonical order is sync.METHODS == ("timecode", "clock", "waveform", "whisper")

# src/director/grade.py
run(*, sync_path: Path, out_path: Path, reference_label: str | None = None,
    lut_specs: list[str] | tuple[str, ...] = (), no_match: bool = False,
    check: bool = False, model: str = "muse-spark-1.3") -> dict
# writes grade.json and one "<label>.cube" beside out_path per converted camera

# src/director/angles.py
run(*, sync_path: Path, words_path: Path, out_path: Path,
    audio_label: str | None, render_path: Path | None, min_shot_s: float,
    model: str, no_muse: bool, grade_path: Path | None = None) -> dict
# writes angles.json; when render_path is set, renders shot parts under
# "<render stem>.parts/" beside it and joins them under the uncut audio

# src/director/reframe.py
run(*, video_path: Path, out_path: Path, render_path: Path | None,
    shots_path: Path | None, no_sam: bool, max_usd: float) -> dict
# writes reframe.json; work files under "<out parent>/<video stem>.reframe/"
```

`cli.main` already turns `FileNotFoundError`, `ValueError`, `RuntimeError`
and `ghostcut.ffmpeg_tools.ToolingError` into `Error: …` on stderr with exit
code 1. `cut` raises only those types (its own `CutError(RuntimeError)`
included), so no CLI error-handling change is needed beyond the new
subcommand.

Stage payload facts `cut` relies on (and nothing more):

- `sync.json`: `{"reference": <path string as given>, "methods": [...],
  "clips": [{"label", "offset_s" (float|null), "method",
  "camera": {"path", "make", "model", "lens", "fps", "width", "height",
  "duration_s", "has_audio", "gamma", "timecode", "tc_rate",
  "captured_at"}, "estimates", "checks", "drift_ppm", "warnings"}]}`.
  The reference clip is label `A` at `offset_s 0.0`; an unplaced clip has
  `offset_s: null`. `sync.run` also maintains a transcript cache at
  `<sync.json parent>/sync-transcripts/<clip stem>.json`.
- `grade.json`: `{"version": 1, "sync", "reference", "reference_label",
  "model", "no_match", "spend_usd", "warnings", "check",
  "cameras": [{"label", "path", "gamma",
  "conversion": {"kind", "source_lut"}, "lut": "<label>.cube"|null,
  "source_colour": {...}, "match": {...}, "warnings"}]}`.
- `angles.json`: `{"version": 1, "sync", "words", "reference", "grade",
  "audio": {"label", "clip_path", "offset_s"}, "method", "model",
  "min_shot_s", "spend_usd", "warnings",
  "lines": [{"id", "start_s", "end_s", "text", "cameras"}],
  "shots": [{"id", "start_s", "end_s", "camera" (a label, or "black"),
  "clip_path", "clip_in_s", "clip_out_s", "line_ids", "shot_size",
  "reason", "method", "overrules", "lut" (cube file name or null)}]}`.
  Shot times are shared-timeline seconds, rounded to 3 decimals. A `black`
  shot is a span no camera covers; it renders as black picture under the
  uncut audio and must become a gap, never a clip, in an exported timeline.
- `reframe.json`: `{"version": 1, "video", "width", "height", "fps",
  "duration_s", "crop": {"width", "height"}, "method", "proxy_fps",
  "estimate_usd", "spend_usd", "frames_billed", "max_usd",
  "saturated_spans", "warnings",
  "shots": [{"id", "start_s", "end_s", "method", "object_id", "presence",
  "reason", "samples"}], "samples": [...], "render": {"path", "width",
  "height", "frames", "crop_moves"} | null}`.
  The render, when requested, is always 1080×1920.

### 1.2 Word timings: the two shapes and the one loader

`angles.load_timed_words(path)` accepts Whisper JSON in either shape —
`{"words": [{"word"|"text", "start", "end"}, …]}` or
`{"segments": [{"words": [...]}, …]}` — drops entries without text or a
numeric `start`, fills a missing `end` from the next word's start (last
word: `start + 0.30`), sorts by `start`, and clamps `end >= start`. It
returns `list[angles.TimedWord(text, start_s, end_s)]` with the raw
(punctuated) text kept, which line splitting needs for sentence breaks.

`sync.load_transcript_words(path)` parses the same two shapes into
normalised words for offset voting; `cut` does not use it for the words
bridge (§6 validates with `angles.load_timed_words`, the loader whose
semantics the next stage actually applies).

### 1.3 The installed `guestlist` calls (verified against the package)

`cut` adds exactly one new Meta call (§9.3). It uses this installed shape,
the same one `angles.pick_with_muse` and `grade.check_with_muse` use for
images:

```python
from guestlist.meta import cache, spark   # inside the calling function only
spark.video_json(video: str | Path, *, schema: dict, name: str,
                 developer: str, prompt: str,
                 model: str = "muse-spark-1.3", effort: str = "medium",
                 max_output_tokens: int = 16000,
                 max_usd: float | None = None) -> SparkResult
# SparkResult.data: dict, .usd: float, .cached: bool, .seconds, .usage, .meta
# The video is uploaded via guestlist.meta.files.upload (purpose=user_data),
# the reply is strict-schema JSON, and the call is content-cached by
# (video hash, model, developer, prompt, schema, effort): a re-run of the
# same proxy with the same prompts costs $0 and reports cached=True.
# max_usd, when given, is enforced by cache.guard before the call.
# spark.check_model refuses any model id ending in "-contributor".
```

Existing calls `cut` inherits by running the stages (it never re-implements
them): `spark.image_json` for angle picks (`angles.DEVELOPER`,
batches of 4 lines, `cache.guard(0.25)` per batch) and for the optional
grade check (`grade.CHECK_DEVELOPER`); SAM 3.1 via
`guestlist.windows.segment_windowed` inside `reframe.track`
($0.20 per 1,000 billed frames, minimum 50 frames per window call;
`reframe.estimate_cost_usd(n_frames)` and `reframe.ensure_within_budget`
already implement the pre-call estimate and refusal).

Pricing facts used by §7's estimates (from `guestlist.meta.pricing`,
2026-10-26 list): `muse-spark-1.3` $1.25 / $0.15 cached / $4.25 per 1M
input/cached/output tokens; `muse-wonder-1.0` $4.00 / $0.20 / $20.00;
SAM video $0.20 per 1,000 frames. `guestlist.meta.cache.BUDGET_USD` is a
global hard stop ($115 in the installed package) enforced by
`cache.guard`; it is a backstop, not `cut`'s per-job `--max-usd` (§7.3),
and `cut` never reads or changes it.

### 1.4 The timebase trap the words bridge must fix

`angles.run` treats word times as **shared-timeline** seconds: it splits
them into lines and tests each line against
`clip.offset_s ≤ line.start_s` and `line.end_s ≤ clip.offset_s +
clip.duration_s`. Whisper timings for one clip are **clip-local** seconds.
The two coincide only when the audio clip sits at offset 0 (i.e. it is the
reference clip). `sync`'s own Whisper method is immune because it only ever
differences two clips' local times. A `director cut` that pointed `angles`
at a raw per-clip transcript for a lav placed at +178 s would produce lines
at 0–180 s that no camera "covers" at those timeline positions in the way
the editor said them — the cut would be wrong while looking successful.
§6 therefore shifts automatically obtained words by the audio clip's
`offset_s` into timeline seconds and writes the shifted file as the job's
canonical `words.json`. User-supplied `--words` is **not** shifted: the
existing `angles` contract already requires timeline times there, and
silently moving an editor's file would break it (§6.3).

### 1.5 The installed `ghostcut` exporters

```python
# ghostcut.models
@dataclass(slots=True)
class MediaClip:
    id: str; source_path: str; proxy_path: str; audio_path: str
    duration_s: float; fps: float; has_audio: bool
    width: int; height: int; layer: str = "a-roll"

@dataclass(slots=True)
class TimelineSegment:
    section: str; clip_id: str; src_in_s: float; src_out_s: float
    timeline_in_s: float

# ghostcut.export_fcpxml
export_fcpxml(clips: list[MediaClip], timeline_segments: list[TimelineSegment],
              output_path: Path, broll_overlays=None,
              project_name: str = "GhostCut Auto Edit") -> str  # the XML text
# ghostcut.export_otio
export_otio(clips, timeline_segments, output_path,
            broll_overlays=None, project_name=...) -> dict       # the payload
```

Behaviour `cut` depends on (read from the installed source):

- Both exporters consider only clips referenced by a segment (or overlay);
  the sequence rate/size come from the used clips (dominant/majority).
- FCPXML: each segment becomes an `asset-clip` with absolute `offset`
  (its `timeline_in_s`), `start` (`src_in_s`) and `duration`
  (`src_out_s - src_in_s`) on the snapped frame grid
  (`ghostcut.ffmpeg_tools.seconds_to_fcpxml_time`). A segment whose
  `clip_id` is unknown is silently skipped, so a gap is represented by
  simply omitting a segment: the next clip's absolute offset preserves
  the hole. A cross-dissolve transition element is inserted **only when
  `segment.section` changes** from the previous segment, and every
  segment gets a `marker` whose value is its section. §8 therefore uses
  one constant section for every shot: no dissolves, one harmless marker
  text per shot.
- OTIO: the V1 track walks segments in `timeline_in_s` order, inserts an
  explicit `Gap` whenever a segment starts after the cursor (half-frame
  tolerance), and raises `ValueError` on overlapping segments. Clip nodes
  carry `source_range` from `src_in_s`/`src_out_s` and an
  `ExternalReference` to the resolved `file://` URI of `source_path`.
- Neither exporter reads `proxy_path` or `audio_path`, supports an
  audio-only track, or applies a LUT. §8 states exactly what that means
  for a separate lav recorder and for graded media instead of pretending
  otherwise.

`ghostcut.config.SUPPORTED_EXTENSIONS` is `{".mov", ".mp4", ".m4v"}`;
`ghostcut.config.read_json` / `write_json` (sorted keys, indent 2, parent
directories created) are the JSON helpers every stage uses and `cut` uses
for `cut.json`. `ghostcut.ffmpeg_tools.probe_media` plus
`extract_media_stats` give duration/fps/size/has-audio for QC;
`run_command` runs every new ffmpeg invocation in §9.

### 1.6 What `cut` must not claim to feed

`app/` reads `app/ui/demo/project.json` (built by `app/make_demo.py` from a
single-camera Render `timeline.json` or from FCPXML work folders). That
schema (`sections`, `talk`, `broll`, `cards`, `library`, `transcript`)
has no representation of multicam shots or camera labels, and this repo
ships no such project. `cut` does not write an app project and the README
must not suggest the app opens a `cut` job. Wiring `sync`+`angles` output
into the app is a separate feature; `cut.json` (§10) is designed so that a
future adapter has every input it needs (stage files, shots, audio label,
spend) without re-running anything.

## 2. Files changed

| File | Change |
|---|---|
| `src/director/cut.py` | New. Discovery, the words bridge, stage orchestration, fingerprints/resume, spend accounting, whole-cut QC (mechanical + one Muse video pass), `cut.json` writer, `run()`. |
| `src/director/export.py` | New. `angles.json` + `sync.json` (+ optional `grade.json`) → `MediaClip`/`TimelineSegment` mapping → FCPXML + OTIO via the installed `ghostcut` exporters. Pure except for reading the three JSON files and the exporters' own file writes. |
| `src/director/cli.py` | New `command_cut(args)` and a `cut` subparser. Thin: resolve paths/flags, `require_ffmpeg_binaries()`, call `cut.run`, print the §11 summary, return the payload. No logic moves into `cli.py`. |
| `tests/test_cut.py` | New. §14 items 1–22. |
| `tests/test_export.py` | New. §14 items 23–30. |
| `README.md` | At build time: the `director cut` quickstart (§11.4) replaces the four-command chain as the headline path; the four stage commands stay documented as the manual route. One "Who built what" row (§15). |
| `AGENTS.md` | At build time: two Layout bullets (`cut.py`, `export.py`). |
| `docs/muse-code/BUILD-LOG.md` | At build time: one OPEN section (§15). |

`src/director/sync.py`, `grade.py`, `angles.py`, `reframe.py` and `app/`
are **not modified**. Every existing command, flag, JSON shape and test
keeps working; `cut` is a caller of the stage `run()` functions, not a
fork of them.

Imports:

- `cut.py`: stdlib (`hashlib`, `json`, `math`, `re`, `shutil`,
  `subprocess`, `dataclasses`, `pathlib`) plus
  `ghostcut.config` (`write_json`), `ghostcut.ffmpeg_tools`
  (`ToolingError`, `extract_media_stats`, `probe_media`, `run_command`),
  the sibling stage modules (`from . import angles as angles_mod`,
  `grade as grade_mod`, `reframe as reframe_mod`, `sync as sync_mod`),
  and `from .export import ExportError, export_timelines`. The stage
  modules are imported at module top (they import only stdlib, ghostcut
  and guarded numpy) so tests can stub `director.cut.sync_mod.run` etc.
  `guestlist` appears only inside `muse_qc` (§9.3); numpy and Pillow are
  never imported by `cut.py` (numpy arrives transitively through `sync`,
  which already guards it).
- `export.py`: stdlib (`json`, `pathlib`) plus `ghostcut.config`
  (`read_json`), `ghostcut.models` (`MediaClip`, `TimelineSegment`),
  `ghostcut.export_fcpxml.export_fcpxml`,
  `ghostcut.export_otio.export_otio`. No ffmpeg, no guestlist.

## 3. Constants

All in `cut.py` unless stated. Every value is decided here; the build
does not tune them.

| Constant | Value | Why |
|---|---|---|
| `MEDIA_EXTENSIONS` | `{".mov", ".mp4", ".m4v", ".mp3", ".wav", ".m4a"}` | Directory expansion only: the three `ghostcut` video extensions plus the audio files a lav/recorder dump actually uses. Explicitly named files are never filtered by extension (the probe decides). |
| `DEFAULT_OUT_DIR` | `"director-cut"` | `--out-dir` default, relative to the working directory. |
| `DEFAULT_MODEL` | `"muse-spark-1.3"` | Same default as `angles`/`grade`; passed through to both and to §9.3. Never a `-contributor` id (guestlist refuses those anyway). |
| `DEFAULT_MIN_SHOT_S` | `2.0` | Passed to `angles.run(min_shot_s=…)`; identical to the `angles` CLI default. |
| `DEFAULT_MAX_USD` | `5.0` | Per-job spend gate (§7.3). A 3-minute demo job measures in cents for angles plus ≈$0.04–0.07 of SAM; $5 refuses accidents, not real jobs. |
| `STAGES` | `("sync", "words", "grade", "angles", "export", "reframe", "qc")` | Canonical stage order everywhere: execution, manifest keys, `--from` validation, CLI printing. `grade` is absent from a `--no-grade` job's manifest entirely (not "skipped"); `export`/`reframe` likewise under `--no-export`/`--no-vertical`. `words` and `qc` are always present. |
| `WHISPER_TIMEOUT_S` | `3600` | Same timeout `sync` gives a local `mlx_whisper` run. |
| `DURATION_TOLERANCE_S` | `0.10` | QC duration agreement (§9.2 checks 2 and 7). Renders are frame-snapped (`angles.frame_grid`), so a larger gap is a real defect, not rounding. |
| `BLACK_SHARE_WARN` | `0.20` | QC check 4: black picture above a fifth of the cut warns. |
| `LOUDNESS_WARN_LOW_LUFS` | `-23.0` | QC check 9 warn bounds on integrated loudness. Talking video mastered far below this is inaudible next to platform content. |
| `LOUDNESS_WARN_HIGH_LUFS` | `-12.0` | Above this a talking master is almost certainly clipping-adjacent. Warn only; `cut` never re-masters. |
| `TRUE_PEAK_WARN_DBTP` | `-0.5` | QC check 9: true peak above −0.5 dBTP warns. |
| `QC_PROXY_FPS` | `5` | The §9.3 proxy rate. Mirrors `reframe.PROXY_FPS`: enough to judge angle, grade and framing continuity at a fraction of the token cost of full rate. |
| `QC_PROXY_SHORT_SIDE` | `540` | The §9.3 proxy short side. Mirrors `reframe.PROXY_SHORT_SIDE`. |
| `QC_INPUT_TOKENS_PER_S` | `300` | Conservative input-token rate for the QC estimate: above the ≈272 tok/s measured for Spark video in `docs/DIRECTOR-PLAN.md`, so the estimate errs high. |
| `QC_MAX_OUTPUT_TOKENS` | `4000` | `max_output_tokens` for the QC call and the output term of its estimate. The schema's finding cap (§9.3) keeps real replies far below this. |
| `TOKEN_RATES_USD_PER_1M` | `{"muse-spark-1.3": (1.25, 4.25), "muse-wonder-1.0": (4.00, 20.00)}` | (input, output) list rates mirrored from `guestlist.meta.pricing.TOKEN_RATES` so the pre-call estimate needs no guestlist import. An unlisted `--model` falls back to the spark rates and adds the warning `qc_estimate_default_rates:<model>`. |
| `SECTION` (in `export.py`) | `"Director cut"` | The one `TimelineSegment.section` for every exported shot (§1.5: a section change would insert cross-dissolves into the FCPXML). |

## 4. Data classes

All `@dataclass(slots=True)`. Times are seconds (float), money is USD
(float), paths are stored as strings in JSON and as `Path` in memory only
where noted.

In `cut.py`:

```python
class CutError(RuntimeError):
    """A cut stage failed in a way the operator must see.
    The message always starts with the stage name, e.g. 'words: …'."""

@dataclass(slots=True)
class JobOptions:
    clips: list[Path]              # discovered, deduplicated, in job order (§5)
    out_dir: Path
    reference_path: Path | None    # as given; passed to sync.run
    audio_path: Path | None        # as given; resolved to a label after sync (§6.1)
    words_path: Path | None        # explicit --words (timeline times, §6.3)
    transcripts_dir: Path | None   # --transcripts; also passed to sync.run
    methods: list[str]             # sync.parse_methods output, canonical order
    no_grade: bool
    grade_check: bool              # passes check=True to grade.run
    no_muse: bool                  # passes no_muse=True to angles.run;
                                   # also disables the grade check and §9.3
    model: str
    min_shot_s: float
    no_vertical: bool
    no_sam: bool                   # passes no_sam=True to reframe.run
    no_export: bool
    no_qc: bool                    # skips §9.2 and §9.3 entirely
    max_usd: float
    force: bool                    # ignore every fingerprint (§7.2)
    project_name: str              # timeline project name (§8)

@dataclass(slots=True)
class StageRecord:
    name: str                      # one of STAGES
    status: str                    # "done" | "skipped" | "failed"
    fingerprint: str               # §7.1 hex digest, "" when not computed
    outputs: list[str]             # file names relative to out_dir
    spend_usd: float               # this stage's Meta spend (0.0 if none)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None       # str(exception) when status == "failed"

@dataclass(slots=True)
class QcCheck:
    id: str                        # §9.2 ids, or "muse:<kind>" for §9.3
    source: str                    # "mechanical" | "muse"
    status: str                    # "pass" | "warn" | "fail"
    detail: dict = field(default_factory=dict)  # measured values, verbatim
```

In `export.py`:

```python
class ExportError(RuntimeError):
    """The angles/sync/grade inputs cannot be mapped to a timeline."""
```

No other dataclasses. Stage payloads stay the dicts the stage `run()`
functions already return; `cut` reloads them from disk with
`ghostcut.config.read_json` when a stage is skipped (§7.2), so a resumed
job and a fresh job converge on identical in-memory state.

## 5. Discovery: from files and folders to the clip list

`discover_clips(inputs: list[Path], out_dir: Path) -> list[Path]` is pure
path logic (no probing; `sync.run` probes and its errors stand). Rules,
in order:

1. Each input, in the order given:
   - a **file** → appended as given (any extension; §3);
   - a **directory** → expanded non-recursively to its entries whose
     suffix (lower-cased) is in `MEDIA_EXTENSIONS` and whose name does
     not start with `.`, sorted by `(name.lower(), name)`;
   - anything else (missing path) → left in the list so `sync.run`
     raises its existing `FileNotFoundError(f"clip not found: {path}")`
     rather than `cut` inventing a second message. A missing path that
     is the *only* problem must read exactly like `director sync`'s.
2. Any resolved path under the resolved `out_dir` is dropped. On a
   re-run the editor may point `cut` at the same folder that contains
   `director-cut/`; the job must never ingest its own `cut.mp4`,
   `vertical.mp4`, proxies or parts.
3. Duplicates by resolved path are dropped, first occurrence wins. (The
   same file named directly and again via its folder is one clip;
   `sync.run` would otherwise refuse with "the same clip was given
   twice", which is the wrong lecture for a folder dump.)
4. Fewer than 2 clips after expansion →
   `CutError("cut: need at least two clips (files, or a folder of media)")`.
   This mirrors `sync`'s `ValueError("need at least two clips")` with the
   `cut:` stage prefix; it is raised before any stage runs and before
   `out_dir` is created.

The resulting order is the job order: it is the order `sync.run` receives
(and therefore the order from which sync derives labels `B`, `C`, … after
the reference). Directory expansion is deliberately non-recursive: card
dumps are flat, and recursing would swallow sidecar folders, exports and
the job's own output tree in ways the editor cannot predict from the
command they typed.

## 6. The words bridge

Stage `words` runs immediately after `sync` and before `grade` (it needs
only the sync payload; placing it early fails the job fast, before any
paid stage, when no word timings can be obtained).

### 6.1 Which clip is the audio clip — `resolve_audio`

```python
def resolve_audio(sync_payload: dict,
                  audio_path: Path | None) -> tuple[str, dict]:
    """(label, sync clip dict) of the clip angles will lay under the cut."""
```

Resolution mirrors `angles.run`'s own audio choice so the words `cut`
prepares are for the clip `angles` will actually use:

1. `audio_path` given → the clip whose `camera.path` resolved path equals
   `audio_path` resolved. No match →
   `CutError(f"words: --audio is not one of the synced clips: {audio_path}")`.
2. Else the clip whose `camera.path` equals `sync_payload["reference"]`
   (string equality, as `angles` does it).
3. Else the clip labelled `A`.
4. Else (a malformed payload with neither) →
   `CutError("words: sync.json has no clips")`. `angles` would fall back
   to the first clip; `cut` refuses instead, because preparing words for
   a guessed clip wastes a transcription and misleads the manifest. This
   case cannot arise from a `sync.run` payload, which always has ≥2
   clips and a reference.

The chosen clip must be placed: `offset_s is None` →
`CutError(f"words: audio clip {label} is not placed on the timeline; "
f"cannot align its words — check the sync warnings in cut.json")`.
(The reference clip always satisfies this; the check guards an explicit
`--audio` pointing at an unplaced lav.)

### 6.2 Where words come from — `resolve_words`

```python
def resolve_words(*, audio_label: str, audio_clip: dict,
                  options: JobOptions) -> tuple[Path, str, list[str]]:
    """(canonical words path, source, warnings).
    source ∈ {"explicit", "transcripts", "sync_cache", "mlx_whisper"}."""
```

Sources are tried in this order; the first that yields **at least one
word** wins:

1. **`explicit`** — `options.words_path` is set. Validated with
   `angles_mod.load_timed_words`; zero words →
   `CutError(f"words: no words in --words file: {path}")` (no fallback:
   the editor named a file deliberately, and silently transcribing a
   different source would hide their mistake). The file is used in
   place, unshifted (§6.3), and is **not** copied into `out_dir`.
2. **`transcripts`** — `options.transcripts_dir` is set and
   `<transcripts_dir>/<audio clip stem>.json` exists. (Stem is
   `Path(camera.path).stem`, the same convention
   `sync.get_transcript_words` uses.) Clip-local; shifted (§6.3).
3. **`sync_cache`** — `<out_dir>/sync-transcripts/<stem>.json` exists
   and is non-empty. This is the cache the `sync` stage just maintained
   (its Whisper method transcribes through the same directory), so on
   the common path the words already exist and this stage costs nothing.
   Clip-local; shifted.
4. **`mlx_whisper`** — `shutil.which("mlx_whisper")` finds the binary:
   run §6.4's command into `<out_dir>/transcripts/`, then load
   `<out_dir>/transcripts/<stem>.json`. Clip-local; shifted.
5. None of the above →
   `CutError("words: no word-timed transcript for clip {label} "
   f"({file name}) and mlx_whisper is not on PATH; install mlx-whisper, "
   "or pass --words WORDS.json, or --transcripts DIR")`.

A candidate file in sources 2–3 that exists but parses to zero words is
**not** an error and not a win: resolution falls through to the next
source, with the warning `words_empty_candidate:<file name>` recorded.
This distinction matters because `sync` treats a missing/empty
transcript as "Whisper method unavailable", never as failure; `cut`
inherits that tolerance everywhere except the explicit `--words` case
the editor vouched for.

Muse Voice Transcribe (`guestlist.meta.transcribe.diarize`) is
deliberately **not** a source. Verified in §1: it returns speaker turns
(`{startMs, endMs, transcript, speaker}`) with no word timings, and
`angles` needs word starts *and* ends to split lines and to place cut
points inside inter-word gaps (§1.2). Sending the audio to Meta would
also be the only network transcription in a pipeline whose speech-to-text
is otherwise local; the design does not buy a worse clock at the price of
uploading footage audio. If a future guestlist endpoint returns word
timings, adding it is a fifth source in this list behind an explicit
flag, not a change to any other section.

### 6.3 The shift — `shift_words`, `words_file_payload`

```python
def shift_words(words: list[angles_mod.TimedWord],
                offset_s: float) -> list[angles_mod.TimedWord]:
    """Clip-local → timeline: start and end both move by offset_s. Pure."""

def words_file_payload(words: list[angles_mod.TimedWord]) -> dict:
    """{"words": [{"word": text, "start": …, "end": …}, …]} at 3 decimals."""
```

For sources `transcripts`, `sync_cache` and `mlx_whisper`: load with
`angles_mod.load_timed_words`, shift by the audio clip's `offset_s`,
write with `ghostcut.config.write_json` to `<out_dir>/words.json`, and
pass that file to `angles.run(words_path=…)`. An `offset_s` of exactly
0.0 still writes the canonical copy (the manifest then names one words
file for every non-explicit job, and the file the editor inspects is the
file `angles` read). For `explicit`, no file is written and
`angles.run` receives the editor's path unchanged.

The payload key is `"word"` (Whisper's own spelling, which
`load_timed_words` accepts via `word` before `text`), times rounded to
3 decimals (`round(x, 3)`, the rounding every stage JSON uses). Word
text is preserved verbatim, punctuation included (§1.2).

### 6.4 The local transcription command — `mlx_whisper_command`

```python
def mlx_whisper_command(clip_path: Path, out_dir: Path) -> list[str]:
    return ["mlx_whisper", str(clip_path), "--output-format", "json",
            "--output-dir", str(out_dir), "--word-timestamps", "True",
            "--verbose", "False"]
```

This is byte-identical in shape to the invocation `sync` uses
(`sync.get_transcript_words`), run with
`subprocess.run(capture_output=True, timeout=WHISPER_TIMEOUT_S)`. A
non-zero exit, a timeout, or a missing/empty output file falls through
to the §6.2 step-5 error, with the warning
`words_mlx_whisper_failed:<file name>` recorded first — transcription
failure is reported as "no words available", never as a traceback,
matching `sync`'s never-raise contract for this method. Nothing is
uploaded: `mlx_whisper` is a local binary, and `cut` states so in the
manifest (`"uploaded": false` is not needed; the source name
`mlx_whisper` is the record).

## 7. Orchestration: stages, resume, spend

### 7.1 Fingerprints — `file_fingerprint`, `fingerprint_of`, `stage_fingerprint`

```python
def file_fingerprint(path: Path) -> dict:
    """{"path": str(path), "size": int, "mtime_ns": int}; missing file →
    {"path": str(path), "size": -1, "mtime_ns": -1}. Never raises."""

def fingerprint_of(payload: object) -> str:
    """sha256 hex of json.dumps(payload, sort_keys=True, default=str)."""

def stage_fingerprint(stage: str, options: dict,
                      files: list[Path]) -> str:
    """fingerprint_of({"stage": stage, "options": options,
    "files": [file_fingerprint(p) for p in files]})."""
```

Media files are fingerprinted by path/size/mtime, never by content hash:
hashing 4K camera originals on every re-run would make resume slower
than the work it saves. Size+mtime is the same freshness signal every
stage cache in this repo already trusts (`angles.grab_frames` reuses a
frame by existence; guestlist content-hashes only the small proxies and
still images it uploads). The options dict per stage is exactly:

| Stage | `options` keys (values from `JobOptions`) | `files` |
|---|---|---|
| `sync` | `methods`, `reference` (string or null) | the discovered clips, in job order |
| `words` | `audio` (label), `source` is **not** an option — it is an outcome | the audio clip file; plus the `--words` file when explicit, or the `--transcripts` candidate file when that directory is set |
| `grade` | `reference_label` (null; `cut` never sets it), `check`, `model`, `no_match` (always false) | `sync.json` |
| `angles` | `audio` (label), `min_shot_s`, `model`, `no_muse`, `graded` (bool) | the canonical words file (or the explicit file), `sync.json`, and `grade.json` when graded |
| `export` | `project_name` | `angles.json`, `sync.json`, and `grade.json` when present |
| `reframe` | `no_sam` | `cut.mp4`, `angles.json` |
| `qc` | `model`, `no_muse`, `no_qc` is excluded (a `--no-qc` job has no qc stage record beyond §10's note) | `cut.mp4`; plus `vertical.mp4` when the reframe stage is enabled |

### 7.2 The run — `run`

```python
def run(*, clips: list[Path], out_dir: Path,
        reference_path: Path | None, audio_path: Path | None,
        words_path: Path | None, transcripts_dir: Path | None,
        methods: list[str], no_grade: bool, grade_check: bool,
        no_muse: bool, model: str, min_shot_s: float,
        no_vertical: bool, no_sam: bool, no_export: bool,
        no_qc: bool, max_usd: float, force: bool,
        project_name: str) -> dict:
    """One job: discover → sync → words → grade → angles → export →
    reframe → qc. Returns the cut.json payload (also written)."""
```

Steps in order; nothing reorders them. `cli.command_cut` builds the
arguments (§11); tests call `run` directly with stubbed stage modules.

1. **Validate money and shot floor.** `max_usd < 0` →
   `ValueError("--max-usd must be >= 0")` (the `reframe` message, reused
   verbatim). `min_shot_s < 0` → `ValueError("--min-shot must be >= 0")`.
2. **Discover** (§5). Build `JobOptions`. Do not create `out_dir` yet
   (§5 step 4 must be able to fail without side effects).
3. **Load the previous manifest**, if `<out_dir>/cut.json` exists and
   parses as a dict with a `stages` dict; otherwise treat the job as
   fresh. A corrupt previous manifest is ignored with the warning
   `previous_manifest_unreadable` carried into the new manifest's
   top-level `warnings` — a hand-edited or truncated `cut.json` must
   never block a re-run.
4. **Stage loop** over the enabled stages in `STAGES` order. A stage is
   enabled per §3. For each stage:
   a. Compute its fingerprint (§7.1) from the *current* inputs.
   b. **Skip test.** The stage is skipped (`status "skipped"`, spend
      $0, outputs and warnings copied from the previous record) when
      *all* hold: not `options.force`; no earlier stage in this run has
      executed (the dirty flag below); the previous record for this
      stage has `status` `"done"` or `"skipped"`; its fingerprint
      equals the current one; and every file in its recorded `outputs`
      still exists under `out_dir`. A skipped stage's payload is
      reloaded from its JSON output (`sync.json`, `grade.json`,
      `angles.json`, `reframe.json`; `words` reloads its word count by
      re-validating the canonical file; `export` and `qc` reload their
      manifest sections — see §10). Rendered MP4s are never reloaded,
      only checked for existence.
   c. **Dirty propagation.** Once any stage executes, every later stage
      executes regardless of fingerprint. Downstream inputs may have
      been rewritten identically, but `cut` does not spend its
      correctness budget proving byte-equality of stage outputs.
   d. **Execute** (§7.4), recording spend and warnings. Write the
      manifest after every stage (§7.5), so a job interrupted by a
      stage failure, a budget refusal or Ctrl-C still leaves a truthful
      `cut.json` naming the last completed stage.
   e. **Failure.** Any exception from a stage: mark the record
      `failed` with `error = str(exc)`, write the manifest with
      top-level `status "failed"` and `failed_stage`, then re-raise as
      `CutError(f"{stage}: {exc}")` unless the exception already is a
      `CutError` with that stage's prefix. `cli.main` prints it and
      exits 1. Files from completed stages stay exactly where they
      are; nothing is rolled back. (Stage `run()` functions write their
      JSON only on success, so a failed stage never leaves a new
      authoritative JSON; a stale JSON from a previous run may remain
      and is identified as stale because its stage record says
      `failed`.)
5. **QC gate.** After the `qc` stage (§9), if any mechanical check has
   status `fail`: write the manifest with `status "failed"` and
   `failed_stage "qc"`, then raise
   `CutError(f"qc: finished cut failed mechanical checks: "
   f"{', '.join(failed ids)} (files kept in {out_dir})")`. Muse
   findings and `warn` checks never fail the job (§9.4).
6. **Status and return.** Otherwise top-level `status` is
   `"complete_with_warnings"` when any stage warning, any QC `warn`,
   or any Muse finding exists, else `"complete"`. Write the final
   manifest and return it.

There is no `--from`/`--only` flag in this design. Resume-by-fingerprint
plus `--force` covers the re-run cases (fix one input, re-run; change
`--min-shot`, re-run) without a second, divergent notion of "which
stages ran". A builder tempted to add one should re-read the brief's
"prefer one complete, working, well-tested thing".

### 7.3 Spend accounting and the `--max-usd` gate

`cut` tracks one number, `spent_usd`: the sum of stage spends as they
complete (`grade` payload `spend_usd`, `angles` payload `spend_usd`,
`reframe` payload `spend_usd`, QC §9.3 actual `usd`; `sync`, `words`
and `export` always $0 — local binaries and pure mapping). Cached
guestlist replies report their stored `usd` through the stage payloads
exactly as the stages report them today; `cut` does not second-guess
that accounting, and the manifest's per-stage `spend_usd` always sums
to the top-level `spend_usd` (±$0.001 of stage-level rounding, which the
tests assert with `assertAlmostEqual`).

The gate is checked **before** each stage that can spend, using the
best estimate available at that point:

- Before `angles` (and before `grade` when `grade_check` is on):
  no honest pre-estimate exists — the number of lines, and therefore
  batches of 4, is unknown until the words are split, and guestlist
  already enforces its own per-call `cache.guard(0.25)` and the global
  hard stop. `cut` therefore checks the *trailing* rule: if
  `spent_usd > max_usd` the job stops before the stage with
  `CutError(f"{stage}: estimated spend ${spent:.2f} already exceeds "
  f"--max-usd ${max_usd:.2f}; raise --max-usd or pass --no-muse")`.
  In practice this fires only when an earlier stage overshot.
- Before `reframe`: `estimate = reframe_mod.estimate_cost_usd(
  reframe_mod.proxy_frame_count(horizontal_duration_s))`, where
  `horizontal_duration_s` is probed from `cut.mp4`
  (`probe_media` + `extract_media_stats`). If
  `spent_usd + estimate > max_usd` →
  `CutError(f"reframe: estimated SAM cost ${estimate:.2f} would push "
  f"spend ${spent:.2f} over --max-usd ${max_usd:.2f}; raise --max-usd "
  "or pass --no-vertical")`. This is the same refusal shape
  `reframe.ensure_within_budget` already uses; `cut` passes
  `max_usd = max(0.0, max_usd - spent_usd)` into `reframe.run` so the
  stage's own guard agrees. With `--no-sam` the estimate is $0 and only
  the trailing rule applies.
- Before the §9.3 Muse QC call: `estimate = estimate_qc_usd(
  horizontal_duration_s, model)` (§9.3). Refusal over the remaining
  budget does **not** fail the job: QC degrades to mechanical-only
  with the warning
  `qc_muse_skipped_over_budget:est $<est> over remaining $<rem>`.
  A whole-cut opinion is valuable, but no editor wants a *check* to be
  the reason a finished cut reports failure.

### 7.4 What each stage execution does

- **sync**: `sync_mod.run(clip_paths=options.clips,
  reference_path=options.reference_path, methods=options.methods,
  transcripts_dir=options.transcripts_dir,
  out_path=out_dir / "sync.json")`. Outputs `["sync.json"]`.
  Warnings are the union of every clip's `warnings`, prefixed by label:
  `f"{label}: {warning}"` (the per-clip arrays otherwise lose their
  subject in one flat manifest list). Spend $0.
- **words**: §6. Outputs `["words.json"]` for shifted sources, `[]` for
  `explicit` (the canonical file is the editor's own path, recorded in
  the manifest's `words` section instead). Spend $0. The record's
  warnings carry the §6.2 fall-through warnings. After resolution,
  re-validate the file `angles` will receive with
  `angles_mod.load_timed_words` and record the word count; zero words
  here is impossible by construction (§6.2 step 1 and the ≥1-word win
  rule) and is asserted, not handled.
- **grade** (unless `--no-grade`):
  `grade_mod.run(sync_path=out_dir/"sync.json",
  out_path=out_dir/"grade.json", reference_label=None, lut_specs=(),
  no_match=False, check=options.grade_check and not options.no_muse,
  model=options.model)`. Outputs `["grade.json"]` plus every
  `"<label>.cube"` the payload lists (`cameras[].lut` non-null).
  Spend is the payload's `spend_usd` (non-zero only with the check).
  When `--no-muse` is set the grade check is forced off — `--no-muse`
  means "no Meta calls in this job" everywhere, and the CLI help says
  so (§11). The LUT cubes land beside `grade.json` in `out_dir`
  because that is where `grade.run` writes them; `cut` does not move
  them, and `angles.load_grade` resolves them relative to `grade.json`
  exactly as the manual pipeline does.
- **angles**: `angles_mod.run(sync_path=…, words_path=<§6 result>,
  out_path=out_dir/"angles.json", audio_label=<§6.1 label>,
  render_path=out_dir/"cut.mp4", min_shot_s=options.min_shot_s,
  model=options.model, no_muse=options.no_muse,
  grade_path=out_dir/"grade.json" if graded else None)`.
  The horizontal master is always rendered: a `cut` job whose output
  is only JSON has failed its brief. Outputs
  `["angles.json", "cut.mp4"]`. Spend is the payload's `spend_usd`.
  Zero shots: `angles.run` raises `ValueError("nothing to render")`
  when `shots == []` and a render path is set, before it writes
  `angles.json`. `cut` catches the stage exception and, when
  `str(exc) == "nothing to render"` exactly, re-raises
  `CutError("angles: no shots: no camera covers any spoken line; "
  "check that the words belong to the audio clip and the sync offsets "
  "in cut.json")` as the stage failure (§7.2 step 4e). No export,
  reframe or QC stage runs. The exact-string mapping is tested in
  §14 item 12.
- **export** (unless `--no-export`): §8.
  `export_timelines(angles_path=…, sync_path=…,
  grade_path=… if graded else None,
  fcpxml_path=out_dir/"timeline.fcpxml",
  otio_path=out_dir/"timeline.otio.json",
  project_name=options.project_name)`. Outputs
  `["timeline.fcpxml", "timeline.otio.json"]`. Spend $0.
- **reframe** (unless `--no-vertical`):
  `reframe_mod.run(video_path=out_dir/"cut.mp4",
  out_path=out_dir/"reframe.json",
  render_path=out_dir/"vertical.mp4",
  shots_path=out_dir/"angles.json", no_sam=options.no_sam,
  max_usd=<§7.3 remaining>)`. Outputs
  `["reframe.json", "vertical.mp4"]`. Spend is the payload's
  `spend_usd`. `reframe` already falls back to centred crops, with
  warnings, when guestlist/SAM is unavailable; `cut` passes those
  warnings through untouched — a vertical master with centred crops is
  a usable deliverable, not a failed job.
- **qc** (unless `--no-qc`): §9. Outputs `[]` (its results live in the
  manifest's `qc` section and the `qc/` work directory). Spend is the
  §9.3 actual spend, $0 for mechanical-only.

### 7.5 The manifest is written incrementally

`_write_manifest(out_dir, payload)` is called after validation, after
every stage (done, skipped or failed), and at completion. It writes
with `ghostcut.config.write_json` (atomicity is the helper's existing
behaviour: parent creation plus a single `write_text`; `cut` does not
add rename machinery the rest of the repo does not have). The payload
under construction is held in memory as one dict; `StageRecord`s are
serialised into it as
`{"status", "fingerprint", "outputs", "spend_usd", "warnings", "error"}`
per stage. The full shape is §10.

## 8. Timeline export (`src/director/export.py`)

The export is a pure mapping from the shots `angles` already decided to
the `ghostcut` exporters' data classes. It makes no editorial decision:
every cut in the timeline is an `angles` shot, at the same time, from the
same camera, for the same reason (carried as far as each format allows).

### 8.1 Functions (every function in `export.py`)

| Signature | Job |
|---|---|
| `load_angles(path: Path) -> dict` | `read_json`; require a dict with `version == 1` and a list `shots`, else `ExportError(f"Not an angles.json: {path}")`. |
| `sync_clips_by_label(path: Path) -> dict[str, dict]` | `read_json`; require a dict with a list `clips`, else `ExportError(f"Not a sync.json: {path}")`. Map label → clip dict; a duplicate label → `ExportError(f"Duplicate label {label} in {path}")` (the message `angles.load_sync` uses, kept). |
| `build_media_clips(sync_payload: dict, used_labels: list[str]) -> list[MediaClip]` | One `MediaClip` per used label, in `used_labels` order (§8.2). |
| `build_segments(angles_payload: dict) -> tuple[list[TimelineSegment], list[dict], list[str]]` | (segments, gaps, warnings) per §8.3. Pure. |
| `export_timelines(*, angles_path: Path, sync_path: Path, grade_path: Path \| None, fcpxml_path: Path, otio_path: Path, project_name: str) -> dict` | Orchestrator: load, map, call both exporters, return the §10 `export` section dict. Raises `ExportError` (a `RuntimeError`, so `cli.main` and `cut`'s stage failure handling both treat it correctly). |

Internal helpers, still listed so the builder does not invent others:
`_grade_luts(grade_path) -> dict[str, str | None]` (label → cube file
name from `cameras[].lut`, `{}` when `grade_path is None`; a grade file
that fails `version == 1` validation raises
`ExportError(f"Not a grade.json: {path}")`).

### 8.2 `MediaClip` mapping

For each label that at least one non-black shot uses, in label order:

```python
MediaClip(
    id=label,                                # "A", "B", "C" — stable, and
                                             # the id an editor sees in
                                             # angles.json for the same camera
    source_path=str(clip["camera"]["path"]), # as recorded in sync.json;
                                             # the exporters resolve it
    proxy_path="",                           # unused by both exporters (§1.5)
    audio_path="",                           # unused by both exporters
    duration_s=float(clip["camera"]["duration_s"] or 0.0),
    fps=float(clip["camera"]["fps"] or 0.0),
    has_audio=bool(clip["camera"]["has_audio"]),
    width=int(clip["camera"]["width"] or 0),
    height=int(clip["camera"]["height"] or 0),
    layer="a-roll",                          # dialogue role in FCPXML
)
```

A used label missing from `sync.json` →
`ExportError(f"angles camera {label} is not in sync.json: {sync_path}")`.
A clip with `fps <= 0` (an audio-only lav that somehow carries a shot —
impossible through `angles`, which only assigns placed video-covering
cameras, but the mapping does not assume it) is still emitted; the
exporters' own fallbacks (sequence format, dominant rate) handle it,
and §8.3's audio warning covers the real lav case.

### 8.3 `TimelineSegment` mapping, gaps, and the audio limitation

Let `t0 = shots[0].start_s` over **all** shots including black ones, so
the exported timeline and the rendered master share an origin: exported
time `0` is the first line's timeline start, and a leading black span
becomes a leading gap in both formats.

For each shot in `angles` order:

- `camera == "black"` → no segment. Append to `gaps`:
  `{"start_s": round(shot.start_s - t0, 3), "end_s": round(shot.end_s - t0, 3),
  "line_ids": shot.line_ids}`.
- Otherwise →
  `TimelineSegment(section=SECTION, clip_id=shot.camera,
  src_in_s=shot.clip_in_s, src_out_s=shot.clip_out_s,
  timeline_in_s=round(shot.start_s - t0, 3))`. Times are the
  `angles.json` values as written (already rounded to 3 decimals);
  `export` does not re-snap them — `seconds_to_fcpxml_time` snaps to
  the frame grid on output, and OTIO's `RationalTime` rounds to whole
  frames at the dominant rate. Because `angles` shots are
  non-overlapping and time-ordered, OTIO's overlap `ValueError` cannot
  fire on a well-formed `angles.json`; if it does (a hand-edited file),
  `export_timelines` lets it propagate as the stage failure — it is a
  `ValueError`, already handled by `cli.main`.

Warnings returned (recorded in the manifest's `export.warnings` and in
the stage record):

1. **Separate audio.** Let `audio_label` be `angles_payload["audio"]["label"]`.
   When no segment uses it (the production audio is a lav or recorder
   file with no picture shots), append exactly
   `f"audio_clip_not_in_timeline:{audio_label}"`. The meaning, stated
   in the README and in §10's example: the exported timeline carries
   each camera's own audio on its clips (the FCPXML `dialogue` role);
   the rendered `cut.mp4`/`vertical.mp4` remain the masters with the
   production audio laid under them, and an editor who rebuilds the
   mix in the NLE re-lays the audio clip at the offset recorded in
   `angles.json`/`cut.json` (`audio.offset_s`). Inventing an audio
   track neither installed exporter supports would produce timelines
   that look complete and are not.
2. **Original media, LUTs beside the timeline.** When `grade_path` is
   given, append `"timelines_reference_original_media"` and, per used
   label with a non-null cube, `"lut:<label>:<cube file name>"`. The
   timelines reference the camera originals (that is what an editor
   finishing a grade wants), so they open showing log/HLG picture;
   the per-camera cubes `grade` already wrote sit in the same
   `out_dir` and are named here so the editor applies the right one
   per camera. `cut` does not copy cubes next to the timelines — they
   are already in the same directory (§7.4 grade).

`export_timelines` then calls, in this order:

```python
export_fcpxml(clips, segments, fcpxml_path, project_name=project_name)
export_otio(clips, segments, otio_path, project_name=project_name)
```

FCPXML first because Final Cut Pro is the brief editor's most likely
finishing tool and, if only one write can succeed (disk full), the
`.fcpxml` is the one the demo opens. The return dict is §10's `export`
section: `{"fcpxml": <file name>, "otio": <file name>,
"project_name": project_name, "shots_exported": len(segments),
"gaps": gaps, "clips_used": [labels in order],
"luts": {label: cube file name or null for every used label}
(only when graded, else {}), "warnings": [...]}`. File names, not
paths: every path in `cut.json` that points inside `out_dir` is a bare
file name (§10).

## 9. Whole-cut QC

Stage `qc` answers, in writing, "is the finished thing sound?" It runs
last, over `cut.mp4` (and `vertical.mp4` when present). Two layers, in
fixed order: mechanical checks (deterministic, can fail the job) then,
unless disabled, one Muse Spark pass that watches a proxy of the
finished cut (advisory, can never fail the job). The separation is the
repo's standing rule — mechanical checks and model judgements are shown
separately, and a model agreeing with itself is never presented as
proof — applied to a whole cut instead of one line or one grade.

### 9.1 New ffmpeg work (the only new ffmpeg in this feature)

All through `ghostcut.ffmpeg_tools.run_command` unless noted.

**QC proxy** — `qc_proxy_command(video: Path, proxy: Path) -> list[str]`,
the `reframe.proxy_command` shape applied to the horizontal master:

```python
["ffmpeg", "-v", "error", "-y", "-i", str(video), "-map", "0:v:0",
 "-vf", f"fps={QC_PROXY_FPS},scale=-2:{QC_PROXY_SHORT_SIDE}",
 "-an", "-map_metadata", "-1", "-c:v", "libx264", "-preset", "veryfast",
 "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
 str(proxy)]
```

Written to `<out_dir>/qc/proxy.mp4`. Skipped (mechanical-only QC, §9.4)
when `--no-muse`, `--no-qc`, the Muse budget gate refuses (§7.3), or
guestlist cannot be imported.

**Loudness probe** — `loudnorm_command(path: Path) -> list[str]`:

```python
["ffmpeg", "-v", "info", "-i", str(path), "-vn",
 "-af", "loudnorm=print_format=json", "-f", "null", "-"]
```

Run with `check=False`; `parse_loudnorm(stderr: str) ->
tuple[float | None, float | None]` extracts `(input_i, input_tp)`:
find the last occurrence of `"[Parsed_loudnorm"` in stderr, take the
first `{` after it, and `json.loads` from there to the last `}` of the
output; any failure gives `(None, None)`, which check 9 reports as
`warn` with `detail {"measured": null}` ("unmeasured"), never as a
crash. ffmpeg prints this block on every build `cut` runs on; the
parser is tested against a captured-shaped fixture (§14 item 20), not
against a live ffmpeg in the unit suite.

Probing for checks 1, 2 and 7 is `probe_media` + `extract_media_stats`
(no new ffprobe invocation is designed; `probe_media`'s fixed entry set
already carries duration, rate, size and stream kinds, and audio
presence for check 3 is `"has_audio"` in the same stats dict).

### 9.2 Mechanical checks — `mechanical_qc`

```python
def mechanical_qc(*, out_dir: Path, sync_payload: dict,
                  angles_payload: dict, reframe_payload: dict | None,
                  export_section: dict | None) -> list[QcCheck]:
```

Checks execute **in this order** and are recorded in this order. Times
and durations in `detail` are rounded to 3 decimals; ids are exactly:

1. `master_exists` — `cut.mp4` exists and probes. `pass`
   `detail {"file": "cut.mp4"}`; any probe failure or missing file →
   `fail` (and checks 2–4 record `warn` with
   `detail {"skipped": "master_exists"}` rather than cascading false
   measurements — one defect, one failure).
2. `duration_match` — `|probed_duration - (last shot end - first shot
   start)| ≤ DURATION_TOLERANCE_S` → `pass`, else `fail`.
   `detail {"rendered_s", "shots_s", "delta_s"}`.
3. `audio_present` — the master has an audio stream. `pass`, else
   `warn` (a silent talking cut is almost certainly wrong, but `cut`
   cannot prove the shoot had sound). `detail {"has_audio": bool}`.
4. `black_share` — summed black-shot duration ÷ shots span.
   `≤ BLACK_SHARE_WARN` → `pass`, else `warn`.
   `detail {"black_s", "span_s", "share"}` (share rounded to 4).
5. `long_shots` — count of shots carrying a `long_shot:` overrule
   (`angles` already reports >15 s shots there). Zero → `pass`, else
   `warn`. `detail {"count", "shot_ids": [...]}`.
6. `unplaced_cameras` — sync clips with `offset_s null`, labels listed.
   None → `pass`, else `warn`. `detail {"labels": [...]}`. (An unplaced
   camera is already a sync warning; QC re-states it because its
   consequence — lines that camera alone covered became black — is a
   property of the finished cut.)
7. `vertical_shape` — only when the reframe stage ran. Probed vertical
   is exactly 1080×1920 and its duration matches the master's within
   `DURATION_TOLERANCE_S` → `pass`, else `fail`.
   `detail {"width", "height", "rendered_s", "master_s", "delta_s"}`.
8. `timelines_parse` — only when the export stage ran. `timeline.fcpxml`
   parses with `xml.etree.ElementTree` and `timeline.otio.json` parses
   with `json.loads` and has top-level `"OTIO_SCHEMA": "Timeline.1"`.
   Both → `pass`, else `fail` (an unparseable timeline is worse than
   none: the editor will trust it). `detail {"fcpxml": bool, "otio": bool}`.
9. `loudness` — §9.1 probe. `(None, None)` → `warn` unmeasured.
   Integrated within `[LOUDNESS_WARN_LOW_LUFS, LOUDNESS_WARN_HIGH_LUFS]`
   and true peak `≤ TRUE_PEAK_WARN_DBTP` → `pass`, else `warn`.
   `detail {"integrated_lufs", "true_peak_dbtp"}`. Never `fail`:
   loudness is a publishing judgement with platform-dependent targets,
   and `cut` offers no re-master to fix it (§15).

### 9.3 The Muse whole-cut pass — `muse_qc`

```python
def estimate_qc_usd(duration_s: float, model: str) -> float:
    """duration_s * QC_INPUT_TOKENS_PER_S * input_rate / 1e6
    + QC_MAX_OUTPUT_TOKENS * output_rate / 1e6, rates per §3. Pure."""

def qc_schema() -> dict: ...
def qc_prompt(duration_s: float, shots: list[dict]) -> str: ...

def muse_qc(*, proxy: Path, duration_s: float, model: str,
            angles_payload: dict) -> tuple[list[QcCheck], float, list[str]]:
    """One spark.video_json call → (checks, spend_usd, warnings).
    Raises QcUnavailable (below) only when guestlist cannot be imported."""
```

```python
class QcUnavailable(RuntimeError):
    """guestlist (the Meta API client) cannot be imported."""
```

Developer prompt (verbatim, stored as `QC_DEVELOPER` in `cut.py`):

> You are Muse, the finishing checker inside GhostCut Director. You are
> watching a low-frame-rate proxy of a finished multicam talking-head
> cut, with its soundtrack. The cut was assembled by choosing, for each
> spoken line, one of several cameras that recorded the same moment, and
> grading every camera to match. Judge only what an editor would catch
> on a first watch of an export: a cut to a camera where the speaker is
> clearly not the person talking or is badly framed, a visible colour
> or exposure jump between cameras that the grade did not fix, picture
> that is black, frozen or the wrong shot for the words, a join where
> the words are audibly clipped or the voice jumps against the picture,
> and framing that loses the speaker. Do not judge the content of what
> is said, do not ask for different words, and do not invent problems
> to fill a quota: an empty findings list on a clean cut is the correct
> answer. Give times in seconds from the start of this proxy, which
> starts at the same moment as the finished cut. Never invent a person,
> a camera, or an event you cannot see or hear.

User prompt (`qc_prompt`): first line
`Check this finished cut ({duration_s:.1f}s). Reply with the overall
verdict and at most 12 findings.`, then a shot map the model can
reason against (it is watching at 5 fps and cannot read shot ids off
the picture), one line per shot:
`shot {id} {start_s:.1f}-{end_s:.1f}s camera {camera}` with black shots
rendered as `shot {id} {start_s:.1f}-{end_s:.1f}s BLACK (no camera
covered this span)`, then
`Cameras are the letters used in angles.json; times below are seconds
on the finished cut.` The shot map is context, not ground truth: the
prompt's last sentence is `If the picture disagrees with this map, trust
the picture and say so in the finding.` — because a map/picture
disagreement is itself the sync defect QC exists to catch.

Schema (`qc_schema()`, strict, `additionalProperties: False` throughout):

```json
{
  "type": "object", "additionalProperties": false,
  "required": ["overall", "summary", "findings"],
  "properties": {
    "overall": {"type": "string",
                "enum": ["publish", "review", "not_ready"]},
    "summary": {"type": "string"},
    "findings": {
      "type": "array", "maxItems": 12,
      "items": {
        "type": "object", "additionalProperties": false,
        "required": ["start_s", "end_s", "kind", "severity",
                     "description", "confidence"],
        "properties": {
          "start_s": {"type": "number"},
          "end_s": {"type": "number"},
          "kind": {"type": "string", "enum": [
            "angle_choice", "grade_mismatch", "av_sync_suspect",
            "black_or_frozen", "framing", "audio_issue", "other"]},
          "severity": {"type": "string",
                       "enum": ["low", "medium", "high"]},
          "description": {"type": "string"},
          "confidence": {"type": "number"}
        }
      }
    }
  }
}
```

Call (inside `muse_qc`, the only `guestlist` import in `cut.py`):

```python
from guestlist.meta import cache, spark   # raises QcUnavailable on ImportError
cache.guard(estimate)                     # guestlist's own backstop too
r = spark.video_json(proxy, schema=qc_schema(), name="cut_qc",
                     developer=QC_DEVELOPER,
                     prompt=qc_prompt(duration_s, shots),
                     model=model, effort="medium",
                     max_output_tokens=QC_MAX_OUTPUT_TOKENS,
                     max_usd=estimate)
```

`video_json` (not `image_json`) is the point: every existing Director
call shows Muse stills or short SAM windows; this is the one place a
model watches the *finished artefact* — motion, joins and soundtrack
together — which is the only vantage from which a wrong-angle cut or a
voice/picture jump is visible at all. It runs at most once per distinct
proxy (content-cached by guestlist), over a 5 fps 540 px proxy, never
over camera originals.

**Mechanical verification of Muse's reply** (in `muse_qc`, in this
order; Muse proposes, these decide what is recorded):

1. Clamp/drop times: a finding with `end_s <= start_s`, a non-numeric
   time, or a range entirely outside `[0, duration_s]` is dropped;
   a partially outside range is clamped. Drops are counted into the
   warning `qc_muse_dropped_findings:<n>` (only when n > 0).
2. Kind/severity/confidence outside the schema enums cannot arrive
   (strict schema), but a `confidence` outside `[0, 1]` is clamped —
   the schema says `number`, not a range, and `cut` does not trust
   the wire more than the schema enforces.
3. Each surviving finding becomes
   `QcCheck(id=f"muse:{kind}", source="muse", status="warn",
   detail={"start_s", "end_s", "severity", "description",
   "confidence", "corroborated": bool})` with times rounded to 3.
   `corroborated` is true exactly when `kind == "black_or_frozen"` and
   the finding's range overlaps a known black shot by at least half
   the finding's length — the one Muse claim a mechanical fact can
   confirm. Nothing else is ever upgraded: an uncorroborated finding
   is labelled model judgement in the manifest, which is what it is.
4. The reply's `overall` becomes one more check:
   `QcCheck(id="muse:overall", source="muse", status="warn" if
   overall != "publish" else "pass", detail={"overall", "summary"})`.
   Even `not_ready` is a `warn` (§9.4).

Any exception from the call itself (network, budget, `SparkNoAnswer`)
is caught inside `muse_qc`'s caller in `run`: QC degrades to
mechanical-only with the warning
`qc_muse_failed:<ExceptionClass>: <str(exc)[:160]>` (the shape
`grade.check_with_muse` already uses for dropped moments), spend $0.
Only the `ImportError` path is distinct, because it is the expected
no-guestlist case: warning `qc_muse_unavailable:<error>`.

### 9.4 What QC can and cannot do to the job

- Mechanical `fail` → job `failed` (§7.2 step 5). The files remain;
  the manifest names the failed checks; the CLI exits 1. An editor
  must be told, in the exit code and not only in a JSON file, that
  the master is not the length the shots promise.
- Mechanical `warn`, any Muse finding, `overall != "publish"` →
  job `complete_with_warnings`. The manifest and the CLI summary
  (§11) list them; nothing is re-rendered, re-cut or "fixed"
  automatically. QC in this feature *reports*; it does not edit.
  Auto-applying model findings to a timeline is the app's accepted-edit
  model, a different feature with a human in the loop (§15).
- `--no-qc` → the `qc` stage is recorded as
  `{"status": "skipped", "fingerprint": "", "outputs": [],
  "spend_usd": 0.0, "warnings": ["disabled by --no-qc"], "error": null}`
  with manifest `qc: null`. (This is the one stage whose record exists
  while disabled; §3's rule that disabled `grade`/`export`/`reframe`
  leave no record stands, because those have their own payload files
  whose absence already tells the story, while QC has no file.)

## 10. `cut.json`: the manifest contract

Written by `_write_manifest` (§7.5) with `write_json` (keys sorted on
disk; construction order below is documentation order). Every path
pointing inside `out_dir` is a bare file name; every path pointing at
editor media is the string exactly as recorded by `sync.json`. Times
rounded to 3 decimals, money to 4 (`round(x, 4)`) except per-stage
`spend_usd`, which keeps the stage payload's own rounding and is
rounded to 4 on serialisation.

```json
{
  "version": 1,
  "status": "complete_with_warnings",
  "failed_stage": null,
  "out_dir": "director-cut",
  "model": "muse-spark-1.3",
  "max_usd": 5.0,
  "spend_usd": 0.0831,
  "warnings": ["previous_manifest_unreadable"],
  "inputs": ["cam-a.mp4", "cam-b.mp4", "lav.wav"],
  "stages": {
    "sync":    {"status": "done", "fingerprint": "9f2c…",
                "outputs": ["sync.json"], "spend_usd": 0.0,
                "warnings": [], "error": null},
    "words":   {"status": "done", "fingerprint": "41ab…",
                "outputs": ["words.json"], "spend_usd": 0.0,
                "warnings": [], "error": null},
    "grade":   {"status": "done", "fingerprint": "…",
                "outputs": ["grade.json", "A.cube", "B.cube"],
                "spend_usd": 0.0, "warnings": [], "error": null},
    "angles":  {"status": "done", "fingerprint": "…",
                "outputs": ["angles.json", "cut.mp4"],
                "spend_usd": 0.0312, "warnings": [], "error": null},
    "export":  {"status": "done", "fingerprint": "…",
                "outputs": ["timeline.fcpxml", "timeline.otio.json"],
                "spend_usd": 0.0,
                "warnings": ["audio_clip_not_in_timeline:C",
                             "timelines_reference_original_media",
                             "lut:A:A.cube"],
                "error": null},
    "reframe": {"status": "done", "fingerprint": "…",
                "outputs": ["reframe.json", "vertical.mp4"],
                "spend_usd": 0.0419, "warnings": [], "error": null},
    "qc":      {"status": "done", "fingerprint": "…", "outputs": [],
                "spend_usd": 0.01, "warnings": [], "error": null}
  },
  "audio": {"label": "C", "clip_path": "lav.wav", "offset_s": 178.746,
            "words_source": "sync_cache", "words_file": "words.json",
            "words": 812, "words_shifted_s": 178.746},
  "export": {"fcpxml": "timeline.fcpxml", "otio": "timeline.otio.json",
             "project_name": "Director cut", "shots_exported": 21,
             "gaps": [{"start_s": 41.0, "end_s": 43.5,
                       "line_ids": [9]}],
             "clips_used": ["A", "B"],
             "luts": {"A": "A.cube", "B": "B.cube"},
             "warnings": ["audio_clip_not_in_timeline:C"]},
  "qc": {
    "status": "warn",
    "checks": [
      {"id": "duration_match", "source": "mechanical",
       "status": "pass",
       "detail": {"rendered_s": 179.977, "shots_s": 179.976,
                  "delta_s": 0.001}},
      {"id": "muse:grade_mismatch", "source": "muse",
       "status": "warn",
       "detail": {"start_s": 62.0, "end_s": 70.5,
                  "severity": "medium",
                  "description": "Camera B reads warmer than A here.",
                  "confidence": 0.8, "corroborated": false}},
      {"id": "muse:overall", "source": "muse", "status": "warn",
       "detail": {"overall": "review",
                  "summary": "Watchable; check the warm shot."}}
    ],
    "spend_usd": 0.01
  }
}
```

Field rules (decided; `version` is the schema version, and any future
incompatible change bumps it):

- `status`: `"running"` in every incremental write before completion,
  then §7.2 step 5–6 values (`"complete"`, `"complete_with_warnings"`,
  `"failed"`). A `cut.json` left at `"running"` means the job was
  interrupted — that is a deliberate, readable state, not a bug.
- `failed_stage`: null unless `status == "failed"`; then the stage name.
- `warnings` (top level): job-level warnings only
  (`previous_manifest_unreadable`, `qc_estimate_default_rates:<model>`,
  `qc_muse_skipped_over_budget:…`, `qc_muse_unavailable:…`,
  `qc_muse_failed:…`). Stage warnings live in their stage records.
- `inputs`: file names (`Path.name`) of the discovered clips in job
  order. Names, never full local paths, so a manifest can be pasted
  into an issue or a build log without leaking a footage directory.
- `stages`: keys exactly `STAGES` minus disabled stages (§3), each the
  `StageRecord` serialisation of §7.5. `fingerprint` is `""` only in
  the `--no-qc` placeholder record (§9.4).
- `audio.words_file`: `"words.json"` for shifted sources, the
  `--words` path as given for `explicit` (in that one case a path
  outside `out_dir` appears, as given — the editor named it), or null
  never: a completed `words` stage always has a file.
  `words_shifted_s` is the applied §6.3 shift (0.0 for explicit and for
  a zero-offset audio clip). `words` is the validated word count.
- `export`: null when `--no-export`; otherwise §8.3's return dict with
  file names as above.
- `qc`: null when `--no-qc`; otherwise `{"status", "checks",
  "spend_usd"}` where `status` is the worst check status
  (`fail` > `warn` > `pass`) and each check is the `QcCheck`
  serialisation `{"id", "source", "status", "detail"}`. Mechanical
  checks come first (§9.2 order), then Muse findings in reply order,
  then `muse:overall` last. A mechanical-only QC has no `muse:` checks
  and its stage warnings say why (§9.3–9.4).

Output tree of a complete job (names fixed; directories created by the
stage that owns them):

```
director-cut/
  cut.json  sync.json  words.json  grade.json  A.cube  B.cube
  angles.json  cut.mp4  cut.parts/  angle-frames/
  timeline.fcpxml  timeline.otio.json
  reframe.json  vertical.mp4  cut.reframe/
  sync-transcripts/  transcripts/  qc/proxy.mp4
```

## 11. Command line

### 11.1 Syntax

```
director cut INPUT [INPUT …] [--out-dir DIR] [--reference CLIP]
             [--audio CLIP] [--words WORDS.json] [--transcripts DIR]
             [--methods timecode,clock,waveform,whisper]
             [--no-grade] [--grade-check] [--no-muse]
             [--model muse-spark-1.3] [--min-shot 2.0]
             [--no-vertical] [--no-sam] [--no-export] [--no-qc]
             [--max-usd 5.00] [--force] [--name "Director cut"]
```

`cli.build_parser` gains a `cut` subparser (after `reframe`), and
`cli.command_cut(args) -> dict` follows the house shape: it calls
`require_ffmpeg_binaries()` unconditionally (every stage probes or
decodes; this is `command_sync`'s precedent, not `command_grade`'s
conditional), builds `cut.run` keyword arguments from `args`, prints
§11.3, returns the payload. `sync.parse_methods(args.methods)` converts
`--methods`; its `ValueError`s surface through `cli.main` unchanged.

### 11.2 Flags (every flag, its default, its effect)

| Flag | Default | Effect |
|---|---|---|
| `INPUT …` | required, `nargs="+"` | Media files and/or folders (§5). |
| `--out-dir` | `director-cut` | Job directory; created by the first stage that writes. |
| `--reference` | none | Reference clip path, passed to `sync.run` (default there: the longest clip). A path, not a label: labels do not exist yet when the editor types the command. |
| `--audio` | none | Production-audio clip **path**, resolved to a label after sync (§6.1). Deliberately not a label, for the same reason as `--reference` — this is the friendlier break from `angles --audio LABEL`, and the help text says "audio clip file". |
| `--words` | none | Word-timed Whisper JSON in **timeline** times (§6.3); wins over every other words source. |
| `--transcripts` | none | Directory of per-clip Whisper JSON (`<stem>.json`); passed to `sync.run` and used by §6.2 source 2. |
| `--methods` | none (all) | Passed to `sync.parse_methods`. |
| `--no-grade` | off | Skip the grade stage; `angles` renders ungraded (`grade_path=None`). The right escape when every camera already records Rec.709. |
| `--grade-check` | off | Ask Muse Spark to check the grade match (`grade.run(check=True)`). Off by default: §9.3 checks the finished cut as a whole, and double-paying for per-moment and whole-cut opinions on every job is spend without a decision attached. |
| `--no-muse` | off | No Meta calls in the job: `angles` rule picks, grade check off, SAM off is *not* implied (SAM is `reframe`'s tracker; use `--no-sam` or `--no-vertical`), QC Muse off. The help text reads exactly: `No Meta model calls for picks or checks (SAM still runs; see --no-sam)`. |
| `--model` | `muse-spark-1.3` | Passed to `angles`, to `grade` (check) and to §9.3. Validated only by guestlist's own contributor refusal; `cut` adds no model allow-list it would have to maintain. |
| `--min-shot` | `2.0` | Passed to `angles.run`. |
| `--no-vertical` | off | Skip the reframe stage: no `reframe.json`, no `vertical.mp4`, QC check 7 absent. |
| `--no-sam` | off | Pass `no_sam=True` to `reframe.run` (centred vertical, $0). |
| `--no-export` | off | Skip the timeline export. |
| `--no-qc` | off | Skip §9 entirely (`qc: null`, §9.4 placeholder record). |
| `--max-usd` | `5.0` | §7.3 gate. |
| `--force` | off | Ignore fingerprints; re-run every enabled stage. |
| `--name` | `"Director cut"` | Project name written into both timelines (`project_name`). |

### 11.3 Console output (exact shapes)

One line per stage as it completes or is skipped, then warnings, then
the result. Stage line: `f"{name}: {status}"` plus, when spend is
non-zero, `f" (${spend:.4f})"`, e.g.:

```
sync: done
words: done (812 words, sync_cache, shifted +178.746s)
grade: done
angles: done ($0.0312)
export: done (21 shots, 1 gap)
reframe: done ($0.0419)
qc: warn (9 checks, 1 muse finding)
! B: Placed by capture clock only: check by eye.
! export: audio_clip_not_in_timeline:C
Wrote director-cut/cut.json (complete_with_warnings, $0.0831)
cut: director-cut/cut.mp4
vertical: director-cut/vertical.mp4
timelines: director-cut/timeline.fcpxml, director-cut/timeline.otio.json
```

Rules: the `words` parenthetical is `(N words, <source>)` plus
`, shifted +X.XXXs` only when the shift is non-zero; the `export`
parenthetical appears only when the stage ran; the `qc` line reads
`qc: pass|warn|fail (N checks, M muse findings)` with the pluralisation
exactly as written for all counts (no singular forms — tested);
warnings print at column 0 as `! <stage>: <warning>`, **except** sync
warnings, which already carry their clip label (§7.4) and print as
`! <warning>` exactly as stored in the stage record. The final three `cut:` /
`vertical:` / `timelines:` lines print only for artefacts that exist
(a `--no-vertical` job prints no `vertical:` line). On failure,
`cli.main` prints `Error: <stage>: …` to stderr after whatever stage
lines already printed, and the exit code is 1 — the stage lines are
the progress log, not a success claim.

### 11.4 The README quickstart this enables (written at build time)

```bash
director cut /path/to/card-dump --out-dir job1
# or, explicitly:
director cut cam-a.mp4 cam-b.mp4 lav.wav --out-dir job1
```

followed by one paragraph naming the outputs (`cut.mp4` to publish,
`vertical.mp4` for 9:16, `timeline.fcpxml` for Final Cut Pro,
`timeline.otio.json` for OTIO/Resolve tools, `cut.json` for what ran,
what it cost and what QC found), the `--no-muse --no-sam` offline form,
and the note that re-running the same command resumes (§7.2). The
existing four-command sections remain underneath as "Running the stages
by hand". No client names, people's names or local footage paths
appear in the README text (the placeholder paths above are the shape
to use).

## 12. How Meta models are used, and why

| Call | Model | Where | Why this model, this input |
|---|---|---|---|
| Angle picks | `muse-spark-1.3` (or `--model`) via `spark.image_json`, one 512 px frame per covering camera per line, batches of 4 lines | inherited from `angles.pick_with_muse` | A still per camera at the line's midpoint is the cheapest input that shows framing, gaze and obstruction — the actual reasons to cut. Mechanical checks (§1 of the angles design) overrule; nothing about this changes. |
| Grade check (opt-in) | same, via `spark.image_json` | inherited from `grade.check_with_muse`, only under `--grade-check` | Per-moment colour outliers, advisory only. Off by default in `cut` (§11.2). |
| Vertical tracking | `sam-3.1` via `segment_windowed` over a 5 fps proxy | inherited from `reframe.track` | Person identity across a cut is a segmentation problem, not a language one; billed per frame, hence the proxy and the §7.3 estimate. |
| Whole-cut check | `muse-spark-1.3` (or `--model`) via `spark.video_json`, one call over the 5 fps 540 px proxy of the finished master | **new**, §9.3 | The only call that sees motion, joins and soundtrack together: wrong-angle cuts and voice/picture jumps do not exist in any single frame. Spark, not Wonder: this is bounded perception against a strict schema, the workload Spark's pricing and latency are built for; `--model muse-wonder-1.0` remains available for an editor who wants the deeper pass and accepts its rates (§3 estimate follows the model). |

Not used, with reasons already decided above: Voice Transcribe for
words (§6.2 — turn-level, no word clock), Muse Image (no generated
imagery belongs in an editing pipeline), any `-contributor` id
(guestlist refuses them; footage never licenses training).

Footage leaves the machine only through these calls, only when the
command that runs them was typed: stills and the SAM/QC proxies, never
camera originals (the QC proxy and SAM windows are derived, downscaled
files; the export and QC stages read originals locally and upload
nothing). Every call is guestlist-cached, so resuming a job re-sends
nothing that already has an answer. Per-job spend is estimated before
the two billed-volume calls (SAM, QC video) and refused or degraded
per §7.3; the global guestlist hard stop remains as backstop.

## 13. Failure and edge cases (decided)

| Case | Behaviour |
|---|---|
| Fewer than 2 discovered clips; missing input path | §5 step 4 `CutError` before `out_dir` exists; a missing path reaches `sync.run` and raises its `FileNotFoundError` verbatim. |
| A clip will not probe | `sync.run` raises (`SyncError`/`ToolingError`); stage `sync` marked failed, manifest written `status "failed"`, `Error: sync: …`, exit 1. No partial `sync.json` (stage writers are success-only). |
| Reference clip unplaceable concerns | The reference is at offset 0 by construction; other clips may be unplaced (`offset_s null`). The job continues: unplaced cameras cover nothing, their solely-covered lines become black shots, QC check 6 warns. Only an unplaced **audio** clip blocks (§6.1). |
| No words anywhere; `mlx_whisper` absent/failing | §6.2 step-5 `CutError` naming all three remedies. Stage `words` failed; `sync.json` and its warnings are already in `out_dir` for diagnosis. |
| Explicit `--words` empty or wrong timebase | Empty → §6.2 step-1 error. Wrong timebase cannot be detected mechanically; the manifest records `words_source "explicit"` and `words_shifted_s 0.0` so the assumption is visible, and black-share/duration QC is where a gross mismatch surfaces. |
| `angles` finds no shots | §7.4 exact-message mapping to the clearer `CutError`; no export/reframe/QC runs. |
| Grade: unknown gamma, PQ, probe failure, clamped match | All existing `grade` warnings pass through; the job continues (an unconverted camera renders as recorded, as the manual pipeline does). `--no-grade` skips the stage entirely. |
| guestlist missing entirely | Angles falls back to rule picks (existing behaviour, recorded in `angles.json` method), grade check is skipped by `angles`/`grade`'s own guards, reframe centres every shot with its `sam_unavailable` warning, QC runs mechanical-only with `qc_muse_unavailable:…`. The job completes. This is the fully-offline form together with `--no-muse` not even being required — but the README's offline example passes both flags so the *intent* is explicit. |
| `--max-usd 0` | Angles/grade-check trailing rule plus SAM $0 only under `--no-sam`; the QC Muse pass degrades with `qc_muse_skipped_over_budget:…`. A $0 job that still needs paid picks stops at §7.3's first gate with the raise-or-`--no-muse` message. |
| SAM saturated windows (16-id cap) | Existing `reframe` warnings pass through; vertical still renders. |
| Vertical already the input shape, tiny clips, one camera | `cut` requires ≥2 clips but they may be one camera plus a lav (the brief's common case): angles then has one covering camera, shots are single-camera, export has one clip, reframe centres or tracks normally. A one-*video*-clip job is valid; a one-clip-total job is §5's error. |
| `out_dir` equals a folder also passed as an input folder | §5 step 2 excludes the job's own outputs. Stage work directories (`cut.parts/`, `qc/`, `*.reframe/`) contain only derived media and are excluded by the same resolved-path rule on re-runs. |
| Re-run with one clip replaced | Its size/mtime fingerprint changes → `sync` re-runs → dirty propagation re-runs everything downstream. The manifest's stage fingerprints are the audit trail. |
| Re-run with only `--name` changed | `sync`…`angles` skip; `export` re-runs (its options include the name) and drags `reframe`/`qc` with it via dirty propagation. Accepted: correctness over thrift, and timelines are cheap. |
| Hand-edited `angles.json` between runs | `angles` stage skips only on fingerprint equality, so the edit is overwritten by a re-run only if an upstream stage also changed; `export` (whose fingerprint covers `angles.json`) picks the edit up. This is the supported way to adjust a shot before re-export: edit, re-run, only downstream stages move. |
| Ctrl-C / killed mid-render | The manifest's last incremental write says `status "running"` with the completed stages marked; the partial stage's record is absent or `running`-era stale. Re-running resumes by fingerprint. `angles` part files under `cut.parts/` are overwritten by its own render. |
| Disk full during export after FCPXML wrote | OTIO write raises; stage `export` failed; `timeline.fcpxml` exists and `cut.json` says the stage failed — never a silent half-success (§8.3 ordering makes the surviving file the more useful one). |
| QC proxy or loudness probe fails | Mechanical checks that need the master fail/warn per §9.2 check 1's rule; a loudness parse failure is `warn` unmeasured; a proxy failure degrades QC to mechanical-only with `qc_muse_failed:ToolingError: …`. QC tooling problems never masquerade as editorial findings. |
| Muse QC replies with out-of-range or malformed findings | §9.3 verification drops/clamps and counts. A reply that is not a dict at all is treated as a failed call (`qc_muse_failed:…`) with zero findings recorded — never recorded as "Muse found nothing". |
| Long jobs (30+ minutes) | No stage in `cut` imposes a duration cap. SAM and QC costs scale with duration and are exactly what §7.3 estimates refuse over `--max-usd`; angles batches scale linearly and are guestlist-cached per batch. The design sets no `--max-duration`: the owner's demo is 3 minutes, the brief editor's talks are tens of minutes, and an arbitrary cap would refuse real work while protecting nothing the budget gate does not. |

## 14. Tests

`tests/test_cut.py` (items 1–22) and `tests/test_export.py` (items
23–30); numbering is continuous across both files. Offline throughout:
no network, no Meta API, no Whisper binary. Style follows
`tests/test_angles.py` — small synthetic payload builders
(`_sync_payload(...)`, `_angles_payload(...)`, `_word(...)`),
`tempfile.TemporaryDirectory` for files, and the `FakeMuseTests`
pattern (a stand-in `guestlist.meta` in `sys.modules`) for item 19.
Stage modules are stubbed by patching `director.cut.sync_mod.run` etc.
(`unittest.mock.patch.object`) with fakes that write the stage's JSON
output and return its payload, so orchestration tests run no ffmpeg.
One line per item: what it builds, what it asserts.

1. `test_discovery_files_and_folder` — a temp folder with `b.mp4`,
   `A.MOV`, `lav.wav`, `.hidden.mp4`, `notes.txt`, plus one explicit
   file given twice (once directly, once via the folder) →
   `discover_clips` returns the media files deduplicated, folder
   entries sorted case-insensitively, hidden and non-media excluded,
   explicit file first.
2. `test_discovery_excludes_out_dir_and_needs_two` — inputs include the
   out directory itself (containing a `cut.mp4`); it contributes
   nothing. A single resulting clip raises `CutError` with the §5
   message and `out_dir` was not created.
3. `test_fingerprints` — a missing file fingerprints to size/mtime
   −1 without raising; identical payloads give identical
   `fingerprint_of` output regardless of dict key order; changing one
   option value or one file's mtime changes `stage_fingerprint`.
4. `test_resolve_audio` — a hand-built sync payload: explicit
   `--audio` path resolves to its label; no `--audio` resolves to the
   reference-path clip, then to label `A`; an unknown `--audio` path
   and an unplaced audio clip each raise the §6.1 `CutError`s.
5. `test_shift_words_round_trip` — words at 1.0–2.0 s shifted by
   +178.746 land at 179.746–180.746; `words_file_payload` written to a
   temp file re-loads through `angles.load_timed_words` with text and
   punctuation preserved and 3-decimal times.
6. `test_resolve_words_explicit` — an explicit `--words` file wins over
   a populated transcripts dir and sync cache, is returned unshifted
   and in place, with source `"explicit"`; an empty explicit file
   raises the §6.2 step-1 error.
7. `test_resolve_words_auto_sources_shift` — with a transcripts dir
   and a sync cache both present, `transcripts` wins; with only the
   cache, `sync_cache` wins and `<out_dir>/words.json` is written with
   shifted times; an existing-but-empty candidate falls through with
   `words_empty_candidate:<name>` in the warnings.
8. `test_resolve_words_mlx_whisper` — `mlx_whisper_command` equals §6.4
   exactly (`--word-timestamps True` included); with `shutil.which`
   and `subprocess.run` stubbed to write a words file, source is
   `"mlx_whisper"` and times are shifted; a failing stub run plus no
   other source raises the §6.2 step-5 error naming all three remedies.
9. `test_resume_skips_then_dirty_reruns` — all stage modules stubbed;
   a first `run` executes every stage (assert call counts), a second
   identical `run` skips every stage (zero stage calls, stage spends
   $0 in the new manifest, `status` still computed from reloaded
   payloads). Touching one input clip (new mtime) makes the third run
   re-execute `sync` and, by dirty propagation, every later stage.
10. `test_force_reruns` — after a complete stubbed run, `force=True`
    re-executes every enabled stage despite matching fingerprints.
11. `test_spend_gates` — with stubbed stages reporting spend near
    `--max-usd`, the reframe stage refuses with the §7.3 message
    before `reframe_mod.run` is called (assert not called); with only
    the QC estimate over the remaining budget, the job completes,
    QC is mechanical-only, and `qc_muse_skipped_over_budget:…` is a
    top-level warning.
12. `test_no_shots_mapping` — a stubbed `angles_mod.run` raising
    `ValueError("nothing to render")` surfaces as the §7.4 `CutError`
    ("no camera covers any spoken line"); `export`/`reframe` stubs
    were never called; the manifest says `failed`/`angles`.
13. `test_failure_writes_manifest_and_keeps_files` — a stubbed
    `grade_mod.run` raising `RuntimeError("boom")` → `CutError`
    starting `grade: `, manifest on disk has `status "failed"`,
    `failed_stage "grade"`, `sync`/`words` records `done`, and the
    written `sync.json` still exists untouched.
14. `test_corrupt_previous_manifest_ignored` — a `cut.json` containing
    garbage is ignored, the run proceeds fresh, and
    `previous_manifest_unreadable` appears in top-level warnings.
15. `test_mechanical_qc_checks` — hand-built payloads and a probed
    stand-in: checks arrive in §9.2 order with exact ids; a duration
    delta over tolerance is the one `fail`; black share above 0.20
    warns with the share in `detail`; a missing master yields exactly
    one `fail` (`master_exists`) and `warn`/`skipped` for checks 2–4.
16. `test_mechanical_qc_vertical_and_timelines` — a valid temp
    vertical/timelines pair passes checks 7–8 (probe stubbed);
    a 1920×1080 "vertical" fails check 7; an unparseable `.fcpxml`
    fails check 8 with `detail {"fcpxml": false, "otio": true}`.
17. `test_estimate_qc_usd` — 180 s costs `180*300*1.25/1e6 +
    4000*4.25/1e6` under spark rates and the wonder equivalent under
    wonder rates (both `assertAlmostEqual`); an unlisted model uses
    spark rates.
18. `test_qc_schema_and_prompt` — `qc_schema()` requires exactly
    `overall`/`summary`/`findings`, finding kinds equal §9.3's enum,
    `maxItems 12`; `qc_prompt` renders a black shot as the `BLACK`
    line and ends with the trust-the-picture sentence;
    `QC_DEVELOPER` contains "empty findings list" (the no-quota rule).
19. `test_muse_qc_with_fake_guestlist` — a fake `spark.video_json`
    asserts it received the proxy path, `name="cut_qc"`, the §9.3
    schema and `max_output_tokens == QC_MAX_OUTPUT_TOKENS`, and
    returns two findings (one out-of-range → dropped and counted,
    one `black_or_frozen` overlapping a black shot → `corroborated`
    true) plus `overall "review"` → checks end with
    `muse:overall` at `warn`, spend is the fake's `usd`. A fake that
    raises produces zero muse checks, mechanical checks intact, and
    exactly the `qc_muse_failed:…` warning.
20. `test_loudnorm_and_proxy_commands` — `loudnorm_command` and
    `qc_proxy_command` equal §9.1 as lists (element-for-element);
    `parse_loudnorm` reads `input_i`/`input_tp` from a captured-shaped
    stderr fixture (a `[Parsed_loudnorm_0 …]` JSON block after ffmpeg
    progress lines) and returns `(None, None)` on garbage.
21. `test_cli_parser_and_print` — `build_parser` parses
    `cut a.mp4 b.mp4` with every §11.2 default (`out_dir`,
    `model`, `min_shot`, `max_usd`, `name` included);
    `command_cut` with `cut.run` stubbed to a two-stage-warning
    payload prints the §11.3 stage lines (`qc: warn (9 checks, 1 muse
    findings)` — pluralisation uninflected), the `!`-warnings, and no
    `vertical:` line when the payload has no vertical artefact.
22. `test_cut_end_to_end_synthetic` *(ffmpeg + numpy)* —
    `@unittest.skipUnless(shutil.which("ffmpeg") and numpy importable)`.
    Two 6 s synthetic clips are made at run time in a temp dir
    (`testsrc2` picture plus a `sine` tone muxed as audio, identical
    content so waveform sync places both at offset 0), an explicit
    timeline `--words` file with words spanning 0.5–5.5 s is supplied,
    and `cut.run` is called with `no_muse=True`, `no_sam=True` and QC
    Muse disabled (`no_muse`). Asserts: `status` is `complete` or
    `complete_with_warnings`; `cut.mp4` probes to ≈6 s; `vertical.mp4`
    is 1080×1920; both timelines parse (check 8's own parsers);
    `cut.json` stages are all `done`; per-stage spends sum to the
    top-level spend. No Meta call, no Whisper, no network.
23. `test_build_media_clips` — a three-clip sync payload where shots
    use A and C yields two `MediaClip`s in label order with the §8.2
    field values (`id` is the label, `layer "a-roll"`, fps/duration
    from the camera dict); a shot naming a label absent from sync
    raises `ExportError`.
24. `test_build_segments_and_gaps` — shots `[A 10–16, black 16–18,
    B 18–24]` yield segments at timeline 0.0 and 8.0 with `t0 == 10.0`
    taken over all shots, one gap `{16-10, 18-10}`, and every
    segment's `section == SECTION`.
25. `test_export_audio_and_lut_warnings` — when `angles.audio.label`
    carries no shot, warnings contain exactly
    `audio_clip_not_in_timeline:<label>`; when it does, they do not.
    With a grade payload, warnings contain
    `timelines_reference_original_media` and `lut:A:A.cube`, and the
    returned `luts` map covers every used label; without one, `luts`
    is `{}` and neither warning appears. A bad grade `version` raises
    `ExportError`.
26. `test_export_timelines_writes_both_formats` — temp
    angles/sync fixtures through the real exporters:
    `timeline.fcpxml` parses as XML, contains asset ids `A`/`B`, no
    `transition` elements (one constant section), and the second
    clip's absolute offset reflects the intervening gap;
    `timeline.otio.json` parses with `OTIO_SCHEMA "Timeline.1"`, its
    V1 track contains a `Gap` child for the black span, and clip
    `source_range` values match the shots' `clip_in_s`/`clip_out_s`.
27. `test_export_load_validation` — a non-angles JSON, a non-sync
    JSON and a duplicate-label sync each raise their §8.1
    `ExportError` messages.
28. `test_export_overlap_propagates` — a hand-edited angles payload
    with overlapping shots raises `ValueError` out of
    `export_timelines` (OTIO's guard), and the already-written
    `.fcpxml` exists (the §8.3 write order).
29. `test_export_project_name` — the given `--name` appears as the
    FCPXML `project` name and the OTIO timeline `name`.
30. `test_cut_json_export_section_shape` — `export_timelines`'
    return value has exactly the §10 `export` keys, file names are
    bare (`Path(name).name == name`), and `shots_exported` excludes
    black shots while `gaps` includes them.

Gate: `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`
green (the 11 pre-existing `ps`-sandbox errors in
`test_engine_render.py`/`test_render_runner.py` on the untouched tree
are noted in `BUILD-LOG.md`, not introduced by this build; the build
verifies they fail identically before its changes). Per `AGENTS.md`,
every ffmpeg-facing function (`qc_proxy_command`, `loudnorm_command`,
and the composed `cut.run` path) is additionally exercised by item 22
on real synthetic media, and the owner runs the built command on the
real demo footage before claiming done (§15).

## 15. What the build also updates, non-goals, live check

**Provenance (at build time, per the brief's Rules).**
`docs/muse-code/BUILD-LOG.md` gains one section in the existing shape:

```markdown
# Build log: `director cut` (2026-11-03)

| Step | By | What |
|---|---|---|
| Brief | Project owner, with Claude Code | `OPEN-BRIEF.md`: the open feature choice. |
| Design | Meta **Muse Code** on **`muse-wonder-1.0`** | `OPEN-DESIGN.md`: the `director cut` feature — one-step orchestration, words bridge, timeline export, whole-cut QC. |
| Build | Meta **Muse Code** on **`muse-spark-1.3`** | `src/director/cut.py`, `src/director/export.py`, the `cut` command in `cli.py`, `tests/test_cut.py`, `tests/test_export.py`. |
| Code review and live test | Claude Code, piloted by the owner | Real footage, below. |

Muse Code token use: TBD (the owner fills this from the run log).
Live test: TBD (the owner fills this from the real-footage run).
```

and the README's "Who built what" table gains:

```markdown
| One-step multicam cut (`director cut`) | Designed by Muse Code on muse-wonder-1.0, built by Muse Code on muse-spark-1.3 |
```

`AGENTS.md` Layout gains, under `src/director/`:

```markdown
  - `cut.py`: one command from files or a folder to the whole job (`sync` → words → `grade` → `angles` → timeline export → `reframe` → whole-cut QC) in one output directory, resumable via `cut.json` fingerprints.
  - `export.py`: `angles.json` shots → FCPXML and OTIO timelines through the installed `ghostcut` exporters.
```

Model-call and token counts stay TBD in both files until the owner
fills them from the run log; the build does not estimate them into the
record.

**Non-goals (explicit, so the build does not drift into them).**

- *Flub/restart/tighten removal.* The largest remaining editorial
  feature, and a separate one: it needs its own take-grouping design
  and a held-out eval against hand picks (the day-1 spike in
  `docs/DIRECTOR-PLAN.md` shows both its promise — 0.94 duration
  precision for transcript-line picks — and its failure mode, line
  granularity spilling across a flub). `cut`'s stage list is where it
  will slot in (between `words` and `angles`, as a `tighten.json`
  that `angles` consumes); this build does not stub it in.
- *The Mac app.* `cut` writes no app project (§1.6). A future adapter
  reads `cut.json` + `angles.json`; it is not scoped here.
- *Acting on QC findings.* QC reports; it never re-cuts, re-grades or
  re-renders (§9.4). Accept/reject of model proposals is the app's
  human-in-the-loop model.
- *Audio re-mastering* (loudness normalisation, de-noise): QC check 9
  measures loudness and warns; no stage in this repo masters audio,
  and `cut` does not become the first by accident.
- *New NLE dialects.* Final Cut Pro (FCPXML) and OTIO are what the
  installed exporters produce. A native Resolve `.xml`/EDL dialect or
  Premiere-specific project file is exporter work in `ghostcut`, not
  Director glue, and no test in this repo could verify it.

**Live check (before done is claimed).** On the owner's real footage —
the 3-minute two/three-camera founder talk for the hackathon demo —
run `director cut <clips> --out-dir demo-job` and: open `cut.mp4` and
`vertical.mp4` end to end; open `timeline.fcpxml` in Final Cut Pro
and confirm every shot is editable, at the right source times, with
the named `.cube` applied per camera; read `cut.json`'s QC section
against what the watch showed. Record the outcome, the stage spends
and any defect found in the BUILD-LOG section above. The synthetic
end-to-end test (§14 item 22) proves the plumbing; only this run
proves the editor's afternoon.

This is a plan, not a special mode; I haven't started implementation.
Reply `go` to execute this plan, or tell me what to change.





