"""Engine side of Render: endpoints, the edit freeze while a render runs, the promote watcher, revert, and the UI wiring.

app/render.py is a separate module; these tests drive the engine through a stand-in with the same interface.
"""
from __future__ import annotations

import http.client
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
import engine  # noqa: E402
import muse  # noqa: E402


class FakeBusy(RuntimeError):
    pass


class FakeRender:
    """The engine-facing interface of app/render.py: status, plan_summary, start, cancel, revert."""
    Busy = FakeBusy

    def __init__(self) -> None:
        self.st: dict | Exception = {"state": "idle", "version": None}
        self.calls: list[str] = []
        self.start_error: Exception | None = None
        self.on_revert = None
        self.plan = {"next_version": "r001", "changes": [{"edit": "e1", "op": "swap_broll", "section": 1,
                                                          "key": "base:1:0", "label": "Swap D1 → D2"}],
                     "fixes": ["voice_sync"], "refusals": [], "notes": [], "estimate_s": 720, "dirty": [1]}

    def status(self, demo: Path) -> dict:
        if isinstance(self.st, Exception):
            raise self.st
        return dict(self.st)

    def plan_summary(self, demo: Path) -> dict:
        return self.plan

    def start(self, demo: Path) -> dict:
        self.calls.append("start")
        if self.start_error:
            raise self.start_error
        self.st = {"state": "running", "version": "r001", "step": "Community picture", "i": 1, "n": 9, "pct": 3.0,
                   "eta_s": 700, "message": "", "log": ""}
        return {"version": "r001", "pid": 4242}

    def cancel(self, demo: Path) -> None:
        self.calls.append("cancel")
        self.st = {"state": "cancelled", "version": "r001"}

    def revert(self, demo: Path) -> dict:
        self.calls.append("revert")
        if isinstance(self.on_revert, Exception):
            raise self.on_revert
        if self.on_revert:
            self.on_revert()
        return {"version": None}


def project(version: str | None = None) -> dict:
    p = {"title": "Cut", "duration": 60.0,
         "sections": [{"name": "Open", "t": 0.0, "d": 10.0}, {"name": "Talk", "t": 10.0, "d": 50.0}],
         "talk": [], "cards": [], "library": [], "findings": [],
         "broll": [{"id": "b1", "tag": "D1", "src": "D1", "t": 20.0, "d": 3.0, "topic": "", "reason": ""}]}
    if version:
        p["version"] = version
    return p


def edit(eid: str) -> dict:
    op = {"op": "swap_broll", "target": "b1", "t": -1, "d": -1, "src": "D2", "lines": [], "caption": "", "why": "test"}
    return {"id": eid, "request": "swap it", "ops": [op], "labels": ["Swap D1 → D2"], "usd": 0.0}


def proposal() -> dict:
    op = {"op": "add_note", "target": "", "t": 30.0, "d": -1, "src": "", "lines": [], "caption": "", "why": "a note",
          "checks": [], "blocked": False, "label": "Note at 30.0 s"}
    return {"request": "note this", "ops": [op], "usd": 0.0}


class Server:
    def __init__(self, eng: engine.Engine) -> None:
        handler = type("H", (engine.Handler,), {"engine": eng})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(handler, directory=str(engine.ROOT)))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def call(self, method: str, path: str, body: dict | None = None, host: str | None = None,
             ctype: str = "application/json") -> tuple[int, bytes]:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest(method, path, skip_host=True)
        c.putheader("Host", host or f"127.0.0.1:{self.port}")
        data = json.dumps(body if body is not None else {}).encode() if method == "POST" else None
        if data is not None:
            c.putheader("Content-Type", ctype)
            c.putheader("Content-Length", str(len(data)))
        c.endheaders(data)
        r = c.getresponse()
        out = r.status, r.read()
        c.close()
        return out

    def json(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        code, raw = self.call(method, path, body)
        return code, json.loads(raw)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.demo = self.tmp / "demo"
        self.demo.mkdir()
        self.write(project(), [edit("e1")])
        self.fake = FakeRender()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, p: dict, edits: list[dict]) -> None:
        (self.demo / "project.json").write_text(json.dumps(p))
        (self.demo / "edits.json").write_text(json.dumps(edits))

    def promote(self, version: str, applied: list[str], relative: bool = False) -> None:
        """What a finished render leaves behind: a new project, its timeline and the current.json pointer."""
        rdir = self.tmp / "renders" / version
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "timeline.json").write_text(json.dumps({"version": version, "edits_applied": applied}))
        (self.demo / "project.json").write_text(json.dumps(project(version)))
        cur = {"version": version, "dir": f"../renders/{version}"} if relative else \
            {"version": version, "dir": str(rdir), "timeline": str(rdir / "timeline.json"), "previous": None}
        (self.demo / "current.json").write_text(json.dumps(cur))

    def saved_ids(self) -> list[str]:
        return [e["id"] for e in json.loads((self.demo / "edits.json").read_text())]


class EngineRenderTests(Base):
    def test_project_reports_render_state(self) -> None:
        self.fake.st = {"state": "running", "version": "r002", "pct": 40.0}
        eng = engine.Engine(self.demo, render=self.fake)
        self.assertEqual(eng.project()["engine"]["render"], {"state": "running", "version": "r002"})

    def test_edits_are_frozen_while_rendering(self) -> None:
        eng = engine.Engine(self.demo, render=self.fake)
        eng.proposals["p1"] = proposal()
        eng.start_render()
        with self.assertRaises(engine.Busy):
            eng.accept("p1", None)
        with self.assertRaises(engine.Busy):
            eng.undo(None)
        self.assertEqual(self.saved_ids(), ["e1"])
        self.assertIn("p1", eng.proposals)
        self.fake.st = {"state": "failed", "version": "r001", "message": "drive gone"}
        self.assertEqual(len(eng.accept("p1", None)["edits"]), 2)

    def test_status_failure_mid_render_keeps_edits_frozen(self) -> None:
        eng = engine.Engine(self.demo, render=self.fake)
        eng.start_render()
        self.fake.st = OSError("render root unreadable")
        eng.poll_render()
        with self.assertRaises(engine.Busy):
            eng.undo(None)

    def test_promotion_reloads_the_cut_drops_applied_edits_and_clears_proposals(self) -> None:
        self.write(project(), [edit("e1"), edit("e2")])
        eng = engine.Engine(self.demo, render=self.fake)
        eng.start_render()
        eng.proposals["p1"] = proposal()
        self.promote("r001", ["e1"])  # edits.json still lists e1, as after a crash between the swap and the reset
        self.fake.st = {"state": "promoted", "version": "r001"}
        eng.poll_render()
        self.assertEqual(eng.base["version"], "r001")
        self.assertEqual([e["id"] for e in eng.edits], ["e2"])
        self.assertEqual(self.saved_ids(), ["e2"])
        self.assertEqual(eng.proposals, {})
        self.assertEqual(eng.project()["engine"]["render"], {"state": "promoted", "version": "r001"})
        (self.demo / "project.json").write_text(json.dumps(project("rX")))
        eng.poll_render()  # the same promotion again: nothing to reload
        self.assertEqual(eng.base["version"], "r001")

    def test_proposal_made_on_the_previous_cut_is_refused(self) -> None:
        eng = engine.Engine(self.demo, render=self.fake)
        eng.proposals["p1"] = {**proposal(), "gen": eng.gen}
        self.promote("r001", ["e1"])
        self.fake.st = {"state": "promoted", "version": "r001"}
        eng.poll_render()
        eng.proposals["p1"] = {**proposal(), "gen": eng.gen - 1}  # an ask that was in flight during the reload
        with self.assertRaisesRegex(KeyError, "previous version"):
            eng.accept("p1", None)

    def test_engine_start_drops_edits_the_current_version_applied(self) -> None:
        self.promote("r003", ["e1"], relative=True)
        self.write(project("r003"), [edit("e1"), edit("e9")])
        self.fake.st = {"state": "promoted", "version": "r003"}
        eng = engine.Engine(self.demo, render=self.fake)
        self.assertEqual([e["id"] for e in eng.edits], ["e9"])
        self.assertEqual(self.saved_ids(), ["e9"])
        self.assertEqual(eng.project()["project"]["pending"], 1)

    def test_legacy_project_without_pointer_keeps_its_edits(self) -> None:
        eng = engine.Engine(self.demo, render=self.fake)
        self.assertEqual([e["id"] for e in eng.edits], ["e1"])

    def test_start_is_single_flight_and_preflight_refusals_are_400(self) -> None:
        eng = engine.Engine(self.demo, render=self.fake)
        self.assertEqual(eng.start_render(), {"version": "r001", "pid": 4242})
        with self.assertRaises(engine.Busy):
            eng.start_render()
        self.assertEqual(self.fake.calls, ["start"])
        fake = FakeRender()
        fake.start_error = RuntimeError("The source drive is not mounted")
        with self.assertRaisesRegex(ValueError, "not mounted"):
            engine.Engine(self.demo, render=fake).start_render()

    def test_render_busy_errors_are_409(self) -> None:
        self.fake.start_error = FakeBusy("r004 is rendering.")
        eng = engine.Engine(self.demo, render=self.fake)
        with self.assertRaisesRegex(engine.Busy, "r004 is rendering"):
            eng.start_render()
        self.fake.on_revert = FakeBusy("A render is running.")
        with self.assertRaises(engine.Busy):
            eng.revert_render()
        self.fake.on_revert = RuntimeError("disk said no")
        with self.assertRaisesRegex(ValueError, "disk said no"):
            eng.revert_render()

    def test_revert_is_idle_only_and_reloads(self) -> None:
        self.promote("r001", [])
        self.write(project("r001"), [])
        eng = engine.Engine(self.demo, render=self.fake)
        eng.start_render()
        with self.assertRaises(engine.Busy):
            eng.revert_render()
        self.assertNotIn("revert", self.fake.calls)
        self.fake.st = {"state": "promoted", "version": "r001"}

        def restore() -> None:
            (self.demo / "current.json").unlink()
            self.write(project(), [edit("e1")])
        self.fake.on_revert = restore
        eng.proposals["p1"] = proposal()
        out = eng.revert_render()
        self.assertNotIn("version", out["project"])
        self.assertEqual([e["id"] for e in out["edits"]], ["e1"])
        self.assertEqual(eng.proposals, {})
        self.assertEqual(out["revert"], {"version": None})

    def test_a_revert_from_the_cli_reloads_before_the_next_accept(self) -> None:
        self.promote("r001", ["e1"])
        self.write(project("r001"), [])
        self.fake.st = {"state": "promoted", "version": "r001", "current": "r001"}
        eng = engine.Engine(self.demo, render=self.fake)
        eng.proposals["p1"] = {**proposal(), "gen": eng.gen}
        # `python app/render.py revert` in a terminal: the original cut is back, with e1 pending again.
        (self.demo / "current.json").unlink()
        self.write(project(), [edit("e1")])
        self.fake.st = {"state": "idle", "version": "r001", "current": None, "message": "r001 was reverted"}
        with self.assertRaisesRegex(KeyError, "Ask again"):  # p1 was made on r001: the reload forgets it
            eng.accept("p1", None)
        self.assertNotIn("version", eng.base)
        self.assertEqual(self.saved_ids(), ["e1"])
        gen = eng.gen
        eng.poll_render()  # nothing changed since: no reload
        self.fake.st = OSError("drive gone")  # a status without "current" is no reason to reload either
        eng.poll_render()
        self.assertEqual(eng.gen, gen)
        self.assertEqual(eng.undo(None)["edits"], [])

    def test_promotion_seen_first_in_current_json_reloads_once(self) -> None:
        eng = engine.Engine(self.demo, render=self.fake)
        eng.start_render()
        self.promote("r001", ["e1"])  # current.json is the commit point; progress.json still says running
        self.fake.st = {**self.fake.st, "current": "r001"}
        eng.poll_render()
        self.assertEqual((eng.base["version"], eng.edits), ("r001", []))
        gen = eng.gen
        eng.proposals["p1"] = {**proposal(), "gen": gen}
        self.fake.st = {"state": "promoted", "version": "r001", "current": "r001"}
        eng.poll_render()
        self.assertEqual(eng.gen, gen)
        self.assertIn("p1", eng.proposals)

    def test_frame_cache_keeps_nearby_times_apart(self) -> None:
        src = self.tmp / "D7.mov"
        src.write_bytes(b"")
        eng = engine.Engine(self.demo, render=self.fake)
        eng.base["library"] = [{"tag": "D7", "file": str(src), "dur": 30.0}]
        seeks = []

        def ffmpeg(cmd, **kw):
            seeks.append(cmd[cmd.index("-ss") + 1])
            Path(cmd[-1]).write_bytes(b"jpg")
        with mock.patch.object(engine.subprocess, "run", side_effect=ffmpeg):
            a, b = eng.frame("D7", 8.75), eng.frame("D7", 8.8)
        self.assertNotEqual(a, b)
        self.assertEqual(seeks, ["8.750", "8.800"])

    def test_watcher_thread_follows_the_render_to_promotion(self) -> None:
        eng = engine.Engine(self.demo, render=self.fake)
        eng.start_render()
        th = eng.watch(every=0.01)
        self.promote("r001", ["e1"])
        self.fake.st = {"state": "promoted", "version": "r001"}
        deadline = time.time() + 5
        while eng.base.get("version") != "r001" and time.time() < deadline:
            time.sleep(0.01)
        eng._stop.set()
        th.join(2)
        self.assertEqual(eng.base.get("version"), "r001")
        self.assertEqual(eng.edits, [])

    def test_missing_render_module_leaves_the_editor_working(self) -> None:
        with mock.patch.dict(sys.modules, {"render": None}):
            eng = engine.Engine(self.demo)
            st = eng.project()["engine"]["render"]
            self.assertEqual(st["state"], "unavailable")
            self.assertIn("Render isn't available", st["message"])
            with self.assertRaises(engine.Unavailable):
                eng.render_plan()
            self.assertEqual(eng.undo(None)["edits"], [])


class EngineRenderHttpTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.eng = engine.Engine(self.demo, render=self.fake)
        self.srv = Server(self.eng)

    def tearDown(self) -> None:
        self.srv.close()
        super().tearDown()

    def test_plan_start_status_cancel(self) -> None:
        code, plan = self.srv.json("GET", "/api/render/plan")
        self.assertEqual((code, plan["next_version"], plan["fixes"]), (200, "r001", ["voice_sync"]))
        code, st = self.srv.json("GET", "/api/render")
        self.assertEqual((code, st["state"]), (200, "idle"))
        code, out = self.srv.json("POST", "/api/render")
        self.assertEqual((code, out), (200, {"version": "r001", "pid": 4242}))
        code, out = self.srv.json("POST", "/api/render")
        self.assertEqual(code, 409)
        self.assertIn("r001", out["error"])
        code, st = self.srv.json("GET", "/api/render")
        self.assertEqual((st["state"], st["step"], st["i"], st["n"]), ("running", "Community picture", 1, 9))
        code, p = self.srv.json("GET", "/api/project")
        self.assertEqual(p["engine"]["render"], {"state": "running", "version": "r001"})
        code, st = self.srv.json("POST", "/api/render/cancel")
        self.assertEqual((code, st["state"]), (200, "cancelled"))
        self.assertEqual(self.fake.calls, ["start", "cancel"])

    def test_accept_and_undo_are_409_while_rendering(self) -> None:
        self.eng.proposals["p1"] = proposal()
        self.srv.json("POST", "/api/render")
        code, out = self.srv.json("POST", "/api/accept", {"id": "p1"})
        self.assertEqual((code, out["error"]), (409, "Rendering — accept after it finishes"))
        code, out = self.srv.json("POST", "/api/accept", {"id": "gone"})
        self.assertEqual(code, 409)
        code, out = self.srv.json("POST", "/api/undo", {})
        self.assertEqual((code, out["error"]), (409, "Rendering — undo after it finishes"))
        code, out = self.srv.json("POST", "/api/render/revert")
        self.assertEqual(code, 409)
        self.assertEqual(self.saved_ids(), ["e1"])

    def test_revert_with_pending_edits_is_409_with_the_render_message(self) -> None:
        import render
        msg = "2 accepted changes are waiting to render on r001: undo or render them first"
        self.eng._render = render
        with mock.patch.object(render, "status", return_value={"state": "idle", "version": None, "current": None}), \
                mock.patch.object(render, "revert", side_effect=render.Busy(msg)):
            code, out = self.srv.json("POST", "/api/render/revert")
        self.assertEqual((code, out), (409, {"error": msg}))
        self.assertEqual(self.saved_ids(), ["e1"])

    def test_status_poll_promotes_before_the_page_reloads(self) -> None:
        self.srv.json("POST", "/api/render")
        self.promote("r001", ["e1"])
        (self.demo / "edits.json").write_text("[]")
        self.fake.st = {"state": "promoted", "version": "r001", "message": "27 checks passed"}
        code, st = self.srv.json("GET", "/api/render")
        self.assertEqual(st["state"], "promoted")
        code, p = self.srv.json("GET", "/api/project")
        self.assertEqual((p["project"]["version"], p["project"]["pending"], p["edits"]), ("r001", 0, []))

    def test_unavailable_render_is_503(self) -> None:
        self.fake.st = FileNotFoundError("no job.json")
        self.eng._render = None
        with mock.patch.dict(sys.modules, {"render": None}):
            for path in ("/api/render/plan", "/api/render"):
                code, out = self.srv.json("GET", path)
                self.assertEqual(code, 503, path)
                self.assertIn("Render isn't available", out["error"])
            code, out = self.srv.json("GET", "/api/project")
            self.assertEqual((code, out["engine"]["render"]["state"]), (200, "unavailable"))

    def test_render_endpoints_keep_the_host_and_json_guards(self) -> None:
        code, _ = self.srv.call("POST", "/api/render", {}, ctype="text/plain")
        self.assertEqual(code, 415)
        for method, path in (("GET", "/api/render"), ("GET", "/api/render/plan"), ("GET", "/api/render/log"),
                             ("POST", "/api/render"), ("POST", "/api/render/revert")):
            code, _ = self.srv.call(method, path, host="evil.example:80")
            self.assertEqual(code, 403, path)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.srv.call("GET", "/app/ui/demo/../../../app/engine.py")[0], 404)
        self.assertEqual(self.srv.call("HEAD", "/app/ui/.hidden")[0], 404)
        code, out = self.srv.json("POST", "/api/render/nope")
        self.assertEqual((code, out["error"]), (404, "unknown endpoint"))

    def test_log_is_served_only_from_the_render_root(self) -> None:
        root = self.tmp / "renders"
        (root / "r001").mkdir(parents=True)
        (root / "r001" / "render.log").write_text("step 1\nfacts: ok\n")
        (self.demo / "job.json").write_text(json.dumps({"render_root": str(root)}))
        self.fake.st = {"state": "failed", "version": "r001", "log": str(root / "r001" / "render.log")}
        code, raw = self.srv.call("GET", "/api/render/log")
        self.assertEqual((code, raw.decode()), (200, "step 1\nfacts: ok\n"))
        (self.tmp / "elsewhere.log").write_text("secret")
        self.fake.st = {"state": "failed", "version": "r001", "log": str(self.tmp / "elsewhere.log")}
        code, raw = self.srv.call("GET", "/api/render/log")
        self.assertEqual(code, 400)
        self.assertNotIn(b"secret", raw)
        self.fake.st = {"state": "refused", "version": "r002", "log": ["refused: cut_span", "nothing encoded"]}
        self.assertEqual(self.srv.call("GET", "/api/render/log")[1].decode(), "refused: cut_span\nnothing encoded")

    def test_serve_starts_the_watcher(self) -> None:
        httpd = engine.serve(0, self.demo, render=self.fake)
        try:
            eng = httpd.RequestHandlerClass.func.engine
            self.assertTrue(any(t.name == "render-watch" and t.is_alive() for t in threading.enumerate()))
            eng._stop.set()
        finally:
            httpd.server_close()


class UiWiringTests(unittest.TestCase):
    ui = APP / "ui"

    def test_every_id_the_script_uses_is_in_the_page(self) -> None:
        js, html = (self.ui / "app.js").read_text(), (self.ui / "index.html").read_text()
        ids = set(re.findall(r'\$\("#([A-Za-z][\w-]*)"\)', js))
        self.assertTrue({"renderBtn", "renderBar", "renderDlg", "renderStart", "verChip"} <= ids)
        self.assertEqual(sorted(i for i in ids if f'id="{i}"' not in html), [])

    def test_render_button_is_live_and_accept_undo_are_gated(self) -> None:
        js, html = (self.ui / "app.js").read_text(), (self.ui / "index.html").read_text()
        self.assertNotIn("next build step", html)
        self.assertIn('data-busy="undo"', html)
        self.assertIn("Rendering — accept after it finishes", js)
        self.assertIn("The current cut stays until the new one passes its checks", js)
        self.assertIn("demo/video.mp4${VQ}", js)

    def test_render_notes_have_a_badge_and_base_refusals_no_undo(self) -> None:
        js, css = (self.ui / "app.js").read_text(), (self.ui / "app.css").read_text()
        self.assertIn('render: "Render"', js)  # render_plan notes are src "render"
        self.assertIn(".b.render", css)
        self.assertIn("!f.edit && !f.render", js)  # make_demo's generated notes are not hand-written demo content
        self.assertIn("if (!r.edit) return", js)  # undo {edit: null} would pop the last edit

    def test_no_absolute_paths_in_tracked_render_code(self) -> None:
        for f in (APP / "engine.py", self.ui / "app.js", self.ui / "index.html", self.ui / "app.css"):
            text = f.read_text()
            self.assertNotIn("/Volumes/", text, f.name)
            self.assertNotIn("/Users/", text, f.name)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_script_parses(self) -> None:
        subprocess.run(["node", "--check", str(self.ui / "app.js")], check=True, capture_output=True)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_frame_preview_uses_the_render_in_point_middle(self) -> None:
        js = (self.ui / "app.js").read_text()
        fn = re.search(r"const frameUrl = \(tag\) => \{.*?\n  \};", js, re.S).group(0)
        lib = {"V10": {"best": [13.0, 19.8], "dur": 9.9099}, "D7": {"best": [0.5, 17.0], "dur": 30.0},
               "P1": {"dur": 6.006}, "W3": {"best": [2.0, 6.0]}, "D8": {"best": [5.0, 5.0], "dur": 8.0},
               "D9": {"best": [3.0, 1.0]}, "C1": {}}
        src = f"const LIB = () => ({json.dumps(lib)});\n{fn}\n" \
              f"console.log(JSON.stringify({json.dumps(list(lib))}.map((t) => +frameUrl(t).split('&t=')[1])));"
        out = subprocess.run(["node", "-e", src], check=True, capture_output=True, text=True).stdout
        import render_plan
        want = [round(render_plan.preview_t({**e, "tag": t}), 3) for t, e in lib.items()]
        self.assertEqual(want, [8.2, 8.75, 3.003, 2.0, 4.0, 0.0, 0.0])  # D7: in_point 7.525 + 4.9 s × 0.5 / 2
        self.assertEqual(json.loads(out), want)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_revert_failure_shows_the_engine_message(self) -> None:
        js = (self.ui / "app.js").read_text()
        api = re.search(r"const api = async \(path, body\) => \{.*?\n  \};", js, re.S).group(0)
        msg = "2 accepted changes are waiting to render on r001: undo or render them first"
        src = f"const fetch = async () => ({{ ok: false, status: 409, json: async () => ({json.dumps({'error': msg})}) }});\n" \
              f"{api}\napi('/api/render/revert', {{}}).then(() => console.log('null'), (e) => console.log(JSON.stringify(e.message)));"
        out = subprocess.run(["node", "-e", src], check=True, capture_output=True, text=True).stdout
        self.assertEqual(json.loads(out), msg)
        revert = re.search(r"function askRevert\(\) \{.*?\n  \}", js, re.S).group(0)
        self.assertRegex(revert, r'(?s)await api\("/api/render/revert", \{\}\);.*catch \(e\) \{[^}]*barText\(`Can.t revert · \$\{e\.message\}`\)')


if __name__ == "__main__":
    unittest.main()
