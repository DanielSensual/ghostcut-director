"""GhostCut Director test run: a folder of on-camera clips -> Spark take log -> cut YouTube draft + FCPXML + SRT.

Inputs come from a work dir prepared by the proxy step (all_takes_proxy.mp4, clip_map.json, all_takes.srt):

    python scripts/director_test_run.py organize <workdir>     # a Python with guestlist.meta installed
    python scripts/director_test_run.py cut <workdir>

organize calls muse-spark-1.3 once (cached by video hash + prompt), cut/export are local and free.
Spark judges delivery from picture + sound; Whisper lines supply every time (mode B from the Day-1 spike).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# director_spike_takes imports guestlist (Spark): it is imported inside the steps that need it, so Render can import
# this module without it.

PROMPT = """These are a real-estate agent's raw on-camera clips for ONE YouTube house tour, joined end to end in shooting
order. She walks the home and talks to camera about the community, each room and its features. She often records the
same line or piece several times; some attempts trail off, stumble, restart, or include crew talk and setup.

Clip boundaries (seconds in this joined video): {clips}

Below is a numbered transcript (Whisper, may mishear words; [start-end] in seconds). Watch and listen to judge delivery,
eye line and energy; use the transcript only to locate things. Segment the WHOLE video in order: every transcript line
belongs to exactly one segment. Give each segment's first_line and last_line numbers instead of times.
- keeper_take: usable to-camera content for the tour.
- alternate_take: another attempt at a piece that is covered by a better attempt (same group id, pick_in_group false).
- flub_or_false_start, off_camera_or_pause, aside_to_editor (talk to crew/editor), setup_or_technical, dead_air_or_still.
For every group of attempts, mark exactly one pick_in_group true: the cleanest, most confident full delivery.
Every ungrouped keeper_take has pick_in_group true. topic = the room or subject in a few words.

TRANSCRIPT:
{transcript}"""


def load(work: Path):
    from director_spike_takes import parse_srt

    clip_map = json.loads((work / "clip_map.json").read_text())
    lines = parse_srt(work / "all_takes.srt")
    return clip_map, lines


def organize(work: Path, effort: str) -> None:
    from director_spike_takes import DEVELOPER, LINES_SCHEMA
    from guestlist.meta import spark

    clip_map, lines = load(work)
    clips = "; ".join(f"{c['clip']} {c['offset']:.1f}-{c['offset'] + c['proxy_duration']:.1f}" for c in clip_map)
    transcript = "\n".join(f"{n} [{a:.1f}-{b:.1f}] {t}" for n, a, b, t in lines)
    r = spark.video_json(work / "all_takes_proxy.mp4", schema=LINES_SCHEMA, name="take_log_lines", developer=DEVELOPER,
                         prompt=PROMPT.format(clips=clips, transcript=transcript), effort=effort,
                         max_output_tokens=24000, max_usd=3.0)
    by_n = {n: (a, b) for n, a, b, _ in lines}
    segments, rejected = [], []
    for s in r.data["segments"]:
        first, last = by_n.get(s["first_line"]), by_n.get(s["last_line"])
        if not first or not last or s["first_line"] > s["last_line"]:
            rejected.append(s)  # kept for review instead of silently dropped
            continue
        segments.append({"t_start": first[0], "t_end": last[1], **s})
    segments.sort(key=lambda s: s["t_start"])
    out = {"model": spark.DEFAULT_MODEL, "effort": effort, "usd": round(r.usd, 4), "seconds": round(r.seconds, 1),
           "cached": r.cached, "summary": r.data["recording_summary"], "segments": segments, "rejected": rejected}
    (work / "take_log.json").write_text(json.dumps(out, indent=2))
    picks = [s for s in segments if s["kind"] == "keeper_take" and s["pick_in_group"]]
    print(json.dumps({"usd": out["usd"], "seconds": out["seconds"], "cached": r.cached, "segments": len(segments),
                      "picks": len(picks), "picked_s": round(sum(s["t_end"] - s["t_start"] for s in picks), 1),
                      "rejected": len(rejected)}, indent=2))


_CUE_WORDS = {"one", "two", "three", "four", "five", "go", "action", "rolling", "camera", "camera's", "cameras",
              "all", "right", "alright", "okay", "ok", "and", "yeah", "speed", "set", "ready", "1", "2", "3", "4", "5"}


def _norm(word: str) -> str:
    return word.strip().strip(".,!?\"'").lower()


def trim_cues(a: float, b: float, words: list[dict]) -> tuple[float, float]:
    """Move a take's edges past spoken countdowns / roll cues ("camera's rolling, three, two, one, go").

    Head: inside the first 8 s, if a run of cue words ends in "go"/"action", start at the next word. Tail: inside the
    last 6 s, if a cue run starts with "camera(s) rolling" or a 3-2-1 count, end before it. Mechanical, word-timed.
    """
    inside = [w for w in words if a - 0.05 <= w["start"] < b]
    head = [w for w in inside if w["start"] < a + 8.0]
    cut_at = None
    for i, w in enumerate(head):
        if _norm(w["word"]) in ("go", "action") and all(_norm(x["word"]) in _CUE_WORDS for x in head[max(0, i - 3):i]):
            cut_at = i
    if cut_at is None:  # a bare count without "go": "One, two three okay so..."
        run = 0
        while run < min(len(head), 6) and _norm(head[run]["word"]) in _CUE_WORDS:
            run += 1
        numbers = sum(1 for x in head[:run] if _norm(x["word"]) in _NUMBERS)
        if numbers >= 2:
            cut_at = run - 1
    if cut_at is not None and cut_at + 1 < len(inside):
        a = inside[cut_at + 1]["start"] - 0.08
    tail = [w for w in inside if w["start"] > b - 6.0]
    for i, w in enumerate(tail):
        n = _norm(w["word"])
        nxt = _norm(tail[i + 1]["word"]) if i + 1 < len(tail) else ""
        if (n.startswith("camera") and nxt == "rolling") or (n == "three" and nxt == "two"):
            if all(_norm(x["word"]) in _CUE_WORDS for x in tail[i:]):
                b = (tail[i - 1]["end"] if i else w["start"]) + 0.05
            break
        if n == "cut" and w["word"].strip().lower().rstrip(".!,") == "cut" and i > 0:  # "Cut. Let me try that again."
            b = tail[i - 1]["end"] + 0.05
            break
        if n in ("sorry", "oops") and i > 0:  # "...the walk-in closet. Oh, sorry, and over here"
            j = i
            while j > 0 and _norm(tail[j - 1]["word"]) in ("oh", "oops", "i'm", "so"):
                j -= 1
            if j > 0:
                b = tail[j - 1]["end"] + 0.05
            break
    # Line times come from a long-file pass; extend the start back through words spoken without a pause.
    before = [w for w in words if w["end"] <= a + 0.1]
    start = a
    while (before and a - before[-1]["end"] < 0.25 and start - before[-1]["start"] < 0.8
           and _norm(before[-1]["word"]) not in _CUE_WORDS and not before[-1]["word"].strip().endswith((".", "?", "!"))):
        a = before.pop()["start"] - 0.05
    return a, b


_NUMBERS = {"one", "two", "three", "four", "five", "1", "2", "3", "4", "5"}


def _snap_to_words(a: float, b: float, words: list[dict]) -> tuple[float, float]:
    """Never start or end inside a spoken word."""
    for w in words:
        if w["start"] < a < w["end"] and _norm(w["word"]) not in _CUE_WORDS:
            a = w["start"] - 0.05
        if w["start"] < b < w["end"]:
            b = w["end"] + 0.05
    return a, b


def to_pieces(segments: list[dict], clip_map: list[dict], lines, words: list[dict] | None = None) -> list[dict]:
    """Picked keeper spans in joined time -> per-source-clip pieces, padded into the surrounding silence."""
    starts = sorted(a for _, a, _, _ in lines)
    ends = sorted(b for _, _, b, _ in lines)
    pieces = []
    for s in segments:
        if s["kind"] != "keeper_take" or not s["pick_in_group"]:
            continue
        a, b = s["t_start"], s["t_end"]
        if words:
            a, b = trim_cues(a, b, words)
        if words:  # pad only into the silence next to the nearest spoken word
            prev_end = max((w["end"] for w in words if w["end"] <= a + 0.1), default=a - 1.0)
            next_start = min((w["start"] for w in words if w["start"] >= b - 0.1), default=b + 1.0)
        else:
            prev_end = max((e for e in ends if e <= a + 1e-6), default=a - 1.0)
            next_start = min((x for x in starts if x >= b - 1e-6), default=b + 1.0)
        a = max(a - 0.15, (prev_end + a) / 2)
        b = min(b + 0.30, (b + next_start) / 2)
        if words:
            a, b = _snap_to_words(a, b, words)
        for c in clip_map:
            lo, hi = c["offset"], c["offset"] + c["proxy_duration"]
            x, y = max(a, lo), min(b, hi)
            spoken = sum(1 for w in words or [] if x <= w["start"] < y)
            fragment = x > a or y < b
            if y - x >= (2.0 if fragment else 0.4) and not (fragment and words and spoken < 4):  # drop crumbs
                pieces.append({"clip": c["clip"], "source": c["source"], "src_in": round(x - lo, 3),
                               "src_out": round(min(y - lo, c["duration"]), 3), "topic": s["topic"],
                               "reason": s["reason"], "first_line": s["first_line"], "last_line": s["last_line"]})
    if words:
        pieces = _split_restarts(pieces, clip_map, words)
        _drop_repeats(pieces, clip_map, words)
    return pieces


_RESTART_WORDS = _CUE_WORDS | {"sorry", "oops", "so", "i'm", "more", "time", "again", "let's", "do", "this", "mm"}


def _split_restarts(pieces: list[dict], clip_map: list[dict], words: list[dict]) -> list[dict]:
    """A countdown inside a take ("... sorry about that. One more time. One, two, three, go.") is a restart: cut from the
    last pause before the stumble to the first word after "go", splitting the take in two."""
    offset = {c["clip"]: c["offset"] for c in clip_map}
    out = []
    for p in pieces:
        lo = offset[p["clip"]]
        inside = [w for w in words if lo + p["src_in"] - 0.05 <= w["start"] < lo + p["src_out"]]
        cut = None
        for i in range(3, len(inside) - 1):
            if _norm(inside[i]["word"]) not in ("go", "action"):
                continue
            if not all(_norm(x["word"]) in _CUE_WORDS for x in inside[i - 2:i]):
                continue
            j = i
            while j > 0 and _norm(inside[j - 1]["word"]) in _RESTART_WORDS and inside[i]["start"] - inside[j - 1]["start"] < 8:
                j -= 1
            k = j  # back to the last real pause, so the broken phrase goes too (repeat check trims the rest)
            while k > 0 and inside[k]["start"] - inside[k - 1]["end"] < 0.5 and inside[j]["start"] - inside[k - 1]["start"] < 6:
                k -= 1
            cut = (inside[k - 1]["end"] + 0.05 - lo if k else p["src_in"], inside[i + 1]["start"] - 0.08 - lo)
            break
        if cut is None:
            out.append(p)
            continue
        left, right = dict(p, src_out=round(cut[0], 3)), dict(p, src_in=round(cut[1], 3))
        out.extend(x for x in (left,) if x["src_out"] - x["src_in"] >= 2.0)
        out.extend(_split_restarts([right], clip_map, words) if right["src_out"] - right["src_in"] >= 0.4 else [])
    return out


def _drop_repeats(pieces: list[dict], clip_map: list[dict], words: list[dict], n: int = 4) -> None:
    """If a piece ends with a line the next piece opens with (two takes of one line), end it before the repeat."""
    offset = {c["clip"]: c["offset"] for c in clip_map}

    def span(p):
        lo = offset[p["clip"]]
        return [w for w in words if lo + p["src_in"] - 0.05 <= w["start"] < lo + p["src_out"]]

    skip = {"the", "this", "a", "an", "that"}  # "welcome to the / this kitchen" is one line

    for cur, nxt in zip(pieces, pieces[1:]):
        tail = [w for w in span(cur)[-25:] if _norm(w["word"]) not in skip]
        head = [_norm(w["word"]) for w in span(nxt)[:25] if _norm(w["word"]) not in skip]
        grams = {tuple(head[i:i + n]) for i in range(len(head) - n + 1)}
        for i in range(len(tail) - n + 1):
            if tuple(_norm(w["word"]) for w in tail[i:i + n]) in grams:
                end = (tail[i - 1]["end"] if i else tail[i]["start"]) + 0.05 - offset[cur["clip"]]
                if end - cur["src_in"] >= 2.0:
                    cur["src_out"] = round(end, 3)
                break


def verify_words(work: Path, pieces: list[dict], clip_map: list[dict], words: list[dict]) -> list[dict]:
    """Re-transcribe each picked take on its own (+2 s each side). Whisper on one long recording can skip a stumble
    ("plenty ... of ... space" stretched over a restart); short windows hear it. Cached per window."""
    offset = {c["clip"]: c["offset"] for c in clip_map}
    vdir = work / "verify"
    vdir.mkdir(exist_ok=True)
    spans = []
    for p in pieces:
        a = max(0.0, offset[p["clip"]] + p["src_in"] - 2.0)
        b = offset[p["clip"]] + p["src_out"] + 2.0
        stem = f"w_{round(a * 100):07d}_{round(b * 100):07d}"
        if not (vdir / f"{stem}.json").exists():
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i",
                            str(work / "all_takes.wav"), str(vdir / f"{stem}.wav")], check=True)
            subprocess.run(["mlx_whisper", str(vdir / f"{stem}.wav"), "--model", "mlx-community/whisper-large-v3-turbo",
                            "--language", "en", "--output-format", "json", "--output-name", stem, "--output-dir", str(vdir),
                            "--condition-on-previous-text", "False", "--word-timestamps", "True"],
                           check=True, capture_output=True)
        found = [dict(w, start=w["start"] + a, end=w["end"] + a)
                 for s in json.loads((vdir / f"{stem}.json").read_text())["segments"] for w in s.get("words", [])]
        spans.append((a, b, found))
    # Windows overlap by their 2 s margins: each window owns its words up to the midpoint of any overlap.
    spans.sort()
    owned = []
    for i, (a, b, found) in enumerate(spans):
        lo = (a + spans[i - 1][1]) / 2 if i and spans[i - 1][1] > a else a
        hi = (b + spans[i + 1][0]) / 2 if i + 1 < len(spans) and spans[i + 1][0] < b else b
        owned.append((lo, hi, [w for w in found if lo <= w["start"] < hi]))
    kept = [w for w in words if not any(lo <= w["start"] < hi for lo, hi, _ in owned)]
    return sorted(kept + [w for _, _, found in owned for w in found], key=lambda w: w["start"])


def cut(work: Path, pieces_file: Path | None = None) -> None:
    from ghostcut.export_fcpxml import export_fcpxml
    from ghostcut.models import MediaClip, TimelineSegment

    clip_map, lines = load(work)
    log = json.loads((work / "take_log.json").read_text())
    words_file = work / "all_takes_words.json"
    words = None
    if words_file.exists():
        words = [w for s in json.loads(words_file.read_text())["segments"] for w in s.get("words", [])]
    if pieces_file:  # e.g. pieces_refined.json from the refine step
        pieces = json.loads(pieces_file.read_text())
    else:
        pieces = to_pieces(log["segments"], clip_map, lines, words)
        if words:
            words = verify_words(work, pieces, clip_map, words)
            pieces = to_pieces(log["segments"], clip_map, lines, words)
    out = work / "render"
    out.mkdir(exist_ok=True)

    # Render each piece at 1080p, native 59.94, 10 ms audio fades so cuts don't click.
    listing = []
    for i, p in enumerate(pieces):
        zoom = float(p.get("zoom", 1.0))  # punch-in: 1.22 = 122% (centered crop of the 4K source)
        key = hashlib.sha1(f"{p['source']}|{p['src_in']}|{p['src_out']}|{zoom}".encode()).hexdigest()[:12]
        f = out / f"piece_{key}.mp4"  # keyed by content, so splitting one take doesn't re-render the rest
        dur = p["src_out"] - p["src_in"]
        crop = f"crop=iw/{zoom}:ih/{zoom}," if zoom != 1.0 else ""
        if not f.exists():
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{p['src_in']:.3f}", "-t", f"{dur:.3f}", "-i", p["source"],
                            "-vf", f"{crop}scale=1920:1080,format=yuv420p", "-r", "60000/1001", "-c:v", "libx264", "-preset", "fast",
                            "-crf", "19", "-af", f"afade=t=in:d=0.01,afade=t=out:st={max(0, dur - 0.01):.3f}:d=0.01",
                            "-ar", "48000", "-ac", "2", "-c:a", "aac", "-b:a", "192k", str(f)], check=True)
        listing.append(f"file '{f}'")
    (out / "list.txt").write_text("\n".join(listing) + "\n")
    joined = out / "joined.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(out / "list.txt"),
                    "-c", "copy", str(joined)], check=True)
    final = work / "ghostcut_test_v13.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(joined), "-c:v", "copy", "-af", "loudnorm=I=-14:TP=-1:LRA=11",
                    "-ar", "48000", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(final)], check=True)

    # Timeline (same picks) for Final Cut / Resolve, and captions in output time.
    clips, segs, srt, t = {}, [], [], 0.0
    for p in pieces:
        c = next(c for c in clip_map if c["clip"] == p["clip"])
        clips.setdefault(p["clip"], MediaClip(p["clip"], p["source"], "", "", c["duration"], 60000 / 1001, True, 3840, 2160))
        segs.append(TimelineSegment(p["topic"], p["clip"], p["src_in"], p["src_out"], round(t, 3)))
        lo = c["offset"]
        for n, a, b, text in lines:
            if p["first_line"] <= n <= p["last_line"]:
                x, y = max(a - lo, p["src_in"]), min(b - lo, p["src_out"])
                if y > x:
                    srt.append((t + x - p["src_in"], t + y - p["src_in"], text))
        t += p["src_out"] - p["src_in"]
    export_fcpxml(list(clips.values()), segs, work / "ghostcut_test_v13.fcpxml",
                  project_name="GhostCut test v13")

    def ts(x: float) -> str:
        ms = int(round(x * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    (work / "ghostcut_test_v13.srt").write_text(
        "\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{text}\n" for i, (a, b, text) in enumerate(srt, 1)))
    (work / "pieces.json").write_text(json.dumps(pieces, indent=1))
    print(json.dumps({"pieces": len(pieces), "duration_s": round(t, 1), "video": str(final)}, indent=2))


BROLL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["shots", "overlays", "missing_shots"],
    "properties": {
        "shots": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["tag", "room", "description", "best_start", "best_end", "quality"],
            "properties": {"tag": {"type": "string"}, "room": {"type": "string"}, "description": {"type": "string"},
                           "best_start": {"type": "number"}, "best_end": {"type": "number"},
                           "quality": {"type": "number"}}}},
        "overlays": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["tag", "broll_start", "broll_end", "timeline_start", "reason"],
            "properties": {"tag": {"type": "string"}, "broll_start": {"type": "number"}, "broll_end": {"type": "number"},
                           "timeline_start": {"type": "number"}, "reason": {"type": "string"}}}},
        "missing_shots": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["what", "where", "why"],
            "properties": {"what": {"type": "string"}, "where": {"type": "string"}, "why": {"type": "string"}}}},
    },
}

BROLL_DEVELOPER = (
    "You are the B-roll editor for a real-estate YouTube house tour. You watch every B-roll shot and place them over the "
    "agent's narration so viewers SEE what she is describing at the moment she says it. Be literal: only claim what is "
    "visible. Never invent rooms or features."
)

BROLL_PROMPT = """The video is {n} B-roll shots of the same model home joined end to end, no audio. Each shot's 4-digit tag is
burned in at the top left. Shot boundaries in this joined video (tag start-end seconds): {shots}

Below is the EDITED narration timeline (seconds in the final video) and its takes (B-roll must sit inside ONE take):
TAKES: {takes}
NARRATION:
{narration}

1. shots: one entry per tag: room, what is shown (camera move too), the best usable range in seconds WITHIN that shot
   (0 = the shot's first frame), quality 0-1 (sharp, steady, well exposed).
2. overlays: lay B-roll over the narration. Rules: match the room/feature being described at that moment; 2.5-6 s each;
   inside one take, never across a take boundary; keep her on camera for the first 6 s of the video, the first 3 s of
   each take and the last 12 s of the video; aim for about 40% B-roll overall (she stays on camera the rest); use
   drone (D tags) and community (C tags) shots for the exterior, neighborhood and lot; no overlap between overlays;
   do not reuse the same part of a shot. broll_start/broll_end are seconds WITHIN the shot.
3. missing_shots: shots a good editor wishes existed for this tour (where in the narration and why).
{subject}"""


def broll_proxy(work: Path, dirs: list[Path]) -> None:
    """Join every B-roll shot (2 s+) into one labeled, silent 12 fps proxy for Spark. Tag = folder letter + last 4 of
    the file name (e.g. D0042 for a drone clip), burned in top left. Writes broll_proxy.mp4 + broll_map.json."""
    pdir = work / "broll_proxy"
    pdir.mkdir(exist_ok=True)
    shots, listing, t = [], [], 0.0
    for folder in dirs:
        letter = folder.name.split("-")[-1][:1].upper()
        for p in sorted(x for x in folder.rglob("*") if x.suffix.lower() in (".mp4", ".mov") and not x.name.startswith("._")):
            probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                                               "-of", "json", str(p)], capture_output=True, text=True).stdout or "{}")
            if not any(s.get("codec_type") == "video" for s in probe.get("streams", [])):
                continue
            dur = float(probe.get("format", {}).get("duration", 0) or 0)
            if dur < 2.0:
                continue
            tag = base = letter + "".join(ch for ch in p.stem if ch.isdigit())[-4:]
            k = 1
            while any(x["tag"] == tag for x in shots):  # drone numbering restarts per flight
                k += 1
                tag = f"{base}{chr(96 + k)}"
            out = pdir / f"{tag}.mp4"
            if not out.exists():
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(p), "-vf",
                                f"scale=640:360:force_original_aspect_ratio=increase,crop=640:360,fps=12,drawtext=text='{tag}':"
                                "x=10:y=10:fontsize=28:fontcolor=yellow:box=1:boxcolor=black@0.6",
                                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", str(out)], check=True)
            pd = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                       str(out)], capture_output=True, text=True).stdout)
            shots.append({"clip": p.name, "tag": tag, "source": str(p), "offset": round(t, 3), "duration": dur,
                          "proxy_duration": pd})
            listing.append(f"file '{out}'")
            t += pd
    (work / "broll_concat.txt").write_text("\n".join(listing) + "\n")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(work / "broll_concat.txt"),
                    "-c", "copy", "-movflags", "+faststart", str(work / "broll_proxy.mp4")], check=True)
    (work / "broll_map.json").write_text(json.dumps(shots, indent=1))
    print(json.dumps({"shots": len(shots), "seconds": round(t, 1)}))


def _overlay_filters(k: int, s: float, e: float, speed: float, last: str) -> tuple[str, str]:
    """Input k as a muted B-roll overlay on [last] from s to e (seconds of the output): cover-scaled to 1080p, played
    at `speed` from its first read frame, labeled [v{k}]. Shared by broll() and render_overlays()."""
    return (f"[{k}:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
            f"setpts=(PTS-STARTPTS)/{speed}+{s:.3f}/TB,fps=60000/1001,format=yuv420p[b{k}]",
            f"[{last}][b{k}]overlay=enable='between(t,{s:.3f},{e:.3f})':eof_action=pass[v{k}]")


def render_overlays(base: Path, overlays: list[dict], out: Path, speed: float = 0.5, probe: float | None = None,
                    dry_run: bool = False) -> Path | list[str]:
    """Video-only section picture: B-roll laid over `base` (a talk cut, render/joined.mp4) with broll()'s filters.

    overlays = [{source, in, t, d}] in list order: t and d in section seconds (d on screen), in = file seconds; each
    reads d * speed seconds of source. An empty list renders the plain talk. probe=s runs the same graph for s seconds
    into the null muxer (nothing written); dry_run returns the argv instead of running it."""
    inputs, chain, last = ["-i", str(base)], [], "0:v"
    for k, o in enumerate(overlays, 1):
        t = o["t"] if "t" in o else o["r"]  # timeline.json broll entries name it r
        inputs += ["-ss", f"{o['in']:.3f}", "-t", f"{o['d'] * speed:.3f}", "-i", str(o["source"])]
        chain += _overlay_filters(k, t, t + o["d"], speed, last)
        last = f"v{k}"
    chain.append(f"[{last}]setsar=1[out]")
    dest = ["-t", f"{probe:.3f}", "-f", "null", "-"] if probe else ["-movflags", "+faststart", str(out)]
    argv = ["ffmpeg", "-v", "error", "-nostdin", "-y", *inputs, "-filter_complex", ";".join(chain), "-map", "[out]",
            "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "19", "-r", "60000/1001", "-pix_fmt", "yuv420p", *dest]
    if dry_run:
        return argv
    subprocess.run(argv, check=True)
    return out


def broll(work: Path, effort: str, subject: str = "", exclude: tuple[str, ...] = (),
          cover: tuple[tuple[float, float], ...] = (), insert: tuple[str, ...] = (), drop_at: tuple[float, ...] = (),
          speed: float = 1.0, reuse_plan: bool = False, cues_file: Path | None = None,
          retime: tuple[tuple[float, float], ...] = ()) -> None:
    from director_spike_takes import parse_srt
    from guestlist.meta import spark
    from ghostcut.export_fcpxml import export_fcpxml
    from ghostcut.models import BrollOverlay, MediaClip, TimelineSegment

    bmap = {b["tag"]: b for b in json.loads((work / "broll_map.json").read_text())}
    pieces = json.loads((work / "pieces.json").read_text())
    srt = cues_file or max(work.glob("ghostcut_test_v*.srt"), key=lambda f: f.stat().st_mtime)  # the latest cut
    name = srt.stem
    cues = parse_srt(srt)
    takes, t = [], 0.0
    for p in pieces:
        d = p["src_out"] - p["src_in"]
        takes.append((t, t + d, p))
        t += d
    total = t
    # Lines cut after the plan was made ("at=dt": dt seconds removed/added at timeline `at`): slide every planned time.
    if retime and not reuse_plan:
        raise SystemExit("--retime only applies to a reused plan")
    sh = lambda t: t + sum(d for at, d in retime if t >= at)  # noqa: E731
    cover = tuple((sh(a), sh(b)) for a, b in cover)
    drop_at = tuple(sh(t) for t in drop_at)
    insert = tuple(f"{sh(float(rng.split('-')[0])):.3f}-{sh(float(rng.split('-')[1])):.3f}={shot}"
                   for rng, shot in (x.split("=") for x in insert))
    if reuse_plan:  # trims of a few frames: keep Spark's verified plan instead of a new (different) one
        raw = json.loads((work / "broll_plan.json").read_text())["raw"]
        r = types.SimpleNamespace(data=raw, usd=0.0, seconds=0.0, cached=True)
    else:
        r = spark.video_json(
            work / "broll_proxy.mp4", schema=BROLL_SCHEMA, name="broll_plan", developer=BROLL_DEVELOPER,
            prompt=BROLL_PROMPT.format(
                n=len(bmap),
                shots="; ".join(f"{b['tag']} {b['offset']:.1f}-{b['offset'] + b['proxy_duration']:.1f}" for b in bmap.values()),
                takes="; ".join(f"{a:.1f}-{b:.1f} {p['topic']}" for a, b, p in takes),
                narration="\n".join(f"[{a:.1f}-{b:.1f}] {text}" for _, a, b, text in cues),
                subject=(f"THE HOME FOR SALE: {subject} Any shot showing a different house (or a feature this home does not "
                         "have, e.g. a pool) may only be used as neighborhood context, never while she describes this home."
                         if subject else "")),
            effort=effort, max_output_tokens=16000, max_usd=2.0)

    # Validate every overlay mechanically; keep the rejects with a reason.
    placed, rejected, used = [], [], []
    for o in sorted(({**o, "timeline_start": sh(o["timeline_start"])} for o in r.data["overlays"]),
                    key=lambda o: o["timeline_start"]):
        b = bmap.get(o["tag"])
        dur = o["broll_end"] - o["broll_start"]
        start, end = o["timeline_start"], o["timeline_start"] + dur
        take = next(((a, z, p) for a, z, p in takes if a + 3.0 <= start and end <= z), None)
        why = (not b and "unknown tag") or (not 2.0 <= dur <= 7.0 and "duration") or \
              (b and not (0 <= o["broll_start"] and o["broll_end"] <= b["duration"]) and "outside shot") or \
              ((start < 6.0 or end > total - 12.0) and "covers protected open/close") or (not take and "crosses a take") or \
              (any(start < pe and ps < end for ps, pe, _ in placed) and "overlaps another overlay") or \
              (any(o["tag"] == tg and o["broll_start"] < ue and us < o["broll_end"] for tg, us, ue in used) and "reused")
        why = why or (o["tag"] in exclude and "excluded (verified wrong subject)")
        if why:
            rejected.append({**o, "rejected": why})
            continue
        placed.append((start, end, {**o, "take": take}))
        used.append((o["tag"], o["broll_start"], o["broll_end"]))
    # A house shot shows one room: if Spark used it under two different takes, keep the take whose topic best
    # matches the shot's own description and drop the rest.
    catalog = {x["tag"]: (x["room"] + " " + x["description"]).lower() for x in r.data["shots"]}
    for tag in {o["tag"] for _, _, o in placed if o["tag"].startswith("R")}:
        uses = [(s, e, o) for s, e, o in placed if o["tag"] == tag]
        topics = {o["take"][2]["topic"] for _, _, o in uses}
        if len(topics) < 2:
            continue
        words_of = lambda t: {x for x in t.lower().replace("/", " ").split() if len(x) > 3}  # noqa: E731
        best = max(topics, key=lambda t: len(words_of(t) & words_of(catalog.get(tag, ""))))
        for use in uses:
            if use[2]["take"][2]["topic"] != best:
                placed.remove(use)
                rejected.append({**{k: v for k, v in use[2].items() if k != "take"}, "rejected": f"shot is {best}, not this room"})
    # Cover on-camera moments that must not show (crew in the background): slide the next overlay in the same take
    # back so it starts at the cover start.
    for c0, c1 in cover:
        later = sorted((x for x in placed if x[0] >= c0 and x[2]["take"][0] <= c0 < x[2]["take"][1] and x[0] - c0 < 8),
                       key=lambda x: x[0])
        if later and later[0][1] - later[0][0] >= c1 - c0:
            s0, e0, o = later[0]
            placed[placed.index(later[0])] = (c0, c0 + e0 - s0, {**o, "timeline_start": c0, "moved_to_cover": [c0, c1]})
    # Drop placements verified wrong by frame (e.g. primary bedroom shown over the guest bedroom).
    for t in drop_at:
        for x in [x for x in placed if x[0] <= t < x[1]]:
            placed.remove(x)
            rejected.append({**{k: v for k, v in x[2].items() if k != "take"}, "rejected": f"dropped at {t} (frame-checked)"})
    # Explicit inserts "start-end=TAG@shot_start" (e.g. continue a shot to hide crew in the background).
    for spec in insert:
        rng, shot = spec.split("=")
        c0, c1 = (float(x) for x in rng.split("-"))
        tag, b0 = shot.split("@")
        take = next(((a, z, p) for a, z, p in takes if a <= c0 and c1 <= z), None)
        if take and tag in bmap and float(b0) + (c1 - c0) * speed <= bmap[tag]["duration"]:
            for clash in [x for x in placed if c0 < x[1] - 0.01 and x[0] + 0.01 < c1]:  # an explicit insert wins
                placed.remove(clash)
                rejected.append({**{k: v for k, v in clash[2].items() if k != "take"}, "rejected": f"replaced by insert {spec}"})
            placed.append((c0, c1, {"tag": tag, "broll_start": float(b0), "broll_end": float(b0) + c1 - c0,
                                    "timeline_start": c0, "reason": "inserted to cover the background", "take": take}))
    placed.sort(key=lambda x: x[0])
    covered = sum(e - s for s, e, _ in placed)
    plan = {"usd": round(r.usd, 4), "seconds": round(r.seconds, 1), "cached": r.cached, "coverage": round(covered / total, 3),
            "speed": speed, "raw": r.data,  # rendered speed; the FCPXML overlays are still 1x (no timeMap retime yet)
            "placed": [o for _, _, o in placed], "rejected": rejected, "shots": r.data["shots"],
            "missing_shots": r.data["missing_shots"]}
    for o in plan["placed"]:
        o["take"] = o["take"][2]["topic"]
    (work / "broll_plan.json").write_text(json.dumps(plan, indent=2))

    # Render: muted B-roll laid over the talking cut, then loudness.
    inputs, chain, last = ["-i", str(work / "render/joined.mp4")], [], "0:v"
    for k, (s, e, o) in enumerate(placed, 1):
        b = bmap[o["tag"]]
        # Slow motion reads less source: at 0.5 each 59.94 frame is shown twice (30p look, no interpolation smear).
        inputs += ["-ss", f"{o['broll_start']:.3f}", "-t", f"{(e - s) * speed:.3f}", "-i", b["source"]]
        chain += _overlay_filters(k, s, e, speed, last)
        last = f"v{k}"
    out = work / f"{name}_broll.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(chain), "-map", f"[{last}]",
                    "-map", "0:a", "-c:v", "libx264", "-preset", "fast", "-crf", "19", "-r", "60000/1001",
                    "-af", "loudnorm=I=-14:TP=-1:LRA=11", "-ar", "48000", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", str(out)], check=True)

    # Timeline with B-roll as connected clips (Final Cut / Resolve).
    clip_map = {c["clip"]: c for c in json.loads((work / "clip_map.json").read_text())}
    clips, segs = {}, []
    for a, _, p in takes:
        clips.setdefault(p["clip"], MediaClip(p["clip"], p["source"], "", "", clip_map[p["clip"]]["duration"],
                                              60000 / 1001, True, 3840, 2160))
        segs.append(TimelineSegment(p["topic"], p["clip"], p["src_in"], p["src_out"], round(a, 3)))
    overlays = []
    for s, e, o in placed:
        b = bmap[o["tag"]]
        clips.setdefault(b["clip"], MediaClip(b["clip"], b["source"], "", "", b["duration"], 60000 / 1001, False,
                                              3840, 2160, layer="b-roll"))
        base = next(p for a, z, p in takes if a <= s and e <= z)
        overlays.append(BrollOverlay(o["take"], base["clip"], b["clip"], o["broll_start"], min(o["broll_end"], b["duration"]),
                                     s, e, o["reason"]))
    export_fcpxml(list(clips.values()), segs, work / f"{name}_broll.fcpxml", broll_overlays=overlays,
                  project_name=f"GhostCut test ({name}, B-roll)")
    print(json.dumps({"usd": plan["usd"], "placed": len(placed), "rejected": len(rejected),
                      "coverage": plan["coverage"], "missing_shots": len(plan["missing_shots"]), "video": str(out)}, indent=2))


_FILLER = {"the", "a", "an", "and", "um", "uh", "so", "this", "that"}
_CREW = [("that", "was", "really", "good"), ("oh", "yeah"), ("are", "we", "good"), ("you're", "good")]


def find_cuts(words: list[dict]) -> list[tuple[float, float, str]]:
    """Spans to remove, found in what the RENDERED cut actually says (final-time seconds)."""
    norm = [_norm(w["word"]) for w in words]
    cuts = []
    # 1. restarts: "... the living room... Sorry, one, two, three, go. The living room"
    for i, n in enumerate(norm):
        if n in ("go", "action") and i >= 2 and all(x in _CUE_WORDS for x in norm[i - 2:i]):
            j = i
            while j > 0 and norm[j - 1] in _RESTART_WORDS and words[i]["start"] - words[j - 1]["start"] < 8:
                j -= 1
            cuts.append((words[j]["start"] - 0.05, words[i + 1]["start"] - 0.08 if i + 1 < len(words) else words[i]["end"],
                         "restart"))
    # 2. a phrase said again within a few seconds ("the living room has high ceilings... the living room has..."):
    #    drop from the first attempt to the start of the second.
    content = [(k, n) for k, n in enumerate(norm) if n not in _FILLER]
    for size in (6, 5, 4, 3):
        for a in range(len(content) - size):
            gram = tuple(n for _, n in content[a:a + size])
            for b in range(a + 1, min(len(content) - size + 1, a + 40)):
                if tuple(n for _, n in content[b:b + size]) != gram:
                    continue
                first, second = words[content[a][0]], words[content[b][0]]
                between = words[content[a][0]:content[b][0]]
                broken_off = not any(w["word"].strip().endswith((".", "?", "!")) and not w["word"].strip().endswith("...")
                                     for w in between)  # a restart breaks off mid-sentence; a list finishes them
                leads = [w["word"].strip() for w in words[max(0, content[b][0] - 2):content[b][0]]]
                joined = any(_norm(x) in ("and", "also", "but", "or", "plus") or x.endswith(",") for x in leads)  # ", you also have"
                last = words[content[a + size - 1][0]]["word"].strip()
                after = words[content[a + size - 1][0] + 1]["word"].strip() if content[a + size - 1][0] + 1 < len(words) else ""
                snapped = last.endswith((",", "...")) or after.endswith(",")  # "the idea is to, and as your guide, the idea"
                limit = 8.0 if snapped and size >= 3 else \
                    0.0 if joined and size <= 4 else (6.0 if broken_off else 0.0) if size == 3 else {4: 8.0}.get(size, 9.0)
                if 0 < second["start"] - first["start"] < limit:  # restarts come fast
                    cuts.append((first["start"] - 0.05, second["start"] - 0.05, "repeat: " + " ".join(gram)))
                break
    # 2b. a phrase abandoned with "..." ("This is... And here is") — from its sentence start to the ellipsis
    for i, w in enumerate(words):
        if w["word"].strip().endswith("...") and i + 1 < len(words):
            j = i
            while j > 0 and not words[j - 1]["word"].strip().endswith((".", "?", "!")) and w["end"] - words[j - 1]["start"] < 4:
                j -= 1
            if w["end"] - words[j]["start"] < 4:
                cuts.append((words[j]["start"] - 0.05, words[i + 1]["start"] - 0.05, "abandoned: " + w["word"].strip()))
    # 2c. dead air inside the cut: keep 0.35 s after the last word and 0.25 s before the next
    for prev, nxt in zip(words, words[1:]):
        if nxt["start"] - prev["end"] > 1.5:
            cuts.append((prev["end"] + 0.35, nxt["start"] - 0.25, "dead air"))
    # 3. crew remarks
    for i in range(len(norm)):
        for phrase in _CREW:
            if tuple(norm[i:i + len(phrase)]) == phrase:
                end = words[i + len(phrase) - 1]["end"]
                cuts.append((words[i]["start"] - 0.05, end + 0.05, "crew: " + " ".join(phrase)))
    # 4. a word said twice in a row ("tour, tour") — keep the second
    for i in range(len(norm) - 1):
        if norm[i] == norm[i + 1] and len(norm[i]) > 3 and norm[i] not in ("very", "lots"):
            cuts.append((words[i]["start"] - 0.03, words[i + 1]["start"] - 0.03, "double: " + norm[i]))
    cuts.sort()
    merged = []
    for a, b, why in cuts:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]), merged[-1][2] + " + " + why)
        else:
            merged.append((a, b, why))
    return merged


def refine(work: Path, words_file: Path) -> None:
    """Cut the spans find_cuts() reports out of pieces.json (final time -> source time), keeping a ledger."""
    words = [w for s in json.loads(words_file.read_text())["segments"] for w in s.get("words", [])]
    pieces = json.loads((work / "pieces.json").read_text())
    cuts = find_cuts(words)
    out, t = [], 0.0
    for p in pieces:
        t0, t1 = t, t + p["src_out"] - p["src_in"]
        t = t1
        keep = [(t0, t1)]
        for a, b, _ in cuts:
            nxt = []
            for x, y in keep:
                if b <= x or a >= y:
                    nxt.append((x, y))
                    continue
                if a > x:
                    nxt.append((x, a))
                if b < y:
                    nxt.append((b, y))
            keep = nxt
        for x, y in keep:
            if y - x >= 0.6:
                out.append(dict(p, src_in=round(p["src_in"] + x - t0, 3), src_out=round(p["src_in"] + y - t0, 3)))
    (work / "pieces_refined.json").write_text(json.dumps(out, indent=1))
    (work / "refine_ledger.json").write_text(json.dumps([{"t_start": round(a, 2), "t_end": round(b, 2), "why": w}
                                                        for a, b, w in cuts], indent=1))
    for a, b, why in cuts:
        said = " ".join(w["word"].strip() for w in words if a <= w["start"] < b)
        print(f"{a:7.2f}-{b:7.2f} {why[:40]:40} | {said[:80]}")
    print(f"{len(cuts)} cuts, {len(pieces)} -> {len(out)} pieces, "
          f"{sum(p['src_out'] - p['src_in'] for p in pieces):.1f}s -> {sum(p['src_out'] - p['src_in'] for p in out):.1f}s")


QA_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "score", "strengths", "findings"],
    "properties": {
        "verdict": {"type": "string"},
        "score": {"type": "number"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "findings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["t_start", "t_end", "category", "severity", "evidence", "fix"],
            "properties": {
                "t_start": {"type": "number"}, "t_end": {"type": "number"},
                "category": {"type": "string", "enum": ["bad_cut", "crew_or_slate_talk", "repeated_line", "audio_problem",
                                                         "broll_mismatch", "broll_quality", "jump_or_flash_frame",
                                                         "pacing", "framing_or_exposure", "other"]},
                "severity": {"type": "string", "enum": ["blocker", "high", "medium", "low"]},
                "evidence": {"type": "string"}, "fix": {"type": "string"}}}},
    },
}

QA_DEVELOPER = (
    "You are the senior editor doing final QC on a real-estate YouTube house tour before it goes to the client. "
    "Watch the picture AND listen to the audio. Report only what you can see or hear, each with a time range and the "
    "exact words or visual evidence. Do not praise inside findings. A finding without evidence is worthless."
)

QA_PROMPT = """This is the full draft (about {minutes:.1f} minutes). The agent narrates a walk-through; B-roll of the home is laid over
her narration. Check: cuts that clip a word or land mid-breath; leftover countdowns, crew talk, "sorry", restarts;
the same line said twice; audio level jumps, clicks, room-tone changes at cuts; B-roll that does not match what she is
saying at that moment; shaky, soft, or badly exposed shots; flash frames; pacing that drags. Score 1-10 as a YouTube
real-estate editor would, and give the verdict in one sentence."""


def qa(work: Path, video: Path, model: str, effort: str) -> None:
    from guestlist.meta import spark

    proxy = work / f"{video.stem}_qa_proxy.mp4"
    if not proxy.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf", "scale=640:-2,fps=12", "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "28", "-ac", "1", "-c:a", "aac", "-b:a", "64k",
                        "-movflags", "+faststart", str(proxy)], check=True)
    minutes = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    str(proxy)], capture_output=True, text=True).stdout) / 60
    r = spark.video_json(proxy, schema=QA_SCHEMA, name="qc_report", developer=QA_DEVELOPER,
                         prompt=QA_PROMPT.format(minutes=minutes), model=model, effort=effort,
                         max_output_tokens=12000, max_usd=3.0)
    report = {"model": model, "usd": round(r.usd, 4), "seconds": round(r.seconds, 1), "cached": r.cached, **r.data}
    (work / f"{video.stem}_qa_{model}.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ("model", "usd", "seconds", "score", "verdict")}, indent=2))
    for f in sorted(report["findings"], key=lambda f: f["t_start"]):
        print(f"{f['t_start']:6.1f}-{f['t_end']:6.1f} {f['severity']:7} {f['category']:20} {f['evidence'][:110]}")


def _ramp(source: Path, start: float, legs: list[list[float]], out: Path, fps: float = 60000 / 1001) -> float:
    """Speed ramp by time remapping. legs = [[seconds_out, speed_from, speed_to], ...]; speed eases between the two
    with a smoothstep (no hard gear change). Motion blur comes from real frames: each output frame averages every
    source frame its 360-degree shutter covers (3x = 3 frames blended), so fast parts streak and slow parts stay
    sharp (each source frame repeated, never cross-blended, which would ghost). Returns the output length."""
    import numpy as np

    speeds = []
    for secs, v0, v1 in legs:
        n = round(secs * fps)
        speeds += [v0 + (v1 - v0) * (3 * u * u - 2 * u ** 3) for u in ((i + 0.5) / n for i in range(n))]
    src_len = sum(speeds) / fps + 0.2
    w, h = 1920, 1080
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{src_len:.3f}", "-i", str(source),
                            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={fps}",
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
                            "-r", "60000/1001", "-i", "-", "-c:v", "libx264", "-preset", "fast", "-crf", "16",
                            "-pix_fmt", "yuv420p", str(out)], stdin=subprocess.PIPE)
    buf, read = {}, 0  # source frame index -> frame

    def frame(i: int):
        nonlocal read
        while read <= i:
            raw = dec.stdout.read(w * h * 3)
            if len(raw) < w * h * 3:
                return buf[max(buf)]  # past the end: hold the last frame
            buf[read] = np.frombuffer(raw, np.uint8).reshape(h, w, 3)
            read += 1
        return buf[i]

    pos = 0.0  # source position in frames
    for v in speeds:
        first, last = int(pos), max(int(pos), int(pos + v) - 1)  # shutter covers [pos, pos + v)
        frames = [frame(i) for i in range(first, last + 1)]
        img = frames[0] if len(frames) == 1 else (sum(f.astype(np.float32) for f in frames) / len(frames)).astype(np.uint8)
        enc.stdin.write(img.tobytes())
        for i in [i for i in buf if i < first]:
            del buf[i]
        pos += v
    enc.stdin.close()
    enc.wait()
    dec.kill()
    return len(speeds) / fps


def intro(work: Path, spec_file: Path) -> None:
    """Cinematic cold open: shots with slow push-ins, a title card, the hook line.
    spec: {"shots": [{"source", "start", "end", "push": 1.07, "speed": 0.5 | "ramp": [[secs, v0, v1], ...]}], "title": {"lines": [..], "at": s, "until": s},
           "hook": {"source", "start", "end", "at": s}}  -> intro/intro.mp4 (same format as the tour cut)."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    spec = json.loads(spec_file.read_text())
    idir = work / "intro"
    idir.mkdir(exist_ok=True)
    parts = []
    for k, sh in enumerate(spec["shots"]):
        if "ramp" in sh:  # speed ramp: render the time-remapped shot first, then push in on it like any other shot
            ramped = idir / f"ramp{k}.mp4"
            dur = _ramp(Path(sh["source"]), sh["start"], sh["ramp"], ramped)
            sh = {**sh, "source": str(ramped), "start": 0.0, "end": dur}
        speed = sh.get("speed", 1.0)  # 0.5 = slow-mo: source start-end plays over twice the time
        dur = (sh["end"] - sh["start"]) / speed
        push = sh.get("push", 1.0)
        zoom = f"scale=w='1920*(1+{push - 1:.4f}*t/{dur:.3f})':h=-2:eval=frame,crop=1920:1080," if push != 1.0 else ""
        part = idir / f"shot{k}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{sh['start']:.3f}", "-t", f"{dur * speed:.3f}", "-i", sh["source"],
                        "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
                        f"setpts=(PTS-STARTPTS)/{speed},{zoom}fps=60000/1001,format=yuv420p", "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                        str(part)], check=True)
        parts.append(part)
    (idir / "list.txt").write_text("".join(f"file '{p}'\n" for p in parts))
    silent = idir / "pictures.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(idir / "list.txt"), "-c", "copy",
                    str(silent)], check=True)
    total = sum(float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                      str(p)], capture_output=True, text=True).stdout) for p in parts)

    # Title card: two lines, soft shadow, lower-left third (reel-captions house style, 16:9).
    card = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    big = ImageFont.truetype("/System/Library/Fonts/Avenir Next.ttc", 92, index=2)
    small = ImageFont.truetype("/System/Library/Fonts/Avenir Next.ttc", 40, index=5)
    lines = spec["title"]["lines"]
    shadow = Image.new("RGBA", card.size, (0, 0, 0, 0))
    for layer, fill in ((shadow, (0, 0, 0, 170)), (card, (255, 255, 255, 255))):
        d = ImageDraw.Draw(layer)
        d.text((120, 760), lines[0], font=big, fill=fill)
        if len(lines) > 1:
            d.text((124, 880), lines[1], font=small, fill=fill)
    card = Image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10)), card)
    card.save(idir / "title.png")

    t0, t1 = spec["title"]["at"], spec["title"]["until"]
    hook = spec["hook"]
    hd = hook["end"] - hook["start"]
    out = idir / "intro.mp4"
    # The hook audio is rendered on its own: in one graph with the title overlay, ffmpeg 8 restarted the audio
    # timestamps where the first shot ended (800 AAC frames for 12.8 s, audio 4 s too long when decoded).
    hook_wav = idir / "hook.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{hook['start']:.3f}", "-t", f"{hd:.3f}", "-i", hook["source"],
                    "-af", f"afade=t=in:d=0.05,afade=t=out:st={hd - 0.08:.3f}:d=0.08,adelay={int(hook['at'] * 1000)}:all=1,"
                    f"apad=whole_dur={total:.3f},aresample=48000,aformat=channel_layouts=stereo,"
                    f"atrim=end_sample={round(total * 48000)}", "-c:a", "pcm_s16le", str(hook_wav)], check=True)
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-i", str(silent), "-loop", "1", "-t", f"{total:.3f}", "-i", str(idir / "title.png"),
        "-i", str(hook_wav), "-filter_complex",
        f"[1:v]format=rgba,fade=t=in:st={t0:.3f}:d=0.27:alpha=1,fade=t=out:st={t1 - 0.4:.3f}:d=0.4:alpha=1[t];"
        f"[0:v]fade=t=in:st=0:d=0.2[b];[b][t]overlay=0:0:enable='between(t,{t0:.3f},{t1:.3f})'[v]",
        "-map", "[v]", "-map", "2:a", "-t", f"{total:.3f}", "-r", "60000/1001", "-c:v", "libx264", "-preset", "fast",
        "-crf", "19", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out)], check=True)
    print(json.dumps({"intro": str(out), "seconds": round(total, 2)}))


def _ping(sr: int = 48000):
    """Soft two-note notification chime (G6 then C7, 70 ms apart) with a short bell decay."""
    import numpy as np

    t = np.arange(int(sr * 0.9)) / sr
    out = np.zeros_like(t)
    for f, delay, amp in ((1567.98, 0.0, 1.0), (2093.0, 0.07, 0.8)):
        tt = np.clip(t - delay, 0, None)
        env = np.where(t >= delay, (1 - np.exp(-tt / 0.003)) * np.exp(-tt / 0.18), 0)
        out += amp * env * (np.sin(2 * np.pi * f * tt) + 0.25 * np.sin(2 * np.pi * 2 * f * tt) * np.exp(-tt / 0.05))
    return out / np.abs(out).max()


def _whoosh(sr: int = 48000, dur: float = 0.35):
    """Airy slide-in whoosh: noise through a band-pass sweeping up, swelling into the card's landing."""
    import numpy as np

    rng = np.random.default_rng(7)
    n = int(sr * dur)
    noise = rng.standard_normal(n)
    out, lo = np.zeros(n), 0.0
    for i in range(n):  # one-pole low-pass whose cutoff sweeps 400 Hz -> 5 kHz
        fc = 400 * (5000 / 400) ** (i / n)
        lo += (1 - np.exp(-2 * np.pi * fc / sr)) * (noise[i] - lo)
        out[i] = lo
    out -= np.convolve(out, np.ones(48) / 48, "same")  # drop the rumble
    env = np.sin(np.pi * np.linspace(0, 1, n) ** 0.7) ** 2
    out *= env
    return out / np.abs(out).max()


CARD_MAX_W = 1784  # px: a card PNG at x=96 must end 40 px inside the 1920 frame
CARD_PATHLIKE = re.compile(r"(Users|private|Volumes|tmp|var)/|\.(sh|py)\b")  # v16 burned a scratch path into a card


def _vdur(path: Path) -> float:
    """Duration of the first video stream (format duration if the container has none)."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                     str(path)], capture_output=True, text=True).stdout)


def facts(work: Path, video: Path | list[Path], spec_file: Path, out: Path, probe: float | None = None) -> Path:
    """On-screen fact cards (only what the agent says on camera): each line slides in on the word that states it, with
    a soft chime; the first line of a card also gets a whoosh. Picture is re-encoded with the cards, audio copied, and
    the effects are written next to `out` as <out>.sfx.wav for the mix step (kept out of the voice so isolation and
    ducking never touch them).
    video as a list: the section pictures in program order, joined frame-exactly by the concat filter (each normalised
    to SAR 1), output video only, SFX bed as long as the summed video streams. probe=s: the same graph for s seconds
    into the null muxer, no SFX. A card line wider than CARD_MAX_W or text that looks like a path raises ValueError.
    spec: {"accent": [r, g, b]?, "cards": [{"until": s, "caption": "SMALL LABEL", "size": 60, "lines": [["TEXT", at_s], ...]}]}"""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    spec = json.loads(spec_file.read_text())
    fdir = work / "facts"
    fdir.mkdir(exist_ok=True)
    accent = tuple(spec.get("accent", [196, 98, 62])) + (255,)  # default: warm terracotta

    def png(text: str, size: int, name: str, bar: bool) -> tuple[Path, int, int]:
        if CARD_PATHLIKE.search(text):
            raise ValueError(f"card text looks like a file path: {text!r}")
        font = ImageFont.truetype("/System/Library/Fonts/Avenir Next.ttc", size, index=2)
        w, h = (int(v) for v in font.getbbox(text)[2:])
        pad, x0 = 24, (22 if bar else 0)
        if w + x0 + 2 * pad > CARD_MAX_W:
            raise ValueError(f"card line is {w + x0 + 2 * pad} px wide (max {CARD_MAX_W}): {text!r}")
        img = Image.new("RGBA", (w + x0 + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
        shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).text((pad + x0, pad), text, font=font, fill=(0, 0, 0, 230))
        img = Image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(9)), img)
        d = ImageDraw.Draw(img)
        # Translucent dark panel behind the text: readable on white web pages as well as sky and walls.
        d.rounded_rectangle((pad - 14, pad - int(h * 0.18), img.width - pad + 14, pad + h + int(h * 0.22)), radius=10,
                            fill=(10, 10, 10, 150))
        if bar:
            d.rectangle((pad, pad + int(h * 0.12), pad + 7, pad + h), fill=accent)
        d.text((pad + x0, pad), text, font=font, fill=(255, 255, 255, 255) if bar else (255, 236, 222, 255))
        f = fdir / f"{name}.png"
        img.save(f)
        return f, img.width, img.height

    items = []  # (png, x, y, at, until)
    sfx_at = []  # (time, kind)
    for c, card in enumerate(spec["cards"]):
        size = card.get("size", 60)
        lines = [png(text, size, f"c{c}l{k}", True) + (at,) for k, (text, at) in enumerate(card["lines"])]
        cap = png(card["caption"], 38, f"c{c}cap", False) + (card["lines"][0][1],) if card.get("caption") else None
        y = 1080 - 110 - sum(h - 34 for _, _, h, _ in lines)
        if cap:
            items.append((cap[0], 96, y - cap[2] + 22, cap[3], card["until"]))
        for f, _, h, at in lines:
            items.append((f, 96, y, at, card["until"]))
            sfx_at.append((at, "ping"))
            y += h - 34
        sfx_at.append((card["lines"][0][1] - 0.2, "whoosh"))

    many = isinstance(video, list)
    videos = video if many else [video]
    inputs, chain, last = [x for v in videos for x in ("-i", str(v))], [], "0:v"
    if many:  # no stream-copy concat: no priming shift, no held frames at the joins, each member decoded on its own
        chain = [f"[{i}:v]setsar=1[s{i}]" for i in range(len(videos))]
        chain.append("".join(f"[s{i}]" for i in range(len(videos))) + f"concat=n={len(videos)}:v=1:a=0[base]")
        last = "base"
    for k, (f, x, y, a, u) in enumerate(items, len(videos)):
        inputs += ["-loop", "1", "-framerate", "60000/1001", "-t", f"{u - a:.3f}", "-i", str(f)]
        chain.append(f"[{k}:v]format=rgba,fade=t=in:d=0.25:alpha=1,fade=t=out:st={u - a - 0.35:.3f}:d=0.35:alpha=1,"
                     f"setpts=PTS+{a:.3f}/TB[f{k}]")
        chain.append(f"[{last}][f{k}]overlay=x='{x}-36*pow(max(0\\,1-(t-{a:.3f})/0.3)\\,2)':y={y}:"
                     f"enable='between(t,{a:.3f},{u:.3f})':eof_action=pass[v{k}]")
        last = f"v{k}"
    amap, acodec = (["-an"], []) if many else (["-map", "0:a"], ["-c:a", "copy"])
    dest = ["-t", f"{probe:.3f}", "-f", "null", "-"] if probe else ["-movflags", "+faststart", str(out)]
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(chain), "-map", f"[{last}]",
                    *amap, "-c:v", "libx264", "-preset", "fast", "-crf", "19", "-r", "60000/1001", "-pix_fmt",
                    "yuv420p", *acodec, *dest], check=True)
    if probe:
        return out

    sr = 48000
    dur = sum(_vdur(v) for v in videos) if many else \
        float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                             capture_output=True, text=True).stdout)
    bed = np.zeros(int(dur * sr) + sr)
    sounds = {"ping": _ping(sr) * 10 ** (-17 / 20), "whoosh": _whoosh(sr) * 10 ** (-26 / 20)}
    for at, kind in sfx_at:
        s = sounds[kind]
        i = max(0, int(at * sr))
        bed[i:i + len(s)] += s[: len(bed) - i]
    sfx = out.with_suffix(".sfx.wav")
    stereo = np.repeat(bed[: int(dur * sr)].astype(np.float32)[:, None], 2, 1)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(sr), "-ac", "2", "-i", "-", str(sfx)],
                   input=stereo.tobytes(), check=True)
    print(json.dumps({"cards": len(spec["cards"]), "overlays": len(items), "sfx": len(sfx_at), "out": str(out)}))
    return sfx


def _lufs(path: Path, extra: list[str] | None = None) -> float:
    out = subprocess.run(["ffmpeg", "-nostats", *(extra or []), "-i", str(path), "-vn", "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    return float([ln for ln in out.splitlines() if ln.strip().startswith("I:")][-1].split()[1])


def _isolate_voice(video: Path, a: float, b: float, out: Path) -> Path:
    """Replace a-b of the video's audio with ElevenLabs voice isolation (outdoor wind, room hum), loudness-matched to
    what it replaces. a and b should be cut points (no crossfade, so the splice keeps sync to the sample)."""
    import os
    import urllib.request
    import uuid

    import numpy as np

    win, iso = out.with_suffix(".win.wav"), out.with_suffix(".iso.mp3")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(video), "-vn",
                    "-ar", "48000", "-ac", "2", str(win)], check=True)
    if not iso.exists():
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"win.wav\"\r\n"
                "Content-Type: audio/wav\r\n\r\n").encode() + win.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request("https://api.elevenlabs.io/v1/audio-isolation", data=body, headers={
            "xi-api-key": os.environ["ELEVENLABS_API_KEY"], "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=300) as r:
            iso.write_bytes(r.read())

    def pcm(path: Path):  # placed by timestamp (async), so concat priming can't shift sample N away from N/48000 s
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-af", "aresample=48000:async=1:first_pts=0",
                              "-ac", "2",
                              "-f", "f32le", "-"], capture_output=True, check=True).stdout
        return np.frombuffer(raw, np.float32).reshape(-1, 2)

    full, clean = pcm(video).copy(), pcm(iso) * 10 ** ((_lufs(win) - _lufs(iso)) / 20)
    # The service can return the clean voice a few ms late (codec delay): align it to the original by correlating
    # 10 ms loudness envelopes, then drop the lead.
    env = lambda x: np.abs(x[:, 0]).reshape(-1)[: len(x) // 480 * 480].reshape(-1, 480).mean(1)  # noqa: E731
    e0, e1 = env(pcm(win)), env(clean)
    m = min(len(e0), len(e1))
    lag = max(range(-30, 31), key=lambda k: float(np.dot(e0[30: m - 30], e1[30 + k: m - 30 + k])))  # +-300 ms
    clean = clean[lag * 480:] if lag >= 0 else np.pad(clean, ((-lag * 480, 0), (0, 0)))
    i0, i1 = round(a * 48000), round(b * 48000)
    full[i0:i1] = np.pad(clean, ((0, max(0, i1 - i0 - len(clean))), (0, 0)))[:i1 - i0]  # same length: sync kept
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", "48000", "-ac", "2", "-i", "-", str(out)],
                   input=full.tobytes(), check=True)
    print(json.dumps({"isolated": [a, b], "lag_ms": lag * 10}))
    return out


def _loudness(path: Path) -> tuple[float, float]:
    """Integrated loudness (LUFS) and true peak (dBTP) in one ebur128 pass."""
    err = subprocess.run(["ffmpeg", "-nostats", "-i", str(path), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    last = lambda key: float([ln for ln in err.splitlines() if ln.strip().startswith(key)][-1].split()[1])  # noqa: E731
    return last("I:"), last("Peak:")


def mix(work: Path, video: Path, music: Path, out: Path, under: float = 20.0, intro: float = 0.0,
        isolate: tuple[float, float] | None = None, sfx: Path | None = None, voice: Path | None = None) -> dict:
    """Music bed under the voice: looped with 2 s crossfades (only when shorter than the video), set `under` LU below
    the voice, ducked further while she talks (sidechain), faded in/out; voice gets an 80 Hz high-pass (wind/handling
    rumble). Picture is copied. voice: a program voice WAV (Render's) used instead of the video's own audio."""
    if voice and isolate:
        raise ValueError("mix: pass voice or isolate, not both")
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                               capture_output=True, text=True).stdout)
    mdur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(music)],
                                capture_output=True, text=True).stdout)
    copies = 1 if mdur >= dur else int(dur // (mdur - 2.0)) + 2  # a long enough bed never loops into its own start
    bed = work / "music_bed.wav"
    graph = "[0:a]anull[x0]" + "".join(f";[x{i - 1}][{i}:a]acrossfade=d=2[x{i}]" for i in range(1, copies)) + \
            f";[x{copies - 1}]atrim=0:{dur:.3f},afade=t=in:d=1.5,afade=t=out:st={dur - 4:.3f}:d=4[out]"
    subprocess.run(["ffmpeg", "-v", "error", "-y", *sum((["-i", str(music)] for _ in range(copies)), []),
                    "-filter_complex", graph, "-map", "[out]", "-ar", "48000", "-ac", "2", str(bed)], check=True)
    level = voice or video
    voice = voice or (_isolate_voice(video, *isolate, work / "voice_isolated.wav") if isolate else video)
    gain = (_lufs(level) - under) - _lufs(bed)
    lift = 10 ** (12 / 20)  # the music carries the cold open: +12 dB until the tour starts
    graph = (f"[0:a]highpass=f=80,asplit=2[v][key];[1:a]volume={gain:.2f}dB,"
             f"volume='if(lt(t,{intro:.3f}),{lift:.4f},1)':eval=frame[m];"
             "[m][key]sidechaincompress=threshold=0.05:ratio=4:attack=30:release=500[md];"
             # fact-card chimes (sfx) sit beside the voice, never key the duck
             f"[v][md]{'[3:a]' if sfx else ''}amix=inputs={3 if sfx else 2}:duration=first:normalize=0,"
             # loudnorm's flush stamps its last 3 s as if the input ended on a 100 ms boundary (a gap of up to 100 ms
             # late, audio longer than the picture): re-stamp by sample count.
             "loudnorm=I=-14:TP=-1.5:LRA=11,asetpts=N/SR/TB[a]")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(voice), "-i", str(bed), "-i", str(video),
                    *(["-i", str(sfx)] if sfx else []), "-filter_complex", graph,
                    "-map", "2:v", "-map", "[a]", "-c:v", "copy", "-ar", "48000", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", str(out)], check=True)
    lufs, tp = _loudness(out)
    res = {"music_gain_db": round(gain, 2), "copies": copies, "out": str(out), "lufs": lufs, "tp": tp}
    print(json.dumps(res))
    return res


PHONE_LIMIT = "alimiter=limit=0.631:level=0:latency=1"  # -4 dBFS: r002 measured +1.1 dBTP without it, -0.7 with it


def phone_copy(master: Path, out: Path, passlog: Path) -> Path:
    """960x540 29.97 fps phone copy, 2-pass x264 at 200k + AAC 72k. The fps filter keeps every other master frame on
    its own timestamp (cuts within one source frame); -r 30000/1001 put them 50 ms late. AAC at 72k overshoots the
    master's peaks by up to 3 dB, so a -4 dBFS limiter (latency-compensated, no make-up gain) keeps it under 0 dBTP."""
    enc = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(master), "-vf", "fps=30000/1001,scale=960:540",
           "-c:v", "libx264", "-b:v", "200k", "-passlogfile", str(passlog)]
    subprocess.run([*enc, "-pass", "1", "-an", "-f", "null", "-"], check=True)
    subprocess.run([*enc, "-pass", "2", "-af", PHONE_LIMIT, "-c:a", "aac", "-b:a", "72k", "-movflags", "+faststart",
                    str(out)], check=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["organize", "cut", "refine", "broll-proxy", "broll", "intro", "facts", "mix", "qa"])
    ap.add_argument("work", type=Path)
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--broll-dir", type=Path, action="append", default=[], help="B-roll folder (repeatable)")
    ap.add_argument("--video", type=Path, help="qa: the draft to review")
    ap.add_argument("--pieces", type=Path, help="cut: render these pieces instead of re-deriving them")
    ap.add_argument("--words", type=Path, help="refine: word-timed Whisper JSON of the rendered cut")
    ap.add_argument("--subject", default="", help="broll: what the home for sale looks like")
    ap.add_argument("--exclude-tags", default="", help="broll: comma-separated shot tags never to use")
    ap.add_argument("--cover", default="", help="broll: start-end ranges (final seconds) to hide under B-roll, comma-separated")
    ap.add_argument("--model", default="muse-wonder-1.0", help="qa: reviewer model")
    ap.add_argument("--insert", default="", help="broll: start-end=TAG@shot_start inserts, comma-separated")
    ap.add_argument("--music", type=Path, help="mix: music bed")
    ap.add_argument("--out", type=Path, help="mix: output file")
    ap.add_argument("--spec", type=Path, help="intro: JSON spec")
    ap.add_argument("--drop-at", default="", help="broll: comma-separated times whose overlay is dropped")
    ap.add_argument("--broll-speed", type=float, default=1.0, help="broll: playback speed of every overlay (0.5 = slow-mo)")
    ap.add_argument("--reuse-plan", action="store_true", help="broll: reuse the saved Spark plan (after small trims)")
    ap.add_argument("--cues", type=Path, help="broll: narration .srt (default: the newest ghostcut_test_v*.srt)")
    ap.add_argument("--isolate", default="", help="mix: A-B seconds of voice to clean with ElevenLabs voice isolation")
    ap.add_argument("--retime", default="", help="broll: at=dt pairs, timeline seconds cut (dt<0) since the reused plan")
    ap.add_argument("--sfx", type=Path, help="mix: effects track (from the facts step) laid beside the voice")
    ap.add_argument("--intro-len", type=float, default=0.0, help="mix: seconds of cold open at the start (music lifted)")
    ap.add_argument("--voice", type=Path, help="mix: program voice WAV used instead of the video's audio")
    args = ap.parse_args()
    {"organize": lambda: organize(args.work, args.effort), "cut": lambda: cut(args.work, args.pieces), "refine": lambda: refine(args.work, args.words),
     "broll-proxy": lambda: broll_proxy(args.work, args.broll_dir),
     "broll": lambda: broll(args.work, args.effort, args.subject, tuple(filter(None, args.exclude_tags.split(","))),
                            tuple(tuple(float(x) for x in r.split("-")) for r in filter(None, args.cover.split(","))),
                            tuple(filter(None, args.insert.split(","))),
                            tuple(float(x) for x in filter(None, args.drop_at.split(","))), args.broll_speed,
                            args.reuse_plan, args.cues,
                            tuple(tuple(float(x) for x in r.split("=")) for r in filter(None, args.retime.split(",")))),
     "intro": lambda: intro(args.work, args.spec),
     "facts": lambda: facts(args.work, args.video, args.spec, args.out),
     "mix": lambda: mix(args.work, args.video, args.music, args.out, intro=args.intro_len,
                        isolate=tuple(float(x) for x in args.isolate.split("-")) if args.isolate else None, sfx=args.sfx,
                        voice=args.voice),
     "qa": lambda: qa(args.work, args.video, args.model, args.effort)}[args.step]()


if __name__ == "__main__":
    main()
