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

import base64
import hashlib
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
import cutjob  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "app" / "ui" / "demo"
STATIC = ("/app/ui/", "/docs/brand/")
NO_PROJECT = "No project yet: build one with app/make_demo.py from a finished Director job (see docs/APP-DESIGN.md)."
NO_MUSE = "Muse needs the Meta client: run the engine with the venv that has guestlist.meta installed."
NO_JOB = "No cut job is open: open one with POST /api/job/open or python app/engine.py --job DIR."
BUSY = "Rendering — {} after it finishes"
CUTJOB_BUSY = "Re-rendering — {} after it finishes"
LOG_TAIL = 256_000
VOICE_MODEL = "muse-spark-1.3"
VOICE_MAX_USD = 2.00
CONNECTIONS_MAX_BODY = 64 * 1024
NO_CONNECT = "Connections are unavailable in this app build (director package not found)."
# KITCHEN: sampler tick (seconds) for the re-render progress sampler (tests patch it smaller).
KITCHEN_TICK_S = 0.5


def sample_parts(parts_dir: Path, started_at: float, total: int | None) -> dict:
    """KITCHEN: count fresh render parts for the ghost-kitchen progress.

    Pure function (no threads, no I/O beyond stat) so tests need no
    engine. ``shot_*.mp4`` files with ``mtime >= started_at - 2.0``
    are fresh (the 2 s absorbs coarse file-system clocks); older
    files are leftovers of a previous run and never count. ffmpeg
    creates ``shot_NNN.mp4`` the moment it *starts* that shot, so
    the newest fresh file is the one being encoded. After the last
    shot, per-span ``audio_*`` parts and then ``parts.txt`` appear.
    """
    try:
        directory = Path(parts_dir)
        if not directory.is_dir():
            return {"phase": "plan", "done": 0, "total": total}
        threshold = float(started_at) - 2.0
        fresh = 0
        join_fresh = False
        for entry in directory.iterdir():
            try:
                if not entry.is_file():
                    continue
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime < threshold:
                continue
            name = entry.name
            if name.startswith("shot_") and name.endswith(".mp4"):
                fresh += 1
            elif name.startswith("audio_") or name == "parts.txt":
                join_fresh = True
        if join_fresh:
            return {"phase": "join", "done": fresh, "total": fresh}
        if fresh > 0:
            total_out = max(total or 0, fresh)
            return {"phase": "shots", "done": fresh - 1, "total": total_out}
        return {"phase": "plan", "done": 0, "total": total}
    except OSError:
        return {"phase": "plan", "done": 0, "total": total}


class Busy(Exception):
    """A render is running: the edit list stays frozen until it ends (HTTP 409)."""


class Unavailable(Exception):
    """app/render.py can't be loaded here (HTTP 503)."""


def _brief(st: dict) -> dict:
    out = {"state": st.get("state") or "idle", "version": st.get("version")}
    if out["state"] == "unavailable":
        out["message"] = st.get("message", "")
    return out


MUSE_UNAVAILABLE = "Muse unavailable, using the built-in planner."  # ONBOARD


class Engine:
    def __init__(self, demo: Path = DEMO, render=None, job_dir: Path | str | None = None, settings_path: Path | str | None = None):  # ONBOARD
        self._settings_path: Path | None = Path(settings_path) if settings_path is not None else None  # ONBOARD: lazy, never read here
        self.demo = demo
        self.edits_file = demo / "edits.json"
        self.spend_file = demo / "muse_spend.json"
        self.spend = json.loads(self.spend_file.read_text()) if self.spend_file.exists() else {"usd": 0.0, "calls": 0}
        self.lock = threading.Lock()
        self.gen = 0
        self._render = render
        self._stop = threading.Event()
        self.render_state: dict = {"state": "idle", "version": None}
        self.job_dir = Path(job_dir) if job_dir else None
        self._cutjob_state: dict = {"state": "idle", "message": ""}
        self._cutjob_thread: threading.Thread | None = None
        self._offload_state: dict = {"state": "idle", "message": ""}
        self._offload_thread: threading.Thread | None = None
        self._offload_plan = None
        self._offload_intent: dict | None = None
        self._offload_progress: dict = {}
        self._offload_root: Path | None = None
        self._scan_lock = threading.Lock()
        self._scan_states: dict[str, dict] = {}
        self._scan_threads: dict[str, threading.Thread] = {}
        self._cut_bg_state: dict = {"state": "idle", "message": ""}
        self._cut_bg_thread: threading.Thread | None = None
        self._render_cookie: tuple = ()
        if self.job_dir is not None:
            cutjob.build_project(self.job_dir, self.demo)
        self._load()
        self.render_state = _brief(self._status())
        self._render_cookie = self._cookie()

    # ONBOARD: per-machine first-run settings (lazy file, never read in __init__)
    def _settings_file(self) -> Path:
        if getattr(self, "_settings_path", None) is not None:
            return Path(self._settings_path)  # type: ignore[arg-type]
        env = os.environ.get("GHOSTCUT_SETTINGS")
        if env:
            return Path(env)
        return Path.home() / "Library" / "Application Support" / "GhostCut Director" / "settings.json"

    @staticmethod
    def _default_onboarding() -> dict:
        return {"seen": False, "version": 1, "step": 0, "skipped": False, "completed_at": None}

    def _read_onboarding(self) -> dict:
        default = self._default_onboarding()
        try:
            path = self._settings_file()
            if not path.exists():
                return dict(default)
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return dict(default)
        if not isinstance(raw, dict):
            return dict(default)
        ob = raw.get("onboarding")
        if not isinstance(ob, dict):
            return dict(default)
        out = dict(default)
        if "seen" in ob:
            out["seen"] = bool(ob["seen"])
        if "skipped" in ob:
            out["skipped"] = bool(ob["skipped"])
        if "step" in ob:
            try:
                out["step"] = int(ob["step"])  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass
        if "completed_at" in ob:
            v = ob["completed_at"]
            out["completed_at"] = str(v) if v is not None else None
        out["version"] = 1
        return out

    def settings_get(self) -> dict:  # ONBOARD
        onboarding = self._read_onboarding()
        first_run = not (onboarding.get("seen") or onboarding.get("completed_at"))
        return {"onboarding": onboarding, "first_run": bool(first_run), "meta_key": bool(os.environ.get("MODEL_API_KEY"))}

    def settings_post(self, body: dict) -> dict:  # ONBOARD
        current = self._read_onboarding()
        incoming: dict = {}
        if isinstance(body, dict):
            cand = body.get("onboarding")
            if isinstance(cand, dict):
                incoming = cand
            elif any(k in body for k in ("seen", "step", "skipped", "completed_at", "version")):
                incoming = body  # tolerate a bare onboarding block
        merged = dict(current)
        if "seen" in incoming:
            merged["seen"] = bool(incoming["seen"])
        if "skipped" in incoming:
            merged["skipped"] = bool(incoming["skipped"])
        if "step" in incoming:
            try:
                merged["step"] = int(incoming["step"])  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass
        if "completed_at" in incoming:
            v = incoming["completed_at"]
            merged["completed_at"] = str(v) if v is not None else None
        merged["version"] = 1
        path = self._settings_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({"onboarding": merged}, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass
        return self.settings_get()

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
        if self.job_dir is not None and isinstance(p.get("cutJob"), dict):
            try:
                cookie = self._cookie()
            except OSError:
                cookie = self._render_cookie
            count = cookie[0] if cookie else 0
            p["cutJob"] = {**p["cutJob"], "editsApplied": count,
                           "renderDirty": bool(cookie) and cookie != self._render_cookie}
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

    # ---------- cut jobs: open a director cut, talk back, re-render ----------

    def _voice(self):
        """director.voice, with src/ put on the path here (503 without it)."""
        src = str(ROOT / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        try:
            from director import voice as voice_mod
        except ImportError as e:
            raise Unavailable(f"Voice needs the Director package: {e}") from e
        return voice_mod

    def _need_job(self) -> Path:
        if self.job_dir is None:
            raise ValueError(NO_JOB)
        return self.job_dir

    def _cookie(self) -> tuple:
        """What the Re-render button compares: tighten edits + angles mtime."""
        if self.job_dir is None:
            return ()
        count = 0
        try:
            saved = json.loads((self.job_dir / "tighten.json").read_text())
            count = len(saved.get("edits_applied", []) or [])
        except (OSError, ValueError):
            pass
        try:
            mtime = (self.job_dir / "angles.json").stat().st_mtime_ns
        except OSError:
            mtime = -1
        return (count, mtime)

    def _tighten_sha(self) -> str:
        try:
            return hashlib.sha256((self._need_job() / "tighten.json").read_bytes()).hexdigest()
        except OSError:
            return ""

    def _cutjob_idle(self, what: str) -> None:
        if self._cutjob_state.get("state") == "running":
            raise Busy(CUTJOB_BUSY.format(what))

    def open_job(self, path: str) -> dict:
        job = Path(path or "")
        cutjob.build_project(job, self.demo)
        self.job_dir = job
        self._cutjob_state = {"state": "idle", "message": ""}
        self._cutjob_thread = None
        self._load()
        self._render_cookie = self._cookie()
        return self.project()

    def _store_voice(self, payload: dict, request: str) -> dict:
        payload["notes"] = self._dress_notes(payload.get("notes", []) or [])  # DRESS
        pid = uuid.uuid4().hex[:8]
        flat = []
        for note in payload.get("notes", []):
            for op in note.get("ops", []) or []:
                flat.append({**op, "note_id": note.get("id")})
        self.proposals[pid] = {"kind": "voice", "request": request, "ops": flat,
                               "notes": payload.get("notes", []), "usd": payload.get("spend_usd", 0.0),
                               "gen": self.gen, "tighten_sha": self._tighten_sha()}
        warnings = [str(w) for w in payload.get("warnings", []) or []]
        cached = any("cache" in w for w in warnings)
        reply = f"Heard {len(payload.get('notes', []))} note(s)"
        # Muse down (402 / network): the notes were heard by local Whisper and/or resolved by the
        # offline rules. Say so on the card (model line + reply) instead of failing the request.
        offline = [w for w in warnings if w.startswith("Muse unavailable")]
        if offline:
            reply = f"{reply} · {' · '.join(offline)}"
        model = "offline rules (Muse unavailable)" if payload.get("resolver") == "rules" else VOICE_MODEL
        out = {"id": pid, "reply": reply, "answers": [], "warnings": warnings,
               "ops": flat, "notes": payload.get("notes", []), "engine": payload.get("engine", ""),
               "usd": payload.get("spend_usd", 0.0), "cached": cached, "model": model}
        # Heard-but-unresolvable: the card shows the heard text with the error.
        if payload.get("error"):
            out["error"] = str(payload["error"])
        return out

    def voice_record(self, audio_b64: str, mime: str, at_s: float) -> dict:
        vm = self._voice()
        job = self._need_job()
        try:
            raw = base64.b64decode(audio_b64 or "", validate=True)
        except Exception as e:
            raise ValueError("Bad audio upload") from e
        if not raw:
            raise ValueError("Bad audio upload")
        ext = {"audio/webm": "webm", "audio/mp4": "m4a", "audio/mpeg": "mp3", "audio/wav": "wav",
               "audio/x-wav": "wav", "video/mp4": "mp4", "video/quicktime": "mov"}.get((mime or "").split(";")[0], "webm")
        dest = self.demo / "voice"
        dest.mkdir(parents=True, exist_ok=True)
        saved = dest / f"rec-{int(time.time() * 1000) % 10**9}.{ext}"
        saved.write_bytes(raw)
        try:
            payload = vm.run(note_path=saved, job_dir=job, model=VOICE_MODEL, max_usd=VOICE_MAX_USD,
                             apply=False, anchors=[float(at_s)], source="live")
        except vm.VoiceError as e:
            raise ValueError(str(e)) from e
        return self._store_voice(payload, f"voice:live:{saved.name}")

    def voice_file(self, name: str, audio_b64: str, confirm: bool = False) -> dict:
        vm = self._voice()
        job = self._need_job()
        try:
            raw = base64.b64decode(audio_b64 or "", validate=True)
        except Exception as e:
            raise ValueError("Bad audio upload") from e
        if not raw:
            raise ValueError("Bad audio upload")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "note").name).strip("._") or "note.m4a"
        dest = self.demo / "voice"
        dest.mkdir(parents=True, exist_ok=True)
        saved = dest / f"{int(time.time() * 1000) % 10**9}-{safe}"
        saved.write_bytes(raw)
        try:
            duration = vm.probe_note_duration(saved)
        except vm.VoiceError as e:
            raise ValueError(str(e)) from e
        estimate = vm.estimate_note_usd(duration)
        if duration > vm.ESTIMATE_CONFIRM_S and not confirm:
            return {"needs_confirm": True, "estimate_usd": round(estimate, 4),
                    "duration_s": round(duration, 3)}
        try:
            payload = vm.run(note_path=saved, job_dir=job, model=VOICE_MODEL, max_usd=VOICE_MAX_USD,
                             apply=False, source="file")
        except vm.VoiceError as e:
            raise ValueError(str(e)) from e
        return self._store_voice(payload, f"voice:file:{safe}")

    def voice_apply(self, pid: str | None = None, pick: list[int] | None = None,
                    candidates: dict | None = None, ops: list[dict] | None = None) -> dict:
        vm = self._voice()
        job = self._need_job()
        self._cutjob_idle("apply")
        if ops is not None:
            ops = self._dress_split_ops(ops)  # DRESS: dress_* ops go to dress.json
            if not ops:
                cutjob.build_project(job, self.demo)
                self._load()
                return self.project()
            context = vm.build_job_context(job)
            checked = vm._check_ops(context, [dict(op) for op in ops], None)
            notes = [{"id": "n0", "status": "resolved", "at_s": None, "beat_id": -1,
                      "ops": checked, "candidates": []}]
            result = vm.apply_notes(job=context, notes=notes, pick=None, source="voice")
            cutjob.build_project(job, self.demo)
            self._load()
            return self.project()
        prop = self.proposals.get(pid or "")
        if not prop or prop.get("kind") != "voice":
            raise KeyError("That proposal is gone (the engine restarted?). Ask again.")
        if prop.get("gen", self.gen) != self.gen:
            raise KeyError("That proposal was made on the previous version of the cut. Ask again.")
        if prop.get("tighten_sha", "") != self._tighten_sha():
            raise ValueError(vm.STALE_MESSAGE)
        with self.lock:
            self._cutjob_idle("apply")
            context = vm.build_job_context(job)
            notes = self._dress_split_apply(prop["notes"], pick)  # DRESS
            try:
                result = vm.apply_notes(job=context, notes=notes, pick=pick,
                                        candidates={k: int(v) for k, v in (candidates or {}).items()},
                                        source="voice")
            except vm.VoiceError as e:
                raise ValueError(str(e)) from e
            self._record_voice_applied(result["applied"])
            cutjob.build_project(job, self.demo)
            self.proposals.pop(pid or "", None)
            self._load()
        return self.project()

    def _record_voice_applied(self, applied: list[str]) -> None:
        if not applied:
            return
        path = self._need_job() / "voice.json"
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        payload["applied"] = list(payload.get("applied", []) or []) + list(applied)
        try:
            path.write_text(json.dumps(payload))
        except OSError:
            pass

    # ---------- DRESS: dress the cut (title cards, music, ghost pop) ----------

    def _dress(self):
        """director.dress, with src/ put on the path here (503 without it). DRESS"""
        src = str(ROOT / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        try:
            from director import dress as dress_mod
        except ImportError as e:
            raise Unavailable(f"Dress needs the Director package: {e}") from e
        return dress_mod

    def _dress_checked(self, dm, ops: list[dict], note_id: str | None = None) -> list[dict]:  # DRESS
        job = self._need_job()
        plan = dm.load_plan(job)
        return [{**dm.validate_op(op, job_dir=job, plan_payload=plan), "note_id": note_id}
                for op in ops]

    def _dress_notes(self, notes: list[dict]) -> list[dict]:
        """DRESS: notes whose words match the dress grammar get checked dress ops.

        Card, B-roll, ghost and bing notes always win over whatever Spark (or the
        no-key stub) put there; music and level notes only once dress.json exists
        or the note says "add music", so "turn the music down" stays a mix note
        on a cut that was never dressed.
        """
        if self.job_dir is None or not notes:
            return notes
        try:
            dm = self._dress()
        except Unavailable:
            return notes
        job = self.job_dir
        has_plan = (job / "dress.json").exists()
        out = []
        for note in notes:
            raw = str((note or {}).get("raw_text") or "")
            try:
                ops = dm.parse_note(raw, job_dir=job, playhead_s=note.get("at_s"))
            except Exception:  # noqa: BLE001 - the voice path must never break on dress
                ops = None
            if ops and ops[0]["op"] == "dress_set_music" and not (
                    has_plan or "add music" in raw.lower()):
                ops = None
            if not ops:
                out.append(note)
                continue
            checked = self._dress_checked(dm, ops, note.get("id"))
            out.append({**note, "ops": checked, "status": "resolved",
                        "message": checked[0]["label"], "candidates": []})
        return out

    def _dress_split_apply(self, notes: list[dict], pick: list[int] | None) -> list[dict]:
        """DRESS: apply picked, unblocked dress_* ops; hand vm.apply_notes them blocked.

        Flat op indexes stay valid because no op is removed, only marked.
        """
        flat = [op for note in notes for op in (note.get("ops") or [])]
        chosen = (set(range(len(flat))) if pick is None
                  else {i for i in pick if 0 <= i < len(flat)})
        dress_ops = [op for i, op in enumerate(flat)
                     if i in chosen and str(op.get("op", "")).startswith("dress_")
                     and not op.get("blocked")]
        if dress_ops:
            self._dress().apply_ops(self._need_job(), dress_ops)
        return [{**note, "ops": [({**op, "blocked": True}
                                  if str(op.get("op", "")).startswith("dress_") else op)
                                 for op in (note.get("ops") or [])]}
                for note in notes]

    def _dress_split_ops(self, ops: list[dict]) -> list[dict]:
        """DRESS: the ops= path; applies dress_* ops and returns the rest."""
        dress_ops = [dict(op) for op in ops if str(op.get("op", "")).startswith("dress_")]
        if dress_ops:
            dm = self._dress()
            checked = self._dress_checked(dm, dress_ops)
            dm.apply_ops(self._need_job(), [op for op in checked if not op.get("blocked")])
        return [op for op in ops if not str(op.get("op", "")).startswith("dress_")]

    def dress_get(self) -> dict:  # DRESS
        dm = self._dress()
        job = self._need_job()
        st = dict(getattr(self, "_dress_state", None) or {"state": "idle", "message": ""})
        plan = dm.load_plan(job)
        return {"state": st.get("state", "idle"), "message": st.get("message", ""),
                "plan": plan, "summary": dm.summary(job), "status": dm.load_status(job),
                "stale": dm.is_stale(job, plan), "fresh": dm.is_fresh(job, plan)}

    def dress_plan(self, no_muse: bool = False) -> dict:  # DRESS
        dm = self._dress()
        job = self._need_job()
        if getattr(self, "_dress_state", {}).get("state") == "running":
            raise Busy("The dressed cut is rendering: plan again when it finishes")
        dm.plan(job, no_muse=bool(no_muse))
        with self.lock:
            cutjob.build_project(job, self.demo)
            self._load()
        return {**self.dress_get(), **self.project()}

    def dress_edit(self, card_id: str | None = None, enabled: bool | None = None,
                   remove: bool = False, ops: list[dict] | None = None) -> dict:  # DRESS
        dm = self._dress()
        job = self._need_job()
        try:
            if card_id:
                dm.set_card(job, str(card_id), enabled=enabled, remove=bool(remove))
            if ops:
                checked = self._dress_checked(dm, [dict(o) for o in ops])
                dm.apply_ops(job, [o for o in checked if not o.get("blocked")])
        except dm.DressError as e:
            raise ValueError(str(e)) from e
        with self.lock:
            cutjob.build_project(job, self.demo)
            self._load()
        return {**self.dress_get(), **self.project()}

    def dress_render(self, renderer=None) -> dict:  # DRESS
        dm = self._dress()
        job = self._need_job()
        if dm.load_plan(job) is None:
            raise ValueError("Plan the dress first (Plan button or director dress plan)")
        run = renderer or dm.render
        with self.lock:
            self._cutjob_idle("dress")
            if getattr(self, "_dress_state", {}).get("state") == "running":
                raise Busy("The dressed cut is already rendering")
            self._dress_state = {"state": "running", "message": "Dressing the cut"}
            th = threading.Thread(target=self._dress_worker, args=(job, run),
                                  name="dress-render", daemon=True)
            self._dress_thread = th
            th.start()
        return {"state": "running"}

    def _dress_worker(self, job: Path, run) -> None:  # DRESS
        try:
            run(job)
            with self.lock:
                cutjob.build_project(job, self.demo)
                self._load()
                self._dress_state = {"state": "done", "message": "Dressed cut ready"}
        except Exception as e:  # noqa: BLE001 - reported by GET /api/dress; cut.mp4 untouched
            with self.lock:
                self._dress_state = {"state": "failed", "message": f"{type(e).__name__}: {e}"}

    def dress_note(self, text: str, at: float | None = None) -> dict:  # DRESS
        dm = self._dress()
        job = self._need_job()
        ops = dm.parse_note(text, job_dir=job, playhead_s=at)
        if not ops:
            return {"matched": False}
        # Review fix: hand _store_voice an empty stub so _dress_notes is the one gate.
        # Before, a music note on a never-dressed cut ("turn the music down") kept its
        # unchecked ops and was swallowed here instead of reaching the chat.
        note = {"id": "n1", "source": "typed", "raw_text": text, "heard": text,
                "anchor_kind": "dress", "at_s": at, "beat_id": -1, "status": "unresolved",
                "message": "", "ops": [], "candidates": [], "engine": "rules", "usd": 0.0}
        stored = self._store_voice({"notes": [note], "spend_usd": 0.0, "engine": "rules",
                                    "warnings": []}, f"dress:typed:{text[:60]}")
        if not stored["ops"]:
            self.proposals.pop(stored["id"], None)
            return {"matched": False}
        return {"matched": True, "id": stored["id"], "reply": stored["notes"][0]["message"],
                "ops": stored["ops"], "notes": stored["notes"], "usd": 0, "cached": True,
                "model": "rules"}

    def taste_list(self) -> dict:
        vm = self._voice()
        self._need_job()
        payload, warnings = vm.load_taste_with_warnings(self.job_dir or Path("."))
        return {"entries": payload.get("entries", []), "warnings": warnings}

    def taste_add(self, text: str) -> dict:
        vm = self._voice()
        job = self._need_job()
        try:
            payload = vm.add_taste(job, text, source="typed")
        except vm.VoiceError as e:
            raise ValueError(str(e)) from e
        return {"entries": payload.get("entries", [])}

    def taste_remove(self, entry_id: str) -> dict:
        vm = self._voice()
        job = self._need_job()
        try:
            payload = vm.remove_taste(job, entry_id)
        except vm.VoiceError as e:
            raise ValueError(str(e)) from e
        return {"entries": payload.get("entries", [])}

    # ---------- connections: outside-service keys (per-machine, no job) ----------

    def _connect(self):
        """director.connect, with src/ put on the path here (503 without it)."""
        src = str(ROOT / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        try:
            from director import connect as connect_mod
        except ImportError as e:
            raise Unavailable(NO_CONNECT) from e
        return connect_mod

    def connections_list(self) -> dict:
        cm = self._connect()
        return {"providers": cm.list_providers()}

    def connections_test(self, provider: str | None = None) -> dict:
        cm = self._connect()
        try:
            if provider:
                results = [cm.test_provider(provider)]
            else:
                results = [cm.test_provider(entry["id"])
                           for entry in cm.list_providers()
                           if entry["connected"]]
        except cm.ConnectError as e:
            raise ValueError(str(e)) from e
        return {"providers": cm.list_providers(),
                "tests": [{"provider": r.provider, "ok": r.ok,
                           "message": r.message, "detail": r.detail}
                          for r in results]}

    def connections_add(self, provider: str, key: str | None = None,
                        key_id: str | None = None,
                        key_secret: str | None = None) -> dict:
        cm = self._connect()
        name = (provider or "").strip()
        try:
            info = cm.provider_info(name)
        except cm.ConnectError as e:
            raise ValueError(str(e)) from e
        if info.id == "elevenlabs":
            if not (key or "").strip():
                raise ValueError("connect: missing 'key' for elevenlabs")
            parts = (key or "",)
        else:
            if not (key_id or "").strip():
                raise ValueError("connect: missing 'key_id' for higgsfield")
            if not (key_secret or "").strip():
                raise ValueError("connect: missing 'key_secret' for higgsfield")
            parts = (key_id or "", key_secret or "")
        try:
            cm.set_provider_key(info.id, parts)
        except cm.ConnectError as e:
            raise ValueError(str(e)) from e
        return {"providers": cm.list_providers()}

    def connections_remove(self, provider: str) -> dict:
        cm = self._connect()
        name = (provider or "").strip()
        try:
            cm.provider_info(name)
        except cm.ConnectError as e:
            raise ValueError(str(e)) from e
        try:
            cm.remove_provider_key(name)
        except cm.ConnectError as e:
            raise ValueError(str(e)) from e
        return {"providers": cm.list_providers()}

    # ---------- Ghost: card offload (Import) + background cut ----------

    def _offload(self):
        """director.offload, with src/ put on the path here (503 without it)."""
        src = str(ROOT / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        try:
            from director import offload as offload_mod
        except ImportError as e:
            raise Unavailable(f"Offload needs the Director package: {e}") from e
        return offload_mod

    def _allowed_path(self, raw: str) -> Path:
        """A client path, re-resolved, confined to /Volumes, home or /tmp."""
        text = str(raw or "").strip()
        if not text:
            raise ValueError("offload: missing path")
        resolved = Path(text).expanduser().resolve()
        roots = [Path("/Volumes"), Path.home().resolve(), Path("/tmp").resolve()]
        for extra in ("/private/tmp", os.environ.get("TMPDIR") or ""):
            if extra:
                try:
                    roots.append(Path(extra).resolve())
                except OSError:
                    pass
        try:
            import tempfile as _tempfile

            roots.append(Path(_tempfile.gettempdir()).resolve())
        except OSError:
            pass
        if not any(resolved == r or r in resolved.parents for r in roots):
            raise ValueError(f"offload: path outside the allowed folders: {text}")
        return resolved

    def _volumes(self) -> list:
        mod = self._offload()
        root = getattr(self, "_offload_root", None)
        return mod.list_volumes(root) if root is not None else mod.list_volumes()

    # ----- background card scans: warm the cache before Ghost is asked -----

    def _scan_public(self) -> list[dict]:
        with self._scan_lock:
            return [{"volume": s["volume"], "name": s["name"],
                     "state": s["state"], "done": s.get("done", 0),
                     "total": s.get("total", 0),
                     "clip_count": s.get("clip_count", 0),
                     "total_bytes": s.get("total_bytes", 0),
                     # ONBOARD: tile facts from the scan
                     "camera_labels": list(s.get("camera_labels") or []),
                     "date_start": s.get("date_start"),
                     "date_end": s.get("date_end"),
                     "sample": s.get("sample"),
                     "message": s.get("message", "")}
                    for s in self._scan_states.values()]

    def _start_background_scans(self) -> None:
        """Scan mounted camera cards in the background (Import opens).

        Each card's scan is cached in director.offload keyed by its
        clip files' names, sizes and mtimes, so a ready card is only
        re-scanned when its files changed; a card that appears starts
        scanning on the next volumes/scans call.
        """
        mod = self._offload()
        for vol in self._volumes():
            if vol.kind != mod.KIND_CAMERA:
                continue
            key = str(vol.path)
            worker = None
            with self._scan_lock:
                live = self._scan_threads.get(key)
                if live is not None and live.is_alive():
                    continue
                state = self._scan_states.get(key)
                if state is not None and state.get("state") == "ready":
                    try:
                        changed = (mod.card_fingerprint(vol.path)
                                   != state.get("_fingerprint"))
                    except Exception:  # noqa: BLE001 - rescan on doubt
                        changed = True
                    if not changed:
                        continue
                self._scan_states[key] = {
                    "volume": key, "name": vol.name, "state": "scanning",
                    "done": 0, "total": 0, "clip_count": 0,
                    "total_bytes": 0, "message": ""}
                worker = threading.Thread(
                    target=self._scan_worker, args=(key,),
                    name=f"offload-scan-{vol.name}", daemon=True)
                self._scan_threads[key] = worker
            if worker is not None:
                worker.start()

    def _scan_worker(self, key: str) -> None:
        mod = self._offload()

        def progress(event: dict) -> None:
            with self._scan_lock:
                state = self._scan_states.get(key)
                if state is not None:
                    state["done"] = int(event.get("done") or 0)
                    state["total"] = int(event.get("total") or 0)
                    state["state"] = "scanning"

        try:
            scan = mod.scan_card(Path(key), progress)
            fingerprint = mod.card_fingerprint(Path(key))
            with self._scan_lock:
                state = self._scan_states.get(key) or {
                    "volume": key, "name": Path(key).name}
                try:  # ONBOARD: tile facts (never fail a ready scan over them)
                    lo, hi = scan.date_range
                except Exception:  # noqa: BLE001
                    lo = hi = None
                state.update({"state": "ready", "done": len(scan.clips),
                              "total": len(scan.clips),
                              "clip_count": len(scan.clips),
                              "total_bytes": scan.total_bytes,
                              "camera_labels": sorted({c.camera_label for c in scan.clips if c.camera_label}),
                              "date_start": lo.isoformat() if lo else None,
                              "date_end": hi.isoformat() if hi else None,
                              "sample": scan.clips[0].name if scan.clips else None,
                              "message": "", "_fingerprint": fingerprint})
                self._scan_states[key] = state
        except Exception as e:  # noqa: BLE001 - scans endpoint reports it
            with self._scan_lock:
                state = self._scan_states.get(key) or {
                    "volume": key, "name": Path(key).name}
                state.update({"state": "error",
                              "message": f"{type(e).__name__}: {e}"})
                self._scan_states[key] = state

    def offload_scans(self) -> dict:
        """Live scan progress per mounted card (the Scanning line)."""
        self._start_background_scans()
        vols = self._volumes()
        current = {str(v.path) for v in vols
                   if v.kind == self._offload().KIND_CAMERA}
        scans = [s for s in self._scan_public()
                 if s["volume"] in current]
        return {"scans": scans,
                "volumes": [v.as_dict() for v in vols]}

    def _get_scan(self, path: Path):
        """A card's scan: the background one when it is running or done."""
        mod = self._offload()
        key = str(Path(path))
        with self._scan_lock:
            worker = self._scan_threads.get(key)
        if worker is not None and worker.is_alive():
            worker.join()
        return mod.scan_card(Path(path))

    def offload_volumes(self) -> dict:
        self._start_background_scans()
        return {"volumes": [v.as_dict() for v in self._volumes()],
                "scans": self._scan_public()}

    def _shoot_sentence(self, scans, plan, named: bool) -> str:
        """Ghost's one sentence, naming the day and the camera(s)."""
        from datetime import date as _date

        mod = self._offload()
        skipped = set(plan.skipped_volumes)
        included = [s for s in scans if str(s.volume) not in skipped]
        names = ", ".join(Path(s.volume).name for s in included)
        count = plan.selected_count
        nbytes = sum(e.size_bytes for e in plan.entries
                     if not e.is_sidecar)
        try:
            day = _date.fromisoformat(plan.shoot_day or plan.date_str)
            label = mod.format_day_short(day)
        except ValueError:
            label = plan.shoot_day or plan.date_str
        if count == 0:
            return (f"No clips from {label} on {names or 'those cards'} — "
                    "ask for everything to copy the whole cards.")
        cameras: list[str] = []
        for scan in included:
            cam = scan.detail or ""
            if not cam:
                labels = sorted({e.camera_label for e in plan.entries
                                 if not e.is_sidecar
                                 and Path(e.source).is_relative_to(scan.volume)})
                cam = ", ".join(labels)
            if cam and cam not in cameras:
                cameras.append(cam)
        if len(cameras) == 1:
            clips = f"{count} {cameras[0]} clips"
        elif cameras:
            clips = f"{count} clips ({', '.join(cameras)})"
        else:
            clips = f"{count} clips"
        size = f"{nbytes / 1e9:.1f} GB"
        if plan.include_all:
            return f"Found everything on the cards: {clips}, {size} on {names}."
        if named:
            return f"Found the {label} shoot: {clips}, {size} on {names}."
        return f"Found today's shoot ({label}): {clips}, {size} on {names}."

    def _plan_card(self, scans, plan, intent: dict, reply: str = "") -> dict:
        by_volume = {str(s.volume): s for s in scans}
        cards = []
        infos = plan.cards_info or [
            {"volume": str(s.volume), "clip_count": len(s.clips),
             "total_clips": len(s.clips), "total_bytes": s.total_bytes,
             "date_start": (s.date_range[0].isoformat()
                            if s.date_range[0] else None),
             "date_end": (s.date_range[1].isoformat()
                          if s.date_range[1] else None),
             "skipped": False} for s in scans]
        for info in infos:
            scan = by_volume.get(str(info.get("volume")))
            total_bytes = int(info.get("total_bytes") or 0)
            cards.append({
                "volume": str(info.get("volume")),
                "name": (Path(str(info.get("volume"))).name
                         if info.get("volume") else ""),
                "kind": scan.kind if scan else "",
                "detail": scan.detail if scan else "",
                "camera": (scan.detail or scan.kind) if scan else "",
                "clip_count": int(info.get("clip_count") or 0),
                "total_clips": int(info.get("total_clips")
                                   or info.get("clip_count") or 0),
                "total_bytes": total_bytes,
                "gb": round(total_bytes / 1e9, 3),
                "date_start": info.get("date_start"),
                "date_end": info.get("date_end"),
                "skipped": bool(info.get("skipped"))})
        from datetime import date as _date

        try:
            day_label = self._offload().format_day_short(
                _date.fromisoformat(plan.shoot_day or plan.date_str))
        except ValueError:
            day_label = plan.shoot_day or plan.date_str
        return {"intent": intent, "reply": reply or intent.get("reply", ""),
                "cards": cards,
                "destination": str(plan.destination),
                "shoot_dir": str(plan.shoot_dir),
                "project": plan.project, "date": plan.date_str,
                "shoot_day": plan.shoot_day or plan.date_str,
                "shoot_day_label": day_label,
                "include_all": bool(plan.include_all),
                "older_count": plan.older_count,
                "older_bytes": plan.older_bytes,
                "older_start": plan.older_start,
                "older_end": plan.older_end,
                "older_summary": plan.older_summary,
                "skipped_cards": [c["name"] for c in cards if c["skipped"]],
                "total_bytes": plan.total_bytes,
                "bytes_to_copy": plan.bytes_to_copy,
                "free_bytes": plan.free_bytes,
                "enough_space": plan.bytes_to_copy <= plan.free_bytes,
                "entries": [e.as_dict() for e in plan.entries],
                "volumes": [v.as_dict() for v in self._volumes()]}

    def _make_plan(self, card_paths: list[str], destination: str,
                   project: str | None, intent: dict,
                   shoot_date: str | None = None,
                   include_all: bool = False) -> dict:
        mod = self._offload()
        if not card_paths:
            raise ValueError("offload: no camera card in that request")
        if not destination:
            raise ValueError("offload: no destination drive in that request")
        cards = [self._allowed_path(p) for p in card_paths]
        dest = self._allowed_path(destination)
        want_all = bool(include_all or intent.get("include_all")
                        or intent.get("everything"))
        want_date = shoot_date or intent.get("date") or intent.get("shoot_date")
        if want_all:
            want_date = None
        try:
            scans = [self._get_scan(c) for c in cards]
            plan = mod.plan_offload(scans, dest, project=project,
                                    shoot_date=want_date,
                                    include_all=want_all)
        except mod.OffloadError as e:
            raise ValueError(str(e)) from e
        self._offload_plan = plan
        self._offload_intent = intent
        self._offload_state = {"state": "planned",
                               "message": f"Ready to copy to {plan.shoot_dir}",
                               "shoot_dir": str(plan.shoot_dir)}
        self._offload_progress = {}
        reply = self._shoot_sentence(scans, plan, named=bool(want_date))
        return self._plan_card(scans, plan, intent, reply=reply)

    # ONBOARD: live briefing shared by Ghost help and the tour
    def _briefing_payload(self) -> dict:
        mod = self._offload()
        vols = self._volumes()
        try:
            self._start_background_scans()  # ONBOARD review: a fresh window still gets clip counts
            scans = {s["volume"]: s for s in self._scan_public()}
        except Exception:  # noqa: BLE001 - briefing never blocks on scans
            scans = {}
        cards = [v for v in vols if v.kind == mod.KIND_CAMERA]
        dests = [v for v in vols if v.kind == mod.KIND_DESTINATION]

        def clip_phrase(v) -> str:
            s = scans.get(str(v.path))
            if s is not None and s.get("clip_count"):
                n = s["clip_count"]
                return f"{v.name}, {n} clip{'s' if n != 1 else ''}"
            if s is not None and s.get("state") == "scanning":
                return f"{v.name}, scanning"
            return v.name

        def gb(n: int) -> str:
            try:
                v = float(n or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v >= 1e9:
                return f"{v / 1e9:.0f} GB"
            if v >= 1e6:
                return f"{v / 1e6:.0f} MB"
            return f"{int(v)} B"

        if cards and dests:
            bits = "; ".join(clip_phrase(v) for v in cards)
            d = dests[0]
            detail = (cards[0].detail or "camera").strip()
            reply = (f"I can see {len(cards)} {detail} card{'s' if len(cards) != 1 else ''} "
                     f"({bits}) and {d.name} with {gb(d.free_bytes)} free. "
                     f"Say 'transfer to my SSD' to begin.")
        elif cards:
            bits = "; ".join(clip_phrase(v) for v in cards)
            reply = (f"I can see {len(cards)} camera card{'s' if len(cards) != 1 else ''} "
                     f"({bits}) — connect your SSD to continue.")
        elif dests:
            d = dests[0]
            reply = (f"I can see {d.name} with {gb(d.free_bytes)} free — "
                     f"insert a camera card to begin.")
        else:
            reply = "Insert a camera card and connect your SSD — I'll plan the copy from there."
        steps = [
            "Insert a camera card — Ghost scans it and counts the clips.",
            "Pick a destination drive and tell Ghost what to copy.",
            "Review the plan card, then Transfer — every file is checksum-verified.",
            "Make the cut: sync, takes, grade and angles, in the background.",
        ]
        live_cards = []
        for v in vols:
            if v.kind != mod.KIND_CAMERA:
                continue
            s = scans.get(str(v.path), {})
            live_cards.append({
                "volume": str(v.path), "name": v.name, "kind": v.kind,
                "detail": v.detail, "free_bytes": v.free_bytes,
                "total_bytes": v.total_bytes,
                "clip_count": s.get("clip_count", 0),
                "state": s.get("state", ""),
            })
        dest = None
        if dests:
            d = dests[0]
            dest = {"volume": str(d.path), "name": d.name, "kind": d.kind,
                    "detail": d.detail, "free_bytes": d.free_bytes,
                    "total_bytes": d.total_bytes}
        return {"reply": reply, "steps": steps,
                "live": {"cards": live_cards, "destination": dest}}

    def offload_briefing(self) -> dict:  # ONBOARD
        payload = self._briefing_payload()
        return {"reply": payload["reply"], "steps": payload["steps"],
                "live": payload["live"]}

    def offload_ask(self, text: str) -> dict:
        mod = self._offload()
        vols = self._volumes()
        # ONBOARD: help is answered from live state before any intent parse
        try:
            _is_help = getattr(mod, "is_help", None)
            if callable(_is_help) and _is_help(str(text or "")):
                payload = self._briefing_payload()
                return {"reply": payload["reply"], "help": True,
                        "steps": payload["steps"], "live": payload["live"]}
        except Exception:  # noqa: BLE001 - fall through to intent
            pass
        try:
            intent = mod.parse_intent(str(text or ""), vols)
        except Exception:
            intent = mod.parse_intent_rule(str(text or ""), vols)
        res = self._offload_ask_intent(intent, vols)
        # ONBOARD: say honestly which planner read the request (Muse can be
        # unavailable, e.g. a 402 when the Meta API credit runs out).
        if isinstance(res, dict):
            res.setdefault("planner", "muse" if intent.method == "spark" else "rules")
            if intent.method != "spark" and os.environ.get("MODEL_API_KEY"):
                res.setdefault("note", MUSE_UNAVAILABLE)
        return res

    def _offload_ask_intent(self, intent, vols) -> dict:  # ONBOARD: body of offload_ask, unchanged
        data = intent.as_dict()
        if intent.confirm:
            if self._offload_plan is None:
                return {"intent": data, "reply": "Nothing to transfer yet — tell me which card to copy."}
            started = self.offload_start()
            return {"intent": data, "reply": intent.reply or "Starting the transfer.",
                    "started": started}
        if intent.open_job:
            job = self._resolve_open_job(intent.open_job)
            opened = self.open_job(str(job))
            return {"intent": data, "reply": intent.reply or "Opening that job.",
                    "opened": True, "project": opened}
        if not intent.cards or not intent.destination:
            return {"intent": data, "reply": intent.reply,
                    "volumes": [v.as_dict() for v in vols]}
        return self._make_plan(list(intent.cards), str(intent.destination),
                               intent.project, data,
                               shoot_date=intent.date,
                               include_all=intent.include_all)

    def _resolve_open_job(self, wanted: str) -> Path:
        if wanted and wanted != "yesterday":
            return self._allowed_path(wanted)
        # "yesterday's job": newest folder holding a Director job.
        candidates: list[Path] = []
        bases: list[Path] = []
        try:
            bases.append(Path("/") / "Volumes" / "Extreme SSD")
        except OSError:
            pass
        bases.append(Path.home())
        for base in bases:
            try:
                if not base.exists():
                    continue
                for child in base.iterdir():
                    if child.is_dir() and ((child / "angles.json").exists()
                                           or (child / "cut.json").exists()):
                        candidates.append(child)
            except OSError:
                continue
        if not candidates:
            raise ValueError("offload: no Director job found to open")
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0]

    def offload_plan(self, cards: list, destination: str,
                     project: str | None = None,
                     shoot_date: str | None = None,
                     include_all: bool = False) -> dict:
        intent = {"cards": [str(c) for c in (cards or [])],
                  "destination": destination, "project": project,
                  "make_cut": False, "open_job": None, "confirm": False,
                  "reply": "Here is the transfer to plan.", "method": "explicit",
                  "date": shoot_date, "shoot_date": shoot_date,
                  "include_all": bool(include_all),
                  "everything": bool(include_all)}
        return self._make_plan([str(c) for c in (cards or [])],
                               str(destination or ""), project, intent,
                               shoot_date=shoot_date,
                               include_all=include_all)

    def offload_start(self) -> dict:
        plan = self._offload_plan
        if plan is None:
            raise ValueError("offload: nothing planned yet — ask Ghost first")
        with self.lock:
            if self._offload_state.get("state") == "running":
                raise Busy("Offloading — wait for it to finish")
            self._offload_state = {"state": "running", "message": "Copying",
                                   "shoot_dir": str(plan.shoot_dir)}
            self._offload_progress = {"bytes_done": 0,
                                      "bytes_total": plan.bytes_to_copy,
                                      "file": ""}
            th = threading.Thread(target=self._offload_worker, args=(plan,),
                                  name="offload-copy", daemon=True)
            self._offload_thread = th
            th.start()
        return {"state": "running", "shoot_dir": str(plan.shoot_dir)}

    def _offload_worker(self, plan) -> None:
        mod = self._offload()

        def progress(event: dict) -> None:
            with self.lock:
                self._offload_progress = dict(event)

        try:
            result = mod.run_offload(plan, progress)
            failed = [e.destination.name for e in result.failed]
            with self.lock:
                if failed:
                    self._offload_state = {
                        "state": "done_with_failures",
                        "message": (f"Verified {result.verified_count} files; "
                                    f"failed: {', '.join(failed)}"),
                        "shoot_dir": str(plan.shoot_dir),
                        "result": result.as_dict()}
                else:
                    self._offload_state = {
                        "state": "done",
                        "message": (f"Verified {result.verified_count} files, "
                                    f"{result.verified_bytes / 1e9:.2f} GB"),
                        "shoot_dir": str(plan.shoot_dir),
                        "result": result.as_dict()}
        except Exception as e:  # noqa: BLE001 - status endpoint reports it
            with self.lock:
                self._offload_state = {"state": "failed",
                                       "message": f"{type(e).__name__}: {e}",
                                       "shoot_dir": str(plan.shoot_dir)}

    def offload_status(self) -> dict:
        state = dict(self._offload_state)
        state["progress"] = dict(self._offload_progress)
        if self._offload_plan is not None:
            state["shoot_dir"] = str(self._offload_plan.shoot_dir)
        return state

    def start_offload_cut(self, shoot_dir: str, out_dir: str | None = None) -> dict:
        shoot = self._allowed_path(shoot_dir)
        if not shoot.is_dir():
            raise ValueError(f"offload: no such shoot folder: {shoot_dir}")
        out = (self._allowed_path(out_dir) if out_dir
               else Path(f"{shoot}-job"))
        with self.lock:
            if self._cut_bg_state.get("state") == "running":
                raise Busy("Cutting — wait for it to finish")
            self._cut_bg_state = {"state": "running", "message": "Starting cut",
                                  "shoot_dir": str(shoot), "out_dir": str(out),
                                  "stages": {}}
            th = threading.Thread(target=self._cut_worker, args=(shoot, out),
                                  name="offload-cut", daemon=True)
            self._cut_bg_thread = th
            th.start()
        return {"state": "running", "shoot_dir": str(shoot), "out_dir": str(out)}

    def _cut_worker(self, shoot: Path, out: Path) -> None:
        try:
            self._run_cut_job(shoot, out)
            with self.lock:
                self._cut_bg_state = {"state": "done",
                                      "message": f"Cut finished in {out}",
                                      "shoot_dir": str(shoot),
                                      "out_dir": str(out), "stages": {}}
            try:
                self.open_job(str(out))
            except Exception as e:  # noqa: BLE001 - cut exists; opening is best-effort
                with self.lock:
                    self._cut_bg_state["message"] = (
                        f"Cut finished in {out} (open failed: {e})")
        except Exception as e:  # noqa: BLE001 - status endpoint reports it
            with self.lock:
                keep = dict(self._cut_bg_state)
                keep.update({"state": "failed",
                             "message": f"{type(e).__name__}: {e}"})
                self._cut_bg_state = keep

    def _run_cut_job(self, shoot: Path, out: Path) -> dict:
        """The background `director cut` (patched by tests with a fake)."""
        src = str(ROOT / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        from director import cut as cut_mod
        from director import sync as sync_mod

        clips = sorted(
            p for p in shoot.rglob("*")
            if p.is_file() and p.suffix.lower()
            in (".mp4", ".mov", ".m4v", ".mp3", ".wav", ".m4a")
            and ".thumbs" not in p.parts and not p.name.startswith("."))
        if len(clips) < 2:
            raise ValueError("offload: a cut needs at least two clips in the shoot folder")
        return cut_mod.run(
            clips=clips, out_dir=out, reference_path=None, audio_path=None,
            words_path=None, transcripts_dir=None,
            methods=sync_mod.parse_methods(None),
            no_grade=False, grade_check=False, no_muse=False,
            model="muse-spark-1.3", min_shot_s=2.0, no_vertical=False,
            no_sam=False, no_export=False, no_qc=False, max_usd=5.0,
            force=False, project_name="Director cut", no_tighten=False)

    def cut_bg_status(self) -> dict:
        state = dict(self._cut_bg_state)
        out = state.get("out_dir")
        if out:
            try:
                manifest = json.loads((Path(out) / "cut.json").read_text())
                stages = manifest.get("stages") or {}
                state["stages"] = {k: (v.get("status") if isinstance(v, dict) else v)
                                   for k, v in stages.items()}
                if manifest.get("status"):
                    state["cut_status"] = manifest["status"]
            except (OSError, ValueError):
                pass
        return state

    def offload_shoot(self, shoot_dir: str) -> dict:
        shoot = self._allowed_path(shoot_dir)
        if not shoot.is_dir():
            raise ValueError(f"offload: no such shoot folder: {shoot_dir}")
        bins: dict[str, list[dict]] = {}
        for media in sorted(shoot.rglob("*")):
            if not media.is_file() or media.name.startswith("."):
                continue
            if ".thumbs" in media.parts or media.name == "offload.json":
                continue
            if media.suffix.lower() not in (".mp4", ".mov", ".m4v", ".wav",
                                            ".mp3", ".m4a"):
                continue
            label = media.parent.name if media.parent != shoot else "Shoot"
            info = self._offload()._probe_info(media)  # noqa: SLF001
            bins.setdefault(label, []).append(
                {"name": media.name, "path": str(media),
                 "duration_s": info.get("duration_s"),
                 "thumb": f"/api/offload/thumb?shoot={shoot}&file={media}"})
        return {"shoot_dir": str(shoot),
                "bins": [{"camera": k, "clips": v}
                         for k, v in sorted(bins.items())]}

    def offload_thumb(self, shoot_raw: str, file_raw: str) -> Path:
        shoot = self._allowed_path(shoot_raw)
        media = self._allowed_path(file_raw)
        if shoot not in media.parents:
            raise ValueError("offload: thumbnail outside the shoot folder")
        thumb_dir = shoot / ".thumbs"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        thumb = thumb_dir / f"{media.stem}.jpg"
        if not thumb.exists():
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(media),
                 "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "4",
                 str(thumb)], check=False, timeout=60,
                capture_output=True)
        if not thumb.exists():
            raise KeyError(f"no thumbnail for {media.name}")
        return thumb

    def offload_voice(self, audio_b64: str, mime: str) -> dict:
        """Ghost by voice: transcribe the request, then plan (no copy)."""
        vm = self._voice()
        try:
            raw = base64.b64decode(audio_b64 or "", validate=True)
        except Exception as e:
            raise ValueError("Bad audio upload") from e
        if not raw:
            raise ValueError("Bad audio upload")
        ext = {"audio/webm": "webm", "audio/mp4": "m4a", "audio/mpeg": "mp3",
               "audio/wav": "wav", "audio/x-wav": "wav",
               "video/mp4": "mp4", "video/quicktime": "mov"}.get(
                   (mime or "").split(";")[0], "webm")
        dest = self.demo / "voice"
        dest.mkdir(parents=True, exist_ok=True)
        saved = dest / f"ghost-{int(time.time() * 1000) % 10**9}.{ext}"
        saved.write_bytes(raw)
        try:
            duration = vm.probe_note_duration(saved)
        except vm.VoiceError as e:
            raise ValueError(str(e)) from e
        from ghostcut.ffmpeg_tools import run_command as _run

        wav = dest / f"{saved.stem}.wav"
        _run(vm.extract_audio_command(saved, wav))
        try:
            heard_warnings: list[str] = []
            turns, _engine, _spend = vm.transcribe_wav(
                wav, duration_s=duration, work_dir=dest, warnings=heard_warnings)
        except vm.VoiceError as e:
            raise ValueError(str(e)) from e
        text = " ".join(t.text for t in turns).strip()
        if not text:
            raise ValueError("offload: I didn't hear any speech in that recording")
        planned = self.offload_ask(text)
        planned["text"] = text
        if heard_warnings:
            planned["warnings"] = list(planned.get("warnings", []) or []) + heard_warnings
        return planned

    def cutjob_status(self) -> dict:
        return dict(self._cutjob_state)

    def start_cutjob_rerender(self) -> dict:
        job = self._need_job()
        try:
            cutjob.require_director_angles()
        except cutjob.CutJobError as e:
            raise Unavailable(str(e)) from e
        with self.lock:
            if self._cutjob_state.get("state") == "running":
                raise Busy(CUTJOB_BUSY.format("re-render"))
            self._cutjob_state = {"state": "running", "message": "Re-rendering cut.mp4"}
            th = threading.Thread(target=self._rerender_worker, args=(job,), name="cutjob-rerender",
                                  daemon=True)
            self._cutjob_thread = th
            th.start()
        return {"state": "running"}

    def _rerender_worker(self, job: Path) -> None:
        # KITCHEN: real-progress sampler around the render.
        started_at = time.time()
        total: int | None = None
        try:
            _angles = json.loads((job / "angles.json").read_text())
            _shots = _angles.get("shots") if isinstance(_angles, dict) else None
            total = len(_shots) if isinstance(_shots, list) else None
        except Exception:  # noqa: BLE001 - unreadable angles: total unknown
            total = None
        parts_dir = job / "cut.next.parts"
        stop = threading.Event()
        # KITCHEN: the ETA averages the finished shots only, timed from the first part, so a long
        # planning phase (angle re-pick, gaze sampling) does not inflate it.
        shots_since: list[float] = []

        def _sampler() -> None:  # KITCHEN
            while not stop.wait(float(KITCHEN_TICK_S)):
                try:
                    snap = sample_parts(parts_dir, started_at, total)
                except Exception:  # noqa: BLE001 - next tick retries
                    continue
                phase = snap.get("phase")
                done = int(snap.get("done") or 0)
                tot = snap.get("total")
                if phase in ("shots", "join"):
                    if tot:
                        pct: int | None = min(99, round(100 * done / tot))
                    else:
                        pct = 99 if phase == "join" else None
                else:
                    pct = None
                if phase in ("shots", "join") and not shots_since:
                    shots_since.append(time.time())
                if done >= 1 and tot:
                    if tot > done:
                        elapsed = max(0.0, time.time() - (shots_since[0] if shots_since else started_at))
                        eta_s: int | None = round((elapsed / done) * (tot - done))
                    else:
                        eta_s = 0
                else:
                    eta_s = None
                with self.lock:
                    if self._cutjob_state.get("state") != "running":
                        return
                    self._cutjob_state.update(
                        {"phase": phase, "done": done, "total": tot,
                         "pct": pct, "eta_s": eta_s})

        sampler = threading.Thread(target=_sampler, name="kitchen-sampler", daemon=True)
        sampler.start()
        try:
            result = cutjob.rerender(job, self.demo)
            message = f"Re-rendered cut.mp4 ({float(result['duration_s']):.1f}s)"
            # KITCHEN: stop and join the sampler before writing done.
            stop.set()
            sampler.join(timeout=2)
            with self.lock:
                self._load()
                self._render_cookie = self._cookie()
                self._cutjob_state = {"state": "done", "message": message}
        except Exception as e:  # noqa: BLE001 - the status endpoint reports it, cut.mp4 is untouched
            # KITCHEN: stop and join the sampler before writing failed.
            stop.set()
            try:
                sampler.join(timeout=2)
            except Exception:  # noqa: BLE001
                pass
            with self.lock:
                self._cutjob_state = {"state": "failed", "message": f"{type(e).__name__}: {e}"}
        finally:
            # KITCHEN: ensure the sampler can never overwrite done/failed.
            stop.set()
            try:
                sampler.join(timeout=2)
            except Exception:  # noqa: BLE001
                pass

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
        if url.path == "/api/taste":
            return self._call(self.engine.taste_list)
        if url.path == "/api/connections":
            return self._call(self.engine.connections_list)
        if url.path == "/api/cutjob/rerender":
            return self._call(self.engine.cutjob_status)
        if url.path == "/api/dress":  # DRESS
            return self._call(self.engine.dress_get)
        if url.path == "/api/offload/volumes":
            return self._call(self.engine.offload_volumes)
        if url.path == "/api/offload/scans":
            return self._call(self.engine.offload_scans)
        if url.path == "/api/offload/status":
            return self._call(self.engine.offload_status)
        if url.path == "/api/offload/cut/status":
            return self._call(self.engine.cut_bg_status)
        if url.path == "/api/settings":  # ONBOARD
            return self._call(self.engine.settings_get)
        if url.path == "/api/offload/briefing":  # ONBOARD
            return self._call(self.engine.offload_briefing)
        if url.path == "/api/offload/shoot":
            q = parse_qs(url.query)
            return self._call(lambda: self.engine.offload_shoot(q.get("shoot", [""])[0]))
        if url.path == "/api/offload/thumb":
            q = parse_qs(url.query)
            try:
                thumb = self.engine.offload_thumb(q.get("shoot", [""])[0],
                                                  q.get("file", [""])[0])
            except (KeyError, ValueError) as e:
                return self._json({"error": str(e)}, 404)
            data = thumb.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
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
        if path.startswith("/api/connections"):
            try:
                size = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                size = 0
            if size > CONNECTIONS_MAX_BODY:
                return self._json({"error": "Request too large"}, 400)
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
        if path == "/api/job/open":
            if not str(body.get("path") or ""):
                raise ValueError("Missing path")
            return e.open_job(str(body["path"]))
        if path == "/api/voice/record":
            return e.voice_record(body.get("audio_b64", ""), body.get("mime", ""),
                                  float(body.get("at_s") or 0))
        if path == "/api/voice/file":
            return e.voice_file(body.get("name", ""), body.get("audio_b64", ""),
                                bool(body.get("confirm")))
        if path == "/api/voice/apply":
            return e.voice_apply(body.get("id"), body.get("pick"), body.get("candidates"),
                                 body.get("ops"))
        if path == "/api/taste/add":
            if not str(body.get("text") or "").strip():
                raise ValueError("Empty preference")
            return e.taste_add(str(body["text"]))
        if path == "/api/taste/remove":
            return e.taste_remove(str(body.get("id", "")))
        if path == "/api/cutjob/rerender":
            return e.start_cutjob_rerender()
        if path == "/api/dress/plan":  # DRESS
            return e.dress_plan(bool(body.get("no_muse")))
        if path == "/api/dress/render":  # DRESS
            return e.dress_render()
        if path == "/api/dress/edit":  # DRESS
            return e.dress_edit(body.get("card_id"), body.get("enabled"),
                                bool(body.get("remove")), body.get("ops"))
        if path == "/api/dress/note":  # DRESS
            text = str(body.get("text") or "").strip()
            if not text:
                raise ValueError("Empty request")
            at = body.get("at")
            return e.dress_note(text, None if at is None else float(at))
        if path == "/api/connections/test":
            return e.connections_test(body.get("provider"))
        if path == "/api/connections/add":
            return e.connections_add(str(body.get("provider") or ""),
                                     body.get("key"), body.get("key_id"),
                                     body.get("key_secret"))
        if path == "/api/connections/remove":
            return e.connections_remove(str(body.get("provider") or ""))
        if path == "/api/offload/ask":
            text = str(body.get("text") or "").strip()
            if not text:
                raise ValueError("Empty request")
            return e.offload_ask(text)
        if path == "/api/offload/plan":
            return e.offload_plan(
                list(body.get("cards") or []),
                str(body.get("destination") or ""),
                body.get("project"),
                shoot_date=(body.get("shoot_date") or body.get("date")),
                include_all=bool(body.get("include_all")
                                  or body.get("everything")
                                  or body.get("include_older")))
        if path == "/api/offload/start":
            return e.offload_start()
        if path == "/api/offload/voice":
            return e.offload_voice(body.get("audio_b64", ""), body.get("mime", ""))
        if path == "/api/offload/cut":
            if not str(body.get("shoot_dir") or body.get("shoot") or ""):
                raise ValueError("Missing shoot_dir")
            return e.start_offload_cut(
                str(body.get("shoot_dir") or body.get("shoot")),
                body.get("out_dir"))
        if path == "/api/settings":  # ONBOARD
            return e.settings_post(body)
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


def serve(port: int = 7791, demo: Path = DEMO, render=None,
          job: Path | str | None = None) -> ThreadingHTTPServer:
    engine = Engine(demo, render, job_dir=job)
    engine.watch()
    handler = type("BoundHandler", (Handler,), {"engine": engine})
    return ThreadingHTTPServer(("127.0.0.1", port), partial(handler, directory=str(ROOT)))


if __name__ == "__main__":
    rest = list(sys.argv[1:])
    job = None
    if "--job" in rest:
        at = rest.index("--job")
        job = rest[at + 1] if at + 1 < len(rest) else None
        del rest[at:at + 2]
        if not job:
            raise SystemExit("engine: --job needs a directory")
    port = int(rest[0]) if rest else 7791
    httpd = serve(port, job=job)
    print(f"GhostCut engine on http://127.0.0.1:{port}/app/ui/", flush=True)
    httpd.serve_forever()
