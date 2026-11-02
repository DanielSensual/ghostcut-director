"""Render v1 planning: the v16 import (the r000 timeline), accepted Muse edits turned into explicit placements, and
the checks that refuse anything Render can't apply before a single frame is encoded. Nothing here encodes media.

Times: a timeline keeps exact section starts from video frame counts; overlays sit at section seconds r (shown at
t + r), d is the on-screen length and `in` is in file seconds. Cards and notes are absolute program seconds.
"""
from __future__ import annotations

import copy
import functools
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from muse import CARD_MIN, CARD_TAIL, MIN_GAP, MIN_INSERT  # noqa: E402  (one source for Muse's checks)

SCHEMA = "ghostcut.timeline/1"
RATE = (60000, 1001)
SR = 48000
GUARD = re.compile(r"(Users|private|Volumes|tmp|var)/|\.(sh|py)\b")
MAX_LINE_PX = 1784
FONT = "/System/Library/Fonts/Avenir Next.ttc"  # the face facts() draws cards with
EDGE = 0.3   # an overlay keeps this far from its section's edges
TAIL = 0.02  # in + speed·d stays this far before the end of the shot file
EPS = 1e-6
FILES = {"master": "master.mp4", "phone": "phone.mp4", "picture": "picture.mp4", "voice": "voice.wav",
         "music": "music_bed.wav", "sfx": "picture.sfx.wav", "transcript": "transcript/voice.json"}
FIXES_V16 = ["card16_text", "voice_sync", "frame_exact_joins", "phone_fps", "music_loop"]
FIX_LABELS = {"voice_sync": "Voice re-synced to picture", "frame_exact_joins": "Frame-exact section joins",
              "phone_fps": "Phone copy timing", "music_loop": "Music bed no longer loops into its start"}
CUT = "Render v1 can't cut talk yet — ask for a note instead"
CROSS = "B-roll stays inside one section"
COLD = "The cold open is a finished piece"


class Refuse(Exception):
    pass


def _sec(frames: int) -> float:
    return round(frames * RATE[1] / RATE[0], 6)


def _ffprobe(path: Path, entries: str, extra: tuple[str, ...] = ()) -> str:
    r = subprocess.run(["ffprobe", "-v", "error", *extra, "-select_streams", "v:0", "-show_entries", entries,
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError(f"ffprobe {Path(path).name}: {r.stderr.strip()[:200]}")
    return r.stdout.strip().split("\n")[0].strip().rstrip(",")


# ---------- job + probes ----------

def load_job(demo: Path) -> dict:
    """app/ui/demo/job.json with `facts` resolved against the demo dir."""
    demo = Path(demo)
    job = json.loads((demo / "job.json").read_text())
    if job.get("facts"):
        job["facts"] = str((demo / job["facts"]).resolve())
    return job


def probe_frames(path: Path) -> int:
    n = _ffprobe(path, "stream=nb_frames")
    if not n.isdigit():
        n = _ffprobe(path, "stream=nb_read_packets", ("-count_packets",))
    return int(n)


def probe_start(path: Path) -> float:
    v = _ffprobe(path, "stream=start_time")
    return float(v) if v not in ("", "N/A") else 0.0


@functools.lru_cache(maxsize=256)
def probe_fps(path: str) -> float | None:
    try:
        num, _, den = _ffprobe(Path(path), "stream=r_frame_rate").partition("/")
        return float(num) / float(den or 1)
    except (RuntimeError, ValueError, ZeroDivisionError, subprocess.SubprocessError, OSError):
        return None


# ---------- r000: the v16 build as a timeline ----------

def _pieces(work: Path, frames_of: Callable[[Path], int]) -> list[dict]:
    out, a = [], 0
    for p in json.loads((work / "pieces.json").read_text()):
        zoom = float(p.get("zoom", 1.0))
        key = hashlib.sha1(f"{p['source']}|{p['src_in']}|{p['src_out']}|{zoom}".encode()).hexdigest()[:12]
        n = frames_of(work / "render" / f"piece_{key}.mp4")
        out.append({"source": p["source"], "src_in": p["src_in"], "src_out": p["src_out"], "zoom": zoom,
                    "topic": p.get("topic", ""), "A": _sec(a), "d": _sec(n), "frames": n})
        a += n
    return out


def _placed(work: Path, s: int, speed: float) -> list[dict]:
    plan = json.loads((work / "broll_plan.json").read_text())
    bmap = {b["tag"]: b for b in json.loads((work / "broll_map.json").read_text())}
    out = []
    for i, o in enumerate(plan["placed"]):
        b = bmap.get(o["tag"])
        if not b:
            raise ValueError(f"{work.name}: placed tag {o['tag']} is missing from broll_map.json")
        out.append({"key": f"v16:{s}:{i}", "tag": o["tag"], "source": b["source"], "file_dur": float(b["duration"]),
                    "in": round(o["broll_start"], 6), "r": round(o["timeline_start"], 6),
                    "d": round(o["broll_end"] - o["broll_start"], 6), "speed": speed, "reason": o.get("reason", ""),
                    "origin": "v16"})
    return out


def import_v16(job: dict, probe: tuple[Callable[[Path], int], Callable[[Path], float]] | None = None) -> dict:
    """The r000 timeline: exact section starts from frame counts, pieces at A_i (running sum of piece lengths),
    overlays from broll_plan placed[], cards from the facts spec rebased from legacy to exact section starts."""
    frames_of, start_of = probe or (probe_frames, probe_start)
    speed = float(job.get("speed", 0.5))
    legacy = job.get("legacy_offsets") or [None] * len(job["sections"])
    secs, f0 = [], 0
    for i, js in enumerate(job["sections"]):
        n = frames_of(Path(js["picture"]))
        sec = {"name": js["name"], "kind": js["kind"], "start_frame": f0, "frames": n, "t": _sec(f0), "d": _sec(n),
               "legacy_t": legacy[i], "work": js.get("work"), "picture": js["picture"], "picture_rendered": None,
               "audio": js["audio"], "audio_from": js["audio"], "audio_offset": 0.0, "pieces": [], "broll": [],
               "isolate": js.get("isolate"), "isolated": [], "camera_fallback": []}
        if js["kind"] == "talk":
            work = Path(js["work"])
            sec["pieces"] = _pieces(work, frames_of)
            total = sum(p["frames"] for p in sec["pieces"])
            if n not in (total, total + 1):
                raise ValueError(f"{js['name']}: the picture has {n} frames but its pieces add up to {total}")
            sec["audio_offset"] = round(start_of(work / "render" / "joined.mp4"), 6)
            sec["broll"] = _placed(work, i, speed)
        else:
            sec["audio_offset"] = round(start_of(Path(js["audio"])), 6)
            sec["pieces"] = [{"source": js["picture"], "src_in": 0.0, "src_out": sec["d"], "zoom": 1.0,
                              "topic": js["name"], "A": 0.0, "d": sec["d"], "frames": n}]
        secs.append(sec)
        f0 += n

    spec = json.loads(Path(job["facts"]).read_text())
    cards = []
    for i, c in enumerate(spec["cards"]):
        first = min(t for _, t in c["lines"])
        s = max((j for j, x in enumerate(legacy) if x is not None and x <= first + EPS), default=None)
        sh = secs[s]["t"] - legacy[s] if s is not None else 0.0
        card = {"key": f"v16:c:{i}", "caption": c.get("caption", ""), "size": c.get("size", 60),
                "until": round(c["until"] + sh, 6), "lines": [[txt, round(t + sh, 6)] for txt, t in c["lines"]]}
        bad = card_problems(card)
        if bad:
            raise ValueError(f"facts card {i + 1}: {bad[0]}")
        cards.append(card)
    tl = {"schema": SCHEMA, "version": "r000", "parent": None, "created": datetime.now().isoformat(timespec="seconds"),
          "rate": list(RATE), "size": [1920, 1080], "sr": SR, "speed": speed, "frames": f0, "duration": _sec(f0),
          "files": dict(FILES), "mix": {"music_gain_db": None, "lufs": None, "tp": None}, "cards": cards,
          "sections": secs, "edits_applied": [], "fixes": list(job.get("fixes", FIXES_V16)), "verify": "verify.json"}
    if spec.get("accent"):
        tl["accent"] = spec["accent"]
    return tl


# ---------- coordinates ----------

def section_of(tl: dict, t: float) -> int:
    """Index of the section holding absolute time t (a timeline's or a project's own coordinates)."""
    secs = tl["sections"]
    return max((i for i, s in enumerate(secs) if s["t"] <= t + EPS), default=0)


def S_shown(tl_or_project: dict, s: int) -> float:
    """Section start as the app showed it: legacy offsets for the v16 project (and the r000 import of it),
    exact frame starts for a project built by Render."""
    p = tl_or_project
    sec = p["sections"][s]
    if p.get("schema") and p.get("version") == "r000" and sec.get("legacy_t") is not None:
        return sec["legacy_t"]
    return sec["t"]


def to_exact(tl: dict, shown: dict, t: float) -> float:
    """A time from the shown project → the timeline's exact program time (same offset into the same section)."""
    s = section_of(shown, t)
    return round(t - S_shown(shown, s) + tl["sections"][s]["t"], 6)


# ---------- in-point rule (shared with the app's frame preview) ----------

def _k(tag: str) -> int:
    return 2 if tag[:1] in ("V", "W", "P") else 1  # 2x prerenders: their library best range is in on-screen seconds


def _best(entry: dict) -> tuple[float, float | None, float | None, float]:
    dur = float(entry.get("dur") or entry.get("file_dur") or 0.0)
    k = _k(entry.get("tag", ""))
    b = [x or 0.0 for x in (entry.get("best") or [0.0, 0.0])][:2]  # a half-empty range reads as 0, as in Muse
    if len(b) == 2 and b[1] > b[0]:
        return (b[0] + b[1]) / 2 / k, b[0] / k, b[1] / k, dur
    return dur / 2, None, None, dur


def preview_t(entry: dict) -> float:
    """File seconds of the frame Muse previews: the middle of the best range (÷2 for V/W/P), or of the file."""
    return round(_best(entry)[0], 6)


def in_point(entry: dict, d: float, speed: float = 0.5) -> float:
    """File seconds an insert of on-screen length d starts at, so its middle frame is preview_t(entry)."""
    mid, lo, hi, dur = _best(entry)
    read = speed * d
    if lo is not None and lo <= min(hi, dur - TAIL) - read:
        return round(min(max(mid - read / 2, lo), min(hi, dur - TAIL) - read), 6)
    top = dur - TAIL - read
    if top < 0:
        raise ValueError(f"{entry.get('tag', 'The shot')} is too short: {dur:.2f} s, and {d:.2f} s at {speed:g}× "
                         f"reads {read:.2f} s")
    return round(min(max(mid - read / 2, 0.0), top), 6)


# ---------- card text guards ----------

@functools.lru_cache(maxsize=8)
def _font(size: int):
    try:
        from PIL import ImageFont
        return ImageFont.truetype(FONT, size, index=2)
    except (ImportError, OSError):
        return None


def line_px(text: str, size: int, bar: bool = True) -> int | None:
    """Width facts() gives this text's PNG (None when the font isn't available here)."""
    font = _font(size)
    if font is None:
        return None
    return int(font.getbbox(text)[2]) + (22 if bar else 0) + 48


def card_problems(card: dict, written: bool = False) -> list[str]:
    """Text guards for a card; `written` (an edit wrote its lines) also enforces 1–3 lines. v16 cards may have 4."""
    lines = [str(x) for x, _ in card.get("lines", [])]
    out = ["A card needs 1–3 lines"] if not lines or (written and len(lines) > 3) else []
    for text in lines + [card.get("caption") or ""]:
        if GUARD.search(text):
            out.append(f"Card text looks like a file path: “{text[:48]}”")
    for text, size, bar in [(x, int(card.get("size") or 60), True) for x in lines] + \
                           ([(card["caption"], 38, False)] if card.get("caption") else []):
        px = line_px(text, size, bar)
        if px and px > MAX_LINE_PX:
            out.append(f"“{text[:48]}” is too wide for the screen ({px} px > {MAX_LINE_PX})")
    return out


def _first(card: dict) -> float:
    return min(t for _, t in card["lines"])


def _card_window(tl: dict, first: float, until: float) -> str | None:
    intro_end = max((s["t"] + s["d"] for s in tl["sections"] if s["kind"] == "intro"), default=0.0)
    if first < intro_end - EPS:
        return COLD
    if first < intro_end + 0.2 - EPS:
        return f"A card starts at least 0.2 s after the cold open ({intro_end + 0.2:.2f} s)"
    if until > tl["duration"] - CARD_TAIL + EPS:
        return f"A card ends at least {CARD_TAIL:g} s before the end ({tl['duration'] - CARD_TAIL:.2f} s)"
    if until <= first:
        return "A card needs a length"
    return None


# ---------- validation ----------

def _problems(plan: dict) -> list[tuple[set, str]]:
    """(keys involved, message) for every rule the plan breaks."""
    out: list[tuple[set, str]] = []
    ch = plan.get("changes", [])
    moved = {c["key"] for c in ch if c["op"] in ("move_broll", "add_broll")}
    retag = {c["key"] for c in ch if c["op"] in ("swap_broll", "add_broll")}
    recard = {c["key"] for c in ch if c["op"] in ("move_card", "edit_card", "add_card")}
    written = {c["key"] for c in ch if c["op"] in ("edit_card", "add_card")}
    dirty = set(plan.get("dirty", []))
    tags = plan.get("tags") or {}
    known, blocked = tags.get("known"), tags.get("blocked", {})
    for s, sec in enumerate(plan["sections"]):
        ovs = sec["broll"]
        if sec["kind"] == "intro":
            if ovs:
                out.append(({o["key"] for o in ovs}, COLD))
            continue
        if [o["r"] for o in ovs] != sorted(o["r"] for o in ovs):
            out.append((set(), f"{sec['name']}: B-roll is not in time order"))
        for o in ovs:
            k = o["key"]
            if s not in dirty and k not in moved and k not in retag:
                continue  # a clean section keeps its v16 file untouched
            if o["r"] < EDGE - EPS or o["r"] + o["d"] > sec["d"] - EDGE + EPS:
                out.append(({k}, f"{o['tag']} at {o['r']:.2f}–{o['r'] + o['d']:.2f} s is closer than {EDGE} s to an "
                                 f"edge of {sec['name']} (0–{sec['d']:.2f} s)"))
            if o["in"] < -EPS or o["in"] + o["speed"] * o["d"] > o["file_dur"] - TAIL + EPS:
                out.append(({k}, f"{o['tag']} is too short: {o['file_dur']:.2f} s, and {o['d']:.2f} s at "
                                 f"{o['speed']:g}× from {o['in']:.2f} s needs {o['in'] + o['speed'] * o['d']:.2f} s"))
            if known is not None and o["tag"] not in known:
                out.append(({k}, f"{o['tag']} is not in the shot library"))
            if k in retag and o["tag"] in blocked:
                out.append(({k}, blocked[o["tag"]]))
        seen = set()
        for a in ovs:
            if a["key"] not in moved:
                continue
            for b in ovs:
                pair = frozenset((a["key"], b["key"]))
                if b is a or pair in seen:
                    continue
                if max(b["r"] - (a["r"] + a["d"]), a["r"] - (b["r"] + b["d"])) < MIN_GAP - EPS:
                    seen.add(pair)  # blame only what moved: a swap or an untouched neighbour can't cause a clash
                    out.append(({x["key"] for x in (a, b) if x["key"] in moved},
                                f"{a['tag']} touches {b['tag']} (keep ≥ {MIN_GAP} s between inserts)"))
    for c in plan["cards"]:
        out += [({c["key"]}, msg) for msg in card_problems(c, c["key"] in written)]
        if c["key"] in recard:
            msg = _card_window(plan, _first(c), c["until"])
            if msg:
                out.append(({c["key"]}, msg))
    return out


def validate(plan: dict) -> list[str]:
    """Every rule a plan (or timeline) breaks, as messages; [] means it can render."""
    return [msg for _, msg in _problems(plan)]


# ---------- edits → plan ----------

def _label(op: dict) -> str:
    return f"{op.get('op', '?')} {op.get('target') or op.get('src') or ''}".strip()


def make_plan(base: dict, shown: dict, edits: list[dict], library: list[dict], never_use: dict,
              fps: Callable[[str], float | None] | None = None) -> tuple[dict, list[dict]]:
    """Apply accepted edits (in order) to the base timeline. Returns (plan, refusals); any refusal means nothing
    may be encoded. `shown` is the project the app showed when the edits were made (positional ids for v16,
    `ref` for projects built by Render); its times convert as r = t − S_shown(section). Every op becomes exactly
    one change or one refusal; the plan holds only the applied ops (one refused by the final checks is taken out
    and the rest re-planned), so validate(plan) only reports problems no edit caused."""
    skip: dict[tuple, dict] = {}
    while True:
        plan, refusals, late = _apply(base, shown, edits, library, never_use, fps or probe_fps, skip)
        if not late:
            return plan, refusals
        skip.update(late)


def _apply(base: dict, shown: dict, edits: list[dict], library: list[dict], never_use: dict,
           fps: Callable[[str], float | None], skip: dict[tuple, dict]) -> tuple[dict, list[dict], dict]:
    plan = copy.deepcopy(base)
    plan.pop("created", None)
    plan.update(parent=base.get("version"), version=None, edits_applied=[],
                fixes=list(base.get("fixes", [])) if base.get("version") == "r000" else [])
    secs = plan["sections"]
    speed = float(plan.get("speed", 0.5))
    lib = {e["tag"]: e for e in library}
    blocked = {t: f"Editor rule: {why}" for t, why in (never_use or {}).items()}
    for e in library:
        if e.get("skip"):
            blocked.setdefault(e["tag"], f"Inventory says skip: {e.get('use', '')}"[:100].rstrip(": "))
    plan["tags"] = {"known": sorted(lib), "blocked": blocked}
    ovs = {o["key"]: o for sec in secs for o in sec["broll"]}
    where = {o["key"]: s for s, sec in enumerate(secs) for o in sec["broll"]}
    cards = {c["key"]: c for c in plan["cards"]}
    flat = [o for sec in base["sections"] for o in sec["broll"]]

    legacy = shown.get("version") is None
    mismatch = None
    if len(shown.get("sections", [])) != len(secs):
        mismatch = "The project's sections don't match the render base"
    elif legacy and base.get("version") not in ("r000", None):
        mismatch = f"The app shows the v16 project but the render base is {base.get('version')}: reload the app"
    elif not legacy and shown.get("version") != base.get("version"):
        mismatch = f"The app shows {shown.get('version')} but the render base is {base.get('version')}: reload the app"
    intro = next((i for i, s in enumerate(secs) if s["kind"] == "intro"), None)
    shown_intro_end = (shown["sections"][intro]["t"] + shown["sections"][intro]["d"]) \
        if intro is not None and not mismatch else 0.0
    items = {it["id"]: it for it in shown.get("broll", []) + shown.get("cards", [])}
    nb = sum(1 for b in shown.get("broll", []) if not b["id"].endswith("+"))
    nc = sum(1 for c in shown.get("cards", []) if not c["id"].endswith("+"))
    added: dict[str, str | None] = {}
    shown_d: dict[str, float] = {}
    dropped: set[str] = set()
    dirty: set[int] = set()
    changes, refusals, notes = [], [], []

    def warn(edit: str, t: float, text: str) -> None:
        notes.append({"t": round(t, 3), "src": "render", "state": "open", "title": text[:70], "body": text,
                      "check": "Render plan", "edit": edit})

    def broll_key(target: str) -> str:
        if target in added:
            if added[target] is None:
                raise Refuse(f"{target} was never added (its edit can't render)")
            key = added[target]
        else:
            it = items.get(target)
            if not it or not target.startswith("b"):
                raise Refuse(f"No B-roll item {target!r} in the project")
            if legacy:
                m = re.fullmatch(r"b(\d+)", target)
                if not m or not 1 <= int(m[1]) <= len(flat):
                    raise Refuse(f"No B-roll item {target!r} in the render base")
                ov = flat[int(m[1]) - 1]
            else:
                ref = it.get("ref") or []
                try:
                    ov = base["sections"][ref[0]]["broll"][ref[1]]
                except (IndexError, KeyError, TypeError):
                    raise Refuse(f"{target} has no place in the render base (ref {ref})") from None
            tag = it.get("was_tag") or it.get("tag")
            if tag != ov["tag"]:
                raise Refuse(f"{target} shows {tag} but the render base has {ov['tag']} there")
            key = ov["key"]
            shown_d.setdefault(key, (it.get("was") or [None, it.get("d", 0.0)])[1])
        if key in dropped:
            raise Refuse(f"{target} was dropped by an earlier edit")
        if secs[where[key]]["kind"] == "intro":
            raise Refuse(COLD)
        return key

    def card_key(target: str) -> str:
        if target in added:
            if added[target] is None:
                raise Refuse(f"{target} was never added (its edit can't render)")
            key = added[target]
        else:
            it = items.get(target)
            if not it or not target.startswith("c"):
                raise Refuse(f"No card {target!r} in the project")
            if legacy:
                m = re.fullmatch(r"c(\d+)", target)
                if not m or not 1 <= int(m[1]) <= len(base["cards"]):
                    raise Refuse(f"No card {target!r} in the render base")
                key = base["cards"][int(m[1]) - 1]["key"]
            else:
                ref = it.get("ref")
                ref = ref[0] if isinstance(ref, (list, tuple)) and ref else ref
                if not isinstance(ref, int) or not 0 <= ref < len(base["cards"]):
                    raise Refuse(f"{target} has no place in the render base (ref {it.get('ref')})")
                key = base["cards"][ref]["key"]
        if key in dropped:
            raise Refuse(f"{target} was dropped by an earlier edit")
        return key

    def span(t: float, d: float, s: int | None = None) -> int:
        """Section holding [t, t + d] in shown coordinates."""
        ss = section_of(shown, t)
        if t < shown_intro_end - EPS or secs[ss]["kind"] == "intro":
            raise Refuse(COLD)
        sec = shown["sections"][ss]
        if (s is not None and ss != s) or t + d > sec["t"] + sec["d"] + EPS:
            raise Refuse(CROSS)
        return ss

    def shot(tag: str) -> dict:
        e = lib.get(tag)
        if not e:
            raise Refuse(f"{tag!r} is not in the shot library")
        if tag in blocked:
            raise Refuse(blocked[tag])
        if not e.get("file") or not e.get("dur"):
            raise Refuse(f"{tag} has no file or length in the shot library")
        return e

    def placed_in(e: dict, d: float) -> float:
        try:
            return in_point(e, d, speed)
        except ValueError as err:
            raise Refuse(str(err)) from None

    def judder(edit: str, s: int, o: dict) -> None:
        rate = fps(o["source"])
        if rate and rate * o["speed"] < 29:
            warn(edit, secs[s]["t"] + o["r"], f"{o['tag']} is a {rate:g} fps shot: at {o['speed']:g}× it plays at "
                                             f"{rate * o['speed']:g} fps and can judder")

    def apply_op(edit: str, k: int, op: dict, new_key: str | None) -> tuple[int | None, str, str]:
        o, target = op["op"], op.get("target", "")
        t, d = float(op.get("t", -1)), float(op.get("d", -1))
        if mismatch:
            raise Refuse(mismatch)
        if o == "cut_span":
            raise Refuse(CUT)
        if o == "drop_broll":
            key = broll_key(target)
            s, ov = where[key], ovs[key]
            secs[s]["broll"].remove(ov)
            dropped.add(key)
            dirty.add(s)
            return s, key, f"Drop {target} {ov['tag']}"
        if o == "move_broll":
            key = broll_key(target)
            s, ov = where[key], ovs[key]
            nd = ov["d"] if abs(d - shown_d.get(key, ov["d"])) <= 0.05 else d  # Muse kept the displayed length
            span(t, nd, s)
            nin = ov["in"]
            if nin + ov["speed"] * nd > ov["file_dur"] - TAIL + EPS:
                nin = ov["file_dur"] - TAIL - ov["speed"] * nd
                if nin < 0:
                    raise Refuse(f"{ov['tag']} is too short: {ov['file_dur']:.2f} s, and {nd:.2f} s at "
                                 f"{ov['speed']:g}× reads {ov['speed'] * nd:.2f} s")
                warn(edit, secs[s]["t"] + t - S_shown(shown, s),
                     f"{target} {ov['tag']} now starts at {nin:.2f} s in the shot so it lasts {nd:.1f} s")
            ov.update(r=round(t - S_shown(shown, s), 6), d=round(nd, 6), **{"in": round(nin, 6)})
            shown_d[key] = round(d, 2)
            dirty.add(s)
            return s, key, f"Move {target} {ov['tag']} → {t:.1f} s for {nd:.1f} s"
        if o == "swap_broll":
            key = broll_key(target)
            s, ov = where[key], ovs[key]
            e = shot(op.get("src", ""))
            nin = placed_in(e, ov["d"])
            old = ov["tag"]
            ov.update(tag=e["tag"], source=e["file"], file_dur=float(e["dur"]), reason=op.get("why") or ov["reason"],
                      **{"in": nin})
            judder(edit, s, ov)
            dirty.add(s)
            return s, key, f"{target} {old} → {e['tag']}"
        if o == "add_broll":
            s = span(t, d)
            if d < MIN_INSERT - EPS:
                raise Refuse(f"An insert needs at least {MIN_INSERT:.1f} s")
            e = shot(op.get("src", ""))
            ov = {"key": new_key, "tag": e["tag"], "source": e["file"], "file_dur": float(e["dur"]),
                  "in": placed_in(e, d), "r": round(t - S_shown(shown, s), 6), "d": round(d, 6), "speed": speed,
                  "reason": op.get("why", ""), "origin": edit}
            secs[s]["broll"].append(ov)
            ovs[new_key], where[new_key], shown_d[new_key] = ov, s, round(d, 2)
            judder(edit, s, ov)
            dirty.add(s)
            return s, new_key, f"Add {e['tag']} at {t:.1f} s for {d:.1f} s"
        if o == "drop_card":
            key = card_key(target)
            c = cards[key]
            plan["cards"].remove(c)
            dropped.add(key)
            return section_of(plan, _first(c)), key, f"Drop card {target} “{c['lines'][0][0]}”"
        if o == "move_card":
            key = card_key(target)
            c = cards[key]
            if t < shown_intro_end - EPS:
                raise Refuse(COLD)
            first = to_exact(plan, shown, t)
            dt = first - _first(c)
            until = first + d if d > 0 else c["until"] + dt
            msg = _card_window(plan, first, until)
            if msg:
                raise Refuse(msg)
            c.update(lines=[[x, round(at + dt, 6)] for x, at in c["lines"]], until=round(until, 6))
            return section_of(plan, first), key, f"Move card {target} → {t:.1f} s"
        if o == "edit_card":
            key = card_key(target)
            c = cards[key]
            lines = [str(x) for x in op.get("lines") or []]
            old = [at for _, at in c["lines"]] or [c["until"] - 1.5]
            times = [old[j] if j < len(old) else round(max(old) + 0.6 * (j - len(old) + 1), 6)
                     for j in range(len(lines))]
            new = {**c, "lines": [[x, at] for x, at in zip(lines, times)], "caption": op.get("caption") or c["caption"]}
            if lines:
                new["until"] = round(max(c["until"], times[-1] + 1.5), 6)
            bad = card_problems(new, True) or [m for m in [_card_window(plan, _first(new), new["until"])] if m]
            if bad:
                raise Refuse(bad[0])
            c.update(new)
            return section_of(plan, _first(c)), key, f"Card {target} → “{' / '.join(lines)}”"
        if o == "add_card":
            if t < shown_intro_end - EPS:
                raise Refuse(COLD)
            first = to_exact(plan, shown, t)
            lines = [str(x) for x in op.get("lines") or []]
            c = {"key": new_key, "caption": op.get("caption", ""), "size": 60, "until": round(first + max(d, CARD_MIN), 6),
                 "lines": [[x, round(first + 0.6 * j, 6)] for j, x in enumerate(lines)]}
            bad = card_problems(c, True) or [m for m in [_card_window(plan, first, c["until"])] if m]
            if bad:
                raise Refuse(bad[0])
            plan["cards"].append(c)
            cards[new_key] = c
            return section_of(plan, first), new_key, f"New card “{' / '.join(lines)}” at {t:.1f} s"
        if o == "add_note":
            at = to_exact(plan, shown, max(t, 0.0))
            why = op.get("why", "")
            notes.append({"t": round(at, 3), "src": "you", "state": "open", "title": why[:70], "body": why,
                          "check": "Added through Muse", "edit": edit})
            return section_of(plan, at), new_key, f"Note at {t:.1f} s"
        raise Refuse(f"Render v1 can't apply {o!r}")

    total = 0
    for e in edits:
        labels = e.get("labels") or []
        for k, op in enumerate(e.get("ops", [])):
            total += 1
            new_key, new_id = f"{e['id']}:{k}", None
            if op.get("op") == "add_broll":  # numbered exactly as muse.apply numbers them
                nb += 1
                new_id = f"b{nb}+"
            elif op.get("op") == "add_card":
                nc += 1
                new_id = f"c{nc}+"
            if new_id:
                added[new_id] = new_key
            label = labels[k] if k < len(labels) else _label(op)
            try:
                if (e["id"], k) in skip:
                    raise Refuse(skip[(e["id"], k)]["reason"])
                s, key, what = apply_op(e["id"], k, op, new_key)
            except Refuse as r:
                refusals.append({"edit": e["id"], "op": op.get("op", ""), "label": label, "reason": str(r)})
                if new_id:
                    added[new_id] = None
                continue
            changes.append({"edit": e["id"], "op": op["op"], "section": s, "key": key, "label": what, "_at": (e["id"], k),
                            "_label": label})

    for sec in secs:
        sec["broll"].sort(key=lambda o: o["r"])
    plan["cards"].sort(key=_first)
    plan.update(dirty=sorted(dirty), changes=changes, notes=notes)
    late: dict[tuple, dict] = {}  # final-state problems go to the last op that touched one of their items
    for keys, msg in _problems(plan):
        hit = next((c for c in reversed(changes) if c["key"] in keys), None)
        if hit is None:
            refusals.append({"edit": None, "op": None, "label": ", ".join(sorted(keys)) or "plan", "reason": msg})
        elif hit["_at"] in late:
            late[hit["_at"]]["reason"] += f"; {msg}"
        else:
            late[hit["_at"]] = {"edit": hit["edit"], "op": hit["op"], "label": hit["_label"], "reason": msg}
    if late:
        return plan, refusals, late
    assert len(changes) + sum(1 for r in refusals if r["edit"] is not None) == total, "an op vanished from the plan"
    for c in changes:
        del c["_at"], c["_label"]
    refused = {r["edit"] for r in refusals}
    plan["edits_applied"] = [e["id"] for e in edits if e["id"] not in refused]
    plan["notes"] = [n for n in notes if n["edit"] not in refused]
    return plan, refusals, {}


def plan_cards(plan: dict) -> dict:
    """The facts() spec for a plan or timeline: cards in absolute program time."""
    spec = {"cards": [{"caption": c.get("caption", ""), "size": c.get("size", 60), "until": c["until"],
                       "lines": [[x, at] for x, at in c["lines"]]} for c in plan["cards"]]}
    if plan.get("accent"):
        spec["accent"] = plan["accent"]
    return spec


def fix_label(tl: dict, fix: str) -> str:
    """A base repair as the plan dialog lists it."""
    m = re.fullmatch(r"card(\d+)_text", fix)
    if m:
        c = next((c for c in tl.get("cards", []) if c.get("key") == f"v16:c:{m[1]}"), None)
        at = f" at {int(_first(c) // 60)}:{int(_first(c) % 60):02d}" if c else ""
        return f"Card {int(m[1]) + 1} text{at}"
    return FIX_LABELS.get(fix, fix.replace("_", " "))
