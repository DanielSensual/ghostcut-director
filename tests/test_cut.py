"""Items 1-22 of docs/muse-code/OPEN-DESIGN.md: `director cut` tests.

Offline throughout: no network, no Meta API, no Whisper binary. Stage
modules are stubbed by patching `director.cut.<stage>_mod.run` with fakes
that write the stage's JSON output and return its payload; the one
end-to-end test makes short synthetic clips with ffmpeg lavfi sources.
"""
from __future__ import annotations

import collections
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from ghostcut.config import write_json
from ghostcut.ffmpeg_tools import ToolingError

from director import cli as cli_mod
from director import cut as cut_mod
from director.angles import TimedWord, load_timed_words
from director.cut import (
    QC_DEVELOPER,
    QC_MAX_OUTPUT_TOKENS,
    CutError,
    JobOptions,
    discover_clips,
    estimate_qc_usd,
    file_fingerprint,
    fingerprint_of,
    loudnorm_command,
    mechanical_qc,
    mlx_whisper_command,
    muse_qc,
    parse_loudnorm,
    qc_prompt,
    qc_proxy_command,
    qc_schema,
    resolve_audio,
    resolve_words,
    shift_words,
    stage_fingerprint,
    words_file_payload,
)


def _word(text: str, start: float, end: float) -> dict:
    return {"word": text, "start": start, "end": end}


def _write_words(path: Path, words: list[dict]) -> Path:
    path.write_text(json.dumps({"words": words}), encoding="utf-8")
    return path


def _sync_clip(label: str, path: str, offset: float | None,
               duration: float = 60.0, warnings: list[str] | None = None) -> dict:
    return {"label": label, "offset_s": offset,
            "method": "reference" if label == "A" else "waveform",
            "camera": {"path": path, "make": "Sony", "model": "A7S III",
                       "lens": "", "fps": 30.0, "width": 3840, "height": 2160,
                       "duration_s": duration, "has_audio": True,
                       "gamma": "slog3", "timecode": None, "tc_rate": None,
                       "captured_at": None},
            "estimates": [], "checks": [], "drift_ppm": None,
            "warnings": list(warnings or [])}


def _sync_payload(clips: list[dict], reference: str) -> dict:
    return {"reference": reference,
            "methods": ["timecode", "clock", "waveform", "whisper"],
            "clips": clips}


def _shot(i: int, start: float, end: float, camera: str,
          overrules: list[str] | None = None) -> dict:
    return {"id": i, "start_s": start, "end_s": end, "camera": camera,
            "clip_path": f"/media/{camera}.mp4" if camera != "black" else "",
            "clip_in_s": 0.0, "clip_out_s": round(end - start, 3),
            "line_ids": [i], "shot_size": "medium", "reason": "test",
            "method": "rule", "overrules": list(overrules or []), "lut": None}


def _angles_payload(shots: list[dict], audio_label: str = "A") -> dict:
    return {"version": 1, "sync": "/job/sync.json", "words": "/job/words.json",
            "reference": "/media/A.mp4", "grade": None,
            "audio": {"label": audio_label,
                      "clip_path": f"/media/{audio_label}.mp4", "offset_s": 0.0},
            "method": "rule", "model": "muse-spark-1.3", "min_shot_s": 2.0,
            "spend_usd": 0.0, "warnings": [], "lines": [], "shots": shots}


def _options(out_dir: Path, **overrides: Any) -> JobOptions:
    values: dict[str, Any] = {
        "clips": [], "out_dir": out_dir, "reference_path": None,
        "audio_path": None, "words_path": None, "transcripts_dir": None,
        "methods": ["timecode", "clock", "waveform", "whisper"],
        "no_grade": False, "grade_check": False, "no_muse": False,
        "model": "muse-spark-1.3", "min_shot_s": 2.0, "no_vertical": False,
        "no_sam": False, "no_export": False, "no_qc": False, "max_usd": 5.0,
        "force": False, "project_name": "Director cut",
    }
    values.update(overrides)
    return JobOptions(**values)


LOUDNORM_PASS_STDERR = """frame=  100 fps= 25 q=-0.0 size=N/A time=00:00:04.00 bitrate=N/A speed=50x
[Parsed_loudnorm_0 @ 0x7f8a1b400000]
{
\t"input_i" : "-16.00",
\t"input_tp" : "-1.50",
\t"input_lra" : "3.20",
\t"input_thresh" : "-26.00",
\t"output_i" : "-24.00",
\t"output_tp" : "-2.00",
\t"output_lra" : "3.20",
\t"output_thresh" : "-34.00",
\t"normalization_type" : "linear",
\t"target_offset" : "-8.00"
}
"""


def _probe_dict(duration: float = 6.0, width: int = 1920,
                height: int = 1080, audio: bool = True) -> dict:
    streams = [{"codec_type": "video", "codec_name": "h264",
                "width": width, "height": height,
                "avg_frame_rate": "30/1", "r_frame_rate": "30/1"}]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac",
                        "sample_rate": "48000", "channels": 2})
    return {"streams": streams,
            "format": {"duration": str(duration), "bit_rate": "1000000"}}


def _loudness_result(stderr: str = LOUDNORM_PASS_STDERR):
    return subprocess.CompletedProcess(
        ["ffmpeg"], 0, stdout="", stderr=stderr)


class DiscoveryTests(unittest.TestCase):
    def test_discovery_files_and_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cards = tmp_path / "cards"
            cards.mkdir()
            for name in ("b.mp4", "A.MOV", "lav.wav", ".hidden.mp4", "notes.txt"):
                (cards / name).touch()
            out_dir = tmp_path / "out"
            found = discover_clips([cards / "b.mp4", cards], out_dir)
            self.assertEqual(found, [cards / "b.mp4", cards / "A.MOV",
                                     cards / "lav.wav"])

    def test_discovery_excludes_out_dir_and_needs_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            first, second = tmp_path / "a.mp4", tmp_path / "b.mp4"
            first.touch()
            second.touch()
            out_dir = tmp_path / "out"
            out_dir.mkdir()
            (out_dir / "cut.mp4").touch()
            self.assertEqual(discover_clips([first, second, out_dir], out_dir),
                             [first, second])
            lonely = tmp_path / "lonely"
            with self.assertRaises(CutError) as raised:
                discover_clips([first], lonely)
            self.assertEqual(str(raised.exception),
                             "cut: need at least two clips (files, or a folder of media)")
            self.assertFalse(lonely.exists())


class FingerprintTests(unittest.TestCase):
    def test_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "gone.mp4"
            self.assertEqual(file_fingerprint(missing),
                             {"path": str(missing), "size": -1, "mtime_ns": -1})
            clip = Path(tmp) / "a.mp4"
            clip.write_text("data", encoding="utf-8")
            self.assertEqual(fingerprint_of({"b": 1, "a": [1, 2]}),
                             fingerprint_of({"a": [1, 2], "b": 1}))
            first = stage_fingerprint("sync", {"methods": ["waveform"]}, [clip])
            self.assertNotEqual(
                first, stage_fingerprint("sync", {"methods": ["clock"]}, [clip]))
            stat = clip.stat()
            os.utime(clip, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
            self.assertNotEqual(
                first, stage_fingerprint("sync", {"methods": ["waveform"]}, [clip]))


class AudioTests(unittest.TestCase):
    def test_resolve_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ref = str(Path(tmp) / "ref.mp4")
            lav = str(Path(tmp) / "lav.wav")
            Path(ref).touch()
            Path(lav).touch()
            payload = _sync_payload(
                [_sync_clip("A", ref, 0.0), _sync_clip("B", lav, 5.0)], ref)
            label, clip = resolve_audio(payload, Path(lav))
            self.assertEqual((label, clip["label"]), ("B", "B"))
            self.assertEqual(resolve_audio(payload, None)[0], "A")
            moved = _sync_payload(
                [_sync_clip("A", ref, 0.0), _sync_clip("B", lav, 5.0)],
                "/nowhere/else.mp4")
            self.assertEqual(resolve_audio(payload, None)[0], "A")
            self.assertEqual(resolve_audio(moved, None)[0], "A")
            with self.assertRaises(CutError) as raised:
                resolve_audio(payload, Path(tmp) / "stranger.mp4")
            self.assertTrue(str(raised.exception).startswith(
                "words: --audio is not one of the synced clips: "))
            unplaced = _sync_payload(
                [_sync_clip("A", ref, 0.0), _sync_clip("B", lav, None)], ref)
            with self.assertRaises(CutError) as raised:
                resolve_audio(unplaced, Path(lav))
            self.assertEqual(
                str(raised.exception),
                "words: audio clip B is not placed on the timeline; "
                "cannot align its words — check the sync warnings in cut.json")
            with self.assertRaises(CutError) as raised:
                resolve_audio({"reference": ref, "clips": []}, None)
            self.assertEqual(str(raised.exception), "words: sync.json has no clips")


class ShiftTests(unittest.TestCase):
    def test_shift_words_round_trip(self) -> None:
        words = [TimedWord("Hello,", 1.0, 1.4), TimedWord("world.", 1.5, 2.0)]
        shifted = shift_words(words, 178.746)
        self.assertAlmostEqual(shifted[0].start_s, 179.746)
        self.assertAlmostEqual(shifted[1].end_s, 180.746)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "words.json"
            path.write_text(json.dumps(words_file_payload(shifted)),
                            encoding="utf-8")
            loaded = load_timed_words(path)
        self.assertEqual([word.text for word in loaded], ["Hello,", "world."])
        self.assertAlmostEqual(loaded[0].start_s, 179.746, places=3)
        self.assertAlmostEqual(loaded[1].end_s, 180.746, places=3)


class ResolveWordsTests(unittest.TestCase):
    def _audio(self, tmp_path: Path, offset: float = 178.746) -> dict:
        clip = tmp_path / "lav.wav"
        clip.touch()
        return _sync_clip("C", str(clip), offset)

    def test_resolve_words_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_dir = tmp_path / "out"
            out_dir.mkdir()
            explicit = _write_words(tmp_path / "mine.json",
                                    [_word("Hello,", 1.0, 1.4),
                                     _word("world.", 1.5, 2.0)])
            transcripts = tmp_path / "transcripts"
            transcripts.mkdir()
            _write_words(transcripts / "lav.json", [_word("other", 0.0, 0.5)])
            cache = out_dir / "sync-transcripts"
            cache.mkdir()
            _write_words(cache / "lav.json", [_word("cached", 0.0, 0.5)])
            audio = self._audio(tmp_path)
            options = _options(out_dir, words_path=explicit,
                               transcripts_dir=transcripts)
            found, source, warnings = resolve_words(
                audio_label="C", audio_clip=audio, options=options)
            self.assertEqual((found, source, warnings),
                             (explicit, "explicit", []))
            self.assertFalse((out_dir / "words.json").exists())
            self.assertEqual([word.start_s for word in load_timed_words(found)],
                             [1.0, 1.5])
            empty = _write_words(tmp_path / "empty.json", [])
            with self.assertRaises(CutError) as raised:
                resolve_words(audio_label="C", audio_clip=audio,
                              options=_options(out_dir, words_path=empty))
            self.assertEqual(str(raised.exception),
                             f"words: no words in --words file: {empty}")

    def test_resolve_words_auto_sources_shift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_dir = tmp_path / "out"
            out_dir.mkdir()
            audio = self._audio(tmp_path)
            transcripts = tmp_path / "transcripts"
            transcripts.mkdir()
            _write_words(transcripts / "lav.json", [_word("said", 1.0, 1.5)])
            cache = out_dir / "sync-transcripts"
            cache.mkdir()
            _write_words(cache / "lav.json", [_word("cached", 2.0, 2.5)])
            found, source, _ = resolve_words(
                audio_label="C", audio_clip=audio,
                options=_options(out_dir, transcripts_dir=transcripts))
            self.assertEqual((found, source), (out_dir / "words.json", "transcripts"))
            self.assertEqual([word.text for word in load_timed_words(found)], ["said"])
            (transcripts / "lav.json").unlink()
            found, source, _ = resolve_words(
                audio_label="C", audio_clip=audio,
                options=_options(out_dir, transcripts_dir=transcripts))
            self.assertEqual(source, "sync_cache")
            loaded = load_timed_words(found)
            self.assertEqual([word.text for word in loaded], ["cached"])
            self.assertAlmostEqual(loaded[0].start_s, 180.746, places=3)
            _write_words(transcripts / "lav.json", [])
            found, source, warnings = resolve_words(
                audio_label="C", audio_clip=audio,
                options=_options(out_dir, transcripts_dir=transcripts))
            self.assertEqual(source, "sync_cache")
            self.assertIn("words_empty_candidate:lav.json", warnings)

    def test_resolve_words_mlx_whisper(self) -> None:
        from director.sync import WHISPER_MODEL

        self.assertEqual(
            mlx_whisper_command(Path("/media/a.mp4"), Path("/out")),
            ["mlx_whisper", "/media/a.mp4", "--model", WHISPER_MODEL,
             "--output-format", "json",
             "--output-dir", "/out", "--word-timestamps", "True",
             "--condition-on-previous-text", "False",
             "--hallucination-silence-threshold", "2", "--verbose", "False"])
        self.assertIn("--model", mlx_whisper_command(
            Path("/media/a.mp4"), Path("/out"), "custom-model"))
        self.assertIn("custom-model", mlx_whisper_command(
            Path("/media/a.mp4"), Path("/out"), "custom-model"))
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_dir = tmp_path / "out"
            out_dir.mkdir()
            audio = self._audio(tmp_path)

            def _succeed(command, **kwargs):
                self.assertEqual(command[0], "mlx_whisper")
                dest = Path(command[command.index("--output-dir") + 1])
                _write_words(dest / "lav.json", [_word("live", 1.0, 1.5)])
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch("shutil.which", return_value="/bin/mlx_whisper"), \
                    mock.patch.object(cut_mod.sync_mod, "metal_visible",
                                      return_value=True), \
                    mock.patch.object(cut_mod.subprocess, "run",
                                      side_effect=_succeed):
                found, source, _ = resolve_words(
                    audio_label="C", audio_clip=audio, options=_options(out_dir))
            self.assertEqual(source, "mlx_whisper")
            loaded = load_timed_words(found)
            self.assertEqual([word.text for word in loaded], ["live"])
            self.assertAlmostEqual(loaded[0].start_s, 179.746, places=3)

            def _fail(command, **kwargs):
                return subprocess.CompletedProcess(command, 1, "", "boom")

            with mock.patch("shutil.which", return_value="/bin/mlx_whisper"), \
                    mock.patch.object(cut_mod.sync_mod, "metal_visible",
                                      return_value=True), \
                    mock.patch.object(cut_mod.subprocess, "run", side_effect=_fail):
                with self.assertRaises(CutError) as raised:
                    resolve_words(audio_label="C", audio_clip=audio,
                                  options=_options(out_dir / "second"))
            message = str(raised.exception)
            self.assertIn("mlx_whisper failed", message)
            self.assertIn("--words WORDS.json", message)
            self.assertIn("--transcripts DIR", message)

            with mock.patch("shutil.which", return_value=None):
                with self.assertRaises(CutError) as raised:
                    resolve_words(audio_label="C", audio_clip=audio,
                                  options=_options(out_dir / "third"))
            message = str(raised.exception)
            self.assertIn("mlx_whisper is not on PATH", message)
            self.assertIn("install mlx-whisper", message)

    def test_resolve_words_skips_whisper_without_gpu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_dir = tmp_path / "out"
            out_dir.mkdir()
            audio = self._audio(tmp_path)
            with mock.patch("shutil.which", return_value="/bin/mlx_whisper"), \
                    mock.patch.object(cut_mod.sync_mod, "metal_visible",
                                      return_value=False), \
                    mock.patch.object(cut_mod.subprocess, "run") as run_mock:
                with self.assertRaises(CutError) as raised:
                    resolve_words(audio_label="C", audio_clip=audio,
                                  options=_options(out_dir))
            run_mock.assert_not_called()
            self.assertIn("no GPU visible", str(raised.exception))
            self.assertIn("words_no_gpu_visible:lav.wav",
                          raised.exception.stage_warnings)


class StageFakes:
    """Stub stage run()s that write JSON outputs and count calls (no ffmpeg)."""

    def __init__(self, *, grade_spend: float = 0.0, angles_spend: float = 0.03,
                 reframe_spend: float = 0.04, sync_warnings: list[str] | None = None):
        self.calls: collections.Counter[str] = collections.Counter()
        self.grade_spend = grade_spend
        self.angles_spend = angles_spend
        self.reframe_spend = reframe_spend
        self.sync_warnings = list(sync_warnings or [])
        self.reframe_max_usd: list[float] = []

    def sync_run(self, *, clip_paths, reference_path, methods,
                 transcripts_dir, out_path, whisper_model=None):
        self.calls["sync"] += 1
        payload = _sync_payload(
            [_sync_clip("A", str(clip_paths[0]), 0.0),
             _sync_clip("B", str(clip_paths[1]), 0.0,
                        warnings=self.sync_warnings)],
            str(clip_paths[0]))
        write_json(Path(out_path), payload)
        return payload

    def grade_run(self, *, sync_path, out_path, reference_label=None,
                  lut_specs=(), no_match=False, check=False, model="muse-spark-1.3"):
        self.calls["grade"] += 1
        payload = {"version": 1, "sync": str(sync_path),
                   "reference": "A", "reference_label": "A", "model": model,
                   "no_match": no_match, "spend_usd": self.grade_spend,
                   "warnings": [], "check": None,
                   "cameras": [
                       {"label": "A", "path": "/media/A.mp4", "gamma": "slog3",
                        "conversion": {"kind": "none_rec709", "source_lut": None},
                        "lut": None, "source_colour": {}, "match": {},
                        "warnings": []},
                       {"label": "B", "path": "/media/B.mp4", "gamma": "slog3",
                        "conversion": {"kind": "none_rec709", "source_lut": None},
                        "lut": None, "source_colour": {}, "match": {},
                        "warnings": []}]}
        write_json(Path(out_path), payload)
        return payload

    def angles_run(self, *, sync_path, words_path, out_path, audio_label,
                   render_path, min_shot_s, model, no_muse, grade_path=None):
        self.calls["angles"] += 1
        payload = _angles_payload([_shot(0, 0.0, 3.0, "A"), _shot(1, 3.0, 6.0, "B")])
        payload["spend_usd"] = self.angles_spend
        write_json(Path(out_path), payload)
        if render_path is not None:
            Path(render_path).touch()
        return payload

    def export_run(self, *, angles_path, sync_path, grade_path, fcpxml_path,
                   otio_path, project_name):
        self.calls["export"] += 1
        Path(fcpxml_path).write_text("<fcpxml/>", encoding="utf-8")
        Path(otio_path).write_text('{"OTIO_SCHEMA": "Timeline.1"}',
                                   encoding="utf-8")
        return {"fcpxml": Path(fcpxml_path).name, "otio": Path(otio_path).name,
                "project_name": project_name, "shots_exported": 2, "gaps": [],
                "clips_used": ["A", "B"], "luts": {}, "warnings": []}

    def reframe_run(self, *, video_path, out_path, render_path, shots_path,
                    no_sam, max_usd):
        self.calls["reframe"] += 1
        self.reframe_max_usd.append(max_usd)
        payload = {"version": 1, "video": str(video_path), "spend_usd": self.reframe_spend,
                   "warnings": [], "shots": []}
        write_json(Path(out_path), payload)
        if render_path is not None:
            Path(render_path).touch()
        return payload


def _stubbed_run(test: unittest.TestCase, fakes: StageFakes,
                 probe_duration: float = 6.0):
    """Patch stage entry points plus probe/loudness for an offline run()."""
    def _probe(path):
        name = Path(path).name
        if name == "vertical.mp4":
            return _probe_dict(duration=probe_duration, width=1080, height=1920)
        return _probe_dict(duration=probe_duration)

    return _StubContext(test, fakes, _probe)


class _StubContext:
    def __init__(self, test, fakes, probe):
        self._test = test
        self._fakes = fakes
        self._probe = probe
        self._stack = contextlib.ExitStack()

    def __enter__(self):
        stack = self._stack
        stack.enter_context(mock.patch.object(cut_mod.sync_mod, "run",
                                              self._fakes.sync_run))
        stack.enter_context(mock.patch.object(cut_mod.grade_mod, "run",
                                              self._fakes.grade_run))
        stack.enter_context(mock.patch.object(cut_mod.angles_mod, "run",
                                              self._fakes.angles_run))
        stack.enter_context(mock.patch.object(cut_mod, "export_timelines",
                                              self._fakes.export_run))
        stack.enter_context(mock.patch.object(cut_mod.reframe_mod, "run",
                                              self._fakes.reframe_run))
        stack.enter_context(mock.patch.object(cut_mod, "probe_media", self._probe))
        stack.enter_context(mock.patch.object(cut_mod, "run_command",
                                              lambda *a, **k: _loudness_result()))
        return self._fakes

    def __exit__(self, *exc):
        return self._stack.__exit__(*exc)


def _job_dir(tmp_path: Path) -> tuple[Path, Path, Path]:
    cards = tmp_path / "cards"
    cards.mkdir()
    first, second = cards / "a.mp4", cards / "b.mp4"
    first.touch()
    second.touch()
    words = _write_words(tmp_path / "words.json",
                         [_word("Hello,", 0.5, 1.0), _word("world.", 1.5, 2.0)])
    return first, second, words


def _run_job(first: Path, second: Path, words: Path, out_dir: Path, **overrides):
    kwargs: dict[str, Any] = {
        "clips": [first, second], "out_dir": out_dir,
        "reference_path": None, "audio_path": None, "words_path": words,
        "transcripts_dir": None,
        "methods": ["timecode", "clock", "waveform", "whisper"],
        "no_grade": False, "grade_check": False, "no_muse": True,
        "model": "muse-spark-1.3", "min_shot_s": 2.0, "no_vertical": False,
        "no_sam": True, "no_export": False, "no_qc": False, "max_usd": 5.0,
        "force": False, "project_name": "Director cut",
    }
    kwargs.update(overrides)
    return cut_mod.run(**kwargs)


class ResumeTests(unittest.TestCase):
    def test_resume_skips_then_dirty_reruns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            first, second, words = _job_dir(tmp_path)
            out_dir = tmp_path / "out"
            fakes = StageFakes()
            with _stubbed_run(self, fakes):
                first_run = _run_job(first, second, words, out_dir)
            self.assertEqual(first_run["status"], "complete_with_warnings")
            self.assertEqual(
                {name: record["status"]
                 for name, record in first_run["stages"].items()},
                {name: "done" for name in
                 ("sync", "words", "grade", "angles", "export", "reframe", "qc")})
            self.assertEqual(
                {name: fakes.calls[name] for name in
                 ("sync", "grade", "angles", "export", "reframe")},
                {name: 1 for name in
                 ("sync", "grade", "angles", "export", "reframe")})
            self.assertAlmostEqual(first_run["spend_usd"], 0.07, delta=0.001)
            with _stubbed_run(self, fakes):
                resumed = _run_job(first, second, words, out_dir)
            self.assertEqual(
                {name: fakes.calls[name] for name in
                 ("sync", "grade", "angles", "export", "reframe")},
                {name: 1 for name in
                 ("sync", "grade", "angles", "export", "reframe")})
            self.assertTrue(all(record["status"] == "skipped"
                                for record in resumed["stages"].values()))
            self.assertTrue(all(record["spend_usd"] == 0.0
                                for record in resumed["stages"].values()))
            self.assertEqual(resumed["status"], "complete_with_warnings")
            stat = first.stat()
            os.utime(first, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
            with _stubbed_run(self, fakes):
                rerun = _run_job(first, second, words, out_dir)
            self.assertEqual(
                {name: fakes.calls[name] for name in
                 ("sync", "grade", "angles", "export", "reframe")},
                {name: 2 for name in
                 ("sync", "grade", "angles", "export", "reframe")})
            self.assertTrue(all(record["status"] == "done"
                                for record in rerun["stages"].values()))

    def test_force_reruns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            first, second, words = _job_dir(tmp_path)
            out_dir = tmp_path / "out"
            fakes = StageFakes()
            with _stubbed_run(self, fakes):
                _run_job(first, second, words, out_dir)
                forced = _run_job(first, second, words, out_dir, force=True)
            self.assertEqual(
                {name: fakes.calls[name] for name in
                 ("sync", "grade", "angles", "export", "reframe")},
                {name: 2 for name in
                 ("sync", "grade", "angles", "export", "reframe")})
            self.assertTrue(all(record["status"] == "done"
                                for record in forced["stages"].values()))

    def test_spend_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            first, second, words = _job_dir(tmp_path)
            out_dir = tmp_path / "out"
            fakes = StageFakes(angles_spend=4.995)
            with _stubbed_run(self, fakes):
                with self.assertRaises(CutError) as raised:
                    _run_job(first, second, words, out_dir, no_sam=False)
            message = str(raised.exception)
            self.assertTrue(message.startswith("reframe: estimated SAM cost $0.01"))
            self.assertIn("over --max-usd $5.00", message)
            self.assertIn("pass --no-vertical", message)
            self.assertEqual(fakes.calls["reframe"], 0)
            failed = json.loads((out_dir / "cut.json").read_text(encoding="utf-8"))
            self.assertEqual((failed["status"], failed["failed_stage"]),
                             ("failed", "reframe"))
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            first, second, words = _job_dir(tmp_path)
            out_dir = tmp_path / "out"
            fakes = StageFakes(angles_spend=4.99)
            with _stubbed_run(self, fakes):
                done = _run_job(first, second, words, out_dir, no_muse=False)
            self.assertTrue(done["status"].startswith("complete"))
            self.assertEqual(
                [check for check in done["qc"]["checks"]
                 if check["source"] == "muse"], [])
            self.assertTrue(any(item.startswith("qc_muse_skipped_over_budget:")
                                for item in done["warnings"]))

    def test_no_shots_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            first, second, words = _job_dir(tmp_path)
            out_dir = tmp_path / "out"
            fakes = StageFakes()

            def _empty(*, sync_path, words_path, out_path, audio_label,
                       render_path, min_shot_s, model, no_muse, grade_path=None):
                fakes.calls["angles"] += 1
                raise ValueError("nothing to render")

            with _stubbed_run(self, fakes), \
                    mock.patch.object(cut_mod.angles_mod, "run", _empty):
                with self.assertRaises(CutError) as raised:
                    _run_job(first, second, words, out_dir)
            self.assertEqual(
                str(raised.exception),
                "angles: no shots: no camera covers any spoken line; "
                "check that the words belong to the audio clip and the sync "
                "offsets in cut.json")
            self.assertEqual(fakes.calls["export"], 0)
            self.assertEqual(fakes.calls["reframe"], 0)
            failed = json.loads((out_dir / "cut.json").read_text(encoding="utf-8"))
            self.assertEqual((failed["status"], failed["failed_stage"]),
                             ("failed", "angles"))

    def test_failure_writes_manifest_and_keeps_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            first, second, words = _job_dir(tmp_path)
            out_dir = tmp_path / "out"
            fakes = StageFakes()

            def _boom(*args, **kwargs):
                raise RuntimeError("boom")

            with _stubbed_run(self, fakes), \
                    mock.patch.object(cut_mod.grade_mod, "run", _boom):
                with self.assertRaises(CutError) as raised:
                    _run_job(first, second, words, out_dir)
            self.assertTrue(str(raised.exception).startswith("grade: "))
            failed = json.loads((out_dir / "cut.json").read_text(encoding="utf-8"))
            self.assertEqual((failed["status"], failed["failed_stage"]),
                             ("failed", "grade"))
            self.assertEqual(failed["stages"]["sync"]["status"], "done")
            self.assertEqual(failed["stages"]["words"]["status"], "done")
            self.assertEqual(failed["stages"]["grade"]["error"], "boom")
            kept = json.loads((out_dir / "sync.json").read_text(encoding="utf-8"))
            self.assertEqual([clip["label"] for clip in kept["clips"]], ["A", "B"])

    def test_corrupt_previous_manifest_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            first, second, words = _job_dir(tmp_path)
            out_dir = tmp_path / "out"
            out_dir.mkdir()
            (out_dir / "cut.json").write_text("{ not json", encoding="utf-8")
            fakes = StageFakes()
            with _stubbed_run(self, fakes):
                done = _run_job(first, second, words, out_dir)
            self.assertTrue(done["status"].startswith("complete"))
            self.assertIn("previous_manifest_unreadable", done["warnings"])
            self.assertTrue(all(record["status"] == "done"
                                for record in done["stages"].values()))


def _qc_payloads(shots: list[dict]) -> tuple[dict, dict]:
    sync = _sync_payload(
        [_sync_clip("A", "/media/A.mp4", 0.0),
         _sync_clip("B", "/media/B.mp4", 0.0)], "/media/A.mp4")
    return sync, _angles_payload(shots)


class MechanicalQcTests(unittest.TestCase):
    def test_mechanical_qc_checks(self) -> None:
        shots = [_shot(0, 0.0, 3.0, "A"), _shot(1, 3.0, 6.0, "B")]
        sync, angles = _qc_payloads(shots)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            with mock.patch.object(cut_mod, "probe_media",
                                   lambda path: _probe_dict(duration=6.0)), \
                    mock.patch.object(cut_mod, "run_command",
                                      lambda *a, **k: _loudness_result()):
                checks = mechanical_qc(out_dir=out_dir, sync_payload=sync,
                                       angles_payload=angles, reframe_payload=None,
                                       export_section=None)
        self.assertEqual([check.id for check in checks],
                         ["master_exists", "duration_match", "audio_present",
                          "black_share", "long_shots", "unplaced_cameras",
                          "loudness"])
        self.assertTrue(all(check.status == "pass" for check in checks))
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            with mock.patch.object(cut_mod, "probe_media",
                                   lambda path: _probe_dict(duration=6.5)), \
                    mock.patch.object(cut_mod, "run_command",
                                      lambda *a, **k: _loudness_result()):
                checks = mechanical_qc(out_dir=out_dir, sync_payload=sync,
                                       angles_payload=angles, reframe_payload=None,
                                       export_section=None)
        failed = [check for check in checks if check.status == "fail"]
        self.assertEqual([check.id for check in failed], ["duration_match"])
        gapped = [_shot(0, 0.0, 4.0, "A"), _shot(1, 4.0, 6.0, "black")]
        _, angles = _qc_payloads(gapped)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            with mock.patch.object(cut_mod, "probe_media",
                                   lambda path: _probe_dict(duration=6.0)), \
                    mock.patch.object(cut_mod, "run_command",
                                      lambda *a, **k: _loudness_result()):
                checks = mechanical_qc(out_dir=out_dir, sync_payload=sync,
                                       angles_payload=angles, reframe_payload=None,
                                       export_section=None)
        black = next(check for check in checks if check.id == "black_share")
        self.assertEqual(black.status, "warn")
        self.assertAlmostEqual(black.detail["share"], round(2.0 / 6.0, 4))
        _, angles = _qc_payloads(shots)

        def _missing(path):
            raise ToolingError("no such file")

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            with mock.patch.object(cut_mod, "probe_media", _missing), \
                    mock.patch.object(cut_mod, "run_command",
                                      lambda *a, **k: _loudness_result()):
                checks = mechanical_qc(out_dir=out_dir, sync_payload=sync,
                                       angles_payload=angles, reframe_payload=None,
                                       export_section=None)
        self.assertEqual([check.id for check in checks if check.status == "fail"],
                         ["master_exists"])
        for check_id in ("duration_match", "audio_present", "black_share"):
            check = next(item for item in checks if item.id == check_id)
            self.assertEqual((check.status, check.detail),
                             ("warn", {"skipped": "master_exists"}))

    def test_mechanical_qc_vertical_and_timelines(self) -> None:
        shots = [_shot(0, 0.0, 3.0, "A"), _shot(1, 3.0, 6.0, "B")]
        sync, angles = _qc_payloads(shots)
        export_section = {"fcpxml": "timeline.fcpxml",
                          "otio": "timeline.otio.json"}
        reframe_payload = {"version": 1}

        def _good_probe(path):
            if Path(path).name == "vertical.mp4":
                return _probe_dict(duration=6.0, width=1080, height=1920)
            return _probe_dict(duration=6.0)

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / "timeline.fcpxml").write_text("<fcpxml/>", encoding="utf-8")
            (out_dir / "timeline.otio.json").write_text(
                '{"OTIO_SCHEMA": "Timeline.1"}', encoding="utf-8")
            with mock.patch.object(cut_mod, "probe_media", _good_probe), \
                    mock.patch.object(cut_mod, "run_command",
                                      lambda *a, **k: _loudness_result()):
                checks = mechanical_qc(out_dir=out_dir, sync_payload=sync,
                                       angles_payload=angles,
                                       reframe_payload=reframe_payload,
                                       export_section=export_section)
        by_id = {check.id: check for check in checks}
        self.assertEqual(by_id["vertical_shape"].status, "pass")
        self.assertEqual(by_id["timelines_parse"].status, "pass")

        def _wide_probe(path):
            if Path(path).name == "vertical.mp4":
                return _probe_dict(duration=6.0, width=1920, height=1080)
            return _probe_dict(duration=6.0)

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / "timeline.fcpxml").write_text("<fcpxml/>", encoding="utf-8")
            (out_dir / "timeline.otio.json").write_text(
                '{"OTIO_SCHEMA": "Timeline.1"}', encoding="utf-8")
            with mock.patch.object(cut_mod, "probe_media", _wide_probe), \
                    mock.patch.object(cut_mod, "run_command",
                                      lambda *a, **k: _loudness_result()):
                checks = mechanical_qc(out_dir=out_dir, sync_payload=sync,
                                       angles_payload=angles,
                                       reframe_payload=reframe_payload,
                                       export_section=export_section)
        by_id = {check.id: check for check in checks}
        self.assertEqual(by_id["vertical_shape"].status, "fail")

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / "timeline.fcpxml").write_text("not xml <", encoding="utf-8")
            (out_dir / "timeline.otio.json").write_text(
                '{"OTIO_SCHEMA": "Timeline.1"}', encoding="utf-8")
            with mock.patch.object(cut_mod, "probe_media", _good_probe), \
                    mock.patch.object(cut_mod, "run_command",
                                      lambda *a, **k: _loudness_result()):
                checks = mechanical_qc(out_dir=out_dir, sync_payload=sync,
                                       angles_payload=angles,
                                       reframe_payload=reframe_payload,
                                       export_section=export_section)
        by_id = {check.id: check for check in checks}
        self.assertEqual(by_id["timelines_parse"].status, "fail")
        self.assertEqual(by_id["timelines_parse"].detail,
                         {"fcpxml": False, "otio": True})


class EstimateTests(unittest.TestCase):
    def test_estimate_qc_usd(self) -> None:
        self.assertAlmostEqual(estimate_qc_usd(180.0, "muse-spark-1.3"), 0.0845)
        self.assertAlmostEqual(estimate_qc_usd(180.0, "muse-wonder-1.0"), 0.296)
        self.assertAlmostEqual(estimate_qc_usd(180.0, "muse-next-9.9"),
                               estimate_qc_usd(180.0, "muse-spark-1.3"))


class SchemaPromptTests(unittest.TestCase):
    def test_qc_schema_and_prompt(self) -> None:
        schema = qc_schema()
        self.assertEqual(set(schema["required"]), {"overall", "summary", "findings"})
        kinds = schema["properties"]["findings"]["items"]["properties"]["kind"]["enum"]
        self.assertEqual(kinds, ["angle_choice", "grade_mismatch", "av_sync_suspect",
                                 "black_or_frozen", "framing", "audio_issue", "other"])
        self.assertEqual(schema["properties"]["findings"]["maxItems"], 12)
        shots = [_shot(0, 0.0, 5.0, "A"), _shot(1, 5.0, 7.0, "black")]
        prompt = qc_prompt(10.0, shots)
        self.assertIn("Check this finished cut (10.0s)", prompt)
        self.assertIn("shot 1 5.0-7.0s BLACK (no camera covered this span)", prompt)
        self.assertTrue(prompt.endswith(
            "If the picture disagrees with this map, trust the picture and say so "
            "in the finding."))
        self.assertIn("empty findings list", QC_DEVELOPER)


class FakeGuestlist:
    """Stand-in for guestlist.meta in sys.modules (no network)."""

    def __init__(self, test: unittest.TestCase, video_json, guard=None):
        self._test = test
        self._video_json = video_json
        self._guard = guard or (lambda estimate: None)
        self.saved: dict[str, Any] = {}

    def __enter__(self):
        meta = types.ModuleType("guestlist.meta")
        meta.spark = types.SimpleNamespace(video_json=self._video_json)
        meta.cache = types.SimpleNamespace(guard=self._guard)
        self.saved = {key: sys.modules.get(key)
                      for key in ("guestlist", "guestlist.meta")}
        sys.modules["guestlist"] = types.ModuleType("guestlist")
        sys.modules["guestlist"].meta = meta
        sys.modules["guestlist.meta"] = meta
        return meta

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
        return False


class MuseQcTests(unittest.TestCase):
    def test_muse_qc_with_fake_guestlist(self) -> None:
        shots = [_shot(0, 0.0, 4.0, "A"), _shot(1, 4.0, 6.0, "black"),
                 _shot(2, 6.0, 10.0, "B")]
        angles = _angles_payload(shots)
        seen: dict[str, Any] = {}

        class _Result:
            def __init__(self, data):
                self.data, self.usd, self.cached = data, 0.0123, False

        def _video_json(video, **kwargs):
            seen.update({"video": video, **kwargs})
            return _Result({
                "overall": "review", "summary": "one black bit",
                "findings": [
                    {"start_s": 999.0, "end_s": 1001.0, "kind": "framing",
                     "severity": "low", "description": "way out",
                     "confidence": 0.5},
                    {"start_s": 4.0, "end_s": 6.0, "kind": "black_or_frozen",
                     "severity": "high", "description": "black",
                     "confidence": 0.9},
                ]})

        with tempfile.TemporaryDirectory() as tmp:
            proxy = Path(tmp) / "proxy.mp4"
            proxy.touch()
            with FakeGuestlist(self, _video_json):
                checks, spend, warnings = muse_qc(
                    proxy=proxy, duration_s=10.0, model="muse-spark-1.3",
                    angles_payload=angles)
        self.assertEqual(seen["video"], proxy)
        self.assertEqual(seen["name"], "cut_qc")
        self.assertEqual(seen["schema"], qc_schema())
        self.assertEqual(seen["max_output_tokens"], QC_MAX_OUTPUT_TOKENS)
        self.assertEqual([check.id for check in checks],
                         ["muse:black_or_frozen", "muse:overall"])
        self.assertEqual(checks[0].status, "warn")
        self.assertTrue(checks[0].detail["corroborated"])
        self.assertEqual(checks[1].status, "warn")
        self.assertEqual(checks[1].detail["overall"], "review")
        self.assertAlmostEqual(spend, 0.0123)
        self.assertEqual(warnings, ["qc_muse_dropped_findings:1"])

        def _failing(video, **kwargs):
            raise RuntimeError("net down")

        def _run_command(command, **kwargs):
            if any("loudnorm" in str(part) for part in command):
                return _loudness_result()
            Path(command[-1]).touch()
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            first, second, words = _job_dir(tmp_path)
            out_dir = tmp_path / "out"
            fakes = StageFakes()
            with _stubbed_run(self, fakes), \
                    mock.patch.object(cut_mod, "run_command", _run_command), \
                    FakeGuestlist(self, _failing):
                done = _run_job(first, second, words, out_dir, no_muse=False)
        self.assertEqual([check for check in done["qc"]["checks"]
                          if check["source"] == "muse"], [])
        self.assertTrue(any(check["id"] == "duration_match"
                            for check in done["qc"]["checks"]))
        self.assertIn("qc_muse_failed:RuntimeError: net down", done["warnings"])


class CommandShapeTests(unittest.TestCase):
    def test_loudnorm_and_proxy_commands(self) -> None:
        self.assertEqual(
            loudnorm_command(Path("/v/cut.mp4")),
            ["ffmpeg", "-v", "info", "-i", "/v/cut.mp4", "-vn",
             "-af", "loudnorm=print_format=json", "-f", "null", "-"])
        self.assertEqual(
            qc_proxy_command(Path("/v/cut.mp4"), Path("/o/qc/proxy.mp4")),
            ["ffmpeg", "-v", "error", "-y", "-i", "/v/cut.mp4", "-map", "0:v:0",
             "-vf", "fps=5,scale=-2:540", "-an", "-map_metadata", "-1",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", "/o/qc/proxy.mp4"])
        integrated, peak = parse_loudnorm(LOUDNORM_PASS_STDERR)
        assert integrated is not None and peak is not None
        self.assertAlmostEqual(integrated, -16.0)
        self.assertAlmostEqual(peak, -1.5)
        self.assertEqual(parse_loudnorm("garbage, no loudnorm block here"),
                         (None, None))
        self.assertEqual(parse_loudnorm("[Parsed_loudnorm_0 @ x] {oops"),
                         (None, None))


def _numpy_importable() -> bool:
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


class CliTests(unittest.TestCase):
    def test_cli_parser_and_print(self) -> None:
        parser = cli_mod.build_parser()
        args = parser.parse_args(["cut", "a.mp4", "b.mp4"])
        self.assertEqual(args.inputs, ["a.mp4", "b.mp4"])
        self.assertEqual(args.out_dir, "director-cut")
        self.assertEqual(args.model, "muse-spark-1.3")
        self.assertEqual(args.min_shot, 2.0)
        self.assertEqual(args.max_usd, 5.0)
        self.assertEqual(args.name, "Director cut")
        self.assertFalse(args.no_grade)
        self.assertFalse(args.no_muse)
        self.assertFalse(args.force)
        payload = {
            "version": 1, "status": "complete_with_warnings",
            "failed_stage": None, "out_dir": "director-cut",
            "model": "muse-spark-1.3", "max_usd": 5.0, "spend_usd": 0.05,
            "warnings": [], "inputs": ["a.mp4", "b.mp4"],
            "stages": {
                "sync": {"status": "done", "fingerprint": "x",
                         "outputs": ["sync.json"], "spend_usd": 0.0,
                         "warnings": ["B: clock only"], "error": None},
                "words": {"status": "done", "fingerprint": "x",
                          "outputs": ["words.json"], "spend_usd": 0.0,
                          "warnings": [], "error": None},
                "angles": {"status": "done", "fingerprint": "x",
                           "outputs": ["angles.json", "cut.mp4"],
                           "spend_usd": 0.05, "warnings": [], "error": None},
                "export": {"status": "done", "fingerprint": "x",
                           "outputs": ["timeline.fcpxml", "timeline.otio.json"],
                           "spend_usd": 0.0,
                           "warnings": ["audio_clip_not_in_timeline:C"],
                           "error": None},
                "qc": {"status": "done", "fingerprint": "x", "outputs": [],
                       "spend_usd": 0.0, "warnings": [], "error": None},
            },
            "audio": {"label": "A", "clip_path": "a.mp4", "offset_s": 0.0,
                      "words_source": "sync_cache", "words_file": "words.json",
                      "words": 812, "words_shifted_s": 0.0},
            "export": {"fcpxml": "timeline.fcpxml", "otio": "timeline.otio.json",
                       "project_name": "Director cut", "shots_exported": 21,
                       "gaps": [{"start_s": 1.0, "end_s": 2.0, "line_ids": [3]}],
                       "clips_used": ["A", "B"], "luts": {}, "warnings": []},
            "qc": {"status": "warn",
                   "checks": [{"id": f"check-{i}", "source": "mechanical",
                               "status": "pass", "detail": {}} for i in range(7)]
                   + [{"id": "muse:framing", "source": "muse", "status": "warn",
                       "detail": {}},
                      {"id": "muse:overall", "source": "muse", "status": "warn",
                       "detail": {"overall": "review", "summary": ""}}],
                   "spend_usd": 0.0},
        }
        with mock.patch.object(cli_mod, "require_ffmpeg_binaries",
                               lambda: None), \
                mock.patch("director.cut.run", return_value=payload) as run_mock:
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                returned = cli_mod.command_cut(args)
        self.assertIs(returned, payload)
        _, kwargs = run_mock.call_args
        self.assertEqual(kwargs["clips"], [Path("a.mp4"), Path("b.mp4")])
        self.assertEqual(kwargs["out_dir"], Path("director-cut"))
        self.assertEqual(kwargs["model"], "muse-spark-1.3")
        self.assertEqual(kwargs["max_usd"], 5.0)
        output = stream.getvalue()
        self.assertIn("sync: done", output)
        self.assertIn("words: done (812 words, sync_cache)", output)
        self.assertIn("angles: done ($0.0500)", output)
        self.assertIn("export: done (21 shots, 1 gap)", output)
        self.assertIn("qc: warn (9 checks, 1 muse findings)", output)
        self.assertIn("! B: clock only", output)
        self.assertIn("! export: audio_clip_not_in_timeline:C", output)
        self.assertIn("Wrote director-cut/cut.json (complete_with_warnings, $0.0500)",
                      output)
        self.assertIn("cut: director-cut/cut.mp4", output)
        self.assertIn("timelines: director-cut/timeline.fcpxml, "
                      "director-cut/timeline.otio.json", output)
        self.assertFalse(any(line.startswith("vertical:")
                             for line in output.splitlines()))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe")
                     and _numpy_importable(),
                     "needs ffmpeg, ffprobe and numpy")
class SyntheticEndToEndTests(unittest.TestCase):
    def test_cut_end_to_end_synthetic(self) -> None:
        import xml.etree.ElementTree as ET

        from ghostcut.ffmpeg_tools import extract_media_stats, probe_media

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cards = tmp_path / "cards"
            cards.mkdir()
            for name in ("a.mp4", "b.mp4"):
                done = subprocess.run(
                    ["ffmpeg", "-v", "error", "-y",
                     "-f", "lavfi", "-i",
                     "testsrc2=size=640x360:rate=30:duration=6",
                     "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-shortest", str(cards / name)],
                    capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, msg=done.stderr)
            words = [
                _word(f"w{i}", 0.5 + i * 0.25, 0.7 + i * 0.25)
                for i in range(20)]
            words_path = _write_words(tmp_path / "words.json", words)
            out_dir = tmp_path / "job"
            payload = cut_mod.run(
                clips=[cards], out_dir=out_dir, reference_path=None,
                audio_path=None, words_path=words_path, transcripts_dir=None,
                methods=["timecode", "clock", "waveform", "whisper"],
                no_grade=False, grade_check=False, no_muse=True,
                model="muse-spark-1.3", min_shot_s=2.0, no_vertical=False,
                no_sam=True, no_export=False, no_qc=False, max_usd=5.0,
                force=False, project_name="Director cut")
            self.assertIn(payload["status"], ("complete", "complete_with_warnings"))
            self.assertTrue(all(record["status"] == "done"
                                for record in payload["stages"].values()))
            master = extract_media_stats(probe_media(out_dir / "cut.mp4"))
            shots = json.loads((out_dir / "angles.json").read_text(
                encoding="utf-8"))["shots"]
            span = shots[-1]["end_s"] - shots[0]["start_s"]
            self.assertAlmostEqual(float(master["duration_s"]), span, delta=0.15)
            self.assertAlmostEqual(float(master["duration_s"]), 6.0, delta=1.5)
            vertical = extract_media_stats(probe_media(out_dir / "vertical.mp4"))
            self.assertEqual((vertical["width"], vertical["height"]), (1080, 1920))
            ET.parse(out_dir / "timeline.fcpxml")
            otio = json.loads((out_dir / "timeline.otio.json").read_text(
                encoding="utf-8"))
            self.assertEqual(otio["OTIO_SCHEMA"], "Timeline.1")
            total = sum(record["spend_usd"]
                        for record in payload["stages"].values())
            self.assertAlmostEqual(total, payload["spend_usd"], delta=0.001)


if __name__ == "__main__":
    unittest.main()


class MatchLutsTests(unittest.TestCase):
    def test_patterns_match_file_names_case_insensitively(self) -> None:
        from director.cut import CutError, match_luts
        with tempfile.TemporaryDirectory() as tmp:
            cube = Path(tmp) / "dlog.cube"
            cube.write_text("LUT_3D_SIZE 2\n", encoding="utf-8")
            drone = Path(tmp) / "DJI_0354.MOV"
            sony = Path(tmp) / "C2935.MP4"
            got = match_luts([f"dji_*={cube}"], [drone, sony])
            self.assertEqual(got, {str(drone): cube.resolve()})
            with self.assertRaisesRegex(CutError, "no input clip matches"):
                match_luts([f"GX*={cube}"], [drone, sony])
            with self.assertRaisesRegex(CutError, "no such .cube"):
                match_luts([f"DJI_*={Path(tmp) / 'missing.cube'}"], [drone])
            with self.assertRaisesRegex(CutError, "want PATTERN=CUBE"):
                match_luts(["DJI_*"], [drone])
