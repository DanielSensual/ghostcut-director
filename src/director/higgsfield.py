"""Higgsfield provider: key storage and a no-generation test call.

Registry-only by design (FINISH-DESIGN.md §1.6, §8): the verified
catalogue offers image/video generation but no finishing capability —
no audio, music, isolation, captions or upscale endpoint — so this
module has a test call and no `generate_*` function. Its absence is
the not-available path made structural: there is no endpoint a caller
could guess at.

Docs verified: Quickstart, Authentication, Requests and lifecycle,
Billing and retention, Errors and retries, the model catalogue.

Designed by Muse Code on muse-wonder-1.0, built by Muse Code on
muse-spark-1.3 from docs/muse-code/FINISH-DESIGN.md.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

HIGGSFIELD_BASE = "https://api.higgsfield.ai"
HIGGSFIELD_TEST_MODEL = "higgsfield-ai/soul/v2/standard"
HTTP_TIMEOUT_S = 300


class HiggsfieldError(RuntimeError):
    """A Higgsfield call failed. Starts with 'higgsfield: ', key-free."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


def http_request(
    method: str,
    url: str,
    *,
    headers: dict,
    body: bytes | None,
    timeout: float,
) -> tuple[int, dict, bytes]:
    """One HTTP round trip. HTTP errors return; network errors raise."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (
                int(response.status),
                dict(response.headers.items()),
                response.read(),
            )
    except urllib.error.HTTPError as error:
        try:
            payload = error.read()
        except Exception:
            payload = b""
        try:
            items = dict(error.headers.items()) if error.headers else {}
        except Exception:
            items = {}
        return int(error.code), items, payload
    except Exception as error:
        raise HiggsfieldError(
            "higgsfield: could not reach Higgsfield "
            "(network or provider error) — try again"
        ) from error


def estimate_request(model_path: str, prompt: str) -> tuple[str, dict]:
    """(url, body) for POST /estimate/<model>. Pure — the test pins this."""
    return f"{HIGGSFIELD_BASE}/estimate/{model_path}", {"prompt": prompt}


def _redact(message: str, key_id: str, key_secret: str) -> str:
    from . import connect as connect_mod

    return connect_mod.redact(message, (key_id, key_secret))


def test_connection(key_id: str, key_secret: str, *,
                    transport=None) -> dict:
    """POST the no-generation estimate. Returns {"ok","message","detail"}."""
    if transport is None:
        transport = http_request
    url, payload = estimate_request(HIGGSFIELD_TEST_MODEL, "test")
    body = json.dumps(payload).encode("utf-8")
    try:
        status, _headers, data = transport(
            "POST", url,
            headers={"Authorization": f"Key {key_id}:{key_secret}",
                     "Accept": "application/json",
                     "Content-Type": "application/json"},
            body=body, timeout=HTTP_TIMEOUT_S)
    except HiggsfieldError as error:
        return {"ok": False,
                "message": _redact(str(error), key_id, key_secret), "detail": ""}
    except Exception as error:
        return {"ok": False,
                "message": _redact("higgsfield: could not reach Higgsfield "
                                   "(network or provider error) — try again",
                                   key_id, key_secret),
                "detail": ""}
    if status == 200:
        try:
            usd = float(json.loads(data.decode("utf-8")).get("usd"))
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
            return {"ok": True,
                    "message": "higgsfield: connected — test ok", "detail": ""}
        shown = f"${usd:.3f}"
        return {"ok": True,
                "message": f"higgsfield: connected — test ok (estimate {shown})",
                "detail": f"estimate {shown}"}
    if status == 401:
        return {"ok": False,
                "message": "higgsfield: Higgsfield rejected this key pair "
                           "(check the ID and secret) — nothing was generated",
                "detail": ""}
    if status == 403:
        return {"ok": False,
                "message": "higgsfield: key accepted, but the account balance "
                           "cannot run models — add funds, then test again",
                "detail": ""}
    if status in (404, 503):
        return {"ok": False,
                "message": "higgsfield: key accepted, but the test model is "
                           "unavailable right now — try again later",
                "detail": ""}
    return {"ok": False,
            "message": "higgsfield: could not reach Higgsfield "
                       "(network or provider error) — try again",
            "detail": ""}
