"""Stdlib contract tests; filesystem permission tests require POSIX.

No DSH binary, container, live SSO account or provider key is used. These tests
do not claim native runtime/browser or real account isolation acceptance.
"""
from __future__ import annotations

import http.cookiejar
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "host_launcher.py"
SPEC = importlib.util.spec_from_file_location("tested_host_launcher", SOURCE)
host = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = host
SPEC.loader.exec_module(host)
OWNER_A = "219f3eea-24c0-4a83-8693-b802c4a765e4"
OWNER_B = "6b1370e2-0bc5-487f-a31d-463c579fab47"


def cookie(*, value="runtime_cookie_fixture_123", domain="127.0.0.1", domain_specified=False,
           path="/", http_only=True):
    return http.cookiejar.Cookie(version=0, name="dsh_session", value=value,
        port=None, port_specified=False, domain=domain, domain_specified=domain_specified,
        domain_initial_dot=False, path=path, path_specified=True, secure=False,
        expires=None, discard=True, comment=None, comment_url=None,
        rest={"HttpOnly": None} if http_only else {}, rfc2109=False)


def response(path, *, identity="workspace_a_123", created=True, rpc_id="rpc-1", ok=True):
    return json.dumps({"type": "server-response", "rpcId": rpc_id,
        "result": {"ok": ok, "value": {"workspace": {"workspaceId": identity,
        "path": str(path)}, "created": created}}}).encode()


class ConfigurationTests(unittest.TestCase):
    def test_stable_uuid_cannot_be_replaced_by_department_path_or_nil(self):
        self.assertEqual(host.owner_uuid(OWNER_A), OWNER_A)
        for candidate in (OWNER_A.upper(), "../other", "employee-1", "", str(uuid.UUID(int=0))):
            with self.subTest(candidate=candidate), self.assertRaises(host.Failure):
                host.owner_uuid(candidate)

    def test_only_controlled_profile_model_route_and_public_authority(self):
        config = host.Config.from_env({"MEGMEET_OWNER_ID": OWNER_A})
        self.assertEqual(config.authority, "platform-ai.megmeet.com")
        for key, value in (("DSH_PROFILE", "../../other"),
                           ("MEGMEET_MODEL_TOKEN_FILE", "/company-key"),
                           ("MEGMEET_MODEL_BASE_URL", "https://attacker.invalid/v1"),
                           ("MEGMEET_PUBLIC_ORIGIN", "https://platform-ai.megmeet.com@attacker.invalid"),
                           ("MEGMEET_PUBLIC_ORIGIN", "https://platform-ai.megmeet.com/?token=secret"),
                           ("MEGMEET_PUBLIC_ORIGIN", "https://[invalid")):
            with self.subTest(key=key, value=value), self.assertRaises(host.Failure):
                host.Config.from_env({"MEGMEET_OWNER_ID": OWNER_A, key: value})

    def test_child_environment_does_not_inherit_parent_secrets_or_proxy(self):
        with mock.patch.dict(os.environ, {"TOKENKEY_API_KEY": "never-forward",
                                         "HTTP_PROXY": "http://example.invalid", "HOME": "/other"}):
            child = host.child_environment("a" * 48)
        self.assertNotIn("TOKENKEY_API_KEY", child)
        self.assertNotIn("HTTP_PROXY", child)
        self.assertEqual(child["HOME"], "/state/home")
        self.assertEqual(child["DSH_HOME"], "/state/dsh")
        self.assertEqual(child["DEEPSEEK_BASE_URL"], host.MODEL_BASE)
        for invalid in ("", "bad\nheader", "white space" * 4):
            with self.assertRaises(host.Failure):
                host.child_environment(invalid)


class ProtocolTests(unittest.TestCase):
    def test_source_verified_named_workspace_request(self):
        body = host.workspace_request("/state/work", "bootstrap-1")
        self.assertEqual(body["method"], "workspace/create")
        self.assertEqual(body["payload"]["args"], {"request": {"path": "/state/work"}})
        self.assertEqual(body["rpcId"], "bootstrap-1")

    def test_rpc_failure_mismatch_and_path_alias_are_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            self.assertEqual(host.validate_workspace(response(work), "rpc-1", work, None), "workspace_a_123")
            for raw in (response(work, rpc_id="wrong"), response(work, ok=False), b"not-json",
                        response(str(work) + "/../work"), response(work, created=1),
                        response(work, identity="../other")):
                with self.subTest(raw=raw), self.assertRaises(host.Failure):
                    host.validate_workspace(raw, "rpc-1", work, None)

    def test_recreation_requires_original_id_and_existing_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            self.assertEqual(host.validate_workspace(response(work, created=False), "rpc-1",
                                                    work, "workspace_a_123"), "workspace_a_123")
            for raw in (response(work, created=True), response(work, identity="different", created=False)):
                with self.assertRaises(host.Failure):
                    host.validate_workspace(raw, "rpc-1", work, "workspace_a_123")

    def test_bootstrap_never_follows_foreign_url(self):
        self.assertEqual(host.local_url("/?token=test"), host.BASE + "/?token=test")
        for target in ("http://evil.invalid/", "//evil.invalid/", "http://127.0.0.1:9999/",
                       "http://user:pass@127.0.0.1:3085/", "/#fragment", "/\r\nHeader:value"):
            with self.subTest(target=target), self.assertRaises(host.Failure):
                host.local_url(target)

    def test_only_private_host_cookie_can_be_exported(self):
        jar = http.cookiejar.CookieJar()
        jar.set_cookie(cookie())
        self.assertEqual(host.cookie_header(jar), "dsh_session=runtime_cookie_fixture_123")
        for candidate in (cookie(domain="example.invalid"), cookie(domain_specified=True),
                          cookie(http_only=False), cookie(path="/other"), cookie(value="hello;evil=x"),
                          cookie(value="hello\r\nInjected: x")):
            jar = http.cookiejar.CookieJar()
            jar.set_cookie(candidate)
            with self.subTest(cookie=candidate), self.assertRaises(host.Failure):
                host.cookie_header(jar)
        with self.assertRaises(host.Failure):
            host.cookie_header(http.cookiejar.CookieJar())

    def test_ready_credentials_are_bounded_and_generation_owner_explicit(self):
        result = host.ready_record(OWNER_A, OWNER_B, "workspace-1", "dsh_session=runtime_token")
        self.assertEqual(result, {"ready": True, "owner_id": OWNER_A, "generation": OWNER_B,
                                "workspace_id": "workspace-1", "cookie": "dsh_session=runtime_token"})
        for value in ("", "missing-equals", "name=", "name=bad\nheader", "name=quoted\"value", "name=" + "x" * 17000):
            with self.subTest(value=value[:30]), self.assertRaises(host.Failure):
                host.ready_record(OWNER_A, OWNER_B, "workspace-1", value)


@unittest.skipUnless(os.name == "posix", "POSIX ownership/mode/directory-fsync semantics")
class PersistentVolumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.uid = os.geteuid()

    def tearDown(self):
        self.temporary.cleanup()

    def test_empty_volume_is_owned_and_cannot_be_adopted_by_another_owner(self):
        host.prepare_state(self.root, OWNER_A, self.uid)
        self.assertEqual((self.root / "work").stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / ".megmeet-owner.json").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(host.Failure):
            host.prepare_state(self.root, OWNER_B, self.uid)

    def test_nonempty_volume_without_owner_refused_without_modifying_file(self):
        (self.root / "data").write_text("must remain")
        with self.assertRaises(host.Failure):
            host.prepare_state(self.root, OWNER_A, self.uid)
        self.assertEqual((self.root / "data").read_text(), "must remain")
        self.assertFalse((self.root / ".megmeet-owner.json").exists())

    def test_busy_directory_lock_cannot_mutate_empty_volume(self):
        descriptor = host.acquire_state_lock(self.root, self.uid)
        try:
            with self.assertRaisesRegex(host.Failure, "STATE_ALREADY_IN_USE"):
                host.acquire_state_lock(self.root, self.uid)
            self.assertEqual(list(self.root.iterdir()), [])
        finally:
            os.close(descriptor)

    def test_fifo_cannot_block_private_file_read(self):
        fifo = self.root / "pipe"
        os.mkfifo(fifo, mode=0o600)
        with self.assertRaises(host.Failure):
            host.read_private(fifo, uid=self.uid)

    def test_existing_settings_and_workspace_identity_are_preserved(self):
        record = host.prepare_state(self.root, OWNER_A, self.uid)
        settings = self.root / "dsh" / "settings.yaml"
        settings.write_text("custom-user-setting: keep\n")
        record.update(profile_state="ready", workspace_id="saved-workspace")
        host.atomic_private(self.root / ".megmeet-owner.json", record)
        restored = host.prepare_state(self.root, OWNER_A, self.uid)
        self.assertEqual(restored["workspace_id"], "saved-workspace")
        self.assertEqual(settings.read_text(), "custom-user-setting: keep\n")

    def test_profile_partial_failure_is_not_silently_overwritten(self):
        record = host.prepare_state(self.root, OWNER_A, self.uid)
        record["profile_state"] = "pending"
        host.atomic_private(self.root / ".megmeet-owner.json", record)
        with self.assertRaisesRegex(host.Failure, "PROFILE_INITIALIZATION_INCOMPLETE"):
            host.prepare_state(self.root, OWNER_A, self.uid)

    def test_private_file_symlink_and_loose_permissions_rejected(self):
        target = self.root / "actual"
        host.atomic_private(target, "private")
        alias = self.root / "alias"
        alias.symlink_to(target)
        with self.assertRaises(host.Failure):
            host.read_private(alias, uid=self.uid)
        target.chmod(0o644)
        with self.assertRaises(host.Failure):
            host.read_private(target, uid=self.uid)

    def test_symlinked_workspace_refused(self):
        host.prepare_state(self.root, OWNER_A, self.uid)
        (self.root / "work").rmdir()
        (self.root / "work").symlink_to(self.root / "home", target_is_directory=True)
        with self.assertRaises(host.Failure):
            host.prepare_state(self.root, OWNER_A, self.uid)

    def test_new_bootstrap_generation_clears_old_credentials_only_for_owner(self):
        host.prepare_bootstrap(self.root, OWNER_A, self.uid)
        host.atomic_private(self.root / "ready.json", {"cookie": "old"})
        host.atomic_private(self.root / "token", "old")
        with self.assertRaises(host.Failure):
            host.prepare_bootstrap(self.root, OWNER_B, self.uid)
        self.assertTrue((self.root / "ready.json").exists())
        host.prepare_bootstrap(self.root, OWNER_A, self.uid)
        self.assertFalse((self.root / "ready.json").exists())
        self.assertFalse((self.root / "token").exists())


class RelayAndLogTests(unittest.TestCase):
    def test_half_closed_request_still_receives_full_response(self):
        client, front = socket.socketpair()
        back, server = socket.socketpair()
        stop = threading.Event()
        worker = threading.Thread(target=host.relay_pair, args=(front, back, stop), daemon=True)
        worker.start()
        client.settimeout(3)
        server.settimeout(3)
        try:
            client.sendall(b"request body")
            client.shutdown(socket.SHUT_WR)
            received = bytearray()
            while True:
                data = server.recv(1024)
                if not data:
                    break
                received.extend(data)
            self.assertEqual(received, b"request body")
            server.sendall(b"response after request EOF")
            server.shutdown(socket.SHUT_WR)
            received = bytearray()
            while True:
                data = client.recv(1024)
                if not data:
                    break
                received.extend(data)
            self.assertEqual(received, b"response after request EOF")
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
        finally:
            stop.set()
            client.close()
            server.close()
            worker.join(timeout=3)

    def test_private_runtime_log_is_bounded_and_stdout_is_not_used(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "startup.log"
            raw = b"DSH private startup\n" + b"x" * (host.MAX_LOG + 10000)
            stream = io.BufferedReader(io.BytesIO(raw))
            with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                host.private_log_reader(stream, path)
            self.assertEqual(path.stat().st_size, host.MAX_LOG)
            self.assertEqual(stream.read(), b"")
            self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
