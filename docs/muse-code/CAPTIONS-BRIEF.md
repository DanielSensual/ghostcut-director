# Brief: burned-in captions and a cinematic title (`captions`)

Written 2026-11-03 by the project owner with Claude Code for Meta's Muse Code agent. Small enough to build straight
from this brief on Muse Spark. Read `AGENTS.md`, `docs/muse-code/OPEN-BRIEF.md` (its Rules apply), then
`src/director/cut.py`, `cutlist.py` (source-to-output time mapping), `tighten.py`, `transcribe.py` (word fields
`speaker`, `heard_by`, `disfluency`), `finish.py`, `reframe.py`, `export.py` and `cli.py`.

## Why

Most of the owner's deliveries are Reels and Shorts, and every one needs burned-in captions and a title hook. Today
they are done by hand. Director already has exact word times on the shared timeline, the verbatim Muse Voice
Transcribe text, and the take picks that say which words survive into the cut. The owner has a house caption style
(below); build to it, not to a generic look.

## The job

1. A `captions` stage in `director cut` after `reframe` (and a `director captions` command for an existing job).
2. **Words on the output timeline:** only words inside kept spans, mapped through the tighten/cut mapping (identity
   when there is no `tighten.json`). Leave out words only Muse heard (`heard_by: "muse"`) and fillers marked as
   disfluencies: captions show what the viewer hears, cleanly. Keep the word list and the line timing from the same
   words file, or the highlight drifts.
3. **Lines:** 2–4 words, never across a cut point, a removed span or a pause over 0.6 s; each line from its first
   word's start to its last word's end (at least 0.4 s); sentence case straight from the words, trailing "." and ","
   stripped.
4. **House style, preset `house` (the default), 1080×1920:** ASS through libass.
   `Style: Cap,Poppins SemiBold,82,&H00FFFFFF,&H00FFFFFF,&H4C000000,&H78000000,0,0,0,0,100,100,0,0,1,5,0,2,60,60,780,1`
   plus `{\blur7}` on every event: a wide, soft, ~70% dark outline that reads as a halo on white walls and sky.
   Bottom-centre, baseline about y = 1140. **Active word:** one Dialogue event per spoken word, each carrying the
   line's full text with only that word wrapped `{\c&H00A4D9F3&}…{\c&H00FFFFFF&}` (champagne `#F3D9A4`). Change
   colour only, never size or weight (a scaled word reflows the line and makes it jitter). Horizontal (1920×1080):
   the same style scaled to the frame, lower third. A second preset, `bold` (Montserrat-style ExtraBold caps, 3–4
   words, for energetic promos), is enough; no more.
5. **Fonts:** the repo ships `assets/fonts/Poppins-SemiBold.otf` (SIL OFL, licence in `assets/fonts/OFL.txt`).
   Point libass at it with `subtitles=filename=…:fontsdir=…` so a fresh clone looks the same; never depend on what's
   installed. A `--fonts-dir` flag can add the editor's own font folder.
6. **Cinematic title (the hook), 0–3.5 s:** a serif, sentence-case title over a soft top gradient, auto-fit to 960 px
   wide, up to 108 px, at most two lines, starting at y = 262 on 9:16 (scaled for 16:9). Rendered with PIL as an RGBA
   PNG and overlaid before the subtitles filter, so captions sit above it. Font: Playfair Display SemiBold when the
   editor has it (search `--fonts-dir`, `~/Library/Fonts`, `/Library/Fonts`), else Georgia Bold, else the shipped
   Poppins; never ship Playfair (its licence reserves the name). Text: `--title "…"`, or, with `--title auto`, one
   Muse Spark call (structured output, `muse-spark-1.3`) that reads the first ~60 s of the cut's transcript and
   proposes a 5–8 word hook taken from what the speaker actually says, with a reason; the mechanical check rejects any
   title with words the speaker didn't say. `--no-title` skips it. Estimate and count the spend like other Spark calls.
7. **Outputs:** `captions.srt` and `captions.ass` in the job folder (SRT imports into Final Cut and Resolve). Burn
   into `vertical.mp4` by default (re-encode video only, copy audio); into the horizontal master only with
   `--captions-horizontal`. Keep the un-captioned files as `*.clean.mp4`.
8. `cut.json` records the stage; `--no-captions` skips it; fingerprints follow the existing pattern; QC checks that
   captions exist and every caption lies inside the video's duration.

## Rules

`OPEN-BRIEF.md` "Rules (hard)". Tests offline: grouping and timing on synthetic words, the output-time mapping with a
synthetic `tighten.json`, SRT/ASS text (style line, blur, per-word colour events), the title auto-fit, the title
check (rejects unspoken words), a stand-in Spark for `--title auto`, the ffmpeg command shape, and one
`skipUnless(ffmpeg)` test that burns captions and a title onto a short `lavfi` clip and checks the output duration
and that frames differ where they show. No Meta API, no Whisper, no media committed. Commit `assets/fonts/` with your
work. Add a CAPTIONS section to `docs/muse-code/BUILD-LOG.md`, a README line and a "Who built what" row.
