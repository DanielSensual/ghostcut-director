"""Offline tests for burned-in captions and the cinematic title.

Grouping and timing run on synthetic words, the output-time mapping on a
synthetic tighten payload, SRT/ASS/title checks on text, the Spark title
proposal behind a stand-in guestlist, and one skipUnless(ffmpeg) test burns
captions plus a title onto a short lavfi clip. No Meta API, no Whisper.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from director import captions as captions_mod
from director import cut as cut_mod
from director.angles import TimedWord
from director.captions import CaptionLine, CaptionWord

FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

try:
    from PIL import Image  # noqa: F401

    PIL_OK = True
except ImportError:
    PIL_OK = False

HOUSE_STYLE = ("Style: Cap,Poppins SemiBold,82,&H00FFFFFF,&H00FFFFFF,"
               "&H4C000000,&H78000000,0,0,0,0,100,100,0,0,1,5,0,2,60,60,780,1")


def _w(text: str, start: float, end: float, **kwargs) -> TimedWord:
    return TimedWord(text, start, end, kwargs.get("speaker", ""),
                     kwargs.get("heard_by", "whisper"),
                     kwargs.get("disfluency", False))


def _cw(text: str, start: float, end: float, span: int = 0) -> CaptionWord:
    return CaptionWord(text, start, end, span)


def _words_file(path: Path, words: list[TimedWord]) -> Path:
    entries = []
    for word in words:
        entry: dict = {"word": word.text, "start": word.start_s,
                       "end": word.end_s}
        if word.speaker:
            entry["speaker"] = word.speaker
        if word.heard_by != "whisper":
            entry["heard_by"] = word.heard_by
        if word.disfluency:
            entry["disfluency"] = True
        entries.append(entry)
    path.write_text(json.dumps({"words": entries}), encoding="utf-8")
    return path


def _angles_file(path: Path, shots: list[tuple[float, float]]) -> Path:
    payload = {"version": 1, "shots": [
        {"id": i, "start_s": start, "end_s": end, "camera": "A",
         "clip_path": "/media/A.mp4", "clip_in_s": 0.0,
         "clip_out_s": round(end - start, 3), "line_ids": [i],
         "shot_size": "medium", "reason": "test", "method": "rule",
         "overrules": [], "lut": None}
        for i, (start, end) in enumerate(shots)]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _tighten_payload(kept: list[tuple[float, float]]) -> dict:
    return {"version": 1, "beats": [],
            "kept_spans": [{"id": f"K{i}", "start_s": start, "end_s": end,
                            "beat_id": 0, "attempt_id": 0,
                            "utterance_ids": [], "kind": "keeper",
                            "reason": ""} for i, (start, end) in enumerate(kept)],
            "removed_spans": []}


class FilterTests(unittest.TestCase):
    def test_drops_muse_only_and_disfluency(self) -> None:
        words = [_w("Hello", 0.0, 0.2), _w("um", 0.2, 0.3, disfluency=True),
                 _w("there", 0.3, 0.5, heard_by="muse"),
                 _w("world", 0.5, 0.7)]
        kept = captions_mod.filter_caption_words(words)
        self.assertEqual([w.text for w in kept], ["Hello", "world"])


class MappingTests(unittest.TestCase):
    def test_identity_without_tighten_and_origin_shift(self) -> None:
        words = [_w("cut", 10.0, 10.2), _w("off", 10.2, 10.4),
                 _w("early", 9.0, 9.2)]
        got = captions_mod.map_words_to_output(words, None, 10.0, 6.0)
        self.assertEqual([(w.text, w.start_s, w.end_s) for w in got],
                         [("cut", 0.0, 0.2), ("off", 0.2, 0.4)])

    def test_drops_past_duration_and_clamps_end(self) -> None:
        words = [_w("last", 5.8, 6.5), _w("gone", 7.0, 7.2)]
        got = captions_mod.map_words_to_output(words, None, 0.0, 6.0)
        self.assertEqual([(w.text, w.start_s, w.end_s) for w in got],
                         [("last", 5.8, 6.0)])

    def test_tighten_mapping_keeps_and_drops(self) -> None:
        tighten = _tighten_payload([(10.0, 12.0), (20.0, 22.0)])
        words = [_w("kept", 10.5, 10.7), _w("cut", 15.0, 15.2),
                 _w("span", 20.5, 20.6), _w("straddle", 11.9, 12.1)]
        got = captions_mod.map_words_to_output(words, tighten, 0.0, 4.0)
        self.assertEqual([(w.text, w.start_s, w.end_s, w.span) for w in got],
                         [("kept", 0.5, 0.7, 0), ("span", 2.5, 2.6, 1)])


class GroupingTests(unittest.TestCase):
    def test_house_word_counts_and_remainder(self) -> None:
        words = [_cw(f"w{i}", i * 0.3, i * 0.3 + 0.2) for i in range(9)]
        lines = captions_mod.group_lines(words, [])
        self.assertEqual([len(line.words) for line in lines], [4, 2, 3])
        self.assertEqual(sum(len(line.words) for line in lines), 9)
        words = [_cw(f"w{i}", i * 0.3, i * 0.3 + 0.2) for i in range(5)]
        lines = captions_mod.group_lines(words, [])
        self.assertEqual([len(line.words) for line in lines], [2, 3])

    def test_splits_on_pause_cut_and_span(self) -> None:
        words = [_cw("one", 0.0, 0.2), _cw("two", 1.0, 1.2),
                 _cw("three", 1.3, 1.5), _cw("four", 1.6, 1.8, span=1)]
        lines = captions_mod.group_lines(words, [1.25])
        self.assertEqual([[w.text for w in line.words] for line in lines],
                         [["one"], ["two"], ["three"], ["four"]])

    def test_line_floor_and_text(self) -> None:
        words = [_cw("Hi", 1.0, 1.1), _cw("there,", 1.1, 1.2)]
        (line,) = captions_mod.group_lines(words, [])
        self.assertEqual((line.start_s, line.end_s), (1.0, 1.4))
        self.assertEqual(line.text, "Hi there")
        words = [_cw("Ends.", 2.0, 2.2), _cw("now", 2.2, 2.4)]
        (line,) = captions_mod.group_lines(words, [])
        self.assertEqual(line.text, "Ends. now")

    def test_floor_never_enters_next_line(self) -> None:
        words = [_cw("a", 0.0, 0.1), _cw("b", 0.1, 0.2),
                 _cw("c", 1.0, 1.1), _cw("d", 1.1, 1.2)]
        first, second = captions_mod.group_lines(words, [])
        self.assertEqual((first.start_s, first.end_s), (0.0, 0.4))
        self.assertEqual((second.start_s, second.end_s), (1.0, 1.4))

    def test_bold_caps_and_counts(self) -> None:
        words = [_cw(f"w{i}", i * 0.3, i * 0.3 + 0.2) for i in range(7)]
        lines = captions_mod.group_lines(words, [], min_words=3, max_words=4,
                                         caps=True)
        self.assertEqual([len(line.words) for line in lines], [4, 3])
        self.assertTrue(all(line.text == line.text.upper() for line in lines))
        words = [_cw(f"w{i}", i * 0.3, i * 0.3 + 0.2) for i in range(5)]
        (line,) = captions_mod.group_lines(words, [], min_words=3,
                                           max_words=4, caps=True)
        self.assertEqual(len(line.words), 5)


class AssSrtTests(unittest.TestCase):
    def _lines(self) -> list[CaptionLine]:
        words = [_cw("Hello", 1.0, 1.2), _cw("world", 1.2, 1.5)]
        return captions_mod.group_lines(words, [])

    def test_house_style_verbatim_and_playres(self) -> None:
        text = captions_mod.build_ass(self._lines(), preset="house",
                                      width=1080, height=1920)
        self.assertIn(HOUSE_STYLE, text.splitlines())
        self.assertIn("PlayResX: 1080", text)
        self.assertIn("PlayResY: 1920", text)

    def test_per_word_colour_events(self) -> None:
        text = captions_mod.build_ass(self._lines(), preset="house",
                                      width=1080, height=1920)
        events = [line for line in text.splitlines()
                  if line.startswith("Dialogue:")]
        self.assertEqual(len(events), 2)
        self.assertTrue(all("{\\blur7}" in event for event in events))
        self.assertIn("{\\c&H00A4D9F3&}Hello{\\c&H00FFFFFF&} world",
                      events[0])
        self.assertIn("Hello {\\c&H00A4D9F3&}world{\\c&H00FFFFFF&}",
                      events[1])
        self.assertNotIn("{\\fs", text)
        first, second = (event.split(",") for event in events)
        self.assertEqual((first[1], first[2]), ("0:00:01.00", "0:00:01.20"))
        self.assertEqual((second[1], second[2]), ("0:00:01.20", "0:00:01.50"))

    def test_horizontal_scaling(self) -> None:
        text = captions_mod.build_ass(self._lines(), preset="house",
                                      width=1920, height=1080)
        self.assertIn("Style: Cap,Poppins SemiBold,58,", text)
        self.assertIn(",1,4,0,2,115,115,119,1", text)
        events = [line for line in text.splitlines()
                  if line.startswith("Dialogue:")]
        self.assertTrue(all("{\\blur4}" in event for event in events))

    def test_bold_style_and_unknown_preset(self) -> None:
        text = captions_mod.build_ass(self._lines(), preset="bold",
                                      width=1080, height=1920)
        self.assertIn("Style: Cap,Montserrat ExtraBold,82,", text)
        with self.assertRaises(captions_mod.CaptionsError):
            captions_mod.style_line("fancy", 1080, 1920)

    def test_srt_shape(self) -> None:
        lines = self._lines()
        self.assertEqual(
            captions_mod.build_srt(lines),
            "1\n00:00:01,000 --> 00:00:01,500\nHello world\n")
        self.assertEqual(captions_mod.parse_srt_times("1\n00:00:01,000 --> "
                                                      "00:00:01,500\nx\n"),
                         [(1.0, 1.5)])
        self.assertEqual(captions_mod.parse_srt_times("garbage"), [])


class TitleCheckTests(unittest.TestCase):
    def test_accepts_spoken_five_to_eight(self) -> None:
        spoken = {"we", "cut", "the", "interview", "in", "one", "pass",
                  "today"}
        ok, _ = captions_mod.check_title("We cut the interview today",
                                         spoken)
        self.assertTrue(ok)
        ok, _ = captions_mod.check_title("We cut the interview in one pass",
                                         spoken)
        self.assertTrue(ok)

    def test_rejects_unspoken_and_wrong_length(self) -> None:
        spoken = {"we", "cut", "the", "interview", "today"}
        ok, why = captions_mod.check_title("We cut the wedding today",
                                           spoken)
        self.assertFalse(ok)
        self.assertIn("title_unspoken:wedding", why)
        ok, why = captions_mod.check_title("We cut today", spoken)
        self.assertFalse(ok)
        self.assertEqual(why, "title_length:3")
        ok, why = captions_mod.check_title(
            "We cut the interview today today today today today", spoken)
        self.assertFalse(ok)
        self.assertEqual(why, "title_length:9")


class FakeGuestlist:
    """A stand-in guestlist.meta: canned title reply, recorded calls."""

    def __init__(self, data=None, usd: float = 0.0012, hit=None, fail=None):
        self.data = data if data is not None else {
            "title": "We cut the interview today",
            "reason": "Said at 1s."}
        self.usd = usd
        self.hit = hit
        self.fail = fail
        self.calls: list[dict] = []
        self.meta = types.ModuleType("guestlist.meta")
        self.meta.cache = SimpleNamespace(
            key=lambda *parts: "fake-key",
            get=lambda _kind, _key: self.hit,
            guard=lambda estimate=0.0: None)
        self.meta.spark = SimpleNamespace(_structured=self._structured)

    def _structured(self, key, messages, **kwargs):
        self.calls.append({"key": key, "messages": messages, **kwargs})
        if self.fail is not None:
            raise self.fail
        return SimpleNamespace(data=self.data, usd=self.usd)

    def install(self, test: unittest.TestCase):
        patcher = mock.patch.dict(sys.modules, {"guestlist.meta": self.meta})
        patcher.start()
        test.addCleanup(patcher.stop)
        return self


class TitleSparkTests(unittest.TestCase):
    def _spoken(self) -> set[str]:
        return {"we", "cut", "the", "interview", "in", "one", "pass",
                "today"}

    def test_auto_title_accepted_and_spend_counted(self) -> None:
        fake = FakeGuestlist().install(self)
        title, reason, spend, cached, warnings = \
            captions_mod.propose_title_with_muse(
                transcript="[1.0s] We cut the interview today",
                spoken=self._spoken(), model="muse-spark-1.3",
                max_usd=5.0)
        self.assertEqual(title, "We cut the interview today")
        self.assertEqual(reason, "Said at 1s.")
        self.assertAlmostEqual(spend, 0.0012)
        self.assertFalse(cached)
        self.assertEqual(warnings, [])
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertEqual(call["name"], "title_hook")
        self.assertEqual(call["model"], "muse-spark-1.3")
        self.assertIn("first minute", call["messages"][0]["text"])
        self.assertEqual(call["schema"], captions_mod.title_schema())

    def test_unspoken_reply_rejected_but_spend_kept(self) -> None:
        fake = FakeGuestlist(data={"title": "We cut the wedding today",
                                   "reason": "Invented."}).install(self)
        title, _reason, spend, _cached, warnings = \
            captions_mod.propose_title_with_muse(
                transcript="[1.0s] We cut the interview today",
                spoken=self._spoken(), model="muse-spark-1.3",
                max_usd=5.0)
        self.assertIsNone(title)
        self.assertAlmostEqual(spend, 0.0012)
        self.assertEqual(warnings, ["title_rejected:title_unspoken:wedding"])
        self.assertEqual(len(fake.calls), 1)

    def test_over_budget_never_calls(self) -> None:
        fake = FakeGuestlist().install(self)
        title, _reason, spend, _cached, warnings = \
            captions_mod.propose_title_with_muse(
                transcript="x" * 100, spoken=self._spoken(),
                model="muse-spark-1.3", max_usd=0.0)
        self.assertIsNone(title)
        self.assertEqual(spend, 0.0)
        self.assertEqual(warnings, ["title_unavailable:exceeds --max-usd"])
        self.assertEqual(fake.calls, [])

    def test_cache_hit_costs_nothing(self) -> None:
        hit = {"data": {"title": "We cut the interview today",
                        "reason": "Cached."}}
        fake = FakeGuestlist(hit=hit).install(self)
        title, reason, spend, cached, warnings = \
            captions_mod.propose_title_with_muse(
                transcript="[1.0s] We cut the interview today",
                spoken=self._spoken(), model="muse-spark-1.3",
                max_usd=5.0)
        self.assertEqual((title, reason, spend, cached, warnings),
                         ("We cut the interview today", "Cached.",
                          0.0, True, []))
        self.assertEqual(fake.calls, [])

    def test_failed_call_degrades(self) -> None:
        FakeGuestlist(fail=RuntimeError("boom")).install(self)
        title, _reason, spend, _cached, warnings = \
            captions_mod.propose_title_with_muse(
                transcript="hello", spoken=self._spoken(),
                model="muse-spark-1.3", max_usd=5.0)
        self.assertIsNone(title)
        self.assertEqual(spend, 0.0)
        self.assertEqual(warnings, ["title_unavailable:RuntimeError: boom"])

    def test_missing_guestlist_raises_title_unavailable(self) -> None:
        with mock.patch.dict(sys.modules, {"guestlist.meta": None}):
            with self.assertRaises(captions_mod.TitleUnavailable):
                captions_mod.propose_title_with_muse(
                    transcript="hello", spoken=self._spoken(),
                    model="muse-spark-1.3", max_usd=5.0)


class EstimateTests(unittest.TestCase):
    def test_estimate_positive_and_default_rates(self) -> None:
        estimate, note = captions_mod.estimate_title_usd("hello",
                                                         "muse-spark-1.3")
        self.assertGreater(estimate, 0.0)
        self.assertIsNone(note)
        _estimate, note = captions_mod.estimate_title_usd(
            "hello", "muse-future-9")
        self.assertEqual(note, "captions_estimate_default_rates:muse-future-9")


class TitleFontTests(unittest.TestCase):
    def test_prefers_playfair_then_georgia_then_shipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fonts = Path(tmp)
            (fonts / "Georgia Bold.ttf").write_text("x", encoding="utf-8")
            (fonts / "PlayfairDisplay-SemiBold.ttf").write_text(
                "x", encoding="utf-8")
            with mock.patch.object(Path, "home", return_value=Path(tmp)):
                path, label = captions_mod.resolve_title_font(fonts)
                self.assertEqual(label, "playfair")
                (fonts / "PlayfairDisplay-SemiBold.ttf").unlink()
                path, label = captions_mod.resolve_title_font(fonts)
                self.assertEqual(label, "georgia")
                self.assertTrue(str(path).endswith("Georgia Bold.ttf"))

    def test_falls_back_to_shipped_poppins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(Path, "home",
                                   return_value=Path(tmp) / "nope"):
                with mock.patch.object(Path, "iterdir",
                                       side_effect=OSError("denied")):
                    path, label = captions_mod.resolve_title_font(None)
        self.assertEqual((path, label),
                         (captions_mod.SHIPPED_POPPINS, "poppins"))


@unittest.skipUnless(PIL_OK, "needs Pillow")
class TitlePngTests(unittest.TestCase):
    def test_autofit_short_and_long(self) -> None:
        font = captions_mod.SHIPPED_POPPINS
        lines, size = captions_mod.fit_title_lines("We cut it today", font,
                                                  max_width=960,
                                                  max_size=108)
        self.assertEqual(lines, ["We cut it today"])
        self.assertEqual(size, 108)
        lines, size = captions_mod.fit_title_lines(
            "We cut the whole interview in one single pass today", font,
            max_width=960, max_size=108)
        self.assertEqual(len(lines), 2)
        self.assertLessEqual(size, 108)

    def test_render_png_has_gradient_and_text(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "title.png"
            info = captions_mod.render_title_png(
                "We cut the interview today", png, width=1080, height=1920,
                font_path=captions_mod.SHIPPED_POPPINS)
            img = Image.open(png).convert("RGBA")
            self.assertEqual(img.size, (1080, 1920))
            self.assertEqual(info["size"], 108)
            top_left = img.getpixel((10, 10))
            self.assertEqual(top_left[:3], (0, 0, 0))
            self.assertGreater(top_left[3], 100)
            bottom = img.getpixel((540, 1900))
            self.assertEqual(bottom[3], 0)
            band = img.crop((0, 240, 1080, 420)).convert("L")
            self.assertGreater(band.getextrema()[1], 200)


class BurnCommandTests(unittest.TestCase):
    def test_plain_burn_shape(self) -> None:
        cmd = captions_mod.burn_command(
            clean_path=Path("/job/vertical.clean.mp4"),
            ass_path=Path("/job/captions.ass"),
            fonts_dir=Path("/repo/assets/fonts"),
            out_path=Path("/job/vertical.mp4"), title_png=None)
        self.assertEqual(cmd[:6],
                         ["ffmpeg", "-v", "error", "-y", "-i",
                          "/job/vertical.clean.mp4"])
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("subtitles=filename=/job/captions.ass", vf)
        self.assertIn("fontsdir=/repo/assets/fonts", vf)
        self.assertEqual(cmd[cmd.index("-c:v") + 1:cmd.index("-c:v") + 4],
                         ["libx264", "-preset", "medium"])
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")
        self.assertEqual(cmd[-1], "/job/vertical.mp4")

    def test_title_overlay_comes_before_subtitles(self) -> None:
        cmd = captions_mod.burn_command(
            clean_path=Path("/job/vertical.clean.mp4"),
            ass_path=Path("/job/captions.ass"),
            fonts_dir=Path("/repo/assets/fonts"),
            out_path=Path("/job/vertical.mp4"),
            title_png=Path("/job/title.png"))
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertLess(graph.index("overlay="), graph.index("subtitles="))
        self.assertIn("enable='between(t,0,3.5)'", graph)
        self.assertIn("/job/title.png", cmd)

    def test_stage_fonts_dir_combines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "job"
            out_dir.mkdir()
            mine = Path(tmp) / "mine"
            mine.mkdir()
            (mine / "Montserrat-ExtraBold.otf").write_text("x",
                                                           encoding="utf-8")
            staged = captions_mod.stage_fonts_dir(out_dir, mine)
            self.assertEqual(staged, out_dir / "caption-fonts")
            names = sorted(p.name for p in staged.iterdir())
            self.assertIn("Montserrat-ExtraBold.otf", names)
            self.assertIn("poppins-semibold.otf", names)
            shutil.rmtree(staged)
            plain = captions_mod.stage_fonts_dir(out_dir, None)
            self.assertEqual(plain, out_dir / "caption-fonts")
            names = sorted(p.name for p in plain.iterdir())
            self.assertEqual(names, ["poppins-semibold.otf"])


def _job_words() -> list[TimedWord]:
    return [_w("We", 0.5, 0.7), _w("cut", 0.7, 0.9),
            _w("the", 0.9, 1.1), _w("interview", 1.1, 1.5),
            _w("in", 2.5, 2.6), _w("one", 2.6, 2.8),
            _w("pass", 2.8, 3.1), _w("today.", 3.1, 3.4)]


class RunTests(unittest.TestCase):
    def _job(self, tmp: Path) -> Path:
        job = tmp / "job"
        job.mkdir()
        _words_file(job / "words.json", _job_words())
        _angles_file(job / "angles.json", [(0.0, 3.0), (3.0, 6.0)])
        return job

    def test_run_without_video_writes_files_and_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp))
            with mock.patch.object(captions_mod, "run_command") as run_mock:
                payload = captions_mod.run(
                    words_path=job / "words.json",
                    angles_path=job / "angles.json", tighten_path=None,
                    out_dir=job)
            run_mock.assert_not_called()
            self.assertEqual(payload["lines_n"], 2)
            self.assertEqual(payload["burned"], [])
            self.assertIn("captions_no_video:nothing to burn into",
                          payload["warnings"])
            self.assertTrue((job / "captions.srt").exists())
            self.assertTrue((job / "captions.ass").exists())
            self.assertTrue((job / "captions.json").exists())
            saved = json.loads((job / "captions.json").read_text(
                encoding="utf-8"))
            self.assertEqual(saved["lines_n"], 2)
            self.assertEqual(saved["lines"][0]["text"],
                             "We cut the interview")
            self.assertIsNone(saved["title"])

    def test_run_skips_burn_when_nothing_heard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp))
            _words_file(job / "words.json",
                        [_w("um", 0.5, 0.7, disfluency=True)])
            (job / "vertical.mp4").write_text("fake", encoding="utf-8")
            with mock.patch.object(captions_mod, "run_command") as run_mock:
                payload = captions_mod.run(
                    words_path=job / "words.json",
                    angles_path=job / "angles.json", tighten_path=None,
                    out_dir=job)
            run_mock.assert_not_called()
            self.assertEqual(payload["burned"], [])
            self.assertFalse((job / "vertical.clean.mp4").exists())
            self.assertTrue(any(w.startswith("captions_no_lines")
                                for w in payload["warnings"]))

    def test_run_burns_from_clean_on_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp))
            (job / "vertical.mp4").write_text("captioned", encoding="utf-8")
            (job / "vertical.clean.mp4").write_text("clean", encoding="utf-8")
            seen: list[list[str]] = []
            with mock.patch.object(
                    captions_mod, "run_command",
                    side_effect=lambda cmd: seen.append(cmd)):
                with mock.patch.object(captions_mod, "_probe_size",
                                       return_value=(1080, 1920)):
                    payload = captions_mod.run(
                        words_path=job / "words.json",
                        angles_path=job / "angles.json",
                        tighten_path=None, out_dir=job)
            self.assertEqual(payload["burned"], ["vertical.mp4"])
            self.assertEqual(payload["clean"], ["vertical.clean.mp4"])
            self.assertEqual(len(seen), 1)
            self.assertIn(str(job / "vertical.clean.mp4"), seen[0])
            self.assertEqual((job / "vertical.clean.mp4").read_text(
                encoding="utf-8"), "clean")

    def test_run_rejects_bad_preset_and_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp))
            with self.assertRaises(captions_mod.CaptionsError):
                captions_mod.run(
                    words_path=job / "words.json",
                    angles_path=job / "angles.json", tighten_path=None,
                    out_dir=job, preset="fancy")
            with self.assertRaises(captions_mod.CaptionsError):
                captions_mod.run(
                    words_path=job / "words.json",
                    angles_path=job / "angles.json", tighten_path=None,
                    out_dir=job, max_usd=-1.0)

    def test_explicit_title_renders_png(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp))
            with mock.patch.object(captions_mod, "run_command"):
                payload = captions_mod.run(
                    words_path=job / "words.json",
                    angles_path=job / "angles.json", tighten_path=None,
                    out_dir=job, title="We cut the interview today")
            title = payload["title"]
            if PIL_OK:
                self.assertEqual(title["text"], "We cut the interview today")
                self.assertEqual(title["source"], "explicit")
                self.assertTrue((job / "title.png").exists())
            else:
                self.assertIsNone(title)
                self.assertTrue(any(w.startswith("title_unavailable")
                                    for w in payload["warnings"]))

    def test_no_title_flag_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = self._job(Path(tmp))
            with mock.patch.object(captions_mod, "run_command"):
                payload = captions_mod.run(
                    words_path=job / "words.json",
                    angles_path=job / "angles.json", tighten_path=None,
                    out_dir=job, title="We cut it", no_title=True)
            self.assertIsNone(payload["title"])
            self.assertIn("captions_title_disabled:--no-title",
                          payload["warnings"])


class QcTests(unittest.TestCase):
    def _payloads(self) -> tuple[dict, dict]:
        sync = {"reference": "/media/A.mp4", "methods": [],
                "clips": [{"label": "A", "offset_s": 0.0, "method": "ref",
                           "camera": {}, "estimates": [], "checks": [],
                           "warnings": []}]}
        angles = {"version": 1, "shots": [
            {"id": 0, "start_s": 0.0, "end_s": 6.0, "camera": "A",
             "clip_path": "/media/A.mp4", "clip_in_s": 0.0,
             "clip_out_s": 6.0, "line_ids": [0], "shot_size": "medium",
             "reason": "t", "method": "rule", "overrules": []}]}
        return sync, angles

    def _probe(self, duration: float = 6.0):
        def _fake(path):
            return {"streams": [{"codec_type": "video", "codec_name": "h264",
                                 "width": 1080, "height": 1920}],
                    "format": {"duration": str(duration)}}
        return _fake

    def _loudness(self):
        return SimpleNamespace(
            returncode=0, stdout="", stderr="[Parsed_loudnorm_0]\n"
            '{"input_i": "-16.00", "input_tp": "-1.50", '
            '"input_lra": "3.20", "input_thresh": "-26.00", '
            '"target_offset": "-8.00"}')

    def _write_captions(self, out_dir: Path,
                        lines: list[dict] | None = None) -> None:
        (out_dir / "captions.srt").write_text("1\n00:00:01,000 --> "
                                              "00:00:01,500\nHi\n\n",
                                              encoding="utf-8")
        (out_dir / "captions.ass").write_text("[Events]\n", encoding="utf-8")
        (out_dir / "captions.json").write_text(json.dumps(
            {"version": 1, "lines": lines if lines is not None else [
                {"id": 0, "start_s": 1.0, "end_s": 1.5}]}), encoding="utf-8")

    def test_captions_checks_pass(self) -> None:
        sync, angles = self._payloads()
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / "cut.mp4").write_text("x", encoding="utf-8")
            (out_dir / "vertical.mp4").write_text("x", encoding="utf-8")
            self._write_captions(out_dir)
            with mock.patch.object(cut_mod, "probe_media",
                                   self._probe()), \
                    mock.patch.object(cut_mod, "run_command",
                                      lambda *a, **k: self._loudness()):
                checks = cut_mod.mechanical_qc(
                    out_dir=out_dir, sync_payload=sync,
                    angles_payload=angles, reframe_payload=None,
                    export_section=None,
                    captions_section={"file": "captions.json"})
        by_id = {check.id: check for check in checks}
        self.assertEqual(by_id["captions_exist"].status, "pass")
        self.assertEqual(by_id["captions_in_range"].status, "pass")

    def test_captions_checks_fail(self) -> None:
        sync, angles = self._payloads()
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / "cut.mp4").write_text("x", encoding="utf-8")
            self._write_captions(
                out_dir, [{"id": 0, "start_s": 1.0, "end_s": 9.0}])
            (out_dir / "captions.ass").unlink()
            with mock.patch.object(cut_mod, "probe_media",
                                   self._probe()), \
                    mock.patch.object(cut_mod, "run_command",
                                      lambda *a, **k: self._loudness()):
                checks = cut_mod.mechanical_qc(
                    out_dir=out_dir, sync_payload=sync,
                    angles_payload=angles, reframe_payload=None,
                    export_section=None,
                    captions_section={"file": "captions.json"})
        by_id = {check.id: check for check in checks}
        self.assertEqual(by_id["captions_exist"].status, "fail")
        self.assertEqual(by_id["captions_in_range"].status, "fail")
        self.assertEqual(by_id["captions_in_range"].detail["outside_ids"],
                         [0])

    def test_no_captions_section_no_checks(self) -> None:
        sync, angles = self._payloads()
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            with mock.patch.object(cut_mod, "probe_media",
                                   self._probe()), \
                    mock.patch.object(cut_mod, "run_command",
                                      lambda *a, **k: self._loudness()):
                checks = cut_mod.mechanical_qc(
                    out_dir=out_dir, sync_payload=sync,
                    angles_payload=angles, reframe_payload=None,
                    export_section=None)
        self.assertNotIn("captions_exist", [check.id for check in checks])


class WiringTests(unittest.TestCase):
    def test_stage_order_and_cli_flags(self) -> None:
        from director import cli as cli_mod

        self.assertLess(cut_mod.STAGES.index("reframe"),
                        cut_mod.STAGES.index("captions"))
        self.assertLess(cut_mod.STAGES.index("captions"),
                        cut_mod.STAGES.index("qc"))
        parser = cli_mod.build_parser()
        args = parser.parse_args(["cut", "a.mp4", "b.mp4"])
        self.assertFalse(args.no_captions)
        self.assertEqual(args.captions_preset, "house")
        self.assertIsNone(args.title)
        args = parser.parse_args(["captions", "--job", "job1"])
        self.assertEqual(args.job, "job1")
        self.assertEqual(args.preset, "house")


def _run(cmd: list[str]) -> None:
    done = subprocess.run(cmd, capture_output=True, text=True)
    assert done.returncode == 0, f"{cmd}\n{done.stderr}"


@unittest.skipUnless(FFMPEG and PIL_OK, "needs ffmpeg, ffprobe and Pillow")
class BurnTests(unittest.TestCase):
    def test_burn_captions_and_title_onto_lavfi(self) -> None:
        from ghostcut.ffmpeg_tools import extract_media_stats, probe_media
        from PIL import Image, ImageChops, ImageStat

        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "job"
            job.mkdir()
            _words_file(job / "words.json", _job_words())
            _angles_file(job / "angles.json", [(0.0, 3.0), (3.0, 6.0)])
            _run(["ffmpeg", "-v", "error", "-y",
                  "-f", "lavfi", "-i",
                  "color=c=white:size=1080x1920:rate=30:duration=6",
                  "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                  "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                  "-shortest", str(job / "vertical.mp4")])
            payload = captions_mod.run(
                words_path=job / "words.json",
                angles_path=job / "angles.json", tighten_path=None,
                out_dir=job, title="We cut the interview today")
            self.assertEqual(payload["burned"], ["vertical.mp4"])
            self.assertTrue((job / "vertical.clean.mp4").exists())
            clean = extract_media_stats(probe_media(
                job / "vertical.clean.mp4"))
            burnt = extract_media_stats(probe_media(job / "vertical.mp4"))
            self.assertAlmostEqual(float(burnt["duration_s"]),
                                   float(clean["duration_s"]), delta=0.15)

            def _frame(video: Path, at_s: float) -> Image.Image:
                out = Path(tmp) / f"f-{at_s}.png"
                _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{at_s:.2f}",
                      "-i", str(video), "-frames:v", "1", str(out)])
                return Image.open(out).convert("L")

            def _band_diff(a: Image.Image, b: Image.Image,
                           box: tuple[int, int, int, int]) -> float:
                diff = ImageChops.difference(a.crop(box), b.crop(box))
                return ImageStat.Stat(diff).mean[0]

            captioned = _frame(job / "vertical.mp4", 1.0)
            plain = _frame(job / "vertical.clean.mp4", 1.0)
            caption_box = (0, 1000, 1080, 1250)
            self.assertGreater(_band_diff(captioned, plain, caption_box),
                               2.0)
            halo = captioned.crop(caption_box)
            self.assertLess(halo.getextrema()[0], 100)
            title_box = (0, 200, 1080, 450)
            self.assertGreater(_band_diff(captioned, plain, title_box), 2.0)
            late = _frame(job / "vertical.mp4", 5.0)
            late_plain = _frame(job / "vertical.clean.mp4", 5.0)
            self.assertLess(_band_diff(late, late_plain, title_box), 2.0)
            safe_mid = _band_diff(captioned, plain, (0, 850, 1080, 950))
            self.assertLess(safe_mid, 2.0)


if __name__ == "__main__":
    unittest.main()
