# Brief: tame mic air on the peaks (`finish` de-plosive)

Written 2026-11-03 by the project owner with Claude Code for Meta's Muse Code agent, from a recipe Claude Code
measured on the owner's real take the same afternoon. Build it straight from this brief on Muse Spark. Read
`AGENTS.md`, `docs/muse-code/OPEN-BRIEF.md` (its Rules apply), `docs/muse-code/FINISH-DESIGN.md`, then
`src/director/finish.py` and `tests/test_finish.py`.

## What the owner heard

"It's here and there… like air hitting the thing, when I get excited, whenever the bars peak. It sounds a little
high." He held the lav transmitter close while talking. Measured on the take (`finish/voice_raw.wav`): no clipping
(master peak −0.99 dBFS), no digital clicks at joins, but at the loudest moments the band below 150 Hz jumps 4–6 dB
above its level in normal speech (seven moments in 125 s, e.g. 11.1, 19.8, 24.5, 26.6, 33.2, 42.7, 120.7 s). Mastering
then adds ~19 dB of gain, so those air bursts stand out.

## What was measured (the recipe to keep)

- Speech-gated low-band level: envelope of the 150 Hz low-passed voice (30 ms RMS windows), median over the louder
  half of the frames (the speech): −42.8 dBFS on this take.
- Split the voice at 160 Hz (two cascaded 2nd-order filters each side). Compress only the low band with ffmpeg
  `acompressor`, threshold = that speech-gated low-band level **+6 dB** (as linear amplitude), ratio 20,
  attack 0.3 ms, release 40 ms, knee 2; high-pass the whole voice at 60 Hz first; sum the bands
  (`amix=inputs=2:normalize=0`).
- Result: the low band at the seven air peaks fell 9.6–13.4 dB (mean −11.2 dB) while normal speech's low band moved
  −0.6 dB and the speech level −0.4 dB: the bursts are tamed, the voice keeps its body.
- Rejected: a fixed-threshold low-band compressor (acted all the time) and `adynamicequalizer` with an adaptive
  threshold plus an 80 Hz high-pass (cut the air peaks 16 dB but thinned normal speech 9 dB, the opposite of what the
  owner wants when he already hears it "a little high").

## The job

1. Add the de-plosive to the local clean chain in `finish` (default on; `--no-deplosive` to skip), with the
   threshold measured per recording as above (numpy is available through the director extra; without numpy fall back
   to a fixed −36 dBFS threshold and warn). Record the measured threshold and the number of moments it acted on in
   `finish.json`.
2. Add a gentle peak limiter after loudness normalisation so true peak never exceeds the target (−1 dBTP); today's
   take measured −0.31 dBTP after `loudnorm`. Measure and record the final true peak.
3. Keep every existing finish behaviour and test; ElevenLabs isolation, when asked for, runs before the de-plosive.
4. Check it on the owner's take: `<job>/finish/voice_raw.wav`
   (read only). Write any test outputs under
   `<air-test>/` and report the low-band change at the seven
   moments and in normal speech, and the final true peak.

## Rules

`OPEN-BRIEF.md` "Rules (hard)". Tests offline: the threshold measurement on a synthetic voice-plus-bursts signal
(made with numpy or lavfi), the filter-graph text, the fallback without numpy, the limiter in the chain, and that a
burst is reduced far more than steady speech (render a short lavfi signal through the chain with ffmpeg when
available). Never call the Meta API or Whisper. Add an AIR section to `docs/muse-code/BUILD-LOG.md`.
