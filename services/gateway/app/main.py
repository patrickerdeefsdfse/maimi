import asyncio
import hashlib
import json
import os
import time
from html import escape

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from .auth_middleware import PlatformAuthMiddleware
from .access_control import AccessDenied, authorize_enterprise_context, list_projects
from .conversation_router import decide_route
from .dsh_adapter import DshAgentAdapter, DshConversationRequest
from .history_store import list_conversations, read_conversation, record_chat_turn, assert_personal_conversation
from .model_policy import is_allowed, list_catalog
from .iam_oauth import router as iam_router, IAMFailure
from .desktop_auth import router as desktop_auth_router
from .personal_model_proxy import router as personal_model_router, DEFAULT_MODEL as CHAT_MODEL
from .enterprise_plugins import router as enterprise_plugins_router
from .personal_cloud import router as personal_cloud_router
from .personal_cloud_web import router as personal_cloud_web_router
from .enterprise_connectors import router as enterprise_connectors_router
from .local_service import router as local_service_router
from .office_remote import router as office_router
from .personal_search import router as personal_search_router

app = FastAPI(title="Megmeet AI Gateway", version="0.1.0")
app.add_middleware(PlatformAuthMiddleware)
app.include_router(iam_router)
app.include_router(desktop_auth_router)
app.include_router(personal_model_router)
app.include_router(enterprise_plugins_router)
app.include_router(personal_cloud_router)
app.include_router(personal_cloud_web_router)
app.include_router(enterprise_connectors_router)
app.include_router(local_service_router)
app.include_router(office_router)
app.include_router(personal_search_router)
dsh_adapter = DshAgentAdapter.from_environment()


@app.exception_handler(IAMFailure)
async def iam_failure_response(request: Request, exc: IAMFailure):
    return JSONResponse({"detail": exc.code}, status_code=exc.status,
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


class ChatRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=100)
    input: str = Field(default="", max_length=200000)
    route_mode: str = Field(default="auto", pattern="^(auto|dsh|enterprise)$")
    project_key: str | None = Field(default=None, max_length=160)
    plugin_key: str | None = Field(default=None, max_length=160)
    file_ids: list[str] = Field(default_factory=list, max_length=50)
    provider_key: str = Field(default="tokenkey", min_length=1, max_length=100)
    model: str = Field(default=CHAT_MODEL, min_length=1, max_length=200)

    @field_validator("conversation_id")
    @classmethod
    def safe_conversation_id(cls, value: str) -> str:
        value = value.strip()
        if value in {".", ".."} or any(char in value for char in "/\\"):
            raise ValueError("conversation_id must be a single path-safe segment")
        return value


def _decide_or_400(request: ChatRequest):
    try:
        return decide_route(
            route_mode=request.route_mode,
            project_key=request.project_key,
            plugin_key=request.plugin_key,
            file_ids=request.file_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _actor(request: Request) -> tuple[str, str]:
    user = getattr(request.state, "user", None)
    if not isinstance(user, dict):
        raise HTTPException(status_code=401, detail="authenticated user is required")
    user_no = str(user.get("user_no") or user.get("userNo") or "").strip()
    organization_code = str(
        user.get("organization_code") or user.get("organizationCode") or ""
    ).strip()
    if not user_no:
        raise HTTPException(status_code=401, detail="authenticated user is incomplete")
    return user_no, organization_code


async def _authorize_enterprise(request: Request, chat: ChatRequest) -> dict[str, object] | None:
    user_no, organization_code = _actor(request)
    try:
        return await asyncio.to_thread(
            authorize_enterprise_context,
            user_no=user_no,
            organization_code=organization_code,
            project_key=chat.project_key,
            plugin_key=chat.plugin_key,
            file_ids=chat.file_ids,
        )
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="authorization storage unavailable") from exc


def _user_workspace_key(request: Request) -> str | None:
    """Derive a stable opaque workspace directory from the SSO identity."""
    user = getattr(request.state, "user", None)
    if not isinstance(user, dict):
        return None
    user_no = str(user.get("user_no") or user.get("userNo") or "").strip()
    organization = str(
        user.get("organization_code") or user.get("organizationCode") or ""
    ).strip()
    if not user_no:
        return None
    # Direct IAM resolves account_no to the immutable employee number from the
    # synchronized directory. A department transfer must not move personal data.
    identity = f"iam:{user_no}" if user.get("auth_provider") == "iam_direct" else f"{organization}:{user_no}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return f"user-{digest}"


def _iam_personal_history(request: Request) -> bool:
    user = getattr(request.state, "user", None)
    return isinstance(user, dict) and user.get("auth_provider") == "iam_direct"


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "healthy",
        "service": "gateway",
        "database_configured": bool(os.getenv("DATABASE_URL")),
        "redis_configured": bool(os.getenv("REDIS_URL")),
    }


@app.get("/ready")
def ready() -> dict[str, object]:
    """Return readiness only when migrations and runtime dependencies work."""
    failures: list[str] = []
    try:
        from .history_store import _connect

        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT to_regclass('platform.schema_migrations') AS schema_migrations,
                       to_regclass('platform.users') AS users,
                       to_regclass('platform.conversations') AS conversations,
                       to_regclass('platform.messages') AS messages
                """
            )
            # _connect uses psycopg dict_row: unnamed columns overwrite each
            # other, and iterating a dict checks keys rather than table values.
            tables = cur.fetchone()
            required_tables = {"schema_migrations", "users", "conversations", "messages"}
            if (not isinstance(tables, dict) or set(tables) != required_tables
                    or any(tables[name] is None for name in required_tables)):
                failures.append("database_migrations")
            else:
                required = os.getenv("MIGRATIONS_REQUIRED_VERSION", "005_history_write.sql")
                cur.execute(
                    "SELECT 1 FROM platform.schema_migrations WHERE version = %s",
                    (required,),
                )
                if cur.fetchone() is None:
                    failures.append("database_migrations")
    except Exception:
        failures.append("database")

    try:
        import redis

        client = redis.from_url(os.getenv("REDIS_URL", ""), socket_timeout=2)
        client.ping()
        client.close()
    except Exception:
        failures.append("redis")

    if failures:
        raise HTTPException(status_code=503, detail="dependencies not ready")
    return {"status": "ready", "database": "ready", "redis": "ready"}


@app.get("/v1/system/components")
def components() -> dict[str, object]:
    dsh_enabled = os.getenv("DSH_ENABLED", "false").lower() == "true"
    dsh_status = "ready" if dsh_adapter.backend is not None else (
        "unavailable" if dsh_enabled else "disabled"
    )
    return {
        "status": "scaffold",
        "components": {
            "dsh_runtime": dsh_status,
            "postgresql": "required",
            "redis": "required",
            "milvus": "optional-vector-profile",
            "elasticsearch": "optional-search-profile",
        },
    }


@app.post("/v1/system/me")
async def current_user(request: Request) -> dict[str, object]:
    # The middleware has already resolved the user in production. This endpoint
    # remains a small compatibility surface for the DSH Web adapter.
    return {"status": "ok", "user": getattr(request.state, "user", None)}


@app.get("/v1/system/sso-check")
async def sso_check(request: Request) -> dict[str, object]:
    """Browser-friendly, read-only SSO probe.

    Deliberately returns only an authentication state, never user or
    organization fields.  The auth middleware remains responsible for
    rejecting unauthenticated requests when AUTH_REQUIRED=true.
    """
    if (os.getenv("AUTH_MODE", "iam_direct").lower() != "iam_direct"
            and os.getenv("AUTH_REQUIRED", "false").lower() != "true"):
        return {"status": "auth_disabled", "authenticated": False}
    user = getattr(request.state, "user", None)
    return {
        "status": "authenticated" if isinstance(user, dict) else "unauthenticated",
        "authenticated": isinstance(user, dict),
    }


@app.get("/v1/system/browser-smoke", response_class=HTMLResponse)
async def browser_smoke() -> HTMLResponse:
    """Render a test-only form for an authenticated browser smoke check."""
    if os.getenv("BROWSER_SMOKE_ENABLED", "false").lower() != "true":
        raise HTTPException(status_code=404, detail="browser smoke disabled")
    return HTMLResponse(
        """<!doctype html>
<meta charset="utf-8"><title>Megmeet AI 测试</title>
<style>body{font:16px sans-serif;max-width:760px;margin:40px auto;padding:0 16px}button{font-size:16px;padding:10px 18px}pre{white-space:pre-wrap;background:#f5f5f5;padding:12px;border-radius:6px}</style>
<h1>Megmeet AI 登录测试</h1>
<p>点击按钮后会发送一次测试对话，并读取当前账号的历史记录。</p>
<form method="post" action="browser-smoke/run"><button type="submit">开始测试</button></form>""",
    )


@app.post("/v1/system/browser-smoke/run", response_class=HTMLResponse)
async def browser_smoke_run(request: Request) -> HTMLResponse:
    """Run the fixed browser smoke turn without requiring JavaScript."""
    if os.getenv("BROWSER_SMOKE_ENABLED", "false").lower() != "true":
        raise HTTPException(status_code=404, detail="browser smoke disabled")

    conversation_id = f"browser-smoke-{time.time_ns()}"
    run_status = 200
    run_payload: dict[str, object] = {}
    history_status = 200
    history_payload: object = []
    try:
        run_payload = await run_chat(
            request,
            ChatRequest(
                conversation_id=conversation_id,
                input="Reply exactly MEGMEET_BROWSER_OK",
                provider_key="tokenkey",
                model="deepseek-flash-v4.1",
            ),
        )
    except HTTPException as exc:
        run_status = exc.status_code
        run_payload = {"detail": exc.detail}
        history_status = exc.status_code
        history_payload = {"detail": "history was not requested"}
    except Exception as exc:
        run_status = 503
        run_payload = {"detail": "browser smoke failed", "error": type(exc).__name__}
        history_status = 503
        history_payload = {"detail": "history unavailable"}
    else:
        try:
            user_no, organization_code = _actor(request)
            history_payload = await asyncio.to_thread(
                list_conversations,
                user_no=user_no,
                organization_code=organization_code,
                limit=5,
                personal_across_organizations=_iam_personal_history(request),
            )
        except HTTPException as exc:
            history_status = exc.status_code
            history_payload = {"detail": exc.detail}
        except Exception as exc:
            history_status = 503
            history_payload = {"detail": "history unavailable", "error": type(exc).__name__}

    def formatted(value: object) -> str:
        return escape(json.dumps(value, ensure_ascii=False, default=str, indent=2))

    return HTMLResponse(
        """<!doctype html>
<meta charset="utf-8"><title>Megmeet AI 测试结果</title>
<style>body{font:16px sans-serif;max-width:900px;margin:40px auto;padding:0 16px}pre{white-space:pre-wrap;background:#f5f5f5;padding:12px;border-radius:6px}a{display:inline-block;margin-top:12px}</style>
<h1>Megmeet AI 测试结果</h1>
<p>模型 HTTP 状态：<strong>"""
        + str(run_status)
        + """</strong></p><pre>"""
        + formatted(run_payload)
        + """</pre><p>历史 HTTP 状态：<strong>"""
        + str(history_status)
        + """</strong></p><pre>"""
        + formatted(history_payload)
        + """</pre><a href="../browser-smoke">返回测试页</a>""",
    )


@app.get("/v1/chat/history")
async def chat_history(request: Request, limit: int = 100) -> list[dict[str, object]]:
    user_no, organization_code = _actor(request)
    try:
        return await asyncio.to_thread(
            list_conversations,
            user_no=user_no,
            organization_code=organization_code,
            limit=limit,
            personal_across_organizations=_iam_personal_history(request),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="history storage unavailable") from exc


@app.get("/v1/chat/history/{conversation_id}")
async def chat_history_detail(request: Request, conversation_id: str) -> dict[str, object]:
    user_no, organization_code = _actor(request)
    if "/" in conversation_id or "\\" in conversation_id:
        raise HTTPException(status_code=400, detail="invalid conversation id")
    try:
        result = await asyncio.to_thread(
            read_conversation,
            user_no=user_no,
            organization_code=organization_code,
            conversation_id=conversation_id,
            personal_across_organizations=_iam_personal_history(request),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="history storage unavailable") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return result


@app.get("/v1/models")
async def models() -> list[dict[str, object]]:
    """List enabled provider/model aliases without exposing credentials."""
    return [item for item in list_catalog()
            if item.get("provider_key") == "tokenkey" and item.get("model_key") == CHAT_MODEL]


@app.get("/v1/projects")
async def projects(request: Request) -> list[dict[str, object]]:
    """List only enabled projects the authenticated user may enter."""
    user_no, organization_code = _actor(request)
    try:
        return await asyncio.to_thread(
            list_projects, user_no=user_no, organization_code=organization_code
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="project storage unavailable") from exc


@app.post("/v1/chat/route")
async def route_chat(http_request: Request, request: ChatRequest) -> dict[str, object]:
    if (request.provider_key != "tokenkey" or request.model != CHAT_MODEL
            or not is_allowed(request.provider_key, request.model)):
        raise HTTPException(status_code=400, detail="provider/model is not enabled")
    decision = _decide_or_400(request)
    project = None
    if decision.enterprise_context_required:
        project = await _authorize_enterprise(http_request, request)
    if decision.enterprise_context_required and not request.input and not request.file_ids:
        raise HTTPException(status_code=400, detail="enterprise request needs input or files")
    return {
        "status": "accepted",
        "conversation_id": request.conversation_id,
        "route": decision.route,
        "reason": decision.reason,
        "provider_key": request.provider_key,
        "model": request.model,
        "project": project,
    }


@app.post("/v1/chat/run")
async def run_chat(http_request: Request, request: ChatRequest) -> dict[str, object]:
    if (request.provider_key != "tokenkey" or request.model != CHAT_MODEL
            or not is_allowed(request.provider_key, request.model)):
        raise HTTPException(status_code=400, detail="provider/model is not enabled")
    decision = _decide_or_400(request)
    if decision.enterprise_context_required:
        await _authorize_enterprise(http_request, request)
        raise HTTPException(status_code=501, detail="enterprise capability runner is not configured")
    if request.file_ids:
        raise HTTPException(status_code=400, detail="this legacy chat endpoint does not accept attachments")
    if isinstance(getattr(http_request.state, "user", None), dict):
        user_no, _ = _actor(http_request)
        try:
            await asyncio.to_thread(assert_personal_conversation, user_no=user_no,
                                    client_conversation_id=request.conversation_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="conversation scope cannot be changed") from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="conversation scope storage unavailable") from exc
    try:
        result = await dsh_adapter.run(
            DshConversationRequest(
                conversation_id=request.conversation_id,
                input_text=request.input,
                provider_key=request.provider_key,
                model=request.model,
                workspace_key=_user_workspace_key(http_request),
            )
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="dsh service unavailable") from exc
    history_persisted: bool | None = None
    if isinstance(getattr(http_request.state, "user", None), dict):
        user_no, organization_code = _actor(http_request)
        try:
            await asyncio.to_thread(
                record_chat_turn,
                user_no=user_no,
                organization_code=organization_code,
                client_conversation_id=request.conversation_id,
                input_text=request.input,
                assistant_text=result.text,
                provider_key=request.provider_key,
                model=request.model,
                route_mode=request.route_mode,
                project_key=request.project_key,
                plugin_key=request.plugin_key,
            )
            history_persisted = True
        except Exception:
            # A model response should remain usable when the history database
            # is temporarily unavailable; the flag lets the UI surface it.
            history_persisted = False
    return {
        "status": result.status,
        "conversation_id": result.conversation_id,
        "route": decision.route,
        "text": result.text,
        "events": result.events,
        "history_persisted": history_persisted,
    }
