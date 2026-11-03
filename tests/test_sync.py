from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from director import sync
from director.sync import (
    CameraInfo,
    ClipResult,
    Estimate,
    FlashEdge,
    SonySidecar,
    TranscriptWord,
    build_payload,
    estimates_agree,
    frame_duration_s,
    normalize_word,
    parse_captured_at,
    parse_methods,
    whisper_estimate,
)

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]

HAS_NUMPY = np is not None


def _camera(label: str = "A", fps: float = 30.0, duration: float = 60.0,
            audio: bool = True, **kw) -> CameraInfo:
    return CameraInfo(label=label, path=kw.pop("path", f"/c/{label}.mp4"), fps=fps,
                      width=kw.pop("width", 1920), height=kw.pop("height", 1080),
                      duration_s=duration, has_audio=audio, **kw)


def _clip(label: str, offset: float | None, duration: float = 60.0,
          fps: float = 30.0, **kw) -> ClipResult:
    return ClipResult(label, _camera(label, fps=fps, duration=duration, **kw),
                      offset, None)


def _estimate(method: str, offset: float, against: str = "A",
              confidence: float = 0.9, precision: float = 0.002,
              **detail) -> Estimate:
    return Estimate(method, offset, confidence, precision, against, dict(detail))


def _word(word: str, start: float) -> TranscriptWord:
    return TranscriptWord(word, start, start + 0.2)


def _probe_dict(duration: float = 60.0, fps: str = "30/1", audio: bool = True) -> dict:
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264",
                "width": 1920, "height": 1080,
                "r_frame_rate": fps, "avg_frame_rate": fps, "tags": {}}]
    if audio:
        streams.append({"index": 1, "codec_type": "audio", "codec_name": "aac",
                        "tags": {}})
    return {"streams": streams, "format": {"duration": str(duration), "tags": {}}}


class LtcTests(unittest.TestCase):
    def test_ltc_decode_plain(self) -> None:
        self.assertEqual(sync.decode_ltc_hex("21553713"), "13:37:55:21")

    def test_ltc_decode_masks_flag_bits(self) -> None:
        self.assertEqual(sync.decode_ltc_hex("E1D5B713"), "13:37:55:21")
        self.assertIsNone(sync.decode_ltc_hex("not-hex!"))
        self.assertIsNone(sync.decode_ltc_hex("123"))
        self.assertIsNone(sync.decode_ltc_hex("2A553713"))
        self.assertIsNone(sync.decode_ltc_hex("21753713"))

    def test_sony_sidecar_parse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "C0001.MP4"
            clip.write_bytes(b"")
            sidecar = Path(tmp) / "C0001M01.XML"
            sidecar.write_text(
                "<Sony>"
                '<CreationDate value="2026-11-02T14:03:11-05:00"/>'
                '<CaptureGammaEquation value="S-Log3.Cine"/>'
                '<LtcChangeTable tcFps="60">'
                '<LtcChange frameCount="1" value="22553713"/>'
                '<LtcChange frameCount="0" value="21553713"/>'
                "</LtcChangeTable></Sony>", encoding="utf-8")
            self.assertEqual(sync.find_sidecar(clip), sidecar)
            parsed = sync.parse_sony_sidecar(sidecar)
            self.assertEqual(parsed.timecode, "13:37:55:21")
            self.assertEqual(parsed.tc_fps, 60.0)
            self.assertEqual(parsed.captured_raw, "2026-11-02T14:03:11-05:00")
            self.assertEqual(parsed.gamma, "s-log3-cine")
            lone = Path(tmp) / "C0002.MP4"
            lone.write_bytes(b"")
            self.assertIsNone(sync.find_sidecar(lone))
            self.assertEqual(sync.parse_sony_sidecar(None), SonySidecar())
            bad = Path(tmp) / "C0002M01.XML"
            bad.write_text("<Sony><unclosed>", encoding="utf-8")
            self.assertEqual(sync.parse_sony_sidecar(bad), SonySidecar())


class TimecodeTests(unittest.TestCase):
    def test_timecode_seconds_24_and_25(self) -> None:
        self.assertAlmostEqual(sync.timecode_to_seconds("00:01:00:12", 24), 60.5)
        self.assertAlmostEqual(sync.timecode_to_seconds("00:01:00:12", 25), 60.48)
        self.assertEqual(sync.timecode_to_seconds("00:00:00:00", 24), 0.0)

    def test_timecode_seconds_2997_non_drop_and_drop(self) -> None:
        self.assertAlmostEqual(
            sync.timecode_to_seconds("00:01:00:00", 29.97), 60.06, places=3)
        self.assertAlmostEqual(
            sync.timecode_to_seconds("00:01:00;02", 29.97), 60.06, places=3)
        self.assertAlmostEqual(
            sync.timecode_to_seconds("00:10:00;00", 29.97), 600.0, delta=0.001)

    def test_timecode_seconds_5994(self) -> None:
        self.assertAlmostEqual(
            sync.timecode_to_seconds("00:01:00;04", 59.94), 60.06, places=3)
        self.assertAlmostEqual(
            sync.timecode_to_seconds("00:00:01:30", 59.94), 1.5015, places=4)

    def test_timecode_rejects_bad_input(self) -> None:
        with self.assertRaises(ValueError):
            sync.timecode_to_seconds("00:01:00;02", 25)
        with self.assertRaises(ValueError):
            sync.timecode_to_seconds("00:00:00:30", 30)
        with self.assertRaises(ValueError):
            sync.timecode_to_seconds("not a timecode", 30)

    def test_midnight_wrap(self) -> None:
        ref = _clip("A", 0.0, 60.0, timecode="00:00:05:00")
        tgt = _clip("B", None, 60.0, timecode="23:59:58:00")
        est = sync.timecode_estimate(tgt, ref)
        assert est is not None
        self.assertAlmostEqual(est.offset_s, -7.0)
        back = sync.timecode_estimate(ref, ClipResult(
            "B", _camera("B", timecode="23:59:58:00"), 0.0, None))
        assert back is not None
        self.assertAlmostEqual(back.offset_s, 7.0)


class ClockTests(unittest.TestCase):
    def test_clock_prefers_quicktime_creationdate(self) -> None:
        probe = {
            "streams": [
                {"index": 0, "codec_type": "video", "width": 3840, "height": 2160,
                 "r_frame_rate": "60000/1001", "avg_frame_rate": "60000/1001",
                 "color_transfer": "arib-std-b67",
                 "tags": {"com.apple.quicktime.creationdate": "2026-11-02T14:03:11-0400",
                          "com.apple.quicktime.make": "Apple"}},
                {"index": 1, "codec_type": "audio", "tags": {}}],
            "format": {"duration": "312.4",
                       "tags": {"creation_time": "2026-11-03T10:00:00Z"}}}
        cam = sync.camera_from_probe("B", "/c/B.mp4", probe, SonySidecar())
        self.assertEqual(cam.captured_at, "2026-11-02T14:03:11-04:00")
        self.assertEqual(cam.gamma, "hlg")
        self.assertIsNotNone(parse_captured_at("2026-11-02T14:03:11Z"))
        self.assertIsNotNone(parse_captured_at("2026-11-02 14:03:11+00:00"))
        self.assertIsNone(parse_captured_at("not a date"))
        self.assertIsNone(parse_captured_at(None))

    def test_camera_from_probe_fields(self) -> None:
        probe = {
            "streams": [
                {"index": 0, "codec_type": "video", "width": 3840, "height": 2160,
                 "r_frame_rate": "60000/1001", "avg_frame_rate": "60000/1001",
                 "color_transfer": "bt709",
                 "tags": {"make": "Sony", "model": "A7S III", "lensmodel": "24-70mm"}},
                {"index": 1, "codec_type": "audio", "tags": {}},
                {"index": 2, "codec_type": "data",
                 "tags": {"timecode": "13:37:55:21"}}],
            "format": {"duration": "312.4", "tags": {}}}
        sidecar = SonySidecar(gamma="s-log3-cine", tc_fps=60.0,
                              timecode="00:00:00:00")
        cam = sync.camera_from_probe("A", "/c/A.mp4", probe, sidecar)
        self.assertEqual((cam.make, cam.model, cam.lens), ("Sony", "A7S III", "24-70mm"))
        self.assertAlmostEqual(cam.fps, 59.94, places=2)
        self.assertEqual(cam.tc_rate, 60.0)
        self.assertEqual(cam.timecode, "13:37:55:21")
        self.assertEqual(cam.gamma, "s-log3-cine")
        lav = sync.camera_from_probe(
            "C", "/c/lav.wav",
            {"streams": [{"index": 0, "codec_type": "audio", "tags": {}}],
             "format": {"duration": "60.0", "tags": {}}}, SonySidecar())
        self.assertEqual((lav.fps, lav.width, lav.height), (0.0, 0, 0))
        self.assertTrue(lav.has_audio)
        self.assertEqual(lav.gamma, "")


def _bursts(dur_s: float, positions: list[float], burst_s: float = 0.8,
            amp: float = 1.0, seed: int = 0, noise: float = 0.0,
            noise_seed: int = 1):
    assert np is not None
    sr = sync.SAMPLE_RATE
    rng = np.random.default_rng(seed)
    out = np.zeros(int(dur_s * sr))
    for pos in positions:
        a = int(pos * sr)
        b = min(len(out), a + int(burst_s * sr))
        if a < len(out):
            out[a:b] = amp * rng.standard_normal(b - a)
    if noise:
        out = out + noise * np.random.default_rng(noise_seed).standard_normal(len(out))
    return out.astype(np.float32)


def _aperiodic_positions() -> list[float]:
    rng = __import__("random").Random(42)
    positions, pos = [], 2.0
    while pos < 66:
        positions.append(pos)
        pos += rng.uniform(2.5, 6.5)
    return positions


def _positions_until(end: float) -> list[float]:
    rng = __import__("random").Random(42)
    positions, pos = [], 2.0
    while pos < end:
        positions.append(pos)
        pos += rng.uniform(2.5, 6.5)
    return positions


def _envelopes(**paths: np.ndarray) -> dict:
    env: dict = {}
    for path, samples in paths.items():
        env[(path, 80)] = sync.log_envelope(samples, 80)
        env[(path, 8)] = sync.log_envelope(samples, 8)
    return env


@unittest.skipUnless(HAS_NUMPY, "waveform sync needs numpy")
class WaveformTests(unittest.TestCase):
    def test_waveform_known_offset_with_gain_and_noise(self) -> None:
        assert np is not None
        sr = sync.SAMPLE_RATE
        content = _bursts(70.0, _aperiodic_positions(), seed=7)
        anchor_s = content[:60 * sr].copy()
        target_s = (0.3 * content[int(6.25 * sr):int(66.25 * sr)]
                    + 0.05 * np.random.default_rng(3).standard_normal(60 * sr))
        env = _envelopes(**{"/c/A.mp4": anchor_s, "/c/B.mp4": target_s})
        est = sync.waveform_estimate(_clip("B", None), _clip("A", 0.0), env)
        assert est is not None
        self.assertAlmostEqual(est.offset_s, 6.25, delta=0.005)
        self.assertTrue(est.detail["clear"])
        self.assertGreater(est.confidence, 0.8)

    def test_waveform_sign_and_short_overlap_refused(self) -> None:
        sr = sync.SAMPLE_RATE
        content = _bursts(70.0, _aperiodic_positions(), seed=7)
        env = _envelopes(**{"/c/A.mp4": content[int(3 * sr):int(63 * sr)].copy(),
                            "/c/B.mp4": content[:60 * sr].copy()})
        est = sync.waveform_estimate(_clip("B", None), _clip("A", 0.0), env)
        assert est is not None
        self.assertAlmostEqual(est.offset_s, -3.0, places=2)
        # a lag overlapping by 4 s of a 60 s pair is below the 10 s floor
        ref_env = sync.log_envelope(content[:60 * sr], 80)
        tgt_env = sync.log_envelope(content[:60 * sr], 80)
        self.assertIsNone(sync.find_peak(ref_env, tgt_env, sync.COARSE_HOP_S,
                                         window_s=(56.0, 0.001)))

    def test_waveform_drift_from_resampled_copy(self) -> None:
        assert np is not None
        sr = sync.SAMPLE_RATE
        positions = list(np.arange(2, 158, 3.0))
        content = _bursts(160.0, positions, burst_s=1.5, seed=11)
        anchor_s = content[:150 * sr].copy()
        drift = 150e-6
        idx = np.arange(150 * sr) * (1 + drift)
        target_s = np.interp(idx, np.arange(len(content)), content).astype(np.float32)
        env = _envelopes(**{"/c/A.mp4": anchor_s, "/c/B.mp4": target_s})
        anchor = _clip("A", 0.0, duration=150.0)
        target = _clip("B", None, duration=150.0)
        est = sync.waveform_estimate(target, anchor, env)
        assert est is not None
        self.assertAlmostEqual(est.detail["drift_ppm"], 150.0, delta=40.0)
        self.assertAlmostEqual(est.offset_s, 0.0, delta=0.02)
        same = _envelopes(**{"/c/A.mp4": anchor_s, "/c/D.mp4": anchor_s.copy()})
        plain = sync.waveform_estimate(_clip("D", None, duration=150.0), anchor, same)
        assert plain is not None
        drifted = plain.detail["drift_ppm"]
        self.assertTrue(drifted is None or abs(drifted) < 40.0)

    def test_waveform_drift_at_large_offset_reports_start_offset(self) -> None:
        assert np is not None
        sr = sync.SAMPLE_RATE
        true_offset, target_dur, ref_dur = 120.0, 75.0, 270.0
        drift = 300e-6
        content = _bursts(280.0, _positions_until(278.0), burst_s=1.5, seed=11)
        anchor_s = content[:int(ref_dur * sr)].copy()
        src_idx = true_offset * sr + np.arange(int(target_dur * sr)) * (1 + drift)
        target_s = np.interp(src_idx, np.arange(len(content)),
                             content).astype(np.float32)
        env = _envelopes(**{"/c/A.mp4": anchor_s, "/c/B.mp4": target_s})
        est = sync.waveform_estimate(_clip("B", None, duration=target_dur),
                                     _clip("A", 0.0, duration=ref_dur), env)
        assert est is not None
        self.assertGreaterEqual(est.detail["drift_windows"], 3)
        self.assertAlmostEqual(est.detail["drift_ppm"], drift * 1e6, delta=60.0)
        self.assertAlmostEqual(est.offset_s, true_offset, delta=0.01)


@unittest.skipUnless(HAS_NUMPY, "waveform sync needs numpy")
class WaveformExactScoreTests(unittest.TestCase):
    def test_identical_audio_scores_one_never_above(self) -> None:
        assert np is not None
        sr = sync.SAMPLE_RATE
        content = _bursts(70.0, _aperiodic_positions(), seed=7)
        anchor_s = content[:60 * sr].copy()
        target_s = content[int(6 * sr):int(66 * sr)].copy()
        env = _envelopes(**{"/c/A.mp4": anchor_s, "/c/B.mp4": target_s})
        est = sync.waveform_estimate(_clip("B", None), _clip("A", 0.0), env)
        assert est is not None
        self.assertAlmostEqual(est.offset_s, 6.0, delta=0.005)
        self.assertLessEqual(est.detail["r"], 1.0)
        self.assertAlmostEqual(est.detail["r"], 1.0, places=3)
        self.assertLessEqual(est.detail["second_r"], 1.0)
        self.assertGreaterEqual(est.detail["second_r"], -1.0)

    def test_find_peak_scores_stay_within_bounds(self) -> None:
        assert np is not None
        rng = np.random.default_rng(11)
        ref = sync.log_envelope(
            rng.standard_normal(60 * sync.SAMPLE_RATE).astype(np.float32), 80)
        tgt = sync.log_envelope(
            rng.standard_normal(60 * sync.SAMPLE_RATE).astype(np.float32), 80)
        peak = sync.find_peak(ref, tgt, sync.COARSE_HOP_S)
        assert peak is not None
        self.assertLessEqual(peak.r, 1.0)
        self.assertGreaterEqual(peak.r, -1.0)
        self.assertLessEqual(peak.second_r, 1.0)
        self.assertGreaterEqual(peak.second_r, -1.0)
        same = sync.find_peak(ref, ref, sync.COARSE_HOP_S)
        assert same is not None
        self.assertLessEqual(same.r, 1.0)
        self.assertAlmostEqual(same.r, 1.0, places=6)

    def test_short_clip_rejects_loud_edge_decoy(self) -> None:
        assert np is not None
        sr = sync.SAMPLE_RATE
        ref = _bursts(150.0, _positions_until(125.0), seed=7, noise=0.02)
        true_start, tgt_dur = 60.0, 30.0
        span = ref[int(true_start * sr):int((true_start + tgt_dur) * sr)].copy()
        target = (2.5 * span).copy()
        target[:int(12 * sr)] *= 8.0
        target = (target + 0.3 * np.random.default_rng(3).standard_normal(
            len(target))).astype(np.float32)
        loud = ref.copy()
        loud[int(138 * sr):] = (
            12.0 * np.random.default_rng(99).standard_normal(
                len(loud) - int(138 * sr))).astype(np.float32)
        env = _envelopes(**{"/c/A.mp4": loud, "/c/B.mp4": target})
        est = sync.waveform_estimate(_clip("B", None, duration=tgt_dur),
                                     _clip("A", 0.0, duration=150.0), env)
        assert est is not None
        self.assertAlmostEqual(est.offset_s, true_start, delta=0.02)
        self.assertTrue(est.detail["clear"])
        self.assertLessEqual(est.detail["r"], 1.0)
        self.assertGreater(est.detail["overlap_s"], 20.0)

    def test_strong_competing_peak_is_not_clear(self) -> None:
        assert np is not None
        sr = sync.SAMPLE_RATE
        pattern = _bursts(20.0, _positions_until(18.0), seed=21, noise=0.02)
        ref = (0.02 * np.random.default_rng(22).standard_normal(
            int(80 * sr))).astype(np.float32)
        ref[int(10 * sr):int(30 * sr)] = pattern
        ref[int(40 * sr):int(60 * sr)] = pattern.copy()
        target = pattern.copy()
        env = _envelopes(**{"/c/A.mp4": ref, "/c/B.mp4": target})
        est = sync.waveform_estimate(_clip("B", None, duration=20.0),
                                     _clip("A", 0.0, duration=80.0), env)
        assert est is not None
        self.assertLess(min(abs(est.offset_s - 10.0), abs(est.offset_s - 40.0)),
                        0.02)
        self.assertFalse(est.detail["clear"])
        self.assertGreater(est.detail["second_r"], 0.9)
        self.assertLess(est.detail["margin"], sync.MARGIN_CLEAR)
        self.assertAlmostEqual(est.detail["margin"],
                               est.detail["r"] - est.detail["second_r"],
                               places=9)

    def test_whisper_hint_rescues_wrong_global_peak(self) -> None:
        assert np is not None
        sr = sync.SAMPLE_RATE
        pattern = _bursts(20.0, _positions_until(18.0), seed=31, noise=0.02)
        ref = (0.02 * np.random.default_rng(32).standard_normal(
            int(100 * sr))).astype(np.float32)
        ref[int(10 * sr):int(30 * sr)] = pattern
        ref[int(60 * sr):int(80 * sr)] = (
            0.8 * pattern + 0.05 * np.random.default_rng(33).standard_normal(
                len(pattern))).astype(np.float32)
        target = pattern.copy()
        env = _envelopes(**{"/c/A.mp4": ref, "/c/B.mp4": target})
        anchor = _clip("A", 0.0, duration=100.0)
        viewer = _clip("B", None, duration=20.0)
        glob = sync.waveform_estimate(viewer, anchor, env)
        assert glob is not None
        self.assertAlmostEqual(glob.offset_s, 10.0, delta=0.05)
        guide = (60.0, sync.PRECISION_S["whisper"] + sync.GUIDE_EXTRA_S,
                 "whisper")
        guided = sync.waveform_estimate(viewer, anchor, env, guide=guide)
        assert guided is not None
        self.assertAlmostEqual(guided.offset_s, 60.0, delta=0.02)
        self.assertLessEqual(guided.detail["r"], 1.0)
        hint = _estimate("whisper", 60.0, "A", 2 / 3, 0.15, moments=2,
                         votes=2, cluster_votes=2)
        chosen, taken = sync.resolve_guided(glob, hint, guided)
        self.assertIs(chosen, guided)
        self.assertTrue(taken)


@unittest.skipUnless(HAS_NUMPY, "waveform sync needs numpy")
class GuidedSearchTests(unittest.TestCase):
    def _words(self, sentences: list[tuple[list[str], float]]) -> list[TranscriptWord]:
        out: list[TranscriptWord] = []
        filler = 0
        for words, start in sentences:
            for k, word in enumerate(words):
                out.append(_word(word, start + 0.25 * k))
            filler += 1
            out.append(_word(f"filler{filler}x", start - 1.0))
        return out

    def _transcript_file(self, directory: Path, stem: str,
                         sentences: list[tuple[list[str], float]]) -> None:
        words = [{"word": w.word, "start": w.start_s, "end": w.end_s}
                 for w in self._words(sentences) if not w.word.startswith("filler")]
        payload = {"segments": [{"words": words}]}
        (directory / f"{stem}.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_short_clip_false_peak_corrected_by_whisper_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("A.mp4", "B.mp4"):
                (root / name).write_bytes(b"")
            transcripts = root / "transcripts"
            transcripts.mkdir()
            anchor_sentences = [(["the", "cat", "sat"], 10.0),
                                (["dogs", "run", "fast"], 50.0)]
            target_sentences = [(["the", "cat", "sat"], 0.0),
                                (["dogs", "run", "fast"], 40.0)]
            self._transcript_file(transcripts, "A", anchor_sentences)
            self._transcript_file(transcripts, "B", target_sentences)
            probes = {str(root / "A.mp4"): _probe_dict(60.0),
                      str(root / "B.mp4"): _probe_dict(8.0)}
            calls: list = []

            def fake_wave(target: ClipResult, anchor: ClipResult, envelopes: dict,
                          *, guide=None):
                calls.append(guide)
                if guide is None:
                    return _estimate("waveform", 40.0, anchor.label, 0.7,
                                     0.002, r=0.7, second_r=0.65, margin=0.05,
                                     clear=False, overlap_s=8.0, guided=False,
                                     guide_method=None, drift_ppm=None,
                                     drift_windows=0)
                return _estimate("waveform", 10.0, anchor.label, 0.9, 0.002,
                                 r=0.9, second_r=-1.0, margin=1.9, clear=True,
                                 overlap_s=8.0, guided=True, guide_method=guide[2],
                                 drift_ppm=None, drift_windows=0)

            with mock.patch("director.sync.probe_clip",
                             side_effect=lambda p: probes[str(p)]), \
                    mock.patch("director.sync.waveform_estimate",
                               side_effect=fake_wave):
                payload = sync.run(
                    clip_paths=[root / "A.mp4", root / "B.mp4"],
                    reference_path=None, methods=["waveform", "whisper"],
                    transcripts_dir=transcripts, out_path=root / "sync.json")
            self.assertEqual(len(calls), 2)
            self.assertIsNone(calls[0])
            assert calls[1] is not None
            self.assertAlmostEqual(calls[1][0], 10.0, places=2)
            self.assertAlmostEqual(calls[1][1], 0.65, places=2)
            placed = payload["clips"][1]
            self.assertEqual(placed["label"], "B")
            self.assertAlmostEqual(placed["offset_s"], 10.0)
            self.assertEqual(placed["method"], "waveform")
            guided = next(e for e in placed["estimates"] if e["method"] == "waveform")
            self.assertIn("unguided", guided["detail"])
            self.assertAlmostEqual(guided["detail"]["unguided"]["offset_s"], 40.0)

    def test_resolve_guided_keeps_strong_global(self) -> None:
        strong = _estimate("waveform", 6.0, "A", 0.9, 0.002,
                           r=0.9, second_r=0.5, margin=0.4, clear=True)
        weak_guide = _estimate("whisper", 6.4, "A", 0.667, 0.15, moments=2)
        peaked = _estimate("waveform", 6.4, "A", 0.5, 0.002,
                            r=0.5, second_r=0.4, margin=0.1, clear=True)
        kept, taken = sync.resolve_guided(strong, weak_guide, peaked)
        self.assertIs(kept, strong)
        self.assertFalse(taken)
        shaky = _estimate("waveform", 6.0, "A", 0.6, 0.002,
                          r=0.6, second_r=0.59, margin=0.01, clear=False)
        close = _estimate("waveform", 6.4, "A", 0.59, 0.002,
                           r=0.59, second_r=0.4, margin=0.19, clear=True)
        chosen, taken = sync.resolve_guided(shaky, weak_guide, close)
        self.assertIs(chosen, close)
        self.assertTrue(taken)
        kept, taken = sync.resolve_guided(shaky, weak_guide, None)
        self.assertIs(kept, shaky)
        self.assertFalse(taken)


def _sentence(words: list[str], start: float, step: float = 0.25) -> list[TranscriptWord]:
    return [_word(w, start + step * k) for k, w in enumerate(words)]


class WhisperTests(unittest.TestCase):
    def test_normalize_word(self) -> None:
        self.assertEqual(normalize_word("Hello,"), "hello")
        self.assertEqual(normalize_word('"world!"'), "world")
        self.assertEqual(normalize_word("don't"), "don't")

    def _padded(self, sentences: list[tuple[list[str], float]],
                  tag: str) -> list[TranscriptWord]:
        out: list[TranscriptWord] = []
        tick = 0.0
        for words, start in sentences:
            for k, word in enumerate(words):
                out.append(_word(word, start + 0.25 * k))
            for _ in range(3):
                tick += 0.25
                out.append(_word(f"{tag}pad{tick:.2f}", start - 2.0 + tick))
        return out

    def test_whisper_votes_and_moments(self) -> None:
        anchor = self._padded(
            [(["the", "cat", "sat"], 10.0),
             (["thank", "you", "very", "much"], 20.0),
             (["thank", "you", "very", "much"], 22.0),
             (["dogs", "run", "fast"], 30.0)], "a")
        target = self._padded(
            [(["the", "cat", "sat"], 4.0),
             (["thank", "you", "very", "much"], 14.0),
             (["dogs", "run", "fast"], 24.07)], "b")
        est = whisper_estimate(_clip("B", None), _clip("A", 0.0), target, anchor)
        assert est is not None
        self.assertAlmostEqual(est.offset_s, 6.0, delta=0.05)
        self.assertEqual(est.detail["moments"], 2)
        self.assertAlmostEqual(est.confidence, 2 / 3)
        self.assertEqual(est.detail["votes"], 2)

    def test_whisper_one_sentence_coincidence_refused(self) -> None:
        shared = ["the", "same", "question", "asked", "twice"]
        anchor = _sentence(shared, 10.0) + _sentence(["wholly", "other", "words"], 40.0)
        target = _sentence(["quite", "different", "matter"], 2.0) + _sentence(shared, 50.0)
        self.assertIsNone(whisper_estimate(_clip("B", None), _clip("A", 0.0),
                                           target, anchor))

    def test_whisper_halved_when_sound_disagrees(self) -> None:
        said = _estimate("whisper", 6.0, "A", 2 / 3, 0.15, moments=2,
                         votes=2, cluster_votes=2)
        weak = _estimate("waveform", 6.0, "A", 0.2, 0.002,
                         r=0.2, second_r=0.1, margin=0.1, clear=False)
        self.assertTrue(sync.penalise_whisper(said, weak, True))
        self.assertAlmostEqual(said.confidence, (2 / 3) / 2)
        self.assertTrue(said.detail["confidence_halved"])
        self.assertEqual(said.detail["confidence_before_halving"], 2 / 3)
        offset, method = sync.choose_pick([said], [])
        self.assertEqual((offset, method), (None, None))
        fresh = _estimate("whisper", 6.0, "A", 2 / 3, 0.15, moments=2)
        self.assertFalse(sync.penalise_whisper(fresh, weak, False))
        self.assertAlmostEqual(fresh.confidence, 2 / 3)


class AgreementTests(unittest.TestCase):
    def test_agreement_thresholds(self) -> None:
        frame = 1 / 30
        wave = _estimate("waveform", 6.0, "A", 0.9, 0.002)
        near_tc = _estimate("timecode", 6.01, "A", 0.5, 0.0)
        far_tc = _estimate("timecode", 6.05, "A", 0.5, 0.0)
        checks = sync.build_checks([wave, near_tc], frame)
        self.assertTrue(checks[0].agree)
        checks = sync.build_checks([wave, far_tc], frame)
        self.assertFalse(checks[0].agree)
        clock = _estimate("clock", 7.9, "A", 0.2, 2.0)
        checks = sync.build_checks([_estimate("waveform", 6.0, "A"), clock], frame)
        self.assertTrue(checks[0].agree)
        clock_far = _estimate("clock", 8.1, "A", 0.2, 2.0)
        checks = sync.build_checks([_estimate("waveform", 6.0, "A"), clock_far], frame)
        self.assertFalse(checks[0].agree)
        self.assertEqual(frame_duration_s(_clip("B", None), _clip("A", 0.0)), 1 / 30)


class SelectionTests(unittest.TestCase):
    def _target(self) -> ClipResult:
        return _clip("B", None, duration=60.0)

    def test_selection_order_and_warnings(self) -> None:
        target = self._target()
        # 1. a clear waveform beats an agreeing Whisper
        wave = _estimate("waveform", 6.0, "A", 0.9, 0.002,
                         r=0.9, second_r=0.5, margin=0.4, clear=True)
        said = _estimate("whisper", 6.0, "A", 1.0, 0.15, moments=3,
                         votes=3, cluster_votes=3)
        checks = sync.build_checks([wave, said], 1 / 30)
        self.assertEqual(sync.choose_pick([wave, said], checks), (6.0, "waveform"))
        self.assertEqual(sync.attempt_warnings(target, [wave, said], checks,
                                               "waveform", 6.0), [])
        # 2. an unclear waveform backed by Whisper still picks the waveform
        shaky = _estimate("waveform", 6.0, "A", 0.5, 0.002,
                          r=0.5, second_r=0.48, margin=0.02, clear=False)
        checks = sync.build_checks([shaky, said], 1 / 30)
        self.assertEqual(sync.choose_pick([shaky, said], checks), (6.0, "waveform"))
        # 3. an unbacked unclear waveform falls through to Whisper
        stray = _estimate("whisper", 6.5, "A", 2 / 3, 0.15, moments=2,
                          votes=2, cluster_votes=2)
        checks = sync.build_checks([shaky, stray], 1 / 30)
        self.assertEqual(sync.choose_pick([shaky, stray], checks), (6.5, "whisper"))
        # 4. Whisper below threshold falls through to flagged timecode
        halved = _estimate("whisper", 6.5, "A", 1 / 3, 0.15, moments=2,
                           confidence_halved=True)
        stamped = _estimate("timecode", 7.25, "A", 0.5, 0.0)
        checks = sync.build_checks([halved, stamped], 1 / 30)
        self.assertEqual(sync.choose_pick([halved, stamped], checks), (7.25, "timecode"))
        warnings = sync.attempt_warnings(target, [halved, stamped], checks,
                                         "timecode", 7.25)
        self.assertIn("Placed by timecode only: right only if the cameras were jammed.",
                      warnings)
        # 5. with no timecode, the clock places the clip flagged
        tick = _estimate("clock", 6.2, "A", 0.2, 2.0)
        checks = sync.build_checks([tick], 1 / 30)
        self.assertEqual(sync.choose_pick([tick], checks), (6.2, "clock"))
        self.assertIn("Placed by capture clock only: check by eye.",
                      sync.attempt_warnings(target, [tick], checks, "clock", 6.2))
        # disagreements against a waveform pick warn, with the jam suffix for timecode
        said_far = _estimate("whisper", 6.5, "A", 2 / 3, 0.15, moments=2,
                             votes=2, cluster_votes=2)
        checks = sync.build_checks([wave, said_far, stamped], 1 / 30)
        warnings = sync.attempt_warnings(target, [wave, said_far, stamped], checks,
                                         "waveform", 6.0)
        self.assertIn("Whisper disagrees with the chosen offset by 0.500 s.", warnings)
        self.assertIn("Timecode disagrees with the chosen offset by 1.250 s. "
                      "It is right only if the cameras were jammed.", warnings)
        # nothing usable places nothing
        self.assertEqual(sync.choose_pick([], []), (None, None))
        self.assertEqual(sync.attempt_warnings(target, [], [], None, None),
                         ["Could not be placed on the timeline."])


class FlashEdgeTests(unittest.TestCase):
    def _luma_drop(self, fps: float = 30.0, n_pre: int = 60,
                   n_dark: int = 30, n_post: int = 60,
                   bright: float = 150.0, dark: float = 30.0
                   ) -> tuple[list[float], list[float]]:
        n = n_pre + n_dark + n_post
        times = [i / fps for i in range(n)]
        luma = ([bright] * n_pre + [dark] * n_dark + [bright] * n_post)
        return times, luma

    def test_find_flash_edges_down_then_up(self) -> None:
        times, luma = self._luma_drop()
        edges = sync.find_flash_edges(times, luma)
        self.assertEqual(len(edges), 2)
        self.assertEqual([e.sign for e in edges], [-1, 1])
        self.assertAlmostEqual(edges[0].time_s, 60 / 30, places=6)
        self.assertAlmostEqual(edges[1].time_s, 90 / 30, places=6)
        self.assertAlmostEqual(edges[0].magnitude, 120.0)
        self.assertAlmostEqual(edges[1].magnitude, 120.0)

    def test_find_flash_edges_ignores_noise(self) -> None:
        times = [i / 30.0 for i in range(300)]
        luma = [100.0 + (1.0 if i % 2 else -1.0) for i in range(300)]
        self.assertEqual(sync.find_flash_edges(times, luma), [])
        self.assertEqual(sync.find_flash_edges([], []), [])
        self.assertEqual(sync.find_flash_edges([0.0], [100.0]), [])

    def test_find_flash_edges_merges_close_steps(self) -> None:
        times = [i / 30.0 for i in range(90)]
        luma = [150.0] * 30 + [100.0, 30.0] + [30.0] * 28 + [150.0] * 30
        edges = sync.find_flash_edges(times, luma)
        self.assertEqual(len(edges), 2)
        self.assertEqual(edges[0].sign, -1)
        self.assertAlmostEqual(edges[0].time_s, 30 / 30, places=6)
        self.assertAlmostEqual(edges[0].magnitude, 120.0)

    def test_flash_confidence_and_precision(self) -> None:
        self.assertAlmostEqual(sync.flash_confidence(1), 0.35)
        self.assertAlmostEqual(sync.flash_confidence(2), 0.75)
        self.assertGreater(sync.flash_confidence(3), sync.flash_confidence(2))
        self.assertGreaterEqual(sync.flash_confidence(5), 0.9)
        target = _clip("B", None, fps=30.0)
        anchor = _clip("A", 0.0, fps=60.0)
        self.assertAlmostEqual(sync.flash_precision_s(target, anchor), 1 / 30)
        slow = _clip("C", None, fps=24.0)
        self.assertAlmostEqual(sync.flash_precision_s(slow, anchor), 1 / 24)


class FlashMatchTests(unittest.TestCase):
    def test_match_two_edges_median_offset(self) -> None:
        target = [FlashEdge(5.0, -1, 100.0), FlashEdge(6.0, 1, 100.0)]
        anchor = [FlashEdge(10.0, -1, 90.0), FlashEdge(11.0, 1, 90.0)]
        matched = sync.match_flash_edges(target, anchor, 1 / 30 + 0.05)
        assert matched is not None
        median, pairs = matched
        self.assertAlmostEqual(median, 5.0)
        self.assertEqual(pairs, [(0, 0), (1, 1)])

    def test_match_rejects_wrong_signs_and_gaps(self) -> None:
        target = [FlashEdge(5.0, -1, 100.0), FlashEdge(6.0, 1, 100.0)]
        opposite = [FlashEdge(10.0, 1, 90.0)]
        lone_down = [FlashEdge(5.0, -1, 100.0)]
        self.assertIsNone(sync.match_flash_edges(lone_down, opposite, 0.08))
        stretched = [FlashEdge(10.0, -1, 90.0), FlashEdge(12.0, 1, 90.0)]
        matched = sync.match_flash_edges(target, stretched, 0.08)
        assert matched is not None
        self.assertEqual(len(matched[1]), 1)
        self.assertIsNone(sync.match_flash_edges([], opposite, 0.08))
        self.assertIsNone(sync.match_flash_edges(target, [], 0.08))

    def test_match_prefers_clock_consistent_single_edge(self) -> None:
        target = [FlashEdge(5.0, -1, 100.0)]
        anchor = [FlashEdge(10.0, -1, 90.0), FlashEdge(60.0, -1, 90.0)]
        plain = sync.match_flash_edges(target, anchor, 0.08)
        assert plain is not None
        self.assertAlmostEqual(plain[0], 5.0)
        guided = sync.match_flash_edges(target, anchor, 0.08, clock_relative_s=55.0)
        assert guided is not None
        self.assertAlmostEqual(guided[0], 55.0)
        self.assertEqual(guided[1], [(0, 1)])


class FlashResolutionTests(unittest.TestCase):
    def _target(self) -> ClipResult:
        return _clip("B", None, duration=60.0)

    def test_audio_wins_over_flash_flash_beats_timecode_clock(self) -> None:
        wave = _estimate("waveform", 6.0, "A", 0.9, 0.002,
                         r=0.9, second_r=0.5, margin=0.4, clear=True)
        flash = _estimate("flash", 6.0, "A", 0.75, 1 / 30, matched_edges=2)
        checks = sync.build_checks([wave, flash], 1 / 30)
        self.assertEqual(sync.choose_pick([wave, flash], checks), (6.0, "waveform"))
        said = _estimate("whisper", 6.5, "A", 2 / 3, 0.15, moments=2,
                         votes=2, cluster_votes=2)
        flash_far = _estimate("flash", 6.0, "A", 0.75, 1 / 30, matched_edges=2)
        checks = sync.build_checks([said, flash_far], 1 / 30)
        self.assertEqual(sync.choose_pick([said, flash_far], checks),
                         (6.5, "whisper"))
        stamped = _estimate("timecode", 7.25, "A", 0.5, 0.0)
        checks = sync.build_checks([flash_far, stamped], 1 / 30)
        self.assertEqual(sync.choose_pick([flash_far, stamped], checks),
                         (6.0, "flash"))
        tick = _estimate("clock", 6.2, "A", 0.2, 2.0)
        checks = sync.build_checks([flash_far, tick], 1 / 30)
        self.assertEqual(sync.choose_pick([flash_far, tick], checks),
                         (6.0, "flash"))

    def test_shaky_waveform_backed_by_flash(self) -> None:
        shaky = _estimate("waveform", 6.0, "A", 0.5, 0.002,
                          r=0.5, second_r=0.48, margin=0.02, clear=False)
        flash = _estimate("flash", 6.01, "A", 0.75, 1 / 30, matched_edges=2)
        checks = sync.build_checks([shaky, flash], 1 / 30)
        self.assertTrue(checks[0].agree)
        self.assertEqual(sync.choose_pick([shaky, flash], checks),
                         (6.0, "waveform"))

    def test_flash_disagreement_and_single_edge_warnings(self) -> None:
        target = self._target()
        wave = _estimate("waveform", 6.0, "A", 0.9, 0.002,
                         r=0.9, second_r=0.5, margin=0.4, clear=True)
        flash_far = _estimate("flash", 6.5, "A", 0.75, 1 / 30, matched_edges=2)
        checks = sync.build_checks([wave, flash_far], 1 / 30)
        warnings = sync.attempt_warnings(target, [wave, flash_far], checks,
                                         "waveform", 6.0)
        self.assertIn("Flash disagrees with the chosen offset by 0.500 s.",
                      warnings)
        lone = _estimate("flash", 6.0, "A", 0.35, 1 / 30, matched_edges=1)
        checks = sync.build_checks([lone], 1 / 30)
        warnings = sync.attempt_warnings(target, [lone], checks, "flash", 6.0)
        self.assertIn("Placed by a single light flash: check by eye.", warnings)

    def test_no_audio_no_flash_clock_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("A.mp4", "B.mp4"):
                (root / name).write_bytes(b"")
            probes = {
                str(root / "A.mp4"): _probe_dict(60.0, audio=False),
                str(root / "B.mp4"): _probe_dict(60.0, audio=False),
            }
            with mock.patch("director.sync.probe_clip",
                             side_effect=lambda p: probes[str(p)]), \
                    mock.patch("director.sync.decode_flash_luma",
                               side_effect=lambda p, window_s=120.0: None):
                cams = [sync.camera_from_probe("", str(root / n),
                                               probes[str(root / n)],
                                               SonySidecar())
                        for n in ("A.mp4", "B.mp4")]
                cams[0].captured_at = "2026-11-02T14:03:11+00:00"
                cams[1].captured_at = "2026-11-02T14:03:16+00:00"
                ref = ClipResult("A", cams[0], 0.0, "reference")
                tgt = ClipResult("B", cams[1], None, None)
                ests = sync._attempt_estimates(tgt, ref, ["clock", "flash"],
                                               {}, {}, None, root, {})
                self.assertEqual([e.method for e in ests], ["clock"])
                checks = sync.build_checks(ests, 1 / 30)
                offset, method = sync.choose_pick(ests, checks)
                self.assertEqual((offset, method), (5.0, "clock"))


@unittest.skipUnless(HAS_NUMPY and shutil.which("ffmpeg"), "flash sync needs numpy and ffmpeg")
class FlashFfmpegTests(unittest.TestCase):
    def _make_clip(self, path: Path, drop: tuple[float, float],
                   with_audio: bool) -> None:
        start, end = drop
        video = (f"color=c=white:s=128x128:r=30:d=10,"
                 f"eq=brightness=-0.6:enable='between(t,{start},{end})'")
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", video]
        if with_audio:
            cmd += ["-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=44100:duration=10",
                    "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
        else:
            cmd += ["-map", "0:v:0", "-an"]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(path)]
        subprocess.run(cmd, capture_output=True, timeout=120, check=True)

    def test_flash_places_silent_clip_within_a_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            anchor_path = root / "A.mp4"
            target_path = root / "B.mp4"
            self._make_clip(anchor_path, (2.0, 3.0), with_audio=True)
            self._make_clip(target_path, (5.0, 6.0), with_audio=False)
            payload = sync.run(
                clip_paths=[anchor_path, target_path],
                reference_path=anchor_path, methods=["flash"],
                transcripts_dir=None, out_path=root / "sync.json")
            by_label = {c["label"]: c for c in payload["clips"]}
            placed = by_label["B"]
            self.assertEqual(placed["method"], "flash")
            self.assertAlmostEqual(placed["offset_s"], -3.0, delta=1 / 30 + 0.005)
            flash = next(e for e in placed["estimates"] if e["method"] == "flash")
            self.assertEqual(flash["against"], "A")
            self.assertEqual(flash["detail"]["matched_edges"], 2)
            self.assertAlmostEqual(flash["precision_s"], 1 / 30, places=3)


@unittest.skipUnless(HAS_NUMPY, "waveform sync needs numpy")
class NonReferenceAnchorTests(unittest.TestCase):
    def _content(self):
        assert np is not None
        sr = sync.SAMPLE_RATE
        n = int(72.0 * sr)
        rng = np.random.default_rng(5)
        walk = rng.standard_normal(n // 400 + 1)
        slow = np.interp(np.arange(n), np.linspace(0, n - 1, len(walk)), walk)
        slow = np.convolve(slow, np.ones(400) / 400, mode="same")
        bed = 0.3 + 0.7 * np.abs(slow) / max(1e-9, np.abs(slow).max())
        out = rng.standard_normal(n) * bed
        placer = np.random.default_rng(9)
        pos = 2.0
        while pos < 68:
            a = int(pos * sr)
            b = min(n, a + int(0.5 * sr))
            out[a:b] += 1.5 * np.random.default_rng(int(pos * 10)).standard_normal(b - a)
            pos += float(placer.uniform(4.0, 8.0))
        return out.astype(np.float32), sr

    def _run_three(self, root: Path, third: str):
        content, sr = self._content()
        takes = {"A.mp4": content[:60 * sr].copy(),
                 "B.mp4": content[int(5 * sr):int(65 * sr)].copy(),
                 third: content[int(60 * sr):int(70 * sr)].copy()
                 if third == "C.mp4"
                 else content[int(20 * sr):int(30 * sr)].copy()}
        durations = {"A.mp4": 60.0, "B.mp4": 60.0, "C.mp4": 10.0, "C2.mp4": 10.0}
        for name in ("A.mp4", "B.mp4", third):
            (root / name).write_bytes(b"")
        probes = {str(root / name): _probe_dict(durations[name]) for name in takes}
        decoded: list[str] = []

        def fake_decode(path, *, has_audio=True):
            decoded.append(Path(path).name)
            return takes[Path(path).name]

        with mock.patch("director.sync.probe_clip",
                         side_effect=lambda p: probes[str(p)]), \
                mock.patch("director.sync.decode_samples",
                           side_effect=fake_decode):
            return sync.run(clip_paths=[root / "A.mp4", root / "B.mp4",
                                        root / third],
                            reference_path=None, methods=["waveform"],
                            transcripts_dir=None,
                            out_path=root / "sync.json"), decoded

    def test_placing_against_non_reference_clip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload, _decoded = self._run_three(Path(tmp), "C.mp4")
            by_label = {c["label"]: c for c in payload["clips"]}
            self.assertAlmostEqual(by_label["B"]["offset_s"], 5.0, places=2)
            placed = by_label["C"]
            self.assertAlmostEqual(placed["offset_s"], 60.0, places=2)
            self.assertEqual(placed["method"], "waveform")
            waves = [e for e in placed["estimates"] if e["method"] == "waveform"]
            self.assertTrue(waves)
            winner = next(e for e in waves if abs(e["offset_s"] - 60.0) < 0.01)
            self.assertEqual(winner["against"], "B")

    def test_trusted_waveform_stops_before_later_anchors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload, _decoded = self._run_three(Path(tmp), "C2.mp4")
            by_label = {c["label"]: c for c in payload["clips"]}
            placed = by_label["C"]
            self.assertAlmostEqual(placed["offset_s"], 20.0, places=2)
            against = {e["against"] for e in placed["estimates"]}
            self.assertEqual(against, {"A"})


class PayloadTests(unittest.TestCase):
    def _results(self):
        ref = ClipResult("A", _camera("A", fps=59.94, duration=312.4,
                                      gamma="s-log3-cine",
                                      timecode="13:37:55:21", tc_rate=60.0,
                                      path="/c/A.mp4"), 0.0, "reference")
        wave = _estimate("waveform", 6.0, "A", 0.9, 0.002,
                         r=0.9, second_r=0.3, margin=0.6, clear=True,
                         overlap_s=54.0, guided=False, guide_method=None,
                         drift_ppm=None, drift_windows=0)
        said = _estimate("whisper", 6.004, "A", 1.0, 0.15,
                         votes=3, cluster_votes=3, moments=3)
        checks = sync.build_checks([wave, said], 1 / 59.94)
        placed = ClipResult("B", _camera("B", fps=59.94, duration=300.0,
                                         gamma="hlg", path="/c/B.mp4"),
                            6.0, "waveform", [wave, said], checks, None, [])
        missing = ClipResult("C", _camera("C", path="/c/C.mp4"), None, None,
                             [], [], None, ["Could not be placed on the timeline."])
        return [ref, placed, missing]

    def test_sync_json_shape_round_trips_through_angles(self) -> None:
        from director.angles import load_sync
        from director.grade import load_sync_for_grade

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sync.json"
            payload = build_payload(self._results(), "/c/A.mp4", list(sync.METHODS))
            Path(path).write_text(json.dumps(payload), encoding="utf-8")
            reference, clips = load_sync(path)
            self.assertEqual(reference, "/c/A.mp4")
            by_label = {c.label: c for c in clips}
            self.assertEqual(by_label["A"].offset_s, 0.0)
            self.assertEqual(by_label["B"].offset_s, 6.0)
            self.assertIsNone(by_label["C"].offset_s)
            ref_label, grade_clips = load_sync_for_grade(path)
            self.assertEqual(ref_label, "A")
            gammas = {c.label: c.gamma for c in grade_clips}
            self.assertEqual(gammas, {"A": "s-log3-cine", "B": "hlg", "C": ""})

    def test_parse_methods(self) -> None:
        self.assertEqual(parse_methods(None),
                         ["timecode", "clock", "waveform", "flash", "whisper"])
        self.assertEqual(parse_methods("whisper,timecode,whisper"),
                         ["timecode", "whisper"])
        self.assertEqual(parse_methods("flash"),
                         ["flash"])
        self.assertEqual(parse_methods("whisper,flash,waveform"),
                         ["waveform", "flash", "whisper"])
        with self.assertRaises(ValueError):
            parse_methods("bogus")
        with self.assertRaises(ValueError):
            parse_methods("")


class CliTests(unittest.TestCase):
    def _payload(self) -> dict:
        return {
            "reference": "a.mp4", "methods": ["waveform"],
            "clips": [
                {"label": "A", "offset_s": 0.0, "method": "reference",
                 "camera": {"path": "a.mp4", "make": "Sony", "model": "A7S III",
                            "gamma": "s-log3-cine", "fps": 59.94},
                 "checks": [], "warnings": []},
                {"label": "B", "offset_s": 6.0, "method": "waveform",
                 "camera": {"path": "b.mp4", "make": "", "model": "",
                            "gamma": "", "fps": 30.0},
                 "checks": [{"a": "waveform", "b": "whisper", "against_a": "A",
                             "against_b": "A", "delta_s": 0.004,
                             "threshold_s": 0.167, "agree": True}],
                 "warnings": ["Whisper disagrees with the chosen offset by 0.500 s."]},
                {"label": "C", "offset_s": None, "method": None,
                 "camera": {"path": "lav.wav", "make": "", "model": "",
                            "gamma": "", "fps": 0.0},
                 "checks": [], "warnings": ["Could not be placed on the timeline."]},
            ],
        }

    def test_cli_parser_and_print(self) -> None:
        from director.cli import build_parser, command_sync

        parser = build_parser()
        args = parser.parse_args(["sync", "a.mp4", "b.mp4", "--out", "s.json"])
        self.assertEqual(args.command, "sync")
        self.assertEqual(args.clips, ["a.mp4", "b.mp4"])
        self.assertEqual(args.out, "s.json")
        self.assertIsNone(args.methods)
        self.assertIsNone(args.transcripts)
        self.assertIsNone(args.reference)
        self.assertIs(args.func, command_sync)
        args.out = "s.json"
        buf = io.StringIO()
        with mock.patch("director.cli.require_ffmpeg_binaries"), \
                mock.patch("director.sync.run", return_value=self._payload()):
            with redirect_stdout(buf):
                command_sync(args)
        lines = buf.getvalue().splitlines()
        self.assertIn("A a.mp4 Sony A7S III s-log3-cine 59.94fps "
                      "offset +0.000s method reference", lines)
        self.assertIn("B b.mp4 - 30.00fps offset +6.000s method waveform", lines)
        self.assertIn("  check waveform vs whisper: agree (delta 0.004s)", lines)
        self.assertIn("  ! Whisper disagrees with the chosen offset by 0.500 s.", lines)
        self.assertIn("C lav.wav - 0.00fps offset -- method unplaced", lines)
        self.assertIn("Wrote s.json", lines)


if __name__ == "__main__":
    unittest.main()
