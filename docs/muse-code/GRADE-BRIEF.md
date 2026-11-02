# Brief: per-camera grading and matching (`ghostcut grade`)

Written 2026-11-01 by the project owner with Claude Code for Meta's Muse Code agent. Muse Wonder writes the design
(`docs/muse-code/GRADE-DESIGN.md`), then Muse Code builds it. Read `AGENTS.md` first and follow its conventions.

## The problem

A multicam shoot mixes Sony cameras recording S-Log3 (flat, grey, wide gamut), an iPhone recording HLG (BT.2020,
10-bit) and sometimes Rec.709. `ghostcut angles` (`src/ghostcut/angles.py`) cuts between them, but renders every
camera as it was recorded, so the cut jumps between a grey log picture and a bright phone picture. Each camera needs
a conversion to Rec.709, and then the cameras need to match each other.

## What exists

- `sync.json` from `ghostcut sync`: each clip has `label`, `offset_s` and `camera` with `gamma` (`s-log3-cine`,
  `s-log3`, `hlg`, `pq`, `rec709`, or empty) and the path. Read `src/ghostcut/sync.py` (`camera_from_probe`,
  `parse_sony_xml`) for where gamma comes from.
- `angles.py`: `render_commands` builds one ffmpeg command per shot (filter chain `look`), then a stream-copy join.
- Meta API client (optional): `from guestlist.meta import cache, spark`; `spark.image_json(...)` as `angles.py`
  uses it. Tests stub it the way `tests/test_angles.py:FakeMuseTests` does.
- ffmpeg 8.1 here has `lut3d`, `colorspace`, `tonemap`, `colorchannelmixer`, but **no `zscale`**. Do not depend on
  zscale.

## What to build

1. **Conversion LUTs, generated in Python (stdlib only).** Write 33-point `.cube` files:
   - `s-log3-cine` / `s-log3`: Sony's published S-Log3 curve to scene linear, the S-Gamut3.Cine (or S-Gamut3) to
     Rec.709 primaries matrix, a gentle filmic shoulder so highlights roll off instead of clipping, then the Rec.709
     encoding. Cite the source of every constant in a comment.
   - `hlg`: the BT.2100 HLG inverse OETF to scene linear, BT.2020 to Rec.709 primaries, the same shoulder, Rec.709
     encoding.
   - `rec709` or unknown: no LUT. `pq`: no LUT, with a warning (not supported).
   - Anchor points the tests pin: S-Log3 code 95/1023 (0 % black) maps to Rec.709 black within 0.01; S-Log3 18 % grey
     (420/1023) and HLG 18 % grey each land within 0.03 of the same Rec.709 grey value; a neutral input stays neutral
     (R = G = B within 0.002); output is clamped to 0–1 and monotonic in each channel.
2. **The pixel path.** The LUT must see full-range RGB decoded with the right matrix: BT.2020 for HLG, BT.709 for
   Sony XAVC; limited (video) range unless the stream says full. Design the exact ffmpeg filter chain for one shot
   (decode → RGB at enough bit depth → `lut3d` with tetrahedral interpolation → match → scale/pad to 1920×1080 →
   yuv420p tagged BT.709). It must work on a 10-bit HEVC iPhone clip and on 8- and 10-bit Sony clips.
3. **Matching.** After conversion, measure each camera against a reference camera (`--reference`, default the sync
   reference): sample up to 8 moments that every camera covers, decode each camera's converted frame at that
   moment to a small raw RGB image through an ffmpeg pipe (no numpy, no Pillow), and take the mean and spread of
   each channel over the centre region (where the speaker is). Solve a per-channel gain and offset that maps each
   camera's numbers onto the reference's. Clamp the gain to 0.7–1.4 and the offset to ±0.10, and record when a clamp
   bites. Bake conversion + match into one `.cube` per camera. Report the measured difference to the reference
   before and after, per channel. `--no-match` skips this.
4. **The owner's own LUT.** `--lut LABEL=PATH` uses a `.cube` the owner supplies (for example a house look made for
   S-Log3) in place of the generated conversion for that camera; matching still runs after it. Parse `.cube` files
   (size, domain, data order) so a supplied LUT can be composed with the match.
5. **Muse check (optional, `--check`).** Send Muse Spark one converted, matched frame per camera at two or three
   shared moments and ask, with a strict schema, which camera stands out (exposure, white balance, saturation,
   contrast), in which direction, and how strongly. Record its answer in `grade.json` as advisory; it changes
   nothing. Cached like `angles.py`.
6. **Output.** `grade/` folder with one `.cube` per graded camera, and `grade.json`: per camera label, gamma, the
   conversion used (or the supplied LUT), the match gain and offset, clamps, before/after measurements, warnings;
   the reference; the Muse check if run, with spend.
7. **Use it.** `ghostcut angles --grade grade.json` applies each shot's camera LUT in the render (black shots
   untouched). Without `--grade`, `angles` renders as it does today.
8. **CLI:** `ghostcut grade --sync sync.json --out grade/grade.json [--reference LABEL] [--lut LABEL=PATH ...]
   [--no-match] [--check] [--model muse-spark-1.3]`.

## Tests (offline, no media, no network, no guestlist)

The anchor points above; `.cube` writing and reading round-trip (header, `LUT_3D_SIZE 33`, red changing fastest);
composing a LUT with a gain/offset; the match solve recovering a known gain and offset from synthetic numbers, and
the clamps; gamma → conversion choice including `pq` and unknown; the per-shot ffmpeg chain with and without a LUT,
for an HLG and a Sony clip; `angles` render commands with `--grade`; the Muse check through a stand-in client.
Copy the style of `tests/test_angles.py`. The full suite must stay at the 4 failures already on main (3
Pillow-missing in `test_render_verify`, 1 `test_render_plan.MakePlan.test_card_refusals`).

## Limits

- Core stays dependency-free; guestlist imported inside the function that calls Muse, guarded.
- Do not change `sync.py`. Change `angles.py` only to add the `--grade` path.
- Never call the Meta API or run ffmpeg on real media from tests or while building; the owner runs the live test.
- No client names, people's names, footage paths or keys in code, tests or docs. No third-party LUT files in the repo.
- Do not commit, push or switch branches. The owner reviews and commits.
