# Brief: talk to Director (voice notes that edit the cut)

Written 2026-11-03 by the project owner with Claude Code for Meta's Muse Code agent. Muse Wonder writes the design
(`docs/muse-code/VOICE-DESIGN.md`), then Muse Code builds it on Muse Spark. Read `AGENTS.md`,
`docs/muse-code/OPEN-BRIEF.md` and `docs/muse-code/OPEN-DESIGN.md` first: this feature sits on top of whatever the
open brief produced, and must work with it.

## Why

Editing for realtors is taste work. The editor watches the cut and reacts: "that pause is too long", "go to the
wide when she points at the kitchen", "lose the part where the crew is talking", "the music is too loud under her
voice", "this client hates jump cuts". Typing every one of those is slow; saying them is how an editor works with
an assistant editor. The owner films six days a week and edits at night. He wants to talk through a cut once, at
full speed, and come back to a cut that has already taken his notes.

## The job

Two ways to give notes by voice, one result: a list of concrete, checked edit proposals the editor can accept,
reject or accept all, and then render.

1. **Talk while watching.** In the app, hold a key (or press a mic button) while the cut plays and say a note.
   Record the playhead time while each note is spoken, so "cut this", "here", "that shot" resolve to the moment on
   screen. Several notes in one viewing are normal.
2. **One long note.** The editor records a long voice memo or a video of themselves (phone, laptop, any length up to
   about 20 minutes) talking through the whole cut, and drops the file on the app or passes it to a command. The
   notes may refer to timecodes ("at about a minute twenty"), to what is said in the video ("where I say 'three
   cameras'"), to shots ("the iPhone angle"), or to the whole video ("tighten every pause over a second", "make it
   under three minutes").

For both: transcribe, split into separate notes, resolve each note to the timeline (from playhead times, spoken
timecodes, or by matching quoted words against the cut's transcript), turn each into the same proposals the typed
Muse instruction already makes or the new ones the open feature adds, run every existing check, and show them
grouped by note with what was understood, so the editor can see "I heard: ...". A note Director cannot do says so
plainly instead of guessing.

Persist the editor's standing preferences ("this client hates jump cuts", "always open on the wide") as a small
taste file per project or client that later Muse calls read. Show it, and let the editor remove an entry.

## Meta models

- **Voice Transcribe** (`muse-voice-transcribe-1.0`, through `guestlist.meta.transcribe.diarize`) for the notes:
  read that function first. It has no word timestamps (turn-level only); for talk-while-watching the playhead time
  per recording is the anchor, which is why it is recorded.
- **Muse Spark** (`muse-spark-1.3`, structured output) to split and resolve notes into operations. Give it the cut's
  transcript with times, the shot list, the taste file and the note. Where seeing the frame matters ("when she points
  at the kitchen"), it may look at stills from the cut.
- A local Whisper fallback is allowed when the Meta key or `guestlist` is missing, behind a clear message.
- Spend: show the estimate before transcribing a long note; refuse above `--max-usd`.

## Recording in the app

The app is a pywebview window (WKWebView on macOS) served by `app/engine.py`. Microphone access from a WKWebView
inside a plain Python process may be refused by macOS. Design for that: the engine can also be opened in a normal
browser at its localhost address, where `getUserMedia` + `MediaRecorder` work; detect a refused microphone and say
how to proceed (open in browser, or drop a voice memo file). Uploads go to the local engine only.

## Command line

A command (name it) that takes a voice/video note file and a project or cut, prints what it heard and the
proposals, and with a flag applies the ones that pass every check. This is how a stranger tries it without the app.

## Rules

Everything in `OPEN-BRIEF.md` "Rules (hard)" applies. In particular tests are offline: no Meta API, no Whisper,
no microphone. Use stand-in clients that return fixed transcripts and fixed Spark replies, and synthetic `lavfi`
media only when a test truly needs a file. Add a VOICE section to `docs/muse-code/BUILD-LOG.md` and a README row.

## What done looks like

- `VOICE-DESIGN.md`: data contracts (a note, a resolved note, the taste file), the Spark prompt and schema, how
  times are resolved and how ambiguity is reported, the engine endpoints and UI, the command, failure cases, and a
  numbered test list.
- The build: code, tests, docs and README, full suite green with
  `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`.
