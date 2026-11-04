from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from director import cutlist
from director.angles import TimedWord


def _w(text: str, start: float, end: float) -> TimedWord:
    return TimedWord(text, start, end)


def _utt(uid: int, start: float, end: float, text: str,
         word_start: int, word_end: int) -> dict:
    return {"id": uid, "start_s": start, "end_s": end, "text": text,
            "word_start": word_start, "word_end": word_end}


def _att(aid: int, uids: list[int], start: float, end: float, text: str,
         verdict: str = "pick", kind: str = "keeper",
         reason: str = "clean", confidence: float = 0.9) -> dict:
    return {"id": aid, "utterance_ids": list(uids), "start_s": start,
            "end_s": end, "text": text, "verdict": verdict, "kind": kind,
            "reason": reason, "confidence": confidence}


def _beat(bid: int, topic: str, attempts: list[dict], pick: int = 0,
          status: str = "kept", priority: int = 3,
          reason: str = "clean") -> dict:
    return {"id": bid, "topic": topic, "status": status,
            "keep_priority": priority, "pick_attempt": pick,
            "reason": reason, "attempts": attempts}


def _payload(beats: list[dict], utterances: list[dict]) -> dict:
    return {"version": 1, "sync": "sync.json", "words": "words.json",
            "audio": {"label": "A", "clip_path": "a.mp4", "offset_s": 0.0},
            "method": "rule", "model": "muse-spark-1.3",
            "pause_limit_s": 0.8, "target_s": None, "spend_usd": 0.0,
            "warnings": [], "duration_in_s": 0.0, "duration_out_s": 0.0,
            "utterances": utterances, "beats": beats, "kept_spans": [],
            "removed_spans": [], "angle_overrides": [], "edits_applied": [],
            "span_exclusions": [], "editor_notes": [], "target": None}


class RecomputeTests(unittest.TestCase):
    def test_recompute_edges_never_cut_words(self) -> None:
        words = [_w("hello", 10.0, 10.4), _w("there.", 10.5, 11.0),
                 _w("oops", 12.0, 12.3), _w("sorry", 12.4, 12.8),
                 _w("welcome", 20.0, 20.4), _w("back.", 20.5, 21.0)]
        payload = _payload(
            [_beat(0, "greeting",
                   [_att(0, [0], 10.0, 11.0, "hello there.")]),
             _beat(1, "welcome",
                   [_att(0, [1], 12.0, 12.8, "oops sorry",
                           verdict="remove", kind="restart",
                           reason="abandons the line"),
                    _att(1, [2], 20.0, 21.0, "welcome back.")],
                   pick=1)],
            [_utt(0, 10.0, 11.0, "hello there.", 0, 2),
             _utt(1, 12.0, 12.8, "oops sorry", 2, 4),
             _utt(2, 20.0, 21.0, "welcome back.", 4, 6)])
        cutlist.recompute_spans(payload, words, pause_limit_s=3.0)
        kept = payload["kept_spans"]
        self.assertEqual(len(kept), 2)
        # Inter-word midpoints around the removed attempt, but never more than
        # EDGE_SILENCE_MAX_S of silence beside the kept words.
        self.assertAlmostEqual(kept[0]["end_s"], (11.0 + 12.0) / 2)
        self.assertAlmostEqual(kept[1]["start_s"],
                               max((12.8 + 20.0) / 2, 20.0 - cutlist.EDGE_SILENCE_MAX_S))
        # No kept span contains any part of a removed word's [start, end].
        for span in kept:
            for word in (words[2], words[3]):
                overlap = (min(span["end_s"], word.end_s)
                           - max(span["start_s"], word.start_s))
                self.assertLessEqual(overlap, 0.0)
        removed = payload["removed_spans"]
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["kind"], "restart")
        self.assertEqual(removed[0]["beat_id"], 1)
        self.assertEqual(removed[0]["attempt_id"], 0)

    def test_pause_trim_splits_and_keeps_breath(self) -> None:
        words = [_w("hello", 10.0, 10.4), _w("there.", 11.82, 12.2),
                 _w("friend.", 12.9, 13.2)]
        payload = _payload(
            [_beat(0, "greeting",
                   [_att(0, [0], 10.0, 13.2, "hello there. friend.")])],
            [_utt(0, 10.0, 13.2, "hello there. friend.", 0, 3)])
        cutlist.recompute_spans(payload, words, pause_limit_s=0.8)
        kept = payload["kept_spans"]
        self.assertEqual(len(kept), 2)
        self.assertAlmostEqual(kept[0]["end_s"], 10.4 + 0.18)
        self.assertAlmostEqual(kept[1]["start_s"], 11.82 - 0.17)
        removed = payload["removed_spans"]
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["kind"], "pause_trim")
        self.assertAlmostEqual(removed[0]["end_s"] - removed[0]["start_s"],
                               1.42 - 0.35)
        self.assertEqual(removed[0]["reason"],
                         "pause 1.42s shortened to 0.35s")
        retained = ((kept[0]["end_s"] - words[0].end_s)
                    + (words[1].start_s - kept[1]["start_s"]))
        self.assertAlmostEqual(retained, 0.35)
        # The 0.7 s gap is untouched: one span covers both words.
        self.assertLess(kept[1]["start_s"], words[1].start_s)
        self.assertGreater(kept[1]["end_s"], words[2].end_s)

    def test_removed_complement_tiles(self) -> None:
        words = [_w("one", 8.0, 8.4), _w("two.", 8.5, 9.0),
                 _w("oops", 10.0, 10.4),
                 _w("three", 15.0, 15.4), _w("four.", 18.4, 18.8),
                 _w("five.", 25.0, 25.4)]
        payload = _payload(
            [_beat(0, "counting",
                   [_att(0, [0], 8.0, 9.0, "one two.")]),
             _beat(1, "more",
                   [_att(0, [1], 10.0, 10.4, "oops",
                           verdict="remove", kind="flub", reason="stumble"),
                    _att(1, [2], 15.0, 18.8, "three four.")],
                   pick=1),
             _beat(2, "last",
                   [_att(0, [3], 25.0, 25.4, "five.")])],
            [_utt(0, 8.0, 9.0, "one two.", 0, 2),
             _utt(1, 10.0, 10.4, "oops", 2, 3),
             _utt(2, 15.0, 18.8, "three four.", 3, 5),
             _utt(3, 25.0, 25.4, "five.", 5, 6)])
        cutlist.recompute_spans(payload, words, pause_limit_s=0.8)
        kept, removed = payload["kept_spans"], payload["removed_spans"]
        # One removed span per inter-kept gap: the flub words, then the
        # trimmed pause inside the second pick. The 3 s pause leaves a
        # 2.65 s word-free middle, yet it classifies pause_trim rather
        # than dead_air: trimmed pauses outrank word-free gaps. The 6.2 s
        # silence before "five." is not kept whole any more: its middle is
        # removed as dead_air, leaving at most EDGE_SILENCE_MAX_S either side.
        self.assertEqual([s["kind"] for s in removed],
                         ["flub", "pause_trim", "dead_air"])
        trim = removed[1]
        self.assertGreater(trim["end_s"] - trim["start_s"], 2.0)
        flub = removed[0]
        self.assertEqual((flub["beat_id"], flub["attempt_id"]), (1, 0))
        # Kept + removed tile [first kept edge, last kept edge] exactly.
        tiled = sorted(kept + removed, key=lambda s: s["start_s"])
        self.assertAlmostEqual(tiled[0]["start_s"], kept[0]["start_s"])
        self.assertAlmostEqual(tiled[-1]["end_s"], kept[-1]["end_s"])
        for prev, span in zip(tiled, tiled[1:]):
            self.assertAlmostEqual(span["start_s"], prev["end_s"])
            self.assertLess(span["start_s"], span["end_s"])
        self.assertEqual([s["id"] for s in kept], ["K0", "K1", "K2", "K3"])
        self.assertEqual([s["id"] for s in removed], ["R0", "R1", "R2"])
        # The word-free branches, below and above the dead-air line.
        dead = cutlist._classify_gap(10.0, 13.5, {}, [], [], {}, {}, [])
        assert dead is not None
        self.assertEqual(dead["kind"], "dead_air")
        self.assertEqual(dead["beat_id"], -1)
        self.assertEqual(dead["reason"], "no words for 3.5s")
        short = cutlist._classify_gap(10.0, 11.0, {}, [], [], {}, {}, [])
        assert short is not None
        self.assertEqual(short["kind"], "unclassified")
        self.assertEqual(short["reason"], "not in the picked take")

    def test_min_span_merges_across_trim(self) -> None:
        words = [_w("oh", 9.6, 9.95), _w("yes.", 10.0, 10.1),
                 _w("indeed.", 11.1, 11.6),
                 _w("um", 29.9, 29.95), _w("tiny.", 30.0, 30.05),
                 _w("uh", 30.1, 30.15)]
        payload = _payload(
            [_beat(0, "agreeing",
                   [_att(0, [0], 9.6, 9.95, "oh",
                           verdict="remove", kind="flub", reason="stumble"),
                    _att(1, [1], 10.0, 11.6, "yes. indeed.")],
                   pick=1),
             _beat(1, "aside",
                   [_att(0, [2], 29.9, 29.95, "um",
                           verdict="remove", kind="flub", reason="stumble"),
                    _att(1, [3], 30.0, 30.05, "tiny."),
                    _att(2, [4], 30.1, 30.15, "uh",
                           verdict="remove", kind="flub", reason="stumble")],
                   pick=1)],
            [_utt(0, 9.6, 9.95, "oh", 0, 1),
             _utt(1, 10.0, 11.6, "yes. indeed.", 1, 3),
             _utt(2, 29.9, 29.95, "um", 3, 4),
             _utt(3, 30.0, 30.05, "tiny.", 4, 5),
             _utt(4, 30.1, 30.15, "uh", 5, 6)])
        cutlist.recompute_spans(payload, words, pause_limit_s=0.8)
        kept = [s for s in payload["kept_spans"] if s["beat_id"] == 0]
        # The 0.305 s fragment merges forward; the trim is undone.
        self.assertEqual(len(kept), 1)
        self.assertAlmostEqual(kept[0]["start_s"], (9.95 + 10.0) / 2)
        self.assertGreater(kept[0]["end_s"], 11.6)
        self.assertFalse(any(s["kind"] == "pause_trim"
                             for s in payload["removed_spans"]
                             if 9.0 < s["start_s"] < 12.0))
        # The lone tiny pick survives, and says so.
        lone = [s for s in payload["kept_spans"] if s["beat_id"] == 1]
        self.assertEqual(len(lone), 1)
        self.assertLess(lone[0]["end_s"] - lone[0]["start_s"], 0.35)
        self.assertIn(f"tighten_span_below_min:{lone[0]['id']}",
                      payload["warnings"])

    def test_time_mapping_round_trip(self) -> None:
        words = [_w("hello", 10.0, 10.4), _w("there.", 10.5, 11.0),
                 _w("oops", 12.0, 12.3),
                 _w("welcome", 20.0, 20.4), _w("back.", 20.5, 21.0)]
        payload = _payload(
            [_beat(0, "greeting",
                   [_att(0, [0], 10.0, 11.0, "hello there.")]),
             _beat(1, "welcome",
                   [_att(0, [1], 12.0, 12.3, "oops",
                           verdict="remove", kind="flub", reason="stumble"),
                    _att(1, [2], 20.0, 21.0, "welcome back.")],
                   pick=1)],
            [_utt(0, 10.0, 11.0, "hello there.", 0, 2),
             _utt(1, 12.0, 12.3, "oops", 2, 3),
             _utt(2, 20.0, 21.0, "welcome back.", 3, 5)])
        cutlist.recompute_spans(payload, words, pause_limit_s=3.0)
        for span in payload["kept_spans"]:
            for moment in (span["start_s"] + 0.01,
                           (span["start_s"] + span["end_s"]) / 2,
                           span["end_s"] - 0.01):
                mapped = cutlist.source_to_output(payload, moment)
                self.assertIsNotNone(mapped)
                assert mapped is not None
                self.assertAlmostEqual(
                    cutlist.output_to_source(payload, mapped), moment)
        removed_mid = (payload["removed_spans"][0]["start_s"]
                       + payload["removed_spans"][0]["end_s"]) / 2
        self.assertIsNone(cutlist.source_to_output(payload, removed_mid))
        playhead = cutlist.source_to_output(payload, 20.2)
        assert playhead is not None
        self.assertEqual(cutlist.beat_at_output(payload, playhead), 1)
        self.assertEqual(cutlist.beat_at_source(payload, 20.2), 1)
        self.assertIsNone(cutlist.beat_at_source(payload, 500.0))

    def test_load_write_round_trip(self) -> None:
        payload = _payload(
            [_beat(0, "topic", [_att(0, [0], 1.0, 2.0, "hi.")])],
            [_utt(0, 1.0, 2.0, "hi.", 0, 1)])
        payload["kept_spans"] = [
            {"id": "K0", "start_s": 0.95049, "end_s": 2.15049, "beat_id": 0,
             "attempt_id": 0, "utterance_ids": [0], "kind": "keeper",
             "reason": "clean"}]
        payload["removed_spans"] = []
        payload["spend_usd"] = 0.17119
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tighten.json"
            cutlist.write_tighten(path, payload)
            loaded = cutlist.load_tighten(path)
        self.assertEqual(loaded["kept_spans"][0]["start_s"], 0.95)
        self.assertEqual(loaded["kept_spans"][0]["end_s"], 2.15)
        self.assertEqual(loaded["spend_usd"], 0.1712)
        self.assertEqual(loaded["beats"][0]["attempts"][0]["confidence"], 0.9)
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text('{"version": 1}', encoding="utf-8")
            with self.assertRaises(ValueError):
                cutlist.load_tighten(bad)


class OperationTests(unittest.TestCase):
    def _two_beats(self) -> tuple[list[TimedWord], dict]:
        words = [_w("hello", 10.0, 10.4), _w("there.", 10.5, 11.0),
                 _w("oops", 12.0, 12.3),
                 _w("hello", 20.0, 20.4), _w("there.", 20.5, 21.0),
                 _w("welcome", 30.0, 30.4), _w("back.", 30.5, 31.0)]
        payload = _payload(
            [_beat(0, "greeting",
                   [_att(0, [0], 10.0, 11.0, "hello there."),
                    _att(1, [1], 12.0, 12.3, "oops",
                          verdict="remove", kind="flub", reason="stumbled"),
                    _att(2, [2], 20.0, 21.0, "hello there.",
                          verdict="alternate", kind="alternate",
                          reason="usable, flatter")]),
             _beat(1, "welcome",
                   [_att(0, [3], 30.0, 31.0, "welcome back.")])],
            [_utt(0, 10.0, 11.0, "hello there.", 0, 2),
             _utt(1, 12.0, 12.3, "oops", 2, 3),
             _utt(2, 20.0, 21.0, "hello there.", 3, 5),
             _utt(3, 30.0, 31.0, "welcome back.", 5, 7)])
        cutlist.recompute_spans(payload, words, pause_limit_s=3.0)
        return words, payload

    def test_apply_operations_swap_and_audit(self) -> None:
        words, payload = self._two_beats()
        before = [(s["start_s"], s["end_s"]) for s in payload["kept_spans"]]
        payload, checked = cutlist.apply_operations(
            payload, words, [{"op": "swap_take", "beat_id": 0,
                              "attempt_id": 2, "why": "use the other take"}],
            source="voice")
        self.assertEqual(len(checked), 1)
        self.assertFalse(checked[0]["blocked"])
        beat = payload["beats"][0]
        self.assertEqual(beat["pick_attempt"], 2)
        self.assertEqual(beat["attempts"][0]["verdict"], "alternate")
        self.assertEqual(beat["attempts"][0]["kind"], "alternate")
        self.assertEqual(beat["attempts"][2]["verdict"], "pick")
        # The kept spans moved to the new pick's words.
        starts = [s["start_s"] for s in payload["kept_spans"]]
        self.assertTrue(any(15.0 < s < 20.0 for s in starts))
        self.assertEqual(len(payload["edits_applied"]), 1)
        entry = payload["edits_applied"][0]
        self.assertEqual(entry["op"], "swap_take")
        self.assertEqual(entry["source"], "voice")
        self.assertEqual(entry["why"], "use the other take")
        # Swapping back restores the original kept spans exactly.
        payload, checked = cutlist.apply_operations(
            payload, words, [{"op": "swap_take", "beat_id": 0,
                              "attempt_id": 0}],
            source="voice")
        self.assertFalse(checked[0]["blocked"])
        after = [(s["start_s"], s["end_s"]) for s in payload["kept_spans"]]
        self.assertEqual(after, before)
        self.assertEqual(len(payload["edits_applied"]), 2)

    def test_operation_checks_block(self) -> None:
        words, payload = self._two_beats()
        snapshot = copy.deepcopy(payload)
        ops = [
            {"op": "swap_take", "beat_id": 0, "attempt_id": 1},
            {"op": "swap_take", "beat_id": 99, "attempt_id": 0},
            {"op": "drop_span", "span_id": "K0"},
            {"op": "set_pause_limit", "pause_limit_s": 5},
        ]
        payload, checked = cutlist.apply_operations(
            payload, words, ops, source="voice")
        self.assertEqual(len(checked), 4)
        self.assertTrue(all(op["blocked"] for op in checked))
        texts = [op["checks"][-1]["text"] for op in checked]
        self.assertEqual(texts[0], "Attempt A1 is marked flub: 'stumbled'")
        self.assertEqual(texts[1], "No beat B99 in this cut")
        self.assertEqual(texts[2], "That is the whole of beat B0 — "
                                   "drop the beat instead")
        self.assertEqual(texts[3],
                         "Pause limit 5s is outside 0.30–3.00s")
        self.assertEqual(payload["beats"], snapshot["beats"])
        self.assertEqual(payload["edits_applied"], [])
        self.assertEqual(
            [(s["start_s"], s["end_s"]) for s in payload["kept_spans"]],
            [(s["start_s"], s["end_s"]) for s in snapshot["kept_spans"]])

    def test_apply_operations_never_empties(self) -> None:
        words, payload = self._two_beats()
        payload, checked = cutlist.apply_operations(
            payload, words,
            [{"op": "drop_beat", "beat_id": 0},
             {"op": "drop_beat", "beat_id": 1}],
            source="voice")
        self.assertEqual(len(checked), 2)
        self.assertTrue(all(op["blocked"] for op in checked))
        for op in checked:
            self.assertIn("That would empty the cut",
                          [check["text"] for check in op["checks"]])
        self.assertTrue(all(b["status"] == "kept" for b in payload["beats"]))
        self.assertTrue(payload["kept_spans"])
        self.assertEqual(payload["edits_applied"], [])

    def test_drop_span_and_restore_span(self) -> None:
        words = [_w("one", 10.0, 10.4), _w("two.", 11.82, 12.2),
                 _w("three.", 20.0, 20.4)]
        payload = _payload(
            [_beat(0, "counting",
                   [_att(0, [0], 10.0, 12.2, "one two.")]),
             _beat(1, "more",
                   [_att(0, [1], 20.0, 20.4, "three.")])],
            [_utt(0, 10.0, 12.2, "one two.", 0, 2),
             _utt(1, 20.0, 20.4, "three.", 2, 3)])
        cutlist.recompute_spans(payload, words, pause_limit_s=0.8)
        self.assertEqual(len(payload["kept_spans"]), 3)
        payload, checked = cutlist.apply_operations(
            payload, words, [{"op": "drop_span", "span_id": "K1"}],
            source="editor")
        self.assertFalse(checked[0]["blocked"])
        self.assertEqual(len(payload["kept_spans"]), 2)
        kinds = [s["kind"] for s in payload["removed_spans"]]
        self.assertIn("editor_drop", kinds)
        payload, checked = cutlist.apply_operations(
            payload, words, [{"op": "restore_span", "span_id": "K1"}],
            source="editor")
        self.assertFalse(checked[0]["blocked"])
        self.assertEqual(len(payload["kept_spans"]), 3)
        self.assertEqual(payload["span_exclusions"], [])

    def test_set_angle_needs_coverage(self) -> None:
        words, payload = self._two_beats()
        payload, checked = cutlist.apply_operations(
            payload, words,
            [{"op": "set_angle", "beat_id": 0, "camera": "B"}],
            source="voice", covers={0: ["A"], 1: ["A", "B"]})
        self.assertTrue(checked[0]["blocked"])
        self.assertEqual(checked[0]["checks"][-1]["text"],
                         "Camera B does not cover beat B0")
        payload, checked = cutlist.apply_operations(
            payload, words,
            [{"op": "set_angle", "beat_id": 1, "camera": "B"}],
            source="voice", covers={0: ["A"], 1: ["A", "B"]})
        self.assertFalse(checked[0]["blocked"])
        self.assertEqual(payload["angle_overrides"],
                         [{"beat_id": 1, "camera": "B",
                           "reason": "editor: angle override"}])


class DropWordsTests(unittest.TestCase):
    """VOICEFIX: a phrase-level cut inside a kept span, and its restore."""

    def _broll(self) -> tuple[list[TimedWord], dict]:
        # Beat 0 keeps "It can also add B-roll, roll, captions too."
        # (one span); beat 1 keeps "Next beat." The dropped middle
        # mirrors the real "It can also add B-roll, roll," cut.
        words = [_w("It", 10.0, 10.3), _w("can", 10.4, 10.6),
                 _w("also", 10.7, 11.0), _w("add", 11.1, 11.4),
                 _w("B-roll,", 11.5, 11.9), _w("roll,", 12.0, 12.3),
                 _w("captions", 12.5, 12.9), _w("too.", 13.0, 13.3),
                 _w("Next", 20.0, 20.3), _w("beat.", 20.4, 20.7)]
        payload = _payload(
            [_beat(0, "what it does",
                   [_att(0, [0], 10.0, 13.3, "It can also add B-roll.")]),
             _beat(1, "next",
                   [_att(0, [1], 20.0, 20.7, "Next beat.")])],
            [_utt(0, 10.0, 13.3, "It can also add B-roll.", 0, 8),
             _utt(1, 20.0, 20.7, "Next beat.", 8, 10)])
        cutlist.recompute_spans(payload, words, pause_limit_s=3.0)
        self.assertEqual(len(payload["kept_spans"]), 2)
        return words, payload

    def test_drop_words_splits_span_with_midpoint_edges(self) -> None:
        words, payload = self._broll()
        before_out = payload["duration_out_s"]
        payload, checked = cutlist.apply_operations(
            payload, words,
            [{"op": "drop_words", "word_start": 3, "word_end": 6,
              "why": "cut the B-roll mention"}],
            source="voice")
        self.assertFalse(checked[0]["blocked"])
        kept = [s for s in payload["kept_spans"] if s["beat_id"] == 0]
        self.assertEqual(len(kept), 2)
        # Edges sit mid-gap, capped at EDGE_SILENCE_MAX_S of silence.
        self.assertAlmostEqual(kept[0]["end_s"], (11.0 + 11.1) / 2)
        self.assertAlmostEqual(kept[1]["start_s"], (12.3 + 12.5) / 2)
        # No kept piece contains any dropped word's [start, end].
        for span in kept:
            for word in (words[3], words[4], words[5]):
                overlap = (min(span["end_s"], word.end_s)
                           - max(span["start_s"], word.start_s))
                self.assertLessEqual(overlap, 0.0)
        dropped = [s for s in payload["removed_spans"]
                   if s["kind"] == "editor_drop"]
        self.assertEqual(len(dropped), 1)
        self.assertEqual((dropped[0]["beat_id"], dropped[0]["attempt_id"]),
                         (0, 0))
        self.assertEqual(dropped[0]["utterance_ids"], [0])
        # Kept + removed still tile exactly; the cut got shorter.
        tiled = sorted(payload["kept_spans"] + payload["removed_spans"],
                       key=lambda s: s["start_s"])
        for prev, span in zip(tiled, tiled[1:]):
            self.assertAlmostEqual(span["start_s"], prev["end_s"])
        self.assertLess(payload["duration_out_s"], before_out)
        self.assertEqual(len(payload["edits_applied"]), 1)
        self.assertEqual(payload["edits_applied"][0]["op"], "drop_words")

    def test_drop_then_restore_round_trip(self) -> None:
        words, payload = self._broll()
        before = [(s["start_s"], s["end_s"]) for s in payload["kept_spans"]]
        payload, checked = cutlist.apply_operations(
            payload, words,
            [{"op": "drop_words", "word_start": 3, "word_end": 6}],
            source="voice")
        self.assertFalse(checked[0]["blocked"])
        self.assertEqual(len(payload["kept_spans"]), 3)
        payload, checked = cutlist.apply_operations(
            payload, words,
            [{"op": "restore_words", "word_start": 3, "word_end": 6}],
            source="voice")
        self.assertFalse(checked[0]["blocked"])
        after = [(s["start_s"], s["end_s"]) for s in payload["kept_spans"]]
        self.assertEqual(after, before)
        self.assertEqual(payload["word_exclusions"], [])
        self.assertEqual(len(payload["edits_applied"]), 2)

    def test_drop_words_at_span_edge_leaves_one_piece(self) -> None:
        words, payload = self._broll()
        payload, checked = cutlist.apply_operations(
            payload, words,
            [{"op": "drop_words", "word_start": 0, "word_end": 3}],
            source="voice")
        self.assertFalse(checked[0]["blocked"])
        kept = [s for s in payload["kept_spans"] if s["beat_id"] == 0]
        self.assertEqual(len(kept), 1)
        self.assertAlmostEqual(kept[0]["start_s"], (11.0 + 11.1) / 2)

    def test_drop_words_checks_block(self) -> None:
        words, payload = self._broll()
        ops = [
            {"op": "drop_words", "word_start": 3.5, "word_end": 6},
            {"op": "drop_words", "word_start": 6, "word_end": 3},
            {"op": "drop_words", "word_start": 0, "word_end": 99},
            {"op": "drop_words", "word_start": 7, "word_end": 9},
            {"op": "drop_words", "word_start": 0, "word_end": 8},
            {"op": "restore_words", "word_start": 3, "word_end": 6},
        ]
        _payload2, checked = cutlist.apply_operations(
            copy.deepcopy(payload), words, ops, source="voice")
        self.assertEqual(len(checked), 6)
        self.assertTrue(all(op["blocked"] for op in checked))
        texts = [op["checks"][-1]["text"] for op in checked]
        self.assertIn("cuts inside a word", texts[0])
        self.assertEqual(texts[1], "Bad word range 6–3")
        self.assertIn("past the last word", texts[2])
        self.assertEqual(texts[3],
                         "Words 7–9 are not all in one kept span")
        self.assertEqual(texts[4], "That is the whole of beat B0 — "
                                   "drop the beat instead")
        self.assertEqual(texts[5], "No dropped words 3–6 to restore")

    def test_drop_words_blocks_a_sub_minimum_piece(self) -> None:
        words = [_w("ah", 10.0, 10.1), _w("cut", 10.3, 10.4),
                 _w("this.", 20.0, 20.4)]
        payload = _payload(
            [_beat(0, "short",
                   [_att(0, [0], 10.0, 20.4, "ah cut this.")])],
            [_utt(0, 10.0, 20.4, "ah cut this.", 0, 3)])
        cutlist.recompute_spans(payload, words, pause_limit_s=3.0)
        _payload2, checked = cutlist.apply_operations(
            payload, words,
            [{"op": "drop_words", "word_start": 1, "word_end": 2}],
            source="voice")
        self.assertTrue(checked[0]["blocked"])
        self.assertIn("under the 0.35s minimum",
                      checked[0]["checks"][-1]["text"])
        # Nothing applied: no exclusion, no audit entry.
        self.assertEqual(payload.get("word_exclusions", []), [])
        self.assertEqual(payload["edits_applied"], [])

    def test_drop_words_duplicate_is_a_noop_and_overlap_blocks(self) -> None:
        words, payload = self._broll()
        payload, checked = cutlist.apply_operations(
            payload, words,
            [{"op": "drop_words", "word_start": 3, "word_end": 6}],
            source="voice")
        self.assertFalse(checked[0]["blocked"])
        payload, checked = cutlist.apply_operations(
            payload, words,
            [{"op": "drop_words", "word_start": 3, "word_end": 6},
             {"op": "drop_words", "word_start": 5, "word_end": 7}],
            source="voice")
        self.assertFalse(checked[0]["blocked"])
        self.assertEqual(checked[0]["checks"][-1]["text"],
                         "Words 3–6 are already dropped")
        self.assertTrue(checked[1]["blocked"])
        self.assertIn("overlap the earlier drop",
                      checked[1]["checks"][-1]["text"])
        self.assertEqual(len(payload["word_exclusions"]), 1)

    def test_word_exclusions_survive_a_take_swap(self) -> None:
        # Word-keyed drops outlive span renumbering: swap the take away
        # and back and the drop is still there.
        words, payload = self._broll()
        payload, checked = cutlist.apply_operations(
            payload, words,
            [{"op": "drop_words", "word_start": 3, "word_end": 6}],
            source="voice")
        self.assertFalse(checked[0]["blocked"])
        self.assertEqual(len(payload["kept_spans"]), 3)
        payload["beats"][0]["status"] = "dropped_editor"
        cutlist.recompute_spans(payload, words, pause_limit_s=3.0)
        payload["beats"][0]["status"] = "kept"
        cutlist.recompute_spans(payload, words, pause_limit_s=3.0)
        self.assertEqual(len(payload["kept_spans"]), 3)
        self.assertEqual(len(payload["word_exclusions"]), 1)


if __name__ == "__main__":
    unittest.main()
