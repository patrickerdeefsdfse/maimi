"""Exercise the shipped offline transport over real local TCP, without DSH.

This guards request forwarding/routing, not the DSH Origin implementation.
Both upstreams and every credential used here are synthetic and process-local.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from threading import Lock, Thread
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi.testclient import TestClient

SOURCE = Path(__file__).resolve().parents[1]
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from entry_app import Config, create_app  # noqa: E402
from probe_client import OfflineTransport, envelope  # noqa: E402


PUBLIC = "https://platform-ai.megmeet.com"


@contextmanager
def tcp_pilot():
    """Use the exact production factory and the shipped offline SSO transport."""
    calls = []
    lock = Lock()
    servers, threads = [], []
    identities = [
        {"cookie": "synthetic-entry-alice", "user_no": "TCP-PROBE-A"},
        {"cookie": "synthetic-entry-bob", "user_no": "TCP-PROBE-B"},
    ]
    files = {}
    bindings = []

    def native_handler(index, workspace_id, expected_cookie):
        class Native(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                request_body = json.loads(raw)
                with lock:
                    calls.append({
                        "index": index,
                        "path": self.path,
                        "headers": {name.lower(): value for name, value in self.headers.items()},
                        "body": request_body,
                    })
                valid = (
                    urlsplit(self.path).path == "/api/workspace/create"
                    and request_body == envelope(request_body.get("rpcId"))
                    and self.headers.get("Cookie") == expected_cookie
                    and self.headers.get("Origin") == "http://127.0.0.1:3085"
                    and self.headers.get("Host") == "127.0.0.1:3085"
                )
                if valid:
                    response_body = {
                        "type": "server-response", "rpcId": request_body["rpcId"],
                        "result": {"ok": True, "value": {
                            "created": False,
                            "workspace": {"workspaceId": workspace_id, "path": "/state/work"},
                        }},
                    }
                else:
                    response_body = {"detail": "synthetic upstream refused invalid request"}
                body = json.dumps(response_body).encode()
                self.send_response(200 if valid else 403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Native

    try:
        for index in range(2):
            owner = str(uuid4())
            workspace_id = str(uuid4())
            cookie = f"dsh_session=synthetic-host-{index}"
            server = ThreadingHTTPServer(("127.0.0.1", 0), native_handler(index, workspace_id, cookie))
            server.daemon_threads = False
            servers.append(server)
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            threads.append(thread)
            binding = {
                "user_id": owner, "user_no": identities[index]["user_no"],
                "upstream": f"http://127.0.0.1:{server.server_port}",
                "bootstrap": f"/run/megmeet-bootstrap/tcp-{index}/ready.json",
                "model_token_file": f"/run/megmeet-entry/tcp-{index}-model-token",
            }
            bindings.append(binding)
            files[binding["bootstrap"]] = json.dumps({
                "ready": True, "owner_id": owner, "generation": str(uuid4()),
                "workspace_id": workspace_id, "cookie": cookie,
            }).encode()
            files[binding["model_token_file"]] = (str(index) * 48).encode()

        config = Config.parse({
            "public_origin": PUBLIC,
            "sso_url": "https://sso.probe.invalid/query-user",
            "tokenkey_url": "https://model.probe.invalid/v1",
            "tokenkey_secret_file": "/run/secrets/not-provided-in-tcp-test",
            "bindings": bindings,
        })

        def reader(path, limit):
            value = files[path]
            if len(value) > limit:
                raise ValueError("synthetic private file exceeds limit")
            return value

        # Do not replace OfflineTransport.real: actual AsyncHTTPTransport sends
        # HTTP to the selected loopback server. Only SSO is synthesized by the
        # same OfflineTransport implementation shipped in the server probe.
        transport = OfflineTransport(config, identities)
        with TestClient(create_app(config, transport=transport, reader=reader)) as client:
            yield client, config, identities, files, calls
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
        assert all(not thread.is_alive() for thread in threads), "test server did not stop"


def test_exact_offline_transport_real_tcp_routes_and_removes_forged_identity():
    with tcp_pilot() as (client, config, identities, files, calls):
        for index, identity in enumerate(identities):
            binding = config.bindings[index]
            foreign = config.bindings[1 - index]
            ready = json.loads(files[binding.bootstrap])
            foreign_ready = json.loads(files[foreign.bootstrap])
            rpc_id = f"real-tcp-{index}"
            response = client.post(
                "/api/workspace/create?owner_id=" + foreign.user_id,
                headers={
                    "Cookie": "access_token=" + identity["cookie"] + "; " + foreign_ready["cookie"],
                    "Origin": PUBLIC,
                    "Host": "attacker.invalid",
                    "Authorization": "Bearer browser-cannot-pick-host",
                    "X-User-ID": foreign.user_id,
                    "X-User-No": foreign.user_no,
                    "X-Forwarded-Host": urlsplit(foreign.upstream).netloc,
                    "Forwarded": "host=attacker.invalid",
                },
                json=envelope(rpc_id),
            )
            assert response.status_code == 200
            result = response.json()
            assert result["rpcId"] == rpc_id
            assert result["result"]["value"]["workspace"]["workspaceId"] == ready["workspace_id"]
            record = calls[-1]
            assert record["index"] == index
            assert urlsplit(record["path"]).path == "/api/workspace/create"
            assert record["body"] == envelope(rpc_id)
            assert record["headers"]["cookie"] == ready["cookie"]
            assert record["headers"]["host"] == "127.0.0.1:3085"
            assert record["headers"]["origin"] == "http://127.0.0.1:3085"
            for name in ("authorization", "x-user-id", "x-user-no", "x-forwarded-host", "forwarded"):
                assert name not in record["headers"]
        assert [record["index"] for record in calls] == [0, 1]


def test_exact_offline_transport_anonymous_request_never_reaches_tcp_upstream():
    with tcp_pilot() as (client, config, identities, _files, calls):
        denied = client.post(
            "/api/workspace/create",
            headers={"Origin": PUBLIC, "X-User-ID": config.bindings[0].user_id},
            json=envelope("anonymous"),
        )
        assert denied.status_code == 403
        assert calls == []
        for origin in ("https://attacker.invalid", "http://127.0.0.1:3085"):
            denied = client.post(
                "/api/workspace/create",
                headers={"Origin": origin,
                         "Cookie": "access_token=" + identities[0]["cookie"],
                         "X-User-ID": config.bindings[1].user_id},
                json=envelope("foreign-origin"),
            )
            assert denied.status_code == 403
            assert calls == []
        # Positive control proves the server, factory and exact transport were
        # available; missing TCP records above aren't a broken test fixture.
        accepted = client.post(
            "/api/workspace/create",
            headers={"Origin": PUBLIC, "Cookie": "access_token=" + identities[0]["cookie"]},
            json=envelope("positive-control"),
        )
        assert accepted.status_code == 200
        assert len(calls) == 1 and calls[0]["index"] == 0
        assert urlsplit(calls[0]["path"]).path == "/api/workspace/create"
