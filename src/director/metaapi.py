"""The Meta Model API as a Connections provider: the key test only.

Muse (Spark, Wonder, Voice Transcribe) and SAM are called through the
`guestlist` client, which reads `MODEL_API_KEY` from the environment. This
module only lets the app's Connections panel and `director connect test meta`
check a pasted key: one GET of the model list, nothing generated, no cost.

Built by Claude Code (Opus) on submission day, so a new user can paste their
Muse key at the start of the tour instead of setting an environment variable.
"""
from __future__ import annotations

import json

from . import elevenlabs as _http  # the shared stdlib HTTP helper (tests patch it there)

META_BASE = "https://api.meta.ai/v1"
HTTP_TIMEOUT_S = 20


def _redact_key(message: str, key: str) -> str:
    from . import connect as connect_mod

    return connect_mod.redact(message, (key,))


def test_connection(key: str, *, transport=None) -> dict:
    """GET /v1/models. Returns {"ok", "message", "detail"}; never raises."""
    if transport is None:
        transport = _http.http_request
    unreachable = ("meta: could not reach the Meta Model API "
                   "(network or provider error) — try again")
    try:
        status, _headers, payload = transport(
            "GET", f"{META_BASE}/models",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            body=None, timeout=HTTP_TIMEOUT_S,
        )
    except Exception:
        return {"ok": False, "message": _redact_key(unreachable, key), "detail": ""}
    if status == 200:
        try:
            data = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            data = {}
        models = data.get("data", []) if isinstance(data, dict) else []
        names = [str(m.get("id", "")) for m in models if isinstance(m, dict)]
        muse = sum(1 for name in names if name.startswith("muse-"))
        return {"ok": True,
                "message": f"meta: connected — key accepted ({len(names)} models visible, "
                           f"{muse} Muse)",
                "detail": f"{len(names)} models visible"}
    if status in (401, 403):
        return {"ok": False,
                "message": "meta: the Meta Model API rejected this key "
                           "(check it on dev.meta.ai) — nothing was generated",
                "detail": ""}
    if status == 402:
        return {"ok": False,
                "message": "meta: the key works but its team has no billing set up "
                           "(dev.meta.ai → Billing) — Muse calls will fail until it does",
                "detail": ""}
    return {"ok": False, "message": _redact_key(unreachable, key), "detail": ""}
