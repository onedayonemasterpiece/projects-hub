from __future__ import annotations

from contextlib import asynccontextmanager
import importlib.util
import logging
import time
import uuid
from typing import Any, Literal

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import COOKIE_NAME, issue_session, parse_session
from .live_resources import ConversationScope
from .live_runtime import build_live_host
from .logging_config import configure_logging
from .settings import Settings
from .store import DurableStore, StoreError

log = logging.getLogger("projects_hub.api")


class DevLogin(BaseModel):
    display_name: str = Field(default="Pilot user", max_length=80)


class ConversationCreate(BaseModel):
    workspace_id: str
    focus_project_id: str | None = None


class LiveStart(BaseModel):
    audio_mode: Literal["realtime", "buffered"] = "realtime"


class LiveInput(BaseModel):
    audio_base64: str | None = Field(default=None, max_length=16_000)
    audio_stream_end: bool | None = None
    activity_start: bool | None = None
    activity_end: bool | None = None
    text: str | None = Field(default=None, max_length=4_000)


def _http_for_code(code: str) -> int:
    if code in {"UNAUTHENTICATED"}:
        return 401
    if code in {"FORBIDDEN"}:
        return 403
    if code.endswith("_NOT_FOUND") or code == "LIVE_SESSION_NOT_FOUND":
        return 404
    if code in {"LIVE_BUSY"}:
        return 429
    if code.startswith("INVALID") or code in {"SOURCE_TRANSCRIPT_PENDING"}:
        return 409 if code == "SOURCE_TRANSCRIPT_PENDING" else 400
    return 503 if code.startswith(("LIVE_", "RESOURCE_")) else 400


def _error(exc: Exception) -> HTTPException:
    code = str(getattr(exc, "code", type(exc).__name__))
    return HTTPException(
        status_code=_http_for_code(code),
        detail={"code": code, "message": str(exc)[:500]},
    )


def _loopback_dev_request(request: Request) -> bool:
    # Development login is deliberately not a reverse-proxy authentication mode.
    # A future public deployment must use a real supported IdP/OIDC flow.
    if any(
        request.headers.get(name)
        for name in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")
    ):
        return False
    raw = (request.headers.get("host") or "").strip().lower()
    if raw.startswith("[") and "]" in raw:
        host = raw[1 : raw.index("]")]
    elif raw.count(":") == 1:
        host = raw.rsplit(":", 1)[0]
    else:
        host = raw
    return host in {"127.0.0.1", "::1", "localhost"}


def create_app(
    settings: Settings | None = None,
    *,
    store: DurableStore | None = None,
    live_host: Any | None = None,
) -> FastAPI:
    configure_logging()
    settings = settings or Settings.from_env()
    owned_store = store is None
    store = store or DurableStore(settings.data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            host = getattr(app.state, "live_host", None)
            if host is not None and hasattr(host, "stop_all"):
                await host.stop_all()
            if owned_store:
                store.close()

    app = FastAPI(title="Projects Hub", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.live_host = live_host

    def host() -> Any:
        if app.state.live_host is None:
            app.state.live_host = build_live_host(store)
        return app.state.live_host

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or "req_" + uuid.uuid4().hex[:16]
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "request failed",
                extra={
                    "event": "http_request",
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": 500,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            )
            raise
        response.headers["x-request-id"] = request_id
        log.info(
            "request",
            extra={
                "event": "http_request",
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return response

    def actor_id_from_request(request: Request) -> str:
        actor_id = parse_session(request.cookies.get(COOKIE_NAME), settings.session_secret)
        if not actor_id:
            raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED"})
        try:
            store.bootstrap(actor_id)
        except StoreError as exc:
            raise _error(exc) from exc
        return actor_id

    @app.exception_handler(StoreError)
    async def store_error(_request: Request, exc: StoreError):
        http = _error(exc)
        return JSONResponse(status_code=http.status_code, content={"error": http.detail})

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": store.ping(),
            "storage": "sqlite-wal",
            "auth_mode": "loopback_dev" if settings.dev_auth else "session",
            "release_sha": settings.release_sha,
            "static_ready": settings.static_dir.is_dir(),
            "live_interaction_available": importlib.util.find_spec("live_interaction") is not None,
            "resource_control_available": importlib.util.find_spec("ai_resource_control") is not None,
        }

    @app.post("/api/dev/login")
    async def dev_login(payload: DevLogin, response: Response, request: Request) -> dict[str, Any]:
        if not settings.dev_auth or not _loopback_dev_request(request):
            raise HTTPException(status_code=404, detail={"code": "DEV_AUTH_DISABLED"})
        bootstrap = store.ensure_dev_workspace(payload.display_name)
        token = issue_session(bootstrap["actor"]["id"], settings.session_secret)
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            max_age=7 * 24 * 60 * 60,
            path="/",
        )
        return bootstrap

    @app.post("/api/logout")
    async def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request) -> dict[str, Any]:
        return store.bootstrap(actor_id_from_request(request))

    @app.post("/api/conversations")
    async def create_conversation(payload: ConversationCreate, request: Request) -> dict[str, Any]:
        return store.create_conversation(
            actor_id_from_request(request),
            payload.workspace_id,
            payload.focus_project_id,
        )

    @app.get("/api/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
        return store.get_conversation(actor_id_from_request(request), conversation_id)

    @app.get("/api/memories")
    async def memories(
        request: Request,
        workspace_id: str,
        project_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        return {
            "items": store.list_memories(actor_id, workspace_id, project_id, limit),
        }

    @app.get("/api/sources/{source_id}")
    async def source(source_id: str, request: Request) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        item = store.get_source(actor_id, source_id)
        return {
            "source": {
                key: item[key]
                for key in (
                    "id",
                    "conversation_id",
                    "workspace_id",
                    "status",
                    "audio_bytes",
                    "audio_chunks",
                    "transcript_revision",
                    "captured_at_ms",
                    "updated_at_ms",
                )
            }
        }

    def live_context(actor_id: str, conversation_id: str) -> tuple[dict[str, Any], str, dict[str, str]]:
        conversation = store.get_conversation(actor_id, conversation_id)
        scope = ConversationScope(
            workspace_id=conversation["workspace_id"],
            subject_id=actor_id,
            conversation_id=conversation_id,
        )
        actor = {"subject": actor_id, "tenant_id": conversation["workspace_id"]}
        return conversation, scope.resource_binding(), actor

    @app.post("/api/live/{conversation_id}/sessions")
    async def live_start(
        conversation_id: str,
        request: Request,
        payload: LiveStart = Body(default_factory=LiveStart),
    ) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        conversation, resource_id, actor = live_context(actor_id, conversation_id)
        try:
            started = await host().start(
                resource_id=resource_id,
                actor=actor,
                model=settings.model,
                conversation_id=conversation_id,
                audio_mode=payload.audio_mode,
            )
        except Exception as exc:
            raise _error(exc) from exc
        log.info(
            "live started",
            extra={
                "event": "live_session",
                "conversation_id": conversation_id,
                "session_id": started.get("session_id"),
                "source_id": started.get("source_id"),
                "result": "started",
            },
        )
        return {**started, "conversation": conversation}

    @app.post("/api/live/{conversation_id}/sessions/{session_id}/input")
    async def live_input(
        conversation_id: str,
        session_id: str,
        payload: LiveInput,
        request: Request,
    ) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        _conversation, resource_id, actor = live_context(actor_id, conversation_id)
        message = payload.model_dump(exclude_none=True)
        if not message:
            raise HTTPException(status_code=400, detail={"code": "INVALID_ARGUMENT"})
        try:
            return await host().input(
                resource_id=resource_id,
                session_id=session_id,
                actor=actor,
                message=message,
            )
        except Exception as exc:
            raise _error(exc) from exc

    @app.get("/api/live/{conversation_id}/sessions/{session_id}/events")
    async def live_events(
        conversation_id: str,
        session_id: str,
        request: Request,
        after: int = 0,
    ) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        _conversation, resource_id, actor = live_context(actor_id, conversation_id)
        try:
            return host().events(
                resource_id=resource_id,
                session_id=session_id,
                actor=actor,
                after=max(0, after),
            )
        except Exception as exc:
            raise _error(exc) from exc

    @app.post("/api/live/{conversation_id}/sessions/{session_id}/stop")
    async def live_stop(conversation_id: str, session_id: str, request: Request) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        _conversation, resource_id, actor = live_context(actor_id, conversation_id)
        try:
            result = await host().stop(
                resource_id=resource_id,
                session_id=session_id,
                actor=actor,
            )
        except Exception as exc:
            raise _error(exc) from exc
        log.info(
            "live stopped",
            extra={
                "event": "live_session",
                "conversation_id": conversation_id,
                "session_id": session_id,
                "result": "stopped",
            },
        )
        return result

    if settings.static_dir.is_dir():
        assets = settings.static_dir / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}")
        async def spa(path: str):
            target = settings.static_dir / path
            if path and target.is_file():
                return FileResponse(target)
            index = settings.static_dir / "index.html"
            if index.is_file():
                return FileResponse(index)
            return JSONResponse(status_code=404, content={"error": {"code": "UI_NOT_BUILT"}})
    else:
        @app.get("/")
        async def root() -> dict[str, str]:
            return {"service": "projects-hub", "ui": "not-built"}

    return app
