"""ElevenLabs provider: test, sound effects, music, voice isolation.

Stdlib HTTP only (`urllib`), no vendor SDK. `http_request` is the only
function that touches the network; tests replace it with a fake. Every
outbound error string passes through `connect.redact` before raising,
and the key travels only in the `xi-api-key` header.

Endpoints verified on the official ElevenLabs API reference (see
FINISH-DESIGN.md §1.5). Models are pinned past the documented
deprecations; rates mirror the API pricing page (list prices, USD).

Designed by Muse Code on muse-wonder-1.0, built by Muse Code on
muse-spark-1.3 from docs/muse-code/FINISH-DESIGN.md.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

ELEVENLABS_BASE = "https://api.elevenlabs.io"
SFX_MODEL = "eleven_text_to_sound_v3"
MUSIC_MODEL = "music_v2_5"
SFX_OUTPUT_FORMAT = "mp3_44100_128"
MUSIC_OUTPUT_FORMAT = "mp3_48000_192"
SFX_PROMPT_INFLUENCE = 0.7
MUSIC_USD_PER_MIN = 0.15
SFX_USD_PER_MIN = 0.12
ISOLATION_USD_PER_MIN = 0.12
HTTP_TIMEOUT_S = 300


class ElevenLabsError(RuntimeError):
    """An ElevenLabs call failed. Starts with 'elevenlabs: ', key-free."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(slots=True)
class GeneratedAudio:
    data: bytes  # MP3 bytes as returned
    model: str
    requested_s: float
    cost_usd: float  # list-price estimate from the requested duration


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
        raise ElevenLabsError(
            "elevenlabs: could not reach ElevenLabs "
            "(network or provider error) — try again"
        ) from error


def _redact_key(message: str, key: str) -> str:
    from . import connect as connect_mod

    return connect_mod.redact(message, (key,))


def test_connection(key: str, *, transport=None) -> dict:
    """GET /v1/models. Returns {"ok", "message", "detail"}; never raises."""
    if transport is None:
        transport = http_request
    url = f"{ELEVENLABS_BASE}/v1/models"
    try:
        status, _headers, payload = transport(
            "GET", url,
            headers={"xi-api-key": key, "Accept": "application/json, audio/mpeg"},
            body=None, timeout=HTTP_TIMEOUT_S,
        )
    except ElevenLabsError as error:
        return {"ok": False,
                "message": _redact_key(str(error), key), "detail": ""}
    except Exception as error:
        return {"ok": False,
                "message": _redact_key(
                    "elevenlabs: could not reach ElevenLabs "
                    "(network or provider error) — try again", key),
                "detail": ""}
    if status == 200:
        try:
            data = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            data = []
        models = data.get("models", data) if isinstance(data, dict) else data
        count = len(models) if isinstance(models, list) else 0
        return {"ok": True,
                "message": f"elevenlabs: connected — test ok ({count} models visible)",
                "detail": f"{count} models visible"}
    if status in (401, 403):
        return {"ok": False,
                "message": "elevenlabs: ElevenLabs rejected this key "
                           "(check it, or its scopes) — nothing was generated",
                "detail": ""}
    return {"ok": False,
            "message": "elevenlabs: could not reach ElevenLabs "
                       "(network or provider error) — try again",
            "detail": ""}


def estimate_sfx_usd(duration_s: float) -> float:
    """List-price estimate for a generated effect. Pure."""
    if duration_s <= 0:
        return 0.0
    return round(duration_s / 60 * SFX_USD_PER_MIN, 4)


def estimate_music_usd(duration_s: float) -> float:
    """List-price estimate for a generated bed. Pure."""
    if duration_s <= 0:
        return 0.0
    return round(duration_s / 60 * MUSIC_USD_PER_MIN, 4)


def estimate_isolation_usd(duration_s: float) -> float:
    """List-price estimate for isolating source minutes. Pure."""
    if duration_s <= 0:
        return 0.0
    return round(duration_s / 60 * ISOLATION_USD_PER_MIN, 4)


def sfx_request_body(prompt: str, duration_s: float) -> dict:
    """The POST /v1/sound-generation JSON. Pure (pinned model/format)."""
    clamped = min(30.0, max(0.5, float(duration_s)))
    return {"text": prompt, "duration_seconds": clamped, "loop": False,
            "prompt_influence": SFX_PROMPT_INFLUENCE, "model_id": SFX_MODEL}


def music_request_body(prompt: str, duration_s: float) -> dict:
    """The POST /v1/music JSON. Pure (pinned model, instrumental)."""
    length_ms = int(round(float(duration_s) * 1000))
    length_ms = min(600000, max(3000, length_ms))
    return {"prompt": prompt, "music_length_ms": length_ms,
            "model_id": MUSIC_MODEL, "force_instrumental": True}


def _raise_for_status(status: int, *, key: str, what: str) -> None:
    if status in (400, 422):
        raise ElevenLabsError(
            f"elevenlabs: ElevenLabs rejected the {what} request "
            "(prompt or length)", status=status)
    if status in (401, 403):
        raise ElevenLabsError(
            "elevenlabs: ElevenLabs rejected this key "
            "(check it, or its scopes)", status=status)
    if status in (402, 429):
        raise ElevenLabsError(
            "elevenlabs: ElevenLabs quota or rate limit reached", status=status)
    if 400 <= status < 500:
        raise ElevenLabsError(
            f"elevenlabs: ElevenLabs rejected the {what} request "
            "(prompt or length)", status=status)
    raise ElevenLabsError(
        f"elevenlabs: ElevenLabs unavailable (status {status})", status=status)


def _post_audio(*, key: str, url: str, payload: dict, what: str,
               model: str, requested_s: float,
               transport) -> GeneratedAudio:
    body = json.dumps(payload).encode("utf-8")
    try:
        status, _headers, data = transport(
            "POST", url,
            headers={"xi-api-key": key, "Accept": "application/json, audio/mpeg",
                     "Content-Type": "application/json"},
            body=body, timeout=HTTP_TIMEOUT_S)
    except ElevenLabsError:
        raise
    except Exception as error:
        raise ElevenLabsError(
            "elevenlabs: could not reach ElevenLabs "
            "(network or provider error) — try again") from error
    if not 200 <= status < 300:
        try:
            _raise_for_status(status, key=key, what=what)
        except ElevenLabsError as error:
            error.args = (_redact_key(str(error), key),)
            raise
    if not data:
        raise ElevenLabsError("elevenlabs: empty audio returned")
    if what == "music":
        cost = estimate_music_usd(requested_s)
    else:
        cost = estimate_sfx_usd(requested_s)
    return GeneratedAudio(data=bytes(data), model=model,
                          requested_s=requested_s, cost_usd=cost)


def generate_sound_effect(*, key: str, prompt: str, duration_s: float,
                          transport=None) -> GeneratedAudio:
    """POST /v1/sound-generation. transport=None uses http_request."""
    if transport is None:
        transport = http_request
    url = f"{ELEVENLABS_BASE}/v1/sound-generation?output_format={SFX_OUTPUT_FORMAT}"
    payload = sfx_request_body(prompt, duration_s)
    try:
        return _post_audio(key=key, url=url, payload=payload, what="sound",
                           model=SFX_MODEL,
                           requested_s=float(payload["duration_seconds"]),
                           transport=transport)
    except ElevenLabsError as error:
        error.args = (_redact_key(str(error), key),)
        raise


def generate_music(*, key: str, prompt: str, duration_s: float,
                   transport=None) -> GeneratedAudio:
    """POST /v1/music (non-streaming). transport=None uses http_request."""
    if transport is None:
        transport = http_request
    url = f"{ELEVENLABS_BASE}/v1/music?output_format={MUSIC_OUTPUT_FORMAT}"
    payload = music_request_body(prompt, duration_s)
    try:
        return _post_audio(key=key, url=url, payload=payload, what="music",
                           model=MUSIC_MODEL,
                           requested_s=float(payload["music_length_ms"]) / 1000.0,
                           transport=transport)
    except ElevenLabsError as error:
        error.args = (_redact_key(str(error), key),)
        raise


def isolation_body(wav_bytes: bytes, filename: str) -> tuple[bytes, str]:
    """multipart/form-data with the `audio` field only. Pure (no key)."""
    boundary = f"----director-{uuid4().hex}"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="audio"; filename="{filename}"\r\n'
        "Content-Type: audio/wav\r\n"
        "\r\n"
    ).encode("utf-8") + bytes(wav_bytes) + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return body, f"multipart/form-data; boundary={boundary}"


def isolate_audio(*, key: str, wav_path: Path, transport=None) -> GeneratedAudio:
    """POST /v1/audio-isolation. transport=None uses http_request."""
    if transport is None:
        transport = http_request
    wav_bytes = Path(wav_path).read_bytes()
    body, content_type = isolation_body(wav_bytes, Path(wav_path).name)
    url = f"{ELEVENLABS_BASE}/v1/audio-isolation"
    try:
        status, _headers, data = transport(
            "POST", url,
            headers={"xi-api-key": key, "Accept": "application/json, audio/mpeg",
                     "Content-Type": content_type},
            body=body, timeout=HTTP_TIMEOUT_S)
    except ElevenLabsError as error:
        error.args = (_redact_key(str(error), key),)
        raise
    except Exception as error:
        raise ElevenLabsError(
            _redact_key("elevenlabs: could not reach ElevenLabs "
                        "(network or provider error) — try again", key)
        ) from error
    if not 200 <= status < 300:
        try:
            _raise_for_status(status, key=key, what="isolation")
        except ElevenLabsError as error:
            error.args = (_redact_key(str(error), key),)
            raise
    if not data:
        raise ElevenLabsError("elevenlabs: empty audio returned")
    return GeneratedAudio(data=bytes(data), model="audio-isolation",
                          requested_s=0.0, cost_usd=0.0)
