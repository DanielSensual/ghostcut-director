"""Build the prototype's demo project (app/ui/demo/) from a finished Director job.

    python app/make_demo.py --video final_phone.mp4 --voice voice_only.mp4 --music score.wav \
        --facts facts.json --transcript whisper.json --inventory inventory.json --title "My tour" \
        --section "Intro=:0:12.813" --section "Community=<work dir>:12.813:199.9" ... \
        [--takes NAME=<work dir>] [--extra findings_and_chat.json]

Each --section is NAME=WORKDIR:START:LENGTH (WORKDIR may be empty). A work dir contributes its newest
`*_broll.fcpxml` (talk clips + B-roll), `broll_map.json` (tags → files), `broll_plan.json`
(shot descriptions + why each insert was placed) and, with --takes, its take log. --transcript is Whisper JSON
(word timestamps) of the finished cut, so every time in the project is on the final timeline.
Everything written lands in app/ui/demo/ (or --out), which is gitignored: demo footage and names stay local.

A Render version builds its project from its own timeline.json instead (exact times, every item with a ref):

    python app/make_demo.py --timeline rNNN/timeline.json --carry app/ui/demo/project.json --out <dir> \
        [--video phone.mp4 --voice voice.wav --music music_bed.wav --transcript whisper.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import numpy as np

OUT = Path(__file__).parent / "ui" / "demo"
CARRY = ("title", "client", "shootDate", "spend", "media", "chat", "suggestions", "neverUse", "importFlag", "startAt",
         "takes", "takeMeta")


def secs(v: str | None) -> float:
    if not v:
        return 0.0
    return float(Fraction(v.rstrip("s")))


def fcpxml_clips(path: Path) -> tuple[list[dict], list[dict], float]:
    """Talk clips on the spine and B-roll connected above them, in the file's own timeline seconds."""
    root = ET.parse(path).getroot()
    spine = root.find(".//spine")
    talk, broll, end = [], [], 0.0
    for el in spine:
        if el.tag != "asset-clip":
            continue
        off, start, dur = secs(el.get("offset")), secs(el.get("start")), secs(el.get("duration"))
        topic, _, src = el.get("name", "").rpartition(" | ")
        talk.append({"t": off, "d": dur, "src": src, "in": start, "topic": topic})
        end = max(end, off + dur)
        for ch in el.findall("asset-clip"):
            if ch.get("lane") == "1":
                name = ch.get("name", "").split(" | ")
                broll.append({"t": off + secs(ch.get("offset")) - start, "d": secs(ch.get("duration")),
                              "src": name[-1], "in": secs(ch.get("start")), "topic": name[1] if len(name) > 2 else ""})
    return talk, broll, end


def envelope(media: str, step: float = 0.5) -> list[float]:
    """RMS per step seconds, mapped from -60..0 dBFS to 0..1."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", media, "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
    n = int(8000 * step)
    a = a[: len(a) // n * n].reshape(-1, n)
    db = 20 * np.log10(np.sqrt((a ** 2).mean(1)) + 1e-9)
    return [round(float(x), 2) for x in np.clip((db + 60) / 60, 0, 1)]


def srt_lines(path: Path) -> dict[int, str]:
    out = {}
    for block in path.read_text().strip().split("\n\n"):
        rows = block.splitlines()
        if len(rows) >= 3 and rows[0].strip().isdigit():
            out[int(rows[0])] = " ".join(rows[2:])
    return out


def takes(work: Path, section: str) -> tuple[list[dict], dict]:
    log = json.loads((work / "take_log.json").read_text())
    clip_map = json.loads((work / "clip_map.json").read_text())
    lines = srt_lines(work / "all_takes.srt")
    rows = []
    for s in log.get("segments", []):
        clip = next((c for c in clip_map if c["offset"] <= s["t_start"] < c["offset"] + c["duration"]), None)
        text = " ".join(lines.get(i, "") for i in range(s["first_line"], s["last_line"] + 1))
        rows.append({"section": section, "kind": s["kind"], "topic": s.get("topic", ""), "group": s.get("group", ""),
                     "pick": s.get("pick_in_group", False), "reason": s.get("reason", ""),
                     "conf": s.get("confidence"), "clip": clip["clip"].rsplit(".", 1)[0][-4:] if clip else "",
                     "in": round(s["t_start"] - clip["offset"], 2) if clip else s["t_start"],
                     "d": round(s["t_end"] - s["t_start"], 2), "text": text[:400]})
    return rows, {k: log.get(k) for k in ("model", "effort", "usd", "seconds")}


def transcript(path: Path) -> list[dict]:
    """Whisper JSON → [{t, e, text, w: [[word, start, end], ...]}] on the final timeline."""
    out = []
    for s in json.loads(path.read_text())["segments"]:
        words = [[w["word"].strip(), round(w["start"], 2), round(w["end"], 2)] for w in s.get("words", [])]
        out.append({"t": round(s["start"], 2), "e": round(s["end"], 2), "text": s["text"].strip(), "w": words})
    return out


def shot_library(works: list[Path], inventory: Path | None) -> tuple[dict, dict]:
    """tag → {tag, what, room, q, skip, file, best}; and clip filename → tag."""
    lib, tag_of = {}, {}
    for work in works:
        bm = work / "broll_map.json"
        for e in json.loads(bm.read_text()) if bm.exists() else []:
            tag_of[e["clip"]] = e["tag"]
            lib.setdefault(e["tag"], {"tag": e["tag"], "file": e["source"], "dur": e.get("duration")})
        plan = work / "broll_plan.json"
        for s in json.loads(plan.read_text()).get("raw", {}).get("shots", []) if plan.exists() else []:
            if s["tag"] in lib:
                lib[s["tag"]].update(what=s.get("description", ""), room=s.get("room", ""), q=s.get("quality"),
                                     best=[s.get("best_start", 0), s.get("best_end", 0)])
    if inventory and inventory.exists():
        for s in json.loads(inventory.read_text()).get("data", {}).get("catalog", []):
            if s["tag"] in lib:
                use = s.get("use_for", "")
                lib[s["tag"]].update(what=s.get("what", ""), use=use, q=s.get("quality"),
                                     skip="skip" in use.lower(),
                                     best=[s.get("best_start", 0), s.get("best_end", 0)])
    return lib, tag_of


def placement_reasons(work: Path) -> dict[str, list[str]]:
    plan = work / "broll_plan.json"
    out: dict[str, list[str]] = {}
    for p in json.loads(plan.read_text()).get("placed", []) if plan.exists() else []:
        out.setdefault(p["tag"], []).append(p.get("reason", ""))
    return out


def link_video(video: str | Path, out: Path) -> None:
    link = out / "video.mp4"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(os.path.abspath(video))


def stills(video: str | Path, out: Path, poster_at: float) -> None:
    (out / "thumbs").mkdir(parents=True, exist_ok=True)
    # Poster: shown before the video loads (pick a title frame with --poster-at).
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{poster_at:.2f}", "-i", str(video), "-frames:v", "1", "-q:v", "3",
                    str(out / "poster.jpg")], check=True)
    # One thumbnail every 2 s; clips pick the frame nearest their midpoint.
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf", "fps=1/2,scale=192:108",
                    "-q:v", "5", str(out / "thumbs" / "%04d.jpg")], check=True)


# ---------- Render timelines (schema ghostcut.timeline/1) ----------

def _num(x: float) -> str:
    return f"{x:.1f}".replace("-", "−")


def _said(words: list[dict], a: float, b: float) -> str:
    said = " ".join(w[0] for s in words for w in s.get("w", []) if a - 0.05 <= w[1] < b)
    return f" (“{said[:100]}”)" if said else ""


def _topic_at(pieces: list[dict], r: float) -> str:
    return next((p.get("topic", "") for p in reversed(pieces) if p["A"] <= r), "")


def _verify(tl: dict, video: Path) -> dict:
    v = tl.get("verify")
    if isinstance(v, dict):
        return v
    f = Path(video).parent / v if isinstance(v, str) else None
    return json.loads(f.read_text()) if f and f.is_file() else {}


def _sync_after_ms(tl: dict, verify: dict) -> float | None:
    """Worst measured voice/picture lag after the render: tl.sync.after_ms, else the passing *sync* checks (ms, as
    render_verify reports them with a text threshold; a numeric threshold below 1 means seconds)."""
    if (tl.get("sync") or {}).get("after_ms") is not None:
        return abs(tl["sync"]["after_ms"])
    secs = lambda c: isinstance(c.get("threshold"), (int, float)) and c["threshold"] < 1  # noqa: E731
    vals = [abs(c["value"]) * (1000 if secs(c) else 1) for c in verify.get("checks", [])
            if "sync" in str(c.get("name", "")).lower() and c.get("status") == "pass"
            and isinstance(c.get("value"), (int, float)) and not isinstance(c.get("value"), bool)]
    return max(vals) if vals else None


def _note(n: dict | str) -> dict:
    """A plan note (an add_note from Muse, or a render warning) as an open Review finding."""
    if isinstance(n, str):
        return {"t": 0, "src": "measured", "state": "open", "title": n[:70], "body": n, "check": "Render plan"}
    text = n.get("body") or n.get("why") or n.get("text") or n.get("title") or ""
    src = n.get("src") or ("you" if n.get("op", "add_note") == "add_note" else "measured")
    t = n.get("t")
    return {**n, "t": t if isinstance(t, (int, float)) and t >= 0 else 0, "src": src, "state": n.get("state", "open"),
            "title": n.get("title") or text[:70], "body": text,
            "check": n.get("check") or ("Added through Muse" if src == "you" else "Render plan")}


def timeline_findings(tl: dict, carry: dict, words: list[dict], loudness: dict, verify: dict) -> list[dict]:
    """The carried findings plus the plan's notes, the voice track's notes, the measured loudness and the render checks'
    warnings. Carried times move from the carry's own section starts (the shown project's; a carry without sections
    is taken as already rebased) to the timeline's. A generated note replaces any carried note on the same subject
    (same key or title, an earlier voice re-sync note, the old loudness line), so renders never pile them up."""
    secs, version = tl["sections"], tl["version"]
    render: list[dict] = []
    if "voice_sync" in tl.get("fixes", []):
        iso = [(sec, x) for sec in secs for x in sec.get("isolated") or []]
        parts = []
        for sec in secs:
            if sec.get("isolated"):
                lags, rs = [x["lag_ms"] for x in sec["isolated"]], [x["r"] for x in sec["isolated"]]
                parts.append(f'{sec["name"]} {min(lags):+.0f} to {max(lags):+.0f} ms (r {min(rs):.2f}–{max(rs):.2f})')
        before = (tl.get("sync") or {}).get("before_ms") or max((abs(x["lag_ms"]) for _, x in iso), default=None)
        after = _sync_after_ms(tl, verify)
        title = "Voice re-synced to picture"
        if before is not None and after is not None:
            title = f"Voice re-synced: up to {before:.0f} ms off before, within {after:.0f} ms now (measured)"
        body = "The voice is decoded by packet timestamp, so every piece sits on its own picture"
        body += f"; the isolated voice was re-placed piece by piece at its measured lag: {', '.join(parts)}." if parts else "."
        first = next((sec for sec, _ in iso), next((s for s in secs if s.get("kind") != "intro"), secs[0]))
        render.append({"t": round(first["t"], 2), "src": "measured", "state": "fixed", "title": title, "body": body,
                       "check": "Envelope cross-correlation, piece by piece", "key": "render:voice_sync"})
    for s, sec in enumerate(secs):
        pieces, S = sec.get("pieces") or [], sec["t"]
        camera: dict[int, str] = {}
        span = (sec.get("isolate") or {}).get("pieces")
        if span and span[1] + 1 < len(pieces):
            camera[span[1] + 1] = (f"The isolated voice ends after piece {span[1]}, so from here on this section plays "
                                   f"camera audio, with more room sound than the isolated pieces before it")
        for x in sec.get("camera_fallback") or []:
            i = x if isinstance(x, int) else x.get("piece")
            why = x.get("reason") if isinstance(x, dict) else None
            if isinstance(i, int):
                camera[i] = (f"Its isolated voice didn't line up with the picture ({why or 'no lag with r ≥ 0.6'}), "
                             f"so it fell back to camera audio")
        for i, why in sorted(camera.items()):
            if i >= len(pieces):
                continue
            t = S + pieces[i]["A"]
            render.append({"t": round(t, 2), "src": "measured", "state": "open", "title": f'{sec["name"]} piece {i} uses camera audio',
                           "body": f"{why}{_said(words, t, t + pieces[i]['d'])}. Isolating it needs one more paid isolation call.",
                           "check": "Voice track report", "key": f"render:camera:{s}:{i}"})
    if "lufs" in loudness:
        lufs, tp = loudness["lufs"], loudness["tp"]
        ok = -15.0 <= lufs <= -13.0 and tp <= -0.5
        render.append({"t": 0, "src": "measured", "state": "ok" if ok else "open",
                       "title": f"Loudness {_num(lufs)} LUFS · {_num(tp)} dBTP",
                       "body": f"Measured on the {version} master: {_num(lufs)} LUFS integrated, {_num(tp)} dBTP true peak "
                               f"(target −14 LUFS ±1, true peak ≤ −0.5 dBTP).", "check": "ffmpeg ebur128", "key": "render:loudness"})
    for c in verify.get("checks", []):
        if c.get("status") == "warn":
            name = str(c.get("name", "check"))
            detail = "; ".join(str(d) for d in (c.get("detail") or [])[:3])
            render.append({"t": c.get("t", 0), "src": "measured", "state": "open", "title": f"Check: {name}"[:70],
                           "body": f"Measured {str(c.get('value'))[:120]} against {str(c.get('threshold'))[:80]}."
                                   + (f" {detail[:400]}" if detail else ""),
                           "check": f"{version} render checks", "key": f"verify:{name}"})

    shown = carry.get("sections") or []

    def rebase(t):
        if len(shown) != len(secs) or not isinstance(t, (int, float)):
            return t
        s = max((i for i, x in enumerate(shown) if x["t"] <= t + 1e-6), default=0)
        return round(t - shown[s]["t"] + secs[s]["t"], 3)

    def replaced(f: dict) -> bool:
        title = str(f.get("title", ""))
        return f.get("key") in keys or title in titles or str(f.get("key", "")).startswith("verify:") or \
            ("render:voice_sync" in keys and title.startswith("Voice re-synced")) or \
            ("render:loudness" in keys and f.get("src") == "measured" and title.startswith("Loudness "))

    def recheck(f: dict) -> dict:
        """A finding that names a shot no longer on screen at its time (the insert there was swapped) opens again."""
        t, said = f.get("t"), set(re.findall(r"\b[A-Z]{1,2}\d{2,5}\b", f"{f.get('title', '')} {f.get('body', '')}"))
        if not isinstance(t, (int, float)) or not said:
            return f
        now = {b["tag"] for sec in secs for b in sec.get("broll") or []
               if sec["t"] + b["r"] - 0.5 <= t <= sec["t"] + b["r"] + b["d"] + 0.5}
        if not now or now & said:
            return f
        return {**f, "state": "open", "body": f"{f.get('body', '')} Since {version} this spot shows "
                                              f"{', '.join(sorted(now))} instead: check it again."}

    keys, titles = {f["key"] for f in render}, {f["title"] for f in render}
    kept = [recheck({**f, "t": rebase(f.get("t"))}) for f in carry.get("findings", []) if not replaced(f)]
    return kept + [_note(n) for n in tl.get("notes", [])] + [{**f, "render": version} for f in render]


def project_from_timeline(tl: dict, carry: dict, transcript_json: Path | None, video: Path, voice: Path | None,
                          music: Path | None, out: Path, skip_media: bool = False) -> dict:
    """The app project for a Render timeline: talk at S + A_i, B-roll at S + r for its on-screen length with its
    in-point in file seconds, cards with their line times, and a ref on every item back into the timeline (talk and
    B-roll [section, index], cards their index). Writes project.json/.js, video.mp4 → video, poster, thumbs and the
    transcript into out."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    link_video(video, out)
    if not skip_media:
        stills(video, out, float(carry.get("startAt") or 0.0))

    sections, talk, broll = [], [], []
    for s, sec in enumerate(tl["sections"]):
        S, name, pieces = sec["t"], sec["name"], sec.get("pieces") or []
        sections.append({"name": name, "kind": sec.get("kind", "talk"), "t": round(S, 6), "d": round(sec["d"], 6)})
        for i, pc in enumerate(pieces):
            talk.append({"t": round(S + pc["A"], 3), "d": round(pc["d"], 3), "src": Path(pc["source"]).name,
                         "in": round(pc["src_in"], 3), "topic": pc.get("topic", ""), "section": name,
                         "zoom": pc.get("zoom", 1.0), "ref": [s, i]})
        if not pieces:
            talk.append({"t": round(S, 3), "d": round(sec["d"], 3), "src": "cold open" if sec.get("kind") == "intro" else name,
                         "in": 0, "topic": name, "section": name, "ref": [s, 0]})
        for i, b in enumerate(sec.get("broll") or []):
            broll.append({"t": round(S + b["r"], 3), "d": round(b["d"], 3), "src": Path(b["source"]).name,
                          "in": round(b["in"], 3), "topic": b.get("topic") or _topic_at(pieces, b["r"]), "tag": b["tag"],
                          "section": name, "reason": b.get("reason", ""), "key": b.get("key", ""), "ref": [s, i]})
    for prefix, items in (("t", talk), ("b", broll)):
        for i, it in enumerate(items, 1):
            it["id"] = f"{prefix}{i}"
    cards = []
    for i, c in enumerate(tl.get("cards", [])):
        first = min(at for _, at in c["lines"])
        cards.append({"id": f"c{i + 1}", "t": round(first, 3), "d": round(c["until"] - first, 3),
                      "caption": c.get("caption", ""), "lines": [txt for txt, _ in c["lines"]],
                      "at": [round(at, 3) for _, at in c["lines"]], "ref": i, "key": c.get("key", "")})
    used = {b["tag"] for b in broll}

    if transcript_json:
        words = transcript(Path(transcript_json))
        dst = out / "transcript" / "voice.json"
        dst.parent.mkdir(exist_ok=True)
        if Path(transcript_json).resolve() != dst.resolve():
            shutil.copyfile(transcript_json, dst)
    else:
        words = carry.get("transcript", [])
    mix = tl.get("mix") or {}
    loudness = {"lufs": round(mix["lufs"], 1), "tp": round(mix["tp"], 1)} if mix.get("lufs") is not None and mix.get("tp") is not None else {}
    if loudness and mix.get("music_gain_db") is not None:
        loudness["music_gain_db"] = round(mix["music_gain_db"], 2)

    project = {"title": "Untitled project", "version": tl["version"], "duration": round(tl["duration"], 6),
               "sections": sections, "talk": talk, "broll": broll, "cards": cards, "takes": [], "takeMeta": {},
               "library": [{**e, "used": e["tag"] in used} for e in carry.get("library", [])],
               "transcript": words, "env": {"mix": envelope(str(video))}, "envStep": 0.5}
    if voice:
        project["env"]["voice"] = envelope(str(voice))
    if music:
        project["env"]["music"] = envelope(str(music))
    project.update({k: carry[k] for k in CARRY if k in carry})
    if loudness:
        project["loudness"] = loudness
    project["findings"] = timeline_findings(tl, carry, words, loudness, _verify(tl, video))
    (out / "project.json").write_text(json.dumps(project))
    (out / "project.js").write_text("window.PROJECT = " + json.dumps(project) + ";\n")
    return project


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", help="the cut the app plays (default with --timeline: its phone copy)")
    ap.add_argument("--voice")
    ap.add_argument("--music")
    ap.add_argument("--facts")
    ap.add_argument("--transcript")
    ap.add_argument("--inventory")
    ap.add_argument("--title", default="Untitled project")
    ap.add_argument("--section", action="append", default=[])
    ap.add_argument("--takes", action="append", default=[], help="NAME=WORKDIR for take logs")
    ap.add_argument("--extra", help="JSON merged into the project (findings, chat, media bins)")
    ap.add_argument("--skip-media", action="store_true", help="reuse the thumbnails and poster already in demo/")
    ap.add_argument("--poster-at", type=float, default=0.0, help="seconds into the video for the poster frame")
    ap.add_argument("--timeline", help="a Render timeline.json: build the project from it instead of FCPXML")
    ap.add_argument("--carry", help="with --timeline: the project.json whose client, library, findings, chat… carry over")
    ap.add_argument("--out", default=str(OUT), help="where the project is written (default app/ui/demo)")
    args = ap.parse_args()
    out = Path(args.out)

    if args.timeline:
        tl_file = Path(args.timeline)
        tl = json.loads(tl_file.read_text())
        files = tl.get("files", {})

        def media(arg: str | None, key: str) -> Path | None:
            f = Path(arg) if arg else tl_file.parent / files[key] if key in files else None
            return f if f and (arg or f.exists()) else None

        video = media(args.video, "phone")
        if not video:
            ap.error("--video is required (the timeline names no phone copy)")
        p = project_from_timeline(tl, json.loads(Path(args.carry).read_text()) if args.carry else {},
                                  media(args.transcript, "transcript"), video, media(args.voice, "voice"),
                                  media(args.music, "music"), out, skip_media=args.skip_media)
        print(json.dumps({"version": p["version"], "sections": len(p["sections"]), "talk": len(p["talk"]),
                          "broll": len(p["broll"]), "cards": len(p["cards"]), "library": len(p["library"]),
                          "words": sum(len(s["w"]) for s in p["transcript"]), "findings": len(p["findings"]),
                          "duration": round(p["duration"], 1)}))
        return
    if not args.video:
        ap.error("--video is required")

    (out / "thumbs").mkdir(parents=True, exist_ok=True)
    link_video(args.video, out)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                args.video], capture_output=True, text=True).stdout)
    if not args.skip_media:
        stills(args.video, out, args.poster_at)

    specs = []
    for spec in args.section:
        name, _, rest = spec.partition("=")
        work, start, length = rest.rsplit(":", 2)
        specs.append((name, Path(work) if work else None, float(start), float(length)))
    lib, tag_of = shot_library([w for _, w, _, _ in specs if w], Path(args.inventory) if args.inventory else None)

    sections, talk, broll = [], [], []
    for name, work, start, length in specs:
        sections.append({"name": name, "t": start, "d": length})
        fx = max(work.glob("*_broll.fcpxml"), key=lambda f: f.stat().st_mtime, default=None) if work else None
        if work and not fx:
            raise SystemExit(f"{work}: no *_broll.fcpxml (run the broll step first)")
        if fx:
            t, b, end = fcpxml_clips(fx)
            k = length / end if end else 1.0  # the B-roll pass retimes slightly; fit the section
            reasons = placement_reasons(work)
            for c in t:
                talk.append({**c, "t": round(start + c["t"] * k, 2), "d": round(c["d"] * k, 2), "section": name})
            for c in b:
                tag = tag_of.get(c["src"], Path(c["src"]).stem)
                why = reasons.get(tag, [])
                broll.append({**c, "tag": tag, "t": round(start + c["t"] * k, 2), "d": round(c["d"] * k, 2),
                              "section": name, "reason": why.pop(0) if why else ""})
        else:
            talk.append({"t": start, "d": length, "src": "cold open", "in": 0, "topic": name, "section": name})
    for prefix, items in (("t", talk), ("b", broll)):
        for i, it in enumerate(items, 1):
            it["id"] = f"{prefix}{i}"
    used = {b["tag"] for b in broll}
    for tag, e in lib.items():
        e["used"] = tag in used

    take_rows, take_meta = [], {}
    for spec in args.takes:
        name, _, work = spec.partition("=")
        rows, take_meta = takes(Path(work), name)
        take_rows += rows

    cards = []
    if args.facts:
        for i, c in enumerate(json.loads(Path(args.facts).read_text())["cards"], 1):
            first = min(at for _, at in c["lines"])
            cards.append({"id": f"c{i}", "t": first, "d": round(c["until"] - first, 2),
                          "caption": c.get("caption", ""), "lines": [txt for txt, _ in c["lines"]]})

    project = {"title": args.title, "duration": round(dur, 3), "sections": sections, "talk": talk,
               "broll": broll, "cards": cards, "takes": take_rows, "takeMeta": take_meta,
               "library": sorted(lib.values(), key=lambda e: e["tag"]),
               "transcript": transcript(Path(args.transcript)) if args.transcript else [],
               "env": {"mix": envelope(args.video)}, "envStep": 0.5}
    if args.voice:
        project["env"]["voice"] = envelope(args.voice)
    if args.music:
        project["env"]["music"] = envelope(args.music)
    if args.extra:
        project.update(json.loads(Path(args.extra).read_text()))
    (out / "project.json").write_text(json.dumps(project))
    (out / "project.js").write_text("window.PROJECT = " + json.dumps(project) + ";\n")
    print(json.dumps({"sections": len(sections), "talk": len(talk), "broll": len(broll), "cards": len(cards),
                      "takes": len(take_rows), "library": len(lib), "words": sum(len(s["w"]) for s in project["transcript"]),
                      "duration": round(dur, 1)}))


if __name__ == "__main__":
    main()
