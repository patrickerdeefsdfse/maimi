#!/usr/bin/env python3
"""Candidate per-user DSH launcher; no Docker or company provider credentials.

The manager supplies one owner's state/bootstrap volumes and a scoped model
token. This launcher is Linux-only. The native-entry service must independently
bind those resources to its authenticated owner; ready.json is not an identity
assertion from the browser. No live deployment is performed by this module.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

UID = 10001
STATE = Path("/state")
BOOTSTRAP = Path("/run/megmeet-bootstrap")
MODEL_TOKEN = Path("/run/megmeet-config/model-token")
MODEL_BASE = "http://native-entry:8080/internal/model/v1"
PROFILE = "native-personal"
BASE = "http://127.0.0.1:3085"
MAX_BODY = 2 * 1024 * 1024
MAX_LOG = 512 * 1024
MAX_CONNECTIONS = 48
COOKIE_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,128}\Z")
OPAQUE = re.compile(r"[A-Za-z0-9_-]{16,256}\Z")


class Failure(Exception):
    """Only fixed, non-sensitive error codes are exposed by main()."""


class StopRequested(Exception):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise Failure(code)


def owner_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise Failure("OWNER_INVALID") from None
    require(str(parsed) == value and parsed.int != 0, "OWNER_INVALID")
    return value


@dataclass(frozen=True)
class Config:
    owner: str
    public_origin: str
    authority: str

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "Config":
        owner = owner_uuid(env.get("MEGMEET_OWNER_ID", ""))
        require(env.get("DSH_PROFILE", PROFILE) == PROFILE, "PROFILE_INVALID")
        require(env.get("MEGMEET_MODEL_TOKEN_FILE", str(MODEL_TOKEN)) == str(MODEL_TOKEN),
                "MODEL_TOKEN_PATH_INVALID")
        require(env.get("MEGMEET_MODEL_BASE_URL", MODEL_BASE) == MODEL_BASE,
                "MODEL_BASE_INVALID")
        origin = env.get("MEGMEET_PUBLIC_ORIGIN", "https://platform-ai.megmeet.com")
        try:
            parsed = urllib.parse.urlsplit(origin)
        except ValueError:
            raise Failure("PUBLIC_ORIGIN_INVALID") from None
        require(parsed.scheme == "https" and parsed.netloc in {
            "platform-ai.megmeet.com", "platform-ai.megmeet.com:443"
        } and not parsed.path and not parsed.query and not parsed.fragment,
                "PUBLIC_ORIGIN_INVALID")
        return cls(owner, origin, parsed.netloc)


def private_dir(path: Path, uid: int = UID) -> None:
    require(path.is_absolute() and not path.is_symlink() and path.is_dir(), "DIRECTORY_INVALID")
    require(path.resolve(strict=True) == path, "DIRECTORY_REDIRECTED")
    info = path.stat()
    require(info.st_uid == uid and stat.S_IMODE(info.st_mode) == 0o700, "DIRECTORY_NOT_PRIVATE")


def read_private(path: Path, *, uid: int = UID, limit: int = 16384) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise Failure("PRIVATE_FILE_UNREADABLE") from None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(not path.is_symlink() and stat.S_ISREG(info.st_mode)
                and info.st_uid == uid and stat.S_IMODE(info.st_mode) == 0o600,
                "PRIVATE_FILE_INVALID")
        raw = stream.read(limit + 1)
        require(len(raw) <= limit, "PRIVATE_FILE_TOO_LARGE")
        return raw


def atomic_private(path: Path, value: dict | str) -> None:
    require(not path.is_symlink(), "WRITE_TARGET_SYMLINK")
    payload = json.dumps(value, ensure_ascii=True, separators=(",", ":")) if isinstance(value, dict) else value
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def workspace_id(value: object) -> str:
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value) is not None,
            "WORKSPACE_ID_INVALID")
    return value


def load_owner(path: Path, owner: str, uid: int = UID) -> dict:
    try:
        record = json.loads(read_private(path, uid=uid))
    except (ValueError, UnicodeError):
        raise Failure("OWNER_RECORD_INVALID") from None
    require(isinstance(record, dict) and set(record) == {
        "schema", "owner_id", "profile_state", "workspace_id"
        } and type(record["schema"]) is int and record["schema"] == 1 and record["owner_id"] == owner
        and record["profile_state"] in {"new", "pending", "ready"}, "OWNER_RECORD_INVALID")
    if record["workspace_id"] is not None:
        workspace_id(record["workspace_id"])
        require(record["profile_state"] == "ready", "OWNER_RECORD_INVALID")
    return record


def prepare_state(root: Path, owner: str, uid: int = UID) -> dict:
    """Refuse adoption of a pre-existing unowned volume or redirected paths."""
    private_dir(root, uid)
    owner_uuid(owner)
    marker = root / ".megmeet-owner.json"
    if not marker.exists() and not marker.is_symlink():
        require(not any(root.iterdir()), "STATE_UNOWNED_NONEMPTY")
        atomic_private(marker, {"schema": 1, "owner_id": owner,
                               "profile_state": "new", "workspace_id": None})
    record = load_owner(marker, owner, uid)
    require(record["profile_state"] != "pending", "PROFILE_INITIALIZATION_INCOMPLETE")
    for name in ("home", "dsh", "cache", "work"):
        path = root / name
        if not path.exists() and not path.is_symlink():
            require(record["profile_state"] == "new", "PERSISTED_DIRECTORY_MISSING")
            path.mkdir(mode=0o700)
        private_dir(path, uid)
    settings = root / "dsh" / "settings.yaml"
    if not settings.exists() and not settings.is_symlink():
        require(record["profile_state"] == "new", "PERSISTED_SETTINGS_MISSING")
        atomic_private(settings, {
            "agent-default-model": {"provider": "deepseek-official", "model": "deepseek-flash-v4.1"},
            "llm-deepseek": {"apiKeyEnv": "DEEPSEEK_API_KEY", "baseURL": MODEL_BASE,
                             "models": [{"id": "deepseek-flash-v4.1", "name": "DeepSeek V4.1 Flash"}]},
        })
    else:
        # Existing user settings are never replaced or parsed as executable YAML.
        read_private(settings, uid=uid, limit=1024 * 1024)
    return record


def acquire_state_lock(root: Path, uid: int = UID) -> int:
    """Lock the volume directory before even creating the ownership marker."""
    import fcntl
    private_dir(root, uid)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        raise Failure("STATE_ALREADY_IN_USE") from None
    return descriptor


def prepare_bootstrap(root: Path, owner: str, uid: int = UID) -> None:
    private_dir(root, uid)
    owner_uuid(owner)
    marker = root / ".owner.json"
    if not marker.exists() and not marker.is_symlink():
        require(not any(root.iterdir()), "BOOTSTRAP_UNOWNED_NONEMPTY")
        atomic_private(marker, {"owner_id": owner})
    try:
        saved = json.loads(read_private(marker, uid=uid))
    except (ValueError, UnicodeError):
        raise Failure("BOOTSTRAP_OWNER_INVALID") from None
    require(saved == {"owner_id": owner}, "BOOTSTRAP_OWNER_INVALID")
    clear_bootstrap(root)


def clear_bootstrap(root: Path) -> None:
    # unlink does not follow symlinks; only these two private runtime files.
    (root / "ready.json").unlink(missing_ok=True)
    (root / "token").unlink(missing_ok=True)


def child_environment(token: str) -> dict[str, str]:
    require(OPAQUE.fullmatch(token) is not None, "MODEL_TOKEN_INVALID")
    # No inherited company keys, SSO cookies, proxy variables or NODE_OPTIONS.
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/state/home",
            "DSH_HOME": "/state/dsh", "XDG_CACHE_HOME": "/state/cache",
            "XDG_CONFIG_HOME": "/state/home/.config", "XDG_DATA_HOME": "/state/home/.local/share",
            "XDG_STATE_HOME": "/state/home/.local/state", "XDG_RUNTIME_DIR": "/tmp",
            "TMPDIR": "/tmp", "LANG": "C.UTF-8", "DSH_TELEMETRY_DISABLED": "1",
            "NODE_OPTIONS": "--max-old-space-size=512", "DEEPSEEK_API_KEY": token,
            "DEEPSEEK_BASE_URL": MODEL_BASE}


def local_url(path: str) -> str:
    result = urllib.parse.urljoin(BASE + "/", path)
    parsed = urllib.parse.urlsplit(result)
    require((parsed.scheme, parsed.netloc) == ("http", "127.0.0.1:3085")
            and not parsed.username and not parsed.password and not parsed.fragment
            and not any(ord(c) < 32 or ord(c) == 127 for c in path), "NONLOCAL_BOOTSTRAP_URL")
    return result


class LocalRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        local_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def make_client(jar=None):
    handlers = [urllib.request.ProxyHandler({}), LocalRedirect()]
    if jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    return urllib.request.build_opener(*handlers)


def request(client, path: str, body=None):
    data = None if body is None else json.dumps(body, ensure_ascii=True).encode("ascii")
    headers = {} if body is None else {"Content-Type": "application/json", "Origin": BASE}
    req = urllib.request.Request(local_url(path), data=data, headers=headers)
    try:
        response = client.open(req, timeout=4)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read(MAX_BODY + 1)
        require(len(raw) <= MAX_BODY, "BOOTSTRAP_RESPONSE_TOO_LARGE")
        return response.code, response.headers, raw


def cookie_header(jar: http.cookiejar.CookieJar) -> str:
    values = []
    names = set()
    for cookie in jar:
        require(COOKIE_NAME.fullmatch(cookie.name) is not None
                and isinstance(cookie.value, str) and 1 <= len(cookie.value) <= 8192
                and not any(ord(c) < 0x21 or ord(c) > 0x7e or c in ';,"\\' for c in cookie.value)
                and cookie.has_nonstandard_attr("HttpOnly") and not cookie.domain_specified
                and cookie.domain == "127.0.0.1" and cookie.path == "/"
                and not cookie.is_expired() and cookie.name not in names,
                "DSH_COOKIE_INVALID")
        names.add(cookie.name)
        values.append(cookie.name + "=" + cookie.value)
    require(1 <= len(values) <= 8, "DSH_COOKIE_COUNT")
    result = "; ".join(values)
    require(len(result) <= 4096, "DSH_COOKIE_TOO_LARGE")
    return result


def workspace_request(path: str, rpc_id: str) -> dict:
    return {"type": "client-request", "rpcId": rpc_id, "method": "workspace/create",
            "payload": {"args": {"request": {"path": path}}}}


def validate_workspace(raw: bytes, rpc_id: str, path: Path, expected: str | None) -> str:
    try:
        response = json.loads(raw)
    except (ValueError, UnicodeError):
        raise Failure("WORKSPACE_RESPONSE_INVALID") from None
    require(isinstance(response, dict) and response.get("type") == "server-response"
            and response.get("rpcId") == rpc_id, "WORKSPACE_RESPONSE_ENVELOPE")
    result = response.get("result")
    require(isinstance(result, dict) and result.get("ok") is True, "WORKSPACE_RPC_REJECTED")
    value = result.get("value")
    require(isinstance(value, dict) and type(value.get("created")) is bool, "WORKSPACE_RESPONSE_VALUE")
    workspace = value.get("workspace")
    require(isinstance(workspace, dict) and workspace.get("path") == str(path.resolve(strict=True))
            and path.is_dir() and not path.is_symlink(), "WORKSPACE_PATH_MISMATCH")
    identity = workspace_id(workspace.get("workspaceId"))
    require(expected is None or (identity == expected and value["created"] is False),
            "PERSISTED_WORKSPACE_CHANGED")
    return identity


def ready_record(owner: str, generation: str, identity: str, cookie: str) -> dict:
    owner_uuid(owner)
    owner_uuid(generation)
    workspace_id(identity)
    require(isinstance(cookie, str) and 0 < len(cookie) <= 4096, "READY_COOKIE_INVALID")
    parts = cookie.split("; ")
    require(1 <= len(parts) <= 8, "READY_COOKIE_INVALID")
    names = set()
    for pair in parts:
        require("=" in pair, "READY_COOKIE_INVALID")
        name, value = pair.split("=", 1)
        require(COOKIE_NAME.fullmatch(name) is not None and name not in names and bool(value)
                and not any(ord(c) < 0x21 or ord(c) > 0x7e or c in ';,"\\' for c in value),
                "READY_COOKIE_INVALID")
        names.add(name)
    return {"ready": True, "owner_id": owner, "generation": generation,
            "workspace_id": identity, "cookie": cookie}


def private_log_reader(stream, path: Path) -> None:
    """Keep only bounded startup output in private tmp; drain the remainder."""
    remaining = MAX_LOG
    output = None
    try:
        output = path.open("xb")
        os.chmod(path, 0o600)
    except OSError:
        if output is not None:
            output.close()
        output = None
    try:
        while True:
            data = stream.read1(4096)
            if not data:
                break
            if remaining and output is not None:
                saved = data[:remaining]
                try:
                    output.write(saved)
                    output.flush()
                except OSError:
                    output.close()
                    output = None
                remaining -= len(saved)
    except OSError:
        pass
    finally:
        if output is not None:
            output.close()


def bootstrap_runtime(process, log_path: Path, deadline: float) -> tuple[object, str, str]:
    token = None
    while time.monotonic() < deadline:
        require(process.poll() is None, "DSH_EXITED_BEFORE_READY")
        if log_path.exists():
            raw = log_path.read_bytes()[:MAX_LOG]
            matches = re.findall(rb"http://127\.0\.0\.1:3085/\?token=([A-Za-z0-9_-]{16,256})(?=[ \t\r\n\x1b])", raw)
            if matches:
                token = matches[-1].decode("ascii")
                break
        time.sleep(0.1)
    require(token is not None, "DSH_STARTUP_TIMEOUT")
    require(request(make_client(), "/")[0] == 401, "ANONYMOUS_DSH_NOT_REJECTED")
    jar = http.cookiejar.CookieJar()
    client = make_client(jar)
    status, headers, raw = request(client, "/?token=" + token)
    require(status == 200 and "text/html" in headers.get("Content-Type", "").lower()
            and b"__DSH_BOOT__" in raw, "DSH_COOKIE_EXCHANGE_FAILED")
    return client, token, cookie_header(jar)


def stop_process(process) -> None:
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        pass
    # Also reap any descendants left in the owned process group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=3)


def relay_pair(left: socket.socket, right: socket.socket, stop: threading.Event) -> None:
    """Bounded, bidirectional relay; half-close only after queued bytes drain."""
    peers = {left: right, right: left}
    buffers = {left: bytearray(), right: bytearray()}
    readable = {left: True, right: True}
    write_closed = {left: False, right: False}
    with selectors.DefaultSelector() as selector:
        try:
            for sock in peers:
                sock.setblocking(False)
            while not stop.is_set():
                for sock, peer in peers.items():
                    if not readable[peer] and not buffers[sock] and not write_closed[sock]:
                        sock.shutdown(socket.SHUT_WR)
                        write_closed[sock] = True
                    events = (selectors.EVENT_READ if readable[sock] and len(buffers[peer]) < 262144 else 0)
                    if buffers[sock]:
                        events |= selectors.EVENT_WRITE
                    try:
                        selector.unregister(sock)
                    except KeyError:
                        pass
                    if events:
                        selector.register(sock, events)
                if not selector.get_map():
                    return
                for selected, event in selector.select(timeout=0.5):
                    sock = selected.fileobj
                    if event & selectors.EVENT_READ:
                        try:
                            data = sock.recv(65536)
                        except BlockingIOError:
                            data = None
                        if data == b"":
                            readable[sock] = False
                        elif data:
                            buffers[peers[sock]].extend(data)
                    if event & selectors.EVENT_WRITE:
                        try:
                            sent = sock.send(buffers[sock])
                            del buffers[sock][:sent]
                        except BlockingIOError:
                            pass
        except OSError:
            return
        finally:
            left.close()
            right.close()


class Bridge:
    def __init__(self):
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.active: dict[threading.Thread, tuple[socket.socket, socket.socket]] = {}
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("0.0.0.0", 3084))
        self.listener.listen(32)
        self.listener.settimeout(0.25)

    def _relay(self, client, upstream):
        try:
            relay_pair(client, upstream, self.stop)
        finally:
            with self.lock:
                self.active.pop(threading.current_thread(), None)

    def serve(self, process) -> None:
        while not self.stop.is_set():
            require(process.poll() is None, "DSH_EXITED")
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            with self.lock:
                full = len(self.active) >= MAX_CONNECTIONS
            if full:
                client.close()
                continue
            try:
                upstream = socket.create_connection(("127.0.0.1", 3085), timeout=2)
            except OSError:
                client.close()
                continue
            thread = threading.Thread(target=self._relay, args=(client, upstream), daemon=True)
            with self.lock:
                self.active[thread] = (client, upstream)
            thread.start()

    def close(self) -> None:
        self.stop.set()
        self.listener.close()
        with self.lock:
            active = list(self.active.items())
        for _, sockets in active:
            for sock in sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        deadline = time.monotonic() + 3
        for thread, _ in active:
            thread.join(timeout=max(0, deadline - time.monotonic()))


def main() -> int:
    process = None
    bridge = None
    collector = None
    temporary = None
    lock_descriptor = None
    owns_bootstrap = False
    rc = 1
    os.umask(0o077)
    try:
        require(os.name == "posix" and os.geteuid() == UID, "LINUX_UID_REQUIRED")
        config = Config.from_env(dict(os.environ))
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(StopRequested()))
        signal.signal(signal.SIGINT, lambda *_: (_ for _ in ()).throw(StopRequested()))
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(Failure("STARTUP_TIMEOUT")))
        signal.alarm(90)
        deadline = time.monotonic() + 90
        lock_descriptor = acquire_state_lock(STATE)
        record = prepare_state(STATE, config.owner)
        prepare_bootstrap(BOOTSTRAP, config.owner)
        owns_bootstrap = True
        token = read_private(MODEL_TOKEN, limit=512).decode("ascii").strip()
        env = child_environment(token)
        dsh = shutil.which("dsh", path=env["PATH"])
        require(dsh is not None, "DSH_CLI_MISSING")
        if record["profile_state"] == "new":
            record["profile_state"] = "pending"
            atomic_private(STATE / ".megmeet-owner.json", record)
            process = subprocess.Popen([dsh, "--profile", PROFILE, "--from-default-profile", "web", "--dump-config"],
                cwd=STATE / "work", env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
            require(process.wait(timeout=min(30, max(1, deadline - time.monotonic()))) == 0,
                    "PROFILE_INITIALIZATION_FAILED")
            stop_process(process)
            process = None
            record["profile_state"] = "ready"
            atomic_private(STATE / ".megmeet-owner.json", record)
        temporary = tempfile.TemporaryDirectory(prefix="megmeet-native-", dir="/tmp")
        log_path = Path(temporary.name) / "startup.log"
        process = subprocess.Popen([dsh, "--profile", PROFILE, "--host", "127.0.0.1", "--port", "3085",
            "--no-open", "--trusted-host", config.authority], cwd=STATE / "work", env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        collector = threading.Thread(target=private_log_reader, args=(process.stdout, log_path), daemon=True)
        collector.start()
        client, web_token, cookie = bootstrap_runtime(process, log_path, deadline)
        rpc_id = "megmeet-bootstrap-" + uuid.uuid4().hex
        status, _, raw = request(client, "/api/workspace/create", workspace_request("/state/work", rpc_id))
        require(status == 200, "WORKSPACE_HTTP_FAILED")
        identity = validate_workspace(raw, rpc_id, STATE / "work", record["workspace_id"])
        record["workspace_id"] = identity
        atomic_private(STATE / ".megmeet-owner.json", record)
        require(process.poll() is None, "DSH_EXITED_BEFORE_READY")
        bridge = Bridge()
        atomic_private(BOOTSTRAP / "token", web_token)
        atomic_private(BOOTSTRAP / "ready.json", ready_record(config.owner, str(uuid.uuid4()), identity, cookie))
        signal.alarm(0)
        print("PERSONAL_HOST_READY: PASS", flush=True)
        bridge.serve(process)
    except StopRequested:
        rc = 0
    except Failure as exc:
        print("PERSONAL_HOST_ERROR: " + str(exc), flush=True)
    except Exception:
        # Never print subprocess output, request payloads or exception messages.
        print("PERSONAL_HOST_ERROR: INTERNAL_FAILURE", flush=True)
    finally:
        if os.name == "posix":
            signal.alarm(0)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        # One failed cleanup must not skip child termination or leave TCP open.
        cleanup = []
        if owns_bootstrap:
            cleanup.append(lambda: clear_bootstrap(BOOTSTRAP))
        if bridge is not None:
            cleanup.append(bridge.close)
        cleanup.append(lambda: stop_process(process))
        if collector is not None:
            cleanup.append(lambda: collector.join(timeout=2))
        if process is not None and process.stdout is not None:
            cleanup.append(process.stdout.close)
        if temporary is not None:
            cleanup.append(temporary.cleanup)
        if lock_descriptor is not None:
            cleanup.append(lambda: os.close(lock_descriptor))
        for action in cleanup:
            try:
                action()
            except Exception:
                print("PERSONAL_HOST_ERROR: CLEANUP_FAILED", flush=True)
                rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
