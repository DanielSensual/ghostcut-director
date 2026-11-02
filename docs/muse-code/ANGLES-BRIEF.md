# Brief: multicam angle choice (`ghostcut angles`)

Written 2026-11-01 by the project owner with Claude Code for Meta's Muse Code agent. Muse Wonder writes the design
(`docs/muse-code/ANGLES-DESIGN.md`), then Muse Code builds it. Read `AGENTS.md` first and follow its conventions.

## What exists

`ghostcut sync` (`src/ghostcut/sync.py`, `cli.py: command_sync`) places clips from several cameras on one
timeline and writes `sync.json`: `{"reference": path, "clips": [ClipSync...]}`. Each clip has `label` (A, B, C…),
`offset_s` (clip time t sits at timeline t + offset_s; `null` = not placed), and `camera` (path, make, model,
fps, width, height, duration_s, gamma). Whisper word JSON loads with `sync.load_words` (list of `(word, start, end)`).

The Meta API client lives in the sibling `guestlist` package (optional, like `app/muse.py` uses it):
`from guestlist.meta import cache, spark`; `spark.image_json(images, schema=, name=, developer=, prompt=, ...)`
sends local images plus text and returns schema-constrained JSON with its cost; `cache.guard(usd)` enforces the
hard budget stop. Read `app/muse.py:ask` and `src/guestlist/meta/spark.py` in the guestlist repo for the call shape.

## What to build

A talking video shot on 2–4 synced cameras becomes one cut that switches angle on the lines, with one continuous
audio track. Muse Spark looks at a frame from every camera that covers each line and picks the angle, with a reason.

1. **Lines.** Split the words of the audio clip into lines: break at sentence-ending punctuation or a pause
   over 0.6 s; cap a line at 8 s (break at the longest pause inside).
2. **Coverage.** A camera covers a line when its clip spans the whole line on the timeline. Unplaced clips never
   cover anything.
3. **Frames.** For every line and covering camera, grab one frame at the line's midpoint (clip time = timeline
   time − offset_s) with ffmpeg, 512 px wide JPEG, cached under `<out dir>/angle-frames/`.
4. **Muse picks.** Batches of lines in one `spark.image_json` call each: the line text and duration, then the
   frames labelled by camera letter. The schema allows only camera letters that cover that line. Muse returns
   per line: camera, shot size (wide / medium / close / side), and a one-sentence reason. Results are cached so a
   re-run costs nothing.
5. **Mechanical checks after Muse** (these overrule it, and each overrule is recorded):
   - the camera must cover the line, else fall back to the rule pick for that line;
   - consecutive lines on the same camera merge into one shot;
   - a shot shorter than `--min-shot` (default 2.0 s) merges into its longer neighbour;
   - cut points sit in the gap between the last word of one line and the first word of the next (midpoint of the
     gap), never inside a word;
   - a shot longer than 15 s is reported, not changed.
6. **Rule fallback** when guestlist is missing, `--no-muse` is given, or a batch fails: open on the reference camera,
   then alternate between covering
   cameras on each line, honouring `--min-shot`. Marked `method: "rule"`.
7. **Output `angles.json`:** the shots (timeline start/end, camera label, clip path, clip in/out, line ids, shot
   size, reason, method `muse` or `rule`, any overrule), the lines, the audio clip, and Meta spend.
8. **Render (`--render cut.mp4`):** ffmpeg trims each shot from its camera's clip, scales and pads to 1920×1080,
   and concatenates; the audio is the audio clip's track for the whole span, uncut, so switching angle never
   touches the sound. No colour grading (log footage stays log; grading is a separate step, not in scope).
9. **CLI:** `ghostcut angles --sync sync.json --words words.json --out angles.json [--audio LABEL]
   [--render cut.mp4] [--min-shot 2.0] [--model muse-spark-1.3] [--no-muse]`. `--audio` defaults to the
   reference clip.

## Tests (offline, no media, no network, no guestlist)

Line splitting, coverage, cut points between words, same-camera merge, min-shot merge, the rule fallback, Muse
replies that name a non-covering or unknown camera, the schema's camera enum per batch, and the ffmpeg render
command built as a list (not run). Copy the style of `tests/test_sync.py`. The full suite must stay green apart
from the 4 failures that already fail on main (3 Pillow-missing in `test_render_verify`, 1
`test_render_plan.MakePlan.test_card_refusals`).

## Limits

- Core stays dependency-free; guestlist is imported inside the function that calls Muse, guarded.
- Do not change `sync.py` behaviour.
- Never call the Meta API from tests. Do not run live Meta calls while building; the owner runs the live test.
- No client names, people's names, footage paths or keys in code, tests or docs.
- Do not commit, push or switch branches. The owner reviews and commits.
