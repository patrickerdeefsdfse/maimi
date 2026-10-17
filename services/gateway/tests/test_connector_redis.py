"""Opt-in actual Redis concurrency regression using a private unix socket.

Set CONNECTOR_TEST_REDIS_URL only for an isolated disposable Redis. Every key
uses random synthetic owners; no FLUSH or broad deletion is performed.
"""
import asyncio
import os
import secrets

import pytest

from app.enterprise_connectors import RedisConnectorStore


def test_redis_atomic_consume_and_refresh_compare_swap():
    url = os.getenv("CONNECTOR_TEST_REDIS_URL")
    if not url or not url.startswith("unix://"):
        pytest.skip("private disposable Redis unix socket not configured")

    async def scenario():
        one, two = RedisConnectorStore(url), RedisConnectorStore(url)
        owner = "synthetic-" + secrets.token_hex(16)
        other = "synthetic-" + secrets.token_hex(16)
        try:
            await one.put("pending", owner, "ticket", "sealed-preview", 60)
            assert await two.pop("pending", other, "ticket") is None
            outcomes = await asyncio.gather(*[(one if n % 2 else two).pop("pending", owner, "ticket") for n in range(20)])
            assert outcomes.count("sealed-preview") == 1 and outcomes.count(None) == 19
            await one.put("grant", owner, "dingtalk", "old-encrypted-token", 60)
            await two.delete("grant", owner, "dingtalk")
            assert not await one.replace_if_equal("grant", owner, "dingtalk", "old-encrypted-token", "refreshed", 60)
            assert await one.get("grant", owner, "dingtalk") is None
            await one.put("grant", owner, "dingtalk", "new-login-token", 60)
            assert not await two.replace_if_equal("grant", owner, "dingtalk", "old-encrypted-token", "old-refresh", 60)
            assert await one.get("grant", owner, "dingtalk") == "new-login-token"
            await one.put("refresh", owner, "dingtalk", "new-worker-lock", 60)
            assert not await two.replace_if_equal("refresh", owner, "dingtalk", "expired-worker-lock", None)
            assert await one.get("refresh", owner, "dingtalk") == "new-worker-lock"
            results = await asyncio.gather(*(store.put("request", owner, "request", "sealed", 60, nx=True) for store in [one, two] * 8))
            assert sum(bool(x) for x in results) == 1
        finally:
            for category, key in (("pending", "ticket"), ("grant", "dingtalk"), ("refresh", "dingtalk"), ("request", "request")):
                await one.delete(category, owner, key)
            await one.close()
            await two.close()
    asyncio.run(scenario())
