"""Real local WebSocket handshake regressions; no DSH or server access.

The strict synthetic server exposes the invalid empty subprotocol header sent
by websockets.legacy when given [], and exercises the actual entry factory.
"""
import asyncio
from concurrent.futures import Future
from contextlib import contextmanager
from http import HTTPStatus
import json
from pathlib import Path
import sys
from threading import Lock, Thread
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from websockets.exceptions import InvalidStatusCode
from websockets.legacy.client import connect
from websockets.legacy.server import serve

SOURCE = Path(__file__).resolve().parents[1]
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))
from entry_app import Config, create_app  # noqa: E402


@contextmanager
def strict_native_servers():
    ready = Future()
    captures = []
    lock = Lock()

    async def main():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        def record(index):
            async def inspect(path, headers):
                with lock:
                    captures.append({
                        "index": index, "path": path, "host": headers.get("Host"),
                        "origin": headers.get("Origin"), "cookie": headers.get("Cookie"),
                        "protocols": headers.get_all("Sec-WebSocket-Protocol"),
                    })
                if (path != "/api/remote.mux" or headers.get("Host") != "127.0.0.1:3085"
                        or headers.get("Origin") != "http://127.0.0.1:3085"
                        or headers.get("Cookie") != f"dsh_session=synthetic-host-{index}"):
                    return HTTPStatus.FORBIDDEN, [], b"synthetic boundary rejected"
                return None
            return inspect

        async def echo(connection):
            async for message in connection:
                await connection.send(message)

        # Available subprotocols enable strict parsing. Offering none is valid,
        # but sending an empty Sec-WebSocket-Protocol field must be rejected.
        async with serve(echo, "127.0.0.1", 0, process_request=record(0),
                         subprotocols=["synthetic-probe"], close_timeout=1) as a:
            async with serve(echo, "127.0.0.1", 0, process_request=record(1),
                             subprotocols=["synthetic-probe"], close_timeout=1) as b:
                ready.set_result((loop, stop, [a.sockets[0].getsockname()[1], b.sockets[0].getsockname()[1]]))
                await stop.wait()

    def run():
        try:
            asyncio.run(main())
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                raise

    thread = Thread(target=run, daemon=True)
    thread.start()
    loop = stop = None
    try:
        loop, stop, ports = ready.result(timeout=5)
        yield ports, captures
    finally:
        if loop is not None:
            loop.call_soon_threadsafe(stop.set)
        thread.join(timeout=5)
        assert not thread.is_alive(), "synthetic WebSocket servers did not stop"


def test_empty_subprotocol_list_emits_invalid_header_but_none_omits_it():
    with strict_native_servers() as (ports, captures):
        async def run():
            args = {
                "host": "127.0.0.1", "port": ports[0], "origin": "http://127.0.0.1:3085",
                "extra_headers": {"Cookie": "dsh_session=synthetic-host-0"},
                "compression": None, "open_timeout": 3, "close_timeout": 1,
            }
            uri = "ws://127.0.0.1:3085/api/remote.mux"
            with pytest.raises(InvalidStatusCode) as rejected:
                async with connect(uri, subprotocols=[], **args):
                    pytest.fail("empty subprotocol header unexpectedly accepted")
            assert rejected.value.status_code == 400
            async with connect(uri, subprotocols=None, **args) as connection:
                await connection.send("synthetic-wire-echo")
                assert await connection.recv() == "synthetic-wire-echo"
                assert connection.subprotocol is None
        asyncio.run(run())
        assert len(captures) == 2
        assert captures[0]["protocols"] == [""]
        assert captures[1]["protocols"] == []
        assert {item["host"] for item in captures} == {"127.0.0.1:3085"}
        assert {item["origin"] for item in captures} == {"http://127.0.0.1:3085"}


@pytest.mark.parametrize("offered", [None, ["synthetic-probe"]], ids=["absent", "negotiated"])
def test_entry_browser_subprotocol_and_real_tcp_routing(offered):
    with strict_native_servers() as (ports, captures):
        bindings, files = [], {}
        for index in range(2):
            binding = {
                "user_id": str(uuid4()), "user_no": f"WS-PROBE-{index}",
                "upstream": f"http://127.0.0.1:{ports[index]}",
                "bootstrap": f"/run/megmeet-bootstrap/wire-{index}/ready.json",
                "model_token_file": f"/run/megmeet-entry/wire-{index}-model-token",
            }
            bindings.append(binding)
            files[binding["model_token_file"]] = (str(index) * 48).encode()
            files[binding["bootstrap"]] = json.dumps({
                "ready": True, "owner_id": binding["user_id"], "generation": str(uuid4()),
                "workspace_id": str(uuid4()), "cookie": f"dsh_session=synthetic-host-{index}",
            }).encode()
        config = Config.parse({
            "public_origin": "https://platform-ai.megmeet.com",
            "sso_url": "https://sso.probe.invalid/query-user",
            "tokenkey_url": "https://model.probe.invalid/v1",
            "tokenkey_secret_file": "/run/secrets/not-provided-in-wire-test",
            "bindings": bindings,
        })

        def read(path, limit):
            value = files[path]
            assert len(value) <= limit
            return value

        def sso(request):
            assert str(request.url) == config.sso_url
            for index in range(2):
                if request.headers.get("Cookie") == f"access_token=synthetic-user-{index}":
                    return httpx.Response(200, json={"code": 0, "data": {"user_no": bindings[index]["user_no"]}})
            return httpx.Response(403)

        # Use the real default websocket_connect, not a mocked handshake.
        app = create_app(config, reader=read, transport=httpx.MockTransport(sso))
        with TestClient(app) as client:
            for index in range(2):
                with client.websocket_connect("/api/remote.mux", subprotocols=offered, headers={
                    "Origin": config.public_origin,
                    "Cookie": f"access_token=synthetic-user-{index}; dsh_session=foreign",
                    "X-User-ID": bindings[1-index]["user_id"],
                }) as websocket:
                    assert websocket.accepted_subprotocol == (offered[0] if offered else None)
                    websocket.send_text(f"synthetic-owner-{index}")
                    assert websocket.receive_text() == f"synthetic-owner-{index}"
        assert [item["index"] for item in captures] == [0, 1]
        assert all(item["protocols"] == (offered or []) for item in captures)
        assert [item["cookie"] for item in captures] == [
            "dsh_session=synthetic-host-0", "dsh_session=synthetic-host-1",
        ]
