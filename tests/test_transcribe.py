"""Items 1-28 of docs/muse-code/TRANSCRIBE-DESIGN.md: `transcribe` tests.

Offline throughout: no network, no Meta API, no real Whisper. The Meta
seam is `transcribe.diarize_chunk` patched with `unittest.mock`; the
ffmpeg seam is `transcribe.run_command` patched the same way, except
item 28, which runs the real extraction on a synthetic lavfi file.
All example sentences are invented for the tests.
"""
from __future__ import annotations

import collections
import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ghostcut.config import write_json

from director import cli as cli_mod
from director import cut as cut_mod
from director import cutlist
from director import tighten as tighten_mod
from director import transcribe as transcribe_mod
from director.angles import TimedWord
from director.transcribe import (
    AlignStats,
    Chunk,
    MuseTurn,
    MuseUnavailable,
    RawTurn,
    TranscribeError,
    TranscribeResult,
)


def _w(text: str, start: float, end: float, speaker: str = "",
       heard_by: str = "whisper", disfluency: bool = False) -> TimedWord:
    return TimedWord(text, start, end, speaker, heard_by, disfluency)


def _turn(id: int, start: float, end: float, speaker: str, text: str,
          raw_speaker: str = "SPEAKER_00", chunk_id: int = 0) -> MuseTurn:
    return MuseTurn(id, start, end, speaker, raw_speaker, text, chunk_id)


def _raw(chunk_id: int, raw_speaker: str, start: float, end: float,
         text: str) -> RawTurn:
    return RawTurn(chunk_id, raw_speaker, start, end, text)


def _diarized(turns: list[tuple[int, int, str, str]],
              duration_ms: int = 60000) -> dict:
    return {
        "audioDurationMs": duration_ms,
        "sessionId": "test-session",
        "transcript": " ".join(text for _, _, text, _ in turns),
        "turns": [{"turnId": pos, "startMs": start, "endMs": end,
                   "transcript": text, "speaker": speaker}
                  for pos, (start, end, text, speaker) in enumerate(turns)],
    }


def _ffmpeg_side_effect(silence_stderr: str = ""):
    """run_command stand-in: touches output WAVs, fakes silencedetect."""
    def _run(command, **kwargs):
        if "silencedetect" in " ".join(command):
            return subprocess.CompletedProcess(command, 0, "", silence_stderr)
        Path(command[-1]).touch()
        return subprocess.CompletedProcess(command, 0, "", "")

    return _run


class NormalizeAndCommandsTests(unittest.TestCase):
    def test_normalize_token_and_tokenize(self) -> None:
        self.assertEqual(transcribe_mod.normalize_token("Timecode."),
                         "timecode")
        self.assertEqual(transcribe_mod.normalize_token("'quoted'"),
                         "quoted")
        self.assertEqual(transcribe_mod.normalize_token("don't"), "don't")
        self.assertEqual(transcribe_mod.normalize_token("..."), "")
        self.assertEqual(transcribe_mod.tokenize_turn("  pack  the\tkit\n"),
                         ["pack", "the", "kit"])

    def test_extract_and_chunk_commands(self) -> None:
        self.assertEqual(
            transcribe_mod.extract_wav_command(Path("in.mp4"),
                                               Path("out.wav")),
            ["ffmpeg", "-v", "error", "-y", "-i", "in.mp4",
             "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", "out.wav"])
        self.assertEqual(
            transcribe_mod.chunk_wav_command(Path("full.wav"), 10.5, 180.0,
                                             Path("chunk.wav")),
            ["ffmpeg", "-v", "error", "-y",
             "-ss", "10.500", "-i", "full.wav",
             "-t", "180.000",
             "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", "chunk.wav"])
        self.assertEqual(
            transcribe_mod.silence_detect_command(Path("full.wav")),
            ["ffmpeg", "-v", "info", "-i", "full.wav",
             "-af", "silencedetect=noise=-40dB:d=0.4",
             "-f", "null", "-"])

    def test_parse_silence_spans(self) -> None:
        stderr = (
            "frame= 100 fps=25 q=-0.0 size=N/A time=00:00:04.00\n"
            "[silencedetect @ 0x7f8a1b400000] silence_start: 1.500\n"
            "[silencedetect @ 0x7f8a1b400000] silence_end: 3.000 "
            "| silence_duration: 1.500\n"
            "[silencedetect @ 0x7f8a1b400000] silence_start: 5.000\n"
            "[silencedetect @ 0x7f8a1b400000] silence_end: 5.500 "
            "| silence_duration: 0.500\n"
            "[silencedetect @ 0x7f8a1b400000] silence_start: 9.000\n")
        self.assertEqual(
            transcribe_mod.parse_silence_spans(stderr, 10.0),
            [(1.5, 3.0), (5.0, 5.5), (9.0, 10.0)])
        self.assertEqual(transcribe_mod.parse_silence_spans("oops\nnope", 4.0),
                         [])


class ChunkPlanningTests(unittest.TestCase):
    def test_plan_chunks_worked_example(self) -> None:
        chunks = transcribe_mod.plan_chunks(
            612.400,
            [(178.2, 179.1), (355.0, 356.4), (470.0, 470.8),
             (590.0, 591.0)])
        self.assertEqual(
            [(round(c.core_start_s, 2), round(c.core_end_s, 2))
             for c in chunks],
            [(0.0, 178.65), (178.65, 355.70), (355.70, 612.40)])
        self.assertEqual(
            [(round(c.start_s, 2), round(c.end_s, 2)) for c in chunks],
            [(0.0, 178.65), (170.65, 355.70), (347.70, 612.40)])
        self.assertTrue(178.2 < chunks[1].core_start_s < 179.1)
        self.assertTrue(355.0 < chunks[2].core_start_s < 356.4)
        single = transcribe_mod.plan_chunks(59.58, [])
        self.assertEqual(len(single), 1)
        self.assertEqual((single[0].core_start_s, single[0].core_end_s,
                          single[0].start_s, single[0].end_s),
                         (0.0, 59.58, 0.0, 59.58))

    def test_plan_chunks_never_inside_speech(self) -> None:
        pauses = [(float(t), float(t + 1)) for t in range(10, 171, 10)]
        pauses.append((575.0, 576.0))
        chunks = transcribe_mod.plan_chunks(630.0, pauses)
        self.assertEqual(len(chunks), 2)
        self.assertAlmostEqual(chunks[0].core_end_s, 170.5, places=6)
        self.assertAlmostEqual(chunks[1].core_start_s, 170.5, places=6)
        self.assertAlmostEqual(chunks[1].core_end_s, 630.0, places=6)
        self.assertGreater(chunks[1].core_end_s - chunks[1].core_start_s,
                           transcribe_mod.CHUNK_MAX_S)
        words = []
        for t in range(0, 630, 5):
            if any(t < end and t + 1 > start for start, end in pauses):
                continue
            words.append(_w("word", float(t), float(t + 1)))
        for chunk in chunks[1:]:
            boundary = chunk.core_start_s
            for word in words:
                self.assertFalse(word.start_s < boundary < word.end_s,
                                 f"boundary {boundary} inside a word")

    def test_pauses_from_words_and_merge(self) -> None:
        words = [_w("pack", 0.0, 1.0), _w("the", 1.5, 2.0),
                 _w("rig", 3.0, 3.5)]
        self.assertEqual(transcribe_mod.pauses_from_words(words),
                         [(2.0, 3.0)])
        merged = transcribe_mod._merged_pauses(
            [(0.5, 1.2), (5.0, 7.0)], [(2.0, 3.0)], words)
        self.assertEqual(merged, [(2.0, 3.0), (5.1, 6.9)])

    def test_estimate_and_over_budget_gate(self) -> None:
        self.assertAlmostEqual(
            transcribe_mod.estimate_transcribe_usd(3600.0), 0.18, places=6)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(transcribe_mod, "run_command",
                                   side_effect=AssertionError("no ffmpeg")), \
                    mock.patch.object(transcribe_mod, "diarize_chunk",
                                      side_effect=AssertionError("no diarize")):
                result = transcribe_mod.transcribe_audio_clip(
                    clip_path=Path(tmp) / "lav.wav",
                    whisper_words=[_w("hello", 0.0, 1.0)],
                    duration_s=600.0, work_dir=Path(tmp) / "work",
                    max_usd=0.01)
            self.assertEqual(result.method, "whisper")
            self.assertEqual(
                result.warnings,
                ["transcribe_over_budget:est $0.03 over remaining $0.01"])


class ReconcileSpeakersTests(unittest.TestCase):
    def test_reconcile_speakers_overlap_vote(self) -> None:
        chunks = [Chunk(0, 0.0, 100.0, 0.0, 100.0),
                  Chunk(1, 100.0, 200.0, 92.0, 200.0)]
        chunk_turns = {
            0: [_raw(0, "SPEAKER_00", 90.0, 95.0, "the quick brown fox"),
                _raw(0, "SPEAKER_01", 95.0, 99.0, "jumps over the lazy dog")],
            1: [_raw(1, "SPEAKER_01", 92.0, 96.0, "the quick brown fox"),
                _raw(1, "SPEAKER_00", 96.0, 99.0, "jumps over the lazy dog"),
                _raw(1, "SPEAKER_01", 100.0, 150.0,
                     "more speech here from the same voice"),
                _raw(1, "SPEAKER_00", 150.0, 160.0, "a short aside remark")],
        }
        turns, speakers, warnings = transcribe_mod.reconcile_speakers(
            chunk_turns, chunks)
        self.assertEqual(warnings, [])
        self.assertEqual(
            [(t.text, t.speaker) for t in turns],
            [("the quick brown fox", "talent"),
             ("jumps over the lazy dog", "other-1"),
             ("more speech here from the same voice", "talent"),
             ("a short aside remark", "other-1")])
        self.assertEqual([t.id for t in turns], [0, 1, 2, 3])
        self.assertEqual([s["id"] for s in speakers], ["talent", "other-1"])
        talent = speakers[0]
        self.assertEqual(set(talent["raw_speakers"]),
                         {"SPEAKER_00", "SPEAKER_01"})
        self.assertAlmostEqual(talent["duration_s"], 55.0)
        self.assertEqual(talent["turns"], 2)

    def test_reconcile_speakers_fallback_and_naming(self) -> None:
        chunks = [Chunk(0, 0.0, 100.0, 0.0, 100.0),
                  Chunk(1, 100.0, 200.0, 92.0, 200.0)]
        chunk_turns = {
            0: [_raw(0, "SPEAKER_00", 10.0, 20.0, "first speaker talking here"),
                _raw(0, "SPEAKER_01", 30.0, 35.0, "second voice brief")],
            1: [_raw(1, "SPEAKER_00", 110.0, 115.0, "lone speech far away"),
                _raw(1, "SPEAKER_01", 120.0, 140.0,
                     "a much longer passage of speech here now")],
        }
        turns, speakers, warnings = transcribe_mod.reconcile_speakers(
            chunk_turns, chunks)
        self.assertEqual(warnings, ["transcribe_speaker_fallback:1"])
        by_text = {t.text: t.speaker for t in turns}
        self.assertEqual(by_text["a much longer passage of speech here now"],
                         "talent")
        self.assertEqual(by_text["lone speech far away"], "other-1")
        three, three_speakers, _ = transcribe_mod.reconcile_speakers(
            {0: [_raw(0, "A", 0.0, 10.0, "ten seconds of the main voice"),
                 _raw(0, "B", 20.0, 25.0, "five seconds here"),
                 _raw(0, "C", 30.0, 35.0, "five seconds there")]},
            [Chunk(0, 0.0, 100.0, 0.0, 100.0)])
        self.assertEqual([s["id"] for s in three_speakers],
                         ["talent", "other-1", "other-2"])
        self.assertEqual(
            [(t.raw_speaker, t.speaker) for t in three],
            [("A", "talent"), ("B", "other-1"), ("C", "other-2")])


class AlignWordsTests(unittest.TestCase):
    def test_align_repeat_whisper_dropped(self) -> None:
        # §7.7 example 1, as the §7.3 algorithm actually emits it:
        # difflib's longest match is [the, problem, is, timecode], so the
        # paired `the` is Muse's second one and the muse-only `the` (the
        # first in Muse order) interpolates into the gap before it. The
        # design's gloss ("difflib anchors the first the") mis-predicts
        # this; the opcode walk of §7.3 is normative and decides.
        whisper = [_w("So", 10.00, 10.12), _w("the", 10.14, 10.22),
                   _w("problem", 10.24, 10.60), _w("is", 10.62, 10.70),
                   _w("timecode", 10.72, 11.10)]
        turns = [_turn(0, 9.98, 11.20, "talent",
                       "So the the problem is timecode.")]
        words, stats = transcribe_mod.align_words(whisper, turns)
        self.assertEqual(
            [(w.text, w.heard_by) for w in words],
            [("So", "both"), ("the", "muse"), ("the", "both"),
             ("problem", "both"), ("is", "both"), ("timecode.", "both")])
        self.assertEqual(
            [(round(w.start_s, 2), round(w.end_s, 2)) for w in words],
            [(10.00, 10.12), (10.12, 10.14), (10.14, 10.22),
             (10.24, 10.60), (10.62, 10.70), (10.72, 11.10)])
        self.assertTrue(words[2].disfluency)
        self.assertFalse(words[1].disfluency)
        self.assertTrue(all(w.speaker == "talent" for w in words))
        self.assertEqual((stats.whisper_words, stats.muse_tokens,
                          stats.matched, stats.muse_only,
                          stats.whisper_only, stats.matched_share),
                         (5, 6, 5, 1, 0, 1.0))

    def test_align_number_digits_vs_words(self) -> None:
        whisper = [_w("We", 20.00, 20.10), _w("shot", 20.12, 20.35),
                   _w("3", 20.37, 20.45), _w("cameras", 20.47, 20.80),
                   _w("at", 20.82, 20.90), _w("24", 20.92, 21.10),
                   _w("frames", 21.12, 21.40)]
        turns = [_turn(0, 19.90, 21.50, "talent",
                       "We shot three cameras at twenty four frames")]
        words, stats = transcribe_mod.align_words(whisper, turns)
        self.assertEqual([w.text for w in words],
                         ["We", "shot", "three", "cameras", "at",
                          "twenty", "four", "frames"])
        self.assertTrue(all(w.heard_by == "both" for w in words))
        three = words[2]
        self.assertEqual((three.start_s, three.end_s), (20.37, 20.45))
        twenty, four = words[5], words[6]
        self.assertAlmostEqual(twenty.start_s, 20.92)
        self.assertAlmostEqual(four.end_s, 21.10)
        self.assertAlmostEqual(twenty.end_s, four.start_s)
        self.assertLess(twenty.start_s, twenty.end_s)
        self.assertEqual((stats.muse_only, stats.whisper_only,
                          stats.matched_share), (0, 0, 1.0))
        flipped_whisper = [_w("We", 20.00, 20.10), _w("shot", 20.12, 20.35),
                           _w("twenty", 20.37, 20.41), _w("four", 20.41, 20.45),
                           _w("cameras", 20.47, 20.80)]
        flipped_turns = [_turn(0, 19.90, 21.00, "talent",
                               "We shot 24 cameras")]
        flipped, flipped_stats = transcribe_mod.align_words(
            flipped_whisper, flipped_turns)
        self.assertEqual([w.text for w in flipped],
                         ["We", "shot", "24", "cameras"])
        digit = flipped[2]
        self.assertEqual(digit.heard_by, "both")
        self.assertEqual((digit.start_s, digit.end_s), (20.37, 20.45))
        self.assertEqual((flipped_stats.muse_only, flipped_stats.whisper_only),
                         (0, 0))

    def test_align_crew_talk_second_speaker(self) -> None:
        whisper = [_w("rolling", 5.00, 5.32), _w("So", 5.60, 5.72),
                   _w("we", 5.74, 5.84), _w("built", 5.86, 6.10),
                   _w("three", 6.12, 6.30), _w("cameras", 6.32, 6.66)]
        turns = [_turn(0, 4.90, 5.40, "other-1", "rolling",
                       raw_speaker="SPEAKER_01"),
                 _turn(1, 5.55, 6.70, "talent", "So we built three cameras")]
        words, _ = transcribe_mod.align_words(whisper, turns)
        self.assertEqual(
            [(w.text, w.speaker) for w in words],
            [("rolling", "other-1"), ("So", "talent"), ("we", "talent"),
             ("built", "talent"), ("three", "talent"),
             ("cameras", "talent")])
        dropped, _ = transcribe_mod.align_words(whisper[1:], turns)
        self.assertEqual(dropped[0].text, "rolling")
        self.assertEqual(dropped[0].heard_by, "muse")
        self.assertEqual(dropped[0].speaker, "other-1")
        self.assertEqual((dropped[0].start_s, dropped[0].end_s),
                         (4.90, 5.60))

    def test_align_replace_and_surplus(self) -> None:
        whisper = [_w("settle", 1.0, 1.4), _w("their", 1.5, 1.9),
                   _w("debts", 2.0, 2.4), _w("here", 2.5, 2.9)]
        turns = [_turn(0, 0.9, 3.0, "talent", "settle there debts here")]
        words, stats = transcribe_mod.align_words(whisper, turns)
        self.assertEqual([(w.text, w.heard_by) for w in words],
                         [("settle", "both"), ("there", "both"),
                          ("debts", "both"), ("here", "both")])
        self.assertEqual((words[1].start_s, words[1].end_s), (1.5, 1.9))
        long_whisper = [_w("alpha", 1.0, 1.4), _w("bravo", 1.5, 1.9),
                        _w("charlie", 2.0, 2.4), _w("delta", 2.5, 2.9),
                        _w("gamma", 3.0, 3.4), _w("kilo", 3.5, 3.9),
                        _w("omega", 4.0, 4.4)]
        long_turns = [_turn(0, 0.9, 4.5, "talent",
                            "alpha extra gamma why1 why2 omega")]
        long_words, long_stats = transcribe_mod.align_words(
            long_whisper, long_turns)
        self.assertEqual(
            [(w.text, w.heard_by) for w in long_words],
            [("alpha", "both"), ("extra", "both"), ("charlie", "whisper"),
             ("delta", "whisper"), ("gamma", "both"), ("why1", "both"),
             ("why2", "muse"), ("omega", "both")])
        self.assertEqual((long_stats.matched, long_stats.muse_only,
                          long_stats.whisper_only),
                         (5, 1, 2))

    def test_align_muse_only_closed_gap(self) -> None:
        whisper = [_w("one", 1.00, 1.50), _w("two", 1.50, 2.00)]
        turns = [_turn(0, 0.90, 2.10, "talent", "one uh two")]
        words, _ = transcribe_mod.align_words(whisper, turns)
        self.assertEqual([w.text for w in words], ["one", "uh", "two"])
        self.assertEqual((words[1].start_s, words[1].end_s), (1.50, 1.50))
        self.assertEqual(words[1].heard_by, "muse")
        self.assertEqual((words[0].start_s, words[0].end_s), (1.00, 1.50))
        self.assertEqual((words[2].start_s, words[2].end_s), (1.50, 2.00))

    def test_align_low_share_discards(self) -> None:
        whisper = [_w(f"alpha{i}", float(i), float(i) + 0.4)
                   for i in range(25)]
        turns = [_turn(0, 0.0, 30.0, "talent",
                       "alpha0 alpha1 alpha2 alpha3 alpha4 alpha5 "
                       "alpha6 alpha7")]
        words, stats = transcribe_mod.align_words(whisper, turns)
        self.assertEqual(len(words), 25)
        self.assertAlmostEqual(stats.matched_share, 0.32)
        with tempfile.TemporaryDirectory() as tmp:
            diarized = _diarized(
                [(0, 8000, turns[0].text, "SPEAKER_00")], duration_ms=60000)
            with mock.patch.object(transcribe_mod, "run_command",
                                   side_effect=_ffmpeg_side_effect()), \
                    mock.patch.object(transcribe_mod, "diarize_chunk",
                                      return_value=(diarized, 0.003, False)):
                result = transcribe_mod.transcribe_audio_clip(
                    clip_path=Path(tmp) / "lav.wav", whisper_words=whisper,
                    duration_s=60.0, work_dir=Path(tmp) / "work",
                    max_usd=2.0)
        self.assertEqual(result.method, "whisper")
        self.assertEqual(result.words, whisper)
        self.assertIn("transcribe_alignment_low:0.32", result.warnings)

    def test_speaker_for_time(self) -> None:
        turns = [_turn(0, 10.0, 12.0, "talent", "first passage here"),
                 _turn(1, 12.0, 14.0, "other-1", "second passage here")]
        self.assertEqual(transcribe_mod.speaker_for_time(turns, 11.0),
                         "talent")
        self.assertEqual(transcribe_mod.speaker_for_time(turns, 11.75),
                         "talent")
        self.assertEqual(transcribe_mod.speaker_for_time(turns, 14.4),
                         "other-1")
        self.assertEqual(transcribe_mod.speaker_for_time(turns, 30.0), "")


class OrchestratorTests(unittest.TestCase):
    def _two_chunk_words(self) -> list[TimedWord]:
        # Word gaps (179.4, 183.5) and (185.1, 189.0) plan a 400 s clip
        # as cores [0, 181.45] and [181.45, 400.0].
        return [_w("alpha", 176.0, 176.4), _w("bravo", 176.6, 177.0),
                _w("charlie", 177.2, 177.6), _w("crew", 178.0, 178.3),
                _w("aside", 178.5, 178.8), _w("remark", 179.0, 179.4),
                _w("delta", 183.5, 183.9), _w("echo", 184.1, 184.5),
                _w("foxtrot", 184.7, 185.1), _w("golf", 189.0, 189.3),
                _w("hotel", 189.6, 190.0)]

    def _two_chunk_diarize(self, first_spend: float = 0.0089,
                           second_spend: float = 0.0,
                           second_cached: bool = True):
        chunk0 = _diarized([(176000, 177600, "alpha bravo charlie",
                             "SPEAKER_00"),
                            (178000, 179400, "crew aside remark",
                             "SPEAKER_01")])
        # Chunk 1's WAV starts at 173.45 s; its first two turns echo the
        # overlap speech with swapped labels, the last two are new.
        chunk1 = _diarized([(2550, 4150, "alpha bravo charlie",
                             "SPEAKER_01"),
                            (4550, 5950, "crew aside remark",
                             "SPEAKER_00"),
                            (10050, 11650, "delta echo foxtrot",
                             "SPEAKER_01"),
                            (15550, 16550, "golf golf um hotel",
                             "SPEAKER_01")])

        def _diarize(wav, **kwargs):
            name = Path(wav).name
            if name == "chunk-000.wav":
                return chunk0, first_spend, False
            if name == "chunk-001.wav":
                return chunk1, second_spend, second_cached
            raise AssertionError(f"unexpected chunk WAV: {name}")

        return _diarize

    def test_transcribe_audio_clip_end_to_end_fake(self) -> None:
        # A 400 s clip (not the design's 200 s: 200 s plans as one
        # chunk, and this item needs the swapped-label second chunk).
        silence = ("[silencedetect @ 0x1] silence_start: 50.000\n"
                   "[silencedetect @ 0x1] silence_end: 52.000\n")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(transcribe_mod, "run_command",
                                   side_effect=_ffmpeg_side_effect(silence)), \
                    mock.patch.object(transcribe_mod, "diarize_chunk",
                                      side_effect=self._two_chunk_diarize()):
                result = transcribe_mod.transcribe_audio_clip(
                    clip_path=Path(tmp) / "lav.wav",
                    whisper_words=self._two_chunk_words(),
                    duration_s=400.0, work_dir=Path(tmp) / "work",
                    max_usd=2.0)
        self.assertEqual(result.method, "muse")
        self.assertEqual(result.warnings, [])
        self.assertEqual(len(result.chunks), 2)
        by_text = {w.text: w for w in result.words}
        self.assertEqual(by_text["crew"].speaker, "other-1")
        self.assertEqual(by_text["alpha"].speaker, "talent")
        self.assertEqual(by_text["delta"].speaker, "talent")
        golf = [w for w in result.words if w.text == "golf"]
        self.assertEqual(len(golf), 2)
        self.assertEqual({w.heard_by for w in golf}, {"both", "muse"})
        muse_golf = next(w for w in golf if w.heard_by == "muse")
        self.assertTrue(muse_golf.disfluency)
        self.assertEqual(muse_golf.speaker, "talent")
        filler = by_text["um"]
        self.assertEqual(filler.heard_by, "muse")
        self.assertTrue(filler.disfluency)
        assert result.stats is not None
        self.assertEqual((result.stats.whisper_words, result.stats.muse_tokens,
                          result.stats.matched, result.stats.muse_only,
                          result.stats.whisper_only),
                         (11, 13, 11, 2, 0))
        self.assertAlmostEqual(result.stats.matched_share, 1.0)
        payload = transcribe_mod.build_transcript_payload(
            audio_label="C", clip_path="lav.wav", offset_s=178.746,
            duration_s=400.0, result=result)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["method"], "muse")
        self.assertEqual(payload["model"], "muse-voice-transcribe-1.0")
        self.assertEqual([s["id"] for s in payload["speakers"]],
                         ["talent", "other-1"])
        self.assertEqual(payload["chunks"][1]["spend_usd"], 0.0)
        self.assertAlmostEqual(
            sum(c["spend_usd"] for c in payload["chunks"]),
            payload["spend_usd"], places=6)
        self.assertAlmostEqual(payload["chunks"][0]["core_start_s"], 178.746)
        self.assertEqual(payload["alignment"]["matched_share"], 1.0)
        self.assertEqual(
            sum(s["words"] for s in payload["speakers"]), len(result.words))

    def test_transcribe_fallbacks(self) -> None:
        whisper = [_w("pack", 1.0, 1.5), _w("the", 1.6, 2.0)]
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"

            def _fields(words):
                return [(w.text, w.start_s, w.end_s, w.speaker,
                         w.heard_by, w.disfluency) for w in words]

            with mock.patch.object(transcribe_mod, "run_command",
                                   side_effect=_ffmpeg_side_effect()), \
                    mock.patch.object(
                        transcribe_mod, "diarize_chunk",
                        side_effect=MuseUnavailable(
                            "No module named 'guestlist'")):
                missing = transcribe_mod.transcribe_audio_clip(
                    clip_path=Path(tmp) / "lav.wav", whisper_words=whisper,
                    duration_s=60.0, work_dir=work, max_usd=2.0)
            self.assertEqual(missing.method, "whisper")
            self.assertEqual(
                missing.warnings[-1],
                "transcribe_unavailable:No module named 'guestlist'")
            self.assertEqual(_fields(missing.words), _fields(whisper))
            self.assertEqual(
                (missing.turns, missing.speakers, missing.chunks,
                 missing.stats, missing.spend_usd),
                ([], [], [], None, 0.0))

            def _fail_second(wav, **kwargs):
                if Path(wav).name == "chunk-000.wav":
                    return _diarized([(1000, 2000, "pack the", "SPEAKER_00")]), \
                        0.001, False
                raise RuntimeError("transcribe HTTP 500: overloaded")

            with mock.patch.object(transcribe_mod, "run_command",
                                   side_effect=_ffmpeg_side_effect()), \
                    mock.patch.object(transcribe_mod, "diarize_chunk",
                                      side_effect=_fail_second):
                failed = transcribe_mod.transcribe_audio_clip(
                    clip_path=Path(tmp) / "lav.wav",
                    whisper_words=self._two_chunk_words(),
                    duration_s=400.0, work_dir=work, max_usd=2.0)
            self.assertEqual(failed.method, "whisper")
            self.assertEqual(
                failed.warnings[-1],
                "transcribe_failed:RuntimeError: "
                "transcribe HTTP 500: overloaded")
            self.assertEqual(failed.turns, [])
            self.assertEqual(_fields(failed.words),
                             _fields(self._two_chunk_words()))

            with mock.patch.object(transcribe_mod, "run_command",
                                   side_effect=_ffmpeg_side_effect()), \
                    mock.patch.object(transcribe_mod, "diarize_chunk",
                                      return_value=(_diarized([]), 0.0,
                                                    False)):
                empty = transcribe_mod.transcribe_audio_clip(
                    clip_path=Path(tmp) / "lav.wav", whisper_words=whisper,
                    duration_s=60.0, work_dir=work, max_usd=2.0)
            self.assertEqual(empty.method, "whisper")
            self.assertEqual(empty.warnings[-1], "transcribe_no_turns")
            self.assertEqual(_fields(empty.words), _fields(whisper))


class WordsPayloadTests(unittest.TestCase):
    def test_words_payload_round_trip(self) -> None:
        enriched = [_w("So", 190.786, 190.906, "talent", "both"),
                    _w("the", 191.006, 191.026, "talent", "muse", True)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "words.json"
            path.write_text(json.dumps(cut_mod.words_file_payload(enriched)),
                            encoding="utf-8")
            from director.angles import load_timed_words

            loaded = load_timed_words(path)
            self.assertEqual(
                [(w.text, w.start_s, w.end_s, w.speaker, w.heard_by,
                  w.disfluency) for w in loaded],
                [(w.text, w.start_s, w.end_s, w.speaker, w.heard_by,
                  w.disfluency) for w in enriched])
            plain = Path(tmp) / "plain.json"
            plain.write_text(json.dumps(
                {"words": [{"word": "hi", "start": 1.0, "end": 1.5}]}),
                encoding="utf-8")
            fallback = load_timed_words(plain)
            self.assertEqual(
                (fallback[0].speaker, fallback[0].heard_by,
                 fallback[0].disfluency),
                ("", "whisper", False))
        shifted = cut_mod.shift_words(enriched, 178.746)
        self.assertEqual(
            [(w.text, round(w.start_s, 3), round(w.end_s, 3), w.speaker,
              w.heard_by, w.disfluency) for w in shifted],
            [("So", 369.532, 369.652, "talent", "both", False),
             ("the", 369.752, 369.772, "talent", "muse", True)])


class _CutStageFakes:
    """Stub sync/angles runs for a two-clip job (no ffmpeg)."""

    def __init__(self) -> None:
        self.calls: collections.Counter[str] = collections.Counter()

    def sync_run(self, *, clip_paths, reference_path, methods,
                 transcripts_dir, out_path, whisper_model=None):
        self.calls["sync"] += 1
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        def _clip(label, path, offset):
            return {
                "label": label, "offset_s": offset,
                "method": "reference" if label == "A" else "waveform",
                "camera": {"path": str(path), "make": "", "model": "",
                           "lens": "", "fps": 30.0, "width": 0, "height": 0,
                           "duration_s": 60.0, "has_audio": True,
                           "gamma": "", "timecode": None, "tc_rate": None,
                           "captured_at": None},
                "estimates": [], "checks": [], "drift_ppm": None,
                "warnings": []}

        payload = {"reference": str(clip_paths[0]),
                   "methods": list(methods),
                   "clips": [_clip("A", clip_paths[0], 0.0),
                             _clip("B", clip_paths[1], 0.0)]}
        write_json(out, payload)
        cache = (out.parent / "sync-transcripts"
                 / f"{Path(clip_paths[0]).stem}.json")
        cache.parent.mkdir(parents=True, exist_ok=True)
        write_json(cache, {"words": [{"word": "hello", "start": 1.0,
                                      "end": 1.5}]})
        return payload

    def angles_run(self, *, sync_path, words_path, out_path, audio_label,
                   render_path, min_shot_s, model, no_muse, grade_path=None):
        self.calls["angles"] += 1
        payload = {"version": 1, "sync": str(sync_path),
                   "words": str(words_path), "reference": "A",
                   "grade": None,
                   "audio": {"label": audio_label, "clip_path": "a.mp4",
                             "offset_s": 0.0},
                   "method": "rule", "model": model,
                   "min_shot_s": min_shot_s, "spend_usd": 0.0,
                   "warnings": [], "lines": [], "shots": []}
        write_json(Path(out_path), payload)
        if render_path is not None:
            Path(render_path).touch()
        return payload


def _run_cut_job(first: Path, second: Path, out_dir: Path, **overrides):
    kwargs: dict = {
        "clips": [first, second], "out_dir": out_dir,
        "reference_path": None, "audio_path": None, "words_path": None,
        "transcripts_dir": None,
        "methods": ["timecode", "clock", "waveform", "whisper"],
        "no_grade": True, "grade_check": False, "no_muse": False,
        "model": "muse-spark-1.3", "min_shot_s": 2.0, "no_vertical": True,
        "no_sam": True, "no_export": True, "no_qc": True, "max_usd": 5.0,
        "force": False, "project_name": "t",
    }
    kwargs.update(overrides)
    return cut_mod.run(**kwargs)


def _muse_result(spend: float = 0.003,
                 warnings: list[str] | None = None) -> TranscribeResult:
    return TranscribeResult(
        method="muse",
        words=[TimedWord("hello", 1.0, 1.5, "talent", "both", False)],
        turns=[MuseTurn(0, 0.9, 1.6, "talent", "SPEAKER_00", "hello", 0)],
        speakers=[{"id": "talent", "raw_speakers": ["SPEAKER_00"],
                   "duration_s": 0.7, "turns": 1, "words": 1}],
        chunks=[Chunk(0, 0.0, 60.0, 0.0, 60.0, spend_usd=spend)],
        stats=AlignStats(1, 1, 1, 0, 0, 1.0), spend_usd=spend,
        warnings=list(warnings or []))


def _whisper_result(warning: str) -> TranscribeResult:
    return TranscribeResult(
        method="whisper", words=[TimedWord("hello", 1.0, 1.5)],
        turns=[], speakers=[], chunks=[], stats=None, spend_usd=0.0,
        warnings=[warning])


class CutWordsStageTests(unittest.TestCase):
    def _job(self, tmp: Path) -> tuple[Path, Path, Path]:
        cards = tmp / "cards"
        cards.mkdir(parents=True)
        first, second = cards / "a.mp4", cards / "b.mp4"
        first.touch()
        second.touch()
        return first, second, tmp / "out"

    def _stubbed(self, fakes: _CutStageFakes):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(cut_mod.sync_mod, "run",
                                              fakes.sync_run))
        stack.enter_context(mock.patch.object(cut_mod.angles_mod, "run",
                                              fakes.angles_run))
        return stack

    def test_cut_words_stage_enriches_and_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # (a) Muse success.
            first, second, out_dir = self._job(tmp_path / "a")
            fakes = _CutStageFakes()
            with self._stubbed(fakes), \
                    mock.patch.object(cut_mod.transcribe_mod,
                                      "transcribe_audio_clip",
                                      return_value=_muse_result()) as fake:
                done = _run_cut_job(first, second, out_dir)
            self.assertEqual(fake.call_count, 1)
            words = json.loads((out_dir / "words.json").read_text(
                encoding="utf-8"))["words"]
            self.assertEqual(words[0]["speaker"], "talent")
            self.assertEqual(words[0]["heard_by"], "both")
            transcript = json.loads((out_dir / "transcript.json").read_text(
                encoding="utf-8"))
            self.assertEqual(transcript["method"], "muse")
            self.assertEqual(done["audio"]["transcribe"]["method"], "muse")
            self.assertAlmostEqual(done["stages"]["words"]["spend_usd"],
                                   0.003, places=6)
            self.assertAlmostEqual(
                sum(r["spend_usd"] for r in done["stages"].values()),
                done["spend_usd"], places=6)
            # (b) Whisper fallback.
            first, second, out_dir = self._job(tmp_path / "b")
            out_dir.mkdir(parents=True)
            (out_dir / "transcript.json").write_text("stale",
                                                    encoding="utf-8")
            fakes = _CutStageFakes()
            with self._stubbed(fakes), \
                    mock.patch.object(
                        cut_mod.transcribe_mod, "transcribe_audio_clip",
                        return_value=_whisper_result(
                            "transcribe_failed:RuntimeError: boom")):
                done = _run_cut_job(first, second, out_dir)
            self.assertFalse((out_dir / "transcript.json").exists())
            self.assertIn("transcribe_failed:RuntimeError: boom",
                          done["stages"]["words"]["warnings"])
            self.assertTrue(done["status"].startswith("complete"))
            self.assertEqual(done["audio"]["transcribe"]["method"],
                             "whisper")
            # (c) --no-transcribe skips without calling.
            first, second, out_dir = self._job(tmp_path / "c")
            fakes = _CutStageFakes()
            with self._stubbed(fakes), \
                    mock.patch.object(cut_mod.transcribe_mod,
                                      "transcribe_audio_clip",
                                      return_value=_muse_result()) as fake:
                done = _run_cut_job(first, second, out_dir,
                                    no_transcribe=True)
            self.assertEqual(fake.call_count, 0)
            self.assertEqual(done["audio"]["transcribe"]["method"],
                             "skipped")
            # (d) Explicit --words skips without calling.
            first, second, out_dir = self._job(tmp_path / "d")
            explicit = tmp_path / "explicit.json"
            explicit.write_text(json.dumps(
                {"words": [{"word": "hi", "start": 5.0, "end": 5.5}]}),
                encoding="utf-8")
            fakes = _CutStageFakes()
            with self._stubbed(fakes), \
                    mock.patch.object(cut_mod.transcribe_mod,
                                      "transcribe_audio_clip",
                                      return_value=_muse_result()) as fake:
                done = _run_cut_job(first, second, out_dir,
                                    words_path=explicit)
            self.assertEqual(fake.call_count, 0)
            self.assertEqual(done["audio"]["transcribe"]["method"],
                             "skipped")

    def test_cut_words_fingerprint_transcribe_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            first, second, out_dir = self._job(tmp_path)
            fakes = _CutStageFakes()
            with self._stubbed(fakes), \
                    mock.patch.object(cut_mod.transcribe_mod,
                                      "transcribe_audio_clip",
                                      return_value=_muse_result()) as fake:
                first_run = _run_cut_job(first, second, out_dir)
                self.assertEqual(first_run["stages"]["words"]["status"],
                                 "done")
                self.assertEqual(fake.call_count, 1)
                second_run = _run_cut_job(first, second, out_dir)
                self.assertEqual(second_run["stages"]["words"]["status"],
                                 "skipped")
                self.assertEqual(fake.call_count, 1)
                third_run = _run_cut_job(first, second, out_dir,
                                         no_transcribe=True)
                self.assertEqual(third_run["stages"]["words"]["status"],
                                 "done")
                self.assertEqual(
                    third_run["audio"]["transcribe"]["method"], "skipped")
                self.assertEqual(fake.call_count, 1)


class TranscriptEvidenceTests(unittest.TestCase):
    def test_build_utterances_transcript_aggregates(self) -> None:
        words = [_w("hello", 0.0, 0.2, "talent"),
                 _w("there", 0.3, 0.5, "talent", "both", True),
                 _w("rolling", 1.0, 3.0, "other-1", "muse")]
        utterances = tighten_mod.build_utterances(words)
        self.assertEqual(len(utterances), 1)
        utterance = utterances[0]
        self.assertEqual(utterance.speaker, "other-1")
        self.assertEqual(utterance.disfluency_count, 1)
        self.assertEqual(utterance.muse_only_count, 1)
        plain = tighten_mod.build_utterances(
            [_w("hello", 0.0, 0.2), _w("there", 0.3, 0.5)])
        self.assertEqual(
            (plain[0].speaker, plain[0].disfluency_count,
             plain[0].muse_only_count),
            ("", 0, 0))
        window = tighten_mod.Window(0, [0], 0.0, 4.0)
        bare = tighten_mod.window_prompt(window, plain, None, [])
        self.assertIn("U0 [0.0-0.5] hello there", bare)
        self.assertNotIn("speaker", bare)
        rich = tighten_mod.window_prompt(
            window,
            [tighten_mod.Utterance(0, 0.0, 0.5, "hello there", 0, 2,
                                   "talent", 1, 0)],
            None, [])
        self.assertIn(
            "U0 [0.0-0.5] hello there (speaker talent, 1 disfluency)", rich)
        self.assertTrue(tighten_mod.DEVELOPER.endswith(
            "Utterances may carry a speaker tag and a disfluency count from "
            "the verbatim transcript: a speaker other than talent, and "
            "repeated or filler words, count against an attempt, but what "
            "you can hear in the delivery still matters more than the tag."))

    def _crew_beat(self) -> tuple[cutlist.Beat, list[tighten_mod.Utterance],
                                 list[TimedWord]]:
        words = [_w("rolling", 5.0, 5.4, "other-1", "both"),
                 _w("again", 5.5, 5.9, "other-1", "both"),
                 _w("so", 10.0, 10.2, "talent", "both"),
                 _w("ready", 10.3, 10.7, "talent", "both")]
        utterances = [tighten_mod.Utterance(0, 5.0, 5.9, "rolling again",
                                            0, 2, "other-1", 0, 0),
                      tighten_mod.Utterance(1, 10.0, 10.7, "so ready",
                                            2, 4, "talent", 0, 0)]
        beat = cutlist.Beat(
            0, "the point", [
                cutlist.Attempt(0, [0], 5.0, 5.9, "rolling again",
                                "pick", "keeper", "spark: lively", 0.9),
                cutlist.Attempt(1, [1], 10.0, 10.7, "so ready",
                                "alternate", "alternate", "spark: flat",
                                0.5)],
            0, "kept", 3, "spark: lively")
        return beat, utterances, words

    def test_attempt_stats_and_speaker_removal(self) -> None:
        beat, utterances, words = self._crew_beat()
        by_id = {u.id: u for u in utterances}
        share, dominant, disfluencies = tighten_mod.attempt_transcript_stats(
            beat.attempts[0], by_id, words)
        self.assertEqual((share, dominant, disfluencies), (0.0, "other-1", 0))
        share, dominant, _ = tighten_mod.attempt_transcript_stats(
            beat.attempts[1], by_id, words)
        self.assertEqual((share, dominant), (1.0, "talent"))
        kept, warnings = tighten_mod.apply_transcript_evidence(
            [beat], utterances, words)
        self.assertEqual(warnings, [])
        self.assertEqual(kept[0].attempts[0].verdict, "remove")
        self.assertEqual(kept[0].attempts[0].kind, "crew_talk")
        self.assertEqual(kept[0].attempts[0].reason,
                         "speaker other-1 is not talent")
        self.assertEqual(kept[0].pick_attempt, 1)
        self.assertEqual(kept[0].status, "kept")
        self.assertIn("(promoted: previous pick was other-1 speech)",
                      kept[0].attempts[1].reason)
        lone = cutlist.Beat(
            3, "crew only",
            [cutlist.Attempt(0, [0], 5.0, 5.9, "rolling again",
                             "pick", "keeper", "spark: kept", 0.9)],
            0, "kept", 3, "spark: kept")
        left, warnings = tighten_mod.apply_transcript_evidence(
            [lone], utterances, words)
        self.assertEqual(left[0].pick_attempt, -1)
        self.assertEqual(left[0].status, "no_usable_attempt")
        self.assertEqual(warnings, ["tighten_beat_other_speaker:B3"])
        bare_words = [_w("rolling", 5.0, 5.4), _w("again", 5.5, 5.9)]
        bare_utterances = [tighten_mod.Utterance(0, 5.0, 5.9,
                                                 "rolling again", 0, 2)]
        bare = cutlist.Beat(
            0, "the point",
            [cutlist.Attempt(0, [0], 5.0, 5.9, "rolling again",
                             "pick", "keeper", "spark: kept", 0.9)],
            0, "kept", 3, "spark: kept")
        same, warnings = tighten_mod.apply_transcript_evidence(
            [bare], bare_utterances, bare_words)
        self.assertEqual(warnings, [])
        self.assertEqual(same[0].attempts[0].verdict, "pick")
        self.assertEqual(same[0].attempts[0].reason, "spark: kept")
        self.assertEqual(same[0].pick_attempt, 0)
        self.assertAlmostEqual(same[0].attempts[0].confidence, 0.9)

    def test_disfluency_swap_and_sole_take(self) -> None:
        words = [_w("well", 1.0, 1.2, "talent", "both", True),
                 _w("um", 1.3, 1.5, "talent", "muse", True),
                 _w("so", 1.6, 1.8, "talent", "both", True),
                 _w("packed", 5.0, 5.4, "talent", "both"),
                 _w("ready", 5.5, 5.9, "talent", "both")]
        utterances = [tighten_mod.Utterance(0, 1.0, 1.8, "well um so",
                                            0, 3, "talent", 3, 1),
                      tighten_mod.Utterance(1, 5.0, 5.9, "packed ready",
                                            3, 5, "talent", 0, 0)]
        beat = cutlist.Beat(
            0, "the point", [
                cutlist.Attempt(0, [0], 1.0, 1.8, "well um so",
                                "pick", "keeper", "spark: lively", 0.9),
                cutlist.Attempt(1, [1], 5.0, 5.9, "packed ready",
                                "alternate", "alternate", "spark: flat",
                                0.5)],
            0, "kept", 3, "spark: lively")
        kept, _ = tighten_mod.apply_transcript_evidence(
            [beat], utterances, words)
        self.assertEqual(kept[0].pick_attempt, 1)
        self.assertEqual(kept[0].attempts[1].verdict, "pick")
        self.assertEqual(kept[0].attempts[1].kind, "keeper")
        self.assertIn("(cleaner take: 0 vs 3 disfluencies)",
                      kept[0].attempts[1].reason)
        self.assertEqual(kept[0].attempts[0].verdict, "alternate")
        thin_words = [_w("well", 1.0, 1.2, "talent", "both", True),
                      _w("packed", 5.0, 5.4, "talent", "both")]
        thin_utterances = [tighten_mod.Utterance(0, 1.0, 1.2, "well",
                                                 0, 1, "talent", 1, 0),
                           tighten_mod.Utterance(1, 5.0, 5.4, "packed",
                                                 1, 2, "talent", 0, 0)]
        thin = cutlist.Beat(
            0, "the point", [
                cutlist.Attempt(0, [0], 1.0, 1.2, "well",
                                "pick", "keeper", "spark: lively", 0.9),
                cutlist.Attempt(1, [1], 5.0, 5.4, "packed",
                                "alternate", "alternate", "spark: flat",
                                0.5)],
            0, "kept", 3, "spark: lively")
        same, _ = tighten_mod.apply_transcript_evidence(
            [thin], thin_utterances, thin_words)
        self.assertEqual(same[0].pick_attempt, 0)
        self.assertAlmostEqual(same[0].attempts[0].confidence, 0.85)
        sole = cutlist.Beat(
            0, "the point",
            [cutlist.Attempt(0, [0], 1.0, 1.8, "well um so",
                             "pick", "keeper", "spark: kept", 0.9)],
            0, "kept", 3, "spark: kept")
        kept_sole, _ = tighten_mod.apply_transcript_evidence(
            [sole], utterances, words)
        self.assertEqual(kept_sole[0].pick_attempt, 0)
        self.assertEqual(kept_sole[0].attempts[0].verdict, "pick")
        self.assertAlmostEqual(kept_sole[0].attempts[0].confidence, 0.75)
        ruled = tighten_mod.rule_beats([
            tighten_mod.Utterance(0, 1.0, 2.0,
                                  "the timecode drifts between cameras.",
                                  0, 5, "talent", 2, 0),
            tighten_mod.Utterance(1, 3.0, 4.0,
                                  "the timecode drifts between cameras.",
                                  5, 10, "talent", 0, 0),
            tighten_mod.Utterance(2, 5.0, 6.0,
                                  "the timecode drifts between cameras.",
                                  10, 15, "other-1", 0, 0)])
        self.assertEqual(len(ruled), 1)
        self.assertEqual(ruled[0].pick_attempt, 1)
        blanked = tighten_mod.rule_beats([
            tighten_mod.Utterance(0, 1.0, 2.0,
                                  "the timecode drifts between cameras.",
                                  0, 5),
            tighten_mod.Utterance(1, 3.0, 4.0,
                                  "the timecode drifts between cameras.",
                                  5, 10),
            tighten_mod.Utterance(2, 5.0, 6.0,
                                  "the timecode drifts between cameras.",
                                  10, 15)])
        self.assertEqual(blanked[0].pick_attempt, 2)

    def test_muse_only_never_anchors_span(self) -> None:
        def _payload(word_end: int) -> dict:
            return {
                "version": 1, "utterances": [
                    {"id": 0, "start_s": 10.0, "end_s": 12.2,
                     "text": "alpha bravo", "word_start": 0,
                     "word_end": word_end}],
                "beats": [{"id": 0, "topic": "t", "status": "kept",
                           "keep_priority": 3, "pick_attempt": 0,
                           "reason": "test",
                           "attempts": [{"id": 0, "utterance_ids": [0],
                                         "start_s": 10.0, "end_s": 12.2,
                                         "text": "alpha bravo",
                                         "verdict": "pick", "kind": "keeper",
                                         "reason": "test",
                                         "confidence": 0.9}]}],
                "kept_spans": [], "removed_spans": [],
                "angle_overrides": [], "edits_applied": [],
                "span_exclusions": [], "editor_notes": [],
                "warnings": [],
            }

        with_filler = [_w("alpha", 10.0, 10.4),
                       _w("um", 11.0, 11.1, "talent", "muse", True),
                       _w("bravo", 11.82, 12.2)]
        payload = _payload(3)
        cutlist.recompute_spans(payload, with_filler, pause_limit_s=0.8)
        self.assertEqual(
            [(round(s["start_s"], 2), round(s["end_s"], 2))
             for s in payload["kept_spans"]],
            [(9.90, 10.58), (11.65, 12.35)])
        self.assertEqual(
            [s["kind"] for s in payload["removed_spans"]], ["pause_trim"])
        without_filler = [_w("alpha", 10.0, 10.4), _w("bravo", 11.82, 12.2)]
        bare = _payload(2)
        cutlist.recompute_spans(bare, without_filler, pause_limit_s=0.8)
        self.assertEqual(
            [(round(s["start_s"], 2), round(s["end_s"], 2))
             for s in bare["kept_spans"]],
            [(round(s["start_s"], 2), round(s["end_s"], 2))
             for s in payload["kept_spans"]])

    def test_tighten_run_with_enriched_words(self) -> None:
        point = "the timecode drifts between cameras."
        rig = "we packed the lighting rig."
        entries = []
        clock = 10.0

        def _sentence(text, speaker, fillers=()):
            nonlocal clock
            for pos, token in enumerate(text.split()):
                entries.append({"word": token, "start": round(clock, 3),
                                "end": round(clock + 0.3, 3),
                                "speaker": speaker, "heard_by": "both"})
                clock += 0.4
                if pos in fillers:
                    entries.append({"word": "um", "start": round(clock, 3),
                                    "end": round(clock + 0.1, 3),
                                    "speaker": speaker, "heard_by": "muse",
                                    "disfluency": True})
                    clock += 0.2
            clock += 0.8

        _sentence(point, "other-1")
        _sentence(point, "talent")
        clock = 50.0
        _sentence(rig, "talent", fillers=(1, 3))
        _sentence(rig, "talent")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            clip = tmp_path / "a.mp4"
            clip.touch()
            sync_path = tmp_path / "sync.json"
            write_json(sync_path, {
                "reference": str(clip),
                "methods": ["waveform"],
                "clips": [{"label": "A", "offset_s": 0.0,
                           "method": "reference",
                           "camera": {"path": str(clip), "fps": 30.0,
                                      "width": 0, "height": 0,
                                      "duration_s": 120.0,
                                      "has_audio": True}}]})
            words_path = tmp_path / "words.json"
            words_path.write_text(json.dumps({"words": entries}),
                                  encoding="utf-8")
            out_path = tmp_path / "tighten.json"
            payload = tighten_mod.run(
                sync_path=sync_path, words_path=words_path,
                out_path=out_path, audio_label="A", pause_limit_s=0.8,
                target_s=None, model="muse-spark-1.3", no_muse=True,
                max_usd=5.0)
        self.assertEqual(payload["transcript_speakers"],
                         ["talent", "other-1"])
        self.assertEqual(len(payload["beats"]), 2)
        first = payload["beats"][0]
        pick = first["attempts"][first["pick_attempt"]]
        self.assertEqual(pick["utterance_ids"], [1])
        removed = [a for a in first["attempts"]
                   if a["utterance_ids"] == [0]][0]
        self.assertEqual(removed["kind"], "crew_talk")
        self.assertEqual(removed["reason"], "speaker other-1 is not talent")
        second = payload["beats"][1]
        clean = second["attempts"][second["pick_attempt"]]
        self.assertEqual(clean["utterance_ids"], [3])
        utterance = payload["utterances"][2]
        self.assertEqual(utterance["speaker"], "talent")
        self.assertEqual(utterance["disfluency_count"], 2)
        self.assertEqual(utterance["muse_only_count"], 2)
        for span in payload["kept_spans"]:
            for uid in span["utterance_ids"]:
                self.assertNotEqual(
                    payload["utterances"][uid]["speaker"], "other-1")


class CliTests(unittest.TestCase):
    def test_cli_transcribe_parser_and_cut_flag(self) -> None:
        from director.sync import WHISPER_MODEL

        parser = cli_mod.build_parser()
        args = parser.parse_args(["cut", "a.mp4", "b.mp4"])
        self.assertFalse(args.no_transcribe)
        self.assertEqual(args.whisper_model, WHISPER_MODEL)
        flagged = parser.parse_args(
            ["cut", "a.mp4", "b.mp4", "--no-transcribe"])
        self.assertTrue(flagged.no_transcribe)
        sync_args = parser.parse_args(["sync", "a.mp4", "b.mp4"])
        self.assertEqual(sync_args.whisper_model, WHISPER_MODEL)
        transcribe_args = parser.parse_args(
            ["transcribe", "--sync", "sync.json", "--words", "whisper.json",
             "--out", "words.json"])
        self.assertEqual(transcribe_args.max_usd, 2.00)
        self.assertIsNone(transcribe_args.transcript)
        with mock.patch.object(cli_mod, "require_ffmpeg_binaries",
                               lambda: None), \
                mock.patch("director.cut.run",
                           return_value={"out_dir": "o", "status": "complete",
                                         "spend_usd": 0.0, "stages": {},
                                         "audio": None, "tighten": None,
                                         "export": None,
                                         "qc": None}) as run_mock:
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                cli_mod.command_cut(flagged)
        _, kwargs = run_mock.call_args
        self.assertTrue(kwargs["no_transcribe"])
        self.assertEqual(kwargs["whisper_model"], WHISPER_MODEL)
        payload = {"method": "muse",
                   "speakers": [{"id": "talent", "duration_s": 540.12,
                                 "turns": 14}],
                   "alignment": {"matched": 790, "whisper_words": 812,
                                 "muse_only": 45, "whisper_only": 22,
                                 "matched_share": 0.9729},
                   "spend_usd": 0.0306, "warnings": []}
        with mock.patch.object(cli_mod, "require_ffmpeg_binaries",
                               lambda: None), \
                mock.patch("director.transcribe.run",
                           return_value=payload) as run_mock:
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                cli_mod.command_transcribe(transcribe_args)
        _, kwargs = run_mock.call_args
        self.assertEqual(kwargs["transcript_path"],
                         Path("words.json").parent / "transcript.json")
        lines = stream.getvalue().splitlines()
        speaker_at = next(i for i, line in enumerate(lines)
                          if line.startswith("talent:"))
        aligned_at = next(i for i, line in enumerate(lines)
                          if line.startswith("aligned "))
        self.assertLess(speaker_at, aligned_at)
        self.assertIn("talent: 540.1s, 14 turns", lines[speaker_at])
        self.assertIn("aligned 790/812 words (97.3%), 45 muse-only, "
                      "22 whisper-only, $0.0306", lines[aligned_at])

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_transcribe_extract_synthetic(self) -> None:
        import wave

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "tone.mp4"
            made = subprocess.run(
                ["ffmpeg", "-v", "error", "-y",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                 "-c:a", "aac", str(source)],
                capture_output=True, text=True)
            self.assertEqual(made.returncode, 0, msg=made.stderr)
            wav = tmp_path / "audio.wav"
            from ghostcut.ffmpeg_tools import run_command

            run_command(transcribe_mod.extract_wav_command(source, wav))
            with wave.open(str(wav), "rb") as handle:
                self.assertEqual(handle.getnchannels(), 1)
                self.assertEqual(handle.getsampwidth(), 2)
                self.assertEqual(handle.getframerate(), 16000)
                self.assertAlmostEqual(
                    handle.getnframes() / handle.getframerate(), 2.0,
                    delta=0.1)
            chunks = transcribe_mod.plan_chunks(2.0, [])
            self.assertEqual(len(chunks), 1)


if __name__ == "__main__":
    unittest.main()
