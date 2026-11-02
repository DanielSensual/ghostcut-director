"""Program voice for Render: every section's audio decoded by packet timestamp and placed sample-exact, with the cached
voice isolation re-placed piece by piece at a measured lag. Nothing is isolated again and no service is called: the
isolation is only read from disk, and a piece it can't be lined up with keeps its camera audio.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import wave
from fractions import Fraction
from pathlib import Path

import numpy as np

VERSION = "voice/2"
SR = 48000
D0 = 0.021  # video start of joined.mp4: piece i's voice sits at A_i + D0 in its section
BAND = (300.0, 3400.0)
FIRST = (-100, 300)  # ms searched for the first piece of an isolated span
STEP = (-15, 60)  # ms searched around the previous piece's lag (the drift steps at each join)
R_MIN = 0.6
FADE = 0.010  # every piece fades out over its last 10 ms
XFADE = 0.005  # equal-power crossfade at slot edges
MAX_FALLBACKS = 2
SUB = 4.0  # s, sub-window length reported per isolated piece
TAIL = 3.2  # s: broll()'s loudnorm flush can stamp a section render's last ~3 s late


def decode_ts(path: Path | str, sr: int = SR) -> np.ndarray:
    """Float32 stereo placed by packet timestamp: gaps are filled and priming can't shift sample N away from N/sr s."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vn", "-af",
                          f"aresample={sr}:async=1:min_hard_comp=0.001:first_pts=0", "-ac", "2", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, 2)


def flush_gaps(audio: Path | str, joined: Path | str, x: np.ndarray, sr: int = SR,
               tail: float = TAIL) -> list[tuple[int, int]]:
    """(first sample, samples) of the silence decode_ts put into x for each positive pts jump in the section render's
    last `tail` s that joined.mp4 doesn't have. broll()'s loudnorm flush stamps its last ~3 s late, so the samples
    after the jump run straight on from the ones before it: the silence is padding, not a pause."""
    from render_verify import audio_gaps  # the same packet scan the verify step uses
    ref, out = audio_gaps(joined), []
    for t, ms in audio_gaps(audio):
        if ms <= 0 or t < len(x) / sr - tail or any(abs(t - u) < 0.2 and abs(ms - v) < 1.0 for u, v in ref):
            continue
        n, lo = round(ms * sr / 1000), max(0, round((t - 0.1) * sr))
        z = np.all(x[lo:min(len(x), round((t + 0.2) * sr))] == 0, axis=1).astype(np.int8)
        d = np.diff(np.concatenate([[0], z, [0]]))
        runs = list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))
        a, b = max(runs, key=lambda r: r[1] - r[0], default=(0, 0))
        if b - a >= n - 2:  # aresample fills the whole jump with exact zeros
            out.append((lo + int(a), min(n, int(b - a))))
    return out


def _band(nfft: int, sr: int, w: float = 50.0) -> np.ndarray:
    f = np.fft.rfftfreq(nfft, 1 / sr)
    up = np.clip((f - BAND[0] + w) / (2 * w), 0, 1)
    down = np.clip((BAND[1] + w - f) / (2 * w), 0, 1)
    return (np.sin(up * np.pi / 2) * np.sin(down * np.pi / 2)) ** 2  # soft edges keep the filter's ringing short


def envelope(x: np.ndarray, sr: int) -> np.ndarray:
    """Log RMS per 1 ms of the 300-3400 Hz band (FFT filter in overlapping blocks, so memory stays flat)."""
    x = np.asarray(x, np.float32)
    if x.ndim == 2:
        x = x.mean(1)
    hop = sr // 1000
    n = len(x) // hop * hop
    if not n:
        return np.zeros(0)
    pad = 4096
    nfft = min(1 << 20, 1 << int(np.ceil(np.log2(n + 2 * pad))))
    block, mask = nfft - 2 * pad, _band(nfft, sr)
    y = np.empty(n, np.float32)
    for a in range(0, n, block):
        b = min(a + block, n)
        lo, hi = max(0, a - pad), min(len(x), b + pad)
        y[a:b] = np.fft.irfft(np.fft.rfft(x[lo:hi], nfft) * mask, nfft)[a - lo:b - lo]
    return np.log(np.sqrt((y.reshape(-1, hop).astype(np.float64) ** 2).mean(1)) + 1e-5)


def _pearson(a: np.ndarray, b: np.ndarray) -> float:
    a, b = a - a.mean(), b - b.mean()
    return float(a @ b / (np.sqrt((a @ a) * (b @ b)) + 1e-12))


def _search(ea: np.ndarray, eb: np.ndarray, s: int, e: int, lo: int, hi: int) -> tuple[float, int]:
    """Lag L (ms) in [lo, hi] maximising r(ea[s:e], eb[s+L:e+L]); lags leaving under half the window are skipped."""
    best = (-1.0, lo)
    for lag in range(lo, hi + 1):
        i0, i1 = max(s + lag, 0), min(e + lag, len(eb))
        if i1 - i0 < max(2, (e - s) // 2):
            continue
        r = _pearson(ea[i0 - lag:i1 - lag], eb[i0:i1])
        if r > best[0]:
            best = (r, lag)
    return best


def realign(iso: np.ndarray, ref: np.ndarray, slots: list[tuple[int, int]], sr: int) -> list[dict]:
    """Per slot (sample range of the reference, in order): the lag of the isolation against it. The first slot
    searches FIRST, each later one STEP around the last trusted lag. ok = r >= R_MIN and the isolation covers the
    whole slot. `sub` re-measures 4 s sub-windows within +-15 ms of the piece lag (the verify step reads them)."""
    hop = sr // 1000
    end = max((e for _, e in slots), default=0)
    ea, eb = envelope(ref[:end + sr], sr), envelope(iso, sr)
    out, prev = [], None
    for s, e in slots:
        s_ms, e_ms = s // hop, min(-(-e // hop), len(ea))
        lo, hi = FIRST if prev is None else (prev + STEP[0], prev + STEP[1])
        r, lag = _search(ea, eb, s_ms, e_ms, lo, hi) if e_ms > s_ms else (-1.0, lo)
        covered = s + lag * hop >= 0 and e + lag * hop <= len(iso)
        if r >= R_MIN:
            prev = lag
        w, sub = round(SUB * 1000), []
        for a in range(s_ms, e_ms - w + 1, w) if r >= R_MIN else ():
            sr_, sl = _search(ea, eb, a, a + w, lag - 15, lag + 15)
            sub.append({"t": round(a / 1000, 3), "lag_ms": sl, "r": round(sr_, 3)})
        out.append({"lag_ms": lag, "r": round(r, 4), "ok": bool(r >= R_MIN and covered), "covered": covered,
                    "sub": sub})
    return out


def _lufs(path: Path | str) -> float:
    out = subprocess.run(["ffmpeg", "-nostats", "-nostdin", "-i", str(path), "-vn", "-af", "ebur128", "-f", "null",
                          "-"], capture_output=True, text=True).stderr
    return float([ln for ln in out.splitlines() if ln.strip().startswith("I:")][-1].split()[1])


def _starts(tl: dict, sr: int) -> list[int]:
    """First sample of every section plus the program end: frame-exact when the timeline has frame counts."""
    num, den = tl.get("rate", [60000, 1001])
    secs = tl["sections"]
    if all("start_frame" in s and "frames" in s for s in secs):
        frames = [s["start_frame"] for s in secs] + [secs[-1]["start_frame"] + secs[-1]["frames"]]
        return [round(Fraction(f * den * sr, num)) for f in frames]
    return [round(s["t"] * sr) for s in secs] + [round((secs[-1]["t"] + secs[-1]["d"]) * sr)]


def _slots(sec: dict, n: int, sr: int) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Per piece, section samples: the slot [A_i + D0, A_i+1 + D0) and its content up to the 10 ms fade-out."""
    d0, ps = sec.get("audio_offset", D0), sec.get("pieces") or []
    full, content = [], []
    for i, p in enumerate(ps):
        nxt = ps[i + 1]["A"] if i + 1 < len(ps) else p["A"] + p.get("d", p["src_out"] - p["src_in"])
        a = min(round((p["A"] + d0) * sr), n)
        full.append((a, min(round((nxt + d0) * sr), n)))
        content.append((a, min(round((p["A"] + d0 + p["src_out"] - p["src_in"] - FADE) * sr), n)))
    return full, content


def _iso_seg(iso: np.ndarray, lag: int, a: int, b: int) -> np.ndarray:
    """Isolation samples for section samples [a, b) at `lag` samples, silent where the isolation has none."""
    seg = np.zeros((b - a, 1), np.float32)
    i0, i1 = max(a + lag, 0), min(b + lag, len(iso))
    if i1 > i0:
        seg[i0 - a - lag:i1 - a - lag, 0] = iso[i0:i1]
    return seg


def _splice(buf: np.ndarray, iso: np.ndarray, regions: list[tuple[int, int, int]], sr: int) -> None:
    """Replace each (a, b, lag) region of buf with the isolation at that lag, mono to both channels, with 5 ms
    equal-power crossfades at every edge (isolation to isolation, or to and from camera audio)."""
    h, n = round(XFADE * sr / 2), len(buf)
    edges = []
    for j, (a, b, lag) in enumerate(regions):
        left = regions[j - 1][2] if j and regions[j - 1][1] == a else None
        edges.append((a, left, lag))
        if not (j + 1 < len(regions) and regions[j + 1][0] == b):
            edges.append((b, lag, None))
    mixes = []
    for e, left, right in edges:
        w0, w1 = max(e - h, 0), min(e + h, n)
        if e <= 0 or e >= n or w1 <= w0:
            continue
        th = ((np.arange(w0, w1) - (e - h) + 0.5) / (2 * h) * np.pi / 2)[:, None]
        src = lambda lag: buf[w0:w1] if lag is None else _iso_seg(iso, lag, w0, w1)  # noqa: E731
        mixes.append((w0, w1, src(left) * np.cos(th) + src(right) * np.sin(th)))
    for a, b, lag in regions:
        buf[a:b] = _iso_seg(iso, lag, a, b)
    for w0, w1, m in mixes:
        buf[w0:w1] = m


def _fingerprint(path: Path | str) -> list[int]:
    st = os.stat(path)
    return [st.st_size, st.st_mtime_ns]


def _write_s16(w: wave.Wave_write, x: np.ndarray, chunk: int = 1 << 20) -> None:
    for a in range(0, len(x), chunk):
        w.writeframes(np.clip(np.round(x[a:a + chunk] * 32768.0), -32768, 32767).astype("<i2").tobytes())


def build_voice(tl: dict, job: dict, cache_dir: Path, log=print) -> tuple[Path, dict]:
    """The program voice as <cache_dir>/<sha1>.wav (s16le stereo, exactly the program length in samples) and its
    report <sha1>.json. Cached by the inputs' (size, mtime_ns), the slots, the spans and VERSION."""
    sr = int(tl.get("sr", SR))
    jsecs = job.get("sections", [])
    starts = _starts(tl, sr)
    total = starts[-1]
    if starts[0] != 0 or any(b < a for a, b in zip(starts, starts[1:])):
        raise ValueError(f"voice: sections don't tile the program: {starts}")
    plan, inputs = [], []
    for i, sec in enumerate(tl["sections"]):
        js = jsecs[i] if i < len(jsecs) else {}
        audio = sec.get("audio") or sec.get("audio_from") or js.get("audio")
        if not audio:
            raise ValueError(f"voice: section {i} ({sec.get('name')}) has no audio file")
        spec = sec.get("isolate") or js.get("isolate")
        n = starts[i + 1] - starts[i]
        full, content = _slots(sec, n, sr)
        if spec:
            first, last = spec["pieces"]
            if not 0 <= first <= last < len(full):
                raise ValueError(f"voice: section {i} isolates pieces {first}-{last} of {len(full)}")
        work = sec.get("work") or js.get("work")
        joined = Path(work) / "render" / "joined.mp4" if work else None
        if not (joined and joined.exists() and joined.resolve() != Path(audio).resolve()):
            joined = None
        plan.append((sec, audio, spec, n, full, content, joined))
        inputs += [_fingerprint(audio)] + ([_fingerprint(spec["iso"]), _fingerprint(spec["win"])] if spec else [])
        inputs += [_fingerprint(joined)] if joined else []
    key = hashlib.sha1(json.dumps({"v": VERSION, "sr": sr, "inputs": inputs, "starts": starts,
                                   "slots": [[full, content] for *_, full, content, _ in plan],
                                   "spans": [spec and list(spec["pieces"]) for _, _, spec, *_ in plan]}).encode())
    key = key.hexdigest()
    cache_dir = Path(cache_dir)
    wav, rep = cache_dir / f"{key}.wav", cache_dir / f"{key}.json"
    if wav.exists() and rep.exists():
        try:
            report = json.loads(rep.read_text())
            with wave.open(str(wav), "rb") as w:
                hit = report.get("samples") == total and w.getnframes() == total
        except (ValueError, EOFError, wave.Error):
            hit = False
        if hit:
            log(f"voice: cached {key[:12]} ({total} samples)")
            return wav, report
    cache_dir.mkdir(parents=True, exist_ok=True)
    report = {"key": key, "version": VERSION, "sr": sr, "samples": total, "sections": [], "pieces": [], "fallbacks": []}
    tmp = wav.with_name(wav.name + ".part")
    try:
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(sr)
            for i, (sec, audio, spec, n, full, content, joined) in enumerate(plan):
                name = sec.get("name", str(i))
                x = decode_ts(audio, sr)
                gaps = flush_gaps(audio, joined, x, sr) if joined else []
                for a, k in sorted(gaps, reverse=True):
                    x = np.delete(x, np.s_[a:a + k], axis=0)
                buf = np.zeros((n, 2), np.float32)
                m = min(n, len(x))
                buf[:m] = x[:m]
                if gaps and m < n:  # the padding is gone, so the audio ends early: fade out instead of stepping
                    f = min(m, round(0.005 * sr))
                    buf[m - f:m] *= np.linspace(1, 0, f, dtype=np.float32)[:, None]
                del x
                srep = {"section": i, "name": name, "start": starts[i], "samples": n, "isolated": [],
                        "camera_fallback": [], "flush_gaps": [{"t": round(a / sr, 4), "ms": round(k * 1000 / sr, 2)}
                                                              for a, k in gaps]}
                for a, k in gaps:
                    log(f"voice: {name}: removed {k * 1000 / sr:.2f} ms of flush padding at {a / sr:.3f} s")
                if spec:
                    _isolate(i, name, spec, buf, full, content, sr, report, srep, starts[i], log)
                _write_s16(w, buf)
                report["sections"].append(srep)
                log(f"voice: {name} at sample {starts[i]}, {n} samples")
        with wave.open(str(tmp), "rb") as w:
            if w.getnframes() != total:
                raise RuntimeError(f"voice: wrote {w.getnframes()} samples, expected {total}")
        os.replace(tmp, wav)
    finally:
        tmp.unlink(missing_ok=True)
    part = rep.with_name(rep.name + ".part")
    part.write_text(json.dumps(report, indent=1))
    os.replace(part, rep)
    log(f"voice: wrote {wav.name} ({total} samples, {len(report['fallbacks'])} camera fallbacks)")
    return wav, report


def _isolate(i: int, name: str, spec: dict, buf: np.ndarray, full: list, content: list, sr: int, report: dict,
             srep: dict, start: int, log) -> None:
    """Re-place the cached isolation over pieces first..last of buf (section audio), in place; failing pieces keep
    camera audio and are listed as fallbacks."""
    first, last = spec["pieces"]
    srep["span"] = [first, last]
    gain_db = _lufs(spec["win"]) - _lufs(spec["iso"])
    iso = decode_ts(spec["iso"], sr).mean(1) * np.float32(10 ** (gain_db / 20))
    found = realign(iso, buf, content[first:last + 1], sr)
    regions, hop = [], sr // 1000
    for k, m in zip(range(first, last + 1), found):
        report["pieces"].append({"section": i, "piece": k, "lag_ms": m["lag_ms"], "r": m["r"], "isolated": m["ok"],
                                 "sub": m["sub"]})
        if m["ok"]:
            regions.append((*full[k], m["lag_ms"] * hop))
            srep["isolated"].append(k)
            continue
        why = (f"isolation doesn't match (r {m['r']:.2f} < {R_MIN})" if m["r"] < R_MIN
               else "isolation ends before the piece does")
        report["fallbacks"].append({"section": i, "name": name, "piece": k, "t": round((start + full[k][0]) / sr, 3),
                                    "lag_ms": m["lag_ms"], "r": m["r"], "reason": why})
        srep["camera_fallback"].append(k)
    srep["gain_db"] = round(gain_db, 2)
    lags = [m["lag_ms"] for m in found if m["ok"]]
    rs = [m["r"] for m in found if m["ok"]]
    log(f"voice: {name} isolation {len(lags)}/{len(found)} pieces"
        + (f", lags {min(lags):+d}..{max(lags):+d} ms, r {min(rs):.2f}-{max(rs):.2f}" if lags else ""))
    if len(srep["camera_fallback"]) > MAX_FALLBACKS:
        raise RuntimeError(f"voice: {name}: {len(srep['camera_fallback'])} pieces fell back to camera audio "
                           f"(at most {MAX_FALLBACKS}): pieces {srep['camera_fallback']}")
    _splice(buf, iso, regions, sr)
