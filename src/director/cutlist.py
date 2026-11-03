"""The shared "what stays" contract: tighten.json load/write, spans, mapping, edits.

`tighten.json` is the one list of what stays in the cut: the ordered kept
spans on the shared (source) timeline, the removed spans with reasons, the
beats, the picks and the alternates. `tighten.run` writes it, `angles`
renders from it, `cut` manifests it, and editor edits (including the voice
feature) mutate it only through `apply_operations` here, so the render can
never disagree with the file about what stays.

Pure: stdlib plus `ghostcut.config` only. No ffmpeg, no guestlist, no numpy,
and deliberately no import of `angles` (which imports this module): word
lists arrive as `WordLike` objects with `text`/`start_s`/`end_s` attributes.

Designed by Muse Code on muse-wonder-1.0, built by Muse Code on
muse-spark-1.3 from docs/muse-code/TIGHTEN-DESIGN.md.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, Sequence

from ghostcut.config import read_json, write_json

# Span-building constants. TIGHTEN-DESIGN.md §3 lists every constant in
# `tighten.py`, but §8 (the algorithm that uses these five) lives here and
# this module must not import `tighten` back, so they are defined here and
# re-exported from `tighten` under the same names.
PAUSE_KEEP_S = 0.35
HEAD_PAD_S = 0.10
TAIL_PAD_S = 0.15
MIN_KEPT_SPAN_S = 0.35
DEAD_AIR_S = 2.0

ATTEMPT_KINDS = ("keeper", "alternate", "flub", "restart", "crew_talk",
                 "aside", "setup", "dead_air")
REASON_KINDS = ("alternate", "flub", "restart", "crew_talk", "aside",
                "setup", "dead_air", "pause_trim", "target_drop",
                "editor_drop", "too_short", "unclassified")

OPERATIONS = ("swap_take", "drop_beat", "restore_beat", "drop_span",
              "restore_span", "set_angle", "clear_angle", "set_pause_limit",
              "add_note")

_NO_BEAT = "No beat B{bid} in this cut"


class WordLike(Protocol):
    text: str
    start_s: float
    end_s: float


@dataclass(slots=True)
class WordSpan:
    """One kept or removed piece of the source timeline."""

    id: str
    start_s: float
    end_s: float
    beat_id: int
    attempt_id: int
    utterance_ids: list[int] = field(default_factory=list)
    kind: str = "keeper"
    reason: str = ""


@dataclass(slots=True)
class Attempt:
    id: int
    utterance_ids: list[int]
    start_s: float
    end_s: float
    text: str
    verdict: str
    kind: str
    reason: str
    confidence: float
    # Repair carriage (TIGHTEN-DESIGN.md §7.4): overrule notes and the
    # unclaimed utterances folded into this attempt's span but excluded
    # from its kept words. Both are omitted from JSON when empty, so a
    # cleanly picked attempt matches the §5 shape exactly.
    notes: list[str] = field(default_factory=list)
    excluded_utterance_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class Beat:
    id: int
    topic: str
    attempts: list[Attempt] = field(default_factory=list)
    pick_attempt: int = -1
    status: str = "kept"
    keep_priority: int = 3
    reason: str = ""


# ---------------------------------------------------------------- load / write


def load_tighten(path: Path) -> dict:
    """The tighten.json payload dict; anything else is a ValueError."""
    data = read_json(Path(path))
    if (not isinstance(data, dict) or data.get("version") != 1
            or not isinstance(data.get("beats"), list)
            or not isinstance(data.get("kept_spans"), list)
            or not isinstance(data.get("removed_spans"), list)):
        raise ValueError(f"Not a tighten.json: {path}")
    for beat in data["beats"]:
        if not isinstance(beat, dict):
            continue
        for attempt in beat.get("attempts", []) or []:
            if not isinstance(attempt, dict):
                continue
            try:
                confidence = float(attempt.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
            attempt["confidence"] = min(1.0, max(0.0, confidence))
    for key in ("angle_overrides", "edits_applied", "span_exclusions",
                "editor_notes", "warnings"):
        if not isinstance(data.get(key), list):
            data[key] = []
    return data


_TIME_KEYS_3 = frozenset({
    "start_s", "end_s", "at_s", "offset_s", "duration_in_s",
    "duration_out_s", "requested_s", "before_s", "after_s",
    "pause_limit_s",
})


def _round_value(key: str, value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if key == "spend_usd" or key == "confidence":
            return round(value, 4)
        if key in _TIME_KEYS_3:
            return round(value, 3)
        return value
    if isinstance(value, dict):
        return {k: _round_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_round_value(key, item) for item in value]
    return value


def write_tighten(path: Path, payload: dict) -> None:
    """Round money/times per §4 (times 3 decimals, money 4) and write."""
    rounded = _round_value("", copy.deepcopy(payload))
    assert isinstance(rounded, dict)
    write_json(Path(path), rounded)


def utterances_of(payload: dict) -> list[tuple[int, float, float, str, int, int]]:
    """The payload's utterances as (id, start, end, text, word_start, word_end)."""
    out = []
    for entry in payload.get("utterances", []) or []:
        if not isinstance(entry, dict):
            continue
        out.append((int(entry.get("id", 0)), float(entry.get("start_s", 0.0)),
                    float(entry.get("end_s", 0.0)), str(entry.get("text", "")),
                    int(entry.get("word_start", 0)), int(entry.get("word_end", 0))))
    out.sort(key=lambda item: item[0])
    return out


# ------------------------------------------------------------------ spans (§8)


def _warn(payload: dict, text: str) -> None:
    warnings = payload.setdefault("warnings", [])
    if text not in warnings:
        warnings.append(text)


def recompute_spans(payload: dict, words: Sequence[WordLike], *,
                    pause_limit_s: float) -> dict:
    """Rebuild kept/removed spans, duration_out_s from beats/picks. Pure.

    The only function that writes `kept_spans`/`removed_spans`: Spark
    picks, rule picks, target drops and editor edits all funnel through
    it. Returns the mutated payload.
    """
    words = list(words)

    def _is_anchor(i: int) -> bool:
        return getattr(words[i], "heard_by", "whisper") != "muse"

    utterance_by_id: dict[int, dict] = {}
    for entry in payload.get("utterances", []) or []:
        if isinstance(entry, dict):
            utterance_by_id[int(entry.get("id", -1))] = entry
    beats = [b for b in payload.get("beats", []) or [] if isinstance(b, dict)]

    # word index -> utterance id; utterance id -> (beat, attempt index)
    word_to_utt: dict[int, int] = {}
    for uid, entry in utterance_by_id.items():
        try:
            start, end = int(entry.get("word_start", 0)), int(entry.get("word_end", 0))
        except (TypeError, ValueError):
            continue
        for i in range(start, end):
            word_to_utt[i] = uid
    utt_to_attempt: dict[int, tuple[dict, int]] = {}
    for beat in beats:
        attempts = beat.get("attempts", []) or []
        for index, attempt in enumerate(attempts):
            if not isinstance(attempt, dict):
                continue
            for uid in attempt.get("utterance_ids", []) or []:
                utt_to_attempt[int(uid)] = (beat, index)

    kept: list[dict] = []
    middles: dict[tuple[float, float], tuple[str, str, int, int,
                                             list[int]]] = {}
    for beat in beats:
        if beat.get("status") != "kept":
            continue
        attempts = [a for a in beat.get("attempts", []) or []
                    if isinstance(a, dict)]
        pick_index = beat.get("pick_attempt", -1)
        if not isinstance(pick_index, int) or not 0 <= pick_index < len(attempts):
            continue
        pick = attempts[pick_index]
        excluded = {int(uid) for uid in
                    pick.get("excluded_utterance_ids", []) or []}
        kept_utts = [int(uid) for uid in pick.get("utterance_ids", []) or []
                     if int(uid) not in excluded]
        indices: list[int] = []
        for uid in kept_utts:
            entry = utterance_by_id.get(uid)
            if entry is None:
                continue
            try:
                start, end = int(entry.get("word_start", 0)), int(
                    entry.get("word_end", 0))
            except (TypeError, ValueError):
                continue
            indices.extend(range(start, end))
        indices = sorted({i for i in indices
                          if 0 <= i < len(words) and _is_anchor(i)})
        if not indices:
            continue
        # Split the pick's kept words into runs at trimmed pauses and at
        # excluded (unclassified) utterances. Anchor gaps only: Muse-only
        # words between two anchors ride along inside the run.
        runs: list[list[int]] = [[indices[0]]]
        bounds: list[tuple[float, float, str, str, list[int]]] = []
        for prev_i, i in zip(indices, indices[1:]):
            prev_w, word = words[prev_i], words[i]
            gap = word.start_s - prev_w.end_s
            continuous = all(not _is_anchor(j)
                             for j in range(prev_i + 1, i))
            if continuous and gap <= pause_limit_s:
                runs[-1].append(i)
                continue
            if continuous and gap <= PAUSE_KEEP_S:
                # Shorter than what a trim retains: keep whole. (Only
                # reachable with --pause-limit under 0.35 s.)
                runs[-1].append(i)
                continue
            runs.append([i])
            if continuous:
                close, open_ = prev_w.end_s + 0.18, word.start_s - 0.17
                bounds.append((close, open_, "pause_trim",
                               f"pause {gap:.2f}s shortened to 0.35s", []))
            else:
                first_excluded = words[prev_i + 1].start_s
                last_excluded = words[i - 1].end_s
                close = min(prev_w.end_s + 0.18, first_excluded)
                open_ = max(word.start_s - 0.17, last_excluded)
                gap_uids = sorted({word_to_utt[j]
                                   for j in range(prev_i + 1, i)
                                   if j in word_to_utt})
                bounds.append((close, open_, "unclassified",
                               "not in the picked take", gap_uids))
        for number, run in enumerate(runs):
            first, last = words[run[0]], words[run[-1]]
            start: float | None = None
            end: float | None = None
            if number > 0:
                start = bounds[number - 1][1]
            else:
                prev_a = next((j for j in range(run[0] - 1, -1, -1)
                               if _is_anchor(j)), None)
                if prev_a is None:
                    start = max(0.0, first.start_s - HEAD_PAD_S)
                else:
                    start = (words[prev_a].end_s + first.start_s) / 2
            if number < len(runs) - 1:
                end = bounds[number][0]
            else:
                next_a = next((j for j in range(run[-1] + 1, len(words))
                               if _is_anchor(j)), None)
                if next_a is None:
                    end = last.end_s + TAIL_PAD_S
                else:
                    end = (last.end_s + words[next_a].start_s) / 2
            assert start is not None and end is not None
            run_utts = sorted({word_to_utt[i] for i in run
                               if i in word_to_utt})
            kept.append({"start_s": start, "end_s": end,
                         "beat_id": beat.get("id", -1),
                         "attempt_id": pick.get("id", pick_index),
                         "utterance_ids": run_utts, "kind": "keeper",
                         "reason": str(pick.get("reason", ""))})
        for close, open_, kind, reason, gap_uids in bounds:
            middles[(close, open_)] = (
                kind, reason, beat.get("id", -1),
                pick.get("id", pick_index), gap_uids)

    kept.sort(key=lambda span: (span["start_s"], span["end_s"]))
    # §8.3: merge a below-minimum span forward across a trimmed pause.
    changed = True
    while changed:
        changed = False
        for pos, span in enumerate(kept):
            if span["end_s"] - span["start_s"] >= MIN_KEPT_SPAN_S:
                continue
            if pos + 1 >= len(kept):
                continue
            following = kept[pos + 1]
            if (following["beat_id"], following["attempt_id"]) != (
                    span["beat_id"], span["attempt_id"]):
                continue
            middle = middles.get((span["end_s"], following["start_s"]))
            if middle is None or middle[0] != "pause_trim":
                continue
            del middles[(span["end_s"], following["start_s"])]
            following["start_s"] = span["start_s"]
            following["utterance_ids"] = sorted(
                set(span["utterance_ids"]) | set(following["utterance_ids"]))
            kept.pop(pos)
            changed = True
            break

    # Span exclusions (drop_span): match by exact rounded range.
    exclusions = [e for e in payload.get("span_exclusions", []) or []
                  if isinstance(e, dict)]
    live_exclusions: list[dict] = []
    excluded_ranges: list[dict] = []
    for exclusion in exclusions:
        try:
            key = (round(float(exclusion.get("start_s", 0.0)), 3),
                   round(float(exclusion.get("end_s", 0.0)), 3))
        except (TypeError, ValueError):
            continue
        hit = next((s for s in kept
                    if (round(s["start_s"], 3), round(s["end_s"], 3)) == key),
                   None)
        if hit is None:
            _warn(payload, f"span_exclusion_stale:{key[0]:.3f}-{key[1]:.3f}")
            continue
        kept.remove(hit)
        live_exclusions.append(exclusion)
        excluded_ranges.append({
            "start_s": hit["start_s"], "end_s": hit["end_s"],
            "beat_id": hit["beat_id"], "attempt_id": hit["attempt_id"],
            "utterance_ids": list(hit["utterance_ids"]),
            "reason": str(exclusion.get("reason", "dropped by editor"))})
    payload["span_exclusions"] = live_exclusions

    for pos, span in enumerate(kept):
        span["id"] = f"K{pos}"
    for span in kept:
        if span["end_s"] - span["start_s"] < MIN_KEPT_SPAN_S:
            _warn(payload, f"tighten_span_below_min:{span['id']}")

    removed = _complement(
        kept, middles, excluded_ranges, beats, utterance_by_id,
        word_to_utt, utt_to_attempt, words)
    for pos, span in enumerate(removed):
        span["id"] = f"R{pos}"
    payload["kept_spans"] = [
        {"id": s["id"], "start_s": s["start_s"], "end_s": s["end_s"],
         "beat_id": s["beat_id"], "attempt_id": s["attempt_id"],
         "utterance_ids": list(s["utterance_ids"]), "kind": s["kind"],
         "reason": s["reason"]} for s in kept]
    payload["removed_spans"] = [
        {"id": s["id"], "start_s": s["start_s"], "end_s": s["end_s"],
         "beat_id": s["beat_id"], "attempt_id": s["attempt_id"],
         "utterance_ids": list(s["utterance_ids"]), "kind": s["kind"],
         "reason": s["reason"]} for s in removed]
    payload["duration_out_s"] = sum(s["end_s"] - s["start_s"] for s in kept)
    return payload


def _complement(kept: list[dict], middles: dict, excluded_ranges: list[dict],
                beats: list[dict], utterance_by_id: dict,
                word_to_utt: dict[int, int],
                utt_to_attempt: dict[int, tuple[dict, int]],
                words: list[WordLike]) -> list[dict]:
    """The removed list: complement of kept, classified per §8.4."""
    removed: list[dict] = []
    if not kept:
        return removed
    edges = sorted({s["start_s"] for s in excluded_ranges}
                   | {s["end_s"] for s in excluded_ranges})
    for first, second in zip(kept, kept[1:]):
        start, end = first["end_s"], second["start_s"]
        if end <= start:
            continue
        cuts = sorted({start, end} | {e for e in edges if start < e < end})
        for low, high in zip(cuts, cuts[1:]):
            piece = _classify_gap(
                low, high, middles, excluded_ranges, beats,
                word_to_utt, utt_to_attempt, words)
            if piece is not None:
                removed.append(piece)
    return removed


def _classify_gap(low: float, high: float, middles: dict,
                  excluded_ranges: list[dict], beats: list[dict],
                  word_to_utt: dict[int, int],
                  utt_to_attempt: dict[int, tuple[dict, int]],
                  words: list[WordLike]) -> dict | None:
    """One removed span for the gap (low, high); None when zero-width."""
    if high <= low:
        return None
    for excluded in excluded_ranges:
        if excluded["start_s"] == low and excluded["end_s"] == high:
            return {"start_s": low, "end_s": high,
                    "beat_id": excluded["beat_id"],
                    "attempt_id": excluded["attempt_id"],
                    "utterance_ids": list(excluded["utterance_ids"]),
                    "kind": "editor_drop", "reason": excluded["reason"]}
    middle = middles.get((low, high))
    if middle is not None:
        kind, reason, beat_id, attempt_id, uids = middle
        return {"start_s": low, "end_s": high, "beat_id": beat_id,
                "attempt_id": attempt_id, "utterance_ids": list(uids),
                "kind": kind, "reason": reason}
    overlapped = [i for i, word in enumerate(words)
                  if word.start_s < high and word.end_s > low]
    by_attempt: dict[tuple[int, int], list[int]] = {}
    for i in overlapped:
        uid = word_to_utt.get(i)
        if uid is None:
            continue
        found = utt_to_attempt.get(uid)
        if found is None:
            continue
        beat, index = found
        by_attempt.setdefault((beat.get("id", -1), index), []).append(i)
    if by_attempt:
        ordered = sorted(by_attempt, key=lambda key: min(by_attempt[key]))
        for key in ordered:
            beat_id, index = key
            beat = next((b for b in beats if b.get("id") == beat_id), None)
            attempts = (beat.get("attempts", []) or []) if beat else []
            attempt = attempts[index] if 0 <= index < len(attempts) else None
            if isinstance(attempt, dict) and attempt.get("verdict") == "remove":
                return {"start_s": low, "end_s": high, "beat_id": beat_id,
                        "attempt_id": attempt.get("id", index),
                        "utterance_ids": sorted(
                            {word_to_utt[i] for i in overlapped
                             if i in word_to_utt}),
                        "kind": str(attempt.get("kind", "flub")),
                        "reason": str(attempt.get("reason", ""))}
        for key in ordered:
            beat_id, index = key
            beat = next((b for b in beats if b.get("id") == beat_id), None)
            attempts = (beat.get("attempts", []) or []) if beat else []
            attempt = attempts[index] if 0 <= index < len(attempts) else None
            if isinstance(attempt, dict) and attempt.get("verdict") == "alternate":
                return {"start_s": low, "end_s": high, "beat_id": beat_id,
                        "attempt_id": attempt.get("id", index),
                        "utterance_ids": sorted(
                            {word_to_utt[i] for i in overlapped
                             if i in word_to_utt}),
                        "kind": "alternate",
                        "reason": str(attempt.get("reason", ""))}
        for key in ordered:
            beat_id, index = key
            beat = next((b for b in beats if b.get("id") == beat_id), None)
            attempts = (beat.get("attempts", []) or []) if beat else []
            attempt = attempts[index] if 0 <= index < len(attempts) else None
            status = beat.get("status") if beat else None
            if (isinstance(attempt, dict) and status == "dropped_target"
                    and attempt.get("verdict") == "pick"):
                return {"start_s": low, "end_s": high, "beat_id": beat_id,
                        "attempt_id": attempt.get("id", index),
                        "utterance_ids": sorted(
                            {word_to_utt[i] for i in overlapped
                             if i in word_to_utt}),
                        "kind": "target_drop",
                        "reason": f"beat B{beat_id} dropped to meet --target"}
            if (isinstance(attempt, dict) and status == "dropped_editor"
                    and attempt.get("verdict") == "pick"):
                return {"start_s": low, "end_s": high, "beat_id": beat_id,
                        "attempt_id": attempt.get("id", index),
                        "utterance_ids": sorted(
                            {word_to_utt[i] for i in overlapped
                             if i in word_to_utt}),
                        "kind": "editor_drop",
                        "reason": f"beat B{beat_id} dropped by editor"}
    if not overlapped and high - low >= DEAD_AIR_S:
        return {"start_s": low, "end_s": high, "beat_id": -1,
                "attempt_id": -1, "utterance_ids": [],
                "kind": "dead_air",
                "reason": f"no words for {high - low:.1f}s"}
    first_beat, first_attempt = -1, -1
    if overlapped:
        uid = word_to_utt.get(overlapped[0])
        found = utt_to_attempt.get(uid) if uid is not None else None
        if found is not None:
            beat, index = found
            first_beat = beat.get("id", -1)
            attempts = beat.get("attempts", []) or []
            attempt = attempts[index] if 0 <= index < len(attempts) else None
            first_attempt = (attempt.get("id", index)
                             if isinstance(attempt, dict) else index)
    return {"start_s": low, "end_s": high, "beat_id": first_beat,
            "attempt_id": first_attempt,
            "utterance_ids": sorted({word_to_utt[i] for i in overlapped
                                     if i in word_to_utt}),
            "kind": "unclassified", "reason": "not in the picked take"}


# ------------------------------------------------------------ time mapping (§8.5)


def source_to_output(payload: dict, t_s: float) -> float | None:
    """Output time of a kept source moment; None when removed."""
    position = 0.0
    for span in payload.get("kept_spans", []) or []:
        start, end = float(span["start_s"]), float(span["end_s"])
        if start <= t_s <= end:
            return position + (t_s - start)
        position += end - start
    return None


def output_to_source(payload: dict, t_s: float) -> float:
    """Source time of an output (playhead) time."""
    kept = [s for s in payload.get("kept_spans", []) or []]
    if not kept:
        return 0.0
    if t_s <= 0:
        return float(kept[0]["start_s"])
    position = 0.0
    for span in kept:
        duration = float(span["end_s"]) - float(span["start_s"])
        if t_s <= position + duration:
            return float(span["start_s"]) + (t_s - position)
        position += duration
    return float(kept[-1]["end_s"])


def beat_at_source(payload: dict, t_s: float) -> int | None:
    """Beat whose pick (else whose attempts) contains the source time."""
    beats = [b for b in payload.get("beats", []) or []
             if isinstance(b, dict)]
    for beat in beats:
        attempts = beat.get("attempts", []) or []
        pick = beat.get("pick_attempt", -1)
        if isinstance(pick, int) and 0 <= pick < len(attempts):
            attempt = attempts[pick]
            if (isinstance(attempt, dict)
                    and float(attempt.get("start_s", 0.0)) <= t_s
                    <= float(attempt.get("end_s", 0.0))):
                return beat.get("id")
    for beat in beats:
        for attempt in beat.get("attempts", []) or []:
            if (isinstance(attempt, dict)
                    and float(attempt.get("start_s", 0.0)) <= t_s
                    <= float(attempt.get("end_s", 0.0))):
                return beat.get("id")
    return None


def beat_at_output(payload: dict, t_s: float) -> int | None:
    """Beat for an output-timeline playhead time."""
    return beat_at_source(payload, output_to_source(payload, t_s))


# ------------------------------------------------------- operations (§5.2–§5.4)


def _blocked(text: str) -> dict:
    return {"ok": False, "text": text, "hard": True}


def _passed(text: str) -> dict:
    return {"ok": True, "text": text, "hard": False}


def validate_operation(payload: dict, op: dict, *,
                       covers: dict[int, list[str]] | None = None
                       ) -> list[dict]:
    """The checks for one op: [{"ok", "text", "hard"}]. A hard failure blocks."""
    name = op.get("op", "")
    if name not in OPERATIONS:
        return [_blocked(f"Unknown operation '{name}'")]
    beats = {b.get("id"): b for b in payload.get("beats", []) or []
             if isinstance(b, dict)}
    if name in ("swap_take", "drop_beat", "restore_beat", "set_angle",
                "clear_angle"):
        bid = op.get("beat_id", -1)
        if bid not in beats:
            return [_blocked(_NO_BEAT.format(bid=bid))]
    if name == "swap_take":
        bid = op.get("beat_id", -1)
        aid = op.get("attempt_id", -1)
        beat = beats[bid]
        attempts = [a for a in beat.get("attempts", []) or []
                    if isinstance(a, dict)]
        attempt = next((a for a in attempts if a.get("id") == aid), None)
        if attempt is None:
            return [_blocked(f"No attempt A{aid} in beat B{bid}")]
        pick = beat.get("pick_attempt", -1)
        current = (attempts[pick].get("id") if isinstance(pick, int)
                   and 0 <= pick < len(attempts) else None)
        if current == aid:
            return [_passed("Already the pick")]
        if attempt.get("verdict") == "remove" or attempt.get("kind") in (
                "flub", "restart", "crew_talk", "aside", "setup"):
            return [_blocked(
                f"Attempt A{aid} is marked {attempt.get('kind')}: "
                f"'{attempt.get('reason', '')}'")]
        return [_passed(f"Swap beat B{bid} to attempt A{aid}")]
    if name == "drop_beat":
        beat = beats[op.get("beat_id", -1)]
        if beat.get("status") == "no_usable_attempt":
            return [_passed(f"Beat B{beat.get('id')} is already fully removed")]
        return [_passed(f"Drop beat B{beat.get('id')}")]
    if name == "restore_beat":
        beat = beats[op.get("beat_id", -1)]
        if beat.get("status") == "no_usable_attempt":
            return [_blocked(
                f"Beat B{beat.get('id')} has no usable attempt to restore")]
        if beat.get("status") == "kept":
            return [_passed(f"Beat B{beat.get('id')} is already kept")]
        return [_passed(f"Restore beat B{beat.get('id')}")]
    if name == "drop_span":
        span_id = op.get("span_id", "")
        kept = [s for s in payload.get("kept_spans", []) or []
                if isinstance(s, dict)]
        span = next((s for s in kept if s.get("id") == span_id), None)
        excluded = {(round(float(e.get("start_s", 0.0)), 3),
                     round(float(e.get("end_s", 0.0)), 3))
                    for e in payload.get("span_exclusions", []) or []
                    if isinstance(e, dict)}
        if span is None or (round(float(span.get("start_s", 0.0)), 3),
                            round(float(span.get("end_s", 0.0)), 3)) in excluded:
            return [_blocked(f"No kept span {span_id} in this cut")]
        beat_spans = [s for s in kept if s.get("beat_id") == span.get("beat_id")
                      and (round(float(s.get("start_s", 0.0)), 3),
                           round(float(s.get("end_s", 0.0)), 3)) not in excluded]
        if len(beat_spans) <= 1:
            return [_blocked(
                f"That is the whole of beat B{span.get('beat_id')} — "
                f"drop the beat instead")]
        return [_passed(f"Drop span {span_id}")]
    if name == "restore_span":
        span_id = op.get("span_id", "")
        exclusions = [e for e in payload.get("span_exclusions", []) or []
                      if isinstance(e, dict) and e.get("span_id") == span_id]
        if not exclusions:
            return [_blocked(f"No dropped span {span_id} to restore")]
        return [_passed(f"Restore span {span_id}")]
    if name == "set_angle":
        bid = op.get("beat_id", -1)
        camera = op.get("camera", "")
        if not camera:
            return [_blocked(f"set_angle needs a camera for beat B{bid}")]
        if covers is not None and camera not in covers.get(bid, []):
            return [_blocked(f"Camera {camera} does not cover beat B{bid}")]
        return [_passed(f"Set beat B{bid} to camera {camera}")]
    if name == "clear_angle":
        bid = op.get("beat_id", -1)
        overrides = [o for o in payload.get("angle_overrides", []) or []
                     if isinstance(o, dict) and o.get("beat_id") == bid]
        if not overrides:
            return [_passed(f"No angle override on beat B{bid}")]
        return [_passed(f"Clear the angle override on beat B{bid}")]
    if name == "set_pause_limit":
        value = op.get("pause_limit_s", -1)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not 0.30 <= float(value) <= 3.00):
            return [_blocked(
                f"Pause limit {value}s is outside 0.30–3.00s")]
        return [_passed(f"Set the pause limit to {float(value):.2f}s")]
    at_s = op.get("at_s")
    if isinstance(at_s, bool) or not isinstance(at_s, (int, float)):
        return [_blocked(f"Bad note time {at_s!r}")]
    return [_passed("Add an editor note")]


def _next_edit_id(payload: dict) -> str:
    taken = 0
    for entry in payload.get("edits_applied", []) or []:
        if not isinstance(entry, dict):
            continue
        raw = str(entry.get("id", ""))
        if raw.startswith("e") and raw[1:].isdigit():
            taken = max(taken, int(raw[1:]))
    return f"e{taken + 1}"


def _apply_op(payload: dict, op: dict, *, source: str) -> None:
    """Apply one validated op and append its audit entry. Mutates payload."""
    name = op.get("op", "")
    beats = {b.get("id"): b for b in payload.get("beats", []) or []
             if isinstance(b, dict)}
    if name == "swap_take":
        beat = beats[op.get("beat_id", -1)]
        aid = op.get("attempt_id", -1)
        attempts = [a for a in beat.get("attempts", []) or []
                    if isinstance(a, dict)]
        pick = beat.get("pick_attempt", -1)
        if not (isinstance(pick, int) and 0 <= pick < len(attempts)
                and attempts[pick].get("id") == aid):
            if isinstance(pick, int) and 0 <= pick < len(attempts):
                attempts[pick]["verdict"] = "alternate"
                attempts[pick]["kind"] = "alternate"
            index = next(i for i, a in enumerate(attempts)
                         if a.get("id") == aid)
            attempts[index]["verdict"] = "pick"
            attempts[index]["kind"] = "keeper"
            beat["pick_attempt"] = index
            beat["reason"] = str(attempts[index].get("reason", ""))
        beat["status"] = "kept"
    elif name == "drop_beat":
        beat = beats[op.get("beat_id", -1)]
        if beat.get("status") != "no_usable_attempt":
            beat["status"] = "dropped_editor"
    elif name == "restore_beat":
        beat = beats[op.get("beat_id", -1)]
        if beat.get("status") != "kept":
            beat["status"] = "kept"
    elif name == "drop_span":
        span_id = op.get("span_id", "")
        span = next(s for s in payload.get("kept_spans", []) or []
                    if isinstance(s, dict) and s.get("id") == span_id)
        payload.setdefault("span_exclusions", []).append({
            "start_s": round(float(span["start_s"]), 3),
            "end_s": round(float(span["end_s"]), 3),
            "reason": "dropped by editor", "span_id": span_id,
            "beat_id": span.get("beat_id", -1),
            "attempt_id": span.get("attempt_id", -1),
            "utterance_ids": list(span.get("utterance_ids", []) or [])})
    elif name == "restore_span":
        span_id = op.get("span_id", "")
        payload["span_exclusions"] = [
            e for e in payload.get("span_exclusions", []) or []
            if not (isinstance(e, dict) and e.get("span_id") == span_id)]
    elif name == "set_angle":
        bid = op.get("beat_id", -1)
        payload["angle_overrides"] = [
            o for o in payload.get("angle_overrides", []) or []
            if not (isinstance(o, dict) and o.get("beat_id") == bid)]
        payload["angle_overrides"].append({
            "beat_id": bid, "camera": op.get("camera", ""),
            "reason": str(op.get("why") or "editor: angle override")})
    elif name == "clear_angle":
        bid = op.get("beat_id", -1)
        payload["angle_overrides"] = [
            o for o in payload.get("angle_overrides", []) or []
            if not (isinstance(o, dict) and o.get("beat_id") == bid)]
    elif name == "set_pause_limit":
        payload["pause_limit_s"] = float(op.get("pause_limit_s", 0.8))
    elif name == "add_note":
        payload.setdefault("editor_notes", []).append({
            "at_s": float(op.get("at_s", 0.0)),
            "text": str(op.get("text", "")), "source": source})
    entry: dict = {"id": _next_edit_id(payload), "source": source, "op": name}
    for key, value in op.items():
        if key not in ("op", "source", "at", "why", "checks", "blocked"):
            entry[key] = value
    entry["at"] = datetime.now().isoformat(timespec="seconds")
    entry["why"] = str(op.get("why", ""))
    payload.setdefault("edits_applied", []).append(entry)


def apply_operations(payload: dict, words: Sequence[WordLike],
                     ops: list[dict], *, source: str,
                     covers: dict[int, list[str]] | None = None
                     ) -> tuple[dict, list[dict]]:
    """The one edit path: validate, apply in order, recompute once.

    Returns (payload, checked_ops); each checked op echoes its fields
    with "blocked" and "checks". A batch that would empty the cut is
    refused whole and nothing is applied.
    """
    snapshot = copy.deepcopy(payload)
    checked: list[dict] = []
    for op in ops:
        op = dict(op)
        checks = validate_operation(payload, op, covers=covers)
        blocked = any(not check["ok"] and check["hard"] for check in checks)
        entry = dict(op)
        entry["blocked"] = blocked
        entry["checks"] = checks
        checked.append(entry)
        if not blocked:
            _apply_op(payload, op, source=source)
    pause_limit = payload.get("pause_limit_s", 0.8)
    try:
        pause_limit = float(pause_limit)
    except (TypeError, ValueError):
        pause_limit = 0.8
    recompute_spans(payload, list(words), pause_limit_s=pause_limit)
    if not payload.get("kept_spans"):
        payload.clear()
        payload.update(snapshot)
        for entry in checked:
            entry["blocked"] = True
            entry["checks"] = list(entry["checks"]) + [
                {"ok": False, "text": "That would empty the cut",
                 "hard": True}]
    return payload, checked
