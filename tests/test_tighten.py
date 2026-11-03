from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from director import angles as angles_mod
from director import cutlist
from director import tighten as tighten_mod
from director.angles import TimedWord
from director.tighten import Utterance, Window


def _w(text: str, start: float, end: float) -> TimedWord:
    return TimedWord(text, start, end)


def _u(uid: int, start: float, end: float, text: str,
       word_start: int, word_end: int) -> Utterance:
    return Utterance(uid, start, end, text, word_start, word_end)


def _write_sync(path: Path, clips: list[dict], reference: str) -> Path:
    path.write_text(json.dumps(
        {"reference": reference, "methods": ["waveform"], "clips": clips}),
        encoding="utf-8")
    return path


def _sync_clip(label: str, clip_path: str, offset: float | None,
               duration: float = 100.0, width: int = 1920,
               height: int = 1080, fps: float = 30.0) -> dict:
    return {"label": label, "offset_s": offset, "method": "waveform",
            "camera": {"path": clip_path, "fps": fps, "width": width,
                       "height": height, "duration_s": duration,
                       "has_audio": True},
            "estimates": [], "checks": [], "warnings": []}


def _write_words(path: Path, words: list[TimedWord]) -> Path:
    path.write_text(json.dumps(
        {"words": [{"word": w.text, "start": w.start_s, "end": w.end_s}
                   for w in words]}), encoding="utf-8")
    return path


@contextlib.contextmanager
def _fake_guestlist(video_json: Any):
    """A stand-in guestlist.meta in sys.modules (no network)."""
    calls: list[tuple] = []

    def _guard(estimate: float = 0.0) -> None:
        calls.append(("guard", estimate))

    def _video_json(video: Any, **kwargs: Any) -> Any:
        calls.append(("video_json", video, kwargs))
        if isinstance(video_json, Exception):
            raise video_json
        return video_json

    meta = types.ModuleType("guestlist.meta")
    meta.cache = types.SimpleNamespace(guard=_guard)
    meta.spark = types.SimpleNamespace(video_json=_video_json)
    package = types.ModuleType("guestlist")
    package.meta = meta
    saved = {key: sys.modules.get(key)
             for key in ("guestlist", "guestlist.meta")}
    sys.modules["guestlist"] = package
    sys.modules["guestlist.meta"] = meta
    try:
        yield calls
    finally:
        for key, module in saved.items():
            if module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = module


def _no_proxy() -> Any:
    return mock.patch.object(tighten_mod, "make_window_proxy",
                             lambda command: None)


class UtteranceTests(unittest.TestCase):
    def test_build_utterances_granularity(self) -> None:
        # A 1.3 s breath mid-sentence splits, punctuation or not.
        words = [_w("The", 0.0, 0.2), _w("cat", 0.3, 0.5),
                 _w("sat", 1.8, 2.0), _w("down.", 2.1, 2.3)]
        utterances = tighten_mod.build_utterances(words)
        self.assertEqual(len(utterances), 2)
        self.assertEqual(utterances[0].text, "The cat")
        self.assertEqual((utterances[0].word_start, utterances[0].word_end),
                         (0, 2))
        # A 7 s sentence splits at its longest pause under the 6 s cap.
        long_words: list[TimedWord] = []
        moment = 10.0
        for i in range(24):
            long_words.append(_w(f"w{i}", moment, moment + 0.2))
            moment += 0.2 + (0.5 if i == 10 else 0.1)
        utterances = tighten_mod.build_utterances(long_words)
        self.assertGreater(len(utterances), 1)
        self.assertTrue(all(u.end_s - u.start_s <= 6.0 for u in utterances))
        self.assertEqual(utterances[0].word_end, 11)
        # Utterances are as fine as angles lines: every line boundary is
        # an utterance boundary, and word indices partition the stream.
        mixed = words + [_w("Again.", moment, moment + 0.3)]
        lines = angles_mod.split_lines(mixed)
        utterances = tighten_mod.build_utterances(mixed)
        ends = {u.word_end for u in utterances}
        for line in lines[:-1]:
            self.assertIn(line.word_end, ends)
        covered = [(u.word_start, u.word_end) for u in utterances]
        flat = [i for start, end in covered for i in range(start, end)]
        self.assertEqual(flat, list(range(len(mixed))))
        with self.assertRaises(tighten_mod.TightenError):
            tighten_mod.build_utterances([])

    def test_build_windows_bounds_and_retreat(self) -> None:
        utterances = [_u(i, i * 3.9, i * 3.9 + 1.0, f"point {i} here.",
                         2 * i, 2 * i + 2) for i in range(130)]
        windows = tighten_mod.build_windows(utterances)
        self.assertGreater(len(windows), 1)
        owned: list[int] = []
        for window in windows:
            self.assertLessEqual(len(window.utterance_ids), 60)
            first = utterances[window.utterance_ids[0]]
            last = utterances[window.utterance_ids[-1]]
            self.assertLessEqual(last.end_s - first.start_s, 240.0)
            owned.extend(window.utterance_ids)
        self.assertEqual(sorted(owned), list(range(130)))
        # A window that would end 0.4 s before a 1 s gap retreats to end
        # before that gap.
        retreat = [_u(0, 0.0, 1.0, "a.", 0, 1),
                   _u(1, 1.3, 2.3, "b.", 1, 2),
                   _u(2, 2.6, 3.6, "c.", 2, 3),
                   _u(3, 3.9, 4.9, "d.", 3, 4),
                   _u(4, 5.2, 6.2, "e.", 4, 5),
                   _u(5, 7.2, 8.2, "f.", 5, 6),
                   _u(6, 8.6, 9.6, "g.", 6, 7),
                   _u(7, 9.9, 10.9, "h.", 7, 8),
                   _u(8, 11.2, 12.2, "i.", 8, 9)]
        with mock.patch.object(tighten_mod, "WINDOW_MAX_UTTERANCES", 6):
            windows = tighten_mod.build_windows(retreat)
        self.assertEqual(windows[0].utterance_ids, [0, 1, 2, 3, 4])
        self.assertEqual(windows[1].utterance_ids[0], 5)

    def test_window_prompt_and_schema(self) -> None:
        schema = tighten_mod.window_schema()
        self.assertEqual(schema["required"], ["summary", "beats"])
        self.assertFalse(schema["additionalProperties"])
        beat_schema = schema["properties"]["beats"]["items"]
        self.assertEqual(beat_schema["required"],
                         ["topic", "keep_priority", "pick_attempt",
                          "attempts"])
        attempt_schema = (beat_schema["properties"]["attempts"]["items"])
        self.assertEqual(
            attempt_schema["properties"]["verdict"]["enum"],
            ["pick", "alternate", "remove"])
        self.assertEqual(
            attempt_schema["properties"]["kind"]["enum"],
            ["keeper", "alternate", "flub", "restart", "crew_talk",
             "aside", "setup", "dead_air"])
        utterances = [_u(0, 1.0, 2.0, "hello there.", 0, 2),
                      _u(1, 2.5, 3.5, "welcome back.", 2, 4),
                      _u(2, 4.0, 5.0, "goodbye.", 4, 5)]
        window = Window(0, [0, 1, 2], 0.0, 6.0)
        prev = cutlist.Beat(
            3, "intro",
            [cutlist.Attempt(0, [0], 1.0, 2.0, "Hello world here",
                             "pick", "keeper", "clean", 0.9)],
            0, "kept", 5, "clean")
        prompt = tighten_mod.window_prompt(window, utterances, prev,
                                           ["Keep it punchy"])
        self.assertTrue(prompt.startswith(
            "Pick the takes in this window (utterances U0–U2)."))
        self.assertIn('Previous beat (already decided): "intro" — pick: '
                      '"Hello world here"', prompt)
        self.assertIn("Editor preference: Keep it punchy", prompt)
        self.assertIn("U1 [2.5-3.5] welcome back.", prompt)
        self.assertTrue(prompt.endswith(
            "The video starts 1.0s before U0; utterance times are on the "
            "recording's timeline, not the video's."))
        self.assertIn("never leave an utterance out", tighten_mod.DEVELOPER)


class VerifyTests(unittest.TestCase):
    def _twenty(self) -> tuple[list[TimedWord], list[Utterance]]:
        words: list[TimedWord] = []
        utterances: list[Utterance] = []
        for i in range(20):
            start = float(i * 2)
            words.append(_w(f"left{i}", start, start + 0.4))
            words.append(_w(f"right{i}.", start + 0.5, start + 0.9))
            utterances.append(_u(i, start, start + 0.9,
                                 f"left{i} right{i}.", 2 * i, 2 * i + 2))
        return words, utterances

    def test_verify_window_reply_repairs(self) -> None:
        words, utterances = self._twenty()
        window = Window(0, list(range(20)), 0.0, 40.0)

        def _attempt(first: int, last: int, verdict: str, kind: str,
                     reason: str = "r", confidence: float = 0.8) -> dict:
            return {"first_utterance": first, "last_utterance": last,
                    "verdict": verdict, "kind": kind, "reason": reason,
                    "confidence": confidence}

        reply = {"summary": "ok", "beats": [
            {"topic": "first", "keep_priority": 4, "pick_attempt": 0,
             "attempts": [_attempt(0, 0, "pick", "keeper", "clean", 0.9),
                          _attempt(0, 1, "pick", "keeper", "also", 0.8)]},
            {"topic": "second", "keep_priority": 3, "pick_attempt": 1,
             "attempts": [_attempt(2, 2, "remove", "keeper", "stumble", 0.7),
                          _attempt(3, 3, "pick", "keeper", "good", 0.85)]},
            {"topic": "rest", "keep_priority": 3, "pick_attempt": 0,
             "attempts": [_attempt(5, 19, "pick", "keeper", "fine", 0.9)]}]}
        beats, warnings = tighten_mod.verify_window_reply(
            reply, window, utterances)
        self.assertEqual(warnings, [])
        first, second, rest = beats
        trimmed = first.attempts[1]
        self.assertEqual(trimmed.utterance_ids, [1])
        self.assertIn("overlap_trimmed", trimmed.notes)
        self.assertEqual(trimmed.verdict, "alternate")
        self.assertEqual(trimmed.kind, "alternate")
        self.assertIn("extra_pick_demoted", trimmed.notes)
        coerced = second.attempts[0]
        self.assertEqual(coerced.kind, "flub")
        self.assertIn("kind_coerced", coerced.notes)
        pick = second.attempts[1]
        self.assertEqual(second.pick_attempt, 1)
        # U4 was unclaimed: it extends the pick's span but is excluded
        # from the pick's kept words.
        self.assertEqual(pick.utterance_ids, [3, 4])
        self.assertEqual(pick.excluded_utterance_ids, [4])
        self.assertEqual(pick.text, utterances[3].text)
        self.assertEqual(rest.pick_attempt, 0)
        payload = {"version": 1, "beats": [tighten_mod._beat_to_dict(b)
                                           for b in beats],
                   "utterances": [{"id": u.id, "start_s": u.start_s,
                                   "end_s": u.end_s, "text": u.text,
                                   "word_start": u.word_start,
                                   "word_end": u.word_end}
                                  for u in utterances],
                   "kept_spans": [], "removed_spans": [], "warnings": []}
        cutlist.recompute_spans(payload, words, pause_limit_s=0.8)
        dropped_mid = (words[8].start_s + words[8].end_s) / 2
        self.assertIsNone(cutlist.source_to_output(payload, dropped_mid))
        self.assertTrue(any(s["kind"] == "unclassified"
                            for s in payload["removed_spans"]))

    def test_verify_window_reply_fallback_threshold(self) -> None:
        words, utterances = self._twenty()
        window = Window(0, list(range(8)), 0.0, 17.0)
        owned = utterances[:8]
        reply = {"summary": "ok", "beats": [
            {"topic": "mess", "keep_priority": 3, "pick_attempt": 0,
             "attempts": [
                 {"first_utterance": 0, "last_utterance": 0,
                  "verdict": "pick", "kind": "keeper", "reason": "r",
                  "confidence": 0.5},
                 {"first_utterance": 100, "last_utterance": 103,
                  "verdict": "remove", "kind": "flub", "reason": "r",
                  "confidence": 0.5}]}]}
        beats, warnings = tighten_mod.verify_window_reply(
            reply, window, owned)
        self.assertEqual(warnings, ["tighten_window_rule_fallback:0"])
        self.assertTrue(all(b.reason.startswith("rule:") for b in beats))
        with self.assertRaises(tighten_mod.WindowReplyError):
            tighten_mod.verify_window_reply("garbage", window, owned)
        with self.assertRaises(tighten_mod.WindowReplyError):
            tighten_mod.verify_window_reply({"summary": "no beats"}, window,
                                            owned)

    def test_invalid_reply_falls_back_in_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sync = _write_sync(
                tmp_path / "sync.json",
                [_sync_clip("A", str(tmp_path / "a.mp4"), 0.0),
                 _sync_clip("B", str(tmp_path / "b.mp4"), 0.0)],
                str(tmp_path / "a.mp4"))
            words = _write_words(
                tmp_path / "words.json",
                [_w("hello", 1.0, 1.4), _w("there.", 1.5, 2.0)])
            out = tmp_path / "tighten.json"
            reply = types.SimpleNamespace(data="garbage", usd=0.5,
                                          cached=False)
            with _fake_guestlist(reply), _no_proxy():
                payload = tighten_mod.run(
                    sync_path=sync, words_path=words, out_path=out,
                    audio_label="A", pause_limit_s=0.8, target_s=None,
                    model="muse-spark-1.3", no_muse=False, max_usd=5.0)
        self.assertIn("tighten_window_reply_invalid:0",
                      payload["warnings"])
        self.assertEqual(payload["method"], "rule")
        self.assertEqual(payload["spend_usd"], 0.5)

    def test_speech_to_the_clip_end_still_reaches_spark(self) -> None:
        # Live test 2026-11-03: talk ran to the end of a 180 s clip, the window's
        # 1 s tail pad overran the video, and every window fell back to the rule.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sync = _write_sync(
                tmp_path / "sync.json",
                [_sync_clip("A", str(tmp_path / "a.mp4"), 0.0, duration=2.3)],
                str(tmp_path / "a.mp4"))
            words = _write_words(
                tmp_path / "words.json",
                [_w("hello", 1.0, 1.4), _w("there.", 1.5, 2.0)])
            commands: list[list[str]] = []
            reply = types.SimpleNamespace(data="garbage", usd=0.0, cached=False)
            with _fake_guestlist(reply) as calls, \
                    mock.patch.object(tighten_mod, "make_window_proxy",
                                      commands.append):
                payload = tighten_mod.run(
                    sync_path=sync, words_path=words,
                    out_path=tmp_path / "tighten.json",
                    audio_label="A", pause_limit_s=0.8, target_s=None,
                    model="muse-spark-1.3", no_muse=False, max_usd=5.0)
        self.assertNotIn("tighten_window_no_video:0", payload["warnings"])
        self.assertTrue(any(call[0] == "video_json" for call in calls))
        duration = float(commands[0][commands[0].index("-t") + 1])
        self.assertLessEqual(duration, 2.3 + 1e-6)


class RuleTests(unittest.TestCase):
    def test_rule_beats_keeps_last_full_pass(self) -> None:
        utterances = [
            _u(0, 0.0, 2.0, "The problem is timecode.", 0, 4),
            _u(1, 3.0, 4.5, "Again the problem is time", 4, 8),
            _u(2, 5.0, 7.0, "The problem is timecode.", 8, 12),
            _u(3, 8.0, 10.0, "We fix it in post.", 12, 16),
            _u(4, 11.0, 13.0, "We fix it in post.", 16, 20),
            _u(5, 14.0, 16.0, "It works great.", 20, 23),
            _u(6, 17.0, 19.0, "It works great.", 23, 26),
        ]
        beats = tighten_mod.rule_beats(utterances)
        self.assertEqual(len(beats), 3)
        first = beats[0]
        self.assertEqual(first.pick_attempt, 2)
        self.assertEqual(first.attempts[0].verdict, "alternate")
        fragment = first.attempts[1]
        self.assertEqual(fragment.verdict, "remove")
        self.assertEqual(fragment.kind, "restart")
        for beat in beats[1:]:
            self.assertEqual(len(beat.attempts), 2)
            self.assertEqual(beat.attempts[1].verdict, "pick")
            self.assertEqual(beat.attempts[0].verdict, "alternate")
        self.assertEqual(beats[0].keep_priority, 5)
        self.assertEqual(beats[1].keep_priority, 3)
        self.assertEqual(beats[2].keep_priority, 5)

    def test_rule_mode_warns_about_asides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sync = _write_sync(
                tmp_path / "sync.json",
                [_sync_clip("A", str(tmp_path / "a.mp4"), 0.0)],
                str(tmp_path / "a.mp4"))
            words = _write_words(
                tmp_path / "words.json",
                [_w("hello", 1.0, 1.4), _w("there.", 1.5, 2.0)])
            out = tmp_path / "tighten.json"
            payload = tighten_mod.run(
                sync_path=sync, words_path=words, out_path=out,
                audio_label="A", pause_limit_s=0.8, target_s=None,
                model="muse-spark-1.3", no_muse=True, max_usd=5.0)
        self.assertEqual(payload["method"], "rule")
        self.assertIn("tighten_rule_mode_keeps_asides",
                      payload["warnings"])


class BudgetTests(unittest.TestCase):
    def test_estimate_and_budget_gate(self) -> None:
        spark = tighten_mod.estimate_window_usd(100.0, "muse-spark-1.3")
        self.assertAlmostEqual(
            spark, 100.0 * 300 * 1.25 / 1e6 + 16000 * 4.25 / 1e6)
        wonder = tighten_mod.estimate_window_usd(100.0, "muse-wonder-1.0")
        self.assertAlmostEqual(
            wonder, 100.0 * 300 * 4.00 / 1e6 + 16000 * 20.00 / 1e6)
        self.assertAlmostEqual(
            tighten_mod.estimate_window_usd(100.0, "mystery-model"), spark)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sync = _write_sync(
                tmp_path / "sync.json",
                [_sync_clip("A", str(tmp_path / "a.mp4"), 0.0),
                 _sync_clip("B", str(tmp_path / "b.mp4"), 0.0)],
                str(tmp_path / "a.mp4"))
            words = _write_words(
                tmp_path / "words.json",
                [_w("hello", 1.0, 1.4), _w("there.", 1.5, 2.0)])
            out = tmp_path / "tighten.json"

            def _must_not_run(video: Any, **kwargs: Any) -> Any:
                raise AssertionError("Spark must not be called over budget")

            with _fake_guestlist(_must_not_run), _no_proxy():
                with self.assertRaises(tighten_mod.TightenError) as raised:
                    tighten_mod.run(
                        sync_path=sync, words_path=words, out_path=out,
                        audio_label="A", pause_limit_s=0.8,
                        target_s=None, model="muse-spark-1.3",
                        no_muse=False, max_usd=0.0)
        message = str(raised.exception)
        self.assertTrue(message.startswith("tighten: estimated Spark cost $"))
        self.assertIn("over --max-usd $0.00", message)
        self.assertIn("raise --max-usd or pass --no-muse", message)


class MuseCallTests(unittest.TestCase):
    def test_pick_window_with_fake_guestlist(self) -> None:
        utterances = [_u(0, 1.0, 2.0, "hello there.", 0, 2),
                      _u(1, 3.0, 4.0, "welcome back.", 2, 4)]
        window = Window(0, [0, 1], 0.0, 5.0)
        reply = {"summary": "two beats",
                 "beats": [
                     {"topic": "one", "keep_priority": 4,
                      "pick_attempt": 0,
                      "attempts": [{"first_utterance": 0,
                                    "last_utterance": 0, "verdict": "pick",
                                    "kind": "keeper", "reason": "clean",
                                    "confidence": 0.9}]},
                     {"topic": "two", "keep_priority": 3,
                      "pick_attempt": 0,
                      "attempts": [{"first_utterance": 1,
                                    "last_utterance": 1, "verdict": "pick",
                                    "kind": "keeper", "reason": "clean",
                                    "confidence": 0.9}]}]}
        proxy = Path("/tmp/window-000.mp4")
        with _fake_guestlist(types.SimpleNamespace(
                data=reply, usd=0.1712, cached=False)) as calls:
            data, usd = tighten_mod.pick_window_with_muse(
                proxy=proxy, window=window, utterances=utterances,
                prev_beat=None, taste=[], model="muse-spark-1.3",
                estimate_usd=0.2)
        self.assertEqual(data, reply)
        self.assertAlmostEqual(usd, 0.1712)
        kinds = [call[0] for call in calls]
        self.assertEqual(kinds, ["guard", "video_json"])
        sent_video, sent_kwargs = calls[1][1], calls[1][2]
        self.assertEqual(sent_video, proxy)
        self.assertEqual(sent_kwargs["name"], "tighten_picks")
        self.assertEqual(sent_kwargs["schema"],
                         tighten_mod.window_schema())
        self.assertEqual(sent_kwargs["max_output_tokens"],
                         tighten_mod.MAX_OUTPUT_TOKENS)
        self.assertEqual(sent_kwargs["model"], "muse-spark-1.3")
        # A failing call degrades that window to the rule, aloud.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sync = _write_sync(
                tmp_path / "sync.json",
                [_sync_clip("A", str(tmp_path / "a.mp4"), 0.0),
                 _sync_clip("B", str(tmp_path / "b.mp4"), 0.0)],
                str(tmp_path / "a.mp4"))
            words = _write_words(
                tmp_path / "words.json",
                [_w("hello", 1.0, 1.4), _w("there.", 1.5, 2.0)])
            out = tmp_path / "tighten.json"
            with _fake_guestlist(RuntimeError("boom")), _no_proxy():
                payload = tighten_mod.run(
                    sync_path=sync, words_path=words, out_path=out,
                    audio_label="A", pause_limit_s=0.8, target_s=None,
                    model="muse-spark-1.3", no_muse=False, max_usd=5.0)
        self.assertIn("tighten_window_failed:0:RuntimeError",
                      payload["warnings"])
        self.assertTrue(all(b["reason"].startswith("rule:")
                            for b in payload["beats"]))

    def test_proxy_command(self) -> None:
        self.assertEqual(
            tighten_mod.window_proxy_command(
                video_path="/v.mp4", video_in_s=2.5,
                audio_path="/a.wav", audio_in_s=180.246,
                duration_s=12.345, out=Path("/tmp/w.mp4")),
            ["ffmpeg", "-v", "error", "-y",
             "-ss", "2.500", "-i", "/v.mp4",
             "-ss", "180.246", "-i", "/a.wav",
             "-t", "12.345",
             "-map", "0:v:0", "-map", "1:a:0",
             "-vf", "fps=5,scale=-2:540",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "1",
             "-b:a", "64k",
             "-movflags", "+faststart", "/tmp/w.mp4"])


class TargetTests(unittest.TestCase):
    def _three_beats(self) -> tuple[list[TimedWord], dict]:
        words = [_w(f"w{i}", float(i), float(i) + 0.5) for i in range(30)]

        def _entry(uid: int, start: int, end: int) -> dict:
            return {"id": uid, "start_s": float(start),
                    "end_s": float(end) - 0.5, "text": f"u{uid}",
                    "word_start": start, "word_end": end}

        def _pick(aid: int, uid: int, start: int, end: int) -> dict:
            return {"id": aid, "utterance_ids": [uid],
                    "start_s": float(start), "end_s": float(end) - 0.5,
                    "text": f"u{uid}", "verdict": "pick", "kind": "keeper",
                    "reason": "r", "confidence": 0.9}

        payload = {
            "version": 1, "pause_limit_s": 3.0, "warnings": [],
            "duration_out_s": 0.0,
            "utterances": [_entry(0, 0, 5), _entry(1, 5, 15),
                           _entry(2, 15, 30)],
            "beats": [
                {"id": 0, "topic": "open", "status": "kept",
                 "keep_priority": 5, "pick_attempt": 0, "reason": "r",
                 "attempts": [_pick(0, 0, 0, 5)]},
                {"id": 1, "topic": "middle", "status": "kept",
                 "keep_priority": 2, "pick_attempt": 0, "reason": "r",
                 "attempts": [_pick(0, 1, 5, 15)]},
                {"id": 2, "topic": "later", "status": "kept",
                 "keep_priority": 2, "pick_attempt": 0, "reason": "r",
                 "attempts": [_pick(0, 2, 15, 30)]}],
            "kept_spans": [], "removed_spans": [],
        }
        cutlist.recompute_spans(payload, words, pause_limit_s=3.0)
        return words, payload

    def test_target_drops_whole_beats_only(self) -> None:
        words, payload = self._three_beats()
        before = payload["duration_out_s"]
        middle_before = [(s["start_s"], s["end_s"])
                         for s in payload["kept_spans"]
                         if s["beat_id"] == 1]
        tighten_mod.apply_target(payload, words, before - 5.0)
        target = payload["target"]
        self.assertEqual(target["dropped_beat_ids"], [2])
        self.assertTrue(target["met"])
        self.assertAlmostEqual(target["before_s"], before)
        self.assertEqual(payload["beats"][2]["status"], "dropped_target")
        self.assertEqual(payload["beats"][0]["status"], "kept")
        self.assertEqual(payload["beats"][1]["status"], "kept")
        # The surviving beat is untouched: whole beats only.
        middle_after = [(s["start_s"], s["end_s"])
                        for s in payload["kept_spans"]
                        if s["beat_id"] == 1]
        self.assertEqual(middle_after, middle_before)
        self.assertLessEqual(payload["duration_out_s"], before - 5.0)

    def test_target_unreachable_keeps_priority_five(self) -> None:
        words, payload = self._three_beats()
        tighten_mod.apply_target(payload, words, 1.0)
        target = payload["target"]
        self.assertFalse(target["met"])
        self.assertEqual(sorted(target["dropped_beat_ids"]), [1, 2])
        self.assertEqual(payload["beats"][0]["status"], "kept")
        self.assertTrue(any(w.startswith("tighten_target_unmet:")
                            and "over 1.0s" in w
                            for w in payload["warnings"]))

    def test_parse_target(self) -> None:
        self.assertEqual(tighten_mod.parse_target("3:00"), 180.0)
        self.assertEqual(tighten_mod.parse_target("1:02:03"), 3723.0)
        self.assertEqual(tighten_mod.parse_target("90"), 90.0)
        with self.assertRaises(ValueError) as raised:
            tighten_mod.parse_target("abc")
        self.assertEqual(str(raised.exception),
                         "Bad --target 'abc': use M:SS or seconds")
        with self.assertRaises(ValueError):
            tighten_mod.parse_target("1:99")


class RunTests(unittest.TestCase):
    def _two_pass_job(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        sync = _write_sync(
            tmp_path / "sync.json",
            [_sync_clip("A", str(tmp_path / "a.mp4"), 0.0),
             _sync_clip("B", str(tmp_path / "b.mp4"), 0.0)],
            str(tmp_path / "a.mp4"))
        words = _write_words(
            tmp_path / "words.json",
            [_w("Alpha", 0.5, 0.9), _w("beta", 1.0, 1.4),
             _w("gamma.", 1.5, 2.0),
             _w("Alpha", 10.5, 10.9), _w("beta", 11.0, 11.4),
             _w("gamma.", 11.5, 12.0)])
        return sync, words, tmp_path / "tighten.json"

    def test_run_end_to_end_words_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sync, words, out = self._two_pass_job(tmp_path)
            payload = tighten_mod.run(
                sync_path=sync, words_path=words, out_path=out,
                audio_label="A", pause_limit_s=0.8, target_s=None,
                model="muse-spark-1.3", no_muse=True, max_usd=5.0)
            reloaded = cutlist.load_tighten(out)
            self.assertEqual(reloaded["beats"], payload["beats"])
            self.assertEqual(len(payload["beats"]), 1)
            beat = payload["beats"][0]
            self.assertEqual(beat["pick_attempt"], 1)
            # Kept spans cover only second-pass words (indices 3-5).
            utterances = {u["id"]: u for u in payload["utterances"]}
            for span in payload["kept_spans"]:
                for uid in span["utterance_ids"]:
                    entry = utterances[uid]
                    self.assertGreaterEqual(entry["word_start"], 3)
            self.assertLess(payload["duration_out_s"],
                            payload["duration_in_s"])
            # Record an editor swap and a hand-added override, then
            # re-run: the override survives verbatim and the swap
            # re-applies against the fresh picks.
            timed = angles_mod.load_timed_words(words)
            payload, checked = cutlist.apply_operations(
                payload, timed,
                [{"op": "swap_take", "beat_id": 0, "attempt_id": 0}],
                source="editor")
            self.assertFalse(checked[0]["blocked"])
            payload["angle_overrides"] = [{"beat_id": 0, "camera": "B",
                                           "reason": "hand"}]
            cutlist.write_tighten(out, payload)
            rerun = tighten_mod.run(
                sync_path=sync, words_path=words, out_path=out,
                audio_label="A", pause_limit_s=0.8, target_s=None,
                model="muse-spark-1.3", no_muse=True, max_usd=5.0)
            self.assertEqual(rerun["angle_overrides"],
                             [{"beat_id": 0, "camera": "B",
                               "reason": "hand"}])
            self.assertEqual(rerun["beats"][0]["pick_attempt"], 0)
            self.assertTrue(any(e["op"] == "swap_take"
                                for e in rerun["edits_applied"]))

    def test_run_rejects_bad_pause_limit_and_empty_words(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sync = _write_sync(
                tmp_path / "sync.json",
                [_sync_clip("A", str(tmp_path / "a.mp4"), 0.0)],
                str(tmp_path / "a.mp4"))
            words = _write_words(tmp_path / "words.json",
                                 [_w("hi.", 1.0, 1.4)])
            with self.assertRaises(tighten_mod.TightenError) as raised:
                tighten_mod.run(
                    sync_path=sync, words_path=words,
                    out_path=tmp_path / "tighten.json", audio_label="A",
                    pause_limit_s=5.0, target_s=None,
                    model="muse-spark-1.3", no_muse=True, max_usd=5.0)
            self.assertEqual(str(raised.exception),
                             "tighten: pause limit 5.0s is outside "
                             "0.30–3.00s")
            empty = _write_words(tmp_path / "empty.json", [])
            with self.assertRaises(tighten_mod.TightenError) as raised:
                tighten_mod.run(
                    sync_path=sync, words_path=empty,
                    out_path=tmp_path / "tighten.json", audio_label="A",
                    pause_limit_s=0.8, target_s=None,
                    model="muse-spark-1.3", no_muse=True, max_usd=5.0)
            self.assertIn("tighten: no words in empty.json",
                          str(raised.exception))


class AnglesIntegrationTests(unittest.TestCase):
    def _angles_job(self, tmp_path: Path) -> tuple[Path, Path]:
        sync = _write_sync(
            tmp_path / "sync.json",
            [_sync_clip("A", str(tmp_path / "a.mp4"), 0.0),
             _sync_clip("B", str(tmp_path / "b.mp4"), 50.0, duration=50.0)],
            str(tmp_path / "a.mp4"))
        words = _write_words(
            tmp_path / "words.json",
            [_w("hello", 10.0, 11.0), _w("there.", 12.0, 13.0),
             _w("oops", 30.0, 31.0), _w("sorry", 32.0, 33.0),
             _w("welcome", 60.0, 61.0), _w("back.", 62.0, 63.0)])
        return sync, words

    def _tighten_for_job(self, tmp_path: Path) -> Path:
        timed = [_w("hello", 10.0, 11.0), _w("there.", 12.0, 13.0),
                 _w("oops", 30.0, 31.0), _w("sorry", 32.0, 33.0),
                 _w("welcome", 60.0, 61.0), _w("back.", 62.0, 63.0)]

        def _entry(uid: int, start: int, end: int) -> dict:
            return {"id": uid, "start_s": timed[start].start_s,
                    "end_s": timed[end - 1].end_s,
                    "text": " ".join(w.text for w in timed[start:end]),
                    "word_start": start, "word_end": end}

        def _attempt(aid: int, uid: int, verdict: str, kind: str,
                     reason: str) -> dict:
            entry = _entry(uid, 2 * uid, 2 * uid + 2)
            return {"id": aid, "utterance_ids": [uid],
                    "start_s": entry["start_s"], "end_s": entry["end_s"],
                    "text": entry["text"], "verdict": verdict, "kind": kind,
                    "reason": reason, "confidence": 0.9}

        payload = {
            "version": 1, "pause_limit_s": 3.0, "warnings": [],
            "duration_out_s": 0.0,
            "audio": {"label": "A", "clip_path": str(tmp_path / "a.mp4"),
                      "offset_s": 0.0},
            "utterances": [_entry(0, 0, 2), _entry(1, 2, 4),
                           _entry(2, 4, 6)],
            "beats": [
                {"id": 0, "topic": "start", "status": "kept",
                 "keep_priority": 5, "pick_attempt": 0, "reason": "clean",
                 "attempts": [_attempt(0, 0, "pick", "keeper", "clean"),
                              _attempt(1, 1, "remove", "flub", "stumble")]},
                {"id": 1, "topic": "end", "status": "kept",
                 "keep_priority": 5, "pick_attempt": 0, "reason": "clean",
                 "attempts": [_attempt(0, 2, "pick", "keeper", "clean")]}],
            "kept_spans": [], "removed_spans": [],
            "angle_overrides": [{"beat_id": 0, "camera": "B",
                                 "reason": "editor"},
                                {"beat_id": 1, "camera": "B",
                                 "reason": "editor"}],
            "edits_applied": [], "span_exclusions": [], "editor_notes": [],
        }
        cutlist.recompute_spans(payload, timed, pause_limit_s=3.0)
        out = tmp_path / "tighten.json"
        cutlist.write_tighten(out, payload)
        return out

    def test_angles_without_tighten_unchanged(self) -> None:
        from director.angles import Shot
        clips = {"A": angles_mod.SyncClip(
            label="A", path="/media/A.mp4", offset_s=0.0, fps=30.0,
            width=1920, height=1080, duration_s=100.0)}
        shots = [Shot(0, 2.0, 5.0, "A", "/media/A.mp4", 2.0, 5.0, [0],
                      "medium", "ok", "muse", []),
                 Shot(1, 5.0, 9.0, "A", "/media/A.mp4", 5.0, 9.0, [1],
                      "medium", "ok", "muse", [])]
        parts, _listing, final = angles_mod.render_commands(
            shots, clips, clips["A"], Path("/tmp/cut.mp4"))
        self.assertEqual(len(parts), len(shots))
        for command in parts:
            self.assertEqual(command.count("-i"), 1)
        joined = " ".join(final)
        self.assertIn("parts.txt", joined)
        self.assertNotIn("audio_parts.txt", joined)
        self.assertIn("-ss", final)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sync, words = self._angles_job(tmp_path)
            plain = angles_mod.run(
                sync_path=sync, words_path=words,
                out_path=tmp_path / "plain.json", audio_label="A",
                render_path=None, min_shot_s=2.0, model="muse-spark-1.3",
                no_muse=True)
            explicit = angles_mod.run(
                sync_path=sync, words_path=words,
                out_path=tmp_path / "explicit.json", audio_label="A",
                render_path=None, min_shot_s=2.0, model="muse-spark-1.3",
                no_muse=True, tighten_path=None)
            self.assertEqual(plain, explicit)
            self.assertNotIn("tighten", plain)
            self.assertNotIn("source_start_s", plain["shots"][0])

    def test_angles_with_tighten_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sync, words = self._angles_job(tmp_path)
            tighten_path = self._tighten_for_job(tmp_path)
            payload = angles_mod.run(
                sync_path=sync, words_path=words,
                out_path=tmp_path / "angles.json", audio_label="A",
                render_path=None, min_shot_s=2.0, model="muse-spark-1.3",
                no_muse=True, tighten_path=tighten_path)
            tighten = cutlist.load_tighten(tighten_path)
        shots = payload["shots"]
        self.assertGreater(len(shots), 1)
        # Output times on the shots, source times recorded.
        self.assertAlmostEqual(shots[0]["start_s"], 0.0)
        for shot in shots:
            for key in ("source_start_s", "source_end_s", "kept_span_id",
                        "beat_id"):
                self.assertIn(key, shot)
        # No shot crosses removed material: every source range sits
        # inside a single kept span.
        for shot in shots:
            self.assertTrue(any(
                span["start_s"] <= shot["source_start_s"]
                and shot["source_end_s"] <= span["end_s"]
                for span in tighten["kept_spans"]))
        # Output shots tile contiguously.
        for prev, shot in zip(shots, shots[1:]):
            self.assertAlmostEqual(shot["start_s"], prev["end_s"])
        # Lines stay in source time.
        self.assertAlmostEqual(payload["lines"][0]["start_s"], 10.0)
        self.assertEqual(payload["tighten"]["beats"], 2)
        # Beat 1's override (B covers it) sticks; beat 0's (B starts at
        # 50 s) is overruled by the coverage check.
        beat_shots = {shot["beat_id"]: shot for shot in shots}
        self.assertEqual(beat_shots[1]["camera"], "B")
        self.assertIn("angle_override:B1", beat_shots[1]["overrules"])
        self.assertEqual(beat_shots[0]["camera"], "A")
        self.assertIn("angle_override:B0", beat_shots[0]["overrules"])
        self.assertTrue(any(o.startswith("not_covering:B->")
                            for o in beat_shots[0]["overrules"]))
        # A tighten file from another job fails loudly.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sync, words = self._angles_job(tmp_path)
            foreign = self._tighten_for_job(tmp_path)
            data = json.loads(foreign.read_text(encoding="utf-8"))
            data["audio"]["label"] = "Z"
            foreign.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError) as raised:
                angles_mod.run(
                    sync_path=sync, words_path=words,
                    out_path=tmp_path / "angles.json", audio_label="A",
                    render_path=None, min_shot_s=2.0,
                    model="muse-spark-1.3", no_muse=True,
                    tighten_path=foreign)
            self.assertIn("tighten.json does not match this job's words",
                          str(raised.exception))

    def test_audio_part_command(self) -> None:
        self.assertEqual(
            angles_mod.audio_part_command("/a.wav", 12.5, 3.0,
                                          Path("/o.m4a")),
            ["ffmpeg", "-v", "error", "-y",
             "-ss", "12.500000", "-t", "3.000000",
             "-i", "/a.wav", "-map", "0:a:0",
             "-af", ("afade=t=in:st=0:d=0.008,"
                     "afade=t=out:st=2.992000:d=0.008,aresample=48000"),
             "-c:a", "aac", "-b:a", "192k", "/o.m4a"])
        short = angles_mod.audio_part_command("/a.wav", 0.0, 0.005,
                                              Path("/o.m4a"))
        self.assertIn("afade=t=out:st=0.000000:d=0.008", " ".join(short))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "needs ffmpeg and ffprobe")
class SyntheticRenderTests(unittest.TestCase):
    def test_tightened_render_synthetic(self) -> None:
        from ghostcut.ffmpeg_tools import extract_media_stats, probe_media

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for name in ("a.mp4", "b.mp4"):
                done = subprocess.run(
                    ["ffmpeg", "-v", "error", "-y",
                     "-f", "lavfi", "-i",
                     "testsrc2=size=320x180:rate=30:duration=16",
                     "-f", "lavfi", "-i", "sine=frequency=440:duration=16",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-shortest", str(tmp_path / name)],
                    capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, msg=done.stderr)
            sync = _write_sync(
                tmp_path / "sync.json",
                [_sync_clip("A", str(tmp_path / "a.mp4"), 0.0,
                            duration=16.0, width=320, height=180),
                 _sync_clip("B", str(tmp_path / "b.mp4"), 0.0,
                            duration=16.0, width=320, height=180)],
                str(tmp_path / "a.mp4"))
            words = _write_words(
                tmp_path / "words.json",
                [_w("hello", 2.0, 2.4), _w("world.", 2.5, 3.0),
                 _w("hello", 12.0, 12.4), _w("world.", 12.5, 13.0)])
            tighten_path = tmp_path / "tighten.json"
            tighten_mod.run(
                sync_path=sync, words_path=words, out_path=tighten_path,
                audio_label="A", pause_limit_s=0.8, target_s=None,
                model="muse-spark-1.3", no_muse=True, max_usd=5.0)
            tighten = cutlist.load_tighten(tighten_path)
            self.assertEqual(len(tighten["beats"]), 1)
            self.assertEqual(tighten["beats"][0]["pick_attempt"], 1)
            angles_mod.run(
                sync_path=sync, words_path=words,
                out_path=tmp_path / "angles.json", audio_label="A",
                render_path=tmp_path / "cut.mp4", min_shot_s=0.5,
                model="muse-spark-1.3", no_muse=True,
                tighten_path=tighten_path)
            kept_s = sum(s["end_s"] - s["start_s"]
                         for s in tighten["kept_spans"])
            master = extract_media_stats(
                probe_media(tmp_path / "cut.mp4"))
            self.assertAlmostEqual(float(master["duration_s"]), kept_s,
                                   delta=1.0 / 30.0 + 0.02)
            probe = probe_media(tmp_path / "cut.mp4")
            fallback = float(master["duration_s"])
            durations = {}
            for stream in probe.get("streams", []):
                try:
                    durations[stream.get("codec_type")] = float(
                        stream.get("duration") or fallback)
                except (TypeError, ValueError):
                    continue
            self.assertIn("video", durations)
            self.assertIn("audio", durations)
            self.assertLess(abs(durations["video"] - durations["audio"]),
                            0.05)
            removed_mid = (2.0 + 2.4) / 2
            self.assertIsNone(
                cutlist.source_to_output(tighten, removed_mid))


if __name__ == "__main__":
    unittest.main()
