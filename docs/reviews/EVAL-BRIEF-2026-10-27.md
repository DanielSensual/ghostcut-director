# GhostCut Director: independent evaluation brief (2026-10-27)

> **Note (2026-10-30):** the review covered `tools/handoff/`, exporters copied in from two client jobs. They are
> not part of Director and were kept out of the public repository. The reviewers' raw notes named here were not
> committed.

Two outside reviewers, then the notes come back to Claude Code for final revisions:
- **Codex** (has this repo): code + plan review. Prompt A.
- **ChatGPT Astra** (no repo): strategy + plan review from attached files. Prompt B.

Bring both outputs back as files in `docs/reviews/` (`codex-eval-2026-10-27.md`, `astra-eval-2026-10-27.md`).

---

## Prompt A: Codex (paste into Codex, started in the repo root)

```text
You are an independent evaluator of GhostCut Director, a hackathon entry. You did not build it. Your job is to find what is wrong, missing, or overclaimed before the deadline, not to praise it. Be specific and blunt.

READ-ONLY. Do not edit, create, delete, commit, or push anything. Do not call any paid API (no Meta Model API, OpenAI, Gemini, ElevenLabs). Do not open .env files or print keys. You may run local, offline commands: reading files, `git log`, `python -m py_compile`, the test suite (`pip install pytest` into .venv first if missing), and `--dry-run` modes that write nothing.

CONTEXT
- Event: Meta Global AI Developer Hackathon (Meta + Cerebral Valley). Entry: STARTUP track, as Ghost AI Systems (solo founder). Submission due 2026-11-04 9:00 PM ET. Today is 2026-10-27 (Day 1 of 9).
- Startup judging: Impact 30% (real-world use, fits theme "Build for Your World"), Use of Meta AI tooling 25% (beyond a basic integration), Demo 25% (works live, video quality), Technical depth 20%. Startup judges weigh technical execution and "holds up in production".
- GhostCut existed before the event (public since 2026-07-27); only the Meta-powered Director layer is new. The submission must disclose that.
- Product: a production company records on set; the director speaks slate calls into a lav mic ("Cam A, scene 2, take 3, keeper", "cut, restart"). GhostCut ingests the card, Whisper transcribes, Muse Spark (video+audio in) organizes takes using the slates, GhostCut cuts, Ghost Captions adds captions, and the agent packages an editable timeline for the client's editor of choice (Final Cut, Resolve, Premiere, CapCut, later Avid), then applies client revision notes.

READ FIRST
1. docs/DIRECTOR-PLAN.md (plan, day schedule, Day-1 spike results). Note: it still says "Indie track" and predates the spoken-slate, captions, revision-loop and multi-editor ideas above.
2. docs/spike-takes-day1*.json and scripts/director_spike_takes.py (the measured Spark take-log spike).
3. tools/handoff/README.md, tools/handoff/capcut_draft_from_fcpxml.py, tools/handoff/nle_xml_export.py (exporters copied in today from two client jobs, not yet wired into the package).
4. src/ghostcut/export_otio.py, export_fcpxml.py, cli.py, models.py (what the package already does).
5. README.md, AGENTS.md, CLAUDE.md.

EVALUATE
1. Plan vs judging: does the plan score well on each of the four criteria for the STARTUP track? What would a judge see as a "basic integration"? What is the single biggest gap?
2. Scope vs time: is the 9-day plan realistic for one person? What should be cut, merged, or reordered? Name a must-ship core and a stretch list.
3. The spike's evidence: are the recall/precision numbers measured honestly (method, ground truth, leakage, sample size of one recording)? What claims would be overclaiming in the submission?
4. Spoken slates: design risks (vocabulary, Whisper mishearing, multi-camera sync, slates missing), and how to evaluate them with a number.
5. Exporters: correctness bugs, hidden project-specific assumptions the README missed, anything still identifying a client (names, paths, IDs), and the cleanest way to make OTIO the single source of truth with per-editor packages. CapCut's draft format is undocumented: judge the risk.
6. Production-readiness for the Startup track: fresh-clone install, failure modes, cost controls, what breaks on someone else's Mac.
7. Repo hygiene for a PUBLIC repo: secrets, personal paths, client data, license issues (SAM License, model terms).
8. Test status: run the tests and report pass/fail counts exactly.

OUTPUT (markdown, this exact structure)
## Verdict
One paragraph: ship-ready, needs work, or wrong direction, and why.
## Findings
A table: id | severity (blocker/high/medium/low) | area | finding | evidence (file:line or command output) | fix | confidence (high/med/low). Most severe first. Every finding needs evidence.
## Cut / keep / add
Three short lists for the 9-day plan.
## Questions for Daniel
Only decisions that are his to make.
## What you ran
Every command and its result summary.
```

---

## Prompt B: ChatGPT Astra (attach `docs/DIRECTOR-PLAN.md`, `tools/handoff/README.md`, and this file)

```text
You are an independent evaluator for a hackathon entry called GhostCut Director. You did not build it. Find what is weak, missing, risky or overclaimed before the deadline. Be specific and blunt; do not praise. Do not write code.

CONTEXT
- Event: Meta Global AI Developer Hackathon (Meta + Cerebral Valley), fully virtual. Entry: STARTUP track as Ghost AI Systems, a one-person AI company in Orlando that is also the software arm of MediaGeekz, a 6-year video production company filming realtors, YouTube and events. Prize ladder: $250k / $150k / $75k / $50k / $25k, plus bonus awards. Submission due 2026-11-04 9:00 PM ET. Today is 2026-10-27 (Day 1 of 9).
- Startup judging: Impact 30% (real-world use; must fit "Build for Your World" or "Push the Frontier"), Use of Meta AI tooling 25% (beyond a basic integration, surfaces unexpected capabilities), Demo 25% (works live, demo video quality), Technical depth 20%. Startup judges weigh technical execution and whether it holds up in production.
- Meta tools available: Muse Spark 1.3 (video, audio, image, PDF in; tools; structured output), Muse Wonder, Muse Image, Voice Transcribe (diarization, turn-level timestamps only), SAM 3.1 (segment + track people/objects in video), SAM Audio (open weights, gated, needs GPU), Muse Code (Meta's coding agent). $150 API credit total.
- GhostCut existed before the event; only the Director layer is new, and that will be disclosed.
- Product idea (newer than the attached plan): the director speaks slate calls into a lav mic on set ("Cam A, scene 2, take 3, keeper"; "cut, restart"). GhostCut ingests the card; Whisper transcribes; Spark organizes takes using the slates plus what it sees and hears; GhostCut cuts; captions are added; the agent packages an editable timeline for the client's editor (Final Cut, Resolve, Premiere, CapCut) with instructions their own AI agent can follow; client revision notes become small timeline changes and re-renders.
- Measured so far (one 22-min talking-head recording vs a hand log): Spark picking numbered Whisper lines got recall 0.85 / precision 0.94 on take seconds, edges 0.1–3.5 s on 8 of 11 takes, for $0.17.

EVALUATE
1. Is this the right entry to win the STARTUP track? Score it 1–10 against each judging criterion as a skeptical judge would, with the reason for each score.
2. The pitch: who exactly is the customer, what is the one-sentence value, and what claim would a judge disbelieve? Rewrite the one-line pitch.
3. Use of Meta tooling: what would look like a "basic integration", and which use of Spark/SAM/Voice Transcribe would most surprise a Meta judge? Rank options by impact vs effort for one person in 8 days.
4. The demo video (under 3 minutes): give a shot-by-shot outline that proves it works on real footage and holds up in production.
5. Scope: a must-ship core, a stretch list, and what to cut. Is the day plan realistic?
6. Evidence and honesty: which numbers need more samples, a held-out test, or a different metric before they go in the submission? What must be disclosed?
7. Risks: legal/IP (client footage, the organizers' perpetual license over submissions, model licenses), technical (CapCut's undocumented project format, SAM Audio access), and "solo founder" credibility.

OUTPUT (markdown, this exact structure)
## Verdict
## Scores (criterion | score | reason)
## Findings (id | severity: blocker/high/medium/low | finding | why it matters | fix)
## Pitch rewrite
## Demo outline
## Cut / keep / add
## Questions for Daniel
```

---

## When the notes come back (for Claude Code)

Merge both into one ledger: each finding marked **accept / reject (with evidence) / Daniel decides**. Verify every
code claim against the files before acting. Then update DIRECTOR-PLAN.md (Startup track, spoken slates, captions,
revision loop, multi-editor packages) and the day plan. Nothing is pushed without Daniel's OK.
