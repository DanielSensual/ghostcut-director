from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
import render  # noqa: E402

PY = sys.executable
needs_ffmpeg = unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                                   "needs ffmpeg and ffprobe on PATH")
REAL_TRANSCRIBE, REAL_LOUDNESS = render._transcribe, render._loudness


def running(pid: int) -> bool:
    """The pid still names a process (reaped children don't)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# ---------- synthetic job: a cold open and two talk sections ----------

def timeline(job: dict) -> dict:
    sec = job["sections"]
    w1 = sec[1]["work"]
    return {
        "schema": "ghostcut.timeline/1", "version": "r000", "rate": [60000, 1001], "sr": 48000, "speed": 0.5,
        "frames": 1320, "duration": 22.022,
        "fixes": ["card0_text", "voice_sync", "frame_exact_joins", "phone_fps", "music_loop"],
        "cards": [{"key": "c1", "caption": "CAP", "size": 60, "until": 7.0, "lines": [["LINE ONE", 5.0]]}],
        "sections": [
            {"name": "Cold open", "kind": "intro", "start_frame": 0, "frames": 120, "t": 0.0, "d": 2.002,
             "legacy_t": 0.0, "picture": sec[0]["picture"], "audio": sec[0]["audio"], "pieces": [], "broll": []},
            {"name": "Community", "kind": "talk", "start_frame": 120, "frames": 600, "t": 2.002, "d": 10.01,
             "legacy_t": 2.0, "work": w1, "picture": sec[1]["picture"], "audio": sec[1]["audio"],
             "audio_offset": 0.021, "isolate": sec[1]["isolate"],
             "pieces": [{"A": 0.0, "d": 5.0}, {"A": 5.0, "d": 5.01}],
             "broll": [{"key": "v16:1:0", "tag": "D1", "source": str(Path(w1).parent / "D1.MOV"), "file_dur": 20.0,
                        "in": 2.0, "r": 1.0, "d": 3.0, "speed": 0.5, "reason": "", "origin": "v16"}]},
            {"name": "House tour", "kind": "talk", "start_frame": 720, "frames": 600, "t": 12.012, "d": 10.01,
             "legacy_t": 12.0, "work": sec[2]["work"], "picture": sec[2]["picture"], "audio": sec[2]["audio"],
             "audio_offset": 0.021, "pieces": [{"A": 0.0, "d": 3.0}, {"A": 3.0, "d": 3.0}, {"A": 6.0, "d": 4.0}],
             "broll": []},
        ],
    }


def fake_modules(calls: dict, *, verify_ok: bool = True, frames: int = 600, on_overlay=None, on_mix=None,
                 mix_dict: bool = False, promoted_ok: bool = True) -> dict:
    rp = types.ModuleType("render_plan")

    def load_job(demo: Path) -> dict:
        job = json.loads((demo / "job.json").read_text())
        job["facts"] = str((demo / job["facts"]).resolve())
        return job

    def make_plan(base, shown, edits, library, never_use):
        calls.setdefault("plan_bases", []).append(base["version"])
        plan = copy.deepcopy(base)
        plan.update(dirty=[], changes=[], notes=[], edits_applied=[])
        refusals = []
        lib = {e["tag"]: e for e in library}
        for e in edits:
            for op in e["ops"]:
                if op["op"] == "cut_span":
                    refusals.append({"edit": e["id"], "op": "cut_span", "label": "Cut 1.0 s",
                                     "reason": "Render v1 can't cut talk yet — ask for a note instead"})
                elif op["op"] == "swap_broll":
                    b = plan["sections"][1]["broll"][0]
                    b.update(tag=op["src"], source=lib[op["src"]]["file"])
                    plan["dirty"] = [1]
                    plan["changes"].append({"edit": e["id"], "op": "swap_broll", "section": 1, "key": b["key"],
                                            "label": f"Swap D1 → {op['src']}"})
                elif op["op"] == "add_note":
                    plan["notes"].append({"t": op["t"], "src": "you", "state": "open", "title": op["why"],
                                          "body": op["why"], "check": "Added through Muse", "edit": e["id"]})
                    plan["changes"].append({"edit": e["id"], "op": "add_note", "section": 2, "key": None,
                                            "label": "Note"})
            plan["edits_applied"].append(e["id"])
        return plan, refusals

    rp.load_job, rp.make_plan = load_job, make_plan
    rp.import_v16 = lambda job, probe=None: timeline(job)
    rp.probe_frames = lambda path: frames
    rp.S_shown = lambda p, s: p["sections"][s]["t"]
    rp.validate = lambda plan: []
    rp.fix_label = lambda tl, fix: f"[{fix}]"
    rp.plan_cards = lambda plan: {"cards": [{k: c[k] for k in ("caption", "size", "until", "lines")}
                                            for c in plan["cards"]]}

    rv = types.ModuleType("render_voice")

    def build_voice(tl, job, cache_dir, log=print):
        calls["voice"] = cache_dir
        wav = Path(cache_dir) / "abc123.wav"
        wav.write_bytes(b"RIFF")
        return wav, {"samples": 1000, "fallbacks": [{"section": 2, "piece": 1, "reason": "r 0.50"}], "pieces": [
            {"section": 1, "piece": 0, "lag_ms": 4, "r": 0.9, "isolated": True},
            {"section": 2, "piece": 0, "lag_ms": 35, "r": 0.9, "isolated": True},
            {"section": 2, "piece": 1, "lag_ms": 67, "r": 0.5, "isolated": False}]}

    rv.build_voice = build_voice

    rverify = types.ModuleType("render_verify")

    def verify(rdir, tl, plan, job, prev_transcript, fps, log=print):
        calls["verify"] = {"prev": prev_transcript, "fingerprints": fps, "tl": tl}
        checks = [{"name": "frames", "value": 1320, "threshold": 1320, "status": "pass"},
                  {"name": "phone peak", "value": 0.4, "threshold": 0.0, "status": "warn"}]
        if not verify_ok:
            checks.append({"name": "sync", "value": 40, "threshold": 5, "status": "fail"})
        (Path(rdir) / "verify.json").write_text(json.dumps(checks))
        return {"ok": verify_ok, "checks": checks}

    def verify_promoted(demo, tl, rdir=None, log=print):
        calls["promoted"] = {"edits": json.loads((Path(demo) / "edits.json").read_text()),
                             "current": json.loads((Path(demo) / "current.json").read_text())["version"]}
        return {"ok": promoted_ok, "checks": [{"name": "promoted_edits", "value": 0, "threshold": 0,
                                               "status": "pass" if promoted_ok else "fail"}]}

    rverify.verify, rverify.verify_promoted = verify, verify_promoted

    md = types.ModuleType("make_demo")

    def project_from_timeline(tl, carry, transcript_json, video, voice, music, out, skip_media=False):
        calls["project"] = {"tl": tl, "carry": carry, "transcript": transcript_json, "video": video, "voice": voice,
                            "music": music, "out": out}
        proj = {"version": tl["version"], "title": carry.get("title"), "findings": carry["findings"],
                "library": carry.get("library"), "duration": tl["duration"]}
        (out / "project.json").write_text(json.dumps(proj))
        (out / "project.js").write_text("window.PROJECT = {};\n")
        (out / "poster.jpg").write_bytes(b"new poster")
        (out / "thumbs").mkdir()
        (out / "thumbs" / "0001.jpg").write_bytes(b"t")
        return proj

    md.project_from_timeline = project_from_timeline

    dtr = types.ModuleType("director_test_run")

    def render_overlays(base, overlays, out, speed=0.5, probe=None, dry_run=False):
        calls.setdefault("overlays", []).append({"base": base, "overlays": overlays, "out": out, "speed": speed,
                                                 "probe": probe})
        argv = ["ffmpeg", "-i", str(base), *(["-t", str(probe), "-f", "null", "-"] if probe else [str(out)])]
        if dry_run:
            return argv
        if on_overlay:
            on_overlay()
        if not probe:
            Path(out).write_bytes(b"section")
        return Path(out)

    def facts(work, video, spec_file, out, probe=None):
        calls.setdefault("facts", []).append({"work": work, "video": list(video), "spec": spec_file, "out": out,
                                              "probe": probe})
        total = 0.0
        for v in video:  # real ffprobe of files that exist, even in a dry run
            r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(v)], capture_output=True, text=True)
            total += float(r.stdout.strip() or 0)
        calls["facts_total"] = total
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc", "-t", "0.01", "-f", "null", "-"],
                       check=True)
        if probe:
            return None
        Path(out).write_bytes(b"picture")
        sfx = Path(out).with_suffix(".sfx.wav")
        sfx.write_bytes(b"sfx")
        return sfx

    def mix(work, video, music, out, under=20.0, intro=0.0, isolate=None, sfx=None, voice=None):
        calls["mix"] = {"work": work, "video": video, "music": music, "out": out, "intro": intro, "sfx": sfx,
                        "voice": voice}
        if on_mix:
            on_mix()
        Path(out).write_bytes(b"master")
        (Path(work) / "music_bed.wav").write_bytes(b"bed")
        res = {"music_gain_db": -27.3, "copies": 1, "out": str(out)}
        if mix_dict:
            return {**res, "lufs": -13.9, "tp": -1.6}
        print(json.dumps(res))

    def phone_copy(master, out, passlog):
        calls["phone"] = {"master": master, "out": out, "passlog": passlog}
        Path(out).write_bytes(b"phone")
        return Path(out)

    dtr.render_overlays, dtr.facts, dtr.mix, dtr.phone_copy = render_overlays, facts, mix, phone_copy
    return {"render_plan": rp, "render_voice": rv, "render_verify": rverify, "make_demo": md,
            "director_test_run": dtr}


def edit(eid: str, op: str, **kw) -> dict:
    o = {"op": op, "target": "b1", "t": -1, "d": -1, "src": "", "lines": [], "caption": "", "why": "test", **kw}
    return {"id": eid, "request": "test", "ops": [o], "labels": [op], "usd": 0.0}


class RenderCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        ssd = tmp / "ssd" / "job"
        w1, w2 = ssd / "work1", ssd / "work2"
        for d in (w1 / "render", w2 / "render"):
            d.mkdir(parents=True)
        for f in (ssd / "intro.mp4", w1 / "v13.mp4", w2 / "v13.mp4", w1 / "render" / "joined.mp4",
                  w2 / "render" / "joined.mp4", w1 / "render" / "piece_aa.mp4", w1 / "pieces.json",
                  w1 / "broll_plan.json", w1 / "broll_map.json", w1 / "iso.mp3", w1 / "win.wav", w2 / "iso.mp3",
                  w2 / "win.wav", ssd / "D1.MOV", ssd / "D2.MOV", ssd / "music.wav", ssd / "protected.mp4",
                  ssd / "old_phone.mp4"):
            f.write_bytes(b"x")
        self.ssd, self.root = ssd, ssd / "renders"
        demo = self.demo = tmp / "demo"
        (demo / "job").mkdir(parents=True)
        (demo / "job" / "facts_base.json").write_text(json.dumps({"cards": []}))
        job = {"render_root": str(self.root), "facts": "job/facts_base.json", "music": str(ssd / "music.wav"),
               "speed": 0.5, "legacy_offsets": [0, 2.0, 12.0], "protected": [str(ssd / "protected.mp4")],
               "sections": [
                   {"name": "Cold open", "kind": "intro", "picture": str(ssd / "intro.mp4"),
                    "audio": str(ssd / "intro.mp4")},
                   {"name": "Community", "kind": "talk", "work": str(w1), "picture": str(w1 / "v13.mp4"),
                    "audio": str(w1 / "v13.mp4"),
                    "isolate": {"iso": str(w1 / "iso.mp3"), "win": str(w1 / "win.wav"), "pieces": [0, 1]}},
                   {"name": "House tour", "kind": "talk", "work": str(w2), "picture": str(w2 / "v13.mp4"),
                    "audio": str(w2 / "v13.mp4"),
                    "isolate": {"iso": str(w2 / "iso.mp3"), "win": str(w2 / "win.wav"), "pieces": [0, 1]}}]}
        (demo / "job.json").write_text(json.dumps(job))
        self.shown = {"title": "Test tour", "client": "Someone", "duration": 22.0,
                      "sections": [{"name": "Cold open", "t": 0.0, "d": 2.0},
                                   {"name": "Community", "t": 2.0, "d": 10.0},
                                   {"name": "House tour", "t": 12.0, "d": 10.0}],
                      "talk": [], "broll": [], "cards": [],
                      "library": [{"tag": "D1", "file": str(ssd / "D1.MOV"), "used": True},
                                  {"tag": "D2", "file": str(ssd / "D2.MOV"), "used": False}],
                      "neverUse": {}, "findings": [{"t": 13.0, "src": "spark", "state": "fixed", "title": "Old note",
                                                    "body": "b", "check": "c"}]}
        (demo / "project.json").write_text(json.dumps(self.shown))
        (demo / "project.js").write_text("window.PROJECT = {};\n")
        (demo / "poster.jpg").write_bytes(b"old poster")
        (demo / "thumbs").mkdir()
        (demo / "thumbs" / "0001.jpg").write_bytes(b"t")
        (demo / "transcript").mkdir()
        (demo / "transcript" / "voice.json").write_text(json.dumps({"segments": [], "old": True}))
        (demo / "video.mp4").symlink_to(ssd / "old_phone.mp4")
        self.set_edits([edit("e1", "swap_broll", src="D2")])
        self.calls: dict = {}
        render._IMPORTED.clear()
        self.use_fakes()
        lp = mock.patch.object(render, "_loudness", lambda path: (-14.2, -1.1))
        lp.start()
        self.addCleanup(lp.stop)

        def transcribe(wav, cache, log=print, popen=None):
            out = Path(cache) / f"{Path(wav).stem}.transcript.json"
            out.write_text(json.dumps({"segments": [], "new": True}))
            return out

        tp = mock.patch.object(render, "_transcribe", transcribe)
        tp.start()
        self.addCleanup(tp.stop)

    def tearDown(self) -> None:
        render._CHILDREN.clear()
        self._tmp.cleanup()

    def use_fakes(self, **kw) -> None:
        if getattr(self, "_mods", None):
            self._mods.stop()
        self._mods = mock.patch.dict(sys.modules, fake_modules(self.calls, **kw))
        self._mods.start()
        self.addCleanup(self._mods.stop)

    def set_edits(self, edits: list[dict]) -> None:
        (self.demo / "edits.json").write_text(json.dumps(edits))

    def j(self, path: Path):
        return json.loads(Path(path).read_text())


class PlanAndVersionTests(RenderCase):
    def test_next_version_never_reuses_numbers(self) -> None:
        job = render._rp().load_job(self.demo)
        self.assertEqual(render.next_version(job), "r001")
        for v in ("r000", "r001", "r003", "rx", "tmp"):
            (self.root / v).mkdir(parents=True, exist_ok=True)
        self.assertEqual(render.next_version(job), "r004")

    def test_plan_summary_lists_changes_fixes_and_estimate_and_writes_nothing(self) -> None:
        s = render.plan_summary(self.demo)
        self.assertEqual(s["next_version"], "r001")
        self.assertEqual(s["base"], "r000")
        self.assertEqual([c["label"] for c in s["changes"]], ["Swap D1 → D2"])
        self.assertEqual(s["fixes"][:2], ["[card0_text]", "[voice_sync]"])
        self.assertEqual(s["fix_keys"][1], "voice_sync")
        self.assertEqual(s["dirty"], [1])
        self.assertEqual(s["dirty_names"], ["Community"])
        self.assertEqual(s["refusals"], [])
        est = 4 + 0.6 * 10.01 + 5 + 20 + (0.37 + 0.053 + 0.15) * 22.022 + 50 + 45 + 2
        self.assertEqual(s["estimate_s"], round(est))
        self.assertFalse(self.root.exists())

    def test_cut_span_is_listed_as_refusal(self) -> None:
        self.set_edits([edit("e9", "cut_span", t=5.0, d=1.0)])
        s = render.plan_summary(self.demo)
        self.assertEqual(s["refusals"][0]["edit"], "e9")
        self.assertIn("can't cut talk", s["refusals"][0]["reason"])

    def test_steps_scale_with_program_and_dirty_sections(self) -> None:
        plan = {"duration": 852.167983, "dirty": [1], "sections": [{}, {"name": "Community", "d": 199.9}]}
        steps = dict((k, e) for k, _, e in render._steps(plan))
        self.assertAlmostEqual(steps["picture:1"], 0.6 * 199.9 + 5)
        self.assertAlmostEqual(steps["cards"], 315.3, delta=0.5)
        self.assertAlmostEqual(steps["mix"], 45.2, delta=0.5)
        self.assertAlmostEqual(steps["phone"], 127.8, delta=0.5)
        self.assertEqual([n for _, n, _ in render._steps(plan)][1:7],
                         ["Community picture", "Voice track", "Cards", "Mix", "Phone copy", "Checks"])


class RunTests(RenderCase):
    @needs_ffmpeg
    def test_run_renders_dirty_section_checks_and_promotes(self) -> None:
        res = render.run(self.demo, "r001")
        self.assertEqual(res["state"], "promoted", res["message"])
        self.assertEqual(res["message"], "r001 ready · 1 checks passed")
        rdir = self.root / "r001"
        out = rdir / "sections" / "1_community.mp4"

        ov = self.calls["overlays"]
        self.assertEqual([c["probe"] for c in ov], [1.0, None])  # smoke run first
        self.assertEqual(ov[1]["base"], self.ssd / "work1" / "render" / "joined.mp4")
        self.assertEqual(ov[1]["overlays"], [{"source": str(self.ssd / "D2.MOV"), "in": 2.0, "t": 1.0, "d": 3.0}])
        self.assertEqual(ov[1]["out"], out)
        fc = self.calls["facts"]
        self.assertEqual([c["probe"] for c in fc], [2.0, None])
        self.assertEqual(fc[1]["video"], [self.ssd / "intro.mp4", out, self.ssd / "work2" / "v13.mp4"])
        self.assertEqual(fc[1]["spec"], rdir / "cards.json")
        mx = self.calls["mix"]
        self.assertEqual((mx["voice"], mx["video"], mx["sfx"], mx["intro"]),
                         (rdir / "voice.wav", rdir / "picture.mp4", rdir / "picture.sfx.wav", 2.002))
        self.assertEqual(self.calls["phone"]["master"], rdir / "master.mp4")
        self.assertEqual(self.calls["voice"], self.root / "voice_cache")
        self.assertEqual(self.calls["verify"]["prev"], self.demo / "transcript" / "voice.json")
        self.assertIn(str(self.ssd / "protected.mp4"), self.calls["verify"]["fingerprints"])

        self.assertTrue((self.root / "r000" / "timeline.json").exists())
        tl = self.j(rdir / "timeline.json")
        self.assertEqual((tl["version"], tl["parent"], tl["edits_applied"]), ("r001", "r000", ["e1"]))
        self.assertEqual(tl["mix"], {"music_gain_db": -27.3, "lufs": -14.2, "tp": -1.1})
        self.assertEqual(tl["files"]["transcript"], "transcript/voice.json")
        self.assertEqual(tl["fixes"][1], "voice_sync")
        s1, s2 = tl["sections"][1], tl["sections"][2]
        self.assertEqual((s1["picture"], s1["picture_rendered"]), (str(out), True))
        self.assertEqual((s2["picture"], s2["picture_rendered"]), (str(self.ssd / "work2" / "v13.mp4"), False))
        self.assertEqual(s1["isolated"], [{"piece": 0, "lag_ms": 4, "r": 0.9}])
        self.assertEqual(s2["camera_fallback"], [1])
        self.assertNotIn("dirty", tl)
        self.assertEqual(self.j(rdir / "plan.json")["dirty"], [1])
        self.assertEqual(self.j(rdir / "transcript" / "voice.json"), {"segments": [], "new": True})
        self.assertTrue(os.path.samefile(rdir / "voice.wav", self.root / "voice_cache" / "abc123.wav"))

        # promoted: live files swapped, old ones kept for Revert, edits reset, current.json written last
        self.assertEqual(self.j(self.demo / "project.json")["version"], "r001")
        self.assertEqual(self.j(self.demo / "edits.json"), [])
        self.assertEqual(os.readlink(self.demo / "video.mp4"), str((rdir / "phone.mp4").resolve()))
        self.assertEqual(self.j(self.demo / "transcript" / "voice.json"), {"segments": [], "new": True})
        cur = self.j(self.demo / "current.json")
        self.assertEqual(cur, {"version": "r001", "dir": str(rdir), "timeline": str(rdir / "timeline.json"),
                               "previous": "legacy"})
        hist = self.demo / ".history" / "legacy"
        self.assertEqual(self.j(hist / "project.json")["title"], "Test tour")
        self.assertEqual([e["id"] for e in self.j(hist / "edits.json")], ["e1"])
        self.assertEqual((hist / "poster.jpg").read_bytes(), b"old poster")
        self.assertFalse((self.demo / ".next-r001").exists())
        self.assertFalse((self.root / "render.lock").exists())
        prog = self.j(rdir / "progress.json")
        self.assertEqual((prog["state"], prog["pct"], prog["eta_s"]), ("promoted", 100.0, 0))

        carry = self.calls["project"]["carry"]  # the shown project: make_demo rebases its findings itself
        self.assertEqual(carry["findings"], self.shown["findings"])
        self.assertEqual(carry["sections"], self.shown["sections"])
        self.assertEqual(carry["library"], self.shown["library"])
        self.assertEqual(self.calls["project"]["video"], rdir / "phone.mp4")
        self.assertEqual(self.calls["project"]["music"], rdir / "music_bed.wav")
        self.assertEqual(self.calls["project"]["out"], self.demo / ".next-r001")
        self.assertEqual(tl["notes"], [])
        self.assertNotIn("tags", tl)
        self.assertEqual(self.calls["promoted"], {"edits": [], "current": "r001"})

        st = render.status(self.demo)
        self.assertEqual((st["state"], st["version"], st["current"]), ("promoted", "r001", "r001"))

    @needs_ffmpeg
    def test_revert_restores_previous_cut_and_its_edits(self) -> None:
        self.assertEqual(render.run(self.demo, "r001")["state"], "promoted")
        self.assertEqual(render.revert(self.demo), {"version": "legacy", "reverted": "r001"})
        self.assertEqual(self.j(self.demo / "project.json")["title"], "Test tour")
        self.assertNotIn("version", self.j(self.demo / "project.json"))
        self.assertEqual([e["id"] for e in self.j(self.demo / "edits.json")], ["e1"])
        self.assertEqual(os.readlink(self.demo / "video.mp4"), str(self.ssd / "old_phone.mp4"))
        self.assertFalse((self.demo / "current.json").exists())
        self.assertEqual(self.j(self.demo / ".history" / "r001" / "project.json")["version"], "r001")
        self.assertEqual(self.j(self.demo / ".history" / "r001" / "current.json")["version"], "r001")
        self.assertFalse((self.demo / ".history" / "legacy").exists())
        self.assertEqual(render.status(self.demo)["state"], "idle")
        with self.assertRaises(ValueError):
            render.revert(self.demo)
        # the next render after a revert gets a new number and keeps both histories apart
        self.assertEqual(render.run(self.demo, "r002")["state"], "promoted")
        self.assertEqual(self.j(self.demo / "current.json")["previous"], "legacy")

    @needs_ffmpeg
    def test_revert_refuses_while_accepted_edits_wait_to_render(self) -> None:
        self.assertEqual(render.run(self.demo, "r001")["state"], "promoted")
        e5, e6 = edit("e5", "add_note", t=15.0, why="Porch"), edit("e6", "add_note", t=16.0, why="Door")
        for pending, msg in (([e5], "1 accepted change is waiting to render on r001: undo or render it first"),
                             ([e5, e6], "2 accepted changes are waiting to render on r001: undo or render them first")):
            self.set_edits(pending)
            with self.assertRaises(render.Busy) as cm:
                render.revert(self.demo)
            self.assertEqual(str(cm.exception), msg)
            # nothing moved: the edits stay live on r001 and no history was written for it
            self.assertEqual(self.j(self.demo / "edits.json"), pending)
            self.assertEqual(self.j(self.demo / "current.json")["version"], "r001")
            self.assertEqual(self.j(self.demo / "project.json")["version"], "r001")
            self.assertFalse((self.demo / ".history" / "r001").exists())
        self.set_edits([])
        self.assertEqual(render.revert(self.demo), {"version": "legacy", "reverted": "r001"})

    @needs_ffmpeg
    def test_a_failed_run_stops_whisper_before_it_releases_the_lock(self) -> None:
        pidf = Path(self._tmp.name) / "whisper.pid"
        whisper = ("import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                   "time.sleep(30)")
        self.addCleanup(lambda: pidf.exists() and running(int(pidf.read_text())) and
                        os.kill(int(pidf.read_text()), signal.SIGKILL))

        def mix_fails_once_whisper_runs():
            for _ in range(200):
                if pidf.exists() and pidf.read_text():
                    break
                time.sleep(0.05)
            raise RuntimeError("mix broke")

        seen, release = {}, render.Run._release

        def check_release(run):
            seen["whisper_running"] = running(int(pidf.read_text()))
            release(run)

        self.use_fakes(on_mix=mix_fails_once_whisper_runs)
        with mock.patch.object(render, "_transcribe", REAL_TRANSCRIBE), \
                mock.patch.object(render, "_whisper_argv", lambda wav, out, name: [PY, "-c", whisper, str(pidf)]), \
                mock.patch.object(render.Run, "_release", check_release):
            t0 = time.time()
            res = render.run(self.demo, "r001")
        self.assertEqual(res["state"], "failed")
        self.assertIn("mix broke", res["message"])
        self.assertIs(seen["whisper_running"], False)  # stopped and reaped before render.lock went
        self.assertFalse(running(int(pidf.read_text())))
        self.assertLess(time.time() - t0, 15)
        self.assertFalse((self.root / "render.lock").exists())
        cache = self.root / "voice_cache"
        self.assertEqual(list(cache.glob(".whisper-*")), [])
        self.assertFalse((cache / "abc123.transcript.json").exists())

    @needs_ffmpeg
    def test_next_render_builds_on_current_timeline(self) -> None:
        self.assertEqual(render.run(self.demo, "r001")["state"], "promoted")
        res = render.run(self.demo, "r002")  # nothing pending and no base repairs left
        self.assertEqual(res["state"], "refused")
        self.assertIn("Nothing to render", res["message"])
        self.assertEqual(self.calls["plan_bases"][-1], "r001")
        self.assertEqual(self.j(self.root / "r002" / "progress.json")["state"], "refused")
        self.assertEqual(self.j(self.demo / "current.json")["version"], "r001")
        again = render.run(self.demo, "r002")  # a finished version is never run again or overwritten
        self.assertEqual(again["state"], "refused")
        self.assertIn("never reused", again["message"])
        self.assertIn("Nothing to render", self.j(self.root / "r002" / "progress.json")["message"])

    def test_refused_edit_encodes_nothing(self) -> None:
        self.set_edits([edit("e1", "swap_broll", src="D2"), edit("e2", "cut_span", t=5.0, d=1.0)])
        res = render.run(self.demo, "r001")
        self.assertEqual(res["state"], "refused")
        self.assertIn("can't cut talk", res["message"])
        self.assertNotIn("overlays", self.calls)
        self.assertFalse((self.root / "r001" / "plan.json").exists())
        self.assertFalse((self.demo / "current.json").exists())
        self.assertFalse((self.root / "render.lock").exists())

    @needs_ffmpeg
    def test_failed_checks_keep_the_current_cut(self) -> None:
        self.use_fakes(verify_ok=False, mix_dict=True)
        res = render.run(self.demo, "r001")
        self.assertEqual(self.j(self.root / "r001" / "timeline.json")["mix"],
                         {"music_gain_db": -27.3, "lufs": -13.9, "tp": -1.6})  # mix's own measurement
        self.assertEqual(res["state"], "failed_verification")
        self.assertEqual(res["message"], "Failed checks: sync")
        self.assertTrue((self.root / "r001" / "master.mp4").exists())  # kept for inspection
        self.assertNotIn("project", self.calls)
        self.assertFalse((self.demo / "current.json").exists())
        self.assertEqual(self.j(self.demo / "project.json")["title"], "Test tour")
        self.assertEqual(render.status(self.demo)["state"], "failed_verification")

    @needs_ffmpeg
    def test_source_file_changed_mid_render_fails(self) -> None:
        def touch():
            with open(self.ssd / "protected.mp4", "ab") as f:
                f.write(b"changed")
        self.use_fakes(on_mix=touch)
        res = render.run(self.demo, "r001")
        self.assertEqual(res["state"], "failed_verification")
        self.assertIn("protected.mp4", res["message"])

    def test_frame_count_mismatch_fails_before_voice(self) -> None:
        self.use_fakes(frames=599)
        res = render.run(self.demo, "r001")
        self.assertEqual(res["state"], "failed")
        self.assertIn("599 frames, expected 600", res["message"])
        self.assertNotIn("voice", self.calls)

    @needs_ffmpeg
    def test_notes_ride_in_the_timeline_and_after_promote_problems_are_reported(self) -> None:
        self.set_edits([edit("e1", "swap_broll", src="D2"), edit("e2", "add_note", t=15.0, why="Reshoot the porch")])
        self.use_fakes(promoted_ok=False)
        res = render.run(self.demo, "r001")
        self.assertEqual(res["state"], "promoted")  # already live: reported, not rolled back
        self.assertIn("after promote: promoted_edits", res["message"])
        tl = self.j(self.root / "r001" / "timeline.json")
        self.assertEqual([n["title"] for n in tl["notes"]], ["Reshoot the porch"])
        self.assertEqual(tl["edits_applied"], ["e1", "e2"])
        self.assertEqual(self.calls["project"]["tl"]["notes"][0]["src"], "you")

    def test_sigterm_cancels_and_restores_handler(self) -> None:
        before = signal.getsignal(signal.SIGTERM)
        self.use_fakes(on_overlay=lambda: os.kill(os.getpid(), signal.SIGTERM))
        res = render.run(self.demo, "r001")
        self.assertEqual(res["state"], "cancelled")
        self.assertEqual(self.j(self.root / "r001" / "progress.json")["state"], "cancelled")
        self.assertFalse((self.root / "render.lock").exists())
        self.assertFalse((self.demo / "current.json").exists())
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_writes_are_confined_to_render_folders(self) -> None:
        r = render.Run(self.demo, "r001", render._rp().load_job(self.demo))
        r.w(self.root / "r001" / "sections" / "1_community.mp4")
        r.w(self.root / "voice_cache" / "x.wav")
        r.w(self.demo / ".next-r001" / "thumbs" / "0001.jpg")
        r.w(self.demo / "current.json")
        for bad in (self.demo / "project.json", self.demo / "edits.json", self.root / "r002" / "x",
                    self.ssd / "work1" / "render" / "joined.mp4", self.root / "r001" / ".." / "r000" / "x"):
            with self.assertRaises(PermissionError, msg=str(bad)):
                r.w(bad)
        with self.assertRaises(ValueError):
            render.Run(self.demo, "../r001", {"render_root": str(self.root)})

    def test_progress_is_weighted_by_step_estimates(self) -> None:
        r = render.Run(self.demo, "r001", render._rp().load_job(self.demo))
        r.steps = [("a", "A", 10.0), ("b", "B", 30.0), ("c", "C", 60.0)]
        r.i, r.step_t0 = 1, time.time() - 15
        s = r.snapshot()
        self.assertEqual((s["step"], s["i"], s["n"]), ("B", 2, 3))
        self.assertAlmostEqual(s["pct"], 25.0, delta=0.2)  # (10 + 0.5 * 30) / 100
        self.assertAlmostEqual(s["eta_s"], 75, delta=1)
        r.step_t0 = time.time() - 300  # an overrun step holds at 95 % of its share
        self.assertAlmostEqual(r.snapshot()["pct"], 38.5, delta=0.2)


class EngineSideTests(RenderCase):
    def test_start_spawns_niced_child_in_own_session_and_locks(self) -> None:
        seen = {}

        class FakePopen:
            pid = 4_000_000

            def __init__(self, argv, **kw):
                seen.update(argv=argv, **kw)

            def poll(self):
                return None

        with mock.patch.object(render.subprocess, "Popen", FakePopen):
            res = render.start(self.demo)
        self.assertEqual(res, {"version": "r001", "pid": 4_000_000})
        self.assertEqual(seen["argv"], ["nice", "-n", "10", sys.executable, str(APP / "render.py"), "run",
                                        "--version", "r001", "--demo", str(self.demo)])
        self.assertTrue(seen["start_new_session"])
        self.assertTrue(seen["env"]["PATH"].startswith("/opt/homebrew/bin:"))
        self.assertEqual(seen["cwd"], str(APP.parent))
        lock = self.j(self.root / "render.lock")
        self.assertEqual((lock["pid"], lock["version"]), (4_000_000, "r001"))
        self.assertEqual([e["id"] for e in self.j(self.root / "r001" / "edits_applied.json")], ["e1"])
        st = render.status(self.demo)
        self.assertEqual((st["state"], st["version"], st["step"]), ("running", "r001", "Starting"))
        with self.assertRaises(render.Busy):
            render.start(self.demo)
        with self.assertRaises(render.Busy):
            render.revert(self.demo)

    def test_start_refuses_before_spawning(self) -> None:
        self.set_edits([edit("e7", "cut_span", t=5.0, d=1.0)])
        with mock.patch.object(render.subprocess, "Popen") as popen:
            with self.assertRaises(render.Refused) as cm:
                render.start(self.demo)
        popen.assert_not_called()
        self.assertIn("e7", str(cm.exception))
        self.assertFalse((self.root / "render.lock").exists())
        self.assertFalse((self.root / "r001").exists())

    def test_start_refuses_missing_sources(self) -> None:
        (self.ssd / "D2.MOV").unlink()
        with mock.patch.object(render.subprocess, "Popen") as popen:
            with self.assertRaises(FileNotFoundError):
                render.start(self.demo)
        popen.assert_not_called()
        self.assertFalse((self.root / "render.lock").exists())

    def test_status_idle_then_interrupted_for_a_dead_pid(self) -> None:
        self.assertEqual(render.status(self.demo)["state"], "idle")
        p = subprocess.Popen([PY, "-c", "pass"])
        p.wait()
        (self.root / "r001").mkdir(parents=True)
        render._write_json(self.root / "r001" / "progress.json", {"state": "running", "step": "Cards"})
        render._write_json(self.root / "render.lock", {"pid": p.pid, "version": "r001"})
        st = render.status(self.demo)
        self.assertEqual((st["state"], st["version"]), ("interrupted", "r001"))
        # a new start clears the stale lock and records the interruption
        with mock.patch.object(render.subprocess, "Popen") as popen:
            popen.return_value.pid = 4_000_001
            popen.return_value.poll.return_value = None
            self.assertEqual(render.start(self.demo)["version"], "r002")
        self.assertEqual(self.j(self.root / "r001" / "progress.json")["state"], "interrupted")

    def test_alive_never_addresses_a_process_group(self) -> None:
        self.assertFalse(render._alive(0))
        self.assertFalse(render._alive(-1))
        self.assertFalse(render._alive(None))
        self.assertTrue(render._alive(os.getpid()))

    def test_cancel_terminates_the_process_group(self) -> None:
        child = subprocess.Popen([PY, "-c", "import time; time.sleep(30)", "render.py"], start_new_session=True)
        render._CHILDREN[child.pid] = child
        (self.root / "r001").mkdir(parents=True)
        render._write_json(self.root / "r001" / "progress.json", {"state": "running", "step": "Cards"})
        render._write_json(self.root / "render.lock", {"pid": child.pid, "version": "r001"})
        st = render.cancel(self.demo)
        self.assertIsNotNone(child.poll())
        self.assertEqual(st["state"], "cancelled")
        self.assertFalse((self.root / "render.lock").exists())

    def test_cancel_leaves_a_foreign_pid_alone(self) -> None:
        child = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"], start_new_session=True)
        self.addCleanup(lambda: (child.kill(), child.wait()))
        render._CHILDREN[child.pid] = child
        (self.root / "r001").mkdir(parents=True)
        render._write_json(self.root / "r001" / "progress.json", {"state": "running", "step": "Cards"})
        render._write_json(self.root / "render.lock", {"pid": child.pid, "version": "r001"})
        render.cancel(self.demo)
        self.assertIsNone(child.poll())

    def test_an_unreadable_lock_blocks_start_only_while_it_is_fresh(self) -> None:
        self.root.mkdir(parents=True)
        lock = self.root / "render.lock"
        lock.write_text("")  # the engine died between creating the lock and writing it
        self.assertEqual(render.status(self.demo)["state"], "idle")
        with mock.patch.object(render.subprocess, "Popen") as popen:
            popen.return_value.pid = 4_000_002
            popen.return_value.poll.return_value = None
            with self.assertRaisesRegex(render.Busy, "Another render is starting"):
                render.start(self.demo)
            popen.assert_not_called()
            old = time.time() - 60
            os.utime(lock, (old, old))
            self.assertEqual(render.start(self.demo)["version"], "r001")
        self.assertEqual(self.j(lock)["pid"], 4_000_002)

    @needs_ffmpeg
    def test_a_reused_pid_does_not_hold_the_render_lock(self) -> None:
        other = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"])  # alive, but not render.py
        self.addCleanup(lambda: (other.kill(), other.wait()))
        (self.root / "r001").mkdir(parents=True)
        render._write_json(self.root / "r001" / "progress.json", {"state": "running", "step": "Cards"})
        render._write_json(self.root / "render.lock", {"pid": other.pid, "version": "r001"})
        self.assertEqual(render.status(self.demo)["state"], "interrupted")
        with self.assertRaisesRegex(ValueError, "Nothing to revert"):  # not Busy
            render.revert(self.demo)
        real = subprocess.Popen

        def popen(argv, **kw):  # the ps behind the render.py check runs for real; the render child is faked
            return real(argv, **kw) if argv[0] == "ps" else mock.Mock(pid=4_000_003, **{"poll.return_value": None})

        with mock.patch.object(render.subprocess, "Popen", popen):
            self.assertEqual(render.start(self.demo)["version"], "r002")
        self.assertEqual(self.j(self.root / "r001" / "progress.json")["state"], "interrupted")
        # the render child's own check (Run._own) doesn't take the reused pid for a render either
        render._CHILDREN.clear()
        render._write_json(self.root / "render.lock", {"pid": other.pid, "version": "r001"})
        res = render.run(self.demo, "r002")
        self.assertEqual(res["state"], "promoted", res["message"])
        self.assertIsNone(other.poll())

    @needs_ffmpeg
    def test_start_refuses_when_a_reused_section_picture_is_gone(self) -> None:
        self.assertEqual(render.run(self.demo, "r001")["state"], "promoted")
        (self.root / "r001" / "sections" / "1_community.mp4").unlink()  # r001 trimmed to free space, say
        self.set_edits([edit("e2", "add_note", t=15.0, why="Porch")])  # nothing dirty: Community stays r001's file
        with mock.patch.object(render.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(FileNotFoundError, "1_community.mp4"):
                render.start(self.demo)
        popen.assert_not_called()
        self.assertFalse((self.root / "render.lock").exists())
        self.assertFalse((self.root / "r002").exists())


class CliAndHelperTests(RenderCase):
    @needs_ffmpeg
    def test_dry_run_prints_every_encode_and_writes_nothing(self) -> None:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=64x36:r=30:d=1", "-c:v",
                        "libx264", "-pix_fmt", "yuv420p", str(self.ssd / "intro.mp4")], check=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = render.main(["run", "--version", "r001", "--dry-run", "--demo", str(self.demo)])
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertEqual(sum(ln.startswith("ffmpeg -i ") and "joined.mp4" in ln for ln in out.splitlines()), 2)
        self.assertEqual(out.count("anullsrc"), 2)  # the facts ffmpeg calls were printed, not run
        self.assertIn("mlx_whisper", out)
        self.assertIn("--word-timestamps True", out)
        # ffprobe still runs on files that exist; the planned section is given its own length
        self.assertAlmostEqual(self.calls["facts_total"], 1.0 + 10.01, delta=0.1)
        self.assertFalse(self.root.exists())

    def test_cli_plan_prints_summary(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            render.main(["plan", "--demo", str(self.demo)])
        self.assertEqual(json.loads(buf.getvalue())["next_version"], "r001")

    def test_transcribe_runs_whisper_once_per_voice(self) -> None:
        cache = Path(self._tmp.name) / "cache"
        cache.mkdir()
        wav = cache / "deadbeef.wav"
        wav.write_bytes(b"RIFF")
        seen = []

        class FakeWhisper:
            def __init__(self, argv, **kw):
                seen.append(argv)
                self.args, self.returncode = argv, 0
                out_dir, name = Path(argv[argv.index("--output-dir") + 1]), argv[argv.index("--output-name") + 1]
                (out_dir / f"{name}.json").write_text('{"segments": []}')

            def wait(self, timeout=None):
                return 0

        with mock.patch.object(render.subprocess, "Popen", FakeWhisper), \
                mock.patch.object(render.subprocess, "run", side_effect=AssertionError("Whisper runs through Popen")):
            a = REAL_TRANSCRIBE(wav, cache, log=lambda m: None)
            b = REAL_TRANSCRIBE(wav, cache, log=lambda m: None)
        self.assertEqual(a, cache / "deadbeef.transcript.json")
        self.assertEqual(a, b)
        self.assertEqual(len(seen), 1)
        argv = seen[0]
        for flag, val in (("--model", render.WHISPER), ("--language", "en"), ("--output-format", "json"),
                          ("--condition-on-previous-text", "False"), ("--word-timestamps", "True")):
            self.assertEqual(argv[argv.index(flag) + 1], val)
        self.assertEqual(list(cache.glob(".whisper-*")), [])

    def test_two_transcribes_of_one_voice_keep_their_own_whisper_folders(self) -> None:
        cache = Path(self._tmp.name) / "cache"
        cache.mkdir()
        wav = cache / "deadbeef.wav"
        wav.write_bytes(b"RIFF")
        whisper = ("import pathlib, sys, time; time.sleep(float(sys.argv[3])); "
                   "pathlib.Path(sys.argv[1], sys.argv[2] + '.json').write_text('{\"segments\": []}')")
        delays, first, res, errs = [1.0, 0.1], threading.Event(), [], []

        def argv(w, out_dir, name):  # the first run is still transcribing when the second one finishes
            first.set()
            return [PY, "-c", whisper, str(out_dir), name, str(delays.pop(0))]

        def one():
            try:
                res.append(REAL_TRANSCRIBE(wav, cache, log=lambda m: None))
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        with mock.patch.object(render, "_whisper_argv", argv):
            a = threading.Thread(target=one)
            a.start()
            self.assertTrue(first.wait(5))
            b = threading.Thread(target=one)
            b.start()
            a.join(15)
            b.join(15)
        self.assertEqual(errs, [])
        self.assertEqual(res, [cache / "deadbeef.transcript.json"] * 2)
        self.assertEqual(self.j(res[0]), {"segments": []})
        self.assertEqual(list(cache.glob(".whisper-*")), [])

    @needs_ffmpeg
    def test_loudness_reads_ebur128_summary(self) -> None:
        wav = Path(self._tmp.name) / "tone.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=3:sample_rate=48000",
                        "-af", "volume=-12dB", "-ac", "2", str(wav)], check=True)
        lufs, tp = REAL_LOUDNESS(wav)
        self.assertTrue(-60 < lufs < -5, lufs)
        self.assertTrue(-60 < tp <= 0, tp)
        self.assertLess(abs(tp - lufs), 3)  # a steady sine: true peak sits next to its loudness

    def test_call_json_reads_a_printed_summary(self) -> None:
        def step():
            print("noise")
            print(json.dumps({"music_gain_db": -3.5}))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(render._call_json(step), {"music_gain_db": -3.5})
            self.assertEqual(render._call_json(lambda: {"a": 1}), {"a": 1})

    def test_fix_labels(self) -> None:
        self.assertEqual(render._fix_label("phone_fps", {}), "[phone_fps]")  # render_plan.fix_label when present
        del sys.modules["render_plan"].fix_label
        self.assertEqual(render._fix_label("phone_fps", {}), "Phone copy timing")
        self.assertEqual(render._fixes({"version": "r001", "fixes": ["voice_sync"]}, {}), [])
        self.assertEqual(render._fixes({"version": "r000", "fixes": ["voice_sync"]}, {}), ["voice_sync"])


class HygieneTests(unittest.TestCase):
    def test_no_absolute_job_paths_in_render(self) -> None:
        text = (APP / "render.py").read_text()
        self.assertNotIn("/Volumes/", text)
        self.assertNotIn("/Users/", text)

    def test_import_and_encode_helpers_never_load_spark_clients(self) -> None:
        code = ("import sys; sys.path.insert(0, 'app'); import render; a = sorted(sys.modules); render._dtr(); "
                "print(sorted(m for m in sys.modules if m.split('.')[0] in ('guestlist', 'openai')), "
                "'director_spike_takes' in sys.modules)")
        r = subprocess.run([PY, "-c", code], cwd=APP.parent, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "[] False")


if __name__ == "__main__":
    unittest.main()
