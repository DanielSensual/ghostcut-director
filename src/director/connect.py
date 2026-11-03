"""Outside-service connections: a provider registry with secure key storage.

Each provider is one entry in `PROVIDERS` plus one module (`elevenlabs`,
`higgsfield`): a name, the environment variables holding its key, the
macOS Keychain accounts holding it otherwise, a cheap test call, and the
capabilities it offers. Keys live in the environment (which wins) or the
macOS Keychain through the optional `keyring` package — never in the
repo, a job file, a log line, a URL or an error message, and no surface
ever shows a saved key back.

Designed by Muse Code on muse-wonder-1.0, built by Muse Code on
muse-spark-1.3 from docs/muse-code/FINISH-DESIGN.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path  # noqa: F401 (re-exported for provider modules' typing)
import os
import re  # noqa: F401 (kept: registry-adjacent helpers share this import set)

CONNECT_SERVICE = "ghostcut-director"


class ConnectError(RuntimeError):
    """A connection operation failed in a way the operator must see.

    The message always starts with 'connect: ' and never contains a key.
    """


@dataclass(slots=True)
class ProviderInfo:
    id: str
    display_name: str
    env_vars: tuple[str, ...]
    keychain_accounts: tuple[str, ...]
    capabilities: tuple[str, ...]
    available: bool
    unavailable_reason: str


PROVIDERS: dict[str, ProviderInfo] = {
    "elevenlabs": ProviderInfo(
        id="elevenlabs",
        display_name="ElevenLabs",
        env_vars=("ELEVENLABS_API_KEY",),
        keychain_accounts=("elevenlabs",),
        capabilities=("sfx", "music", "isolation"),
        available=True,
        unavailable_reason="",
    ),
    "higgsfield": ProviderInfo(
        id="higgsfield",
        display_name="Higgsfield",
        env_vars=("HF_API_KEY_ID", "HF_API_KEY_SECRET"),
        keychain_accounts=("higgsfield-id", "higgsfield-secret"),
        capabilities=(),
        available=False,
        unavailable_reason=(
            "no finishing capability yet (image/video generation only) "
            "— see FINISH-DESIGN §1.6"
        ),
    ),
}


@dataclass(slots=True)
class KeyStatus:
    provider: str
    connected: bool
    source: str  # "env" | "keychain" | "none"
    env_var: str  # first env var name (for messages); "" if none


@dataclass(slots=True)
class TestResult:
    provider: str
    ok: bool
    message: str  # editor-readable, key-free
    detail: str = ""  # e.g. "3 models visible" — never a key


def provider_info(provider: str) -> ProviderInfo:
    """PROVIDERS[provider], else the unknown-provider ConnectError. Pure."""
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise ConnectError(
            f"connect: unknown provider '{provider}' "
            "(known: elevenlabs, higgsfield)"
        ) from None


def keychain_available() -> bool:
    """Whether a Keychain backend can be reached. Never raises."""
    try:
        import keyring

        return keyring.get_keyring() is not None
    except Exception:
        return False


def _keychain():  # noqa: ANN202 (keyring is an optional import)
    """The keyring module, or raise the install ConnectError."""
    try:
        import keyring

        if keyring.get_keyring() is None:
            raise ImportError("no keyring backend")
        return keyring
    except Exception as error:
        raise ConnectError(
            "connect: the macOS Keychain is unavailable; install the "
            'connection extra: pip install -e ".[connect]"'
        ) from error


def _install_message(info: ProviderInfo) -> str:
    vars_text = " and ".join(info.env_vars)
    return (
        "connect: the macOS Keychain is unavailable; install the "
        'connection extra: pip install -e ".[connect]" — '
        f"or set {vars_text} instead"
    )


def _read_env(info: ProviderInfo) -> tuple[str, ...] | None:
    """The stripped env key parts, or None when any part is missing/empty."""
    parts: list[str] = []
    for var in info.env_vars:
        value = os.environ.get(var, "")
        if not value.strip():
            return None
        parts.append(value.strip())
    return tuple(parts)


def _read_keychain(info: ProviderInfo) -> tuple[str, ...] | None:
    """The stripped Keychain key parts, or None when unavailable/incomplete."""
    if not keychain_available():
        return None
    try:
        import keyring
    except Exception:
        return None
    parts: list[str] = []
    for account in info.keychain_accounts:
        try:
            value = keyring.get_password(CONNECT_SERVICE, account)
        except Exception:
            return None
        if not (value or "").strip():
            return None
        parts.append((value or "").strip())
    return tuple(parts)


def key_status(provider: str) -> KeyStatus:
    """Env first, then Keychain. Never returns key material."""
    info = provider_info(provider)
    if _read_env(info) is not None:
        return KeyStatus(provider, True, "env", info.env_vars[0])
    if _read_keychain(info) is not None:
        return KeyStatus(provider, True, "keychain", info.env_vars[0])
    return KeyStatus(provider, False, "none", info.env_vars[0])


def get_provider_key(provider: str) -> tuple[str, ...] | None:
    """The key parts in env_vars order, or None when unconnected.

    The only function that returns secrets; callers must not log,
    store or serialise the result.
    """
    info = provider_info(provider)
    found = _read_env(info)
    if found is not None:
        return found
    return _read_keychain(info)


def set_provider_key(provider: str, parts: tuple[str, ...]) -> KeyStatus:
    """Validate and save key parts to the Keychain. Returns the status."""
    info = provider_info(provider)
    cleaned = tuple(part.strip() for part in parts)
    if len(cleaned) != len(info.env_vars) or any(not part for part in cleaned):
        if len(info.env_vars) > 1:
            missing = [
                var
                for var, part in zip(info.env_vars, list(cleaned) + [""] * len(info.env_vars))
                if not part
            ]
            raise ConnectError(
                f"connect: {info.display_name} needs both "
                f"{' and '.join(info.env_vars)} "
                f"(missing: {', '.join(missing)})"
            )
        raise ConnectError("connect: that key was empty; nothing was saved")
    try:
        ring = _keychain()
    except ConnectError:
        raise ConnectError(_install_message(info)) from None
    for account, part in zip(info.keychain_accounts, cleaned):
        ring.set_password(CONNECT_SERVICE, account, part)
    return key_status(provider)


def remove_provider_key(provider: str) -> KeyStatus:
    """Delete the Keychain entries (missing entries are success).

    An env connection stays connected: the caller tells the editor to
    unset it. With no Keychain backend and no env connection there is
    nothing Director can verify, so removing fails with the install
    message.
    """
    info = provider_info(provider)
    if _read_env(info) is not None:
        if keychain_available():
            try:
                ring = _keychain()
            except ConnectError:
                ring = None
            if ring is not None:
                for account in info.keychain_accounts:
                    try:
                        ring.delete_password(CONNECT_SERVICE, account)
                    except Exception:
                        pass
        return key_status(provider)
    if not keychain_available():
        raise ConnectError(_install_message(info))
    ring = _keychain()
    for account in info.keychain_accounts:
        try:
            ring.delete_password(CONNECT_SERVICE, account)
        except Exception:
            pass
    return key_status(provider)


def _missing_env_var(info: ProviderInfo) -> str:
    """The first unset env var, or "" when none/set-complete (half-pair aid)."""
    missing = [var for var in info.env_vars if not os.environ.get(var, "").strip()]
    if missing and len(missing) < len(info.env_vars):
        return missing[0]
    return ""


def list_providers() -> list[dict]:
    """One key-free dict per provider, in registry order, for CLI/engine."""
    out: list[dict] = []
    for info in PROVIDERS.values():
        status = key_status(info.id)
        out.append({
            "id": info.id,
            "name": info.display_name,
            "connected": status.connected,
            "source": status.source,
            "env_vars": list(info.env_vars),
            "capabilities": list(info.capabilities),
            "available": info.available,
            "unavailable_reason": info.unavailable_reason,
            "missing": _missing_env_var(info),
        })
    return out


def _not_connected_message(info: ProviderInfo) -> str:
    vars_text = " and ".join(info.env_vars)
    return (
        f"{info.id}: not connected — set {vars_text} or: "
        f"director connect add {info.id}"
    )


def test_provider(provider: str) -> TestResult:
    """Run the provider's cheap test call. No key → ok False, not an error."""
    info = provider_info(provider)
    parts = get_provider_key(provider)
    if parts is None:
        return TestResult(provider, False, _not_connected_message(info))
    try:
        if provider == "elevenlabs":
            from . import elevenlabs as provider_mod

            result = provider_mod.test_connection(parts[0])
        elif provider == "higgsfield":
            from . import higgsfield as provider_mod

            result = provider_mod.test_connection(parts[0], parts[1])
        else:  # pragma: no cover (registry dispatch is exhaustive)
            raise ConnectError(
                f"connect: unknown provider '{provider}' "
                "(known: elevenlabs, higgsfield)"
            )
    except ConnectError:
        raise
    except RuntimeError as error:
        return TestResult(provider, False, redact(str(error), parts))
    return TestResult(
        provider,
        bool(result.get("ok")),
        redact(str(result.get("message", "")), parts),
        redact(str(result.get("detail", "")), parts),
    )


def redact(text: str, secrets: tuple[str, ...]) -> str:
    """Replace every non-empty secret substring with ••••. Pure."""
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "••••")
    return out
