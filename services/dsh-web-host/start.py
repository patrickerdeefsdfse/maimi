from __future__ import annotations

import os
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Iterable
from pathlib import Path


WEB_HOST = "127.0.0.1"
WEB_PORT = int(os.getenv("DSH_WEB_PORT", "3085"))
BRIDGE_HOST = "0.0.0.0"
BRIDGE_PORT = int(os.getenv("DSH_BRIDGE_PORT", "3084"))


def _trusted_hosts() -> list[str]:
    raw = os.getenv("DSH_TRUSTED_HOSTS", "platform-ai.megmeet.com,platform-ai.megmeet.com:443")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _runtime_env(dsh_home: str) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("HOME", dsh_home)
    env.setdefault("DSH_HOME", dsh_home)
    env.setdefault("DSH_TELEMETRY_DISABLED", "1")
    key_file = env.get("TOKENKEY_API_KEY_FILE", "").strip() or env.get("DEEPSEEK_API_KEY_FILE", "").strip()
    if key_file:
        path = Path(key_file)
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"configured API key file is not a regular file: {path}")
        env["DEEPSEEK_API_KEY"] = path.read_text(encoding="utf-8").strip()
        if env.get("TOKENKEY_API_KEY_FILE", "").strip():
            env["DEEPSEEK_BASE_URL"] = env.get(
                "TOKENKEY_BASE_URL", "https://tokenkey.megmeet.com/v1"
            ).strip()
    return env


def _pipe(source: socket.socket, target: socket.socket) -> None:
    try:
        while True:
            data = source.recv(65536)
            if not data:
                return
            target.sendall(data)
    except OSError:
        # The peer may close a browser/Nginx connection while the other
        # direction is still being forwarded. This is a normal disconnect.
        return
    finally:
        source.close()
        target.close()


def _close_all(sockets: Iterable[socket.socket]) -> None:
    for sock in sockets:
        try:
            sock.close()
        except OSError:
            pass


def main() -> int:
    dsh_home = os.getenv("DSH_HOME", "/var/lib/megmeet/dsh")
    command = [
        "dsh",
        "--profile",
        "web",
        "--host",
        WEB_HOST,
        "--port",
        str(WEB_PORT),
        "--no-open",
    ]
    for authority in _trusted_hosts():
        command.extend(["--trusted-host", authority])

    env = _runtime_env(dsh_home)
    dsh = subprocess.Popen(command, env=env)

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((BRIDGE_HOST, BRIDGE_PORT))
    listener.listen(64)

    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True
        try:
            listener.close()
        except OSError:
            pass
        if dsh.poll() is None:
            dsh.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        while not stopping:
            if dsh.poll() is not None:
                return dsh.returncode or 1
            try:
                client, _ = listener.accept()
            except OSError:
                if stopping:
                    break
                raise
            try:
                upstream = socket.create_connection((WEB_HOST, WEB_PORT), timeout=10)
            except OSError:
                client.close()
                time.sleep(0.1)
                continue
            threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()
    finally:
        _close_all([listener])
        if dsh.poll() is None:
            dsh.terminate()
            try:
                dsh.wait(timeout=10)
            except subprocess.TimeoutExpired:
                dsh.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
