"""The ghost kitchen render screen (KITCHEN-BRIEF): the engine's part sampler, the worker's status, the page hooks,
the vendored three.js, and kitchen.js itself driven in node against a stub DOM.

Offline: no socket, no server, no ffmpeg, no Meta API. The node smoke test uses the real vendored three.js with a
stand-in WebGLRenderer (the sandbox has no GPU), so it checks the scene builds, animates, counts triangles and
disposes, not what it looks like.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

APP = Path(__file__).resolve().parents[1] / "app"
UI = APP / "ui"
sys.path.insert(0, str(APP))
import engine  # noqa: E402

HAVE_NODE = shutil.which("node") is not None


def touch(path: Path, mtime: float) -> Path:
    path.write_bytes(b"")
    os.utime(path, (mtime, mtime))
    return path


class SamplePartsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="kitchen-parts-")
        self.addCleanup(self.tmp.cleanup)
        self.parts = Path(self.tmp.name) / "cut.next.parts"
        self.t0 = time.time()

    def test_no_folder_is_plan(self) -> None:
        self.assertEqual(engine.sample_parts(self.parts, self.t0, 23), {"phase": "plan", "done": 0, "total": 23})

    def test_stale_files_never_count(self) -> None:
        self.parts.mkdir()
        for i in range(5):
            touch(self.parts / f"shot_{i:03d}.mp4", self.t0 - 60)
        touch(self.parts / "parts.txt", self.t0 - 60)
        touch(self.parts / "audio_000.m4a", self.t0 - 2.5)
        self.assertEqual(engine.sample_parts(self.parts, self.t0, 23)["phase"], "plan")
        self.assertEqual(engine.sample_parts(self.parts, self.t0, 23)["done"], 0)

    def test_the_two_second_slack_counts_coarse_clocks(self) -> None:
        self.parts.mkdir()
        touch(self.parts / "shot_000.mp4", self.t0 - 1.5)
        self.assertEqual(engine.sample_parts(self.parts, self.t0, 23)["phase"], "shots")

    def test_three_fresh_shots_means_two_done(self) -> None:
        self.parts.mkdir()
        for i in range(3):
            touch(self.parts / f"shot_{i:03d}.mp4", self.t0 + i)
        touch(self.parts / "shot_010.mp4", self.t0 - 100)  # a leftover of the previous run
        self.assertEqual(engine.sample_parts(self.parts, self.t0, 23), {"phase": "shots", "done": 2, "total": 23})

    def test_audio_part_or_parts_list_is_join(self) -> None:
        for marker in ("audio_000.m4a", "parts.txt"):
            with self.subTest(marker=marker):
                shutil.rmtree(self.parts, ignore_errors=True)
                self.parts.mkdir()
                for i in range(4):
                    touch(self.parts / f"shot_{i:03d}.mp4", self.t0 + i)
                touch(self.parts / marker, self.t0 + 5)
                self.assertEqual(engine.sample_parts(self.parts, self.t0, 23), {"phase": "join", "done": 4, "total": 4})

    def test_more_fresh_shots_than_the_stale_total_raise_it(self) -> None:
        self.parts.mkdir()
        for i in range(9):
            touch(self.parts / f"shot_{i:03d}.mp4", self.t0 + i)
        for total in (5, None, 0):
            snap = engine.sample_parts(self.parts, self.t0, total)
            self.assertEqual((snap["phase"], snap["done"], snap["total"]), ("shots", 8, 9))
            self.assertLessEqual(snap["done"], snap["total"])


class RerenderWorkerTests(unittest.TestCase):
    """_rerender_worker with cutjob.rerender faked: the sampler merges progress, then never outlives done/failed."""

    def make(self, shots: int | None = 6):
        tmp = tempfile.TemporaryDirectory(prefix="kitchen-job-")
        self.addCleanup(tmp.cleanup)
        job = Path(tmp.name)
        if shots is not None:
            (job / "angles.json").write_text(json.dumps({"shots": [{"camera": "A"}] * shots}))
        eng = engine.Engine.__new__(engine.Engine)  # no demo project, no watcher threads
        eng.lock = threading.Lock()
        eng.demo = job / "demo"
        eng._cutjob_state = {"state": "running", "message": "Re-rendering cut.mp4"}
        eng._load = lambda: None
        eng._cookie = lambda: ()
        return eng, job

    def fake_rerender(self, seen: list, n_shots: int = 3, hold: float = 0.35):
        def rerender(job: Path, demo: Path) -> dict:
            parts = job / "cut.next.parts"
            parts.mkdir()
            for i in range(n_shots):
                (parts / f"shot_{i:03d}.mp4").write_bytes(b"")
                time.sleep(0.06)
            time.sleep(hold)
            seen.append(dict(self.eng._cutjob_state))
            return {"duration_s": 12.5}
        return rerender

    def test_progress_fields_are_additive_and_done_is_final(self) -> None:
        self.eng, job = self.make(6)
        seen: list = []
        with mock.patch.object(engine, "KITCHEN_TICK_S", 0.02), \
                mock.patch.object(engine.cutjob, "rerender", self.fake_rerender(seen)):
            self.eng._rerender_worker(job)
            mid = seen[0]
            self.assertEqual(mid["state"], "running")
            self.assertEqual(mid["message"], "Re-rendering cut.mp4")
            self.assertEqual((mid["phase"], mid["done"], mid["total"]), ("shots", 2, 6))
            self.assertEqual(mid["pct"], 33)
            self.assertIsInstance(mid["eta_s"], int)
            final = dict(self.eng._cutjob_state)
            time.sleep(0.15)  # a late tick must not write running back
        self.assertEqual(final, {"state": "done", "message": "Re-rendered cut.mp4 (12.5s)"})
        self.assertEqual(self.eng._cutjob_state, final)
        self.assertFalse(any(t.name == "kitchen-sampler" and t.is_alive() for t in threading.enumerate()))

    def test_failure_is_final_too(self) -> None:
        self.eng, job = self.make(None)

        def boom(job: Path, demo: Path) -> dict:
            (job / "cut.next.parts").mkdir()
            (job / "cut.next.parts" / "shot_000.mp4").write_bytes(b"")
            time.sleep(0.2)
            raise RuntimeError("ffmpeg exited 1")
        with mock.patch.object(engine, "KITCHEN_TICK_S", 0.02), mock.patch.object(engine.cutjob, "rerender", boom):
            self.eng._rerender_worker(job)
            time.sleep(0.15)
        self.assertEqual(self.eng._cutjob_state, {"state": "failed", "message": "RuntimeError: ffmpeg exited 1"})

    def test_plan_phase_has_no_percentage(self) -> None:
        self.eng, job = self.make(4)
        seen: list = []

        def planning(job: Path, demo: Path) -> dict:
            time.sleep(0.2)
            seen.append(dict(self.eng._cutjob_state))
            return {"duration_s": 3.0}
        with mock.patch.object(engine, "KITCHEN_TICK_S", 0.02), mock.patch.object(engine.cutjob, "rerender", planning):
            self.eng._rerender_worker(job)
        self.assertEqual((seen[0]["phase"], seen[0]["pct"], seen[0]["eta_s"], seen[0]["total"]), ("plan", None, None, 4))


class PageTests(unittest.TestCase):
    def test_index_has_the_overlay_and_the_script(self) -> None:
        html = (UI / "index.html").read_text()
        for needle in ('id="kitchen"', "kitchenCanvas", "kitchenCaption", "kitchenReadout",
                       '<script src="kitchen.js"></script>', 'href="kitchen.css"'):
            self.assertIn(needle, html)
        self.assertLess(html.index('<script src="kitchen.js"'), html.index('<script src="app.js"'))
        player = html[html.index('<div class="player">'):]
        self.assertLess(player.index('id="video"'), player.index('id="kitchen"'))

    def test_vendored_three_is_mit_and_self_contained(self) -> None:
        mod = UI / "vendor" / "three.module.min.js"
        if not mod.exists():
            self.skipTest("three.js not vendored")
        self.assertTrue((UI / "vendor" / "three.LICENSE").exists())
        head = mod.read_text()[:400]
        self.assertTrue("MIT" in head or "SPDX" in head)
        self.assertIsNone(re.search(r"^\s*import[\s{*]", mod.read_text(), re.M))

    def test_kitchen_js_is_offline_and_safe(self) -> None:
        js = (UI / "kitchen.js").read_text()
        self.assertIsNone(re.search(r"https?://", js))
        self.assertNotIn("eval(", js)
        self.assertNotIn("new Function", js)
        for path in ("/Users/", "/Volumes/"):
            self.assertNotIn(path, js)
        self.assertIn("prefers-reduced-motion: reduce", js)
        self.assertIn('import("./vendor/three.module.min.js")', js)
        css = (UI / "kitchen.css").read_text()
        self.assertIsNone(re.search(r"https?://|@import|url\(", css))

    def test_app_js_hands_off_without_reloading(self) -> None:
        js = (UI / "app.js").read_text()
        poll = re.search(r"async function pollCutjob\(\) \{.*?\n  \}", js, re.S).group(0)
        self.assertLess(poll.index("Kitchen.update(st)"), poll.index('if (st.state === "running") return;'))
        done = poll[poll.index('if (st.state === "done")'):]
        self.assertIn('await api("/api/project")', done)
        self.assertIn("applyResult(j)", done)
        self.assertIn("video.src = `demo/video.mp4?v=${Date.now()}`", done)
        self.assertIn("renderWords(); lockEdits();", done)
        self.assertIn("Kitchen.finish()", done)
        self.assertIn("location.reload()", done[done.index("catch"):])  # the old path is the fallback only
        self.assertIn("Kitchen.fail(", poll)
        self.assertIn("demo/video.mp4${VQ}", js)
        start = re.search(r"async function startCutjobRerender\(\) \{.*?\n  \}", js, re.S).group(0)
        self.assertIn('Kitchen.start({ mode: "cutjob"', start)
        self.assertIn("video.pause()", start)

    @unittest.skipUnless(HAVE_NODE, "node not installed")
    def test_kitchen_js_parses(self) -> None:
        subprocess.run(["node", "--check", str(UI / "kitchen.js")], check=True, capture_output=True)


HARNESS = r"""// Runs kitchen.js against a stub DOM. argv: kitchen.js path, three module path or "none".
import vm from "node:vm";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { pathToFileURL } from "node:url";
const [kitchenPath, threePath] = process.argv.slice(2);
const out = { errors: [] };
const mk = (id) => ({
  id, hidden: id === "kitchen", textContent: "", style: {}, children: [], clientWidth: 640, clientHeight: 360,
  classList: { s: new Set(), toggle(c, on) { on ? this.s.add(c) : this.s.delete(c); }, add(c) { this.s.add(c); }, remove(c) { this.s.delete(c); }, contains(c) { return this.s.has(c); } },
  appendChild(c) { this.children.push(c); c.parentElement = this; return c; }, remove() {},
  addEventListener() {}, setPointerCapture() {}, getBoundingClientRect: () => ({ left: 0, top: 0, width: 640, height: 360 }),
  getContext: () => null,
});
const els = { kitchen: mk("kitchen"), kitchenCanvas: mk("kitchenCanvas"), kitchenCaption: mk("kitchenCaption"), kitchenReadout: mk("kitchenReadout"), video: { pause() {}, paused: true }, "view-edit": { hidden: false } };
els.kitchenCanvas.parentElement = els.kitchen;
const player = mk("player");
const document = { hidden: false, readyState: "loading", getElementById: (id) => els[id] || null, querySelector: (s) => (s === ".player" ? player : null), createElement: (t) => mk(t), addEventListener() {} };
let shim = null;
if (threePath !== "none") {
  shim = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "kitchen-")), "three-shim.mjs");
  fs.writeFileSync(shim, `export * from ${JSON.stringify(pathToFileURL(threePath).href)};
export class WebGLRenderer { constructor() { globalThis.__renders = 0; } setPixelRatio() {} setSize() {} render(scene) { globalThis.__renders++; let tri = 0; scene.traverse((o) => { if (o.isMesh && o.visible) { const g = o.geometry; tri += (g.index ? g.index.count : g.attributes.position.count) / 3; } }); globalThis.__tris = Math.max(globalThis.__tris || 0, tri); if (globalThis.__first == null) globalThis.__first = tri; } dispose() { globalThis.__disposed = true; } }\n`);
}
let rafQ = [];
const ctx = {
  document, location: { hash: "" }, console, performance, setTimeout, clearTimeout, setInterval, clearInterval, Promise,
  requestAnimationFrame: (f) => { rafQ.push(f); return rafQ.length; }, cancelAnimationFrame() {},
  matchMedia: () => ({ matches: process.env.REDUCED === "1" }), addEventListener() {}, devicePixelRatio: 2,
};
ctx.window = ctx; ctx.globalThis = ctx;
vm.createContext(ctx);
const script = new vm.Script(fs.readFileSync(kitchenPath, "utf8"), {
  filename: "kitchen.js",
  importModuleDynamically: shim ? () => import(pathToFileURL(shim).href) : undefined,
});
script.runInContext(ctx);
const K = ctx.Kitchen;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const pump = async (n) => { for (let i = 0; i < n; i++) { const q = rafQ; rafQ = []; q.forEach((f) => f(performance.now())); await sleep(40); } };
try {
  // wrong-state calls are no-ops
  K.update({ state: "running", phase: "shots", done: 3, total: 9 }); K.fail("x"); K.stop(); await K.finish();
  out.hiddenBefore = els.kitchen.hidden;
  K.start({ mode: "cutjob", thumbs: 40 });
  out.shownOnStart = !els.kitchen.hidden;
  K.update({ state: "running", phase: "plan", done: 0, total: 23, pct: null, eta_s: null });
  out.plan = [els.kitchenCaption.textContent, els.kitchenReadout.textContent, els.kitchenReadout.classList.contains("kitchen-shimmer")];
  await sleep(150); await pump(4);
  K.update({ state: "running", phase: "shots", done: 7, total: 23, pct: 30, eta_s: 240 });
  out.shots = els.kitchenReadout.textContent;
  await pump(30);
  K.update({ state: "running", phase: "join", done: 23, total: 23, pct: 99, eta_s: 0 });
  out.join = els.kitchenCaption.textContent;
  out.fallbackMid = els.kitchen.children.some((c) => c.className === "kitchen-fallback" && !c.hidden);
  K.update({ state: "running", stages: { sync: "done", words: "done" }, message: "cutting" });
  out.stage = els.kitchenReadout.textContent;
  K.update({ state: "running", step: "Encoding", i: 2, n: 5, pct: 40, eta_s: 12 });
  out.legacy = els.kitchenReadout.textContent;
  const p = K.finish(); await pump(30); await p;
  out.hiddenAfterFinish = els.kitchen.hidden;
  out.fallback = els.kitchen.children.some((c) => c.className === "kitchen-fallback" && !c.hidden);
  K.start({ mode: "cutjob" }); K.fail("RuntimeError: boom"); K.fail("again");
  out.failText = els.kitchenReadout.textContent;
  await sleep(2200); out.hiddenAfterFail = els.kitchen.hidden;
  K.preview(); K.update({ phase: "shots", done: 2, total: 23, pct: 9 });
  out.preview = els.kitchenReadout.textContent; K.stop(); out.hiddenAfterStop = els.kitchen.hidden;
  out.renders = ctx.__renders || globalThis.__renders || 0; out.tris = globalThis.__tris || 0; out.disposed = !!globalThis.__disposed; out.firstTris = globalThis.__first;
} catch (e) { out.errors.push(String(e && e.stack || e)); }
console.log(JSON.stringify(out));
process.exit(0);
"""


@unittest.skipUnless(HAVE_NODE, "node not installed")
class KitchenScriptTests(unittest.TestCase):
    """kitchen.js in a node vm with a stub DOM: the readouts, the no-op safety, the fallbacks and the real scene."""

    def run_harness(self, three: str, reduced: bool = False) -> dict:
        with tempfile.TemporaryDirectory(prefix="kitchen-node-") as tmp:
            harness = Path(tmp) / "harness.mjs"
            harness.write_text(HARNESS)
            env = {**os.environ, "REDUCED": "1" if reduced else "0"}
            out = subprocess.run(["node", "--experimental-vm-modules", "--no-warnings", str(harness),
                                  str(UI / "kitchen.js"), three], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        res = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(res["errors"], [])
        return res

    def check_readouts(self, res: dict) -> None:
        self.assertTrue(res["hiddenBefore"])  # update/fail/stop/finish before start were no-ops
        self.assertTrue(res["shownOnStart"])
        self.assertEqual(res["plan"], ["Picking the angles", "Picking the angles", True])
        self.assertEqual(res["shots"], "shots 7/23 \u00b7 30% \u00b7 about 4 min left")
        self.assertEqual(res["join"], "Joining the plates")
        self.assertEqual(res["stage"], "tighten \u00b7 2/10 stages")
        self.assertEqual(res["legacy"], "shots 2/5 \u00b7 40% \u00b7 about 12 s left")
        self.assertTrue(res["hiddenAfterFinish"])
        self.assertEqual(res["failText"], "RuntimeError: boom")
        self.assertTrue(res["hiddenAfterFail"])
        self.assertTrue(res["preview"].startswith("preview \u00b7 "))
        self.assertTrue(res["hiddenAfterStop"])

    def test_webgl_scene_builds_animates_and_disposes(self) -> None:
        mod = UI / "vendor" / "three.module.min.js"
        if not mod.exists():
            self.skipTest("three.js not vendored")
        res = self.run_harness(str(mod))
        self.check_readouts(res)
        self.assertFalse(res["fallbackMid"])
        self.assertGreater(res["renders"], 10)
        self.assertLess(res["firstTris"], 3000)  # the resting scene
        self.assertLess(res["tris"], 4000)  # with a burst of cards and splashes in flight
        self.assertTrue(res["disposed"])

    def test_no_three_uses_the_css_kitchen(self) -> None:
        res = self.run_harness("none")
        self.check_readouts(res)
        self.assertTrue(res["fallbackMid"])
        self.assertEqual(res["renders"], 0)

    def test_reduced_motion_uses_the_css_kitchen(self) -> None:
        mod = UI / "vendor" / "three.module.min.js"
        res = self.run_harness(str(mod) if mod.exists() else "none", reduced=True)
        self.check_readouts(res)
        self.assertTrue(res["fallbackMid"])
        self.assertEqual(res["renders"], 0)


if __name__ == "__main__":
    unittest.main()
