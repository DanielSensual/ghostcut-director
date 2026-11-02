"""GhostCut Director engine: serves the app and its JSON API on 127.0.0.1.

    python app/engine.py [port]     →  http://127.0.0.1:7791/

GET  /api/project            the cut with accepted edits applied, the edit list and the render state
POST /api/muse               {text, at} → Spark's proposal, every operation with its checks
POST /api/accept             {id, pick?} → apply a proposal's unblocked operations (409 while rendering)
POST /api/undo               {edit?} → remove that edit (default: the last one; 409 while rendering)
GET  /api/frame?tag=&t=      a still from a library shot (only tags in the project's library)
GET  /api/render/plan        what the next render would do: changes, fixes, refusals, estimate
POST /api/render             render the accepted edits into the next version (409 while one runs)
GET  /api/render             its progress {state, version, step, i, n, pct, eta_s, message, log}
GET  /api/render/log         its log, as text
POST /api/render/cancel      stop it (the current cut stays)
POST /api/render/revert      go back to the previous version (409 while rendering)

Run with the Python that has the Meta client installed (guestlist.meta) for Muse; everything else works without it.
A fresh clone has no project (app/ui/demo/ is gitignored): build one with app/make_demo.py.
Render (app/render.py) runs as its own process; a watcher thread here reloads the cut when it promotes a version, or
when current.json moves any other way (`python app/render.py revert`).

Only the app's own files are served (app/ui/, docs/brand/), only to a 127.0.0.1/localhost Host header, and the
API takes JSON bodies only, so other pages in a browser can't read files or spend Meta credits through it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import muse  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "app" / "ui" / "demo"
STATIC = ("/app/ui/", "/docs/brand/")
NO_PROJECT = "No project yet: build one with app/make_demo.py from a finished Director job (see docs/APP-DESIGN.md)."
NO_MUSE = "Muse needs the Meta client: run the engine with the venv that has guestlist.meta installed."
BUSY = "Rendering — {} after it finishes"
LOG_TAIL = 256_000


class Busy(Exception):
    """A render is running: the edit list stays frozen until it ends (HTTP 409)."""


class Unavailable(Exception):
    """app/render.py can't be loaded here (HTTP 503)."""


def _brief(st: dict) -> dict:
    out = {"state": st.get("state") or "idle", "version": st.get("version")}
    if out["state"] == "unavailable":
        out["message"] = st.get("message", "")
    return out


class Engine:
    def __init__(self, demo: Path = DEMO, render=None):
        self.demo = demo
        self.edits_file = demo / "edits.json"
        self.spend_file = demo / "muse_spend.json"
        self.spend = json.loads(self.spend_file.read_text()) if self.spend_file.exists() else {"usd": 0.0, "calls": 0}
        self.lock = threading.Lock()
        self.gen = 0
        self._render = render
        self._stop = threading.Event()
        self.render_state: dict = {"state": "idle", "version": None}
        self._load()
        self.render_state = _brief(self._status())

    def _load(self) -> None:
        """Read the cut and its edit list (again after a render promotes or reverts). Edits the current version
        already contains are dropped, and proposals made on the previous cut are forgotten."""
        self.loaded = self._current()  # first: current.json is written last, so a cut swapped mid-read loads again
        p = self.demo / "project.json"
        self.base = json.loads(p.read_text()) if p.exists() else None
        self.edits = json.loads(self.edits_file.read_text()) if self.edits_file.exists() else []
        done = self._applied()
        if any(e["id"] in done for e in self.edits):
            self.edits = [e for e in self.edits if e["id"] not in done]
            self._save()
        self.proposals: dict[str, dict] = {}
        self.gen += 1

    def _current(self) -> str | None:
        """The version current.json points at (None: the original cut), as render.status() reports it in 'current'."""
        try:
            return json.loads((self.demo / "current.json").read_text()).get("version")
        except (OSError, ValueError, AttributeError):
            return None

    def _applied(self) -> set[str]:
        """Edit ids the current version was rendered from (current.json → its timeline.json)."""
        done = set((self.base or {}).get("edits_applied") or [])
        try:
            cur = json.loads((self.demo / "current.json").read_text())
            tl = Path(cur.get("timeline") or Path(cur["dir"]) / "timeline.json")
            tl = tl if tl.is_absolute() else self.demo / tl
            done |= set(json.loads(tl.read_text()).get("edits_applied") or [])
        except (OSError, ValueError, KeyError, TypeError):  # legacy project, or its drive isn't mounted
            pass
        return done

    def project(self) -> dict:
        if self.base is None:
            raise FileNotFoundError(NO_PROJECT)
        p = muse.apply(self.base, self.edits)
        return {"project": p, "edits": [{k: e[k] for k in ("id", "request", "labels", "usd")} for e in self.edits],
                "engine": {"live": True, "museSpent": round(self.spend["usd"], 4), "museCalls": self.spend["calls"],
                           "render": dict(self.render_state)}}

    def ask(self, text: str, at: float) -> dict:
        if self.base is None:
            raise FileNotFoundError(NO_PROJECT)
        gen, current = self.gen, muse.apply(self.base, self.edits)
        data, meta = muse.ask(current, text, at)
        if not meta["cached"]:
            with self.lock:
                self.spend = {"usd": self.spend["usd"] + meta["usd"], "calls": self.spend["calls"] + 1}
                self.spend_file.write_text(json.dumps(self.spend))
        ops = muse.validate(current, data.get("ops", []))
        pid = uuid.uuid4().hex[:8]
        self.proposals[pid] = {"request": text, "ops": ops, "usd": meta["usd"], "gen": gen}
        return {"id": pid, "reply": data.get("reply", ""), "answers": data.get("answers", []), "ops": ops, **meta}

    def accept(self, pid: str, pick: list[int] | None) -> dict:
        self.poll_render()  # a version promoted since the proposal was made → its ids belong to the old cut
        self._idle("accept")  # answered first; checked again under the lock
        prop = self.proposals.get(pid)
        if not prop:
            raise KeyError("That proposal is gone (the engine restarted?). Ask again.")
        if prop.get("gen", self.gen) != self.gen:
            raise KeyError("That proposal was made on the previous version of the cut. Ask again.")
        chosen = [op for i, op in enumerate(prop["ops"]) if not op["blocked"] and (pick is None or i in pick)]
        if not chosen:
            raise ValueError("Nothing to apply: every operation was blocked or unpicked.")
        with self.lock:
            self._idle("accept")
            clean = [{k: op[k] for k in muse.OP_SCHEMA["required"]} for op in chosen]
            self.edits.append({"id": f"e{int(time.time() * 1000) % 10**9}", "request": prop["request"], "ops": clean,
                               "labels": [op["label"] for op in chosen], "usd": prop["usd"]})
            self._save()
        self.proposals.pop(pid, None)
        return self.project()

    def undo(self, edit: str | None) -> dict:
        self.poll_render()
        with self.lock:
            self._idle("undo")
            if self.edits:
                idx = next((i for i, e in enumerate(self.edits) if e["id"] == edit), len(self.edits) - 1)
                self.edits.pop(idx)
                self._save()
        return self.project()

    def _save(self) -> None:
        tmp = self.edits_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.edits, indent=1))
        os.replace(tmp, self.edits_file)

    def frame(self, tag: str, t: float) -> Path:
        e = next((x for x in (self.base or {}).get("library", []) if x["tag"] == tag), None)
        if not e or not Path(e["file"]).exists():
            raise KeyError(f"no library shot {tag!r}")
        t = max(0.0, min(t, (e.get("dur") or t + 1) - 0.1))
        out = self.demo / "frames" / f"{re.sub(r'[^A-Za-z0-9_-]', '', tag)}_{t:.3f}.jpg"  # 8.75 and 8.80 differ
        if not out.exists():
            out.parent.mkdir(exist_ok=True)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", e["file"], "-frames:v", "1",
                            "-vf", "scale=384:-2", "-q:v", "4", str(out)], check=True, timeout=60)
        return out

    # ---------- render (app/render.py runs it as a child process; this side starts, follows and gates it) ----------

    def _r(self):
        """app/render.py, imported on first use so the editor still works where Render can't run."""
        if self._render is None:
            try:
                import render
            except Exception as e:
                raise Unavailable(f"Render isn't available: {e}") from e
            self._render = render
        return self._render

    def _status(self) -> dict:
        try:
            return self._r().status(self.demo)
        except Exception as e:  # no job.json, drive unplugged, render module missing
            if self.render_state.get("state") == "running":  # fail closed: edits stay frozen
                return dict(self.render_state)
            return {"state": "unavailable", "version": None, "message": str(e)}

    def _sync(self, st: dict) -> None:
        brief = _brief(st)
        with self.lock:
            prev = (self.render_state.get("state"), self.render_state.get("version"))
            self.render_state = brief
            # current.json moved (a promotion, or `render.py revert` from a terminal): the cut and edits.json are new.
            moved = "current" in st and st["current"] != self.loaded
            promoted = brief["state"] == "promoted" and prev != ("promoted", brief["version"])
            if moved or promoted and self.loaded != brief["version"]:
                self._load()

    def _idle(self, what: str) -> None:
        """After poll_render(); under self.lock when it guards a write."""
        if self.render_state.get("state") == "running":
            raise Busy(BUSY.format(what))

    def poll_render(self) -> dict:
        st = self._status()
        self._sync(st)
        return st

    def watch(self, every: float = 1.0) -> threading.Thread:
        """Follow the render: when it promotes a version (or a revert moves current.json), reload the cut."""
        def loop():
            while not self._stop.wait(every if self.render_state.get("state") == "running" else every * 5):
                try:
                    self.poll_render()
                except Exception as e:  # a half-written project file: try again on the next tick
                    print(f"render watcher: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        th = threading.Thread(target=loop, name="render-watch", daemon=True)
        th.start()
        return th

    def render_status(self) -> dict:
        st = self._r().status(self.demo)
        self._sync(st)
        return st

    def render_plan(self) -> dict:
        return self._r().plan_summary(self.demo)

    def start_render(self) -> dict:
        r = self._r()
        self.poll_render()
        with self.lock:
            if self.render_state.get("state") == "running":
                raise Busy(f"{self.render_state.get('version') or 'A render'} is still rendering")
            try:
                out = r.start(self.demo)
            except Exception as e:
                if isinstance(e, getattr(r, "Busy", ())) or self._status().get("state") == "running":
                    raise Busy(str(e) or "A render is already running") from e
                if isinstance(e, RuntimeError):  # preflight refusals are the caller's to fix
                    raise ValueError(str(e)) from e
                raise
            self.render_state = {"state": "running", "version": out.get("version")}
        return out

    def cancel_render(self) -> dict:
        self._r().cancel(self.demo)
        return self.render_status()

    def revert_render(self) -> dict:
        r = self._r()
        self.poll_render()
        with self.lock:
            self._idle("revert")
            try:
                out = r.revert(self.demo)
            except RuntimeError as e:
                raise (Busy if isinstance(e, getattr(r, "Busy", ())) else ValueError)(str(e)) from e
            self._load()
        st = self._status()
        with self.lock:
            self.render_state = _brief(st)
        return {**self.project(), "revert": json.loads(json.dumps(out, default=str))}

    def render_log(self) -> str:
        log = self._r().status(self.demo).get("log") or ""
        if isinstance(log, list):
            return "\n".join(map(str, log))
        if "\n" in log or not log.endswith(".log"):
            return log
        root = Path(json.loads((self.demo / "job.json").read_text())["render_root"]).resolve()
        p = (Path(log) if Path(log).is_absolute() else root / log).resolve()
        if root not in p.parents:
            raise KeyError("That log is outside this job's render folder")
        with open(p, "rb") as f:
            f.seek(max(0, p.stat().st_size - LOG_TAIL))
            return f.read().decode("utf-8", "replace")


class Handler(SimpleHTTPRequestHandler):
    engine: Engine

    def log_message(self, fmt, *args):  # quiet static files and the 1 s render poll, keep API calls
        line = str(args[0]) if args else ""
        if "/api/" in line and not line.startswith("GET /api/render "):
            super().log_message(fmt, *args)

    def end_headers(self):
        # A local app server: never let the web view keep a stale app.css / app.js.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text: str):
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _call(self, fn, text: bool = False):
        """Run an API call and turn its exceptions into JSON errors the UI can show."""
        try:
            out = fn()
        except Busy as e:
            return self._json({"error": str(e)}, 409)
        except Unavailable as e:
            return self._json({"error": str(e)}, 503)
        except ModuleNotFoundError:
            return self._json({"error": NO_MUSE}, 503)
        except FileNotFoundError as e:
            return self._json({"error": str(e)}, 404)
        except (KeyError, ValueError) as e:
            return self._json({"error": str(e).strip("'\"")}, 400)
        except Exception as e:  # surface Spark / budget / render errors to the UI instead of a dropped connection
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        return self._text(out) if text else self._json(out)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _local_host(self) -> bool:
        """Reject requests whose Host isn't this server (DNS rebinding)."""
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _project(self) -> dict:
        self.engine.poll_render()
        return self.engine.project()

    def do_GET(self):
        if not self._local_host():
            return self._json({"error": "forbidden"}, 403)
        url = urlparse(self.path)
        if url.path in ("/", "/app", "/app/"):
            self.send_response(302)
            self.send_header("Location", "/app/ui/")
            self.end_headers()
            return
        if url.path == "/api/project":
            return self._call(self._project)
        if url.path == "/api/render":
            return self._call(self.engine.render_status)
        if url.path == "/api/render/plan":
            return self._call(self.engine.render_plan)
        if url.path == "/api/render/log":
            return self._call(self.engine.render_log, text=True)
        if url.path == "/api/frame":
            q = parse_qs(url.query)
            try:
                path = self.engine.frame(q.get("tag", [""])[0], float(q.get("t", ["0"])[0]))
            except (KeyError, ValueError, subprocess.SubprocessError) as e:
                return self._json({"error": str(e)}, 404)
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if not url.path.startswith(STATIC) or any(seg.startswith(".") for seg in url.path.split("/")):
            return self._json({"error": "not found"}, 404)
        return super().do_GET()

    def do_HEAD(self):
        url = urlparse(self.path)
        if not self._local_host() or not url.path.startswith(STATIC) or \
                any(seg.startswith(".") for seg in url.path.split("/")):
            self.send_error(404)
            return
        return super().do_HEAD()

    def do_POST(self):
        if not self._local_host():
            return self._json({"error": "forbidden"}, 403)
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._json({"error": "JSON only"}, 415)
        path = urlparse(self.path).path
        return self._call(lambda: self._post(path, self._body()))

    def _post(self, path: str, body: dict) -> dict:
        e = self.engine
        if path == "/api/muse":
            text = (body.get("text") or "").strip()
            if not text:
                raise ValueError("Empty request")
            return e.ask(text, float(body.get("at") or 0))
        if path == "/api/accept":
            return e.accept(body.get("id", ""), body.get("pick"))
        if path == "/api/undo":
            return e.undo(body.get("edit"))
        if path == "/api/render":
            return e.start_render()
        if path == "/api/render/cancel":
            return e.cancel_render()
        if path == "/api/render/revert":
            return e.revert_render()
        raise FileNotFoundError("unknown endpoint")

    # HTTP Range, so the video player can seek (Safari/WKWebView will not play without it).
    def send_head(self):
        path = self.translate_path(self.path)
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        if not m or not os.path.isfile(path):
            return super().send_head()
        size = os.path.getsize(path)
        start = int(m[1]) if m[1] else max(0, size - int(m[2] or 0))
        end = min(int(m[2]), size - 1) if m[1] and m[2] else size - 1
        if start >= size:
            self.send_error(416)
            return None
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self._remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        left = getattr(self, "_remaining", None)
        if left is None:
            return super().copyfile(source, outputfile)
        while left > 0:
            chunk = source.read(min(left, 1 << 16))
            if not chunk:
                break
            outputfile.write(chunk)
            left -= len(chunk)


def serve(port: int = 7791, demo: Path = DEMO, render=None) -> ThreadingHTTPServer:
    engine = Engine(demo, render)
    engine.watch()
    handler = type("BoundHandler", (Handler,), {"engine": engine})
    return ThreadingHTTPServer(("127.0.0.1", port), partial(handler, directory=str(ROOT)))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7791
    httpd = serve(port)
    print(f"GhostCut engine on http://127.0.0.1:{port}/app/ui/", flush=True)
    httpd.serve_forever()
