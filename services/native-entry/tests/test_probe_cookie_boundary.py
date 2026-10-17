"""Cookie-denial regression checks for the real-DSH probe; no Docker/network."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

import httpx

SOURCE = Path(__file__).resolve().parents[1] / "probe_client.py"
if str(SOURCE.parent) not in sys.path:
    sys.path.insert(0, str(SOURCE.parent))
SPEC = importlib.util.spec_from_file_location("tested_probe_cookie_boundary_client", SOURCE)
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)

BINDINGS = (
    SimpleNamespace(upstream="http://host-a:3084"),
    SimpleNamespace(upstream="http://host-b:3084"),
)
READIES = (
    {"cookie": "dsh_session=private_A_fixture"},
    {"cookie": "dsh_session=private_B_fixture"},
)


class DirectClient:
    """Only send(Request) is supported, so high-level cookie merging fails."""
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.requests = []
        self.entered = 0
        self.exited = 0
        self.cookies = httpx.Cookies()
        for name in ("host-a", "host-b"):
            self.cookies.set("dsh_session", "client_jar_cookie_must_not_merge", domain=name, path="/")
        self.headers = httpx.Headers({"Cookie": "client_header_must_not_merge",
                                     "Authorization": "Bearer_client_header_must_not_merge"})

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, *_args):
        self.exited += 1

    async def send(self, request):
        if not isinstance(request, httpx.Request):
            raise AssertionError("The denial probe must construct an explicit httpx.Request")
        self.requests.append(request)
        if not self.statuses:
            raise AssertionError("Unexpected request after the first failed boundary check")
        response = httpx.Response(self.statuses.pop(0), request=request,
            headers={"Set-Cookie": "dsh_session=response_cookie_must_not_merge; Path=/; HttpOnly"},
            content=b"private response body must not be printed")
        # Real AsyncClient.send also saves response cookies. They must never be
        # merged into a later anonymous or explicitly foreign-cookie Request.
        self.cookies.extract_cookies(response)
        return response


class NativeCookieBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, statuses, *, phase="initial"):
        client = DirectClient(statuses)
        output = io.StringIO()
        with mock.patch.object(probe.httpx, "AsyncClient", return_value=client) as factory:
            with contextlib.redirect_stdout(output):
                await probe.native_cookie_boundary(BINDINGS, READIES, phase)
        factory.assert_called_once_with(trust_env=False, follow_redirects=False, timeout=5)
        return client, output.getvalue()

    async def test_all_eight_real_destinations_receive_internal_authority_and_correct_cookie(self):
        client, output = await self.invoke([401] * 8)
        self.assertEqual((client.entered, client.exited), (1, 1))
        self.assertEqual(len(client.requests), 8)
        observed = set()
        for request in client.requests:
            self.assertEqual(request.headers["Host"], "127.0.0.1:3085")
            self.assertEqual(request.headers["Origin"], "http://127.0.0.1:3085")
            self.assertEqual(request.headers["Accept-Encoding"], "identity")
            self.assertEqual(request.url.port, 3084)
            self.assertEqual(request.url.query, b"")
            index = 0 if request.url.host == "host-a" else 1
            sent_cookie = request.headers.get("Cookie")
            self.assertIn(sent_cookie, (None, READIES[1 - index]["cookie"]))
            observed.add((request.url.host, request.method, request.url.path, sent_cookie))
            if request.method == "POST":
                self.assertEqual(request.url.path, "/api/workspace/create")
                self.assertEqual(request.headers["Content-Type"], "application/json")
                body = json.loads(request.content)
                self.assertEqual(body["type"], "client-request")
                self.assertEqual(body["method"], "workspace/create")
                self.assertTrue(body["rpcId"])
                self.assertEqual(body["payload"], {"args": {"request": {"path": "/state/work"}}})
            else:
                self.assertEqual((request.method, request.url.path, request.content), ("GET", "/", b""))
        expected = {
            (name, method, path, value)
            for index, name in enumerate(("host-a", "host-b"))
            for value in (None, READIES[1 - index]["cookie"])
            for method, path in (("GET", "/"), ("POST", "/api/workspace/create"))
        }
        self.assertEqual(observed, expected)
        self.assertEqual(output, "NATIVE_ANONYMOUS_AND_FOREIGN_COOKIES_REJECTED_INITIAL: PASS\n")

    async def test_no_client_cookie_jar_or_default_header_can_enter_any_request(self):
        client, output = await self.invoke([401] * 8)
        for request in client.requests:
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("client_jar_cookie", str(request.headers))
            self.assertNotIn("client_header", str(request.headers))
            self.assertNotIn("response_cookie", str(request.headers))
        for value in ("private_A_fixture", "private_B_fixture", "private response", "response_cookie"):
            self.assertNotIn(value, output)

    async def test_a_200_at_any_position_preserves_failure_and_never_reports_pass(self):
        for position in range(8):
            with self.subTest(position=position):
                client = DirectClient([401] * position + [200])
                output = io.StringIO()
                with mock.patch.object(probe.httpx, "AsyncClient", return_value=client):
                    with contextlib.redirect_stdout(output):
                        with self.assertRaisesRegex(probe.Failure, "^NATIVE_PRIVATE_COOKIE_BOUNDARY$"):
                            await probe.native_cookie_boundary(BINDINGS, READIES, "initial")
                self.assertEqual(len(client.requests), position + 1)
                self.assertEqual(client.exited, 1)
                self.assertNotIn("PASS", output.getvalue())
                self.assertNotIn("private response", output.getvalue())

    async def test_other_non401_statuses_cannot_masquerade_as_authentication_rejection(self):
        for status in (302, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                client = DirectClient([status])
                with mock.patch.object(probe.httpx, "AsyncClient", return_value=client):
                    with self.assertRaisesRegex(probe.Failure, "^NATIVE_PRIVATE_COOKIE_BOUNDARY$"):
                        await probe.native_cookie_boundary(BINDINGS, READIES, "initial")
                self.assertEqual(len(client.requests), 1)
                self.assertEqual(client.exited, 1)

    async def test_recreated_host_phase_has_exactly_one_independent_success_marker(self):
        client, output = await self.invoke([401] * 8, phase="restored")
        self.assertEqual(len(client.requests), 8)
        self.assertEqual(output, "NATIVE_ANONYMOUS_AND_FOREIGN_COOKIES_REJECTED_RESTORED: PASS\n")
        self.assertEqual(output.count(": PASS"), 1)


if __name__ == "__main__":
    unittest.main()
