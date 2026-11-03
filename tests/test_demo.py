"""Items of docs/muse-code/DEMO-BRIEF.md: `director demo` tests.

Offline throughout: no network, no Meta API, no Whisper, no `say`. The
script/word-timing generator, the offset checks and the report are pure unit
tests; the one end-to-end test forces the tone-burst voice and the script
word timings and runs the real pipeline on synthetic media with ffmpeg.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from director import demo as demo_mod
from director.angles import TimedWord
from director.demo import (
    CAMERA_OFFSETS,
    FLUB_UTTERANCE,
    FRAME_S,
    LEAD_S,
    SCRIPT,
    SILENT_CAMERAS,
    check_files,
    check_offsets,
    fit_word_times,
    format_report,
    lav_duration_s,
    plan_word_times,
    restart_removed_s,
    script_text,
    slice_master,
    speech_span_s,
    synth_tone_speech,
    trim_silence,
)


def _timed(words) -> list[TimedWord]:
    return [TimedWord(w.text, w.start_s, w.end_s) for w in words]


class ScriptGeneratorTests(unittest.TestCase):
    def test_plan_starts_after_the_lead_and_stays_ordered(self) -> None:
        words = plan_word_times()
        self.assertGreater(len(words), 20)
        self.assertAlmostEqual(words[0].start_s, LEAD_S)
        for prev, word in zip(words, words[1:]):
            self.assertLess(prev.end_s, word.start_s)
            self.assertLess(word.start_s - prev.end_s, 2.0)

    def test_plan_is_about_25_to_40_seconds_of_speech(self) -> None:
        span = speech_span_s(plan_word_times())
        self.assertGreaterEqual(span, 25.0)
        self.assertLessEqual(span, 40.0)

    def test_plan_makes_eight_utterances_under_the_split_limit(self) -> None:
        from director import tighten as tighten_mod

        utterances = tighten_mod.build_utterances(_timed(plan_word_times()))
        self.assertEqual(len(utterances), 8)
        for utterance in utterances:
            self.assertLessEqual(
                utterance.end_s - utterance.start_s,
                tighten_mod.MAX_UTTERANCE_S)

    def test_flub_carries_no_terminal_period(self) -> None:
        flub = SCRIPT[FLUB_UTTERANCE]
        self.assertFalse(flub[-1].endswith((".", "!", "?")))
        for index, utterance in enumerate(SCRIPT):
            if index != FLUB_UTTERANCE:
                self.assertTrue(utterance[-1].endswith((".", "!", "?")))

    def test_rule_fallback_removes_exactly_the_restart(self) -> None:
        from director import tighten as tighten_mod

        utterances = tighten_mod.build_utterances(_timed(plan_word_times()))
        beats = tighten_mod.rule_beats(utterances)
        removed = [attempt
                   for beat in beats for attempt in beat.attempts
                   if attempt.verdict == "remove"]
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0].kind, "flub")
        self.assertGreater(removed[0].end_s - removed[0].start_s, 4.0)
        # every other beat keeps a single complete pass
        for beat in beats:
            if beat.id == 2:
                continue
            self.assertEqual(len(beat.attempts), 1)
            self.assertEqual(beat.attempts[0].verdict, "pick")

    def test_say_text_reads_naturally(self) -> None:
        text = script_text()
        self.assertIn("do that again.", text)
        self.assertIn("sorry,", text)

    def test_fit_scales_the_grid_onto_the_voice(self) -> None:
        planned = plan_word_times()
        fitted = fit_word_times(planned, 20.0)
        self.assertEqual(len(fitted), len(planned))
        self.assertAlmostEqual(fitted[0].start_s, LEAD_S)
        self.assertAlmostEqual(fitted[-1].end_s, LEAD_S + 20.0)
        for prev, word in zip(fitted, fitted[1:]):
            self.assertLess(prev.end_s, word.start_s)

    def test_fit_rejects_an_empty_voice(self) -> None:
        with self.assertRaises(demo_mod.DemoError):
            fit_word_times(plan_word_times(), 0.0)

    def test_tone_bursts_sit_on_the_words_and_nowhere_else(self) -> None:
        from director.demo import SAMPLE_RATE

        words = plan_word_times()
        pcm = synth_tone_speech(words)
        self.assertEqual(len(pcm), round(lav_duration_s(words) * SAMPLE_RATE))
        # silence through the lead-in
        self.assertTrue(all(sample == 0.0
                            for sample in pcm[:int(LEAD_S * SAMPLE_RATE)]))
        # each word's middle sounds, each inter-utterance pause is silent
        for word in words:
            mid = pcm[int((word.start_s + word.end_s) / 2 * SAMPLE_RATE)]
            self.assertNotEqual(mid, 0.0)
        gap = pcm[int((words[5].end_s + 0.5) * SAMPLE_RATE)]
        self.assertEqual(gap, 0.0)
        # deterministic
        self.assertEqual(pcm, synth_tone_speech(words))

    def test_broken_voice_falls_through_to_tone(self) -> None:
        from unittest import mock

        planned = plan_word_times()
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                with mock.patch.object(
                        demo_mod, "_run_or_demo_error", return_value=None):
                    with mock.patch.object(
                            demo_mod, "decode_pcm_mono", return_value=[]):
                        words, speech, used = demo_mod.render_speech(
                            planned, Path(tmp), first_choice="say")
        self.assertEqual(used, "tone")
        self.assertIs(words, planned)
        self.assertEqual(len(speech),
                         round(lav_duration_s(planned)
                               * demo_mod.SAMPLE_RATE))

    def test_failing_voice_falls_through_to_tone(self) -> None:
        from unittest import mock

        planned = plan_word_times()
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                with mock.patch.object(
                        demo_mod, "_run_or_demo_error",
                        side_effect=demo_mod.DemoError("no say")):
                    _words, _speech, used = demo_mod.render_speech(
                        planned, Path(tmp), first_choice="say")
        self.assertEqual(used, "tone")

    def test_working_voice_fits_the_grid(self) -> None:
        from unittest import mock

        from director.demo import SAMPLE_RATE

        planned = plan_word_times()
        fake = [0.5] * (20 * SAMPLE_RATE)
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                with mock.patch.object(
                        demo_mod, "_run_or_demo_error", return_value=None):
                    with mock.patch.object(
                            demo_mod, "decode_pcm_mono", return_value=fake):
                        words, speech, used = demo_mod.render_speech(
                            planned, Path(tmp), first_choice="say")
        self.assertEqual(used, "say")
        self.assertEqual(speech, fake)
        self.assertAlmostEqual(words[-1].end_s, LEAD_S + 20.0, places=1)

    def test_effective_backend_probes_before_committing(self) -> None:
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                with mock.patch.object(
                        demo_mod, "_run_or_demo_error", return_value=None):
                    with mock.patch.object(
                            demo_mod, "decode_pcm_mono", return_value=[]):
                        self.assertEqual(
                            demo_mod.effective_voice_backend(
                                "say", Path(tmp)), "tone")
                    with mock.patch.object(
                            demo_mod, "decode_pcm_mono",
                            return_value=[0.5] * demo_mod.SAMPLE_RATE):
                        self.assertEqual(
                            demo_mod.effective_voice_backend(
                                "say", Path(tmp)), "say")
            self.assertEqual(
                demo_mod.effective_voice_backend("tone", Path(tmp)), "tone")

    def test_trim_and_slice(self) -> None:
        from director.demo import SAMPLE_RATE

        pcm = [0.0] * SAMPLE_RATE + [0.5] * SAMPLE_RATE + [0.0] * SAMPLE_RATE
        trimmed = trim_silence(pcm)
        self.assertLess(len(trimmed), len(pcm))
        self.assertGreater(len(trimmed), SAMPLE_RATE)
        master = [1.0] * (5 * SAMPLE_RATE)
        cut = slice_master(master, 2.0, 0.5)
        self.assertEqual(len(cut), 3 * SAMPLE_RATE)
        self.assertAlmostEqual(cut[0], 0.5)


class OffsetCheckTests(unittest.TestCase):
    def _sync(self, offsets: dict[str, float | None],
              warnings: dict[str, list[str]] | None = None,
              methods: dict[str, str] | None = None) -> dict:
        clips = []
        for pos, name in enumerate([*CAMERA_OFFSETS, *SILENT_CAMERAS]):
            clips.append({
                "label": "BCD"[pos],
                "offset_s": offsets.get(name),
                "method": (methods or {}).get(name, "waveform"),
                "camera": {"path": f"/shoot/{name}"},
                "warnings": (warnings or {}).get(name, []),
            })
        return {"clips": clips}

    def test_exact_offsets_pass(self) -> None:
        sync = self._sync(
            {"cam-a.mp4": 1.0, "cam-b.mp4": 4.0, "cam-c.mp4": None},
            {"cam-c.mp4": ["Clip D has no audio and no light flash was found: "
                           "could not be placed on the timeline."]},
            {"cam-a.mp4": "waveform", "cam-b.mp4": "waveform"})
        checks = check_offsets(sync, {**CAMERA_OFFSETS, **SILENT_CAMERAS})
        self.assertEqual(len(checks), 3)
        self.assertTrue(all(check.passed for check in checks))

    def test_one_frame_is_a_pass_two_frames_is_a_fail(self) -> None:
        sync = self._sync({"cam-a.mp4": 1.0 + FRAME_S, "cam-b.mp4": 4.0,
                           "cam-c.mp4": None},
                          {"cam-c.mp4": ["could not be placed on the timeline"]})
        checks = check_offsets(sync, {**CAMERA_OFFSETS, **SILENT_CAMERAS})
        self.assertTrue(checks[0].passed)
        sync = self._sync({"cam-a.mp4": 1.0 + 2 * FRAME_S, "cam-b.mp4": 4.0,
                           "cam-c.mp4": None},
                          {"cam-c.mp4": ["could not be placed on the timeline"]})
        checks = check_offsets(sync, {**CAMERA_OFFSETS, **SILENT_CAMERAS})
        self.assertFalse(checks[0].passed)
        self.assertIn("off by", checks[0].note)

    def test_unplaced_camera_fails(self) -> None:
        sync = self._sync({"cam-a.mp4": None, "cam-b.mp4": 4.0,
                           "cam-c.mp4": None},
                          {"cam-c.mp4": ["could not be placed on the timeline"]})
        checks = check_offsets(sync, {**CAMERA_OFFSETS, **SILENT_CAMERAS})
        self.assertFalse(checks[0].passed)

    def test_silent_camera_must_be_reported_not_mis_synced(self) -> None:
        sync = self._sync({"cam-a.mp4": 1.0, "cam-b.mp4": 4.0,
                           "cam-c.mp4": None}, {"cam-c.mp4": []})
        (silent,) = [c for c in check_offsets(
            sync, {**CAMERA_OFFSETS, **SILENT_CAMERAS}) if c.name == "cam-c.mp4"]
        self.assertFalse(silent.passed)
        sync = self._sync({"cam-a.mp4": 1.0, "cam-b.mp4": 4.0,
                           "cam-c.mp4": 9.999}, {"cam-c.mp4": []},
                          {"cam-c.mp4": "clock"})
        (silent,) = [c for c in check_offsets(
            sync, {**CAMERA_OFFSETS, **SILENT_CAMERAS}) if c.name == "cam-c.mp4"]
        self.assertFalse(silent.passed)

    def test_restart_seconds_count_take_removal_only(self) -> None:
        payload = {"removed_spans": [
            {"kind": "flub", "start_s": 11.0, "end_s": 17.0,
             "reason": "rule: incomplete attempt"},
            {"kind": "pause_trim", "start_s": 20.0, "end_s": 20.5,
             "reason": "pause 1.00s shortened to 0.35s"},
        ]}
        attempt_s, total_s = restart_removed_s(payload)
        self.assertAlmostEqual(attempt_s, 6.0)
        self.assertAlmostEqual(total_s, 6.5)
        self.assertEqual(restart_removed_s({"removed_spans": []}), (0.0, 0.0))


class ReportTests(unittest.TestCase):
    def _report(self, passed: bool = True) -> str:
        from director.demo import OffsetCheck

        checks = [OffsetCheck("cam-a.mp4", "B", 1.0, 1.0, "waveform", True, ""),
                  OffsetCheck("cam-b.mp4", "C", 4.0, 4.0, "waveform", True, ""),
                  OffsetCheck("cam-c.mp4", "D", 2.0, None, None, True,
                              "silent camera, unplaced and clearly reported")]
        if not passed:
            checks[0] = OffsetCheck("cam-a.mp4", "B", 1.0, None, None,
                                    False, "not placed on the timeline")
        shoot = demo_mod.Shoot(
            dir=Path("demo/shoot"), lav=Path("demo/shoot/lav.wav"),
            cameras=[], silent=[], words_path=Path("demo/shoot/demo-words.json"),
            voice_backend="tone", known_offsets={}, lav_duration_s=43.0)
        text, ok = format_report(
            out_dir=Path("demo"), job_dir=Path("demo/job"), shoot=shoot,
            use_muse=False, words_note="script word timings",
            offset_checks=checks, attempt_s=6.2, total_removed_s=6.2,
            kept_s=37.4,
            file_checks=[("cut.mp4", True), ("vertical.mp4", True),
                         ("timeline.fcpxml", True), ("cut.json", True)],
            job_status="complete_with_warnings")
        self.assertEqual(ok, passed)
        return text

    def test_passing_report_names_offsets_files_and_next_steps(self) -> None:
        text = self._report(passed=True)
        self.assertIn("cam-a.mp4: found +1.000 s, known +1.000 s — PASS", text)
        self.assertIn("cam-c.mp4: silent camera", text)
        self.assertIn("6.2 s removed", text)
        self.assertIn("demo/job/cut.mp4", text)
        self.assertIn("demo/job/vertical.mp4", text)
        self.assertIn("demo/job/timeline.fcpxml", text)
        self.assertIn("demo/job/cut.json", text)
        self.assertIn("director cut your-folder", text)
        self.assertIn("director voice", text)
        self.assertIn("python app/main.py --job demo/job", text)
        self.assertTrue(text.rstrip().endswith("Demo PASS"))

    def test_failing_report_says_fail(self) -> None:
        text = self._report(passed=False)
        self.assertIn("FAIL", text)
        self.assertTrue(text.rstrip().endswith("Demo FAIL"))

    def test_check_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "job"
            job.mkdir()
            (job / "cut.mp4").write_bytes(b"data")
            (job / "cut.json").write_bytes(b"{}")
            found = dict(check_files(job))
        self.assertTrue(found["cut.mp4"])
        self.assertTrue(found["cut.json"])
        self.assertFalse(found["vertical.mp4"])


class CliWiringTests(unittest.TestCase):
    def test_demo_parser_defaults(self) -> None:
        from director import cli as cli_mod

        args = cli_mod.build_parser().parse_args(["demo"])
        self.assertEqual(args.func, cli_mod.command_demo)
        self.assertEqual(args.out_dir, "director-demo")
        self.assertFalse(args.muse)
        self.assertFalse(args.keep)

    def test_demo_parser_flags(self) -> None:
        from director import cli as cli_mod

        args = cli_mod.build_parser().parse_args(
            ["demo", "--out-dir", "/tmp/x", "--muse", "--keep"])
        self.assertEqual(args.out_dir, "/tmp/x")
        self.assertTrue(args.muse)
        self.assertTrue(args.keep)


@unittest.skipUnless(shutil.which("ffmpeg"), "demo needs ffmpeg on PATH")
class DemoEndToEndTests(unittest.TestCase):
    def test_demo_passes_with_the_tone_stand_in(self) -> None:
        with tempfile.TemporaryDirectory(prefix="director-demo-") as tmp:
            out_dir = Path(tmp) / "demo"
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                payload = demo_mod.run(
                    out_dir=out_dir, muse_requested=False, keep=False,
                    voice_backend="tone", allow_whisper=False)
            self.assertTrue(payload["passed"], payload["report"])
            self.assertEqual(payload["voice_backend"], "tone")
            self.assertEqual(payload["words_source"], "explicit")
            self.assertGreater(payload["restart_removed_s"], 4.0)
            self.assertTrue(payload["removal_gated"])
            for check in payload["offsets"]:
                self.assertTrue(check["passed"], check)
            for name in ("cut.mp4", "vertical.mp4", "timeline.fcpxml",
                         "timeline.otio.json", "cut.json"):
                self.assertTrue(payload["files"][name], name)
                self.assertTrue((out_dir / "job" / name).exists(), name)
            stored = json.loads((out_dir / "demo.json").read_text(
                encoding="utf-8"))
            self.assertEqual(stored["passed"], True)
            self.assertIn("Demo PASS", payload["report"])
