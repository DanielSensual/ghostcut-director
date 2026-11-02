"""Render v1: rebuild a finished GhostCut job from explicit placements, re-check it, then swap it into the app.

    python app/render.py plan                          # what the next render would do (JSON)
    python app/render.py import [--dry-run]            # the original build as <render_root>/r000/timeline.json
    python app/render.py run --version r001 [--dry-run]
    python app/render.py verify r001
    python app/render.py revert

The engine calls plan_summary / start / status / cancel / revert. start() spawns `run` at nice 10 in its own
session. A run writes only into <render_root>/rNNN/, <render_root>/voice_cache/ and app/ui/demo/.next-rNNN/, and
touches the live demo files only after every check has passed (promote; the old files go to .history/ for Revert).
Job paths come from the gitignored app/ui/demo/job.json, never from this file.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import importlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

APP = Path(__file__).resolve().parent
ROOT = APP.parent
DEMO = APP / "ui" / "demo"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

WHISPER = "mlx-community/whisper-large-v3-turbo"
BREW = "/opt/homebrew/bin"
MIN_FREE = 4 << 30
LIVE = ("project.json", "project.js", "poster.jpg", "thumbs", "transcript", "video.mp4")
PLAN_ONLY = ("dirty", "changes", "refusals", "tags")
FILES = {"master": "master.mp4", "phone": "phone.mp4", "picture": "picture.mp4", "voice": "voice.wav",
         "music": "music_bed.wav", "sfx": "picture.sfx.wav", "transcript": "transcript/voice.json"}
FIXES = {"voice_sync": "Voice re-synced to the picture", "frame_exact_joins": "Frame-exact section joins",
         "phone_fps": "Phone copy timing", "music_loop": "Music bed no longer loops into its start"}
_CHILDREN: dict[int, subprocess.Popen] = {}
_IMPORTED: dict[tuple, dict] = {}
_CANCEL = threading.Event()


class Busy(RuntimeError):
    """A render is already running (the engine answers 409)."""


class Refused(ValueError):
    """The render can't start or go on; nothing was encoded or promoted."""


class VerifyFailed(RuntimeError):
    """The new cut failed its checks; its files stay in rNNN for inspection."""


class Cancelled(BaseException):
    """SIGTERM from cancel(). A BaseException so library code can't swallow it."""


# ---------- modules built next to this one ----------

def _mod(name: str):
    return importlib.import_module(name)


def _rp():
    return _mod("render_plan")


def _dtr():
    """The encode steps of scripts/director_test_run.py (it imports its Spark client only inside the Spark steps)."""
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module("director_test_run")


# ---------- small helpers ----------

def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _write_json(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    os.replace(tmp, path)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _versions(root: Path) -> list[int]:
    try:
        return sorted(int(p.name[1:]) for p in root.iterdir() if p.is_dir() and re.fullmatch(r"r\d{3,}", p.name))
    except OSError:
        return []


def _alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:  # 0 / -1 would address a whole process group
        return False
    proc = _CHILDREN.get(pid)
    if proc is not None:
        return proc.poll() is None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return os.waitpid(pid, os.WNOHANG) == (0, 0)  # reaps our own exited child (a zombie still answers kill 0)
    except ChildProcessError:
        return True


def _is_render(pid: int) -> bool:
    out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    return "render.py" in out


def _held(lock: dict | None) -> bool:
    """render.lock belongs to a live render: the render.py it names, or an engine still starting one (no version
    yet). A pid the OS gave to another process after the render died doesn't count."""
    if not lock:
        return False
    pid = lock.get("pid", 0)
    return _alive(pid) and (not lock.get("version") or pid in _CHILDREN or _is_render(pid))


def _unique(path: Path) -> Path:
    n, p = 2, path
    while p.exists() or p.is_symlink():
        p, n = path.with_name(f"{path.name}-{n}"), n + 1
    return p


def _link(src: Path, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def _ids(edits: list) -> list[str]:
    return [e if isinstance(e, str) else e.get("id") for e in edits]


def fingerprints(paths) -> dict[str, list[int] | None]:
    out = {}
    for p in paths:
        try:
            st = os.stat(p)
            out[str(p)] = [st.st_size, st.st_mtime_ns]
        except OSError:
            out[str(p)] = None
    return out


def _read_files(job: dict, base: dict) -> list[str]:
    """Every source a render reads: none of them may change (size, mtime_ns) while it runs."""
    paths = {*job.get("protected", []), job.get("music"), str(job.get("facts") or "")}
    for s in job["sections"]:
        iso = s.get("isolate") or {}
        paths |= {s.get("picture"), s.get("audio"), iso.get("iso"), iso.get("win")}
        if s.get("work"):
            w = Path(s["work"])
            paths |= {str(w / n) for n in ("pieces.json", "broll_plan.json", "broll_map.json", "render/joined.mp4")}
            paths |= {str(p) for p in w.glob("render/piece_*.mp4")}
    paths |= {s.get("picture") for s in base.get("sections", [])}
    return sorted(p for p in paths if p)


def _missing(job: dict, plan: dict | None = None) -> list[str]:
    paths = [job.get("music"), str(job.get("facts") or "")]
    for s in job["sections"]:
        paths += [s.get("picture"), s.get("audio"), s.get("work"), *(s.get("isolate") or {}).values()]
    dirty = (plan or {}).get("dirty", [])
    for s in dirty:
        sec = plan["sections"][s]
        paths += [str(Path(sec["work"]) / "render" / "joined.mp4"), *(b["source"] for b in sec["broll"])]
    # clean sections reuse their base's picture, which may be an earlier render's rNNN/sections/ file
    paths += [sec.get("picture") for s, sec in enumerate((plan or {}).get("sections", [])) if s not in dirty]
    return [p for p in paths if isinstance(p, str) and p and not Path(p).exists()]


def _loudness(path: Path) -> tuple[float, float]:
    """Integrated loudness (LUFS) and true peak (dBTP) of a file's audio."""
    err = subprocess.run(["ffmpeg", "-nostats", "-i", str(path), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"],
                         capture_output=True, text=True).stderr.splitlines()
    i = [ln for ln in err if ln.strip().startswith("I:")][-1].split()[1]
    p = [ln for ln in err if ln.strip().startswith("Peak:")][-1].split()[1]
    return float(i), float(p)


def _whisper_argv(wav: Path, out_dir: Path, name: str) -> list[str]:
    return ["mlx_whisper", str(wav), "--model", WHISPER, "--language", "en", "--output-format", "json",
            "--condition-on-previous-text", "False", "--word-timestamps", "True", "--verbose", "False",
            "--output-dir", str(out_dir), "--output-name", name]


def _transcribe(wav: Path, cache: Path, log=print, popen=None) -> Path:
    """Whisper with word times on the program voice, cached next to it (the voice is the same for every v1 render).
    Each call works in its own temp folder, so two runs on one voice never delete each other's output; popen
    (Run._popen) lets the run stop Whisper when it ends early."""
    out = cache / f"{wav.stem}.transcript.json"
    if out.exists():
        log(f"transcript: cached {out.name}")
        return out
    tmp = Path(tempfile.mkdtemp(dir=cache, prefix=f".whisper-{wav.stem}-"))
    t0 = time.time()
    try:
        proc = (popen or subprocess.Popen)(_whisper_argv(wav, tmp, wav.stem), stdout=subprocess.DEVNULL)
        if proc.wait():
            raise subprocess.CalledProcessError(proc.returncode, proc.args)
        if not out.exists():  # another run on the same voice may have finished first
            os.replace(tmp / f"{wav.stem}.json", out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    log(f"transcript: {time.time() - t0:.0f} s")
    return out


def _call_json(fn, *a, **kw) -> dict:
    """Call a step that prints a JSON summary (and may return one); the summary as a dict."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = fn(*a, **kw)
    text = buf.getvalue()
    if text.strip():
        print(text.rstrip(), flush=True)
    if isinstance(res, dict):
        return res
    for line in reversed(text.splitlines()):
        with contextlib.suppress(ValueError):
            if line.lstrip().startswith("{"):
                return json.loads(line)
    return {}


# ---------- base timeline, plan and estimate ----------

def base_timeline(job: dict, demo: Path, write=None) -> dict:
    """current.json → its timeline.json; otherwise the original build (r000), imported once."""
    cur = _read_json(demo / "current.json")
    if cur:
        return json.loads(Path(cur["timeline"]).read_text())
    f = Path(job["render_root"]) / "r000" / "timeline.json"
    if f.exists():
        return json.loads(f.read_text())
    key = (str(f), os.stat(job["facts"]).st_mtime_ns if job.get("facts") else 0)
    if key not in _IMPORTED:
        _IMPORTED[key] = _rp().import_v16(job)
    tl = copy.deepcopy(_IMPORTED[key])
    if write:
        f.parent.mkdir(parents=True, exist_ok=True)
        _write_json(write(f), tl)
    return tl


def _shown(demo: Path) -> tuple[dict, list[dict]]:
    shown = _read_json(demo / "project.json")
    if shown is None:
        raise FileNotFoundError("No project yet: build one with app/make_demo.py.")
    return shown, _read_json(demo / "edits.json") or []


def _make_plan(base: dict, shown: dict, edits: list[dict]) -> tuple[dict, list[dict]]:
    """make_plan's plan and refusals, plus any validate() problem it did not already refuse."""
    rp = _rp()
    plan, refusals = rp.make_plan(base, shown, edits, shown.get("library", []), shown.get("neverUse", {}))
    seen = " | ".join(str(r.get("reason")) for r in refusals)
    return plan, list(refusals) + [{"edit": None, "op": None, "label": "plan", "reason": p}
                                   for p in rp.validate(plan) if p not in seen]


def _fixes(base: dict, plan: dict) -> list[str]:
    """Base repairs ride along with the first render after the import only."""
    return list(plan.get("fixes") or base.get("fixes") or []) if base.get("version") == "r000" else []


def _fix_label(key: str, plan: dict) -> str:
    label = getattr(_rp(), "fix_label", None)
    return label(plan, key) if label else FIXES.get(key, key.replace("_", " "))


def _steps(plan: dict) -> list[tuple[str, str, float]]:
    """(key, name, estimated seconds): overlay 0.6 s per section second; cards, mix and phone scale with length."""
    dur = plan.get("duration") or sum(s["d"] for s in plan["sections"])
    out = [("plan", "Plan", 4.0)]
    out += [(f"picture:{s}", f"{plan['sections'][s]['name']} picture", 0.6 * plan["sections"][s]["d"] + 5)
            for s in plan.get("dirty", [])]
    return out + [("voice", "Voice track", 20.0), ("cards", "Cards", 0.37 * dur), ("mix", "Mix", 0.053 * dur),
                  ("phone", "Phone copy", 0.15 * dur), ("checks", "Checks", 50.0), ("project", "App project", 45.0),
                  ("promote", "Promote", 2.0)]


def next_version(job: dict) -> str:
    """The next rNNN; numbers are never reused, failed renders included (r000 is the import)."""
    v = _versions(Path(job["render_root"]))
    return f"r{(v[-1] + 1) if v else 1:03d}"


def _mounted(job: dict) -> None:
    if not Path(job["render_root"]).parent.exists():
        raise FileNotFoundError(f"The job drive is not mounted ({Path(job['render_root']).parent}).")


def plan_summary(demo: Path = DEMO) -> dict:
    """What Render would do with the accepted edits, for the Render dialog. Encodes and writes nothing."""
    job = _rp().load_job(demo)
    _mounted(job)
    base = base_timeline(job, demo)
    shown, edits = _shown(demo)
    plan, refusals = _make_plan(base, shown, edits)
    fixes = _fixes(base, plan)
    return {"next_version": next_version(job), "base": base.get("version"), "changes": plan.get("changes", []),
            "fixes": [_fix_label(f, plan) for f in fixes], "fix_keys": fixes, "refusals": refusals,
            "notes": plan.get("notes", []), "estimate_s": round(sum(e for _, _, e in _steps(plan))),
            "dirty": plan.get("dirty", []), "dirty_names": [plan["sections"][s]["name"] for s in plan.get("dirty", [])]}


# ---------- engine side: start / status / cancel / revert ----------

def _lock(job: dict) -> dict | None:
    return _read_json(Path(job["render_root"]) / "render.lock")


def _tail(path: Path, n: int = 12) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 4096))
            return f.read().decode(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _mark(rdir: Path, state: str, message: str) -> None:
    p = _read_json(rdir / "progress.json") or {}
    if p.get("state", "running") == "running":
        _write_json(rdir / "progress.json", {**p, "state": state, "message": message, "updated": _now()})


def status(demo: Path = DEMO) -> dict:
    """The newest render's progress: idle|running|refused|failed|failed_verification|cancelled|promoted|interrupted."""
    idle = {"state": "idle", "version": None, "step": "", "i": 0, "n": 0, "pct": 0, "eta_s": 0, "message": "",
            "log": "", "tail": []}
    try:
        job = _rp().load_job(demo)
        root = Path(job["render_root"])
        lock, cur = _lock(job), _read_json(demo / "current.json") or {}
        vs = [v for v in _versions(root) if v > 0 and (root / f"r{v:03d}" / "progress.json").exists()]
        v = lock.get("version") if lock and lock.get("version") else (f"r{vs[-1]:03d}" if vs else None)
    except (OSError, ValueError, KeyError, TypeError) as e:
        return {**idle, "message": str(e)}
    out = {**idle, "current": cur.get("version"), "previous": cur.get("previous")}
    if not v:
        return out
    rdir = root / v
    out.update({**(_read_json(rdir / "progress.json") or {"state": "running", "step": "Starting"}), "version": v,
                "log": str(rdir / "render.log"), "tail": _tail(rdir / "render.log")})
    if out["state"] == "running" and not (lock and lock.get("version") == v and _held(lock)):
        out.update(state="interrupted", message="The render stopped before it finished; the current cut stays.")
    if out["state"] == "promoted" and cur.get("version") != v:
        out.update(state="idle", message=f"{v} was reverted; showing {cur.get('version') or 'the original cut'}.")
    return out


def start(demo: Path = DEMO) -> dict:
    """Preflight under render.lock, snapshot the edits, spawn the render; {version, pid}."""
    job = _rp().load_job(demo)
    root = Path(job["render_root"])
    _mounted(job)
    root.mkdir(exist_ok=True)
    lock = root / "render.lock"
    held = _read_json(lock)
    with contextlib.suppress(OSError):  # unreadable for 30 s: the engine died between creating and writing it
        if held is None and time.time() - lock.stat().st_mtime > 30:
            lock.unlink()
    if _held(held):
        raise Busy(f"{held['version']} is rendering." if held.get("version") else "Another render is starting.")
    if held:
        if held.get("version"):
            _mark(root / held["version"], "interrupted", "The render stopped before it finished.")
        lock.unlink(missing_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise Busy("Another render is starting.") from None
    os.write(fd, json.dumps({"pid": os.getpid(), "version": None, "started": _now()}).encode())
    os.close(fd)
    rdir = None
    try:
        if shutil.disk_usage(root).free < MIN_FREE:
            raise Refused(f"Less than {MIN_FREE >> 30} GB free on the render drive.")
        base = base_timeline(job, demo)
        shown, edits = _shown(demo)
        plan, refusals = _make_plan(base, shown, edits)
        if refusals:
            raise Refused("Can't render: " + "; ".join(f"{r.get('edit') or r.get('label')} ({r['reason']})"
                                                       for r in refusals))
        if not plan.get("changes") and not _fixes(base, plan):
            raise Refused("Nothing to render: no accepted edits.")
        missing = _missing(job, plan)
        if missing:
            raise FileNotFoundError("Missing source files: " + ", ".join(missing[:5]))
        version, steps = next_version(job), _steps(plan)
        rdir = root / version
        rdir.mkdir()
        _write_json(rdir / "edits_applied.json", edits)
        _write_json(rdir / "progress.json", {"state": "running", "version": version, "step": "Starting", "i": 0,
                                             "n": len(steps), "pct": 0, "eta_s": round(sum(e for _, _, e in steps)),
                                             "message": "", "steps": [s[1] for s in steps], "started": _now()})
        env = {**os.environ, "PATH": f"{BREW}:{os.environ.get('PATH', '')}"}
        with open(rdir / "render.log", "ab") as logf:
            proc = subprocess.Popen(["nice", "-n", "10", sys.executable, str(APP / "render.py"), "run", "--version",
                                     version, "--demo", str(demo)], cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                                    stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
        _CHILDREN[proc.pid] = proc
        _write_json(lock, {"pid": proc.pid, "version": version, "started": _now()})
    except BaseException as e:
        if rdir is not None:
            _mark(rdir, "failed", f"The render could not start: {e}")
        if (_read_json(lock) or {}).get("pid") == os.getpid():
            lock.unlink(missing_ok=True)
        raise
    return {"version": version, "pid": proc.pid}


def cancel(demo: Path = DEMO) -> dict:
    """SIGTERM the render's process group (ffmpeg and Whisper included), SIGKILL after 5 s."""
    job = _rp().load_job(demo)
    root = Path(job["render_root"])
    held = _lock(job)
    if not held or not held.get("pid") or not _alive(held["pid"]) or not _is_render(held["pid"]):
        return status(demo)
    pid = held["pid"]
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    for _ in range(50):
        if not _alive(pid):
            break
        time.sleep(0.1)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        time.sleep(0.3)
    if not _alive(pid) and (_lock(job) or {}).get("pid") == pid:
        if held.get("version"):
            _mark(root / held["version"], "cancelled", "Cancelled; the current cut stays.")
        (root / "render.lock").unlink(missing_ok=True)
    return status(demo)


def revert(demo: Path = DEMO) -> dict:
    """Put the previous cut back (files, edits.json, current.json); the reverted one moves to .history/."""
    job = _rp().load_job(demo)
    held = _lock(job)
    if _held(held):
        raise Busy("A render is running.")
    cur = _read_json(demo / "current.json")
    if not cur:
        raise ValueError("Nothing to revert: this is the original cut.")
    n = len(_read_json(demo / "edits.json") or [])
    if n:  # they belong to this version: Revert would set them aside in .history/ with no way back in the app
        raise Busy(f"{n} accepted change{' is' if n == 1 else 's are'} waiting to render on {cur['version']}: "
                   f"undo or render {'it' if n == 1 else 'them'} first")
    hist = demo / ".history" / cur["previous"]
    if not hist.is_dir():
        raise FileNotFoundError(f"The previous cut is gone (.history/{cur['previous']}).")
    back = _unique(demo / ".history" / cur["version"])
    back.mkdir(parents=True)
    for name in LIVE + ("edits.json",):
        if (demo / name).exists() or (demo / name).is_symlink():
            os.replace(demo / name, back / name)
    _write_json(back / "current.json", cur)
    for name in LIVE + ("edits.json",):
        if (hist / name).exists() or (hist / name).is_symlink():
            os.replace(hist / name, demo / name)
    if (hist / "current.json").exists():
        os.replace(hist / "current.json", demo / "current.json")
    else:
        (demo / "current.json").unlink()
    with contextlib.suppress(OSError):
        hist.rmdir()
    restored = (_read_json(demo / "current.json") or {}).get("version") or cur["previous"]
    return {"version": restored, "reverted": cur["version"]}


# ---------- the render itself (child process) ----------

def _on_term(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    _CANCEL.set()
    raise Cancelled()


class Run:
    def __init__(self, demo: Path, version: str, job: dict | None = None):
        if not re.fullmatch(r"r\d{3,}", version) or version == "r000":
            raise ValueError(f"bad version {version!r} (rNNN, r001 or later)")
        self.demo, self.version = Path(demo), version
        self.job = job or _rp().load_job(self.demo)
        self.root = Path(self.job["render_root"])
        self.rdir, self.nxt = self.root / version, self.demo / f".next-{version}"
        self.allowed = [self.rdir, self.root / "voice_cache", self.nxt, self.demo / ".history",
                        self.demo / "current.json", self.root / "render.lock", self.root / "r000" / "timeline.json"]
        self.steps: list[tuple[str, str, float]] = [("plan", "Plan", 4.0)]
        self.i, self.t0, self.step_t0 = 0, time.time(), time.time()
        self.state, self.message, self.owned, self.checks = "running", "", False, []
        self._plock, self._stop = threading.Lock(), threading.Event()
        self.whisper: subprocess.Popen | None = None
        self._wlock = threading.Lock()

    # every path this run writes goes through w(): only its own folders (and the live demo files while promoting)
    def w(self, path) -> Path:
        p = Path(path)
        real = p.parent.resolve() / p.name
        if not any(real == a or a in real.parents for a in (Path(x).parent.resolve() / Path(x).name
                                                            for x in self.allowed)):
            raise PermissionError(f"render write outside its folders: {real}")
        return p

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        if self.owned:
            with open(self.rdir / "render.log", "a") as f:
                f.write(line + "\n")
        if sys.stdout.isatty() or not self.owned:
            print(line, flush=True)

    def snapshot(self) -> dict:
        n, est = len(self.steps), [e for _, _, e in self.steps]
        i = min(self.i, n - 1)
        cur, el = est[i], time.time() - self.step_t0
        total = sum(est) or 1.0
        frac = min(el / cur, 0.95) if cur else 0.0
        done = self.state == "promoted"
        return {"state": self.state, "version": self.version, "step": self.steps[i][1], "i": i + 1, "n": n,
                "pct": 100.0 if done else round(100 * (sum(est[:i]) + frac * cur) / total, 1),
                "eta_s": 0 if self.state != "running" else round(sum(est[i + 1:]) + max(cur - el, 0)),
                "message": self.message, "steps": [s[1] for s in self.steps], "pid": os.getpid(),
                "elapsed_s": round(time.time() - self.t0, 1), "updated": _now()}

    def progress(self) -> None:
        if self.owned:
            with self._plock:
                _write_json(self.w(self.rdir / "progress.json"), self.snapshot())

    def step(self, key: str) -> None:
        self.i = next(i for i, s in enumerate(self.steps) if s[0] == key)
        self.step_t0 = time.time()
        self.log(f"step {self.i + 1}/{len(self.steps)}: {self.steps[self.i][1]}")
        self.progress()

    def _tick(self) -> None:
        while not self._stop.wait(2.0):
            with contextlib.suppress(OSError):
                self.progress()

    def _popen(self, argv: list[str], **kw) -> subprocess.Popen:
        """Start Whisper for _transcribe, unless the run is already ending (run() stops the one it started)."""
        with self._wlock:
            if self._stop.is_set():
                raise RuntimeError("The render ended before Whisper started.")
            self.whisper = subprocess.Popen(argv, **kw)
            return self.whisper

    def _stop_whisper(self) -> None:
        """A step that fails or is refused leaves Whisper running in the pool: end it before render.lock goes, so
        the next render never runs next to it."""
        with self._wlock:
            proc = self.whisper
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _own(self) -> None:
        lock = _read_json(self.root / "render.lock")
        if lock and lock.get("version") != self.version and _held(lock):
            raise Busy(f"{lock.get('version')} is rendering.")
        if self.rdir.exists():
            p = _read_json(self.rdir / "progress.json") or {}
            if p.get("state", "running") != "running":
                raise Refused(f"{self.version} already ran ({p['state']}); versions are never reused.")
        else:
            self.root.mkdir(exist_ok=True)
            self.rdir.mkdir()
            _write_json(self.rdir / "edits_applied.json", _read_json(self.demo / "edits.json") or [])
        self.owned = True
        _write_json(self.w(self.root / "render.lock"), {"pid": os.getpid(), "version": self.version, "started": _now()})

    def _release(self) -> None:
        lock = self.root / "render.lock"
        if (_read_json(lock) or {}).get("pid") == os.getpid():
            lock.unlink(missing_ok=True)

    def run(self) -> dict:
        old = signal.signal(signal.SIGTERM, _on_term)
        _CANCEL.clear()
        pool = ThreadPoolExecutor(1)
        try:
            try:
                self._own()
                threading.Thread(target=self._tick, daemon=True).start()
                self.progress()
                self._pipeline(pool)
            except Refused as e:
                self.state, self.message = "refused", str(e)
            except VerifyFailed as e:
                self.state, self.message = "failed_verification", str(e)
            except Busy as e:
                self.state, self.message = "refused", str(e)
            except Exception as e:  # noqa: BLE001 -- any failure leaves the current cut in place
                self.state, self.message = "failed", f"{type(e).__name__}: {e}"
                self.log(traceback.format_exc())
            finally:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
        except Cancelled:  # _on_term ignores later SIGTERMs, so nothing below can be interrupted
            pass
        except KeyboardInterrupt:
            _CANCEL.set()
        if _CANCEL.is_set() and self.state != "promoted":
            self.state, self.message = "cancelled", "Cancelled; the current cut stays."
        self._stop.set()  # from here _popen starts nothing
        self._stop_whisper()
        pool.shutdown(wait=True, cancel_futures=True)  # quick now: Whisper has exited
        if self.owned:
            self.log(f"{self.state}: {self.message}")
            self.progress()
            self._release()
        signal.signal(signal.SIGTERM, old)
        return self.snapshot()

    def _pipeline(self, pool: ThreadPoolExecutor) -> None:
        dtr, rp, rv, rverify, md = _dtr(), _rp(), _mod("render_voice"), _mod("render_verify"), _mod("make_demo")
        base = base_timeline(self.job, self.demo, write=self.w)
        shown = _read_json(self.demo / "project.json") or {}
        edits = _read_json(self.rdir / "edits_applied.json") or []
        plan, refusals = _make_plan(base, shown, edits)
        if refusals:
            raise Refused("; ".join(f"{r.get('label') or r.get('edit')}: {r['reason']}" for r in refusals))
        fixes = _fixes(base, plan)
        if not plan.get("changes") and not fixes:
            raise Refused("Nothing to render: no accepted edits.")
        missing = _missing(self.job, plan)
        if missing:
            raise Refused("Missing source files: " + ", ".join(missing[:5]))
        plan.update(version=self.version, parent=base.get("version"))
        ids = _ids(plan.get("edits_applied") or edits)
        self.fp = fingerprints(_read_files(self.job, base))
        _write_json(self.w(self.rdir / "fingerprints.json"), self.fp)
        _write_json(self.w(self.rdir / "plan.json"), plan)
        _write_json(self.w(self.rdir / "cards.json"), rp.plan_cards(plan))
        self.steps = _steps(plan)
        self.log(f"plan: base {base.get('version')}, dirty {plan.get('dirty')}, {len(plan.get('changes', []))} "
                 f"changes, fixes {fixes}")
        self.progress()
        secs, speed = plan["sections"], plan.get("speed", 0.5)

        for s in plan.get("dirty", []):  # video only, over the same talk render the original pass used
            sec = secs[s]
            self.step(f"picture:{s}")
            joined = Path(sec["work"]) / "render" / "joined.mp4"
            ov = [{"source": b["source"], "in": b["in"], "t": b["r"], "d": b["d"]} for b in sec["broll"]]
            out = self.w(self.rdir / "sections" / f"{s}_{_slug(sec['name'])}.mp4")
            out.parent.mkdir(exist_ok=True)
            dtr.render_overlays(joined, ov, out, speed=speed, probe=1.0)
            dtr.render_overlays(joined, ov, out, speed=speed)
            n = rp.probe_frames(out)
            if n != sec["frames"]:
                raise RuntimeError(f"{sec['name']} picture has {n} frames, expected {sec['frames']}.")
            sec["picture"] = str(out)
        for s, sec in enumerate(secs):  # rendered by this version; clean sections keep the file their base used
            sec["picture_rendered"] = s in plan.get("dirty", [])

        self.step("voice")
        cache = self.w(self.root / "voice_cache")
        cache.mkdir(exist_ok=True)
        wav, report = rv.build_voice(plan, self.job, cache, log=self.log)
        self.w(wav)
        voice = self.w(self.rdir / "voice.wav")
        _link(Path(wav), voice)
        words = pool.submit(_transcribe, Path(wav), cache, self.log, popen=self._popen)  # GPU, while x264 runs

        self.step("cards")
        pics, spec = [Path(sec["picture"]) for sec in secs], self.rdir / "cards.json"
        picture = self.w(self.rdir / "picture.mp4")
        dtr.facts(self.rdir, pics, spec, picture, probe=2.0)
        sfx = self.w(Path(dtr.facts(self.rdir, pics, spec, picture) or picture.with_suffix(".sfx.wav")))

        self.step("mix")
        master = self.w(self.rdir / "master.mp4")
        intro = secs[1]["t"] if len(secs) > 1 and secs[0].get("kind") == "intro" else 0.0
        res = _call_json(dtr.mix, self.rdir, picture, Path(self.job["music"]), master, intro=intro, sfx=sfx,
                         voice=voice)
        lufs, tp = (res["lufs"], res["tp"]) if res.get("lufs") is not None and res.get("tp") is not None \
            else _loudness(master)
        mix = {"music_gain_db": res.get("music_gain_db"), "lufs": lufs, "tp": tp}
        self.log(f"mix: {mix}")

        self.step("phone")
        phone = self.w(self.rdir / "phone.mp4")
        dtr.phone_copy(master, phone, self.w(self.rdir / "phone2p"))

        self.step("checks")
        tdst = self.w(self.rdir / "transcript" / "voice.json")
        tdst.parent.mkdir(exist_ok=True)
        shutil.copyfile(words.result(), tdst)
        tl = self._timeline(plan, base, fixes, mix, report, ids)
        _write_json(self.w(self.rdir / "timeline.json"), tl)
        prev = self.demo / "transcript" / "voice.json"
        result = rverify.verify(self.rdir, tl, plan, self.job, prev if prev.exists() else None, self.fp, log=self.log)
        self.checks = result.get("checks", [])
        changed = [p for p, v in fingerprints(self.fp).items() if v != self.fp[p]]
        if changed:
            raise VerifyFailed("Source files changed during the render: " + ", ".join(Path(p).name for p in changed))
        fails = [c["name"] for c in self.checks if c.get("status") == "fail"]
        if fails or not result.get("ok"):
            raise VerifyFailed("Failed checks: " + (", ".join(fails) or "verify not ok"))

        self.step("project")
        nxt = self.w(self.nxt)
        if nxt.exists():
            shutil.rmtree(nxt)
        nxt.mkdir()
        # carry = the project the app showed: its keys, library and findings (rebased by its own section starts)
        proj = md.project_from_timeline(tl, copy.deepcopy(shown), tdst, phone, voice, self.rdir / "music_bed.wav",
                                        nxt) or _read_json(nxt / "project.json") or {}
        if not ((nxt / "video.mp4").exists() or (nxt / "video.mp4").is_symlink()):
            (nxt / "video.mp4").symlink_to(phone.resolve())
        if not (nxt / "transcript" / "voice.json").exists():
            (nxt / "transcript").mkdir(exist_ok=True)
            shutil.copyfile(tdst, nxt / "transcript" / "voice.json")
        if proj.get("version") != self.version:
            raise RuntimeError(f"The new project says version {proj.get('version')!r}, not {self.version}.")

        self.step("promote")
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a half-swapped app is worse than a late cancel
        self._promote(shown, ids)
        self.state = "promoted"
        self.message = f"{self.version} ready · {sum(c.get('status') == 'pass' for c in self.checks)} checks passed"
        self._after_promote(rverify, tl, ids)

    def _timeline(self, plan: dict, base: dict, fixes: list[str], mix: dict, report: dict, ids: list[str]) -> dict:
        """timeline.json: the plan without its plan-only keys, plus what this run measured. notes (warnings and
        add_note findings) stay in it: the app project turns them into Review findings."""
        tl = {k: copy.deepcopy(v) for k, v in plan.items() if k not in PLAN_ONLY}
        tl.update(schema=plan.get("schema", "ghostcut.timeline/1"), version=self.version, parent=base.get("version"),
                  created=_now(), files=dict(FILES), mix=mix, edits_applied=ids, fixes=fixes, verify="verify.json")
        fallbacks = [f for f in report.get("fallbacks", []) if isinstance(f, dict)]
        for s, sec in enumerate(tl["sections"]):
            pieces = [p for p in report.get("pieces", []) if p.get("section") == s]
            lo, hi = ((sec.get("isolate") or self._job_sec(s).get("isolate") or {}).get("pieces") or [0, -1])[:2]
            sec["isolated"] = [{"piece": p["piece"], "lag_ms": p.get("lag_ms"), "r": p.get("r")}
                               for p in pieces if p.get("isolated")]
            sec["camera_fallback"] = sorted({p["piece"] for p in pieces if lo <= p["piece"] <= hi and
                                             not p.get("isolated")} |
                                            {f["piece"] for f in fallbacks if f.get("section") == s and "piece" in f})
            sec["audio_from"] = sec.get("audio_from") or sec.get("audio") or self._job_sec(s).get("audio")
        return tl

    def _job_sec(self, s: int) -> dict:
        return self.job["sections"][s] if s < len(self.job["sections"]) else {}

    def _promote(self, shown: dict, ids: list[str]) -> None:
        """live → .history/<prev>, .next-rNNN → live, edits.json reset, current.json last (the commit point)."""
        demo, cur = self.demo, _read_json(self.demo / "current.json")
        self.allowed += [demo / n for n in LIVE + ("edits.json",)]
        hist = self.w(_unique(demo / ".history" / ((cur or {}).get("version") or shown.get("version") or "legacy")))
        hist.mkdir(parents=True)
        pending = _read_json(demo / "edits.json") or []
        for name in LIVE + ("edits.json",):
            if (demo / name).exists() or (demo / name).is_symlink():
                os.replace(self.w(demo / name), hist / name)
        if cur:
            _write_json(hist / "current.json", cur)
        for name in LIVE:
            if (self.nxt / name).exists() or (self.nxt / name).is_symlink():
                os.replace(self.nxt / name, self.w(demo / name))
        _write_json(self.w(demo / "edits.json"), [e for e in pending if e.get("id") not in ids])
        with contextlib.suppress(OSError):
            self.nxt.rmdir()
        _write_json(self.w(demo / "current.json"), {"version": self.version, "dir": str(self.rdir),
                                                    "timeline": str(self.rdir / "timeline.json"),
                                                    "previous": hist.name})
        self.log(f"promoted {self.version}; previous cut in .history/{hist.name}")

    def _after_promote(self, rverify, tl: dict, ids: list[str]) -> None:
        """edits.json is [], the live project is this version, nothing applied is queued again. Logged, not undone."""
        try:
            if hasattr(rverify, "verify_promoted"):
                bad = [c["name"] for c in rverify.verify_promoted(self.demo, tl, self.rdir, log=self.log)["checks"]
                       if c.get("status") == "fail"]
            else:
                edits = _read_json(self.demo / "edits.json")
                proj = _read_json(self.demo / "project.json") or {}
                bad = [n for n, ok in (("promoted_edits", edits == []),
                                       ("promoted_version", proj.get("version") == self.version),
                                       ("promoted_no_reapply", not any(e.get("id") in ids for e in edits or [])))
                       if not ok]
        except Exception as e:  # noqa: BLE001 -- the new cut is live either way; say what could not be checked
            bad = [f"after-promote check: {type(e).__name__}: {e}"]
        if bad:
            self.message += " · after promote: " + ", ".join(bad)
            self.log("after promote failed: " + ", ".join(bad))


def run(demo: Path, version: str) -> dict:
    """Render `version` end to end (the child process started by start()); its final progress."""
    return Run(demo, version).run()


# ---------- dry run ----------

@contextlib.contextmanager
def _print_argv(durations: dict[str, float], default: float):
    """Print every ffmpeg command instead of running it; ffprobe of files that exist still runs (read-only)."""
    saved = {k: getattr(subprocess, k) for k in ("run", "check_output", "check_call", "call")}
    real = saved["run"]

    def fake(argv, *a, **kw):
        argv = [str(x) for x in argv]
        files = [x for x in argv[1:] if "/" in x]
        text = kw.get("text") or kw.get("universal_newlines")
        if Path(argv[0]).name == "ffprobe":
            if files and all(Path(f).exists() for f in files):
                return real(argv, *a, **{**kw, "check": False})
            out, err = f"{next((durations[f] for f in files if f in durations), default)}\n", ""
        else:
            print(shlex.join(argv), flush=True)
            out, err = "", "    I:         -14.0 LUFS\n    Peak:        -1.0 dBFS\n"
        return subprocess.CompletedProcess(argv, 0, out if text else out.encode(), err if text else err.encode())

    subprocess.run = fake
    subprocess.check_output = lambda argv, *a, **kw: fake(argv, *a, **kw).stdout
    subprocess.check_call = subprocess.call = lambda argv, *a, **kw: fake(argv, *a, **kw).returncode
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(subprocess, k, v)


def dry_run(demo: Path, version: str | None = None) -> int:
    """Plan, then print every encode command the run would make. Writes nothing outside a temp folder."""
    job = _rp().load_job(demo)
    version = version or next_version(job)
    base = base_timeline(job, demo)
    shown, edits = _shown(demo)
    plan, refusals = _make_plan(base, shown, edits)
    print(json.dumps({"version": version, "base": base.get("version"), "dirty": plan.get("dirty", []),
                      "changes": plan.get("changes", []), "refusals": refusals, "fixes": _fixes(base, plan),
                      "missing": _missing(job, plan)}, indent=1))
    if refusals:
        return 1
    dtr = _dtr()
    work = Path(tempfile.mkdtemp(prefix=f"ghostcut-dry-{version}-"))
    print(f"# dry run: {work} stands for {Path(job['render_root']) / version}")
    secs, durs = plan["sections"], {}
    try:
        for s in plan.get("dirty", []):
            sec = secs[s]
            joined = Path(sec["work"]) / "render" / "joined.mp4"
            ov = [{"source": b["source"], "in": b["in"], "t": b["r"], "d": b["d"]} for b in sec["broll"]]
            out = work / "sections" / f"{s}_{_slug(sec['name'])}.mp4"
            print(f"# {sec['name']} picture: smoke run, then the full encode")
            for probe in (1.0, None):
                print(shlex.join(str(x) for x in dtr.render_overlays(joined, ov, out, speed=plan.get("speed", 0.5),
                                                                      probe=probe, dry_run=True)))
            sec["picture"], durs[str(out)] = str(out), sec["d"]
        print(f"# voice: build_voice → {Path(job['render_root']) / 'voice_cache'} (cached across renders)")
        print("# transcript: " + shlex.join(_whisper_argv(work / "voice.wav", work, "voice")))
        spec = work / "cards.json"
        _write_json(spec, _rp().plan_cards(plan))
        pics = [Path(sec["picture"]) for sec in secs]
        intro = secs[1]["t"] if len(secs) > 1 and secs[0].get("kind") == "intro" else 0.0
        with _print_argv(durs, plan.get("duration") or 0.0):
            print("# cards: smoke run, then the full encode")
            dtr.facts(work, pics, spec, work / "picture.mp4", probe=2.0)
            dtr.facts(work, pics, spec, work / "picture.mp4")
            print("# mix")
            _call_json(dtr.mix, work, work / "picture.mp4", Path(job["music"]), work / "master.mp4", intro=intro,
                       sfx=work / "picture.sfx.wav", voice=work / "voice.wav")
            print("# phone copy")
            dtr.phone_copy(work / "master.mp4", work / "phone.mp4", work / "phone2p")
        print("# checks, app project and promote are skipped in a dry run")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


# ---------- CLI ----------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="GhostCut Render v1")
    ap.add_argument("cmd", choices=["plan", "import", "run", "verify", "revert", "status"])
    ap.add_argument("target", nargs="?", help="verify: the version (rNNN)")
    ap.add_argument("--version", help="run: the version to render (default: the next one)")
    ap.add_argument("--dry-run", action="store_true", help="print every encode command, encode nothing")
    ap.add_argument("--demo", type=Path, default=DEMO, help="the app's demo folder (job.json, project.json)")
    a = ap.parse_args(argv)
    demo = a.demo
    if a.cmd == "plan":
        print(json.dumps(plan_summary(demo), indent=1))
        return 0
    if a.cmd == "status":
        print(json.dumps(status(demo), indent=1))
        return 0
    if a.cmd == "revert":
        print(json.dumps(revert(demo)))
        return 0
    job = _rp().load_job(demo)
    root = Path(job["render_root"])
    if a.cmd == "import":
        f = root / "r000" / "timeline.json"
        existed = f.exists()
        tl = json.loads(f.read_text()) if existed else _rp().import_v16(job)
        if not existed and not a.dry_run:  # written once; later imports read it back
            f.parent.mkdir(parents=True, exist_ok=True)
            _write_json(f, tl)
        print(json.dumps({"timeline": str(f), "existed": existed, "written": not existed and not a.dry_run,
                          "frames": tl.get("frames"),
                          "duration": tl.get("duration"), "cards": len(tl.get("cards", [])),
                          "sections": [{k: s.get(k) for k in ("name", "start_frame", "frames", "t", "d")} |
                                       {"broll": len(s.get("broll", [])), "pieces": len(s.get("pieces", []))}
                                       for s in tl.get("sections", [])]}, indent=1))
        return 0
    if a.cmd == "run":
        if a.dry_run:
            return dry_run(demo, a.version)
        res = run(demo, a.version or next_version(job))
        print(json.dumps({k: res[k] for k in ("state", "version", "message")}))
        return 0 if res["state"] == "promoted" else 1
    v = a.target or a.version
    if not v or not re.fullmatch(r"r\d{3,}", v):
        ap.error("verify needs a version, e.g. verify r001")
    rdir, cur = root / v, _read_json(demo / "current.json") or {}
    prev = demo / ".history" / cur["previous"] / "transcript" / "voice.json" if cur.get("version") == v else \
        demo / "transcript" / "voice.json"
    res = _mod("render_verify").verify(rdir, json.loads((rdir / "timeline.json").read_text()),
                                       json.loads((rdir / "plan.json").read_text()), job,
                                       prev if prev.exists() else None, _read_json(rdir / "fingerprints.json") or {})
    print(json.dumps(res, indent=1))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
