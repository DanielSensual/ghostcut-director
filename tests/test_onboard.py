"""ONBOARD: first-run settings, Ghost's help and briefing, scan tile facts, the
Muse-unavailable fallback, and the new UI files.

All offline: settings live in a temp file, volumes under a temp root, and no
test sets MODEL_API_KEY with a reachable Meta client (the fallback test makes
the Spark call raise, the way a 402 does). No Meta, ElevenLabs, xAI or Whisper.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "app"))
from director import offload  # noqa: E402
import engine  # noqa: E402

from tests.test_offload import FakeRender, sony_card, two_day_cards  # noqa: E402

UI = ROOT / "app" / "ui"
NEW_FILES = [UI / "ghost.css", UI / "import.css", UI / "import.js", UI / "onboard.js",
             UI / "ghost" / "mascot.js", UI / "ghost" / "ghost.svg"]


def make_engine(root: Path, settings: Path | None = None):
    demo = root / "demo"
    demo.mkdir(exist_ok=True)
    eng = engine.Engine(demo, render=FakeRender(),
                        settings_path=settings or (root / "settings.json"))
    return eng


def wait_ready(eng) -> dict:
    eng.offload_volumes()
    scans: dict = {}
    for _ in range(300):
        scans = {s["name"]: s for s in eng.offload_scans()["scans"]}
        if scans and all(s["state"] == "ready" for s in scans.values()):
            break
        time.sleep(0.02)
    return scans


class SettingsTests(unittest.TestCase):
    def test_round_trip_first_run_then_seen(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "deep" / "GhostCut Director" / "settings.json"
            eng = make_engine(Path(td), path)
            self.assertFalse(path.exists())  # lazy: nothing written by __init__ or GET
            got = eng.settings_get()
            self.assertTrue(got["first_run"])
            self.assertEqual(got["onboarding"]["version"], 1)
            self.assertFalse(got["onboarding"]["seen"])
            self.assertIn("meta_key", got)
            done = eng.settings_post({"onboarding": {"seen": True, "step": 5, "skipped": False,
                                                     "completed_at": "2026-11-04T19:00:00Z"}})
            self.assertFalse(done["first_run"])
            self.assertEqual(done["onboarding"]["step"], 5)
            self.assertEqual(json.loads(path.read_text()),
                             {"onboarding": done["onboarding"]})
            # A fresh engine on the same file agrees.
            self.assertFalse(make_engine(Path(td), path).settings_get()["first_run"])

    def test_corrupt_file_is_first_run_and_unknown_keys_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text("{not json")
            eng = make_engine(Path(td), path)
            self.assertTrue(eng.settings_get()["first_run"])
            out = eng.settings_post({"onboarding": {"seen": True, "evil": 1},
                                     "meta_key": True, "api_key": "x"})
            self.assertNotIn("evil", out["onboarding"])
            saved = json.loads(path.read_text())
            self.assertEqual(list(saved), ["onboarding"])
            self.assertNotIn("evil", saved["onboarding"])

    def test_env_path_and_handler_routes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "env-settings.json"
            demo = Path(td) / "demo"
            demo.mkdir()
            with mock.patch.dict(os.environ, {"GHOSTCUT_SETTINGS": str(path)}):
                eng = engine.Engine(demo, render=FakeRender())
                handler = engine.Handler.__new__(engine.Handler)
                handler.engine = eng
                out = handler._post("/api/settings", {"onboarding": {"skipped": True, "seen": True}})
            self.assertTrue(out["onboarding"]["skipped"])
            self.assertTrue(path.exists())

    def test_meta_key_reported_honestly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            eng = make_engine(Path(td))
            with mock.patch.dict(os.environ, {"MODEL_API_KEY": "test-not-a-key"}):
                self.assertTrue(eng.settings_get()["meta_key"])
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MODEL_API_KEY", None)
                self.assertFalse(eng.settings_get()["meta_key"])


class HelpTests(unittest.TestCase):
    def test_is_help_phrasings(self) -> None:
        for t in ["help", "Help!", "what can you do?", "How does this work", "start the tutorial",
                  "walk me through it", "how do I get started"]:
            self.assertTrue(offload.is_help(t), t)
        for t in ["transfer to my SSD", "the footage is on the SD card, transfer it to my SSD",
                  "go ahead", "open the last job", "helpful footage", "",
                  "help me copy the SD card to my SSD", "can you help me transfer it to the SSD"]:
            self.assertFalse(offload.is_help(t), t)

    def test_briefing_is_in_the_developer_prompt_and_schema_unchanged(self) -> None:
        self.assertIn(offload.GHOST_BRIEFING, offload.GHOST_DEVELOPER)
        self.assertIn("never writes to a card", offload.GHOST_BRIEFING)
        self.assertEqual(sorted(offload.INTENT_SCHEMA["required"]),
                         sorted(["cards", "destination", "project", "make_cut", "open_job",
                                 "confirm", "reply", "date", "include_all"]))

    def test_live_help_reply_and_briefing_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vols = root / "volumes"
            two_day_cards(vols)
            (vols / "Extreme SSD").mkdir(parents=True, exist_ok=True)
            eng = make_engine(root)
            eng._offload_root = vols
            wait_ready(eng)
            with mock.patch.object(offload, "parse_intent", side_effect=AssertionError("not for help")):
                got = eng.offload_ask("What can you do?")
            self.assertTrue(got["help"])
            self.assertIn("CardA, 4 clips", got["reply"])
            self.assertIn("CardB, 1 clip)", got["reply"])
            self.assertIn("Extreme SSD", got["reply"])
            self.assertIn("transfer to my SSD", got["reply"])
            self.assertGreaterEqual(len(got["steps"]), 3)
            brief = eng.offload_briefing()
            self.assertEqual(set(brief), {"reply", "steps", "live"})
            self.assertEqual(brief["reply"], got["reply"])
            self.assertEqual(sorted(c["name"] for c in brief["live"]["cards"]), ["CardA", "CardB"])
            self.assertEqual(brief["live"]["destination"]["name"], "Extreme SSD")

    def test_briefing_with_nothing_mounted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vols = Path(td) / "volumes"
            vols.mkdir()
            eng = make_engine(Path(td))
            eng._offload_root = vols
            brief = eng.offload_briefing()
            self.assertIn("Insert a camera card", brief["reply"])
            self.assertEqual(brief["live"], {"cards": [], "destination": None})


class ScanEnrichmentTests(unittest.TestCase):
    def setUp(self) -> None:
        offload.clear_scan_cache()

    def tearDown(self) -> None:
        offload.clear_scan_cache()

    def test_scan_entries_carry_tile_facts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vols = root / "volumes"
            sony_card(vols, "CardA")
            eng = make_engine(root)
            eng._offload_root = vols
            scan = wait_ready(eng)["CardA"]
            for k in ("camera_labels", "date_start", "date_end", "sample"):
                self.assertIn(k, scan)
            self.assertIsInstance(scan["camera_labels"], list)
            self.assertTrue(scan["date_start"].startswith("2024-03-04"))
            self.assertEqual(scan["sample"], "C0001.MP4")  # a file name, never a path
            self.assertNotIn("/", scan["sample"])
            self.assertNotIn("_fingerprint", scan)


class MuseFallbackTests(unittest.TestCase):
    """The Meta API can answer 402 (credit exhausted): Ghost plans with rules and says so."""

    def test_spark_error_falls_back_to_rules_with_a_note(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vols = root / "volumes"
            two_day_cards(vols)
            (vols / "Extreme SSD").mkdir(parents=True, exist_ok=True)
            eng = make_engine(root)
            eng._offload_root = vols

            def payment_required(*a, **k):
                raise RuntimeError("HTTP 402: Payment Required")

            fake_spark = mock.MagicMock()
            fake_spark._structured.side_effect = payment_required
            fake_cache = mock.MagicMock()
            fake_cache.get.return_value = None
            fake_meta = mock.MagicMock(spark=fake_spark, cache=fake_cache)
            with mock.patch.dict(os.environ, {"MODEL_API_KEY": "test-not-a-key"}), \
                    mock.patch.dict(sys.modules, {"guestlist": mock.MagicMock(meta=fake_meta),
                                                  "guestlist.meta": fake_meta}):
                got = eng.offload_ask("the footage is on the SD card, transfer it to my SSD")
            fake_spark._structured.assert_called()  # Muse was really tried, then refused
            self.assertEqual(got["planner"], "rules")
            self.assertEqual(got["note"], engine.MUSE_UNAVAILABLE)
            self.assertTrue(got["shoot_dir"].endswith("2026-11-03 Shoot"))
            self.assertTrue(got["reply"].startswith("Found today's shoot"))

    def test_no_key_plans_with_rules_without_a_note(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vols = root / "volumes"
            two_day_cards(vols)
            (vols / "Extreme SSD").mkdir(parents=True, exist_ok=True)
            eng = make_engine(root)
            eng._offload_root = vols
            env = {k: v for k, v in os.environ.items() if k != "MODEL_API_KEY"}
            with mock.patch.dict(os.environ, env, clear=True):
                got = eng.offload_ask("transfer it to my SSD")
            self.assertEqual(got["planner"], "rules")
            self.assertNotIn("note", got)


class OnboardUiTests(unittest.TestCase):
    def test_index_loads_every_new_file(self) -> None:
        html = (UI / "index.html").read_text()
        for ref in ('href="ghost.css"', 'href="import.css"', 'src="ghost/mascot.js"',
                    'src="import.js"', 'src="onboard.js"', 'id="helpBtn"'):
            self.assertIn(ref, html)

    def test_every_id_in_app_and_import_exists(self) -> None:
        html = (UI / "index.html").read_text()
        for f in ("app.js", "import.js", "onboard.js"):
            ids = set(re.findall(r'\$\("#([A-Za-z][\w-]*)"\)', (UI / f).read_text()))
            ids |= set(re.findall(r'getElementById\("([A-Za-z][\w-]*)"\)', (UI / f).read_text()))
            self.assertEqual(sorted(i for i in ids if f'id="{i}"' not in html), [], f)

    def test_hooks_and_contract_strings(self) -> None:
        js = (UI / "app.js").read_text()
        self.assertIn("window.GCImport.mount(el, window.GC)", js)
        self.assertIn("window.GC = Object.assign(window.GC || {}", js)
        self.assertIn('"notour"', js)
        self.assertIn('"tour"', js)
        imp = (UI / "import.js").read_text()
        self.assertIn("window.GCImport", imp)
        self.assertIn("encodeURIComponent(c.path", imp)
        self.assertIn('location.hash = "#edit"', imp)
        self.assertIn("el.hidden", imp)
        self.assertIn("Muse unavailable, using the built-in planner.", imp)
        self.assertEqual(engine.MUSE_UNAVAILABLE, "Muse unavailable, using the built-in planner.")
        tour = (UI / "onboard.js").read_text()
        self.assertIn("window.GCTour", tour)
        self.assertIn("Skip", tour)
        self.assertIn("/api/settings", tour)
        self.assertIn("window.GhostMark = function GhostMark(size, mood)", (UI / "ghost" / "mascot.js").read_text())

    def test_no_local_paths_or_remote_assets_in_new_files(self) -> None:
        for f in NEW_FILES:
            text = f.read_text()
            self.assertNotIn("/Users/", text, f.name)
            self.assertNotIn("/Volumes/", text, f.name)
            self.assertNotRegex(text, r"https?://(?!www\.w3\.org/)", f.name)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_scripts_parse(self) -> None:
        for f in (UI / "import.js", UI / "onboard.js", UI / "ghost" / "mascot.js"):
            subprocess.run(["node", "--check", str(f)], check=True, capture_output=True)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_glyph_choice(self) -> None:
        src = ("global.window = {}; global.document = {};\n" + (UI / "import.js").read_text()
               + "\nconsole.log(JSON.stringify(['Sony XAVC', 'DJI Osmo', 'GoPro', 'iPhone', 'Zoom recorder', '']"
                 ".map(window.GCImport.glyphKind)));")
        out = subprocess.run(["node", "-e", src], check=True, capture_output=True, text=True).stdout
        self.assertEqual(json.loads(out), ["sony", "dji", "gopro", "iphone", "recorder", "card"])


if __name__ == "__main__":
    unittest.main()
