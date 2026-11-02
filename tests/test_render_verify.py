from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import wave
from unittest import mock
from pathlib import Path

try:
    import numpy as np
except ImportError:
    raise unittest.SkipTest("needs numpy (pip install -e \".[director]\")")
if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    raise unittest.SkipTest("needs ffmpeg and ffprobe on PATH")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import render_verify as rv  # noqa: E402

SR = 48000
FPS = 60000 / 1001


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", *args], check=True)


def speech(seconds: float, seed: int = 0) -> np.ndarray:
    """Noise-burst 'syllables' with pauses: a speech-like envelope."""
    rng = np.random.default_rng(seed)
    x, t = np.zeros(int(seconds * SR)), 0
    while t < len(x):
        on, gap = int(rng.uniform(0.08, 0.3) * SR), int(rng.uniform(0.05, 0.4) * SR)
        seg = rng.standard_normal(min(on, len(x) - t)) * rng.uniform(0.1, 0.4)
        x[t:t + len(seg)] = seg
        t += on + gap
    return x


def wav(path: Path, x: np.ndarray, ch: int = 2) -> Path:
    y = (np.clip(x, -1, 1) * 32767).astype("<i2")
    if ch == 2:
        y = np.repeat(y[:, None], 2, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(y.tobytes())
    return path


def colors(path: Path, runs: list[tuple[str, int]], size: str = "320x180", extra: list[str] | None = None) -> Path:
    """A video of solid-colour runs (colour, frames) at 60000/1001, joined frame-exactly."""
    ins, chain = [], []
    for k, (c, n) in enumerate(runs):
        ins += ["-f", "lavfi", "-i", f"color=c={c}:s={size}:r=60000/1001:d={n / FPS + 0.5:.4f}"]
        chain.append(f"[{k}:v]trim=end_frame={n},setpts=PTS-STARTPTS[s{k}]")
    graph = ";".join(chain) + ";" + "".join(f"[s{k}]" for k in range(len(runs))) + f"concat=n={len(runs)}:v=1:a=0[v]"
    ff(*ins, "-filter_complex", graph, "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0",
       "-pix_fmt", "yuv420p", "-r", "60000/1001", *(extra or []), str(path))
    return path


def talk_tl(tmp: Path, S_frames: int = 60) -> dict:
    """A cold open of S_frames and one talk section with two pieces (5 s and 6 s), with its own joined.mp4."""
    S = S_frames / FPS
    return {"rate": [60000, 1001], "sr": SR, "frames": S_frames + 700, "duration": (S_frames + 700) / FPS,
            "sections": [{"name": "Cold open", "kind": "intro", "start_frame": 0, "frames": S_frames, "t": 0.0, "d": S},
                         {"name": "Talk", "kind": "talk", "start_frame": S_frames, "frames": 700, "t": S, "d": 700 / FPS,
                          "work": str(tmp / "work"), "audio": str(tmp / "work" / "section.mp4"), "audio_offset": 0.021,
                          "pieces": [{"A": 0.0, "d": 5.0, "source": str(tmp / "cam.wav"), "src_in": 2.0},
                                     {"A": 5.0, "d": 6.0, "source": str(tmp / "nope.MP4"), "src_in": 1.0}]}]}


class EnvelopeLagTests(unittest.TestCase):
    def test_recovers_known_lags(self) -> None:
        x = speech(10, seed=1)
        T, W, M = 3 * SR, 6 * SR, 50
        ref = x[T - M * 48: T + W + M * 48]
        for late in (0, 7, -12, 37):
            test = x[T - late * 48: T - late * 48 + W]
            lag, r = rv.lag_ms(test, ref, M)
            self.assertLessEqual(abs(lag - late), 1, late)
            self.assertGreater(r, 0.9)

    def test_unrelated_audio_has_low_r(self) -> None:
        a, b = speech(6, seed=2), speech(6.1, seed=3)
        self.assertLess(rv.lag_ms(a, b, 50)[1], 0.5)


class PcmTests(unittest.TestCase):
    def test_window_and_padding_follow_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            x = np.zeros(3 * SR)
            x[SR:SR + 480] = 0.5
            w = wav(Path(d) / "a.wav", x)
            y = rv.pcm(w, 0.5, 1.0)
            self.assertEqual(len(y), SR)
            self.assertEqual(int(np.argmax(np.abs(y) > 0.1)), SR // 2)
            z = rv.pcm(w, -0.2, 0.5)
            self.assertEqual(len(z), SR // 2)
            self.assertFalse(np.any(np.abs(z) > 0.1))
            x[SR:SR + 4800] = 0.5 * np.random.default_rng(4).standard_normal(4800)
            m = Path(d) / "a.mp4"  # AAC priming (1024 samples) is undone by the timestamp decode
            ff("-i", str(wav(Path(d) / "b.wav", x)), "-c:a", "aac", "-b:a", "192k", str(m))
            onset = int(np.argmax(np.abs(rv.pcm(m, 0.9, 0.3)) > 0.05))
            self.assertLessEqual(abs(onset - SR // 10), 240)


class SectionPictureTests(unittest.TestCase):
    def test_frames_format_and_no_audio(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            v = Path(d) / "s.mp4"
            ff("-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=60000/1001:d=0.5", "-frames:v", "30", "-c:v", "libx264",
               "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an", str(v))
            self.assertEqual(rv.check_section_picture(v, 30, "1")["status"], "pass")
            bad = rv.check_section_picture(v, 31, "1")
            self.assertEqual(bad["status"], "fail")
            self.assertIn("30 frames, base 31", bad["detail"])
            a = Path(d) / "sa.mp4"
            ff("-i", str(v), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest", "-c:v", "copy", "-c:a",
               "aac", str(a))
            self.assertIn("has an audio stream", rv.check_section_picture(a, 30, "1")["detail"])


class VoiceFormatTests(unittest.TestCase):
    def test_exact_samples_and_stereo(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            w = wav(Path(d) / "v.wav", np.zeros(48001))
            self.assertEqual(rv.check_voice_format(w, 48001)["status"], "pass")
            self.assertEqual(rv.check_voice_format(w, 48000)["status"], "fail")
            m = wav(Path(d) / "m.wav", np.zeros(48001), ch=1)
            self.assertIn("1 channels", rv.check_voice_format(m, 48001)["detail"])

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            rv.check_voice_format(Path("/nonexistent/voice.wav"), 1)


class TimelineTests(unittest.TestCase):
    def test_frame_exact_starts(self) -> None:
        secs = [{"name": "a", "start_frame": 0, "frames": 768, "t": 0.0},
                {"name": "b", "start_frame": 768, "frames": 11981, "t": 768 * 1001 / 60000}]
        tl = {"rate": [60000, 1001], "frames": 12749, "duration": 12749 * 1001 / 60000, "sections": secs}
        self.assertEqual(rv.check_timeline(tl)["status"], "pass")
        secs[1]["t"] = 12.813
        self.assertEqual(rv.check_timeline(tl)["status"], "fail")


class IsolationTests(unittest.TestCase):
    def tl(self, lags: list[float], rs: list[float] | None = None, span=(0, 3)) -> dict:
        iso = [{"piece": k, "lag_ms": lag, "r": (rs or [0.85] * len(lags))[k]} for k, lag in enumerate(lags)]
        return {"sections": [{"name": "Community", "isolate": {"pieces": list(span)}, "isolated": iso}]}

    def test_steps_and_r(self) -> None:
        iso, fb = rv.check_isolation(self.tl([4, 20, 38, 38]))
        self.assertEqual((iso["status"], fb["status"]), ("pass", "pass"))
        self.assertEqual(iso["value"], "Community 4/4 isolated")
        self.assertEqual(rv.check_isolation(self.tl([4, 90, 95, 96]))[0]["status"], "fail")
        self.assertEqual(rv.check_isolation(self.tl([4, 20, 38, 38], [0.85, 0.5, 0.9, 0.9]))[0]["status"], "fail")
        self.assertEqual(rv.check_isolation(self.tl([4, -20, 38, 38]))[0]["status"], "fail")

    def test_fallbacks_warn_then_fail(self) -> None:
        self.assertEqual(rv.check_isolation(self.tl([4, 20, 38]))[1]["status"], "warn")
        self.assertEqual(rv.check_isolation(self.tl([4], span=(0, 3)))[1]["status"], "fail")

    def test_unreported_r_fails(self) -> None:
        tl = {"sections": [{"name": "C", "isolate": {"pieces": [0, 1]},
                            "isolated": [{"piece": 0, "lag_ms": 4, "r": None}, {"piece": 1, "lag_ms": None, "r": 0.9}]}]}
        self.assertEqual(len(rv.check_isolation(tl)[0]["detail"]), 2)

    def test_spec_from_job(self) -> None:
        tl = {"sections": [{"name": "House", "isolated": [{"piece": 0, "lag_ms": 35, "r": 0.9}]}]}
        job = {"sections": [{"isolate": {"pieces": [0, 1]}}]}
        self.assertEqual(rv.check_isolation(tl, job)[0]["value"], "House 1/2 isolated")


class JoinsAndMasterTests(unittest.TestCase):
    def test_held_frame_at_a_join_fails(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            good = colors(Path(d) / "g.mp4", [("red", 10), ("blue", 10), ("green", 10)])
            self.assertEqual(rv.check_joins(good, [10, 20], FPS)["status"], "pass")
            held = colors(Path(d) / "h.mp4", [("red", 11), ("blue", 9)])
            c = rv.check_joins(held, [10], FPS)
            self.assertEqual((c["status"], c["value"]), ("fail", 1))
            self.assertIn("biggest at 11", c["detail"][0])
            self.assertEqual(rv.check_joins(good, [11], FPS)["status"], "fail")

    def test_frames_and_audio_span(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            v = colors(Path(d) / "v.mp4", [("red", 60)])
            ok, long = Path(d) / "ok.mp4", Path(d) / "long.mp4"
            ff("-i", str(v), "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={60 / FPS:.4f}", "-c:v", "copy",
               "-c:a", "aac", str(ok))
            ff("-i", str(v), "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={60 / FPS + 0.2:.4f}", "-c:v", "copy",
               "-c:a", "aac", str(long))
            frames, span = rv.check_master(ok, 60)
            self.assertEqual((frames["status"], span["status"]), ("pass", "pass"))
            self.assertEqual(rv.check_master(ok, 61)[0]["status"], "fail")
            self.assertEqual(rv.check_master(long, 60)[1]["status"], "fail")


class LoudnessTests(unittest.TestCase):
    def test_reads_ebur128_and_applies_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            w = Path(d) / "s.wav"
            ff("-f", "lavfi", "-i", "sine=f=440:d=3:r=48000", "-ac", "2", str(w))
            i, tp = rv.loudness(w)
            self.assertTrue(-30 < i < -10 and -30 < tp < 0)
            lo, peak = rv.check_loudness(w, "x", (i - 1, i + 1), tp + 0.5)
            self.assertEqual((lo["status"], peak["status"], lo["name"]), ("pass", "pass", "x_loudness"))
            lo, peak = rv.check_loudness(w, "x", (i + 1, i + 2), tp - 0.5, "warn")
            self.assertEqual((lo["status"], peak["status"]), ("warn", "warn"))


class AudioGapTests(unittest.TestCase):
    def test_a_pts_jump_inside_the_mp4_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            src = wav(Path(d) / "s.wav", speech(3, seed=6))
            clean, jump = Path(d) / "clean.mp4", Path(d) / "jump.mp4"
            ff("-i", str(src), "-c:a", "aac", str(clean))
            ff("-i", str(src), "-af", "asetpts=PTS+gte(T\\,1.5)*0.08/TB", "-c:a", "aac", str(jump))
            self.assertEqual(rv.audio_gaps(clean), [])
            gaps = rv.audio_gaps(jump)
            self.assertEqual(len(gaps), 1)
            self.assertAlmostEqual(gaps[0][0], 1.5, delta=0.03)
            self.assertAlmostEqual(gaps[0][1], 80.0, delta=1.0)
            self.assertEqual(rv.check_audio_gaps(jump, "master")["status"], "fail")
            self.assertEqual(rv.check_audio_gaps(clean, "master")["status"], "pass")


class CutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.master = colors(d / "m.mp4", [("red", 37), ("blue", 40), ("green", 30)])
        cls.phone = d / "p.mp4"
        ff("-i", str(cls.master), "-vf", "fps=30000/1001", "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0",
           str(cls.phone))
        cls.late = d / "late.mp4"  # the -r path the phone copy used before: frames land later
        ff("-i", str(cls.master), "-vf", "setpts=PTS+0.05/TB,fps=30000/1001", "-c:v", "libx264", "-preset", "ultrafast",
           "-qp", "0", str(cls.late))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_cut_near_finds_the_first_new_frame(self) -> None:
        self.assertAlmostEqual(rv.cut_near(self.master, 37 / FPS, FPS), 37 / FPS, places=6)
        self.assertAlmostEqual(rv.cut_near(self.master, 77 / FPS + 0.05, FPS), 77 / FPS, places=6)
        self.assertIsNone(rv.cut_near(self.master, 100 / FPS, FPS, win=0.05))

    def test_a_fast_shot_before_the_cut_is_not_the_cut(self) -> None:
        """A moving low-contrast shot steps ~13 every frame; the cut into the next shot steps ~100. The cut is the
        strongest step, not the motion step nearest the expected time (the phone copy's D0292 out in r001)."""
        rng = np.random.default_rng(4)
        tex = (100 + rng.integers(-20, 21, (108, 192 + 40))).astype(np.uint8)
        fr = [tex[:, :192]] * 20 + [tex[:, 2 * k:2 * k + 192] for k in range(1, 11)] + \
            [np.full((108, 192), 200, np.uint8)] * 20
        raw = Path(self.tmp.name) / "fast.gray"
        raw.write_bytes(b"".join(f.tobytes() for f in fr))
        fast = Path(self.tmp.name) / "fast.mp4"
        ff("-f", "rawvideo", "-pix_fmt", "gray", "-s", "192x108", "-r", "60000/1001", "-i", str(raw), "-c:v", "libx264",
           "-preset", "ultrafast", "-qp", "0", "-pix_fmt", "yuv420p", str(fast))
        d = rv.diffs(fast, 18, 14, FPS)
        self.assertTrue(all(6 < x < 30 for x in d[2:10]), d)  # the motion steps are spikes on their own
        self.assertAlmostEqual(rv.cut_near(fast, 27 / FPS, FPS), 30 / FPS, places=6)

    def test_master_tolerance(self) -> None:
        c, got = rv.check_cuts(self.master, [("in", 37 / FPS - 0.01), ("out", 77 / FPS)], FPS)
        self.assertEqual(c["status"], "pass")
        self.assertEqual(len(got), 2)
        c, _ = rv.check_cuts(self.master, [("in", 37 / FPS - 0.1)], FPS)
        self.assertEqual(c["status"], "fail")

    def test_phone_within_one_source_frame(self) -> None:
        _, got = rv.check_cuts(self.master, [("in", 37 / FPS), ("out", 77 / FPS)], FPS)
        self.assertEqual(rv.check_phone_cuts(self.phone, got, 30000 / 1001)["status"], "pass")
        self.assertEqual(rv.check_phone_cuts(self.late, got, 30000 / 1001)["status"], "fail")

    def test_phone_frames(self) -> None:
        self.assertEqual(rv.check_phone_frames(self.phone, 107)["status"], "pass")
        self.assertEqual(rv.check_phone_frames(self.phone, 120)["status"], "fail")

    def test_intro_cuts_are_the_first_hard_cuts(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            v = colors(Path(d) / "i.mp4", [("red", 20), ("blue", 20), ("green", 20), ("yellow", 20)])
            got = rv.intro_cuts(v, FPS)
            self.assertEqual([round(t * FPS) for t in got], [20, 40])


class ExpectedCutsTests(unittest.TestCase):
    def test_changed_overlays_only(self) -> None:
        tl = {"sections": [{"t": 0.0}, {"t": 12.8128, "broll": [
            {"key": "v16:1:16", "tag": "D0292", "r": 145.7, "d": 4.9},
            {"key": "v16:1:17", "tag": "R1", "r": 151.0, "d": 3.0}]}]}
        plan = {"changes": [{"op": "swap_broll", "section": 1, "key": "v16:1:16"},
                            {"op": "drop_broll", "section": 1, "key": "v16:1:3"}]}
        got = rv.expected_cuts(tl, plan)
        self.assertEqual([lb for lb, _ in got], ["D0292 in", "D0292 out"])
        self.assertAlmostEqual(got[0][1], 158.5128)
        self.assertAlmostEqual(got[1][1], 163.4128)


class SyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        (d / "work" / "render").mkdir(parents=True)
        cls.tl = talk_tl(d)
        cls.S = cls.tl["sections"][1]["t"]
        x = speech(11.7, seed=5)
        wav(d / "work" / "joined.wav", x)
        ff("-i", str(d / "work" / "joined.wav"), "-c:a", "aac", "-b:a", "192k", str(d / "work" / "render" / "joined.mp4"))
        ff("-i", str(d / "work" / "joined.wav"), "-c:a", "aac", "-b:a", "192k", str(d / "work" / "section.mp4"))
        lead = round(cls.S * SR)
        cls.voice = wav(d / "voice.wav", np.concatenate([np.zeros(lead), x]))
        cls.late = wav(d / "late.wav", np.concatenate([np.zeros(lead + 12 * 48), x]))
        rng = np.random.default_rng(9)
        mix = np.concatenate([np.zeros(lead), x]) + 0.01 * rng.standard_normal(lead + len(x))
        cls.master = Path(d) / "master.mp4"
        ff("-i", str(wav(d / "mix.wav", mix)), "-c:a", "aac", "-b:a", "192k", str(cls.master))
        cam = np.concatenate([np.zeros(2 * SR - 21 * 48), x])  # the camera file holds piece 0 from src_in = 2.0
        wav(d / "cam.wav", cam)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_pick_first_middle_last(self) -> None:
        self.assertEqual(rv._pick({"pieces": [{"d": 3}, {"d": 5}, {"d": 4}, {"d": 9}, {"d": 2}, {"d": 6}]}), [1, 3, 5])
        self.assertEqual(rv._pick({"pieces": [{"d": 2}]}), [])

    def test_voice_against_joined(self) -> None:
        c = rv.check_sync_joined(self.voice, self.tl)
        self.assertEqual(c["status"], "pass", c)
        self.assertLessEqual(c["value"], 1)
        bad = rv.check_sync_joined(self.late, self.tl)
        self.assertEqual(bad["status"], "fail")
        self.assertGreaterEqual(bad["value"], 11)

    def test_late_tail_fails(self) -> None:
        """A section whose last ~3 s play late (a loudnorm flush jump copied into the voice) fails on its tail row."""
        lead, x = round(self.S * SR), speech(11.7, seed=5)
        cut, pad = round(9.5 * SR), 30 * 48
        tail = np.concatenate([np.zeros(lead), x[:cut], np.zeros(pad), x[cut:len(x) - pad]])
        c = rv.check_sync_joined(wav(Path(self.tmp.name) / "tail_late.wav", tail), self.tl)
        self.assertEqual(c["status"], "fail", c)
        row = next(r for r in c["detail"] if "Talk tail" in r)
        self.assertTrue(row.startswith("✗"), row)
        self.assertIn("+30 ms", row)
        ok = next(r for r in rv.check_sync_joined(self.voice, self.tl)["detail"] if "Talk tail" in r)
        self.assertIn("+0 ms", ok)

    def test_master_against_voice(self) -> None:
        self.assertEqual(rv.check_sync_master(self.master, self.voice, self.tl)["status"], "pass")
        self.assertEqual(rv.check_sync_master(self.master, self.late, self.tl)["status"], "fail")
        self.assertEqual(rv.check_sync_phone(self.master, self.master, self.tl)["status"], "pass")

    def test_camera_source_and_missing_drive(self) -> None:
        c = rv.check_sync_camera(self.voice, self.tl)
        self.assertEqual(c["status"], "warn")  # piece 1's camera file is not mounted
        self.assertLessEqual(c["value"], 2)
        self.assertTrue(any("not mounted" in r for r in c["detail"]))

    def test_isolated_subwindows(self) -> None:
        tl = json.loads(json.dumps(self.tl))
        tl["sections"][1]["isolated"] = [{"piece": 0, "lag_ms": 30, "r": 0.9}, {"piece": 1, "lag_ms": 40, "r": 0.9}]
        c = rv.check_iso_subwindows(self.voice, tl)
        self.assertEqual(c["status"], "pass", c)
        self.assertEqual(rv.check_iso_subwindows(self.late, tl)["status"], "fail")
        tl["sections"][1]["isolated"] = [{"piece": 0, "lag_ms": 30, "r": 0.9}]
        tl["sections"][1]["pieces"][0]["d"] = 1.0  # too short to measure: a warning, not a pass
        self.assertEqual(rv.check_iso_subwindows(self.voice, tl)["status"], "warn")
        del tl["sections"][1]["isolated"]
        self.assertEqual(rv.check_iso_subwindows(self.voice, tl)["status"], "pass")


class WordsTests(unittest.TestCase):
    TEXT = "without further ado let's go check it out the community has a pool a gym and two parks".split()

    def tl(self) -> dict:
        return {"sections": [{"name": "Cold open", "t": 0.0, "d": 4.0},
                             {"name": "Talk", "t": 4.0, "d": 20.0, "audio_offset": 0.021,
                              "pieces": [{"A": 0.0, "d": 5.0}, {"A": 5.0, "d": 5.0}, {"A": 10.0, "d": 10.0}],
                              "isolated": [{"piece": 0, "lag_ms": 4, "r": 0.9}, {"piece": 1, "lag_ms": 9, "r": 0.9}]}]}

    def transcript(self, path: Path, words: list[str], t0: float = 0.5, step: float = 0.5, dur: float = 0.4) -> Path:
        ws = [{"word": " " + w.capitalize(), "start": t0 + k * step, "end": t0 + k * step + dur} for k, w in enumerate(words)]
        path.write_text(json.dumps({"text": " ".join(words), "segments": [{"start": t0, "end": ws[-1]["end"], "words": ws}]}))
        return path

    def test_switch_points(self) -> None:
        self.assertEqual(rv.switch_points(self.tl()), [4.0, 4.021, 14.021])

    def test_same_words_pass(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            prev = self.transcript(Path(d) / "p.json", self.TEXT, t0=15.1)
            new = self.transcript(Path(d) / "n.json", self.TEXT, t0=15.0)
            got = {c["name"]: c for c in rv.check_words(prev, new, self.tl())}
            self.assertEqual(got["words_ratio"]["value"], 1.0)
            self.assertEqual(got["words_missing_run"]["status"], "pass")
            self.assertEqual(got["words_at_joins"]["status"], "pass")
            self.assertEqual(got["word_shift"]["value"], {"Talk": -0.1})

    def test_lost_run_and_word_across_a_switch(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            prev = self.transcript(Path(d) / "p.json", self.TEXT, t0=10.0)
            new = self.transcript(Path(d) / "n.json", self.TEXT[:4] + self.TEXT[9:], t0=13.9, dur=0.3)
            got = {c["name"]: c for c in rv.check_words(prev, new, self.tl())}
            self.assertEqual(got["words_missing_run"]["value"], 5)
            self.assertEqual(got["words_missing_run"]["status"], "fail")
            self.assertEqual(got["words_ratio"]["status"], "fail")
            self.assertEqual(got["words_at_joins"]["status"], "fail")  # "without" spans 14.021

    def test_switch_in_a_pause_only_warns(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            x = speech(20, seed=7)
            x[round(13.9 * SR):round(14.2 * SR)] = 0  # the voice pauses across the 14.021 switch
            quiet = wav(Path(d) / "q.wav", x)
            x[round(13.9 * SR):round(14.2 * SR)] = 0.3 * np.random.default_rng(8).standard_normal(round(0.3 * SR))
            loud = wav(Path(d) / "l.wav", x)
            ws = [("evening", 13.95, 14.15)]
            self.assertEqual(rv.check_words_at_joins(ws, self.tl(), quiet)["status"], "warn")
            self.assertEqual(rv.check_words_at_joins(ws, self.tl(), loud)["status"], "fail")
            self.assertEqual(rv.check_words_at_joins(ws, self.tl())["status"], "fail")
            self.assertEqual(rv.check_words_at_joins([("evening", 14.2, 14.5)], self.tl(), loud)["status"], "pass")

    def test_spoken_amounts_match_their_digits(self) -> None:
        said = "the lots range from zero dollars to as much as two hundred and fifty thousand dollars".split()
        typed = "the lots range from 0 to as much as 250 000 depending".split()
        ws = lambda xs: [(w, k * 0.3, k * 0.3 + 0.2) for k, w in enumerate(xs)]  # noqa: E731
        self.assertEqual([w for w, *_ in rv._numbers(ws(said))], "the lots range from # to as much as #".split())
        self.assertEqual([w for w, *_ in rv._numbers(ws(typed))], "the lots range from # to as much as # depending".split())
        self.assertEqual([w for w, *_ in rv._numbers(ws("rock and roll one and only".split()))],
                         ["rock", "and", "roll", "#", "and", "only"])  # "and" joins numbers only
        with tempfile.TemporaryDirectory() as d:
            prev = self.transcript(Path(d) / "p.json", said, t0=30.0)
            new = self.transcript(Path(d) / "n.json", typed[:-1], t0=30.0)
            got = {c["name"]: c for c in rv.check_words(prev, new, self.tl())}
            self.assertEqual(got["words_missing_run"]["status"], "pass", got["words_missing_run"])
            self.assertEqual(got["words_ratio"]["value"], 1.0)

    def test_a_cut_the_assembly_already_has_only_warns(self) -> None:
        """Loud voice across the 14.021 switch: a warning when the section's joined.mp4 is as loud there (the
        original dialogue edit), a failure when it is quiet (this render put speech on the cut)."""
        with tempfile.TemporaryDirectory() as d:
            x = speech(20, seed=7)
            x[round(13.9 * SR):round(14.2 * SR)] = 0.3 * np.random.default_rng(8).standard_normal(round(0.3 * SR))
            voice = wav(Path(d) / "v.wav", x)
            ws = [("evening", 13.95, 14.15)]
            for quiet_assembly, want in ((False, "warn"), (True, "fail")):
                y = x[round(4.0 * SR):].copy()  # the Talk section's own time
                if quiet_assembly:
                    y[round(9.9 * SR):round(10.2 * SR)] = 0
                (Path(d) / "work" / "render").mkdir(parents=True, exist_ok=True)
                ff("-i", str(wav(Path(d) / "a.wav", y)), "-c:a", "aac", "-b:a", "192k",
                   str(Path(d) / "work" / "render" / "joined.mp4"))
                tl = self.tl()
                tl["sections"][1]["work"] = str(Path(d) / "work")
                c = rv.check_words_at_joins(ws, tl, voice)
                self.assertEqual(c["status"], want, c)
                self.assertEqual(c["t"], 14.021)  # the app opens the finding there
                self.assertEqual("from the original edit" in c["detail"][0], not quiet_assembly, c)

    def test_app_transcript_format_and_no_previous(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            app = Path(d) / "a.json"
            app.write_text(json.dumps([{"t": 1, "e": 2, "text": "Hi there.", "w": [["Hi", 1.0, 1.3], ["there.", 1.3, 2.0]]}]))
            self.assertEqual(rv.words(app), [("hi", 1.0, 1.3), ("there", 1.3, 2.0)])
            got = {c["name"]: c for c in rv.check_words(None, app, self.tl())}
            self.assertEqual(got["words_ratio"]["status"], "warn")


def card_png(path: Path, w: int, h: int) -> Path:
    from PIL import Image

    Image.new("RGBA", (w, h), (255, 255, 255, 255)).save(path)
    return path


@unittest.skipUnless(importlib.util.find_spec("PIL"), "needs Pillow")
class CardTests(unittest.TestCase):
    def test_box_from_png_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d)
            card_png(f / "c0l0.png", 500, 110), card_png(f / "c0l1.png", 420, 110), card_png(f / "c0cap.png", 300, 70)
            card = {"caption": "CAP", "until": 5, "lines": [["A", 1.0], ["B", 1.6]]}
            self.assertEqual(rv.card_box(0, card, f), (96, 770, 596, 1004))
            self.assertAlmostEqual(rv.card_mid(card), (1.9 + 4.65) / 2)

    def test_visible_inside_box_only_and_contact_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "facts").mkdir()
            png = card_png(d / "facts" / "c0l0.png", 600, 110)
            base = d / "base.mp4"
            ff("-f", "lavfi", "-i", "color=c=gray:s=1920x1080:r=60000/1001:d=1", "-frames:v", "60", "-c:v", "libx264",
               "-preset", "ultrafast", "-crf", "16", "-pix_fmt", "yuv420p", str(base))
            master = d / "master.mp4"
            ff("-i", str(base), "-i", str(png), "-filter_complex", f"[0:v][1:v]overlay=96:{1080 - 110 - 76}", "-c:v",
               "libx264", "-preset", "ultrafast", "-crf", "16", "-pix_fmt", "yuv420p", str(master))
            tl = {"rate": [60000, 1001], "sections": [{"name": "Talk", "start_frame": 0, "frames": 60, "t": 0.0,
                                                       "picture": str(base)}]}
            cards = [{"caption": "", "until": 0.95, "lines": [["TEST", 0.1]]}]
            vis, sheet = rv.check_cards(cards, tl, master, d / "facts", d / "qa" / "cards.jpg")
            self.assertEqual(vis["status"], "pass", vis)
            self.assertEqual(sheet["status"], "pass")
            self.assertTrue((d / "qa" / "cards.jpg").stat().st_size > 1000)
            self.assertEqual(rv.check_cards(cards, tl, base, d / "facts", d / "qa" / "c2.jpg")[0]["status"], "warn")

    def test_text_consistency_and_guards(self) -> None:
        spec = [{"caption": "DEPENDS ON SIZE", "until": 5, "lines": [["LOT PREMIUMS  $0 – $250,000", 1.0]]}]
        same = [{"key": "c17", "caption": "DEPENDS ON SIZE", "until": 5, "lines": [["LOT PREMIUMS  $0 – $250,000", 1.0]]}]
        text, guard = rv.check_card_text(spec, same)
        self.assertEqual((text["status"], guard["status"]), ("pass", "pass"))
        other = [{"caption": "DEPENDS ON SIZE", "lines": [["LOT PREMIUMS", 1.0]]}]
        self.assertEqual(rv.check_card_text(spec, other)[0]["status"], "warn")
        self.assertEqual(rv.check_card_text(spec, same, project_cards=[{"caption": "X", "lines": ["Y"]}])[0]["status"],
                         "warn")
        bad = [{"caption": "", "lines": [["LOT PREMIUMS  /private/x/v16.sh – 50,000", 1.0]]}]
        self.assertEqual(rv.check_card_text(bad, bad)[1]["status"], "fail")
        four = [{"caption": "AMENITIES", "lines": [["POOL", 1], ["GYM", 2], ["PARK", 3], ["DOG PARK", 4]]}]
        self.assertEqual(rv.check_card_text(four, four)[1]["status"], "pass")  # v16 cards may have 4 lines
        self.assertEqual(rv.check_card_text([{"lines": []}], [{"lines": []}])[1]["status"], "fail")
        with tempfile.TemporaryDirectory() as d:
            card_png(Path(d) / "c0l0.png", 1800, 100)
            self.assertIn("1800 px", rv.check_card_text(spec, spec, Path(d))[1]["detail"][0])


class HygieneTests(unittest.TestCase):
    def test_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "v16.mp4"
            p.write_bytes(b"abc")
            st = p.stat()
            fp = {str(p): [st.st_size, st.st_mtime_ns], str(Path(d) / "gone"): {"size": 1, "mtime_ns": 1}}
            c = rv.check_fingerprints(fp)
            self.assertEqual((c["status"], c["value"]), ("fail", 1))
            del fp[str(Path(d) / "gone")]
            self.assertEqual(rv.check_fingerprints(fp)["status"], "pass")
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1000))
            self.assertEqual(rv.check_fingerprints(fp)["status"], "fail")

    def test_outputs_inside_rdir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "r001"
            rdir.mkdir()
            tl = {"sections": [{"picture": str(rdir / "sections" / "1_community.mp4"), "picture_rendered": True},
                               {"picture": "/elsewhere/v16.mp4"}]}
            self.assertEqual(rv.check_outputs(rdir, tl, rv._files(rdir, tl))["status"], "pass")
            tl["files"] = {"master": "/elsewhere/master.mp4"}
            self.assertEqual(rv.check_outputs(rdir, tl, rv._files(rdir, tl))["status"], "fail")
            tl["files"] = {"voice": str(Path(d) / "voice_cache" / "abc.wav")}
            self.assertEqual(rv.check_outputs(rdir, tl, rv._files(rdir, tl))["status"], "pass")


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.problems: list[str] = []
        stub = types.SimpleNamespace(validate=lambda plan: self.problems)
        self.enterContext(mock.patch.dict(sys.modules, {"render_plan": stub}))

    def test_one_change_per_op(self) -> None:
        edits = [{"id": "e1", "ops": [{"op": "swap_broll"}, {"op": "add_note"}]}]
        plan = {"changes": [{"edit": "e1", "op": "swap_broll", "section": 1, "key": "v16:1:17"}],
                "notes": [{"edit": "e1", "title": "reshoot"}, "D0292 is 50p"], "edits_applied": ["e1"]}
        got = {c["name"]: c["status"] for c in rv.check_plan(plan, edits)}
        self.assertEqual(got, {"plan_valid": "pass", "plan_refusals": "pass", "plan_changes": "pass"})
        plan["notes"] = []
        self.assertEqual(rv.check_plan(plan, edits)[2]["status"], "fail")

    def test_validate_problems_and_refusals_fail(self) -> None:
        self.problems = ["overlay 3 is 0.02 s from overlay 4"]
        plan = {"changes": [], "refusals": [{"label": "Cut 2.0 s", "reason": "cut_span"}], "edits_applied": ["e9"]}
        got = {c["name"]: c for c in rv.check_plan(plan)}
        self.assertEqual(got["plan_valid"]["detail"], self.problems)
        self.assertEqual(got["plan_refusals"]["status"], "fail")
        self.assertEqual(got["plan_changes"]["detail"], ["e9: no change"])


class VerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch.dict(sys.modules, {"render_plan": types.SimpleNamespace(validate=lambda plan: [])}))

    def test_missing_outputs_fail_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "r001"
            rdir.mkdir()
            tl = talk_tl(Path(d))
            tl["version"] = "r001"
            tl["sections"][0]["picture"] = str(Path(d) / "intro.mp4")
            lines: list[str] = []
            out = rv.verify(rdir, tl, {"dirty": [1], "changes": []}, {}, None, {}, log=lines.append)
            self.assertFalse(out["ok"])
            saved = json.loads((rdir / "verify.json").read_text())
            self.assertEqual(saved["version"], "r001")
            self.assertEqual(len(saved["checks"]), len(out["checks"]))
            names = {c["name"] for c in out["checks"]}
            for n in ("plan_valid", "timeline", "section_1_picture", "voice_format", "master_frames", "cuts_master",
                      "words", "fingerprints", "outputs_inside", "phone"):
                self.assertIn(n, names)
            self.assertTrue(all(set(c) >= {"name", "value", "threshold", "status"} for c in out["checks"]))
            self.assertTrue(all(c["status"] in ("pass", "warn", "fail") for c in out["checks"]))
            self.assertEqual({c["status"] for c in out["checks"] if c["name"] == "phone"}, {"warn"})
            self.assertTrue(lines[-1].startswith("verify FAILED"))
            self.assertFalse((rdir / "verify.json.tmp").exists())

    def test_a_malformed_timeline_is_a_failed_check(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = rv.verify(Path(d), {"sections": [{"name": "x", "kind": "talk"}]}, {"dirty": [3]}, {}, None, {},
                            log=lambda s: None)
            self.assertFalse(out["ok"])
            self.assertTrue((Path(d) / "verify.json").exists())
            self.assertIn("section_3_picture", {c["name"] for c in out["checks"]})

    def test_promoted_state(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            demo, rdir = Path(d) / "demo", Path(d) / "r001"
            demo.mkdir(), rdir.mkdir()
            (rdir / "verify.json").write_text(json.dumps({"ok": True, "checks": []}))
            cards = [{"caption": "C", "until": 3, "lines": [["A", 1.0]]}]
            tl = {"version": "r001", "edits_applied": ["e1"], "cards": cards}
            (demo / "edits.json").write_text("[]")
            (demo / "current.json").write_text(json.dumps({"version": "r001"}))
            (demo / "project.json").write_text(json.dumps({"version": "r001", "cards": [{"caption": "C", "lines": ["A"]}]}))
            self.assertTrue(rv.verify_promoted(demo, tl, rdir, log=lambda s: None)["ok"])
            self.assertTrue(json.loads((rdir / "verify.json").read_text())["promoted_ok"])
            (demo / "edits.json").write_text(json.dumps([{"id": "e1", "ops": []}]))
            out = rv.verify_promoted(demo, tl, log=lambda s: None)
            self.assertFalse(out["ok"])
            self.assertEqual({c["name"] for c in out["checks"] if c["status"] == "fail"},
                             {"promoted_edits", "promoted_no_reapply"})


if __name__ == "__main__":
    unittest.main()
