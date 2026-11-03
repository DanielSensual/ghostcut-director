from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

from director.angles import (
    Line,
    Pick,
    SyncClip,
    TimedWord,
    apply_checks,
    batch_prompt,
    batch_schema,
    covering,
    covers,
    cut_points,
    frame_command,
    frame_grid,
    load_timed_words,
    pick_with_muse,
    render_commands,
    rule_pick_for,
    rule_picks,
    split_lines,
)


def _clip(label: str, offset: float | None, duration: float, fps: float = 30.0) -> SyncClip:
    return SyncClip(label=label, path=f"/media/{label}.mp4", offset_s=offset,
                    fps=fps, width=1920, height=1080, duration_s=duration)


def _word(text: str, start: float, end: float) -> TimedWord:
    return TimedWord(text, start, end)


def _line(i: int, start: float, end: float, cameras: list[str], text: str = "hello world") -> Line:
    return Line(i, start, end, text, i, i + 1, list(cameras))


def _pick(line_id: int, camera: str | None, method: str = "muse") -> Pick:
    return Pick(line_id, camera, "medium" if camera else "", "test reason", method, [])


class LineTests(unittest.TestCase):
    def test_lines_break_on_sentence_and_pause(self) -> None:
        words = [_word("hello", 0.0, 0.3), _word("world.", 0.4, 0.7),
                 _word("next", 0.8, 1.0), _word("line", 1.1, 1.3)]
        lines = split_lines(words)
        self.assertEqual([l.text for l in lines], ["hello world.", "next line"])
        words = [_word("a", 0.0, 0.2), _word("b", 0.3, 0.5),
                 _word("c", 1.5, 1.7), _word("d", 1.8, 2.0)]
        self.assertEqual(len(split_lines(words)), 2)
        # a 0.6 s-exact gap does not start a line
        words = [_word("a", 0.0, 0.2), _word("b", 0.8, 1.0)]
        self.assertEqual(len(split_lines(words)), 1)

    def test_long_line_breaks_at_longest_pause(self) -> None:
        words: list[TimedWord] = []
        t = 0.0
        for i in range(21):
            words.append(_word(f"w{i}", t, t + 0.3))
            t += 0.3 + (0.5 if i == 10 else 0.15)
        lines = split_lines(words)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(l.end_s - l.start_s <= 8.0 for l in lines))
        # the split sits at the widest gap (after w10)
        self.assertEqual(lines[0].word_end, 11)

    def test_missing_word_end_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "words.json"
            p.write_text(json.dumps({"words": [
                {"text": "hello", "start": 1.0},
                {"text": "world", "start": 1.5, "end": 1.8},
                {"text": "bye", "start": 2.0},
            ]}), encoding="utf-8")
            out = load_timed_words(p)
        self.assertEqual([w.end_s for w in out], [1.5, 1.8, 2.3])


class CoverageTests(unittest.TestCase):
    def test_coverage_needs_whole_line_and_placement(self) -> None:
        line = _line(0, 10.0, 12.0, [])
        self.assertFalse(covers(_clip("A", 10.0, 1.9), line))
        self.assertFalse(covers(_clip("B", None, 100.0), line))
        self.assertTrue(covers(_clip("C", 10.0, 2.0), line))
        self.assertTrue(covers(_clip("D", 0.0, 12.0), line))

    def test_audio_only_clip_never_covers(self) -> None:
        line = _line(0, 10.0, 12.0, [])
        lav = SyncClip(label="A", path="/media/lav.wav", offset_s=0.0,
                       fps=0.0, width=0, height=0, duration_s=100.0)
        self.assertFalse(covers(lav, line))
        self.assertEqual(
            [c.label for c in covering([lav, _clip("B", 0.0, 100.0)], line)],
            ["B"])
        self.assertEqual(rule_pick_for(line, [lav, _clip("B", 0.0, 100.0)],
                                       "A"), "B")

    def test_cut_points_sit_between_words(self) -> None:
        words = [_word("hello", 0.0, 0.4), _word("world.", 0.5, 0.9),
                 _word("next", 1.5, 1.8), _word("line", 1.9, 2.2)]
        lines = split_lines(words)
        self.assertEqual(len(lines), 2)
        cuts = cut_points(lines, words)
        self.assertEqual(len(cuts), 1)
        self.assertGreater(cuts[0], 0.9)
        self.assertLess(cuts[0], 1.5)
        self.assertAlmostEqual(cuts[0], (0.9 + 1.5) / 2)


class MergeTests(unittest.TestCase):
    def _clips(self) -> list[SyncClip]:
        return [_clip("A", 0.0, 100.0), _clip("B", 0.0, 100.0), _clip("C", 0.0, 100.0)]

    def test_same_camera_lines_merge(self) -> None:
        clips = self._clips()
        lines = [_line(0, 0.0, 2.0, ["A", "B"]), _line(1, 2.5, 4.5, ["A", "B"]),
                 _line(2, 5.0, 7.0, ["A", "B"])]
        shots = apply_checks([_pick(0, "A"), _pick(1, "A"), _pick(2, "B")],
                             lines, clips, min_shot_s=1.0)
        self.assertEqual(len(shots), 2)
        self.assertEqual([s.line_ids for s in shots], [[0, 1], [2]])

    def test_min_shot_merges_into_longer_neighbour(self) -> None:
        clips = self._clips()
        lines = [_line(0, 0.0, 5.0, ["A", "B", "C"]), _line(1, 5.5, 6.5, ["A", "B", "C"]),
                 _line(2, 7.0, 10.0, ["A", "B", "C"])]
        shots = apply_checks([_pick(0, "A"), _pick(1, "B"), _pick(2, "C")],
                             lines, clips, min_shot_s=2.0)
        self.assertEqual(len(shots), 2)
        self.assertEqual(shots[0].camera, "A")
        self.assertEqual(sorted(shots[0].line_ids + shots[1].line_ids), [0, 1, 2])
        self.assertTrue(any("min_shot_merge" in o for s in shots for o in s.overrules))
        # tie joins the previous neighbour
        lines = [_line(0, 0.0, 3.0, ["A", "B", "C"]), _line(1, 3.5, 4.5, ["A", "B", "C"]),
                 _line(2, 5.0, 8.0, ["A", "B", "C"])]
        shots = apply_checks([_pick(0, "A"), _pick(1, "B"), _pick(2, "C")],
                             lines, clips, min_shot_s=2.0)
        self.assertEqual(shots[0].camera, "A")
        self.assertIn(1, shots[0].line_ids)

    def test_min_shot_merge_only_into_covering_camera(self) -> None:
        # A covers L0 only, C covers L1+L2; the short B shot must join C, not the longer A
        clips = [_clip("A", 0.0, 5.0), _clip("B", 0.0, 10.0), _clip("C", 5.0, 5.0)]
        lines = [_line(0, 0.0, 5.0, ["A", "B"]), _line(1, 5.5, 6.5, ["B", "C"]),
                 _line(2, 7.0, 10.0, ["B", "C"])]
        shots = apply_checks([_pick(0, "A"), _pick(1, "B"), _pick(2, "C")],
                             lines, clips, min_shot_s=2.0)
        self.assertEqual(len(shots), 2)
        joined = next(s for s in shots if 1 in s.line_ids)
        self.assertEqual(joined.camera, "C")
        # neither neighbour covers: the short shot is kept and noted
        clips = [_clip("A", 0.0, 5.0), _clip("B", 0.0, 10.0), _clip("C", 6.0, 4.0)]
        lines = [_line(0, 0.0, 4.0, ["A", "B"]), _line(1, 4.5, 5.5, ["B"]),
                 _line(2, 6.5, 9.0, ["B", "C"])]
        shots = apply_checks([_pick(0, "A"), _pick(1, "B"), _pick(2, "C")],
                             lines, clips, min_shot_s=2.0)
        kept = next(s for s in shots if s.line_ids == [1])
        self.assertEqual(kept.camera, "B")
        self.assertTrue(any(o.startswith("min_shot_kept") for o in kept.overrules))

    def test_single_camera_single_shot(self) -> None:
        clips = [_clip("A", 0.0, 100.0)]
        lines = [_line(0, 0.0, 8.0, ["A"]), _line(1, 8.5, 20.0, ["A"])]
        shots = apply_checks([_pick(0, "A"), _pick(1, "A")], lines, clips, min_shot_s=2.0)
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0].camera, "A")
        self.assertTrue(any(o.startswith("long_shot") for o in shots[0].overrules))


class RuleTests(unittest.TestCase):
    def test_rule_opens_on_reference_then_alternates(self) -> None:
        clips = [_clip("A", 0.0, 100.0), _clip("B", 0.0, 100.0)]
        lines = [_line(0, 0.0, 2.0, ["A", "B"]), _line(1, 2.5, 4.5, ["A", "B"]),
                 _line(2, 5.0, 7.0, ["A", "B"])]
        picks = rule_picks(lines, clips, "A", 2.0)
        self.assertEqual([p.camera for p in picks], ["A", "B", "A"])
        # a large min-shot keeps the open shot put
        picks = rule_picks(lines, clips, "A", 10.0)
        self.assertEqual([p.camera for p in picks], ["A", "A", "A"])

    def test_rule_forced_cut_when_camera_drops_out(self) -> None:
        clips = [_clip("A", 0.0, 3.0), _clip("B", 0.0, 100.0)]
        lines = [_line(0, 0.0, 2.0, ["A", "B"]), _line(1, 3.5, 5.5, ["B"])]
        picks = rule_picks(lines, clips, "A", 10.0)
        self.assertEqual([p.camera for p in picks], ["A", "B"])
        self.assertEqual(rule_pick_for(lines[1], clips, "A"), "B")


class OverruleTests(unittest.TestCase):
    def _one(self, cameras: list[str]) -> tuple[list[Line], list[SyncClip]]:
        return [_line(0, 0.0, 2.0, cameras)], [_clip("A", 0.0, 100.0), _clip("B", 0.0, 100.0)]

    def test_non_covering_muse_pick_is_overruled(self) -> None:
        lines, clips = self._one(["A"])
        shots = apply_checks([_pick(0, "B")], lines, clips, min_shot_s=1.0)
        self.assertEqual(shots[0].camera, "A")
        self.assertEqual(shots[0].method, "rule")
        self.assertTrue(any(o.startswith("not_covering:B->A") for o in shots[0].overrules))

    def test_unknown_muse_camera_is_overruled(self) -> None:
        lines, clips = self._one(["A"])
        shots = apply_checks([_pick(0, "Z")], lines, clips, min_shot_s=1.0)
        self.assertEqual(shots[0].camera, "A")
        self.assertTrue(any(o.startswith("unknown_camera:Z->") for o in shots[0].overrules))

    def test_no_camera_line_becomes_black_but_neighbours_do_not_stretch(self) -> None:
        clips = [_clip("A", 0.0, 100.0), _clip("B", 0.0, 100.0)]
        lines = [_line(0, 0.0, 2.0, ["A", "B"]), _line(1, 3.0, 5.0, []),
                 _line(2, 6.0, 8.0, ["A", "B"])]
        shots = apply_checks([_pick(0, "A"), _pick(1, None, "rule"), _pick(2, "B")],
                             lines, clips, min_shot_s=1.0)
        self.assertEqual([s.camera for s in shots], ["A", "black", "B"])
        self.assertLess(shots[0].end_s, 3.0)
        self.assertGreater(shots[2].start_s, 5.0)

    def test_black_shot_durations_sum_to_audio_span(self) -> None:
        clips = [_clip("A", 0.0, 100.0), _clip("B", 0.0, 100.0)]
        lines = [_line(0, 0.0, 2.0, ["A"]), _line(1, 3.0, 5.0, []),
                 _line(2, 6.0, 8.0, ["B"])]
        shots = apply_checks([_pick(0, "A"), _pick(1, None, "rule"), _pick(2, "B")],
                             lines, clips, min_shot_s=1.0)
        span = shots[-1].end_s - shots[0].start_s
        self.assertAlmostEqual(sum(s.end_s - s.start_s for s in shots), span)
        self.assertAlmostEqual(span, 8.0)


class SchemaTests(unittest.TestCase):
    def test_batch_schema_enum_is_per_line_coverage(self) -> None:
        lines = [_line(0, 0.0, 1.0, ["A", "B"]), _line(1, 1.5, 2.5, ["B"]),
                 _line(2, 3.0, 4.0, ["A", "B", "C"]), _line(3, 4.5, 5.5, ["A"])]
        coverage = {l.id: l.cameras for l in lines}
        schema = batch_schema(lines, coverage)
        picks = schema["properties"]["picks"]
        self.assertEqual(picks["minItems"], 4)
        self.assertEqual(picks["maxItems"], 4)
        for item, line in zip(picks["prefixItems"], lines):
            self.assertEqual(item["properties"]["camera"]["enum"], line.cameras)
            self.assertEqual(item["properties"]["line_id"]["enum"], [line.id])
        prompt = batch_prompt(lines, coverage)
        self.assertIn("L0", prompt)


class CommandTests(unittest.TestCase):
    def test_frame_command_shape(self) -> None:
        cmd = frame_command("/media/A.mp4", 3.5, Path("/tmp/L000_A.jpg"))
        self.assertIsInstance(cmd, list)
        self.assertTrue(all(isinstance(x, str) for x in cmd))
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))
        self.assertIn("scale=512:-2", " ".join(cmd))
        self.assertIn("-frames:v", cmd)

    def test_render_is_one_part_per_shot_with_uncut_audio(self) -> None:
        from director.angles import Shot
        clips = {"A": _clip("A", 0.0, 100.0), "B": _clip("B", 2.0, 100.0)}
        shots = [Shot(0, 2.0, 5.0, "A", clips["A"].path, 2.0, 5.0, [0], "medium", "ok", "muse", []),
                 Shot(1, 5.0, 6.0, "black", "", 0.0, 1.0, [1], "", "", "rule", ["no_camera"]),
                 Shot(2, 6.0, 9.0, "B", clips["B"].path, 4.0, 7.0, [2], "close", "ok", "muse", [])]
        parts, listing, final = render_commands(shots, clips, clips["A"], Path("/tmp/cut.mp4"))
        self.assertEqual(len(parts), 3)
        for cmd in parts:
            self.assertTrue(all(isinstance(x, str) for x in cmd))
            self.assertEqual(cmd.count("-i"), 1)  # one decoder open at a time
            self.assertIn("pad=1920:1080", " ".join(cmd))
        # 30 fps: 3 s, 1 s and 3 s are exactly 90, 30 and 90 frames
        self.assertEqual([cmd[cmd.index("-frames:v") + 1] for cmd in parts], ["90", "30", "90"])
        self.assertEqual(parts[0][parts[0].index("-ss") + 1], "2.000000")
        self.assertEqual(parts[2][parts[2].index("-ss") + 1], "4.000000")
        self.assertIn("color=c=black", " ".join(parts[1]))
        self.assertEqual(len(listing.strip().splitlines()), 3)
        # the join copies the picture and takes the audio clip uncut for the whole span
        self.assertIn("concat", final)
        self.assertEqual(final[final.index("-c:v") + 1], "copy")
        audio_at = final.index(clips["A"].path)
        self.assertEqual(final[audio_at - 3:audio_at - 1], ["-t", "7.000000"])
        self.assertEqual(final[audio_at - 5:audio_at - 3], ["-ss", "2.000000"])

    def test_cuts_land_on_whole_frames(self) -> None:
        # 29.97 fps and cut points at arbitrary times: every shot is a whole number of frames, so a long
        # concat can't drift against the uncut audio
        from director.angles import Shot
        edges = [3.0, 5.123, 7.777, 11.5, 13.04]
        shots = [Shot(i, a, b, "A", "/media/A.mp4", a, b, [i], "medium", "", "rule", [])
                 for i, (a, b) in enumerate(zip(edges, edges[1:]))]
        grid = frame_grid(shots, 29.97)
        self.assertEqual(grid[0], 3.0)
        for t in grid:
            frames = (t - 3.0) * 30000 / 1001
            self.assertAlmostEqual(frames, round(frames), places=6)
        for t, g in zip(edges, grid):
            self.assertLessEqual(abs(t - g), 0.5 * 1001 / 30000 + 1e-9)


class MergeAfterShortTests(unittest.TestCase):
    def test_short_shot_between_one_camera_leaves_one_shot(self) -> None:
        clips = [_clip("A", 0.0, 20.0), _clip("B", 0.0, 20.0)]
        lines = [_line(0, 0.0, 4.0, ["A", "B"]), _line(1, 4.5, 5.5, ["A", "B"]), _line(2, 6.0, 10.0, ["A", "B"])]
        shots = apply_checks([_pick(0, "A"), _pick(1, "B"), _pick(2, "A")], lines, clips, min_shot_s=2.0)
        self.assertEqual([(s.camera, s.line_ids) for s in shots], [("A", [0, 1, 2])])


class FakeMuseTests(unittest.TestCase):
    """The Muse path end to end with a stand-in for the guestlist client (no network)."""

    def setUp(self) -> None:
        self.calls: list[dict] = []
        calls = self.calls

        class Result:
            def __init__(self, data):
                self.data, self.usd, self.cached = data, 0.01, False

        def image_json(images, **kw):
            calls.append({"images": list(images), **kw})
            return Result({"picks": [
                {"line_id": 0, "camera": "B", "shot_size": "close", "reason": "eyes to lens"},
                {"line_id": 0, "camera": "A", "shot_size": "wide", "reason": "second answer"},
                {"line_id": 1, "camera": "C", "shot_size": "wide", "reason": "not a camera here"},
            ]})

        meta = types.ModuleType("guestlist.meta")
        meta.spark = types.SimpleNamespace(image_json=image_json)
        meta.cache = types.SimpleNamespace(guard=lambda usd: None)
        self.saved = {k: sys.modules.get(k) for k in ("guestlist", "guestlist.meta")}
        sys.modules["guestlist"] = types.ModuleType("guestlist")
        sys.modules["guestlist"].meta = meta
        sys.modules["guestlist.meta"] = meta

    def tearDown(self) -> None:
        for k, v in self.saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def test_images_are_named_and_bad_replies_are_overruled(self) -> None:
        clips = [_clip("A", 0.0, 30.0), _clip("B", 0.0, 30.0)]
        lines = [_line(0, 0.0, 3.0, ["A", "B"]), _line(1, 3.5, 7.0, ["A", "B"])]
        frames = {(l.id, c): Path(f"/frames/L{l.id}_{c}.jpg") for l in lines for c in l.cameras}
        picks, usd, _ = pick_with_muse(lines, clips, frames, model="m", frames_dir=Path("/frames"))
        prompt = self.calls[0]["prompt"]
        self.assertIn("1 = L0 camera A, 2 = L0 camera B, 3 = L1 camera A, 4 = L1 camera B", prompt)
        self.assertEqual(self.calls[0]["images"], [frames[(0, "A")], frames[(0, "B")],
                                                   frames[(1, "A")], frames[(1, "B")]])
        self.assertEqual((picks[0].camera, picks[0].overruled), ("B", ["duplicate_reply"]))
        shots = apply_checks(picks, lines, clips, min_shot_s=2.0)
        self.assertTrue(any(o.startswith("unknown_camera:C") for s in shots for o in s.overrules))
        self.assertAlmostEqual(usd, 0.01)


if __name__ == "__main__":
    unittest.main()
