# Brief: pick the takes, lose the flubs (`tighten`)

Written 2026-11-03 by the project owner with Claude Code for Meta's Muse Code agent. Muse Wonder writes the design
(`docs/muse-code/TIGHTEN-DESIGN.md`), then Muse Code builds it on Muse Spark. Read `AGENTS.md`,
`docs/muse-code/OPEN-BRIEF.md` and `docs/muse-code/OPEN-DESIGN.md` first. `OPEN-DESIGN.md` §15 already names where
this goes: a stage between `words` and `angles` in `director cut`, writing a `tighten.json` that `angles` consumes.

## Why

A talking recording is never the video. The speaker restarts sentences, says "again" or "let me do that one more
time", does two or three full passes, pauses to think, talks to the crew, checks the light. Today `angles` cuts the
whole shared timeline under the uncut audio, so every flub and every pass ends up in the cut. The owner's own
hackathon demo (a 3-minute founder talk, 2–3 full passes on 2–3 cameras) cannot be cut by Director without this.
Choosing which take to keep is also where an editor's taste lives, and it is the single slowest part of the owner's
week.

## What exists to build on

- The shared-timeline words from `director cut`'s words bridge.
- `docs/DIRECTOR-PLAN.md` and `docs/spike-takes-day1*.json`: the day-1 spike. Muse Spark given numbered Whisper lines
  and asked to pick keeper lines scored recall 0.85 / precision 0.94 against hand picks, with edges 0.1–3.5 s;
  asked for its own times it scored 0.76 / 0.74. Its failure mode: a coarse Whisper line that spans a flub. Use what
  the spike learned; the spike script is in `scripts/director_spike_takes.py`.
- `angles.py` renders shot by shot and joins the parts under one audio track; read how before deciding how a
  tightened cut renders its audio.

## The job

1. Group the recording into what was said: each beat or sentence, with every attempt at it (full passes, restarts).
2. Pick one attempt per beat, with a one-line reason an editor would accept ("clean delivery", "second pass, first
   one stumbles on 'timecode'"). Mark flubs, restarts, crew talk and dead air as removed, each with a reason.
3. Inside a kept attempt, shorten pauses longer than a limit (a flag) without clipping breaths or word tails.
4. Write `tighten.json`: the ordered kept spans on the shared timeline, the removed spans with reasons, the groups,
   the picks and the alternates, so a human (or the voice-notes feature in `VOICE-BRIEF.md`) can swap an alternate
   in later without re-asking the model.
5. Render: `angles` (or `cut`) uses the kept spans so picture and audio are both cut, with every edge between
   words, short audio crossfades at joins so there are no clicks, and the cut stays frame-accurate.
6. Optional target length (`--target 3:00`): when the kept cut is longer, ask which whole beats to drop, never
   mid-sentence; report what was dropped.

## Meta models

Muse Spark with structured output picks takes. Decide what it should see: numbered lines (the spike's best mode),
the audio of competing attempts (delivery, stumbles Whisper smoothed over), frames (eyes on lens, a sneeze). The
mechanical rules overrule it: no cut inside a word, no kept span shorter than a readable minimum, no beat dropped
silently, spend estimated and capped. A rule fallback (`--no-muse`) keeps the last full pass of each beat.

## Rules

Everything in `OPEN-BRIEF.md` "Rules (hard)" applies. Tests use stand-in Spark replies and synthetic words; no
media, no network. Add a TIGHTEN section to `docs/muse-code/BUILD-LOG.md` and a README row.

## What done looks like

`TIGHTEN-DESIGN.md` with data contracts (`tighten.json` with example values), the Spark prompt and schema, the
grouping algorithm, the pause rule, the render change, how `cut` and `angles` consume it without breaking a run that
has no `tighten.json`, failure cases and a numbered test list. Then the build, full suite green.
