"""Voice notes: transcribe, anchor, resolve, verify, apply (VOICE §11 items 1-16).

Never calls the Meta API, Whisper or a microphone: stand-in
guestlist.meta modules in sys.modules return fixed transcripts and fixed
Spark replies (the test_angles.FakeMuseTests pattern), and subprocesses
are stubbed at the module boundary.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from director import transcribe as transcribe_mod
from director import voice as voice_mod

RATE = 0.18 / 3600


# ------------------------------------------------- fixtures


def _words() -> list[dict]:
    rows = []
    for word, start, end in [
            ("Alpha", 0.5, 0.9), ("beta.", 1.0, 1.4),
            ("Alpha", 10.5, 10.9), ("beta", 11.0, 11.4),
            ("green", 11.5, 11.7), ("lagoon.", 11.7, 12.0),
            ("Delta", 20.5, 20.9), ("epsilon", 21.0, 21.4),
            ("green", 21.5, 21.7), ("lagoon.", 21.7, 22.0)]:
        rows.append({"word": word, "start": start, "end": end})
    return rows


def _sync() -> dict:
    return {"reference": "a.mp4", "clips": [
        {"label": "A", "offset_s": 0.0, "camera": {"path": "a.mp4", "fps": 30,
         "width": 1920, "height": 1080, "duration_s": 30, "make": "Sony",
         "model": "FX3"}},
        {"label": "B", "offset_s": 0.0, "camera": {"path": "b.mp4", "fps": 30,
         "width": 1920, "height": 1080, "duration_s": 30, "make": "Apple",
         "model": "iPhone"}}]}


def _tighten() -> dict:
    return {
        "version": 1, "method": "rule", "model": "muse-spark-1.3",
        "pause_limit_s": 0.8, "spend_usd": 0.0, "warnings": [],
        "duration_in_s": 22.1, "duration_out_s": 3.4,
        "utterances": [
            {"id": 0, "start_s": 0.5, "end_s": 1.4,
             "text": "Alpha beta.", "word_start": 0, "word_end": 2},
            {"id": 1, "start_s": 10.5, "end_s": 12.0,
             "text": "Alpha beta green lagoon.", "word_start": 2,
             "word_end": 6},
            {"id": 2, "start_s": 20.5, "end_s": 22.0,
             "text": "Delta epsilon green lagoon.", "word_start": 6,
             "word_end": 10}],
        "beats": [
            {"id": 0, "topic": "the opener", "status": "kept",
             "keep_priority": 5, "pick_attempt": 1, "reason": "clean pass",
             "attempts": [
                 {"id": 0, "utterance_ids": [0], "start_s": 0.5,
                  "end_s": 1.4, "text": "Alpha beta.",
                  "verdict": "alternate", "kind": "alternate",
                  "reason": "flat", "confidence": 0.8},
                 {"id": 1, "utterance_ids": [1], "start_s": 10.5,
                  "end_s": 12.0, "text": "Alpha beta green lagoon.",
                  "verdict": "pick", "kind": "keeper", "reason": "clean",
                  "confidence": 0.9}]},
            {"id": 1, "topic": "the demo", "status": "kept",
             "keep_priority": 5, "pick_attempt": 0, "reason": "only pass",
             "attempts": [
                 {"id": 0, "utterance_ids": [2], "start_s": 20.5,
                  "end_s": 22.0, "text": "Delta epsilon green lagoon.",
                  "verdict": "pick", "kind": "keeper", "reason": "only",
                  "confidence": 0.9}]}],
        "kept_spans": [
            {"id": "K0", "start_s": 10.4, "end_s": 12.1, "beat_id": 0,
             "attempt_id": 1, "utterance_ids": [1], "kind": "keeper",
             "reason": "clean"},
            {"id": "K1", "start_s": 20.4, "end_s": 22.1, "beat_id": 1,
             "attempt_id": 0, "utterance_ids": [2], "kind": "keeper",
             "reason": "only"}],
        "removed_spans": [], "angle_overrides": [], "edits_applied": [],
        "span_exclusions": [], "editor_notes": []}


def _angles(*, beat_ids: bool = True) -> dict:
    def _shot(sid: int, start: float, end: float, camera: str,
              size: str, line: int, clip: str, cin: float, cout: float,
              bid: int) -> dict:
        shot = {"id": sid, "start_s": start, "end_s": end,
                "camera": camera, "clip_path": clip, "clip_in_s": cin,
                "clip_out_s": cout, "line_ids": [line], "shot_size": size,
                "reason": "rule", "method": "rule", "overrules": [],
                "lut": None}
        if beat_ids:
            shot["beat_id"] = bid
        return shot

    return {
        "version": 1, "sync": "sync.json", "words": "words.json",
        "reference": "a.mp4", "grade": None,
        "audio": {"label": "A", "clip_path": "a.mp4", "offset_s": 0.0},
        "method": "rule", "model": "muse-spark-1.3", "min_shot_s": 2.0,
        "spend_usd": 0.0, "warnings": [],
        "lines": [
            {"id": 0, "start_s": 10.5, "end_s": 12.0,
             "text": "Alpha beta green lagoon.", "cameras": ["A", "B"]},
            {"id": 1, "start_s": 20.5, "end_s": 22.0,
             "text": "Delta epsilon green lagoon.", "cameras": ["A", "B"]}],
        "shots": [
            _shot(0, 0.0, 1.7, "A", "medium", 0, "a.mp4", 10.4, 12.1, 0),
            _shot(1, 1.7, 3.4, "B", "close", 1, "b.mp4", 20.4, 22.1, 1)]}


def _write_job(root: Path, *, tighten: bool = True) -> Path:
    job = root / "job"
    job.mkdir(parents=True, exist_ok=True)
    (job / "words.json").write_text(json.dumps({"words": _words()}))
    (job / "sync.json").write_text(json.dumps(_sync()))
    (job / "angles.json").write_text(json.dumps(_angles()))
    if tighten:
        (job / "tighten.json").write_text(json.dumps(_tighten()))
    (job / "cut.json").write_text(json.dumps(
        {"version": 1, "status": "ok", "out_dir": str(job)}))
    return job


def _entry(**over: object) -> dict:
    base: dict = {"note_index": 1, "heard": "", "confidence": 0.9,
                  "anchor_kind": "none", "at_s": -1, "quote": "",
                  "beat_id": -1, "status": "resolved", "message": "",
                  "ops": [], "taste": [], "raw_text": "",
                  "playhead_at": None}
    base.update(over)
    return base


def _op(name: str, **over: object) -> dict:
    base: dict = {"op": name, "beat_id": -1, "attempt_id": -1,
                  "span_id": "", "camera": "", "pause_limit_s": -1,
                  "target_s": -1, "word_start": -1, "word_end": -1,
                  "text": "", "why": "the note says so"}
    base.update(over)
    return base


# ------------------------------------------------- fake Meta client


class _FakeCache:
    def key(self, *parts: object) -> str:
        return json.dumps(parts, sort_keys=True, default=str)

    def get(self, _ns: str, _key: str) -> None:
        return None

    def guard(self, _usd: float) -> None:
        return None

    def sha256_file(self, _path: object) -> str:
        return "fake-sha"


class _FakePricing:
    def transcribe_cost(self, seconds: float) -> float:
        return float(seconds) * RATE


class _FakeTranscribe:
    MODEL = "fake-transcribe"

    def __init__(self, turns: list[dict], duration_ms: int = 8000):
        self._turns = turns
        self._duration_ms = duration_ms
        self.calls: list[str] = []

    def diarize(self, path: object, model: object = None,
                **_kw: object) -> dict:
        self.calls.append(str(path))
        return {"turns": [dict(t) for t in self._turns],
                "audioDurationMs": self._duration_ms}


class _FakeSpark:
    def __init__(self, reply: dict | None = None,
                 image: dict | None = None):
        self._reply = reply if reply is not None else {"notes": []}
        self._image = image if image is not None else {}
        self.structured_calls: list[dict] = []
        self.image_calls: list[dict] = []

    def _structured(self, key: object, content: object, **kw: object):
        assert kw.get("name") == "voice_resolve", kw.get("name")
        self.structured_calls.append({"key": key, "content": content,
                                      **kw})
        return SimpleNamespace(data=self._reply, usd=0.0123,
                               cached=False)

    def image_json(self, images: object, **kw: object):
        self.image_calls.append({"images": images, **kw})
        return SimpleNamespace(data=self._image, usd=0.0045,
                               cached=False)


@contextlib.contextmanager
def _fake_meta(turns: list[dict] | None = None,
               reply: dict | None = None, image: dict | None = None):
    """Stand-in guestlist.meta modules; the real package is restored."""
    names = ["guestlist", "guestlist.meta"]
    saved = {name: sys.modules.get(name) for name in names}
    package = types.ModuleType("guestlist")
    package.__path__ = []  # type: ignore[attr-defined]
    meta = types.ModuleType("guestlist.meta")
    meta.cache = _FakeCache()  # type: ignore[attr-defined]
    meta.pricing = _FakePricing()  # type: ignore[attr-defined]
    fake_transcribe = _FakeTranscribe(turns or [])
    meta.transcribe = fake_transcribe  # type: ignore[attr-defined]
    fake_spark = _FakeSpark(reply, image)
    meta.spark = fake_spark  # type: ignore[attr-defined]
    package.meta = meta  # type: ignore[attr-defined]
    sys.modules["guestlist"] = package
    sys.modules["guestlist.meta"] = meta
    try:
        yield SimpleNamespace(transcribe=fake_transcribe,
                              spark=fake_spark)
    finally:
        for name in names:
            if saved[name] is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved[name]


class _GuestlistBlocker:
    """Meta path hook: guestlist unimportable with a fixed message."""

    def find_spec(self, name: str, _path=None, _target=None):  # type: ignore[no-untyped-def]
        if name == "guestlist" or name.startswith("guestlist."):
            raise ImportError("No module named 'guestlist' (blocked)")


@contextlib.contextmanager
def _no_guestlist():
    """guestlist unimportable, as when the Meta client is not installed."""
    names = [name for name in sys.modules
             if name == "guestlist" or name.startswith("guestlist.")]
    saved = {name: sys.modules.pop(name) for name in names}
    blocker = _GuestlistBlocker()
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        sys.meta_path.remove(blocker)
        sys.modules.update(saved)


def _ok_run(cmd: list[str], **_kw: object):
    return subprocess.CompletedProcess(cmd, 0, "", "")


def _probe_for(duration_s: float):
    def _fake(_path: object) -> dict:
        return {"format": {"duration": str(duration_s)},
                "streams": [{"codec_type": "audio"}]}
    return _fake


class JobFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="voice-tests-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.job = _write_job(self.root)

    def context(self) -> voice_mod.JobContext:
        return voice_mod.build_job_context(self.job)

    def untightened(self) -> voice_mod.JobContext:
        (self.job / "tighten.json").unlink()
        return voice_mod.build_job_context(self.job)


class ExtractTests(unittest.TestCase):
    """Item 1: extraction reuses transcribe; the §4.1 refusals fire first."""

    def test_command_matches_transcribe_element_for_element(self) -> None:
        src, wav = Path("memo.m4a"), Path("memo.wav")
        self.assertEqual(voice_mod.extract_audio_command(src, wav),
                         transcribe_mod.extract_wav_command(src, wav))

    def test_note_over_20_minutes_refuses(self) -> None:
        with mock.patch("director.voice.probe_media",
                        side_effect=_probe_for(1300.0)):
            with self.assertRaises(voice_mod.VoiceError) as caught:
                voice_mod.probe_note_duration(Path("long.m4a"))
        self.assertEqual(str(caught.exception),
                         "voice: note is 22 min; the limit is 20 min — "
                         "split it or trim the file")

    def test_note_without_audio_refuses(self) -> None:
        def _silent(_path: object) -> dict:
            return {"format": {"duration": "5.0"},
                    "streams": [{"codec_type": "video"}]}

        with mock.patch("director.voice.probe_media",
                        side_effect=_silent):
            with self.assertRaises(voice_mod.VoiceError) as caught:
                voice_mod.probe_note_duration(Path("muted.mp4"))
        self.assertEqual(
            str(caught.exception),
            "voice: no audio in muted.mp4; record a voice memo or a "
            "video with sound")


class TranscribeTests(unittest.TestCase):
    """Item 2: the fake diarizer; item 3: the Whisper fallback."""

    def test_meta_turns_come_back_in_seconds_with_speakers(self) -> None:
        turns = [
            {"startMs": 500, "endMs": 2500,
             "transcript": "use the other take here", "speaker": "S1"},
            {"startMs": 3000, "endMs": 5200,
             "transcript": "always open on the wide", "speaker": "S2"},
            {"startMs": 6000, "endMs": 7000, "transcript": "   ",
             "speaker": "S2"}]
        with _fake_meta(turns=turns):
            got, engine, spend = voice_mod.transcribe_wav(
                Path("note.wav"), duration_s=8.0)
        self.assertEqual(engine, "meta")
        self.assertEqual([(t.start_s, t.end_s, t.text, t.speaker)
                          for t in got],
                         [(0.5, 2.5, "use the other take here", "S1"),
                          (3.0, 5.2, "always open on the wide", "S2")])
        self.assertAlmostEqual(spend, 8.0 * RATE)

    def test_whisper_fallback_when_guestlist_is_missing(self) -> None:
        def _mlx(cmd: list[str], **_kw: object):
            out_dir = Path(cmd[cmd.index("--output-dir") + 1])
            stem = Path(cmd[1]).stem
            (out_dir / f"{stem}.json").write_text(json.dumps(
                {"segments": [
                    {"start": 1.0, "end": 3.0,
                     "text": " use the other take "},
                    {"start": 3.5, "end": 4.0, "text": "  "}]}))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with _no_guestlist(), \
                mock.patch("director.voice.run_command",
                           side_effect=_mlx):
            got, engine, spend = voice_mod.transcribe_wav(
                Path("note.wav"), duration_s=8.0)
        self.assertEqual(engine, "whisper")
        self.assertEqual([(t.start_s, t.end_s, t.text, t.speaker)
                          for t in got],
                         [(1.0, 3.0, "use the other take", "")])
        self.assertEqual(spend, 0.0)

    def test_neither_transcriber_names_both(self) -> None:
        with _no_guestlist(), \
                mock.patch("shutil.which", return_value=None):
            with self.assertRaises(voice_mod.VoiceError) as caught:
                voice_mod.transcribe_wav(Path("note.wav"),
                                         duration_s=8.0)
        self.assertEqual(
            str(caught.exception),
            "voice: cannot transcribe: the Meta client is unavailable "
            "(No module named 'guestlist' (blocked)) and mlx_whisper "
            "is not on PATH; install one of them, or type the note "
            "instead")


class EstimateTests(JobFixture):
    """Item 4: the §4.3 estimate, and the refusal before any spend."""

    def test_1200_seconds_costs_transcribe_plus_resolve(self) -> None:
        expected = (1200 * RATE + (1200 / 60 * 1200) * 1.25 / 1e6
                    + 8000 * 4.25 / 1e6)
        self.assertAlmostEqual(voice_mod.estimate_note_usd(1200),
                               expected)

    def test_over_cap_refuses_before_the_diarizer_runs(self) -> None:
        note = self.root / "memo.m4a"
        note.write_bytes(b"fake")
        with _fake_meta(turns=[]) as fakes, \
                mock.patch("director.voice.probe_note_duration",
                           return_value=30.0):
            with self.assertRaises(voice_mod.VoiceError) as caught:
                voice_mod.run(note_path=note, job_dir=self.job,
                              max_usd=0.01)
        self.assertIn("exceeds --max-usd $0.01", str(caught.exception))
        self.assertEqual(fakes.transcribe.calls, [])


class AnchorParseTests(unittest.TestCase):
    """Item 5: spoken timecodes and quote matching."""

    def test_spoken_timecodes(self) -> None:
        self.assertEqual(voice_mod.parse_spoken_timecode(
            "at about a minute twenty fix it"), 80.0)
        self.assertEqual(voice_mod.parse_spoken_timecode("cut at 1:20"),
                         80.0)
        self.assertEqual(voice_mod.parse_spoken_timecode(
            "make it under two minutes in"), 120.0)
        self.assertEqual(voice_mod.parse_spoken_timecode(
            "drop it at ninety seconds"), 90.0)
        self.assertIsNone(voice_mod.parse_spoken_timecode(
            "use the other take here"))

    def test_quote_matching(self) -> None:
        words = [voice_mod.Turn(0.0, 1.0, text)
                 for text in ("alpha beta green lagoon "
                              "delta epsilon green lagoon").split()]
        self.assertEqual(voice_mod.match_quote(words, "green lagoon"),
                         [2, 6])
        self.assertEqual(
            voice_mod.match_quote(words, "Alpha beta green lagoon."), [0])
        self.assertEqual(voice_mod.match_quote(words, "lagoon"), [])
        self.assertEqual(voice_mod.match_quote(words, "um uh"), [])


class AnchorVerifyTests(JobFixture):
    """Item 6: quotes and the playhead place notes; §6.3 decides."""

    def test_unique_quote_anchors_its_beat(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="where I say alpha beta green lagoon use the wide",
            heard="use the wide where you say alpha beta green lagoon",
            quote="alpha beta green lagoon",
            ops=[_op("set_angle", beat_id=-1, camera="B",
                     pause_limit_s=-1)])])
        self.assertEqual(note.anchor_kind, "quote")
        self.assertEqual(note.beat_id, 0)
        self.assertAlmostEqual(note.at_s or -1, 0.1)
        self.assertEqual(note.status, "resolved")
        op = note.ops[0]
        self.assertEqual(op["beat_id"], 0)
        self.assertFalse(op["blocked"])

    def test_quote_twice_is_ambiguous_with_both_candidates(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="where I say green lagoon swap the take",
            heard="swap the take at green lagoon",
            quote="green lagoon",
            ops=[_op("swap_take", beat_id=-1, attempt_id=0)])])
        self.assertEqual(note.status, "ambiguous")
        self.assertEqual([c["beat_id"] for c in note.candidates], [0, 1])
        self.assertTrue(all("green lagoon" in c["label"]
                            for c in note.candidates))
        self.assertTrue(note.ops[0]["blocked"])

    def test_candidate_pick_applies_the_beat_op(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="where I say green lagoon swap the take",
            heard="swap the take at green lagoon",
            quote="green lagoon",
            ops=[_op("swap_take", beat_id=-1, attempt_id=0)])])
        self.assertEqual(note.status, "ambiguous")
        result = voice_mod.apply_notes(
            job=job, notes=[note.as_dict()], pick=[0],
            candidates={"n1": 0}, source="voice")
        self.assertEqual(len(result["applied"]), 1)
        saved = json.loads((self.job / "tighten.json").read_text())
        self.assertEqual(saved["beats"][0]["pick_attempt"], 0)

    def test_playhead_overrules_a_wrong_spark_beat(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="use the other take here",
            heard="swap to the other take", beat_id=1, playhead_at=0.5,
            ops=[_op("swap_take", beat_id=1, attempt_id=0)])])
        self.assertEqual(note.beat_id, 0)
        self.assertEqual(note.anchor_kind, "playhead")
        self.assertIn("Heard as beat B1; the words/timecode point to "
                      "B0, used B0", note.message)
        self.assertEqual(note.ops[0]["beat_id"], 0)
        self.assertFalse(note.ops[0]["blocked"])

    def test_timecode_past_the_end_is_unresolved(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="at two minutes in drop this beat",
            heard="drop this beat at two minutes",
            ops=[_op("drop_beat", beat_id=-1)])])
        self.assertEqual(note.status, "unresolved")
        self.assertEqual(note.ops, [])
        self.assertEqual(note.message,
                         "You said 2:00, the cut is 0:03 long")


class ContractTests(unittest.TestCase):
    """Item 7: the developer prompt and the resolve schema."""

    def test_developer_prompt_warns_against_quiet_picks(self) -> None:
        self.assertIn("do not pick one quietly", voice_mod.DEVELOPER)

    def test_schema_op_enum_is_the_section_6_1_list(self) -> None:
        props = voice_mod.RESOLVE_SCHEMA["properties"]["notes"]["items"][
            "properties"]["ops"]["items"]["properties"]
        self.assertEqual(
            props["op"]["enum"],
            ["swap_take", "drop_beat", "restore_beat", "drop_span",
             "restore_span", "drop_words", "restore_words", "set_angle",
             "clear_angle", "set_pause_limit", "add_note", "add_taste",
             "retime_target"])
        self.assertIn("word_start", props)
        self.assertIn("word_end", props)

    def test_developer_prompt_names_the_phrase_cut(self) -> None:
        self.assertIn("drop_words", voice_mod.DEVELOPER)


class ResolveTests(JobFixture):
    """Item 8: one fake resolve call -> checked swap, split taste."""

    def test_swap_is_checked_and_taste_is_a_separate_op(self) -> None:
        note = self.root / "memo.m4a"
        note.write_bytes(b"fake")
        turns = [
            {"startMs": 500, "endMs": 2500,
             "transcript": "use the other take here", "speaker": "S1"},
            {"startMs": 3000, "endMs": 5000,
             "transcript": "always open on the wide", "speaker": "S1"}]
        reply = {"notes": [{
            "note_index": 1, "heard": "swap to the other take",
            "confidence": 0.9, "anchor_kind": "playhead", "at_s": 0.5,
            "quote": "", "beat_id": 1, "status": "resolved",
            "message": "",
            "ops": [_op("swap_take", beat_id=1, attempt_id=0)],
            "taste": ["always open on the wide"]}]}
        with _fake_meta(turns=turns, reply=reply) as fakes, \
                mock.patch("director.voice.probe_note_duration",
                           return_value=30.0), \
                mock.patch("director.voice.run_command",
                           side_effect=_ok_run):
            payload = voice_mod.run(note_path=note, job_dir=self.job,
                                    anchors=[0.5], source="live")
        self.assertEqual(len(fakes.spark.structured_calls), 1)
        (resolved,) = payload["notes"]
        self.assertEqual(resolved["source"], "live")
        self.assertEqual(resolved["engine"], "meta")
        swap, taste = resolved["ops"]
        self.assertEqual(swap["op"], "swap_take")
        self.assertEqual(swap["beat_id"], 0)
        self.assertFalse(swap["blocked"])
        self.assertEqual(swap["label"], "Swap B0 → A0")
        self.assertIn("Heard as beat B1", resolved["message"])
        self.assertEqual(taste["op"], "add_taste")
        self.assertFalse(taste["blocked"])
        self.assertEqual((self.job / "voice.json").exists(), True)


class VerifyEdgeTests(JobFixture):
    """Item 9: vanished attempts, the confidence floor, the cursor."""

    def test_vanished_attempt_blocks_with_cutlist_text(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="take nine please", heard="swap to take nine",
            beat_id=0, playhead_at=0.5,
            ops=[_op("swap_take", beat_id=0, attempt_id=9)])])
        self.assertEqual(note.status, "resolved")
        (op,) = note.ops
        self.assertTrue(op["blocked"])
        self.assertIn("No attempt A9 in beat B0",
                      op["checks"][0]["text"])

    def test_sub_floor_confidence_is_unresolved_with_heard(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="mumble take", heard="maybe swap the take",
            confidence=0.2, beat_id=0, playhead_at=0.5,
            ops=[_op("swap_take", beat_id=0, attempt_id=0)])])
        self.assertEqual(note.status, "unresolved")
        self.assertEqual(note.ops, [])
        self.assertEqual(note.message, voice_mod.UNPLACED_MESSAGE)
        self.assertEqual(note.heard, "maybe swap the take")

    def test_camera_reference_advances_from_the_previous_note(self) -> None:
        job = self.context()
        first, second = voice_mod.verify_resolved(job, [
            _entry(raw_text="where I say alpha beta green lagoon keep it",
                   heard="keep the opener",
                   quote="alpha beta green lagoon",
                   ops=[_op("add_note", text="keep")]),
            _entry(raw_text="go to the close-up",
                   heard="switch to the close-up",
                   ops=[_op("set_angle", beat_id=-1, camera="B")])])
        self.assertEqual(first.beat_id, 0)
        self.assertEqual(second.anchor_kind, "beat")
        self.assertEqual(second.beat_id, 1)
        self.assertIn("advanced from the previous note",
                      second.message)
        self.assertFalse(second.ops[0]["blocked"])


class RefusalTests(JobFixture):
    """Item 10: §5 coercions say so instead of guessing."""

    def test_mix_change_becomes_a_note_with_the_exact_message(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="turn the music down a bit",
            heard="lower the music", playhead_at=0.5, ops=[])])
        self.assertEqual(note.status, "resolved")
        (op,) = note.ops
        self.assertEqual(op["op"], "add_note")
        self.assertEqual(op["text"], "lower the music")
        self.assertFalse(op["blocked"])
        self.assertEqual(note.message, voice_mod.MIX_MESSAGE)

    def test_unknown_footage_becomes_a_note(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="use the drone shot here",
            heard="switch to the drone shot", beat_id=0,
            playhead_at=0.5,
            ops=[_op("set_angle", beat_id=0, camera="drone")])])
        self.assertEqual(note.status, "resolved")
        (op,) = note.ops
        self.assertEqual(op["op"], "add_note")
        self.assertEqual(note.message, voice_mod.NO_SHOT_MESSAGE)


class MixedNoteTests(JobFixture):
    """Item 11: take + note + retime + pause ops, taste split out."""

    def test_every_op_checked_and_taste_separated(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="swap it and shorten pauses",
            heard="swap the take, note it, retime, shorten pauses",
            beat_id=0, playhead_at=0.5,
            ops=[_op("swap_take", beat_id=0, attempt_id=0),
                 _op("add_note"),
                 _op("retime_target", target_s=240),
                 _op("set_pause_limit", pause_limit_s=0.4)],
            taste=["always open on the wide"])])
        self.assertEqual(note.status, "resolved")
        kinds = [op["op"] for op in note.ops]
        self.assertEqual(kinds, ["swap_take", "add_note",
                                 "retime_target", "set_pause_limit",
                                 "add_taste"])
        self.assertTrue(all(not op["blocked"] for op in note.ops))
        added = note.ops[1]
        self.assertEqual(added["at_s"], 0.5)
        self.assertEqual(added["text"],
                         "swap the take, note it, retime, shorten pauses")
        self.assertEqual(note.ops[2]["label"], "Make it under 4:00")
        self.assertEqual(note.ops[3]["label"],
                         "Pauses over 0.4s shortened")

    def test_untightened_angle_validates_against_the_shot(self) -> None:
        job = self.untightened()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="go to the iPhone angle",
            heard="switch to the iPhone angle", playhead_at=2.0,
            ops=[_op("set_angle", beat_id=-1, camera="B")])])
        self.assertEqual(note.status, "resolved")
        (op,) = note.ops
        self.assertFalse(op["blocked"])
        self.assertEqual(op["label"], "Camera B for beat B?")


class TasteTests(unittest.TestCase):
    """Item 12: the taste file round-trips; ids never reused."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="taste-tests-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_add_remove_never_reuses_ids(self) -> None:
        first = voice_mod.add_taste(self.root, "always open wide",
                                    source="voice", note_id="n1")
        second = voice_mod.add_taste(self.root, "never fade audio",
                                     source="typed")
        self.assertEqual([e["id"] for e in second["entries"]],
                         ["t1", "t2"])
        self.assertEqual(voice_mod.taste_entries(self.root),
                         ["always open wide", "never fade audio"])
        third = voice_mod.remove_taste(self.root, "t1")
        self.assertEqual([e["id"] for e in third["entries"]], ["t2"])
        fourth = voice_mod.add_taste(self.root, "music under -20",
                                     source="voice")
        self.assertEqual([e["id"] for e in fourth["entries"]],
                         ["t2", "t3"])

    def test_fifty_first_entry_refuses_with_the_exact_text(self) -> None:
        payload = {"version": 1, "entries": [
            {"id": f"t{n}", "text": f"rule {n}", "source": "typed"}
            for n in range(1, 51)]}
        (self.root / "taste.json").write_text(json.dumps(payload))
        with self.assertRaises(voice_mod.VoiceError) as caught:
            voice_mod.add_taste(self.root, "one more", source="voice")
        self.assertEqual(str(caught.exception),
                         "voice: the taste file already has 50 entries; "
                         "remove one in the app before adding more")

    def test_corrupt_file_loads_empty_with_warning(self) -> None:
        (self.root / "taste.json").write_text("{nope")
        payload, warnings = voice_mod.load_taste_with_warnings(self.root)
        self.assertEqual(payload, {"version": 1, "entries": []})
        self.assertEqual(warnings, [voice_mod.TASTE_CORRUPT_WARNING])


class UntightenedTests(JobFixture):
    """Item 13: untightened jobs take angles and notes, not swaps."""

    def test_swap_blocks_while_angle_validates(self) -> None:
        job = self.untightened()
        swapped, angled = voice_mod.verify_resolved(job, [
            _entry(raw_text="use the other take",
                   heard="swap the take", beat_id=0, playhead_at=0.5,
                   ops=[_op("swap_take", beat_id=0, attempt_id=0)]),
            _entry(raw_text="go to the iPhone angle",
                   heard="switch to the iPhone angle", playhead_at=2.0,
                   ops=[_op("set_angle", beat_id=-1, camera="B")])])
        self.assertTrue(swapped.ops[0]["blocked"])
        self.assertEqual(swapped.ops[0]["checks"][0]["text"],
                         voice_mod.NOT_TIGHTENED_MESSAGE)
        self.assertFalse(angled.ops[0]["blocked"])


class CliPrintTests(JobFixture):
    """Item 14: heard-first output, blocks, ambiguity, unresolved."""

    def _args(self, **over: object) -> argparse.Namespace:
        base = {"note": "memo.m4a", "job": str(self.job), "sync": None,
                "words": None, "tighten": None, "angles": None,
                "apply": False, "render": False,
                "model": "muse-spark-1.3", "max_usd": 2.0, "yes": True,
                "no_whisper": True}
        base.update(over)
        return argparse.Namespace(**base)

    def test_prints_what_it_heard_then_the_resolution(self) -> None:
        from director import cli as cli_mod

        payload = {
            "notes": [
                {"id": "n1", "raw_text": "use the other take",
                 "heard": "swap to the other take", "engine": "meta",
                 "status": "resolved", "message": "",
                 "ops": [
                     {"op": "swap_take", "beat_id": 0,
                      "label": "Swap B0 → A0", "blocked": False,
                      "why": "the note says so", "checks": []},
                     {"op": "swap_take", "beat_id": 0,
                      "label": "Swap B0 → A9", "blocked": True,
                      "why": "", "checks": [{"ok": False, "hard": True,
                                             "text": "No attempt A9"}]}],
                 "candidates": []},
                {"id": "n2", "raw_text": "swap it at green lagoon",
                 "heard": "swap at green lagoon", "engine": "meta",
                 "status": "ambiguous",
                 "message": "That could be 2 moments — pick one",
                 "ops": [], "candidates": [
                     {"beat_id": 0, "at_s": 0.1,
                      "label": "0.1s — alpha beta green lagoon"},
                     {"beat_id": 1, "at_s": 1.8,
                      "label": "1.8s — delta epsilon green lagoon"}]},
                {"id": "n3", "raw_text": "hmm", "heard": "hmm",
                 "engine": "whisper", "status": "unresolved",
                 "message": "I couldn't place that note in the cut",
                 "ops": [], "candidates": []}],
            "warnings": [], "applied": []}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("director.voice.probe_note_duration",
                        return_value=30.0), \
                mock.patch("director.cli.require_ffmpeg_binaries"), \
                mock.patch("director.voice.run",
                           return_value=payload) as ran, \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr",
                                                           err):
            cli_mod.command_voice(self._args())
        ran.assert_called_once()
        text = out.getvalue()
        self.assertIn('N1 [engine meta] "use the other take"', text)
        self.assertIn("I heard: swap to the other take", text)
        self.assertIn('→ B0 "the opener": Swap B0 → A0', text)
        self.assertIn("✗", text)
        self.assertIn("No attempt A9", text)
        self.assertIn("? That could be 2 moments — pick one", text)
        self.assertIn("• B0: 0.1s — alpha beta green lagoon", text)
        self.assertIn('N3 [engine whisper (local — nothing uploaded)] '
                      '"hmm"', text)
        self.assertIn("… I couldn't place that note in the cut", text)
        self.assertIn("voice: ≈$", err.getvalue())


class VisualTests(JobFixture):
    """Item 15: the one bounded picture per visual note."""

    def _visual_note(self) -> voice_mod.ResolvedNote:
        return voice_mod.ResolvedNote(
            id="n1", source="file",
            raw_text="when she points at the kitchen use the wide",
            heard="use the wide when she points at the kitchen",
            anchor_kind="quote", at_s=1.8, beat_id=1,
            status="resolved", message="",
            ops=[{**_op("set_angle", beat_id=1, camera="A"),
                  "blocked": False,
                  "checks": [{"ok": True, "hard": False,
                              "text": "Camera A covers beat B1"}],
                  "label": "Camera A for beat B1"}],
            candidates=[], engine="meta", usd=0.0)

    def test_confirmed_keeps_the_op_with_a_frame_check(self) -> None:
        (self.job / "cut.mp4").write_bytes(b"fake")
        image = {"confirmed": True, "camera": "A",
                 "description": "a hand pointing left"}
        with _fake_meta(image=image) as fakes, \
                mock.patch("director.voice.run_command",
                           side_effect=_ok_run):
            (note,) = [self._visual_note()]
            spend, warnings = voice_mod.confirm_visual_notes(
                self.context(), [note])
        self.assertEqual(len(fakes.spark.image_calls), 1)
        (op,) = note.ops
        self.assertEqual(op["op"], "set_angle")
        self.assertIn("Frame check: a hand pointing left",
                      [c["text"] for c in op["checks"]])
        self.assertEqual(warnings, [])
        self.assertAlmostEqual(spend, 0.0045)

    def test_unconfirmed_becomes_a_note(self) -> None:
        (self.job / "cut.mp4").write_bytes(b"fake")
        image = {"confirmed": False, "camera": "",
                 "description": "no pointing visible"}
        with _fake_meta(image=image), \
                mock.patch("director.voice.run_command",
                           side_effect=_ok_run):
            note = self._visual_note()
            voice_mod.confirm_visual_notes(self.context(), [note])
        (op,) = note.ops
        self.assertEqual(op["op"], "add_note")
        self.assertEqual(
            note.message,
            "I couldn't see that moment at 1.8s — noted instead of "
            "changed")

    def test_no_meta_client_keeps_the_op_with_a_soft_check(self) -> None:
        (self.job / "cut.mp4").write_bytes(b"fake")
        with _no_guestlist():
            note = self._visual_note()
            spend, _warnings = voice_mod.confirm_visual_notes(
                self.context(), [note])
        (op,) = note.ops
        self.assertEqual(op["op"], "set_angle")
        self.assertIn(voice_mod.FRAME_UNAVAILABLE,
                      [c["text"] for c in op["checks"]])
        self.assertEqual(spend, 0.0)


class RunTests(JobFixture):
    """Item 16: transcribe then resolve over a tiny note file."""

    def _reply(self) -> dict:
        return {"notes": [
            {"note_index": 1, "heard": "swap to the other take",
             "confidence": 0.9, "anchor_kind": "none", "at_s": -1,
             "quote": "alpha beta green lagoon", "beat_id": 0,
             "status": "resolved", "message": "",
             "ops": [_op("swap_take", beat_id=0, attempt_id=0)],
             "taste": []},
            {"note_index": 2, "heard": "note the demo",
             "confidence": 0.9, "anchor_kind": "none", "at_s": -1,
             "quote": "delta epsilon green lagoon", "beat_id": 1,
             "status": "resolved", "message": "",
             "ops": [_op("add_note", text="the demo lands")],
             "taste": []}]}

    def test_transcribe_then_resolve_writes_voice_json(self) -> None:
        note = self.root / "memo.m4a"
        note.write_bytes(b"fake")
        turns = [
            {"startMs": 500, "endMs": 1500,
             "transcript": "where I say alpha beta green lagoon swap it",
             "speaker": "S1"},
            {"startMs": 4000, "endMs": 5000,
             "transcript": "where I say delta epsilon green lagoon note it",
             "speaker": "S1"}]
        with _fake_meta(turns=turns, reply=self._reply()) as fakes, \
                mock.patch("director.voice.probe_note_duration",
                           return_value=30.0), \
                mock.patch("director.voice.run_command",
                           side_effect=_ok_run):
            payload = voice_mod.run(note_path=note, job_dir=self.job)
        self.assertEqual(fakes.transcribe.calls != [], True)
        self.assertEqual(len(fakes.spark.structured_calls), 1)
        self.assertEqual(payload["engine"], "meta")
        self.assertEqual(len(payload["turns"]), 2)
        first, second = payload["notes"]
        self.assertEqual(first["beat_id"], 0)
        self.assertEqual(first["ops"][0]["op"], "swap_take")
        self.assertFalse(first["ops"][0]["blocked"])
        self.assertEqual(second["beat_id"], 1)
        saved = json.loads((self.job / "voice.json").read_text())
        self.assertEqual(saved["notes"], payload["notes"])
        self.assertGreater(payload["spend_usd"], 0.0)

    def test_empty_transcript_is_one_unresolved_note(self) -> None:
        note = self.root / "memo.m4a"
        note.write_bytes(b"fake")
        with _fake_meta(turns=[], reply=self._reply()) as fakes, \
                mock.patch("director.voice.probe_note_duration",
                           return_value=30.0), \
                mock.patch("director.voice.run_command",
                           side_effect=_ok_run):
            payload = voice_mod.run(note_path=note, job_dir=self.job)
        self.assertEqual(fakes.spark.structured_calls, [])
        (only,) = payload["notes"]
        self.assertEqual(only["status"], "unresolved")
        self.assertEqual(only["message"], voice_mod.NO_SPEECH_FILE)
        saved = json.loads((self.job / "voice.json").read_text())
        self.assertEqual(saved["notes"], payload["notes"])


class QuoteRangeTests(unittest.TestCase):
    """VOICEFIX item 2: hyphen-tolerant quote matching to word ranges."""

    def test_hyphen_matches_split_words_both_ways(self) -> None:
        split = [voice_mod.Turn(0.0, 1.0, text)
                 for text in "It can also add B roll captions".split()]
        self.assertEqual(voice_mod.match_quote_ranges(split, "add B-roll"),
                         [(3, 6)])
        joined = [voice_mod.Turn(0.0, 1.0, text)
                  for text in ["It", "can", "also", "add", "B-roll,",
                               "captions"]]
        self.assertEqual(voice_mod.match_quote_ranges(joined, "add B roll"),
                         [(3, 5)])

    def test_single_hyphenated_word_is_enough_to_anchor(self) -> None:
        # "B-roll" normalises to two content tokens, clearing the floor.
        words = [voice_mod.Turn(0.0, 1.0, text)
                 for text in "get B roll in".split()]
        self.assertEqual(voice_mod.match_quote(words, "B-roll"), [1])
        self.assertEqual(voice_mod.match_quote_ranges(words, "B-roll"),
                         [(1, 3)])

    def test_ranges_are_word_index_pairs(self) -> None:
        words = [voice_mod.Turn(0.0, 1.0, text)
                 for text in ("alpha beta green lagoon "
                              "delta epsilon green lagoon").split()]
        self.assertEqual(voice_mod.match_quote_ranges(words, "green lagoon"),
                         [(2, 4), (6, 8)])
        self.assertEqual(voice_mod.match_quote(words, "green lagoon"),
                         [2, 6])
        # One content word is still not an anchor.
        self.assertEqual(voice_mod.match_quote_ranges(words, "lagoon"), [])


class BothMatchesTests(JobFixture):
    """VOICEFIX: "cut both places where I mention X" drops every match."""

    def _note(self, raw: str) -> dict:
        return _entry(
            raw_text=raw, heard=raw, quote="green lagoon",
            ops=[_op("drop_words", word_start=-1, word_end=-1)])

    def test_both_expands_to_one_drop_per_match(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [self._note(
            "Cut both places where I mention green lagoon")])
        self.assertEqual(note.status, "resolved")
        self.assertEqual([(op["word_start"], op["word_end"])
                          for op in note.ops], [(4, 6), (8, 10)])
        self.assertTrue(all(not op["blocked"] for op in note.ops))
        self.assertEqual(note.ops[0]["label"],
                         "Drop words 4–6 (“green lagoon.”)")

    def test_all_and_every_also_take_every_match(self) -> None:
        job = self.context()
        for raw in ("Cut all the places where I say green lagoon",
                    "Cut every place where I say green lagoon"):
            (note,) = voice_mod.verify_resolved(job, [self._note(raw)])
            self.assertEqual(note.status, "resolved")
            self.assertEqual(len(note.ops), 2)

    def test_apply_drops_both_phrases(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [self._note(
            "Cut both places where I mention green lagoon")])
        result = voice_mod.apply_notes(
            job=job, notes=[note.as_dict()], source="voice")
        self.assertEqual(len(result["applied"]), 2)
        saved = json.loads((self.job / "tighten.json").read_text())
        self.assertEqual(saved["word_exclusions"],
                         [{"word_start": 4, "word_end": 6,
                           "reason": "dropped by editor"},
                          {"word_start": 8, "word_end": 10,
                           "reason": "dropped by editor"}])
        self.assertLess(saved["duration_out_s"], 3.4)
        # No kept span keeps any dropped word's [start, end].
        kept = [w for w in json.loads(
            (self.job / "words.json").read_text())["words"]]
        for span in saved["kept_spans"]:
            for word in (kept[4], kept[5], kept[8], kept[9]):
                overlap = (min(span["end_s"], word["end"])
                           - max(span["start_s"], word["start"]))
                self.assertLessEqual(overlap, 0.0)

    def test_drop_span_with_a_matching_quote_becomes_drop_words(self) -> None:
        # The real note-2 Spark reply shape: drop_span plus the quote.
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="Cut both places where I mention green lagoon",
            heard="Cut both mentions of green lagoon",
            quote="green lagoon",
            ops=[_op("drop_span", span_id="")])])
        self.assertEqual(note.status, "resolved")
        self.assertEqual([op["op"] for op in note.ops],
                         ["drop_words", "drop_words"])
        self.assertTrue(all(not op["blocked"] for op in note.ops))

    def test_drop_span_without_a_quote_keeps_its_check(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="cut that span",
            heard="cut that span",
            ops=[_op("drop_span", span_id="")])])
        (op,) = note.ops
        self.assertTrue(op["blocked"])
        self.assertEqual(op["checks"][-1]["text"],
                         "No kept span  in this cut")


class AmbiguousWordTests(JobFixture):
    """VOICEFIX: one mention of matched words still asks which one."""

    def test_single_mention_asks_with_both_candidates(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="cut the part where I say green lagoon",
            heard="cut the part where you say green lagoon",
            quote="green lagoon",
            ops=[_op("drop_words", word_start=-1, word_end=-1)])])
        self.assertEqual(note.status, "ambiguous")
        self.assertEqual([(c["word_start"], c["word_end"])
                          for c in note.candidates], [(4, 6), (8, 10)])
        (op,) = note.ops
        self.assertTrue(op["blocked"])
        self.assertIn("Ambiguous — pick one of 2 moments",
                      op["checks"][-1]["text"])

    def test_candidate_pick_applies_that_range(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="cut the part where I say green lagoon",
            heard="cut the part where you say green lagoon",
            quote="green lagoon",
            ops=[_op("drop_words", word_start=-1, word_end=-1)])])
        result = voice_mod.apply_notes(
            job=job, notes=[note.as_dict()], pick=[0],
            candidates={"n1": 1}, source="voice")
        self.assertEqual(len(result["applied"]), 1)
        saved = json.loads((self.job / "tighten.json").read_text())
        self.assertEqual(saved["word_exclusions"],
                         [{"word_start": 8, "word_end": 10,
                           "reason": "dropped by editor"}])

    def test_quote_matching_nowhere_blocks_with_its_text(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="cut the part where I say purple planet",
            heard="cut the part where you say purple planet",
            quote="purple planet",
            ops=[_op("drop_words", word_start=-1, word_end=-1)])])
        (op,) = note.ops
        self.assertTrue(op["blocked"])
        self.assertIn("No words match", op["checks"][-1]["text"])


class CutEdgeAnchorTests(JobFixture):
    """VOICEFIX item 3: the opening and the end anchor angle notes."""

    def test_very_start_anchors_the_opening_beat(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="At the very start, use the drone camera",
            heard="Use the drone camera at the very start",
            ops=[_op("set_angle", beat_id=-1, camera="B")])])
        self.assertEqual(note.status, "resolved")
        self.assertEqual(note.anchor_kind, "beat")
        self.assertEqual(note.at_s, 0.0)
        self.assertEqual(note.beat_id, 0)
        (op,) = note.ops
        self.assertEqual(op["beat_id"], 0)
        self.assertFalse(op["blocked"])

    def test_misheard_quote_falls_through_to_the_start(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="At the very start where I say my mic use the wide",
            heard="Use the wide at the very start",
            quote="my microphone check",
            ops=[_op("set_angle", beat_id=-1, camera="B")])])
        self.assertEqual(note.status, "resolved")
        self.assertEqual(note.beat_id, 0)

    def test_at_the_end_anchors_the_last_beat(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="use the wide at the end",
            heard="Use the wide at the end",
            ops=[_op("set_angle", beat_id=-1, camera="A")])])
        self.assertEqual(note.status, "resolved")
        self.assertEqual(note.beat_id, 1)
        self.assertAlmostEqual(note.at_s or -1, 3.4)

    def test_relative_end_does_not_anchor(self) -> None:
        job = self.context()
        (note,) = voice_mod.verify_resolved(job, [_entry(
            raw_text="cut the end of that sentence",
            heard="cut the end of that sentence",
            ops=[_op("add_note", text="trim")])])
        self.assertIsNone(note.at_s)

    def test_shot_lines_name_their_cameras(self) -> None:
        job = self.context()
        prompt = voice_mod.resolve_prompt(job, [[voice_mod.Turn(
            0.0, 1.0, "use the drone camera")]], [None])
        self.assertIn("Apple iPhone", prompt)
        self.assertIn("Sony FX3", prompt)


if __name__ == "__main__":
    unittest.main()

