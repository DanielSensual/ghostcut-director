"""director dress: title cards, music bed, ghost pop (offline, fakes only).

No network: Meta is a fake guestlist (or the built-in planner), ElevenLabs
is a recorded fake transport, and the key helper is patched to None so a
key in the environment can never reach a real provider.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "src"))

import cutjob  # noqa: E402
import engine as engine_mod  # noqa: E402
from director import dress  # noqa: E402
from director import elevenlabs as elevenlabs_mod  # noqa: E402
from director import finish as finish_mod  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
try:
    import PIL  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

SPOKEN = ("so this software lined it up and it can take two different cameras "
          "synced them together and give you a clip with captions and B-roll "
          "spending eight hours on set and then color grade them from log "
          "footage and cut from one angle to the other with the sound enhanced "
          "and then it is delivered to your client at the end of the day "
          "thanks for watching this video today")


def _words(text: str = SPOKEN, start: float = 4.0, step: float = 0.5) -> list[dict]:
    return [{"text": w, "start_s": round(start + i * step, 3),
             "end_s": round(start + i * step + 0.4, 3)} for i, w in enumerate(text.split())]


def _job(root: Path, *, duration: float = 60.0, words=None) -> Path:
    job = root / "my job"  # a space in the path, like the real job
    job.mkdir(parents=True, exist_ok=True)
    ws = words if words is not None else _words()
    lines = [{"start_s": ws[i]["start_s"], "end_s": ws[min(i + 3, len(ws) - 1)]["end_s"],
              "text": " ".join(w["text"] for w in ws[i:i + 4]), "words": ws[i:i + 4]}
             for i in range(0, len(ws), 4)]
    (job / "captions.json").write_text(json.dumps({
        "duration_out_s": duration, "lines": lines, "title": None}))
    (job / "angles.json").write_text(json.dumps({"shots": [
        {"id": 0, "start_s": 0.0, "end_s": duration, "camera": "A", "clip_in_s": 0.0,
         "line_ids": [], "beat_id": -1}], "lines": []}))
    return job


class _NoKeys(unittest.TestCase):
    """Every test: no ElevenLabs key, and the real HTTP helper explodes."""

    def setUp(self) -> None:
        super().setUp()
        for target, value in (("director.finish._elevenlabs_key", lambda: None),):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        def _no_http(*_a, **_k):
            raise AssertionError("tests must not reach the network")
        patcher = mock.patch.object(elevenlabs_mod, "http_request", _no_http)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = Path(tempfile.mkdtemp(prefix="dress test "))
        self.addCleanup(shutil.rmtree, self.tmp, True)


class GateTests(_NoKeys):
    def setUp(self) -> None:
        super().setUp()
        self.words = dress._as_out_words(_words())

    def check(self, text, quote, existing=None, duration=60.0):
        return dress.check_card({"text": text, "quote": quote, "why": ""},
                                self.words, duration, existing)

    def test_accepts_a_spoken_card(self) -> None:
        card, reason = self.check("Two cameras synced", "synced them together")
        self.assertEqual(reason, "")
        self.assertAlmostEqual(card["t"], 10.5 - 0.10, places=3)
        self.assertTrue(2.0 <= card["d"] <= 3.2)

    def test_title_window_is_rejected(self) -> None:
        words = dress._as_out_words(_words(start=1.0))
        card, reason = dress.check_card({"text": "Software lined it up", "quote": "software lined"},
                                        words, 60.0)
        self.assertIsNone(card)
        self.assertEqual(reason, "title_window")

    def test_final_second_is_rejected(self) -> None:
        card, reason = self.check("Thanks for watching", "thanks for watching", duration=self.words[-6].start_s + 2.0)
        self.assertIsNone(card)
        self.assertEqual(reason, "final_second")

    def test_too_close_is_rejected(self) -> None:
        first, _ = self.check("Two cameras synced", "synced them together")
        card, reason = self.check("A clip", "give you a clip", existing=[first])
        self.assertIsNone(card)
        self.assertEqual(reason, "too_close")

    def test_unspoken_digit_rejected_and_number_word_accepted(self) -> None:
        card, reason = self.check("9 hours on set", "eight hours on set")
        self.assertIsNone(card)
        self.assertTrue(reason.startswith("unspoken_digit"))
        card, reason = self.check("8 hours on set", "eight hours on set")
        self.assertEqual(reason, "")

    def test_quote_not_in_words(self) -> None:
        card, reason = self.check("Purple llama", "purple llama dances")
        self.assertIsNone(card)
        self.assertEqual(reason, "quote_not_found")

    def test_path_like_text(self) -> None:
        card, reason = self.check("Users/me/clip.py", "synced them")
        self.assertIsNone(card)
        self.assertEqual(reason, "path_like")

    def test_ninth_card(self) -> None:
        existing = [{"t": 100.0 + 5 * i, "d": 2.0, "enabled": True} for i in range(8)]
        card, reason = self.check("Two cameras synced", "synced them together", existing=existing,
                                  duration=200.0)
        self.assertIsNone(card)
        self.assertEqual(reason, "too_many_cards")

    def test_locate_finds_synced_them_together(self) -> None:
        start, end = dress.locate("synced them together", self.words)
        self.assertEqual([w.text for w in self.words[start:end]], ["synced", "them", "together"])
        self.assertEqual(dress.locate("B roll", self.words), dress.locate("B-roll", self.words))


class AssTests(_NoKeys):
    cards = [{"id": "c1", "t": 5.0, "d": 2.6, "text": "Two cameras {synced}", "pos": "top-left",
              "enabled": True},
             {"id": "c2", "t": 9.5, "d": 2.0, "text": "Off card", "enabled": False}]

    def test_pill_has_round_ends_and_a_box_at_the_origin(self) -> None:
        # Review fix: the top-left corner was a straight "l r 0" diagonal (a chevron on screen).
        path = dress.pill_path(300, 84)
        self.assertTrue(path.startswith("m 0 42 b "))
        nums = [float(x) for x in path.replace("m", " ").replace("l", " ").replace("b", " ").split()]
        xs, ys = nums[0::2], nums[1::2]
        self.assertEqual((min(xs), min(ys), max(xs), max(ys)), (0.0, 0.0, 300.0, 84.0))
        self.assertEqual(path.count(" b "), 4)

    def test_events_times_pos_and_measurer(self) -> None:
        widths = []

        def measure(text):
            widths.append(text)
            return 400.0
        ass = dress.build_ass(self.cards, measurer=measure)
        self.assertIn("PlayResX: 1920", ass)
        self.assertIn("0:00:05.00,0:00:07.60", ass)
        self.assertIn("\\an5\\pos(466,150)", ass)  # margin 60 + ghost slot 170 + half of a 472 px pill
        self.assertIn("\\t(0,140,\\fscx108\\fscy108)", ass)
        self.assertIn("\\fad(0,200)", ass)
        self.assertIn("\\p1", ass)
        self.assertIn("m 0 ", ass)
        self.assertNotIn("Off card", ass)
        self.assertNotIn("{synced}", ass)  # ass_escape
        self.assertEqual(widths, ["Two cameras {synced}"])

    def test_wide_pill_stays_inside_the_frame(self) -> None:
        x, _ = dress.card_position({"pos": "top-left"}, 0, 1000.0)
        self.assertGreaterEqual(x - 500, 60)
        x, _ = dress.card_position({"pos": "top-right"}, 1, 1000.0)
        self.assertLessEqual(x + 500, 1860)

    def test_appending_keeps_the_caption_events(self) -> None:
        base = ("[Script Info]\nPlayResX: 1920\nPlayResY: 1080\n\n[V4+ Styles]\n"
                "Format: Name, Fontname\nStyle: house,Poppins\n\n[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:01.00,0:00:02.00,house,,0,0,0,,hello\n"
                "Dialogue: 0,0:00:02.00,0:00:03.00,house,,0,0,0,,world\n")
        out = dress.build_ass(self.cards, base_ass=base, measurer=lambda t: 300.0)
        events = [l for l in base.splitlines() if l.startswith("Dialogue")]
        for line in events:
            self.assertIn(line, out)
        self.assertTrue(out.startswith("[Script Info]"))
        self.assertIn("Style: Card,", out)
        self.assertLess(out.index("Style: Card,"), out.index("[Events]"))


@unittest.skipUnless(HAVE_PIL, "Pillow missing")
class GhostTests(_NoKeys):
    def test_frames_count_size_alpha_cropped(self) -> None:
        from PIL import Image

        icon = self.tmp / "icon.png"
        art = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
        art.paste((255, 255, 255, 255), (100, 100, 300, 300))
        art.save(icon)
        frames = dress.build_ghost_frames(self.tmp, icon)
        self.assertEqual(len(frames), 38)
        mid = Image.open(frames[15])
        self.assertEqual(mid.size, (320, 320))
        self.assertEqual(mid.mode, "RGBA")
        self.assertEqual(mid.getpixel((0, 0))[3], 0)
        # Cropped to the alpha box: the opaque square fills ~200 px.
        bbox = mid.getchannel("A").getbbox()
        self.assertGreaterEqual(bbox[2] - bbox[0], 180)
        self.assertEqual(dress.build_ghost_frames(self.tmp, icon), frames)  # cached

    def test_missing_icon_skips(self) -> None:
        self.assertEqual(dress.build_ghost_frames(self.tmp / "x", self.tmp / "none.png"), [])


class CommandTests(_NoKeys):
    def test_encode_inputs_title_ghost_and_escaped_paths(self) -> None:
        job = self.tmp / "a job, with: spaces"
        cards = [{"t": 5.0, "text": "One"}, {"t": 9.0, "text": "Two", "pos": "top-right"}]
        cmd = dress.encode_command(
            source=job / "cut.clean.mp4", title_png=job / "title-1920x1080.png",
            ghost_pattern=job / "dress/ghost/pop_%03d.png", pop_cards=cards,
            overlay_ass=job / "dress/overlay_all.ass", fonts_dir=job / "caption-fonts",
            master_wav=job / "dress/master.wav", out_path=job / "cut.dressed.next.mp4",
            measurer=lambda t: 200.0)
        self.assertEqual(cmd.count("-i"), 1 + 1 + 2 + 1)
        self.assertEqual(cmd[cmd.index("-i", cmd.index("-i", cmd.index("-i") + 1) + 1) - 2:][:2],
                         ["-framerate", "24"])
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("enable='between(t,0,3.5)'", graph)
        self.assertIn("eof_action=pass", graph)
        self.assertIn("setpts=PTS-STARTPTS+5.000/TB", graph)
        self.assertIn("a job\\, with\\: spaces", graph)
        self.assertEqual(cmd[cmd.index("-map", cmd.index("-map") + 1) + 1], "4:a")
        self.assertNotIn("copy", cmd[cmd.index("-c:v") + 1])
        self.assertIn("libx264", cmd)

    def test_mix_ducks_the_bed_only(self) -> None:
        cmds = dress.mix_commands(
            source=self.tmp / "cut.mp4", dress_dir=self.tmp / "dress", duration_s=30.0,
            cards=[{"t": 5.0, "enabled": True}], music_asset=self.tmp / "m.mp3",
            bing_asset=self.tmp / "b.mp3", music_gain_db=-20.0)
        self.assertEqual(len(cmds), 5)  # voice, bed, sfx, duck, loudnorm measure
        duck = cmds[3][cmds[3].index("-filter_complex") + 1]
        self.assertIn("[1:a]volume=-20.0dB[m];[m][key]sidechaincompress", duck)
        self.assertIn("[v][bed][2:a]amix=inputs=3:normalize=0", duck)
        self.assertNotIn("[2:a]volume", duck)
        sfx = cmds[2][cmds[2].index("-filter_complex") + 1]
        self.assertIn("volume=-10.0dB", sfx)
        self.assertNotIn("sidechaincompress", sfx)

    def test_sfx_only_graph_has_no_dangling_pad(self) -> None:
        cmd = dress.duck_command(Path("v.wav"), None, Path("s.wav"), Path("o.wav"))
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertNotIn("asplit", graph)
        self.assertNotIn("sidechaincompress", graph)


class _FakeAudio:
    def __init__(self):
        self.calls = []


class AssetTests(_NoKeys):
    def _transport(self, status=200, body=b"ID3fake"):
        calls = []

        def transport(method, url, *, headers, body_=None, timeout=None, **kw):
            calls.append(url)
            return status, {}, body
        return transport, calls

    def test_no_key_local_bing_and_music_skipped(self) -> None:
        assets, notes, spent = dress.ensure_assets(self.tmp, duration_s=60.0, key=None)
        if dress.np is None:
            self.skipTest("numpy missing")
        kinds = {a["kind"]: a for a in assets}
        self.assertEqual(kinds["bing"]["provider"], "local")
        self.assertNotIn("music", kinds)
        self.assertIn("dress_sfx_local", notes)
        self.assertIn("dress_music_unavailable", notes)
        self.assertEqual(spent, 0.0)

    def test_cache_hit_makes_no_calls(self) -> None:
        calls = []

        def boom(**kw):
            calls.append(kw)
            raise AssertionError("no call on a cache hit")
        assets_dir = self.tmp / "dress" / "assets"
        assets_dir.mkdir(parents=True)
        for kind, prompt, bucket in (("music", dress.DEFAULT_MUSIC_PROMPT, 60.0),
                                     ("bing", dress.BING_PROMPT, 1.0), ("pop", dress.POP_PROMPT, 0.5)):
            (assets_dir / dress._asset_name(kind, prompt, bucket)).write_bytes(b"x")
        with mock.patch.object(elevenlabs_mod, "generate_music", boom), \
                mock.patch.object(elevenlabs_mod, "generate_sound_effect", boom), \
                mock.patch.object(finish_mod, "_probe_audio", lambda p: 1.0):
            assets, notes, spent = dress.ensure_assets(self.tmp, duration_s=50.0, key="k")
        self.assertEqual(calls, [])
        self.assertEqual(len(assets), 3)
        self.assertEqual(spent, 0.0)

    def test_spend_cap_stops_generation(self) -> None:
        called = []
        with mock.patch.object(elevenlabs_mod, "generate_music", lambda **kw: called.append(kw)), \
                mock.patch.object(elevenlabs_mod, "generate_sound_effect", lambda **kw: called.append(kw)):
            assets, notes, _ = dress.ensure_assets(self.tmp, duration_s=60.0, key="k",
                                                   max_provider_usd=0.0)
        self.assertEqual(called, [])
        self.assertTrue(any(n.startswith("dress_provider_cap_reached") for n in notes))

    def test_401_stops_and_402_stops(self) -> None:
        for status, note in ((401, "finish_provider_auth_failed:elevenlabs"),
                             (402, "finish_provider_quota:elevenlabs")):
            calls = []

            def fail(**kw):
                calls.append(kw)
                raise elevenlabs_mod.ElevenLabsError("elevenlabs: nope", status=status)
            with mock.patch.object(elevenlabs_mod, "generate_music", fail), \
                    mock.patch.object(elevenlabs_mod, "generate_sound_effect", fail):
                _assets, notes, _ = dress.ensure_assets(self.tmp / str(status), duration_s=60.0, key="k")
            self.assertEqual(len(calls), 1)
            self.assertIn(note, notes)

    def test_empty_body_skips(self) -> None:
        empty = SimpleNamespace(data=b"", cost_usd=0.0)
        with mock.patch.object(elevenlabs_mod, "generate_music", lambda **kw: empty), \
                mock.patch.object(elevenlabs_mod, "generate_sound_effect", lambda **kw: empty):
            assets, notes, _ = dress.ensure_assets(self.tmp, duration_s=60.0, key="k")
        self.assertEqual(assets, [])
        self.assertTrue(any("empty audio" in n for n in notes))


class FakeGuestlist:
    def __init__(self, data=None, fail=None):
        self.data, self.fail, self.calls = data, fail, []
        self.meta = types.ModuleType("guestlist.meta")
        self.meta.cache = SimpleNamespace(key=lambda *p: "k", get=lambda *_: None,
                                          guard=lambda estimate=0.0: None)
        self.meta.spark = SimpleNamespace(_structured=self._structured)

    def _structured(self, key, messages, **kwargs):
        self.calls.append(kwargs)
        if self.fail is not None:
            raise self.fail
        return SimpleNamespace(data=self.data, usd=0.01)

    def install(self, test):
        patcher = mock.patch.dict(sys.modules, {"guestlist.meta": self.meta})
        patcher.start()
        test.addCleanup(patcher.stop)
        return self


class PlanTests(_NoKeys):
    def test_spark_reply_passes_the_gate(self) -> None:
        job = _job(self.tmp)
        fake = FakeGuestlist({"cards": [
            {"quote": "synced them together", "text": "Two cameras synced", "why": "x"},
            {"quote": "purple llama", "text": "Nope", "why": "x"}]}).install(self)
        payload = dress.plan(job, key=None)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["max_output_tokens"], 6000)
        muse = [c for c in payload["cards"] if c["source"] == "muse"]
        self.assertEqual([c["text"] for c in muse], ["Two cameras synced"])
        self.assertIn("dress_card_rejected:quote_not_found", payload["warnings"])
        self.assertTrue((job / "dress.json").exists())

    def test_402_falls_back_to_the_built_in_planner(self) -> None:
        job = _job(self.tmp)
        FakeGuestlist(fail=RuntimeError("402 Payment Required: credit exhausted")).install(self)
        payload = dress.plan(job, key=None)
        self.assertIn(dress.MUSE_FALLBACK_NOTE, payload["notes"])
        self.assertTrue(payload["cards"])
        self.assertTrue(all(c["source"] == "rule" for c in payload["cards"]))
        self.assertEqual(payload["design"]["model"], "rules")

    def test_any_propose_error_falls_back(self) -> None:
        # Review fix: an error outside Spark's own try (cache, import-time key check) still falls back.
        job = _job(self.tmp)

        def boom(**_kw):
            raise RuntimeError("402 Payment Required")
        payload = dress.plan(job, key=None, propose=boom)
        self.assertIn(dress.MUSE_FALLBACK_NOTE, payload["notes"])
        self.assertTrue(payload["cards"])

    def test_garbage_reply_and_missing_guestlist_fall_back(self) -> None:
        job = _job(self.tmp)
        FakeGuestlist({"nonsense": True}).install(self)
        self.assertTrue(dress.plan(job, key=None)["cards"])

        def missing(**_kw):
            raise dress.DressUnavailable("No module named 'guestlist'")
        payload = dress.plan(job, key=None, propose=missing)
        self.assertIn(dress.MUSE_FALLBACK_NOTE, payload["notes"])
        self.assertTrue(all(c["source"] == "rule" for c in payload["cards"]))

    def test_no_muse_uses_rules_and_never_calls_spark(self) -> None:
        job = _job(self.tmp)
        fake = FakeGuestlist({"cards": []}).install(self)
        payload = dress.plan(job, no_muse=True, key=None)
        self.assertEqual(fake.calls, [])
        self.assertTrue(payload["cards"])
        texts = [c["text"] for c in payload["cards"]]
        self.assertIn("Two cameras synced", texts)
        times = [c["t"] for c in payload["cards"]]
        self.assertEqual(times, sorted(times))
        self.assertTrue(all(t >= dress.FIRST_CARD_S for t in times))
        self.assertEqual([c["pos"] for c in payload["cards"]][:2], ["top-left", "top-right"])

    def test_replan_keeps_typed_cards(self) -> None:
        job = _job(self.tmp)
        dress.plan(job, no_muse=True, key=None)
        dress.apply_ops(job, [dress.validate_op(op, job_dir=job) for op in
                              dress.parse_note("add a card that says Hello there", job_dir=job,
                                               playhead_s=50.0)])
        payload = dress.plan(job, no_muse=True, key=None)
        self.assertIn("Hello there", [c["text"] for c in payload["cards"]])


class GrammarTests(_NoKeys):
    def setUp(self) -> None:
        super().setUp()
        self.words = dress._as_out_words(_words())

    def parse(self, text, at=None):
        return dress.parse_note(text, words=self.words, playhead_s=at)

    def test_table(self) -> None:
        cases = {
            "add a card that says Two cameras where I say synced them together":
                ("dress_add_card", {"text": "Two cameras"}),
            "Add a title that says Hello": ("dress_add_card", {"text": "Hello"}),
            "add a pop-up that says Ready": ("dress_add_card", {"text": "Ready"}),
            "add text that says Done": ("dress_add_card", {"text": "Done"}),
            "add B-roll where I say synced": ("dress_add_card", {"text": "Synced"}),
            "add a card where I say color grade": ("dress_add_card", {"text": "Color Grade"}),
            "remove the card at synced": ("dress_remove_card", {}),
            "delete the card here": ("dress_remove_card", {}),
            "lose the card": ("dress_remove_card", {}),
            "no cards": ("dress_set_cards", {"mode": "off"}),
            "more cards": ("dress_set_cards", {"mode": "more"}),
            "fewer cards": ("dress_set_cards", {"mode": "fewer"}),
            "no music": ("dress_set_music", {"enabled": False}),
            "add music": ("dress_set_music", {"enabled": True}),
            "more music": ("dress_set_music", {"gain_delta_db": 4.0}),
            "music louder": ("dress_set_music", {"gain_delta_db": 4.0}),
            "less music": ("dress_set_music", {"gain_delta_db": -4.0}),
            "quieter music": ("dress_set_music", {"gain_delta_db": -4.0}),
            "turn the music down a bit": ("dress_set_music", {"gain_delta_db": -4.0}),
            "different music: lofi piano": ("dress_set_music", {"prompt": "lofi piano"}),
            "make the music warm acoustic guitar": ("dress_set_music", {"prompt": "warm acoustic guitar"}),
            "no ghost": ("dress_set_ghost", {"enabled": False}),
            "no bing": ("dress_set_sfx", {"enabled": False}),
            "no sound effects": ("dress_set_sfx", {"enabled": False}),
        }
        for text, (op, fields) in cases.items():
            with self.subTest(text=text):
                ops = self.parse(text, at=20.0)
                self.assertIsNotNone(ops)
                self.assertEqual(ops[0]["op"], op)
                for k, v in fields.items():
                    self.assertEqual(ops[0][k], v)
        self.assertIsNone(self.parse("cut the second take"))
        self.assertIsNone(self.parse("swap to camera B here"))

    def test_add_broll_where_i_say_synced_anchors(self) -> None:
        op = self.parse("add B-roll where I say synced")[0]
        self.assertAlmostEqual(op["t"], 10.5 - 0.10, places=3)
        checked = dress.validate_op(op, plan_payload={"source": {"duration_s": 60.0}, "cards": []})
        self.assertFalse(checked["blocked"])
        self.assertEqual(checked["label"], "Card: Synced @10.4s")
        self.assertTrue(all(set(c) == {"ok", "text", "hard"} for c in checked["checks"]))

    def test_unplaced_and_title_window_block(self) -> None:
        from director.voice import UNPLACED_MESSAGE

        op = self.parse("add a card that says Hi")[0]  # no playhead, no quote
        checked = dress.validate_op(op, plan_payload={"source": {"duration_s": 60.0}})
        self.assertTrue(checked["blocked"])
        self.assertIn(UNPLACED_MESSAGE, [c["text"] for c in checked["checks"]])
        early = dress.validate_op(self.parse("add a card that says Hi", at=1.0)[0],
                                  plan_payload={"source": {"duration_s": 60.0}})
        self.assertTrue(early["blocked"])
        long = dress.validate_op(self.parse("add a card that says one two three four five six seven",
                                            at=20.0)[0],
                                 plan_payload={"source": {"duration_s": 60.0}})
        self.assertTrue(long["blocked"])

    def test_apply_ops_music_fewer_and_history(self) -> None:
        job = _job(self.tmp)
        dress.plan(job, no_muse=True, key=None)
        before = dress.load_plan(job)
        n = len([c for c in before["cards"] if c["enabled"]])
        ops = [dress.validate_op(o, job_dir=job) for o in
               self.parse("turn the music down") + self.parse("fewer cards")]
        payload = dress.apply_ops(job, ops)
        self.assertEqual(payload["music"]["gain_db"], -24.0)
        self.assertEqual(len([c for c in payload["cards"] if c["enabled"]]), (n + 1) // 2)
        self.assertEqual([h["op"] for h in payload["history"]][-2:],
                         ["dress_set_music", "dress_set_cards"])
        info = dress.summary(job)
        self.assertTrue(info["exists"])
        self.assertFalse(info["fresh"])


def _engine(root: Path, job: Path):
    demo = root / "demo"
    demo.mkdir(parents=True, exist_ok=True)
    return engine_mod.Engine(demo, render=None, job_dir=job)


class EngineTests(_NoKeys):
    def test_dress_note_stores_a_proposal(self) -> None:
        job = _job(self.tmp)
        eng = _engine(self.tmp, job)
        r = eng.dress_note("add B-roll where I say synced", 20.0)
        self.assertTrue(r["matched"])
        self.assertEqual(r["model"], "rules")
        self.assertEqual(r["ops"][0]["op"], "dress_add_card")
        self.assertFalse(r["ops"][0]["blocked"])
        self.assertIn(r["id"], eng.proposals)
        self.assertEqual(eng.dress_note("what does this cut say", 1.0), {"matched": False})
        # Review fix: a music note on a never-dressed cut goes to the chat, not an unchecked card.
        before = set(eng.proposals)
        self.assertEqual(eng.dress_note("turn the music down a bit", 1.0), {"matched": False})
        self.assertEqual(set(eng.proposals), before)

    def test_dress_notes_replaces_spark_ops_and_gates_music(self) -> None:
        job = _job(self.tmp)
        eng = _engine(self.tmp, job)
        spark = {"id": "n1", "raw_text": "add B-roll where I say synced", "status": "resolved",
                 "at_s": 10.0, "ops": [{"op": "add_note", "text": "b-roll"}]}
        music = {"id": "n2", "raw_text": "turn the music down a bit", "status": "resolved",
                 "at_s": 10.0, "ops": [{"op": "add_note", "text": "mix"}]}
        out = eng._dress_notes([spark, music])
        self.assertEqual(out[0]["ops"][0]["op"], "dress_add_card")
        self.assertEqual(out[1], music)  # no dress.json: stays a mix note
        dress.plan(job, no_muse=True, key=None)
        out = eng._dress_notes([music])
        self.assertEqual(out[0]["ops"][0]["op"], "dress_set_music")
        self.assertEqual(out[0]["ops"][0]["gain_db"], -24.0)

    def test_split_apply_writes_and_blocks(self) -> None:
        job = _job(self.tmp)
        eng = _engine(self.tmp, job)
        notes = eng._dress_notes([{"id": "n1", "raw_text": "add B-roll where I say synced",
                                   "status": "unresolved", "at_s": None, "ops": []},
                                  {"id": "n2", "raw_text": "cut that", "status": "resolved",
                                   "ops": [{"op": "drop_words", "blocked": False}]}])
        handed = eng._dress_split_apply(notes, None)
        self.assertTrue(handed[0]["ops"][0]["blocked"])
        self.assertFalse(handed[1]["ops"][0]["blocked"])
        self.assertEqual([c["text"] for c in dress.load_plan(job)["cards"]], ["Synced"])

    def test_render_busy_guard(self) -> None:
        job = _job(self.tmp)
        dress.plan(job, no_muse=True, key=None)
        eng = _engine(self.tmp, job)
        gate = threading.Event()
        with mock.patch.object(cutjob, "build_project", lambda *a, **k: {}):
            self.assertEqual(eng.dress_render(renderer=lambda j: gate.wait(5)), {"state": "running"})
            with self.assertRaises(engine_mod.Busy):
                eng.dress_render(renderer=lambda j: None)
            gate.set()
            eng._dress_thread.join(5)
        self.assertEqual(eng._dress_state["state"], "done")
        eng._cutjob_state = {"state": "running"}
        with self.assertRaises(engine_mod.Busy):
            eng.dress_render(renderer=lambda j: None)


def _voice_job(root: Path) -> Path:
    """A full director cut job (words, sync, angles, cut) the voice path can read."""
    tests = str(ROOT / "tests")
    if tests not in sys.path:
        sys.path.insert(0, tests)
    from test_cutjob import _write_job
    job = _write_job(root)
    dress.save_plan(job, {"cards": [], "music": {"enabled": True}, "ghost": {}, "sfx": {}})
    return job


class RenderLockTests(_NoKeys):
    """Render lock after verification (Claude Code, 2026-11-04, Astra F03).

    The dress render saves the plan it loaded at its start, so while it runs
    every plan change is refused (Busy, HTTP 409) and dress.json is untouched.
    """

    MUSIC_OFF = [{"op": "dress_set_music", "enabled": False}]

    def _start(self, eng) -> threading.Event:
        """Astra's renderer: load the plan, wait, save that snapshot back (the old render())."""
        ready, release = threading.Event(), threading.Event()

        def renderer(job: Path) -> None:
            old = dress.load_plan(job)
            ready.set()
            if not release.wait(10):
                raise RuntimeError("test timed out")
            old["rendered"] = {"at": "test"}
            dress.save_plan(job, old)

        self.assertEqual(eng.dress_render(renderer=renderer), {"state": "running"})
        self.assertTrue(ready.wait(5))
        return release

    def _finish(self, eng, release: threading.Event) -> None:
        release.set()
        eng._dress_thread.join(5)
        self.assertEqual(eng._dress_state["state"], "done")

    def test_edit_during_render_is_a_409_then_works_after(self) -> None:
        job = _job(self.tmp)
        dress.plan(job, no_muse=True, key=None)
        eng = _engine(self.tmp, job)
        before = dress.load_plan(job)
        card = before["cards"][0]["id"]
        with mock.patch.object(cutjob, "build_project", lambda *a, **k: {}):
            release = self._start(eng)
            try:
                for call in (lambda: eng.dress_edit(ops=self.MUSIC_OFF),
                             lambda: eng.dress_edit(card_id=card, enabled=False),
                             lambda: eng.dress_edit(card_id=card, remove=True),
                             lambda: eng.dress_plan(no_muse=True),
                             lambda: eng.dress_note("no music", 1.0)):
                    with self.assertRaises(engine_mod.Busy) as cm:
                        call()
                    self.assertEqual(str(cm.exception), engine_mod.DRESS_BUSY)
                # The route layer turns it into a 409 with the readable message (no socket).
                stub = SimpleNamespace(_json=lambda obj, code=200: (code, obj))
                self.assertEqual(engine_mod.Handler._call(stub, lambda: eng.dress_edit(ops=self.MUSIC_OFF)),
                                 (409, {"error": engine_mod.DRESS_BUSY}))
                self.assertEqual(dress.load_plan(job), before)
            finally:
                self._finish(eng, release)
        # Astra's repro: the render's snapshot save no longer undoes an accepted edit.
        self.assertTrue(dress.load_plan(job)["music"]["enabled"])
        eng.dress_edit(ops=self.MUSIC_OFF)
        eng.dress_edit(card_id=card, enabled=False)
        after = dress.load_plan(job)
        self.assertFalse(after["music"]["enabled"])
        self.assertFalse(next(c for c in after["cards"] if c["id"] == card)["enabled"])

    def test_voice_dress_ops_during_render_are_a_409(self) -> None:
        job = _voice_job(self.tmp)
        eng = _engine(self.tmp, job)
        r = eng.dress_note("no music", 1.0)
        self.assertTrue(r["matched"])
        with mock.patch.object(cutjob, "build_project", lambda *a, **k: {}):
            release = self._start(eng)
            try:
                with self.assertRaises(engine_mod.Busy) as cm:
                    eng.voice_apply(r["id"])  # an accepted voice/typed dress proposal
                self.assertEqual(str(cm.exception), engine_mod.DRESS_BUSY)
                with self.assertRaises(engine_mod.Busy):
                    eng.voice_apply(ops=self.MUSIC_OFF)  # the ops= path
                self.assertTrue(dress.load_plan(job)["music"]["enabled"])
                self.assertIn(r["id"], eng.proposals)  # the refusal did not consume it
            finally:
                self._finish(eng, release)
        # The finished render reloads the cut (proposals on the old cut are forgotten):
        # the same note, said again, now applies.
        r = eng.dress_note("no music", 1.0)
        eng.voice_apply(r["id"])
        self.assertFalse(dress.load_plan(job)["music"]["enabled"])
        self.assertEqual(dress.load_plan(job)["history"][-1]["op"], "dress_set_music")

    def test_plan_in_progress_blocks_render_and_edits(self) -> None:
        job = _job(self.tmp)
        dress.plan(job, no_muse=True, key=None)
        eng = _engine(self.tmp, job)
        entered, release, errors = threading.Event(), threading.Event(), []
        real_plan = dress.plan

        def slow_plan(j, **kw):
            entered.set()
            release.wait(10)
            return real_plan(j, key=None, **kw)

        def run() -> None:
            try:
                eng.dress_plan(no_muse=True)
            except Exception as error:  # noqa: BLE001 - asserted below
                errors.append(error)

        with mock.patch.object(dress, "plan", slow_plan), \
                mock.patch.object(cutjob, "build_project", lambda *a, **k: {}):
            th = threading.Thread(target=run)
            th.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(engine_mod.Busy):
                    eng.dress_render(renderer=lambda j: None)
                with self.assertRaises(engine_mod.Busy) as cm:
                    eng.dress_edit(ops=[{"op": "dress_set_ghost", "enabled": False}])
                self.assertEqual(str(cm.exception), engine_mod.DRESS_PLANNING)
            finally:
                release.set()
                th.join(10)
        self.assertEqual(errors, [])
        self.assertFalse(eng._dress_planning)
        eng.dress_edit(ops=[{"op": "dress_set_ghost", "enabled": False}])
        self.assertFalse(dress.load_plan(job)["ghost"]["enabled"])

    def test_render_save_keeps_a_newer_plan(self) -> None:
        job = _job(self.tmp)
        dress.plan(job, no_muse=True, key=None)
        snapshot = dress.load_plan(job)  # what render() loaded at its start
        snapshot.setdefault("warnings", []).append("dress_ghost_skipped:icon or Pillow missing")
        newer = dress.load_plan(job)
        newer["music"]["enabled"] = False  # an edit that landed while it ran (CLI, another tool)
        dress.save_plan(job, newer)
        rendered = {"file": "cut.dressed.mp4", "source_stamp": "1:2",
                    "plan_fingerprint": dress._plan_fingerprint(snapshot), "at": "t"}
        out = dress._save_rendered(job, snapshot, rendered)
        saved = dress.load_plan(job)
        self.assertEqual(saved, out)
        self.assertFalse(saved["music"]["enabled"])
        self.assertEqual(saved["rendered"], rendered)
        self.assertIn("dress_ghost_skipped:icon or Pillow missing", saved["warnings"])
        # Rendered from the older plan, so it reads as stale, not fresh.
        self.assertNotEqual(saved["rendered"]["plan_fingerprint"], dress._plan_fingerprint(saved))


class BuildProjectTests(_NoKeys):
    def _fresh(self, job: Path, *, stale: bool = False) -> None:
        (job / "cut.mp4").write_bytes(b"plain")
        (job / "cut.dressed.mp4").write_bytes(b"dressed")
        payload = dress.plan(job, no_muse=True, key=None)
        payload["rendered"] = {"file": "cut.dressed.mp4",
                               "source_stamp": "0:0" if stale else dress._stamp(job / "cut.mp4"),
                               "plan_fingerprint": dress._plan_fingerprint(payload), "at": ""}
        dress.save_plan(job, payload)

    def test_fresh_links_and_cards(self) -> None:
        job = _job(self.tmp)
        self._fresh(job)
        out = self.tmp / "demo"
        with mock.patch.object(cutjob, "_stills", lambda *a: None):
            project = cutjob.build_project(job, out)
        self.assertTrue((out / "dressed.mp4").is_symlink())
        self.assertEqual((out / "video.mp4").resolve(), (job / "cut.mp4").resolve())
        self.assertEqual(project["cutJob"]["dress"]["video"], "demo/dressed.mp4")
        self.assertTrue(project["cutJob"]["dress"]["fresh"])
        self.assertTrue(project["cards"])
        self.assertTrue(all(isinstance(c["lines"], list) for c in project["cards"]))
        # Review fix: muse.context indexes c["caption"]; the chat must survive a dressed cut.
        sys.path.insert(0, str(ROOT / "app"))
        import muse as legacy_muse
        legacy_muse.context(project, 5.0)

    def test_stale_is_ignored(self) -> None:
        job = _job(self.tmp)
        out = self.tmp / "demo"
        out.mkdir()
        (out / "dressed.mp4").write_bytes(b"old")
        self._fresh(job, stale=True)
        with mock.patch.object(cutjob, "_stills", lambda *a: None):
            project = cutjob.build_project(job, out)
        self.assertFalse((out / "dressed.mp4").exists())
        self.assertFalse(project["cutJob"]["dress"]["fresh"])


def _ff(*args) -> None:
    done = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


@unittest.skipUnless(HAVE_FFMPEG and HAVE_PIL, "ffmpeg/ffprobe or Pillow missing")
class EndToEndTests(_NoKeys):
    def test_render_a_synthetic_cut(self) -> None:
        words = _words("this is the part where the cameras are synced", start=0.5, step=0.4)
        job = _job(self.tmp, duration=6.0, words=words)
        _ff("-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=24000/1001:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=300:duration=6", "-c:v", "libx264",
            "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(job / "cut.mp4"))
        assets = job / "dress" / "assets"
        assets.mkdir(parents=True)
        _ff("-f", "lavfi", "-i", "sine=frequency=880:duration=1", str(assets / "bing-test.mp3"))
        _ff("-f", "lavfi", "-i", "sine=frequency=110:duration=30", str(assets / "music-test.mp3"))
        payload = dress.plan(job, no_muse=True, key=None)
        payload["cards"] = [{"id": "c1", "t": 3.85, "d": 2.0, "text": "Cameras synced",
                             "pos": "top-left", "ghost": True, "enabled": True, "source": "typed"}]
        payload["assets"] = [{"kind": "bing", "file": "bing-test.mp3"},
                             {"kind": "music", "file": "music-test.mp3"}]
        payload["music"]["enabled"] = True
        dress.save_plan(job, payload)
        before = (job / "cut.mp4").read_bytes()
        out = dress.render(job)
        self.assertEqual((job / "cut.mp4").read_bytes(), before)
        stats = dress.extract_media_stats(dress.probe_media(job / "cut.dressed.mp4"))
        self.assertAlmostEqual(float(stats["duration_s"]), 6.0, delta=0.1)
        self.assertTrue(stats["has_audio"])
        self.assertEqual((int(stats["width"]), int(stats["height"])), (1920, 1080))
        self.assertTrue(dress.is_fresh(job, out))
        self.assertEqual(dress.load_status(job)["state"], "done")
        self.assertFalse((job / "cut.dressed.next.mp4").exists())


class CliTests(_NoKeys):
    def test_plan_and_show_offline(self) -> None:
        import contextlib
        import io

        from director import cli as cli_mod

        job = _job(self.tmp)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli_mod.main(["dress", "plan", "--job", str(job), "--no-muse",
                                           "--ai-video", "1"]), 0)
            self.assertEqual(cli_mod.main(["dress", "show", "--job", str(job)]), 0)
        out = buf.getvalue()
        self.assertIn("not built, use DRESS stretch", out)
        self.assertIn("Two cameras synced", out)
        self.assertIn("Next: director dress render", out)
        self.assertIn("Render: not rendered", out)


class UiTests(unittest.TestCase):
    def test_dress_js_parses_and_hooks_exist(self) -> None:
        ui = ROOT / "app" / "ui"
        if shutil.which("node"):
            done = subprocess.run(["node", "--check", str(ui / "dress.js")], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
        js = (ui / "app.js").read_text()
        self.assertIn("window.GCDress?.mount(", js)
        self.assertIn("window.GCDress?.refresh?.()", js)
        html = (ui / "index.html").read_text()
        self.assertIn('id="dressBtn"', html)
        self.assertIn('src="dress.js"', html)


if __name__ == "__main__":
    unittest.main()
