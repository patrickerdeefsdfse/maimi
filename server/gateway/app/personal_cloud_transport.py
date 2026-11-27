"""Pool internal HTTP sockets, keeping cookies and credentials request-scoped."""
import asyncio

import httpx


class BorrowedTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport):
        self.transport = transport

    async def handle_async_request(self, request):
        return await self.transport.handle_async_request(request)

    # AsyncBaseTransport.aclose() is a no-op. Closing a short-lived client
    # must release its response stream, but never another request's pool.


_transports = {}


async def start():
    loop = asyncio.get_running_loop()
    if loop not in _transports:
        # A long-running worker response must never queue a manager heartbeat.
        # Preserve the existing ability to open simultaneous upstream streams.
        limits = httpx.Limits(max_connections=None, max_keepalive_connections=32, keepalive_expiry=5)
        _transports[loop] = {
            name: BorrowedTransport(httpx.AsyncHTTPTransport(limits=limits, trust_env=False))
            for name in ("manager", "worker")
        }


async def stop():
    pools = _transports.pop(asyncio.get_running_loop(), {})
    await asyncio.gather(*(borrowed.transport.aclose() for borrowed in pools.values()))


def client(name, timeout):
    # Command-line callers without an ASGI lifespan own their own transport.
    transport = _transports.get(asyncio.get_running_loop(), {}).get(name)
    options = {"transport": transport} if transport is not None else {}
    return httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False, **options)
