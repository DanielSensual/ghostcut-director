from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from director import gaze as gaze_mod
from director.angles import Line, Pick, SyncClip, apply_checks, batch_prompt


def _line(i: int, start: float, end: float, cameras: list[str]) -> Line:
    return Line(i, start, end, "hello world", i, i + 1, list(cameras))


def _pick(line_id: int, camera: str | None, method: str = "muse") -> Pick:
    return Pick(line_id, camera, "medium" if camera else "", "test reason",
                method, [])


class AddressedTests(unittest.TestCase):
    def test_clear_winner(self) -> None:
        winner, reason = gaze_mod.decide_addressed_camera({
            "B": [6.0, -7.0, 5.0], "C": [41.0, 38.0, 45.0]})
        self.assertEqual(winner, "B")
        self.assertEqual(reason,
                         "speaker addresses camera B: |yaw| 6° vs 41° on C")

    def test_winner_counts_missing_faces_as_not_addressed(self) -> None:
        winner, reason = gaze_mod.decide_addressed_camera({
            "B": [6.0, 7.0, None], "C": [None, None, None]})
        self.assertEqual(winner, "B")
        self.assertEqual(reason,
                         "speaker addresses camera B: |yaw| 6° vs no face on C")

    def test_ambiguous_when_nobody_has_a_majority(self) -> None:
        winner, reason = gaze_mod.decide_addressed_camera({
            "B": [30.0, 35.0, 5.0], "C": [40.0, 38.0, -42.0]})
        self.assertIsNone(winner)
        self.assertEqual(reason, "gaze ambiguous: B 1/3 frontal, C 0/3 frontal")

    def test_ambiguous_when_two_cameras_win(self) -> None:
        winner, reason = gaze_mod.decide_addressed_camera({
            "B": [5.0, 6.0, 30.0], "C": [-6.0, 7.0, 8.0]})
        self.assertIsNone(winner)
        self.assertEqual(reason, "gaze ambiguous: B 2/3 frontal, C 3/3 frontal")

    def test_no_face_at_all(self) -> None:
        winner, reason = gaze_mod.decide_addressed_camera({
            "B": [None, None], "C": [None]})
        self.assertIsNone(winner)
        self.assertEqual(reason, "no face detected on any camera")

    def test_threshold_and_majority_boundaries(self) -> None:
        self.assertTrue(gaze_mod.is_addressed(18.0))
        self.assertTrue(gaze_mod.is_addressed(-18.0))
        self.assertFalse(gaze_mod.is_addressed(18.01))
        self.assertFalse(gaze_mod.is_addressed(None))
        # one of two is not most of the line
        winner, _ = gaze_mod.decide_addressed_camera({"B": [5.0, 40.0]})
        self.assertIsNone(winner)
        winner, _ = gaze_mod.decide_addressed_camera({"B": [5.0, 6.0]})
        self.assertEqual(winner, "B")

    def test_median_abs_yaw(self) -> None:
        self.assertAlmostEqual(
            gaze_mod.median_abs_yaw([6.0, -12.0, None]) or -1.0, 9.0)
        self.assertIsNone(gaze_mod.median_abs_yaw([None, None]))
        self.assertIsNone(gaze_mod.median_abs_yaw([]))

    def test_build_line_gaze_and_json(self) -> None:
        gaze = gaze_mod.build_line_gaze(
            3, {"B": [6.0, -7.0, 5.0], "C": [41.0, 38.0, 45.0]})
        self.assertEqual((gaze.line_id, gaze.pick), (3, "B"))
        payload = gaze_mod.line_gaze_json(gaze)
        self.assertEqual(payload["pick"], "B")
        self.assertIn("speaker addresses camera B", payload["reason"])
        self.assertEqual(payload["B"]["yaw_deg"], [6.0, -7.0, 5.0])
        self.assertEqual((payload["B"]["addressed"], payload["B"]["samples"]),
                         (3, 3))
        self.assertAlmostEqual(payload["B"]["median_abs_yaw"], 6.0)
        self.assertEqual(payload["C"]["yaw_deg"], [41.0, 38.0, 45.0])
        self.assertAlmostEqual(payload["C"]["median_abs_yaw"], 41.0)


class GazePromptTests(unittest.TestCase):
    def test_prompt_without_gaze_is_unchanged(self) -> None:
        lines = [_line(0, 0.0, 1.0, ["A", "B"])]
        prompt = batch_prompt(lines, {0: ["A", "B"]})
        self.assertNotIn("gaze", prompt)
        self.assertIn('L0 (1.0s) cameras A, B: "hello world"', prompt)

    def test_prompt_carries_gaze_numbers(self) -> None:
        lines = [_line(0, 0.0, 1.0, ["B", "C"])]
        line_gaze = {0: gaze_mod.build_line_gaze(
            0, {"B": [6.0, -7.0, 5.0], "C": [41.0, 38.0, 45.0]})}
        prompt = batch_prompt(lines, {0: ["B", "C"]}, line_gaze)
        self.assertIn("[gaze: B |yaw| 6° (3/3 frontal); "
                      "C |yaw| 41° (0/3 frontal)]", prompt)

    def test_prompt_names_a_faceless_camera(self) -> None:
        gaze = gaze_mod.build_line_gaze(0, {"B": [6.0, 7.0], "C": [None, None]})
        self.assertEqual(gaze_mod.format_gaze_note(gaze),
                         "B |yaw| 6° (2/2 frontal); C no face (0/2 frontal)")

    def test_muse_call_sees_the_gaze_numbers(self) -> None:
        calls: list[dict] = []

        class Result:
            def __init__(self, data):
                self.data, self.usd, self.cached = data, 0.0, False

        def image_json(images, **kw):
            calls.append(dict(kw))
            return Result({"picks": [
                {"line_id": 0, "camera": "B", "shot_size": "close",
                 "reason": "frontal"},
            ]})

        meta = types.ModuleType("guestlist.meta")
        meta.spark = types.SimpleNamespace(image_json=image_json)
        meta.cache = types.SimpleNamespace(guard=lambda usd: None)
        saved = {k: sys.modules.get(k) for k in ("guestlist", "guestlist.meta")}
        sys.modules["guestlist"] = types.ModuleType("guestlist")
        sys.modules["guestlist"].meta = meta
        sys.modules["guestlist.meta"] = meta
        try:
            from director.angles import pick_with_muse
            lines = [_line(0, 0.0, 3.0, ["B", "C"])]
            clips = [SyncClip("B", "/media/B.mp4", 0.0, 30.0, 1920, 1080, 30.0),
                     SyncClip("C", "/media/C.mp4", 0.0, 30.0, 1920, 1080, 30.0)]
            frames = {(0, "B"): Path("/frames/L0_B.jpg"),
                      (0, "C"): Path("/frames/L0_C.jpg")}
            line_gaze = {0: gaze_mod.build_line_gaze(
                0, {"B": [6.0, 7.0, 5.0], "C": [41.0, 38.0, 45.0]})}
            pick_with_muse(lines, clips, frames, model="m",
                           frames_dir=Path("/frames"), line_gaze=line_gaze)
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        self.assertIn("[gaze: B |yaw| 6° (3/3 frontal); "
                      "C |yaw| 41° (0/3 frontal)]", calls[0]["prompt"])


class GazeOverrideTests(unittest.TestCase):
    def test_gaze_overrules_a_different_muse_pick(self) -> None:
        from director.angles import apply_gaze_overrides
        picks = [_pick(0, "B"), _pick(1, "B")]
        line_gaze = {
            0: gaze_mod.build_line_gaze(
                0, {"B": [40.0, 38.0, 45.0], "C": [5.0, -6.0, 7.0]}),
            1: gaze_mod.build_line_gaze(
                1, {"B": [40.0, 4.0, 45.0], "C": [41.0, 38.0, 45.0]}),
        }
        self.assertEqual(line_gaze[0].pick, "C")
        self.assertIsNone(line_gaze[1].pick)
        apply_gaze_overrides(picks, line_gaze)
        self.assertEqual(picks[0].camera, "C")
        self.assertEqual(picks[0].method, "gaze")
        self.assertEqual(picks[0].shot_size, "medium")
        self.assertIn("speaker addresses camera C", picks[0].reason)
        self.assertEqual(picks[0].overruled, ["gaze_overrule:B->C"])
        self.assertEqual((picks[1].camera, picks[1].method), ("B", "muse"))

    def test_agreeing_gaze_keeps_the_camera_and_records_its_reason(self) -> None:
        from director.angles import apply_gaze_overrides
        picks = [_pick(0, "B")]
        line_gaze = {0: gaze_mod.build_line_gaze(
            0, {"B": [6.0, 7.0, 5.0], "C": [41.0, 38.0, 45.0]})}
        apply_gaze_overrides(picks, line_gaze)
        self.assertEqual((picks[0].camera, picks[0].method), ("B", "gaze"))
        self.assertIn("speaker addresses camera B", picks[0].reason)
        self.assertEqual(picks[0].overruled, [])

    def test_checks_still_overrule_a_gaze_pick_that_does_not_cover(self) -> None:
        clips = [SyncClip("A", "/media/A.mp4", 0.0, 30.0, 1920, 1080, 100.0),
                 SyncClip("B", "/media/B.mp4", 0.0, 30.0, 1920, 1080, 100.0)]
        lines = [_line(0, 0.0, 2.0, ["A"])]
        pick = Pick(0, "B", "medium", "speaker addresses camera B", "gaze", [])
        shots = apply_checks([pick], lines, clips, min_shot_s=1.0)
        self.assertEqual(shots[0].camera, "A")
        self.assertTrue(any(o.startswith("not_covering:B->A")
                            for o in shots[0].overrules))


class GazeSampleTests(unittest.TestCase):
    def test_sample_times_span_the_line(self) -> None:
        times = gaze_mod.line_sample_times(10.0, 16.0, 3)
        self.assertEqual(times, [11.0, 13.0, 15.0])
        self.assertEqual(gaze_mod.line_sample_times(1.0, 1.0, 3), [])
        self.assertEqual(gaze_mod.line_sample_times(0.0, 2.0, 0), [])

    def test_frame_command_shape(self) -> None:
        cmd = gaze_mod.gaze_frame_command("/media/B.mp4", 3.5,
                                          Path("/tmp/G000_B_0.jpg"),
                                          Path("/g/B.cube"))
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("lut3d=file='/g/B.cube'", vf)
        self.assertIn("scale=640:-2", vf)
        self.assertIn("-frames:v", cmd)
        bare = gaze_mod.gaze_frame_command("/media/B.mp4", 3.5,
                                           Path("/tmp/G000_B_0.jpg"), None)
        self.assertNotIn("lut3d", bare[bare.index("-vf") + 1])
        with self.assertRaises(ValueError):
            gaze_mod.gaze_frame_command("/media/B.mp4", 3.5,
                                        Path("/tmp/x.jpg"), Path("/g/a'b.cube"))

    def test_no_ffmpeg_gives_no_gaze(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            out = gaze_mod.sample_line_gaze(
                [_line(0, 0.0, 3.0, ["B"])], {"B": SimpleNamespace(
                    offset_s=0.0, path="/media/B.mp4")}, {},
                Path("/tmp/nowhere"))
        self.assertEqual(out, {})

    def test_no_helper_gives_no_gaze(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("director.sync.build_lips_helper",
                           return_value=(None, "nope")):
            out = gaze_mod.sample_line_gaze(
                [_line(0, 0.0, 3.0, ["B"])], {"B": SimpleNamespace(
                    offset_s=0.0, path="/media/B.mp4")}, {},
                Path(tmp) / "gaze-frames")
        self.assertEqual(out, {})

    def test_sampling_maps_helper_output_to_lines(self) -> None:
        from director.sync import LipsFrame
        with tempfile.TemporaryDirectory() as tmp:
            frames_dir = Path(tmp) / "gaze-frames"

            def fake_run(cmd: list[str]) -> None:
                out = Path(cmd[-1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"fake-jpeg")

            def fake_helper(helper: Path, directory: Path):
                names = sorted(p.name for p in Path(directory).iterdir())
                self.assertEqual(names, ["G000_B_0.jpg", "G000_B_1.jpg",
                                         "G000_B_2.jpg", "G001_B_0.jpg",
                                         "G001_B_1.jpg", "G001_B_2.jpg"])
                return [LipsFrame(0, None, (0.3, 0.2, 0.2, 0.3), 6.0, 0.0, 0.0),
                        LipsFrame(1, None, (0.3, 0.2, 0.2, 0.3), -7.0, 0.0, 0.0),
                        LipsFrame(2, None, None, None, None, None),
                        LipsFrame(3, None, (0.3, 0.2, 0.2, 0.3), 40.0, 0.0, 0.0),
                        LipsFrame(4, None, (0.3, 0.2, 0.2, 0.3), 4.0, 0.0, 0.0),
                        LipsFrame(5, None, (0.3, 0.2, 0.2, 0.3), 45.0, 0.0, 0.0)]

            lines = [_line(0, 10.0, 13.0, ["B"]), _line(1, 20.0, 23.0, ["B"])]
            clips = {"B": SimpleNamespace(offset_s=2.0, path="/media/B.mp4")}
            with mock.patch("ghostcut.ffmpeg_tools.run_command",
                            side_effect=fake_run), \
                    mock.patch("director.gaze.shutil.which",
                               return_value="/usr/bin/ffmpeg"), \
                    mock.patch("director.sync.build_lips_helper",
                               return_value=(Path("/fake/helper"), None)), \
                    mock.patch("director.sync.run_lips_helper",
                               side_effect=fake_helper):
                out = gaze_mod.sample_line_gaze(lines, clips, {"B": None},
                                                frames_dir)
            self.assertEqual(set(out), {0, 1})
            self.assertEqual(out[0].cameras["B"].yaws, [6.0, -7.0, None])
            self.assertEqual(out[0].pick, "B")
            self.assertIsNone(out[1].pick)
            self.assertIn("gaze ambiguous", out[1].reason)


class GazeRunTests(unittest.TestCase):
    def test_run_records_gaze_picks(self) -> None:
        from director.angles import run as run_angles
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sync = base / "sync.json"
            sync.write_text(json.dumps({
                "reference": "/media/A.mp4",
                "clips": [
                    {"label": "A", "offset_s": 0.0,
                     "camera": {"path": "/media/A.mp4", "fps": 30.0,
                                "width": 1920, "height": 1080,
                                "duration_s": 100.0}},
                    {"label": "B", "offset_s": 0.0,
                     "camera": {"path": "/media/B.mp4", "fps": 30.0,
                                "width": 1920, "height": 1080,
                                "duration_s": 100.0}},
                ]}), encoding="utf-8")
            words = base / "words.json"
            words.write_text(json.dumps({"words": [
                {"word": "hello", "start": 6.0, "end": 6.4},
                {"word": "world.", "start": 6.5, "end": 6.9},
            ]}), encoding="utf-8")
            line_gaze = {0: gaze_mod.build_line_gaze(
                0, {"A": [40.0, 38.0, 45.0], "B": [5.0, -6.0, 7.0]})}
            with mock.patch("director.gaze.sample_line_gaze",
                            return_value=line_gaze):
                payload = run_angles(
                    sync_path=sync, words_path=words,
                    out_path=base / "angles.json", audio_label=None,
                    render_path=None, min_shot_s=2.0, model="m", no_muse=True)
        self.assertEqual(payload["shots"][0]["camera"], "B")
        self.assertIn("speaker addresses camera B",
                      payload["shots"][0]["reason"])
        entry = payload["lines"][0]["gaze"]
        self.assertEqual(entry["pick"], "B")
        self.assertEqual(entry["B"]["yaw_deg"], [5.0, -6.0, 7.0])


if __name__ == "__main__":
    unittest.main()
