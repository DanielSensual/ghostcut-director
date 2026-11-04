# Brief: voice notes that land on a real cut (`voice` fixes)

Written 2026-11-03 by the project owner with Claude Code for Meta's Muse Code agent, after testing voice notes on
the owner's final take. Build straight from this brief on Muse Spark. Read `AGENTS.md`, `docs/muse-code/OPEN-BRIEF.md`
(its Rules apply), `docs/muse-code/VOICE-DESIGN.md`, `docs/muse-code/TIGHTEN-DESIGN.md` §5, then
`src/director/voice.py`, `src/director/cutlist.py`, `app/cutjob.py` and their tests.

## What happened (real job, read-only: `/Volumes/Extreme SSD/Meta-Hackathon-2026-11-03/final/job/`)

`director voice note.wav --job <that folder> --yes`, notes spoken in plain English:

1. "At the very start, where I say my mic, my drone has no microphone, use the drone camera."
   Heard correctly ("Use the drone camera at the very start…") but: "I couldn't place that note in the cut."
   Expected: `set_angle` to camera C on the beat/shot that opens the cut (output time 0), or on the line containing
   the quoted words. A first-line "My mic" lasts 0.7 s and was merged into the next shot by the minimum-shot rule;
   the override must survive that (the merged shot takes the override's camera, or the override applies to the
   whole opening shot).
2. "Cut both places where I mention B-roll." (and earlier: "cut the part where I say it can add B-roll")
   Heard only "Cut the first place…", then failed: "Drop span: No kept span in this cut." The two phrases are inside
   longer kept spans (beat "What Ghost Cut Director does": "It can also add B-roll, roll," and beat 5: "get B roll
   in"), so `drop_span` (whole spans) cannot express it. Before that, with one ambiguous mention, it correctly asked
   which one; with "both", it must take both.

## The job

1. **A phrase-level cut.** Add a `drop_words` operation to `cutlist` (word index range inside a kept span; edges go
   between words with the existing head/tail pads and edge-silence rule; the kept span splits around it; a matching
   `restore_words`). It goes through `apply_operations`, the same checks (no cut inside a word, no kept piece shorter
   than the minimum, no beat emptied silently), and `recompute_spans`. `angles` already renders whatever kept spans
   say.
2. **Resolve quoted words to ranges.** When a note quotes or paraphrases words ("where I say it can add B-roll",
   "where I mention B-roll"), find every matching word range in the kept transcript (normalised, tolerant of "B roll"
   vs "B-roll"); "both", "all", "every" take all matches; a single ambiguous match still asks.
3. **Anchor angle notes.** "At the very start / the opening / the first shot", "at the end", quoted words, and spoken
   timecodes resolve to a beat (and its shots) for `set_angle`; make the override hold through the minimum-shot merge.
4. **Check on the real job.** Run both notes above against a COPY of the job (copy the JSON files you need into
   `/tmp/voicefix-job/` and point `--job` there; never write into the real job), with `--apply`, and report the
   resulting `tighten.json` edits and the shots `angles` would produce (you may run `director angles` on the copy
   without `--render`). Your sandbox may block network: if Muse calls fail, unit-test the resolver with the
   transcripts above and say so.

## Rules

`OPEN-BRIEF.md` "Rules (hard)". Tests offline with stand-in transcripts and Spark replies built from the two real
notes above: drop_words split and restore, the checks, quoted-phrase matching with "both", the ambiguous single
match, the opening-shot angle anchor surviving the merge. Never call the Meta API or Whisper in tests. Add a
VOICEFIX section to `docs/muse-code/BUILD-LOG.md`.
