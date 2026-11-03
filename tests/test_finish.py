"""Items 15-38 of docs/muse-code/FINISH-DESIGN.md: finishing tests.

Offline throughout: fake Keychain/env keys, fake HTTP transports at
the provider seam, a fake guestlist for the Spark design call, and
short synthetic lavfi cuts (behind skipUnless ffmpeg) for the mix and
master. No network, no Meta API, no real keys.
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
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import engine as engine_mod  # noqa: E402
from director import cli as cli_mod  # noqa: E402
from director import connect as connect_mod  # noqa: E402
from director import cut as cut_mod  # noqa: E402
from director import elevenlabs as elevenlabs_mod  # noqa: E402
from director import finish as finish_mod  # noqa: E402
from director import export as export_mod  # noqa: E402
from director.angles import TimedWord  # noqa: E402

FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
ELEVEN_KEY = "test-key-eleven-finish1"


def clean_env(test: unittest.TestCase, **extra: str):
    patcher = mock.patch.dict(os.environ, {}, clear=False)
    patcher.start()
    test.addCleanup(patcher.stop)
    for var in ("ELEVENLABS_API_KEY", "HF_API_KEY_ID", "HF_API_KEY_SECRET"):
        os.environ.pop(var, None)
    os.environ.update(extra)


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
    module._store = data
    return module


class FakeTransport:
    """Canned replies (popped in order) with a full call record."""

    def __init__(self, replies: list):
        self.replies = list(replies)
        self.calls: list[tuple] = []

    def __call__(self, method, url, *, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        if len(self.replies) > 1:
            return self.replies.pop(0)
        return self.replies[0]


class RouteTransport:
    """Reply bytes chosen by URL substring, every call recorded."""

    def __init__(self, routes: list[tuple[str, list]]):
        self.routes = {key: list(replies) for key, replies in routes}
        self.calls: list[tuple] = []

    def __call__(self, method, url, *, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        for key, replies in self.routes.items():
            if key in url:
                if len(replies) > 1:
                    return replies.pop(0)
                return replies[0]
        raise AssertionError(f"unstubbed provider URL: {url}")


class FakeGuestlist:
    """A fake guestlist.meta: canned design reply, recorded calls."""

    def __init__(self, data=None, usd: float = 0.0210, hit=None, fail=None):
        self.data = data if data is not None else {"music": None, "effects": []}
        self.usd = usd
        self.hit = hit
        self.fail = fail
        self.calls: list[dict] = []
        self.meta = types.ModuleType("guestlist.meta")
        self.meta.cache = SimpleNamespace(
            key=lambda *parts: "fake-key",
            get=lambda _kind, _key: self.hit,
            guard=lambda estimate=0.0: None)
        self.meta.spark = SimpleNamespace(_structured=self._structured)

    def _structured(self, key, messages, **kwargs):
        self.calls.append({"key": key, "messages": messages, **kwargs})
        if self.fail is not None:
            raise self.fail
        return SimpleNamespace(data=self.data, usd=self.usd)

    def install(self, test: unittest.TestCase):
        patcher = mock.patch.dict(sys.modules, {"guestlist.meta": self.meta})
        patcher.start()
        test.addCleanup(patcher.stop)
        return self


def _run(cmd: list[str]) -> None:
    done = subprocess.run(cmd, capture_output=True, text=True)
    assert done.returncode == 0, f"{cmd}\n{done.stderr}"


def _make_cut(path: Path, duration: float = 6.0, freq: float = 440.0,
              volume: float = 0.05) -> Path:
    """testsrc2 picture + a quiet tone voice."""
    _run(["ffmpeg", "-v", "error", "-y",
          "-f", "lavfi", "-i",
          f"testsrc2=size=640x360:rate=30:duration={duration}",
          "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}",
          "-af", f"volume={volume}",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
          "-shortest", str(path)])
    return path


def _make_gap_voice_cut(path: Path, duration: float = 6.0,
                        voice_end: float = 3.0) -> Path:
    """Tone voice for the first half, silence after (ducking fixture)."""
    _run(["ffmpeg", "-v", "error", "-y",
          "-f", "lavfi", "-i",
          f"testsrc2=size=640x360:rate=30:duration={duration}",
          "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
          "-af", f"volume=0.05,volume=0:enable='gte(t,{voice_end})'",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
          "-shortest", str(path)])
    return path


def _make_mp3(path: Path, duration: float, freq: float = 220.0,
              volume: float = 0.5) -> Path:
    """A provider-asset stand-in: a stereo tone MP3 of exact-ish duration.

    Stereo like real ElevenLabs MP3s (dual-mono at unity: FFmpeg's
    implicit mono→stereo conversion splits power at −3 dB per channel,
    so mono fixtures would misstate stem levels).
    """
    _run(["ffmpeg", "-v", "error", "-y",
          "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}",
          "-af", f"volume={volume},pan=stereo|c0=c0|c1=c0",
          "-c:a", "libmp3lame", str(path)])
    return path


def _make_silent_cut(path: Path, duration: float = 3.0) -> Path:
    _run(["ffmpeg", "-v", "error", "-y",
          "-f", "lavfi", "-i",
          f"testsrc2=size=640x360:rate=30:duration={duration}",
          "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={duration}",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
          "-shortest", str(path)])
    return path


def _make_video_only(path: Path, duration: float = 2.0) -> Path:
    _run(["ffmpeg", "-v", "error", "-y",
          "-f", "lavfi", "-i",
          f"testsrc2=size=640x360:rate=30:duration={duration}",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(path)])
    return path


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _angles(shots: list[dict] | None = None, audio_label: str = "A") -> dict:
    if shots is None:
        shots = [{"id": 0, "start_s": 0.0, "end_s": 6.0, "camera": "A",
                  "clip_path": "/media/A.mp4", "clip_in_s": 0.0,
                  "clip_out_s": 6.0, "line_ids": [0], "shot_size": "medium",
                  "reason": "test", "method": "rule", "overrules": []}]
    return {"version": 1, "audio": {"label": audio_label,
                                   "clip_path": f"/media/{audio_label}.mp4",
                                   "offset_s": 0.0},
            "method": "rule", "spend_usd": 0.0, "warnings": [],
            "lines": [], "shots": shots}


def _words_file(path: Path, words: list[tuple[str, float, float]]) -> Path:
    return _write_json(path, {"words": [
        {"word": text, "start": start, "end": end}
        for text, start, end in words]})


def _taste_file(path: Path, entries: list[str]) -> Path:
    return _write_json(path, {"version": 1, "entries": [
        {"id": f"t{i}", "text": text, "source": "test",
         "at": "2026-11-03T00:00:00"}
        for i, text in enumerate(entries)]})


def _loudness(path: Path) -> tuple[float | None, float | None]:
    from ghostcut.ffmpeg_tools import run_command

    proc = run_command(
        finish_mod.loudnorm_measure_command(path, -14.0), check=False)
    parsed = finish_mod.parse_loudnorm_full(proc.stderr or "")
    if parsed is None:
        return None, None
    return parsed["input_i"], parsed["input_tp"]


def _mean_volume(path: Path, start: float, duration: float) -> float:
    """volumedetect mean dB over a window; silence reads as -inf."""
    import re as _re

    from ghostcut.ffmpeg_tools import run_command

    proc = run_command(
        ["ffmpeg", "-v", "info", "-ss", f"{start:.3f}", "-t",
         f"{duration:.3f}", "-i", str(path), "-af", "volumedetect",
         "-f", "null", "-"],
        check=False)
    match = _re.search(r"mean_volume:\s*([-\d.]+|n/a|-inf)\s*dB",
                       proc.stderr or "")
    if not match:
        return float("nan")
    try:
        return float(match.group(1))
    except ValueError:
        return float("-inf")


class Ctx:
    """The engine with a throwaway demo dir, through the real dispatch."""

    def __init__(self, test: unittest.TestCase):
        self._tmp = tempfile.TemporaryDirectory(prefix="finish-tests-")
        test.addCleanup(self._tmp.cleanup)
        self.demo = Path(self._tmp.name) / "demo"
        self.demo.mkdir()
        self.engine = engine_mod.Engine(self.demo, render=None)
        self.handler = engine_mod.Handler.__new__(engine_mod.Handler)
        self.handler.engine = self.engine
        self.handler.server = SimpleNamespace(
            server_address=("127.0.0.1", 7791))
        self.answered: list[tuple[int, dict]] = []
        self.handler._json = (
            lambda obj, code=200: self.answered.append((code, obj)))

    def _headers(self, size: int = 0):
        return {"Content-Length": str(size), "Host": "127.0.0.1:7791",
                "Content-Type": "application/json"}

    def post(self, path: str, body: dict):
        import io as _io

        data = json.dumps(body).encode()
        self.handler.path = path
        self.handler.headers = self._headers(len(data))
        self.handler.rfile = _io.BytesIO(data)
        self.handler.do_POST()
        return self.answered[-1]

    def get(self, path: str):
        self.handler.path = path
        self.handler.headers = self._headers()
        self.handler.rfile = io.BytesIO(b"")
        self.handler.do_GET()
        return self.answered[-1]


class CommandTests(unittest.TestCase):
    """Item 15: command builders, element-for-element."""

    def test_extract_clean_measure_apply(self) -> None:
        video, wav = Path("/v/cut.mp4"), Path("/o/voice_raw.wav")
        self.assertEqual(
            finish_mod.extract_voice_command(video, wav),
            ["ffmpeg", "-v", "error", "-y", "-i", "/v/cut.mp4",
             "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "48000",
             "-c:a", "pcm_s16le", "/o/voice_raw.wav"])
        self.assertEqual(finish_mod.isolation_input_command(video, wav),
                         finish_mod.extract_voice_command(video, wav))
        local = finish_mod.clean_voice_command(wav, Path("/o/clean.wav"),
                                               isolated=False)
        chain = local[local.index("-af") + 1]
        self.assertIn("afftdn", chain)
        self.assertIn("highpass", chain)
        self.assertIn("acompressor", chain)
        isolated = finish_mod.clean_voice_command(wav, Path("/o/clean.wav"),
                                                  isolated=True)
        chain = isolated[isolated.index("-af") + 1]
        self.assertNotIn("afftdn", chain)
        self.assertIn("highpass", chain)
        self.assertIn("acompressor", chain)
        self.assertEqual(
            finish_mod.loudnorm_measure_command(wav, -14.0),
            ["ffmpeg", "-nostats", "-nostdin", "-i", "/o/voice_raw.wav",
             "-vn", "-af",
             "loudnorm=I=-14.0:TP=-1.0:LRA=11:print_format=json",
             "-f", "null", "-"])
        self.assertEqual(
            finish_mod.loudnorm_apply_command(
                Path("/o/a.wav"), Path("/o/b.wav"), target_lufs=-16.0,
                measured={"input_i": -31.2, "input_tp": -18.4,
                          "input_lra": 4.4, "input_thresh": -41.2,
                          "offset": 2.0}),
            ["ffmpeg", "-v", "error", "-y", "-i", "/o/a.wav",
             "-af", "loudnorm=I=-16.0:TP=-1.0:LRA=11:measured_I=-31.2"
                    ":measured_TP=-18.4:measured_LRA=4.4"
                    ":measured_thresh=-41.2:offset=2.0:linear=true",
             "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", "/o/b.wav"])

    def test_music_mix_single_af(self) -> None:
        plan = finish_mod.MusicPlan(
            prompt="warm pads", start_s=2.0, end_s=8.0, gain_db=-20.0,
            dropouts=[(4.0, 5.0)], reason="r", confidence=0.8)
        cmd = finish_mod.music_mix_command(Path("/o/bed.mp3"), 12.0, plan,
                                           Path("/o/music_mix.wav"))
        self.assertEqual(cmd.count("-af"), 1)
        chain = cmd[cmd.index("-af") + 1]
        self.assertIn("aloop=loop=-1:size=2147483647", chain)
        self.assertIn("atrim=0:6.000", chain)
        self.assertIn("volume=enable='between(t,2.000,3.000)':volume=0", chain)
        self.assertIn("afade=t=in:st=0:d=1", chain)
        self.assertIn("afade=t=out:st=5.000:d=1", chain)
        self.assertIn("adelay=2000|2000", chain)
        self.assertIn("apad,atrim=0:12.000", chain)

    def test_sfx_mix(self) -> None:
        cues = [
            (finish_mod.SoundCue("sfx_001", "riser", 1.2, 1.5, "p", -14.0,
                                 "r", 0.7), Path("/o/sfx_001.mp3")),
            (finish_mod.SoundCue("sfx_002", "whoosh", 4.0, 1.0, "q", -16.0,
                                 "w", 0.6), Path("/o/sfx_002.mp3")),
        ]
        cmd = finish_mod.sfx_mix_command(cues, 6.0, Path("/o/sfx_mix.wav"))
        self.assertEqual(cmd[:4], ["ffmpeg", "-v", "error", "-y"])
        self.assertEqual(cmd.count("-i"), 2)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("adelay=1200|1200", graph)
        self.assertIn("adelay=4000|4000", graph)
        self.assertIn("volume=-14.0dB", graph)
        self.assertIn("atrim=0:1.500", graph)
        self.assertIn("amix=inputs=2:duration=longest:normalize=0", graph)
        self.assertIn("apad,atrim=0:6.000[out]", graph)

    def test_duck_mix_four_branches(self) -> None:
        voice, bed, sfx, out = (Path("/o/voice.wav"), Path("/o/bed.wav"),
                                 Path("/o/sfx.wav"), Path("/o/mix.wav"))
        self.assertEqual(
            finish_mod.duck_mix_command(voice, None, None, out),
            ["ffmpeg", "-v", "error", "-y", "-i", "/o/voice.wav",
             "-af", "aresample=48000,pan=stereo|c0=c0|c1=c0",
             "-c:a", "pcm_s16le", "/o/mix.wav"])
        both = finish_mod.duck_mix_command(voice, bed, sfx, out)
        graph = both[both.index("-filter_complex") + 1]
        self.assertIn("[0:a]asplit=2[vm][key]", graph)
        self.assertIn("[vm]pan=stereo|c0=c0|c1=c0[v]", graph)
        self.assertIn("[1:a]volume=-20.0dB[m]", graph)
        self.assertIn("[m][2:a]amix=inputs=2:duration=longest:normalize=0[bed]",
                      graph)
        self.assertIn("sidechaincompress=threshold=0.1:ratio=4:attack=5"
                      ":release=400", graph)
        self.assertIn("[v][ducked]amix=inputs=2:duration=first:normalize=0[out]",
                      graph)
        low = finish_mod.duck_mix_command(voice, bed, None, out,
                                          music_gain_db=-26.0)
        low_graph = low[low.index("-filter_complex") + 1]
        self.assertIn("[1:a]volume=-26.0dB[bed]", low_graph)
        self.assertNotIn("[2:a]", low_graph)
        fx = finish_mod.duck_mix_command(voice, None, sfx, out)
        fx_graph = fx[fx.index("-filter_complex") + 1]
        self.assertIn("[1:a][key]sidechaincompress=", fx_graph)
        self.assertNotIn("volume=", fx_graph)

    def test_mux(self) -> None:
        self.assertEqual(
            finish_mod.mux_command(Path("/v/cut.mp4"), Path("/o/master.wav"),
                                   Path("/o/out.mp4")),
            ["ffmpeg", "-v", "error", "-y", "-i", "/v/cut.mp4",
             "-i", "/o/master.wav", "-map", "0:v:0", "-map", "1:a:0",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-movflags", "+faststart", "/o/out.mp4"])


class LoudnormParseTests(unittest.TestCase):
    """Item 16: parse_loudnorm_full."""

    def test_full_block_truncated_garbage(self) -> None:
        from tests.test_cut import LOUDNORM_PASS_STDERR

        parsed = finish_mod.parse_loudnorm_full(LOUDNORM_PASS_STDERR)
        assert parsed is not None
        self.assertEqual(parsed, {"input_i": -16.0, "input_tp": -1.5,
                                  "input_lra": 3.2, "input_thresh": -26.0,
                                  "offset": -8.0})
        self.assertIsNone(finish_mod.parse_loudnorm_full(
            LOUDNORM_PASS_STDERR[:120]))
        self.assertIsNone(finish_mod.parse_loudnorm_full("garbage\nno json\n"))
        self.assertIsNone(finish_mod.parse_loudnorm_full(""))


class TimelineTests(unittest.TestCase):
    """Item 17: the finish timeline conversion."""

    def test_untightened_and_tightened(self) -> None:
        angles = _angles([{"id": 0, "start_s": 5.0, "end_s": 11.0,
                           "camera": "A", "clip_path": "/m/A.mp4",
                           "clip_in_s": 0.0, "clip_out_s": 6.0,
                           "line_ids": [0], "shot_size": "m",
                           "reason": "t", "method": "rule",
                           "overrules": []}])
        self.assertEqual(finish_mod.finish_origin(angles), 5.0)
        words = [TimedWord("hey", 5.0, 5.4), TimedWord("there", 7.0, 7.3)]
        self.assertEqual(finish_mod.word_intervals(angles, None, words),
                         [(0.0, 0.4, "hey"), (2.0, 2.3, "there")])
        self.assertEqual(
            finish_mod.beat_sections(angles, None, words),
            [{"id": "cut", "topic": "Cut", "start_s": 0.0, "end_s": 6.0}])
        tighten = {
            "kept_spans": [
                {"id": "K0", "start_s": 10.0, "end_s": 12.0, "beat_id": 0,
                 "attempt_id": 0, "utterance_ids": [], "kind": "keeper",
                 "reason": ""},
                {"id": "K1", "start_s": 20.0, "end_s": 22.0, "beat_id": 1,
                 "attempt_id": 0, "utterance_ids": [], "kind": "keeper",
                 "reason": ""}],
            "beats": [{"id": 0, "topic": "intro"}, {"id": 1, "topic": "demo"}],
        }
        tight_angles = _angles([{"id": 0, "start_s": 0.0, "end_s": 4.0,
                                 "camera": "A", "clip_path": "/m/A.mp4",
                                 "clip_in_s": 0.0, "clip_out_s": 4.0,
                                 "line_ids": [0], "shot_size": "m",
                                 "reason": "t", "method": "rule",
                                 "overrules": []}])
        kept = [TimedWord("kept", 10.5, 10.9), TimedWord("gone", 15.0, 15.4),
                TimedWord("also", 20.2, 20.6)]
        self.assertEqual(
            finish_mod.word_intervals(tight_angles, tighten, kept),
            [(0.5, 0.9, "kept"), (2.2, 2.6, "also")])
        self.assertEqual(
            finish_mod.beat_sections(tight_angles, tighten, kept),
            [{"id": 0, "topic": "intro", "start_s": 0.0, "end_s": 2.0},
             {"id": 1, "topic": "demo", "start_s": 2.0, "end_s": 4.0}])


class TasteTests(unittest.TestCase):
    """Item 18: the parse_taste table (music-ask rides the gate, item 22)."""

    def _parse(self, *entries: str) -> finish_mod.TasteFinish:
        return finish_mod.parse_taste(list(entries))

    def test_table(self) -> None:
        self.assertEqual(self._parse("no whooshes").forbid_kinds,
                         frozenset({"whoosh"}))
        self.assertEqual(self._parse("NO RISERS").forbid_kinds,
                         frozenset({"riser"}))
        self.assertEqual(self._parse("no impacts").forbid_kinds,
                         frozenset({"impact"}))
        self.assertEqual(self._parse("no hits").forbid_kinds,
                         frozenset({"impact"}))
        self.assertTrue(self._parse("music low").music_low)
        self.assertTrue(self._parse("quiet music").music_low)
        self.assertTrue(self._parse("music quiet").music_low)
        self.assertEqual(self._parse("master to -16").loudness, -16.0)
        self.assertEqual(self._parse("master to −16").loudness, -16.0)
        self.assertEqual(self._parse("loudness -16").loudness, -16.0)
        self.assertEqual(self._parse("master to -14").loudness, -14.0)
        self.assertTrue(self._parse("isolate the voice please").isolate)
        self.assertTrue(self._parse("clean voice with elevenlabs").isolate)
        self.assertTrue(self._parse("allow edge effects").allow_edges)
        only_music = self._parse("no music")
        self.assertTrue(only_music.no_music)
        self.assertFalse(only_music.no_sound)
        for text in ("no sound", "no effects", "no sound design"):
            both = self._parse(text)
            self.assertTrue(both.no_music)
            self.assertTrue(both.no_sound)

    def test_negatives(self) -> None:
        self.assertEqual(self._parse("this client hates jump cuts"),
                         finish_mod.TasteFinish())
        self.assertEqual(self._parse("no ducking"), finish_mod.TasteFinish())
        vague = self._parse("an edge at the first second would be nice")
        self.assertFalse(vague.allow_edges)


class VerifyTests(unittest.TestCase):
    """Item 20: the verify_design gauntlet."""

    def _cue(self, kind: str, at: float, duration: float = 1.0,
             gain: float = -10.0, conf: float = 0.7,
             prompt: str = "soft sound") -> finish_mod.SoundCue:
        return finish_mod.SoundCue("", kind, at, duration, prompt, gain,
                                   "reason", conf)

    def test_gauntlet(self) -> None:
        taste = finish_mod.TasteFinish(forbid_kinds=frozenset({"whoosh"}))
        bed = finish_mod.MusicPlan("bed", 5.0, 5.0, -20.0, [], "r", 0.9)
        cues = [
            finish_mod.SoundCue("", "riser", float("nan"), 1.0, "x", -10.0,
                                "r", 0.5),
            self._cue("whoosh", 25.0, conf=0.6, prompt="whoosh sfx"),
            self._cue("riser", 10.0, duration=0.1, gain=5.0, conf=0.9,
                       prompt="soft riser"),
            self._cue("impact", 20.05, gain=-10.0, conf=0.8,
                       prompt="soft impact"),
            self._cue("riser", 30.05, duration=2.0, conf=0.7,
                       prompt="riser two"),
            self._cue("riser", 40.0, duration=3.0, gain=-8.0, conf=0.75,
                       prompt="riser three"),
            self._cue("impact", 0.5, conf=0.5, prompt="early hit"),
            self._cue("riser", 119.5, conf=0.5, prompt="late riser"),
            self._cue("riser", 100.0), self._cue("riser", 101.0),
            self._cue("riser", 102.0), self._cue("riser", 103.0),
        ]
        words = [(20.0, 20.5, "hello"), (30.0, 35.0, "long word here"),
                 (41.0, 42.0, "cap")]
        music, placed, notes = finish_mod.verify_design(
            bed, cues, duration_s=120.0, words=words, sections=[],
            taste=taste)
        self.assertIsNone(music)
        self.assertEqual(notes, [
            "finish_music_dropped:invalid_plan",
            "finish_cue_dropped:1:invalid_cue",
            "finish_cue_dropped:whoosh:25.000s:forbidden_by_taste",
            "finish_cue_dropped:impact:0.500s:edge",
            "finish_cue_dropped:riser:119.500s:edge",
            "finish_cue_moved:impact:20.050s->20.550s:word",
            "finish_cue_dropped:riser:30.050s:word",
            "finish_cue_capped:riser:40.000s:-12.0dB:words",
            "finish_cue_dropped:riser:103.000s:density",
        ])
        self.assertEqual([cue.id for cue in placed],
                         ["sfx_001", "sfx_002", "sfx_003",
                          "sfx_004", "sfx_005", "sfx_006"])
        self.assertEqual((placed[0].duration_s, placed[0].gain_db),
                         (0.5, -6.0))
        self.assertEqual(placed[1].at_s, 20.55)
        self.assertEqual(placed[2].gain_db, -12.0)

    def test_total_cap_and_edges_and_music_low(self) -> None:
        taste = finish_mod.TasteFinish()
        cues = [self._cue("riser", 5.0 + 30.0 * k, conf=0.9 - k * 0.01)
                for k in range(13)]
        _music, placed, notes = finish_mod.verify_design(
            None, cues, duration_s=400.0, words=[], sections=[],
            taste=taste)
        self.assertEqual(len(placed), 12)
        self.assertEqual([cue.id for cue in placed],
                         [f"sfx_{i:03d}" for i in range(1, 13)])
        self.assertIn("finish_cue_dropped:riser:365.000s:total_cap", notes)
        _, rescued, edge_notes = finish_mod.verify_design(
            None, [self._cue("impact", 0.5)], duration_s=120.0, words=[],
            sections=[],
            taste=finish_mod.TasteFinish(allow_edges=True))
        self.assertEqual([cue.id for cue in rescued], ["sfx_001"])
        self.assertEqual(edge_notes, [])
        bed = finish_mod.MusicPlan("warm pads", 0.0, 60.0, -20.0,
                                   [(10.0, 12.0), (11.0, 13.0)], "r", 0.8)
        kept, _, low_notes = finish_mod.verify_design(
            bed, [], duration_s=120.0, words=[], sections=[],
            taste=finish_mod.TasteFinish(music_low=True))
        assert kept is not None
        self.assertEqual(kept.gain_db, -26.0)
        self.assertEqual(kept.dropouts, [(10.0, 13.0)])
        self.assertIn("finish_music_low:taste", low_notes)


class PromptSchemaTests(unittest.TestCase):
    """Item 21: design_prompt hygiene and the strict schema."""

    def test_prompt_and_schema(self) -> None:
        prompt = finish_mod.design_prompt(
            duration_s=6.0, voice_stats={"rms_db": -38.2, "peak_db": -12.1},
            taste_entries=["no whooshes", "music low"],
            sections=[{"id": "cut", "topic": "Cut", "start_s": 0.0,
                       "end_s": 6.0}],
            shots=[{"id": 0, "start_s": 0.0, "end_s": 6.0, "camera": "A"}],
            gaps=[(0.4, 5.6)], words=[(0.2, 0.4, 'say "hi"')],
            forbids=["whoosh"])
        for needle in ("Sections:", "cut", "Shots:", "shot s0 0.000-6.000",
                       "cam-A", "Gaps", "0.400-5.600", "Words:",
                       '0.200-0.400 "say \\"hi\\""', "no whooshes",
                       "music low", "forbidden_kinds: [whoosh]",
                       "Design the sound for this cut. JSON only."):
            self.assertIn(needle, prompt)
        self.assertNotIn("/media/", prompt)
        self.assertNotIn("contributor", prompt)
        schema = finish_mod.design_schema()
        self.assertEqual(schema["required"], ["music", "effects"])
        self.assertFalse(schema["additionalProperties"])
        effect = schema["properties"]["effects"]["items"]
        self.assertEqual(effect["properties"]["kind"]["enum"],
                         ["riser", "whoosh", "impact"])
        self.assertEqual(set(effect["required"]),
                         {"kind", "at_s", "duration_s", "prompt", "gain_db",
                          "reason", "confidence"})
        music_props = schema["properties"]["music"]["properties"]
        self.assertEqual(set(music_props),
                         {"prompt", "start_s", "end_s", "gain_db",
                          "dropouts", "reason", "confidence"})


def _job_dir(test: unittest.TestCase, duration: float = 3.0,
             words: list[tuple[str, float, float]] | None = None) -> Path:
    """A minimal finishable job: cut.mp4 + angles.json + words.json."""
    tmp = tempfile.TemporaryDirectory(prefix="finish-job-")
    test.addCleanup(tmp.cleanup)
    job = Path(tmp.name) / "job"
    job.mkdir()
    _make_cut(job / "cut.mp4", duration=duration)
    shots = [{"id": 0, "start_s": 0.0, "end_s": duration, "camera": "A",
              "clip_path": "/media/A.mp4", "clip_in_s": 0.0,
              "clip_out_s": duration, "line_ids": [0], "shot_size": "medium",
              "reason": "test", "method": "rule", "overrules": []}]
    _write_json(job / "angles.json", _angles(shots))
    if words is None:
        words = [("hello", 0.5, 0.9), ("there", 1.5, 1.9)]
    _words_file(job / "words.json", words)
    return job


@unittest.skipUnless(FFMPEG, "needs ffmpeg and ffprobe")
class LoudnessPrecedenceTests(unittest.TestCase):
    """Item 19: flag > taste > default."""

    def _target(self, job: Path, **kwargs) -> float:
        clean_env(self)
        with mock.patch.dict(sys.modules, {"keyring": make_keyring({})}):
            payload = finish_mod.run(
                job_dir=job, model="muse-spark-1.3", no_muse=True,
                sound=False, isolate=False, loudness=kwargs.get("loudness"),
                max_provider_usd=2.0, max_usd=5.0)
        return float(payload["target_lufs"])

    def test_precedence(self) -> None:
        self.assertEqual(self._target(_job_dir(self)), -14.0)
        job = _job_dir(self)
        _taste_file(job / "taste.json", ["master to -16"])
        self.assertEqual(self._target(job), -16.0)
        job = _job_dir(self)
        _taste_file(job / "taste.json", ["master to -14"])
        self.assertEqual(self._target(job, loudness=-16.0), -16.0)


@unittest.skipUnless(FFMPEG, "needs ffmpeg and ffprobe")
class GateTests(unittest.TestCase):
    """Item 22: the sound gate matrix."""

    def _run(self, job: Path, **kwargs) -> tuple[dict, mock.Mock]:
        clean_env(self, **kwargs.pop("env", {}))
        spy = mock.Mock(return_value=(None, [], 0.0, False, []))
        with (mock.patch.dict(sys.modules, {"keyring": make_keyring({})}),
              mock.patch.object(finish_mod, "design_with_muse", spy)):
            payload = finish_mod.run(
                job_dir=job, model="muse-spark-1.3",
                no_muse=kwargs.get("no_muse", False),
                sound=kwargs.get("sound", False),
                isolate=False, loudness=None, max_provider_usd=2.0,
                max_usd=5.0)
        return payload, spy

    def test_matrix(self) -> None:
        payload, spy = self._run(_job_dir(self))
        spy.assert_not_called()
        self.assertIsNone(payload["design"])
        payload, spy = self._run(_job_dir(self), sound=True)
        spy.assert_not_called()
        self.assertIn("finish_sound_off_no_provider", payload["notes"])
        job = _job_dir(self)
        _taste_file(job / "taste.json", ["no sound effects at all"])
        payload, spy = self._run(job, sound=True,
                                 env={"ELEVENLABS_API_KEY": ELEVEN_KEY})
        spy.assert_not_called()
        self.assertIn("finish_sound_forbidden_by_taste", payload["notes"])
        payload, spy = self._run(_job_dir(self), sound=True,
                                 env={"ELEVENLABS_API_KEY": ELEVEN_KEY})
        spy.assert_called_once()
        self.assertIsNotNone(payload["design"])
        # Taste asking for music enables sound without the flag.
        job = _job_dir(self)
        _taste_file(job / "taste.json", ["please add gentle music"])
        payload, spy = self._run(job, env={"ELEVENLABS_API_KEY": ELEVEN_KEY})
        spy.assert_called_once()
        self.assertIsNotNone(payload["design"])

    def test_meta_failure_degrades(self) -> None:
        clean_env(self, ELEVENLABS_API_KEY=ELEVEN_KEY)
        FakeGuestlist(fail=RuntimeError("spark is down")).install(self)
        transport = FakeTransport([])
        transport.calls = []
        strict = mock.Mock(side_effect=AssertionError("no provider call"))
        with (mock.patch.dict(sys.modules, {"keyring": make_keyring({})}),
              mock.patch.object(elevenlabs_mod, "http_request", strict)):
            payload = finish_mod.run(
                job_dir=_job_dir(self), model="muse-spark-1.3",
                no_muse=False, sound=True, isolate=False, loudness=None,
                max_provider_usd=2.0, max_usd=5.0)
        self.assertTrue(any(note.startswith("finish_design_unavailable:")
                            for note in payload["notes"]))
        # The design ran and failed: the record stands with $0, no assets.
        self.assertEqual(payload["design"]["model"], "muse-spark-1.3")
        self.assertEqual(payload["design"]["meta_usd"], 0.0)
        self.assertEqual(payload["assets"], [])
        strict.assert_not_called()


class DesignCallTests(unittest.TestCase):
    """Item 22 (continued): design_with_muse itself, no ffmpeg."""

    def _design(self, guest: FakeGuestlist, **kwargs):
        guest.install(self)
        return finish_mod.design_with_muse(
            duration_s=6.0, voice_stats={"rms_db": -30.0, "peak_db": -10.0},
            taste_entries=[], sections=[{"id": "cut", "topic": "Cut",
                                          "start_s": 0.0, "end_s": 6.0}],
            shots=[{"id": 0, "start_s": 0.0, "end_s": 6.0, "camera": "A"}],
            words=[(0.5, 0.9, "hello")],
            taste=kwargs.get("taste", finish_mod.TasteFinish()),
            model=kwargs.get("model", "muse-spark-1.3"),
            max_usd=kwargs.get("max_usd", 5.0), job_name="job")

    def test_call_cached_gate_invalid(self) -> None:
        data = {"music": None, "effects": [
            {"kind": "riser", "at_s": 2.0, "duration_s": 1.0,
             "prompt": "soft riser", "gain_db": -14.0, "reason": "r",
             "confidence": 0.7}]}
        guest = FakeGuestlist(data=data)
        music, cues, usd, cached, notes = self._design(guest)
        self.assertIsNone(music)
        self.assertEqual([cue.id for cue in cues], ["sfx_001"])
        self.assertEqual(usd, 0.0210)
        self.assertFalse(cached)
        self.assertEqual(guest.calls[0]["name"], "finish_design")
        self.assertEqual(guest.calls[0]["max_output_tokens"], 6000)
        hit = FakeGuestlist(hit={"data": data}, usd=0.0)
        _m, _c, usd, cached, _n = self._design(hit)
        self.assertEqual(usd, 0.0)
        self.assertTrue(cached)
        self.assertEqual(hit.calls, [])
        poor = FakeGuestlist(data=data)
        _m, _c, usd, _cached, notes = self._design(poor, max_usd=0.0)
        self.assertEqual(poor.calls, [])
        self.assertIn("finish_design_unavailable:exceeds --max-usd", notes)
        weird = FakeGuestlist(data=["not", "a", "dict"])
        _m, _c, _u, _cached, notes = self._design(weird)
        self.assertIn("finish_design_unavailable:invalid reply", notes)
        odd = FakeGuestlist(data=data)
        _m, _c, _u, _cached, notes = self._design(odd, model="muse-unknown-9")
        self.assertIn("finish_estimate_default_rates:muse-unknown-9", notes)

    def test_import_error_raises(self) -> None:
        with mock.patch.dict(sys.modules, {"guestlist.meta": None}):
            with self.assertRaises(finish_mod.DesignUnavailable):
                finish_mod.design_with_muse(
                    duration_s=6.0, voice_stats={}, taste_entries=[],
                    sections=[], shots=[], words=[],
                    taste=finish_mod.TasteFinish(), model="muse-spark-1.3",
                    max_usd=5.0, job_name="job")


def _plan_3s() -> dict:
    return {"music": {"prompt": "warm pads underscore instrumental no vocals",
                      "start_s": 0.0, "end_s": 3.0, "gain_db": -20.0,
                      "dropouts": [], "reason": "bed", "confidence": 0.8},
            "effects": [
                {"kind": "riser", "at_s": 1.0, "duration_s": 1.0,
                 "prompt": "soft riser", "gain_db": -14.0, "reason": "r",
                 "confidence": 0.7},
                {"kind": "whoosh", "at_s": 1.8, "duration_s": 0.5,
                 "prompt": "soft whoosh", "gain_db": -16.0, "reason": "w",
                 "confidence": 0.6}]}


@unittest.skipUnless(FFMPEG, "needs ffmpeg and ffprobe")
class OrchestrationTests(unittest.TestCase):
    """Item 23: provider orchestration over a fake transport."""

    def _assets(self, root: Path) -> dict[str, bytes]:
        out = {}
        for name, duration in (("iso", 3.0), ("bed", 3.0), ("cue1", 1.0),
                               ("cue2", 0.5)):
            target = root / f"{name}.mp3"
            _make_mp3(target, duration)
            out[name] = target.read_bytes()
        return out

    def _run(self, job: Path, transport, guest_data: dict,
             isolate: bool = False) -> dict:
        clean_env(self, ELEVENLABS_API_KEY=ELEVEN_KEY)
        FakeGuestlist(data=guest_data).install(self)
        with (mock.patch.dict(sys.modules, {"keyring": make_keyring({})}),
              mock.patch.object(elevenlabs_mod, "http_request", transport)):
            return finish_mod.run(
                job_dir=job, model="muse-spark-1.3", no_muse=False,
                sound=True, isolate=isolate, loudness=None,
                max_provider_usd=2.0, max_usd=5.0)

    def test_call_order(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="finish-orch-")
        self.addCleanup(tmp.cleanup)
        media = self._assets(Path(tmp.name))
        transport = RouteTransport([
            ("audio-isolation", [(200, {}, media["iso"])]),
            ("/v1/music", [(200, {}, media["bed"])]),
            ("sound-generation", [(200, {}, media["cue1"]),
                                  (200, {}, media["cue2"])]),
        ])
        job = _job_dir(self, words=[("hey", 0.2, 0.4), ("yo", 2.4, 2.6)])
        payload = self._run(job, transport, _plan_3s(), isolate=True)
        kinds = [url for _m, url, _h, _b, _t in transport.calls]
        self.assertEqual(
            [url.split("api.elevenlabs.io/")[1].split("?")[0] for url in kinds],
            ["v1/audio-isolation", "v1/music", "v1/sound-generation",
             "v1/sound-generation"])
        self.assertEqual(payload["voice"]["method"], "isolated")
        self.assertEqual(
            [asset["id"] for asset in payload["assets"]],
            ["voice_isolation", "music", "sfx_001", "sfx_002"])

    def test_cap_crossing_skips_tail(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="finish-cap-")
        self.addCleanup(tmp.cleanup)
        media = self._assets(Path(tmp.name))
        transport = RouteTransport([
            ("audio-isolation", [(200, {}, media["iso"])]),
            ("/v1/music", [(200, {}, media["bed"])]),
            ("sound-generation", [(200, {}, media["cue1"])]),
        ])
        clean_env(self, ELEVENLABS_API_KEY=ELEVEN_KEY)
        FakeGuestlist(data=_plan_3s()).install(self)
        job = _job_dir(self, words=[("hey", 0.2, 0.4), ("yo", 2.4, 2.6)])
        with (mock.patch.dict(sys.modules, {"keyring": make_keyring({})}),
              mock.patch.object(elevenlabs_mod, "http_request", transport)):
            payload = finish_mod.run(
                job_dir=job, model="muse-spark-1.3", no_muse=False,
                sound=True, isolate=True, loudness=None,
                max_provider_usd=0.014, max_usd=5.0)
        self.assertIn("finish_provider_cap_reached:$0.01", payload["notes"])
        self.assertTrue((job / "finish" / "music_bed.mp3").exists())
        self.assertFalse((job / "finish" / "sfx_001.mp3").exists())
        self.assertFalse((job / "finish" / "sfx_002.mp3").exists())
        self.assertFalse(any("sound-generation" in url
                             for _m, url, _h, _b, _t in transport.calls))

    def test_auth_stop_keeps_earlier_assets(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="finish-auth-")
        self.addCleanup(tmp.cleanup)
        media = self._assets(Path(tmp.name))
        transport = RouteTransport([
            ("/v1/music", [(200, {}, media["bed"])]),
            ("sound-generation", [(200, {}, media["cue1"]),
                                  (401, {}, b"denied")]),
        ])
        job = _job_dir(self, words=[("hey", 0.2, 0.4), ("yo", 2.4, 2.6)])
        payload = self._run(job, transport, _plan_3s())
        self.assertIn("finish_provider_auth_failed:elevenlabs",
                      payload["notes"])
        self.assertTrue((job / "finish" / "sfx_001.mp3").exists())
        self.assertFalse((job / "finish" / "sfx_002.mp3").exists())
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(len(payload["plan"]["effects"]), 2)
        self.assertEqual(
            [asset["id"] for asset in payload["assets"]],
            ["music", "sfx_001"])
        # A 401 on the first cue stops everything after it too.
        transport = RouteTransport([
            ("/v1/music", [(200, {}, media["bed"])]),
            ("sound-generation", [(401, {}, b"denied")]),
        ])
        job = _job_dir(self, words=[("hey", 0.2, 0.4), ("yo", 2.4, 2.6)])
        payload = self._run(job, transport, _plan_3s())
        self.assertIn("finish_provider_auth_failed:elevenlabs",
                      payload["notes"])
        self.assertFalse((job / "finish" / "sfx_001.mp3").exists())
        self.assertEqual(len(transport.calls), 2)

    def test_empty_body_cue_skipped(self) -> None:
        plan = {"music": None, "effects": [
            {"kind": "riser", "at_s": 1.0, "duration_s": 1.0,
             "prompt": "soft riser", "gain_db": -14.0, "reason": "r",
             "confidence": 0.7}]}
        transport = FakeTransport([(200, {}, b"")])
        job = _job_dir(self, words=[("hey", 0.2, 0.4)])
        payload = self._run(job, transport, plan)
        self.assertIn("finish_cue_skipped:sfx_001:empty audio",
                      payload["notes"])
        self.assertFalse((job / "finish" / "sfx_001.mp3").exists())
        self.assertEqual(payload["assets"], [])


@unittest.skipUnless(FFMPEG, "needs ffmpeg and ffprobe")
class LocalRunTests(unittest.TestCase):
    """Item 24: local finish end-to-end, no keys."""

    def test_quiet_cut_masters_to_target(self) -> None:
        clean_env(self)
        tmp = tempfile.TemporaryDirectory(prefix="finish-local-")
        self.addCleanup(tmp.cleanup)
        job = Path(tmp.name) / "job"
        job.mkdir()
        _make_cut(job / "cut.mp4", duration=6.0)
        shots = [{"id": 0, "start_s": 0.0, "end_s": 6.0, "camera": "A",
                  "clip_path": "/media/A.mp4", "clip_in_s": 0.0,
                  "clip_out_s": 6.0, "line_ids": [0], "shot_size": "medium",
                  "reason": "test", "method": "rule", "overrules": []}]
        _write_json(job / "angles.json", _angles(shots))
        _words_file(job / "words.json", [("hello", 0.5, 0.9)])
        with mock.patch.dict(sys.modules, {"keyring": make_keyring({})}):
            payload = finish_mod.run(
                job_dir=job, model="muse-spark-1.3", no_muse=True,
                sound=False, isolate=False, loudness=None,
                max_provider_usd=2.0, max_usd=5.0)
        integrated, peak = _loudness(job / "cut.mp4")
        assert integrated is not None and peak is not None
        self.assertAlmostEqual(integrated, -14.0, delta=1.0)
        self.assertLessEqual(peak, -0.8)
        from ghostcut.ffmpeg_tools import extract_media_stats, probe_media

        stats = extract_media_stats(probe_media(job / "cut.mp4"))
        self.assertGreater(int(stats.get("width") or 0), 0)
        self.assertAlmostEqual(float(stats.get("duration_s") or 0.0), 6.0,
                               delta=0.1)
        self.assertEqual(payload["voice"]["method"], "local")
        self.assertEqual(payload["voice"]["clean_chain"],
                         "highpass+afftdn+compressor")
        self.assertEqual(payload["assets"], [])
        self.assertEqual(payload["provider_spend_usd"], 0.0)
        self.assertTrue(payload["provider_costs_are_estimates"])
        self.assertIsNone(payload["design"])


@unittest.skipUnless(FFMPEG, "needs ffmpeg and ffprobe")
class IsolatedRunTests(unittest.TestCase):
    """Item 25: isolation success and the local fallback."""

    def test_isolated_and_fallback(self) -> None:
        clean_env(self, ELEVENLABS_API_KEY=ELEVEN_KEY)
        tmp = tempfile.TemporaryDirectory(prefix="finish-iso-")
        self.addCleanup(tmp.cleanup)
        iso_mp3 = _make_mp3(Path(tmp.name) / "iso.mp3", 3.0)
        transport = FakeTransport([(200, {}, iso_mp3.read_bytes())])
        FakeGuestlist().install(self)
        job = _job_dir(self)
        with (mock.patch.dict(sys.modules, {"keyring": make_keyring({})}),
              mock.patch.object(elevenlabs_mod, "http_request", transport)):
            payload = finish_mod.run(
                job_dir=job, model="muse-spark-1.3", no_muse=True,
                sound=False, isolate=True, loudness=None,
                max_provider_usd=2.0, max_usd=5.0)
        self.assertTrue((job / "finish" / "voice_isolated.mp3").exists())
        self.assertEqual(payload["voice"]["method"], "isolated")
        self.assertEqual(payload["voice"]["clean_chain"],
                         "highpass+compressor")
        assets = payload["assets"]
        self.assertEqual(len(assets), 1)
        asset = assets[0]
        self.assertEqual(asset["id"], "voice_isolation")
        self.assertEqual(asset["model"], "audio-isolation")
        self.assertAlmostEqual(asset["cost_usd"], 3.0 / 60 * 0.12, delta=0.002)
        self.assertIn("ElevenLabs", asset["licence"])
        integrated, _peak = _loudness(job / "cut.mp4")
        assert integrated is not None
        self.assertAlmostEqual(integrated, -14.0, delta=1.0)
        failing = FakeTransport([(500, {}, b"oops")])
        job = _job_dir(self)
        with (mock.patch.dict(sys.modules, {"keyring": make_keyring({})}),
              mock.patch.object(elevenlabs_mod, "http_request", failing)):
            payload = finish_mod.run(
                job_dir=job, model="muse-spark-1.3", no_muse=True,
                sound=False, isolate=True, loudness=None,
                max_provider_usd=2.0, max_usd=5.0)
        self.assertEqual(len(failing.calls), 1)  # isolation never retries
        self.assertTrue(any(note.startswith("finish_isolate_fallback:local")
                            for note in payload["notes"]))
        self.assertEqual(payload["voice"]["method"], "local")
        integrated, _peak = _loudness(job / "cut.mp4")
        assert integrated is not None
        self.assertAlmostEqual(integrated, -14.0, delta=1.0)


def _plan_6s() -> dict:
    return {"music": {"prompt": "warm pads underscore instrumental no vocals",
                      "start_s": 0.0, "end_s": 6.0, "gain_db": -20.0,
                      "dropouts": [{"start_s": 4.5, "end_s": 5.5}],
                      "reason": "bed", "confidence": 0.8},
            "effects": [
                {"kind": "riser", "at_s": 1.2, "duration_s": 1.5,
                 "prompt": "soft riser", "gain_db": -14.0, "reason": "r",
                 "confidence": 0.7},
                {"kind": "whoosh", "at_s": 4.0, "duration_s": 1.0,
                 "prompt": "soft whoosh", "gain_db": -16.0, "reason": "w",
                 "confidence": 0.6}]}


@unittest.skipUnless(FFMPEG, "needs ffmpeg and ffprobe")
class MixTests(unittest.TestCase):
    """Item 26: bed + effects mix, dropout, ducking, master."""

    def test_bed_effects_dropout_master(self) -> None:
        clean_env(self, ELEVENLABS_API_KEY=ELEVEN_KEY)
        tmp = tempfile.TemporaryDirectory(prefix="finish-mix-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        bed = _make_mp3(root / "bed.mp3", 6.0, freq=220.0)
        cue1 = _make_mp3(root / "cue1.mp3", 1.5, freq=880.0)
        cue2 = _make_mp3(root / "cue2.mp3", 1.0, freq=660.0)
        transport = RouteTransport([
            ("/v1/music", [(200, {}, bed.read_bytes())]),
            ("sound-generation", [(200, {}, cue1.read_bytes()),
                                  (200, {}, cue2.read_bytes())]),
        ])
        FakeGuestlist(data=_plan_6s()).install(self)
        job = root / "job"
        job.mkdir()
        _make_gap_voice_cut(job / "cut.mp4")
        shots = [{"id": 0, "start_s": 0.0, "end_s": 6.0, "camera": "A",
                  "clip_path": "/media/A.mp4", "clip_in_s": 0.0,
                  "clip_out_s": 6.0, "line_ids": [0], "shot_size": "medium",
                  "reason": "test", "method": "rule", "overrules": []}]
        _write_json(job / "angles.json", _angles(shots))
        _words_file(job / "words.json", [("hey", 0.2, 0.4), ("yo", 5.6, 5.8)])
        with (mock.patch.dict(sys.modules, {"keyring": make_keyring({})}),
              mock.patch.object(elevenlabs_mod, "http_request", transport)):
            payload = finish_mod.run(
                job_dir=job, model="muse-spark-1.3", no_muse=False,
                sound=True, isolate=False, loudness=None,
                max_provider_usd=2.0, max_usd=5.0)
        from ghostcut.ffmpeg_tools import extract_media_stats, probe_media

        for name in ("music_mix.wav", "sfx_mix.wav", "mix_premaster.wav"):
            stats = extract_media_stats(probe_media(job / "finish" / name))
            self.assertAlmostEqual(float(stats["duration_s"]), 6.0, delta=0.2)
        music_mix = job / "finish" / "music_mix.wav"
        self.assertLess(_mean_volume(music_mix, 4.6, 0.8), -60.0)
        self.assertGreater(_mean_volume(music_mix, 0.5, 1.0), -40.0)
        self.assertEqual(
            [asset["id"] for asset in payload["assets"]],
            ["music", "sfx_001", "sfx_002"])
        integrated, _peak = _loudness(job / "cut.mp4")
        assert integrated is not None
        self.assertAlmostEqual(integrated, -14.0, delta=1.0)

    def test_ducking_suppresses_bed_under_voice(self) -> None:
        # The premaster always contains the voice, so a raw window
        # comparison cannot show ducking; instead the bed term under
        # speech (premaster minus voice-only energy, uncorrelated
        # tones, RMS metering) is compared against the bed-only tail.
        # (lavfi sine peaks at 1/8, so the voice runs hot enough to
        # cross the 0.10 sidechain threshold.)
        import re as _re

        from ghostcut.ffmpeg_tools import run_command

        def _rms(path: Path, start: float, duration: float) -> float:
            proc = run_command(
                ["ffmpeg", "-v", "info", "-ss", f"{start:.3f}", "-t",
                 f"{duration:.3f}", "-i", str(path), "-af", "astats",
                 "-f", "null", "-"],
                check=False)
            levels = [float(found) for found in _re.findall(
                r"RMS level dB:\s*(-inf|[-\d.]+)", proc.stderr or "")]
            assert levels, proc.stderr
            return sum(levels) / len(levels)

        tmp = tempfile.TemporaryDirectory(prefix="finish-duck-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        voice, bed = root / "voice.wav", root / "bed.wav"
        _run(["ffmpeg", "-v", "error", "-y",
              "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
              "-af", "volume=2.0,volume=0:enable='gte(t,3)'",
              "-c:a", "pcm_s16le", str(voice)])
        _run(["ffmpeg", "-v", "error", "-y",
              "-f", "lavfi", "-i", "sine=frequency=220:duration=6",
              "-af", "volume=6.0,pan=stereo|c0=c0|c1=c0",
              "-c:a", "pcm_s16le", str(bed)])
        ducked, voice_only = root / "ducked.wav", root / "voice_only.wav"
        run_command(finish_mod.duck_mix_command(voice, bed, None, ducked))
        run_command(finish_mod.duck_mix_command(voice, None, None, voice_only))
        mix_a = _rms(ducked, 1.0, 1.0)
        vox_a = _rms(voice_only, 1.0, 1.0)
        self.assertGreater(mix_a, vox_a)
        bed_ducked = 10 * math.log10(10 ** (mix_a / 10) - 10 ** (vox_a / 10))
        bed_full = _rms(ducked, 4.0, 1.0)
        self.assertGreater(bed_full - bed_ducked, 2.0)


@unittest.skipUnless(FFMPEG, "needs ffmpeg and ffprobe")
class EdgeTests(unittest.TestCase):
    """Item 27: silence, no audio, re-run, untouched bytes."""

    def test_silent_fallback_or_unmeasurable(self) -> None:
        clean_env(self)
        tmp = tempfile.TemporaryDirectory(prefix="finish-silent-")
        self.addCleanup(tmp.cleanup)
        job = Path(tmp.name) / "job"
        job.mkdir()
        _make_silent_cut(job / "cut.mp4")
        shots = [{"id": 0, "start_s": 0.0, "end_s": 3.0, "camera": "A",
                  "clip_path": "/media/A.mp4", "clip_in_s": 0.0,
                  "clip_out_s": 3.0, "line_ids": [0], "shot_size": "medium",
                  "reason": "test", "method": "rule", "overrules": []}]
        _write_json(job / "angles.json", _angles(shots))
        _words_file(job / "words.json", [("hey", 0.5, 0.9)])
        with mock.patch.dict(sys.modules, {"keyring": make_keyring({})}):
            try:
                payload = finish_mod.run(
                    job_dir=job, model="muse-spark-1.3", no_muse=True,
                    sound=False, isolate=False, loudness=None,
                    max_provider_usd=2.0, max_usd=5.0)
            except finish_mod.FinishError as error:
                self.assertEqual(
                    str(error),
                    "finish: could not measure loudness for mastering")
            else:
                self.assertIn("finish_loudness_unmeasured",
                              payload["warnings"])

    def test_video_only_leaves_bytes_untouched(self) -> None:
        import hashlib

        clean_env(self)
        tmp = tempfile.TemporaryDirectory(prefix="finish-noaudio-")
        self.addCleanup(tmp.cleanup)
        job = Path(tmp.name) / "job"
        job.mkdir()
        _make_video_only(job / "cut.mp4")
        before = hashlib.sha256((job / "cut.mp4").read_bytes()).hexdigest()
        shots = [{"id": 0, "start_s": 0.0, "end_s": 2.0, "camera": "A",
                  "clip_path": "/media/A.mp4", "clip_in_s": 0.0,
                  "clip_out_s": 2.0, "line_ids": [0], "shot_size": "medium",
                  "reason": "test", "method": "rule", "overrules": []}]
        _write_json(job / "angles.json", _angles(shots))
        with mock.patch.dict(sys.modules, {"keyring": make_keyring({})}):
            with self.assertRaises(finish_mod.FinishError) as raised:
                finish_mod.run(
                    job_dir=job, model="muse-spark-1.3", no_muse=True,
                    sound=False, isolate=False, loudness=None,
                    max_provider_usd=2.0, max_usd=5.0)
        self.assertEqual(str(raised.exception),
                         "finish: cut.mp4 has no audio stream to finish")
        self.assertEqual(
            hashlib.sha256((job / "cut.mp4").read_bytes()).hexdigest(), before)

    def test_rerun_reuses_finish_dir(self) -> None:
        clean_env(self)
        job = _job_dir(self)
        with mock.patch.dict(sys.modules, {"keyring": make_keyring({})}):
            first = finish_mod.run(
                job_dir=job, model="muse-spark-1.3", no_muse=True,
                sound=False, isolate=False, loudness=None,
                max_provider_usd=2.0, max_usd=5.0)
            second = finish_mod.run(
                job_dir=job, model="muse-spark-1.3", no_muse=True,
                sound=False, isolate=False, loudness=None,
                max_provider_usd=2.0, max_usd=5.0)
        self.assertTrue((job / "finish" / "voice_raw.wav").exists())
        self.assertEqual(first["voice"], second["voice"])
        self.assertEqual(first["target_lufs"], second["target_lufs"])


def _cut_cards(root: Path, duration: float = 3.0) -> Path:
    cards = root / "cards"
    cards.mkdir(exist_ok=True)
    for name in ("a.mp4", "b.mp4"):
        _make_cut(cards / name, duration=duration, freq=440.0, volume=0.2)
    return cards


def _cut_words(path: Path) -> Path:
    return _words_file(path, [(f"w{i}", 0.2 + i * 0.12, 0.28 + i * 0.12)
                              for i in range(20)])


def _mini_cut(test: unittest.TestCase, root: Path, out_name: str,
              **kwargs) -> tuple[Path, dict]:
    """A fast real cut: waveform sync, explicit words, no grade/QC extras."""
    cards = root / "cards"
    if not (cards / "a.mp4").exists() or not (cards / "b.mp4").exists():
        _cut_cards(root)
    words_path = root / "words.json"
    if not words_path.exists():
        _cut_words(words_path)
    out_dir = root / out_name
    params = {"clips": [cards], "out_dir": out_dir, "reference_path": None,
              "audio_path": None, "words_path": words_path,
              "transcripts_dir": None, "methods": ["waveform"],
              "no_grade": True, "grade_check": False, "no_muse": True,
              "model": "muse-spark-1.3", "min_shot_s": 0.5,
              "no_vertical": True, "no_sam": True, "no_export": False,
              "no_qc": False, "no_tighten": True, "max_usd": 5.0,
              "force": False, "project_name": "Director cut",
              "no_finish": False}
    params.update(kwargs)
    with mock.patch.dict(sys.modules, {"keyring": make_keyring({})}):
        payload = cut_mod.run(**params)
    return out_dir, payload


@unittest.skipUnless(FFMPEG, "needs ffmpeg and ffprobe")
class CutIntegrationTests(unittest.TestCase):
    """Items 28-29: finish inside director cut."""

    def test_finish_stage_order_spend_and_off_switch(self) -> None:
        self.assertLess(cut_mod.STAGES.index("angles"),
                        cut_mod.STAGES.index("finish"))
        self.assertLess(cut_mod.STAGES.index("finish"),
                        cut_mod.STAGES.index("export"))
        clean_env(self, ELEVENLABS_API_KEY=ELEVEN_KEY)
        tmp = tempfile.TemporaryDirectory(prefix="finish-cut28-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        out_dir, payload = _mini_cut(self, root, "job")
        self.assertIn(payload["status"], ("complete", "complete_with_warnings"))
        self.assertIn("finish", payload["stages"])
        self.assertEqual(payload["stages"]["finish"]["status"], "done")
        self.assertTrue((out_dir / "finish.json").exists())
        self.assertEqual(payload["provider_spend_usd"], 0.0)
        self.assertEqual(payload["stages"]["finish"]["provider_spend_usd"], 0.0)
        total = sum(record["spend_usd"]
                    for record in payload["stages"].values())
        self.assertAlmostEqual(total, payload["spend_usd"], delta=0.001)
        self.assertNotIn(ELEVEN_KEY, (out_dir / "cut.json").read_text())
        out2, payload2 = _mini_cut(self, root, "job-nofinish", no_finish=True)
        self.assertNotIn("finish", payload2["stages"])
        self.assertFalse((out2 / "finish.json").exists())

    def test_cut_console_finish_lines(self) -> None:
        clean_env(self)
        tmp = tempfile.TemporaryDirectory(prefix="finish-cutcli-")
        self.addCleanup(tmp.cleanup)
        out_dir, payload = _mini_cut(self, Path(tmp.name), "job")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli_mod._print_cut_progress(payload)
            cli_mod._print_cut_summary(payload)
        text = out.getvalue()
        self.assertIn("Provider spend: $0.00 (estimated list prices)", text)
        self.assertRegex(text, r"finish: done( \(\$0\.0000\))? \(local, -1\d\.\d LUFS\)")
        payload["export"] = {**(payload.get("export") or {}),
                             "finish_stems": ["music_bed.mp3"]}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli_mod._print_cut_summary(payload)
        self.assertIn("re-lay finish stems from", out.getvalue())

    def test_resume_rerun_and_key_rotation(self) -> None:
        clean_env(self)
        tmp = tempfile.TemporaryDirectory(prefix="finish-cut29-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        out_dir, first = _mini_cut(self, root, "job")
        self.assertEqual(first["stages"]["finish"]["status"], "done")
        before = (out_dir / "cut.mp4").stat().st_mtime_ns
        _out, second = _mini_cut(self, root, "job")
        self.assertEqual(second["stages"]["finish"]["status"], "skipped")
        self.assertEqual((out_dir / "cut.mp4").stat().st_mtime_ns, before)
        _taste_file(out_dir / "taste.json", ["music low"])
        _out, third = _mini_cut(self, root, "job")
        self.assertEqual(third["stages"]["finish"]["status"], "done")
        strict = mock.Mock(side_effect=AssertionError("must stay offline"))
        with (mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key-one"}),
              mock.patch.object(elevenlabs_mod, "http_request", strict)):
            _out, fourth = _mini_cut(self, root, "job")
        self.assertEqual(fourth["stages"]["finish"]["status"], "done")
        strict.assert_not_called()
        with (mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key-two"}),
              mock.patch.object(elevenlabs_mod, "http_request", strict)):
            _out, fifth = _mini_cut(self, root, "job")
        self.assertEqual(fifth["stages"]["finish"]["status"], "skipped")
        strict.assert_not_called()


class ExportStemsTests(unittest.TestCase):
    """Item 30: stems are listed, timelines byte-identical."""

    def test_stems_and_identical_timelines(self) -> None:
        import hashlib

        from tests.test_export import (_angles_payload, _shot, _sync_payload,
                                       _write)

        tmp = tempfile.TemporaryDirectory(prefix="finish-export-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        shots = [_shot(0, 10.0, 16.0, "A", 1.0, 7.0),
                 _shot(1, 16.0, 18.0, "black"),
                 _shot(2, 18.0, 24.0, "B", 0.5, 6.5)]
        angles = _write(root / "angles.json", _angles_payload(shots))
        sync = _write(root / "sync.json", _sync_payload())
        finish_file = root / "finish.json"
        _write_json(finish_file, {
            "version": 1, "video": "cut.mp4", "duration_s": 14.0,
            "voice": {"method": "local", "clean_chain": "c",
                      "isolate_requested": False},
            "provider_costs_are_estimates": True, "target_lufs": -14.0,
            "loudness_before": None, "loudness_after": None, "design": None,
            "plan": {"music": None, "effects": []},
            "assets": [
                {"id": "music", "kind": "music", "file": "music_bed.mp3",
                 "provider": "elevenlabs", "model": "m", "prompt": "p",
                 "duration_s": 6.0, "cost_usd": 0.01, "licence": "L"},
                {"id": "sfx_001", "kind": "whoosh", "file": "sfx_001.mp3",
                 "provider": "elevenlabs", "model": "m", "prompt": "q",
                 "duration_s": 1.0, "cost_usd": 0.01, "licence": "L"},
                {"id": "voice_isolation", "kind": "voice_isolation",
                 "file": "voice_isolated.mp3", "provider": "elevenlabs",
                 "model": "m", "prompt": "", "duration_s": 3.0,
                 "cost_usd": 0.01, "licence": "L"},
            ],
            "provider_spend_usd": 0.03, "notes": [], "warnings": []})
        plain = export_mod.export_timelines(
            angles_path=angles, sync_path=sync, grade_path=None,
            fcpxml_path=root / "plain.fcpxml", otio_path=root / "plain.otio.json",
            project_name="Director cut")
        with_finish = export_mod.export_timelines(
            angles_path=angles, sync_path=sync, grade_path=None,
            fcpxml_path=root / "with.fcpxml", otio_path=root / "with.otio.json",
            project_name="Director cut", finish_path=finish_file)
        self.assertNotIn("finish_stems", plain)
        self.assertEqual(with_finish["finish_stems"],
                         ["music_bed.mp3", "sfx_001.mp3"])
        self.assertIn("finish_audio_not_in_timelines:music_bed.mp3,sfx_001.mp3",
                      with_finish["warnings"])
        for plain_name, with_name in (("plain.fcpxml", "with.fcpxml"),
                                      ("plain.otio.json", "with.otio.json")):
            left = hashlib.sha256((root / plain_name).read_bytes()).hexdigest()
            right = hashlib.sha256((root / with_name).read_bytes()).hexdigest()
            self.assertEqual(left, right)


@unittest.skipUnless(FFMPEG, "needs ffmpeg and ffprobe")
class FinishCliTests(unittest.TestCase):
    """Item 31: director finish forms."""

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_mod.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_job_explicit_loudness_and_bad_values(self) -> None:
        clean_env(self)
        with mock.patch.dict(sys.modules, {"keyring": make_keyring({})}):
            job = _job_dir(self)
            code, out, _err = self._run(
                ["finish", "--job", str(job), "--no-muse"])
            self.assertEqual(code, 0)
            self.assertIn("This will replace the audio of", out)
            self.assertIn(f"Finished {job / 'cut.mp4'}: local master", out)
            self.assertIn("Wrote", out)
            self.assertTrue((job / "finish.json").exists())
            tmp = tempfile.TemporaryDirectory(prefix="finish-explicit-")
            self.addCleanup(tmp.cleanup)
            root = Path(tmp.name)
            _make_cut(root / "mycut.mp4", duration=3.0)
            shots = [{"id": 0, "start_s": 0.0, "end_s": 3.0, "camera": "A",
                      "clip_path": "/media/A.mp4", "clip_in_s": 0.0,
                      "clip_out_s": 3.0, "line_ids": [0],
                      "shot_size": "medium", "reason": "t", "method": "rule",
                      "overrules": []}]
            _write_json(root / "angles.json", _angles(shots))
            code, out, _err = self._run(
                ["finish", "--video", str(root / "mycut.mp4"),
                 "--angles", str(root / "angles.json"), "--no-muse",
                 "--loudness", "-16"])
            self.assertEqual(code, 0)
            payload = json.loads((root / "finish.json").read_text())
            self.assertEqual(payload["target_lufs"], -16.0)
            integrated, _peak = _loudness(root / "mycut.mp4")
            assert integrated is not None
            self.assertAlmostEqual(integrated, -16.0, delta=1.0)
            self.assertIn("(target -16)", out)
            code, _out, err = self._run(
                ["finish", "--job", str(job), "--loudness", "-5"])
            self.assertEqual(code, 1)
            self.assertIn("--loudness must be between -20 and -10", err)
            code, _out, err = self._run(
                ["finish", "--job", str(job), "--max-provider-usd", "-1"])
            self.assertEqual(code, 1)
            self.assertIn("--max-provider-usd must be >= 0", err)


@unittest.skipUnless(FFMPEG, "needs ffmpeg and ffprobe")
class NoKeySweepTests(unittest.TestCase):
    """Item 32: keys appear in no finish/cut artifact or console line."""

    def test_sweep(self) -> None:
        clean_env(self, ELEVENLABS_API_KEY=ELEVEN_KEY)
        tmp = tempfile.TemporaryDirectory(prefix="finish-sweep-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        iso = _make_mp3(root / "iso.mp3", 3.0)
        bed = _make_mp3(root / "bed.mp3", 3.0)
        cue = _make_mp3(root / "cue.mp3", 1.0)
        transport = RouteTransport([
            ("audio-isolation", [(200, {}, iso.read_bytes())]),
            ("/v1/music", [(200, {}, bed.read_bytes())]),
            ("sound-generation", [(200, {}, cue.read_bytes())]),
        ])
        FakeGuestlist(data=_plan_3s()).install(self)
        job = root / "job"
        job.mkdir()
        _make_cut(job / "cut.mp4", duration=3.0)
        shots = [{"id": 0, "start_s": 0.0, "end_s": 3.0, "camera": "A",
                  "clip_path": "/media/A.mp4", "clip_in_s": 0.0,
                  "clip_out_s": 3.0, "line_ids": [0], "shot_size": "medium",
                  "reason": "t", "method": "rule", "overrules": []}]
        _write_json(job / "angles.json", _angles(shots))
        _words_file(job / "words.json", [("hey", 0.2, 0.4)])
        out, err = io.StringIO(), io.StringIO()
        with (mock.patch.dict(sys.modules, {"keyring": make_keyring({})}),
              mock.patch.object(elevenlabs_mod, "http_request", transport),
              contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)):
            code = cli_mod.main(["finish", "--job", str(job), "--sound",
                                 "--isolate"])
        self.assertEqual(code, 0)
        self.assertNotIn(ELEVEN_KEY, out.getvalue())
        self.assertNotIn(ELEVEN_KEY, err.getvalue())
        self.assertNotIn(ELEVEN_KEY, (job / "finish.json").read_text())
        names = " ".join(path.name for path in (job / "finish").iterdir())
        self.assertNotIn(ELEVEN_KEY, names)
        _out_dir, payload = _mini_cut(self, root, "job2", no_qc=True)
        self.assertNotIn(ELEVEN_KEY, json.dumps(payload))


class ConstantTests(unittest.TestCase):
    """Item 33: §3 rates, models and endpoints are pinned."""

    def test_pins(self) -> None:
        self.assertEqual(elevenlabs_mod.ELEVENLABS_BASE,
                         "https://api.elevenlabs.io")
        self.assertEqual(elevenlabs_mod.SFX_MODEL, "eleven_text_to_sound_v3")
        self.assertEqual(elevenlabs_mod.MUSIC_MODEL, "music_v2_5")
        self.assertEqual(elevenlabs_mod.SFX_OUTPUT_FORMAT, "mp3_44100_128")
        self.assertEqual(elevenlabs_mod.MUSIC_OUTPUT_FORMAT, "mp3_48000_192")
        self.assertEqual(elevenlabs_mod.SFX_USD_PER_MIN, 0.12)
        self.assertEqual(elevenlabs_mod.MUSIC_USD_PER_MIN, 0.15)
        self.assertEqual(elevenlabs_mod.ISOLATION_USD_PER_MIN, 0.12)
        self.assertEqual(finish_mod.MUSIC_GAIN_DB, -20.0)
        self.assertEqual(finish_mod.MUSIC_GAIN_LOW_DB, -26.0)
        self.assertEqual(finish_mod.DUCK_THRESHOLD, 0.10)
        self.assertEqual(finish_mod.DUCK_RATIO, 4)
        self.assertEqual(finish_mod.MAX_SFX_TOTAL, 12)
        self.assertEqual(finish_mod.ISOLATION_MAX_S, 3600)
        self.assertEqual(finish_mod.DEFAULT_MAX_PROVIDER_USD, 2.00)


class AppConnectionsTests(unittest.TestCase):
    """Items 34-38: the Connections panel contract."""

    def test_connections_live_in_both_modes(self) -> None:
        clean_env(self)
        with mock.patch.dict(sys.modules, {"keyring": make_keyring({})}):
            ctx = Ctx(self)
            code, legacy = ctx.get("/api/connections")
            self.assertEqual(code, 200)
            ctx.engine.job_dir = ctx.demo / "job"
            code, jobbed = ctx.get("/api/connections")
            self.assertEqual(code, 200)
            self.assertEqual(legacy, jobbed)
            code, _payload = ctx.post("/api/connections/test",
                                      {"provider": "elevenlabs",
                                       "job": "ignored"})
            self.assertEqual(code, 200)

    def test_save_never_echoes(self) -> None:
        clean_env(self)
        ring = make_keyring()
        with mock.patch.dict(sys.modules, {"keyring": ring}):
            ctx = Ctx(self)
            code, payload = ctx.post("/api/connections/add",
                                     {"provider": "elevenlabs",
                                      "key": ELEVEN_KEY})
            self.assertEqual(code, 200)
            dumped = json.dumps(payload)
            self.assertNotIn(ELEVEN_KEY, dumped)
            for entry in payload["providers"]:
                self.assertNotIn("mask", entry)
                self.assertNotIn("last4", entry)
                self.assertNotIn("fingerprint", entry)
            self.assertEqual(ring._store[("ghostcut-director", "elevenlabs")],
                             ELEVEN_KEY)

    def test_test_route_never_generates(self) -> None:
        clean_env(self, ELEVENLABS_API_KEY=ELEVEN_KEY,
                  HF_API_KEY_ID="test-id-1", HF_API_KEY_SECRET="test-secret-2")
        eleven = FakeTransport(
            [(200, {}, b'{"models": [{"id": "a"}, {"id": "b"}]}')])
        higgs = FakeTransport([(200, {}, b'{"usd": "0.094"}')])
        with (mock.patch.dict(sys.modules, {"keyring": make_keyring({})}),
              mock.patch.object(elevenlabs_mod, "http_request", eleven),
              mock.patch("director.higgsfield.http_request", higgs)):
            ctx = Ctx(self)
            code, payload = ctx.post("/api/connections/test", {})
            self.assertEqual(code, 200)
            self.assertEqual(
                [(method, url) for method, url, _h, _b, _t in eleven.calls],
                [("GET", "https://api.elevenlabs.io/v1/models")])
            self.assertEqual(
                [(method, url) for method, url, _h, _b, _t in higgs.calls],
                [("POST", "https://api.higgsfield.ai/estimate/"
                          "higgsfield-ai/soul/v2/standard")])
            for _m, url, _h, _b, _t in eleven.calls + higgs.calls:
                self.assertNotIn("sound-generation", url)
                self.assertNotIn("v1/music", url)
                self.assertNotIn("audio-isolation", url)
            self.assertTrue(all(item["ok"] for item in payload["tests"]))

    @unittest.skipUnless(FFMPEG, "needs ffmpeg and ffprobe")
    def test_finished_job_needs_no_app_reader(self) -> None:
        from tests.test_cutjob import _write_job

        clean_env(self)
        with mock.patch.dict(sys.modules, {"keyring": make_keyring({})}):
            ctx = Ctx(self)
            job = _write_job(ctx.demo)
            _write_json(job / "finish.json", {
                "version": 1, "video": "cut.mp4", "duration_s": 6.0,
                "voice": {"method": "local", "clean_chain": "c",
                          "isolate_requested": False},
                "provider_costs_are_estimates": True, "target_lufs": -14.0,
                "loudness_before": None, "loudness_after": None,
                "design": None, "plan": {"music": None, "effects": []},
                "assets": [], "provider_spend_usd": 0.0,
                "notes": [], "warnings": []})
            (job / "finish").mkdir(exist_ok=True)
            _make_video_only(job / "cut.mp4", duration=1.0)
            ctx.engine.open_job(str(job))
            code, _payload = ctx.get("/api/project")
            self.assertEqual(code, 200)
            self.assertEqual(sorted(path.name for path in job.glob("*.mp4")),
                             ["cut.mp4"])

    def test_panel_copy_comes_from_registry(self) -> None:
        clean_env(self)
        with mock.patch.dict(sys.modules, {"keyring": make_keyring({})}):
            ctx = Ctx(self)
            _code, payload = ctx.get("/api/connections")
            higgs = {entry["id"]: entry
                     for entry in payload["providers"]}["higgsfield"]
            self.assertEqual(
                higgs["unavailable_reason"],
                connect_mod.PROVIDERS["higgsfield"].unavailable_reason)


if __name__ == "__main__":
    unittest.main()
