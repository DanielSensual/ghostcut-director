"""Cut jobs in the app: mapping, voice endpoints, re-render (VOICE §11 items 17-24).

Never calls the Meta API, Whisper or a microphone: voice resolution is
faked at the voice module boundary, and the one full-loop test renders
synthetic lavfi clips with the rule fallback.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import cutjob
import engine as engine_mod
from director import angles as angles_mod
from director import tighten as tighten_mod
from director import voice as voice_mod

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


# ------------------------------------------------- fixtures


def _words() -> list[dict]:
    return [{"word": w, "start": s, "end": e} for w, s, e in [
        ("Alpha", 0.5, 0.9), ("beta.", 1.0, 1.4),
        ("Alpha", 10.5, 10.9), ("beta", 11.0, 11.4),
        ("green", 11.5, 11.7), ("lagoon.", 11.7, 12.0),
        ("Delta", 20.5, 20.9), ("epsilon", 21.0, 21.4),
        ("green", 21.5, 21.7), ("lagoon.", 21.7, 22.0)]]


def _sync(a_path: str = "a.mp4", b_path: str = "b.mp4") -> dict:
    def _clip(label: str, path: str) -> dict:
        return {"label": label, "offset_s": 0.0,
                "camera": {"path": path, "fps": 30, "width": 1920,
                           "height": 1080, "duration_s": 30,
                           "make": "Sony", "model": "FX3"}}

    return {"reference": a_path,
            "clips": [_clip("A", a_path), _clip("B", b_path)]}


def _tighten() -> dict:
    return {
        "version": 1, "method": "rule", "model": "muse-spark-1.3",
        "pause_limit_s": 0.8, "spend_usd": 0.0, "warnings": [],
        "duration_in_s": 22.1, "duration_out_s": 3.4,
        "utterances": [
            {"id": 0, "start_s": 0.5, "end_s": 1.4,
             "text": "Alpha beta.", "word_start": 0, "word_end": 2},
            {"id": 1, "start_s": 10.5, "end_s": 12.0,
             "text": "Alpha beta green lagoon.", "word_start": 2,
             "word_end": 6},
            {"id": 2, "start_s": 20.5, "end_s": 22.0,
             "text": "Delta epsilon green lagoon.", "word_start": 6,
             "word_end": 10}],
        "beats": [
            {"id": 0, "topic": "the opener", "status": "kept",
             "keep_priority": 5, "pick_attempt": 1, "reason": "clean pass",
             "attempts": [
                 {"id": 0, "utterance_ids": [0], "start_s": 0.5,
                  "end_s": 1.4, "text": "Alpha beta.",
                  "verdict": "alternate", "kind": "alternate",
                  "reason": "flat", "confidence": 0.8},
                 {"id": 1, "utterance_ids": [1], "start_s": 10.5,
                  "end_s": 12.0, "text": "Alpha beta green lagoon.",
                  "verdict": "pick", "kind": "keeper", "reason": "clean",
                  "confidence": 0.9}]},
            {"id": 1, "topic": "the demo", "status": "kept",
             "keep_priority": 5, "pick_attempt": 0, "reason": "only pass",
             "attempts": [
                 {"id": 0, "utterance_ids": [2], "start_s": 20.5,
                  "end_s": 22.0, "text": "Delta epsilon green lagoon.",
                  "verdict": "pick", "kind": "keeper", "reason": "only",
                  "confidence": 0.9}]}],
        "kept_spans": [
            {"id": "K0", "start_s": 10.4, "end_s": 12.1, "beat_id": 0,
             "attempt_id": 1, "utterance_ids": [1], "kind": "keeper",
             "reason": "clean"},
            {"id": "K1", "start_s": 20.4, "end_s": 22.1, "beat_id": 1,
             "attempt_id": 0, "utterance_ids": [2], "kind": "keeper",
             "reason": "only"}],
        "removed_spans": [], "angle_overrides": [], "edits_applied": [],
        "span_exclusions": [],
        "editor_notes": [{"at_s": 11.0, "text": "trim the head?",
                          "source": "voice"}]}


def _angles() -> dict:
    return {
        "version": 1, "sync": "sync.json", "words": "words.json",
        "reference": "a.mp4", "grade": None,
        "audio": {"label": "A", "clip_path": "a.mp4", "offset_s": 0.0},
        "method": "rule", "model": "muse-spark-1.3", "min_shot_s": 2.0,
        "spend_usd": 0.01, "warnings": [],
        "lines": [
            {"id": 0, "start_s": 10.5, "end_s": 12.0,
             "text": "Alpha beta green lagoon.", "cameras": ["A", "B"]},
            {"id": 1, "start_s": 20.5, "end_s": 22.0,
             "text": "Delta epsilon green lagoon.", "cameras": ["A", "B"]}],
        "shots": [
            {"id": 0, "start_s": 0.0, "end_s": 1.7, "camera": "A",
             "clip_path": "a.mp4", "clip_in_s": 10.4, "clip_out_s": 12.1,
             "line_ids": [0], "shot_size": "medium", "reason": "rule",
             "method": "rule", "overrules": [], "lut": None,
             "beat_id": 0},
            {"id": 1, "start_s": 1.7, "end_s": 3.4, "camera": "B",
             "clip_path": "b.mp4", "clip_in_s": 20.4, "clip_out_s": 22.1,
             "line_ids": [1], "shot_size": "close", "reason": "rule",
             "method": "rule", "overrules": [], "lut": None,
             "beat_id": 1}]}


def _cut(job: Path) -> dict:
    return {"version": 1, "status": "ok", "failed_stage": None,
            "out_dir": str(job), "model": "muse-spark-1.3",
            "max_usd": 5.0, "spend_usd": 0.06, "warnings": [],
            "inputs": ["a.mp4", "b.mp4"],
            "stages": {"sync": {"spend_usd": 0.01},
                       "tighten": {"spend_usd": 0.02},
                       "grade": {"spend_usd": 0.0},
                       "angles": {"spend_usd": 0.03}}}


def _write_job(root: Path, *, tighten: bool = True,
               voice: bool = False) -> Path:
    job = root / "job"
    job.mkdir(parents=True, exist_ok=True)
    (job / "words.json").write_text(json.dumps({"words": _words()}))
    (job / "sync.json").write_text(json.dumps(_sync()))
    (job / "angles.json").write_text(json.dumps(_angles()))
    if tighten:
        (job / "tighten.json").write_text(json.dumps(_tighten()))
    (job / "cut.json").write_text(json.dumps(_cut(job)))
    if voice:
        (job / "voice.json").write_text(json.dumps({
            "version": 1, "notes": [
                {"id": "n1", "status": "unresolved",
                 "raw_text": "hmm", "heard": "hmm",
                 "message": "I couldn't place that note in the cut",
                 "at_s": 1.0, "ops": []},
                {"id": "n2", "status": "resolved", "raw_text": "swap",
                 "heard": "swap", "message": "", "at_s": 0.5,
                 "ops": []}]}))
    return job


def _tiny_wav_b64() -> str:
    import io as _io

    buf = _io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _swap_payload_for(job: Path) -> dict:
    """What voice.run returns for 'use the other take here' at 0.5 s —
    computed through the real verifier, not hand-checked."""
    context = voice_mod.build_job_context(job)
    [note] = voice_mod.verify_resolved(context, [{
        "note_index": 1, "heard": "swap to the other take",
        "confidence": 0.9, "anchor_kind": "playhead", "at_s": 0.5,
        "quote": "", "beat_id": 0, "status": "resolved", "message": "",
        "ops": [{"op": "swap_take", "beat_id": 0, "attempt_id": 0,
                 "span_id": "", "camera": "", "pause_limit_s": -1,
                 "target_s": -1, "text": "",
                 "why": "the note says so"}],
        "taste": [], "raw_text": "use the other take here",
        "playhead_at": 0.5}], source="live")
    note.engine = "meta"
    note.usd = 0.01
    return {"version": 1, "note_file": "rec.webm", "engine": "meta",
            "duration_s": 2.0, "spend_usd": 0.01,
            "notes": [note.as_dict()], "applied": [],
            "turns": [{"start_s": 0.5, "end_s": 1.5,
                       "text": "use the other take here",
                       "speaker": "S1"}],
            "warnings": []}


class Ctx:
    """The engine with a throwaway demo dir, through the real HTTP
    dispatch (do_GET/do_POST/_call/_post) with no TCP socket."""

    def __init__(self, test: unittest.TestCase):
        self._tmp = tempfile.TemporaryDirectory(prefix="cutjob-tests-")
        test.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.demo = self.root / "demo"
        self.demo.mkdir()
        self.engine = engine_mod.Engine(self.demo, render=None)
        self.handler = engine_mod.Handler.__new__(engine_mod.Handler)
        self.handler.engine = self.engine
        self.handler.server = SimpleNamespace(  # type: ignore[attr-defined]
            server_address=("127.0.0.1", 7791))
        self.answered: list[tuple[int, dict]] = []
        self.handler._json = (  # type: ignore[method-assign]
            lambda obj, code=200: self.answered.append((code, obj)))

    def post(self, path: str, body: dict):
        import io as _io

        data = json.dumps(body).encode()
        self.handler.path = path
        self.handler.headers = {"Content-Length": str(len(data)),
                                "Host": "127.0.0.1:7791",
                                "Content-Type": "application/json"}
        self.handler.rfile = _io.BytesIO(data)
        self.handler.do_POST()
        return self.answered[-1]

    def get(self, path: str):
        self.handler.path = path
        self.handler.do_GET()
        return self.answered[-1]


class MappingTests(unittest.TestCase):
    """Item 17: the job's JSON becomes the project the app renders."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cutjob-map-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.job = _write_job(self.root, voice=True)

    def test_sections_talk_takes_transcript_findings_spend(self) -> None:
        out = self.root / "demo"
        project = cutjob.build_project(self.job, out)
        self.assertEqual(
            [(s["name"], s["t"], s["d"]) for s in project["sections"]],
            [("the opener", 0.0, 1.7), ("the demo", 1.7, 1.7)])
        self.assertEqual(
            [(c["id"], c["t"], c["src"], c["topic"], c["section"],
              c["ref"]) for c in project["talk"]],
            [("t1", 0.0, "camera A", "the opener", "the opener", [0]),
             ("t2", 1.7, "camera B", "the demo", "the demo", [1])])
        takes = {(t["group"], t["aid"]): t for t in project["takes"]}
        self.assertEqual(takes[("B0", 0)]["kind"], "alternate")
        self.assertFalse(takes[("B0", 0)]["pick"])
        self.assertEqual(takes[("B0", 1)]["kind"], "keeper_take")
        self.assertTrue(takes[("B0", 1)]["pick"])
        # Words in removed material never reach the Words tab.
        text = " ".join(s["text"] for s in project["transcript"])
        self.assertIn("green lagoon", text)
        self.assertEqual(len(project["transcript"]), 2)
        first = project["transcript"][0]
        self.assertEqual(first["t"], 0.1)
        self.assertTrue(all(w[0] != "" for w in first["w"]))
        checks = {(f["title"], f["check"]) for f in project["findings"]}
        self.assertIn(("trim the head?", "Voice note"), checks)
        self.assertIn(("hmm", "Voice note"), checks)
        self.assertEqual(len(project["findings"]), 2)
        self.assertEqual(project["spend"]["total"], 0.06)
        self.assertEqual(
            [row[0] for row in project["spend"]["rows"]],
            ["sync", "tighten", "grade", "angles"])
        self.assertEqual(project["cutJob"],
                         {"dir": str(self.job), "tightened": True})
        self.assertEqual(project["title"], "Director cut")
        self.assertTrue((out / "project.json").exists())
        self.assertTrue((out / "project.js").exists())

    def test_missing_render_builds_without_media(self) -> None:
        out = self.root / "demo"
        project = cutjob.build_project(self.job, out)
        self.assertEqual(project["duration"], 3.4)
        self.assertFalse((out / "video.mp4").exists())
        self.assertFalse((out / "poster.jpg").exists())

    @unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
    def test_render_present_is_linked_with_stills(self) -> None:
        done = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
             "testsrc2=size=320x180:rate=30:duration=5", "-c:v",
             "libx264", "-pix_fmt", "yuv420p",
             str(self.job / "cut.mp4")],
            capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, msg=done.stderr)
        out = self.root / "demo"
        cutjob.build_project(self.job, out)
        self.assertTrue((out / "video.mp4").exists())
        self.assertTrue((out / "poster.jpg").exists())
        self.assertTrue((out / "thumbs" / "0001.jpg").exists())


class UntightenedTests(unittest.TestCase):
    """Item 18: untightened jobs open with one section, no takes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cutjob-open-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_single_cut_section_and_empty_takes(self) -> None:
        job = _write_job(self.root, tighten=False)
        project = cutjob.build_project(job, self.root / "demo")
        self.assertEqual(project["sections"],
                         [{"name": "Cut", "t": 0.0, "d": 3.4}])
        self.assertEqual(project["takes"], [])
        self.assertEqual(project["takeMeta"], {})
        self.assertFalse(project["cutJob"]["tightened"])
        self.assertEqual(len(project["talk"]), 2)

    def test_bad_directory_raises_the_plain_message(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(cutjob.CutJobError) as caught:
            cutjob.build_project(empty, self.root / "demo")
        self.assertEqual(
            str(caught.exception),
            f"Not a Director cut job: {empty} has no angles.json "
            f"(run director cut there first)")


class OpenTests(unittest.TestCase):
    """Item 19: POST /api/job/open returns the project, mic-visible."""

    def test_open_returns_the_project_payload(self) -> None:
        ctx = Ctx(self)
        job = _write_job(ctx.root)
        status, body = ctx.post("/api/job/open", {"path": str(job)})
        self.assertEqual(status, 200)
        project = body["project"]
        self.assertEqual(project["title"], "Director cut")
        self.assertEqual(project["cutJob"]["tightened"], True)
        self.assertEqual(project["cutJob"]["editsApplied"], 0)
        self.assertFalse(project["cutJob"]["renderDirty"])
        self.assertEqual(len(project["talk"]), 2)
        self.assertTrue((ctx.demo / "project.json").exists())

    def test_open_bad_directory_is_a_400(self) -> None:
        ctx = Ctx(self)
        empty = ctx.root / "empty"
        empty.mkdir()
        status, body = ctx.post("/api/job/open", {"path": str(empty)})
        self.assertEqual(status, 400)
        self.assertIn("Not a Director cut job", body["error"])


class RecordApplyTests(unittest.TestCase):
    """Item 20: record -> stored proposal -> Accept-all swaps the take."""

    def test_record_then_apply_swaps_the_pick(self) -> None:
        ctx = Ctx(self)
        job = _write_job(ctx.root)
        status, _ = ctx.post("/api/job/open", {"path": str(job)})
        self.assertEqual(status, 200)
        payload = _swap_payload_for(job)
        with mock.patch("director.voice.run",
                        return_value=payload) as ran:
            status, proposal = ctx.post("/api/voice/record", {
                "audio_b64": _tiny_wav_b64(), "mime": "audio/wav",
                "at_s": 0.5})
        self.assertEqual(status, 200)
        ran.assert_called_once()
        _args, kwargs = ran.call_args
        self.assertEqual(kwargs["anchors"], [0.5])
        self.assertEqual(proposal["engine"], "meta")
        (note,) = proposal["notes"]
        self.assertEqual(note["beat_id"], 0)
        (op,) = proposal["ops"]
        self.assertEqual(op["label"], "Swap B0 → A0")
        self.assertFalse(op["blocked"])
        self.assertEqual(op["note_id"], "n1")
        status, body = ctx.post("/api/voice/apply",
                                   {"id": proposal["id"]})
        self.assertEqual(status, 200)
        project = body["project"]
        takes = {(t["group"], t["aid"]): t
                 for t in project["takes"]}
        self.assertTrue(takes[("B0", 0)]["pick"])
        self.assertEqual(takes[("B0", 0)]["kind"], "keeper_take")
        self.assertTrue(project["cutJob"]["renderDirty"])
        saved = json.loads((job / "tighten.json").read_text())
        beats = {b["id"]: b for b in saved["beats"]}
        self.assertEqual(beats[0]["pick_attempt"], 0)
        self.assertEqual(len(saved["edits_applied"]), 1)
        self.assertEqual(saved["edits_applied"][0]["source"], "voice")


class ConfirmTests(unittest.TestCase):
    """Item 21: the over-a-minute file gate, before any transcription."""

    def test_long_file_needs_confirm_then_transcribes(self) -> None:
        ctx = Ctx(self)
        job = _write_job(ctx.root)
        ctx.post("/api/job/open", {"path": str(job)})
        payload = _swap_payload_for(job)
        blob = base64.b64encode(b"fake-long-note").decode("ascii")
        with mock.patch("director.voice.probe_note_duration",
                        return_value=61.0), \
                mock.patch("director.voice.run",
                           return_value=payload) as ran:
            status, first = ctx.post("/api/voice/file",
                                        {"name": "memo.m4a",
                                         "audio_b64": blob})
            self.assertEqual(status, 200)
            self.assertTrue(first["needs_confirm"])
            self.assertGreater(first["estimate_usd"], 0.0)
            self.assertEqual(first["duration_s"], 61.0)
            ran.assert_not_called()
            status, second = ctx.post("/api/voice/file",
                                         {"name": "memo.m4a",
                                          "audio_b64": blob,
                                          "confirm": True})
            self.assertEqual(status, 200)
            self.assertIn("notes", second)
            ran.assert_called_once()


class TasteApiTests(unittest.TestCase):
    """Item 22: the taste endpoints round-trip."""

    def test_add_list_remove(self) -> None:
        ctx = Ctx(self)
        job = _write_job(ctx.root)
        ctx.post("/api/job/open", {"path": str(job)})
        status, body = ctx.post("/api/taste/add",
                                   {"text": "always open on the wide"})
        self.assertEqual(status, 200)
        self.assertEqual([e["id"] for e in body["entries"]], ["t1"])
        status, body = ctx.get("/api/taste")
        self.assertEqual(status, 200)
        self.assertEqual([e["text"] for e in body["entries"]],
                         ["always open on the wide"])
        status, body = ctx.post("/api/taste/remove", {"id": "t1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["entries"], [])
        status, body = ctx.post("/api/taste/remove", {"id": "t9"})
        self.assertEqual(status, 400)


class StaleTests(unittest.TestCase):
    """Item 23: a proposal from before another apply stays untouched."""

    def test_edited_tighten_blocks_with_the_stale_message(self) -> None:
        ctx = Ctx(self)
        job = _write_job(ctx.root)
        ctx.post("/api/job/open", {"path": str(job)})
        payload = _swap_payload_for(job)
        with mock.patch("director.voice.run", return_value=payload):
            status, proposal = ctx.post("/api/voice/record", {
                "audio_b64": _tiny_wav_b64(), "mime": "audio/wav",
                "at_s": 0.5})
        self.assertEqual(status, 200)
        saved = json.loads((job / "tighten.json").read_text())
        saved["beats"][0]["attempts"][0]["reason"] = "edited elsewhere"
        (job / "tighten.json").write_text(json.dumps(saved))
        status, body = ctx.post("/api/voice/apply",
                                   {"id": proposal["id"]})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], voice_mod.STALE_MESSAGE)
        saved = json.loads((job / "tighten.json").read_text())
        self.assertEqual(saved["beats"][0]["pick_attempt"], 1)
        self.assertEqual(saved["edits_applied"], [])


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class RerenderTests(unittest.TestCase):
    """Item 24: apply, re-render, and the new cut probes to the swap."""

    def _media(self, path: Path, duration: int = 16) -> None:
        done = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
             f"testsrc2=size=320x180:rate=30:duration={duration}", "-f",
             "lavfi", "-i", f"sine=frequency=440:duration={duration}",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
             "-shortest", str(path)],
            capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, msg=done.stderr)

    def test_apply_then_rerender_probes_to_the_swapped_take(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="cutjob-rerender-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        job = root / "job"
        job.mkdir()
        self._media(job / "a.mp4")
        self._media(job / "b.mp4")
        text = ("brave new cutting room " * 3).split()
        text[-1] += "."
        words = []
        for i, token in enumerate(text):
            start = 0.5 + i * 0.3
            words.append({"word": token, "start": round(start, 3),
                          "end": round(start + 0.2, 3)})
        short = text[:6]
        short[-1] += "."
        for i, token in enumerate(short):
            start = 10.5 + i * 0.25
            words.append({"word": token, "start": round(start, 3),
                          "end": round(start + 0.2, 3)})
        (job / "words.json").write_text(json.dumps({"words": words}))
        (job / "sync.json").write_text(json.dumps(
            _sync(str(job / "a.mp4"), str(job / "b.mp4"))))
        (job / "cut.json").write_text(json.dumps(_cut(job)))
        tightened = tighten_mod.run(
            sync_path=job / "sync.json", words_path=job / "words.json",
            out_path=job / "tighten.json", audio_label="A",
            pause_limit_s=0.8, target_s=None, model="muse-spark-1.3",
            no_muse=True, max_usd=5.0)
        beats = {b["id"]: b for b in tightened["beats"]}
        self.assertEqual(len(beats), 1)
        (only,) = beats.values()
        self.assertEqual(only["pick_attempt"], 1)
        self.assertEqual(only["attempts"][0]["verdict"], "alternate")
        angles_mod.run(sync_path=job / "sync.json",
                       words_path=job / "words.json",
                       out_path=job / "angles.json", audio_label="A",
                       render_path=job / "cut.mp4", min_shot_s=0.5,
                       model="muse-spark-1.3", no_muse=True,
                       tighten_path=job / "tighten.json")
        from ghostcut.ffmpeg_tools import (extract_media_stats,
                                           probe_media)
        first = extract_media_stats(probe_media(job / "cut.mp4"))
        self.assertAlmostEqual(float(first["duration_s"]),
                               float(tightened["duration_out_s"]),
                               delta=0.4)

        ctx = Ctx(self)
        status, _ = ctx.post("/api/job/open", {"path": str(job)})
        self.assertEqual(status, 200)
        payload = _swap_payload_for(job)
        with mock.patch("director.voice.run", return_value=payload):
            status, proposal = ctx.post("/api/voice/record", {
                "audio_b64": _tiny_wav_b64(), "mime": "audio/wav",
                "at_s": 0.5})
        self.assertEqual(status, 200)
        status, body = ctx.post("/api/voice/apply",
                                   {"id": proposal["id"]})
        self.assertEqual(status, 200)
        saved = json.loads((job / "tighten.json").read_text())
        self.assertEqual(saved["beats"][0]["pick_attempt"], 0)

        result = cutjob.rerender(job, ctx.demo)
        self.assertTrue(result["tightened"])
        second = extract_media_stats(probe_media(job / "cut.mp4"))
        expected = float(saved["duration_out_s"])
        self.assertAlmostEqual(float(second["duration_s"]), expected,
                               delta=0.5)
        self.assertGreater(abs(float(second["duration_s"])
                               - float(first["duration_s"])), 1.0)
        self.assertTrue((job / "cut.prev.mp4").exists())
        prev = extract_media_stats(probe_media(job / "cut.prev.mp4"))
        self.assertAlmostEqual(float(prev["duration_s"]),
                               float(first["duration_s"]), delta=0.2)
        rebuilt = json.loads((ctx.demo / "project.json").read_text())
        takes = {(t["group"], t["aid"]): t for t in rebuilt["takes"]}
        key = (f"B{only['id']}", 0)
        self.assertTrue(takes[key]["pick"])
        self.assertAlmostEqual(float(result["duration_s"]), expected,
                               delta=0.5)


if __name__ == "__main__":
    unittest.main()

