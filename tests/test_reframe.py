from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from director import reframe
from director.reframe import (
    Detection,
    MediaInfo,
    ShotPlan,
    ShotSpan,
    build_shots,
    clamp_crop_x,
    crop_size,
    detections_from_result,
    ensure_within_budget,
    estimate_cost_usd,
    fill_gaps,
    gaussian_smooth,
    load_shot_cuts,
    merge_close_cuts,
    per_frame_crop_xs,
    plan_shots,
    proxy_command,
    proxy_frame_count,
    render_command,
    sam_window_spans,
    sendcmd_text,
    shot_sample_ranges,
    subject_for_shot,
)

FRAME_W, FRAME_H = 1920, 1080


def _det(frame: int, oid: str, box: tuple[float, float, float, float],
         confidence: float | None = 0.9) -> Detection:
    return Detection(frame=frame, object_id=oid, box=box, confidence=confidence or 0.0)


def _tall(frame: int, oid: str, centre_x: float, height: float = 400.0) -> Detection:
    return _det(frame, oid, (centre_x - 150.0, 300.0, centre_x + 150.0, 300.0 + height))


def _shot(id: int, start: float, end: float) -> ShotSpan:
    return ShotSpan(id=id, start_s=start, end_s=end)


def _probe_dict(width: int = 1920, height: int = 1080, fps: str = "30/1",
                duration: float = 60.0, audio: bool = True) -> dict:
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264",
                "width": width, "height": height,
                "r_frame_rate": fps, "avg_frame_rate": fps}]
    if audio:
        streams.append({"index": 1, "codec_type": "audio", "codec_name": "aac"})
    return {"streams": streams, "format": {"duration": str(duration)}}


class CropTests(unittest.TestCase):
    def test_crop_size_1080p_and_2160p(self) -> None:
        self.assertEqual(crop_size(1920, 1080), (608, 1080))
        self.assertEqual(crop_size(3840, 2160), (1216, 2160))
        w, h = crop_size(1920, 1079)
        self.assertEqual(h, 1079)
        self.assertEqual(w % 2, 0)
        w, _ = crop_size(1000, 900)
        self.assertLessEqual(w, 1000)
        self.assertEqual(w % 2, 0)

    def test_clamp_crop_x_edges_and_centre(self) -> None:
        self.assertEqual(clamp_crop_x(960.0, 1920, 608), (1920 - 608) // 2)
        self.assertEqual(clamp_crop_x(100.0, 1920, 608), 0)
        self.assertEqual(clamp_crop_x(1900.0, 1920, 608), 1920 - 608)
        self.assertEqual(clamp_crop_x(500.0, 1920, 1920), 0)
        self.assertEqual(clamp_crop_x(500.0, 1000, 1200), 0)
        self.assertEqual(clamp_crop_x(904.5, 1920, 608), 601)


class ProbeTests(unittest.TestCase):
    def test_parse_probe_rejects_vertical_inputs(self) -> None:
        with self.assertRaises(ValueError):
            reframe.parse_probe(_probe_dict(width=1080, height=1080), Path("sq.mp4"))
        with self.assertRaises(ValueError):
            reframe.parse_probe(_probe_dict(width=1080, height=1920), Path("vert.mp4"))
        with self.assertRaises(ValueError):
            reframe.parse_probe(_probe_dict(fps="0/0"), Path("norate.mp4"))
        info = reframe.parse_probe(_probe_dict(fps="30000/1001"), Path("cut.mp4"))
        self.assertAlmostEqual(info.fps, 30000 / 1001)
        with self.assertRaises(ValueError):
            reframe.parse_probe({"streams": [], "format": {"duration": "5"}}, Path("a.mp4"))

    def test_proxy_command_shape(self) -> None:
        command = proxy_command(Path("cut.mp4"), Path("work/proxy.mp4"))
        self.assertIsInstance(command, list)
        self.assertTrue(all(isinstance(part, str) for part in command))
        self.assertIn("fps=5,scale=-2:540", " ".join(command))
        self.assertIn("-an", command)
        self.assertIn("-map_metadata", command)
        self.assertIn("-1", command)
        self.assertIn("0:v:0", command)
        self.assertEqual(command[-1], "work/proxy.mp4")

    def test_proxy_frame_count(self) -> None:
        self.assertEqual(proxy_frame_count(60.0), 300)
        self.assertEqual(proxy_frame_count(0.1), 1)
        self.assertEqual(proxy_frame_count(61.2), 306)


class ShotTests(unittest.TestCase):
    def _angles_file(self, starts: list[float]) -> Path:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"version": 1, "shots": [
            {"id": i, "start_s": s, "end_s": s + 1.0} for i, s in enumerate(starts)
        ]}, tmp)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return Path(tmp.name)

    def test_load_shot_cuts_from_angles_json(self) -> None:
        self.assertEqual(load_shot_cuts(self._angles_file([0.0, 2.5, 7.0])), [2.5, 7.0])
        self.assertEqual(load_shot_cuts(self._angles_file([0.0])), [])
        with self.assertRaises(ValueError):
            load_shot_cuts(self._angles_file([]))
        bad = self._angles_file([0.0, 1.0])
        payload = json.loads(bad.read_text())
        del payload["shots"]
        bad.write_text(json.dumps(payload))
        with self.assertRaises(ValueError):
            load_shot_cuts(bad)
        bad2 = self._angles_file([0.0, 1.0])
        payload = json.loads(bad2.read_text())
        del payload["shots"][1]["start_s"]
        bad2.write_text(json.dumps(payload))
        with self.assertRaises(ValueError):
            load_shot_cuts(bad2)
        bad3 = self._angles_file([0.0])
        bad3.write_text("{not json")
        with self.assertRaises(ValueError):
            load_shot_cuts(bad3)

    def test_build_shots_tiles_and_merges_near_duplicates(self) -> None:
        shots = build_shots([1.0, 1.15, 5.0], 10.0)
        self.assertEqual([(s.start_s, s.end_s) for s in shots],
                         [(0.0, 1.0), (1.0, 5.0), (5.0, 10.0)])
        self.assertEqual([s.id for s in shots], [0, 1, 2])
        edge_shots = build_shots([0.1, 9.9], 10.0)
        self.assertEqual([(s.start_s, s.end_s) for s in edge_shots], [(0.0, 10.0)])
        single = build_shots([], 10.0)
        self.assertEqual(len(single), 1)
        self.assertEqual((single[0].start_s, single[0].end_s), (0.0, 10.0))
        for shots in (shots, edge_shots, single):
            self.assertEqual(shots[0].start_s, 0.0)
            self.assertEqual(shots[-1].end_s, 10.0)
            for a, b in zip(shots, shots[1:]):
                self.assertEqual(a.end_s, b.start_s)

    def test_merge_close_cuts_chain_keeps_earliest(self) -> None:
        self.assertEqual(merge_close_cuts([2.0, 2.2, 2.4], 10.0), [2.0, 2.4])

    def test_shot_sample_ranges_partition(self) -> None:
        shots = [_shot(0, 0.0, 1.0), _shot(1, 1.0, 3.0)]
        self.assertEqual(shot_sample_ranges(shots, 15), [(0, 5), (5, 15)])
        shots = [_shot(0, 0.0, 0.25), _shot(1, 0.25, 0.3), _shot(2, 0.3, 1.0)]
        ranges = shot_sample_ranges(shots, 5)
        self.assertEqual(ranges, [(0, 2), (2, 2), (2, 5)])
        covered = [k for start, end in ranges for k in range(start, end)]
        self.assertEqual(covered, list(range(5)))
        for (a_start, a_end), (b_start, b_end) in zip(ranges, ranges[1:]):
            self.assertEqual(a_end, b_start)


class SubjectTests(unittest.TestCase):
    def test_subject_most_present_wins(self) -> None:
        dets = [_tall(k, "small", 400.0, height=300.0) for k in range(8)]
        dets += [_tall(k, "big", 1500.0, height=800.0) for k in range(5)]
        choice = subject_for_shot(dets, n_samples=10, frame_w=FRAME_W,
                                  frame_h=FRAME_H, fallback_reason=None)
        self.assertEqual(choice.object_id, "small")
        self.assertAlmostEqual(choice.presence, 0.8)
        self.assertEqual(choice.reason, "ok")
        self.assertEqual(set(choice.centres), set(range(8)))

    def test_subject_passerby_refused(self) -> None:
        dets = [_tall(k, "passer", 400.0) for k in range(3)]
        choice = subject_for_shot(dets, n_samples=10, frame_w=FRAME_W,
                                  frame_h=FRAME_H, fallback_reason=None)
        self.assertIsNone(choice.object_id)
        self.assertEqual(choice.reason, "low_presence")
        self.assertAlmostEqual(choice.presence, 0.3)

    def test_subject_tiny_background_refused(self) -> None:
        dets = [_tall(k, "tiny", 400.0, height=108.0) for k in range(9)]
        choice = subject_for_shot(dets, n_samples=10, frame_w=FRAME_W,
                                  frame_h=FRAME_H, fallback_reason=None)
        self.assertIsNone(choice.object_id)
        self.assertEqual(choice.reason, "too_small")
        self.assertAlmostEqual(choice.presence, 0.9)

    def test_subject_empty_shot(self) -> None:
        choice = subject_for_shot([], n_samples=10, frame_w=FRAME_W,
                                  frame_h=FRAME_H, fallback_reason=None)
        self.assertIsNone(choice.object_id)
        self.assertEqual(choice.reason, "no_detections")
        self.assertEqual(choice.presence, 0.0)
        flagged = subject_for_shot([], n_samples=10, frame_w=FRAME_W,
                                   frame_h=FRAME_H, fallback_reason="no_sam")
        self.assertEqual(flagged.reason, "no_sam")
        nosamples = subject_for_shot([], n_samples=0, frame_w=FRAME_W,
                                     frame_h=FRAME_H, fallback_reason="no_sam")
        self.assertEqual(nosamples.reason, "no_samples")

    def test_subject_duplicate_detections_in_one_sample_averaged(self) -> None:
        dets = [_tall(k, "one", 400.0) for k in range(4)]
        dets.append(_det(1, "one", (550.0, 300.0, 850.0, 700.0)))
        choice = subject_for_shot(dets, n_samples=4, frame_w=FRAME_W,
                                  frame_h=FRAME_H, fallback_reason=None)
        self.assertEqual(choice.object_id, "one")
        self.assertAlmostEqual(choice.presence, 1.0)
        self.assertAlmostEqual(choice.centres[1], (400.0 + 700.0) / 2)


class SmoothTests(unittest.TestCase):
    def test_fill_gaps_interpolates_and_holds_ends(self) -> None:
        self.assertEqual(fill_gaps([None, 10.0, None, None, 40.0, None]),
                         [10.0, 10.0, 20.0, 30.0, 40.0, 40.0])
        self.assertEqual(fill_gaps([None, 7.0, None]), [7.0, 7.0, 7.0])
        with self.assertRaises(ValueError):
            fill_gaps([None, None])

    def test_gaussian_smooth_constant_kept(self) -> None:
        out = gaussian_smooth([500.0] * 20)
        for value in out:
            self.assertAlmostEqual(value, 500.0, delta=1e-9)

    def test_gaussian_smooth_no_lag_on_ramp(self) -> None:
        ramp = [float(i * 10) for i in range(60)]
        out = gaussian_smooth(ramp)
        for i in range(9, 51):
            self.assertAlmostEqual(out[i], ramp[i], delta=1e-6)
        # Edges pull toward the interior (reflection never borrows across
        # the boundary); the pull is bounded by ~sigma*sqrt(2/pi) steps.
        for got, want in zip(out, ramp):
            self.assertLessEqual(abs(got - want), 25.0)

    def test_gaussian_smooth_removes_jitter(self) -> None:
        jitter = [490.0 if i % 2 == 0 else 510.0 for i in range(30)]
        out = gaussian_smooth(jitter)
        for value in out[1:-1]:
            self.assertLessEqual(abs(value - 500.0), 2.0)

    def test_each_shot_planned_on_its_own(self) -> None:
        shots = [_shot(0, 0.0, 1.0), _shot(1, 1.0, 2.0), _shot(2, 2.0, 3.0)]
        dets = [_tall(k, "left", 400.0) for k in range(5)]
        dets += [_tall(k, "right", 1500.0) for k in range(10, 15)]
        plans = plan_shots(shots, dets, n_samples=15, frame_w=FRAME_W,
                           frame_h=FRAME_H, fallback_reason=None)
        self.assertEqual([p.method for p in plans], ["subject", "centre", "subject"])
        self.assertEqual(plans[0].object_id, "left")
        self.assertEqual(plans[2].object_id, "right")
        for c in plans[0].centres:
            self.assertAlmostEqual(c, 400.0, delta=1e-6)
        for c in plans[2].centres:
            self.assertAlmostEqual(c, 1500.0, delta=1e-6)
        self.assertTrue(all(c == FRAME_W / 2.0 for c in plans[1].centres))
        self.assertTrue(all(c > 1200.0 for c in plans[2].centres))


class PathTests(unittest.TestCase):
    def _two_shot_plans(self) -> list[ShotPlan]:
        left = ShotPlan(id=0, start_s=0.0, end_s=1.0, method="subject", object_id="1",
                        presence=1.0, reason="ok", sample_start=0, sample_end=5,
                        centres=[400.0] * 5)
        right = ShotPlan(id=1, start_s=1.0, end_s=2.0, method="subject", object_id="2",
                         presence=1.0, reason="ok", sample_start=5, sample_end=10,
                         centres=[1500.0] * 5)
        return [left, right]

    def test_per_frame_crop_jumps_exactly_at_cut(self) -> None:
        xs = per_frame_crop_xs(self._two_shot_plans(), fps=30.0, duration_s=2.0,
                               frame_w=FRAME_W, crop_w=608)
        self.assertEqual(len(xs), 60)
        before = clamp_crop_x(400.0, FRAME_W, 608)
        after = clamp_crop_x(1500.0, FRAME_W, 608)
        self.assertEqual(xs[:30], [before] * 30)
        self.assertEqual(xs[30:], [after] * 30)

    def test_per_frame_crop_interpolates_between_samples(self) -> None:
        plan = ShotPlan(id=0, start_s=0.0, end_s=6.0, method="subject", object_id="1",
                        presence=1.0, reason="ok", sample_start=0, sample_end=30,
                        centres=[1000.0 + 10.0 * k for k in range(30)])
        xs = per_frame_crop_xs([plan], fps=30.0, duration_s=6.0,
                               frame_w=FRAME_W, crop_w=608)
        self.assertEqual(len(xs), 180)
        lo = clamp_crop_x(1140.0, FRAME_W, 608)
        hi = clamp_crop_x(1150.0, FRAME_W, 608)
        self.assertEqual(xs[87], (lo + hi) // 2)
        self.assertLessEqual(abs(xs[86] - (lo + (hi - lo) / 3)), 1.0)
        self.assertLessEqual(abs(xs[88] - (lo + 2 * (hi - lo) / 3)), 1.0)
        for a, b in zip(xs, xs[1:]):
            self.assertLessEqual(abs(b - a), 11)

    def test_sendcmd_writes_only_changes(self) -> None:
        text, moves = sendcmd_text([5, 5, 5, 9, 9, 12], fps=10.0)
        self.assertEqual(moves, 2)
        self.assertEqual(text, "0.300000 crop@rf x 9;\n0.500000 crop@rf x 12;\n")
        self.assertEqual(sendcmd_text([5, 5, 5], fps=10.0), ("", 0))

    def test_render_command_shape(self) -> None:
        command = render_command(Path("cut.mp4"), Path("out.mp4"), crop_w=608,
                                 crop_h=1080, first_x=96, script_path=Path("w/crop.sendcmd"))
        self.assertIsInstance(command, list)
        self.assertEqual(command.count("-i"), 1)
        flat = " ".join(command)
        vf = command[command.index("-vf") + 1]
        self.assertLess(flat.index("sendcmd="), flat.index("crop@rf"))
        self.assertIn("crop@rf=w=608:h=1080:x=96:y=0", vf)
        self.assertIn("scale=1080:1920:flags=lanczos", vf)
        self.assertIn("setsar=1", vf)
        self.assertIn("format=yuv420p", vf)
        self.assertIn("0:a?", command)
        self.assertIn("-c:a", command)
        self.assertEqual(command[command.index("-c:a") + 1], "copy")
        self.assertEqual(command[-1], "out.mp4")


class CostTests(unittest.TestCase):
    def test_estimate_cost_60s_anchor(self) -> None:
        self.assertEqual(
            sam_window_spans(300),
            [(0, 50), (45, 95), (90, 140), (135, 185), (180, 230), (225, 275), (270, 300)],
        )
        self.assertAlmostEqual(estimate_cost_usd(300), 0.07, delta=1e-9)
        self.assertAlmostEqual(estimate_cost_usd(30), 0.01, delta=1e-9)

    def test_cost_guard_refuses_above_cap(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ensure_within_budget(0.07, 0.05, 300)
        message = str(ctx.exception)
        self.assertIn("$0.07", message)
        self.assertIn("$0.05", message)
        self.assertIsNone(ensure_within_budget(0.07, 2.0, 300))
        with mock.patch.object(reframe, "probe_cut",
                               side_effect=AssertionError("must not probe")):
            with self.assertRaises(ValueError):
                reframe.run(video_path=Path("cut.mp4"), out_path=Path("r.json"),
                            render_path=None, shots_path=None,
                            no_sam=False, max_usd=-1.0)


class TrackTests(unittest.TestCase):
    def _stand_in(self, frame_size=(960, 540)):
        dets = [
            types.SimpleNamespace(frame=0, object_id=7,
                                  box=(100.0, 100.0, 200.0, 300.0), confidence=None),
            types.SimpleNamespace(frame=1, object_id="a",
                                  box=(-50.0, -20.0, 100.0, 200.0), confidence=0.9),
            types.SimpleNamespace(frame=2, object_id="b",
                                  box=(10.0, 10.0, 10.0, 50.0), confidence=0.5),
            types.SimpleNamespace(frame=99, object_id="c",
                                  box=(10.0, 10.0, 50.0, 60.0), confidence=0.5),
        ]
        return types.SimpleNamespace(
            detections=dets, frame_size=frame_size, usd=0.05, frames_billed=100,
            saturated_spans=lambda: [(50, 60)],
        )

    def test_detections_from_result_scales_and_clips(self) -> None:
        dets = detections_from_result(self._stand_in(), frame_w=1920, frame_h=1080,
                                      n_samples=10, fallback_frame_size=(1920, 1080))
        self.assertEqual(len(dets), 2)
        self.assertEqual(dets[0].object_id, "7")
        self.assertEqual(dets[0].box, (200.0, 200.0, 400.0, 600.0))
        self.assertEqual(dets[0].confidence, 0.0)
        self.assertEqual(dets[1].object_id, "a")
        self.assertEqual(dets[1].box, (0.0, 0.0, 200.0, 400.0))

    def test_plan_from_stand_in_segment_windowed_result(self) -> None:
        result = types.SimpleNamespace(
            detections=[
                types.SimpleNamespace(frame=k, object_id="1",
                                      box=(600.0, 300.0, 900.0, 700.0), confidence=0.9)
                for k in range(10)
            ],
            frame_size=(1920, 1080), usd=0.05, frames_billed=100,
            saturated_spans=lambda: [],
        )
        dets = detections_from_result(result, frame_w=FRAME_W, frame_h=FRAME_H,
                                      n_samples=15, fallback_frame_size=(1920, 1080))
        shots = [_shot(0, 0.0, 1.0), _shot(1, 1.0, 2.0), _shot(2, 2.0, 3.0)]
        plans = plan_shots(shots, dets, n_samples=15, frame_w=FRAME_W,
                           frame_h=FRAME_H, fallback_reason=None)
        self.assertEqual(plans[0].method, "subject")
        self.assertEqual(plans[0].object_id, "1")
        self.assertEqual(plans[1].method, "subject")
        self.assertEqual(plans[2].method, "centre")
        self.assertEqual(plans[2].reason, "no_detections")


class RunTests(unittest.TestCase):
    def _media(self) -> MediaInfo:
        return MediaInfo(path=Path("cut.mp4"), width=1920, height=1080,
                         fps=30.0, duration_s=4.0)

    def _angles_file(self, directory: Path) -> Path:
        path = directory / "angles.json"
        path.write_text(json.dumps({"version": 1, "shots": [
            {"id": 0, "start_s": 0.0, "end_s": 2.0},
            {"id": 1, "start_s": 2.0, "end_s": 4.0},
        ]}))
        return path

    def test_run_end_to_end_centred_without_sam(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            out_path = directory / "reframe.json"
            shots_path = self._angles_file(directory)
            with mock.patch.object(reframe, "probe_cut", return_value=self._media()), \
                    mock.patch.object(reframe, "run_command",
                                      side_effect=AssertionError("no ffmpeg")) as run_cmd:
                payload = reframe.run(video_path=Path("cut.mp4"), out_path=out_path,
                                      render_path=None, shots_path=shots_path,
                                      no_sam=True, max_usd=2.0)
            run_cmd.assert_not_called()
            on_disk = json.loads(out_path.read_text())
            self.assertEqual(on_disk, payload)
            self.assertEqual(payload["method"], "centre")
            self.assertEqual([s["method"] for s in payload["shots"]], ["centre", "centre"])
            self.assertEqual([s["reason"] for s in payload["shots"]], ["no_sam", "no_sam"])
            self.assertEqual(payload["spend_usd"], 0)
            self.assertEqual(payload["frames_billed"], 0)
            self.assertEqual(payload["estimate_usd"], 0.0)
            self.assertEqual(payload["render"], None)
            self.assertEqual(len(payload["samples"]), 20)
            self.assertTrue(all(s["centre_x"] == 960.0 for s in payload["samples"]))
            self.assertTrue(any("no_sam" in w for w in payload["warnings"]))

    def test_run_refuses_vertical_before_any_command(self) -> None:
        portrait = MediaInfo(path=Path("vert.mp4"), width=1080, height=1920,
                             fps=30.0, duration_s=4.0)
        with mock.patch.object(reframe, "probe_cut", return_value=portrait), \
                mock.patch.object(reframe, "run_command",
                                  side_effect=AssertionError("no ffmpeg")), \
                mock.patch.object(reframe, "track",
                                  side_effect=AssertionError("no sam")):
            with self.assertRaises(ValueError):
                reframe.run(video_path=Path("vert.mp4"), out_path=Path("r.json"),
                            render_path=None, shots_path=None,
                            no_sam=True, max_usd=2.0)


if __name__ == "__main__":
    unittest.main()
