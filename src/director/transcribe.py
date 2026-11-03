"""Muse Voice Transcribe as the transcript of record.

Local Whisper writes what the speaker meant: it smooths repeats, fillers
and half-started words out of the text, and it has no idea who is talking.
Meta's Voice Transcribe keeps disfluencies verbatim and labels speakers,
but returns turn-level times only. Neither engine alone is the transcript
Director needs, so this module aligns them: the production-audio clip is
transcribed with Voice Transcribe (16 kHz mono WAV, chunked on silence,
speakers reconciled across chunks) and Muse's tokens are mapped onto
Whisper's timed words with a tolerant sequence alignment. Whisper's word
times stay the clock; Muse's text and speakers are the record.

Designed by Muse Code on muse-wonder-1.0, built by Muse Code on
muse-spark-1.3 from docs/muse-code/TRANSCRIBE-DESIGN.md.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from ghostcut.config import write_json
from ghostcut.ffmpeg_tools import ToolingError, run_command

from . import angles as angles_mod

TRANSCRIBE_MODEL = "muse-voice-transcribe-1.0"
TRANSCRIBE_SR = 16000
CHUNK_TARGET_S = 180.0
CHUNK_MIN_S = 60.0
CHUNK_MAX_S = 300.0
CHUNK_SEARCH_S = 45.0
CHUNK_PAUSE_S = 0.6
SILENCE_NOISE_DB = "-40dB"
SILENCE_MIN_S = 0.40
CHUNK_OVERLAP_S = 8.0
SPEAKER_MATCH_MIN_OVERLAP_S = 0.30
SPEAKER_MATCH_MIN_SIM = 0.60
ALIGN_MIN_SHARE = 0.40
ALIGN_WARN_SHARE = 0.70
TRANSCRIBE_PER_SECOND_USD = 0.18 / 3600
FILLER_TOKENS = frozenset({"um", "uh", "umm", "uhh", "erm", "er",
                           "ah", "hmm", "mm", "mhm"})
MAIN_SPEAKER = "talent"


class TranscribeError(RuntimeError):
    """A transcribe step failed in a way the operator must see.

    The message always starts with 'transcribe: '.
    """


class MuseUnavailable(RuntimeError):
    """guestlist (the Meta API client) cannot be imported."""


@dataclass(slots=True)
class Chunk:
    id: int
    core_start_s: float
    core_end_s: float
    start_s: float
    end_s: float
    spend_usd: float = 0.0


@dataclass(slots=True)
class RawTurn:
    chunk_id: int
    raw_speaker: str
    start_s: float
    end_s: float
    text: str


@dataclass(slots=True)
class MuseTurn:
    id: int
    start_s: float
    end_s: float
    speaker: str
    raw_speaker: str
    text: str
    chunk_id: int


@dataclass(slots=True)
class MuseToken:
    text: str
    norm: str
    turn_id: int
    speaker: str


@dataclass(slots=True)
class AlignStats:
    whisper_words: int
    muse_tokens: int
    matched: int
    muse_only: int
    whisper_only: int
    matched_share: float


@dataclass(slots=True)
class TranscribeResult:
    method: str
    words: list[angles_mod.TimedWord]
    turns: list[MuseTurn]
    speakers: list[dict]
    chunks: list[Chunk]
    stats: AlignStats | None
    spend_usd: float
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------- normalisation (§7.1)


def normalize_token(text: str) -> str:
    """Lower-case, strip edge punctuation, fold apostrophes. Pure."""
    lowered = (text or "").lower()
    start, end = 0, len(lowered)
    while start < end and not (
            lowered[start].isalnum() or lowered[start] == "'"):
        start += 1
    while end > start and not (
            lowered[end - 1].isalnum() or lowered[end - 1] == "'"):
        end -= 1
    return lowered[start:end].strip("'")


def tokenize_turn(text: str) -> list[str]:
    """Whitespace split, empties dropped. Pure."""
    return (text or "").split()


# ------------------------------------------------------- ffmpeg commands (§6)


def extract_wav_command(src: Path, wav: Path) -> list[str]:
    """The clip to the 16 kHz mono WAV `diarize` requires. Pure."""
    return ["ffmpeg", "-v", "error", "-y", "-i", str(src),
            "-vn", "-ac", "1", "-ar", str(TRANSCRIBE_SR),
            "-c:a", "pcm_s16le", str(wav)]


def chunk_wav_command(full_wav: Path, start_s: float,
                      duration_s: float, out: Path) -> list[str]:
    """One chunk's WAV, cut from the full WAV. Pure."""
    return ["ffmpeg", "-v", "error", "-y",
            "-ss", f"{start_s:.3f}", "-i", str(full_wav),
            "-t", f"{duration_s:.3f}",
            "-ac", "1", "-ar", str(TRANSCRIBE_SR),
            "-c:a", "pcm_s16le", str(out)]


def silence_detect_command(wav: Path) -> list[str]:
    """Report the WAV's silences on stderr. Pure."""
    return ["ffmpeg", "-v", "info", "-i", str(wav),
            "-af", f"silencedetect=noise={SILENCE_NOISE_DB}:d={SILENCE_MIN_S}",
            "-f", "null", "-"]


def parse_silence_spans(stderr: str, duration_s: float) -> list[tuple[float, float]]:
    """silencedetect stderr to silence spans; garbage gives []. Pure."""
    spans: list[tuple[float, float]] = []
    pending: float | None = None
    for match in re.finditer(r"silence_(start|end):\s*([0-9]+(?:\.[0-9]+)?)",
                             stderr or ""):
        kind, value = match.group(1), float(match.group(2))
        if kind == "start":
            if pending is not None:
                spans.append((pending, duration_s))
            pending = value
        elif pending is not None:
            spans.append((pending, value))
            pending = None
    if pending is not None:
        spans.append((pending, duration_s))
    return spans


def pauses_from_words(words: list[angles_mod.TimedWord],
                      min_gap_s: float = CHUNK_PAUSE_S
                      ) -> list[tuple[float, float]]:
    """Inter-word gaps >= min_gap_s as (prev.end, next.start) pauses. Pure."""
    out: list[tuple[float, float]] = []
    for prev, nxt in zip(words, words[1:]):
        if nxt.start_s - prev.end_s >= min_gap_s:
            out.append((prev.end_s, nxt.start_s))
    return out


def _merged_pauses(silences: list[tuple[float, float]],
                   word_pauses: list[tuple[float, float]],
                   whisper_words: list[angles_mod.TimedWord]
                   ) -> list[tuple[float, float]]:
    """The pause list the planner sees: silences that swallow no word
    (trimmed 0.10 s per edge) plus word gaps overlapping no kept silence."""
    kept: list[tuple[float, float]] = []
    for start, end in silences:
        if any(word.start_s < end and word.end_s > start
               for word in whisper_words):
            continue
        trimmed = (start + 0.10, end - 0.10)
        if trimmed[0] < trimmed[1]:
            kept.append(trimmed)
    for start, end in word_pauses:
        if not any(start < kept_end and end > kept_start
                   for kept_start, kept_end in kept):
            kept.append((start, end))
    return sorted(set(kept))


# ------------------------------------------------------- chunk planning (§6.2)


def plan_chunks(duration_s: float, pauses: list[tuple[float, float]]) -> list[Chunk]:
    """Cores tiling [0, duration_s]; boundaries only inside pauses. Pure."""
    ordered = sorted(pauses)

    def _make(idx: int, core_start: float, core_end: float) -> Chunk:
        start = core_start if idx == 0 else max(
            0.0, core_start - CHUNK_OVERLAP_S)
        return Chunk(idx, core_start, core_end, start, core_end)

    if duration_s <= CHUNK_MAX_S:
        return [_make(0, 0.0, duration_s)]

    def _best(lo: float, hi: float, desired: float) -> tuple[float, float] | None:
        cands = [p for p in ordered if lo <= (p[0] + p[1]) / 2 <= hi]
        if not cands:
            return None
        return min(cands, key=lambda p: (-(p[1] - p[0]),
                                         abs((p[0] + p[1]) / 2 - desired),
                                         (p[0] + p[1]) / 2))

    chunks: list[Chunk] = []
    core_start = 0.0
    while True:
        if duration_s - core_start <= CHUNK_MAX_S:
            chunks.append(_make(len(chunks), core_start, duration_s))
            break
        desired = core_start + CHUNK_TARGET_S
        found = _best(desired - CHUNK_SEARCH_S, desired + CHUNK_SEARCH_S,
                      desired)
        if found is None:
            found = _best(core_start + CHUNK_MIN_S,
                          core_start + CHUNK_MAX_S, desired)
        if found is None:
            later = [(p[0] + p[1]) / 2 for p in ordered
                     if (p[0] + p[1]) / 2 > core_start + CHUNK_MAX_S]
            if not later:
                chunks.append(_make(len(chunks), core_start, duration_s))
                break
            boundary = min(later)
        else:
            boundary = (found[0] + found[1]) / 2
        boundary = min(boundary, duration_s)
        chunks.append(_make(len(chunks), core_start, boundary))
        core_start = boundary
        if core_start >= duration_s:
            break
    if (len(chunks) > 1
            and chunks[-1].core_end_s - chunks[-1].core_start_s < CHUNK_MIN_S):
        chunks[-2].core_end_s = chunks[-1].core_end_s
        chunks[-2].end_s = chunks[-1].end_s
        chunks.pop()
    return chunks


def estimate_transcribe_usd(duration_s: float) -> float:
    """List-price estimate for transcribing duration_s of audio. Pure."""
    return duration_s * TRANSCRIBE_PER_SECOND_USD


# ------------------------------------------------------- the diarizer (§6.4)


def diarize_chunk(wav: Path) -> tuple[dict, float, bool]:
    """(result, spend_usd, cached). The only guestlist import."""
    try:
        from guestlist.meta import cache, pricing
        from guestlist.meta import transcribe as meta_transcribe
    except ImportError as error:
        raise MuseUnavailable(str(error)) from error
    request = {"mode": "DIARIZATION", "model": meta_transcribe.MODEL,
               "audioEncoding": "WAV"}
    key = cache.key(cache.sha256_file(wav), meta_transcribe.MODEL, request)
    cached = cache.get("transcribe", key) is not None
    result = meta_transcribe.diarize(wav)
    if cached:
        return result, 0.0, True
    seconds = float(result.get("audioDurationMs") or 0) / 1000.0
    return result, pricing.transcribe_cost(seconds), False


def _text_similarity(a: str, b: str) -> float:
    """difflib ratio over normalised token strings (the tighten measure)."""
    def _norm(text: str) -> str:
        return " ".join(token for token in
                        re.findall(r"[a-z0-9']+", text.lower())
                        if token not in ("um", "uh", "erm"))

    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


# ------------------------------------------------------- speaker reconciliation (§6.5)


def reconcile_speakers(chunk_turns: dict[int, list[RawTurn]],
                       chunks: list[Chunk]
                       ) -> tuple[list[MuseTurn], list[dict], list[str]]:
    """Chunk-local raw labels to public talent/other-N names. Pure."""
    warnings: list[str] = []
    ordered = list(chunks)
    if not ordered:
        return [], [], warnings
    mapping: dict[tuple[int, str], str] = {}
    prov_order: list[str] = []
    prov_duration: dict[str, float] = {}
    raw_audit: dict[str, list[str]] = {}

    def _new_prov() -> str:
        pid = f"G{len(prov_order)}"
        prov_order.append(pid)
        prov_duration[pid] = 0.0
        raw_audit[pid] = []
        return pid

    def _record(prov: str, raw: str) -> None:
        if raw not in raw_audit[prov]:
            raw_audit[prov].append(raw)

    def _label_stats(turns: list[RawTurn]) -> dict[str, list]:
        stats: dict[str, list] = {}
        for idx, turn in enumerate(turns):
            entry = stats.setdefault(turn.raw_speaker, [0.0, idx])
            entry[0] += max(0.0, turn.end_s - turn.start_s)
        return stats

    def _ranked_labels(turns: list[RawTurn]) -> list[str]:
        stats = _label_stats(turns)
        return [label for label, _ in sorted(
            stats.items(), key=lambda kv: (-kv[1][0], kv[1][1]))]

    def _kept(turn: RawTurn, chunk: Chunk, final: bool) -> bool:
        mid = (turn.start_s + turn.end_s) / 2
        if mid < chunk.core_start_s:
            return False
        if final:
            return mid <= chunk.core_end_s
        return mid < chunk.core_end_s

    first = ordered[0]
    for label in _ranked_labels(chunk_turns.get(first.id, [])):
        prov = _new_prov()
        mapping[(first.id, label)] = prov
        _record(prov, label)
    only = len(ordered) == 1
    for turn in chunk_turns.get(first.id, []):
        if _kept(turn, first, only):
            prov_duration[mapping[(first.id, turn.raw_speaker)]] += max(
                0.0, turn.end_s - turn.start_s)

    for pos in range(1, len(ordered)):
        chunk = ordered[pos]
        prev = ordered[pos - 1]
        cur_turns = chunk_turns.get(chunk.id, [])
        prev_turns = [t for t in chunk_turns.get(prev.id, [])
                      if _kept(t, prev, False)]
        overlap_start, overlap_end = chunk.start_s, chunk.core_start_s
        prev_in = [t for t in prev_turns
                   if t.start_s < overlap_end and t.end_s > overlap_start]
        cur_in = [t for t in cur_turns
                  if t.start_s < overlap_end and t.end_s > overlap_start]
        votes: dict[tuple[str, str], list] = {}
        for cur in cur_in:
            for old in prev_in:
                overlap = (min(cur.end_s, old.end_s)
                           - max(cur.start_s, old.start_s))
                if overlap < SPEAKER_MATCH_MIN_OVERLAP_S:
                    continue
                sim = _text_similarity(old.text, cur.text)
                if sim < SPEAKER_MATCH_MIN_SIM:
                    continue
                prov = mapping[(prev.id, old.raw_speaker)]
                entry = votes.setdefault((cur.raw_speaker, prov),
                                         [0.0, 0.0, cur.start_s])
                entry[0] += overlap
                entry[1] = max(entry[1], sim)
                entry[2] = min(entry[2], cur.start_s)
        if not votes:
            warnings.append(f"transcribe_speaker_fallback:{chunk.id}")
            existing = sorted(
                prov_order,
                key=lambda p: (-prov_duration[p], prov_order.index(p)))
            for rank, label in enumerate(_ranked_labels(cur_turns)):
                prov = existing[rank] if rank < len(existing) else _new_prov()
                mapping[(chunk.id, label)] = prov
                _record(prov, label)
        else:
            assigned: dict[str, str] = {}
            taken: set[str] = set()
            ranked = sorted(votes.items(),
                            key=lambda kv: (-kv[1][0], -kv[1][1], kv[1][2]))
            for (label, prov), _ in ranked:
                if label in assigned or prov in taken:
                    continue
                assigned[label] = prov
                taken.add(prov)
            for label in _ranked_labels(cur_turns):
                if label not in assigned:
                    assigned[label] = _new_prov()
            ordered_assign: list[tuple[str, str]] = []
            seen: set[str] = set()
            for (label, prov), _ in ranked:
                if assigned.get(label) == prov and label not in seen:
                    ordered_assign.append((label, prov))
                    seen.add(label)
            for label in _ranked_labels(cur_turns):
                if label not in seen:
                    ordered_assign.append((label, assigned[label]))
                    seen.add(label)
            for label, prov in ordered_assign:
                mapping[(chunk.id, label)] = prov
                _record(prov, label)
        final = pos == len(ordered) - 1
        for turn in cur_turns:
            if _kept(turn, chunk, final):
                prov_duration[mapping[(chunk.id, turn.raw_speaker)]] += max(
                    0.0, turn.end_s - turn.start_s)

    kept: list[tuple[RawTurn, str]] = []
    for pos, chunk in enumerate(ordered):
        final = pos == len(ordered) - 1
        for turn in chunk_turns.get(chunk.id, []):
            if _kept(turn, chunk, final):
                kept.append((turn, mapping[(chunk.id, turn.raw_speaker)]))
    kept.sort(key=lambda pair: pair[0].start_s)
    totals: dict[str, float] = {}
    first_seen: dict[str, float] = {}
    for turn, prov in kept:
        totals[prov] = totals.get(prov, 0.0) + max(
            0.0, turn.end_s - turn.start_s)
        first_seen.setdefault(prov, turn.start_s)
    ranked_provs = sorted(totals,
                          key=lambda p: (-totals[p], first_seen[p]))
    public = {prov: (MAIN_SPEAKER if rank == 0 else f"other-{rank}")
              for rank, prov in enumerate(ranked_provs)}
    turns = [MuseTurn(pos, turn.start_s, turn.end_s, public[prov],
                      turn.raw_speaker, turn.text, turn.chunk_id)
             for pos, (turn, prov) in enumerate(kept)]
    speakers = [{"id": public[prov],
                 "raw_speakers": list(raw_audit[prov]),
                 "duration_s": totals[prov],
                 "turns": sum(1 for _, p in kept if p == prov),
                 "words": 0}
                for prov in ranked_provs]
    return turns, speakers, warnings


def speaker_for_time(turns: list[MuseTurn], t_s: float) -> str:
    """Public speaker of the turn a moment falls in; "" when none is near."""
    for turn in turns:
        if turn.start_s <= t_s <= turn.end_s:
            return turn.speaker
    best: MuseTurn | None = None
    best_dist = 0.5
    for turn in turns:
        if t_s < turn.start_s:
            dist = turn.start_s - t_s
        else:
            dist = t_s - turn.end_s
        if dist <= 0.5 + 1e-9 and (best is None or dist < best_dist - 1e-9):
            best, best_dist = turn, dist
    return best.speaker if best is not None else ""


# ------------------------------------------------------- alignment (§7)


def _parse_number_phrase(norms: list[str], i: int) -> tuple[str, int] | None:
    """A number starting at i as (canonical digits, tokens consumed)."""
    if i < 0 or i >= len(norms):
        return None
    token = norms[i] or ""
    if re.fullmatch(r"\d[\d,]*(\.\d+)?", token):
        stripped = token.replace(",", "")
        if "." in stripped:
            return stripped, 1
        return str(int(stripped)), 1
    ones = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
            "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
            "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
            "eighteen": 18, "nineteen": 19}
    tens = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
            "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
    digits = {"zero": "0", "one": "1", "two": "2", "three": "3",
              "four": "4", "five": "5", "six": "6", "seven": "7",
              "eight": "8", "nine": "9"}
    exp: list[str] = []
    owner: list[int] = []
    for pos in range(i, len(norms)):
        for part in (norms[pos] or "").split("-"):
            exp.append(part)
            owner.append(pos)
    total, current = 0, 0
    started = False
    tens_seen = False
    group_closed = False
    at = 0
    consumed = 0
    while at < len(exp):
        word = exp[at]
        if word in ones:
            if group_closed:
                break
            if tens_seen and ones[word] >= 10:
                break
            current += ones[word]
            started = True
            group_closed = True
            at += 1
            consumed = at
        elif word in tens:
            if group_closed or tens_seen:
                break
            current += tens[word]
            started = True
            tens_seen = True
            at += 1
            consumed = at
        elif word == "hundred":
            if not started or current == 0:
                break
            current *= 100
            tens_seen = False
            group_closed = False
            at += 1
            consumed = at
        elif word in ("thousand", "million"):
            scale = 1000 if word == "thousand" else 1000000
            total += (current or 1) * scale
            current = 0
            started = True
            tens_seen = False
            group_closed = False
            at += 1
            consumed = at
        elif word == "point":
            digits_out = ""
            end = at + 1
            while end < len(exp):
                piece = exp[end]
                if piece in digits:
                    digits_out += digits[piece]
                elif re.fullmatch(r"\d", piece or ""):
                    digits_out += piece
                else:
                    break
                end += 1
            if not digits_out:
                break
            return f"{total + current}.{digits_out}", owner[end - 1] - i + 1
        else:
            break
    if not started:
        return None
    return str(total + current), owner[consumed - 1] - i + 1


def _number_units(norms: list[str]) -> list[tuple[str, int, int]]:
    """A norm list grouped into (key, start, end) units. Pure."""
    units: list[tuple[str, int, int]] = []
    i = 0
    while i < len(norms):
        parsed = _parse_number_phrase(norms, i)
        if parsed is not None:
            key, count = parsed
            units.append((key, i, i + count))
            i += count
        else:
            units.append((norms[i], i, i + 1))
            i += 1
    return units


def _is_disfluency(norm: str, prev_norm: str | None) -> bool:
    """A filler, or an immediate repeat of the previous Muse token."""
    if norm in FILLER_TOKENS:
        return True
    return bool(norm) and norm == prev_norm


def _subdivide(tokens: list[str], start: float,
               end: float) -> list[tuple[float, float]]:
    """Split [start, end] among tokens, weighted by len + 1. Pure."""
    weights = [len(token) + 1 for token in tokens]
    total = sum(weights)
    out: list[tuple[float, float]] = []
    edge = start
    acc = 0
    for pos, weight in enumerate(weights):
        acc += weight
        if pos == len(weights) - 1:
            out.append((edge, end))
        else:
            nxt = start + (end - start) * acc / total
            out.append((edge, nxt))
            edge = nxt
    return out


def align_words(whisper_words: list[angles_mod.TimedWord],
                turns: list[MuseTurn]
                ) -> tuple[list[angles_mod.TimedWord], AlignStats]:
    """Muse's tokens on Whisper's clock, in emission order. Pure."""
    kept_whisper: list[tuple[str, angles_mod.TimedWord]] = []
    for word in whisper_words:
        norm = normalize_token(word.text)
        if norm:
            kept_whisper.append((norm, word))
    whisper_units: list[tuple[str, list[angles_mod.TimedWord], float, float]] = []
    for key, start, end in _number_units([n for n, _ in kept_whisper]):
        span = [w for _, w in kept_whisper[start:end]]
        whisper_units.append((key, span, span[0].start_s, span[-1].end_s))
    turn_tokens: list[tuple[list[str], list[str]]] = []
    for turn in turns:
        raw = tokenize_turn(turn.text)
        turn_tokens.append((raw, [normalize_token(t) for t in raw]))
    flat = [norm for _, norms in turn_tokens for norm in norms]
    muse_units: list[tuple[str, list[tuple[str, str, str | None]], MuseTurn]] = []
    base = 0
    for turn, (raw, norms) in zip(turns, turn_tokens):
        kept_idx = [i for i, norm in enumerate(norms) if norm]
        kept_norms = [norms[i] for i in kept_idx]
        for key, start, end in _number_units(kept_norms):
            tokens: list[tuple[str, str, str | None]] = []
            for at in range(start, end):
                global_pos = base + kept_idx[at]
                prev = flat[global_pos - 1] if global_pos > 0 else None
                tokens.append((raw[kept_idx[at]], kept_norms[at], prev))
            muse_units.append((key, tokens, turn))
        base += len(norms)

    matched = 0
    muse_only_units = 0
    whisper_only_units = 0
    items: list = []
    matcher = difflib.SequenceMatcher(
        a=[k for k, _, _, _ in whisper_units],
        b=[k for k, _, _ in muse_units], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            matched += i2 - i1
            for wi, mi in zip(range(i1, i2), range(j1, j2)):
                items.append(("pair", wi, mi))
        elif tag == "replace":
            pairs = min(i2 - i1, j2 - j1)
            matched += pairs
            for at in range(pairs):
                items.append(("pair", i1 + at, j1 + at))
            for wi in range(i1 + pairs, i2):
                whisper_only_units += 1
                items.append(("whisper", wi))
            for mi in range(j1 + pairs, j2):
                muse_only_units += 1
                items.append(("muse", mi))
        elif tag == "delete":
            for wi in range(i1, i2):
                whisper_only_units += 1
                items.append(("whisper", wi))
        else:
            for mi in range(j1, j2):
                muse_only_units += 1
                items.append(("muse", mi))

    def _pair_words(wi: int, mi: int) -> list[angles_mod.TimedWord]:
        _, _, start, end = whisper_units[wi]
        _, tokens, _ = muse_units[mi]
        speaker = speaker_for_time(turns, (start + end) / 2)
        return [angles_mod.TimedWord(
            text, slice_start, slice_end, speaker, "both",
            _is_disfluency(norm, prev))
            for (text, norm, prev), (slice_start, slice_end) in zip(
                tokens, _subdivide([t for t, _, _ in tokens], start, end))]

    def _whisper_words(wi: int) -> list[angles_mod.TimedWord]:
        _, span, _, _ = whisper_units[wi]
        return [angles_mod.TimedWord(
            word.text, word.start_s, word.end_s,
            speaker_for_time(turns, (word.start_s + word.end_s) / 2),
            "whisper", False) for word in span]

    resolved: list = []
    for item in items:
        if item[0] == "pair":
            resolved.extend(("anchor", w) for w in _pair_words(item[1], item[2]))
        elif item[0] == "whisper":
            resolved.extend(("anchor", w) for w in _whisper_words(item[1]))
        else:
            _, tokens, turn = muse_units[item[1]]
            resolved.extend(("muse", token, turn) for token in tokens)
    out: list[angles_mod.TimedWord] = []
    pos = 0
    while pos < len(resolved):
        if resolved[pos][0] == "anchor":
            out.append(resolved[pos][1])
            pos += 1
            continue
        end = pos
        while end < len(resolved) and resolved[end][0] == "muse":
            end += 1
        run = [(token, turn) for _, token, turn in resolved[pos:end]]
        if pos > 0:
            lo = out[-1].end_s
        else:
            lo = run[0][1].start_s
        nxt = end
        while nxt < len(resolved) and resolved[nxt][0] != "anchor":
            nxt += 1
        if nxt < len(resolved):
            hi = max(resolved[nxt][1].start_s, lo)
        else:
            hi = max(run[-1][1].end_s, lo)
        total = len(run)
        for rank, ((text, norm, prev), turn) in enumerate(run):
            slice_start = lo + (hi - lo) * rank / total
            slice_end = lo + (hi - lo) * (rank + 1) / total
            out.append(angles_mod.TimedWord(
                text, slice_start, slice_end, turn.speaker, "muse",
                _is_disfluency(norm, prev)))
        pos = end
    stats = AlignStats(
        whisper_words=len(whisper_units), muse_tokens=len(muse_units),
        matched=matched, muse_only=muse_only_units,
        whisper_only=whisper_only_units,
        matched_share=(matched / len(whisper_units)
                       if whisper_units else 0.0))
    return out, stats


# ------------------------------------------------------- payload (§5.2)


def build_transcript_payload(*, audio_label: str, clip_path: str,
                             offset_s: float, duration_s: float,
                             result: TranscribeResult) -> dict:
    """The transcript.json payload from a timeline-shifted result. Pure."""
    counts: dict[str, int] = {}
    for word in result.words:
        if word.speaker:
            counts[word.speaker] = counts.get(word.speaker, 0) + 1
    turns_by_chunk: dict[int, int] = {}
    for turn in result.turns:
        turns_by_chunk[turn.chunk_id] = turns_by_chunk.get(turn.chunk_id, 0) + 1
    if result.stats is None:
        alignment = None
    else:
        alignment = {
            "whisper_words": result.stats.whisper_words,
            "muse_tokens": result.stats.muse_tokens,
            "matched": result.stats.matched,
            "muse_only": result.stats.muse_only,
            "whisper_only": result.stats.whisper_only,
            "matched_share": round(result.stats.matched_share, 4)}
    return {
        "version": 1,
        "audio": {"label": audio_label, "clip_path": clip_path,
                  "offset_s": round(offset_s, 3)},
        "method": result.method,
        "model": TRANSCRIBE_MODEL,
        "duration_s": round(duration_s, 3),
        "spend_usd": round(result.spend_usd, 4),
        "speakers": [{
            "id": entry["id"],
            "raw_speakers": list(entry["raw_speakers"]),
            "duration_s": round(entry["duration_s"], 3),
            "turns": entry["turns"],
            "words": counts.get(entry["id"], 0)} for entry in result.speakers],
        "chunks": [{
            "id": chunk.id,
            "core_start_s": round(chunk.core_start_s + offset_s, 3),
            "core_end_s": round(chunk.core_end_s + offset_s, 3),
            "start_s": round(chunk.start_s + offset_s, 3),
            "end_s": round(chunk.end_s + offset_s, 3),
            "turns": turns_by_chunk.get(chunk.id, 0),
            "spend_usd": round(chunk.spend_usd, 4)}
            for chunk in result.chunks],
        "turns": [{
            "id": turn.id, "start_s": round(turn.start_s, 3),
            "end_s": round(turn.end_s, 3), "speaker": turn.speaker,
            "raw_speaker": turn.raw_speaker, "text": turn.text,
            "chunk_id": turn.chunk_id} for turn in result.turns],
        "alignment": alignment,
        "warnings": list(result.warnings),
    }


# ------------------------------------------------------- orchestrator (§6–§7)


def transcribe_audio_clip(*, clip_path: Path,
                          whisper_words: list[angles_mod.TimedWord],
                          duration_s: float, work_dir: Path,
                          max_usd: float) -> TranscribeResult:
    """Extract, chunk, diarize, reconcile, align — in clip-local time.

    Never raises for a Meta-side failure: those return a method="whisper"
    result with the §12 warning. Raises TranscribeError only when no
    audio could be extracted or the clip has no duration.
    """
    warnings: list[str] = []

    def _fallback(reason: str) -> TranscribeResult:
        return TranscribeResult(
            method="whisper", words=whisper_words, turns=[], speakers=[],
            chunks=[], stats=None, spend_usd=0.0,
            warnings=[*warnings, reason])

    if duration_s <= 0:
        raise TranscribeError("transcribe: audio clip has no duration")
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    estimate = estimate_transcribe_usd(duration_s)
    if estimate > max_usd:
        return _fallback(
            f"transcribe_over_budget:est ${estimate:.2f} over "
            f"remaining ${max_usd:.2f}")
    full_wav = work_dir / "audio.wav"
    try:
        run_command(extract_wav_command(Path(clip_path), full_wav))
    except (ToolingError, OSError) as error:
        raise TranscribeError(
            "transcribe: no audio could be extracted from "
            f"{Path(clip_path).name}") from error
    if not full_wav.exists():
        raise TranscribeError(
            "transcribe: no audio could be extracted from "
            f"{Path(clip_path).name}")
    try:
        proc = run_command(silence_detect_command(full_wav), check=False)
        silences = parse_silence_spans(proc.stderr or "", duration_s)
    except OSError:
        silences = []
    if not silences:
        warnings.append("transcribe_silence_fallback:words")
    pauses = _merged_pauses(silences, pauses_from_words(whisper_words),
                            whisper_words)
    chunks = plan_chunks(duration_s, pauses)
    chunk_turns: dict[int, list[RawTurn]] = {}
    chunk_spends: dict[int, float] = {}
    spend = 0.0
    try:
        for chunk in chunks:
            chunk_wav = work_dir / f"chunk-{chunk.id:03d}.wav"
            run_command(chunk_wav_command(
                full_wav, chunk.start_s, chunk.end_s - chunk.start_s,
                chunk_wav))
            diarized, chunk_spend, _cached = diarize_chunk(chunk_wav)
            chunk_spends[chunk.id] = chunk_spend
            spend += chunk_spend
            raws: list[RawTurn] = []
            for turn in diarized.get("turns", []) or []:
                if not isinstance(turn, dict):
                    continue
                try:
                    text = str(turn.get("transcript") or "").strip()
                    start = (chunk.start_s
                             + float(turn.get("startMs") or 0) / 1000.0)
                    end = (chunk.start_s
                           + float(turn.get("endMs") or 0) / 1000.0)
                except (TypeError, ValueError):
                    continue
                if not text or end <= start:
                    continue
                raws.append(RawTurn(chunk.id, str(turn.get("speaker") or ""),
                                    start, end, text))
            chunk_turns[chunk.id] = raws
    except MuseUnavailable as error:
        return _fallback(f"transcribe_unavailable:{error}")
    except Exception as error:
        return _fallback(
            f"transcribe_failed:{type(error).__name__}: {str(error)[:160]}")
    if not any(chunk_turns.values()):
        return _fallback("transcribe_no_turns")
    muse_turns, speakers, rec_warnings = reconcile_speakers(chunk_turns,
                                                            chunks)
    warnings.extend(rec_warnings)
    aligned, stats = align_words(whisper_words, muse_turns)
    if stats.matched_share < ALIGN_MIN_SHARE:
        return TranscribeResult(
            method="whisper", words=whisper_words, turns=[], speakers=[],
            chunks=[], stats=None, spend_usd=0.0,
            warnings=[*warnings,
                      f"transcribe_alignment_low:{stats.matched_share:.2f}"])
    if stats.matched_share < ALIGN_WARN_SHARE:
        warnings.append(f"transcribe_alignment_low:{stats.matched_share:.2f}")
    spent_chunks = [replace(chunk, spend_usd=chunk_spends.get(chunk.id, 0.0))
                    for chunk in chunks]
    counts: dict[str, int] = {}
    for word in aligned:
        if word.speaker:
            counts[word.speaker] = counts.get(word.speaker, 0) + 1
    for entry in speakers:
        entry["words"] = counts.get(entry["id"], 0)
    return TranscribeResult(
        method="muse", words=aligned, turns=muse_turns, speakers=speakers,
        chunks=spent_chunks, stats=stats, spend_usd=spend, warnings=warnings)


# ------------------------------------------------------- standalone (§11.2)


def run(*, sync_path: Path, words_path: Path, out_path: Path,
        transcript_path: Path, audio_label: str | None, max_usd: float,
        no_muse: bool) -> dict:
    """Transcribe one recording by hand; the words bridge as a command.

    Returns the §5.2 payload (written to transcript_path on success), or
    the in-memory whisper equivalent while writing only the words file.
    """
    sync_path, words_path, out_path, transcript_path = (
        Path(sync_path), Path(words_path), Path(out_path),
        Path(transcript_path))
    reference, clips = angles_mod.load_sync(sync_path)
    by_label = {clip.label: clip for clip in clips}
    if audio_label is not None:
        audio = by_label.get(audio_label)
        if audio is None:
            raise ValueError(f"Unknown --audio label: {audio_label}")
    else:
        audio = next((c for c in clips if c.path == reference), None)
        if audio is None:
            audio = by_label.get("A")
        if audio is None and clips:
            audio = clips[0]
    if audio is None:
        raise ValueError("sync.json has no clips")
    if audio.offset_s is None:
        raise ValueError(f"transcribe: audio clip {audio.label} is not "
                         f"placed on the timeline")
    offset = float(audio.offset_s)
    whisper_words = angles_mod.load_timed_words(words_path)

    def _entry(word: angles_mod.TimedWord) -> dict:
        entry = {"word": word.text, "start": round(word.start_s, 3),
                 "end": round(word.end_s, 3)}
        if word.speaker:
            entry["speaker"] = word.speaker
        if word.speaker or word.heard_by != "whisper":
            entry["heard_by"] = word.heard_by
        if word.disfluency:
            entry["disfluency"] = True
        return entry

    def _shift(words: list[angles_mod.TimedWord]
               ) -> list[angles_mod.TimedWord]:
        return [angles_mod.TimedWord(
            word.text, word.start_s + offset, word.end_s + offset,
            word.speaker, word.heard_by, word.disfluency) for word in words]

    if no_muse:
        shifted = _shift(whisper_words)
        write_json(out_path, {"words": [_entry(w) for w in shifted]})
        return build_transcript_payload(
            audio_label=audio.label, clip_path=audio.path, offset_s=offset,
            duration_s=audio.duration_s,
            result=TranscribeResult(method="whisper", words=shifted, turns=[],
                                    speakers=[], chunks=[], stats=None,
                                    spend_usd=0.0, warnings=[]))
    result = transcribe_audio_clip(
        clip_path=Path(audio.path), whisper_words=whisper_words,
        duration_s=audio.duration_s, work_dir=out_path.parent / "transcribe",
        max_usd=max_usd)
    shifted_words = _shift(result.words)
    write_json(out_path, {"words": [_entry(w) for w in shifted_words]})
    if result.method != "muse":
        return build_transcript_payload(
            audio_label=audio.label, clip_path=audio.path, offset_s=offset,
            duration_s=audio.duration_s,
            result=replace(result, words=shifted_words))
    shifted_result = replace(
        result, words=shifted_words,
        turns=[replace(turn, start_s=turn.start_s + offset,
                       end_s=turn.end_s + offset) for turn in result.turns])
    payload = build_transcript_payload(
        audio_label=audio.label, clip_path=audio.path, offset_s=offset,
        duration_s=audio.duration_s, result=shifted_result)
    write_json(transcript_path, payload)
    return payload
