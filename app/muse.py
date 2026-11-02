"""Muse: a typed edit request → Spark proposes timeline operations → mechanical checks → a reversible edit layer.

Spark only proposes. Every operation is checked against the project itself (ids, ranges, neighbours, the shot
library, Whisper word timings) before the editor can accept it, and accepted operations live in an edit list
on top of the rendered cut, so any of them can be undone. Nothing here touches footage or renders.
"""
from __future__ import annotations

import copy
import re

OPS = ["drop_broll", "move_broll", "swap_broll", "add_broll", "drop_card", "move_card", "edit_card", "add_card",
       "cut_span", "add_note"]
MIN_GAP = 0.05  # an insert that touches its neighbour drops it at render time (render_plan.validate uses this one)
SPEED = 0.5  # B-roll plays at half speed
EDGE = 0.3  # an insert keeps this far from its section's edges
CARD_EDGE = 0.2  # a card starts at least this long after the cold open
# Render v1's own numbers (render_plan imports MIN_GAP from here, so these can't come from render_plan)
MIN_INSERT = 1.0  # Render refuses an added insert shorter than this
CARD_TAIL = 0.5  # a card ends at least this long before the end of the video
CARD_MIN = 2.0  # Render keeps an added card up at least this long
CUT_REFUSED = "Render v1 can't cut talk yet — ask for a note instead"
CROSSES = "B-roll stays inside one section"
COLD_OPEN = "The cold open is a finished piece"
PATHLIKE = re.compile(r"(Users|private|Volumes|tmp|var)/|\.(sh|py)\b")

OP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["op", "target", "t", "d", "src", "lines", "caption", "why"],
    "properties": {
        "op": {"type": "string", "enum": OPS},
        "target": {"type": "string"},
        "t": {"type": "number"},
        "d": {"type": "number"},
        "src": {"type": "string"},
        "lines": {"type": "array", "items": {"type": "string"}},
        "caption": {"type": "string"},
        "why": {"type": "string"},
    },
}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reply", "answers", "ops"],
    "properties": {
        "reply": {"type": "string"},
        "answers": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["t", "label"],
            "properties": {"t": {"type": "number"}, "label": {"type": "string"}}}},
        "ops": {"type": "array", "items": OP_SCHEMA},
    },
}

DEVELOPER = """You are Muse, the editing assistant inside GhostCut Director, a Mac app that assembles talking-head videos
with B-roll, fact cards and music. The editor types a request about the current cut. Reply in one to three plain
sentences, and when the request asks for a change, propose timeline operations:
- drop_broll / drop_card: target = the item id.
- move_broll / move_card: target, new t (start, seconds on the final timeline) and d (length).
- swap_broll: target, src = a library tag. add_broll: src, t, d (target "").
- edit_card: target, lines (1-3 short uppercase lines), caption. add_card: t, d, lines, caption (target "").
- cut_span: t, d = a span of the talk track to remove (a repeat, a stumble, dead air). Start and end in pauses
  between words, never inside a word.
- add_note: t, why = a note for the review list (a reshoot item, a question for the client).
Unused fields: "" for strings, -1 for numbers, [] for lines. Every operation needs a one-sentence why.
Rules the app enforces (proposals that break them are blocked): only facts the speaker says on camera go on a
card, placed when she says them; B-roll never shows people or crew, and "this home" lines only show the home for
sale; inserts keep at least 0.05 s from their neighbours; library shots marked skip are never used.
If the request is a question, answer it with answers [{t, label}] pointing at the moments and no operations.
If the request can't be done with these operations, say so plainly and propose nothing. Never invent footage."""


def _where(p: dict, t: float) -> str:
    sec = next((s["name"] for s in p["sections"] if s["t"] <= t < s["t"] + s["d"]), "")
    talk = next((c["topic"] for c in p["talk"] if c["t"] <= t < c["t"] + c["d"]), "")
    return " › ".join(x for x in (sec, talk) if x)


def context(p: dict, at: float) -> str:
    """The cut as text: sections, every timeline item by id, the transcript and the unused shot library."""
    lib = {e["tag"]: e for e in p.get("library", [])}
    out = [f'VIDEO "{p["title"]}" · {p["duration"]:.1f} s · playhead {at:.1f} s ({_where(p, at)})', "", "SECTIONS"]
    out += [f'- {s["name"]}: {s["t"]:.1f}-{s["t"] + s["d"]:.1f}' for s in p["sections"]]
    items = [(c["t"], f'{c["id"]} TALK {c["t"]:.1f}-{c["t"] + c["d"]:.1f} {c["topic"]}') for c in p["talk"]]
    for b in p["broll"]:
        what = lib.get(b["tag"], {}).get("what", "")
        items.append((b["t"], f'{b["id"]} BROLL {b["t"]:.1f}-{b["t"] + b["d"]:.1f} {b["tag"]}: {what[:90]}'))
    for c in p["cards"]:
        items.append((c["t"], f'{c["id"]} CARD {c["t"]:.1f}-{c["t"] + c["d"]:.1f} "{" / ".join(c["lines"])}" '
                              f'(caption "{c["caption"]}")'))
    out += ["", "TIMELINE (time order; edit items by id)"] + [line for _, line in sorted(items)]
    out += ["", "TRANSCRIPT (Whisper on the finished cut; [start-end] seconds)"]
    out += [f'[{s["t"]:.1f}-{s["e"]:.1f}] {s["text"]}' for s in p.get("transcript", [])]
    out += ["", "SHOT LIBRARY (not on the timeline; tag [clean range in the shot] what; skip = never use)"]
    for e in p.get("library", []):
        if not e.get("used") and e.get("what"):
            b = e.get("best") or [0, 0]
            skip = e.get("skip") or e["tag"] in p.get("neverUse", {})
            out.append(f'{e["tag"]} [{b[0]:.1f}-{b[1]:.1f}] {e["what"][:110]}{" · SKIP" if skip else ""}')
    findings = [f for f in p.get("findings", []) if f.get("state") == "open"]
    if findings:
        out += ["", "OPEN REVIEW NOTES"] + [f'- {f["t"]:.1f} {f["title"]}: {f["body"][:160]}' for f in findings]
    return "\n".join(out)


def ask(p: dict, request: str, at: float, *, effort: str = "medium") -> tuple[dict, dict]:
    """One Spark call (text in, schema-constrained JSON out). Cached: asking the same thing twice is free."""
    from guestlist.meta import cache, spark  # the Meta API client + spend ledger with its hard budget stop

    prompt = f"{context(p, at)}\n\nREQUEST (playhead {at:.1f} s): {request.strip()}"
    model = spark.DEFAULT_MODEL
    k = cache.key("muse", model, DEVELOPER, prompt, SCHEMA, effort)
    hit = cache.get("spark", k)
    if hit:
        return hit["data"], {"usd": 0.0, "seconds": 0.0, "cached": True, "model": model}
    cache.guard(0.25)
    r = spark._structured(k, [{"type": "input_text", "text": prompt}], schema=SCHEMA, name="muse_edit",
                          developer=DEVELOPER, model=model, effort=effort, max_output_tokens=8000, record={},
                          detail={"request": request[:80]})
    return r.data, {"usd": round(r.usd, 4), "seconds": round(r.seconds, 1), "cached": False, "model": model}


# ---------- mechanical checks ----------

NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())}
NUMBER_WORDS.update({"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90})


def numbers_in(text: str) -> set[float]:
    """Numeric values in card text or speech: '3,000', '$450,000', '10-15', 'four', 'the low $300s' → 300."""
    vals = set()
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", text):
        vals.add(float(m.group().replace(",", "")))
    for w in re.findall(r"[a-z]+", text.lower()):
        if w in NUMBER_WORDS:
            vals.add(float(NUMBER_WORDS[w]))
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(thousand|k\b)", text.lower()):
        vals.add(float(m.group(1)) * 1000)
    return vals


def _words_between(p: dict, a: float, b: float) -> list[list]:
    return [w for s in p.get("transcript", []) for w in s["w"] if w[2] > a and w[1] < b]


def _said_nearby(p: dict, lines: list[str], caption: str, t: float, window: float = 20.0) -> tuple[bool, str, bool]:
    """(ok, message, hard). Numbers on a card must be spoken within ±window s; words are a softer check."""
    spoken = " ".join(w[0] for w in _words_between(p, t - window, t + window))
    text = " ".join(lines)
    need = numbers_in(text)
    if need:
        have = numbers_in(spoken)
        missing = sorted(n for n in need if n not in have)
        if missing:
            shown = ", ".join(f"{n:g}" for n in missing)
            return False, f"Not said within ±{window:g} s: {shown}", True
        return True, f"Every number is said within ±{window:g} s", True
    keys = {w for w in re.findall(r"[a-z]{4,}", text.lower())}
    heard = {w for w in re.findall(r"[a-z]{4,}", spoken.lower())}
    if not keys:
        return True, "No facts to check", False
    share = len(keys & heard) / len(keys)
    return share >= 0.5, f"{share:.0%} of the card's words are said within ±{window:g} s", False


def _inside_word(p: dict, t: float) -> list | None:
    return next((w for w in _words_between(p, t, t) if w[1] + 0.02 < t < w[2] - 0.02), None)


def tag_k(tag: str) -> int:
    """2 for the 2× prerenders (V, W and P tags), whose library 'best' is in on-screen seconds; 1 otherwise."""
    return 2 if tag[:1] in ("V", "W", "P") else 1


def preview_t(entry: dict) -> float:
    """File seconds of a library shot's preview frame: the middle of its clean range, or of the file when it has none.
    Render's in-point rule centres an inserted shot on this same frame."""
    b0, b1 = [x or 0 for x in (entry.get("best") or [0, 0])][:2]
    if b1 > b0:
        return (b0 + b1) / 2 / tag_k(entry.get("tag", ""))
    return (entry.get("dur") or entry.get("file_dur") or 0) / 2


def intro_end(p: dict) -> float:
    """End of the cold open, a finished piece Render never re-cuts (0 when the project has none)."""
    for s in p["sections"]:
        if s.get("kind") == "intro" or any(c.get("src") == "cold open" and s["t"] <= c["t"] < s["t"] + s["d"]
                                           for c in p["talk"]):
            return s["t"] + s["d"]
    return 0.0


def _section(p: dict, t: float, name: str = "") -> dict | None:
    return next((s for s in p["sections"] if s["name"] == name), None) or \
        next((s for s in p["sections"] if s["t"] <= t < s["t"] + s["d"]), None)


def _long_enough(e: dict, tag: str, length: float) -> tuple[bool, str] | None:
    """A shot plays at 0.5×, so it needs half the on-screen length in file seconds."""
    dur = e.get("dur") or e.get("file_dur")
    if not dur:
        return None
    ok = SPEED * length <= dur - 0.02
    return ok, (f"{tag} is long enough for {length:.1f} s at {SPEED:g}×" if ok else
                f"{tag} is too short for {length:.1f} s at {SPEED:g}× ({dur:.1f} s shot)")


def _gap_problems(p: dict, item_id: str, t: float, d: float) -> list[str]:
    out = []
    for b in p["broll"]:
        if b["id"] == item_id or b.get("pending") == "drop":
            continue
        gap = max(b["t"] - (t + d), t - (b["t"] + b["d"]))
        if gap < MIN_GAP:
            out.append(f'{b["id"]} ({b["tag"]})')
    return out


def check(p: dict, op: dict) -> list[dict]:
    """Checks for one operation. hard=True failures block it."""
    o, out = op["op"], []

    def add(ok: bool, text: str, hard: bool = True):
        out.append({"ok": bool(ok), "text": text, "hard": hard})

    items = {it["id"]: it for it in p["broll"] + p["cards"] + p["talk"]}
    lib = {e["tag"]: e for e in p.get("library", [])}
    if o == "cut_span":
        add(False, CUT_REFUSED)
    needs_target = o in ("drop_broll", "move_broll", "swap_broll", "drop_card", "move_card", "edit_card")
    if needs_target:
        it = items.get(op["target"])
        kind = "broll" if o.endswith("broll") else "card"
        if not it or not op["target"].startswith("b" if kind == "broll" else "c"):
            add(False, f'No {kind} item {op["target"]!r} on the timeline')
            return out
    t, d = op.get("t", -1), op.get("d", -1)
    if o in ("move_broll", "add_broll", "move_card", "add_card", "cut_span"):
        ok = t >= 0 and d >= 0.3 and t + d <= p["duration"]
        add(ok, f"{t:.2f}–{t + d:.2f} s is inside the video" if ok else f"Range {t:.2f} +{d:.2f} s is not usable")
        if not ok:
            return out
    end = intro_end(p)
    if end and o not in ("cut_span", "add_note"):
        moves_to = o in ("move_broll", "add_broll", "move_card", "add_card")
        if (needs_target and items[op["target"]]["t"] < end) or \
                (moves_to and t < end + (CARD_EDGE if o.endswith("card") else 0)):
            add(False, COLD_OPEN)
            return out
    if o in ("move_broll", "add_broll"):
        clash = _gap_problems(p, op["target"], t, d)
        add(not clash, f"Keeps ≥ {MIN_GAP} s from other inserts" if not clash else f"Touches {', '.join(clash)}")
        it = items.get(op["target"], {})
        home = _section(p, it["t"], it.get("section", "")) if o == "move_broll" else _section(p, t)
        inside = home is not None and home["t"] - 1e-6 <= t and t + d <= home["t"] + home["d"] + 1e-6
        add(inside, f'Stays inside {home["name"]}' if inside else CROSSES)
        if inside:
            ok = home["t"] + EDGE - 1e-6 <= t and t + d <= home["t"] + home["d"] - EDGE + 1e-6
            add(ok, f'Keeps ≥ {EDGE} s from the edges of {home["name"]}' if ok else
                f'Within {EDGE} s of the edge of {home["name"]}')
        if o == "move_broll" and (fit := _long_enough(lib.get(it["tag"], {}), it["tag"], d)):
            add(*fit)
        if o == "add_broll":
            ok = d >= MIN_INSERT - 1e-6
            add(ok, f"At least {MIN_INSERT:.1f} s long" if ok else f"An insert needs at least {MIN_INSERT:.1f} s")
    if o in ("swap_broll", "add_broll"):
        e = lib.get(op["src"])
        if not e:
            add(False, f'{op["src"]!r} is not in the shot library')
        else:
            never = p.get("neverUse", {}).get(op["src"])
            if never:
                add(False, f"Editor rule: {never}")
            add(not e.get("skip"), "Inventory: usable" if not e.get("skip") else f'Inventory says skip: {e.get("use", "")[:80]}')
            span = (e.get("best") or [0, 0])[1] - (e.get("best") or [0, 0])[0]
            length = d if o == "add_broll" else items[op["target"]]["d"]
            if fit := _long_enough(e, op["src"], length):
                add(*fit)
            if span > 0:  # best is in on-screen seconds for the 2× prerenders
                k = tag_k(op["src"])
                add(length <= span / k / SPEED + 0.2, f"Fits the shot's clean range ({span / k:.1f} s at 0.5× speed)", hard=False)
    if o in ("edit_card", "add_card"):
        at = t if o == "add_card" else items[op["target"]]["t"]
        ok, msg, hard = _said_nearby(p, op["lines"], op["caption"], at)
        add(ok, msg, hard)
        n = len(op["lines"])
        add(1 <= n <= 3, "1–3 lines" if 1 <= n <= 3 else "A card needs 1–3 lines")
        add(all(len(x) <= 32 for x in op["lines"]), "Short lines (≤ 32 characters)", hard=False)
        path = next((x for x in op["lines"] + [op.get("caption") or ""] if PATHLIKE.search(x)), None)
        add(path is None, "No file paths in the text" if path is None else f"Looks like a file path: {path[:60]!r}")
    if o == "move_card":
        it = items[op["target"]]
        ok, msg, hard = _said_nearby(p, it["lines"], it["caption"], t)
        add(ok, msg, hard)
    if o in ("move_card", "add_card"):
        until = t + (max(d, CARD_MIN) if o == "add_card" else d)  # Render's until rule
        ok = until <= p["duration"] - CARD_TAIL + 1e-6
        add(ok, f"Ends ≥ {CARD_TAIL:g} s before the end" if ok else
            f"A card ends at least {CARD_TAIL:g} s before the end ({p['duration'] - CARD_TAIL:.2f} s)")
    if o == "cut_span":
        for edge in (t, t + d):
            w = _inside_word(p, edge)
            add(w is None, f"{edge:.2f} s is between words" if w is None else
                f'{edge:.2f} s is inside “{w[0]}” ({w[1]:.2f}–{w[2]:.2f})')
        gone = " ".join(w[0] for w in _words_between(p, t, t + d))
        add(True, f"Removes: “{gone[:120]}”" if gone else "Removes a pause (no words)", hard=False)
    return out


def label(p: dict, op: dict) -> str:
    items = {it["id"]: it for it in p["broll"] + p["cards"] + p["talk"]}
    it = items.get(op["target"], {})
    name = it.get("tag") or " / ".join(it.get("lines", [])[:2]) or op["target"]
    o = op["op"]
    return {
        "drop_broll": f"Drop insert {name}",
        "move_broll": f"Move {name} → {op['t']:.1f} s for {op['d']:.1f} s",
        "swap_broll": f"Swap {name} → {op['src']}",
        "add_broll": f"Add {op['src']} at {op['t']:.1f} s for {op['d']:.1f} s",
        "drop_card": f"Drop card “{name}”",
        "move_card": f"Move card “{name}” → {op['t']:.1f} s",
        "edit_card": f"Card “{name}” → “{' / '.join(op['lines'])}”",
        "add_card": f"New card “{' / '.join(op['lines'])}” at {op['t']:.1f} s",
        "cut_span": f"Cut {op['d']:.2f} s of talk at {op['t']:.1f} s",
        "add_note": f"Note at {op['t']:.1f} s",
    }.get(o, o)


def validate(p: dict, ops: list[dict]) -> list[dict]:
    """Each op with its checks, a readable label and whether it is blocked. Later ops see earlier ones applied."""
    out, cur = [], p
    for op in ops:
        op = {**op, "op": op.get("op", ""), "target": op.get("target", ""), "lines": op.get("lines") or []}
        checks = check(cur, op)
        blocked = any(not c["ok"] and c["hard"] for c in checks)
        out.append({**op, "checks": checks, "blocked": blocked, "label": label(cur, op)})
        if not blocked:
            cur = apply(cur, [{"id": "_", "ops": [op]}])
    return out


# ---------- the edit layer ----------

def apply(base: dict, edits: list[dict]) -> dict:
    """The cut with every accepted edit applied as a pending change (shown on the timeline until rendered)."""
    p = copy.deepcopy(base)
    p.setdefault("cuts", [])
    p.setdefault("findings", [])
    pending = 0
    for e in edits:
        for op in e["ops"]:
            items = {it["id"]: it for it in p["broll"] + p["cards"]}
            o, it = op["op"], items.get(op.get("target", ""))
            mark = {"edit": e["id"], "why": op.get("why", "")}
            if o in ("drop_broll", "drop_card") and it:
                it.update(pending="drop", **mark)
            elif o in ("move_broll", "move_card") and it:
                it.setdefault("was", [it["t"], it["d"]])
                it.update(t=round(op["t"], 2), d=round(op["d"], 2), pending=it.get("pending") or "change", **mark)
            elif o == "swap_broll" and it:
                it.setdefault("was_tag", it["tag"])
                it.update(tag=op["src"], src=op["src"], reason=op.get("why", ""), pending="change", **mark)
            elif o == "edit_card" and it:
                it.setdefault("was_lines", it["lines"])
                it.update(lines=op["lines"], caption=op.get("caption") or it["caption"], pending="change", **mark)
            elif o == "add_broll":
                sec = next((s["name"] for s in p["sections"] if s["t"] <= op["t"] < s["t"] + s["d"]), "")
                p["broll"].append({"id": f"b{len(p['broll']) + 1}+", "tag": op["src"], "src": op["src"],
                                   "t": round(op["t"], 2), "d": round(op["d"], 2), "section": sec,
                                   "topic": "", "reason": op.get("why", ""), "pending": "add", **mark})
            elif o == "add_card":
                p["cards"].append({"id": f"c{len(p['cards']) + 1}+", "t": round(op["t"], 2), "d": round(op["d"], 2),
                                   "lines": op["lines"], "caption": op.get("caption", ""), "pending": "add", **mark})
            elif o == "cut_span":
                p["cuts"].append({"t": round(op["t"], 2), "d": round(op["d"], 2), "pending": "cut", **mark})
            elif o == "add_note":
                p["findings"].append({"t": op["t"], "src": "you", "state": "open", "title": op.get("why", "")[:70],
                                      "body": op.get("why", ""), "check": "Added through Muse", "edit": e["id"]})
            else:
                continue
            pending += 1
    p["pending"] = pending
    return p
