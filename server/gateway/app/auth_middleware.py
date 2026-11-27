from __future__ import annotations

import os
from typing import Any, Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from .auth_client import AuthServiceUnavailable, query_user
from .iam_oauth import IAMFailure, PUBLIC_PATHS, authenticate_request
from .desktop_auth import PUBLIC_PATHS as DESKTOP_PUBLIC_PATHS, BEARER_SCOPES, authenticate_desktop_request


_BROWSER_ENTRIES = {
    "/maimi/web": "/maimi/web/",
    "/maimi/web/chat": "/maimi/web/",
    "/maimi/web/chat/": "/maimi/web/",
    "/maimi/plugins": "/maimi/plugins/",
    "/maimi/monitor": "/maimi/monitor/",
}


def login_required(request: Request, detail: str) -> Response:
    destination = _BROWSER_ENTRIES.get(request.url.path)
    if (destination and request.method in {"GET", "HEAD"}
            and not request.headers.get("authorization")
            and request.headers.get("sec-fetch-mode", "navigate") == "navigate"
            and "text/html" in request.headers.get("accept", "").lower()
            and (destination != "/maimi/monitor/" or os.getenv("MAIMI_CLUSTER_MONITOR_ENABLED") == "true")):
        return RedirectResponse(destination, status_code=303, headers={"Cache-Control": "no-store"})
    return JSONResponse({"detail": detail}, status_code=401, headers={"Cache-Control": "no-store"})


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
            from .distribution import public_download
            if public_download(request):
                return await call_next(request)
            from .office_remote import internal_route
            from .native_history import internal_route as history_internal_route
            from .native_workspace_state import internal_route as workspace_internal_route
            from .storage_api import internal_route as storage_internal_route
            from .maintenance_metrics import internal_route as maintenance_internal_route
            from .execution_bridge import internal_route as execution_internal_route
            from .connector_desktop_handoff import HEADERS as HANDOFF_HEADERS, public_handoff_request
            if public_handoff_request(request):
                response = await call_next(request)
                response.headers.update(HANDOFF_HEADERS)
                return response
            if request.url.path in INTERNAL_MODEL_PATHS | INTERNAL_CONNECTOR_PATHS | INTERNAL_SEARCH_PATHS or internal_route(request) or history_internal_route(request) or workspace_internal_route(request) or storage_internal_route(request) or maintenance_internal_route(request) or execution_internal_route(request):
                return await call_next(request)
            # Selecting the new provider always enforces authentication; an old
            # AUTH_REQUIRED=false setting must not silently disable it.
            if os.getenv("MAIMI_DEPLOYMENT_MODE") == "production-ha" and request.method in {"GET", "HEAD"} and request.url.path in {"/", "/maimi", "/maimi/", "/maimi/plugins/", "/maimi/plugins/assets/app.js", "/maimi/plugins/assets/app.css", "/maimi/plugins/assets/entry.css"}:
                return await call_next(request)
            # These fixed files contain only the monitor login shell, no metrics.
            monitor_shell = {"/maimi/monitor/", "/maimi/monitor/assets/app.js", "/maimi/monitor/assets/app.css"}
            if os.getenv("MAIMI_CLUSTER_MONITOR_ENABLED") == "true" and request.method in {"GET", "HEAD"} and request.url.path in monitor_shell:
                return await call_next(request)
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
                if exc.status == 401:
                    return login_required(request, exc.code)
                return JSONResponse({"detail": exc.code}, status_code=exc.status,
                                    headers={"Cache-Control": "no-store"})
            if user is None:
                return login_required(request, "authentication required")
            request.state.user = user
            response = await call_next(request)
            # Explicit human actions only. Polls, monitor views and device heartbeats are excluded.
            active_paths = {'/v1/personal-model/chat/completions',
                '/v1/personal-cloud/native/api/session/prompt',
                '/v1/personal-cloud/native/api/session/create',
                '/v1/personal-cloud/native/api/session/uploadFileBinary',
                '/api/session/prompt','/api/session/create','/api/session/uploadFileBinary'}
            if request.method == 'POST' and request.url.path in active_paths and response.status_code < 400:
                from .observability.history_runtime import activity
                activity(user.get('user_no'), 'desktop' if getattr(request.state,'trusted_actor',None) else 'web')
            return response
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
