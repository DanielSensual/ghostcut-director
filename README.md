# GhostCut Director

GhostCut Director is a Mac app and a `director` command line that cut multicam talking video: I give it a folder of camera cards, and it syncs the clips, picks the takes, grades the cameras to match, chooses an angle for every spoken line, finishes the audio, and hands back a horizontal cut, a vertical cut, editable timelines, and a written record of every choice.

## Try it in two minutes

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[director]"
director demo --out-dir director-demo
```

You need Python 3.11 or newer and `ffmpeg`/`ffprobe` on your `PATH`. No footage and no API keys are needed.

`director demo` makes a small synthetic shoot in `director-demo/shoot/` (a lav track and three cameras at known offsets, one of them silent, with a deliberate restart in the script), runs the real `director cut` pipeline on it offline, and checks its own work. You will see the sync offsets against the known ones (pass within one frame), how many seconds the restart removal took out, and where to look next: `director-demo/job/cut.mp4`, `director-demo/job/vertical.mp4`, `director-demo/job/timeline.fcpxml`, and `director-demo/job/cut.json`. It exits non-zero if a check fails. `director demo --muse` runs the same shoot with Muse and SAM when `MODEL_API_KEY` is set; `--keep` reuses shoot media already in the output folder.

Demo video (3 minutes): VIDEO_LINK_HERE — placeholder, the link goes here.

## Built with Meta's AI

Most of this repository is work that Meta's Muse Code designed and built during the event; the Director app and its render checks were built by Claude Code with me (see Who built what). The pattern for most features was: I and Claude Code wrote a brief, Muse Code on `muse-wonder-1.0` wrote the design, and Muse Code on `muse-spark-1.3` wrote the code and tests. Sections below cite [docs/muse-code/BUILD-LOG.md](docs/muse-code/BUILD-LOG.md) for every number.

- **Muse Code chose `director cut` itself.** The brief for that piece ([docs/muse-code/OPEN-BRIEF.md](docs/muse-code/OPEN-BRIEF.md)) listed problems an editor hits and left the choice of what to build open. Muse Code chose the one-step `director cut` command — folder in, finished job out — and designed it in `OPEN-DESIGN.md` before building it (BUILD-LOG.md, "Build log: `director cut` (2026-11-03)").
- **Muse Spark inside the product.** Spark picks takes (`tighten`), picks the camera for each line (`angles`), proposes the title hook (`captions`), places sound in `finish`, resolves voice notes, and watches a proxy of the finished cut for the whole-cut QC pass. The mechanical checks run after it and can overrule it; every overrule is recorded (BUILD-LOG.md, "Build log: `ghostcut angles` (2026-11-01)"; "Build log: take picking (`tighten`) (2026-11-03)").
- **Muse Voice Transcribe.** The production audio is transcribed with Voice Transcribe, which keeps repetitions and fillers (`will will`, `um`) and labels speakers. Whisper still supplies the word times; Voice Transcribe supplies the text and the speakers, and the two are aligned (BUILD-LOG.md, "Build log: Voice Transcribe transcript (`transcribe`) (2026-11-03)").
- **SAM 3.1.** The vertical reframe tracks people with SAM 3.1 through the Meta Model API and follows the speaker in each shot (BUILD-LOG.md, "Build log: `director reframe` (2026-11-02)").

What Muse Code cost, at list rates with cached input at the cached rate where the log says so: sync about $5.19 in total ($2.11 design + $2.07 build + $0.67 scoring fix + $0.34 drift fix; BUILD-LOG.md, "`director sync` (2026-11-02)"), reframe about $2.34 ($1.46 + $0.88; BUILD-LOG.md, "`director reframe` (2026-11-02)"), angles at most about $6.20 and grade at most about $9.60, both counted with no cache discount (BUILD-LOG.md, "`ghostcut angles` (2026-11-01)" and "`ghostcut grade` (2026-11-01)"). The lip-reading sync method took 65 calls and 9.07M input tokens (8.84M cached) on `muse-spark-1.3` (BUILD-LOG.md, "Lips method").

The Meta calls go through the optional `guestlist` package: `pip install -e ".[muse]"`, key from the `MODEL_API_KEY` environment variable only. Everything else runs without it.

## What it does

One command does the whole job:

```bash
director cut /path/to/card-dump --out-dir job1
# or name the files:
director cut cam-a.mp4 cam-b.mp4 lav.wav --out-dir job1
```

Re-running the same command resumes: stages whose inputs did not change are skipped (`--force` re-runs everything). `cut.json` in the job folder records each stage, its spend, its warnings, and what QC found. `director cut --no-muse --no-sam` runs fully offline. The stages, in the order `director cut` runs them:

- **Sync** — every clip onto one shared timeline (`sync.json`) from timecode, capture clock, waveform, Whisper, light flash, and lip reading for cameras with no microphone; estimates are checked against each other before one is picked (`director sync`, `--methods` to choose a subset).
- **Transcribe** — the production audio transcribed by Muse Voice Transcribe (verbatim, speakers labelled `talent`, `other-1`, …) and aligned onto Whisper word times (`words.json` + `transcript.json`; `--no-transcribe` keeps Whisper words only).
- **Take picking** — one kept take per beat, restarts, flubs, crew talk and dead air listed as removed spans with reasons in `tighten.json`; pauses over `--pause-limit` (default 0.8 s) shortened to 0.35 s; `--target 3:00` drops whole beats only, never mid-sentence; `--no-tighten` keeps the whole recording.
- **Grade** — a per-camera `.cube` from the published S-Log3 and HLG curves, then exposure, white balance and contrast matched on skin inside the face box (whole-frame match with a warning when no face is found); cameras Director cannot read take a supplied LUT, e.g. `--lut 'DJI_*=dlog.cube'`; `--grade-reference 'DJI_*'` picks the camera to match to.
- **Angle per line** — the gaze rule first (the camera the speaker addresses, from Apple Vision yaw sampled about every 0.5 s — the helper's continuous landmark yaw when it reports one, the coarse yaw otherwise — wins the line, and a held turn of address inside a line cuts to the addressed camera on the nearest word gap), then Muse Spark from one frame per covering camera; mechanical checks overrule and are recorded in `angles.json`, each shot with one reason; shot-by-shot render under the production audio gives `cut.mp4`; `--push-in 3` to `6` adds a slow push-in on every shot.
- **Finishing** — the cut's voice cleaned locally (mic-air de-plosive on by default, `--no-deplosive` to skip; ElevenLabs isolation with `--finish-isolate` when connected), optional ElevenLabs sound design placed by Muse Spark with `--finish-sound`, ducked under the voice, mastered two-pass to −14 LUFS / −1 dBTP (`--finish-loudness -16` for the quieter target); `--no-finish` skips the stage.
- **Captions and title** — karaoke captions burned into `vertical.mp4` (house style by default, `--captions-preset bold` for caps promos; `--captions-horizontal` also burns `cut.mp4`), `captions.srt`/`captions.ass` written beside them, un-captioned files kept as `*.clean.mp4`, and a serif title hook over the first 3.5 s (`--title "…"` or `--title auto` for one checked Muse Spark proposal); `--no-captions` skips the stage.
- **Timelines** — the same shots as `timeline.fcpxml` for Final Cut Pro and `timeline.otio.json` for OTIO/Resolve tools, referencing the camera originals (`--no-export` skips). Two notes: the timelines open showing log/HLG picture — apply the per-camera `.cube` files from the job folder (`cut.json` says which cube belongs to which camera) — and each timeline clip carries its own camera's audio, so I re-lay the production audio at the offset in `angles.json`/`cut.json` (`audio.offset_s`); the rendered `cut.mp4`/`vertical.mp4` already have it laid correctly.
- **Vertical** — SAM 3.1 follows the speaker per shot for the 9:16 `vertical.mp4`, with a smoothed crop path in `reframe.json` (`--no-vertical` skips; `--no-sam` centres every shot).
- **Whole-cut QC** — mechanical checks (durations, black share, loudness, size) plus one Muse Spark pass over a proxy of the finished cut; findings land in `cut.json` (`--no-qc` skips).

The same stages run by hand, one command at a time — see the commands reference below.

## Tested on real footage

These numbers are copied from the live-test sections of [docs/muse-code/BUILD-LOG.md](docs/muse-code/BUILD-LOG.md). Two rows (the local master and the demo) are synthetic and say so; every other row is real footage.

| Check | What was measured | Result |
|---|---|---|
| Sync, long lav | 73.6 s camera clip against a 767 s lav recording | Placed at +178.746 s at its first frame (known answer +178.74 s); 132 ppm drift over 4 windows; Whisper agreeing to 6 ms |
| Sync, split clip | One 4K clip made into two cameras 6 s apart | +6.000 s, r 1.0 |
| Sync, repeated question | The same stock question in two different interviews | Correctly left unplaced in both pairs |
| Angles, A/V sync | Two-camera cut from one real 4K clip | Video 59.977 s, audio 59.976 s; a camera-B shot matches camera A at 0 frames offset (PSNR 46.8 dB vs 46.2 / 44.8 one frame either side) |
| Angles, Muse Spark | Same clip, 14 lines | 4 calls, $0.032, 5 shots; every reason describes the frame |
| Grade, match | Punch-in given a gain cast | Gains 1.32 / 1.23 / 1.11 against an ideal 1.28 / 1.18 / 1.06; matched frame 32.1 dB PSNR from the uncast original, up from 20.5 dB |
| Lips sync, silent drone | Mavic 3 at 45°, no audio, D-Log; its clock 476 s off | Placed by lips-vs-lips at +895.408 s, ratio 5.86 (r 0.926 over 104 s); lips-vs-audio read +895.475 to +895.478 s, and minus the 69 ms mouth-before-sound bias measured on the front camera they land at +895.409 s, 1 ms from the placement |
| Lips sync, front camera | A7S III facing the speaker (waveform truth +899.146 s, r 0.979) | Lips-vs-audio cross-check +899.215 s (ratio 1.17) |
| Reframe, SAM 3.1 | Real 60 s 4K talking cut, five shots, speaker off centre | 350 frames billed, $0.07; speaker followed in all five shots (presence 1.00); cuts within 1 ms of the earlier reframe; crop path within 1.9 px (mean 0.3 px); render 1080×1920, 3,595 frames, audio 59.976 s |
| De-plosive | The owner's take through the shipped finish chain | Threshold −36.8 dBFS, 52 moments; low band at the seven air peaks −9.6 to −14.2 dB (mean −11.6 dB) against −0.6 dB for normal speech; mastered −14.82 LUFS, final true peak −0.99 dBTP |
| Finish, local master | 6 s synthetic cut, no keys connected | −14.0 LUFS against a −14 target; an independent re-measure read −14.02 LUFS integrated |
| Demo, by hand | `director demo` on the synthetic shoot | PASS: offsets +1.000/+4.000 exact by waveform, silent camera clearly unplaced, 6.2 s removed, all nine stages done, finish at −14.0 LUFS |

## Built during the hackathon

[GhostCut](https://github.com/DanielSensual/ghostcut) existed before the event. It is a pinned dependency here — the commit from before the event plus its in-event export fixes (`be52bc8`, see `pyproject.toml`) — and I did not build it for this entry. Everything in this repository was built during the Meta Global AI Developer Hackathon, Oct 26 – Nov 4, 2026: the `director` command line, the Mac app in `app/`, the tests, the briefs and designs in `docs/muse-code/`, and this README.

Sync and reframe were also built earlier in the event by Claude Code, inside GhostCut, where they are still public. The versions in this repository are Muse Code's rewrites: it received only the briefs, never that code, and its run logs show no reads outside this repository and its installed packages. Both rewrites were checked against the same real recordings (BUILD-LOG.md, "Live test" under "`director sync`" and "`director reframe`").

### Who built what

From [docs/muse-code/BUILD-LOG.md](docs/muse-code/BUILD-LOG.md) and the git log, nothing else. "Designed" means the design document in `docs/muse-code/`; the briefs were written by me with Claude Code.

| Piece | Who |
|---|---|
| Angle choice (`ghostcut angles`, then `director angles`) | Designed by Muse Code on muse-wonder-1.0, built by Muse Code on muse-spark-1.3; reviewed and fixed by Claude Code |
| Per-camera grade (`ghostcut grade`, then `director grade`) | Designed by Muse Code on muse-wonder-1.0, built by Muse Code on muse-spark-1.3; reviewed and fixed by Claude Code |
| Multicam sync (`director sync`) | Designed by Muse Code on muse-wonder-1.0, built by Muse Code on muse-spark-1.3; four defects found on real audio by me and Claude Code and fixed by Muse Code |
| Light-flash sync (the `flash` method) | Built by Muse Code on muse-spark-1.3 from FLASH-BRIEF.md |
| Lip-reading sync (the `lips` method) | Brief and validated recipe by me with Claude Code; built by Muse Code on muse-spark-1.3 |
| SAM 3.1 vertical reframe (`director reframe`) | Designed by Muse Code on muse-wonder-1.0, built by Muse Code on muse-spark-1.3, live-tested by Claude Code |
| One-step multicam cut (`director cut`) | Chosen and designed by Muse Code on muse-wonder-1.0 from the open brief, built by Muse Code on muse-spark-1.3 |
| Take picking (`director tighten`, the `tighten` stage of `director cut`) | Designed by Muse Code on muse-wonder-1.0, built by Muse Code on muse-spark-1.3 |
| Voice Transcribe transcript (`director transcribe`, the transcribe step of `director cut`) | Designed by Muse Code on muse-wonder-1.0, built by Muse Code on muse-spark-1.3 |
| Voice notes (`director voice`) and the app opening cut jobs | Designed by Muse Code on muse-wonder-1.0, built by Muse Code on muse-spark-1.3 |
| Outside-service connections (`director connect`, the app's Connections panel) | Designed by Muse Code on muse-wonder-1.0, built by Muse Code on muse-spark-1.3 |
| Finishing (`director finish`, the `finish` stage of `director cut`) | Designed by Muse Code on muse-wonder-1.0, built by Muse Code on muse-spark-1.3 |
| Burned-in captions and the title (`director captions`, the `captions` stage of `director cut`) | Built by Muse Code on muse-spark-1.3 straight from CAPTIONS-BRIEF.md (no separate design) |
| Try it with no footage (`director demo`) | Built by Muse Code on muse-spark-1.3 from DEMO-BRIEF.md |
| Gaze angle choice and skin match (`look`) | Built by Muse Code on muse-spark-1.3 from LOOK-BRIEF.md, after my review of the final take |
| Gaze cuts inside a line (`angles` gazecut) | Built by Muse Code on muse-wonder-1.0 from GAZECUT-BRIEF.md, after my review of the app on submission day |
| Mic-air de-plosive and peak limiter (the `finish` de-plosive, `--no-deplosive`) | Built by Muse Code on muse-spark-1.3 from AIR-BRIEF.md |
| Slow push-in on every shot (`angles`/`cut --push-in`) | Built by Muse Code on muse-spark-1.3 from PUSH-BRIEF.md, after my review of the final take |
| Phrase-level voice-note cuts (`drop_words`, quote ranges, cut-edge anchors) | Built by Muse Code on muse-spark-1.3 from VOICEFIX-BRIEF.md, after my review of the final take |
| Card offload (`director offload`, Ghost on the Import tab) | Built by Muse Code on muse-wonder-1.0 from OFFLOAD-BRIEF.md, after the owner's review of the app on submission day |
| Voice notes without Muse (offline resolver, Whisper fall-through on a 402) | Built by Claude Code (Opus) after the Meta API credit ran out on submission day; see BUILD-LOG.md, "VOICE-OFFLINE" |
| The Director app, Render, and the test-run scripts in `scripts/` | Built by Claude Code with me (imported into this repository) |
| The briefs in `docs/muse-code/` | Me, with Claude Code |

## The Director app

The app is the same cut with a review step. Muse proposes timeline edits and I accept the ones I want; Render re-renders only what changed into the next version; about 35 checks run before a new version replaces the current one, and revert goes back to the previous version.

```bash
python app/main.py              # needs pywebview; starts the local engine and opens the window
python app/main.py --job job1   # open a finished director cut job
```

A finished `director cut` job opens directly: beats become sections, shots become talk clips, take picks fill the Why panel, and kept words become the Words tab. Reviewing takes and notes works even before the job has rendered. A project made by the Director test run (`scripts/`) also opens; this repository does not ship one.

I can also talk back to a cut. In the app I hold the mic button (or hold `V`) and say "use the other take here" or "tighten every pause"; from the command line, `director voice memo.m4a --job job1` does the same with a memo file (add `--apply`, then `--render` to re-render). Notes are anchored by playhead time, a spoken timecode, quoted words, or the opening/closing beat ("at the very start", "at the end"), and resolved into checked edit proposals — the same checks, the same apply path, the same render. A note can cut a phrase inside a kept span ("cut both places where I mention B-roll"); "both"/"all"/"every" takes every match, one ambiguous mention still asks. Standing preferences ("always open on the wide") persist per job in `taste.json` and are read on the next pick or note. The Mac window cannot reach the microphone, so recording there shows a banner pointing at the same page in a browser, where recording works.

## Import with Ghost

The Import tab is Ghost, the offload agent. I say (type, or hold the mic and speak) "the footage is on the SD card, transfer it to my SSD"; Muse Spark reads the request against the mounted volumes, and when no Meta key is present a rule parser handles the same phrasings, so the demo never depends on the network. Ghost answers with a plan card — each card found (camera guess, clip count, GB, recorded date range), the dated destination folder, and the space check — and copies nothing until I click Transfer (or say "yes, go"). By default only the most recent shoot day on the cards is planned ("Found today's shoot (Nov 3): 8 Sony clips, 22.0 GB on Untitled 1."); the plan card names what was left out ("Also on the cards: 107 older clips, Oct 19 – Oct 29 (not copied)") with an Include them button, a card with nothing from that day is listed as skipped, and naming a date or saying "everything" plans that instead. Cards are scanned in the background when the Import tab opens (cached by their files' names, sizes and mtimes), with a live "Scanning Untitled 1 · 40/111" line until each is ready. Copies stream through `*.part` files and are renamed only after a streamed checksum (xxhash when installed, otherwise BLAKE2b) verifies against the source; `offload.json` in the shoot folder records every source, destination, size, checksum and verified/skipped flag. The card is only ever read: nothing is written, moved or ejected there. Afterwards the shoot shows as per-camera bins with a thumbnail and duration per clip, and Make the cut runs the whole `director cut` job in the background and opens it in Story/Edit/Deliver. "Open yesterday's job" (or the job-folder box) reopens an existing job the same way.

From a fresh clone the same flow runs without the app:

```bash
director offload                                   # the most recent shoot day on every mounted camera card (asks first)
director offload --to "/path/to/drive" --project "Launch Talk" --yes
director offload --all                             # every clip on the cards, not just the latest day (--date YYYY-MM-DD plans one day)
director cut "/path/to/drive/2026-11-04 Launch Talk" --out-dir launch-job
```

Cards land in `<destination>/<YYYY-MM-DD> <project or "Shoot">/<camera label>/` with original file names and sidecars (Sony `M01.XML`, subtitle files) beside their clips; files already there with the same size and checksum are skipped, and the plan refuses with the numbers when the destination lacks the space.

## Commands reference

Every `director` subcommand, from `src/director/cli.py`. File names below are placeholders; `--model` defaults to `muse-spark-1.3` wherever it appears.

| Command | What it does | Flags I use most |
|---|---|---|
| `director cut INPUT…` | Cut files or a folder of cards to a finished job in one step (sync → words → tighten → grade → angles → finish → export → reframe → captions → QC) | `--out-dir job1`, `--no-muse --no-sam` (offline), `--no-tighten`, `--pause-limit 0.8`, `--target 3:00`, `--lut 'DJI_*=dlog.cube'`, `--grade-reference 'DJI_*'`, `--push-in 4`, `--force` |
| `director demo` | Make a synthetic shoot and run the real pipeline on it, with known-answer checks | `--out-dir director-demo`, `--muse`, `--keep` |
| `director sync CLIP…` | Place every clip on one shared timeline; writes `sync.json` | `--reference CLIP`, `--methods timecode,clock,waveform,whisper,flash,lips`, `--transcripts DIR`, `--whisper-model MODEL`, `--out sync.json` |
| `director transcribe` | Transcribe one recording with Muse Voice Transcribe and align it onto Whisper word times | `--sync sync.json --words whisper.json --out words.json`, `--audio LABEL`, `--no-muse`, `--max-usd 2.0` |
| `director tighten` | Pick one take per beat and list removed spans with reasons; writes `tighten.json` | `--sync sync.json --words words.json --out tighten.json`, `--pause-limit 0.8`, `--target 3:00`, `--no-muse`, `--max-usd 5.0` |
| `director grade` | Write a per-camera `.cube` to Rec.709 and match cameras; writes `grade.json` | `--sync sync.json --out grade.json`, `--reference LABEL`, `--lut LABEL=PATH`, `--no-match`, `--check` |
| `director angles` | Pick the camera for each spoken line; writes `angles.json`, renders with `--render` | `--sync sync.json --words words.json --out angles.json`, `--render cut.mp4`, `--grade grade.json`, `--tighten tighten.json`, `--min-shot 2.0`, `--push-in 4`, `--no-muse` |
| `director reframe VIDEO` | Reframe a finished 16:9 cut to 9:16 following the speaker; writes `reframe.json` | `--out reframe.json --render vertical.mp4`, `--shots angles.json`, `--no-sam`, `--max-usd 2.0` |
| `director finish` | Clean, optionally design sound for, and master a finished cut's audio in place | `--job job1` (or `--video cut.mp4 --angles angles.json`), `--sound`, `--isolate`, `--no-deplosive`, `--loudness -16`, `--no-muse` |
| `director captions` | Burn karaoke captions and a title hook into a finished job's videos | `--job job1`, `--title "…"` or `--title auto`, `--no-title`, `--preset bold`, `--captions-horizontal` |
| `director voice NOTE` | Turn a voice memo into checked edit proposals against a job or a hand-run pipeline | `--job job1`, `--apply`, `--render` (with `--job`), `--sync/--words/--angles/--tighten` (hand-run), `--yes`, `--max-usd 2.00` |
| `director connect list\|add\|test\|remove` | Show, save, test, or delete outside-service keys (ElevenLabs, Higgsfield) | `director connect list`, `director connect add elevenlabs`, `director connect test`, `director connect remove elevenlabs` |
| `director offload [CARD…]` | Copy camera cards to the destination drive with checksum verification; writes `offload.json` | `--to DEST`, `--project NAME`, `--yes` |

Running the stages by hand uses the same files `director cut` writes. For example:

```bash
director sync a-cam.mp4 b-cam.mp4 lav.mp3 --out sync.json
director grade --sync sync.json --out grade.json
director tighten --sync sync.json --words words.json --out tighten.json
director angles --sync sync.json --words words.json --out angles.json --grade grade.json --tighten tighten.json --render cut.mp4
director reframe cut.mp4 --out reframe.json --shots angles.json --render vertical.mp4
director captions --job job1 --title "Why this cut took one pass"
```

## Keys

Keys come from environment variables or from `director connect`, never from files in a job or in this repository, and no screen, log, or error message ever shows a saved key back.

- Meta models: `MODEL_API_KEY` (with `pip install -e ".[muse]"`). Without it, the offline paths above still run.
- ElevenLabs: `ELEVENLABS_API_KEY`, or `director connect add elevenlabs` (with `pip install -e ".[connect]"` for macOS Keychain storage). Environment variables win over saved keys.
- Higgsfield: `HF_API_KEY_ID` and `HF_API_KEY_SECRET`, or `director connect add higgsfield`. See the limitation below: connecting it changes nothing yet.

```bash
director connect list    # every provider, connected or not, and what it can do
director connect test    # a cheap no-generation test call per connection; exits 1 if any fails
```

The app's Connections panel (top bar) is the same registry. Generated audio is billed to your provider account at list prices — `cut.json` carries the estimate, capped by `--max-provider-usd` (default $2.00) — and each asset in `finish/` keeps its prompt, provider, estimated cost, and licence note. Use of generated files is subject to your provider's terms for your plan; `finish.json` says so per asset.

## Honest limitations

- **macOS-only pieces.** Lip-reading sync and the gaze rule use Apple Vision through a shipped Swift helper (`src/director/lips_helper.swift`, built with `swiftc` on first use). Without `swiftc` or Vision they skip with a warning and the other sync methods still run. Local Whisper is `mlx_whisper`, so clip transcription on other platforms needs `--transcripts` or supplied `--words`. The rest of the pipeline runs on Linux — CI runs the test suite and `director demo` on Ubuntu.
- **The timelines carry the angles, not the recorder.** The FCPXML/OTIO export carries every camera angle as a clip, but GhostCut's pre-event exporter has no audio-only lane, so a separate audio recorder does not appear as its own track. I re-lay the production audio in the editor at the offset recorded in `angles.json`/`cut.json`; the rendered `cut.mp4` and `vertical.mp4` are unaffected.
- **The push-in render is slow.** `--push-in` renders each shot through a 4× upscale so the zoom does not stair-step, which costs real time on long cuts. It is off by default.
- **Higgsfield is key storage only.** `director connect` can save and test a Higgsfield key, but no finishing capability uses it yet, so connecting it changes nothing.
- A failed finish stage does not fail the job: the cut keeps its recorded audio, `cut.json` records the failure, and the job ends as `complete_with_warnings`.

## Install and test

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[director]"   # the app and its tests: numpy + Pillow
pip install -e ".[muse]"       # optional: the Meta Model API client (guestlist)
pip install -e ".[connect]"    # optional: macOS Keychain key storage (keyring)
python -m unittest discover -s tests -p 'test_*.py'
```

Tests self-skip when their prerequisites (numpy, Pillow, ffmpeg, the macOS card font) are absent, and no test calls the Meta API. A green run with skips is normal. CI (`.github/workflows/ci.yml`) runs the suite bare and with the `director` extra on Ubuntu, then runs `director demo` end to end.

## Licence

MIT (see [LICENSE](LICENSE)).
