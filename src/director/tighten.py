"""Pick the takes, lose the flubs: words become beats, one kept take each.

The recording's words are grouped into utterances (short sentence/pause
units), utterances into windows, and per window Muse Spark watches a proxy
(picture plus production audio) beside the numbered utterances and groups
them into beats — one point the speaker meant to make once — picking the
take an editor would publish. Mechanical verification repairs or discards
each reply; the rule fallback keeps the last full pass of each beat. Kept
spans, pause trims and the removed complement are computed by
`cutlist.recompute_spans`, so every edit path agrees on what stays.

Designed by Muse Code on muse-wonder-1.0, built by Muse Code on
muse-spark-1.3 from docs/muse-code/TIGHTEN-DESIGN.md.
"""
from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from ghostcut.config import read_json
from ghostcut.ffmpeg_tools import ToolingError, run_command

from . import angles as angles_mod
from . import cutlist
from .cutlist import DEAD_AIR_S, MIN_KEPT_SPAN_S
from .cutlist import HEAD_PAD_S, PAUSE_KEEP_S, TAIL_PAD_S  # noqa: F401 (re-exported §3)

MAX_UTTERANCE_S = 6.0
UTTERANCE_PAUSE_S = 1.2
WINDOW_MAX_S = 240.0
WINDOW_MAX_UTTERANCES = 60
WINDOW_TAIL_S = 15.0
PROXY_FPS = 5
PROXY_SHORT_SIDE = 540
PROXY_AUDIO_KBPS = 64
INPUT_TOKENS_PER_S = 300
MAX_OUTPUT_TOKENS = 16000
TOKEN_RATES_USD_PER_1M = {
    "muse-spark-1.3": (1.25, 4.25),
    "muse-wonder-1.0": (4.00, 20.00),
}
DEFAULT_PAUSE_LIMIT_S = 0.8
BEAT_SIM_MIN = 0.45
WINDOW_RULE_FALLBACK_SHARE = 0.25

FILLERS = frozenset({"um", "uh", "erm"})
RESTART_MARKERS = ("again", "one more time", "let me redo", "start again",
                   "cut that")
REMOVED_ATTEMPT_KINDS = ("flub", "restart", "crew_talk", "aside", "setup",
                         "dead_air")
SWAP_BLOCKED_KINDS = ("flub", "restart", "crew_talk", "aside", "setup")

DEVELOPER = (
    "You are Muse, the assistant editor inside GhostCut Director. You are "
    "watching one window of a raw talking-head recording — the speaker "
    "records the same points several times, restarts sentences, talks to "
    "the crew, and pauses to think. Below the video is the window's "
    "transcript as numbered utterances with times; the transcript may "
    "mishear words, so judge what was meant from the audio and picture, "
    "and use the numbers only to locate things. Group the utterances into "
    "beats: one beat is one point the speaker meant to make once, and each "
    "attempt at it is a run of consecutive utterances. For every beat pick "
    "exactly one attempt — the one an editor would publish — and give a "
    "one-sentence reason an editor would accept, naming what beat the "
    "others (\"clean delivery\", \"second pass, first one stumbles on "
    "'timecode'\"). Mark every other attempt as an alternate (usable, not "
    "chosen) or as removed with its kind: a flub or false start, a restart "
    "that abandons the line, crew talk, an aside to the editor or crew, "
    "setup or technical talk, or dead air. Judge delivery from the sound "
    "and the picture together: stumbles and trailing off, eyes on the "
    "lens, a clean start and a finished ending. Prefer a complete attempt "
    "over a slightly livelier incomplete one. Never invent words, people "
    "or events you cannot hear or see, and never leave an utterance out: "
    "every numbered utterance in the window belongs to exactly one attempt."
)


class TightenError(RuntimeError):
    """A tighten step failed in a way the operator must see.

    The message always starts with 'tighten: '.
    """


class MuseUnavailable(RuntimeError):
    """guestlist (the Meta API client) cannot be imported."""


class WindowReplyError(RuntimeError):
    """A Spark window reply has no usable beats list (caller falls back)."""


@dataclass(slots=True)
class Utterance:
    id: int
    start_s: float
    end_s: float
    text: str
    word_start: int
    word_end: int


@dataclass(slots=True)
class Window:
    id: int
    utterance_ids: list[int]
    start_s: float
    end_s: float


# ------------------------------------------------------- utterances (§6.1)


def build_utterances(words: list[angles_mod.TimedWord]) -> list[Utterance]:
    """Sentence/pause units of at most MAX_UTTERANCE_S, numbered U0... Pure."""
    if not words:
        raise TightenError("tighten: no words to tighten; "
                           "the words stage produced an empty transcript")
    out: list[Utterance] = []

    def emit(part: list[angles_mod.TimedWord], start_idx: int) -> None:
        out.append(Utterance(len(out), part[0].start_s, part[-1].end_s,
                             " ".join(w.text for w in part),
                             start_idx, start_idx + len(part)))

    cur: list[angles_mod.TimedWord] = []
    cur_idx = 0
    for i, word in enumerate(words):
        if not cur:
            cur, cur_idx = [word], i
            continue
        prev = cur[-1]
        gap = word.start_s - prev.end_s
        if (angles_mod._sentence_end(prev.text)
                or gap > angles_mod.PAUSE_S + 1e-9
                or gap >= UTTERANCE_PAUSE_S):
            emit(cur, cur_idx)
            cur, cur_idx = [word], i
            continue
        if word.end_s - cur[0].start_s > MAX_UTTERANCE_S:
            acc = cur + [word]
            gaps = [acc[k + 1].start_s - acc[k].end_s
                    for k in range(len(acc) - 1)]
            if not gaps or max(gaps) - min(gaps) < 1e-9 or max(gaps) <= 1e-9:
                emit(cur, cur_idx)
                cur, cur_idx = [word], i
                continue
            k = angles_mod._longest_pause_split(acc)
            emit(acc[:k], cur_idx)
            cur, cur_idx = acc[k:], cur_idx + k
            continue
        cur.append(word)
    if cur:
        emit(cur, cur_idx)
    return out


# ------------------------------------------------------------- windows (§6.3)


def build_windows(utterances: list[Utterance]) -> list[Window]:
    """Greedy windows of <= WINDOW_MAX_S and <= WINDOW_MAX_UTTERANCES. Pure."""
    windows: list[Window] = []
    pos, total = 0, len(utterances)
    while pos < total:
        start = utterances[pos].start_s
        end = pos
        while (end + 1 < total
               and end + 1 - pos + 1 <= WINDOW_MAX_UTTERANCES
               and utterances[end + 1].end_s - start <= WINDOW_MAX_S):
            end += 1
        if end + 1 < total:
            gap_after = (utterances[end + 1].start_s
                         - utterances[end].end_s)
            if gap_after < DEAD_AIR_S:
                window_end = utterances[end].end_s
                best: tuple[float, int] | None = None
                for k in range(pos + 1, end + 1):
                    gap = (utterances[k].start_s
                           - utterances[k - 1].end_s)
                    if (gap >= 0.6
                            and utterances[k].start_s
                            >= window_end - WINDOW_TAIL_S
                            and (best is None or gap > best[0]
                                 or (gap == best[0] and k > best[1]))):
                        best = (gap, k)
                if best is not None:
                    end = best[1] - 1
        core = [u.id for u in utterances[pos:end + 1]]
        windows.append(Window(
            len(windows), core,
            max(0.0, utterances[pos].start_s - 1.0),
            utterances[end].end_s + 1.0))
        pos = end + 1
    return windows


# -------------------------------------------------------- rule fallback (§7.5)


def normalize_tokens(text: str) -> list[str]:
    """Lower-case, punctuation stripped, fillers (um, uh, erm) dropped. Pure."""
    return [token for token in re.findall(r"[a-z0-9']+", text.lower())
            if token not in FILLERS]


def text_similarity(a: str, b: str) -> float:
    """difflib.SequenceMatcher ratio over the normalised token strings. Pure."""
    return difflib.SequenceMatcher(None, " ".join(normalize_tokens(a)),
                                   " ".join(normalize_tokens(b))).ratio()


def _is_restart(text: str) -> bool:
    head = " ".join(normalize_tokens(text)[:3])
    return any(head == marker or head.startswith(marker + " ")
               for marker in RESTART_MARKERS)


def rule_beats(utterances: list[Utterance]) -> list[cutlist.Beat]:
    """Last complete pass per beat; restart markers and similarity group. Pure."""
    groups: list[list[Utterance]] = []
    representatives: list[str] = []
    for utterance in utterances:
        if not groups:
            groups.append([utterance])
            representatives.append(utterance.text)
            continue
        gap = utterance.start_s - groups[-1][-1].end_s
        if gap > 20.0 or text_similarity(
                utterance.text, representatives[-1]) < BEAT_SIM_MIN:
            groups.append([utterance])
            representatives.append(utterance.text)
        else:
            groups[-1].append(utterance)
            if len(utterance.text) > len(representatives[-1]):
                representatives[-1] = utterance.text
    beats: list[cutlist.Beat] = []
    for number, group in enumerate(groups):
        complete = []
        for pos, utterance in enumerate(group):
            tokens = utterance.text.split()
            complete.append(
                bool(tokens) and angles_mod._sentence_end(tokens[-1])
                or pos == len(group) - 1)
        pick = max(i for i, done in enumerate(complete) if done)
        attempts: list[cutlist.Attempt] = []
        for pos, utterance in enumerate(group):
            if pos == pick:
                verdict, kind = "pick", "keeper"
                reason = "rule: last complete pass"
            elif complete[pos]:
                verdict, kind = "alternate", "alternate"
                reason = "rule: complete alternate pass"
            elif _is_restart(utterance.text):
                verdict, kind = "remove", "restart"
                reason = "rule: incomplete attempt"
            else:
                verdict, kind = "remove", "flub"
                reason = "rule: incomplete attempt"
            attempts.append(cutlist.Attempt(
                pos, [utterance.id], utterance.start_s, utterance.end_s,
                utterance.text, verdict, kind, reason, 0.5))
        pick_text = attempts[pick].text
        topic = pick_text if len(pick_text) <= 60 else pick_text[:57] + "..."
        priority = 5 if (number == 0 or number == len(groups) - 1) else 3
        beats.append(cutlist.Beat(number, topic, attempts, pick, "kept",
                                  priority, "rule: last complete pass"))
    return beats


# ------------------------------------------------- verification (§7.4)


def _clamp_confidence(value: object) -> float:
    try:
        confidence = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(confidence):
        return 0.0
    return min(1.0, max(0.0, confidence))


def _clamp_priority(value: object) -> int:
    try:
        priority = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 3
    return min(5, max(1, priority))


def verify_window_reply(reply: dict, window: Window,
                        utterances: list[Utterance], *,
                        beat_id_offset: int = 0
                        ) -> tuple[list[cutlist.Beat], list[str]]:
    """Repair a Spark window reply per §7.4; rule beats past the threshold.

    `beat_id_offset` is the global id of this window's first beat, so the
    below-minimum warning names the payload-level beat. A reply with no
    beats list raises WindowReplyError (the caller falls back per window).
    """
    by_id = {u.id: u for u in utterances}
    core = list(window.utterance_ids)
    core_set = set(core)
    if not isinstance(reply, dict) or not isinstance(reply.get("beats"), list):
        raise WindowReplyError(f"window {window.id} reply has no beats list")
    touched: set[int] = set()
    parsed: list[tuple[dict, list[tuple[int, int, dict, list[str]]]]] = []
    claimed: set[int] = set()
    floor, ceil = min(core), max(core)
    for raw_beat in reply["beats"]:
        if not isinstance(raw_beat, dict):
            continue
        raw_attempts = raw_beat.get("attempts", [])
        if not isinstance(raw_attempts, list):
            raw_attempts = []
        attempts: list[tuple[int, int, dict, list[str]]] = []
        for raw in raw_attempts:
            if not isinstance(raw, dict):
                continue
            try:
                first = int(raw.get("first_utterance"))  # type: ignore[arg-type]
                last = int(raw.get("last_utterance"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            original = list(range(first, last + 1)) if last >= first else []
            clamped = (max(first, floor), min(last, ceil))
            if clamped[1] < clamped[0]:
                touched.update(i for i in original if i in core_set)
                continue
            if clamped != (first, last):
                touched.update(i for i in original if i in core_set)
            free = [i for i in range(clamped[0], clamped[1] + 1)
                    if i not in claimed]
            if not free:
                touched.update(range(clamped[0], clamped[1] + 1))
                continue
            runs = [[free[0]]]
            for uid in free[1:]:
                if uid == runs[-1][-1] + 1:
                    runs[-1].append(uid)
                else:
                    runs.append([uid])
            run = max(runs, key=len)
            notes: list[str] = []
            if len(run) < clamped[1] - clamped[0] + 1:
                notes.append("overlap_trimmed")
                touched.update(set(range(clamped[0], clamped[1] + 1))
                               - set(run))
            claimed.update(run)
            attempts.append((run[0], run[-1], raw, notes))
        attempts.sort(key=lambda item: item[0])
        if attempts:
            parsed.append((raw_beat, attempts))
    if not parsed:
        touched.update(core)
        return (rule_beats([by_id[i] for i in sorted(core)]),
                [f"tighten_window_rule_fallback:{window.id}"])
    parsed.sort(key=lambda item: item[1][0][0])
    # Coverage: every core utterance in exactly one attempt. An unclaimed
    # utterance joins the preceding attempt (or the first one when it
    # comes before everything): it extends that attempt's span but is
    # recorded in `joined` so it never contributes kept words.
    ordered: list[tuple[int, int]] = [(bi, ai)
                                     for bi, (_, attempts) in enumerate(parsed)
                                     for ai in range(len(attempts))]
    joined: set[int] = set()
    for uid in sorted(set(core) - claimed):
        target: tuple[int, int] | None = None
        for bi, ai in ordered:
            if parsed[bi][1][ai][1] < uid:
                target = (bi, ai)
        if target is None:
            target = ordered[0]
        bi, ai = target
        first, last, raw, notes = parsed[bi][1][ai]
        parsed[bi][1][ai] = (min(first, uid), max(last, uid), raw, notes)
        claimed.add(uid)
        joined.add(uid)
        touched.add(uid)
    beats: list[cutlist.Beat] = []
    warnings: list[str] = []
    for local, (raw_beat, attempts) in enumerate(parsed):
        rows: list[dict] = []
        for pos, (first, last, raw, notes) in enumerate(attempts):
            uids = list(range(first, last + 1))
            rows.append({
                "pos": pos, "uids": uids,
                "verdict": str(raw.get("verdict", "")),
                "kind": str(raw.get("kind", "")),
                "reason": str(raw.get("reason", "")),
                "confidence": _clamp_confidence(raw.get("confidence", 0.0)),
                "notes": list(notes),
            })
        picks = [row for row in rows if row["verdict"] == "pick"]
        if not picks:
            alternates = [row for row in rows
                          if row["verdict"] == "alternate"]
            if alternates:
                promoted = max(alternates,
                               key=lambda row: (row["confidence"], -row["pos"]))
                promoted["verdict"] = "pick"
                promoted["notes"].append("pick_promoted")
                touched.update(promoted["uids"])
                picks = [promoted]
        elif len(picks) > 1:
            for demoted in picks[1:]:
                demoted["verdict"] = "alternate"
                demoted["kind"] = "alternate"
                demoted["notes"].append("extra_pick_demoted")
                touched.update(demoted["uids"])
            picks = picks[:1]
        for row in rows:
            if row["verdict"] == "pick" and row["kind"] != "keeper":
                row["kind"] = "keeper"
                row["notes"].append("pick_kind_coerced")
                touched.update(row["uids"])
            elif row["verdict"] == "alternate" and row["kind"] != "alternate":
                row["kind"] = "alternate"
                row["notes"].append("kind_coerced")
                touched.update(row["uids"])
            elif (row["verdict"] == "remove"
                    and row["kind"] not in REMOVED_ATTEMPT_KINDS):
                row["kind"] = "flub"
                row["notes"].append("kind_coerced")
                touched.update(row["uids"])
        spans = {row["pos"]: (by_id[row["uids"][-1]].end_s
                              - by_id[row["uids"][0]].start_s)
                 for row in rows}
        pick_index = -1
        status = "no_usable_attempt"
        if picks:
            pick_index = picks[0]["pos"]
            status = "kept"
            if spans[pick_index] < MIN_KEPT_SPAN_S:
                usable = [row for row in rows
                          if row["verdict"] == "alternate"]
                if usable:
                    longest = max(usable,
                                  key=lambda row: (spans[row["pos"]],
                                                   -row["pos"]))
                    picks[0]["verdict"] = "alternate"
                    picks[0]["kind"] = "alternate"
                    picks[0]["notes"].append("pick_too_short")
                    touched.update(picks[0]["uids"])
                    longest["verdict"] = "pick"
                    longest["kind"] = "keeper"
                    longest["notes"].append("pick_promoted")
                    touched.update(longest["uids"])
                    pick_index = longest["pos"]
                if spans[pick_index] < MIN_KEPT_SPAN_S:
                    warnings.append(
                        "tighten_beat_below_min:"
                        f"B{beat_id_offset + local}")
        built: list[cutlist.Attempt] = []
        for row in rows:
            uids = row["uids"]
            excluded = sorted(set(uids) & joined)
            text = " ".join(by_id[uid].text for uid in uids
                            if uid not in joined)
            built.append(cutlist.Attempt(
                row["pos"], uids,
                by_id[uids[0]].start_s, by_id[uids[-1]].end_s, text,
                row["verdict"], row["kind"], row["reason"],
                row["confidence"], row["notes"], excluded))
        # The schema carries no beat-level reason: the beat's reason is
        # why the pick won, i.e. the pick attempt's reason.
        reason = str(built[pick_index].reason) if pick_index >= 0 else ""
        beats.append(cutlist.Beat(
            local, str(raw_beat.get("topic", "")), built, pick_index,
            status, _clamp_priority(raw_beat.get("keep_priority", 3)),
            reason))
    if core and len(touched) / len(core) > WINDOW_RULE_FALLBACK_SHARE:
        return (rule_beats([by_id[i] for i in sorted(core)]),
                [f"tighten_window_rule_fallback:{window.id}"])
    return beats, warnings


# ------------------------------------------------- prompt and schema (§7.3)


def window_schema() -> dict:
    """Strict schema for the per-window take picks."""
    return {
        "type": "object", "additionalProperties": False,
        "required": ["summary", "beats"],
        "properties": {
            "summary": {"type": "string"},
            "beats": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["topic", "keep_priority", "pick_attempt",
                             "attempts"],
                "properties": {
                    "topic": {"type": "string"},
                    "keep_priority": {"type": "integer",
                                      "enum": [1, 2, 3, 4, 5]},
                    "pick_attempt": {"type": "integer"},
                    "attempts": {"type": "array", "minItems": 1, "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["first_utterance", "last_utterance",
                                     "verdict", "kind", "reason",
                                     "confidence"],
                        "properties": {
                            "first_utterance": {"type": "integer"},
                            "last_utterance": {"type": "integer"},
                            "verdict": {"type": "string",
                                        "enum": ["pick", "alternate",
                                                 "remove"]},
                            "kind": {"type": "string",
                                     "enum": ["keeper", "alternate", "flub",
                                              "restart", "crew_talk", "aside",
                                              "setup", "dead_air"]},
                            "reason": {"type": "string"},
                            "confidence": {"type": "number"},
                        },
                    }},
                },
            }},
        },
    }


def window_prompt(window: Window, utterances: list[Utterance],
                  prev_beat: cutlist.Beat | None,
                  taste: list[str]) -> str:
    """The user prompt for one window: numbered utterances plus context."""
    by_id = {u.id: u for u in utterances}
    core = [by_id[i] for i in window.utterance_ids]
    lines = [f"Pick the takes in this window (utterances "
             f"U{core[0].id}–U{core[-1].id})."]
    if prev_beat is not None:
        pick_text = ""
        if 0 <= prev_beat.pick_attempt < len(prev_beat.attempts):
            pick_text = prev_beat.attempts[prev_beat.pick_attempt].text[:160]
        lines.append(
            f'Previous beat (already decided): "{prev_beat.topic}" — pick: '
            f'"{pick_text}". If the first utterances continue that point, '
            f"group them as a new attempt of a beat with the same topic.")
    for entry in taste or []:
        lines.append(f"Editor preference: {entry}")
    for utterance in core:
        lines.append(f"U{utterance.id} [{utterance.start_s:.1f}-"
                     f"{utterance.end_s:.1f}] {utterance.text}")
    lines.append(f"The video starts 1.0s before U{core[0].id}; utterance "
                 f"times are on the recording's timeline, not the video's.")
    return "\n".join(lines)


def estimate_window_usd(duration_s: float, model: str) -> float:
    """Pre-call Spark estimate for one window. Pure."""
    in_rate, out_rate = TOKEN_RATES_USD_PER_1M.get(
        model, TOKEN_RATES_USD_PER_1M["muse-spark-1.3"])
    return (duration_s * INPUT_TOKENS_PER_S * in_rate / 1e6
            + MAX_OUTPUT_TOKENS * out_rate / 1e6)


# ------------------------------------------------- window proxies (§7.2)


def window_proxy_command(*, video_path: str, video_in_s: float,
                         audio_path: str, audio_in_s: float,
                         duration_s: float, out: Path) -> list[str]:
    """ffmpeg: proxy picture from the video clip, sound from the audio clip."""
    return ["ffmpeg", "-v", "error", "-y",
            "-ss", f"{video_in_s:.3f}", "-i", str(video_path),
            "-ss", f"{audio_in_s:.3f}", "-i", str(audio_path),
            "-t", f"{duration_s:.3f}",
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", f"fps={PROXY_FPS},scale=-2:{PROXY_SHORT_SIDE}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "1",
            "-b:a", f"{PROXY_AUDIO_KBPS}k",
            "-movflags", "+faststart", str(out)]


def make_window_proxy(command: list[str]) -> None:
    """Run a window proxy command; ToolingError degrades that window."""
    run_command(command)


# ------------------------------------------------- the Spark call (§7.3)


def pick_window_with_muse(*, proxy: Path, window: Window,
                          utterances: list[Utterance],
                          prev_beat: cutlist.Beat | None,
                          taste: list[str], model: str,
                          estimate_usd: float) -> tuple[dict, float]:
    """One spark.video_json call for a window. Returns (reply data, spend)."""
    try:
        from guestlist.meta import cache, spark
    except ImportError as error:
        raise MuseUnavailable(str(error)) from error
    cache.guard(estimate_usd)
    result = spark.video_json(
        proxy, schema=window_schema(), name="tighten_picks",
        developer=DEVELOPER,
        prompt=window_prompt(window, utterances, prev_beat, taste),
        model=model, effort="medium", max_output_tokens=MAX_OUTPUT_TOKENS,
        max_usd=estimate_usd)
    return result.data, float(result.usd or 0.0)


# ------------------------------------------------- target length (§9)


def parse_target(text: str) -> float:
    """M:SS, MM:SS, H:MM:SS or bare seconds to seconds. Pure."""
    error = ValueError(f"Bad --target {text!r}: use M:SS or seconds")
    raw = str(text).strip()
    if not raw:
        raise error
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) not in (2, 3):
            raise error
        if len(parts) == 2:
            hours, minutes, seconds = "0", parts[0], parts[1]
        else:
            hours, minutes, seconds = parts
        if not re.fullmatch(r"\d+", hours) or not re.fullmatch(r"\d+", minutes):
            raise error
        if not re.fullmatch(r"\d+(?:\.\d+)?", seconds):
            raise error
        hours_f, minutes_f, seconds_f = (
            float(hours), float(minutes), float(seconds))
        if minutes_f >= 60 or seconds_f >= 60:
            raise error
        return hours_f * 3600 + minutes_f * 60 + seconds_f
    try:
        value = float(raw)
    except ValueError:
        raise error from None
    if not math.isfinite(value) or value < 0:
        raise error
    return value


def apply_target(payload: dict, words: list[angles_mod.TimedWord],
                 target_s: float) -> dict:
    """Drop whole beats until the kept cut fits. Pure. Returns the payload."""
    pause_limit = payload.get("pause_limit_s", DEFAULT_PAUSE_LIMIT_S)
    try:
        pause_limit = float(pause_limit)
    except (TypeError, ValueError):
        pause_limit = DEFAULT_PAUSE_LIMIT_S
    before = float(payload.get("duration_out_s", 0.0))
    dropped: list[int] = []
    while float(payload.get("duration_out_s", 0.0)) > target_s:
        candidates = [b for b in payload.get("beats", []) or []
                      if isinstance(b, dict) and b.get("status") == "kept"
                      and int(b.get("keep_priority", 3)) < 5
                      and int(b.get("pick_attempt", -1)) >= 0]
        if not candidates:
            break
        durations: dict[int, float] = {}
        for span in payload.get("kept_spans", []) or []:
            if not isinstance(span, dict):
                continue
            durations[span.get("beat_id", -1)] = (
                durations.get(span.get("beat_id", -1), 0.0)
                + float(span.get("end_s", 0.0))
                - float(span.get("start_s", 0.0)))
        candidates.sort(key=lambda b: (int(b.get("keep_priority", 3)),
                                       -durations.get(b.get("id", -1), 0.0)))
        victim = candidates[0]
        victim["status"] = "dropped_target"
        dropped.append(victim.get("id", -1))
        cutlist.recompute_spans(payload, words, pause_limit_s=pause_limit)
    after = float(payload.get("duration_out_s", 0.0))
    met = after <= target_s
    payload["target"] = {"requested_s": target_s, "before_s": before,
                         "after_s": after, "met": met,
                         "dropped_beat_ids": dropped}
    if not met:
        warning = f"tighten_target_unmet:{after:.1f}s over {target_s:.1f}s"
        warnings = payload.setdefault("warnings", [])
        if warning not in warnings:
            warnings.append(warning)
    return payload


# ------------------------------------------------- serialization


def _attempt_to_dict(attempt: cutlist.Attempt) -> dict:
    data: dict = {
        "id": attempt.id, "utterance_ids": list(attempt.utterance_ids),
        "start_s": attempt.start_s, "end_s": attempt.end_s,
        "text": attempt.text, "verdict": attempt.verdict,
        "kind": attempt.kind, "reason": attempt.reason,
        "confidence": attempt.confidence,
    }
    if attempt.notes:
        data["notes"] = list(attempt.notes)
    if attempt.excluded_utterance_ids:
        data["excluded_utterance_ids"] = list(
            attempt.excluded_utterance_ids)
    return data


def _beat_to_dict(beat: cutlist.Beat) -> dict:
    return {
        "id": beat.id, "topic": beat.topic, "status": beat.status,
        "keep_priority": beat.keep_priority,
        "pick_attempt": beat.pick_attempt, "reason": beat.reason,
        "attempts": [_attempt_to_dict(a) for a in beat.attempts],
    }


# ------------------------------------------------- orchestrator (§12.3)


def _load_taste(taste_path: Path | None) -> list[str]:
    if taste_path is None or not Path(taste_path).exists():
        return []
    try:
        data = read_json(Path(taste_path))
    except Exception:
        return []
    if not isinstance(data, dict) or not isinstance(
            data.get("entries"), list):
        return []
    return [str(entry.get("text", ""))
            for entry in data["entries"]
            if isinstance(entry, dict) and str(entry.get("text", "")).strip()]


def run(*, sync_path: Path, words_path: Path, out_path: Path,
        audio_label: str | None, pause_limit_s: float,
        target_s: float | None, model: str, no_muse: bool,
        max_usd: float, taste_path: Path | None = None) -> dict:
    """Words -> utterances -> windows -> picks -> spans -> tighten.json.

    Returns the payload (also written).
    """
    sync_path, words_path, out_path = (
        Path(sync_path), Path(words_path), Path(out_path))
    if (isinstance(pause_limit_s, bool)
            or not isinstance(pause_limit_s, (int, float))
            or not 0.30 <= float(pause_limit_s) <= 3.00):
        raise TightenError(
            f"tighten: pause limit {pause_limit_s}s is outside 0.30–3.00s")
    pause_limit = float(pause_limit_s)
    reference, clips = angles_mod.load_sync(sync_path)
    by_label = {clip.label: clip for clip in clips}
    if audio_label is not None:
        audio = by_label.get(audio_label)
        if audio is None:
            raise TightenError(
                f"tighten: unknown --audio label: {audio_label}")
    else:
        audio = next((c for c in clips if c.path == reference), None)
        if audio is None:
            audio = by_label.get("A")
        if audio is None and clips:
            audio = clips[0]
    if audio is None:
        raise TightenError("tighten: sync.json has no clips")
    if audio.offset_s is None:
        raise TightenError(
            f"tighten: audio clip {audio.label} is not placed on the "
            f"timeline; cannot align its words — check the sync warnings")
    words = angles_mod.load_timed_words(words_path)
    if not words:
        raise TightenError(
            f"tighten: no words in {Path(words_path).name}; "
            f"the words stage produced an empty transcript")
    utterances = build_utterances(words)
    windows = build_windows(utterances)
    reference_clip = next((c for c in clips if c.path == reference), None)
    if reference_clip is not None and reference_clip.width > 0:
        video = reference_clip
    else:
        placed = sorted((c for c in clips
                         if c.offset_s is not None and c.width > 0),
                        key=lambda c: c.label)
        video = placed[0] if placed else None
    taste = _load_taste(taste_path)

    warnings: list[str] = []
    if model not in TOKEN_RATES_USD_PER_1M and not no_muse:
        warnings.append(f"tighten_estimate_default_rates:{model}")
    beats_all: list[cutlist.Beat] = []
    rule_positions: set[int] = set()
    spent = 0.0
    any_muse, any_rule = False, False
    if no_muse:
        beats_all = rule_beats(utterances)
        rule_positions = set(range(len(beats_all)))
        any_rule = True
    else:
        muse_dead = False
        prev_beat: cutlist.Beat | None = None
        for window in windows:
            core_set = set(window.utterance_ids)
            owned = sorted((u for u in utterances if u.id in core_set),
                           key=lambda u: u.id)

            def _use_rule() -> None:
                beats = rule_beats(owned)
                rule_positions.update(
                    range(len(beats_all), len(beats_all) + len(beats)))
                beats_all.extend(beats)

            covered = (video is not None and video.offset_s is not None
                       and video.offset_s <= window.start_s + 1e-9
                       and window.end_s
                       <= video.offset_s + video.duration_s + 1e-9)
            if muse_dead or video is None or not covered:
                _use_rule()
                any_rule = True
                if not muse_dead:
                    warnings.append(f"tighten_window_no_video:{window.id}")
                prev_beat = beats_all[-1] if beats_all else prev_beat
                continue
            proxy = (out_path.parent / "tighten-windows"
                     / f"window-{window.id:03d}.mp4")
            proxy.parent.mkdir(parents=True, exist_ok=True)
            duration = window.end_s - window.start_s
            try:
                make_window_proxy(window_proxy_command(
                    video_path=video.path,
                    video_in_s=max(0.0, window.start_s
                                   - (video.offset_s or 0.0)),
                    audio_path=audio.path,
                    audio_in_s=max(0.0, window.start_s
                                   - (audio.offset_s or 0.0)),
                    duration_s=duration, out=proxy))
            except (ToolingError, OSError):
                _use_rule()
                any_rule = True
                warnings.append(
                    f"tighten_window_proxy_failed:{window.id}")
                prev_beat = beats_all[-1] if beats_all else prev_beat
                continue
            estimate = estimate_window_usd(duration, model)
            if spent + estimate > max_usd:
                raise TightenError(
                    f"tighten: estimated Spark cost ${estimate:.2f} for "
                    f"window {window.id} would push spend ${spent:.2f} over "
                    f"--max-usd ${max_usd:.2f}; raise --max-usd or pass "
                    f"--no-muse")
            try:
                data, usd = pick_window_with_muse(
                    proxy=proxy, window=window, utterances=utterances,
                    prev_beat=prev_beat, taste=taste, model=model,
                    estimate_usd=estimate)
            except MuseUnavailable as error:
                muse_dead = True
                warnings.append(f"tighten_muse_unavailable:{error}")
                _use_rule()
                any_rule = True
                prev_beat = beats_all[-1] if beats_all else prev_beat
                continue
            except Exception as error:
                warnings.append("tighten_window_failed:"
                                f"{window.id}:{type(error).__name__}")
                _use_rule()
                any_rule = True
                prev_beat = beats_all[-1] if beats_all else prev_beat
                continue
            spent += usd
            try:
                found, found_warnings = verify_window_reply(
                    data, window, utterances,
                    beat_id_offset=len(beats_all))
            except WindowReplyError:
                warnings.append(
                    f"tighten_window_reply_invalid:{window.id}")
                _use_rule()
                any_rule = True
                prev_beat = beats_all[-1] if beats_all else prev_beat
                continue
            warnings.extend(found_warnings)
            if f"tighten_window_rule_fallback:{window.id}" in found_warnings:
                rule_positions.update(
                    range(len(beats_all), len(beats_all) + len(found)))
                any_rule = True
            else:
                any_muse = True
            beats_all.extend(found)
            prev_beat = beats_all[-1] if beats_all else prev_beat

    for position, beat in enumerate(beats_all):
        beat.id = position
    for position in rule_positions:
        beats_all[position].keep_priority = 3
    if beats_all:
        if 0 in rule_positions:
            beats_all[0].keep_priority = 5
        if len(beats_all) - 1 in rule_positions:
            beats_all[-1].keep_priority = 5
    if any_muse and not any_rule:
        method = "muse"
    elif any_rule and not any_muse:
        method = "rule"
    else:
        method = "mixed"
    if method in ("rule", "mixed"):
        warnings.append("tighten_rule_mode_keeps_asides")

    def _display(path: Path) -> str:
        path = Path(path)
        if path.parent == out_path.parent:
            return path.name
        return str(path)

    payload: dict = {
        "version": 1,
        "sync": _display(sync_path),
        "words": _display(words_path),
        "audio": {"label": audio.label, "clip_path": audio.path,
                  "offset_s": round(audio.offset_s or 0.0, 3)},
        "method": method,
        "model": model,
        "pause_limit_s": pause_limit,
        "target_s": target_s,
        "spend_usd": round(spent, 4),
        "warnings": warnings,
        "duration_in_s": round(words[-1].end_s - words[0].start_s, 3),
        "duration_out_s": 0.0,
        "utterances": [{"id": u.id, "start_s": u.start_s, "end_s": u.end_s,
                        "text": u.text, "word_start": u.word_start,
                        "word_end": u.word_end} for u in utterances],
        "beats": [_beat_to_dict(beat) for beat in beats_all],
        "kept_spans": [],
        "removed_spans": [],
        "angle_overrides": [],
        "edits_applied": [],
        "span_exclusions": [],
        "editor_notes": [],
        "target": None,
    }
    old: dict | None = None
    if out_path.exists():
        try:
            old = cutlist.load_tighten(out_path)
        except Exception:
            old = None
    if old is not None:
        # Editor edits survive re-runs: verbatim state first, then the
        # beat/attempt ops through the one edit path. Span drops are
        # range-keyed state (their old span ids are meaningless after a
        # re-pick), so their exclusions carry over verbatim and only
        # their history entries do — recompute drops stale ranges aloud.
        payload["angle_overrides"] = list(old.get("angle_overrides", []) or [])
        payload["editor_notes"] = list(old.get("editor_notes", []) or [])
        payload["span_exclusions"] = [
            dict(e) for e in old.get("span_exclusions", []) or []
            if isinstance(e, dict)]
        old_edits = [e for e in old.get("edits_applied", []) or []
                     if isinstance(e, dict)]
        payload["edits_applied"] = [
            dict(e) for e in old_edits
            if e.get("op") in ("drop_span", "restore_span")]
        cutlist.recompute_spans(payload, words, pause_limit_s=pause_limit)
        for entry in old_edits:
            if entry.get("op") in ("drop_span", "restore_span"):
                continue
            op = {k: v for k, v in entry.items()
                  if k not in ("id", "at", "source")}
            _, checked = cutlist.apply_operations(
                payload, words, [op],
                source=str(entry.get("source", "editor")))
            if checked and checked[0].get("blocked"):
                warning = f"tighten_edit_stale:{entry.get('id')}"
                if warning not in payload["warnings"]:
                    payload["warnings"].append(warning)
    else:
        cutlist.recompute_spans(payload, words, pause_limit_s=pause_limit)
    if target_s is not None:
        apply_target(payload, words, float(target_s))
    cutlist.write_tighten(out_path, payload)
    return payload
