"""Unit tests for the offline native DSH Web probe.

These tests exercise only the probe's standard-library helpers.  They never
start Docker, a DSH process, or a network listener.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request


PROBE_PATH = Path(__file__).parents[1] / "probe-dsh-native-web.py"
SPEC = importlib.util.spec_from_file_location("probe_dsh_native_web", PROBE_PATH)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class LocalUrlTests(unittest.TestCase):
    def test_local_and_external_urls(self):
        self.assertEqual(
            probe.local_url("/assets/app.js"),
            "http://127.0.0.1:13090/assets/app.js",
        )
        with self.assertRaisesRegex(probe.ProbeFailure, "NON_LOCAL_RESOURCE"):
            probe.local_url("https://evil.example/a.js")
        with self.assertRaisesRegex(probe.ProbeFailure, "NON_LOCAL_RESOURCE"):
            probe.local_url("//evil.example/a.js")

    def test_redirect_handler_rejects_external_redirect(self):
        handler = probe.LocalRedirect()
        with self.assertRaisesRegex(probe.ProbeFailure, "NON_LOCAL_RESOURCE"):
            handler.redirect_request(Request(probe.BASE), None, 302, "Found", {}, "https://evil.example/")


class HtmlContractTests(unittest.TestCase):
    def test_boot_graph_accepts_both_supported_global_forms(self):
        for assignment in (
            'window.__DSH_BOOT__ = {"entries": []};',
            'globalThis["__DSH_BOOT__"] = {"entries": [{"id": "chat"}]};',
        ):
            graph = probe.boot_graph("<script>" + assignment + "</script>")
            self.assertIsInstance(graph["entries"], list)

    def test_boot_graph_does_not_eval_malformed_javascript(self):
        source = 'globalThis["__DSH_BOOT__"] = not_json; __import__("os").system("touch /tmp/should-not-exist")'
        # If this helper ever regresses to eval/exec, the patched side effect
        # makes the test fail instead of silently running attacker input.
        with patch("os.system", side_effect=AssertionError("unexpected execution")):
            with self.assertRaisesRegex(probe.ProbeFailure, "BOOT_GRAPH_JSON"):
                probe.boot_graph(source)

    def test_resource_extraction_is_limited_to_boot_assets(self):
        parser = probe.Resources()
        parser.feed(
            '<script src="./assets/app.js"></script>'
            '<link rel="stylesheet" href="./assets/app.css">'
            '<link rel="modulepreload" href="/plugins/combo.js">'
            '<img src="ignored.png">'
        )
        self.assertEqual(
            parser.urls,
            {"./assets/app.js", "./assets/app.css", "/plugins/combo.js"},
        )


class OutputSafetyTests(unittest.TestCase):
    def test_launch_url_never_writes_token(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = probe.launch_url("dsh web: http://127.0.0.1:13090/?token=SECRET_TOKEN")
        self.assertEqual(result, "http://127.0.0.1:13090/?token=SECRET_TOKEN")
        self.assertEqual(output.getvalue(), "")

    def test_diagnostics_allowlist_hides_raw_sensitive_log(self):
        output = io.StringIO()
        log = (
            "Authorization: Bearer SUPER_SECRET_TOKEN\n"
            "DSH_HOME=/private/path\n"
            "ENOENT Cannot find package '@deepseek-ai/dsh-client-ui-chat'\n"
        )
        with redirect_stdout(output):
            probe.diagnostics(log)
        text = output.getvalue()
        self.assertIn("RUNTIME_ERROR_CODES: ENOENT", text)
        self.assertIn("MISSING_PACKAGES: @deepseek-ai/dsh-client-ui-chat", text)
        self.assertNotIn("SUPER_SECRET_TOKEN", text)
        self.assertNotIn("/private/path", text)


class FakeSocket:
    def __init__(self, valid_accept: bool = True):
        self.valid_accept = valid_accept
        self.request = b""
        self.response = b""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def sendall(self, payload: bytes):
        self.request += payload
        headers = dict(
            line.split(": ", 1)
            for line in payload.decode("ascii").split("\r\n")
            if ": " in line
        )
        key = headers["Sec-WebSocket-Key"]
        accept = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        if not self.valid_accept:
            accept = "invalid"
        self.response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("ascii")

    def recv(self, _size: int) -> bytes:
        response, self.response = self.response, b""
        return response


class WebSocketTests(unittest.TestCase):
    def test_valid_upgrade_and_cookie(self):
        fake = FakeSocket()
        with patch.object(probe.socket, "create_connection", return_value=fake):
            self.assertEqual(probe.websocket_status("dsh-session=opaque"), 101)
        self.assertIn(b"Cookie: dsh-session=opaque", fake.request)
        self.assertIn(b"GET /api/remote.mux HTTP/1.1", fake.request)

    def test_invalid_accept_is_rejected(self):
        fake = FakeSocket(valid_accept=False)
        with patch.object(probe.socket, "create_connection", return_value=fake):
            with self.assertRaisesRegex(probe.ProbeFailure, "WEBSOCKET_ACCEPT"):
                probe.websocket_status()


if __name__ == "__main__":
    unittest.main()
