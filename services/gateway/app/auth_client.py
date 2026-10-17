from __future__ import annotations

import os
from typing import Any

import httpx


AUTH_QUERY_USER_URL = os.getenv(
    "AUTH_QUERY_USER_URL",
    "https://platformai-test.megmeet.com/v1/auth/query-user",
)
AUTH_VERIFY_ACCESS_URL = os.getenv(
    "AUTH_VERIFY_ACCESS_URL",
    "https://platformai-test.megmeet.com/v1/endpoint/verify-access",
)
AUTH_TLS_VERIFY = os.getenv("AUTH_TLS_VERIFY", "true").lower() not in {
    "0",
    "false",
    "no",
}


class AuthServiceUnavailable(RuntimeError):
    pass


async def query_user(cookies: dict[str, str]) -> dict[str, Any] | None:
    """Resolve the current SSO user without exposing cookies to the browser code."""
    # Do not call the SSO endpoint when the browser sent no credentials.  The
    # test SSO service reports a missing token as HTTP 500; treating that as a
    # transport outage turns an ordinary logged-out request into a misleading
    # 503 and prevents the UI from starting its refresh/login flow.
    if not cookies:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=5.0,
            follow_redirects=False,
            verify=AUTH_TLS_VERIFY,
        ) as client:
            response = await client.post(AUTH_QUERY_USER_URL, cookies=cookies)
    except httpx.HTTPError as exc:
        raise AuthServiceUnavailable("SSO service unavailable") from exc
    if response.status_code in (401, 403):
        return None
    try:
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise AuthServiceUnavailable("SSO service unavailable") from exc
    payload = response.json()
    if payload.get("code") != 0:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None
