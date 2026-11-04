# Brief: Ghost, first live run (offload fixes)

Written 2026-11-04 by the project owner with Claude Code for Meta's Muse Code agent, after trying Ghost in the app
with the owner's two Sony cards mounted. Build it straight from this brief on Muse Wonder. Read
`docs/muse-code/OFFLOAD-BRIEF.md` (its rules apply) and your OFFLOAD section of `docs/muse-code/BUILD-LOG.md`.

## What happened

Typed: "The footage is on the SD card, transfer it to my SSD". After about 25 s Ghost answered "Found 2 cards
(Untitled, Untitled 1) — here is the transfer to plan." The plan: 4 clips (10.13 GB, 2026-10-29) + 111 clips
(79.43 GB, 2026-10-19 – 2026-11-03), to `<SSD>/2026-10-19 Shoot`, 89.56 GB.

## Fix

1. **Today's shoot, not the whole card history.** Cards hold weeks of old clips. By default plan only the most recent
   shoot day found on the cards (local date of the recorded time), name the folder by that day, and say what was left
   out on the plan card: "Also on the cards: 106 older clips, Oct 19 – Oct 29 (not copied)" with an **Include them**
   button. Honour a date or "everything"/"all of it" in the request (rule parser and the Spark intent). A card with no
   clips from that day is listed as skipped, not copied. Ghost's sentence names the day and the camera(s):
   "Found today's shoot (Nov 3): 5 Sony clips, 9.8 GB on Untitled 1."
2. **The Transfer button is unreadable**: grey text (`rgb(138,147,168)`) on the orange primary background. A more
   specific rule overrides `.primary`'s colour inside the plan card; make primary buttons there use the primary text
   colour, and check the other new buttons (Change, Include them, Make the cut, Open job) the same way.
3. **Answer fast.** Scan the mounted cards in the background when the Import tab opens (and when a card appears), cache
   each card's scan (keyed by volume and its clip files' names, sizes and mtimes), and show a live "Scanning Untitled
   1 · 40/111" line until it is ready, so the reply to a request is near-instant. Re-scan only what changed.

Tests offline: latest-day default, "everything" and a named date, the left-out summary, the skipped card, the scan
cache hit/miss. Keep the suite green. Add to your OFFLOAD section of `BUILD-LOG.md`.
