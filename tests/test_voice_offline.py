"""VOICE-OFFLINE: voice notes keep working when Muse is unavailable.

Built by Claude Code (Opus) after the Meta API credit ran out (HTTP 402
"billing_not_configured"). Never calls the Meta API or Whisper: the
guestlist.meta stand-ins from test_voice raise the 402 here, and the
local Whisper call is stubbed at `_whisper_turns`.

The fixture is shaped like the hackathon job (founder talk, a Sony on
camera B, a DJI drone on camera C) with words taken from its transcript.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from director import voice as voice_mod
from test_voice import _fake_meta, _no_guestlist, _ok_run

APP = Path(__file__).resolve().parents[1] / "app"

BEATS = [
    "My mic my drone has no microphone but this software lined it up "
    "here.",
    "It can also add B-roll, roll, captions and all the things that you "
    "need.",
    "Get them mixed in get captions in get B roll in and ready for edit.",
]
BILLING = ('transcribe HTTP 402: {"error":{"code":"billing_not_configured",'
           '"message":"Billing is not configured for this account"}}')


class _Http402(Exception):
    """What the OpenAI SDK raises for a 402: an error with status_code."""

    status_code = 402

    def __init__(self) -> None:
        super().__init__("Error code: 402 - {'error': "
                         "'billing_not_configured'}")


def _write_offline_job(root: Path) -> Path:
    job = root / "job"
    job.mkdir(parents=True, exist_ok=True)
    words: list[dict] = []
    utterances: list[dict] = []
    beats: list[dict] = []
    spans: list[dict] = []
    shots: list[dict] = []
    lines: list[dict] = []
    clock = 0.0
    for bid, sentence in enumerate(BEATS):
        start_index, start_s = len(words), round(clock, 3)
        for token in sentence.split():
            words.append({"word": token, "start": round(clock, 3),
                          "end": round(clock + 0.3, 3)})
            clock += 0.4
        end_s = round(clock, 3)
        utterances.append({"id": bid, "start_s": start_s,
                           "end_s": round(end_s - 0.1, 3), "text": sentence,
                           "word_start": start_index,
                           "word_end": len(words)})
        beats.append({
            "id": bid, "topic": f"beat {bid}", "status": "kept",
            "keep_priority": 5, "pick_attempt": 0, "reason": "only pass",
            "attempts": [{"id": 0, "utterance_ids": [bid],
                          "start_s": start_s,
                          "end_s": round(end_s - 0.1, 3),
                          "text": sentence, "verdict": "pick",
                          "kind": "keeper", "reason": "only",
                          "confidence": 0.9}]})
        spans.append({"id": f"K{bid}", "start_s": start_s,
                      "end_s": end_s, "beat_id": bid, "attempt_id": 0,
                      "utterance_ids": [bid], "kind": "keeper",
                      "reason": "only"})
        camera = "C" if bid == 0 else "B"
        shots.append({"id": bid, "start_s": start_s, "end_s": end_s,
                      "camera": camera, "clip_path": f"{camera}.mp4",
                      "clip_in_s": start_s, "clip_out_s": end_s,
                      "line_ids": [bid], "shot_size": "medium",
                      "reason": "rule", "method": "rule", "overrules": [],
                      "lut": None, "beat_id": bid})
        lines.append({"id": bid, "start_s": start_s, "end_s": end_s,
                      "text": sentence, "cameras": ["B", "C"]})
    total = clock

    def _clip(label: str, path: str) -> dict:
        return {"label": label, "offset_s": 0.0, "camera": {
            "path": path, "fps": 30, "width": 1920, "height": 1080,
            "duration_s": total + 5, "make": "", "model": ""}}

    sync = {"reference": "00701_Wireless PRO.WAV", "clips": [
        _clip("A", "/cards/00701_Wireless PRO.WAV"),
        _clip("B", "/cards/C2942_take1.MP4"),
        _clip("C", "/cards/DJI_0355_take1.MOV")]}
    tighten = {
        "version": 1, "method": "rule", "model": "muse-spark-1.3",
        "pause_limit_s": 0.8, "spend_usd": 0.0, "warnings": [],
        "duration_in_s": total, "duration_out_s": total,
        "utterances": utterances, "beats": beats, "kept_spans": spans,
        "removed_spans": [], "angle_overrides": [], "edits_applied": [],
        "span_exclusions": [], "editor_notes": []}
    angles = {
        "version": 1, "sync": "sync.json", "words": "words.json",
        "reference": "a.wav", "grade": None,
        "audio": {"label": "A", "clip_path": "a.wav", "offset_s": 0.0},
        "method": "rule", "model": "muse-spark-1.3", "min_shot_s": 2.0,
        "spend_usd": 0.0, "warnings": [], "lines": lines, "shots": shots}
    (job / "words.json").write_text(json.dumps({"words": words}))
    (job / "sync.json").write_text(json.dumps(sync))
    (job / "tighten.json").write_text(json.dumps(tighten))
    (job / "angles.json").write_text(json.dumps(angles))
    (job / "cut.json").write_text(json.dumps(
        {"version": 1, "status": "ok", "out_dir": str(job)}))
    return job


class OfflineFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="voice-offline-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.job = _write_offline_job(self.root)

    def context(self) -> voice_mod.JobContext:
        return voice_mod.build_job_context(self.job)

    def typed(self, text: str, at_s: float | None = None
              ) -> list[voice_mod.ResolvedNote]:
        return voice_mod.resolve_typed(self.context(), text, at_s)

    def drops(self, note: voice_mod.ResolvedNote) -> list[str]:
        return [op["text"] for op in note.ops
                if op["op"] == "drop_words" and not op["blocked"]]


class ReasonTests(unittest.TestCase):
    """Which failures mean "Muse is unavailable" (fall back) vs a bug."""

    def test_billing_auth_network_count_and_bugs_do_not(self) -> None:
        reason = voice_mod.muse_unavailable_reason
        self.assertEqual(reason(RuntimeError(BILLING)), "402")
        self.assertEqual(reason(_Http402()), "402")
        self.assertEqual(reason(RuntimeError("transcribe HTTP 503: x")),
                         "HTTP 503")
        self.assertEqual(reason(ConnectionError("refused")), "network")
        self.assertEqual(reason(ImportError("no guestlist")),
                         "no Meta client")
        self.assertIsNone(reason(ValueError("Expecting value: line 1")))
        self.assertIsNone(reason(KeyError("notes")))


class TranscribeFallThroughTests(unittest.TestCase):
    """A 402 from Muse Voice Transcribe goes straight to local Whisper."""

    def test_402_falls_to_whisper_once_and_says_why(self) -> None:
        heard = [voice_mod.Turn(0.0, 2.0, "cut where I say get B roll in")]
        with _fake_meta() as fakes, \
                mock.patch.object(fakes.transcribe, "diarize",
                                  side_effect=RuntimeError(BILLING)) as call, \
                mock.patch("director.voice._whisper_turns",
                           return_value=heard):
            warnings: list[str] = []
            turns, engine, spend = voice_mod.transcribe_wav(
                Path("note.wav"), duration_s=3.0, warnings=warnings)
        self.assertEqual(call.call_count, 1)  # no retries
        self.assertEqual(engine, "whisper")
        self.assertEqual(spend, 0.0)
        self.assertEqual(turns, heard)
        self.assertEqual(warnings,
                         ["Muse unavailable (402), heard with local Whisper"])

    def test_without_a_warnings_list_the_signature_is_unchanged(self) -> None:
        heard = [voice_mod.Turn(0.0, 2.0, "remove captions")]
        with _fake_meta() as fakes, \
                mock.patch.object(fakes.transcribe, "diarize",
                                  side_effect=RuntimeError(BILLING)), \
                mock.patch("director.voice._whisper_turns",
                           return_value=heard):
            out = voice_mod.transcribe_wav(Path("note.wav"), duration_s=3.0)
        self.assertEqual(out, (heard, "whisper", 0.0))


class PhrasingTests(OfflineFixture):
    """The owner's demo phrasings, resolved by the offline rules."""

    def test_cut_where_i_say_with_a_colon_and_a_stutter(self) -> None:
        (note,) = self.typed("cut where I say: it can also add B-roll, roll")
        self.assertEqual(note.status, "resolved")
        self.assertEqual(self.drops(note), ["It can also add B-roll, roll,"])
        self.assertEqual(note.beat_id, 1)

    def test_cut_where_i_say_get_b_roll_in(self) -> None:
        (note,) = self.typed("cut where I say get B roll in")
        self.assertEqual(self.drops(note), ["get B roll in"])
        self.assertEqual(note.beat_id, 2)

    def test_cut_both_places_takes_every_match(self) -> None:
        (note,) = self.typed("cut both places where I mention B-roll")
        self.assertEqual(note.status, "resolved")
        self.assertEqual(self.drops(note), ["B-roll,", "B roll"])

    def test_one_mention_of_two_asks_instead_of_guessing(self) -> None:
        (note,) = self.typed("cut where I mention B-roll")
        self.assertEqual(note.status, "ambiguous")
        self.assertEqual(len(note.candidates), 2)

    def test_cut_the_part_where_i_say(self) -> None:
        (note,) = self.typed("Cut the part where I say lined it up here.")
        self.assertEqual(self.drops(note), ["lined it up here."])

    def test_remove_x(self) -> None:
        (note,) = self.typed("remove captions and all the things")
        self.assertEqual(self.drops(note), ["captions and all the things"])

    def test_misheard_edge_still_finds_the_longest_matching_run(self) -> None:
        (note,) = self.typed("cut where I say ready for edit please")
        self.assertEqual(self.drops(note), ["ready for edit."])
        (note,) = self.typed("remove captions and all the stuff")
        self.assertEqual(self.drops(note), ["captions and all the"])

    def test_typed_quotes(self) -> None:
        (note,) = self.typed('cut "ready for edit"')
        self.assertEqual(self.drops(note), ["ready for edit."])

    def test_very_start_use_the_drone_camera(self) -> None:
        (note,) = self.typed("at the very start use the drone camera")
        self.assertEqual(note.status, "resolved")
        self.assertEqual((note.anchor_kind, note.at_s, note.beat_id),
                         ("beat", 0.0, 0))
        (op,) = note.ops
        self.assertEqual((op["op"], op["camera"], op["beat_id"]),
                         ("set_angle", "C", 0))
        self.assertFalse(op["blocked"])

    def test_use_camera_b_here_takes_the_playhead(self) -> None:
        (note,) = self.typed("use camera B here", at_s=7.0)
        self.assertEqual((note.anchor_kind, note.beat_id),
                         ("playhead", 1))
        (op,) = note.ops
        self.assertEqual((op["op"], op["camera"]), ("set_angle", "B"))
        self.assertFalse(op["blocked"])

    def test_use_the_sony_at_the_end(self) -> None:
        (note,) = self.typed("use the Sony at the end")
        self.assertEqual(note.beat_id, 2)
        (op,) = note.ops
        self.assertEqual((op["op"], op["camera"]), ("set_angle", "B"))

    def test_b_roll_never_reads_as_camera_b(self) -> None:
        (note,) = self.typed("cut every mention of B roll")
        self.assertTrue(all(op["op"] == "drop_words" for op in note.ops))
        self.assertEqual(len(self.drops(note)), 2)

    def test_the_recorded_demo_note_splits_into_two_changes(self) -> None:
        angle, cut = self.typed(
            "At the very start, start, where I say my mic, my drone has "
            "no microphone. Use the drone camera and cut both places where "
            "I mention B roll.")
        self.assertEqual(angle.anchor_kind, "quote")
        self.assertEqual(angle.beat_id, 0)
        self.assertEqual([(op["op"], op["camera"]) for op in angle.ops],
                         [("set_angle", "C")])
        self.assertEqual(self.drops(cut), ["B-roll,", "B roll"])

    def test_pauses_and_preferences(self) -> None:
        (note,) = self.typed("tighten the pauses")
        self.assertEqual([op["op"] for op in note.ops], ["set_pause_limit"])
        self.assertEqual(note.ops[0]["pause_limit_s"], 0.5)
        (note,) = self.typed("always open on the drone")
        self.assertEqual(note.status, "preference")

    def test_unparseable_note_says_what_to_try(self) -> None:
        (note,) = self.typed("hmm, I'm not sure about this bit")
        self.assertEqual(note.status, "unresolved")
        self.assertEqual(note.ops, [])
        self.assertEqual(note.message, voice_mod.OFFLINE_HELP_MESSAGE)
        self.assertIn("cut where I say", note.message)

    def test_mix_note_keeps_the_mix_rule(self) -> None:
        (note,) = self.typed("make the music louder")
        self.assertEqual([op["op"] for op in note.ops], ["add_note"])
        self.assertTrue(note.message.startswith(voice_mod.MIX_MESSAGE))

    def test_reply_notes_have_the_schema_shape(self) -> None:
        groups = [[voice_mod.Turn(0, 1, "cut where I say get B roll in")],
                  [voice_mod.Turn(3, 4, "use camera B here")],
                  [voice_mod.Turn(6, 7, "what a day")]]
        reply = voice_mod.offline_reply_notes(self.context(), groups)
        item_schema = voice_mod.RESOLVE_SCHEMA["properties"]["notes"]["items"]
        op_schema = item_schema["properties"]["ops"]["items"]
        for note in reply:
            self.assertEqual(set(note), set(item_schema["required"]))
            for op in note["ops"]:
                self.assertEqual(set(op), set(op_schema["required"]))
                self.assertIn(op["op"],
                              op_schema["properties"]["op"]["enum"])
        self.assertEqual([n["note_index"] for n in reply], [1, 2, 3])


class ResolveFallThroughTests(OfflineFixture):
    """resolve_notes: Muse first; the rules only when it cannot answer."""

    def test_402_from_spark_resolves_offline(self) -> None:
        turns = [voice_mod.Turn(0.0, 2.0, "cut where I say get B roll in")]
        with _fake_meta() as fakes, \
                mock.patch.object(fakes.spark, "_structured",
                                  side_effect=_Http402()) as call:
            notes, spend, warnings = voice_mod.resolve_notes(
                turns=turns, job=self.context(), anchors=[None],
                model=voice_mod.RESOLVE_MODEL)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(spend, 0.0)
        (note,) = notes
        self.assertEqual(self.drops(note), ["get B roll in"])
        self.assertIn("Muse unavailable: 402", note.message)
        self.assertEqual(warnings, [voice_mod.OFFLINE_WARNING.format(
            reason="402")])

    def test_spark_working_is_still_primary(self) -> None:
        reply = {"notes": [{
            "note_index": 1, "heard": "cut it", "confidence": 0.9,
            "anchor_kind": "quote", "at_s": -1, "quote": "ready for edit",
            "beat_id": -1, "status": "resolved", "message": "from Muse",
            "ops": [{"op": "drop_words", "beat_id": -1, "attempt_id": -1,
                     "span_id": "", "camera": "", "pause_limit_s": -1,
                     "target_s": -1, "word_start": -1, "word_end": -1,
                     "text": "", "why": "muse"}],
            "taste": []}]}
        turns = [voice_mod.Turn(0.0, 2.0, "lose the ready for edit line")]
        with _fake_meta(reply=reply) as fakes:
            notes, spend, warnings = voice_mod.resolve_notes(
                turns=turns, job=self.context(), anchors=[None],
                model=voice_mod.RESOLVE_MODEL)
        self.assertEqual(len(fakes.spark.structured_calls), 1)
        self.assertEqual(notes[0].message, "from Muse")
        self.assertEqual(warnings, [])
        self.assertGreater(spend, 0.0)

    def test_other_spark_failures_still_raise(self) -> None:
        turns = [voice_mod.Turn(0.0, 2.0, "cut where I say get B roll in")]
        with _fake_meta() as fakes, \
                mock.patch.object(fakes.spark, "_structured",
                                  side_effect=ValueError("bad JSON")):
            with self.assertRaises(voice_mod.VoiceError):
                voice_mod.resolve_notes(
                    turns=turns, job=self.context(), anchors=[None],
                    model=voice_mod.RESOLVE_MODEL)

    def test_missing_guestlist_resolves_offline(self) -> None:
        turns = [voice_mod.Turn(0.0, 2.0, "use the Sony at the end")]
        with _no_guestlist():
            notes, _spend, warnings = voice_mod.resolve_notes(
                turns=turns, job=self.context(), anchors=[],
                model=voice_mod.RESOLVE_MODEL)
        (note,) = notes
        self.assertEqual(note.ops[0]["camera"], "B")
        self.assertIn("no Meta client", warnings[0])

    def test_visual_check_is_skipped_not_failed(self) -> None:
        (self.job / "cut.mp4").write_bytes(b"fake")
        note = voice_mod.ResolvedNote(
            id="n1", source="file",
            raw_text="when he points at the drone use camera C",
            heard="", anchor_kind="quote", at_s=1.0, beat_id=0,
            status="resolved", message="",
            ops=[{"op": "set_angle", "beat_id": 0, "camera": "C",
                  "blocked": False, "checks": [], "label": "Camera C"}])
        with _fake_meta() as fakes, \
                mock.patch.object(fakes.spark, "image_json",
                                  side_effect=_Http402()), \
                mock.patch("director.voice.run_command",
                           side_effect=_ok_run):
            spend, warnings = voice_mod.confirm_visual_notes(
                self.context(), [note])
        (op,) = note.ops
        self.assertEqual(op["op"], "set_angle")  # kept, not downgraded
        self.assertIn("Frame check skipped (Muse unavailable: 402)",
                      [c["text"] for c in op["checks"]])
        self.assertEqual(warnings,
                         ["Muse unavailable (402), frame check skipped"])
        self.assertEqual(spend, 0.0)


class RunOfflineTests(OfflineFixture):
    """`director voice` / the app end to end with Muse returning 402."""

    def test_run_hears_with_whisper_and_resolves_offline(self) -> None:
        note = self.root / "rec.webm"
        note.write_bytes(b"fake")
        heard = [voice_mod.Turn(0.0, 3.0,
                                "cut both places where I mention B-roll")]
        with _fake_meta() as fakes, \
                mock.patch.object(fakes.transcribe, "diarize",
                                  side_effect=RuntimeError(BILLING)), \
                mock.patch.object(fakes.spark, "_structured",
                                  side_effect=_Http402()), \
                mock.patch("director.voice._whisper_turns",
                           return_value=heard), \
                mock.patch("director.voice.probe_note_duration",
                           return_value=3.0), \
                mock.patch("director.voice.run_command",
                           side_effect=_ok_run):
            payload = voice_mod.run(note_path=note, job_dir=self.job,
                                    anchors=[5.0], source="live")
        self.assertNotIn("error", payload)
        self.assertEqual(payload["engine"], "whisper")
        self.assertEqual(payload["resolver"], "rules")
        self.assertEqual(payload["spend_usd"], 0.0)
        self.assertEqual(payload["warnings"][:2], [
            "Muse unavailable (402), heard with local Whisper",
            voice_mod.OFFLINE_WARNING.format(reason="402")])
        (only,) = payload["notes"]
        self.assertEqual(len([op for op in only["ops"]
                              if not op["blocked"]]), 2)

    def test_engine_card_says_muse_is_unavailable(self) -> None:
        if str(APP) not in sys.path:
            sys.path.insert(0, str(APP))
        import engine  # noqa: E402

        fake = SimpleNamespace(proposals={}, gen=1, _tighten_sha=lambda: "",
                               _dress_notes=lambda notes: notes)
        payload = {"engine": "whisper", "resolver": "rules",
                   "spend_usd": 0.0, "notes": [{"id": "n1", "ops": []}],
                   "warnings": [
                       "Muse unavailable (402), heard with local Whisper",
                       voice_mod.OFFLINE_WARNING.format(reason="402")]}
        out = engine.Engine._store_voice(fake, payload, "voice:live:x")
        self.assertNotIn("error", out)
        self.assertIn("Muse unavailable (402), heard with local Whisper",
                      out["reply"])
        self.assertEqual(out["model"], "offline rules (Muse unavailable)")
        self.assertEqual(out["warnings"], payload["warnings"])


if __name__ == "__main__":
    unittest.main()
