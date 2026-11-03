# Brief: connections and finishing (`director connect`, `finish`)

Written 2026-11-03 by the project owner with Claude Code for Meta's Muse Code agent. Muse Wonder writes the design
(`docs/muse-code/FINISH-DESIGN.md`), then Muse Code builds it on Muse Spark. Read `AGENTS.md`,
`docs/muse-code/OPEN-BRIEF.md` (its Rules apply), `OPEN-DESIGN.md`, `TIGHTEN-DESIGN.md`, `TRANSCRIBE-DESIGN.md` and
`VOICE-DESIGN.md` first. Voice notes are being built right now in another checkout; treat `VOICE-DESIGN.md` as the
contract for the app's cut-job view and `src/director/voice.py`.

## Why

`director cut` stops at picture. The owner still finishes every job by hand: cleans and levels the voice, masters
loudness, adds a music bed, a riser into a section, a whoosh on a title. On today's real shoot the lav recorded at
−32 to −41 dB RMS (it was hand-held): usable, but nothing in Director brings it to a publishable level. Editors also
already pay for generation services (ElevenLabs for sound effects, music and voice isolation; Higgsfield for
effects) and want Director to use them with their own keys.

## The job

1. **Connections.** A way for the editor to add, test and remove their own API keys for outside services:
   - `director connect` on the command line (list, add, test, remove), and a Connections panel in the Mac app
     (paste a key, Test, Remove; never shows a saved key back).
   - Keys stored securely on the editor's own machine: the macOS Keychain through the `keyring` package (an optional
     extra), with environment variables taking precedence (`ELEVENLABS_API_KEY` and the like). Never in the repo, in
     job files, in `cut.json`, in logs, in URLs or in error messages. The app's engine only listens on 127.0.0.1.
   - Design it as a small provider registry so a service is one module: name, key env var, a cheap test call, and the
     capabilities it offers.
   - **ElevenLabs** is the first provider (sound effects, music, voice/audio isolation). **Higgsfield** second: you may
     read Higgsfield's official public documentation on the web to see whether it has a key-based HTTP API for the
     capabilities that fit (effects, captions, upscale); if it does, design that provider; if it only offers MCP or the
     API is unclear, design the registry entry and a clear "not available yet" path instead of guessing endpoints.
   - You may read ElevenLabs' official API documentation on the web to get endpoints, parameters, limits and pricing
     right. Read nothing else outside this repository and its installed packages.
2. **Finish** (a stage in `director cut` after `angles`, before `reframe`, and a `director finish` command):
   - **Clean and level the voice** with a local ffmpeg chain that needs no key (high-pass, gentle noise reduction,
     compression), or ElevenLabs audio isolation when connected and asked for.
   - **Master** to a loudness target (default −14 LUFS integrated, −1 dBTP; a flag for −16), two-pass, measured.
   - **Sound design** (only with a connected provider, off unless asked or set in the taste file): Muse Spark decides
     where sound belongs by reading the cut (beats, sections, shot changes, the transcript) and, where it helps, a proxy
     of the cut; it proposes a music bed (mood, length, where it drops out) and a few effects (a riser into a section,
     a soft whoosh on a title, an impact on the cold open), each with a reason; mechanical rules overrule (never under
     a spoken word unless ducked, never more than N per minute, never in the first or last second unless asked). The
     provider generates them; Director mixes them with sidechain ducking under the voice and masters the result.
   - The finished mix replaces the audio of `cut.mp4` and feeds `vertical.mp4`. Every generated asset is saved in the
     job folder with its prompt, provider, cost and licence note, so the editor can reuse or replace it in Final Cut
     (the timeline export should carry the music and effects on their own audio lanes if the installed exporters
     allow; say so if they don't).
   - Spend: estimate before calling any provider (credits or dollars, whatever the provider bills), count it in
     `cut.json`, refuse above `--max-usd` or a provider-specific cap.
3. **Taste.** The voice-notes taste file can say "no whooshes", "music low", "always master to −16": the finish stage
   reads it.

## Rules

`OPEN-BRIEF.md` "Rules (hard)" apply. Tests are offline: fake HTTP for every provider, a fake keyring, synthetic audio
from ffmpeg `lavfi` when a test needs a file. Never call ElevenLabs, Higgsfield or the Meta API from tests. No key, no
real recording and no generated asset is ever committed. The README section says plainly that generated audio is
subject to the provider's own terms. Add FINISH sections to `docs/muse-code/BUILD-LOG.md` and README rows.

## What done looks like

`FINISH-DESIGN.md`: the provider registry and key storage (with the exact security rules and failure messages), the
`connect` command and the app panel (engine endpoints, UI), the finish stage (ffmpeg chains, the Spark prompt and
schema for sound placement, the mechanical rules, the mix and master, the files written, how `cut`, `angles`,
`reframe` and the exporters consume it), how it degrades with no keys, spend handling, and a numbered test list.
Scope it so it builds in one Muse Spark run after voice notes; if it cannot, split it into two builds and say which
comes first.
