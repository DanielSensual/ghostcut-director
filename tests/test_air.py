"""Mic-air de-plosive + post-loudnorm limiter (AIR-BRIEF.md).

Offline throughout: synthetic numpy signals, filter-graph text checks,
a fake missing-numpy fallback, and short ffmpeg renders of numpy-made
WAVs. No network, no Meta API, no Whisper.
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from director import cli as cli_mod  # noqa: E402
from director import finish as finish_mod  # noqa: E402

FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]
HAS_NUMPY = np is not None


def clean_env(test: unittest.TestCase) -> None:
    patcher = mock.patch.dict(os.environ, {}, clear=False)
    patcher.start()
    test.addCleanup(patcher.stop)
    for var in ("ELEVENLABS_API_KEY", "HF_API_KEY_ID", "HF_API_KEY_SECRET"):
        os.environ.pop(var, None)


def make_keyring(store: dict | None = None):
    data: dict = store if store is not None else {}

    class PasswordDeleteError(Exception):
        pass

    module = types.ModuleType("keyring")
    module.errors = SimpleNamespace(PasswordDeleteError=PasswordDeleteError)
    module.get_keyring = lambda: object()
    module.get_password = lambda service, account: data.get((service, account))
    module.set_password = (
        lambda service, account, value: data.__setitem__((service, account), value))

    def _delete(service, account):
        try:
            del data[(service, account)]
        except KeyError:
            raise PasswordDeleteError("missing") from None

    module.delete_password = _delete
    return module


def _run(cmd: list[str]) -> None:
    done = subprocess.run(cmd, capture_output=True, text=True)
    assert done.returncode == 0, f"{cmd}\n{done.stderr}"


def _make_cut(path: Path, duration: float = 3.0) -> Path:
    _run(["ffmpeg", "-v", "error", "-y",
          "-f", "lavfi", "-i",
          f"testsrc2=size=320x180:rate=30:duration={duration}",
          "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
          "-af", "volume=0.05",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
          "-shortest", str(path)])
    return path


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _angles(duration: float = 3.0) -> dict:
    return {"version": 1,
            "audio": {"label": "A", "clip_path": "/media/A.mp4",
                      "offset_s": 0.0},
            "method": "rule", "spend_usd": 0.0, "warnings": [],
            "lines": [],
            "shots": [{"id": 0, "start_s": 0.0, "end_s": duration,
                       "camera": "A", "clip_path": "/media/A.mp4",
                       "clip_in_s": 0.0, "clip_out_s": duration,
                       "line_ids": [0], "shot_size": "medium",
                       "reason": "test", "method": "rule",
                       "overrules": []}]}


def _synth_voice(sr: int = 48000, duration: float = 6.0,
                 bursts: tuple[tuple[float, float], ...] = ((1.0, 1.2),
                                                            (3.0, 3.2))):
    """Speech-like base (440 Hz + a little 120 Hz) plus low bursts."""
    assert np is not None
    t = np.arange(int(sr * duration)) / float(sr)
    base = (0.05 * np.sin(2 * math.pi * 440.0 * t)
            + 0.010 * np.sin(2 * math.pi * 120.0 * t))
    for start, end in bursts:
        mask = (t >= start) & (t < end)
        base[mask] += 0.30 * np.sin(2 * math.pi * 70.0 * t[mask])
    # Avoid clipping the test WAV.
    peak = float(np.max(np.abs(base)))
    if peak >= 0.99:
        base = base / peak * 0.95
    return base


def _write_wav(path: Path, samples, sr: int = 48000) -> Path:
    assert np is not None
    pcm = (np.asarray(samples) * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(pcm.tobytes())
    return path


def _lowband_levels(path: Path):
    """30 ms RMS dB of the twice-lowpassed voice, via finish's filter."""
    assert np is not None
    with wave.open(str(path), "rb") as handle:
        sr = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    hop = int(round(sr * 0.03))
    nblocks = x.size // hop
    low = finish_mod._lowpass_twice(x[:nblocks * hop], sr)
    rms = np.sqrt(np.mean(low.reshape(nblocks, hop) ** 2, axis=1))
    with np.errstate(divide="ignore"):
        return 20.0 * np.log10(np.maximum(rms, 1e-12)), sr


@unittest.skipUnless(HAS_NUMPY, "needs numpy (the director extra)")
class ThresholdTests(unittest.TestCase):
    """The per-recording threshold on a synthetic voice-plus-bursts."""

    def test_two_bursts_two_moments(self) -> None:
        samples = _synth_voice()
        threshold, moments = (
            finish_mod.measure_deplosive_threshold_from_samples(samples, 48000))
        self.assertEqual(moments, 2)
        # Base low band sits near -43 dBFS; the threshold rides 6 dB above.
        self.assertGreater(threshold, -41.0)
        self.assertLess(threshold, -33.0)
        linear = finish_mod.dbfs_to_linear(threshold)
        self.assertGreaterEqual(linear, 0.000976563)
        self.assertLessEqual(linear, 1.0)

    def test_speech_gated_level(self) -> None:
        assert np is not None
        levels = np.array([-50.0, -45.0, -44.0, -43.0, -20.0, -19.0])
        # Louder half: -43, -20, -19 → median -20.
        self.assertAlmostEqual(finish_mod.speech_gated_level(levels), -20.0)

    def test_wav_file_measures_same(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="air-thr-")
        self.addCleanup(tmp.cleanup)
        wav = _write_wav(Path(tmp.name) / "v.wav", _synth_voice())
        threshold, moments, fallback = (
            finish_mod.measure_deplosive_threshold(wav))
        self.assertFalse(fallback)
        self.assertEqual(moments, 2)
        direct, _ = finish_mod.measure_deplosive_threshold_from_samples(
            _synth_voice(), 48000)
        self.assertAlmostEqual(threshold, direct, delta=0.2)


class FilterGraphTests(unittest.TestCase):
    """The de-plosive filter text, no ffmpeg needed."""

    def test_deplosive_block(self) -> None:
        thr = finish_mod.dbfs_to_linear(-36.8)
        for isolated in (False, True):
            cmd = finish_mod.clean_voice_command(
                Path("/v.wav"), Path("/c.wav"), isolated=isolated,
                deplosive_threshold=thr)
            self.assertIn("-filter_complex", cmd)
            graph = cmd[cmd.index("-filter_complex") + 1]
            self.assertIn("highpass=f=60", graph)
            self.assertEqual(graph.count("lowpass=f=160"), 2)
            self.assertEqual(graph.count("highpass=f=160"), 2)
            self.assertIn(f"acompressor=threshold={thr:.7g}", graph)
            self.assertIn("ratio=20:attack=0.3:release=40:knee=2", graph)
            self.assertIn("amix=inputs=2:normalize=0", graph)
            # The rest of the clean chain survives, after the mix.
            tail = graph.split("amix=inputs=2:normalize=0[mixed];[mixed]")[1]
            self.assertIn("acompressor=threshold=0.125", tail)
            if isolated:
                self.assertNotIn("afftdn", graph)
            else:
                self.assertIn("afftdn", graph)

    def test_none_keeps_original_chain(self) -> None:
        cmd = finish_mod.clean_voice_command(
            Path("/v.wav"), Path("/c.wav"), isolated=False)
        self.assertIn("-af", cmd)
        self.assertNotIn("-filter_complex", cmd)
        chain = cmd[cmd.index("-af") + 1]
        self.assertNotIn("lowpass=f=160", chain)
        self.assertNotIn("amix", chain)

    def test_limiter(self) -> None:
        cmd = finish_mod.limiter_command(Path("/u.wav"), Path("/m.wav"))
        chain = cmd[cmd.index("-af") + 1]
        self.assertEqual(
            chain, "alimiter=limit=0.891251:attack=5:release=50:level=0"
                   ":latency=1")

    def test_dbfs_to_linear(self) -> None:
        self.assertAlmostEqual(finish_mod.dbfs_to_linear(-36.0), 0.0158489,
                               places=6)
        self.assertAlmostEqual(finish_mod.dbfs_to_linear(-1.0), 0.8912509,
                               places=6)


class FallbackTests(unittest.TestCase):
    """Without numpy the threshold falls back to fixed −36 dBFS."""

    def test_no_numpy(self) -> None:
        with mock.patch.object(finish_mod, "np", None):
            threshold, moments, fallback = (
                finish_mod.measure_deplosive_threshold(Path("/none.wav")))
        self.assertEqual(threshold, -36.0)
        self.assertEqual(moments, 0)
        self.assertTrue(fallback)

    def test_unreadable_file(self) -> None:
        if not HAS_NUMPY:
            self.skipTest("needs numpy")
        threshold, moments, fallback = finish_mod.measure_deplosive_threshold(
            Path("/definitely/not/a/file.wav"))
        self.assertEqual(threshold, -36.0)
        self.assertEqual(moments, 0)
        self.assertTrue(fallback)


@unittest.skipUnless(FFMPEG and HAS_NUMPY,
                     "needs ffmpeg/ffprobe and numpy")
class BurstReductionTests(unittest.TestCase):
    """A burst is reduced far more than steady speech."""

    def test_burst_vs_steady(self) -> None:
        assert np is not None
        tmp = tempfile.TemporaryDirectory(prefix="air-burst-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        src = _write_wav(root / "src.wav", _synth_voice(duration=4.0,
                                                        bursts=((1.0, 1.25),
                                                                (2.5, 2.75))))
        threshold, moments, fallback = (
            finish_mod.measure_deplosive_threshold(src))
        self.assertFalse(fallback)
        self.assertEqual(moments, 2)
        dst = root / "clean.wav"
        from ghostcut.ffmpeg_tools import run_command

        run_command(finish_mod.clean_voice_command(
            src, dst, isolated=True,
            deplosive_threshold=finish_mod.dbfs_to_linear(threshold)))
        before, _ = _lowband_levels(src)
        after, _ = _lowband_levels(dst)
        hop_s = 0.03
        changes = []
        for moment in (1.12, 2.62):
            idx = int(moment / hop_s)
            before_peak = float(before[idx - 2:idx + 3].max())
            after_peak = float(after[idx - 2:idx + 3].max())
            changes.append(after_peak - before_peak)
        gated_before = finish_mod.speech_gated_level(before)
        gated_after = finish_mod.speech_gated_level(after)
        steady = gated_after - gated_before
        for change in changes:
            self.assertLess(change, -6.0)
        self.assertGreater(steady, -2.0)
        self.assertLess(min(changes), steady - 4.0)


@unittest.skipUnless(FFMPEG, "needs ffmpeg and ffprobe")
class FinishAirTests(unittest.TestCase):
    """De-plosive on by default, off with the flag; limiter runs."""

    def _job(self, duration: float = 3.0) -> Path:
        tmp = tempfile.TemporaryDirectory(prefix="air-job-")
        self.addCleanup(tmp.cleanup)
        job = Path(tmp.name) / "job"
        job.mkdir()
        _make_cut(job / "cut.mp4", duration=duration)
        _write_json(job / "angles.json", _angles(duration))
        _write_json(job / "words.json",
                    {"words": [{"word": "hello", "start": 0.5,
                                "end": 0.9}]})
        return job

    def test_default_on_records_threshold(self) -> None:
        clean_env(self)
        job = self._job()
        with mock.patch.dict(sys.modules, {"keyring": make_keyring({})}):
            payload = finish_mod.run(
                job_dir=job, model="muse-spark-1.3", no_muse=True,
                sound=False, isolate=False, loudness=None,
                max_provider_usd=2.0, max_usd=5.0)
        deplosive = payload["deplosive"]
        self.assertTrue(deplosive["enabled"])
        self.assertIsNotNone(deplosive["threshold_dbfs"])
        self.assertGreaterEqual(deplosive["moments"], 0)
        self.assertFalse((job / "finish" / "master_unlimited.wav").exists())
        after = payload["loudness_after"]
        self.assertIsNotNone(after)
        self.assertLessEqual(float(after["true_peak_dbtp"]), -0.8)

    def test_no_deplosive_flag(self) -> None:
        clean_env(self)
        job = self._job()
        with mock.patch.dict(sys.modules, {"keyring": make_keyring({})}):
            payload = finish_mod.run(
                job_dir=job, model="muse-spark-1.3", no_muse=True,
                sound=False, isolate=False, loudness=None,
                max_provider_usd=2.0, max_usd=5.0, deplosive=False)
        deplosive = payload["deplosive"]
        self.assertFalse(deplosive["enabled"])
        self.assertIsNone(deplosive["threshold_dbfs"])
        self.assertEqual(deplosive["moments"], 0)

    def test_cli_no_deplosive(self) -> None:
        clean_env(self)
        job = self._job()
        out, err = io.StringIO(), io.StringIO()
        with (mock.patch.dict(sys.modules, {"keyring": make_keyring({})}),
              contextlib.redirect_stdout(out),
              contextlib.redirect_stderr(err)):
            code = cli_mod.main(["finish", "--job", str(job), "--no-muse",
                                 "--no-deplosive"])
        self.assertEqual(code, 0)
        payload = json.loads((job / "finish.json").read_text())
        self.assertFalse(payload["deplosive"]["enabled"])

    def test_limiter_holds_hot_master(self) -> None:
        from ghostcut.ffmpeg_tools import run_command

        tmp = tempfile.TemporaryDirectory(prefix="air-limit-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        hot = root / "hot.wav"
        _run(["ffmpeg", "-v", "error", "-y",
              "-f", "lavfi", "-i", "sine=frequency=1000:duration=2",
              "-af", "volume=12.0", "-ac", "2", "-ar", "48000",
              "-c:a", "pcm_s16le", str(hot)])
        limited = root / "limited.wav"
        run_command(finish_mod.limiter_command(hot, limited))
        proc = run_command(
            finish_mod.loudnorm_measure_command(limited, -14.0), check=False)
        parsed = finish_mod.parse_loudnorm_full(proc.stderr or "")
        assert parsed is not None
        self.assertLessEqual(parsed["input_tp"], -0.9)


if __name__ == "__main__":
    unittest.main()
