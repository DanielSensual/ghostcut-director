"""Items 23-30 of docs/muse-code/OPEN-DESIGN.md: timeline export tests.

Offline: no network, no Meta API, no Whisper. The real installed
`ghostcut` exporters run on small synthetic fixtures.
"""
from __future__ import annotations

import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from ghostcut.ffmpeg_tools import seconds_to_fcpxml_time

from director.export import (
    SECTION,
    ExportError,
    build_media_clips,
    build_segments,
    export_timelines,
    load_angles,
    sync_clips_by_label,
)


def _camera(path: str, duration: float = 60.0, fps: float = 30.0) -> dict:
    return {"path": path, "make": "Sony", "model": "A7S III", "lens": "",
            "fps": fps, "width": 3840, "height": 2160,
            "duration_s": duration, "has_audio": True, "gamma": "slog3",
            "timecode": None, "tc_rate": None, "captured_at": None}


def _sync_payload() -> dict:
    return {
        "reference": "/media/A.mp4",
        "methods": ["timecode", "clock", "waveform", "whisper"],
        "clips": [
            {"label": "A", "offset_s": 0.0, "method": "reference",
             "camera": _camera("/media/A.mp4"), "estimates": [], "checks": [],
             "drift_ppm": None, "warnings": []},
            {"label": "B", "offset_s": 2.0, "method": "waveform",
             "camera": _camera("/media/B.mp4", duration=58.0), "estimates": [],
             "checks": [], "drift_ppm": None, "warnings": []},
            {"label": "C", "offset_s": 5.0, "method": "waveform",
             "camera": _camera("/media/C.mp4", duration=55.0), "estimates": [],
             "checks": [], "drift_ppm": None, "warnings": []},
        ],
    }


def _shot(i: int, start: float, end: float, camera: str,
          in_s: float = 0.0, out_s: float = 1.0) -> dict:
    return {"id": i, "start_s": start, "end_s": end, "camera": camera,
            "clip_path": f"/media/{camera}.mp4" if camera != "black" else "",
            "clip_in_s": in_s, "clip_out_s": out_s, "line_ids": [i],
            "shot_size": "medium", "reason": "test", "method": "rule",
            "overrules": [], "lut": None}


def _angles_payload(shots: list[dict], audio_label: str = "A") -> dict:
    return {"version": 1, "sync": "/job/sync.json", "words": "/job/words.json",
            "reference": "/media/A.mp4", "grade": None,
            "audio": {"label": audio_label, "clip_path": f"/media/{audio_label}.mp4",
                      "offset_s": 0.0},
            "method": "rule", "model": "muse-spark-1.3", "min_shot_s": 2.0,
            "spend_usd": 0.0, "warnings": [], "lines": [], "shots": shots}


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class MediaClipTests(unittest.TestCase):
    def test_build_media_clips(self) -> None:
        clips = build_media_clips(_sync_payload(), ["A", "C"])
        self.assertEqual([clip.id for clip in clips], ["A", "C"])
        self.assertEqual(clips[0].source_path, "/media/A.mp4")
        self.assertEqual(clips[0].layer, "a-roll")
        self.assertEqual(clips[0].fps, 30.0)
        self.assertEqual(clips[0].duration_s, 60.0)
        self.assertTrue(clips[0].has_audio)
        self.assertEqual((clips[0].width, clips[0].height), (3840, 2160))
        self.assertEqual((clips[0].proxy_path, clips[0].audio_path), ("", ""))
        self.assertEqual(clips[1].duration_s, 55.0)
        with self.assertRaises(ExportError):
            build_media_clips(_sync_payload(), ["A", "Z"])


class SegmentTests(unittest.TestCase):
    def test_build_segments_and_gaps(self) -> None:
        shots = [_shot(0, 10.0, 16.0, "A", 1.0, 7.0),
                 _shot(1, 16.0, 18.0, "black"),
                 _shot(2, 18.0, 24.0, "B", 0.5, 6.5)]
        segments, gaps, _ = build_segments(_angles_payload(shots))
        self.assertEqual(len(segments), 2)
        self.assertEqual([seg.timeline_in_s for seg in segments], [0.0, 8.0])
        self.assertEqual([(seg.src_in_s, seg.src_out_s) for seg in segments],
                         [(1.0, 7.0), (0.5, 6.5)])
        self.assertEqual([seg.clip_id for seg in segments], ["A", "B"])
        self.assertTrue(all(seg.section == SECTION for seg in segments))
        self.assertEqual(gaps, [{"start_s": 6.0, "end_s": 8.0, "line_ids": [1]}])


class WarningTests(unittest.TestCase):
    def test_export_audio_and_lut_warnings(self) -> None:
        shots = [_shot(0, 10.0, 16.0, "A", 1.0, 7.0),
                 _shot(1, 16.0, 22.0, "B", 0.5, 6.5)]
        _, _, warnings = build_segments(_angles_payload(shots, audio_label="C"))
        self.assertEqual(warnings, ["audio_clip_not_in_timeline:C"])
        _, _, warnings = build_segments(_angles_payload(shots, audio_label="A"))
        self.assertEqual(warnings, [])
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            angles = _write(tmp_path / "angles.json",
                            _angles_payload(shots, audio_label="C"))
            sync = _write(tmp_path / "sync.json", _sync_payload())
            grade = _write(tmp_path / "grade.json", {
                "version": 1,
                "cameras": [{"label": "A", "lut": "A.cube"},
                            {"label": "B", "lut": None},
                            {"label": "C", "lut": "C.cube"}]})
            section = export_timelines(
                angles_path=angles, sync_path=sync, grade_path=grade,
                fcpxml_path=tmp_path / "timeline.fcpxml",
                otio_path=tmp_path / "timeline.otio.json",
                project_name="Director cut")
            self.assertIn("timelines_reference_original_media", section["warnings"])
            self.assertIn("lut:A:A.cube", section["warnings"])
            self.assertNotIn("lut:B:B.cube", section["warnings"])
            self.assertEqual(section["luts"], {"A": "A.cube", "B": None})
            plain = export_timelines(
                angles_path=angles, sync_path=sync, grade_path=None,
                fcpxml_path=tmp_path / "plain.fcpxml",
                otio_path=tmp_path / "plain.otio.json",
                project_name="Director cut")
            self.assertEqual(plain["luts"], {})
            self.assertNotIn("timelines_reference_original_media", plain["warnings"])
            self.assertTrue(all(not item.startswith("lut:")
                                for item in plain["warnings"]))
            bad_grade = _write(tmp_path / "bad-grade.json", {"version": 2})
            with self.assertRaises(ExportError):
                export_timelines(
                    angles_path=angles, sync_path=sync, grade_path=bad_grade,
                    fcpxml_path=tmp_path / "bad.fcpxml",
                    otio_path=tmp_path / "bad.otio.json",
                    project_name="Director cut")


class FormatTests(unittest.TestCase):
    def _shotted(self, tmp_path: Path) -> tuple[Path, Path]:
        shots = [_shot(0, 10.0, 16.0, "A", 1.0, 7.0),
                 _shot(1, 16.0, 18.0, "black"),
                 _shot(2, 18.0, 24.0, "B", 0.5, 6.5)]
        return (_write(tmp_path / "angles.json", _angles_payload(shots)),
                _write(tmp_path / "sync.json", _sync_payload()))

    def test_export_timelines_writes_both_formats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            angles, sync = self._shotted(tmp_path)
            fcpxml_path = tmp_path / "timeline.fcpxml"
            otio_path = tmp_path / "timeline.otio.json"
            export_timelines(angles_path=angles, sync_path=sync, grade_path=None,
                             fcpxml_path=fcpxml_path, otio_path=otio_path,
                             project_name="Director cut")
            root = ET.parse(fcpxml_path).getroot()
            self.assertEqual(root.tag, "fcpxml")
            self.assertEqual({asset.get("uid") for asset in root.iter("asset")},
                             {"A", "B"})
            self.assertEqual(list(root.iter("transition")), [])
            asset_clips = list(root.iter("asset-clip"))
            self.assertEqual(len(asset_clips), 2)
            self.assertEqual(asset_clips[1].get("offset"),
                             seconds_to_fcpxml_time(8.0, 30.0))
            payload = json.loads(otio_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["OTIO_SCHEMA"], "Timeline.1")
            tracks = payload["tracks"]["children"]
            self.assertEqual(tracks[0]["name"], "V1")
            kinds = [child["OTIO_SCHEMA"] for child in tracks[0]["children"]]
            self.assertEqual(kinds, ["Clip.2", "Gap.1", "Clip.2"])
            ranges = [child["source_range"] for child in tracks[0]["children"]
                      if child["OTIO_SCHEMA"] == "Clip.2"]
            self.assertEqual(ranges[0]["start_time"]["value"], round(1.0 * 30))
            self.assertEqual(ranges[0]["duration"]["value"], round(6.0 * 30))
            self.assertEqual(ranges[1]["start_time"]["value"], round(0.5 * 30))
            self.assertEqual(ranges[1]["duration"]["value"], round(6.0 * 30))

    def test_export_load_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            not_angles = _write(tmp_path / "nope.json", {"shots": "yes"})
            with self.assertRaises(ExportError) as raised:
                load_angles(not_angles)
            self.assertIn(f"Not an angles.json: {not_angles}", str(raised.exception))
            not_sync = _write(tmp_path / "nope-sync.json", {"clips": {}})
            with self.assertRaises(ExportError) as raised:
                sync_clips_by_label(not_sync)
            self.assertIn(f"Not a sync.json: {not_sync}", str(raised.exception))
            payload = _sync_payload()
            payload["clips"].append(dict(payload["clips"][0]))
            dup = _write(tmp_path / "dup.json", payload)
            with self.assertRaises(ExportError) as raised:
                sync_clips_by_label(dup)
            self.assertIn(f"Duplicate label A in {dup}", str(raised.exception))

    def test_export_overlap_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            shots = [_shot(0, 10.0, 16.0, "A", 1.0, 7.0),
                     _shot(1, 14.0, 20.0, "B", 0.0, 6.0)]
            angles = _write(tmp_path / "angles.json", _angles_payload(shots))
            sync = _write(tmp_path / "sync.json", _sync_payload())
            fcpxml_path = tmp_path / "timeline.fcpxml"
            with self.assertRaises(ValueError):
                export_timelines(angles_path=angles, sync_path=sync, grade_path=None,
                                 fcpxml_path=fcpxml_path,
                                 otio_path=tmp_path / "timeline.otio.json",
                                 project_name="Director cut")
            self.assertTrue(fcpxml_path.exists())

    def test_export_project_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            angles, sync = self._shotted(tmp_path)
            fcpxml_path = tmp_path / "timeline.fcpxml"
            otio_path = tmp_path / "timeline.otio.json"
            export_timelines(angles_path=angles, sync_path=sync, grade_path=None,
                             fcpxml_path=fcpxml_path, otio_path=otio_path,
                             project_name="My Talk")
            root = ET.parse(fcpxml_path).getroot()
            self.assertEqual(next(root.iter("project")).get("name"), "My Talk")
            payload = json.loads(otio_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["name"], "My Talk")


class SectionShapeTests(unittest.TestCase):
    def test_cut_json_export_section_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            angles, sync = FormatTests()._shotted(tmp_path)
            section = export_timelines(
                angles_path=angles, sync_path=sync, grade_path=None,
                fcpxml_path=tmp_path / "timeline.fcpxml",
                otio_path=tmp_path / "timeline.otio.json",
                project_name="Director cut")
            self.assertEqual(set(section),
                             {"fcpxml", "otio", "project_name", "shots_exported",
                              "gaps", "clips_used", "luts", "warnings"})
            for name in (section["fcpxml"], section["otio"]):
                self.assertEqual(Path(name).name, name)
            self.assertEqual(section["shots_exported"], 2)
            self.assertEqual(len(section["gaps"]), 1)
            self.assertEqual(section["clips_used"], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
