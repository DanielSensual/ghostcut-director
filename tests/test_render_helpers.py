from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import director_test_run as d  # noqa: E402

HAVE_FF = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
FONT = Path("/System/Library/Fonts/Avenir Next.ttc").exists()
try:
    import numpy as np
    import PIL  # noqa: F401
    HAVE_NP = True
except ImportError:  # pragma: no cover
    HAVE_NP = False
RATE = 1001 / 60000


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args], check=True)


def streams(path: Path) -> list[dict]:
    out = subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-show_entries",
                          "stream=codec_type,codec_name,width,height,r_frame_rate,sample_aspect_ratio,pix_fmt,start_time,"
                          "duration,nb_read_packets,sample_rate,channels", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"]


def vstream(path: Path) -> dict:
    return next(s for s in streams(path) if s["codec_type"] == "video")


def luma(path: Path, w: int = 32, h: int = 18) -> "np.ndarray":
    """Every decoded frame (in pts order, no rate conversion) as a small gray image: (n, h, w) float."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vf", f"scale={w}:{h},format=gray",
                          "-fps_mode", "passthrough", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w).astype(float)


def framemd5(path: Path) -> list[str]:
    out = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v", "-f", "framemd5", "-"],
                         capture_output=True, text=True, check=True).stdout
    return [ln.split(",")[-1].strip() for ln in out.splitlines() if ln and not ln.startswith("#")]


@contextmanager
def stub_modules(mods: dict):
    saved = {k: sys.modules.get(k) for k in mods}
    sys.modules.update(mods)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def old_overlay(k: int, s: float, e: float, speed: float, last: str) -> list[str]:
    """broll()'s overlay strings before the refactor, verbatim."""
    return [f"[{k}:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
            f"setpts=(PTS-STARTPTS)/{speed}+{s:.3f}/TB,fps=60000/1001,format=yuv420p[b{k}]",
            f"[{last}][b{k}]overlay=enable='between(t,{s:.3f},{e:.3f})':eof_action=pass[v{k}]"]


class OverlayArgvTest(unittest.TestCase):
    def test_overlay_filters_exact(self):
        self.assertEqual(d._overlay_filters(17, 145.7, 150.6, 0.5, "v16"), (
            "[17:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
            "setpts=(PTS-STARTPTS)/0.5+145.700/TB,fps=60000/1001,format=yuv420p[b17]",
            "[v16][b17]overlay=enable='between(t,145.700,150.600)':eof_action=pass[v17]"))
        self.assertEqual(list(d._overlay_filters(1, 7.6, 11.8, 1.0, "0:v")), old_overlay(1, 7.6, 11.8, 1.0, "0:v"))

    def test_render_overlays_argv_two(self):
        ov = [{"source": "src/D0292.MP4", "in": 7.525, "t": 145.7, "d": 4.9},
              {"source": "src/V10.mp4", "in": 3.5, "t": 156.05, "d": 3.537}]
        argv = d.render_overlays(Path("base.mp4"), ov, Path("out.mp4"), dry_run=True)
        self.assertEqual(argv[:5], ["ffmpeg", "-v", "error", "-nostdin", "-y"])
        self.assertEqual(argv[5:19], ["-i", "base.mp4", "-ss", "7.525", "-t", "2.450", "-i", "src/D0292.MP4",
                                      "-ss", "3.500", "-t", f"{3.537 * 0.5:.3f}", "-i", "src/V10.mp4"])
        graph = argv[argv.index("-filter_complex") + 1]
        self.assertEqual(graph, ";".join(old_overlay(1, 145.7, 150.6, 0.5, "0:v") + old_overlay(2, 156.05, 159.587, 0.5, "v1")
                                         + ["[v2]setsar=1[out]"]))
        self.assertEqual(argv[argv.index("-filter_complex") + 2:], [
            "-map", "[out]", "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "19", "-r", "60000/1001",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "out.mp4"])

    def test_render_overlays_empty_and_probe(self):
        argv = d.render_overlays(Path("base.mp4"), [], Path("out.mp4"), probe=1.0, dry_run=True)
        self.assertEqual(argv[5:7], ["-i", "base.mp4"])
        self.assertEqual(argv[argv.index("-filter_complex") + 1], "[0:v]setsar=1[out]")
        self.assertEqual(argv[-5:], ["-t", "1.000", "-f", "null", "-"])
        self.assertNotIn("out.mp4", argv)
        self.assertNotIn("+faststart", argv)

    def test_render_overlays_accepts_timeline_r(self):
        a = d.render_overlays(Path("b.mp4"), [{"source": "x.mp4", "in": 1.0, "r": 20.0, "d": 3.0}], Path("o.mp4"),
                              dry_run=True)
        b = d.render_overlays(Path("b.mp4"), [{"source": "x.mp4", "in": 1.0, "t": 20.0, "d": 3.0}], Path("o.mp4"),
                              dry_run=True)
        self.assertEqual(a, b)

    def test_broll_render_argv_unchanged(self):
        """broll() (reuse_plan, no Spark) still sends ffmpeg exactly the pre-refactor inputs and graph."""
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "pieces.json").write_text(json.dumps([{"clip": "A.MP4", "source": "cam/A.MP4", "src_in": 0.0,
                                                           "src_out": 40.0, "topic": "kitchen"}]))
            (work / "clip_map.json").write_text(json.dumps([{"clip": "A.MP4", "duration": 60.0}]))
            (work / "broll_map.json").write_text(json.dumps([
                {"tag": "D1", "clip": "D1.MP4", "source": "drone/D1.MP4", "duration": 30.0},
                {"tag": "R2", "clip": "R2.MOV", "source": "house/R2.MOV", "duration": 20.0}]))
            (work / "broll_plan.json").write_text(json.dumps({"raw": {
                "overlays": [{"tag": "R2", "broll_start": 1.0, "broll_end": 4.0, "timeline_start": 15.25, "reason": "island"},
                             {"tag": "D1", "broll_start": 2.0, "broll_end": 6.5, "timeline_start": 8.0, "reason": "lake"}],
                "shots": [{"tag": "D1", "room": "drone", "description": "lake"},
                          {"tag": "R2", "room": "kitchen", "description": "island"}],
                "missing_shots": []}}))
            (work / "cues.srt").write_text("")
            calls = []
            stubs = {"guestlist": types.ModuleType("guestlist"),
                     "guestlist.meta": types.SimpleNamespace(spark=None),
                     "director_spike_takes": types.SimpleNamespace(parse_srt=lambda p: [])}
            with stub_modules(stubs), mock.patch.object(d.subprocess, "run", side_effect=lambda a, **k: calls.append(a)), \
                    redirect_stdout(io.StringIO()):
                d.broll(work, "medium", insert=("20.0-24.0=D1@10",), speed=0.5, reuse_plan=True,
                        cues_file=work / "cues.srt")
            self.assertTrue((work / "cues_broll.fcpxml").exists())
        argv = calls[0]
        placed = [(8.0, 12.5, 2.0, "drone/D1.MP4"), (15.25, 18.25, 1.0, "house/R2.MOV"), (20.0, 24.0, 10.0, "drone/D1.MP4")]
        inputs, chain, last = ["-i", str(work / "render/joined.mp4")], [], "0:v"
        for k, (s, e, b0, src) in enumerate(placed, 1):
            inputs += ["-ss", f"{b0:.3f}", "-t", f"{(e - s) * 0.5:.3f}", "-i", src]
            chain += old_overlay(k, s, e, 0.5, last)
            last = f"v{k}"
        self.assertEqual(argv, ["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(chain), "-map", "[v3]",
                                "-map", "0:a", "-c:v", "libx264", "-preset", "fast", "-crf", "19", "-r", "60000/1001",
                                "-af", "loudnorm=I=-14:TP=-1:LRA=11", "-ar", "48000", "-c:a", "aac", "-b:a", "192k",
                                "-movflags", "+faststart", str(work / "cues_broll.mp4")])


class HygieneTest(unittest.TestCase):
    def test_import_pulls_no_paid_clients(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import director_test_run; "
                "print(sorted(m for m in sys.modules if m.split('.')[0] in ('guestlist', 'openai', 'director_spike_takes')))")
        out = subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.strip(), "[]")

    def test_no_absolute_user_paths(self):
        src = (SCRIPTS / "director_test_run.py").read_text()
        for root in ("Volumes", "Users"):  # spelled apart so this file stays clean for the same grep
            self.assertNotIn(f"/{root}/", src)

    def test_mix_refuses_voice_with_isolate(self):
        with mock.patch.object(d.subprocess, "run", side_effect=AssertionError("ran")):
            with self.assertRaises(ValueError):
                d.mix(Path("w"), Path("v.mp4"), Path("m.wav"), Path("o.mp4"), isolate=(0.0, 1.0), voice=Path("voice.wav"))

    def test_cli_voice(self):
        argv = ["director_test_run.py", "mix", "w", "--video", "p.mp4", "--music", "m.wav", "--out", "o.mp4",
                "--intro-len", "12.8128", "--voice", "voice.wav", "--sfx", "p.sfx.wav"]
        with mock.patch.object(d, "mix") as m, mock.patch.object(sys, "argv", argv):
            d.main()
        m.assert_called_once_with(Path("w"), Path("p.mp4"), Path("m.wav"), Path("o.mp4"), intro=12.8128, isolate=None,
                                  sfx=Path("p.sfx.wav"), voice=Path("voice.wav"))
        argv = argv[:-4]  # a single-file call as before: no voice
        with mock.patch.object(d, "mix") as m, mock.patch.object(sys, "argv", argv):
            d.main()
        self.assertIsNone(m.call_args.kwargs["voice"])


@unittest.skipUnless(HAVE_FF and HAVE_NP, "needs ffmpeg + numpy")
class RenderOverlaysMediaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = cls.dir = Path(cls.tmp.name)
        # Base like render/joined.mp4: 120 frames whose video starts at 0.021 s, audio at 0.
        ff("-f", "lavfi", "-i", "color=c=0x101010:s=320x180:r=60000/1001", "-frames:v", "120", "-c:v", "libx264",
           "-pix_fmt", "yuv420p", str(t / "v.mp4"))
        ff("-f", "lavfi", "-i", "sine=f=440:r=48000", "-t", "2.05", "-ac", "2", "-c:a", "aac", str(t / "a.m4a"))
        ff("-itsoffset", "0.021", "-i", str(t / "v.mp4"), "-i", str(t / "a.m4a"), "-map", "0:v", "-map", "1:a",
           "-c", "copy", str(t / "base.mp4"))
        # B-roll source: red for 1 s, then blue (to read the 0.5x in-point mapping off the picture).
        ff("-f", "lavfi", "-i", "color=c=red:s=320x180:r=60000/1001:d=1", "-f", "lavfi",
           "-i", "color=c=blue:s=320x180:r=60000/1001:d=2", "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]",
           "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(t / "src.mp4"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_matches_broll_picture(self):
        t, base = self.dir, self.dir / "base.mp4"
        ov = [{"source": str(t / "src.mp4"), "in": 0.8, "t": 0.5, "d": 1.0}]
        out = d.render_overlays(base, ov, t / "section.mp4")
        self.assertEqual(out, t / "section.mp4")
        # What broll() renders for the same placement (its graph, audio mapped), as the frame-exact reference.
        ff("-i", str(base), "-ss", "0.800", "-t", "0.500", "-i", str(t / "src.mp4"), "-filter_complex",
           ";".join(old_overlay(1, 0.5, 1.5, 0.5, "0:v")), "-map", "[v1]", "-map", "0:a", "-c:v", "libx264",
           "-preset", "fast", "-crf", "19", "-r", "60000/1001", "-af", "loudnorm=I=-14:TP=-1:LRA=11", "-ar", "48000",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(t / "legacy.mp4"))
        v, legacy = vstream(out), vstream(t / "legacy.mp4")
        self.assertEqual(int(v["nb_read_packets"]), int(legacy["nb_read_packets"]))
        self.assertEqual(framemd5(out), framemd5(t / "legacy.mp4"))
        self.assertEqual([s["codec_type"] for s in streams(out)], ["video"])
        self.assertEqual((v["codec_name"], v["pix_fmt"], v["r_frame_rate"], v["sample_aspect_ratio"]),
                         ("h264", "yuv420p", "60000/1001", "1:1"))
        frames = luma(out).mean(axis=(1, 2))
        at = lambda s: frames[round(s / RATE)]  # noqa: E731
        red, blue, dark = luma(t / "src.mp4")[0].mean(), luma(t / "src.mp4")[-1].mean(), at(0.25)
        self.assertLess(abs(at(0.6) - red), 3)  # on screen 0.1 s in -> source 0.85 s (red)
        self.assertLess(abs(at(1.3) - blue), 3)  # 0.8 s in -> source 1.2 s (blue)
        self.assertLess(abs(at(1.7) - dark), 3)
        self.assertGreater(abs(red - dark), 20)

    def test_empty_list_is_plain_talk(self):
        out = d.render_overlays(self.dir / "base.mp4", [], self.dir / "plain.mp4")
        ref = self.dir / "plain_ref.mp4"
        ff("-i", str(self.dir / "base.mp4"), "-map", "0:v", "-c:v", "libx264", "-r", "60000/1001", str(ref))
        self.assertEqual(int(vstream(out)["nb_read_packets"]), int(vstream(ref)["nb_read_packets"]))
        self.assertEqual(vstream(out)["sample_aspect_ratio"], "1:1")

    def test_probe_writes_nothing(self):
        ov = [{"source": str(self.dir / "src.mp4"), "in": 0.0, "t": 0.5, "d": 1.0}]
        out = d.render_overlays(self.dir / "base.mp4", ov, self.dir / "probe.mp4", probe=0.5)
        self.assertEqual(out, self.dir / "probe.mp4")
        self.assertFalse(out.exists())
        with self.assertRaises(subprocess.CalledProcessError):  # a missing source fails the smoke run
            d.render_overlays(self.dir / "base.mp4", [{**ov[0], "source": str(self.dir / "nope.mp4")}],
                              self.dir / "probe.mp4", probe=0.5)


@unittest.skipUnless(HAVE_FF and HAVE_NP and FONT, "needs ffmpeg, numpy, Pillow and the Avenir Next font")
class FactsTest(unittest.TestCase):
    """Three sections of 37 + 41 + 23 frames with mismatched SARs (none, 1:1, 8001:8000), the first with audio."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = cls.dir = Path(cls.tmp.name)
        cls.parts = []
        for i, (n, color, sar) in enumerate(((37, "0x303030", "0"), (41, "0x606060", "1"), (23, "0x909090", "8001/8000"))):
            p = t / f"s{i}.mp4"
            audio = ["-f", "lavfi", "-i", "sine=f=330:r=48000", "-map", "0:v", "-map", "1:a", "-c:a", "aac",
                     "-shortest"] if i == 0 else []
            ff("-f", "lavfi", "-i", f"color=c={color}:s=1920x1080:r=60000/1001", *audio, "-frames:v", str(n),
               "-vf", f"setsar=sar={sar}:max=10000", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
               str(p))
            cls.parts.append(p)
        assert [vstream(p).get("sample_aspect_ratio", "N/A") for p in cls.parts] == ["N/A", "1:1", "8001:8000"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def spec(self, lines: list, caption: str = "HOME") -> Path:
        f = self.dir / "spec.json"
        f.write_text(json.dumps({"accent": [40, 120, 200],
                                 "cards": [{"until": 1.6, "caption": caption, "size": 60, "lines": lines}]}))
        return f

    def test_concat_list_frame_exact(self):
        spec = self.spec([["LOT PREMIUMS  $0 – $250,000", 0.8]])
        work = self.dir / "w1"
        work.mkdir()
        with redirect_stdout(io.StringIO()):
            sfx = d.facts(work, self.parts, spec, work / "picture.mp4")
        out = work / "picture.mp4"
        self.assertEqual(sfx, work / "picture.sfx.wav")
        self.assertEqual([s["codec_type"] for s in streams(out)], ["video"])
        v = vstream(out)
        self.assertEqual(int(v["nb_read_packets"]), 101)
        self.assertEqual((v["sample_aspect_ratio"], v["r_frame_rate"], float(v["start_time"])), ("1:1", "60000/1001", 0.0))
        g = luma(out)
        flat = g[:, :6, -10:].mean(axis=(1, 2))  # top right: never under a card
        self.assertTrue(all(abs(flat[i] - flat[0]) < 2 for i in range(37)))
        self.assertGreater(flat[37] - flat[36], 20)  # each join lands on its exact frame
        self.assertGreater(flat[78] - flat[77], 20)
        self.assertTrue(all(abs(flat[i] - flat[78]) < 2 for i in range(78, 101)))
        box = g[:, 13:17, 1:8].mean(axis=(1, 2))  # bottom left, where the card line sits
        self.assertLess(abs(box[30] - flat[30]), 2)  # before the card
        self.assertGreater(abs(box[70] - flat[70]), 6)  # card shown at 1.17 s
        a = next(s for s in streams(sfx) if s["codec_type"] == "audio")
        total = sum(float(vstream(p)["duration"]) for p in self.parts)
        self.assertEqual((int(a["sample_rate"]), a["channels"]), (48000, 2))
        self.assertAlmostEqual(float(a["duration"]), int(total * 48000) / 48000, places=4)

    def test_mismatched_sar_needs_the_setsar(self):
        """The house render's 8001:8000 SAR is why each member is normalised before the concat."""
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", *[x for p in self.parts for x in ("-i", str(p))],
                            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[b]", "-map", "[b]", "-f", "null", "-"],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("SAR 8001:8000", r.stderr)

    def test_probe_returns_before_sfx(self):
        spec = self.spec([["3 BEDROOMS", 0.8]])
        work = self.dir / "w2"
        work.mkdir()
        out = d.facts(work, self.parts, spec, work / "picture.mp4", probe=0.5)
        self.assertEqual(out, work / "picture.mp4")
        self.assertFalse(out.exists())
        self.assertFalse((work / "picture.sfx.wav").exists())
        self.assertTrue((work / "facts" / "c0l0.png").exists())

    def test_single_path_keeps_audio(self):
        spec = self.spec([["3 BEDROOMS", 0.2]])
        work = self.dir / "w3"
        work.mkdir()
        with redirect_stdout(io.StringIO()):
            sfx = d.facts(work, self.parts[0], spec, work / "facts.mp4")
        kinds = {s["codec_type"]: s for s in streams(work / "facts.mp4")}
        self.assertEqual(kinds["audio"]["codec_name"], "aac")
        fmt = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    str(self.parts[0])], capture_output=True, text=True).stdout)
        a = next(s for s in streams(sfx) if s["codec_type"] == "audio")
        self.assertAlmostEqual(float(a["duration"]), int(fmt * 48000) / 48000, places=4)

    def test_guards_raise_before_encoding(self):
        bad = [[["X" * 120, 0.5]],  # far wider than 1784 px at size 60
               [["LOT PREMIUMS  /private/tmp/claude/v16.sh – 50,000", 0.5]],
               [["SEE Volumes/MEDIA", 0.5]],
               [["RUN BUILD.sh", 0.5]],
               [["NOTES.py", 0.5]]]
        for lines in bad:
            with self.subTest(lines=lines), mock.patch.object(d.subprocess, "run", side_effect=AssertionError("encoded")):
                with self.assertRaises(ValueError):
                    d.facts(self.dir, self.parts, self.spec(lines), self.dir / "x.mp4")
        with mock.patch.object(d.subprocess, "run", side_effect=AssertionError("encoded")):
            with self.assertRaises(ValueError):  # captions go through the same guard
                d.facts(self.dir, self.parts, self.spec([["OK", 0.5]], caption="tmp/x"), self.dir / "x.mp4")

    def test_widest_allowed_line_passes(self):
        from PIL import ImageFont
        font = ImageFont.truetype("/System/Library/Fonts/Avenir Next.ttc", 60, index=2)
        text = "W"
        while font.getbbox(text + "W")[2] + 22 + 48 <= d.CARD_MAX_W:
            text += "W"
        work = self.dir / "w4"
        work.mkdir()
        d.facts(work, self.parts, self.spec([[text, 0.5]]), work / "p.mp4", probe=0.2)
        from PIL import Image
        self.assertLessEqual(Image.open(work / "facts" / "c0l0.png").width, d.CARD_MAX_W)
        with self.assertRaises(ValueError):
            d.facts(work, self.parts, self.spec([[text + "W", 0.5]]), work / "p.mp4", probe=0.2)


@unittest.skipUnless(HAVE_FF, "needs ffmpeg")
class MixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = cls.dir = Path(cls.tmp.name)
        ff("-f", "lavfi", "-i", "color=c=gray:s=320x180:r=60000/1001", "-f", "lavfi", "-i", "sine=f=200:r=48000",
           "-frames:v", "360", "-af", "volume=0.02", "-ac", "2", "-c:v", "libx264", "-c:a", "aac", "-shortest",
           str(t / "video.mp4"))
        ff("-f", "lavfi", "-i", "color=c=gray:s=320x180:r=60000/1001", "-frames:v", "360", "-c:v", "libx264",
           str(t / "picture.mp4"))
        ff("-f", "lavfi", "-i", "sine=f=440:r=48000", "-t", "6.006", "-af", "volume=0.3", "-ac", "2", "-c:a", "pcm_s16le",
           str(t / "voice.wav"))
        ff("-f", "lavfi", "-i", "sine=f=220:r=48000", "-t", "8", "-ac", "2", "-c:a", "pcm_s16le", str(t / "long.wav"))
        ff("-f", "lavfi", "-i", "sine=f=220:r=48000", "-t", "4", "-ac", "2", "-c:a", "pcm_s16le", str(t / "short.wav"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_mix(self, name: str, **kw) -> tuple[dict, list[list[str]]]:
        work = self.dir / name
        work.mkdir()
        calls, real = [], subprocess.run

        def spy(argv, **k):
            calls.append(list(argv))
            return real(argv, **k)

        with mock.patch.object(d.subprocess, "run", side_effect=spy), redirect_stdout(io.StringIO()) as log:
            res = d.mix(work, out=work / "master.mp4", **kw)
        self.assertEqual(json.loads(log.getvalue().strip().splitlines()[-1]), res)
        return res, calls

    def graphs(self, calls: list[list[str]]) -> list[str]:
        return [c[c.index("-filter_complex") + 1] for c in calls if "-filter_complex" in c]

    def test_voice_wav_and_long_bed(self):
        t = self.dir
        res, calls = self.run_mix("v", video=t / "picture.mp4", music=t / "long.wav", voice=t / "voice.wav",
                                  intro=1.0, sfx=t / "voice.wav")
        self.assertEqual(res["copies"], 1)
        bed_graph, mix_graph = self.graphs(calls)
        self.assertNotIn(";;", bed_graph)
        self.assertEqual(bed_graph, "[0:a]anull[x0];[x0]atrim=0:6.006,afade=t=in:d=1.5,afade=t=out:st=2.006:d=4[out]")
        final = next(c for c in calls if "-filter_complex" in c and mix_graph in c)
        self.assertEqual(final[final.index("-i") + 1], str(t / "voice.wav"))  # the voice WAV is input 0
        self.assertAlmostEqual(res["music_gain_db"], d._lufs(t / "voice.wav") - 20 - d._lufs(t / "v" / "music_bed.wav"),
                               delta=0.006)
        master = t / "v" / "master.mp4"
        kinds = {s["codec_type"]: s for s in streams(master)}
        self.assertEqual(int(kinds["video"]["nb_read_packets"]), 360)
        # loudnorm's flush used to leave a 94 ms timestamp gap here (audio 6.100 s against 6.006 s of picture).
        self.assertIn("loudnorm=I=-14:TP=-1.5:LRA=11,asetpts=N/SR/TB[a]", mix_graph)
        self.assertLess(abs(float(kinds["audio"]["duration"]) - float(kinds["video"]["duration"])), 0.005)
        self.assertIsInstance(res["lufs"], float)
        self.assertIsInstance(res["tp"], float)
        self.assertAlmostEqual(res["lufs"], d._lufs(master), delta=0.05)

    def test_short_bed_loops_as_before_and_levels_on_video(self):
        t = self.dir
        res, calls = self.run_mix("s", video=t / "video.mp4", music=t / "short.wav")
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    str(t / "video.mp4")], capture_output=True, text=True).stdout)
        copies = int(dur // (4.0 - 2.0)) + 2
        self.assertEqual(res["copies"], copies)
        old = "[0:a]anull[x0];" + ";".join(f"[x{i - 1}][{i}:a]acrossfade=d=2[x{i}]" for i in range(1, copies)) + \
              f";[x{copies - 1}]atrim=0:{dur:.3f},afade=t=in:d=1.5,afade=t=out:st={dur - 4:.3f}:d=4[out]"
        bed_graph, _ = self.graphs(calls)
        self.assertEqual(bed_graph, old)
        final = calls[-2]  # the mix itself; the last call measures the master
        self.assertEqual(final[final.index("-i") + 1], str(t / "video.mp4"))
        self.assertAlmostEqual(res["music_gain_db"], d._lufs(t / "video.mp4") - 20 - d._lufs(t / "s" / "music_bed.wav"),
                               delta=0.006)


@unittest.skipUnless(HAVE_FF and HAVE_NP, "needs ffmpeg + numpy")
class PhoneCopyTest(unittest.TestCase):
    def test_two_pass_fps_filter_timing(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            # Bright from master frame 120 (an even frame) to 180, dark again from 181 (an odd frame).
            ff("-f", "lavfi", "-i", "color=c=black:s=640x360:r=60000/1001", "-f", "lavfi", "-i", "sine=f=440:r=48000",
               "-frames:v", "240", "-vf", "geq=lum='if(between(N,120,180),235,16)':cb=128:cr=128", "-ac", "2",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(t / "master.mp4"))
            out = d.phone_copy(t / "master.mp4", t / "phone.mp4", t / "phone2p")
            self.assertEqual(out, t / "phone.mp4")
            self.assertTrue((t / "phone2p-0.log").exists())
            kinds = {s["codec_type"]: s for s in streams(out)}
            v = kinds["video"]
            self.assertEqual((v["width"], v["height"], v["r_frame_rate"]), (960, 540, "30000/1001"))
            self.assertEqual(kinds["audio"]["codec_name"], "aac")
            self.assertIn(int(v["nb_read_packets"]), (120, 121))
            bright = luma(out).mean(axis=(1, 2)) > 128
            on = int(np.argmax(bright))
            off = on + int(np.argmin(bright[on:]))
            for master_frame, phone_frame in ((120, on), (181, off)):
                lag = phone_frame * 2 * RATE - master_frame * RATE
                self.assertGreaterEqual(lag, -1e-6)
                self.assertLessEqual(lag, 0.034)

    def test_phone_audio_goes_through_the_limiter(self):
        """AAC at 72k overshot r002's -1.1 dBTP master to +1.1 dBTP; the -4 dBFS limiter measured -0.7 on that master
        (sync 0 ms). Pass 2 must carry it; pass 1 has no audio."""
        calls = []
        with mock.patch.object(d.subprocess, "run", side_effect=lambda argv, **kw: calls.append(argv)):
            d.phone_copy(Path("m.mp4"), Path("p.mp4"), Path("p2p"))
        self.assertEqual(d.PHONE_LIMIT, "alimiter=limit=0.631:level=0:latency=1")
        self.assertNotIn("-af", calls[0])
        i = calls[1].index("-af")
        self.assertEqual(calls[1][i + 1], d.PHONE_LIMIT)
        self.assertLess(i, calls[1].index("-c:a"))

if __name__ == "__main__":
    unittest.main()
