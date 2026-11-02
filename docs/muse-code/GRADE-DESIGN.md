# Design: per-camera grading and matching (`ghostcut grade`)

Implements [GRADE-BRIEF.md](GRADE-BRIEF.md) under [AGENTS.md](../../AGENTS.md): stdlib-only core, annotated, dataclasses at boundaries, guestlist imported only inside the Muse-calling function, no media/network in tests. Baseline preserved: 4 failures (3 Pillow in `test_render_verify`, 1 `test_render_plan.MakePlan.test_card_refusals`).

## 1. Module layout

| File | Change |
|---|---|
| `src/ghostcut/grade.py` | New; all grading logic. Imports `config.read_json/write_json`, `ffmpeg_tools` (`run_command`, `ToolingError`), stdlib only. Never imports `angles` (avoids a cycle). |
| `src/ghostcut/angles.py` | Only `--grade`: `load_grade()`, `grade=` on `render_commands()`/`run()`, per-shot `"lut"` in angles.json. Without `--grade`, commands byte-identical to today. |
| `src/ghostcut/cli.py` | `command_grade()`, `grade` subparser, `--grade` on `angles`, pass-through in `command_angles()`. |
| `tests/test_grade.py` | New; §11. |
| `src/ghostcut/sync.py` | Unchanged. |

`grade.py` has its own clip view because [load_sync in angles.py:192](../../src/ghostcut/angles.py:192) drops `gamma`/colour. Gamma already sits in `clips[].camera.gamma`: Sony `CaptureGammaEquation` via [parse_sony_xml in sync.py:122](../../src/ghostcut/sync.py:122) overrides stream transfer in [camera_from_probe in sync.py:150](../../src/ghostcut/sync.py:150) (`arib-std-b67→hlg`, `smpte2084→pq`, `bt709→rec709`, else raw transfer, else `""`).

## 2. Types and every function

```python
LUT_SIZE = 33; MEASURE_W, MEASURE_H = 64, 36; MAX_MOMENTS = 8; CHECK_MOMENTS = 3
GAIN_MIN, GAIN_MAX, OFFSET_MAX = 0.7, 1.4, 0.10
@dataclass(slots=True) class Cube: size:int; data:list[tuple[float,float,float]]  # [(b*size+g)*size+r], red fastest
    domain_min=(0.,0.,0.); domain_max=(1.,1.,1.); title=""
@dataclass(slots=True) class SourceColour: range="tv"; space="bt709"; primaries="bt709"; transfer=""; pix_fmt=""; bits=8
@dataclass(slots=True) class GradeClip: label,path,gamma:str; offset_s:float|None; duration_s:float
    width=height=0; fps=0.0; colour:SourceColour
@dataclass(slots=True) class ChannelStats: mean,std:tuple[float,float,float]; moments:int  # Rec.709-encoded, post-conversion
@dataclass(slots=True) class MatchResult: gain,offset,gain_clamped,offset_clamped:tuple[...]; matched:bool
@dataclass(slots=True) class GradeShotInfo: label:str; lut:Path|None; colour:SourceColour  # used by angles
class GradeError(RuntimeError); class CheckUnavailable(RuntimeError)
```

Colour (pure): `clamp01(x)->float`; `slog3_to_linear(s)->float`, `hlg_to_linear(s)->float`, `shoulder(l)->float`, `rec709_encode(l)->float` (§3); `_primaries_to_xyz(primaries)->tuple[float,...]` (9 floats); `gamut_matrix(gamma)->tuple[float,...]` (source-linear→Rec.709-linear, `ValueError` if no conversion); `convert_pixel(gamma,r,g,b)->tuple[float,float,float]` (full scalar §3 pipeline; anchor tests call this, not the quantised cube); `conversion_kind(gamma,supplied)->tuple[str,str|None]` (§6).

Cubes: `identity_cube(size=33)->Cube`; `generate_conversion_cube(gamma)->Cube` (`convert_pixel` on 33³ grid); `format_cube(cube,title)->str` (pure; tests assert on it); `write_cube(path,cube)->None`; `parse_cube(text,source="<cube>")->Cube` (`ValueError` naming source+line); `read_cube(path)->Cube`; `cube_at(cube,r,g,b)->tuple` (trilinear after domain normalisation); `resample_cube(cube,size=33)->Cube`; `compose_gain_offset(cube,gain,offset)->Cube` (node-wise `clamp01(g*v+o)`, no interpolation); `parse_lut_specs(items:list[str])->dict[str,Path]` (malformed/duplicate → `ValueError`).

Sync/probe/sample/match: `load_sync_for_grade(path)->tuple[str,list[GradeClip]]` (reference label = clip whose `camera.path==data["reference"]`, else `ValueError`; label presence/uniqueness as in `angles.load_sync`; gamma lower-cased/stripped); `default_colour(gamma)->SourceColour` (`hlg→tv/bt2020nc/bt2020`, else `tv/bt709/bt709`); `probe_source_colour(clip)->SourceColour` (§5; failure → default, caller warns `probe_failed:<label>`); `lut_filter_chain(colour,lut:Path|None)->str` (exact §5 `-vf`; `lut=None` returns today's legacy `look` string verbatim; also used by `angles`); `pairwise_moments(a,b)->list[float]` (§7); `shared_moments(clips)->list[float]` (intersection of all placed clips, same construction; for the check); `sample_command(clip,at_local_s,conversion_lut)->list[str]` (§5); `decode_centre_frame(clip,at_local_s,conversion_lut)->bytes` (`subprocess.run(capture_output=True)` — binary pipe, so not the text-mode `ffmpeg_tools.run_command`; bad exit/length → `GradeError` + last 300 chars of stderr); `centre_stats(raw:bytes)->ChannelStats` (one frame, `moments=1`; wrong length → `GradeError`); `measure_camera(clip,moments,conversion_lut)->ChannelStats` (mean of per-frame means, mean of per-frame stds); `solve_match(cam,ref)->MatchResult` (§7); `apply_stats(stats,gain,offset)->ChannelStats` (`mean'=clamp01(g*m+o)` for reporting, `std'=g*std`).

Check/orchestration: `CHECK_DEVELOPER:str`; `check_schema(labels)->dict`; `check_prompt(moment_s,labels)->str` (§8); `check_frame_command(clip,at_local_s,final_lut,out)->list[str]` (matched cube, then `,scale=512:-2`, `-frames:v 1 -q:v 3`); `check_with_muse(frames:list[tuple[float,dict[str,Path]]],labels,*,model)->tuple[dict,float,list[str]]` (raises `CheckUnavailable` on ImportError; moment with <2 existing frames skipped); `run(*,sync_path,out_path,reference_label,lut_specs,no_match,check,model)->dict` (§9). In `angles.py`: `load_grade(path)->dict[str,GradeShotInfo]` (§10); `render_commands(...,grade=None)` and `run(...,grade_path=None)` gain keyword-only params.

## 3. Colour math

Pipeline per pixel, full-range RGB 0..1: (1) per-channel inverse transfer → scene linear in source gamut; (2) 3×3 gamut matrix → Rec.709 primaries; (3) clamp negative linear to 0 (hard gamut clip, no soft mapping); (4) per-channel shoulder; (5) per-channel Rec.709 encode; (6) clamp 0..1.

### 3.1 S-Log3 (Sony, *S-Log3 Technical Summary*; same constants as colour-science `eotf_inverse_SLog3` / ACES S-Log3 IDT)

Work in 10-bit codes `C = 1023*s` so published integers are literal. Log branch, `C >= 171.21029469287395`: `L = 10**((C-420.0)/261.5)*0.19 - 0.01`. Linear branch below the cut (straight line through the 0% black point and the cut point, where the log branch gives `L=0.01125`): `L = (C-95.0)*0.01125/(171.21029469287395-95.0)`. Published anchors in the constants: 0% reflectance = code 95, 18% grey = code 420, cut at linear 0.01125. Below 95, `L<0` and stages 3/6 clamp to black. `s-log3-cine`/`s-log3` share the curve; only the gamut differs.

### 3.2 HLG (ITU-R BT.2100 inverse OETF)

`a=0.17883277`, `b=1-4a=0.28466892`, `c=0.5-a*ln(4a)=0.55991073`. `s<=0.5: L=s*s/3`; `s>0.5: L=(exp((s-c)/a)+b)/12`. `L` is BT.2020 scene linear, `s=1→L=1`. **No BT.2100 OOTF**: it is display-side brightness adaptation and would move 18% grey off the S-Log3 value, breaking the shared anchor; §3.4 is the only display adaptation.

Grey definitions (the anchors): "18% grey" = scene-linear reflectance 0.18, expressed per recording by inverting its curve — S-Log3: `420/1023=0.410557` (black: `95/1023=0.092864`); HLG: `a*ln(12*0.18-b)+c = 0.672334` (≈688/1023; black: `0.0`). Both decode to exactly `L=0.18`; neutrals stay on the white point through either matrix (rows sum to 1); the shoulder is identity at 0.18; so both land on `rec709_encode(0.18)=1.099*0.18**0.45-0.099=0.4089`. Expected test numbers: `|convert_pixel(g,95/1023 ×3)|<=0.01` per channel; both greys within 0.03 of 0.4089 and of each other; neutrality `max-min<=0.002`; HLG `0→0`.

### 3.3 Gamut matrices (derived, never hand-typed)

Primaries (CIE x,y), all with D65 white `(0.3127,0.3290)` — no chromatic adaptation: S-Gamut3.Cine R(0.766,0.275) G(0.225,0.800) B(0.089,−0.087) (Sony spec); S-Gamut3 R(0.730,0.280) G(0.165,0.840) B(0.100,−0.030) (Sony spec); BT.2020 R(0.708,0.292) G(0.170,0.797) B(0.131,0.046) (ITU-R BT.2020); Rec.709 R(0.640,0.330) G(0.300,0.600) B(0.150,0.060) (ITU-R BT.709).

Derivation (`_primaries_to_xyz`): primary → column `(x/y, 1, (1-x-y)/y)`; white → `W=(0.3127/0.3290, 1, (1-0.3127-0.3290)/0.3290)=(0.950456,1,1.089058)`; solve `P·s=W` (explicit 3×3 inverse, stdlib); matrix `=P·diag(s)`. Then `gamut = XYZ(Rec709)⁻¹ · XYZ(source)` at import. Same white ⇒ `gamut·(1,1,1)=(1,1,1)` to float error; neutrality is test-asserted, rows are not renormalised. Eyeball reference for BT.2020→709 (code derives it): rows ≈ `(1.6605,−0.5876,−0.0729)`, `(−0.1246,1.1329,−0.0084)`, `(−0.0182,−0.1006,1.1187)`.

### 3.4 Shoulder (GhostCut design, unpublished)

Identity for `l<=0.18`; above: `l' = 0.18 + 0.82*(1-exp(-(l-0.18)/0.82))`. Continuous, derivative 1 at the knee (so grey anchors are exact), monotonic, asymptote 1.0 pre-encoding (encoded output asymptotes to 1.0, never hard-clips). Per channel after the matrix; slight hue shift in saturated highlights accepted. Constants: knee 0.18 (the grey anchor), headroom `0.82=1−0.18`.

### 3.5 Rec.709 encode (ITU-R BT.709 OETF)

`l<0.018: 4.5*l`; else `1.099*l**0.45-0.099`; then clamp 0..1. Monotonicity as tested: the neutral ramp `convert_pixel(g,v,v,v)` is non-decreasing in every channel, and each channel's own response (others 0) is non-decreasing; cross-channel responses may fall and clip at 0 — gamut rotation, not non-monotonicity.

## 4. .cube write/read/compose

Write (`format_cube`), exactly: `TITLE "ghostcut <label> <kind>"`, `LUT_3D_SIZE 33`, `DOMAIN_MIN 0.000000 0.000000 0.000000`, `DOMAIN_MAX 1.000000 1.000000 1.000000`, then 33³ lines `"%.6f %.6f %.6f"`, red fastest (index `(b*33+g)*33+r`; b outer, g middle, r inner).

Read (`parse_cube`): `#` comments/blanks ignored; optional quoted `TITLE`; `LUT_3D_SIZE n` (2..256) required with exactly `n³` data rows after it; `DOMAIN_MIN/MAX` 3 floats each, default 0/1, any order before data. `LUT_1D_SIZE`, a second size, a non-3-float row, wrong row count, or `domain_max<=domain_min` → `ValueError`. No 1D support.

Compose: generated/identity cubes are canonical (33, 0..1), so `compose_gain_offset` is node-wise. A supplied cube is canonicalised once by `resample_cube`: per canonical node `x∈[0,1]³`, `cube_at` clamps `x` into the supplied domain, normalises `(x−dmin)/(dmax−dmin)` per channel, trilinearly interpolates the supplied grid (edge-clamped). A 17/65-point or non-0..1-domain cube thus composes like a 33 cube; baked output is always 33/0..1. ffmpeg applies the final cube tetrahedrally; node values are exact, trilinear-resample interior error is below the 6-decimal text precision.

## 5. Pixel path: exact chains

Probe per clip (`run`): `ffprobe -v error -show_entries stream=codec_type,pix_fmt,color_range,color_space,color_transfer,color_primaries -of json <path>`, first video stream. `range`: `pc→full` else `tv`; `space`: stream value if `bt2020nc`/`bt709` else gamma default; `bits`: 10 if `pix_fmt` contains `10` else 8. The LUT sees RGB decoded with the recording's YUV matrix (BT.2020 HLG, BT.709 Sony XAVC; that RGB is then *interpreted* as S-Gamut/BT.2020 primaries for §3.3), limited range unless the stream says full. Decode is forced via `scale` with explicit `in_range`/`in_color_matrix` (untagged/mis-tagged streams cannot pick another matrix) and `rgb48le` (10-bit never truncated to 8 before the LUT; 8-bit upscaled exactly `v<<8`).

Graded shot (`lut_filter_chain` inside the existing [render_commands in angles.py:663](../../src/ghostcut/angles.py:663) scaffolding; `-ss/-t/-frames:v` unchanged):

```
ffmpeg -v error -y -ss <clip_in> -t <dur+1> -i <clip> -map 0:v:0 -vf <VF> -frames:v <n> -an -c:v libx264 -preset medium -crf 16 -color_primaries bt709 -color_trc bt709 -colorspace bt709 -color_range tv <part.mp4>
<VF> = scale=in_range=<tv|full>:in_color_matrix=<bt2020nc|bt709>:out_range=full:out_color_matrix=rgb:flags=accurate_rnd+full_chroma_int,format=rgb48le,lut3d=file='<lut>':interp=tetrahedral,setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv,scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=<fps>,format=yuv420p
```

`lut3d` sits immediately after `format=rgb48le`, before all spatial scaling: full-resolution full-range source-matrix RGB at 16-bit. Match is baked into `<lut>`, so no second colour filter. The second `scale` works in graded Rec.709 RGB; `setparams` tags BT.709/TV so `format=yuv420p` converts with the BT.709 matrix, limited; encoder flags repeat tags on parts (join is stream-copy, so part tags are output tags). LUT path containing `'` or `:` → `GradeError` at load (no escaping scheme). Per source only `in_range`/`in_color_matrix` change: HLG 10-bit HEVC (`yuv420p10le`, tv, bt2020nc, arib-std-b67) → `tv/bt2020nc`; Sony 8-bit (`yuv420p`, tv, bt709) and Sony 10-bit (`yuv420p10le`/`yuv422p10le`, tv, bt709) → `tv/bt709`; `full` whenever the probe says `pc`. No `zscale`/`colorspace`/`tonemap` anywhere. No LUT for the camera → today's legacy `look` string exactly. Black shots: legacy lavfi chain always.

Measurement pipe (`sample_command`), conversion LUT only, centre 50%×50% cropped in-filter (the speaker), raw out (`-ss` before `-i`, as in `angles.frame_command`):

```
ffmpeg -v error -y -ss <at_local> -i <clip> -map 0:v:0 -frames:v 1 -vf scale=in_range=<..>:in_color_matrix=<..>:out_range=full:out_color_matrix=rgb:flags=accurate_rnd+full_chroma_int,format=rgb48le[,lut3d=file='<conv>':interp=tetrahedral],crop=w=iw/2:h=ih/2:x=iw/4:y=ih/4,scale=64:36:flags=bilinear,format=rgb24 -f rawvideo -pix_fmt rgb24 pipe:1
```

`centre_stats` consumes exactly `64*36*3=6912` bytes. No numpy/Pillow/temp files.

## 6. Conversion choice

| gamma | kind | Cube |
|---|---|---|
| `s-log3-cine` / `s-log3` | `generated_slog3_cine` / `generated_slog3` | §3 (Cine / S-Gamut3) |
| `hlg` | `generated_hlg` | §3, BT.2020 |
| `rec709` | `none_rec709` | None (already Rec.709); match baked into identity-based cube |
| `pq` | `none_pq` | None; warning `pq_not_supported:<label>`; excluded from match and check |
| other / `""` | `none_unknown` | None; warning `unknown_gamma:<label>:<gamma\|empty>`; excluded from match and check |
| any + `--lut` | `supplied` | Supplied cube replaces conversion (even pq/unknown); match still applies; `source_lut` = path as given |

## 7. Match solve and clamps

Measurements are post-conversion (generated cube, supplied cube, or — `rec709` only — no cube), Rec.709-encoded, via §5. `pairwise_moments(ref,X)`: overlap `[max(off), min(off+dur)]` of the two placed clips shrunk 5% each end; `[]` if either unplaced or shrunk overlap `<0.5s`; else `min(8, max(1,int(overlap_s)))` moments at `(i+0.5)/n`. The reference is re-measured at each camera's moments so before/after compare like with like.

Per channel: `gain_raw = std_ref/std_cam`; clamp gain to `[0.7,1.4]` (record `gain_clamped`); `offset_raw = mean_ref − gain_clamped*mean_cam`; clamp offset to `±0.10` (record `offset_clamped`). Degenerate spread (`std<=1e-6` either side): gain 1, offset from means, warning `degenerate_spread:<label>`. This exactly recovers a synthetic affine: if cam came from ref by `g0,o0`, then `std_cam=std_ref/g0`, `mean_cam=(mean_ref−o0)/g0`. Final cube: `compose_gain_offset(base, gain, offset)`, base = generated / resampled-supplied / `identity_cube()` (matched `rec709`). Reference: gain 1, offset 0, plain conversion cube (none if `rec709`). Exact-identity match still yields the conversion cube. Report per channel: `before` (camera mean/std), `reference`, `after=apply_stats(before,clamped)`, `diff_before=before.mean−reference.mean`, `diff_after=after.mean−reference.mean`. `--no-match`: no sampling, no ffmpeg, gain 1/offset 0, `matched:false`, measurement fields `null`.

## 8. Muse check (`--check`, advisory; changes nothing)

After final cubes exist. Frames: first `CHECK_MOMENTS` of `shared_moments(all placed, non-excluded cameras)`; per moment one 512-wide JPEG per camera via `check_frame_command` (matched cube), cached by existence at `<out_dir>/check-frames/M<i>_<label>.jpg`. One `spark.image_json` per moment, images in sorted-label order, `cache.guard(0.25)` before each call (mirrors [pick_with_muse in angles.py:312](../../src/ghostcut/angles.py:312)); `spark.image_json(images, schema=..., name="grade_check", developer=CHECK_DEVELOPER, prompt=..., model=model, effort="medium", max_output_tokens=2000)`. Spend sums `r.usd`; cached replies warn `"<n> check(s) served from cache"`; a failed call drops that moment (`check_moment_failed:<i>`), never fails the grade. ImportError → `CheckUnavailable` → warning `muse unavailable: …`, `check:null`. The recorded reply is stored verbatim and read by no other code path.

`CHECK_DEVELOPER` (verbatim):

```
You are Muse, the colour-match checker inside GhostCut. You are given one frame from every camera at the same moment of the same event, after each camera has been converted to Rec.709 and matched to the reference camera. The frames show the same scene from different angles, so composition, framing and who is facing the lens will differ; ignore those. Judge only colour: exposure, white balance, saturation and contrast. For each of the four, name the one camera that stands out most from the others, the direction it stands out in, and how strongly. If no camera stands out on a dimension, say so with camera "none", direction "none" and strength "none". Never invent a camera that is not listed. This check is advisory: nothing is adjusted from your answer.
```

`check_prompt(moment_s, labels)` (verbatim template; the numbered list is generated from the sorted labels actually sent):

```
Check the colour match at t=<moment_s:.1f>s. Cameras, in image order: 1 = camera A, 2 = camera B. For each of exposure, white balance, saturation and contrast, reply with the camera that stands out (or "none"), the direction, and the strength. Also give the single overall standout camera (or "none").
```

Schema (`check_schema(labels)`, strict, tuple-style like `angles.batch_schema`): top `{moment_s:number, overall_standout:enum(labels+["none"]), assessments}`; `additionalProperties:false` everywhere; `assessments`: `prefixItems` exactly 4, `items:false`, `min/maxItems:4`, fixed order exposure, white_balance, saturation, contrast. Each item `{dimension:enum([that dimension]), standout_camera:enum(labels+["none"]), direction:enum(<per-dimension>), strength:enum(["none","slight","moderate","strong"]), note:string}`. Directions: exposure `["brighter","darker","none"]`; white_balance `["warmer","cooler","none"]`; saturation `["more_saturated","less_saturated","none"]`; contrast `["higher_contrast","lower_contrast","none"]`.

## 9. grade.json

`config.write_json` (sorted keys) to `--out`; cubes beside it as `<label>.cube`; `lut` is the file name relative to that directory. Floats rounded 6 dp, moments 3 dp.

```json
{"version": 1, "sync": "<as given>", "reference": "<reference clip path>", "reference_label": "A",
 "model": "muse-spark-1.3", "no_match": false, "spend_usd": 0.0, "warnings": [], "check": null,
 "cameras": [{"label": "A", "path": "...", "gamma": "s-log3-cine",
   "conversion": {"kind": "generated_slog3_cine", "source_lut": null}, "lut": "A.cube",
   "source_colour": {"range": "tv", "space": "bt709", "primaries": "bt709", "transfer": "bt709", "pix_fmt": "yuv422p10le", "bits": 10},
   "match": {"matched": true, "is_reference": false, "moments": [12.5, 20.0],
     "gain": [1,1,1], "offset": [0,0,0], "gain_clamped": [false,false,false], "offset_clamped": [false,false,false],
     "before": {"mean": [0,0,0], "std": [0,0,0]}, "reference": {"mean": [0,0,0], "std": [0,0,0]},
     "after": {"mean": [0,0,0], "std": [0,0,0]}, "diff_before": [0,0,0], "diff_after": [0,0,0]},
   "warnings": []}]}
```

Unmatched/`--no-match`: `matched:false`, `moments:[]`, `before/reference/after/diff_*` `null`. `check` when run: `{"model","spend_usd","moments":[{"moment_s","frames":{"A":"check-frames/M0_A.jpg"},"reply":{…schema object…}}]}`.

`run()` order: load sync → resolve reference (§10) → read+resample supplied LUTs (failure aborts before anything is written) → probe colour → build conversion cubes → per non-reference camera: moments, measure, solve, compose → write cubes → optional check → write grade.json last (a grade.json on disk always has its cubes).

## 10. angles --grade wiring; CLI

`angles.load_grade(path)`: `version!=1` → `ValueError`; per camera with non-null `lut`, resolve relative to the grade.json dir, require existence (`ValueError`), carry `source_colour` into `GradeShotInfo`; `lut:null` → `GradeShotInfo(lut=None)` (legacy render). `render_commands(grade=...)`: black → legacy untouched; camera with cube → `lut_filter_chain(colour,lut)`; camera without cube or absent from grade.json → legacy, with `run()` warning `grade_missing_camera:<label>` for the absent case; a grade label not in sync.json → `ValueError` before rendering. angles.json gains `"grade": "<path|null>"` and per-shot `"lut": "<file name|null>"`. The Muse picker frames stay ungraded (picker unchanged).

CLI: `ghostcut grade --sync sync.json --out grade/grade.json [--reference LABEL] [--lut LABEL=PATH ...] [--no-match] [--check] [--model muse-spark-1.3]`. Subparser after `angles`: `--sync`/`--out` required; `--reference` default `None` → sync reference clip's label (unknown label → `ValueError`); `--lut` append, default `[]`; `--no-match`/`--check` store-true; `--model` default `"muse-spark-1.3"`. `command_grade` calls `require_ffmpeg_binaries()` only when decoding will happen (`not no_match or check`), runs `grade.run`, prints per camera `"<label> <gamma> <kind> gain <g,g,g> offset <o,o,o>"` (` clamped` appended if any clamp bit; gain/offset omitted when unmatched), warnings as `! …`, then `Wrote <out>`; errors flow through `main()`'s existing handler. `angles` gains `--grade` (default `None`); `command_angles` passes it and adds `or getattr(args,"grade",None)` to its `require_ffmpeg_binaries` condition so `--grade` alone still validates the file.

## 11. Tests (`tests/test_grade.py`, unittest, offline, no media/network/guestlist)

Anchors via `convert_pixel`: slog3 black (`95/1023` → ≤0.01), slog3 grey and HLG grey (`0.410557`/`0.672334` → 0.4089 ±0.03, and each other ±0.03), neutrality (≤0.002), clamping/monotonic neutral ramp, HLG 0→0. Cubes: round-trip (header, `LUT_3D_SIZE 33`, second data line `(1/32,0,0)` — red fastest), parse rejections (1D, row count, non-float, inverted domain), resample of a 17-point domain-0..2 supplied cube, node-wise compose with clamping. `conversion_kind` table (all §6 rows incl. `pq`, `""`, supplied-overrides-pq). Match: recover synthetic `g0=(1.2,0.9,1.1), o0=(0.03,−0.02,0.01)` to 1e-9; clamps (gain 2.0→1.4 flagged, offset derived from clamped gain then clamped 0.10); degenerate spread → gain 1. Sampling pure parts: `pairwise_moments` overlap/unplaced, `centre_stats` on crafted 6912 bytes, `sample_command` shape (`pipe:1`, `rgb24`, LUT present, `crop` before `scale=64:36`). Chains: HLG (`bt2020nc`, `rgb48le` before `lut3d`, tetrahedral, `yuv420p` last, no `zscale`), Sony 8/10-bit (`bt709`, identical string — depth lives in `rgb48le`), full-range (`in_range=full`), no-LUT chain equals legacy string. angles: `render_commands` with grade (graded shot has its cube+matrix, black/ungraded do not) and without (unchanged); `load_grade` rejects missing cube/bad version. Check: fake `guestlist.meta` in `sys.modules` exactly as [FakeMuseTests in tests/test_angles.py:297](../../tests/test_angles.py:297) — image order matches the prompt list, schema enums = labels sent, reply recorded verbatim under `check` only, spend sums; schema shape (4 prefixItems, per-dimension directions). Run/CLI without ffmpeg: temp-dir hand-written sync.json with nonexistent paths through `grade.run(no_match=True)` — cubes parse, reference resolution, supplied kind, `pq` → `lut:null`+warning; `parse_lut_specs` errors. Full suite green except the 4 baseline failures.

## 12. Edge cases (decided)

- **No shared moment with the reference** (`pairwise_moments==[]`, incl. `offset_s=None`): conversion-only cube, `matched:false`, gain 1/offset 0, measurements `null`, warning `no_shared_moment:<label>`; other cameras still match. Global intersection empty but pairwise overlaps exist: matching proceeds pairwise, only the check is skipped (`check:null`, `no_shared_moment_for_check`). Unplaced reference: `GradeError`.
- **Supplied LUT, different size/domain**: accepted, resampled once (§4); size outside 2..256 or inverted domain → `ValueError` aborting before any write. Label not in sync.json → `ValueError`. File never modified.
- **Unknown gamma**: no conversion, no match, no check frame, legacy render (§6); with `--lut` it grades and matches via the supplied cube.
- **Reference itself log/HLG**: reference gets its conversion cube at gain 1/offset 0; others are measured through their conversions against the reference measured *through its conversion* — the target is the converted reference, never raw log. Reference `rec709`: no cube, target is its decoded Rec.709. Reference `pq`/unknown (incl. via `--reference`): matching disabled for all, warning `reference_unconverted:<label>`; conversion cubes still written.
- Misc: an unparseable supplied LUT aborts the run (never silently fall back to generated). A moment whose decode fails is dropped (`sample_failed:<label>:<t>`); all moments failing degrades that camera to the no-shared-moment outcome. Duplicate sync labels → `ValueError`.

## 13. Owner review (2026-11-01) — these override the sections above

1. **HLG exposure follows ITU-R BT.2408.** HLG reference white (diffuse 100 %) is signal 0.75, scene linear
   `E_ref = hlg_to_linear(0.75) = 0.26496`, and an 18 % grey card sits at signal ≈ 0.38. Decoding HLG straight to
   `L = E` (§3.2) would put that grey at linear 0.048 and render the iPhone about 1.9 stops dark. So the HLG
   branch is `L = hlg_to_linear(s) / E_ref`: reference white → 1.0, 18 % grey → 0.18, highlights above reference
   white go above 1.0 and the shoulder rolls them off. Still no OOTF. The HLG grey anchor becomes the signal for
   `E = 0.18 * E_ref` (≈ 0.3779, BT.2408's 38 %), landing on 0.4089 like S-Log3's code 420; the anchor tests use it,
   plus HLG 0.75 → the same output as S-Log3 100 % reflectance (code `420 + 261.5*log10(1.01/0.19)` ≈ 609.8) within 0.005.
2. **Explicit matrices on both sides of the LUT.** `scale`'s `out_color_matrix` has no `rgb` value here (check
   `ffmpeg -h filter=scale`); converting to an RGB format ignores it, so drop it from the input scale. On the way
   out, do not rely on `setparams` to choose the RGB→YUV matrix (swscale's default output matrix is BT.601): the
   chain after `lut3d` is `scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=…,setsar=1,
   fps=<fps>,scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,setparams=color_primaries=bt709:
   color_trc=bt709:colorspace=bt709:range=tv`. Same for the check JPEG (`out_range=full` there, since JPEG is
   full range) and the measurement pipe stays RGB end to end.
3. Docs and code use repo-relative paths only; no absolute paths from the build machine.

## 14. Changed after the build (2026-11-01, Claude Code review and live test)

- **§7 match solve replaced.** Matching spreads failed on a real wide/close pair (red diff −0.02 → −0.11, gains
  0.73…1.34, offsets clamped): framing, not colour, drives a channel's spread. `solve_match` now takes exposure from
  the BT.709 luma ratio of the means (clamp 0.7–1.4) and white balance from red and blue against green (clamp
  0.8–1.25 each), as pure gains; offsets are always 0, so black stays black.
- **S-Log3 reads the stored code in either decode range**: `C = 64 + 876·s` after a limited-range decode,
  `1023·s` after a full-range one, so the conversion cube is generated per camera for its probed range.
- Colour is probed even under `--no-match` (the render decodes with what the cube assumed).
- Rec.709 cameras match (onto an identity cube) and appear in the check; the check frame of a camera with no cube
  is shown as recorded.
- The check gives Spark 6000 output tokens (2000 ran out on reasoning) and records why a moment failed.
