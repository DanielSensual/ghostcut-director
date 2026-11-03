"""Items 1-14 of docs/muse-code/FINISH-DESIGN.md: connections tests.

Offline throughout: a fake Keychain module in sys.modules, fake HTTP
transports at the provider seam, fake env vars. No network, no Meta
API, no real keys (every key here is "test-key-...").
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import engine as engine_mod  # noqa: E402
from director import cli as cli_mod  # noqa: E402
from director import connect as connect_mod  # noqa: E402
from director import elevenlabs as elevenlabs_mod  # noqa: E402
from director import higgsfield as higgsfield_mod  # noqa: E402

ELEVEN_KEY = "test-key-eleven-abc123"
HIGGS_ID = "test-key-higgs-id-1"
HIGGS_SECRET = "test-key-higgs-secret-2"


def make_keyring(store: dict | None = None):
    """An in-memory keyring module replacement."""
    data: dict = store if store is not None else {}

    class PasswordDeleteError(Exception):
        pass

    module = types.ModuleType("keyring")
    module.errors = SimpleNamespace(PasswordDeleteError=PasswordDeleteError)
    module.get_keyring = lambda: object()
    module.get_password = lambda service, account: data.get((service, account))
    module.set_password = (
        lambda service, account, value: data.__setitem__((service, account), value))
    def _delete(service, account):
        try:
            del data[(service, account)]
        except KeyError:
            raise PasswordDeleteError("missing") from None
    module.delete_password = _delete
    module._store = data
    return module


class FakeTransport:
    """A canned provider transport that records every call."""

    def __init__(self, replies: list):
        self.replies = list(replies)
        self.calls: list[tuple] = []

    def __call__(self, method, url, *, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        if len(self.replies) > 1:
            return self.replies.pop(0)
        return self.replies[0]


def clean_env(test: unittest.TestCase, **extra: str):
    """Provider vars removed, then extras added."""
    test.addCleanup(mock.patch.dict(os.environ, {}, clear=False).stop)
    patcher = mock.patch.dict(os.environ, {}, clear=False)
    patcher.start()
    test.addCleanup(patcher.stop)
    for var in ("ELEVENLABS_API_KEY", "HF_API_KEY_ID", "HF_API_KEY_SECRET"):
        os.environ.pop(var, None)
    os.environ.update(extra)


class Ctx:
    """The engine with a throwaway demo dir, through the real dispatch."""

    def __init__(self, test: unittest.TestCase):
        self._tmp = tempfile.TemporaryDirectory(prefix="connect-tests-")
        test.addCleanup(self._tmp.cleanup)
        self.demo = Path(self._tmp.name) / "demo"
        self.demo.mkdir()
        self.engine = engine_mod.Engine(self.demo, render=None)
        self.handler = engine_mod.Handler.__new__(engine_mod.Handler)
        self.handler.engine = self.engine
        self.handler.server = SimpleNamespace(
            server_address=("127.0.0.1", 7791))
        self.answered: list[tuple[int, dict]] = []
        self.handler._json = (
            lambda obj, code=200: self.answered.append((code, obj)))

    def _headers(self, size: int = 0):
        return {"Content-Length": str(size), "Host": "127.0.0.1:7791",
                "Content-Type": "application/json"}

    def post(self, path: str, body: dict):
        import io as _io

        data = json.dumps(body).encode()
        self.handler.path = path
        self.handler.headers = self._headers(len(data))
        self.handler.rfile = _io.BytesIO(data)
        self.handler.do_POST()
        return self.answered[-1]

    def get(self, path: str):
        self.handler.path = path
        self.handler.headers = self._headers()
        self.handler.rfile = io.BytesIO(b"")
        self.handler.do_GET()
        return self.answered[-1]


class RegistryTests(unittest.TestCase):
    """Item 1: registry shape."""

    def test_registry_shape(self) -> None:
        self.assertEqual(list(connect_mod.PROVIDERS), ["elevenlabs", "higgsfield"])
        eleven = connect_mod.PROVIDERS["elevenlabs"]
        self.assertEqual(eleven.capabilities, ("sfx", "music", "isolation"))
        self.assertTrue(eleven.available)
        self.assertEqual(eleven.unavailable_reason, "")
        higgs = connect_mod.PROVIDERS["higgsfield"]
        self.assertEqual(higgs.capabilities, ())
        self.assertFalse(higgs.available)
        self.assertTrue(higgs.unavailable_reason)
        with self.assertRaises(connect_mod.ConnectError) as raised:
            connect_mod.provider_info("nope")
        self.assertEqual(str(raised.exception),
                         "connect: unknown provider 'nope' "
                         "(known: elevenlabs, higgsfield)")


class KeyStorageTests(unittest.TestCase):
    """Items 2-4: env beats Keychain, half-pair, set/remove round-trip."""

    def test_env_beats_keychain(self) -> None:
        clean_env(self)
        store = {("ghostcut-director", "elevenlabs"): "test-key-chain-1"}
        with mock.patch.dict(sys.modules, {"keyring": make_keyring(store)}):
            self.assertEqual(connect_mod.key_status("elevenlabs").source, "keychain")
            self.assertEqual(connect_mod.get_provider_key("elevenlabs"),
                             ("test-key-chain-1",))
            with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": ELEVEN_KEY}):
                self.assertEqual(connect_mod.key_status("elevenlabs").source, "env")
                self.assertEqual(connect_mod.get_provider_key("elevenlabs"),
                                 (ELEVEN_KEY,))
            with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": "   "}):
                self.assertEqual(connect_mod.key_status("elevenlabs").source,
                                 "keychain")
            self.assertEqual(connect_mod.key_status("elevenlabs").source, "keychain")
        with mock.patch.dict(sys.modules, {"keyring": make_keyring({})}):
            self.assertFalse(connect_mod.key_status("elevenlabs").connected)
            self.assertIsNone(connect_mod.get_provider_key("elevenlabs"))

    def test_higgsfield_half_pair(self) -> None:
        clean_env(self, HF_API_KEY_ID=HIGGS_ID)
        with mock.patch.dict(sys.modules, {"keyring": make_keyring({})}):
            self.assertFalse(connect_mod.key_status("higgsfield").connected)
            entries = {entry["id"]: entry
                       for entry in connect_mod.list_providers()}
            self.assertEqual(entries["higgsfield"]["missing"], "HF_API_KEY_SECRET")
            with mock.patch.dict(os.environ, {"HF_API_KEY_SECRET": HIGGS_SECRET}):
                status = connect_mod.key_status("higgsfield")
                self.assertTrue(status.connected)
                self.assertEqual(status.source, "env")
                self.assertEqual(
                    connect_mod.get_provider_key("higgsfield"),
                    (HIGGS_ID, HIGGS_SECRET))

    def test_set_remove_round_trip(self) -> None:
        clean_env(self)
        ring = make_keyring()
        with mock.patch.dict(sys.modules, {"keyring": ring}):
            status = connect_mod.set_provider_key("elevenlabs", (ELEVEN_KEY,))
            self.assertTrue(status.connected)
            self.assertEqual(status.source, "keychain")
            self.assertEqual(ring._store[("ghostcut-director", "elevenlabs")],
                             ELEVEN_KEY)
            self.assertEqual(
                connect_mod.get_provider_key("elevenlabs"), (ELEVEN_KEY,))
            status = connect_mod.remove_provider_key("elevenlabs")
            self.assertFalse(status.connected)
            # Twice is still success.
            status = connect_mod.remove_provider_key("elevenlabs")
            self.assertFalse(status.connected)
            with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": ELEVEN_KEY}):
                status = connect_mod.remove_provider_key("elevenlabs")
                self.assertTrue(status.connected)
                self.assertEqual(status.source, "env")


class NoLeakTests(unittest.TestCase):
    """Items 5-6: payloads and errors never carry key material."""

    def test_no_leak_list_payloads(self) -> None:
        clean_env(self, ELEVENLABS_API_KEY=ELEVEN_KEY,
                  HF_API_KEY_ID=HIGGS_ID, HF_API_KEY_SECRET=HIGGS_SECRET)
        ring = make_keyring({("ghostcut-director", "elevenlabs"): "test-key-chain-9"})
        with mock.patch.dict(sys.modules, {"keyring": ring}):
            payload = connect_mod.list_providers()
            dumped = json.dumps(payload)
            for secret in (ELEVEN_KEY, HIGGS_ID, HIGGS_SECRET, "test-key-chain-9"):
                self.assertNotIn(secret, dumped)
            for entry in payload:
                self.assertNotIn("mask", entry)
                self.assertNotIn("last4", entry)
                self.assertNotIn("fingerprint", entry)
            down = FakeTransport([(500, {}, b"unreachable")])
            with mock.patch.object(elevenlabs_mod, "http_request", down):
                result = connect_mod.test_provider("elevenlabs")
            self.assertNotIn(ELEVEN_KEY, str(result))
            self.assertNotIn(ELEVEN_KEY, json.dumps(
                {"providers": payload,
                 "tests": [{"provider": result.provider, "ok": result.ok,
                            "message": result.message,
                            "detail": result.detail}]}))

    def test_redaction(self) -> None:
        self.assertNotIn("test-key-abc", connect_mod.redact(
            "prefix test-key-abc suffix", ("test-key-abc",)))
        self.assertEqual(connect_mod.redact("nothing here", ("test-key-abc",)),
                         "nothing here")
        # A 401 whose body deliberately contains the key still raises key-free.
        transport = FakeTransport([(401, {}, ELEVEN_KEY.encode())])
        with self.assertRaises(elevenlabs_mod.ElevenLabsError) as raised:
            elevenlabs_mod.generate_sound_effect(
                key=ELEVEN_KEY, prompt="soft whoosh", duration_s=2.0,
                transport=transport)
        self.assertNotIn(ELEVEN_KEY, str(raised.exception))


class ProviderCallTests(unittest.TestCase):
    """Items 7-8: the cheap test calls over a fake transport."""

    def test_elevenlabs_test_call(self) -> None:
        models = {"models": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
        transport = FakeTransport([(200, {}, json.dumps(models).encode())])
        result = elevenlabs_mod.test_connection(ELEVEN_KEY, transport=transport)
        self.assertTrue(result["ok"])
        self.assertEqual(result["message"],
                         "elevenlabs: connected — test ok (3 models visible)")
        self.assertEqual(result["detail"], "3 models visible")
        (method, url, headers, body, _timeout), = transport.calls
        self.assertEqual(method, "GET")
        self.assertEqual(url, "https://api.elevenlabs.io/v1/models")
        self.assertEqual(headers.get("xi-api-key"), ELEVEN_KEY)
        self.assertNotIn(ELEVEN_KEY, url)
        self.assertIsNone(body)
        denied = elevenlabs_mod.test_connection(
            ELEVEN_KEY, transport=FakeTransport([(401, {}, b"{}")]))
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["message"],
                         "elevenlabs: ElevenLabs rejected this key "
                         "(check it, or its scopes) — nothing was generated")
        down = elevenlabs_mod.test_connection(
            ELEVEN_KEY, transport=FakeTransport([(500, {}, b"oops")]))
        self.assertFalse(down["ok"])
        self.assertEqual(down["message"],
                         "elevenlabs: could not reach ElevenLabs "
                         "(network or provider error) — try again")

    def test_higgsfield_test_call(self) -> None:
        url, body = higgsfield_mod.estimate_request(
            higgsfield_mod.HIGGSFIELD_TEST_MODEL, "test")
        self.assertEqual(
            url, "https://api.higgsfield.ai/estimate/higgsfield-ai/soul/v2/standard")
        self.assertEqual(body, {"prompt": "test"})
        transport = FakeTransport([(200, {}, b'{"usd": "0.094"}')])
        result = higgsfield_mod.test_connection(
            HIGGS_ID, HIGGS_SECRET, transport=transport)
        self.assertTrue(result["ok"])
        self.assertEqual(result["message"],
                         "higgsfield: connected — test ok (estimate $0.094)")
        (method, seen_url, headers, seen_body, _timeout), = transport.calls
        self.assertEqual(method, "POST")
        self.assertEqual(seen_url, url)
        self.assertEqual(headers.get("Authorization"),
                         f"Key {HIGGS_ID}:{HIGGS_SECRET}")
        self.assertNotIn(HIGGS_ID, seen_url)
        self.assertNotIn(HIGGS_SECRET, seen_url)
        self.assertEqual(json.loads(seen_body.decode()), {"prompt": "test"})
        denied = higgsfield_mod.test_connection(
            HIGGS_ID, HIGGS_SECRET,
            transport=FakeTransport([(401, {}, b"{}")]))
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["message"],
                         "higgsfield: Higgsfield rejected this key pair "
                         "(check the ID and secret) — nothing was generated")
        broke = higgsfield_mod.test_connection(
            HIGGS_ID, HIGGS_SECRET,
            transport=FakeTransport([(403, {}, b"{}")]))
        self.assertFalse(broke["ok"])
        self.assertEqual(broke["message"],
                         "higgsfield: key accepted, but the account balance "
                         "cannot run models — add funds, then test again")
        self.assertNotIn("rejected key", broke["message"])
        gone = higgsfield_mod.test_connection(
            HIGGS_ID, HIGGS_SECRET,
            transport=FakeTransport([(404, {}, b"{}")]))
        self.assertFalse(gone["ok"])
        self.assertEqual(gone["message"],
                         "higgsfield: key accepted, but the test model is "
                         "unavailable right now — try again later")


class ConnectCliTests(unittest.TestCase):
    """Item 9: the connect command."""

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_mod.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_add_test_remove_list(self) -> None:
        clean_env(self)
        ring = make_keyring()
        models = {"models": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
        transport = FakeTransport([(200, {}, json.dumps(models).encode())])
        with (mock.patch.dict(sys.modules, {"keyring": ring}),
              mock.patch.object(elevenlabs_mod, "http_request", transport),
              mock.patch("getpass.getpass", return_value=ELEVEN_KEY)):
            code, out, _err = self._run(["connect", "add", "elevenlabs"])
        self.assertEqual(code, 0)
        self.assertIn("Saved ElevenLabs key to the macOS Keychain.", out)
        self.assertIn("elevenlabs: connected — test ok (3 models visible)", out)
        self.assertEqual(ring._store[("ghostcut-director", "elevenlabs")],
                         ELEVEN_KEY)
        self.assertNotIn(ELEVEN_KEY, out)
        shadow_transport = FakeTransport(
            [(200, {}, json.dumps(models).encode())])
        with (mock.patch.dict(sys.modules, {"keyring": ring}),
              mock.patch.object(elevenlabs_mod, "http_request",
                                shadow_transport),
              mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": ELEVEN_KEY}),
              mock.patch("getpass.getpass", return_value="test-key-other")):
            code, out, _err = self._run(
                ["connect", "add", "elevenlabs", "--key", "test-key-other"])
        self.assertEqual(code, 0)
        self.assertIn("Note: ELEVENLABS_API_KEY is set, so it will be used "
                      "instead of the saved key.", out)
        clean_env(self)
        with mock.patch.dict(sys.modules, {"keyring": make_keyring({})}):
            code, _out, err = self._run(["connect", "test", "elevenlabs"])
            self.assertEqual(code, 1)
            self.assertIn("elevenlabs: not connected — set ELEVENLABS_API_KEY or: "
                          "director connect add elevenlabs", err)
            with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": ELEVEN_KEY}):
                code, out, _err = self._run(["connect", "remove", "elevenlabs"])
                self.assertEqual(code, 0)
                self.assertIn("ElevenLabs is connected through ELEVENLABS_API_KEY; "
                              "unset it to disconnect.", out)
            code, out, _err = self._run(["connect", "list", "--json"])
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual([entry["id"] for entry in payload["providers"]],
                             ["elevenlabs", "higgsfield"])
            self.assertNotIn(ELEVEN_KEY, out)


class KeychainAbsentTests(unittest.TestCase):
    """Item 10: everything but save/remove works without keyring."""

    def test_degradation(self) -> None:
        clean_env(self, ELEVENLABS_API_KEY=ELEVEN_KEY)
        with mock.patch.dict(sys.modules, {"keyring": None}):
            self.assertFalse(connect_mod.keychain_available())
            status = connect_mod.key_status("elevenlabs")
            self.assertTrue(status.connected)
            self.assertEqual(status.source, "env")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli_mod.main(["connect", "list"])
            self.assertEqual(code, 0)
            self.assertIn("elevenlabs: connected (env ELEVENLABS_API_KEY)", out.getvalue())
            with self.assertRaises(connect_mod.ConnectError) as raised:
                connect_mod.set_provider_key("elevenlabs", (ELEVEN_KEY,))
            self.assertEqual(
                str(raised.exception),
                "connect: the macOS Keychain is unavailable; install the "
                'connection extra: pip install -e ".[connect]" — '
                "or set ELEVENLABS_API_KEY instead")
            ctx = Ctx(self)
            code, payload = ctx.post(
                "/api/connections/add",
                {"provider": "elevenlabs", "key": "test-key-x"})
            self.assertEqual(code, 400)
            self.assertIn("the macOS Keychain is unavailable",
                          payload.get("error", ""))


class EngineTests(unittest.TestCase):
    """Item 11: the Connections engine endpoints."""

    def test_connections_endpoints(self) -> None:
        clean_env(self)
        ring = make_keyring()
        models = {"models": [{"id": "a"}]}
        transport = FakeTransport([(200, {}, json.dumps(models).encode())])
        with (mock.patch.dict(sys.modules, {"keyring": ring}),
              mock.patch.object(elevenlabs_mod, "http_request", transport)):
            ctx = Ctx(self)
            code, payload = ctx.get("/api/connections")
            self.assertEqual(code, 200)
            self.assertEqual([entry["id"] for entry in payload["providers"]],
                             ["elevenlabs", "higgsfield"])
            code, payload = ctx.post("/api/connections/add",
                                     {"provider": "elevenlabs",
                                      "key": ELEVEN_KEY})
            self.assertEqual(code, 200)
            entry = {e["id"]: e for e in payload["providers"]}["elevenlabs"]
            self.assertTrue(entry["connected"])
            self.assertEqual(entry["source"], "keychain")
            self.assertEqual(ring._store[("ghostcut-director", "elevenlabs")],
                             ELEVEN_KEY)
            self.assertNotIn(ELEVEN_KEY, json.dumps(payload))
            code, payload = ctx.post("/api/connections/test",
                                     {"provider": "elevenlabs"})
            self.assertEqual(code, 200)
            (method, url, _h, _b, _t), = transport.calls
            self.assertEqual((method, url),
                             ("GET", "https://api.elevenlabs.io/v1/models"))
            self.assertTrue(payload["tests"][0]["ok"])
            code, payload = ctx.post("/api/connections/remove",
                                     {"provider": "elevenlabs"})
            self.assertEqual(code, 200)
            entry = {e["id"]: e for e in payload["providers"]}["elevenlabs"]
            self.assertFalse(entry["connected"])
            code, payload = ctx.post("/api/connections/add",
                                     {"provider": "nope", "key": "x"})
            self.assertEqual(code, 400)
            self.assertEqual(payload["error"],
                             "connect: unknown provider 'nope' "
                             "(known: elevenlabs, higgsfield)")
            self.assertNotIn(ELEVEN_KEY, json.dumps(ctx.answered))


class ProviderShapeTests(unittest.TestCase):
    """Items 12-14: multipart body, request builders, estimates."""

    def test_isolation_body(self) -> None:
        body, content_type = elevenlabs_mod.isolation_body(
            b"WAVBYTES", "voice.wav")
        self.assertIn(b'name="audio"', body)
        self.assertIn(b'filename="voice.wav"', body)
        self.assertIn(b"WAVBYTES", body)
        self.assertIn("multipart/form-data; boundary=", content_type)
        self.assertNotIn(b"file_format", body)

    def test_request_builders(self) -> None:
        sfx = elevenlabs_mod.sfx_request_body("soft whoosh", 2.0)
        self.assertEqual(sfx["model_id"], "eleven_text_to_sound_v3")
        self.assertFalse(sfx["loop"])
        self.assertEqual(sfx["prompt_influence"], 0.7)
        self.assertEqual(sfx["duration_seconds"], 2.0)
        recorded = FakeTransport([(200, {}, b"MP3")])
        elevenlabs_mod.generate_sound_effect(
            key=ELEVEN_KEY, prompt="soft whoosh", duration_s=2.0,
            transport=recorded)
        (_m, url, headers, raw, _t), = recorded.calls
        self.assertIn("output_format=mp3_44100_128", url)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        sent = json.loads(raw.decode())
        self.assertEqual(sent["model_id"], "eleven_text_to_sound_v3")
        music = elevenlabs_mod.music_request_body("warm pads", 2.0)
        self.assertEqual(music["model_id"], "music_v2_5")
        self.assertTrue(music["force_instrumental"])
        self.assertEqual(music["music_length_ms"], 3000)
        self.assertEqual(
            elevenlabs_mod.music_request_body("warm pads", 700.0)["music_length_ms"],
            600000)
        recorded = FakeTransport([(200, {}, b"MP3")])
        elevenlabs_mod.generate_music(
            key=ELEVEN_KEY, prompt="warm pads", duration_s=9.0,
            transport=recorded)
        (_m, url, _h, raw, _t), = recorded.calls
        self.assertIn("output_format=mp3_48000_192", url)
        self.assertEqual(json.loads(raw.decode())["music_length_ms"], 9000)

    def test_estimates(self) -> None:
        self.assertEqual(elevenlabs_mod.estimate_sfx_usd(30), 0.06)
        self.assertEqual(elevenlabs_mod.estimate_music_usd(60), 0.15)
        self.assertEqual(elevenlabs_mod.estimate_isolation_usd(120), 0.24)
        self.assertEqual(elevenlabs_mod.estimate_sfx_usd(0), 0.0)
        self.assertEqual(elevenlabs_mod.estimate_music_usd(-5), 0.0)
        self.assertEqual(elevenlabs_mod.estimate_isolation_usd(-1), 0.0)


if __name__ == "__main__":
    unittest.main()
