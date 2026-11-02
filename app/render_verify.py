"""Render v1 re-check: measures a finished rNNN against its plan, the timestamp-decoded references and the previous
transcript before anything is promoted.

Every check is {name, value, threshold, status: pass|warn|fail} (plus `detail` when there is something to list);
verify() writes rNNN/verify.json and rNNN/qa/cards.jpg, and any fail blocks promotion. Each check is a small
function over files and dicts, so it can be tested on synthetic media. Reads only, apart from those two outputs.
"""
from __future__ import annotations

import difflib
import json
import math
import os
import re
import statistics
import subprocess
import time
from collections import Counter
from fractions import Fraction
from pathlib import Path

import numpy as np

SR = 48000
RATE = (60000, 1001)
D0 = 0.021  # video start of joined.mp4: piece i's voice sits at S + A_i + D0
GUARD = re.compile(r"(Users|private|Volumes|tmp|var)/|\.(sh|py)\b")
MAX_LINE_W = 1784
FILES = {"master": "master.mp4", "phone": "phone.mp4", "picture": "picture.mp4", "voice": "voice.wav",
         "music": "music_bed.wav", "sfx": "picture.sfx.wav", "transcript": "transcript/voice.json"}
TS = "aresample={sr}:async=1:min_hard_comp=0.001:first_pts=0"  # decode by packet timestamp, as the render does

R_CAMERA, R_ISO, R_MIX = 0.8, 0.6, 0.5
LAG_PIECE, LAG_MASTER, LAG_PHONE, LAG_CAMERA, LAG_SUB = 5, 3, 5, 15, 4
STEP = (-15, 60)
CUT_MASTER, CUT_PHONE = (-0.005, 0.035), (0.0, 0.034)
LUFS, TP_MASTER, TP_PHONE, PHONE_MB = (-15.0, -13.0), -0.5, 0.0, 35
WORD_RATIO, WORD_RUN, WORD_EDGE = 0.95, 4, 0.02
CARD_IN, CARD_OUT = 6.0, 2.5


def _c(name: str, value, threshold, ok: bool, level: str = "fail", detail: list | None = None) -> dict:
    c = {"name": name, "value": value, "threshold": threshold, "status": "pass" if ok else level}
    if detail:
        c["detail"] = detail
    return c


def _need(path: Path | str | None, what: str = "") -> Path:
    if not path or not Path(path).exists():
        raise FileNotFoundError(f"{what or Path(str(path)).name} missing")
    return Path(path)


# ---------- media helpers ----------

_PROBES: dict[tuple, dict] = {}


def probe(path: Path) -> dict:
    st = os.stat(path)
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _PROBES:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "format=start_time,duration,size:stream=codec_type,codec_name,pix_fmt,width,height,"
                              "r_frame_rate,start_time,duration,nb_frames,sample_rate,channels,duration_ts",
                              "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
        _PROBES[key] = json.loads(out)
    return _PROBES[key]


def _stream(path: Path, kind: str) -> dict | None:
    return next((s for s in probe(path).get("streams", []) if s.get("codec_type") == kind), None)


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def video_frames(path: Path) -> int:
    v = _stream(path, "video") or {}
    if str(v.get("nb_frames", "N/A")).isdigit():
        return int(v["nb_frames"])
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                          "stream=nb_read_packets", "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    return int(out.strip() or 0)


def pcm(path: Path, ss: float = 0.0, t: float | None = None, sr: int = SR) -> np.ndarray:
    """Mono float64 from `ss` for `t` seconds, placed by packet timestamp; time before 0 is silence."""
    pad = max(0.0, -ss)
    if t is not None and t <= pad:
        return np.zeros(round(t * sr))
    cmd = ["ffmpeg", "-v", "error", "-nostdin"] + (["-ss", f"{ss + pad:.6f}"] if ss + pad > 0 else [])
    cmd += (["-t", f"{t - pad + 0.05:.6f}"] if t is not None else []) + ["-i", str(path), "-vn", "-af", TS.format(sr=sr),
                                                                         "-ac", "1", "-f", "f32le", "-"]
    x = np.frombuffer(subprocess.run(cmd, capture_output=True, check=True).stdout, np.float32).astype(np.float64)
    x = np.concatenate([np.zeros(round(pad * sr)), x])
    if t is not None:
        n = round(t * sr)
        x = np.pad(x[:n], (0, max(0, n - len(x))))
    return x


def envelope(x: np.ndarray, sr: int = SR) -> np.ndarray:
    """1 ms log RMS of the 300–3400 Hz speech band."""
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / sr)
    spec[(f < 300) | (f > 3400)] = 0
    y = np.fft.irfft(spec, len(x))
    hop = sr // 1000
    n = len(y) // hop * hop
    return np.log(np.sqrt((y[:n].reshape(-1, hop) ** 2).mean(1)) + 1e-6)


def _xcorr(a: np.ndarray, b: np.ndarray, search: int) -> tuple[int, float]:
    """a: test envelope; b: its reference envelope with `search` ms extra on both sides. (lag ms, Pearson r);
    lag > 0 means the test is late against the reference."""
    a = (a - a.mean()) / (a.std() + 1e-9)
    n, best = len(a), (search, -2.0)
    for k in range(max(-1, min(2 * search, len(b) - n)) + 1):
        s = b[k:k + n]
        r = float(a @ ((s - s.mean()) / (s.std() + 1e-9))) / n
        if r > best[1]:
            best = (k, r)
    return search - best[0], best[1]


def lag_ms(test: np.ndarray, ref: np.ndarray, search: int, sr: int = SR) -> tuple[int, float]:
    return _xcorr(envelope(test, sr), envelope(ref, sr), search)


def measure(test: Path, t_test: float, ref: Path, t_ref: float, w: float, search: int = 50,
            sr: int = SR) -> tuple[int, float]:
    """Lag of test[t_test, +w) against ref[t_ref, +w), both decoded by timestamp."""
    a = pcm(test, t_test, w, sr)
    b = pcm(ref, t_ref - search / 1000, w + 2 * search / 1000, sr)
    return lag_ms(a, b, search, sr)


def frames(path: Path, k0: int, n: int, fps: float, size: tuple[int, int] | None = None,
           pix: str = "rgb24") -> np.ndarray:
    """Frames k0 … k0+n−1 (exact: the seek lands half a frame before k0)."""
    v = _stream(path, "video") or {}
    off = _f(v.get("start_time")) - _f(probe(path).get("format", {}).get("start_time"))
    w, h = size or (int(v["width"]), int(v["height"]))
    ss = (k0 - 0.5) / fps + off
    ch = 1 if pix == "gray" else 3
    cmd = ["ffmpeg", "-v", "error", "-nostdin"] + (["-ss", f"{ss:.6f}"] if ss > 0 else [])
    cmd += ["-i", str(path), "-an", "-fps_mode", "passthrough", "-frames:v", str(n)]
    cmd += ["-vf", f"scale={w}:{h}"] if size else []
    raw = subprocess.run(cmd + ["-pix_fmt", pix, "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    size_b = w * h * ch
    x = np.frombuffer(raw[: len(raw) // size_b * size_b], np.uint8)
    return x.reshape(-1, h, w) if ch == 1 else x.reshape(-1, h, w, ch)


def loudness(path: Path) -> tuple[float, float]:
    """(integrated LUFS, true peak dBTP) from ebur128."""
    err = subprocess.run(["ffmpeg", "-nostats", "-nostdin", "-i", str(path), "-vn", "-af", "ebur128=peak=true", "-f",
                          "null", "-"], capture_output=True, text=True).stderr
    i = re.findall(r"^\s*I:\s*(-?(?:inf|[\d.]+))\s*LUFS", err, re.M)
    p = re.findall(r"^\s*Peak:\s*(-?(?:inf|[\d.]+))\s*dBFS", err, re.M)
    if not i or not p:
        raise RuntimeError(f"no ebur128 summary for {Path(path).name}")
    return float(i[-1]), float(p[-1])


def diffs(path: Path, k0: int, n: int, fps: float) -> np.ndarray:
    """Mean absolute luma change into each frame k0+1 … k0+n−1 (0–255, at 192x108)."""
    x = frames(path, k0, n, fps, (192, 108), "gray").astype(np.float32)
    return np.abs(np.diff(x, axis=0)).mean((1, 2))


def _spikes(d: np.ndarray, floor: float, over: float) -> list[int]:
    """Frame steps that stand out from the shot's own motion: ≥ floor and ≥ over × the median step."""
    thr = max(floor, over * float(np.median(d))) if len(d) else floor
    return [k for k in range(len(d)) if d[k] >= thr]


def cut_near(path: Path, t: float, fps: float, win: float = 0.25, floor: float = 6.0) -> float | None:
    """Presentation time of the hard cut nearest t within ±win (the first frame of the new shot), or None."""
    vs = _f((_stream(path, "video") or {}).get("start_time"))
    k0 = max(0, math.floor((t - vs - win) * fps))
    d = diffs(path, k0, round(2 * win * fps) + 2, fps)
    cand = _spikes(d, floor, 4.0)
    if not cand:
        return None
    top = max(d[k] for k in cand)
    cand = [k for k in cand if d[k] >= 0.5 * top]  # a fast shot's own motion steps (~14) are not its cut (~75)
    return vs + (k0 + min(cand, key=lambda k: abs(vs + (k0 + k + 1) / fps - t)) + 1) / fps


def intro_cuts(path: Path, fps: float, n: int = 2, floor: float = 20.0) -> list[float]:
    """The first n hard cuts of a finished cold open (the reference for the master's cut timing)."""
    vs = _f((_stream(path, "video") or {}).get("start_time"))
    return [vs + (k + 1) / fps for k in _spikes(diffs(path, 0, video_frames(path), fps), floor, 8.0)][:n]


# ---------- timeline helpers ----------

def _fps(tl: dict) -> float:
    a, b = tl.get("rate", RATE)
    return a / b


def _get(tl: dict, job: dict | None, i: int, *keys: str):
    js = (job or {}).get("sections") or []
    for src in (tl["sections"][i], js[i] if i < len(js) else {}):
        for k in keys:
            if src.get(k) is not None:
                return src[k]
    return None


def _isolated(sec: dict) -> dict[int, dict]:
    return {int(e["piece"]): e for e in sec.get("isolated") or [] if e.get("isolated", True)}


def _pick(sec: dict, min_d: float = 4.0) -> list[int]:
    """First, middle and last piece of at least min_d seconds."""
    long = [k for k, p in enumerate(sec.get("pieces") or []) if p["d"] >= min_d]
    return sorted({long[0], long[len(long) // 2], long[-1]}) if long else []


def _talk(tl: dict) -> list[int]:
    return [i for i, s in enumerate(tl["sections"]) if s.get("kind") == "talk"]


def _section_at(tl: dict, t: float) -> str:
    return next((s["name"] for s in tl["sections"] if s["t"] <= t < s["t"] + s["d"]), tl["sections"][-1]["name"])


def switch_points(tl: dict) -> list[float]:
    """Section joins plus every isolated/camera switch inside a section (program seconds)."""
    pts = [s["t"] for s in tl["sections"][1:]]
    for s in tl["sections"]:
        iso, pieces = _isolated(s), s.get("pieces") or []
        if not iso:
            continue
        on = [k in iso for k in range(len(pieces))]
        for k in range(len(pieces) + 1):
            if (on[k - 1] if k else False) != (on[k] if k < len(pieces) else False):
                a = pieces[k]["A"] if k < len(pieces) else pieces[-1]["A"] + pieces[-1]["d"]
                pts.append(s["t"] + a + s.get("audio_offset", D0))
    return sorted({round(p, 4) for p in pts})


def expected_cuts(tl: dict, plan: dict) -> list[tuple[str, float]]:
    """Program times where each changed overlay starts and ends (S + r, S + r + d)."""
    out = []
    for ch in plan.get("changes") or []:
        if ch.get("op") not in ("swap_broll", "move_broll", "add_broll"):
            continue
        sec = tl["sections"][ch["section"]]
        b = next((b for b in sec.get("broll") or [] if b.get("key") == ch.get("key")), None)
        if b:
            out += [(f'{b["tag"]} in', sec["t"] + b["r"]), (f'{b["tag"]} out', sec["t"] + b["r"] + b["d"])]
    return out


# ---------- checks: plan and timeline ----------

def check_plan(plan: dict, edits: list | None = None) -> list[dict]:
    """The plan still validates, nothing was refused, and every accepted op produced exactly one change."""
    import render_plan

    problems = list(render_plan.validate(plan))
    refused = [f'{r.get("label") or r.get("op")}: {r.get("reason")}' if isinstance(r, dict) else str(r)
               for r in plan.get("refusals") or []]
    want = Counter((e["id"], o["op"]) for e in edits or [] if isinstance(e, dict) for o in e.get("ops", []))
    have = Counter((c.get("edit"), c.get("op")) for c in plan.get("changes") or [])
    for n in plan.get("notes") or []:  # add_note may land in notes rather than changes
        if isinstance(n, dict) and have[(n.get("edit"), "add_note")] < want[(n.get("edit"), "add_note")]:
            have[(n.get("edit"), "add_note")] += 1
    if want:
        miscount = [f"{e} {o}: {want[(e, o)]} op(s), {have[(e, o)]} change(s)" for e, o in sorted(want | have, key=str)
                    if want[(e, o)] != have[(e, o)]]
    else:  # no snapshot: every applied edit id must at least show up in the changes
        ids = [a if isinstance(a, str) else a.get("id") for a in plan.get("edits_applied") or []]
        miscount = [f"{e}: no change" for e in ids if not any(k[0] == e for k in have)]
    return [_c("plan_valid", len(problems), 0, not problems, detail=problems),
            _c("plan_refusals", len(refused), 0, not refused, detail=refused),
            _c("plan_changes", sum(have.values()), "one change per op", not miscount, detail=miscount)]


def check_timeline(tl: dict) -> dict:
    """Section starts are frame-exact and add up to the program."""
    fps, sr = _fps(tl), tl.get("sr", SR)
    bad, f = [], 0
    for s in tl["sections"]:
        if s.get("start_frame") != f:
            bad.append(f'{s["name"]}: start_frame {s.get("start_frame")} ≠ {f}')
        if abs(s["t"] - f / fps) > 1e-5:
            bad.append(f'{s["name"]}: t {s["t"]} ≠ {f / fps:.6f}')
        f += s["frames"]
    if f != tl.get("frames"):
        bad.append(f'sections add up to {f} frames, timeline says {tl.get("frames")}')
    if abs(tl.get("duration", f / fps) - f / fps) > 1e-5:
        bad.append(f'duration {tl.get("duration")} ≠ {f / fps:.6f}')
    starts = [round(s["t"] * sr) for s in tl["sections"]]
    return _c("timeline", f, tl.get("frames"), not bad, detail=bad or [f"voice starts (samples): {starts}"])


def check_section_picture(path: Path, frames_: int, name: str, rate: tuple[int, int] = RATE) -> dict:
    """A re-rendered section: exactly the base frame count, h264 yuv420p 1920x1080 at the program rate, no audio."""
    _need(path)
    v, got = _stream(path, "video") or {}, video_frames(path)
    want = {"codec_name": "h264", "pix_fmt": "yuv420p", "width": 1920, "height": 1080,
            "r_frame_rate": f"{rate[0]}/{rate[1]}"}
    bad = [f"{got} frames, base {frames_}"] if got != frames_ else []
    bad += [f"{k} {v.get(k)} ≠ {w}" for k, w in want.items() if v.get(k) != w]
    bad += ["has an audio stream"] if _stream(path, "audio") else []
    return _c(f"section_{name}", got, frames_, not bad, detail=bad)


# ---------- checks: voice ----------

def check_voice_format(path: Path, samples: int, sr: int = SR) -> dict:
    """voice.wav: exactly the program length in samples, 48 kHz stereo."""
    a = _stream(_need(path), "audio") or {}
    n = int(a.get("duration_ts") or round(_f(a.get("duration")) * sr))
    bad = [f"{n} samples, want {samples}"] if n != samples else []
    bad += [f'{a.get("sample_rate")} Hz'] if int(a.get("sample_rate") or 0) != sr else []
    bad += [f'{a.get("channels")} channels'] if a.get("channels") != 2 else []
    return _c("voice_format", n, samples, not bad, detail=bad)


def check_isolation(tl: dict, job: dict | None = None) -> list[dict]:
    """Per isolated piece: r ≥ 0.6 and a lag step from the previous piece in [−15, +60] ms. Camera fallbacks warn;
    more than 2 in one span fail."""
    rows, bad, fb, fb_fail, n_fb = [], [], [], False, 0
    for i, sec in enumerate(tl["sections"]):
        spec = _get(tl, job, i, "isolate")
        if not spec:
            continue
        span, iso, prev = range(spec["pieces"][0], spec["pieces"][1] + 1), _isolated(sec), None
        for k in span:
            e = iso.get(k)
            if e is None:
                continue
            r, lag = e.get("r"), e.get("lag_ms")
            if r is None or r < R_ISO:
                bad.append(f'{sec["name"]} piece {k}: r {"not reported" if r is None else f"{r:.2f}"}')
            if lag is None:
                bad.append(f'{sec["name"]} piece {k}: lag not reported')
            elif prev is not None and not STEP[0] <= lag - prev <= STEP[1]:
                bad.append(f'{sec["name"]} piece {k}: lag step {lag - prev:+.0f} ms')
            prev = lag if lag is not None else prev
        miss = [k for k in span if k not in iso]
        rows.append(f'{sec["name"]} {len(span) - len(miss)}/{len(span)} isolated')
        n_fb += len(miss)
        fb_fail |= len(miss) > 2
        if miss:
            fb.append(f'{sec["name"]}: camera audio on piece{"s" if len(miss) > 1 else ""} {", ".join(map(str, miss))}')
    return [_c("voice_isolation", "; ".join(rows), f"r ≥ {R_ISO}, lag step {STEP[0]}…+{STEP[1]} ms", not bad,
               detail=bad),
            {**_c("voice_fallbacks", n_fb, "0 (warn), ≤ 2 per span", not fb, "fail" if fb_fail else "warn"),
             **({"detail": fb} if fb else {})}]


def check_iso_subwindows(voice: Path, tl: dict, job: dict | None = None, w: float = 4.0, search: int = 30,
                         edge: float = 0.1) -> dict:
    """Inside every isolated piece, 4 s windows of voice.wav sit within 4 ms of the section's own audio."""
    _need(voice)
    rows, bad, worst, n = [], [], 0, 0
    if not any(_isolated(sec) for sec in tl["sections"]):
        return _c("voice_iso_windows", None, f"|lag| ≤ {LAG_SUB} ms", True, detail=["no isolated pieces"])
    for i, sec in enumerate(tl["sections"]):
        iso = _isolated(sec)
        if not iso:
            continue
        ref, d0 = _need(_get(tl, job, i, "audio", "audio_from"), f'{sec["name"]} audio'), sec.get("audio_offset", D0)
        for k in sorted(iso):
            p = sec["pieces"][k]
            a0, a1 = p["A"] + d0 + edge, p["A"] + p["d"] + d0 - edge
            if a1 - a0 < 1.5:
                continue
            ea = envelope(pcm(voice, sec["t"] + a0, a1 - a0))
            eb = envelope(pcm(ref, a0 - search / 1000, a1 - a0 + 2 * search / 1000))
            win, lags = int(min(w, a1 - a0) * 1000), []
            for j in range(0, len(ea) - win + 1, win):
                lag, r = _xcorr(ea[j:j + win], eb[j:j + win + 2 * search], search)
                if r >= R_MIX:
                    lags.append(lag)
            n += len(lags)
            if lags:
                worst = max(worst, *map(abs, lags))
                rows.append(f'{sec["name"]} piece {k}: {" ".join(f"{x:+d}" for x in lags)} ms')
                if max(map(abs, lags)) > LAG_SUB:
                    bad.append(rows[-1])
    if not n:
        return _c("voice_iso_windows", None, f"|lag| ≤ {LAG_SUB} ms", False, "warn", ["no isolated window measured"])
    return _c("voice_iso_windows", worst, f"|lag| ≤ {LAG_SUB} ms", not bad, detail=bad or rows)


# ---------- checks: master and sync ----------

def check_master(master: Path, frames_: int, tail: float = 0.03) -> list[dict]:
    """Video has the program's frames and starts at 0; audio starts at 0 and ends within `tail` of the video."""
    v, a = _stream(_need(master), "video") or {}, _stream(master, "audio") or {}
    got, vs, as_ = video_frames(master), _f(v.get("start_time")), _f(a.get("start_time"), math.nan)
    vend, aend = vs + _f(v.get("duration")), as_ + _f(a.get("duration"))
    fps = _f(Fraction(v.get("r_frame_rate") or "60000/1001"))
    bad = [f"{got} frames, want {frames_}"] if got != frames_ else []
    bad += [f"video starts at {vs:.4f}"] if abs(vs) > 5e-4 else []
    bad += [f"video lasts {vend - vs:.4f} s for {got} frames"] if abs(vend - vs - got / fps) > 0.5 / fps else []
    audio = [] if abs(as_) <= 5e-4 else [f"audio starts at {as_:.4f}"]
    audio += [] if abs(aend - vend) <= tail else [f"audio ends {aend - vend:+.3f} s from the video"]
    return [_c("master_frames", got, frames_, not bad, detail=bad),
            _c("master_audio_span", round(aend - vend, 4), f"start 0, end within {tail} s of video", not audio,
               detail=audio)]


def audio_gaps(path: Path) -> list[tuple[float, float]]:
    """(time s, jump ms) wherever the audio timeline jumps: a packet lasting longer than the codec's frame (how mp4
    stores a pts gap) or a pts that doesn't follow the previous packet's end. loudnorm's end-of-stream flush leaves
    one ~80 ms jump; a player then plays everything after it late."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                          "stream=time_base:packet=pts,duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    tb = Fraction(data["streams"][0]["time_base"])
    pk = [(int(q["pts"]), int(q.get("duration") or 0)) for q in data.get("packets", []) if "pts" in q]
    if len(pk) < 2:
        return []
    frame = statistics.median(d for _, d in pk)
    jumps = [(b[0], b[0] - a[0] - a[1]) for a, b in zip(pk, pk[1:])] + [(t, d - frame) for t, d in pk[:-1]]
    return sorted((float(t * tb), round(float(j * tb) * 1000, 2)) for t, j in jumps if abs(j) * tb > Fraction(1, 48000))


def check_audio_gaps(path: Path, name: str, level: str = "fail") -> dict:
    """The audio timestamps run without jumps, so the timestamp-placed sound stays on the picture to the end."""
    gaps = audio_gaps(_need(path))
    return _c(f"{name}_audio_gaps", round(sum(abs(g) for _, g in gaps), 2), "0 ms of pts jumps", not gaps, level,
              detail=[f"{t:.3f} s: {g:+.1f} ms" for t, g in gaps[:10]])


def check_joins(master: Path, joins: list[int], fps: float, step: float = 1.0) -> dict:
    """Each section join is a clean cut exactly at its frame: frame f is no hold of f − 1 (a lossy encode never
    repeats bit-exact, so the luma step is measured) and is the biggest step within ±3 frames."""
    bad, rows = [], []
    for f in joins:
        k0 = max(0, f - 4)
        d = diffs(_need(master), k0, 8, fps)  # d[k] is the step into frame k0 + k + 1
        at, here = k0 + int(np.argmax(d)) + 1, float(d[f - k0 - 1]) if f - k0 - 1 < len(d) else 0.0
        rows.append(f"frame {f}: step {here:.2f}, biggest at {at}")
        if here < step or at != f:
            bad.append(rows[-1])
    return _c("master_joins", len(bad), "cut exactly at each join frame", not bad, detail=bad or rows)


def check_loudness(path: Path, name: str, lufs: tuple[float, float] | None = LUFS, tp: float = TP_MASTER,
                   level: str = "fail") -> list[dict]:
    i, p = loudness(_need(path))
    out = [] if lufs is None else [_c(f"{name}_loudness", i, f"{lufs[0]}…{lufs[1]} LUFS", lufs[0] <= i <= lufs[1],
                                      level)]
    return out + [_c(f"{name}_true_peak", p, f"≤ {tp} dBTP", p <= tp, level)]


def _sync_rows(pairs, search: int, limit: int, r_min, name: str, threshold: str) -> dict:
    """pairs: (label, test, t_test, ref, t_ref, w, r_needed or None)."""
    rows, bad, worst = [], [], 0
    for label, test, t_test, ref, t_ref, w, r_need in pairs:
        lag, r = measure(test, t_test, ref, t_ref, w, search)
        need = r_need if r_need is not None else r_min
        row = f"{label}: {lag:+d} ms r {r:.2f}"
        worst = max(worst, abs(lag))
        if abs(lag) > limit or r < need:
            bad.append(row)
        rows.append(row)
    if not rows:
        return _c(name, None, threshold, False, "warn", ["nothing to measure"])
    return _c(name, worst, threshold, not bad, detail=[("✗ " if r in bad else "") + r for r in rows])


def check_sync_joined(voice: Path, tl: dict, job: dict | None = None, w: float = 6.0, search: int = 100,
                      tail: float = 2.8) -> dict:
    """voice.wav at S + A_i + D0 against the timestamp-decoded joined.mp4 at A_i + D0 for 3 pieces per talk section,
    plus each talk section's last `tail` s, where a loudnorm flush can stamp the section render's audio late."""
    _need(voice)
    pairs = []
    for i in _talk(tl):
        sec, d0 = tl["sections"][i], tl["sections"][i].get("audio_offset", D0)
        joined, iso = _need(Path(_get(tl, job, i, "work") or "") / "render" / "joined.mp4"), _isolated(sec)
        for k in _pick(sec):
            p = sec["pieces"][k]
            pairs.append((f'{sec["name"]} piece {k}', voice, sec["t"] + p["A"] + d0, joined, p["A"] + d0,
                          min(w, p["d"] - 0.1), R_ISO if k in iso else R_CAMERA))
        if sec.get("pieces") and sec["d"] > tail + 1:
            last = len(sec["pieces"]) - 1
            pairs.append((f'{sec["name"]} tail', voice, sec["t"] + sec["d"] - tail, joined, sec["d"] - tail,
                          tail - 0.4, R_ISO if last in iso else R_CAMERA))
    return _sync_rows(pairs, search, LAG_PIECE, R_CAMERA, "sync_voice_joined",
                      f"|lag| ≤ {LAG_PIECE} ms, r ≥ {R_CAMERA} camera / {R_ISO} isolated")


def check_sync_master(master: Path, voice: Path, tl: dict, w: float = 6.0, search: int = 50) -> dict:
    """The master's audio against voice.wav at 6 points (first and last checked piece of each talk section)."""
    _need(master), _need(voice)
    pairs = []
    for i in _talk(tl):
        sec, d0 = tl["sections"][i], tl["sections"][i].get("audio_offset", D0)
        ks = _pick(sec)
        for k in sorted({ks[0], ks[-1]}) if ks else []:
            t = sec["t"] + sec["pieces"][k]["A"] + d0
            pairs.append((f'{sec["name"]} piece {k}', master, t, voice, t, min(w, sec["pieces"][k]["d"] - 0.1), None))
    return _sync_rows(pairs, search, LAG_MASTER, R_MIX, "sync_master_voice", f"|lag| ≤ {LAG_MASTER} ms")


def check_sync_phone(phone: Path, master: Path, tl: dict, w: float = 6.0, search: int = 50) -> dict:
    """Phone audio against the master's at the middle checked piece of each talk section."""
    _need(phone), _need(master)
    pairs = []
    for i in _talk(tl):
        sec, d0 = tl["sections"][i], tl["sections"][i].get("audio_offset", D0)
        ks = _pick(sec)
        if ks:
            k = ks[len(ks) // 2]
            t = sec["t"] + sec["pieces"][k]["A"] + d0
            pairs.append((f'{sec["name"]} piece {k}', phone, t, master, t, min(w, sec["pieces"][k]["d"] - 0.1), None))
    return _sync_rows(pairs, search, LAG_PHONE, R_MIX, "sync_phone_master", f"|lag| ≤ {LAG_PHONE} ms")


def check_sync_camera(voice: Path, tl: dict, w: float = 6.0, search: int = 50) -> dict:
    """The checked pieces against their camera files at src_in; a missing drive or a weak match is a warning."""
    _need(voice)
    rows, fail, soft, worst = [], False, False, None
    for i in _talk(tl):
        sec, d0 = tl["sections"][i], tl["sections"][i].get("audio_offset", D0)
        for k in _pick(sec):
            p, label = sec["pieces"][k], f'{sec["name"]} piece {k}'
            if not p.get("source") or not Path(p["source"]).exists():
                rows.append(f"{label}: camera file not mounted")
                soft = True
                continue
            lag, r = measure(voice, sec["t"] + p["A"] + d0, Path(p["source"]), p["src_in"], min(w, p["d"] - 0.1), search)
            if r < R_MIX:
                rows.append(f"{label}: {lag:+d} ms r {r:.2f} (too weak to judge)")
                soft = True
                continue
            worst = max(worst or 0, abs(lag))
            rows.append(("✗ " if abs(lag) > LAG_CAMERA else "") + f"{label}: {lag:+d} ms r {r:.2f}")
            fail |= abs(lag) > LAG_CAMERA
    status = "fail" if fail else "warn" if soft or worst is None else "pass"
    return {"name": "sync_camera", "value": worst, "threshold": f"|lag| ≤ {LAG_CAMERA} ms", "status": status,
            **({"detail": rows} if rows else {})}


# ---------- checks: picture timing ----------

def check_cuts(master: Path, expected: list[tuple[str, float]], fps: float,
               tol: tuple[float, float] = CUT_MASTER) -> tuple[dict, list[tuple[str, float, float | None]]]:
    """Scene cuts in the master fall within tol of where the plan puts them."""
    _need(master)
    got, bad = [], []
    for label, t in expected:
        c = cut_near(master, t, fps)
        got.append((label, t, c))
        if c is None or not tol[0] <= c - t <= tol[1]:
            bad.append(f"{label} at {t:.3f}: " + ("no cut found" if c is None else f"{c - t:+.3f} s"))
    worst = max((abs(c - t) for _, t, c in got if c is not None), default=None)
    rows = [f"{label} {t:.3f} → " + ("none" if c is None else f"{c:.3f} ({c - t:+.3f})") for label, t, c in got]
    if not expected:
        return _c("cuts_master", None, f"{tol[0]:+}…{tol[1]:+} s", False, "warn", ["no cuts to check"]), got
    return _c("cuts_master", None if worst is None else round(worst, 4), f"{tol[0]:+}…{tol[1]:+} s", not bad,
              detail=bad or rows), got


def check_phone_cuts(phone: Path, master_cuts: list[tuple[str, float, float | None]], fps: float,
                     tol: tuple[float, float] = CUT_PHONE) -> dict:
    """The phone copy's cuts sit 0…+34 ms after the master's (the fps filter, not -r)."""
    _need(phone)
    rows, bad = [], []
    for label, _, m in master_cuts:
        if m is None:
            continue
        c = cut_near(phone, m, fps)
        rows.append(f"{label} {m:.3f} → " + ("none" if c is None else f"{c - m:+.3f}"))
        if c is None or not tol[0] <= c - m <= tol[1]:
            bad.append(rows[-1])
    if not rows:
        return _c("cuts_phone", None, f"{tol[0]:+}…{tol[1]:+} s", False, "warn", ["no master cut to compare"])
    return _c("cuts_phone", len(bad), f"{tol[0]:+}…{tol[1]:+} s of the master", not bad, detail=bad or rows)


def check_phone_frames(phone: Path, master_frames: int) -> dict:
    got, lo = video_frames(_need(phone)), master_frames // 2
    return _c("phone_frames", got, f"{lo}…{lo + 2}", lo <= got <= lo + 2)


# ---------- checks: words ----------

def _norm(w: str) -> str:
    return re.sub(r"[^a-z0-9]", "", w.lower())


NUM_WORDS = set("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
                "seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred thousand "
                "million billion".split())
NUM_UNITS = {"dollar", "dollars", "percent", "k"}


def _numbers(ws: list[tuple[str, float, float]]) -> list[tuple[str, float, float]]:
    """Each spoken amount as one "#" token: Whisper writes "$250,000" (250 000) on one track and "three hundred and
    seventy five thousand dollars" on another, and neither is a lost word."""
    num = lambda w: w.isdigit() or w in NUM_WORDS  # noqa: E731
    out, i = [], 0
    while i < len(ws):
        j = i
        while j < len(ws) and (num(ws[j][0]) or (j > i and ws[j][0] == "and" and j + 1 < len(ws) and num(ws[j + 1][0]))):
            j += 1
        if j == i:
            out.append(ws[i])
            i += 1
            continue
        while j < len(ws) and ws[j][0] in NUM_UNITS:
            j += 1
        out.append(("#", ws[i][1], ws[j - 1][2]))
        i = j
    return out


def words(path: Path) -> list[tuple[str, float, float]]:
    """Whisper JSON ({segments: [{words}]}) or the app's transcript ([{w: [[word, start, end]]}]) → normalised words."""
    data = json.loads(Path(path).read_text())
    out = []
    for s in data["segments"] if isinstance(data, dict) else data:
        ws = [(w["word"], w["start"], w["end"]) for w in s["words"]] if "words" in s else [w[:3] for w in s.get("w", [])]
        out += [(_norm(t), float(a), float(b)) for t, a, b in ws if _norm(t)]
    return out


def pause_db(voice: Path, x: float, half: float = 0.015, ctx: float = 2.0) -> float:
    """How far (dB) the speech band at x ± half sits below the local speech level (90th percentile over ± ctx)."""
    e = envelope(pcm(voice, x - ctx, 2 * ctx))
    c, h = len(e) // 2, round(half * 1000)
    return float(20 * (np.percentile(e, 90) - e[c - h:c + h + 1].max()) / np.log(10))


def _assembly(tl: dict, job: dict | None, x: float) -> tuple[Path, float] | None:
    """The talk section's own assembled audio (its joined.mp4) and x in its time, for the section starting at or
    before x: the dialogue edit this render's voice was placed from."""
    i = max((k for k, s in enumerate(tl["sections"]) if s["t"] <= x + 1e-6), default=None)
    work = _get(tl, job, i, "work") if i is not None else None
    f = Path(work) / "render" / "joined.mp4" if work else None
    return (f, x - tl["sections"][i]["t"]) if f and f.exists() else None


def check_words_at_joins(new_words: list[tuple[str, float, float]], tl: dict, voice: Path | None = None,
                         quiet: float = 20.0, job: dict | None = None, same: float = 6.0) -> dict:
    """No section join or isolated/camera switch inside a Whisper word. Whisper's word edges drift into pauses, so
    with voice.wav at hand a hit where the voice is ≥ `quiet` dB below its local level is only a warning. So is a
    hit where the section's assembled audio is as loud at that point (within `same` dB): the original dialogue edit
    cut there (or a take starts on its first word), and a render that can't cut talk can't move it. A hit where the
    voice is loud but the assembly is quiet is this render's doing and fails."""
    hard, soft = [], []
    for x in switch_points(tl):
        for w, a, b in new_words:
            if a + WORD_EDGE < x < b - WORD_EDGE:
                db = pause_db(voice, x) if voice and Path(voice).exists() else None
                row = f'{x:.3f} inside "{w}" ({a:.2f}–{b:.2f})' + ("" if db is None else f", voice {db:.0f} dB down")
                src = _assembly(tl, job, x) if db is not None and db < quiet else None
                inherited = src is not None and pause_db(*src) - db <= same
                if inherited:
                    row += f" (the assembly is {pause_db(*src):.0f} dB down there: from the original edit)"
                (soft if db is not None and (db >= quiet or inherited) else hard).append(row)
    rows = hard + soft
    return {"name": "words_at_joins", "value": len(rows), "threshold": "no join or switch inside a word",
            "status": "fail" if hard else "warn" if soft else "pass",
            **({"detail": rows, "t": float(rows[0].split()[0])} if rows else {})}


def check_words(prev: Path | None, new: Path, tl: dict, voice: Path | None = None,
                job: dict | None = None) -> list[dict]:
    """Same words as the previous version, nothing lost, no join or voice switch inside a word, median shifts."""
    nw = words(_need(new, "transcript"))
    at_joins = check_words_at_joins(nw, tl, voice, job=job)
    if not prev or not Path(prev).exists():
        return [_c("words_ratio", None, f"≥ {WORD_RATIO}", False, "warn", ["no previous transcript"]), at_joins]
    pw, nw = _numbers(words(Path(prev))), _numbers(nw)
    sm = difflib.SequenceMatcher(None, [w[0] for w in pw], [w[0] for w in nw], autojunk=False)
    run, where, shifts = 0, "", {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for i, j in zip(range(i1, i2), range(j1, j2)):
                shifts.setdefault(_section_at(tl, nw[j][1]), []).append(nw[j][1] - pw[i][1])
        elif i2 - i1 > run and (tag == "delete" or difflib.SequenceMatcher(
                None, " ".join(w[0] for w in pw[i1:i2]), " ".join(w[0] for w in nw[j1:j2])).ratio() < 0.6):
            run, where = i2 - i1, f'{pw[i1][1]:.2f}: "{" ".join(w[0] for w in pw[i1:i2])[:120]}"'
    ratio = round(sm.ratio(), 4)
    med = {k: round(statistics.median(v), 3) for k, v in shifts.items()}
    return [_c("words_ratio", ratio, f"≥ {WORD_RATIO}", ratio >= WORD_RATIO, detail=[f"{len(pw)} → {len(nw)} words"]),
            _c("words_missing_run", run, f"< {WORD_RUN} words", run < WORD_RUN, detail=[where] if run >= WORD_RUN else None),
            at_joins,
            _c("word_shift", med, "report (median s per section)", True)]


# ---------- checks: cards ----------

def _lines(card: dict) -> list[str]:
    return [x[0] if isinstance(x, (list, tuple)) else x for x in card.get("lines") or []]


def _line_times(card: dict) -> list[float]:
    return [float(x[1]) for x in card.get("lines") or [] if isinstance(x, (list, tuple))]


def card_box(c: int, card: dict, facts_dir: Path, height: int = 1080) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1) of card c on screen, from its PNG sizes and facts()'s layout (x = 96, bottom margin 110)."""
    from PIL import Image

    sizes = [Image.open(_need(facts_dir / f"c{c}l{k}.png")).size for k in range(len(_lines(card)))]
    y = height - 110 - sum(h - 34 for _, h in sizes)
    x1, y0, y1 = 96 + max(w for w, _ in sizes), y, y
    for w, h in sizes:
        y1 = y + h
        y += h - 34
    cap = facts_dir / f"c{c}cap.png"
    if card.get("caption") and cap.exists():
        cw, chh = Image.open(cap).size
        y0, x1 = y0 - chh + 22, max(x1, 96 + cw)
    return 96, max(0, y0), x1, min(height, y1)


def card_mid(card: dict) -> float:
    """Middle of the span where the whole card is on screen (after the last slide-in, before the fade-out)."""
    ts = _line_times(card)
    a, b = max(ts) + 0.3, card["until"] - 0.35
    return (a + b) / 2 if b > a else (min(ts) + card["until"]) / 2


def check_cards(cards: list[dict], tl: dict, master: Path, facts_dir: Path, sheet: Path,
                inside: float = CARD_IN, outside: float = CARD_OUT) -> list[dict]:
    """At each card's middle the master differs from its section picture inside the card box and nowhere else;
    writes a contact sheet of every card as it plays."""
    fps, tiles, bad, rows = _fps(tl), [], [], []
    for c, card in enumerate(cards):
        x0, y0, x1, y1 = card_box(c, card, facts_dir)
        n = math.floor(card_mid(card) * fps)
        s = next(s for s in tl["sections"] if s["start_frame"] <= n < s["start_frame"] + s["frames"])
        m = frames(_need(master), n, 1, fps)[0]
        p = frames(_need(s["picture"], f'{s["name"]} picture'), n - s["start_frame"], 1, fps)[0]
        diff = np.abs(m.astype(np.int16) - p.astype(np.int16)).mean(2)
        mask = np.ones(diff.shape, bool)
        mask[max(0, y0 - 12):y1 + 12, max(0, x0 - 12):x1 + 12] = False
        din, dout = float(diff[y0:y1, x0:x1].mean()), float(diff[mask].mean())
        row = f"c{c + 1} at {n / fps:.2f}: inside {din:.1f}, outside {dout:.2f}"
        rows.append(row)
        if din < inside or dout > outside:
            bad.append(row)
        tiles.append((f"c{c + 1}  {int(n / fps // 60)}:{n / fps % 60:05.2f}  {' / '.join(_lines(card))[:60]}",
                      m[max(0, y0 - 40):, :min(m.shape[1], x1 + 120)]))
    if tiles:
        contact_sheet(tiles, sheet)
    return [_c("cards_visible", len(bad), f"inside ≥ {inside}, outside ≤ {outside}", not bad, "warn",
               detail=bad or rows),
            _c("cards_sheet", str(sheet.name) if tiles else None, "written", bool(tiles), "warn")]


def contact_sheet(tiles: list[tuple[str, np.ndarray]], out: Path, cols: int = 3, tw: int = 640) -> Path:
    from PIL import Image, ImageDraw

    ims = [Image.fromarray(np.ascontiguousarray(a)) for _, a in tiles]
    ims = [im.resize((tw, max(1, round(im.height * tw / im.width)))) for im in ims]
    th = max(im.height for im in ims) + 22
    sheet = Image.new("RGB", (cols * tw, math.ceil(len(ims) / cols) * th), (18, 18, 18))
    d = ImageDraw.Draw(sheet)
    for k, (im, (label, _)) in enumerate(zip(ims, tiles)):
        x, y = k % cols * tw, k // cols * th
        sheet.paste(im, (x, y + 22))
        try:
            d.text((x + 6, y + 5), label, fill=(235, 235, 235))
        except UnicodeEncodeError:  # Pillow without FreeType: the bitmap font is latin-1 only
            d.text((x + 6, y + 5), label.encode("latin-1", "replace").decode("latin-1"), fill=(235, 235, 235))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=85)
    return out


def check_card_text(spec_cards: list[dict], tl_cards: list[dict], facts_dir: Path | None = None,
                    project_cards: list[dict] | None = None) -> list[dict]:
    """The same card text in cards.json, timeline.json (and project.json); no path text, at least one line, lines
    ≤ 1784 px. (Written cards keep 1–3 lines through the plan; v16 cards may have 4.)"""
    key = lambda c: (c.get("caption") or "", tuple(_lines(c)))  # noqa: E731
    want = [key(c) for c in spec_cards]
    diff = [f"timeline card {k + 1}: {got} ≠ {w}" for k, (w, got) in enumerate(zip(want, map(key, tl_cards))) if w != got]
    diff += [f"{len(spec_cards)} cards in cards.json, {len(tl_cards)} in timeline.json"] if len(spec_cards) != len(tl_cards) else []
    if project_cards is not None:
        diff += [f"project card {k + 1}: {got} ≠ {w}" for k, (w, got) in enumerate(zip(want, map(key, project_cards)))
                 if w != got]
        diff += [f"{len(project_cards)} cards in project.json"] if len(project_cards) != len(spec_cards) else []
    guard = []
    for c, card in enumerate(spec_cards):
        texts = [card.get("caption") or ""] + _lines(card)
        guard += [f"c{c + 1}: path-like text {t!r}" for t in texts if GUARD.search(t)]
        guard += [f"c{c + 1}: no lines"] if not _lines(card) else []
        if facts_dir:
            from PIL import Image

            for k in range(len(_lines(card))):
                f = facts_dir / f"c{c}l{k}.png"
                if f.exists() and Image.open(f).size[0] > MAX_LINE_W:
                    guard.append(f"c{c + 1} line {k + 1}: {Image.open(f).size[0]} px wide")
    return [_c("cards_text", len(diff), "identical everywhere", not diff, "warn", detail=diff),
            _c("cards_guard", len(guard), f"no path text, ≥ 1 line, ≤ {MAX_LINE_W} px", not guard, detail=guard)]


# ---------- checks: hygiene and phone ----------

def check_outputs(rdir: Path, tl: dict, files: dict[str, Path]) -> dict:
    """Everything this version wrote resolves inside rNNN (voice.wav may link into voice_cache)."""
    roots = [rdir.resolve(), (rdir.parent / "voice_cache").resolve()]
    paths = [p for k, p in files.items() if k != "transcript"]
    paths += [Path(s["picture"]) for s in tl["sections"] if s.get("picture_rendered")]
    out = [str(p) for p in paths if not any(Path(p).resolve().is_relative_to(r) for r in roots)]
    return _c("outputs_inside", len(out), 0, not out, detail=out)


def check_fingerprints(fingerprints: dict) -> dict:
    """(size, mtime_ns) of every protected and read file is unchanged since preflight."""
    changed = []
    for p, v in (fingerprints or {}).items():
        if v is None:
            continue
        want = (v["size"], v["mtime_ns"]) if isinstance(v, dict) else tuple(v)[:2]
        try:
            st = os.stat(p)
        except FileNotFoundError:
            changed.append(f"{p}: missing")
            continue
        if (st.st_size, st.st_mtime_ns) != tuple(want):
            changed.append(p)
    return _c("fingerprints", len(changed), f"0 of {len(fingerprints or {})} changed", not changed, detail=changed)


def check_phone(phone: Path, mb: float = PHONE_MB) -> list[dict]:
    size = _need(phone).stat().st_size / 1e6
    return check_loudness(phone, "phone", None, TP_PHONE, "warn") + [
        _c("phone_size", round(size, 1), f"≤ {mb} MB", size <= mb, "warn")]


# ---------- the whole pass ----------

def _files(rdir: Path, tl: dict) -> dict[str, Path]:
    return {k: v if Path(v).is_absolute() else rdir / v for k, v in {**FILES, **(tl.get("files") or {})}.items()}


def _write(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    os.replace(tmp, path)


def verify(rdir: Path, tl: dict, plan: dict, job: dict, prev_transcript: Path | None, fingerprints: dict,
           log=print, transcript: Path | None = None) -> dict:
    """Run every check on rNNN, write rNNN/verify.json (+ qa/cards.jpg). {ok, checks, measured}; ok is False when
    any check fails. A check that can't run (missing file, ffmpeg error) counts as failed at its own level."""
    rdir = Path(rdir)
    f = _files(rdir, tl)
    if transcript:
        f["transcript"] = Path(transcript)
    fps, checks, got = _fps(tl), [], {}
    sr = tl.get("sr", SR)

    def step(name: str, fn, level: str = "fail"):
        try:
            out = fn()
        except Exception as e:  # noqa: BLE001 - a check that can't run is a failed check, never a crash
            out = _c(name, f"{type(e).__name__}: {e}"[:300], "runs", False, level)
        for c in out if isinstance(out, list) else [out]:
            checks.append(c)
            log(f'verify {c["status"]:4}  {c["name"]}: {c["value"]}  ({c["threshold"]})')
        return out

    snap = rdir / "edits_applied.json"  # the edits this version was started with
    step("plan", lambda: check_plan(plan, json.loads(snap.read_text()) if snap.exists() else None))
    step("timeline", lambda: check_timeline(tl))
    for i in plan.get("dirty") or []:
        step(f"section_{i}_picture", lambda i=i: check_section_picture(
            Path(tl["sections"][i]["picture"]), tl["sections"][i]["frames"], f"{i}_picture", tuple(tl.get("rate", RATE))))
    rate = tl.get("rate", RATE)
    step("voice_format", lambda: check_voice_format(f["voice"], round(Fraction(tl["frames"] * rate[1] * sr, rate[0])),
                                                    sr))
    step("voice_isolation", lambda: check_isolation(tl, job))
    step("voice_iso_windows", lambda: check_iso_subwindows(f["voice"], tl, job))
    step("master_frames", lambda: check_master(f["master"], tl["frames"]))
    step("master_audio_gaps", lambda: check_audio_gaps(f["master"], "master"))
    step("master_joins", lambda: check_joins(f["master"], [s["start_frame"] for s in tl["sections"][1:]], fps))
    step("master_loudness", lambda: check_loudness(f["master"], "master"))
    step("sync_voice_joined", lambda: check_sync_joined(f["voice"], tl, job))
    step("sync_master_voice", lambda: check_sync_master(f["master"], f["voice"], tl))
    step("sync_camera", lambda: check_sync_camera(f["voice"], tl), "warn")

    def cuts():
        intro = tl["sections"][0] if tl["sections"][0].get("kind") == "intro" else None
        cold = intro_cuts(_need(intro["picture"], "cold open picture"), fps) if intro else []
        c, got["cuts"] = check_cuts(f["master"], expected_cuts(tl, plan) +
                                    [(f"cold open {k + 1}", t) for k, t in enumerate(cold)], fps)
        return c

    step("cuts_master", cuts)
    step("cuts_phone", lambda: check_phone_cuts(f["phone"], got.get("cuts") or [],
                                                 _f(Fraction(_stream(_need(f["phone"]), "video")["r_frame_rate"]))))
    step("sync_phone_master", lambda: check_sync_phone(f["phone"], f["master"], tl))
    step("phone_frames", lambda: check_phone_frames(f["phone"], tl["frames"]))
    step("phone_audio_gaps", lambda: check_audio_gaps(f["phone"], "phone"))
    step("words", lambda: check_words(prev_transcript, f["transcript"], tl, f["voice"], job))
    spec = lambda: json.loads((rdir / "cards.json").read_text())["cards"] if (rdir / "cards.json").exists() \
        else tl.get("cards") or []  # noqa: E731 - the facts spec the cards pass actually drew
    step("cards_text", lambda: check_card_text(spec(), tl.get("cards") or [], rdir / "facts"))
    step("cards_visible", lambda: check_cards(spec(), tl, f["master"], rdir / "facts", rdir / "qa" / "cards.jpg"),
         "warn")
    step("outputs_inside", lambda: check_outputs(rdir, tl, f))
    step("fingerprints", lambda: check_fingerprints(fingerprints))
    step("phone", lambda: check_phone(f["phone"]), "warn")

    by = {c["name"]: c["value"] for c in checks if isinstance(c["value"], (int, float))}
    measured = {"frames": by.get("master_frames"), "lufs": by.get("master_loudness"), "tp": by.get("master_true_peak"),
                "phone_tp": by.get("phone_true_peak"), "phone_frames": by.get("phone_frames"),
                "sync_ms": by.get("sync_voice_joined"), "cuts": [[lb, round(t, 4), None if c is None else round(c, 4)]
                                                                  for lb, t, c in got.get("cuts") or []]}
    ok = not any(c["status"] == "fail" for c in checks)
    _write(rdir / "verify.json", {"version": tl.get("version"), "ok": ok, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                  "checks": checks, "measured": measured})
    log(f'verify {"passed" if ok else "FAILED"}: {sum(c["status"] == "pass" for c in checks)}/{len(checks)} pass, '
        f'{sum(c["status"] == "warn" for c in checks)} warn, {sum(c["status"] == "fail" for c in checks)} fail')
    return {"ok": ok, "checks": checks, "measured": measured}


def verify_promoted(demo: Path, tl: dict, rdir: Path | None = None, log=print) -> dict:
    """After promote: edits.json is [], the live project is this version with nothing pending, no applied edit is
    still queued, and its card text matches the timeline. Appended to rNNN/verify.json when rdir is given."""
    demo = Path(demo)
    edits = json.loads((demo / "edits.json").read_text()) if (demo / "edits.json").exists() else []
    project = json.loads(_need(demo / "project.json").read_text())
    current = json.loads((demo / "current.json").read_text()) if (demo / "current.json").exists() else {}
    again = [e.get("id") for e in edits if e.get("id") in set(tl.get("edits_applied") or [])]
    checks = [_c("promoted_edits", len(edits), 0, edits == []),
              _c("promoted_version", project.get("version"), tl.get("version"),
                 project.get("version") == tl.get("version") == current.get("version")),
              _c("promoted_pending", project.get("pending", 0), 0, not project.get("pending")),
              _c("promoted_no_reapply", len(again), 0, not again, detail=again)]
    cards = tl.get("cards") or []
    checks += [{**c, "name": "promoted_" + c["name"]}
               for c in check_card_text(cards, cards, project_cards=project.get("cards") or [])[:1]]
    for c in checks:
        log(f'verify {c["status"]:4}  {c["name"]}: {c["value"]}  ({c["threshold"]})')
    ok = not any(c["status"] == "fail" for c in checks)
    if rdir and (Path(rdir) / "verify.json").exists():
        v = json.loads((Path(rdir) / "verify.json").read_text())
        v["checks"] = [c for c in v["checks"] if not c["name"].startswith("promoted_")] + checks
        v["promoted_ok"] = ok
        _write(Path(rdir) / "verify.json", v)
    return {"ok": ok, "checks": checks}
