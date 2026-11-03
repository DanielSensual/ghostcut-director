"""PUSH-BRIEF.md: a slow push-in on every shot.

Offline throughout except one skipUnless(ffmpeg + ffprobe) test that
renders a short synthetic shot through the real render path and measures
smoothness from the rendered frames. No network, no Meta API, no Whisper.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from director import angles as angles_mod
from director.angles import (
    Shot,
    SyncClip,
    face_centre_from_box,
    push_in_filter,
    render_commands,
    shot_face_centres,
)

FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _clip(label: str, path: str, offset: float | None = 0.0,
          duration: float = 100.0, fps: float = 30.0) -> SyncClip:
    return SyncClip(label=label, path=path, offset_s=offset,
                    fps=fps, width=1920, height=1080, duration_s=duration)


def _shot(i: int, start: float, end: float, camera: str) -> Shot:
    if camera == "black":
        return Shot(i, start, end, camera, "", 0.0, end - start,
                    [i], "", "", "rule", ["no_camera"])
    return Shot(i, start, end, camera, f"/media/{camera}.mp4",
                start, end, [i], "medium", "ok", "muse", [])


def _vf(cmd: list[str]) -> str:
    return cmd[cmd.index("-vf") + 1]


class PushFilterTests(unittest.TestCase):
    def test_off_returns_empty(self) -> None:
        self.assertEqual(push_in_filter(0.0, 60, 0.5, 0.5, "30"), "")
        self.assertEqual(push_in_filter(-5.0, 60, 0.5, 0.5, "30"), "")
        self.assertEqual(push_in_filter(float("nan"), 60, 0.5, 0.5, "30"), "")
        self.assertEqual(push_in_filter(5.0, 1, 0.5, 0.5, "30"), "")
        self.assertEqual(push_in_filter(5.0, 0, 0.5, 0.5, "30"), "")

    def test_centred_shape(self) -> None:
        seg = push_in_filter(5.0, 60, 0.5, 0.5, "30")
        self.assertIn("scale=7680:4320:flags=lanczos", seg)
        self.assertIn("z='1+0.050000*on/59'", seg)
        self.assertIn("x='iw/2-(iw/zoom/2)'", seg)
        self.assertIn("y='ih/2-(ih/zoom/2)'", seg)
        self.assertIn("d=1", seg)
        self.assertIn("s=1920x1080", seg)
        self.assertIn("fps=30", seg)

    def test_face_centre_drifts(self) -> None:
        seg = push_in_filter(5.0, 120, 0.35, 0.45, "30")
        self.assertIn("x='(0.5+(0.350000-0.5)*on/119)*iw-(iw/zoom/2)'", seg)
        self.assertIn("y='(0.5+(0.450000-0.5)*on/119)*ih-(ih/zoom/2)'", seg)

    def test_centre_clamped_into_frame(self) -> None:
        seg = push_in_filter(5.0, 60, 2.0, -1.0, "30")
        self.assertIn("(1.000000-0.5)", seg)
        self.assertIn("(0.000000-0.5)", seg)

    def test_fps_passes_through(self) -> None:
        seg = push_in_filter(5.0, 60, 0.5, 0.5, "30000/1001")
        self.assertTrue(seg.endswith("fps=30000/1001"))


class FaceCentreTests(unittest.TestCase):
    def test_vision_box_to_ffmpeg_coords(self) -> None:
        # Vision origin lower-left; ffmpeg origin upper-left.
        fx, fy = face_centre_from_box((0.25, 0.6, 0.2, 0.2))
        self.assertAlmostEqual(fx, 0.35)
        self.assertAlmostEqual(fy, 0.3)

    def test_clamped(self) -> None:
        fx, fy = face_centre_from_box((-0.5, 0.9, 0.2, 0.2))
        self.assertEqual((fx, fy), (0.0, 0.0))


class RenderCommandsTests(unittest.TestCase):
    def _shots(self) -> list[Shot]:
        return [_shot(0, 2.0, 5.0, "A"),
                _shot(1, 5.0, 6.0, "black"),
                _shot(2, 6.0, 9.0, "B")]

    def _clips(self) -> dict[str, SyncClip]:
        return {"A": _clip("A", "/media/A.mp4"),
                "B": _clip("B", "/media/B.mp4")}

    def test_off_has_no_push_in(self) -> None:
        shots, clips = self._shots(), self._clips()
        default = render_commands(shots, clips, clips["A"], Path("/tmp/cut.mp4"))
        explicit = render_commands(shots, clips, clips["A"], Path("/tmp/cut.mp4"),
                                   push_in=0.0, face_centres={0: (0.3, 0.4)})
        self.assertEqual(default[0], explicit[0])
        self.assertNotIn("zoompan", " ".join(" ".join(cmd) for cmd in default[0]))

    def test_on_pushes_cameras_leaves_black(self) -> None:
        shots, clips = self._shots(), self._clips()
        parts, _, _ = render_commands(shots, clips, clips["A"],
                                       Path("/tmp/cut.mp4"), push_in=5.0)
        vfs = [_vf(cmd) for cmd in parts]
        for vf in (vfs[0], vfs[2]):
            self.assertIn("zoompan", vf)
            # after the grade/framing, before the output format
            self.assertLess(vf.index("fps=30,"), vf.index("zoompan"))
            self.assertLess(vf.index("zoompan"), vf.index("format=yuv420p"))
        self.assertNotIn("zoompan", vfs[1])
        # shot boundaries and durations unchanged
        off, _, _ = render_commands(shots, clips, clips["A"], Path("/tmp/cut.mp4"))
        self.assertEqual(
            [cmd[cmd.index("-frames:v") + 1] for cmd in parts],
            [cmd[cmd.index("-frames:v") + 1] for cmd in off])

    def test_face_centres_routed_per_shot(self) -> None:
        shots, clips = self._shots(), self._clips()
        parts, _, _ = render_commands(shots, clips, clips["A"],
                                       Path("/tmp/cut.mp4"), push_in=5.0,
                                       face_centres={0: (0.35, 0.45)})
        self.assertIn("(0.350000-0.5)", _vf(parts[0]))
        # shot 2 has no face: the frame centre
        self.assertIn("x='iw/2-(iw/zoom/2)'", _vf(parts[2]))

    def test_graded_chain_push_goes_after_framing(self) -> None:
        from director.grade import GradeShotInfo, SourceColour

        shots, clips = self._shots(), self._clips()
        grade = {label: GradeShotInfo(label, Path(f"/g/{label}.cube"),
                                      SourceColour())
                 for label in ("A", "B")}
        parts, _, _ = render_commands(shots, clips, clips["A"],
                                       Path("/tmp/cut.mp4"),
                                       grade=grade, push_in=5.0)
        vf = _vf(parts[0])
        self.assertIn("lut3d=", vf)
        self.assertLess(vf.index("fps=30,"), vf.index("zoompan"))
        self.assertLess(vf.index("zoompan"), vf.index("out_color_matrix"))

    def test_single_frame_shot_has_no_push_in(self) -> None:
        clips = self._clips()
        shots = [Shot(0, 2.0, 2.01, "A", "/media/A.mp4", 2.0, 2.01,
                      [0], "medium", "ok", "muse", [])]
        parts, _, _ = render_commands(shots, clips, clips["A"],
                                       Path("/tmp/cut.mp4"), push_in=5.0)
        self.assertNotIn("zoompan", _vf(parts[0]))

    def test_invalid_rejected(self) -> None:
        shots, clips = self._shots(), self._clips()
        for bad in (-1.0, float("nan")):
            with self.assertRaises(ValueError):
                render_commands(shots, clips, clips["A"], Path("/tmp/cut.mp4"),
                                push_in=bad)


class SamplingTests(unittest.TestCase):
    def _shot(self) -> Shot:
        return Shot(0, 10.0, 14.0, "B", "/media/B.mp4", 8.0, 12.0,
                    [0], "medium", "ok", "muse", [])

    def test_no_ffmpeg_gives_no_centres(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            out = shot_face_centres(
                [self._shot()], {"B": SimpleNamespace(path="/media/B.mp4")},
                None, "30", Path("/tmp/nowhere"))
        self.assertEqual(out, {})

    def test_no_helper_gives_no_centres(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("director.sync.build_lips_helper",
                           return_value=(None, "nope")):
            out = shot_face_centres(
                [self._shot()], {"B": SimpleNamespace(path="/media/B.mp4")},
                None, "30", Path(tmp) / "push-faces")
        self.assertEqual(out, {})

    def test_helper_boxes_map_to_shots(self) -> None:
        from director.sync import LipsFrame

        shots = [self._shot(),
                 Shot(1, 14.0, 18.0, "black", "", 0.0, 4.0,
                      [1], "", "", "rule", ["no_camera"])]
        seen: list[list[str]] = []

        def fake_run(cmd: list[str]) -> None:
            seen.append(cmd)
            out = Path(cmd[-1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"fake-jpeg")

        def fake_helper(helper: Path, directory: Path):
            names = sorted(p.name for p in Path(directory).iterdir())
            self.assertEqual(names, ["P000_B.jpg"])
            return [LipsFrame(0, None, (0.25, 0.6, 0.2, 0.2), 5.0, 0.0, 0.0)]

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("director.angles.run_command",
                            side_effect=fake_run), \
                    mock.patch("director.sync.build_lips_helper",
                               return_value=(Path("/fake/helper"), None)), \
                    mock.patch("director.sync.run_lips_helper",
                               side_effect=fake_helper):
                out = shot_face_centres(
                    shots, {"B": SimpleNamespace(path="/media/B.mp4")},
                    None, "30", Path(tmp) / "push-faces")
        self.assertEqual(set(out), {0})
        self.assertAlmostEqual(out[0][0], 0.35)
        self.assertAlmostEqual(out[0][1], 0.3)
        # one output-size frame at the shot's middle; the black shot skipped
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][seen[0].index("-ss") + 1], "10.000")
        self.assertIn("pad=1920:1080", seen[0][seen[0].index("-vf") + 1])

    def test_no_face_gives_no_centre(self) -> None:
        from director.sync import LipsFrame

        def fake_run(cmd: list[str]) -> None:
            out = Path(cmd[-1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"fake-jpeg")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("director.angles.run_command",
                            side_effect=fake_run), \
                    mock.patch("director.sync.build_lips_helper",
                               return_value=(Path("/fake/helper"), None)), \
                    mock.patch("director.sync.run_lips_helper",
                               return_value=[LipsFrame(0, None, None,
                                                       None, None, None)]):
                out = shot_face_centres(
                    [self._shot()], {"B": SimpleNamespace(path="/media/B.mp4")},
                    None, "30", Path(tmp) / "push-faces")
        self.assertEqual(out, {})

    def test_missing_clip_never_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("director.sync.build_lips_helper",
                           return_value=(Path("/fake/helper"), None)), \
                mock.patch("director.sync.run_lips_helper") as helper_run:
            out = shot_face_centres([self._shot()], {}, None, "30",
                                    Path(tmp) / "push-faces")
        self.assertEqual(out, {})
        helper_run.assert_not_called()


class RunTests(unittest.TestCase):
    def _job(self, base: Path) -> tuple[Path, Path]:
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
        return sync, words

    def test_run_records_push_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sync, words = self._job(base)
            with mock.patch("director.gaze.sample_line_gaze", return_value={}):
                payload = angles_mod.run(
                    sync_path=sync, words_path=words,
                    out_path=base / "angles.json", audio_label=None,
                    render_path=None, min_shot_s=2.0, model="m", no_muse=True,
                    push_in=4.0)
            saved = json.loads((base / "angles.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["push_in"], 4.0)
        self.assertEqual(payload["push_in"], 4.0)

    def test_run_defaults_to_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sync, words = self._job(base)
            with mock.patch("director.gaze.sample_line_gaze", return_value={}):
                payload = angles_mod.run(
                    sync_path=sync, words_path=words,
                    out_path=base / "angles.json", audio_label=None,
                    render_path=None, min_shot_s=2.0, model="m", no_muse=True)
        self.assertEqual(payload["push_in"], 0.0)

    def test_run_rejects_negative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sync, words = self._job(base)
            with mock.patch("director.gaze.sample_line_gaze", return_value={}):
                with self.assertRaises(ValueError):
                    angles_mod.run(
                        sync_path=sync, words_path=words,
                        out_path=base / "angles.json", audio_label=None,
                        render_path=None, min_shot_s=2.0, model="m",
                        no_muse=True, push_in=-2.0)


def _bar_separations(path: Path, frames: int) -> list[float]:
    """Per-frame (right bar centroid − left bar centroid) from rawvideo."""
    width, height = 1920, 1080
    done = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        capture_output=True, check=False)
    raw = done.stdout
    stride = width * height * 3
    if len(raw) // stride != frames:
        raise AssertionError(
            f"want {frames} decoded frames, have {len(raw) // stride}")
    seps = []
    for n in range(frames):
        buf = raw[n * stride:(n + 1) * stride]
        dark = [0.0] * width
        for row in range(500, 580):
            off = row * width * 3
            for x in range(width):
                dark[x] += 255.0 - (buf[off + x * 3] + buf[off + x * 3 + 1]
                                    + buf[off + x * 3 + 2]) / 3.0

        def centroid(lo: int, hi: int) -> float:
            total = sum(dark[lo:hi])
            if total <= 0:
                return (lo + hi) / 2
            return sum(x * dark[x] for x in range(lo, hi)) / total

        seps.append(centroid(1220, 1320) - centroid(600, 700))
    return seps


@unittest.skipUnless(FFMPEG, "needs ffmpeg and ffprobe")
class RenderSmoothnessTests(unittest.TestCase):
    def test_rendered_push_in_is_monotonic(self) -> None:
        from ghostcut.ffmpeg_tools import extract_media_stats, probe_media

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            clip = base / "cam.mp4"
            video = ("color=c=white:s=1920x1080:r=30:d=2,"
                     "drawbox=x=640:y=0:w=6:h=1080:c=black:t=fill,"
                     "drawbox=x=1274:y=0:w=6:h=1080:c=black:t=fill,"
                     "format=yuv420p")
            done = subprocess.run(
                ["ffmpeg", "-v", "error", "-y",
                 "-f", "lavfi", "-i", video,
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                 "-map", "0:v:0", "-map", "1:a:0",
                 "-frames:v", "60", "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", "16", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-shortest", str(clip)],
                capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, msg=done.stderr)
            sync_clip = _clip("A", str(clip), 0.0, 2.0)
            shots = [Shot(0, 0.0, 2.0, "A", str(clip), 0.0, 2.0,
                          [0], "medium", "ok", "rule", [])]
            out = base / "cut.mp4"
            angles_mod.render(shots, {"A": sync_clip}, sync_clip, out,
                              grade=None, kept_spans=None,
                              push_in=5.0, face_centres={})
            stats = extract_media_stats(probe_media(out))
            self.assertAlmostEqual(float(stats["duration_s"]), 2.0, delta=0.10)
            seps = _bar_separations(out, 60)
            zooms = [sep / seps[0] for sep in seps]
            self.assertAlmostEqual(zooms[-1], 1.05, delta=0.01)
            self.assertGreater(zooms[-1] - zooms[0], 0.03)
            diffs = [b - a for a, b in zip(zooms, zooms[1:])]
            reversals = [d for d in diffs if d < -1e-4]
            self.assertEqual(reversals, [])
            rises = sum(1 for d in diffs if d > 1e-6)
            self.assertGreaterEqual(rises, 54)


if __name__ == "__main__":
    unittest.main()
