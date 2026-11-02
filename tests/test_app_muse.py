from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import muse  # noqa: E402


def project() -> dict:
    words = [["The", 10.0, 10.2], ["home", 10.2, 10.6], ["is", 10.7, 10.8], ["almost", 10.9, 11.3],
             ["2,400", 11.3, 12.0], ["square", 12.0, 12.4], ["feet,", 12.4, 12.9], ["with", 13.6, 13.8],
             ["three", 13.8, 14.1], ["to", 14.1, 14.2], ["four", 14.2, 14.6], ["bedrooms.", 14.6, 15.3]]
    return {
        "title": "Test tour", "duration": 60.0,
        "sections": [{"name": "Tour", "t": 0.0, "d": 60.0}],
        "talk": [{"id": "t1", "t": 0.0, "d": 60.0, "topic": "exterior", "src": "A.MP4", "in": 0.0}],
        "broll": [{"id": "b1", "tag": "R1", "src": "R1", "t": 5.0, "d": 3.0, "topic": "", "reason": ""},
                  {"id": "b2", "tag": "R2", "src": "R2", "t": 20.0, "d": 3.0, "topic": "", "reason": ""}],
        "cards": [{"id": "c1", "t": 11.3, "d": 4.0, "caption": "MODEL HOME", "lines": ["ALMOST 2,400 SQ FT"]}],
        "transcript": [{"t": 10.0, "e": 15.3, "text": " ".join(w[0] for w in words), "w": words}],
        "library": [{"tag": "R1", "used": True}, {"tag": "R2", "used": True},
                    {"tag": "D9", "used": False, "what": "drone over the lake", "best": [0.0, 6.0]},
                    {"tag": "V0", "used": False, "what": "people by the fence", "skip": True, "use": "skip: people"},
                    {"tag": "W3", "used": False, "what": "promo page"}],
        "neverUse": {"W3": "misleading promo"},
        "findings": [],
    }


def op(o: str, **kw) -> dict:
    return {"op": o, "target": "", "t": -1, "d": -1, "src": "", "lines": [], "caption": "", "why": "test", **kw}


class NumbersTests(unittest.TestCase):
    def test_reads_digits_words_and_thousands(self) -> None:
        self.assertEqual(muse.numbers_in("ALMOST 2,400 SQ FT"), {2400.0})
        self.assertEqual(muse.numbers_in("3-4 BEDROOMS"), {3.0, 4.0})
        self.assertEqual(muse.numbers_in("three to four bedrooms"), {3.0, 4.0})
        self.assertIn(512000.0, muse.numbers_in("about 512k"))


class CheckTests(unittest.TestCase):
    def test_card_numbers_must_be_said_nearby(self) -> None:
        p = project()
        ok = muse.validate(p, [op("add_card", t=13.8, d=3.0, lines=["3-4 BEDROOMS"])])[0]
        self.assertFalse(ok["blocked"])
        bad = muse.validate(p, [op("add_card", t=13.8, d=3.0, lines=["6 BEDROOMS"])])[0]
        self.assertTrue(bad["blocked"])
        self.assertIn("6", next(c["text"] for c in bad["checks"] if not c["ok"]))

    def test_cut_is_refused_but_still_checks_word_edges(self) -> None:
        p = project()
        inside = muse.validate(p, [op("cut_span", t=11.5, d=1.0)])[0]
        self.assertTrue(inside["blocked"])
        self.assertTrue(any("inside" in c["text"] and not c["ok"] for c in inside["checks"]))
        between = muse.validate(p, [op("cut_span", t=12.95, d=0.6)])[0]
        self.assertTrue(between["blocked"])
        self.assertEqual([c["text"] for c in between["checks"] if not c["ok"]], [muse.CUT_REFUSED])
        self.assertEqual(muse.CUT_REFUSED, "Render v1 can't cut talk yet — ask for a note instead")

    def test_inserts_keep_their_gap(self) -> None:
        p = project()
        touching = muse.validate(p, [op("move_broll", target="b2", t=8.02, d=2.0)])[0]
        self.assertTrue(touching["blocked"])
        clear = muse.validate(p, [op("move_broll", target="b2", t=8.2, d=2.0)])[0]
        self.assertFalse(clear["blocked"])

    def test_skip_and_never_use_shots_are_blocked(self) -> None:
        p = project()
        self.assertTrue(muse.validate(p, [op("swap_broll", target="b1", src="V0")])[0]["blocked"])
        self.assertTrue(muse.validate(p, [op("swap_broll", target="b1", src="W3")])[0]["blocked"])
        self.assertTrue(muse.validate(p, [op("swap_broll", target="b1", src="NOPE")])[0]["blocked"])
        self.assertFalse(muse.validate(p, [op("swap_broll", target="b1", src="D9")])[0]["blocked"])

    def test_unknown_target_is_blocked(self) -> None:
        self.assertTrue(muse.validate(project(), [op("drop_broll", target="b99")])[0]["blocked"])
        self.assertTrue(muse.validate(project(), [op("drop_card", target="b1")])[0]["blocked"])


class ApplyTests(unittest.TestCase):
    def test_edits_are_pending_and_reversible(self) -> None:
        base = project()
        edits = [{"id": "e1", "ops": [op("drop_broll", target="b1"), op("swap_broll", target="b2", src="D9")]},
                 {"id": "e2", "ops": [op("add_card", t=13.8, d=3.0, lines=["3-4 BEDROOMS"])]}]
        p = muse.apply(base, edits)
        self.assertEqual(p["pending"], 3)
        self.assertEqual(p["broll"][0]["pending"], "drop")
        self.assertEqual((p["broll"][1]["tag"], p["broll"][1]["was_tag"]), ("D9", "R2"))
        self.assertEqual(p["cards"][-1]["pending"], "add")
        self.assertEqual(muse.apply(base, edits[:1])["pending"], 2)  # undo = apply without the edit
        self.assertNotIn("pending", base["broll"][0])  # the base cut is never modified

    def test_context_lists_items_by_id(self) -> None:
        ctx = muse.context(project(), 11.0)
        self.assertIn("b1 BROLL 5.0-8.0 R1", ctx)
        self.assertIn('c1 CARD 11.3-15.3 "ALMOST 2,400 SQ FT"', ctx)
        self.assertIn("V0 [0.0-0.0] people by the fence · SKIP", ctx)


def sectioned() -> dict:
    """A cold open, then two talk sections: Tour 10-60 and Yard 60-100."""
    p = project()
    p["duration"] = 100.0
    p["sections"] = [{"name": "Cold open", "kind": "intro", "t": 0.0, "d": 10.0},
                     {"name": "Tour", "t": 10.0, "d": 50.0}, {"name": "Yard", "t": 60.0, "d": 40.0}]
    p["talk"] = [{"id": "t1", "t": 0.0, "d": 10.0, "topic": "Cold open", "src": "cold open", "in": 0, "section": "Cold open"},
                 {"id": "t2", "t": 10.0, "d": 50.0, "topic": "exterior", "src": "A.MP4", "in": 0.0, "section": "Tour"},
                 {"id": "t3", "t": 60.0, "d": 40.0, "topic": "yard", "src": "B.MP4", "in": 0.0, "section": "Yard"}]
    p["broll"] = [{"id": "b1", "tag": "R1", "src": "R1", "t": 20.0, "d": 3.0, "section": "Tour", "topic": "", "reason": ""},
                  {"id": "b2", "tag": "R2", "src": "R2", "t": 50.0, "d": 3.0, "section": "Tour", "topic": "", "reason": ""},
                  {"id": "b3", "tag": "R1", "src": "R1", "t": 70.0, "d": 3.0, "section": "Yard", "topic": "", "reason": ""}]
    p["library"] += [{"tag": "D1", "used": False, "what": "short drone", "dur": 2.0, "best": [0.0, 2.0]},
                     {"tag": "V5", "used": False, "what": "slow pan", "dur": 9.9099, "best": [13.0, 19.8]}]
    return p


def failing(r: dict) -> list[str]:
    return [c["text"] for c in r["checks"] if not c["ok"] and c["hard"]]


class RenderRuleTests(unittest.TestCase):
    def test_broll_stays_inside_one_section(self) -> None:
        p = sectioned()
        across = muse.validate(p, [op("move_broll", target="b2", t=58.0, d=4.0)])[0]
        self.assertEqual(failing(across), [muse.CROSSES])
        self.assertEqual(muse.CROSSES, "B-roll stays inside one section")
        elsewhere = muse.validate(p, [op("move_broll", target="b2", t=80.0, d=3.0)])[0]  # leaves its own section
        self.assertEqual(failing(elsewhere), [muse.CROSSES])
        self.assertFalse(muse.validate(p, [op("move_broll", target="b2", t=40.0, d=3.0)])[0]["blocked"])
        add_across = muse.validate(p, [op("add_broll", src="D9", t=58.5, d=2.0)])[0]
        self.assertIn(muse.CROSSES, failing(add_across))
        self.assertFalse(muse.validate(p, [op("add_broll", src="D9", t=30.0, d=3.0)])[0]["blocked"])

    def test_broll_keeps_off_section_edges(self) -> None:
        p = sectioned()
        near = muse.validate(p, [op("move_broll", target="b1", t=10.1, d=2.0)])[0]
        self.assertEqual(failing(near), ["Within 0.3 s of the edge of Tour"])
        self.assertFalse(muse.validate(p, [op("move_broll", target="b1", t=10.3, d=2.0)])[0]["blocked"])
        self.assertTrue(muse.validate(p, [op("move_broll", target="b2", t=56.0, d=3.8)])[0]["blocked"])

    def test_cold_open_is_finished(self) -> None:
        p = sectioned()
        self.assertEqual(muse.intro_end(p), 10.0)
        for o in (op("add_broll", src="D9", t=5.0, d=3.0), op("move_broll", target="b1", t=4.0, d=2.0),
                  op("add_card", t=9.0, d=3.0, lines=["3-4 BEDROOMS"]),
                  op("add_card", t=10.1, d=3.0, lines=["3-4 BEDROOMS"]), op("move_card", target="c1", t=2.0, d=3.0)):
            r = muse.validate(p, [o])[0]
            self.assertEqual(failing(r), [muse.COLD_OPEN], o)
        self.assertEqual(muse.COLD_OPEN, "The cold open is a finished piece")
        self.assertFalse(muse.validate(p, [op("add_note", t=3.0, why="ask about the drone")])[0]["blocked"])
        self.assertFalse(muse.validate(p, [op("add_card", t=13.8, d=3.0, lines=["3-4 BEDROOMS"])])[0]["blocked"])
        # a legacy project has no kinds: its cold open is the section holding the "cold open" talk item
        legacy = sectioned()
        del legacy["sections"][0]["kind"]
        self.assertEqual(muse.intro_end(legacy), 10.0)
        self.assertEqual(muse.intro_end(project()), 0.0)

    def test_shot_must_cover_its_span_at_half_speed(self) -> None:
        p = sectioned()
        short = muse.validate(p, [op("add_broll", src="D1", t=30.0, d=4.0)])[0]
        self.assertEqual(failing(short), ["D1 is too short for 4.0 s at 0.5× (2.0 s shot)"])
        self.assertFalse(muse.validate(p, [op("add_broll", src="D1", t=30.0, d=3.9)])[0]["blocked"])
        self.assertFalse(muse.validate(p, [op("swap_broll", target="b1", src="D1")])[0]["blocked"])  # b1 is 3.0 s

    def test_clean_range_is_in_onscreen_seconds_for_prerenders(self) -> None:
        p = sectioned()
        r = muse.validate(p, [op("add_broll", src="V5", t=30.0, d=6.5)])[0]
        fits = next(c for c in r["checks"] if c["text"].startswith("Fits"))
        self.assertTrue(fits["ok"])
        self.assertIn("3.4 s", fits["text"])
        self.assertFalse(r["blocked"])

    def test_card_text_is_never_a_path(self) -> None:
        p = sectioned()
        r = muse.validate(p, [op("edit_card", target="c1", lines=["ALMOST 2,400 SQ FT"], caption="tmp/v16.sh")])[0]
        self.assertTrue(r["blocked"])
        self.assertTrue(any(t.startswith("Looks like a file path") for t in failing(r)))
        self.assertFalse(muse.validate(p, [op("edit_card", target="c1", lines=["ALMOST 2,400 SQ FT"],
                                                caption="MODEL HOME")])[0]["blocked"])

    def test_an_added_insert_lasts_at_least_a_second(self) -> None:
        p = sectioned()
        short = muse.validate(p, [op("add_broll", src="D9", t=30.0, d=0.6)])[0]
        self.assertEqual(failing(short), ["An insert needs at least 1.0 s"])
        self.assertFalse(muse.validate(p, [op("add_broll", src="D9", t=30.0, d=1.0)])[0]["blocked"])

    def test_a_written_card_has_one_to_three_lines(self) -> None:
        p = sectioned()
        caption_only = muse.validate(p, [op("edit_card", target="c1", lines=[], caption="NEW CAPTION")])[0]
        self.assertEqual(failing(caption_only), ["A card needs 1–3 lines"])
        four = muse.validate(p, [op("add_card", t=13.8, d=3.0, lines=["3 BEDS", "4 BEDS", "LAKE", "DOCK"])])[0]
        self.assertEqual(failing(four), ["A card needs 1–3 lines"])
        long = muse.validate(p, [op("edit_card", target="c1", lines=["ALMOST 2,400 SQUARE FEET OF LIVING SPACE"])])[0]
        self.assertFalse(long["blocked"])  # the 32-character length stays a soft check
        self.assertTrue(any(not c["ok"] and not c["hard"] for c in long["checks"]))

    def test_a_card_ends_half_a_second_before_the_end(self) -> None:
        p = sectioned()  # 100 s
        p["cards"].append({"id": "c2", "t": 70.0, "d": 3.0, "caption": "", "lines": ["LAKE VIEW"]})
        end = "A card ends at least 0.5 s before the end (99.50 s)"
        self.assertEqual(failing(muse.validate(p, [op("add_card", t=97.5, d=2.4, lines=["LAKE VIEW"])])[0]), [end])
        self.assertFalse(muse.validate(p, [op("add_card", t=97.5, d=2.0, lines=["LAKE VIEW"])])[0]["blocked"])
        # an added card stays up at least 2 s, so a short one near the end still runs past the limit
        self.assertEqual(failing(muse.validate(p, [op("add_card", t=97.6, d=1.0, lines=["LAKE VIEW"])])[0]), [end])
        self.assertFalse(muse.validate(p, [op("add_card", t=97.0, d=1.0, lines=["LAKE VIEW"])])[0]["blocked"])
        self.assertEqual(failing(muse.validate(p, [op("move_card", target="c2", t=97.0, d=2.8)])[0]), [end])
        self.assertFalse(muse.validate(p, [op("move_card", target="c2", t=97.0, d=2.5)])[0]["blocked"])

    def test_preview_frame_rule(self) -> None:
        drone = {"tag": "D0292", "dur": 20.0, "best": [0.5, 17.0]}
        self.assertAlmostEqual(muse.preview_t(drone), 8.75)
        self.assertAlmostEqual(muse.preview_t(drone) - 0.5 * 4.9 / 2, 7.525)  # Render's in-point for a 4.9 s insert
        self.assertAlmostEqual(muse.preview_t({"tag": "V10", "dur": 9.9099, "best": [13.0, 19.8]}), 8.2)
        self.assertAlmostEqual(muse.preview_t({"tag": "P1", "dur": 6.006, "best": None}), 3.003)
        self.assertAlmostEqual(muse.preview_t({"tag": "W5", "dur": 3.244908, "best": [0, 0]}), 1.622454)
        self.assertEqual([muse.tag_k(t) for t in ("V10", "W7", "P1", "D0292", "R9934", "C", "")], [2, 2, 2, 1, 1, 1, 1])

    def test_shared_constants(self) -> None:
        self.assertEqual(muse.MIN_GAP, 0.05)
        self.assertEqual(muse.SPEED, 0.5)


if __name__ == "__main__":
    unittest.main()
