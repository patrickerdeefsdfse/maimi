from __future__ import annotations

import os
from typing import Any, Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .auth_client import AuthServiceUnavailable, query_user
from .iam_oauth import IAMFailure, PUBLIC_PATHS, authenticate_request
from .desktop_auth import PUBLIC_PATHS as DESKTOP_PUBLIC_PATHS, BEARER_SCOPES, authenticate_desktop_request


class PlatformAuthMiddleware(BaseHTTPMiddleware):
    """Explicit direct-IAM authentication, with a legacy bridge for older stacks."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        mode = os.getenv("AUTH_MODE", "iam_direct").lower()
        if mode == "iam_direct":
            # Exact worker operations validate their own client-bound lease.
            # Connector confirmation also requires one-use user-UI proof.
            from .personal_cloud import INTERNAL_MODEL_PATHS, INTERNAL_CONNECTOR_PATHS
            from .personal_search import INTERNAL_PATHS as INTERNAL_SEARCH_PATHS
            from .personal_cloud_web import PUBLIC_WEB_PATHS
            from .office_remote import internal_route
            from .connector_desktop_handoff import HEADERS as HANDOFF_HEADERS, public_handoff_request
            if public_handoff_request(request):
                response = await call_next(request)
                response.headers.update(HANDOFF_HEADERS)
                return response
            if request.url.path in INTERNAL_MODEL_PATHS | INTERNAL_CONNECTOR_PATHS | INTERNAL_SEARCH_PATHS or internal_route(request):
                return await call_next(request)
            # Selecting the new provider always enforces authentication; an old
            # AUTH_REQUIRED=false setting must not silently disable it.
            if request.url.path in {"/health", "/ready"} | PUBLIC_PATHS | DESKTOP_PUBLIC_PATHS | PUBLIC_WEB_PATHS:
                return await call_next(request)
            try:
                if request.url.path.startswith(BEARER_SCOPES) and request.headers.get("authorization"):
                    actor = await authenticate_desktop_request(request)
                    request.state.trusted_actor = actor
                    user = actor.platform() if actor else None
                else:
                    user = await authenticate_request(request)
            except IAMFailure as exc:
                return JSONResponse({"detail": exc.code}, status_code=exc.status,
                                    headers={"Cache-Control": "no-store"})
            if user is None:
                return JSONResponse({"detail": "authentication required"}, status_code=401,
                                    headers={"Cache-Control": "no-store"})
            request.state.user = user
            return await call_next(request)
        if mode != "legacy":
            return JSONResponse({"detail": "authentication mode unavailable"}, status_code=503)
        if os.getenv("AUTH_REQUIRED", "false").lower() != "true":
            return await call_next(request)
        if request.url.path in {"/health", "/ready", "/docs", "/openapi.json"}:
            return await call_next(request)

        try:
            user: dict[str, Any] | None = await query_user(dict(request.cookies))
        except AuthServiceUnavailable:
            return JSONResponse({"detail": "authentication service unavailable"}, status_code=503)
        if user is None:
            return JSONResponse({"detail": "authentication required"}, status_code=403)
        request.state.user = user
        return await call_next(request)
