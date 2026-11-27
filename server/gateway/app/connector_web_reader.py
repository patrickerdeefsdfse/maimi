"""Bounded public HTTPS reader, independent of DSH and proxy environment.

DNS answers are validated before connecting and the selected IP is pinned for
the socket; the original hostname is retained for SNI and certificate checks.
"""
from __future__ import annotations

import http.client
import asyncio
import ipaddress
import json
import os
import socket
import ssl
import sys
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit


class WebReadFailure(Exception):
    pass


_read_slots = asyncio.Semaphore(4)


async def read_public_page_async(value: str) -> dict:
    """Killable reader: DNS and TLS cannot outlive a cancelled tool request.

    No credential/environment is forwarded on stdin; the child only receives
    the public URL and uses the pinned, proxy-free reader below.
    """
    public_url(value)
    process = None
    try:
        await asyncio.wait_for(_read_slots.acquire(), timeout=1)
    except TimeoutError:
        raise WebReadFailure("web_reader_busy") from None
    try:
        async with asyncio.timeout(25):
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-B", os.path.abspath(__file__),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            output, _ = await process.communicate(value.encode("utf-8"))
            if process.returncode or len(output) > 128 * 1024:
                raise WebReadFailure("web_page_unavailable")
            result = json.loads(output)
            if "error" in result:
                raise WebReadFailure(result["error"])
            return result
    except TimeoutError:
        raise WebReadFailure("web_page_timeout") from None
    except (OSError, ValueError, TypeError):
        raise WebReadFailure("web_page_unavailable") from None
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        _read_slots.release()


def public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
        return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)
    except ValueError:
        return False


def public_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value) or "\\" in value:
        raise WebReadFailure("web_url_denied")
    try:
        p = urlsplit(value)
        host = (p.hostname or "").encode("idna").decode("ascii").lower()
        if (p.scheme != "https" or not host or p.username or p.password or p.port not in (None, 443)
                or p.fragment or "%" in host or host.endswith(".") or "." not in host
                or host.endswith((".local", ".localhost", ".internal", ".test", ".invalid"))):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not public_address(host):
                raise ValueError()
        return urlunsplit(("https", host, p.path or "/", p.query, ""))
    except (ValueError, UnicodeError):
        raise WebReadFailure("web_url_denied") from None


def resolve_public(host: str) -> str:
    answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(a[4][0] for a in answers))
    if not addresses or any(not public_address(a) for a in addresses):
        raise WebReadFailure("web_address_denied")
    return addresses[0]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout=6):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # No second hostname resolution, HTTP proxy, cookies or authorization.
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
        elif tag in {"p", "br", "div", "li", "h1", "h2", "h3", "tr"} and not self.skip:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.skip:
            self.skip -= 1

    def handle_data(self, value):
        if not self.skip:
            self.parts.append(value)


def plain_text(value: str, maximum=16000) -> str:
    parser = TextExtractor()
    parser.feed(value)
    return "\n".join(" ".join(line.split()) for line in "".join(parser.parts).splitlines() if line.strip())[:maximum]


def read_public_page(value: str) -> dict:
    """Only a manually selected tool call should invoke this reader."""
    started = time.monotonic()
    current = public_url(value)
    try:
        for hop in range(3):
            p = urlsplit(current)
            address = resolve_public(p.hostname)
            conn = PinnedHTTPSConnection(p.hostname, address)
            try:
                conn.request("GET", urlunsplit(("", "", p.path, p.query, "")), headers={
                    "Accept": "text/html,text/plain", "Accept-Encoding": "identity",
                    "User-Agent": "MEGMEET-Office-Assistant/1.0 (+public-page-reader)"})
                response = conn.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if hop == 2 or not location:
                        raise WebReadFailure("web_redirect_limit")
                    current = public_url(urljoin(current, location))
                    continue
                if response.status != 200:
                    raise WebReadFailure("web_page_unavailable")
                kind = response.getheader("Content-Type", "").split(";", 1)[0].lower()
                if kind not in {"text/html", "text/plain", "application/xhtml+xml"}:
                    raise WebReadFailure("web_content_type_denied")
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise WebReadFailure("web_encoding_denied")
                chunks, size = [], 0
                while True:
                    if time.monotonic() - started > 20:
                        raise WebReadFailure("web_page_timeout")
                    # read1 returns currently available bytes instead of waiting
                    # for a full chunk under a slow trickle response.
                    part = response.read1(16384)
                    if not part:
                        break
                    size += len(part)
                    if size > 512 * 1024:
                        raise WebReadFailure("web_page_too_large")
                    chunks.append(part)
                raw = b"".join(chunks).decode("utf-8", errors="replace")
                text = plain_text(raw) if kind != "text/plain" else raw[:16000]
                return {"url": current, "text": text, "untrusted_external_content": True,
                        "truncated": len(text) == 16000, "bytes_read": size}
            finally:
                conn.close()
    except WebReadFailure:
        raise
    except (OSError, ValueError, http.client.HTTPException):
        raise WebReadFailure("web_page_unavailable") from None
    raise WebReadFailure("web_page_unavailable")


if __name__ == "__main__":
    try:
        result = read_public_page(sys.stdin.buffer.read(8193).decode("utf-8"))
    except (WebReadFailure, UnicodeError) as exc:
        result = {"error": str(exc) if isinstance(exc, WebReadFailure) else "web_url_denied"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))
