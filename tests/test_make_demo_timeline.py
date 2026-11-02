from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import numpy  # noqa: F401  (make_demo needs it)
except ImportError:
    raise unittest.SkipTest("needs numpy (pip install -e \".[director]\")")
if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    raise unittest.SkipTest("needs ffmpeg and ffprobe on PATH")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import make_demo  # noqa: E402
import muse  # noqa: E402


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def piece(name: str, a: float, d: float, topic: str, zoom: float = 1.0) -> dict:
    return {"source": f"cam/{name}", "src_in": 1.0, "src_out": 1.0 + d, "zoom": zoom, "topic": topic, "A": a, "d": d}


def overlay(s: int, i: int, tag: str, source: str, r: float, d: float, inp: float) -> dict:
    return {"key": f"v16:{s}:{i}", "tag": tag, "source": f"shots/{source}", "file_dur": 20.0, "in": inp, "r": r, "d": d,
            "speed": 0.5, "reason": f"why {tag}", "origin": "v16"}


def timeline(version: str = "r001") -> dict:
    """An 8 s program: a 2 s cold open, then two 3 s talk sections (v16-style legacy starts slightly off)."""
    return {
        "schema": "ghostcut.timeline/1", "version": version, "parent": "r000", "rate": [60000, 1001], "sr": 48000,
        "speed": 0.5, "frames": 480, "duration": 8.0, "verify": "verify.json",
        "files": {"phone": "phone.mp4", "voice": "voice.wav", "music": "music_bed.wav"},
        "mix": {"music_gain_db": -6.43, "lufs": -14.18, "tp": -1.02},
        "fixes": ["card16_text", "voice_sync"], "edits_applied": ["e303661983"],
        "sections": [
            {"name": "Cold open", "kind": "intro", "t": 0.0, "d": 2.0, "legacy_t": 0.0, "pieces": [], "broll": []},
            {"name": "Community", "kind": "talk", "t": 2.0, "d": 3.0, "legacy_t": 2.01,
             "pieces": [piece("A1.MP4", 0.0, 1.5, "hook"), piece("A2.MP4", 1.5, 1.5, "farms", 1.12)],
             "broll": [overlay(1, 0, "D0292", "DJI_0292.MOV", 0.4, 1.0, 7.525),
                       overlay(1, 1, "C9915", "C9915.MP4", 1.8, 0.8, 2.0)],
             "isolate": {"iso": "iso.mp3", "win": "win.wav", "pieces": [0, 1]},
             "isolated": [{"piece": 0, "lag_ms": 4, "r": 0.9}, {"piece": 1, "lag_ms": 128, "r": 0.77}],
             "camera_fallback": []},
            {"name": "House tour", "kind": "talk", "t": 5.0, "d": 3.0, "legacy_t": 5.08,
             "pieces": [piece("H1.MP4", 0.0, 1.0, "porch"), piece("H2.MP4", 1.0, 1.0, "porch"),
                        piece("H3.MP4", 2.0, 1.0, "kitchen")],
             "broll": [overlay(2, 0, "V10", "V10.mp4", 0.5, 0.6, 3.0)],
             "isolate": {"iso": "iso.mp3", "win": "win.wav", "pieces": [0, 0]},
             "isolated": [{"piece": 0, "lag_ms": 35, "r": 0.94}],
             "camera_fallback": [{"piece": 2, "reason": "r 0.41"}]},
        ],
        "cards": [{"key": "c0", "caption": "THE PARK", "size": 60, "until": 3.6, "lines": [["THE PARK", 2.5]]},
                  {"key": "c1", "caption": "DEPENDS ON SIZE", "size": 60, "until": 7.5,
                   "lines": [["LOT PREMIUMS  $0 – $250,000", 5.5], ["SECOND LINE", 6.1]]}],
        "notes": [{"t": 6.5, "src": "you", "state": "open", "title": "Ask about the lot", "body": "Ask about the lot size",
                   "check": "Added through Muse", "edit": "e9"},
                  "D0292 is 50p and judders at 0.5×"],
    }


def carry() -> dict:
    """A legacy (v16) project: no version, k-scaled times, the hard-coded loudness finding."""
    return {
        "title": "Test tour", "client": "Client · Agent", "shootDate": "Oct 13", "startAt": 1.0,
        "spend": {"total": 1.0, "rows": []}, "media": [{"bin": "A-cam", "count": 2, "note": ""}],
        "chat": [{"who": "you", "text": "hi"}], "suggestions": ["Tighten"], "neverUse": {"W3": "promo"},
        "importFlag": {"title": "Found", "body": "b", "where": "w", "clips": "c"},
        "takes": [{"topic": "hook", "kind": "keeper_take"}], "takeMeta": {"model": "m"},
        "library": [{"tag": "D0292", "dur": 20.0, "used": False}, {"tag": "C9915", "used": True},
                    {"tag": "V10", "used": False}, {"tag": "D0295", "used": True}],
        "findings": [{"t": 0, "src": "measured", "state": "ok", "title": "Loudness −14.3 LUFS · −1.0 dBTP",
                      "body": "hard-coded", "check": "ffmpeg ebur128"},
                     {"t": 5.5, "src": "spark", "state": "open", "title": "Kitchen", "body": "b", "check": "c"}],
        "loudness": {"lufs": -14.3, "tp": -1.0}, "transcript": [{"t": 0.0, "e": 1.0, "text": "old", "w": []}],
        "env": {"mix": [0.5]}, "envStep": 0.5, "talk": [], "broll": [], "cards": [], "duration": 8.2,
        "sections": [{"name": "Cold open", "t": 0.0, "d": 2.01}, {"name": "Community", "t": 2.01, "d": 3.07},
                     {"name": "House tour", "t": 5.08, "d": 3.12}],
    }


WHISPER = {"segments": [
    {"start": 2.1, "end": 3.0, "text": " Welcome home.", "words": [{"word": " Welcome", "start": 2.1, "end": 2.5},
                                                              {"word": " home.", "start": 2.5, "end": 3.0}]},
    {"start": 6.1, "end": 7.9, "text": " A glass of wine in the evening.", "words": [
        {"word": " glass", "start": 6.1, "end": 6.4}, {"word": " of", "start": 6.4, "end": 6.5},
        {"word": " wine", "start": 6.5, "end": 6.9}, {"word": " in", "start": 7.1, "end": 7.2},
        {"word": " the", "start": 7.2, "end": 7.3}, {"word": " evening.", "start": 7.3, "end": 7.9}]}]}
VERIFY = {"ok": True, "checks": [
    # render_verify's own shape: worst |lag| in ms against a text threshold
    {"name": "sync_voice_joined", "value": 4, "threshold": "|lag| ≤ 5 ms, r ≥ 0.8 camera / 0.6 isolated",
     "status": "pass"},
    {"name": "sync_master_voice", "value": 1, "threshold": "|lag| ≤ 3 ms", "status": "pass"},
    {"name": "phone_true_peak", "value": 0.4, "threshold": 0.0, "status": "warn"}]}


class ProjectFromTimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.dir = Path(cls.tmp.name)
        ffmpeg("-f", "lavfi", "-i", "testsrc=s=160x90:r=10:d=8", "-f", "lavfi", "-i", "sine=f=440:d=8",
               "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
               str(d / "phone.mp4"))
        ffmpeg("-f", "lavfi", "-i", "sine=f=300:d=8", "-ac", "2", "-ar", "48000", str(d / "voice.wav"))
        ffmpeg("-f", "lavfi", "-i", "sine=f=200:d=8", "-ac", "2", "-ar", "48000", str(d / "music_bed.wav"))
        (d / "whisper.json").write_text(json.dumps(WHISPER))
        (d / "verify.json").write_text(json.dumps(VERIFY))
        cls.out = d / ".next-r001"
        cls.p = make_demo.project_from_timeline(timeline(), carry(), d / "whisper.json", d / "phone.mp4",
                                                d / "voice.wav", d / "music_bed.wav", cls.out)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def finding(self, p: dict, title: str) -> dict:
        hits = [f for f in p["findings"] if f["title"].startswith(title)]
        self.assertEqual(len(hits), 1, [f["title"] for f in p["findings"]])
        return hits[0]

    def test_ids_map_back_to_the_timeline(self) -> None:
        p, tl = self.p, timeline()
        self.assertEqual([b["id"] for b in p["broll"]], ["b1", "b2", "b3"])
        self.assertEqual([c["id"] for c in p["talk"]], ["t1", "t2", "t3", "t4", "t5", "t6"])
        for b in p["broll"]:
            sec = tl["sections"][b["ref"][0]]
            ov = sec["broll"][b["ref"][1]]
            self.assertEqual((b["tag"], b["key"], b["in"]), (ov["tag"], ov["key"], ov["in"]))
            self.assertAlmostEqual(b["t"], sec["t"] + ov["r"], places=3)
            self.assertAlmostEqual(b["d"], ov["d"], places=3)
            self.assertEqual(b["section"], sec["name"])
        for c in p["talk"][1:]:
            sec = tl["sections"][c["ref"][0]]
            pc = sec["pieces"][c["ref"][1]]
            self.assertAlmostEqual(c["t"], sec["t"] + pc["A"], places=3)
            self.assertEqual((c["d"], c["in"], c["topic"]), (pc["d"], pc["src_in"], pc["topic"]))
        for c in p["cards"]:
            card = tl["cards"][c["ref"]]
            self.assertEqual(c["lines"], [x for x, _ in card["lines"]])
            self.assertEqual(c["at"], [t for _, t in card["lines"]])
            self.assertAlmostEqual(c["t"] + c["d"], card["until"], places=3)

    def test_items_sit_at_exact_program_times(self) -> None:
        p = self.p
        self.assertEqual(p["talk"][0], {"t": 0.0, "d": 2.0, "src": "cold open", "in": 0, "topic": "Cold open",
                                        "section": "Cold open", "ref": [0, 0], "id": "t1"})
        self.assertEqual((p["talk"][2]["t"], p["talk"][2]["src"], p["talk"][2]["zoom"]), (3.5, "A2.MP4", 1.12))
        b1, b2, b3 = p["broll"]
        self.assertEqual((b1["t"], b1["d"], b1["in"], b1["src"], b1["topic"]), (2.4, 1.0, 7.525, "DJI_0292.MOV", "hook"))
        self.assertEqual((b2["t"], b2["topic"]), (3.8, "farms"))
        self.assertEqual((b3["t"], b3["ref"], b3["section"]), (5.5, [2, 0], "House tour"))
        self.assertEqual(p["cards"][1], {"id": "c2", "t": 5.5, "d": 2.0, "caption": "DEPENDS ON SIZE",
                                         "lines": ["LOT PREMIUMS  $0 – $250,000", "SECOND LINE"], "at": [5.5, 6.1],
                                         "ref": 1, "key": "c1"})
        self.assertEqual([(s["name"], s["kind"], s["t"], s["d"]) for s in p["sections"]],
                         [("Cold open", "intro", 0.0, 2.0), ("Community", "talk", 2.0, 3.0), ("House tour", "talk", 5.0, 3.0)])
        self.assertEqual((p["version"], p["duration"]), ("r001", 8.0))

    def test_carried_keys_and_measured_loudness(self) -> None:
        p, c = self.p, carry()
        for k in make_demo.CARRY:
            self.assertEqual(p[k], c[k], k)
        self.assertEqual(p["loudness"], {"lufs": -14.2, "tp": -1.0, "music_gain_db": -6.43})
        self.assertEqual({e["tag"]: e["used"] for e in p["library"]},
                         {"D0292": True, "C9915": True, "V10": True, "D0295": False})
        self.assertEqual([len(p["env"][k]) for k in ("mix", "voice", "music")], [16, 16, 16])
        self.assertEqual(p["transcript"][1]["w"][0], ["glass", 6.1, 6.4])

    def test_findings(self) -> None:
        p = self.p
        loud = self.finding(p, "Loudness ")
        self.assertEqual((loud["title"], loud["state"], loud["render"]), ("Loudness −14.2 LUFS · −1.0 dBTP", "ok", "r001"))
        self.assertEqual(self.finding(p, "Kitchen")["t"], 5.42)  # rebased from the legacy section start
        note = self.finding(p, "Ask about the lot")
        self.assertEqual((note["src"], note["edit"], note["body"]), ("you", "e9", "Ask about the lot size"))
        warn = self.finding(p, "D0292 is 50p")
        self.assertEqual((warn["src"], warn["state"]), ("measured", "open"))
        sync = self.finding(p, "Voice re-synced")
        self.assertEqual(sync["title"], "Voice re-synced: up to 128 ms off before, within 4 ms now (measured)")
        self.assertIn("Community +4 to +128 ms (r 0.77–0.90)", sync["body"])
        self.assertEqual((sync["state"], sync["t"]), ("fixed", 2.0))
        cam = self.finding(p, "House tour piece 1 uses camera audio")
        self.assertEqual((cam["state"], cam["t"]), ("open", 6.0))
        self.assertIn("“glass of wine”", cam["body"])
        self.assertIn("r 0.41", self.finding(p, "House tour piece 2 uses camera audio")["body"])
        self.assertEqual(self.finding(p, "Check: phone_true_peak")["state"], "open")

    def test_files_written(self) -> None:
        out = self.out
        self.assertEqual(json.loads((out / "project.json").read_text()), self.p)
        self.assertTrue((out / "project.js").read_text().startswith("window.PROJECT = {"))
        self.assertEqual(os.readlink(out / "video.mp4"), os.path.abspath(self.dir / "phone.mp4"))
        self.assertEqual(json.loads((out / "transcript" / "voice.json").read_text()), WHISPER)
        self.assertTrue((out / "poster.jpg").stat().st_size > 0)
        self.assertGreaterEqual(len(list((out / "thumbs").glob("*.jpg"))), 4)

    def test_next_render_replaces_its_own_notes(self) -> None:
        tl = timeline("r002")
        tl.update(fixes=[], notes=[], mix={"music_gain_db": -6.4, "lufs": -13.9, "tp": -0.8})
        p = make_demo.project_from_timeline(tl, copy.deepcopy(self.p), None, self.dir / "phone.mp4", None, None,
                                            self.dir / ".next-r002", skip_media=True)
        titles = [f["title"] for f in p["findings"]]
        self.assertEqual(len(titles), len(set(titles)), titles)
        self.assertEqual(self.finding(p, "Kitchen")["t"], 5.42)  # not rebased twice
        self.assertEqual(self.finding(p, "Voice re-synced")["render"], "r001")  # carried, not repeated
        self.assertEqual(self.finding(p, "House tour piece 1")["render"], "r002")
        self.assertEqual(self.finding(p, "Loudness ")["title"], "Loudness −13.9 LUFS · −0.8 dBTP")
        self.assertEqual(p["transcript"], self.p["transcript"])  # no new transcript: keep the carried one
        self.assertFalse((self.dir / ".next-r002" / "poster.jpg").exists())

    def test_a_finding_over_a_swapped_insert_opens_again_and_warnings_say_where(self) -> None:
        """A fixed finding that names the shot it was covered with opens again once a render swaps that insert; one that
        already names the new shot stays as it is. A warning carries its time and its first detail lines."""
        c = carry()
        c["findings"] += [{"t": 2.9, "src": "spark", "state": "fixed", "title": "Worker behind her",
                           "body": "Covered with drone D0295.", "check": "Frame check: no people"},
                          {"t": 2.9, "src": "you", "state": "fixed", "title": "Drone pick", "body": "D0292 it is.",
                           "check": "c"}]
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "verify.json").write_text(json.dumps({"ok": True, "checks": [
                {"name": "words_at_joins", "value": 1, "threshold": "no join or switch inside a word",
                 "status": "warn", "t": 6.02, "detail": ['6.020 inside "a" (5.90–6.06), voice 13 dB down']}]}))
            os.symlink(self.dir / "phone.mp4", Path(d) / "phone.mp4")
            p = make_demo.project_from_timeline(timeline(), c, None, Path(d) / "phone.mp4", None, None,
                                                Path(d) / ".next-r001", skip_media=True)
        worker = self.finding(p, "Worker behind her")
        self.assertEqual((worker["state"], worker["t"]), ("open", 2.89))
        self.assertTrue(worker["body"].endswith("Since r001 this spot shows D0292 instead: check it again."))
        self.assertEqual(self.finding(p, "Drone pick")["state"], "fixed")
        self.assertEqual(self.finding(p, "Kitchen")["state"], "open")  # no shot named: untouched
        warn = self.finding(p, "Check: words_at_joins")
        self.assertEqual(warn["t"], 6.02)
        self.assertIn('inside "a"', warn["body"])

    def test_prebuilt_carry_is_not_rebased_again_or_duplicated(self) -> None:
        """A caller may pass findings it already rebased and annotated (no sections, no version): keep their times and
        let the measured notes replace its notes on the same subjects."""
        pre = {k: v for k, v in carry().items() if k in make_demo.CARRY + ("library",)}
        pre["findings"] = [
            {"t": 5.42, "src": "spark", "state": "open", "title": "Kitchen", "body": "b", "check": "c"},
            {"t": 2.0, "src": "measured", "state": "fixed", "title": "Voice re-synced to the picture", "body": "b", "check": "c"},
            {"t": 6.0, "src": "measured", "state": "open", "title": "House tour piece 1 uses camera audio", "body": "b", "check": "c"},
            {"t": 0.0, "src": "measured", "state": "open", "title": "Check: phone_true_peak", "body": "b", "check": "c"},
            {"t": 0, "src": "measured", "state": "ok", "title": "Loudness −14.3 LUFS · −1.0 dBTP", "body": "b", "check": "c"}]
        p = make_demo.project_from_timeline(timeline(), pre, None, self.dir / "phone.mp4", None, None,
                                            self.dir / ".next-pre", skip_media=True)
        titles = [f["title"] for f in p["findings"]]
        self.assertEqual(len(titles), len(set(titles)), titles)
        self.assertEqual(self.finding(p, "Kitchen")["t"], 5.42)
        self.assertEqual(len([x for x in titles if x.startswith("Voice re-synced")]), 1)
        self.assertEqual(len([x for x in titles if x.startswith("Loudness ")]), 1)
        self.assertEqual(self.finding(p, "House tour piece 1")["render"], "r001")

    def test_muse_works_on_the_built_project(self) -> None:
        p = self.p
        self.assertEqual(muse.intro_end(p), 2.0)
        blocked = muse.validate(p, [{"op": "add_broll", "target": "", "t": 1.0, "d": 0.5, "src": "D0292",
                                     "lines": [], "caption": "", "why": "x"}])[0]
        self.assertIn(muse.COLD_OPEN, [c["text"] for c in blocked["checks"] if not c["ok"]])
        swapped = muse.apply(p, [{"id": "e1", "ops": [{"op": "swap_broll", "target": "b1", "src": "D0295"}]}])
        self.assertEqual((swapped["broll"][0]["ref"], swapped["broll"][0]["was_tag"]), ([1, 0], "D0292"))

    def test_cli_reads_media_next_to_the_timeline(self) -> None:
        d = self.dir
        (d / "timeline.json").write_text(json.dumps(timeline()))
        (d / "carry.json").write_text(json.dumps(carry()))
        out = d / "cli-out"
        r = subprocess.run([sys.executable, str(ROOT / "app" / "make_demo.py"), "--timeline", str(d / "timeline.json"),
                            "--carry", str(d / "carry.json"), "--out", str(out), "--skip-media"],
                           capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(r.stdout)["version"], "r001")
        p = json.loads((out / "project.json").read_text())
        self.assertEqual(len(p["env"]["voice"]), 16)
        self.assertEqual(os.readlink(out / "video.mp4"), os.path.abspath(d / "phone.mp4"))


class LegacyPathTests(unittest.TestCase):
    def test_fcpxml_path_still_k_scales_into_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            ffmpeg("-f", "lavfi", "-i", "testsrc=s=160x90:r=10:d=4", "-f", "lavfi", "-i", "sine=d=4", "-c:v", "libx264",
                   "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "cut.mp4"))
            work = d / "work"
            work.mkdir()
            (work / "x_broll.fcpxml").write_text(
                '<fcpxml><library><event><project><sequence><spine>'
                '<asset-clip offset="0s" start="1s" duration="2s" name="hook | A.MP4">'
                '<asset-clip lane="1" offset="1.5s" start="0s" duration="1s" name="R1 | hook | R1.MP4"/>'
                '</asset-clip></spine></sequence></project></event></library></fcpxml>')
            subprocess.run([sys.executable, str(ROOT / "app" / "make_demo.py"), "--video", str(d / "cut.mp4"),
                            "--section", f"Tour={work}:0:4", "--skip-media", "--out", str(d / "demo")],
                           capture_output=True, check=True)
            p = json.loads((d / "demo" / "project.json").read_text())
            self.assertEqual(p["talk"][0]["d"], 4.0)
            self.assertEqual((p["broll"][0]["id"], p["broll"][0]["t"], p["broll"][0]["tag"]), ("b1", 1.0, "R1"))
            self.assertNotIn("version", p)


class HygieneTests(unittest.TestCase):
    def test_no_absolute_paths_in_tracked_app_code(self) -> None:
        for name in ("make_demo.py", "muse.py"):
            text = (ROOT / "app" / name).read_text()
            self.assertNotIn("/Volumes/", text, name)
            self.assertNotIn("/Users/", text, name)


if __name__ == "__main__":
    unittest.main()
