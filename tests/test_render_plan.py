from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
import render_plan as rp  # noqa: E402

LEGACY = [0.0, 12.813, 212.713]
PIECES = {"comm": [1481, 1784, 8715], "seat": [1836, 12804]}  # + 1 held frame per talk section
PLACED = {
    "comm": [("C1", 7.0, 4.5, 2.0), ("W4", 63.05, 2.637, 0.0), ("D5", 145.7, 4.9, 10.0), ("V10", 156.05, 3.537, 7.0),
             ("X9", 159.587, 3.0, 0.0)],  # X9 touches V10 (gap 0) as some v16 inserts do
    "seat": [("W7", 49.5, 12.7, 0.0), ("R2", 100.0, 4.5, 1.0)],
}
DUR = {"C1": 15.5, "W4": 3.495158, "D5": 68.48, "V10": 9.9099, "X9": 6.0, "W7": 6.498158, "R2": 10.0, "D2": 17.96,
       "SK": 9.0, "NV": 9.0, "SH": 2.0}
BEST = {"D2": [0.5, 17.0], "V10": [13.0, 19.8], "D5": [2.0, 66.0], "C1": [1.0, 14.0]}
CARDS = [
    {"caption": "BY THE BUILDER", "size": 76, "until": 39.6, "lines": [["THE PARK", 36.0]]},
    {"caption": "AMENITIES", "until": 190.6, "lines": [["POOL", 185.05], ["GYM", 186.35], ["PLAYGROUND", 187.45],
                                                      ["DOG PARK", 188.4]]},
    {"caption": "DEPENDS ON SIZE + LOCATION", "until": 375.21, "lines": [["LOT PREMIUMS  $0 – $250,000", 368.26]]},
]


def piece_key(p: dict) -> str:
    zoom = float(p.get("zoom", 1.0))
    return hashlib.sha1(f"{p['source']}|{p['src_in']}|{p['src_out']}|{zoom}".encode()).hexdigest()[:12]


def build(tmp: Path, cards: list[dict] | None = None) -> tuple[dict, dict]:
    """A synthetic job (cold open + two talk sections) and the frame counts its fake probe returns."""
    frames = {str(tmp / "intro.mp4"): 768}
    sections = [{"name": "Cold open", "kind": "intro", "picture": str(tmp / "intro.mp4"), "audio": str(tmp / "intro.mp4")}]
    for name, w in (("Community", "comm"), ("Explainer", "seat")):
        work = tmp / w
        (work / "render").mkdir(parents=True)
        pieces, t = [], 1.0
        for i, n in enumerate(PIECES[w]):
            p = {"clip": f"{w}{i}.MP4", "source": str(tmp / f"{w}{i}.MP4"), "src_in": t, "src_out": round(t + n / 59.94, 2),
                 "topic": f"{w} {i}", **({"zoom": 1.1} if i == 1 else {})}
            pieces.append(p)
            frames[str(work / "render" / f"piece_{piece_key(p)}.mp4")] = n
        (work / "pieces.json").write_text(json.dumps(pieces))
        placed = [{"tag": tag, "timeline_start": r, "broll_start": i, "broll_end": i + d, "reason": f"why {tag}",
                   "take": "x"} for tag, r, d, i in PLACED[w]]
        (work / "broll_plan.json").write_text(json.dumps({"speed": 0.5, "placed": placed}))
        (work / "broll_map.json").write_text(json.dumps(
            [{"tag": tag, "clip": f"{tag}.mov", "source": str(tmp / f"{tag}.mov"), "duration": DUR[tag]} for tag in DUR]))
        pic = work / "ghostcut_test_v13_broll.mp4"
        frames[str(pic)] = sum(PIECES[w]) + 1
        sections.append({"name": name, "kind": "talk", "work": str(work), "picture": str(pic), "audio": str(pic)})
    sections[1]["isolate"] = {"iso": str(tmp / "comm" / "iso.mp3"), "win": str(tmp / "comm" / "win.wav"),
                              "pieces": [0, 2]}
    (tmp / "job").mkdir()
    (tmp / "job" / "facts.json").write_text(json.dumps({"cards": cards if cards is not None else CARDS}))
    job = {"render_root": str(tmp / "renders"), "sections": sections, "legacy_offsets": LEGACY,
           "facts": "job/facts.json", "speed": 0.5}
    (tmp / "job.json").write_text(json.dumps(job))
    return job, frames


def fake_probe(frames: dict):
    return (lambda p: frames[str(p)], lambda p: 0.021 if Path(p).name == "joined.mp4" else 0.0)


def library() -> list[dict]:
    out = []
    for tag, dur in DUR.items():
        e = {"tag": tag, "file": f"shots/{tag}.mov", "dur": dur, "used": False}
        if tag in BEST:
            e["best"] = BEST[tag]
        if tag == "SK":
            e.update(skip=True, use="skip: people in frame")
        out.append(e)
    return out


def shown_v16(tl: dict) -> dict:
    """The legacy project the app shows for r000: legacy section starts, positional ids, 1x-clamped W lengths."""
    broll = []
    for s, sec in enumerate(tl["sections"]):
        for o in sec["broll"]:
            d = 1.75 if o["tag"][0] == "W" else (2.91 if o["tag"] == "V10" else o["d"])
            broll.append({"id": f"b{len(broll) + 1}", "tag": o["tag"], "src": o["tag"], "t": LEGACY[s] + o["r"] + 0.05,
                          "d": d, "section": sec["name"]})
    cards = [{"id": f"c{i}", "t": min(t for _, t in c["lines"]), "d": c["until"] - min(t for _, t in c["lines"]),
              "caption": c["caption"], "lines": [x for x, _ in c["lines"]]} for i, c in enumerate(CARDS, 1)]
    cards[2]["lines"] = ["LOT PREMIUMS  /private/tmp/x/v16.sh – 50,000"]  # what v16 burned; the base is repaired
    return {"sections": [{"name": "Cold open", "t": 0.0, "d": 12.813}, {"name": "Community", "t": 12.813, "d": 199.9},
                         {"name": "Explainer", "t": 212.713, "d": 244.329}],
            "broll": broll, "cards": cards, "talk": [], "duration": 457.2}


def op(o: str, target: str = "", t: float = -1, d: float = -1, src: str = "", lines=None, caption: str = "",
       why: str = "") -> dict:
    return {"op": o, "target": target, "t": t, "d": d, "src": src, "lines": lines or [], "caption": caption, "why": why}


def edit(eid: str, *ops: dict) -> dict:
    return {"id": eid, "request": "", "ops": list(ops), "labels": [f"label {eid}:{k}" for k in range(len(ops))]}


def fps(path: str) -> float:
    return 50.0 if "D2" in path else 59.94


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        job, frames = build(self.tmp)
        self.job = rp.load_job(self.tmp)
        self.tl = rp.import_v16(self.job, fake_probe(frames))
        self.shown = shown_v16(self.tl)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plan(self, *edits, shown=None, base=None, never=None):
        return rp.make_plan(base or self.tl, shown or self.shown, list(edits), library(),
                            {"NV": "misleading promo"} if never is None else never, fps=fps)

    def ov(self, plan, key):
        return next(o for sec in plan["sections"] for o in sec["broll"] if o["key"] == key)


class ImportV16(Base):
    def test_sections_start_at_exact_frame_times(self):
        secs = self.tl["sections"]
        self.assertEqual([s["start_frame"] for s in secs], [0, 768, 12749])
        self.assertEqual([s["t"] for s in secs], [0.0, 12.8128, 212.695817])
        self.assertEqual([s["frames"] for s in secs], [768, 11981, 14641])
        self.assertEqual(secs[1]["d"], 199.883017)
        self.assertEqual((self.tl["frames"], self.tl["duration"]), (27390, 456.9565))
        self.assertEqual([s["legacy_t"] for s in secs], LEGACY)
        self.assertEqual([s["audio_offset"] for s in secs], [0.0, 0.021, 0.021])
        self.assertEqual((self.tl["schema"], self.tl["version"], self.tl["rate"], self.tl["sr"]),
                         ("ghostcut.timeline/1", "r000", [60000, 1001], 48000))
        self.assertEqual(self.tl["fixes"], rp.FIXES_V16)

    def test_pieces_sit_at_the_running_sum_of_their_lengths(self):
        ps = self.tl["sections"][1]["pieces"]
        self.assertEqual([p["A"] for p in ps], [0.0, 24.708017, 54.471083])
        self.assertEqual([p["d"] for p in ps], [24.708017, 29.763067, 145.395250])
        self.assertEqual(ps[1]["zoom"], 1.1)
        self.assertEqual(set(ps[0]) >= {"source", "src_in", "src_out", "zoom", "topic", "A", "d"}, True)
        intro = self.tl["sections"][0]
        self.assertEqual(len(intro["pieces"]), 1)
        self.assertEqual((intro["pieces"][0]["A"], intro["pieces"][0]["d"]), (0.0, 12.8128))
        self.assertEqual(self.tl["sections"][1]["isolate"]["pieces"], [0, 2])

    def test_overlays_come_from_placed(self):
        b = self.tl["sections"][1]["broll"][2]
        self.assertEqual(b, {"key": "v16:1:2", "tag": "D5", "source": str(self.tmp / "D5.mov"), "file_dur": 68.48,
                             "in": 10.0, "r": 145.7, "d": 4.9, "speed": 0.5, "reason": "why D5", "origin": "v16"})
        self.assertEqual(self.tl["sections"][2]["broll"][0]["key"], "v16:2:0")

    def test_cards_are_rebased_per_section(self):
        c = self.tl["cards"]
        self.assertEqual(c[0]["lines"][0][1], 35.9998)  # Community: -0.0002
        self.assertEqual(c[0]["size"], 76)
        self.assertEqual(c[1]["size"], 60)
        self.assertEqual(len(c[1]["lines"]), 4)  # v16 cards may have four lines
        self.assertEqual((c[2]["lines"][0][1], c[2]["until"]), (368.242817, 375.192817))  # Explainer: -0.017183
        self.assertEqual([x["key"] for x in c], ["v16:c:0", "v16:c:1", "v16:c:2"])

    def test_refuses_path_text_in_the_facts(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            bad = copy.deepcopy(CARDS)
            bad[2]["lines"][0][0] = "LOT PREMIUMS  /private/tmp/x/v16.sh – 50,000"
            _, frames = build(tmp, bad)
            with self.assertRaisesRegex(ValueError, "file path"):
                rp.import_v16(rp.load_job(tmp), fake_probe(frames))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_frame_count_must_match_the_pieces(self):
        _, frames = build(self.tmp / "x")
        frames[str(self.tmp / "x" / "comm" / "ghostcut_test_v13_broll.mp4")] += 5
        with self.assertRaisesRegex(ValueError, "pieces add up"):
            rp.import_v16(rp.load_job(self.tmp / "x"), fake_probe(frames))

    def test_missing_tag_in_broll_map(self):
        _, frames = build(self.tmp / "y")
        m = self.tmp / "y" / "seat" / "broll_map.json"
        m.write_text(json.dumps([b for b in json.loads(m.read_text()) if b["tag"] != "R2"]))
        with self.assertRaisesRegex(ValueError, "R2 is missing"):
            rp.import_v16(rp.load_job(self.tmp / "y"), fake_probe(frames))

    def test_load_job_resolves_facts(self):
        self.assertEqual(Path(self.job["facts"]), (self.tmp / "job" / "facts.json").resolve())


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class Probes(unittest.TestCase):
    def test_frames_start_and_rate(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("a.mp4", "a.mkv"):
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x36:rate=60000/1001",
                                "-frames:v", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(Path(d) / name)],
                               check=True)
                self.assertEqual(rp.probe_frames(Path(d) / name), 30)  # mkv has no nb_frames: packets are counted
            self.assertAlmostEqual(rp.probe_start(Path(d) / "a.mp4"), 0.0)
            self.assertAlmostEqual(rp.probe_fps(str(Path(d) / "a.mp4")), 59.94, places=2)
            self.assertIsNone(rp.probe_fps(str(Path(d) / "missing.mp4")))


class InPoint(unittest.TestCase):
    def test_drone_shot_mid_of_best(self):
        e = {"tag": "D0292", "dur": 17.96, "best": [0.5, 17.0]}
        self.assertEqual(rp.in_point(e, 4.9), 7.525)
        self.assertEqual(rp.preview_t(e), 8.75)

    def test_v_tag_best_is_halved(self):
        e = {"tag": "V10", "dur": 9.9099, "best": [13.0, 19.8]}
        self.assertEqual(rp.preview_t(e), 8.2)
        self.assertAlmostEqual(rp.in_point(e, 3.537), 8.2 - 0.5 * 3.537 / 2, places=6)
        self.assertEqual(rp.preview_t({"tag": "W7", "file_dur": 6.4}), 3.2)  # no best: middle of the file

    def test_clamps_and_fallbacks(self):
        self.assertEqual(rp.in_point({"tag": "R1", "dur": 6.0}, 4.0), 2.0)
        self.assertEqual(rp.in_point({"tag": "R1", "dur": 10.0, "best": [0.0, 1.0]}, 6.0), 0.0)  # empty best range
        self.assertEqual(rp.in_point({"tag": "R1", "dur": 10.0, "best": [8.0, 9.9]}, 4.0), 7.95)  # empty → whole file
        self.assertEqual(rp.in_point({"tag": "R1", "dur": 10.0, "best": [0.0, 20.0]}, 4.0), 7.98)  # end - 0.02 - read
        with self.assertRaisesRegex(ValueError, "too short"):
            rp.in_point({"tag": "R1", "dur": 2.0}, 4.0)

    def test_half_empty_best_reads_as_zero_like_muse(self):
        import muse
        for best in ([3.0, None], [None, 4.0], [], [None, None]):
            e = {"tag": "R1", "dur": 10.0, "best": best}
            self.assertEqual(rp.preview_t(e), muse.preview_t(e), best)
        self.assertEqual((rp.MIN_INSERT, rp.CARD_TAIL, rp.CARD_MIN), (muse.MIN_INSERT, muse.CARD_TAIL, muse.CARD_MIN))


class Coordinates(Base):
    def test_section_of_and_shown_starts(self):
        self.assertEqual([rp.section_of(self.tl, t) for t in (0.0, 12.8127, 12.8128, 300.0, 999.0)], [0, 0, 1, 2, 2])
        self.assertEqual(rp.S_shown(self.shown, 1), 12.813)  # v16 project: legacy offsets
        self.assertEqual(rp.S_shown(self.tl, 2), 212.713)  # the r000 import of it
        later = {**self.tl, "version": "r001"}
        self.assertEqual(rp.S_shown(later, 2), 212.695817)
        self.assertEqual(rp.to_exact(self.tl, self.shown, 300.0), 299.982817)


class MakePlan(Base):
    def test_swap_like_r001(self):
        plan, refused = self.plan(edit("e1", op("swap_broll", "b3", src="D2", why="clean pond")))
        self.assertEqual(refused, [])
        b = self.ov(plan, "v16:1:2")
        self.assertEqual((b["tag"], b["source"], b["file_dur"], b["in"], b["r"], b["d"]),
                         ("D2", "shots/D2.mov", 17.96, 7.525, 145.7, 4.9))
        self.assertEqual(b["reason"], "clean pond")
        self.assertEqual(round(plan["sections"][1]["t"] + b["r"], 3), 158.513)
        self.assertEqual(plan["dirty"], [1])
        self.assertEqual(plan["changes"], [{"edit": "e1", "op": "swap_broll", "section": 1, "key": "v16:1:2",
                                            "label": "b3 D5 → D2"}])
        self.assertEqual(plan["edits_applied"], ["e1"])
        self.assertEqual(plan["fixes"], rp.FIXES_V16)
        self.assertEqual((plan["parent"], plan["version"]), ("r000", None))
        self.assertIn("25 fps", plan["notes"][0]["body"])  # 50p judders at 0.5x
        self.assertEqual(rp.validate(plan), [])  # the v16 X9/V10 touch in this dirty section is not new
        self.assertEqual(self.tl["sections"][1]["broll"][2]["tag"], "D5")  # base untouched

    def test_tag_assert_on_positional_ids(self):
        shown = copy.deepcopy(self.shown)
        shown["broll"][2]["tag"] = "D9"
        _, refused = self.plan(edit("e1", op("swap_broll", "b3", src="D2")), shown=shown)
        self.assertIn("shows D9 but the render base has D5", refused[0]["reason"])
        self.assertEqual(refused[0]["label"], "label e1:0")
        shown["broll"][2].update(tag="D2", was_tag="D5")  # a project with the swap already shown
        self.assertEqual(self.plan(edit("e1", op("swap_broll", "b3", src="D2")), shown=shown)[1], [])

    def test_ref_mapping_for_render_built_projects(self):
        base = {**copy.deepcopy(self.tl), "version": "r001", "fixes": rp.FIXES_V16}
        shown = copy.deepcopy(self.shown)
        shown["version"] = "r001"
        shown["sections"] = [{"name": s["name"], "t": s["t"], "d": s["d"]} for s in base["sections"]]
        items = [{"tag": o["tag"], "t": sec["t"] + o["r"], "d": o["d"], "ref": [s, j]}
                 for s, sec in enumerate(base["sections"]) for j, o in enumerate(sec["broll"])][::-1]
        shown["broll"] = [{**it, "id": f"b{i}"} for i, it in enumerate(items, 1)]  # ids don't follow the base: ref does
        shown["cards"] = [{"id": f"c{i}", "t": c["lines"][0][1], "d": 2, "lines": [], "caption": "",
                           "ref": len(base["cards"]) - i} for i, c in enumerate(base["cards"], 1)]
        plan, refused = self.plan(edit("e1", op("move_broll", "b7", t=12.8128 + 20.0, d=4.5)),
                                  edit("e2", op("drop_card", "c1")), shown=shown, base=base)
        self.assertEqual(refused, [])
        self.assertEqual(self.ov(plan, "v16:1:0")["r"], 20.0)  # exact starts: r = t - S
        self.assertEqual(plan["fixes"], [])  # repairs already shipped in r001
        self.assertNotIn("v16:c:2", [c["key"] for c in plan["cards"]])
        shown["version"] = "r002"
        _, refused = self.plan(edit("e1", op("drop_broll", "b7")), shown=shown, base=base)
        self.assertIn("reload the app", refused[0]["reason"])

    def test_move_keeps_the_real_length_when_muse_kept_the_shown_one(self):
        plan, refused = self.plan(edit("e1", op("move_broll", "b2", t=12.813 + 70.0, d=1.75)))
        self.assertEqual(refused, [])
        b = self.ov(plan, "v16:1:1")
        self.assertEqual((b["r"], b["d"], b["in"]), (70.0, 2.637, 0.0))  # never divided by k

    def test_move_with_a_new_length_and_in_point_clamp(self):
        plan, refused = self.plan(edit("e1", op("move_broll", "b1", t=12.813 + 20.0, d=6.0)),
                                  edit("e2", op("move_broll", "b4", t=12.813 + 120.0, d=6.0)))
        self.assertEqual(refused, [])
        self.assertEqual((self.ov(plan, "v16:1:0")["r"], self.ov(plan, "v16:1:0")["d"]), (20.0, 6.0))
        v = self.ov(plan, "v16:1:3")
        self.assertEqual((v["d"], v["in"]), (6.0, 6.8899))
        self.assertIn("now starts at 6.89 s", plan["notes"][0]["body"])
        _, refused = self.plan(edit("e1", op("move_broll", "b4", t=12.813 + 120.0, d=20.0)))
        self.assertIn("too short", refused[0]["reason"])

    def test_moves_stay_inside_one_section(self):
        for t, d, msg in ((210.0, 5.0, rp.CROSS), (300.0, 3.0, rp.CROSS), (5.0, 3.0, rp.COLD)):
            _, refused = self.plan(edit("e1", op("move_broll", "b1", t=t, d=d)))
            self.assertEqual(refused[0]["reason"], msg)

    def test_add_broll(self):
        plan, refused = self.plan(edit("e1", op("add_broll", t=12.813 + 30.0, d=4.0, src="D2", why="pond")))
        self.assertEqual(refused, [])
        b = self.ov(plan, "e1:0")
        self.assertEqual((b["tag"], b["r"], b["d"], b["in"], b["origin"], b["reason"]), ("D2", 30.0, 4.0, 7.75, "e1", "pond"))
        self.assertEqual([o["key"] for o in plan["sections"][1]["broll"]][:2], ["v16:1:0", "e1:0"])  # sorted by r
        for t, d, msg in ((455.0, 5.0, rp.CROSS), (5.0, 3.0, rp.COLD), (100.0, 0.6, "at least 1.0 s")):
            _, refused = self.plan(edit("e1", op("add_broll", t=t, d=d, src="D2")))
            self.assertIn(msg, refused[0]["reason"])

    def test_added_items_are_numbered_like_muse_apply(self):
        plan, refused = self.plan(edit("e1", op("add_broll", t=12.813 + 30.0, d=4.0, src="D2"),
                                       op("add_card", t=100.0, d=3.0, lines=["NEW"])),
                                  edit("e2", op("move_broll", "b8+", t=12.813 + 35.0, d=4.0),
                                       op("edit_card", "c4+", lines=["NEWER"])))
        self.assertEqual(refused, [])
        self.assertEqual(self.ov(plan, "e1:0")["r"], 35.0)
        self.assertEqual(next(c for c in plan["cards"] if c["key"] == "e1:1")["lines"][0][0], "NEWER")
        _, refused = self.plan(edit("e1", op("add_broll", t=5.0, d=4.0, src="D2")),
                               edit("e2", op("drop_broll", "b8+")))
        self.assertEqual([r["edit"] for r in refused], ["e1", "e2"])
        self.assertIn("never added", refused[1]["reason"])

    def test_overlap_and_edge_refused_with_the_edit(self):
        plan, refused = self.plan(edit("e1", op("move_broll", "b1", t=12.813 + 60.0, d=3.03)))
        self.assertEqual(refused, [{"edit": "e1", "op": "move_broll", "label": "label e1:0",
                                    "reason": "C1 touches W4 (keep ≥ 0.05 s between inserts)"}])
        self.assertEqual((plan["changes"], plan["edits_applied"], plan["dirty"]), ([], [], []))
        self.assertEqual(self.ov(plan, "v16:1:0")["r"], 7.0)  # the refused move is not in the plan
        self.assertEqual(rp.validate(plan), [])
        self.assertEqual(self.plan(edit("e1", op("move_broll", "b1", t=12.813 + 60.0, d=3.0)))[1], [])  # 0.05 s is ok
        _, refused = self.plan(edit("e1", op("move_broll", "b1", t=12.813 + 0.1, d=3.0)))
        self.assertIn("closer than 0.3 s", refused[0]["reason"])

    def test_problems_go_to_the_last_edit_that_touched_the_item(self):
        plan, refused = self.plan(edit("e1", op("move_broll", "b3", t=12.813 + 100.0, d=4.9)),
                                  edit("e2", op("move_broll", "b3", t=12.813 + 0.1, d=8.0)))
        self.assertEqual([r["edit"] for r in refused], ["e2"])
        self.assertIn("closer than 0.3 s", refused[0]["reason"])
        self.assertIn("D5 touches C1", refused[0]["reason"])
        self.assertEqual((plan["edits_applied"], [c["edit"] for c in plan["changes"]]), (["e1"], ["e1"]))

    def test_later_edit_can_clear_an_earlier_clash(self):
        _, refused = self.plan(edit("e1", op("move_broll", "b1", t=12.813 + 60.0, d=3.03)),
                               edit("e2", op("move_broll", "b2", t=12.813 + 70.0, d=1.75)))
        self.assertEqual(refused, [])
        # ...but when that later edit is refused, the earlier clash is back and refused too
        plan, refused = self.plan(edit("e1", op("move_broll", "b1", t=12.813 + 60.0, d=3.03)),
                                  edit("e2", op("move_broll", "b2", t=12.813 + 0.1, d=1.75)))
        self.assertEqual([(r["edit"], r["reason"][:10]) for r in refused], [("e1", "C1 touches"), ("e2", "W4 at 0.10")])
        self.assertEqual((plan["changes"], rp.validate(plan)), ([], []))

    def test_clash_is_blamed_on_the_move_not_a_later_swap_of_the_neighbour(self):
        plan, refused = self.plan(edit("e1", op("move_broll", "b1", t=12.813 + 60.0, d=3.03)),
                                  edit("e2", op("swap_broll", "b2", src="D2")))
        self.assertEqual([(r["edit"], r["reason"][:10]) for r in refused], [("e1", "C1 touches")])
        self.assertEqual((plan["edits_applied"], [c["label"] for c in plan["changes"]]), (["e2"], ["b2 W4 → D2"]))
        self.assertEqual(rp.validate(plan), [])

    def test_drop_and_ops_on_dropped_items(self):
        plan, refused = self.plan(edit("e1", op("drop_broll", "b6"), op("drop_broll", "b7")))
        self.assertEqual(refused, [])
        self.assertEqual(plan["sections"][2]["broll"], [])  # renders as plain talk
        self.assertEqual(plan["dirty"], [2])
        _, refused = self.plan(edit("e1", op("drop_broll", "b6")), edit("e2", op("swap_broll", "b6", src="D2")))
        self.assertIn("dropped by an earlier edit", refused[0]["reason"])

    def test_swap_refusals(self):
        cases = {"NOPE": "not in the shot library", "SK": "Inventory says skip: skip: people in frame",
                 "NV": "Editor rule: misleading promo", "SH": "too short"}
        for src, msg in cases.items():
            _, refused = self.plan(edit("e1", op("swap_broll", "b3", src=src)))
            self.assertIn(msg, refused[0]["reason"], src)

    def test_cut_span_and_unknown_ops_refused(self):
        _, refused = self.plan(edit("e1", op("cut_span", t=100.0, d=2.0)), edit("e2", op("warp", "b1")))
        self.assertEqual(refused[0]["reason"], "Render v1 can't cut talk yet — ask for a note instead")
        self.assertIn("can't apply", refused[1]["reason"])

    def test_cards(self):
        plan, refused = self.plan(edit("e1", op("move_card", "c1", t=50.0, d=5.0)),
                                  edit("e2", op("edit_card", "c3", lines=["LOT PREMIUMS", "$0 – $250,000", "BY SIZE"])),
                                  edit("e3", op("add_card", t=100.0, d=1.0, lines=["A", "B"], caption="CAP")),
                                  edit("e4", op("drop_card", "c2")))
        self.assertEqual(refused, [])
        cards = {c["key"]: c for c in plan["cards"]}
        self.assertEqual((cards["v16:c:0"]["lines"][0][1], cards["v16:c:0"]["until"]), (49.9998, 54.9998))
        c3 = cards["v16:c:2"]
        self.assertEqual([t for _, t in c3["lines"]], [368.242817, 368.842817, 369.442817])
        self.assertEqual((c3["until"], c3["caption"]), (375.192817, "DEPENDS ON SIZE + LOCATION"))
        self.assertEqual(cards["e3:0"], {"key": "e3:0", "caption": "CAP", "size": 60, "until": 101.9998,
                                         "lines": [["A", 99.9998], ["B", 100.5998]]})
        self.assertNotIn("v16:c:1", cards)
        self.assertEqual([c["key"] for c in plan["cards"]], ["v16:c:0", "e3:0", "v16:c:2"])  # time order
        self.assertEqual(plan["dirty"], [])  # cards never dirty a section picture

    def test_card_move_without_length_shifts_until(self):
        plan, _ = self.plan(edit("e1", op("move_card", "c1", t=40.0)))
        c = plan["cards"][0]
        self.assertEqual((c["lines"][0][1], c["until"]), (39.9998, 43.5998))

    def test_card_refusals(self):
        cases = [(op("move_card", "c1", t=5.0), rp.COLD), (op("add_card", t=5.0, lines=["X"]), rp.COLD),
                 (op("move_card", "c1", t=12.9), "0.2 s after the cold open"),
                 (op("move_card", "c3", t=455.0, d=3.0), "0.5 s before the end"),
                 (op("edit_card", "c1", lines=["SEE var/notes.py"]), "file path"),
                 (op("edit_card", "c1", lines=["A", "B", "C", "D"]), "1–3 lines"),
                 (op("edit_card", "c1", lines=["W" * 80]), "too wide"), (op("drop_card", "c9"), "No card")]
        for o, msg in cases:
            if msg == "too wide" and rp.line_px("W", 60) is None:
                continue  # width is measured with the card face, which only macOS ships
            _, refused = self.plan(edit("e1", o))
            self.assertIn(msg, refused[0]["reason"] if refused else "", o)

    def test_note(self):
        plan, refused = self.plan(edit("e1", op("add_note", t=300.0, why="Reshoot the porch line")))
        self.assertEqual(refused, [])
        self.assertEqual(plan["notes"], [{"t": 299.983, "src": "you", "state": "open", "title": "Reshoot the porch line",
                                          "body": "Reshoot the porch line", "check": "Added through Muse",
                                          "edit": "e1"}])
        self.assertEqual(plan["changes"][0]["key"], "e1:0")
        self.assertEqual(plan["dirty"], [])

    def test_every_op_is_one_change_or_one_refusal(self):
        edits = [edit("e1", op("swap_broll", "b3", src="D2"), op("cut_span", t=50, d=1)),
                 edit("e2", op("drop_card", "c2"), op("add_note", t=20.0, why="x"), op("move_broll", "b99", t=50, d=2)),
                 edit("e3", op("move_broll", "b1", t=12.813 + 60.0, d=3.03))]
        plan, refused = self.plan(*edits)
        self.assertEqual(len(plan["changes"]) + len(refused), 6)
        self.assertEqual(plan["edits_applied"], [])
        self.assertEqual(sorted({r["edit"] for r in refused}), ["e1", "e2", "e3"])


class Validate(Base):
    def test_timeline_and_plan_cards(self):
        self.assertEqual(rp.validate(self.tl), [])
        spec = rp.plan_cards({**self.tl, "accent": [1, 2, 3]})
        self.assertEqual(spec["accent"], [1, 2, 3])
        self.assertEqual(spec["cards"][2], {"caption": "DEPENDS ON SIZE + LOCATION", "size": 60, "until": 375.192817,
                                            "lines": [["LOT PREMIUMS  $0 – $250,000", 368.242817]]})

    def test_catches_path_text_and_bad_overlays(self):
        tl = copy.deepcopy(self.tl)
        tl["cards"][1]["caption"] = "SEE /private/tmp/run.sh"
        tl["sections"][0]["broll"].append(dict(tl["sections"][1]["broll"][0], key="x"))
        tl["sections"][2]["broll"][1].update(r=241.0)  # past D - 0.3, in a dirty section
        tl["sections"][2]["broll"][0].update(**{"in": 0.5})  # W7: 0.5 + 6.35 > 6.478
        tl["dirty"] = [2]
        msgs = " | ".join(rp.validate(tl))
        for want in ("file path", rp.COLD, "closer than 0.3 s", "W7 is too short"):
            self.assertIn(want, msgs)

    def test_fix_labels(self):
        self.assertEqual(rp.fix_label(self.tl, "card2_text"), "Card 3 text at 6:08")
        self.assertEqual(rp.fix_label(self.tl, "voice_sync"), "Voice re-synced to picture")


class Hygiene(unittest.TestCase):
    def test_no_machine_paths_in_tracked_code(self):
        src = (APP / "render_plan.py").read_text()
        for root in ("Volumes", "Users"):
            self.assertNotIn(f"/{root}/", src)

    def test_planning_never_imports_guestlist_or_openai(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import render_plan as rp\n"
                "tl = {'version': 'r000', 'speed': 0.5, 'duration': 10, 'cards': [], 'fixes': [],\n"
                "      'sections': [{'name': 'A', 'kind': 'talk', 't': 0, 'd': 10, 'broll': []}]}\n"
                "p, r = rp.make_plan(tl, {'sections': [{'name': 'A', 't': 0, 'd': 10}], 'broll': [], 'cards': []},\n"
                "                    [], [], {}, fps=lambda f: None)\n"
                "assert rp.validate(p) == [] and r == []\n"
                "print(sorted(m for m in sys.modules if m.split('.')[0] in ('guestlist', 'openai')))")
        out = subprocess.run([sys.executable, "-c", code, str(APP)], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
