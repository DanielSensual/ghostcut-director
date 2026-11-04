"""Offload + Ghost: layout detection, verified copies, intent, engine.

All offline: synthetic card trees in temp dirs, dummy media bytes (ffprobe
simply finds nothing to say about them), and a fake render for the engine.
No Meta API, no Whisper.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "app"))
from director import offload  # noqa: E402
import engine  # noqa: E402

SONY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<NonRealTimeMeta xmlns="urn:schemas-professionalDisc:nonRealTimeMeta:ver.2.20">
  <CreationDate value="2024-03-04T05:06:07+00:00"/>
  <Device manufacturer="Sony" modelName="ILME-FX3"/>
</NonRealTimeMeta>
"""


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def sony_card(root: Path, volume: str = "CARD") -> Path:
    vol = root / volume
    write(vol / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001.MP4", b"clip-one-bytes")
    write(vol / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001M01.XML",
          SONY_XML.encode())
    write(vol / "PRIVATE" / "M4ROOT" / "CLIP" / "C0002.MP4", b"clip-two-bytes!!")
    write(vol / "PRIVATE" / "M4ROOT" / "CLIP" / "C0002M01.XML",
          SONY_XML.encode())
    write(vol / "PRIVATE" / "M4ROOT" / "THMBNL" / "C0001T01.JPG", b"thumb")
    write(vol / "AVF_INFO" / "AVIN0001.BNP", b"system")
    return vol


class LayoutTests(unittest.TestCase):
    def test_sony_dji_gopro_recorder_detection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vols = root / "volumes"
            sony = sony_card(vols, "SonyCard")
            dji = vols / "DroneCard"
            write(dji / "DCIM" / "DJI_001" / "DJI_0001.MP4", b"dji")
            write(dji / "DCIM" / "DJI_001" / "DJI_0001.SRT", b"subs")
            write(dji / "DCIM" / "DJI_001" / "DJI_0001.LRF", b"proxy")
            gopro = vols / "GoProCard"
            write(gopro / "DCIM" / "100GOPRO" / "GOPR0001.MP4", b"gopro")
            rec = vols / "RecorderCard"
            write(rec / "TAKE01.WAV", b"wavbytes")
            dest = vols / "Extreme SSD"
            dest.mkdir(parents=True)
            found = {v.name: v for v in offload.list_volumes(vols)}
            self.assertEqual(found["SonyCard"].kind, "camera_card")
            self.assertEqual(found["SonyCard"].detail, "Sony")
            self.assertEqual(found["DroneCard"].detail, "DJI")
            self.assertEqual(found["GoProCard"].detail, "GoPro")
            self.assertEqual(found["RecorderCard"].kind, "camera_card")
            self.assertEqual(found["Extreme SSD"].kind, "destination")
            for vol in found.values():
                self.assertGreaterEqual(vol.total_bytes, 0)
                self.assertGreaterEqual(vol.free_bytes, 0)
            # Scan skips proxies, thumbnails and system folders.
            self.assertEqual([c.source.name for c in offload.scan_card(dji).clips],
                             ["DJI_0001.MP4"])
            self.assertEqual([c.source.name for c in offload.scan_card(sony).clips],
                             ["C0001.MP4", "C0002.MP4"])
            self.assertEqual(offload.scan_card(gopro).clips[0].camera_label, "GoPro")
            self.assertEqual(offload.scan_card(rec).clips[0].kind, "audio")

    def test_list_volumes_skips_boot_installer_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vols = Path(td) / "volumes"
            (vols / ".hidden").mkdir(parents=True)
            inst = vols / "Thing Installer"
            inst.mkdir(parents=True)
            boot = vols / "Boot"
            (boot / "System").mkdir(parents=True)
            (boot / "Applications").mkdir(parents=True)
            card = sony_card(vols, "Card")
            names = {v.name for v in offload.list_volumes(vols)}
            self.assertEqual(names, {"Card"})
            self.assertEqual(card.name, "Card")


class ScanTests(unittest.TestCase):
    def test_sidecar_pairing_and_xml_time(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vol = sony_card(Path(td))
            scan = offload.scan_card(vol)
            by_name = {c.source.name: c for c in scan.clips}
            clip = by_name["C0001.MP4"]
            self.assertEqual([p.name for p in clip.sidecars], ["C0001M01.XML"])
            self.assertEqual(clip.time_source, "xml")
            self.assertEqual(clip.make, "Sony")
            self.assertEqual(clip.model, "ILME-FX3")
            self.assertEqual(clip.camera_label, "FX3")
            self.assertEqual(clip.recorded_at.date().isoformat(), "2024-03-04")
            self.assertGreater(clip.size_bytes, 0)

    def test_mtime_is_last_resort(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vol = Path(td) / "Loose"
            clip = write(vol / "DJI_0009.MP4", b"loose")
            scan = offload.scan_card(vol)
            self.assertEqual(scan.clips[0].time_source, "mtime")
            self.assertEqual(scan.clips[0].camera_label, "DJI")
            self.assertIsNotNone(scan.clips[0].recorded_at)
            self.assertEqual(clip.name, "DJI_0009.MP4")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not on PATH")
    def test_ffprobe_time_and_duration_win(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vol = Path(td) / "Card"
            out = vol / "PRIVATE" / "M4ROOT" / "CLIP" / "C0007.MP4"
            out.parent.mkdir(parents=True)
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                 "-i", "testsrc2=duration=1:size=320x240:rate=10",
                 "-metadata", "creation_time=2024-05-06T07:08:09Z",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
                check=True, capture_output=True)
            scan = offload.scan_card(vol)
            self.assertEqual(scan.clips[0].time_source, "ffprobe")
            self.assertAlmostEqual(scan.clips[0].duration_s, 1.0, delta=0.3)
            self.assertEqual(scan.clips[0].recorded_at.date().isoformat(),
                             "2024-05-06")


class PlanRunTests(unittest.TestCase):
    def test_plan_layout_skip_identical_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vol = sony_card(root)
            dest = root / "dest"
            dest.mkdir()
            plan = offload.plan_offload([vol], dest, project="Demo Shoot")
            self.assertEqual(plan.shoot_dir.parent, dest)
            self.assertEqual(plan.shoot_dir.name, "2024-03-04 Demo Shoot")
            names = sorted(e.destination.name for e in plan.entries)
            self.assertEqual(names, ["C0001.MP4", "C0001M01.XML",
                                     "C0002.MP4", "C0002M01.XML"])
            self.assertTrue(all(e.destination.parent.name == "FX3"
                                for e in plan.entries))
            events: list[dict] = []
            result = offload.run_offload(plan, events.append)
            self.assertEqual(result.verified_count, 4)
            self.assertEqual(result.failed, [])
            self.assertTrue(events)
            self.assertIn("bytes_total", events[-1])
            manifest = json.loads((plan.shoot_dir / "offload.json").read_text())
            self.assertEqual(len(manifest["files"]), 4)
            self.assertTrue(all(manifest["verified"]))
            # Second plan: everything already there and identical.
            again = offload.plan_offload([vol], dest, project="Demo Shoot")
            self.assertTrue(all(e.skipped for e in again.entries))
            self.assertEqual(again.bytes_to_copy, 0)

    def test_skipped_file_changed_after_plan_is_copied_again(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "card" / "clip.mp4"
            write(source, b"original")
            clip = offload.ClipInfo(source, 8, 1.0, None, "mtime",
                                    camera_label="Camera", volume=source.parent)
            scan = offload.CardScan(source.parent, [clip])
            offload.run_offload(offload.plan_offload([scan], root / "dest"))
            plan = offload.plan_offload([scan], root / "dest")
            self.assertTrue(plan.entries[0].skipped)
            plan.entries[0].destination.write_bytes(b"corrupt!")
            result = offload.run_offload(plan)
            self.assertEqual(plan.entries[0].destination.read_bytes(), source.read_bytes())
            self.assertTrue(result.entries[0].verified)
            self.assertFalse(result.entries[0].skipped)

    def test_space_refusal_names_the_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vol = sony_card(root)
            dest = root / "dest"
            dest.mkdir()
            fake = mock.Mock(free=1, total=100, used=99)
            with mock.patch.object(offload.shutil, "disk_usage", return_value=fake):
                with self.assertRaises(offload.OffloadError) as ctx:
                    offload.plan_offload([vol], dest)
            self.assertIn("not enough space", str(ctx.exception))
            self.assertIn("bytes", str(ctx.exception))

    def test_checksum_mismatch_keeps_part_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vol = sony_card(root)
            dest = root / "dest"
            dest.mkdir()
            plan = offload.plan_offload([vol], dest)
            real = offload.file_checksum

            def lying(path, chunk_size=offload.CHUNK_SIZE):
                if str(path).endswith(".part"):
                    return "0" * 64
                return real(path, chunk_size)

            with mock.patch.object(offload, "file_checksum", lying):
                result = offload.run_offload(plan)
            self.assertEqual(result.verified_count, 0)
            self.assertEqual(len(result.failed), 4)
            for entry in result.entries:
                self.assertFalse(entry.verified)
                self.assertTrue(Path(str(entry.destination) + ".part").exists())
                self.assertFalse(entry.destination.exists())

    def test_resume_after_interrupt_and_source_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vol = sony_card(root / "src")
            # One clip spans several copy chunks, so an interrupt lands
            # genuinely mid-file with a partial .part behind it.
            big = vol / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001.MP4"
            big.write_bytes(b"x" * (3 * 1024 * 1024))
            dest = root / "dest"
            dest.mkdir()
            before = {p: (p.stat().st_mtime_ns, p.stat().st_size)
                      for p in vol.rglob("*") if p.is_file()}
            os.chmod(vol, 0o555)
            try:
                plan = offload.plan_offload([vol], dest)
                calls = {"n": 0}

                def interrupting(event):
                    calls["n"] += 1
                    if calls["n"] >= 2:
                        raise KeyboardInterrupt

                with self.assertRaises(KeyboardInterrupt):
                    offload.run_offload(plan, interrupting)
                parts = list(plan.shoot_dir.rglob("*.part"))
                self.assertTrue(parts)  # partial copy left behind
                self.assertLess(parts[0].stat().st_size, big.stat().st_size)
                result = offload.run_offload(plan)  # resumes cleanly
                self.assertEqual(result.verified_count, 4)
                self.assertEqual(list(plan.shoot_dir.rglob("*.part")), [])
            finally:
                os.chmod(vol, 0o755)
            after = {p: (p.stat().st_mtime_ns, p.stat().st_size)
                     for p in vol.rglob("*") if p.is_file()}
            self.assertEqual(before, after)

    def test_duplicate_camera_labels_are_numbered(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = sony_card(root, "CardA")
            b = sony_card(root, "CardB")
            dest = root / "dest"
            dest.mkdir()
            plan = offload.plan_offload([a, b], dest)
            folders = sorted({e.destination.parent.name for e in plan.entries
                              if not e.is_sidecar})
            self.assertEqual(folders, ["FX3", "FX3 2"])


SHOT = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def clip_on(card: Path, rel: str, data: bytes, label: str = "Camera",
            carries_card: bool = True) -> offload.ClipInfo:
    """A clip written onto a synthetic card, recorded at SHOT."""
    source = write(card / rel, data)
    return offload.ClipInfo(source, len(data), 1.0, SHOT, "mtime",
                            camera_label=label,
                            volume=card if carries_card else None)


def listing(folder: Path) -> list[str]:
    """Every path under a folder, relative and sorted (the card check)."""
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))


class SafetyTests(unittest.TestCase):
    """Astra F01/F02: one destination per file, and never onto a card."""

    def test_nested_duplicate_names_refused_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            card = root / "CARD"
            first = clip_on(card, "DCIM/100MEDIA/clip.mp4", b"first")
            second = clip_on(card, "DCIM/101MEDIA/clip.mp4", b"second")
            dest = root / "dest"
            dest.mkdir()
            before = listing(card)
            with self.assertRaises(offload.OffloadError) as ctx:
                offload.plan_offload(
                    [offload.CardScan(card, [first, second])], dest,
                    project="Dupes")
            message = str(ctx.exception)
            day = offload.local_day(SHOT).isoformat()
            self.assertTrue(message.startswith("offload: "), message)
            self.assertIn(str(first.source), message)
            self.assertIn(str(second.source), message)
            self.assertIn(str(dest / f"{day} Dupes" / "Camera" / "clip.mp4"),
                          message)
            self.assertIn("nothing was copied", message)
            self.assertIn("both are on", message)
            self.assertEqual(list(dest.iterdir()), [])
            self.assertEqual(listing(card), before)

    def test_hand_edited_duplicate_refused_at_run_time(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            card = root / "CARD"
            a = clip_on(card, "A/clip.mp4", b"first")
            b = clip_on(card, "B/other.mp4", b"second")
            dest = root / "dest"
            plan = offload.plan_offload([offload.CardScan(card, [a, b])], dest)
            plan.entries[1].destination = plan.entries[0].destination
            with self.assertRaises(offload.OffloadError) as ctx:
                offload.run_offload(plan)
            self.assertIn("would both be copied to", str(ctx.exception))
            self.assertFalse(dest.exists())

    def test_two_same_camera_cards_with_one_clip_name_refused(self) -> None:
        # Clips that don't carry their card share one camera folder, so
        # C0001.MP4 from each card would land on one destination.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            card_a, card_b = root / "CardA", root / "CardB"
            a = clip_on(card_a, "DCIM/100MEDIA/C0001.MP4", b"card-a",
                        carries_card=False)
            b = clip_on(card_b, "DCIM/100MEDIA/C0001.MP4", b"card-b!",
                        carries_card=False)
            dest = root / "dest"
            dest.mkdir()
            with self.assertRaises(offload.OffloadError) as ctx:
                offload.plan_offload([offload.CardScan(card_a, [a]),
                                      offload.CardScan(card_b, [b])], dest)
            message = str(ctx.exception)
            self.assertIn(str(a.source), message)
            self.assertIn(str(b.source), message)
            self.assertIn("transfer the cards one at a time", message)
            self.assertEqual(list(dest.iterdir()), [])
            self.assertEqual(card_a.joinpath("DCIM/100MEDIA/C0001.MP4")
                             .read_bytes(), b"card-a")

    def test_destination_inside_a_card_refused_at_plan_and_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            card = sony_card(root)
            before = listing(card)
            # Plan time: a folder on the card itself.
            with self.assertRaises(offload.OffloadError) as ctx:
                offload.plan_offload([card], card / "offload")
            self.assertIn("is inside the source card", str(ctx.exception))
            self.assertEqual(listing(card), before)
            # Run time, hand-edited: a good plan re-pointed at the card.
            dest = root / "dest"
            plan = offload.plan_offload([card], dest)
            plan.destination = card
            plan.shoot_dir = card / plan.shoot_dir.name
            for entry in plan.entries:
                entry.destination = (plan.shoot_dir
                                     / entry.destination.parent.name
                                     / entry.destination.name)
            with self.assertRaises(offload.OffloadError) as ctx:
                offload.run_offload(plan)
            self.assertIn("source card", str(ctx.exception))
            self.assertEqual(listing(card), before)
            self.assertFalse(dest.exists())
            # Run time, stale: the destination became a link onto the card.
            plan = offload.plan_offload([card], dest)
            dest.symlink_to(card / "PRIVATE", target_is_directory=True)
            with self.assertRaises(offload.OffloadError) as ctx:
                offload.run_offload(plan)
            self.assertIn("is inside the source card", str(ctx.exception))
            self.assertEqual(listing(card), before)

    def test_destination_equal_to_the_card_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            card = sony_card(root)
            before = listing(card)
            with self.assertRaises(offload.OffloadError) as ctx:
                offload.plan_offload([card], card, project="test")
            message = str(ctx.exception)
            self.assertTrue(message.startswith("offload: the destination"))
            self.assertIn("is the source card itself", message)
            self.assertEqual(listing(card), before)
            # Another spelling of the card (a link to it) is the card too.
            alias = root / "alias"
            alias.symlink_to(card, target_is_directory=True)
            with self.assertRaises(offload.OffloadError):
                offload.plan_offload([card], alias)
            self.assertEqual(listing(card), before)

    def test_card_inside_the_shoot_folder_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "dest"
            day = offload.local_day(SHOT).isoformat()
            card = dest / f"{day} Shoot" / "CARD"
            clip = clip_on(card, "clip.mp4", b"bytes")
            before = listing(dest)
            with self.assertRaises(offload.OffloadError) as ctx:
                offload.plan_offload([offload.CardScan(card, [clip])], dest)
            self.assertIn("is inside the shoot folder", str(ctx.exception))
            self.assertEqual(listing(dest), before)

    def test_normal_single_card_plan_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            card = sony_card(root)  # root/CARD
            dest = root / "CARD-backup"  # shares the card's name prefix
            before = listing(card)
            plan = offload.plan_offload([card], dest, project="Safe")
            self.assertEqual(plan.source_roots, [str(card.resolve())])
            offload.check_plan_safety(plan)
            result = offload.run_offload(plan)
            self.assertEqual(result.verified_count, 4)
            self.assertEqual(result.failed, [])
            self.assertEqual(
                len({str(e.destination) for e in result.entries}), 4)
            self.assertTrue((plan.shoot_dir / "offload.json").is_file())
            self.assertEqual(listing(card), before)


class IntentTests(unittest.TestCase):
    def volumes(self):
        return [
            offload.Volume(Path("/tmp/CardA"), "CardA", "camera_card",
                           10**10, 10**11, "Sony"),
            offload.Volume(Path("/tmp/Drone"), "Drone", "camera_card",
                           10**10, 10**11, "DJI"),
            offload.Volume(Path("/tmp/Extreme"), "Extreme SSD", "destination",
                           10**11, 10**12, "SSD"),
        ]

    def test_owner_phrasing(self) -> None:
        intent = offload.parse_intent_rule(
            "the footage is on the SD card, transfer it to my SSD",
            self.volumes())
        self.assertEqual(len(intent.cards), 2)  # every mounted camera card
        self.assertEqual(intent.destination, "/tmp/Extreme")
        self.assertIsNone(intent.project)
        self.assertFalse(intent.make_cut)

    def test_drone_extreme_project_and_cut(self) -> None:
        vols = self.volumes()
        drone = offload.parse_intent_rule("copy the drone card to my SSD",
                                          vols)
        self.assertEqual(drone.cards, ["/tmp/Drone"])
        self.assertEqual(drone.destination, "/tmp/Extreme")
        named = offload.parse_intent_rule(
            "transfer the SD card to the extreme SSD, project Beach Day, "
            "then make the cut", vols)
        self.assertEqual(named.project, "Beach Day")
        self.assertTrue(named.make_cut)
        self.assertTrue(offload.parse_intent_rule("yes, go", vols).confirm)
        self.assertEqual(
            offload.parse_intent_rule("open yesterday's job", vols).open_job,
            "yesterday")

    def test_parse_intent_offline_uses_rules(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            intent = offload.parse_intent(
                "the footage is on the SD card, transfer it to my SSD",
                self.volumes())
        self.assertEqual(intent.method, "rule")
        self.assertEqual(intent.destination, "/tmp/Extreme")


class FakeRender:
    Busy = RuntimeError

    def status(self, demo):
        return {"state": "idle", "version": None}

    def plan_summary(self, demo):
        return {}

    def start(self, demo):
        return {"version": "r001"}

    def cancel(self, demo):
        return None

    def revert(self, demo):
        return {"version": None}


class EngineTests(unittest.TestCase):
    def post(self, eng, path, body):
        """Handler._post without a socket (sandboxes may block binds)."""
        handler = engine.Handler.__new__(engine.Handler)
        handler.engine = eng
        return handler._post(path, body or {})

    def test_plan_start_status_with_fake_copy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vols = root / "volumes"
            card = sony_card(vols, "CardA")
            dest = vols / "Extreme SSD"
            dest.mkdir(parents=True)
            demo = root / "demo"
            demo.mkdir()
            eng = engine.Engine(demo, render=FakeRender())
            eng._offload_root = vols
            planned = self.post(
                eng, "/api/offload/plan",
                {"cards": [str(card)], "destination": str(dest),
                 "project": "Demo"})
            self.assertTrue(planned["shoot_dir"].endswith("2024-03-04 Demo"))
            self.assertEqual(planned["cards"][0]["clip_count"], 2)
            self.assertTrue(planned["enough_space"])
            # Nothing copied until Transfer.
            self.assertFalse(Path(planned["shoot_dir"]).exists())
            asked = eng.offload_ask(
                "the footage is on the SD card, transfer it to my SSD")
            self.assertIn("shoot_dir", asked)

            def fake_copy(plan, progress=None):
                for entry in plan.entries:
                    entry.destination.parent.mkdir(parents=True, exist_ok=True)
                    entry.destination.write_bytes(entry.source.read_bytes())
                    entry.checksum = offload.file_checksum(entry.source)
                    entry.verified = True
                    if progress:
                        progress({"file": entry.destination.name,
                                  "bytes_done": entry.size_bytes,
                                  "bytes_total": plan.bytes_to_copy,
                                  "eta_s": 0})
                return offload.OffloadResult(
                    shoot_dir=plan.shoot_dir, entries=list(plan.entries),
                    manifest=plan.shoot_dir / "offload.json")

            with mock.patch.object(offload, "run_offload", fake_copy):
                started = self.post(eng, "/api/offload/start", {})
                self.assertEqual(started["state"], "running")
                status = {}
                for _ in range(100):
                    status = eng.offload_status()
                    if status.get("state") == "done":
                        break
                    time.sleep(0.02)
            self.assertEqual(status.get("state"), "done")
            self.assertIn("Verified 4 files", status.get("message", ""))
            self.assertTrue(
                (Path(asked["shoot_dir"]) / "FX3" / "C0001.MP4").exists())
            vols_out = eng.offload_volumes()
            self.assertEqual({v["name"] for v in vols_out["volumes"]},
                             {"CardA", "Extreme SSD"})
            # Paths outside the allowed roots are refused.
            with self.assertRaises(ValueError):
                self.post(eng, "/api/offload/plan",
                          {"cards": ["/etc"], "destination": str(dest)})


class SafetyEngineTests(unittest.TestCase):
    """The refusals reach the UI as readable 400s, never a 500."""

    def call(self, eng, path, body):
        """Handler._call around _post: the JSON and status the UI gets."""
        handler = engine.Handler.__new__(engine.Handler)
        handler.engine = eng
        sent: dict = {}
        handler._json = lambda obj, code=200: sent.update(obj=obj, code=code)
        handler._call(lambda: handler._post(path, body or {}))
        return sent["obj"], sent["code"]

    def setup_engine(self, root: Path):
        vols = root / "volumes"
        card = sony_card(vols, "CardA")
        dest = vols / "Extreme SSD"
        dest.mkdir(parents=True)
        demo = root / "demo"
        demo.mkdir()
        eng = engine.Engine(demo, render=FakeRender())
        eng._offload_root = vols
        return eng, card, dest

    def test_plan_onto_the_card_is_a_readable_400(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            eng, card, _ = self.setup_engine(Path(td))
            before = listing(card)
            body, code = self.call(
                eng, "/api/offload/plan",
                {"cards": [str(card)], "destination": str(card / "copies")})
            self.assertEqual(code, 400)
            self.assertTrue(body["error"].startswith("offload: the destination"),
                            body)
            self.assertIn("is inside the source card", body["error"])
            self.assertEqual(listing(card), before)

    def test_transfer_rechecks_a_stale_plan(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            eng, card, dest = self.setup_engine(Path(td))
            before = listing(card)
            planned, code = self.call(
                eng, "/api/offload/plan",
                {"cards": [str(card)], "destination": str(dest)})
            self.assertEqual(code, 200, planned)
            plan = eng._offload_plan
            plan.entries[0].destination = card / "PRIVATE" / "evil.MP4"
            body, code = self.call(eng, "/api/offload/start", {})
            self.assertEqual(code, 400)
            self.assertIn("would be written on the source card", body["error"])
            self.assertNotEqual(eng.offload_status().get("state"), "running")
            # The worker's own re-check reports a sentence, not a type name.
            eng._offload_worker(plan)
            status = eng.offload_status()
            self.assertEqual(status["state"], "failed")
            self.assertTrue(status["message"].startswith("offload: "), status)
            self.assertFalse(Path(planned["shoot_dir"]).exists())
            self.assertEqual(listing(card), before)


def dated_xml(day: str) -> bytes:
    return SONY_XML.replace(
        "2024-03-04T05:06:07+00:00", f"{day}T12:00:00+00:00").encode()


def dated_card(root: Path, volume: str, clips) -> Path:
    """A Sony card whose clips were recorded on the given days."""
    vol = root / volume
    for name, day, data in clips:
        write(vol / "PRIVATE" / "M4ROOT" / "CLIP" / name, data)
        write(vol / "PRIVATE" / "M4ROOT" / "CLIP"
              / f"{Path(name).stem}M01.XML", dated_xml(day))
    return vol


def two_day_cards(root: Path):
    card_a = dated_card(root, "CardA", [
        ("C0001.MP4", "2026-10-19", b"old-one"),
        ("C0002.MP4", "2026-10-29", b"old-two"),
        ("C0003.MP4", "2026-11-03", b"new-one"),
        ("C0004.MP4", "2026-11-03", b"new-two!!"),
    ])
    card_b = dated_card(root, "CardB", [
        ("C0009.MP4", "2026-10-20", b"other-card"),
    ])
    dest = root / "dest"
    dest.mkdir(exist_ok=True)
    return card_a, card_b, dest


class LatestDayTests(unittest.TestCase):
    def test_latest_day_default_left_out_and_skipped_card(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            card_a, card_b, dest = two_day_cards(Path(td))
            plan = offload.plan_offload([card_a, card_b], dest)
            clips = sorted(e.destination.name for e in plan.entries
                           if not e.is_sidecar)
            self.assertEqual(clips, ["C0003.MP4", "C0004.MP4"])
            self.assertEqual(plan.shoot_dir.name, "2026-11-03 Shoot")
            self.assertEqual(plan.shoot_day, "2026-11-03")
            self.assertEqual(plan.selected_count, 2)
            self.assertEqual(plan.older_count, 3)
            self.assertEqual(
                plan.older_summary,
                "Also on the cards: 3 older clips, "
                "Oct 19 – Oct 29 (not copied)")
            by_name = {c["name"]: c for c in plan.cards_info}
            self.assertFalse(by_name["CardA"]["skipped"])
            self.assertEqual(by_name["CardA"]["clip_count"], 2)
            self.assertTrue(by_name["CardB"]["skipped"])
            self.assertEqual(by_name["CardB"]["clip_count"], 0)
            self.assertIn(str(card_b), plan.skipped_volumes)
            as_dict = plan.as_dict()
            self.assertEqual(as_dict["older_summary"], plan.older_summary)
            self.assertEqual(as_dict["skipped_cards"], [str(card_b)])

    def test_everything_and_a_named_date(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            card_a, card_b, dest = two_day_cards(Path(td))
            everything = offload.plan_offload([card_a, card_b], dest,
                                              include_all=True)
            self.assertEqual(everything.selected_count, 5)
            self.assertEqual(everything.older_count, 0)
            self.assertIsNone(everything.older_summary)
            self.assertTrue(everything.include_all)
            named = offload.plan_offload([card_a, card_b], dest,
                                         shoot_date="2026-10-29")
            self.assertEqual(
                [e.destination.name for e in named.entries
                 if not e.is_sidecar], ["C0002.MP4"])
            self.assertEqual(named.shoot_dir.name, "2026-10-29 Shoot")
            self.assertEqual(named.older_count, 4)
            with self.assertRaises(offload.OffloadError):
                offload.plan_offload([card_a], dest, shoot_date="Nov 3")


class IntentScopeTests(unittest.TestCase):
    def volumes(self):
        return IntentTests().volumes()

    def test_everything_and_all_of_it(self) -> None:
        vols = self.volumes()
        for phrase in ("transfer everything on the SD card to my SSD",
                       "copy all of it to my SSD",
                       "include them, transfer the SD card to my SSD"):
            intent = offload.parse_intent_rule(phrase, vols)
            self.assertTrue(intent.include_all, phrase)
            self.assertTrue(intent.everything, phrase)
            self.assertIsNone(intent.date, phrase)

    def test_a_named_date(self) -> None:
        vols = self.volumes()
        iso = offload.parse_intent_rule(
            "copy the SD card from 2026-11-03 to my SSD", vols)
        self.assertEqual(iso.date, "2026-11-03")
        self.assertEqual(iso.shoot_date, "2026-11-03")
        named = offload.parse_intent_rule(
            "transfer the Nov 3 shoot to my SSD", vols)
        self.assertTrue((named.date or "").endswith("-11-03"))
        self.assertFalse(named.include_all)
        plain = offload.parse_intent_rule(
            "the footage is on the SD card, transfer it to my SSD", vols)
        self.assertIsNone(plain.date)
        self.assertFalse(plain.include_all)


class ScanCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        offload.clear_scan_cache()

    def tearDown(self) -> None:
        offload.clear_scan_cache()

    def test_cache_hit_then_miss_on_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vol = sony_card(Path(td))
            calls = {"n": 0}
            real = offload._probe_info

            def counting(path):
                calls["n"] += 1
                return real(path)

            with mock.patch.object(offload, "_probe_info", counting):
                first = offload.scan_card(vol)
                self.assertEqual(calls["n"], 2)
                self.assertEqual(len(first.clips), 2)
                events: list[dict] = []
                again = offload.scan_card(vol, events.append)
                self.assertEqual(calls["n"], 2)  # hit: nothing re-probed
                self.assertEqual(len(again.clips), 2)
                self.assertEqual(events[-1]["done"], events[-1]["total"])
                self.assertEqual(events[-1]["total"], 2)
                write(vol / "PRIVATE" / "M4ROOT" / "CLIP" / "C0003.MP4",
                      b"clip-three")
                write(vol / "PRIVATE" / "M4ROOT" / "CLIP" / "C0003M01.XML",
                      SONY_XML.encode())
                grown = offload.scan_card(vol)
                self.assertEqual(calls["n"], 3)  # only the new clip
                self.assertEqual(len(grown.clips), 3)
                big = vol / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001.MP4"
                big.write_bytes(b"clip-one-bytes-but-longer")
                offload.scan_card(vol)
                self.assertEqual(calls["n"], 4)  # changed clip re-probed

    def test_fingerprint_tracks_the_file_list(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vol = sony_card(Path(td))
            before = offload.card_fingerprint(vol)
            self.assertEqual(len(before), 2)
            write(vol / "PRIVATE" / "M4ROOT" / "CLIP" / "C0003.MP4", b"x")
            self.assertNotEqual(offload.card_fingerprint(vol), before)


class GhostPlanTests(unittest.TestCase):
    def post(self, eng, path, body):
        handler = engine.Handler.__new__(engine.Handler)
        handler.engine = eng
        return handler._post(path, body or {})

    def engine_with_cards(self, root: Path):
        vols = root / "volumes"
        new_card, old_card, _ = two_day_cards(vols)
        dest = vols / "Extreme SSD"
        dest.mkdir(parents=True, exist_ok=True)
        demo = root / "demo"
        demo.mkdir()
        eng = engine.Engine(demo, render=FakeRender())
        eng._offload_root = vols
        return eng, new_card, old_card, dest

    def test_ghost_sentence_plan_card_and_include_them(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            eng, new_card, old_card, dest = self.engine_with_cards(Path(td))
            asked = eng.offload_ask(
                "the footage is on the SD card, transfer it to my SSD")
            self.assertTrue(
                asked["reply"].startswith("Found today's shoot (Nov 3):"),
                asked["reply"])
            self.assertIn("Sony clips", asked["reply"])
            self.assertIn("CardA", asked["reply"])
            self.assertTrue(asked["shoot_dir"].endswith("2026-11-03 Shoot"))
            by_name = {c["name"]: c for c in asked["cards"]}
            self.assertEqual(by_name["CardA"]["clip_count"], 2)
            self.assertFalse(by_name["CardA"]["skipped"])
            self.assertTrue(by_name["CardB"]["skipped"])
            self.assertEqual(
                asked["older_summary"],
                "Also on the cards: 3 older clips, "
                "Oct 19 – Oct 29 (not copied)")
            # Include them: the same plan over the whole cards.
            included = self.post(
                eng, "/api/offload/plan",
                {"cards": [str(new_card), str(old_card)],
                 "destination": str(dest), "include_all": True})
            self.assertEqual(included["older_count"], 0)
            self.assertIsNone(included["older_summary"])
            self.assertIn("everything", included["reply"])
            self.assertEqual(
                len([e for e in included["entries"]
                     if not e["is_sidecar"]]), 5)
            # A named date plans that day instead.
            named = eng.offload_plan([str(new_card), str(old_card)],
                                     str(dest), shoot_date="2026-10-29")
            self.assertIn("Oct 29", named["reply"])
            self.assertTrue(named["shoot_dir"].endswith("2026-10-29 Shoot"))

    def test_background_scan_reaches_ready_and_stays_cached(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            eng, new_card, old_card, dest = self.engine_with_cards(Path(td))
            eng.offload_volumes()  # Import opens: scans start
            scans = {}
            for _ in range(200):
                scans = {s["name"]: s
                         for s in eng.offload_scans()["scans"]}
                if scans and all(s["state"] == "ready"
                                 for s in scans.values()):
                    break
                time.sleep(0.02)
            self.assertEqual(scans["CardA"]["state"], "ready")
            self.assertEqual(scans["CardA"]["clip_count"], 4)
            self.assertEqual(scans["CardB"]["clip_count"], 1)
            again = {s["name"]: s for s in eng.offload_scans()["scans"]}
            self.assertEqual(again["CardA"]["state"], "ready")


class GhostUiTests(unittest.TestCase):
    def test_plan_card_buttons_and_scanning_line_are_wired(self) -> None:
        # ONBOARD: the Import view moved to import.js; read both.
        js = ((ROOT / "app" / "ui" / "app.js").read_text()
              + (ROOT / "app" / "ui" / "import.js").read_text())
        css = (ROOT / "app" / "ui" / "app.css").read_text()
        self.assertIn("Include them", js)
        self.assertIn("Scanning ", js)
        self.assertIn("/api/offload/scans", js)
        self.assertIn("older_summary", js)
        # The Transfer button keeps its white label on the orange
        # primary background inside the plan card's action row.
        self.assertIn(".ghost-plan .primary", css)
        self.assertIn(".acts button.primary", css)
        self.assertTrue(hasattr(engine.Engine, "offload_scans"))


if __name__ == "__main__":
    unittest.main()
