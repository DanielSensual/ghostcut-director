# Design: multicam angle choice (`ghostcut angles`)

Implements `docs/muse-code/ANGLES-BRIEF.md`. Conventions from `AGENTS.md`: core stays
stdlib-only, `from __future__ import annotations`, dataclasses at module boundaries,
guestlist imported lazily inside the calling function, tests offline with `unittest`.
`sync.py` is not modified.

## 1. Inputs as they actually are

- `sync.json` is `{"reference": path, "methods": [...], "clips": [ClipSync...]}` written by
  `cli.command_sync` via `dataclass_to_dict`. Each clip dict has `label` (`A` is the
  reference), `offset_s` (`float | None`; clip time `t` is timeline `t + offset_s`, so
  timeline `T` is clip time `T - offset_s`), and `camera` = `{path, make, model, lens, fps,
  width, height, duration_s, has_audio, gamma, timecode, tc_rate, captured_at}`.
- `sync.load_words` returns `list[tuple[str, float]]` (word, start) only — it discards
  `end`. Lines and cut points need word ends, so `angles.py` parses the same Whisper JSON
  itself (see `load_timed_words`); it does not call or change `load_words`.
- `guestlist.meta.spark.image_json(images, *, schema, name, developer, prompt, model=...,
  effort=..., max_output_tokens=..., max_usd=...) -> SparkResult(data, usage, usd, seconds,
  cached, meta)` inlines images as data URLs and is already content-cached by
  `(image hashes, model, developer, prompt, schema, effort)`; `cache.guard(usd)` raises
  `cache.BudgetExceeded` over the hard stop. `app/muse.py:ask` is the call-shape model:
  build key inputs, `cache.guard(...)` before the call, read `r.data` / `r.usd`.

## 2. Module layout

One new module, one CLI function, one test file. No other file changes except the
`angles` subparser + import in `cli.py`.

| File | Change |
|---|---|
| `src/ghostcut/angles.py` | New. All logic: lines, coverage, frames, Muse, checks, rule, render command, `angles.json` writer. |
| `src/ghostcut/cli.py` | New `command_angles(args)` + `angles` subparser. Thin: parse paths, call `angles.run`, print summary. |
| `tests/test_angles.py` | New. Offline `unittest`, style of `tests/test_sync.py`. |

`angles.py` imports only stdlib + `.ffmpeg_tools` (`run_command`, `ToolingError`) +
`.config` (`write_json`, `read_json`) + `.models.dataclass_to_dict`. No numpy, no
guestlist at module top.

### Functions (every function in `angles.py`)

Constants: `PAUSE_S = 0.6`, `MAX_LINE_S = 8.0`, `BATCH_LINES = 4`, `FRAME_W = 512`,
`OUT_W, OUT_H = 1920, 1080`, `LONG_SHOT_S = 15.0`, `SHOT_SIZES = ("wide", "medium",
"close", "side")`.

| Signature | Job |
|---|---|
| `load_timed_words(path: Path) -> list[TimedWord]` | Parse `{"words": [...]}` or `{"segments": [{"words": [...]}]}`; keep `(text, start, end)` with raw text (punctuation kept for sentence breaks). Drop empty/`start is None`. Missing `end` → next word's `start`, else `start + 0.30`. Sort by `start`; clamp `end >= start`. |
| `split_lines(words: list[TimedWord]) -> list[Line]` | §3 line splitting. Pure. |
| `load_sync(path: Path) -> tuple[str, list[SyncClip]]` | Read `sync.json` → `(reference_path, clips)` as `SyncClip` views (§4). Raise `ValueError` on missing `clips`/duplicate label. |
| `covers(clip: SyncClip, line: Line) -> bool` | §5 coverage predicate. Pure. |
| `covering(clips: list[SyncClip], line: Line) -> list[SyncClip]` | Covering clips in label order. Pure. |
| `frame_time(clip: SyncClip, line: Line) -> float` | Clip-local midpoint: `(line.start_s + line.end_s) / 2 - clip.offset_s`. Pure. |
| `frame_path(frames_dir: Path, line: Line, clip: SyncClip) -> Path` | `angle-frames/L{line.id:03d}_{clip.label}.jpg`. Pure. |
| `frame_command(clip_path: str, at_s: float, out: Path) -> list[str]` | §8 grab command as a list. Pure (tested, not run). |
| `grab_frames(lines, clips, frames_dir: Path) -> dict[tuple[int, str], Path]` | For each line × covering camera, run `frame_command` via `run_command` unless the file exists and is non-empty. Key `(line.id, label)`. A failed grab omits that camera for that line (recorded on the line's `warnings`). |
| `batch_schema(batch: list[Line], coverage: dict[int, list[str]]) -> dict` | §6 JSON schema for one batch; per-line camera enum = that line's covering labels. Pure. |
| `batch_prompt(batch: list[Line], coverage: dict[int, list[str]]) -> str` | §6 user prompt text. Pure. |
| `pick_with_muse(lines, clips, frames, *, model: str, frames_dir: Path) -> tuple[list[Pick], float, list[str]]` | Batched `spark.image_json` calls (§6). Returns picks for every line, total `usd`, warnings. Raises nothing for per-batch API failure — that batch falls back per §7 and adds a warning. Raises `MuseUnavailable` only when guestlist cannot be imported at all. |
| `rule_picks(lines, clips, reference_label: str, min_shot_s: float) -> list[Pick]` | §7 deterministic fallback for all lines. Pure. |
| `rule_pick_for(line, clips, previous_label: str \| None) -> str \| None` | One line's rule choice: next covering label after `previous_label` in label order (wrapping), else first covering label; `None` when nothing covers. Pure. |
| `apply_checks(picks, lines, clips, *, min_shot_s: float) -> list[Shot]` | §5 checks in fixed order → shots. Pure; the only place shots are built. |
| `cut_points(lines: list[Line], words: list[TimedWord]) -> list[float]` | One boundary per adjacent line pair: midpoint of `(prev last word end, next first word start)`; `len == len(lines) - 1`. Pure. |
| `render_command(shots, clips_by_label, audio: SyncClip, out: Path) -> list[str]` | §8 render command as a list. Pure (tested, not run). |
| `render(shots, clips_by_label, audio, out: Path) -> None` | `run_command(render_command(...))`. Only called with `--render`. |
| `run(*, sync_path: Path, words_path: Path, out_path: Path, audio_label: str \| None, render_path: Path \| None, min_shot_s: float, model: str, no_muse: bool) -> dict` | Orchestrator (§9). Returns the `angles.json` payload dict (also written to `out_path`). |
| `main` glue in `cli.command_angles(args: argparse.Namespace) -> dict` | Resolve `--audio` (default: clip whose `camera.path == sync["reference"]`, else label `A`), call `run`, print `N lines → M shots (method, $X.XXXX)`, return payload. |

Internal helpers (still listed; no other functions exist): `_sentence_end(text: str) ->
bool` (last char in `.!?` after stripping quotes/brackets), `_longest_pause_split(words)
-> int` (index after which the intra-line gap is largest), `_merge_same_camera(picks,
lines) -> list[Shot]`, `_merge_short(shots, min_shot_s) -> list[Shot]`,
`_shot_warnings(shots) -> None` (sets `long_shot` overrules). `MuseUnavailable(RuntimeError)`
is the sole new exception.

## 3. Lines

`split_lines` walks timed words once. Start a new line before word `w` when any holds:

1. the previous word ends a sentence (`_sentence_end`), or
2. `w.start - prev.end > PAUSE_S` (0.6 s), or
3. adding `w` would make the line exceed `MAX_LINE_S` (8 s): then break at the longest
   pause *inside* the accumulated words (largest `next.start - cur.end`; ties → earliest);
   if all gaps are equal/zero, break before `w`.

Line `start_s` = first word start, `end_s` = last word end, `text` = words joined with
single spaces (original punctuation kept). Ids are `0..n-1` in time order (`L0`, `L1`, …
in JSON as integers). Words before the first line / gaps belong to no line; lines never
overlap and are sorted. Empty word list → `[]` ( `run` then writes zero shots and exits 0
with a warning, §10).

## 4. Data classes

All `@dataclass(slots=True)` in `angles.py`; serialized with `dataclass_to_dict`.

```python
@dataclass(slots=True)
class TimedWord:  # internal; not written to angles.json
    text: str; start_s: float; end_s: float

@dataclass(slots=True)
class SyncClip:   # view over one sync.json clip dict; not written verbatim
    label: str; path: str; offset_s: float | None
    fps: float; width: int; height: int; duration_s: float

@dataclass(slots=True)
class Line:
    id: int; start_s: float; end_s: float; text: str
    word_start: int; word_end: int        # slice into the TimedWord list, [start, end)
    cameras: list[str]                    # covering labels, label order; filled by run()
    warnings: list[str] = field(default_factory=list)  # e.g. "no_camera", "frame_failed:B"

@dataclass(slots=True)
class Pick:       # one per line, before checks; not written verbatim
    line_id: int; camera: str | None      # None = no camera covers the line
    shot_size: str                        # wide|medium|close|side; "" when camera is None
    reason: str                           # Muse one sentence, or rule text
    method: str                           # "muse" | "rule"
    overruled: list[str] = field(default_factory=list)  # e.g. ["not_covering:B->A"]

@dataclass(slots=True)
class Shot:       # one per angles.json shot; consecutive same-camera lines merged
    id: int; start_s: float; end_s: float          # timeline seconds (cut points, §5.4)
    camera: str; clip_path: str
    clip_in_s: float; clip_out_s: float            # = timeline - offset_s of its camera
    line_ids: list[int]
    shot_size: str                        # first line's size in the shot
    reason: str                           # line reasons joined with " "
    method: str                           # "muse" if every line muse, else "rule"
    overrules: list[str] = field(default_factory=list)  # union of line overrules + merge notes
```

Invariants: every `Line` gets exactly one `Pick`; `Shot.line_ids` partition the lines
that have a camera (uncovered lines belong to no shot, §10); shots are time-ordered,
non-overlapping, and `clip_out_s - clip_in_s == end_s - start_s`.

## 5. Coverage and mechanical checks

Coverage: `covers` is `clip.offset_s is not None and clip.offset_s <= line.start_s + 1e-9
and line.end_s <= clip.offset_s + clip.duration_s - 1e-9`. Unplaced clips never cover.
Epsilon only absorbs float dust on exact boundaries.

`apply_checks(picks, lines, clips, min_shot_s)` runs these steps **in this order**;
nothing reorders them:

1. **Coverage overrule (per line).** If `pick.camera` is `None`, not a known label, or
   not in `line.cameras`: replace with `rule_pick_for(line, clips, prev_kept_label)`
   (`prev_kept_label` = previous line's post-check camera, `None` for line 0), set
   `method = "rule"`, append `f"not_covering:{bad or 'none'}->{new or 'none'}"` (or
   `f"unknown_camera:{bad}->{new}"`) to `pick.overruled`. If `line.cameras` is empty the
   pick stays `camera=None`, `overruled += ["no_camera"]`. A `shot_size` outside
   `SHOT_SIZES` is coerced to `"medium"` with `overruled += [f"bad_shot_size:{old}"]`.
   A line missing from the Muse reply is treated as `camera=None` before this step
   (`overruled += ["missing_from_reply"]` in `pick_with_muse`).
2. **Same-camera merge.** `_merge_same_camera`: maximal runs of consecutive lines with
   equal non-`None` camera become one `Shot` (boundaries fixed in step 4). `None`-camera
   lines break runs and join no shot.
3. **Min-shot merge.** `_merge_short`, repeated until stable: find the first shot with
   `end_s - start_s < min_shot_s` (duration measured on line extents at this stage); merge
   it into its longer neighbour (tie → previous neighbour; only neighbour → that one;
   single shot overall → leave it). The survivor keeps its own camera/sizes; absorbed line
   ids append in time order; append `"min_shot_merge:L{a}+L{b}"` to survivor `overrules`.
   One-shot-per-line alternation can therefore collapse whole stretches — intended.
4. **Cut points.** `cut_points` from the *words*: boundary `i` = `(words[lines[i].word_end
   - 1].end_s + words[lines[i+1].word_start].start_s) / 2`. Shot `start_s`/`end_s` are then
   reassigned: first shot starts at `lines[first].start_s`, last ends at
   `lines[last].end_s`, interior edges are the cut point between the shots' adjacent
   lines. Hence every cut lies in an inter-word gap, never inside a word, and shots tile
   `[first line start, last line end]` with no gaps except spans of uncovered lines
   (a shot ends at its last line's `end_s`, the next starts at its first line's `start_s`).
   `clip_in_s = start_s - offset_s`, `clip_out_s = end_s - offset_s` per shot camera.
5. **Long-shot report.** Any shot `> LONG_SHOT_S` (15 s) gets `overrules +=
   [f"long_shot:{dur:.1f}s"]`; picture is unchanged.

## 6. Muse picks

Decision: `BATCH_LINES = 4` lines per `spark.image_json` call (≤ 4 lines × 4 cameras =
16 images, each 512 px JPEG). Lines with no covering camera are excluded from batches
(they go straight to a `None` rule pick) but keep their ids, so reply ids index the
global line list. Batches are consecutive slices of the remaining lines.

Developer prompt (verbatim, stored as `DEVELOPER` in `angles.py`):

> You are Muse, the multicam angle picker inside GhostCut. You are given, for each of
> several consecutive spoken lines, the line text and duration and one frame from every
> camera that covers the line, labelled by camera letter (A, B, C, ...). The frames for a
> line all show the same moment from different angles. For each line, pick the one camera
> whose angle best serves that line, name the shot size you see in that frame (wide,
> medium, close, or side), and give a one-sentence reason grounded in what is visible
> (who is framed, gaze, gesture, obstructions). Prefer the camera where the current
> speaker is clearly framed and looking toward lens; avoid frames where the speaker is
> turned away, blocked, mid-blink as the only flaw-free alternative exists, or where the
> framing cuts off the subject. You may only choose among the camera letters listed for
> that line. Return one entry for every line id given, in order. Never invent a camera,
> a person, or an action you cannot see.

User prompt (`batch_prompt`): first line `Pick the angle for each line. Reply with one
entry per line, in order.` then per line: `L{id} ({dur:.1f}s) cameras {letters joined
", "}: "{text}"`. Images are appended after the text in line order, camera-label order
within a line — exactly the order `batch_prompt` lists them; `pick_with_muse` builds the
`images` list in that same nested loop. Schema `name="angles_pick"`, `effort="medium"`,
`max_output_tokens=4000`, `model` from `--model` (default `muse-spark-1.3`).

Per-batch schema (`batch_schema`): strict object `{"picks": [...]}` with
`additionalProperties: False`. Because strict JSON Schema cannot vary an enum per array
item by position, the schema is built per batch as a fixed-length tuple-style array:
`"prefixItems": [one item schema per line in the batch]`, `"items": False`,
`"minItems" == "maxItems" == len(batch)`. Each item is
`{"type": "object", "additionalProperties": False, "required": ["line_id", "camera",
"shot_size", "reason"], "properties": {"line_id": {"type": "integer", "enum": [line.id]},
"camera": {"type": "string", "enum": coverage[line.id]}, "shot_size": {"type": "string",
"enum": list(SHOT_SIZES)}, "reason": {"type": "string"}}}`. So the *schema itself* only
admits covering cameras for that line; §5 step 1 still re-checks (defense against a
non-strict endpoint or a cached odd reply). `image_json`'s built-in cache makes a re-run
with identical frames/prompts/schema cost $0; `pick_with_muse` sums `r.usd` across
batches (cached batches contribute their stored `usd` of the first run — reported as
spent, with `cached` count in warnings). Before the first uncached batch,
`cache.guard(0.25)` is called (pattern from `app/muse.py:ask`); `BudgetExceeded` is a
batch failure (§7), not a crash.

Reply handling: entries are matched by `line_id` (not position). Unknown ids ignored;
duplicates: first wins, `overruled += ["duplicate_reply"]`. Any exception from a batch
(`RuntimeError`, `SparkNoAnswer`, `BudgetExceeded`, `KeyError`…) → every line in that
batch gets a rule pick (`method="rule"`, `overruled += ["batch_failed"]`); other batches
still use Muse.

## 7. Rule fallback

Used for a whole run when guestlist import fails, `--no-muse` is given, or (per batch)
a batch fails; also used by §5 step 1 per line. `method: "rule"` on every affected pick;
run-level method is `"rule"` if no line used Muse, else `"mixed"` is *not* used —
`angles.json.method` is `"muse"` when ≥1 line is Muse and the rest rule, recorded per
shot instead (shot `method` per §4). Reason text: `"rule: reference opening"` /
`"rule: alternation"`.

`rule_picks` is deterministic and honours `--min-shot` without lookahead beyond duration
accumulation: line 0 picks the reference camera if it covers, else the first covering
label. For each later line, let `cur` be the current open shot's camera and `dur` its
accumulated line-extent duration (`last line end - first line start`). If `cur` covers
the line and `dur < min_shot_s`, stay on `cur`. Else switch to `rule_pick_for` (next
covering label after `cur`, wrapping; `cur` itself if it is the only cover). If `cur`
does not cover the line, switch immediately regardless of `dur` (a forced cut beats
min-shot). After `apply_checks`, step 3 re-enforces min-shot on the merged result, so
rule output never yields a mergeable short shot except a lone shot or edge-forced cuts.

## 8. ffmpeg commands

Frame grab (`frame_command`, run per line × covering camera, cached by file existence):

```
ffmpeg -v error -ss {at_s:.3f} -i {clip_path} -frames:v 1 -vf scale=512:-2 -q:v 3 -y {out}
```

`-ss` before `-i` (input seek, keyframe + decode-to-exact-frame) at clip-local
`frame_time`. `scale=512:-2` keeps aspect, height even. No fps/orientation normalization
is needed for a still; a rotated phone clip's stored rotation is applied by ffmpeg's
autorotate on decode, so the JPEG matches what an editor sees.

Render (`render_command`): one ffmpeg invocation, built as a list, never a shell string.
Inputs: one `-i` per shot (in shot order), then `-i {audio.clip_path}` last (index `N`).
Per shot `i`: input seeking trims in the filter, not with `-ss`, so cuts stay exact:

```
[{i}:v]trim=start={clip_in_s:.3f}:end={clip_out_s:.3f},setpts=PTS-STARTPTS,
scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,
pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={out_fps},format=yuv420p[v{i}]
```

then `[v0][v1]...concat=n={N}:v=1:a=0[vout]`, and audio uncut from the audio clip:
`[{N}:a]atrim=start={audio_in:.3f}:end={audio_out:.3f},asetpts=PTS-STARTPTS[aout]` where
`audio_in = first_shot.start_s - audio.offset_s`, `audio_out = last_shot.end_s -
audio.offset_s` (audio clip time = timeline − its offset). Map `[vout]`/`[aout]`,
`-c:v libx264 -preset medium -crf 18 -c:a aac -b:a 192k -movflags +faststart {out}`.
No grade filters — log stays log. Decision: `out_fps` = the audio clip's `fps` snapped
via `snap_frame_rate` when > 0, else `30`; the per-shot `fps` filter resamples mixed
source rates (23.976/29.97/59.94) onto that one grid, and `scale+pad` letterboxes mixed
orientations (portrait pads sides, landscape pads top/bottom) to 1920×1080. Shots whose
camera file is missing raise `ToolingError` before ffmpeg runs.

## 9. `angles.json` shape and CLI wiring

```json
{
  "version": 1,
  "sync": "<sync path>", "words": "<words path>",
  "reference": "<reference clip path>",
  "audio": {"label": "A", "clip_path": "...", "offset_s": 0.0},
  "method": "muse" | "rule",
  "model": "muse-spark-1.3",
  "min_shot_s": 2.0,
  "spend_usd": 0.0,
  "warnings": ["..."],
  "lines": [{"id": 0, "start_s": 0.0, "end_s": 0.0, "text": "...", "cameras": ["A", "B"]}],
  "shots": [{"id": 0, "start_s": 0.0, "end_s": 0.0, "camera": "A", "clip_path": "...",
             "clip_in_s": 0.0, "clip_out_s": 0.0, "line_ids": [0],
             "shot_size": "medium", "reason": "...", "method": "muse", "overrules": []}]
}
```

All floats rounded to 3 decimals on write (`round(x, 3)` in `run` only; internals keep
full precision). Written with `config.write_json` (sorted keys, indent 2). Frames live in
`{out_path.parent}/angle-frames/`; `--render` path is used verbatim.

CLI (mirrors `command_sync`): subparser `angles` with `--sync` (required), `--words`
(required), `--out` (required), `--audio` (label, default `None` → reference), `--render`
(default `None`), `--min-shot` (`float`, default `2.0`), `--model` (default
`"muse-spark-1.3"`), `--no-muse` (`store_true`). `command_angles` calls
`require_ffmpeg_binaries()` only when frames or render are needed (always: frames), loads
nothing else, prints one line per overrule plus the final summary. `main`'s existing
`except (FileNotFoundError, ValueError, RuntimeError, ToolingError)` already covers
failures; `MuseUnavailable` subclasses `RuntimeError`.

Run order in `run()`: load sync → load words → `split_lines` → fill `line.cameras` →
resolve audio clip (unknown `--audio` label → `ValueError`) → grab frames (skipped
entirely under `--no-muse` or when Muse is unavailable) → Muse or rule picks →
`apply_checks` → optional `render` → write `angles.json` → return payload. Degraded
guestlist import (`ImportError` on `from guestlist.meta import cache, spark` inside
`pick_with_muse`) raises `MuseUnavailable`, caught in `run`, which falls back to
`rule_picks` for all lines and adds warning `"muse unavailable: <err>"`.

## 10. Edge cases (decided behaviour)

- **Camera drops out mid-take** (clip ends mid-line): `covers` is false for that line
  (whole-line rule) — the camera simply isn't offered to Muse for it; lines fully
  before the dropout still use it. If the dropout splits coverage so a shot's camera
  vanishes, §5 step 1 forces a cut at that line even under `min_shot`.
- **A line no camera covers**: `cameras == []`, excluded from Muse batches, pick
  `camera=None`, `overruled=["no_camera"]`, belongs to no shot. The rendered cut has a
  picture gap only if such lines sit between shots — they do not extend neighbouring
  shots; audio (uncut, spanning first-to-last *shot*) still covers the gap only when
  the gap is interior. Leading/trailing uncovered lines shrink the rendered span.
  `warnings += [f"line L{id} has no covering camera"]`. If *no* line is covered, zero
  shots, no render ( `--render` → `ValueError("nothing to render")`), exit still writes
  `angles.json`.
- **Only one camera covering** (a line, or the whole take): rule and Muse agree —
  schema enum has one letter; same-camera merge collapses everything into one shot;
  min-shot never forces a switch (no neighbour to merge into / only neighbour rule).
- **Mixed frame rates**: sync offsets are seconds, so coverage/cuts are rate-free.
  Render resamples every shot with `fps={out_fps}` (§8); cut times are not frame-snapped
  (sub-frame cuts resample cleanly; frame-exactness is sync's job, already done).
- **Mixed orientations / sizes**: frames are aspect-kept stills; render `scale+pad`
  letterboxes to 1920×1080 with `setsar=1`. Portrait-in-landscape gets side bars — no
  cropping, ever.
- **Unplaced clip** (`offset_s is null`): never covers, never offered, may still be
  `--audio` only if it is the reference (reference is always placed at 0; any other
  unplaced `--audio` → `ValueError`).
- **Zero-length / single-word lines, zero words, one line total**: all flow through the
  same code; a single shot shorter than `min_shot` is left as-is (§5 step 3).
- **Muse returns a non-covering or unknown camera despite the enum**: §5 step 1
  overrules to the rule pick and records it; covered by tests below.

## 11. Tests (`tests/test_angles.py`, offline, no media/network/guestlist)

Style: `unittest.TestCase`, small synthetic `SyncClip`/`TimedWord` builders like
`test_sync._cam`. Muse is never imported (guestlist absent in CI is the point);
Muse-path tests call `apply_checks` / `batch_schema` with hand-built picks. Each test:

| Test | Asserts |
|---|---|
| `test_lines_break_on_sentence_and_pause` | `.!?` and a >0.6 s gap each start a new line; a 0.6 s-exact gap does not. |
| `test_long_line_breaks_at_longest_pause` | A >8 s run splits at the largest internal gap; both halves ≤ 8 s. |
| `test_missing_word_end_falls_back` | `load_timed_words` on dict fixtures: `end` = next start, last word +0.30. |
| `test_coverage_needs_whole_line_and_placement` | Clip ending 0.1 s before line end doesn't cover; `offset_s=None` never covers; exact-boundary clip covers. |
| `test_cut_points_sit_between_words` | Every boundary is strictly after prev word end and before next word start, at the midpoint. |
| `test_same_camera_lines_merge` | A,A,B picks → 2 shots with `line_ids [0,1],[2]`. |
| `test_min_shot_merges_into_longer_neighbour` | 1.0 s shot between 5 s/3 s shots joins the 5 s one; tie joins previous; repeats until stable. |
| `test_rule_opens_on_reference_then_alternates` | `rule_picks`: line 0 = reference, then next-covering rotation, staying put while open shot < min-shot. |
| `test_rule_forced_cut_when_camera_drops_out` | Covering set shrinking mid-take cuts immediately despite min-shot. |
| `test_non_covering_muse_pick_is_overruled` | Pick naming a non-covering label → rule camera, `method rule`, `not_covering:` recorded. |
| `test_unknown_muse_camera_is_overruled` | Pick `"Z"` → same overrule path with `unknown_camera:`. |
| `test_no_camera_line_joins_no_shot` | Uncovered middle line in no shot; neighbours don't stretch over it. |
| `test_batch_schema_enum_is_per_line_coverage` | `batch_schema` prefix items carry exactly each line's covering labels + its `line_id` enum; `minItems == maxItems == 4`. |
| `test_render_command_is_a_list_with_uncut_audio` | `render_command` returns `list[str]`: per-shot `trim/scale/pad/fps`, one `concat`, audio `atrim` spanning first-to-last shot from the audio input, no `-ss`. |
| `test_frame_command_shape` | `frame_command` = input-seek list with `scale=512:-2`, `-frames:v 1`. |
| `test_single_camera_single_shot` | One covering camera → one shot, no min-shot merge, `long_shot` noted if >15 s. |

Suite gate per brief: `python -m unittest discover -s tests -p 'test_*.py'` green except
the 4 pre-existing failures (3 Pillow in `test_render_verify`, 1
`test_render_plan.MakePlan.test_card_refusals`).

## 12. Owner review (2026-11-01) — these override the sections above

1. **No picture gaps.** The audio runs uncut, so a gap in the picture shifts every later shot out of sync.
   An interior span with no covering camera becomes a `Shot` with `camera: "black"` (`clip_path: ""`,
   `method: "rule"`, `overrules: ["no_camera"]`), rendered from a `color=c=black:s=1920x1080:r={out_fps}:d={dur}`
   source. Shots then tile `[first covered line start, last covered line end]` exactly, and the rendered video
   is as long as the audio span. Leading and trailing uncovered lines still trim the span. Add a test that the
   shot durations sum to the audio span.
2. **Min-shot merge only into a camera that covers.** `_merge_short` merges a short shot into a neighbour only
   when the neighbour's camera covers every absorbed line; prefer the longer such neighbour, else the other, else
   leave the short shot as it is and record `min_shot_kept:L{a}` in its overrules. Never merge into or out of a
   `black` shot. Add a test.
3. **Seek per input, not in the filter.** `trim` without a seek decodes each 4K file from its first frame for
   every shot. Each shot input is `-ss {clip_in_s:.3f} -t {dur:.3f} -i {clip_path}` (input seek; exact when
   transcoding), and the per-shot chain starts at `setpts=PTS-STARTPTS`. Audio: `-ss {audio_in:.3f}
   -t {span:.3f} -i {audio path}` with no `atrim`. Update `test_render_command_is_a_list_with_uncut_audio`
   accordingly (it now asserts `-ss` before every `-i`).
