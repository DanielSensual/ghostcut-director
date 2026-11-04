"""Dress the cut: title cards, ElevenLabs music and a ghost that pops.

`director dress plan` (network) writes `dress.json` and caches assets;
`director dress render` (ffmpeg only, offline) writes `cut.dressed.mp4`.
It never touches `cut.mp4`, `cut.clean.mp4`, `finish.json` or
`angles.json`.

Planning is one Muse Spark call over timed caption lines; mechanical
checks (`check_card`) decide. Cards are ASS pill overlays appended to
the horizontal captions, the ghost is a Pillow pop sequence from the
owner's brand icon, and the mix ducks only the music bed under the
voice (bing/pop sit beside it, never ducked).

Built by Muse Code on muse-wonder-1.0 from
docs/muse-code/DRESS-BRIEF.md, after the owner's review of the app on
submission day; Muse's run stopped when its Meta API credit ran out.
Finished, fixed and tested by Claude Code (Opus), which added the
built-in planner used when Muse is unavailable and the note grammar.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ghostcut.config import read_json, write_json
from ghostcut.ffmpeg_tools import (
    ToolingError,
    extract_media_stats,
    probe_media,
    run_command,
)

from . import captions as captions_mod
from . import cutlist
from . import elevenlabs as elevenlabs_mod
from . import finish as finish_mod

try:
    import numpy as np
except ImportError:  # the director extra is optional
    np = None  # type: ignore[assignment]

VERSION = 1
DEFAULT_MODEL = "muse-spark-1.3"
MAX_OUTPUT_TOKENS = 6000
TITLE_END_S = captions_mod.TITLE_END_S  # 3.5
FIRST_CARD_S = TITLE_END_S + 0.3  # 3.8
DEFAULT_MUSIC_PROMPT = (
    "Minimal modern documentary tech underscore, soft synth pads, "
    "light pulse, optimistic, no vocals, steady 95 bpm")
BING_PROMPT = "single bright glass bell ping, cheerful magical sparkle, clean"
POP_PROMPT = "soft cartoon pop"
BING_DURATION_S = 1.0
POP_DURATION_S = 0.5
BING_GAIN_DB = -10.0
POP_GAIN_DB = -16.0
MUSIC_GAIN_DEFAULT = -20.0
LIMITS = {"max_cards": 8, "min_gap_s": 0.6, "min_card_s": 2.0,
          "max_card_s": 3.2}
CARD_PATHLIKE = re.compile(r"(Users|private|Volumes|tmp|var)/|\.(sh|py)\b")
DEFAULT_MAX_PROVIDER_USD = 1.00

DEVELOPER = (
    "You propose title cards for a finished talking-head cut. You are "
    "given timed caption lines from the cut. Return only JSON matching "
    "the schema; every other byte is discarded.\n"
    "Propose 4 to 8 cards. Each card text is 2 to 6 words, sentence "
    "case, no end punctuation. Each quote is 2 to 6 contiguous spoken "
    "words copied exactly from the transcript. No numbers, names or "
    "claims that are not spoken. Cards label what the speaker is "
    "saying at that moment; prefer concrete spoken phrases."
)

SPARK_RATES = {
    "muse-spark-1.3": (1.25, 4.25),
    "muse-wonder-1.0": (4.00, 20.00),
}

_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16",
    "seventeen": "17", "eighteen": "18", "nineteen": "19",
    "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50",
    "sixty": "60", "seventy": "70", "eighty": "80", "ninety": "90",
    "hundred": "100",
}
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "by",
    "for", "with", "is", "it", "at", "as", "be", "are", "was",
})


class DressError(RuntimeError):
    """A dress step failed. The message always starts with 'dress: '."""


class DressUnavailable(RuntimeError):
    """guestlist cannot be imported (the --no-muse degradation)."""


# ------------------------------------------------------------ files


def _stamp(path: Path) -> str:
    info = Path(path).stat()
    return f"{info.st_size}:{info.st_mtime_ns}"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    write_json(tmp, payload)
    os.replace(tmp, path)


def load_plan(job_dir: Path) -> dict | None:
    path = Path(job_dir) / "dress.json"
    if not path.exists():
        return None
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_plan(job_dir: Path, payload: dict) -> dict:
    _atomic_write_json(Path(job_dir) / "dress.json", payload)
    return payload


def source_video(job_dir: Path) -> Path:
    """The un-captioned source for the dressed cut. Pure-ish (stats files).

    Mirrors `captions._clean_source`: when `.cut.mp4.captioned` equals
    the stamp of `cut.mp4`, the captioned burn came from
    `cut.clean.mp4`, so dress from clean; otherwise `cut.mp4` itself is
    a fresh uncaptioned re-render and is the source.
    """
    job = Path(job_dir)
    cut = job / "cut.mp4"
    clean = job / "cut.clean.mp4"
    marker = job / ".cut.mp4.captioned"
    if cut.exists() and clean.exists() and marker.exists():
        try:
            if marker.read_text(encoding="utf-8").strip() == _stamp(cut):
                return clean
        except OSError:
            pass
    if cut.exists():
        return cut
    return clean


def _is_captioned(job_dir: Path) -> bool:
    job = Path(job_dir)
    cut = job / "cut.mp4"
    clean = job / "cut.clean.mp4"
    marker = job / ".cut.mp4.captioned"
    if not (cut.exists() and clean.exists() and marker.exists()):
        return False
    try:
        return marker.read_text(encoding="utf-8").strip() == _stamp(cut)
    except OSError:
        return False


def _probe_duration(path: Path) -> float | None:
    try:
        stats = extract_media_stats(probe_media(Path(path)))
    except (ToolingError, OSError, ValueError):
        return None
    try:
        duration = float(stats.get("duration_s") or 0.0)
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def _probe_size(path: Path) -> tuple[int, int] | None:
    try:
        stats = extract_media_stats(probe_media(Path(path)))
    except (ToolingError, OSError, ValueError):
        return None
    try:
        width = int(stats.get("width") or 0)
        height = int(stats.get("height") or 0)
    except (TypeError, ValueError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def _captions_payload(job_dir: Path) -> dict | None:
    path = Path(job_dir) / "captions.json"
    if not path.exists():
        return None
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _duration(job_dir: Path, source: Path | None = None) -> float:
    job = Path(job_dir)
    if source is not None and Path(source).exists():
        probed = _probe_duration(Path(source))
        if probed is not None:
            return probed
    caps = _captions_payload(job)
    if caps is not None:
        try:
            value = float(caps.get("duration_out_s") or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    angles_path = job / "angles.json"
    if angles_path.exists():
        try:
            data = read_json(angles_path)
            shots = data.get("shots", []) if isinstance(data, dict) else []
            origin = captions_mod.origin_from_shots(shots)
            dur = captions_mod.duration_from_shots(shots, origin)
            if dur:
                return float(dur)
        except (OSError, ValueError):
            pass
    return 0.0


def _title_info(job_dir: Path) -> tuple[dict, Path | None]:
    """(dress title block, horizontal title PNG or None)."""
    job = Path(job_dir)
    caps = _captions_payload(job)
    title = caps.get("title") if caps else None
    horizontal = job / "title-1920x1080.png"
    if isinstance(title, dict) and title and horizontal.exists():
        return ({"enabled": True, "file": horizontal.name,
                 "end_s": TITLE_END_S}, horizontal)
    if isinstance(title, dict) and title:
        name = str(title.get("file") or "")
        candidate = job / name if name else None
        if candidate is not None and candidate.exists():
            return ({"enabled": True, "file": name,
                     "end_s": TITLE_END_S}, candidate)
        return ({"enabled": False, "file": name, "end_s": TITLE_END_S},
                None)
    return ({"enabled": False, "file": "", "end_s": TITLE_END_S}, None)


def _plan_fingerprint(payload: dict) -> str:
    core = {
        "cards": payload.get("cards", []),
        "music": payload.get("music", {}),
        "ghost": payload.get("ghost", {}),
        "sfx": payload.get("sfx", {}),
        "title": payload.get("title", {}),
        "captions": payload.get("captions", {}),
    }
    blob = json.dumps(core, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def is_fresh(job_dir: Path, payload: dict | None = None) -> bool:
    """A dressed render exists for the current source and plan."""
    job = Path(job_dir)
    dressed = job / "cut.dressed.mp4"
    if not dressed.exists():
        return False
    plan = payload if payload is not None else load_plan(job)
    if plan is None:
        return False
    rendered = plan.get("rendered") or {}
    if not isinstance(rendered, dict) or not rendered.get("file"):
        return False
    source = source_video(job)
    if not source.exists():
        return False
    try:
        stamp = _stamp(source)
    except OSError:
        return False
    if rendered.get("source_stamp") != stamp:
        return False
    fingerprint = rendered.get("plan_fingerprint")
    if fingerprint and fingerprint != _plan_fingerprint(plan):
        return False
    return True


def is_stale(job_dir: Path, payload: dict | None = None) -> bool:
    job = Path(job_dir)
    if not (job / "cut.dressed.mp4").exists():
        return False
    return not is_fresh(job_dir, payload)


# ------------------------------------------------------------ words


@dataclass(slots=True)
class OutWord:
    text: str
    start_s: float
    end_s: float


def _word_text(word) -> str:  # noqa: ANN001
    if isinstance(word, dict):
        return str(word.get("text") or word.get("word") or "")
    return str(getattr(word, "text", "") or "")


def _word_times(word) -> tuple[float, float]:  # noqa: ANN001
    if isinstance(word, dict):
        try:
            return float(word.get("start_s", word.get("start", 0.0))), float(
                word.get("end_s", word.get("end", 0.0)))
        except (TypeError, ValueError):
            return 0.0, 0.0
    return float(getattr(word, "start_s", 0.0)), float(
        getattr(word, "end_s", 0.0))


def _as_out_words(words) -> list[OutWord]:  # noqa: ANN001
    out: list[OutWord] = []
    for word in words or []:
        text = _word_text(word).strip()
        if not text:
            continue
        start, end = _word_times(word)
        out.append(OutWord(text, start, end))
    out.sort(key=lambda w: (w.start_s, w.end_s))
    return out


def load_output_words(job_dir: Path) -> list[OutWord]:
    """Heard words on the output timeline (captions.json first)."""
    job = Path(job_dir)
    caps = _captions_payload(job)
    if caps is not None and isinstance(caps.get("lines"), list):
        flat: list[OutWord] = []
        for line in caps["lines"]:
            if not isinstance(line, dict):
                continue
            for word in line.get("words", []) or []:
                if not isinstance(word, dict):
                    continue
                text = str(word.get("text") or "").strip()
                if not text:
                    continue
                try:
                    flat.append(OutWord(
                        text, float(word.get("start_s", 0.0)),
                        float(word.get("end_s", 0.0))))
                except (TypeError, ValueError):
                    continue
        if flat:
            flat.sort(key=lambda w: (w.start_s, w.end_s))
            return flat
    # Rebuild exactly as captions.run does.
    words_path = job / "words.json"
    angles_path = job / "angles.json"
    if words_path.exists() and angles_path.exists():
        try:
            from . import angles as angles_mod

            words = angles_mod.load_timed_words(words_path)
            heard = captions_mod.filter_caption_words(words)
            angles_payload = read_json(angles_path)
            shots = (angles_payload.get("shots", [])
                     if isinstance(angles_payload, dict) else [])
            origin = captions_mod.origin_from_shots(shots)
            duration = captions_mod.duration_from_shots(shots, origin)
            tighten = None
            tighten_path = job / "tighten.json"
            if tighten_path.exists():
                try:
                    tighten = cutlist.load_tighten(tighten_path)
                except (OSError, ValueError):
                    tighten = None
            mapped = captions_mod.map_words_to_output(
                heard, tighten, origin, duration)
            return [OutWord(w.text, w.start_s, w.end_s) for w in mapped]
        except (OSError, ValueError):
            pass
    if words_path.exists():
        try:
            from . import angles as angles_mod

            return [OutWord(w.text, w.start_s, w.end_s)
                    for w in angles_mod.load_timed_words(words_path)]
        except (OSError, ValueError):
            return []
    return []


def load_caption_lines(job_dir: Path,
                       words: list[OutWord] | None = None) -> list[dict]:
    """Timed caption lines [{start_s, text}] for the Spark prompt."""
    job = Path(job_dir)
    caps = _captions_payload(job)
    if caps is not None and isinstance(caps.get("lines"), list):
        lines = []
        for line in caps["lines"]:
            if not isinstance(line, dict):
                continue
            try:
                start = float(line.get("start_s", 0.0))
            except (TypeError, ValueError):
                continue
            lines.append({"start_s": start,
                          "text": str(line.get("text", ""))})
        if lines:
            return lines
    out_words = words if words is not None else load_output_words(job)
    if not out_words:
        return []
    # Group with captions grouping over CaptionWord views.
    cap_words = [captions_mod.CaptionWord(w.text, w.start_s, w.end_s, 0)
                 for w in out_words]
    lines = captions_mod.group_lines(cap_words, [])
    return [{"start_s": line.start_s, "text": line.text}
            for line in lines]


def normalize_token(text: str) -> str:
    """Lower-case, punctuation stripped (voice.normalize_quote_token)."""
    return re.sub(r"[^a-z0-9']+", "", (text or "").lower()).strip("'")


_TOKEN_SPLIT_RE = re.compile(r"[-\u2010-\u2015]")


def _want_tokens(quote: str) -> list[str]:
    want: list[str] = []
    for chunk in (quote or "").split():
        for piece in _TOKEN_SPLIT_RE.split(chunk):
            token = normalize_token(piece)
            if token:
                want.append(token)
    return want


def _stream(words: list[OutWord]) -> list[tuple[str, int]]:
    stream: list[tuple[str, int]] = []
    for index, word in enumerate(words):
        for chunk in word.text.split():
            for piece in _TOKEN_SPLIT_RE.split(chunk):
                token = normalize_token(piece)
                if token:
                    stream.append((token, index))
    return stream


def locate_quote(quote: str, words) -> tuple[int, int] | None:  # noqa: ANN001
    """(word_start, word_end) of the first contiguous match, or None.

    Dress anchoring allows a single word ("synced"); voice anchoring
    requires two, a card anchor does not.
    """
    out_words = _as_out_words(words) if words and not isinstance(
        words[0], OutWord) else list(words or [])  # type: ignore[arg-type]
    # _as_out_words loses identity for OutWord input; handle directly.
    if words and isinstance(words[0], OutWord):
        out_words = list(words)  # type: ignore[arg-type]
    want = _want_tokens(quote)
    if not want:
        return None
    stream = _stream(out_words)
    for i in range(len(stream) - len(want) + 1):
        if [t for t, _ in stream[i:i + len(want)]] == want:
            return (stream[i][1], stream[i + len(want) - 1][1] + 1)
    return None


# Backwards-friendly alias named in the brief's test list.
locate = locate_quote


def _numeric_equiv(token: str) -> set[str]:
    token = normalize_token(token)
    out = {token}
    if token in _NUMBER_WORDS:
        out.add(_NUMBER_WORDS[token])
    for word, digit in _NUMBER_WORDS.items():
        if digit == token:
            out.add(word)
    return out


def _is_pathlike(text: str) -> bool:
    if CARD_PATHLIKE.search(text or ""):
        return True
    if "/" in (text or "") or "\\" in (text or ""):
        return True
    return False


def check_card(raw, words, duration_s: float, existing=None):  # noqa: ANN001
    """Muse proposes, code decides. Returns (card|None, reason).

    `raw` is a Spark card dict {quote, text, why} (a (text, quote)
    pair is also accepted for tests). `words` are output-timeline
    words; `existing` are already-kept card dicts.
    """
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        raw = {"text": raw[0], "quote": raw[1], "why": ""}
    if not isinstance(raw, dict):
        return None, "invalid_card"
    text = " ".join(str(raw.get("text", "")).split()).strip()
    quote = str(raw.get("quote", "")).strip()
    why = str(raw.get("why", "")).strip()
    out_words = list(words or [])
    if out_words and not isinstance(out_words[0], OutWord):
        out_words = _as_out_words(out_words)
    existing = list(existing or [])
    if _is_pathlike(text):
        return None, "path_like"
    tokens_text = text.split()
    if not (1 <= len(tokens_text) <= 6):
        return None, f"text_length:{len(tokens_text)}"
    if text and text[-1] in ".!?;:":
        return None, "end_punctuation"
    want = _want_tokens(quote)
    if not (1 <= len(want) <= 6):
        return None, f"quote_length:{len(want)}"
    found = locate_quote(quote, out_words)
    if found is None:
        return None, "quote_not_found"
    word_start, word_end = found
    if not (0 <= word_start < word_end <= len(out_words)):
        return None, "quote_not_found"
    first = out_words[word_start]
    t = round(first.start_s - 0.10, 3)
    try:
        raw_d = float(raw.get("d", raw.get("duration_s", 2.6)))
    except (TypeError, ValueError):
        raw_d = 2.6
    d = min(LIMITS["max_card_s"], max(LIMITS["min_card_s"], raw_d))
    d = round(d, 3)
    if t < FIRST_CARD_S - 1e-9:
        return None, "title_window"
    if duration_s and t + d > float(duration_s) - 1.0 + 1e-9:
        return None, "final_second"
    for other in existing:
        try:
            ot = float(other.get("t", 0.0))
            od = float(other.get("d", 2.6))
        except (TypeError, ValueError):
            continue
        gap = max(ot - (t + d), t - (ot + od))
        # Overlap or closer than min_gap in either direction.
        if not (t >= ot + od + LIMITS["min_gap_s"] - 1e-9
                or ot >= t + d + LIMITS["min_gap_s"] - 1e-9):
            return None, "too_close"
    if len([c for c in existing if c.get("enabled", True)]) >= LIMITS["max_cards"]:
        return None, "too_many_cards"
    # Numeric tokens must be spoken (number words count).
    window_words = [w for w in out_words
                    if t - 20.0 <= w.start_s <= t + d + 20.0]
    spoken: set[str] = set()
    for word in window_words:
        for chunk in word.text.split():
            for piece in _TOKEN_SPLIT_RE.split(chunk):
                token = normalize_token(piece)
                if token:
                    spoken.update(_numeric_equiv(token))
    for token in _want_tokens(text):
        if token.isdigit() or token in _NUMBER_WORDS:
            if not (_numeric_equiv(token) & spoken):
                return None, f"unspoken_digit:{token}"
    # At least 60% of content tokens spoken within +-20 s.
    content = [tok for tok in _want_tokens(text) if tok not in _STOPWORDS]
    if content:
        spoken_plain = {normalize_token(w.text) for w in window_words}
        # Expand spoken through token splitting too.
        spoken_plain = set()
        for word in window_words:
            for chunk in word.text.split():
                for piece in _TOKEN_SPLIT_RE.split(chunk):
                    tok = normalize_token(piece)
                    if tok:
                        spoken_plain.add(tok)
                        spoken_plain.update(_numeric_equiv(tok))
        hits = sum(1 for tok in content
                   if _numeric_equiv(tok) & spoken_plain)
        if hits / len(content) < 0.6 - 1e-9:
            return None, "content_unspoken"
    pos_index = len([c for c in existing if c.get("enabled", True)])
    pos = str(raw.get("pos") or ("top-left" if pos_index % 2 == 0
                                 else "top-right"))
    if pos not in ("top-left", "top-right", "top-center"):
        pos = "top-left" if pos_index % 2 == 0 else "top-right"
    card = {
        "id": str(raw.get("id") or f"c{pos_index + 1}"),
        "t": t, "d": d, "text": text, "pos": pos,
        "ghost": bool(raw.get("ghost", True)),
        "anchor": {"quote": quote, "word_start": word_start,
                   "word_end": word_end},
        "source": str(raw.get("source") or "muse"),
        "why": why, "enabled": True,
    }
    return card, ""


# ------------------------------------------------------------ Spark


def dress_schema() -> dict:
    """The strict dress_cards schema. Pure."""
    return {
        "type": "object", "additionalProperties": False,
        "required": ["cards"],
        "properties": {"cards": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["quote", "text", "why"],
            "properties": {
                "quote": {"type": "string"},
                "text": {"type": "string"},
                "why": {"type": "string"}}}}},
    }


def dress_prompt(lines: list[dict]) -> str:
    """The card request over timed caption lines. Pure."""
    shown = [f"[{float(line.get('start_s', 0.0)):.1f}s] "
             f"{line.get('text', '')}" for line in lines]
    return ("Propose title cards for this cut. Reply with cards, each "
            "with a quote (2-6 contiguous spoken words), text (2-6 "
            "words, sentence case, no end punctuation) and a why.\n\n"
            "Transcript:\n" + "\n".join(shown))


def _estimate_usd(prompt: str, model: str) -> float:
    in_rate, out_rate = SPARK_RATES.get(
        model, SPARK_RATES[DEFAULT_MODEL])
    return (len(DEVELOPER + prompt) / 4 * in_rate / 1e6
            + MAX_OUTPUT_TOKENS * out_rate / 1e6)


def propose_cards_with_muse(*, lines: list[dict], model: str,
                            max_usd: float
                            ) -> tuple[list[dict], float, bool, list[str]]:
    """One Spark call. Returns (raw_cards, spend, cached, warnings).

    Raises DressUnavailable only when guestlist is missing.
    """
    try:
        from guestlist.meta import cache, spark
    except ImportError as error:
        raise DressUnavailable(str(error)) from error
    warnings: list[str] = []
    prompt = dress_prompt(lines)
    schema = dress_schema()
    key = cache.key("dress", model, DEVELOPER, prompt, schema, "medium")
    hit = cache.get("spark", key)
    if hit:
        data, spend, cached = hit["data"], 0.0, True
    else:
        estimate = _estimate_usd(prompt, model)
        if estimate > max_usd:
            return [], 0.0, False, ["dress_plan_unavailable:exceeds --max-usd"]
        try:
            cache.guard(estimate)
            reply = spark._structured(
                key, [{"type": "input_text", "text": prompt}],
                schema=schema, name="dress_cards",
                developer=DEVELOPER, model=model, effort="medium",
                max_output_tokens=MAX_OUTPUT_TOKENS,
                record={}, detail={"job": "dress"})
        except Exception as error:  # noqa: BLE001 - degrade to no cards
            cause = f"{type(error).__name__}: {error}"[:160]
            return [], 0.0, False, [f"dress_plan_unavailable:{cause}"]
        data = reply.data
        try:
            spend = round(float(reply.usd or 0.0), 4)
        except (TypeError, ValueError):
            spend = 0.0
        cached = False
    if not isinstance(data, dict):
        return [], spend, cached, ["dress_plan_unavailable:invalid reply"]
    cards = data.get("cards", [])
    if not isinstance(cards, list):
        return [], spend, cached, ["dress_plan_unavailable:invalid reply"]
    return [dict(c) for c in cards if isinstance(c, dict)], spend, cached, warnings


# ------------------------------------------------------------ assets


def _asset_name(kind: str, prompt: str, bucket: float) -> str:
    digest = hashlib.sha256(
        f"{kind}:{prompt}:{bucket}".encode()).hexdigest()[:8]
    return f"{kind}-{digest}.mp3"


def _music_bucket(duration_s: float) -> float:
    return float(math.ceil(max(1.0, duration_s) / 30.0) * 30)


def _ping_samples(sr: int = 48000):
    """Copied from scripts/director_test_run._ping (numpy bell)."""
    assert np is not None
    t = np.arange(int(sr * 0.9)) / sr
    out = np.zeros_like(t)
    for freq, delay, amp in ((1567.98, 0.0, 1.0), (2093.0, 0.07, 0.8)):
        tt = np.clip(t - delay, 0, None)
        env = np.where(
            t >= delay,
            (1 - np.exp(-tt / 0.003)) * np.exp(-tt / 0.18), 0)
        out += amp * env * (
            np.sin(2 * np.pi * freq * tt)
            + 0.25 * np.sin(2 * np.pi * 2 * freq * tt) * np.exp(-tt / 0.05))
    peak = np.abs(out).max() or 1.0
    return out / peak


def _whoosh_samples(sr: int = 48000, dur: float = 0.5):
    """Copied from scripts/director_test_run._whoosh (sweeping noise)."""
    assert np is not None
    rng = np.random.default_rng(7)
    n = int(sr * dur)
    noise = rng.standard_normal(n)
    out, lo = np.zeros(n), 0.0
    for i in range(n):
        fc = 400 * (5000 / 400) ** (i / n)
        lo += (1 - np.exp(-2 * np.pi * fc / sr)) * (noise[i] - lo)
        out[i] = lo
    out -= np.convolve(out, np.ones(48) / 48, "same")
    env = np.sin(np.pi * np.linspace(0, 1, n) ** 0.7) ** 2
    out *= env
    peak = np.abs(out).max() or 1.0
    return out / peak


def _pop_samples(sr: int = 48000, dur: float = 0.5):
    """A soft cartoon pop: a pitch-dropping sine burst."""
    assert np is not None
    t = np.arange(int(sr * dur)) / sr
    freq = 600 * np.exp(-t * 8) + 180
    phase = np.cumsum(2 * np.pi * freq / sr)
    env = np.exp(-t * 9) * (1 - np.exp(-t / 0.004))
    out = np.sin(phase) * env
    peak = np.abs(out).max() or 1.0
    return out / peak


def _write_synth(path: Path, samples, sr: int = 48000) -> None:  # noqa: ANN001
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(samples, -1.0, 1.0)
    stereo = np.repeat(pcm[:, None], 2, axis=1)
    ints = (stereo * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(ints.tobytes())


def ensure_assets(job_dir: Path, *, duration_s: float,
                  music_prompt: str = DEFAULT_MUSIC_PROMPT,
                  music_enabled: bool = True,
                  max_provider_usd: float = DEFAULT_MAX_PROVIDER_USD,
                  transport=None, key: str | None = None):  # noqa: ANN001
    """Cache music/bing/pop in dress/assets. Returns (assets, notes, spend).

    Cache hit means zero HTTP calls. Missing key: bing/pop fall back
    to a local numpy synth (`dress_sfx_local`), music is skipped
    (`dress_music_unavailable`). 401/403/402/429 stop provider calls.
    """
    job = Path(job_dir)
    assets_dir = job / "dress" / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    assets: list[dict] = []
    spent = 0.0
    if key is None:
        try:
            key = finish_mod._elevenlabs_key()  # noqa: SLF001
        except Exception:  # noqa: BLE001
            key = None
    from . import connect as connect_mod

    def _redact(text: str) -> str:
        return connect_mod.redact(text, (key,) if key else ())

    jobs: list[tuple[str, str, float, float]] = []
    # (kind, prompt, duration_s, bucket)
    if music_enabled:
        bucket = _music_bucket(duration_s)
        jobs.append(("music", music_prompt, bucket, bucket))
    jobs.append(("bing", BING_PROMPT, BING_DURATION_S, BING_DURATION_S))
    jobs.append(("pop", POP_PROMPT, POP_DURATION_S, POP_DURATION_S))

    provider_dead = False
    cap_note = f"dress_provider_cap_reached:${max_provider_usd:.2f}"

    for kind, prompt, length_s, bucket in jobs:
        name = _asset_name(kind, prompt, bucket)
        target = assets_dir / name
        asset_id = kind
        if target.exists() and target.stat().st_size > 0:
            probed = finish_mod._probe_audio(target)  # noqa: SLF001
            dur = probed if probed is not None else length_s
            # Preserve provider if a previous plan recorded it.
            assets.append({
                "id": asset_id, "kind": kind, "file": name,
                "provider": "elevenlabs", "model": (
                    elevenlabs_mod.MUSIC_MODEL if kind == "music"
                    else elevenlabs_mod.SFX_MODEL),
                "prompt": prompt, "duration_s": round(float(dur), 3),
                "cost_usd": 0.0,
                "licence": finish_mod._licence(  # noqa: SLF001
                    "ElevenLabs",
                    elevenlabs_mod.MUSIC_MODEL if kind == "music"
                    else elevenlabs_mod.SFX_MODEL)})
            continue
        if provider_dead:
            continue
        if kind == "music" and key is None:
            notes.append("dress_music_unavailable")
            continue
        if key is None:
            # Local fallback for bing/pop only.
            if np is None:
                notes.append(f"dress_sfx_unavailable:{kind}")
                continue
            try:
                samples = (_ping_samples() if kind == "bing"
                           else _pop_samples())
                _write_synth(target, samples)
            except Exception:  # noqa: BLE001
                notes.append(f"dress_sfx_unavailable:{kind}")
                continue
            notes.append("dress_sfx_local")
            assets.append({
                "id": asset_id, "kind": kind, "file": name,
                "provider": "local", "model": "numpy-synth",
                "prompt": prompt,
                "duration_s": round(float(len(samples) / 48000), 3),
                "cost_usd": 0.0,
                "licence": "Local synthesis; no provider terms."})
            continue
        # Provider path, gated by the spend cap.
        if kind == "music":
            estimate = elevenlabs_mod.estimate_music_usd(length_s)
            caller = elevenlabs_mod.generate_music
            model = elevenlabs_mod.MUSIC_MODEL
        else:
            estimate = elevenlabs_mod.estimate_sfx_usd(length_s)
            caller = elevenlabs_mod.generate_sound_effect
            model = elevenlabs_mod.SFX_MODEL
        if spent + estimate > max_provider_usd + 1e-9:
            notes.append(cap_note)
            continue
        generated = None
        error: Exception | None = None
        for attempt in range(2):
            try:
                if transport is not None:
                    generated = caller(key=key, prompt=prompt,
                                       duration_s=length_s,
                                       transport=transport)
                else:
                    generated = caller(key=key, prompt=prompt,
                                       duration_s=length_s)
                error = None
                break
            except elevenlabs_mod.ElevenLabsError as exc:
                error = exc
                status = getattr(exc, "status", None)
                if status in (401, 403):
                    notes.append("finish_provider_auth_failed:elevenlabs")
                    provider_dead = True
                    break
                if status in (402, 429):
                    notes.append("finish_provider_quota:elevenlabs")
                    provider_dead = True
                    break
                if "empty audio" in str(exc):
                    notes.append(f"dress_asset_skipped:{kind}:empty audio")
                    break
                if (status is None or status >= 500) and attempt == 0:
                    continue
                notes.append(_redact(
                    f"dress_asset_skipped:{kind}:{exc}"))
                break
            except Exception as exc:  # noqa: BLE001
                error = exc
                notes.append(_redact(
                    f"dress_asset_skipped:{kind}:{exc}"))
                break
        if provider_dead or generated is None:
            continue
        if not generated.data:
            notes.append(f"dress_asset_skipped:{kind}:empty audio")
            continue
        target.write_bytes(bytes(generated.data))
        probed = finish_mod._probe_audio(target)  # noqa: SLF001
        if probed is None:
            try:
                target.unlink()
            except OSError:
                pass
            notes.append(f"dress_asset_skipped:{kind}:empty audio")
            continue
        cost = round(float(generated.cost_usd), 4)
        spent = round(spent + cost, 4)
        assets.append({
            "id": asset_id, "kind": kind, "file": name,
            "provider": "elevenlabs", "model": model,
            "prompt": prompt, "duration_s": round(float(probed), 3),
            "cost_usd": cost,
            "licence": finish_mod._licence("ElevenLabs", model)})  # noqa: SLF001
    # De-duplicate notes while preserving order.
    seen: set[str] = set()
    unique_notes: list[str] = []
    for note in notes:
        if note not in seen:
            seen.add(note)
            unique_notes.append(note)
    return assets, unique_notes, round(spent, 4)


# ------------------------------------------------------------ ghost


def build_ghost_frames(job_dir: Path, icon_path: Path | None = None,
                       *, size_px: int = 200) -> list[Path]:
    """Cached pop sequence dress/ghost/pop_%03d.png (about 38 frames).

    Crops the icon to its alpha bbox first, then scale-in (ease-out-
    back), bob/tilt hold, scale-out at 24 fps on a 320x320 canvas.
    Returns [] when Pillow or the icon is missing (cards still render).
    """
    job = Path(job_dir)
    out_dir = job / "dress" / "ghost"
    existing = sorted(out_dir.glob("pop_*.png")) if out_dir.exists() else []
    if len(existing) >= 38:
        return existing[:38]
    try:
        from PIL import Image
    except ImportError:
        return []
    icon = Path(icon_path) if icon_path is not None else (
        Path(__file__).resolve().parents[2]
        / "docs" / "brand" / "ghostcut-icon-1024.png")
    if not icon.exists():
        return []
    try:
        art = Image.open(icon).convert("RGBA")
    except OSError:
        return []
    bbox = art.getchannel("A").getbbox()
    if bbox:
        art = art.crop(bbox)
    out_dir.mkdir(parents=True, exist_ok=True)
    canvas = 320
    fps = 24
    total = 38

    def _ease_out_back(x: float) -> float:
        c1 = 1.70158
        c3 = c1 + 1
        return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2

    frames: list[Path] = []
    for index in range(total):
        t = index / fps
        if t < 0.35:
            scale = max(0.01, _ease_out_back(t / 0.35)) * 1.0
            # Overshoot to 1.18 is inside ease-out-back (peak >1).
            angle = 0.0
            bob = 0.0
        elif t < 1.35:
            hold = t - 0.35
            scale = 1.0
            bob = math.sin(hold * 2 * math.pi / 1.0) * 6
            angle = math.sin(hold * 2 * math.pi / 1.0) * 4
        else:
            frac = (t - 1.35) / 0.2
            scale = max(0.01, 1.0 - frac)
            angle = 0.0
            bob = 0.0
        size = max(1, int(size_px * scale))
        frame = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
        sprite = art.resize((size, size), Image.LANCZOS)
        if angle:
            sprite = sprite.rotate(angle, resample=Image.BICUBIC,
                                   expand=True)
        x = (canvas - sprite.width) // 2
        y = (canvas - sprite.height) // 2 + int(bob)
        frame.paste(sprite, (x, y), sprite)
        target = out_dir / f"pop_{index:03d}.png"
        frame.save(target)
        frames.append(target)
    return frames


# ------------------------------------------------------------ ASS cards


def _default_measurer(text: str) -> float:
    try:
        from PIL import ImageFont

        font = ImageFont.truetype(
            str(captions_mod.SHIPPED_POPPINS), CARD_FONT)
        # libass sizes a font by its line height (ascent + descent), PIL
        # by the em square, so scale PIL's width to what libass draws.
        ascent, descent = font.getmetrics()
        return float(font.getlength(text)) * CARD_FONT / (ascent + descent)
    except Exception:  # noqa: BLE001 - Pillow/font missing in tests
        return float(len(text) * 28)


def pill_path(width: float, height: float) -> str:
    """A rounded-pill drawing starting at m 0 0. Pure."""
    w = max(10.0, float(width))
    h = max(10.0, float(height))
    r = h / 2
    # The box spans (0,0)-(w,h), so \an5 centres it on the card point.
    # Four quarter-circle beziers (k = 0.5523), so both ends are round.
    # Review fix (Claude Code): the top-left corner was a straight
    # diagonal ("l r 0"), which rendered as a chevron.
    c = r * 0.5523
    return (
        f"m 0 {r:.0f} "
        f"b 0 {r - c:.0f} {r - c:.0f} 0 {r:.0f} 0 "
        f"l {w - r:.0f} 0 "
        f"b {w - r + c:.0f} 0 {w:.0f} {r - c:.0f} {w:.0f} {r:.0f} "
        f"b {w:.0f} {r + c:.0f} {w - r + c:.0f} {h:.0f} {w - r:.0f} {h:.0f} "
        f"l {r:.0f} {h:.0f} "
        f"b {r - c:.0f} {h:.0f} 0 {r + c:.0f} 0 {r:.0f}")


CARD_Y = 150
CARD_MARGIN_X = 60
PILL_PAD_X = 72
CARD_FONT = 84
PILL_H = 96.0
# The ghost sits in this slot at the frame's outer edge, so it never
# lands on the speaker's face in the middle of the frame.
GHOST_SLOT = 170


def card_position(card: dict, index: int,
                  pill_width: float = 0.0) -> tuple[int, int]:
    """(x, y) for a card on 1920x1080, clear of the lower third.

    The pill is centred on x (\\an5), so a wide pill moves inward to
    keep CARD_MARGIN_X from the frame edge.
    """
    pos = str(card.get("pos") or "")
    if pos not in ("top-left", "top-right", "top-center"):
        pos = "top-left" if index % 2 == 0 else "top-right"
    half = max(0.0, float(pill_width)) / 2
    if pos == "top-center":
        return (960, CARD_Y)
    if pos == "top-right":
        return (int(1920 - CARD_MARGIN_X - GHOST_SLOT - half), CARD_Y)
    return (int(CARD_MARGIN_X + GHOST_SLOT + half), CARD_Y)


def ghost_position(card: dict, index: int,
                   pill_width: float) -> tuple[int, int]:
    """(x, y) of the ghost sprite centre beside its pill."""
    _, y = card_position(card, index, pill_width)
    edge = CARD_MARGIN_X + GHOST_SLOT // 2
    if card.get("pos") == "top-right" or (
            not card.get("pos") and index % 2 == 1):
        return (1920 - edge, y)
    return (edge, y)


def build_ass(cards, *, base_ass: str | None = None, measurer=None,  # noqa: ANN001
              width: int = 1920, height: int = 1080) -> str:
    """Card events as ASS, appended to a captions ASS when given.

    Per card two events on \\an5 at the same \\pos: a \\p1 pill and
    the text. Pill width comes from `measurer` (injected in tests).
    """
    measure = measurer or _default_measurer
    enabled = [c for c in (cards or []) if c.get("enabled", True)]
    card_lines: list[str] = []
    for index, card in enumerate(enabled):
        try:
            start = float(card.get("t", 0.0))
            end = start + float(card.get("d", 2.6))
        except (TypeError, ValueError):
            continue
        text = str(card.get("text", ""))
        pill_w = float(measure(text)) + PILL_PAD_X
        x, y = card_position(card, index, pill_w)
        pill_h = PILL_H
        drawing = pill_path(pill_w, pill_h)
        start_s = captions_mod.format_ass_time(start)
        end_s = captions_mod.format_ass_time(end)
        pill_text = (
            f"{{\\an5\\pos({x},{y})\\p1\\c&H000000&\\alpha&H4D&"
            f"\\bord3\\3c{captions_mod.ACTIVE_BGR}"
            f"\\fscx60\\fscy60\\t(0,140,\\fscx108\\fscy108)"
            f"\\t(140,260,\\fscx100\\fscy100)\\fad(0,200)}}{drawing}")
        card_lines.append(
            f"Dialogue: 1,{start_s},{end_s},CardPill,,0,0,0,,{pill_text}")
        text_event = (
            f"{{\\an5\\pos({x},{y})"
            f"\\fscx60\\fscy60\\t(0,140,\\fscx108\\fscy108)"
            f"\\t(140,260,\\fscx100\\fscy100)\\fad(0,200)}}"
            f"{captions_mod.ass_escape(text)}")
        card_lines.append(
            f"Dialogue: 1,{start_s},{end_s},Card,,0,0,0,,{text_event}")
    styles = (
        "Style: Card,Poppins SemiBold," + str(CARD_FONT) + ",&H00FFFFFF,&H00FFFFFF,"
        "&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n"
        "Style: CardPill,Poppins SemiBold," + str(CARD_FONT) + ",&H00FFFFFF,&H00FFFFFF,"
        "&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1")
    if base_ass is not None:
        base = base_ass if base_ass.endswith("\n") else base_ass + "\n"
        # Insert styles before [Events] when present, else append.
        if "[V4+ Styles]" in base and "Style: Card," not in base:
            # Append card styles at the end of the styles block.
            marker = "[Events]"
            if marker in base:
                head, tail = base.split(marker, 1)
                base = head.rstrip("\n") + "\n" + styles + "\n\n" + marker + tail
            else:
                base = base + styles + "\n"
        if "[Events]" not in base:
            base = base + "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        return base.rstrip("\n") + "\n" + "\n".join(card_lines) + (
            "\n" if card_lines else "")
    header = "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}",
        f"PlayResY: {height}", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, "
        "SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
        "MarginV, Encoding",
        captions_mod.style_line("house", width, height),
        styles, "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text"])
    return header + "\n" + "\n".join(card_lines) + "\n"


# ------------------------------------------------------------ mix / encode


def sfx_cues(cards, *, bing_prompt: str = BING_PROMPT,  # noqa: ANN001
             pop_prompt: str = POP_PROMPT) -> list:
    """One bing SoundCue per enabled card (pop kept for grammar). Pure."""
    cues = []
    for index, card in enumerate(
            [c for c in (cards or []) if c.get("enabled", True)], 1):
        try:
            at = float(card.get("t", 0.0)) + 0.05
        except (TypeError, ValueError):
            continue
        cues.append(finish_mod.SoundCue(
            id=f"bing_{index:02d}", kind="bing", at_s=round(at, 3),
            duration_s=BING_DURATION_S, prompt=bing_prompt,
            gain_db=BING_GAIN_DB, reason="card pop", confidence=1.0))
    return cues


def duck_command(voice: Path, bed_mix: Path | None, sfx_mix: Path | None,
                 out: Path, *, music_gain_db: float = MUSIC_GAIN_DEFAULT
                 ) -> list[str]:
    """Duck the bed only; the SFX bus gets no second volume. Pure."""
    duck = (f"sidechaincompress=threshold={finish_mod.DUCK_THRESHOLD}"
            f":ratio={finish_mod.DUCK_RATIO}"
            f":attack={finish_mod.DUCK_ATTACK_MS}"
            f":release={finish_mod.DUCK_RELEASE_MS}")
    if bed_mix is None and sfx_mix is None:
        return ["ffmpeg", "-v", "error", "-y", "-i", str(voice),
                "-af", "aresample=48000,pan=stereo|c0=c0|c1=c0",
                "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(out)]
    head = "[0:a]asplit[vm][key];[vm]pan=stereo|c0=c0|c1=c0[v];"
    if bed_mix is not None and sfx_mix is not None:
        args = ["ffmpeg", "-v", "error", "-y", "-i", str(voice),
                "-i", str(bed_mix), "-i", str(sfx_mix)]
        graph = (f"{head}[1:a]volume={music_gain_db:.1f}dB[m];"
                 f"[m][key]{duck}[bed];"
                 f"[v][bed][2:a]amix=inputs=3:normalize=0[out]")
    elif bed_mix is not None:
        args = ["ffmpeg", "-v", "error", "-y", "-i", str(voice),
                "-i", str(bed_mix)]
        graph = (f"{head}[1:a]volume={music_gain_db:.1f}dB[bed];"
                 f"[bed][key]{duck}[ducked];"
                 f"[v][ducked]amix=inputs=2:normalize=0[out]")
    else:
        # No bed: nothing to duck, so no asplit (an unused [key] pad
        # would fail the graph). Fixed by Claude Code.
        args = ["ffmpeg", "-v", "error", "-y", "-i", str(voice),
                "-i", str(sfx_mix)]
        graph = ("[0:a]pan=stereo|c0=c0|c1=c0[v];"
                 "[v][1:a]amix=inputs=2:normalize=0[out]")
    return args + ["-filter_complex", graph, "-map", "[out]",
                   "-ac", "2", "-ar", "48000",
                   "-c:a", "pcm_s16le", str(out)]


def mix_commands(*, source: Path, dress_dir: Path, duration_s: float,
                 cards, music_asset: Path | None,  # noqa: ANN001
                 bing_asset: Path | None, pop_asset: Path | None = None,
                 music_gain_db: float = MUSIC_GAIN_DEFAULT,
                 music_prompt: str = DEFAULT_MUSIC_PROMPT,
                 target_lufs: float = -14.0) -> list[list[str]]:
    """The dress mix in step order 1, 2, 5, 3, 4 (pure builders).

    Step 4 (master) is the loudnorm measure command only here; the
    apply/limiter commands need the measured block and are built in
    `render` from `finish.loudnorm_apply_command`/`limiter_command`.
    """
    dress_dir = Path(dress_dir)
    voice_wav = dress_dir / "voice.wav"
    bed_mix = dress_dir / "bed_mix.wav"
    sfx_mix_path = dress_dir / "sfx_mix.wav"
    premaster = dress_dir / "premaster.wav"
    commands: list[list[str]] = []
    commands.append(finish_mod.extract_voice_command(Path(source), voice_wav))
    bed_cmd = None
    if music_asset is not None:
        plan = finish_mod.MusicPlan(
            prompt=music_prompt, start_s=0.0, end_s=float(duration_s),
            gain_db=MUSIC_GAIN_DEFAULT, dropouts=[])
        bed_cmd = finish_mod.music_mix_command(
            Path(music_asset), float(duration_s), plan, bed_mix)
        commands.append(bed_cmd)
    cues = sfx_cues(cards)
    sfx_cmd = None
    if cues and bing_asset is not None:
        pairs = [(cue, Path(bing_asset)) for cue in cues]
        sfx_cmd = finish_mod.sfx_mix_command(pairs, float(duration_s),
                                             sfx_mix_path)
        commands.append(sfx_cmd)
    duck = duck_command(
        voice_wav,
        bed_mix if music_asset is not None else None,
        sfx_mix_path if sfx_cmd is not None else None,
        premaster, music_gain_db=music_gain_db)
    commands.append(duck)
    commands.append(finish_mod.loudnorm_measure_command(premaster,
                                                          target_lufs))
    return commands


def encode_command(*, source: Path, title_png: Path | None,
                   ghost_pattern: Path | None, pop_cards,  # noqa: ANN001
                   overlay_ass: Path, fonts_dir: Path,
                   master_wav: Path, out_path: Path,
                   measurer=None) -> list[str]:  # noqa: ANN001
    """One ffmpeg video pass for the dressed cut. Pure."""
    measure = measurer or _default_measurer
    args: list[str] = ["ffmpeg", "-v", "error", "-y", "-i", str(source)]
    title_index: int | None = None
    next_index = 1
    if title_png is not None:
        args += ["-i", str(title_png)]
        title_index = next_index
        next_index += 1
    ghost_indices: list[int] = []
    for _card in pop_cards:
        args += ["-framerate", "24", "-i", str(ghost_pattern)]
        ghost_indices.append(next_index)
        next_index += 1
    master_index = next_index
    args += ["-i", str(master_wav)]
    filters: list[str] = []
    current = "0:v"
    label_n = 0
    if title_index is not None:
        filters.append(
            f"[0:v][{title_index}:v]overlay=0:0:"
            f"enable='between(t,0,{TITLE_END_S})'[v{label_n}]")
        current = f"v{label_n}"
        label_n += 1
    for pos, (card, gindex) in enumerate(zip(pop_cards, ghost_indices)):
        try:
            at = float(card.get("t", 0.0))
        except (TypeError, ValueError):
            at = 0.0
        pill_w = float(measure(str(card.get("text", "")))) + PILL_PAD_X
        gx, gy = ghost_position(card, pos, pill_w)
        # Overlay top-left from the sprite centre (320 canvas, sprite centred).
        ox = gx - 160
        oy = gy - 160
        filters.append(
            f"[{gindex}:v]format=rgba,setpts=PTS-STARTPTS+{at:.3f}/TB"
            f"[g{pos}]")
        filters.append(
            f"[{current}][g{pos}]overlay=x={ox}:y={oy}:"
            f"eof_action=pass:repeatlast=0[v{label_n}]")
        current = f"v{label_n}"
        label_n += 1
    subs = (f"subtitles=filename="
            f"{captions_mod._escape_filter_path(Path(overlay_ass))}"  # noqa: SLF001
            f":fontsdir="
            f"{captions_mod._escape_filter_path(Path(fonts_dir))}")  # noqa: SLF001
    filters.append(f"[{current}]{subs}[vout]")
    args += ["-filter_complex", ";".join(filters),
             "-map", "[vout]", "-map", f"{master_index}:a",
             "-c:v", "libx264", "-preset", "fast", "-crf", "17",
             "-pix_fmt", "yuv420p", "-r", "24000/1001",
             "-c:a", "aac", "-b:a", "192k",
             "-movflags", "+faststart", str(out_path)]
    return args


def _target_lufs(job_dir: Path) -> float:
    finish_path = Path(job_dir) / "finish.json"
    if finish_path.exists():
        try:
            data = read_json(finish_path)
            return float(data.get("target_lufs", -14.0))
        except (OSError, ValueError, TypeError):
            pass
    return -14.0


def _write_status(job_dir: Path, state: str, pct: float,
                  eta_s: float | None, message: str) -> dict:
    payload = {"state": state, "pct": round(float(pct), 1),
               "eta_s": eta_s, "message": message}
    dress_dir = Path(job_dir) / "dress"
    dress_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(dress_dir / "status.json", payload)
    return payload


def load_status(job_dir: Path) -> dict:
    path = Path(job_dir) / "dress" / "status.json"
    if not path.exists():
        return {"state": "idle", "pct": 0, "eta_s": None, "message": ""}
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return {"state": "idle", "pct": 0, "eta_s": None, "message": ""}
    return data if isinstance(data, dict) else {
        "state": "idle", "pct": 0, "eta_s": None, "message": ""}


def _overlay_ass(job_dir: Path, cards) -> Path:  # noqa: ANN001
    """dress/overlay_all.ass: captions (copied or rebuilt) + cards."""
    job = Path(job_dir)
    dress_dir = job / "dress"
    dress_dir.mkdir(parents=True, exist_ok=True)
    out = dress_dir / "overlay_all.ass"
    if _is_captioned(job):
        base_path = job / "captions-horizontal.ass"
        base = (base_path.read_text(encoding="utf-8")
                if base_path.exists() else None)
        text = build_ass(cards, base_ass=base)
        out.write_text(text, encoding="utf-8")
        return out
    # Fresh uncaptioned re-render: rebuild as captions.run does.
    words_path = job / "words.json"
    angles_path = job / "angles.json"
    base_text = None
    if words_path.exists() and angles_path.exists():
        try:
            from . import angles as angles_mod

            words = angles_mod.load_timed_words(words_path)
            heard = captions_mod.filter_caption_words(words)
            angles_payload = read_json(angles_path)
            shots = (angles_payload.get("shots", [])
                     if isinstance(angles_payload, dict) else [])
            origin = captions_mod.origin_from_shots(shots)
            duration = captions_mod.duration_from_shots(shots, origin)
            tighten = None
            tighten_path = job / "tighten.json"
            if tighten_path.exists():
                try:
                    tighten = cutlist.load_tighten(tighten_path)
                except (OSError, ValueError):
                    tighten = None
            mapped = captions_mod.map_words_to_output(
                heard, tighten, origin, duration)
            cuts = captions_mod.cut_times_from_shots(shots, origin)
            cap_words = [captions_mod.CaptionWord(
                w.text, w.start_s, w.end_s, w.span) for w in mapped]
            lines = captions_mod.group_lines(cap_words, cuts)
            base_text = captions_mod.build_ass(
                lines, preset="house", width=1920, height=1080)
        except (OSError, ValueError):
            base_text = None
    text = build_ass(cards, base_ass=base_text)
    out.write_text(text, encoding="utf-8")
    return out


def _save_rendered(job: Path, snapshot: dict, rendered: dict) -> dict:
    """Write the render's own result into the CURRENT dress.json. Returns it.

    Render lock after verification (Claude Code, 2026-11-04, Astra F03):
    render() used to save the snapshot it loaded at its start, so a plan
    edit made while it ran (music off) came back. Only `rendered` and the
    render's warnings are merged; the fingerprint stays the snapshot's, so
    a plan changed mid-render reads as stale instead of fresh.
    """
    current = load_plan(job)
    if current is None:
        current = snapshot
    else:
        warnings = list(current.get("warnings", []) or [])
        warnings += [w for w in snapshot.get("warnings", []) or []
                     if w not in warnings]
        current["warnings"] = warnings
    current["rendered"] = rendered
    return save_plan(job, current)


def render(job_dir: Path, *, measurer=None) -> dict:  # noqa: ANN001
    """FFmpeg-only render to cut.dressed.mp4. Returns dress.json."""
    job = Path(job_dir)
    plan = load_plan(job)
    if plan is None:
        raise DressError("dress: no dress.json; run director dress plan first")
    source = source_video(job)
    if not source.exists():
        raise DressError(f"dress: no cut to dress in {job}")
    duration = _duration(job, source)
    if duration <= 0:
        raise DressError("dress: could not measure the cut duration")
    dress_dir = job / "dress"
    dress_dir.mkdir(parents=True, exist_ok=True)
    _write_status(job, "running", 0, None, "Dressing the cut")
    try:
        cards = [c for c in plan.get("cards", []) or []
                 if isinstance(c, dict)]
        ghost_cfg = plan.get("ghost") or {}
        ghost_on = bool(ghost_cfg.get("enabled", True))
        pop_cards = [c for c in cards
                     if c.get("enabled", True) and c.get("ghost", True)
                     and ghost_on]
        ghost_pattern = dress_dir / "ghost" / "pop_%03d.png"
        if pop_cards:
            frames = build_ghost_frames(job)
            if not frames:
                pop_cards = []
                plan.setdefault("warnings", []).append(
                    "dress_ghost_skipped:icon or Pillow missing")
            else:
                ghost_pattern = frames[0].parent / "pop_%03d.png"
        overlay = _overlay_ass(job, cards)
        fonts_dir = captions_mod.stage_fonts_dir(job, None)
        # Assets from the plan (cached only; render never calls out).
        by_kind: dict[str, Path] = {}
        for asset in plan.get("assets", []) or []:
            if isinstance(asset, dict) and asset.get("file"):
                by_kind[str(asset.get("kind"))] = (
                    dress_dir / "assets" / str(asset["file"]))
        music_cfg = plan.get("music") or {}
        music_asset = by_kind.get("music") if music_cfg.get(
            "enabled", True) else None
        if music_asset is not None and not music_asset.exists():
            music_asset = None
        bing_asset = by_kind.get("bing")
        if bing_asset is not None and not bing_asset.exists():
            bing_asset = None
        if bing_asset is None and cards:
            # Review fix (Claude Code): a plan made only from typed notes
            # (apply_ops on a never-planned cut) has no assets; synth the
            # offline bing so its cards still ping. No network.
            bing_asset = dress_dir / "assets" / "bing-local.wav"
            if not bing_asset.exists():
                try:
                    _write_synth(bing_asset, _ping_samples())
                except Exception:  # noqa: BLE001 - cards still render silent
                    bing_asset = None
        if not bool(ghost_cfg.get("sfx", True)):
            bing_asset = None  # "no bing" / "no sound effects"
        music_gain = float(music_cfg.get("gain_db", MUSIC_GAIN_DEFAULT))
        target = _target_lufs(job)
        commands = mix_commands(
            source=source, dress_dir=dress_dir, duration_s=duration,
            cards=cards, music_asset=music_asset, bing_asset=bing_asset,
            pop_asset=by_kind.get("pop"), music_gain_db=music_gain,
            music_prompt=str(music_cfg.get("prompt",
                                           DEFAULT_MUSIC_PROMPT)),
            target_lufs=target)
        # Steps 1, 2, 5, 3 then the master (step 4).
        premaster = dress_dir / "premaster.wav"
        for command in commands[:-1]:
            run_command(command)
        _write_status(job, "running", 35, None, "Mixing the sound")
        measure = run_command(commands[-1], check=False)
        measured = finish_mod.parse_loudnorm_full(measure.stderr or "")
        unlimited = dress_dir / "master_unlimited.wav"
        master = dress_dir / "master.wav"
        if measured is None:
            run_command([
                "ffmpeg", "-v", "error", "-y", "-i", str(premaster),
                "-af", f"loudnorm=I={target}:TP=-1.0:LRA=11",
                "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le",
                str(unlimited)])
        else:
            run_command(finish_mod.loudnorm_apply_command(
                premaster, unlimited, target_lufs=target,
                measured=measured))
        try:
            run_command(finish_mod.limiter_command(unlimited, master))
            unlimited.unlink(missing_ok=True)
        except ToolingError:
            os.replace(unlimited, master)
        _write_status(job, "running", 55, None, "Dressing the picture")
        _title, title_png = _title_info(job)
        title_input = title_png if (
            (plan.get("title") or {}).get("enabled", True)
            and title_png is not None and title_png.exists()) else None
        next_out = job / "cut.dressed.next.mp4"
        command = encode_command(
            source=source, title_png=title_input,
            ghost_pattern=ghost_pattern if pop_cards else None,
            pop_cards=pop_cards, overlay_ass=overlay,
            fonts_dir=fonts_dir, master_wav=master, out_path=next_out,
            measurer=measurer)
        # One encode with -progress parsed into dress/status.json.
        progress_cmd = command[:-1] + ["-progress", "pipe:1", command[-1]]
        started = time.monotonic()
        proc = subprocess.Popen(  # noqa: S603
            progress_cmd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        out_time_ms = 0.0
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.startswith("out_time_ms="):
                try:
                    out_time_ms = int(line.split("=", 1)[1]) / 1e6
                except ValueError:
                    pass
                pct = 55 + 45 * min(1.0, out_time_ms / max(duration, 0.1))
                elapsed = time.monotonic() - started
                eta = (round(elapsed / max(out_time_ms, 0.1)
                             * max(duration - out_time_ms, 0.0))
                       if out_time_ms > 0.5 else None)
                _write_status(job, "running", pct, eta,
                              "Dressing the cut")
        proc.wait()
        stderr = proc.stderr.read() if proc.stderr else ""
        proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
        if proc.returncode != 0:
            try:
                next_out.unlink(missing_ok=True)
            except OSError:
                pass
            raise DressError(
                f"dress: encode failed ({stderr.strip()[:200]})")
        probed = _probe_duration(next_out)
        stats_ok = False
        try:
            stats = extract_media_stats(probe_media(next_out))
            stats_ok = bool(stats.get("has_audio")) and int(
                stats.get("width") or 0) > 0
        except (ToolingError, OSError, ValueError):
            stats_ok = False
        if (probed is None or abs(probed - duration) > 0.1
                or not stats_ok):
            try:
                next_out.unlink(missing_ok=True)
            except OSError:
                pass
            raise DressError(
                "dress: dressed render failed its duration check; "
                "cut.mp4 is unchanged")
        os.replace(next_out, job / "cut.dressed.mp4")
        plan = _save_rendered(job, plan, {
            "file": "cut.dressed.mp4", "source_stamp": _stamp(source),
            "plan_fingerprint": _plan_fingerprint(plan),
            "at": datetime.now().isoformat(timespec="seconds")})
        _write_status(job, "done", 100, 0, "Dressed cut ready")
        return plan
    except Exception as error:
        _write_status(job, "failed", 0, None, str(error)[:200])
        if isinstance(error, DressError):
            raise
        raise DressError(f"dress: render failed ({error})") from error


# ------------------------------------------------------------ plan


def _default_plan(job: Path, source: Path, duration: float) -> dict:
    title, _png = _title_info(job)
    caps = _captions_payload(job)
    return {
        "version": VERSION,
        "source": {"video": source.name,
                   "stamp": _stamp(source) if source.exists() else "",
                   "duration_s": round(float(duration), 3),
                   "captioned": _is_captioned(job)},
        "title": title,
        "captions": {"enabled": caps is not None, "preset": "house"},
        "cards": [],
        "ghost": {"enabled": True, "size_px": 150, "sfx": True},
        "music": {"enabled": True, "prompt": DEFAULT_MUSIC_PROMPT,
                  "gain_db": MUSIC_GAIN_DEFAULT, "duck": True,
                  "asset": None},
        "sfx": {
            "bing": {"prompt": BING_PROMPT, "duration_s": BING_DURATION_S,
                     "gain_db": BING_GAIN_DB, "asset": None},
            "pop": {"prompt": POP_PROMPT, "duration_s": POP_DURATION_S,
                    "gain_db": POP_GAIN_DB, "asset": None}},
        "broll": [],
        "limits": dict(LIMITS),
        "assets": [],
        "design": {"model": DEFAULT_MODEL, "cached": False,
                   "spend_usd": 0.0},
        "provider_spend_usd": 0.0,
        "notes": [], "warnings": [], "history": [],
        "rendered": {"file": "", "source_stamp": "", "at": ""},
    }


def plan(job_dir: Path, *, model: str = DEFAULT_MODEL,
         max_usd: float = 5.0, max_provider_usd: float = DEFAULT_MAX_PROVIDER_USD,
         no_muse: bool = False, transport=None, key: str | None = None,
         propose=None) -> dict:  # noqa: ANN001
    """Write dress.json and cache assets. Returns the payload.

    `propose` (tests) replaces `propose_cards_with_muse`.
    """
    propose = propose or propose_cards_with_muse
    job = Path(job_dir)
    job.mkdir(parents=True, exist_ok=True)
    source = source_video(job)
    duration = _duration(job, source if source.exists() else None)
    existing = load_plan(job) or {}
    payload = _default_plan(job, source, duration)
    # Preserve editor state that a re-plan must not lose.
    if existing:
        for field in ("title", "captions", "ghost", "music", "sfx"):
            if isinstance(existing.get(field), dict):
                payload[field] = {**payload[field], **existing[field]}
        payload["history"] = list(existing.get("history", []) or [])
        payload["rendered"] = existing.get(
            "rendered", payload["rendered"])
    kept = [c for c in (existing.get("cards", []) or [])
            if isinstance(c, dict)
            and c.get("source") not in ("muse", "rule")]
    words = load_output_words(job)
    lines = load_caption_lines(job, words)
    notes: list[str] = []
    warnings: list[str] = []
    spend = 0.0
    cached = False
    auto_cards: list[dict] = []
    muse_failed = False
    if not lines or not words:
        notes.append("dress_plan_no_transcript:no auto cards")
    elif no_muse:
        notes.append("dress_plan_no_muse:using the built-in planner")
    else:
        try:
            raw_cards, spend, cached, plan_warnings = propose(
                lines=lines, model=model, max_usd=max_usd)
            warnings.extend(plan_warnings)
            accepted: list[dict] = list(kept)
            for raw in raw_cards:
                card, reason = check_card(raw, words, duration, accepted)
                if card is None:
                    warnings.append(f"dress_card_rejected:{reason}")
                    continue
                card["source"] = "muse"
                accepted.append(card)
                auto_cards.append(card)
                if len(auto_cards) >= LIMITS["max_cards"]:
                    break
            muse_failed = not auto_cards
            if muse_failed and plan_warnings:
                notes.append(plan_warnings[0])
        except Exception as error:  # noqa: BLE001 - review fix: any Muse failure falls back
            muse_failed = True
            notes.append(f"dress_plan_unavailable:{type(error).__name__}")
    # Muse unavailable (402 credit, no key, network, garbage reply) or
    # --no-muse: the deterministic transcript planner fills in, so a
    # no-key demo still has cards. Added by Claude Code.
    if words and lines and (no_muse or muse_failed):
        auto_cards = rule_cards(words, duration, existing=kept)
        if not no_muse:
            notes.append(MUSE_FALLBACK_NOTE)
    # Renumber muse+kept together in time order, ids stable by order.
    all_cards = sorted(kept + auto_cards,
                        key=lambda c: float(c.get("t", 0.0)))
    for index, card in enumerate(all_cards, 1):
        card["id"] = f"c{index}"
        # Alternate corners in time order (Claude Code: check_card picks
        # by acceptance order, which is not time order).
        card["pos"] = "top-left" if index % 2 == 1 else "top-right"
    payload["cards"] = all_cards
    used_rules = bool(words and lines and (no_muse or muse_failed))
    payload["design"] = {"model": "rules" if used_rules else model,
                         "cached": cached,
                         "spend_usd": round(float(spend), 4)}
    music_prompt = str((payload.get("music") or {}).get(
        "prompt") or DEFAULT_MUSIC_PROMPT)
    music_enabled = bool((payload.get("music") or {}).get("enabled", True))
    assets, asset_notes, provider_spend = ensure_assets(
        job, duration_s=duration, music_prompt=music_prompt,
        music_enabled=music_enabled,
        max_provider_usd=max_provider_usd, transport=transport, key=key)
    notes.extend(asset_notes)
    payload["assets"] = assets
    payload["provider_spend_usd"] = round(float(provider_spend), 4)
    by_kind = {a["kind"]: a for a in assets}
    if "music" in by_kind:
        payload["music"]["asset"] = by_kind["music"]["file"]
    if "bing" in by_kind:
        payload["sfx"]["bing"]["asset"] = by_kind["bing"]["file"]
    if "pop" in by_kind:
        payload["sfx"]["pop"]["asset"] = by_kind["pop"]["file"]
    payload["notes"] = notes
    payload["warnings"] = warnings
    save_plan(job, payload)
    return payload


# ------------------------------------------------------------ rule planner
# Everything below was added by Claude Code (Opus) after Muse Code's run
# stopped on the Meta API 402: the deterministic planner that stands in
# for Spark, the note grammar, the op checks and the plan editor.

MUSE_FALLBACK_NOTE = "Muse unavailable, using the built-in planner"

# Spoken keyword -> house labels to try (each must still pass
# `check_card`: 60% of its words spoken within +-20 s). When no label
# passes, the card shows the words spoken around the keyword.
RULE_LEXICON: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("synced", "sync", "syncing"),
     ("Two cameras synced", "Cameras synced", "Synced by sound")),
    (("captions", "caption", "subtitles"),
     ("B-roll and captions", "Captions added", "Word-by-word captions")),
    (("grade", "graded", "grading"),
     ("Color grade from log", "Color graded", "Color grade")),
    (("angle", "angles"), ("Cut angle to angle", "Every angle")),
    (("transcribe", "transcript", "whisper"),
     ("Whisper and transcribe", "Transcribed")),
    (("enhanced", "audio", "sound"), ("Sound enhanced", "Clean sound")),
    (("spark",), ("Muse Spark sees the footage", "Muse Spark")),
    (("crops", "crop", "reframe", "vertical"),
     ("Different crops", "Reframed crops")),
    (("delivered", "deliver"),
     ("Completed and delivered", "Delivered to the client")),
    (("b-roll", "broll"), ("B-roll added", "B-roll")),
)
_RULE_EDGE_FILLER = frozenset({
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "by", "for",
    "with", "is", "it", "at", "as", "be", "are", "was", "so", "that",
    "this", "them", "they", "you", "your", "we", "our", "i", "me", "my",
    "he", "she", "his", "her", "its", "it's", "basically", "just",
    "like", "um", "uh", "really", "very", "can", "will", "also", "all",
    "get", "got", "there", "here", "what", "which", "who", "but", "if",
    "then", "has", "have", "had", "do", "did", "does", "even", "being",
    "though", "about", "some", "any", "from",
})
RULE_MAX_CARDS = 6


def _clean_word(text: str) -> str:
    return re.sub(r"^[^\w]+|[^\w]+$", "", text or "")


def _phrase_around(words: list[OutWord], index: int,
                   before: int = 2, after: int = 2) -> tuple[int, int]:
    """(start, end) of a 1-4 word phrase around words[index]. Pure.

    Filler words are trimmed from both edges; an immediately repeated
    token ("B-roll, roll") is dropped by stopping before it.
    """
    lo = max(0, index - before)
    hi = min(len(words), index + after + 1)
    # Do not cross a long pause (a sentence break) on either side.
    for j in range(index, lo, -1):
        if words[j].start_s - words[j - 1].end_s > 0.6:
            lo = j
            break
    for j in range(index + 1, hi):
        if words[j].start_s - words[j - 1].end_s > 0.6:
            hi = j
            break
    while lo < index and normalize_token(words[lo].text) in _RULE_EDGE_FILLER:
        lo += 1
    while hi - 1 > index and normalize_token(
            words[hi - 1].text) in _RULE_EDGE_FILLER:
        hi -= 1
    # Cut at a repeated token so "b-roll, roll" stays "b-roll".
    seen: set[str] = set()
    for j in range(lo, hi):
        toks = _want_tokens(words[j].text)
        if any(t in seen for t in toks):
            if j > index:
                hi = j
                break
            lo = j + 1
            seen = set()
            continue
        seen.update(toks)
    while hi - lo > 4:
        if index - lo > hi - 1 - index:
            lo += 1
        else:
            hi -= 1
    return lo, hi


def _sentence_case(words: list[str]) -> str:
    text = " ".join(w for w in (_clean_word(x) for x in words) if w)
    if not text:
        return ""
    return text[0].upper() + text[1:]


def rule_cards(words, duration_s: float, *, existing=None,  # noqa: ANN001
               max_cards: int = RULE_MAX_CARDS) -> list[dict]:
    """Deterministic cards from the spoken transcript (no network). Pure.

    Finds lexicon keywords on the output timeline, builds a short card
    from the words spoken around each one, and passes every candidate
    through `check_card`, the same gate Muse's proposals face. One card
    per topic, spread out so they do not bunch up.
    """
    out_words = list(words or [])
    if out_words and not isinstance(out_words[0], OutWord):
        out_words = _as_out_words(out_words)
    if not out_words or duration_s <= 0:
        return []
    accepted: list[dict] = [dict(c) for c in (existing or [])]
    spacing = max(6.0, float(duration_s) / (max_cards * 2.5))
    candidates: list[tuple[int, int]] = []  # (topic, word index)
    for index, word in enumerate(out_words):
        toks = _want_tokens(word.text)
        joined = "-".join(toks)
        for topic, (keys, _labels) in enumerate(RULE_LEXICON):
            if joined in keys or any(t in keys for t in toks):
                candidates.append((topic, index))
                break
    made: list[dict] = []
    # Walk topics in priority order; the first clean hit per topic wins.
    for topic, (_keys, labels) in enumerate(RULE_LEXICON):
        if len(made) >= max_cards:
            break
        for cand_topic, index in candidates:
            if cand_topic != topic:
                continue
            lo, hi = _phrase_around(out_words, index)
            spoken = [w.text for w in out_words[lo:hi]]
            quote = " ".join(_clean_word(w) for w in spoken)
            t_guess = out_words[lo].start_s - 0.10
            if any(abs(float(c.get("t", 0.0)) - t_guess) < spacing
                   for c in accepted if c.get("enabled", True)):
                continue
            keyword = _clean_word(out_words[index].text)
            card = None
            for text in (*labels, _sentence_case(spoken)):
                if not text or len(text.split()) > 6:
                    continue
                raw = {"text": text, "quote": quote, "source": "rule",
                       "why": f"Built-in planner: you say \u201c{keyword}\u201d here"}
                card, _reason = check_card(raw, out_words, duration_s,
                                           accepted)
                if card is not None:
                    break
            if card is None:
                continue
            card["source"] = "rule"
            accepted.append(card)
            made.append(card)
            break
    made.sort(key=lambda c: c["t"])
    return made


# ------------------------------------------------------------ grammar

_Q = r"[\"'“”‘’]?"
_END = r"\s*[.!?]*\s*$"
_ADD_SAYS = re.compile(
    r"\b(?:add|put|drop in|give me|show)\s+(?:a\s+|an\s+|some\s+|the\s+)?"
    r"(?:title\s+card|card|title|pop-?\s?up|text)\s+(?:that\s+)?"
    r"(?:says|saying|reading|reads)\s*:?\s*" + _Q + r"(?P<x>.+?)" + _Q +
    r"(?:\s*,?\s+(?:where|when)\s+i\s+say\s+" + _Q + r"(?P<y>.+?)" + _Q +
    r")?(?:\s+here)?" + _END, re.IGNORECASE)
_ADD_WHERE = re.compile(
    r"\b(?:add|put|drop in|give me|show)\s+(?:a\s+|an\s+|some\s+|the\s+)?"
    r"(?:b[- ]?roll|title\s+card|card|title|pop-?\s?up)\s+"
    r"(?:where|when)\s+i\s+say\s+" + _Q + r"(?P<y>.+?)" + _Q + _END,
    re.IGNORECASE)
_REMOVE = re.compile(
    r"\b(?:remove|delete|lose|drop|kill|cut)\s+(?:the|this|that)\s+"
    r"(?:title\s+card|card|title|pop-?\s?up)"
    r"(?:\s+(?:at|where\s+i\s+say|when\s+i\s+say)\s+" + _Q + r"(?P<y>.+?)"
    + _Q + r"|\s+(?P<here>here|now))?" + _END, re.IGNORECASE)
_NO_CARDS = re.compile(r"\bno\s+(?:more\s+)?(?:title\s+)?cards\b", re.I)
_MORE_CARDS = re.compile(r"\bmore\s+(?:title\s+)?cards\b", re.I)
_FEWER_CARDS = re.compile(r"\b(?:fewer|less)\s+(?:title\s+)?cards\b", re.I)
_NO_MUSIC = re.compile(r"\b(?:no|kill\s+the|lose\s+the|remove\s+the)\s+music\b", re.I)
_ADD_MUSIC = re.compile(r"\badd\s+(?:some\s+|the\s+)?music\b", re.I)
_MUSIC_UP = re.compile(
    r"\b(?:more\s+music|music\s+louder|louder\s+music|turn\s+(?:the\s+)?"
    r"music\s+up|turn\s+up\s+the\s+music|raise\s+the\s+music)\b", re.I)
_MUSIC_DOWN = re.compile(
    r"\b(?:less\s+music|quieter\s+music|music\s+quieter|music\s+softer|"
    r"turn\s+(?:the\s+)?music\s+down|turn\s+down\s+the\s+music|"
    r"lower\s+the\s+music)\b", re.I)
_MUSIC_PROMPT = re.compile(
    r"\b(?:different\s+music\s*:?\s*|make\s+the\s+music\s+)(?P<z>.+?)" + _END,
    re.I)
_NO_GHOST = re.compile(r"\bno\s+(?:more\s+)?ghosts?\b", re.I)
_NO_SFX = re.compile(r"\bno\s+(?:more\s+)?(?:bings?|sound\s+effects?|sfx)\b",
                     re.I)
MUSIC_STEP_DB = 4.0
MUSIC_MIN_DB = -30.0
MUSIC_MAX_DB = -10.0
DEFAULT_CARD_S = 2.6
MUSIC_REPLAN_USD = 0.375


def _anchor(quote: str, words: list[OutWord]) -> tuple[float | None, dict]:
    found = locate_quote(quote, words)
    if found is None:
        return None, {"quote": quote, "word_start": None, "word_end": None}
    start, end = found
    return (round(words[start].start_s - 0.10, 3),
            {"quote": quote, "word_start": start, "word_end": end})


def _strip_text(text: str) -> str:
    return " ".join(str(text or "").strip().strip("\"'“”").split())


def parse_note(text: str, *, job_dir: Path | None = None,
               playhead_s: float | None = None,
               words=None) -> list[dict] | None:  # noqa: ANN001
    """A typed or spoken dress note -> dress ops, or None. Offline.

    Deterministic regex plus quote matching; first match wins. Ops are
    unchecked here (`validate_op` adds checks).
    """
    raw = " ".join(str(text or "").split())
    if not raw:
        return None
    if words is None:
        words = load_output_words(Path(job_dir)) if job_dir else []
    out_words = list(words or [])
    if out_words and not isinstance(out_words[0], OutWord):
        out_words = _as_out_words(out_words)
    at = None
    if playhead_s is not None:
        try:
            at = round(float(playhead_s), 3)
        except (TypeError, ValueError):
            at = None

    m = _ADD_SAYS.search(raw)
    if m:
        card_text = _strip_text(m.group("x"))
        quote = _strip_text(m.group("y") or "")
        if quote:
            t, anchor = _anchor(quote, out_words)
        else:
            t, anchor = at, {"quote": "", "word_start": None,
                             "word_end": None}
        return [{"op": "dress_add_card", "text": card_text, "t": t,
                 "d": DEFAULT_CARD_S, "anchor": anchor, "source": "typed"}]
    m = _ADD_WHERE.search(raw)
    if m:
        quote = _strip_text(m.group("y"))
        t, anchor = _anchor(quote, out_words)
        tokens = [_clean_word(w) for w in quote.split()]
        card_text = " ".join(w[:1].upper() + w[1:] for w in tokens if w)
        card_text = " ".join(card_text.split()[:6])
        return [{"op": "dress_add_card", "text": card_text, "t": t,
                 "d": DEFAULT_CARD_S, "anchor": anchor, "source": "typed"}]
    m = _REMOVE.search(raw)
    if m:
        quote = _strip_text(m.group("y") or "")
        t = _anchor(quote, out_words)[0] if quote else at
        return [{"op": "dress_remove_card", "t": t, "quote": quote}]
    if _NO_CARDS.search(raw):
        return [{"op": "dress_set_cards", "mode": "off"}]
    if _FEWER_CARDS.search(raw):
        return [{"op": "dress_set_cards", "mode": "fewer"}]
    if _MORE_CARDS.search(raw):
        return [{"op": "dress_set_cards", "mode": "more"}]
    if _NO_MUSIC.search(raw):
        return [{"op": "dress_set_music", "enabled": False}]
    if _MUSIC_DOWN.search(raw):
        return [{"op": "dress_set_music", "gain_delta_db": -MUSIC_STEP_DB}]
    if _MUSIC_UP.search(raw):
        return [{"op": "dress_set_music", "gain_delta_db": MUSIC_STEP_DB}]
    if _ADD_MUSIC.search(raw):
        return [{"op": "dress_set_music", "enabled": True}]
    m = _MUSIC_PROMPT.search(raw)
    if m:
        prompt = _strip_text(m.group("z"))
        if prompt:
            return [{"op": "dress_set_music", "prompt": prompt[:300]}]
    if _NO_GHOST.search(raw):
        return [{"op": "dress_set_ghost", "enabled": False}]
    if _NO_SFX.search(raw):
        return [{"op": "dress_set_sfx", "enabled": False}]
    return None


def _check(ok: bool, text: str, hard: bool = True) -> dict:
    """The voice._blocked/_passed shape {ok, text, hard}."""
    return {"ok": bool(ok), "text": text, "hard": bool(hard and not ok)}


def _plan_duration(job_dir: Path | None, plan_payload: dict | None) -> float:
    try:
        value = float(((plan_payload or {}).get("source") or {}).get(
            "duration_s") or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    if value > 0 or job_dir is None:
        return value
    job = Path(job_dir)
    source = source_video(job)
    return _duration(job, source if source.exists() else None)


def _enabled_cards(plan_payload: dict | None) -> list[dict]:
    return [c for c in ((plan_payload or {}).get("cards") or [])
            if isinstance(c, dict) and c.get("enabled", True)]


def _nearest_card(cards: list[dict], t: float | None) -> dict | None:
    if t is None or not cards:
        return None
    return min(cards, key=lambda c: abs(
        float(c.get("t", 0.0)) + float(c.get("d", 0.0)) / 2 - t))


def op_label(op: dict) -> str:
    """The one-line label the proposal card shows. Pure."""
    name = op.get("op", "")
    t = op.get("t")
    at = f" @{float(t):.1f}s" if isinstance(t, (int, float)) else ""
    if name == "dress_add_card":
        return f"Card: {op.get('text', '')}{at}"
    if name == "dress_remove_card":
        target = op.get("card_text") or op.get("quote") or "nearest card"
        return f"Remove card: {target}{at}"
    if name == "dress_set_cards":
        return {"off": "Cards: off", "more": "Cards: plan more",
                "fewer": "Cards: keep every other"}.get(
                    op.get("mode"), "Cards")
    if name == "dress_set_music":
        if "prompt" in op:
            return f"Music: {op['prompt'][:60]}"
        if "gain_delta_db" in op:
            delta = float(op["gain_delta_db"])
            label = f"Music {'up' if delta > 0 else 'down'} {abs(delta):.0f} dB"
            if "gain_db" in op:
                label += f" (to {float(op['gain_db']):.0f} dB)"
            return label
        return "Music: on" if op.get("enabled") else "Music: off"
    if name == "dress_set_ghost":
        return "Ghost: on" if op.get("enabled") else "Ghost: off"
    if name == "dress_set_sfx":
        return "Bing: on" if op.get("enabled") else "Bing: off"
    return name


def validate_op(op: dict, *, job_dir: Path | None = None,
                plan_payload: dict | None = None,
                duration_s: float | None = None) -> dict:
    """Code decides: a checked copy with checks, blocked, label, why."""
    from .voice import UNPLACED_MESSAGE  # read-only import

    out = dict(op)
    if plan_payload is None and job_dir is not None:
        plan_payload = load_plan(Path(job_dir))
    duration = float(duration_s) if duration_s else _plan_duration(
        job_dir, plan_payload)
    cards = _enabled_cards(plan_payload)
    checks: list[dict] = []
    name = out.get("op", "")
    if name == "dress_add_card":
        text = _strip_text(out.get("text", ""))
        out["text"] = text
        n_words = len(text.split())
        checks.append(_check(1 <= n_words <= 6 and not _is_pathlike(text),
                             f"Text is {n_words} word(s), plain"
                             if 1 <= n_words <= 6 and not _is_pathlike(text)
                             else "Card text must be 1 to 6 plain words"))
        t = out.get("t")
        if t is None:
            checks.append(_check(False, UNPLACED_MESSAGE))
        else:
            t = round(max(0.0, float(t)), 3)
            d = min(LIMITS["max_card_s"], max(LIMITS["min_card_s"], float(
                out.get("d") or DEFAULT_CARD_S)))
            if duration and t + d > duration - 1.0:
                d = max(LIMITS["min_card_s"], round(duration - 1.0 - t, 3))
            out["t"], out["d"] = t, round(d, 3)
            checks.append(_check(t >= FIRST_CARD_S - 1e-9,
                                 f"Starts after the title ({FIRST_CARD_S:.1f}s)"
                                 if t >= FIRST_CARD_S - 1e-9 else
                                 f"Inside the title (cards start at {FIRST_CARD_S:.1f}s)"))
            fits = not duration or t + d <= duration - 1.0 + 1e-9
            checks.append(_check(fits, "Ends before the last second" if fits
                                 else "Too close to the end of the cut"))
            clash = None
            for other in cards:
                ot, od = float(other.get("t", 0.0)), float(other.get("d", 0.0))
                if not (t >= ot + od + LIMITS["min_gap_s"] - 1e-9
                        or ot >= t + d + LIMITS["min_gap_s"] - 1e-9):
                    clash = other
                    break
            checks.append(_check(clash is None,
                                 "Clear of the other cards" if clash is None
                                 else f"Overlaps the card “{clash.get('text', '')}”"))
        room = len(cards) < LIMITS["max_cards"]
        checks.append(_check(room, f"{len(cards) + 1} of {LIMITS['max_cards']} cards"
                             if room else f"Already {LIMITS['max_cards']} cards"))
        anchor = out.get("anchor") or {}
        out["why"] = (f"Anchored where you say “{anchor['quote']}”"
                      if anchor.get("word_start") is not None
                      else "At the playhead" if t is not None else "")
    elif name == "dress_remove_card":
        target = _nearest_card(cards, out.get("t"))
        if target is None:
            checks.append(_check(False, "No card there to remove"
                                 if cards else "There are no cards yet"))
        else:
            out["card_id"] = target.get("id")
            out["card_text"] = target.get("text", "")
            out["t"] = target.get("t")
            checks.append(_check(True, f"Card “{target.get('text', '')}”"))
        out["why"] = "The card nearest to that moment"
    elif name == "dress_set_cards":
        mode = out.get("mode")
        if mode == "fewer":
            checks.append(_check(len(cards) >= 2, f"Keeps {(len(cards) + 1) // 2} of {len(cards)}"
                                 if len(cards) >= 2 else "Fewer than two cards"))
        elif mode == "more":
            checks.append(_check(len(cards) < LIMITS["max_cards"],
                                 "Adds cards with the built-in planner"
                                 if len(cards) < LIMITS["max_cards"]
                                 else f"Already {LIMITS['max_cards']} cards"))
        else:
            checks.append(_check(True, f"Hides {len(cards)} card(s)"))
        out["why"] = "Card plan"
    elif name == "dress_set_music":
        music = (plan_payload or {}).get("music") or {}
        if "gain_delta_db" in out:
            current = float(music.get("gain_db", MUSIC_GAIN_DEFAULT))
            target = max(MUSIC_MIN_DB, min(MUSIC_MAX_DB,
                                           current + float(out["gain_delta_db"])))
            out["gain_db"] = round(target, 1)
            moved = abs(target - current) > 1e-9
            checks.append(_check(moved, f"Bed {current:.0f} → {target:.0f} dB"
                                 if moved else f"Already at the limit ({current:.0f} dB)"))
        elif "prompt" in out:
            checks.append(_check(True, "New bed prompt"))
            checks.append({"ok": False, "hard": False,
                           "text": f"The next dress plan regenerates it (costs about ${MUSIC_REPLAN_USD})"})
        else:
            checks.append(_check(True, "Music on" if out.get("enabled") else "Music off"))
        out["why"] = "Music bed"
    elif name in ("dress_set_ghost", "dress_set_sfx"):
        checks.append(_check(True, op_label(out)))
        out["why"] = "Ghost pop-up" if name == "dress_set_ghost" else "Card bing"
    else:
        checks.append(_check(False, "Not a dress change"))
    out["checks"] = checks
    out["blocked"] = any(not c["ok"] and c["hard"] for c in checks)
    out["label"] = op_label(out)
    return out


def _empty_plan(job: Path) -> dict:
    source = source_video(job)
    duration = _duration(job, source if source.exists() else None)
    if source.exists():
        return _default_plan(job, source, duration)
    payload = _default_plan(job, job / "cut.mp4", duration)
    return payload


def apply_ops(job_dir: Path, ops: list[dict]) -> dict:
    """Apply checked, unblocked dress ops to dress.json. Returns it."""
    job = Path(job_dir)
    payload = load_plan(job) or _empty_plan(job)
    cards = [c for c in payload.get("cards", []) or [] if isinstance(c, dict)]
    history = list(payload.get("history", []) or [])
    notes = list(payload.get("notes", []) or [])
    now = datetime.now().isoformat(timespec="seconds")
    for op in ops or []:
        if not isinstance(op, dict) or op.get("blocked"):
            continue
        name = op.get("op", "")
        if name == "dress_add_card" and op.get("t") is not None:
            used = {str(c.get("id")) for c in cards}
            n = len(cards) + 1
            while f"c{n}" in used:
                n += 1
            enabled = [c for c in cards if c.get("enabled", True)]
            cards.append({
                "id": f"c{n}", "t": float(op["t"]),
                "d": float(op.get("d") or DEFAULT_CARD_S),
                "text": str(op.get("text", "")),
                "pos": op.get("pos") or ("top-left" if len(enabled) % 2 == 0
                                         else "top-right"),
                "ghost": True,
                "anchor": op.get("anchor") or {},
                "source": op.get("source") or "typed",
                "why": str(op.get("why", "")), "enabled": True})
        elif name == "dress_remove_card":
            cards = [c for c in cards if c.get("id") != op.get("card_id")]
        elif name == "dress_set_cards":
            mode = op.get("mode")
            if mode == "off":
                for card in cards:
                    card["enabled"] = False
            elif mode == "fewer":
                on = [c for c in cards if c.get("enabled", True)]
                for i, card in enumerate(on):
                    if i % 2 == 1:
                        card["enabled"] = False
            elif mode == "more":
                for card in cards:
                    card["enabled"] = True
                words = load_output_words(job)
                duration = _plan_duration(job, payload)
                room = LIMITS["max_cards"] - len(cards)
                if room > 0:
                    cards.extend(rule_cards(words, duration, existing=cards,
                                            max_cards=room))
        elif name == "dress_set_music":
            music = dict(payload.get("music") or {})
            if "enabled" in op:
                music["enabled"] = bool(op["enabled"])
            if "gain_db" in op:
                music["gain_db"] = float(op["gain_db"])
            elif "gain_delta_db" in op:
                music["gain_db"] = max(MUSIC_MIN_DB, min(MUSIC_MAX_DB, float(
                    music.get("gain_db", MUSIC_GAIN_DEFAULT))
                    + float(op["gain_delta_db"])))
            if op.get("prompt"):
                music["prompt"] = str(op["prompt"])
                music["asset"] = None
                payload["assets"] = [a for a in payload.get("assets", []) or []
                                     if a.get("kind") != "music"]
                notes.append("dress_music_replan: run director dress plan to "
                             f"generate the new bed (about ${MUSIC_REPLAN_USD})")
            payload["music"] = music
        elif name == "dress_set_ghost":
            ghost = dict(payload.get("ghost") or {})
            ghost["enabled"] = bool(op.get("enabled"))
            payload["ghost"] = ghost
        elif name == "dress_set_sfx":
            ghost = dict(payload.get("ghost") or {})
            ghost["sfx"] = bool(op.get("enabled"))
            payload["ghost"] = ghost
        else:
            continue
        history.append({"op": name, "note_id": op.get("note_id"), "at": now})
    cards.sort(key=lambda c: float(c.get("t", 0.0)))
    payload["cards"] = cards
    payload["history"] = history
    payload["notes"] = notes
    save_plan(job, payload)
    return payload


def set_card(job_dir: Path, card_id: str, *, enabled: bool | None = None,
             remove: bool = False) -> dict:
    """The panel's toggle and Remove. Returns dress.json."""
    job = Path(job_dir)
    payload = load_plan(job)
    if payload is None:
        raise DressError("dress: no dress.json; plan first")
    cards = [c for c in payload.get("cards", []) or [] if isinstance(c, dict)]
    if not any(c.get("id") == card_id for c in cards):
        raise DressError(f"dress: no card {card_id}")
    if remove:
        cards = [c for c in cards if c.get("id") != card_id]
    elif enabled is not None:
        for card in cards:
            if card.get("id") == card_id:
                card["enabled"] = bool(enabled)
    payload["cards"] = cards
    payload.setdefault("history", []).append({
        "op": "dress_remove_card" if remove else "dress_toggle_card",
        "note_id": None, "at": datetime.now().isoformat(timespec="seconds")})
    return save_plan(job, payload)


def summary(job_dir: Path) -> dict:
    """The compact state the UI shows. Never raises on a broken plan."""
    job = Path(job_dir)
    payload = load_plan(job)
    if payload is None:
        return {"exists": False, "cards": [], "fresh": False,
                "stale": False}
    music = payload.get("music") or {}
    ghost = payload.get("ghost") or {}
    fresh = is_fresh(job, payload)
    return {
        "exists": True,
        "cards": [{"id": c.get("id"), "t": c.get("t"), "d": c.get("d"),
                   "text": c.get("text", ""), "enabled": c.get("enabled", True),
                   "source": c.get("source", "")}
                  for c in payload.get("cards", []) or [] if isinstance(c, dict)],
        "music": {"enabled": bool(music.get("enabled", True)),
                  "gain_db": music.get("gain_db", MUSIC_GAIN_DEFAULT),
                  "prompt": music.get("prompt", ""),
                  "has_asset": bool(music.get("asset"))},
        "ghost": {"enabled": bool(ghost.get("enabled", True)),
                  "sfx": bool(ghost.get("sfx", True))},
        "design": payload.get("design") or {},
        "provider_spend_usd": payload.get("provider_spend_usd", 0.0),
        "notes": list(payload.get("notes", []) or []),
        "warnings": list(payload.get("warnings", []) or []),
        "fresh": fresh,
        "stale": (job / "cut.dressed.mp4").exists() and not fresh,
    }
