from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from fractions import Fraction
from pathlib import Path
from unittest import mock

try:
    import numpy as np
except ImportError:
    raise unittest.SkipTest("needs numpy (pip install -e \".[director]\")")
if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    raise unittest.SkipTest("needs ffmpeg and ffprobe on PATH")

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
import render_voice as rv  # noqa: E402

SR = 48000
LAGS = [0, 17, 36, 36, 37, 67, 66, 93, 129]


def speech(rng: np.random.Generator, n: int, sr: int = SR) -> np.ndarray:
    """Noise bursts with sharp onsets and silent gaps: enough like speech for a 1 ms envelope."""
    x, t = np.zeros(n, np.float32), int(0.03 * sr)
    while t < n - int(0.3 * sr):
        m = int(rng.uniform(0.06, 0.25) * sr)
        ramp = np.minimum(1, np.arange(m) / (0.002 * sr)) * np.minimum(1, np.arange(m)[::-1] / (0.004 * sr))
        x[t:t + m] = rng.standard_normal(m) * rng.uniform(0.02, 0.12) * ramp
        t += m + int(rng.uniform(0.03, 0.2) * sr)
    return x


def write_wav(path: Path, x: np.ndarray, sr: int = SR) -> Path:
    x = np.asarray(x, np.float32)
    x = x if x.ndim == 2 else x[:, None]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(x.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.clip(np.round(x * 32767), -32768, 32767).astype("<i2").tobytes())
    return path


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, w.getnchannels()) / 32768.0


def drifted(clean: np.ndarray, slots: list[tuple[int, int]], lags_ms: list[int], extra: int = SR // 2) -> np.ndarray:
    """What a back-to-back isolation looks like: each piece lands `lag` ms later than in the reference."""
    iso = np.zeros(len(clean) + extra, np.float32)
    for (s, e), lag in zip(slots, lags_ms):
        iso[s + lag * 48:e + lag * 48] = clean[s:e]
    return iso


class DecodeEnvelope(unittest.TestCase):
    def test_decode_places_samples_by_timestamp(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "gap.mka"  # 3 s tone with 0.2 s of packets removed but their timestamps kept
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:r=48000:d=3", "-af",
                            "aselect='not(between(t,1,1.2))'", "-ac", "2", "-c:a", "pcm_s16le", str(f)], check=True)
            x = rv.decode_ts(f)
        self.assertEqual(x.dtype, np.float32)
        self.assertEqual(x.shape[1], 2)
        self.assertLess(abs(len(x) - 3 * SR), 48)  # back to back it would be ~2.79 s
        self.assertLess(np.abs(x[int(1.05 * SR):int(1.15 * SR)]).max(), 1e-3)  # the hole stays silent
        i = np.arange(int(2.0 * SR), int(2.0 * SR) + 480)
        ref = 0.125 * np.sqrt(0.5) * np.sin(2 * np.pi * 440 * i / SR)  # 1/8, upmixed -3 dB; same phase after the hole
        self.assertLess(np.abs(x[i, 0] - ref).max(), 2e-3)

    def test_envelope_is_1ms_and_speech_band(self):
        t = np.arange(SR) / SR
        x = np.concatenate([0.5 * np.sin(2 * np.pi * 1000 * t), 0.5 * np.sin(2 * np.pi * 8000 * t),
                            0.5 * np.sin(2 * np.pi * 100 * t)]).astype(np.float32)
        e = rv.envelope(np.stack([x, x], 1), SR)
        self.assertEqual(len(e), 3000)
        self.assertAlmostEqual(float(np.median(e[100:900])), np.log(0.5 / np.sqrt(2)), delta=0.05)
        self.assertLess(float(np.median(e[1100:1900])), np.log(0.5 / np.sqrt(2)) - 6)
        self.assertLess(float(np.median(e[2100:2900])), np.log(0.5 / np.sqrt(2)) - 4)

    def test_envelope_blocks_match_one_pass(self):
        x = np.random.default_rng(3).standard_normal(int(25 * SR)).astype(np.float32)  # > one FFT block
        e = rv.envelope(x, SR)
        self.assertEqual(len(e), 25000)
        tail = rv.envelope(x[20 * SR:], SR)
        np.testing.assert_allclose(e[20000 + 100:], tail[100:], atol=1e-3)


class Realign(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(7)
        self.slots = [(int((0.1 + 4.5 * i) * SR), int((4.3 + 4.5 * i) * SR)) for i in range(9)]
        n = self.slots[-1][1] + SR
        self.clean = speech(rng, n)
        self.ref = self.clean + 0.004 * rng.standard_normal(n).astype(np.float32)  # camera: voice plus wind
        self.iso = drifted(self.clean, self.slots, LAGS) * 0.4
        self.rng = rng

    def test_recovers_per_piece_lags(self):
        out = rv.realign(self.iso, np.stack([self.ref, self.ref], 1), self.slots, SR)
        self.assertEqual(len(out), 9)
        for m, lag in zip(out, LAGS):
            self.assertLessEqual(abs(m["lag_ms"] - lag), 1, out)
            self.assertTrue(m["ok"])
            self.assertGreater(m["r"], 0.9)
            self.assertEqual(len(m["sub"]), 1)
            self.assertLessEqual(abs(m["sub"][0]["lag_ms"] - m["lag_ms"]), 1)

    def test_falls_back_below_r_and_keeps_tracking(self):
        s, e = self.slots[4]
        self.iso[s + LAGS[4] * 48:e + LAGS[4] * 48] = speech(self.rng, e - s) * 0.4  # someone else's audio
        out = rv.realign(self.iso, self.ref, self.slots, SR)
        self.assertFalse(out[4]["ok"])
        self.assertLess(out[4]["r"], rv.R_MIN)
        self.assertEqual(out[4]["sub"], [])
        for k in (5, 6, 7, 8):  # searched around piece 3's lag, the last one trusted
            self.assertLessEqual(abs(out[k]["lag_ms"] - LAGS[k]), 1)
            self.assertTrue(out[k]["ok"])

    def test_isolation_too_short_is_not_ok(self):
        e = self.slots[-1][1]
        out = rv.realign(self.iso[:e + LAGS[-1] * 48 - 480], self.ref, self.slots, SR)  # ends 10 ms early
        self.assertGreater(out[-1]["r"], 0.6)
        self.assertFalse(out[-1]["covered"])
        self.assertFalse(out[-1]["ok"])
        self.assertTrue(all(m["ok"] for m in out[:-1]))


class Layout(unittest.TestCase):
    def test_section_starts_are_frame_exact(self):
        frames = [768, 11981, 14641, 23689]
        start = np.concatenate([[0], np.cumsum(frames)[:-1]]).tolist()
        tl = {"rate": [60000, 1001], "sections": [{"start_frame": s, "frames": f} for s, f in zip(start, frames)]}
        starts = rv._starts(tl, SR)
        self.assertEqual(starts[-1], 40904063)
        self.assertEqual(starts[1], round(Fraction(768 * 1001 * SR, 60000)))
        self.assertEqual(rv._starts({"sections": [{"t": 0, "d": 1.0}, {"t": 1.0, "d": 2.5}]}, SR), [0, 48000, 168000])

    def test_slots_follow_container_starts_and_fade(self):
        sec = {"audio_offset": 0.021, "pieces": [{"A": 0.0, "d": 2.5, "src_in": 1.0, "src_out": 3.48},
                                                 {"A": 2.5, "d": 1.2, "src_in": 0.0, "src_out": 1.18}]}
        full, content = rv._slots(sec, 10 * SR, SR)
        self.assertEqual(full, [(1008, 121008), (121008, 178608)])
        self.assertEqual(content, [(1008, 1008 + round((2.48 - 0.01) * SR)), (121008, 121008 + round(1.17 * SR))])
        self.assertEqual(rv._slots(sec, 150000, SR)[0][1], (121008, 150000))  # clipped to the section

    def test_splice_crossfades_equal_power(self):
        buf = np.ones((4800, 2), np.float32)
        iso = np.arange(6000, dtype=np.float32) / 6000
        rv._splice(buf, iso, [(1000, 2000, 50), (2000, 3000, 100)], SR)
        h = 120
        np.testing.assert_allclose(buf[1000 + h:2000 - h, 0], iso[1000 + h + 50:2000 - h + 50], rtol=1e-6)
        np.testing.assert_allclose(buf[2000 + h:3000 - h, 1], iso[2000 + h + 100:3000 - h + 100], rtol=1e-6)
        self.assertTrue(np.all(buf[:1000 - h] == 1) and np.all(buf[3000 + h:] == 1))
        th = (1990 - (2000 - h) + 0.5) / (2 * h) * np.pi / 2
        mix = iso[1990 + 50] * np.cos(th) + iso[1990 + 100] * np.sin(th)  # lag 50 fading into lag 100
        self.assertAlmostEqual(float(buf[1990, 0]), mix, places=5)
        th = (3005 - (3000 - h) + 0.5) / (2 * h) * np.pi / 2
        self.assertAlmostEqual(float(buf[3005, 0]), iso[3005 + 100] * np.cos(th) + np.sin(th), places=5)


class BuildVoice(unittest.TestCase):
    """Cold open + an isolated talk section + a plain one, at real frame rates but seconds long."""

    LAGS = [12, 30, 29, 55]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        rng = np.random.default_rng(11)
        self.n = [48048, 480480, 96096]  # 60, 600 and 120 frames at 60000/1001
        self.pieces = [{"source": "A.MP4", "src_in": 0.0, "src_out": 2.48, "zoom": 1.0, "topic": "", "A": 2.5 * i,
                        "d": 2.5} for i in range(4)]
        sec = {"audio_offset": 0.021, "pieces": self.pieces}
        self.full, self.content = rv._slots(sec, self.n[1], SR)
        t = np.arange(self.n[0]) / SR
        self.intro = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        self.clean = np.zeros(self.n[1], np.float32)
        for s, e in self.content:
            self.clean[s + 960:e - 2400] = speech(rng, e - s - 3360)
        self.camera = self.clean + 0.01 * rng.standard_normal(self.n[1]).astype(np.float32)
        self.seated = 0.1 * rng.standard_normal((self.n[2], 2)).astype(np.float32)
        self.files = {"intro": write_wav(d / "intro.wav", np.stack([self.intro, self.intro], 1)),
                      "talk": write_wav(d / "talk.wav", np.stack([self.camera, self.camera], 1)[:10 * SR]),  # short
                      "seated": write_wav(d / "seated.wav", self.seated[:90000]),  # short: padded with silence
                      "win": write_wav(d / "win.wav", np.stack([self.camera, self.camera], 1)[:10 * SR]),
                      "iso": write_wav(d / "iso.wav", drifted(self.clean, self.content, self.LAGS) * 0.5)}
        self.cache = d / "voice_cache"
        self.logs: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def timeline(self, iso: Path | None = None) -> tuple[dict, dict]:
        f = self.files
        tl = {"rate": [60000, 1001], "sr": SR, "frames": 780, "sections": [
            {"name": "Cold open", "kind": "intro", "start_frame": 0, "frames": 60, "audio": str(f["intro"]),
             "pieces": []},
            {"name": "Community", "kind": "talk", "start_frame": 60, "frames": 600, "audio": str(f["talk"]),
             "audio_offset": 0.021, "pieces": self.pieces,
             "isolate": {"iso": str(iso or f["iso"]), "win": str(f["win"]), "pieces": [0, 3]}},
            {"name": "Explainer", "kind": "talk", "start_frame": 660, "frames": 120, "audio": str(f["seated"]),
             "audio_offset": 0.021, "pieces": []}]}
        return tl, {"sections": [{"audio": s["audio"]} for s in tl["sections"]]}

    def build(self, iso: Path | None = None):
        tl, job = self.timeline(iso)
        return rv.build_voice(tl, job, self.cache, log=self.logs.append)

    def test_sample_exact_program_with_isolation_replaced(self):
        wav, rep = self.build()
        self.assertEqual(wav.parent, self.cache)
        with wave.open(str(wav), "rb") as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()), (2, 2, SR, 624624))
        self.assertEqual(rep["samples"], 624624)
        self.assertEqual(json.loads(wav.with_suffix(".json").read_text()), rep)
        self.assertEqual([s["start"] for s in rep["sections"]], [0, 48048, 528528])
        self.assertEqual([(p["section"], p["piece"]) for p in rep["pieces"]], [(1, k) for k in range(4)])
        self.assertEqual([p["lag_ms"] for p in rep["pieces"]], self.LAGS)
        self.assertTrue(all(p["isolated"] for p in rep["pieces"]))
        self.assertEqual(rep["fallbacks"], [])
        self.assertEqual(rep["sections"][1]["isolated"], [0, 1, 2, 3])
        y = read_wav(wav)
        np.testing.assert_allclose(y[:48048, 0], self.intro, atol=2e-4)
        np.testing.assert_allclose(y[528528:528528 + 90000], self.seated[:90000], atol=2e-4)
        self.assertTrue(np.all(y[528528 + 90000:] == 0))  # past the decoded audio: silence
        talk = y[48048:528528, 0]
        body = np.concatenate([np.arange(a + 120, b - 120) for a, b in self.full])
        g = float(talk[body] @ self.clean[body] / (self.clean[body] @ self.clean[body]))
        self.assertAlmostEqual(g, 1.0, delta=0.05)  # loudness-matched to the camera window it replaces
        self.assertAlmostEqual(rep["sections"][1]["gain_db"], 20 * np.log10(2 * np.sqrt(2)), delta=0.6)  # mono iso
        for a, b in self.full:  # every piece back on the camera's timing, wind gone
            np.testing.assert_allclose(talk[a + 120:b - 120], self.clean[a + 120:b - 120] * g, atol=2e-4)
        np.testing.assert_allclose(talk[:880], self.camera[:880], atol=2e-4)  # before the first slot: camera
        self.assertTrue(any("isolation 4/4" in m for m in self.logs))

    def test_cached_by_inputs(self):
        wav, rep = self.build()
        with mock.patch.object(rv, "decode_ts", side_effect=AssertionError("decoded again")):
            wav2, rep2 = self.build()
        self.assertEqual((wav2, rep2), (wav, rep))
        self.assertTrue(any("cached" in m for m in self.logs))
        st = self.files["iso"].stat()
        os.utime(self.files["iso"], ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        wav3, _ = self.build()
        self.assertNotEqual(wav3, wav)

    def test_mismatched_piece_keeps_camera_audio(self):
        iso = drifted(self.clean, self.content, self.LAGS) * 0.5
        s, e = self.content[2]
        iso[s + 29 * 48:e + 29 * 48] = speech(np.random.default_rng(5), e - s) * 0.5
        wav, rep = self.build(write_wav(Path(self.tmp.name) / "iso2.wav", iso))
        self.assertEqual([p["isolated"] for p in rep["pieces"]], [True, True, False, True])
        self.assertEqual(len(rep["fallbacks"]), 1)
        fb = rep["fallbacks"][0]
        self.assertEqual((fb["section"], fb["piece"]), (1, 2))
        self.assertIn("r ", fb["reason"])
        self.assertAlmostEqual(fb["t"], (48048 + self.full[2][0]) / SR, places=3)
        self.assertEqual(rep["sections"][1]["camera_fallback"], [2])
        a, b = self.full[2]
        np.testing.assert_allclose(read_wav(wav)[48048 + a + 120:48048 + b - 120, 0], self.camera[a + 120:b - 120],
                                   atol=2e-4)

    def test_more_than_two_fallbacks_fail(self):
        other = speech(np.random.default_rng(9), len(self.clean) + SR // 2) * 0.5
        with self.assertRaisesRegex(RuntimeError, "fell back to camera audio"):
            self.build(write_wav(Path(self.tmp.name) / "iso3.wav", other))
        self.assertEqual(list(self.cache.glob("*")), [])

    def test_import_stays_offline(self):
        out = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import render_voice; "
                              "print(sorted(m for m in ('guestlist', 'openai') if m in sys.modules))", str(APP)],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.strip(), "[]")
        src = (APP / "render_voice.py").read_text()
        self.assertNotIn("/Volumes/", src)
        self.assertNotIn("/Users/", src)


class FlushGapTest(unittest.TestCase):
    """broll()'s loudnorm flush stamps a section render's last ~3 s late; the voice must not copy that jump."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "work" / "render").mkdir(parents=True)
        self.x = speech(np.random.default_rng(3), round(6.006 * SR))  # 6.006 s: not on loudnorm's 100 ms grid
        src = write_wav(d / "talk.wav", np.stack([self.x, self.x], 1))
        self.joined = d / "work" / "render" / "joined.mp4"
        self.section = d / "work" / "section.mp4"
        for out, af in ((self.joined, []), (self.section, ["-af", "loudnorm=I=-16", "-ar", str(SR)])):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), *af, "-c:a", "aac", "-b:a", "192k",
                            str(out)], check=True)
        self.d = d

    def tearDown(self):
        self.tmp.cleanup()

    def test_flush_padding_found_and_joined_jumps_ignored(self):
        x = rv.decode_ts(self.section)
        gaps = rv.flush_gaps(self.section, self.joined, x)
        self.assertEqual(len(gaps), 1)
        a, k = gaps[0]
        self.assertGreater(a / SR, len(x) / SR - rv.TAIL)
        self.assertTrue(np.all(x[a:a + k] == 0))
        self.assertEqual(rv.flush_gaps(self.section, self.section, x), [])  # the same jump in joined: kept

    def test_voice_tail_stays_on_joined(self):
        tl = {"rate": [60000, 1001], "sr": SR, "frames": 360, "sections": [
            {"name": "Talk", "kind": "talk", "start_frame": 0, "frames": 360, "audio": str(self.section),
             "work": str(self.d / "work"), "audio_offset": 0.021, "pieces": []}]}
        wav, rep = rv.build_voice(tl, {"sections": [{}]}, self.d / "cache", log=lambda m: None)
        self.assertEqual(len(rep["sections"][0]["flush_gaps"]), 1)
        ref = rv.envelope(rv.decode_ts(self.joined), SR)
        s, e = len(ref) - 2600, len(ref) - 200  # the last 2.6..0.2 s, inside the flushed tail
        late = rv._search(rv.envelope(rv.decode_ts(self.section), SR), ref, s, e, -150, 150)
        self.assertLess(late[1], -50)  # the section render itself plays its tail late (it matches joined earlier)
        r, lag = rv._search(rv.envelope(read_wav(wav), SR), ref, s, e, -150, 150)
        self.assertLessEqual(abs(lag), 1)
        self.assertGreater(r, 0.9)


if __name__ == "__main__":
    unittest.main()
