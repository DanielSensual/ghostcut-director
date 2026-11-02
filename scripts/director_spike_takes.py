"""Day-1 spike for GhostCut Director: can Muse Spark organize a raw talking-head recording into takes?

Feeds one long talking-head proxy (picture + audio) to muse-spark-1.3 and asks for a continuous segmentation:
keeper takes, alternate attempts of the same piece (grouped, with a pick), flubs, off-camera pauses, asides to
the editor, and setup/dead air. Then scores the answer against a hand-checked take index (the C2764 TAKES.md
from 2026-10-02, built from Whisper + review).

Runs with Guestlist's Meta client (shared cache + spend ledger):

    python scripts/director_spike_takes.py <proxy.mp4> [--out docs/spike-takes-day1.json]   # a Python with guestlist.meta

Re-runs are free: the Spark call is cached by (video hash, prompts, schema).
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path

if os.environ.get("GUESTLIST_SRC"):  # a guestlist checkout instead of an installed package
    sys.path.insert(0, os.environ["GUESTLIST_SRC"])
from guestlist.meta import spark  # noqa: E402

KINDS = ["keeper_take", "alternate_take", "flub_or_false_start", "off_camera_or_pause",
         "aside_to_editor", "setup_or_technical", "dead_air_or_still"]

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["recording_summary", "segments"],
    "properties": {
        "recording_summary": {"type": "string"},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["t_start", "t_end", "kind", "topic", "group", "pick_in_group", "reason", "confidence"],
                "properties": {
                    "t_start": {"type": "number"},
                    "t_end": {"type": "number"},
                    "kind": {"type": "string", "enum": KINDS},
                    "topic": {"type": "string"},
                    "group": {"type": "string"},
                    "pick_in_group": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        },
    },
}

DEVELOPER = (
    "You are the assistant editor for a solo creator who films himself talking to camera. You watch and listen to "
    "the raw camera recording and log it so the editor can cut publishable pieces without re-watching it. Be "
    "literal and time-accurate. Judge delivery from both the picture (eye line, framing, expression) and the "
    "audio (stumbles, restarts, filler, trailing off). Never invent content you cannot see or hear."
)

PROMPT = """This is ONE continuous raw recording. In it the creator records several separate short pieces (different topics,
sometimes for different brands). Between pieces he talks to his AI editor, checks gear, looks away, or sits silently.
Sometimes he restarts a piece, repeats a line, or stumbles and continues.

Segment the WHOLE recording from 0 to the end, in order, with no gaps and no overlaps. Times are seconds from the start.
For each segment give:
- kind:
  - keeper_take: a usable, publishable piece (or part of one) said to camera.
  - alternate_take: another attempt at content that is also covered by a different attempt.
  - flub_or_false_start: a stumble, restart, "cut that", or a broken line that should be cut out of a take.
  - off_camera_or_pause: looking away from camera or a pause in the middle of a piece.
  - aside_to_editor: instructions or comments aimed at the editor/AI, not the audience.
  - setup_or_technical: gear checks, troubleshooting, getting ready.
  - dead_air_or_still: silence or a held still with nothing usable.
- topic: a few words naming the piece.
- group: when two or more segments are attempts at the SAME piece, give them the same short id (e.g. "g1"); otherwise "".
- pick_in_group: true for the single best attempt in each group (and for every ungrouped keeper_take); false otherwise.
- reason: one sentence — why this kind, and for picks, why this attempt beats the others.
- confidence: 0 to 1.

A piece interrupted by a flub or an off-camera moment and then continued is ONE piece: log the parts as keeper_take
segments with the flub/pause segment between them, all with the same topic."""

# Hand-checked take index for C2764 (TAKES.md, 2026-10-02). Ranges in clip seconds.
TRUTH = {
    "T01": [(6.84, 67.11)],
    "T02": [(134.93, 170.04)],
    "T03": [(195.53, 248.62), (262.18, 285.16)],
    "T04": [(287.41, 333.37)],
    "T05a": [(411.54, 424.17)],
    "T05b": [(446.57, 454.95)],
    "T06": [(491.37, 499.54)],
    "T07": [(590.67, 630.71), (643.02, 652.69)],
    "T08": [(712.59, 810.35)],
    "T09": [(815.36, 1020.23)],
    "T10": [(1032.70, 1035.66), (1040.29, 1096.35)],
}
# Spans inside or between takes that the human edit removed on purpose.
REMOVED = {
    "T03 off-camera look": (248.62, 262.18),
    "T07 flub 'cut that there'": (630.71, 643.02),
    "T10 repeated stumble": (1035.66, 1040.29),
    "still-hold thumbnail plates": (336.0, 384.0),
    "receiver troubleshooting": (1100.0, 1330.0),
}


# Mode "lines": Spark still watches and listens, but anchors each segment to numbered transcript lines; the
# times then come from Whisper, not from Spark's own sense of time.
LINES_SCHEMA = copy.deepcopy(SCHEMA)
_seg = LINES_SCHEMA["properties"]["segments"]["items"]
for _f in ("t_start", "t_end"):
    del _seg["properties"][_f]
_seg["properties"]["first_line"] = {"type": "integer"}
_seg["properties"]["last_line"] = {"type": "integer"}
_seg["required"] = ["first_line", "last_line"] + [f for f in _seg["required"] if f not in ("t_start", "t_end")]

LINES_PROMPT = PROMPT.replace(
    "Segment the WHOLE recording from 0 to the end, in order, with no gaps and no overlaps. Times are seconds from the start.",
    "Below is a numbered transcript of the recording (Whisper, may mishear words; [start-end] in seconds). Watch and "
    "listen to the video to judge delivery, and use the transcript only to locate things. Segment the WHOLE recording "
    "in order: every transcript line belongs to exactly one segment. Give each segment's first_line and last_line "
    "numbers instead of times. Split a line's neighbours into separate segments when the delivery changes.",
) + "\n\nTRANSCRIPT:\n{transcript}"


def parse_srt(path: str | Path) -> list[tuple[int, float, float, str]]:
    def secs(t: str) -> float:
        h, m, s = t.replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    lines = []
    for block in Path(path).read_text().strip().split("\n\n"):
        parts = block.strip().split("\n")
        if len(parts) >= 3 and "-->" in parts[1]:
            a, b = (x.strip() for x in parts[1].split("-->"))
            lines.append((len(lines) + 1, secs(a), secs(b), " ".join(parts[2:]).strip()))
    return lines


def overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def score(segments: list[dict]) -> dict:
    keep = [(s["t_start"], s["t_end"]) for s in segments if s["kind"] == "keeper_take" and s["pick_in_group"]]
    any_keep = [(s["t_start"], s["t_end"]) for s in segments if s["kind"] in ("keeper_take", "alternate_take")]
    bounds = sorted({round(x, 2) for r in keep for x in r})
    takes = {}
    for name, ranges in TRUTH.items():
        secs = sum(b - a for a, b in ranges)
        covered = sum(overlap(r, k) for r in ranges for k in keep)
        covered_any = sum(overlap(r, k) for r in ranges for k in any_keep)
        edges = [ranges[0][0], ranges[-1][1]]
        err = [min((abs(e - b) for b in bounds), default=None) for e in edges]
        takes[name] = {"seconds": round(secs, 1), "picked_coverage": round(covered / secs, 3),
                       "any_attempt_coverage": round(covered_any / secs, 3),
                       "start_err_s": None if err[0] is None else round(err[0], 2),
                       "end_err_s": None if err[1] is None else round(err[1], 2)}
    truth_ranges = [r for rs in TRUTH.values() for r in rs]
    keep_secs = sum(b - a for a, b in keep)
    spill = keep_secs - sum(overlap(k, t) for k in keep for t in truth_ranges)
    removed = {name: round(sum(overlap(span, k) for k in keep), 1) for name, span in REMOVED.items()}
    truth_secs = sum(b - a for a, b in truth_ranges)
    return {
        "picked_keeper_seconds": round(keep_secs, 1),
        "truth_seconds": round(truth_secs, 1),
        "recall_of_truth": round(sum(overlap(k, t) for k in keep for t in truth_ranges) / truth_secs, 3),
        "precision_vs_truth": round(1 - spill / keep_secs, 3) if keep_secs else None,
        "removed_spans_still_kept_s": removed,
        "takes": takes,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("proxy")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "docs/spike-takes-day1.json"))
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--mode", choices=["times", "lines"], default="times")
    ap.add_argument("--srt", help="Whisper SRT in clip time (required for --mode lines)")
    args = ap.parse_args()

    if args.mode == "times":
        r = spark.video_json(args.proxy, schema=SCHEMA, name="take_log", developer=DEVELOPER, prompt=PROMPT,
                             effort=args.effort, max_output_tokens=24000, max_usd=3.0)
        segs = r.data["segments"]
    else:
        lines = parse_srt(args.srt)
        text = "\n".join(f"{n} [{a:.1f}-{b:.1f}] {t}" for n, a, b, t in lines)
        r = spark.video_json(args.proxy, schema=LINES_SCHEMA, name="take_log_lines", developer=DEVELOPER,
                             prompt=LINES_PROMPT.format(transcript=text), effort=args.effort,
                             max_output_tokens=24000, max_usd=3.0)
        by_n = {n: (a, b) for n, a, b, _ in lines}
        segs = []
        for s in r.data["segments"]:
            first, last = by_n.get(s["first_line"]), by_n.get(s["last_line"])
            if not first or not last:
                continue
            segs.append({"t_start": first[0], "t_end": last[1], **s})
    segs = sorted(segs, key=lambda s: s["t_start"])
    result = {
        "mode": args.mode,
        "proxy": Path(args.proxy).name,
        "model": spark.DEFAULT_MODEL,
        "effort": args.effort,
        "usd": round(r.usd, 4),
        "seconds": round(r.seconds, 1),
        "cached": r.cached,
        "usage": r.usage,
        "score": score(segs),
        "recording_summary": r.data["recording_summary"],
        "segments": segs,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2))
    print(json.dumps({k: result[k] for k in ("usd", "seconds", "cached", "score")}, indent=2))


if __name__ == "__main__":
    main()
