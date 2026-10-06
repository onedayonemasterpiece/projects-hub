from __future__ import annotations

from contextlib import asynccontextmanager
import html
import importlib.util
import logging
import time
import uuid
from typing import Any, Literal

from fastapi import Body, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .auth import COOKIE_NAME, SESSION_TTL_SECONDS, issue_session, parse_session
from .board import BoardService
from .board_api import attach_board_routes
from .board_view_context import BoardViewContextStore
from .board_view_context_api import attach_board_view_context_routes
from .collaboration import CollaborationService
from .collaboration_api import attach_collaboration_routes
from .collaboration_analysis import CollaborationAnalysisService
from .collaboration_analysis_api import attach_collaboration_analysis_routes
from .github_app import GitHubAppError
from .github_connections import GitHubConnections
from .device_commands import DeviceCommandService
from .development import DevelopmentService
from .live_resources import ConversationScope
from .live_runtime import build_live_host
from .logging_config import configure_logging
from .readiness import ReadinessService
from .settings import Settings
from .store import DurableStore, StoreError
from .version import __version__

log = logging.getLogger("projects_hub.api")


class DevLogin(BaseModel):
    display_name: str = Field(default="Pilot user", max_length=80)


class InviteLogin(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class OwnerInviteRequest(BaseModel):
    display_name: str = Field(default="Владелец", min_length=1, max_length=80)
    ttl_seconds: int = Field(default=15 * 60, ge=60, le=24 * 60 * 60)


class ConversationCreate(BaseModel):
    workspace_id: str
    focus_project_id: str | None = None


class LiveStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audio_mode: Literal["realtime", "buffered"] = "realtime"
    recovery_only: bool = False
    client_source_id: str | None = Field(
        default=None,
        pattern=r"^local_[0-9a-f]{32}$",
        max_length=38,
    )
    transport: Literal["wss"] = "wss"
    client_version: str | None = Field(
        default=None,
        pattern=r"^[0-9A-Za-z._+-]{1,32}$",
        max_length=32,
    )
    client_timezone: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9._+/-]{1,100}$",
        max_length=100,
    )
    attempt_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9._:-]{1,96}$",
        max_length=96,
    )


class LiveInput(BaseModel):
    audio_base64: str | None = Field(default=None, max_length=16_000)
    audio_stream_end: bool | None = None
    activity_start: bool | None = None
    activity_end: bool | None = None
    text: str | None = Field(default=None, max_length=4_000)


class GitHubInstallStart(BaseModel):
    workspace_id: str
    conversation_id: str | None = None


class GitHubRepositoryBind(BaseModel):
    workspace_id: str
    project_id: str | None = None
    role: str = Field(max_length=64)
    access_mode: str = Field(max_length=64)
    allowed_paths: list[str] = Field(default_factory=list, max_length=32)


class DeviceRegister(BaseModel):
    workspace_id: str
    display_name: str = Field(min_length=1, max_length=120)
    platform: Literal["android"] = "android"
    capabilities: list[str] = Field(min_length=1, max_length=16)


class DeviceCapabilitiesUpdate(BaseModel):
    capabilities: list[str] = Field(min_length=1, max_length=16)


class TaskStateChange(BaseModel):
    workspace_id: str
    state: Literal["accepted", "done", "snoozed", "rejected"]


class DeviceReceipt(BaseModel):
    claim_token: str = Field(min_length=20, max_length=200)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["applied", "rejected", "failed"]
    result: dict[str, Any] = Field(default_factory=dict)


def _http_for_code(code: str) -> int:
    if code in {"UNAUTHENTICATED", "DEVICE_UNAUTHENTICATED", "INVITE_INVALID"}:
        return 401
    if code in {
        "FORBIDDEN",
        "GITHUB_FORBIDDEN",
        "GITHUB_WRITE_POLICY_DENIED",
        "GITHUB_WRITE_PERMISSION_MISSING",
        "GITHUB_WEBHOOK_INVALID",
        "PROJECT_FORBIDDEN",
        "DEVICE_COMMAND_CLAIM_INVALID",
    }:
        return 403
    if code in {"IDENTITY_PROVIDER_INVALID"}:
        return 502
    if code in {"IDENTITY_PROVIDER_UNAVAILABLE"}:
        return 503
    if code.endswith("_NOT_FOUND") or code in {
        "LIVE_SESSION_NOT_FOUND",
        "GITHUB_NOT_FOUND",
        "GITHUB_REPOSITORY_NOT_FOUND",
        "GITHUB_INSTALLATION_NOT_FOUND",
        "DEVICE_NOT_FOUND",
        "DEVICE_COMMAND_NOT_FOUND",
    }:
        return 404
    if code in {"LIVE_BUSY"}:
        return 429
    if code in {
        "DEVICE_SELECTION_REQUIRED",
        "DEVICE_CAPABILITY_NOT_AVAILABLE",
        "DEVICE_COMMAND_CONFLICT",
        "DEVICE_COMMAND_OUTCOME_UNKNOWN",
        "DEVICE_READBACK_REQUIRED",
        "LIVE_TRANSPORT_MISMATCH",
        "LIVE_SOCKET_BUSY",
        "GITHUB_PROJECT_DOCS_REQUIRED",
        "GITHUB_WRITE_CONFLICT",
        "COLLABORATION_COMMAND_CONFLICT",
        "GITHUB_READBACK_MISMATCH",
    }:
        return 409
    if code.startswith("INVALID") or code in {"SOURCE_TRANSCRIPT_PENDING"}:
        return 409 if code == "SOURCE_TRANSCRIPT_PENDING" else 400
    return 503 if code.startswith(("LIVE_", "RESOURCE_")) or code in {
        "GITHUB_APP_NOT_CONFIGURED",
        "GITHUB_UNAVAILABLE",
        "GITHUB_ERROR",
        "GITHUB_INVALID_RESPONSE",
    } else 400


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


def _public_edge_request(request: Request, settings: Settings) -> bool:
    if not settings.public_auth_enabled:
        return False
    expected_host = settings.public_origin.removeprefix("https://")
    return (
        (request.headers.get("host") or "").strip().lower() == expected_host.lower()
        and (request.headers.get("x-forwarded-host") or "").strip().lower() == expected_host.lower()
        and (request.headers.get("x-forwarded-proto") or "").strip().lower() == "https"
        and (request.headers.get("x-forwarded-port") or "").strip() == "443"
        and not request.headers.get("forwarded")
        and not request.headers.get("x-forwarded-for")
    )


async def _live_start_payload(request: Request) -> LiveStart:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 4096:
            raise HTTPException(
                status_code=413,
                detail={"code": "LIVE_BOOTSTRAP_TOO_LARGE"},
            )
    if not raw:
        return LiveStart()
    try:
        return LiveStart.model_validate_json(bytes(raw))
    except ValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_ARGUMENT", "message": "Invalid Live bootstrap"},
        ) from exc


def create_app(
    settings: Settings | None = None,
    *,
    store: DurableStore | None = None,
    live_host: Any | None = None,
    github_connections: Any | None = None,
    device_commands: DeviceCommandService | None = None,
    readiness: ReadinessService | None = None,
    development: DevelopmentService | None = None,
    collaboration: CollaborationService | None = None,
    collaboration_analysis: CollaborationAnalysisService | None = None,
    regional_knowledge_factory: Any | None = None,
) -> FastAPI:
    configure_logging()
    settings = settings or Settings.from_env()
    owned_store = store is None
    store = store or DurableStore(settings.data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        development_service = getattr(app.state, "development", None)
        if development_service is not None and hasattr(
            development_service, "start_background"
        ):
            await development_service.start_background()
        collaboration_analysis_service = getattr(app.state, "collaboration_analysis", None)
        if collaboration_analysis_service is not None:
            await collaboration_analysis_service.start_background()
        try:
            yield
        finally:
            host = getattr(app.state, "live_host", None)
            if host is not None and hasattr(host, "stop_all"):
                await host.stop_all()
            if collaboration_analysis_service is not None:
                await collaboration_analysis_service.close()
            if development_service is not None and hasattr(
                development_service, "close"
            ):
                await development_service.close()
            if owned_store:
                store.close()

    app = FastAPI(title="Projects Hub", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.board = BoardService(store)
    app.state.board_view_context = BoardViewContextStore(store, app.state.board)
    app.state.board_hub = None
    app.state.live_host = live_host
    app.state.github_connections = github_connections or GitHubConnections(store, settings)
    app.state.device_commands = device_commands or DeviceCommandService(store)
    app.state.readiness = readiness or ReadinessService(store)
    app.state.development = development or DevelopmentService(store, app.state.readiness)
    app.state.collaboration = collaboration or CollaborationService(
        store, app.state.github_connections
    )
    app.state.collaboration_analysis = collaboration_analysis or CollaborationAnalysisService(
        store,
        app.state.collaboration,
        development=app.state.development,
    )
    def host() -> Any:
        if app.state.live_host is None:
            app.state.live_host = build_live_host(
                store,
                board=app.state.board,
                board_hub=app.state.board_hub,
                board_view_context=app.state.board_view_context,
                device_commands=app.state.device_commands,
                readiness=app.state.readiness,
                development=app.state.development,
                github_connections=app.state.github_connections,
                collaboration=app.state.collaboration,
                collaboration_analysis=app.state.collaboration_analysis,
                regional_knowledge_factory=regional_knowledge_factory,
            )
        return app.state.live_host

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            direct_loopback = _loopback_dev_request(request)
            if settings.public_auth_enabled and not direct_loopback:
                if not _public_edge_request(request, settings):
                    return JSONResponse(
                        status_code=403,
                        content={"error": {"code": "PUBLIC_ORIGIN_REQUIRED"}},
                    )
                device_call = request.url.path.startswith("/api/device/")
                if request.url.path != "/api/github/webhook" and not device_call:
                    origin = (request.headers.get("origin") or "").strip().rstrip("/")
                    if origin != settings.public_origin:
                        return JSONResponse(
                            status_code=403,
                            content={"error": {"code": "ORIGIN_MISMATCH"}},
                        )
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

    app.state.board_hub = attach_board_routes(
        app,
        service=app.state.board,
        actor_id_from_request=actor_id_from_request,
        session_secret=settings.session_secret,
        cookie_name=COOKIE_NAME,
    )
    attach_board_view_context_routes(
        app,
        service=app.state.board_view_context,
        actor_id_from_request=actor_id_from_request,
    )
    attach_collaboration_routes(
        app,
        service=app.state.collaboration,
        actor_id_from_request=actor_id_from_request,
    )
    attach_collaboration_analysis_routes(
        app,
        service=app.state.collaboration_analysis,
        actor_id_from_request=actor_id_from_request,
    )

    @app.exception_handler(StoreError)
    async def store_error(_request: Request, exc: StoreError):
        http = _error(exc)
        return JSONResponse(status_code=http.status_code, content={"error": http.detail})

    @app.exception_handler(GitHubAppError)
    async def github_error(_request: Request, exc: GitHubAppError):
        return JSONResponse(
            status_code=_http_for_code(exc.code),
            content={"error": {"code": exc.code, "message": str(exc)[:300]}},
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": store.ping(),
            "storage": "sqlite-wal",
            "auth_mode": (
                "first_party_invite+loopback_dev"
                if settings.public_auth_enabled and settings.dev_auth
                else "first_party_invite"
                if settings.public_auth_enabled
                else "loopback_dev"
                if settings.dev_auth
                else "session"
            ),
            "version": __version__,
            "release_sha": settings.release_sha,
            "static_ready": settings.static_dir.is_dir(),
            "live_interaction_available": importlib.util.find_spec("live_interaction") is not None,
            "resource_control_available": importlib.util.find_spec("ai_resource_control") is not None,
            "github_app_configured": app.state.github_connections.configured,
        }

    @app.get("/api/auth/config")
    async def auth_config() -> dict[str, Any]:
        if settings.public_auth_enabled:
            return {"mode": "first_party_invite"}
        return {
            "mode": "loopback_dev" if settings.dev_auth else "disabled",
        }

    @app.post("/api/auth/invite")
    async def auth_invite(
        payload: InviteLogin,
        response: Response,
        request: Request,
    ) -> dict[str, Any]:
        if not settings.public_auth_enabled or not _public_edge_request(request, settings):
            raise HTTPException(status_code=404, detail={"code": "PUBLIC_AUTH_DISABLED"})
        bootstrap = store.consume_login_invite(payload.token)
        token = issue_session(bootstrap["actor"]["id"], settings.session_secret)
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            max_age=SESSION_TTL_SECONDS,
            path="/",
        )
        log.info(
            "first-party invite consumed",
            extra={"event": "auth_invite", "result": "ok"},
        )
        return bootstrap

    @app.post("/api/dev/owner-invite")
    async def dev_owner_invite(
        payload: OwnerInviteRequest,
        request: Request,
    ) -> dict[str, Any]:
        if not settings.dev_auth or not _loopback_dev_request(request):
            raise HTTPException(status_code=404, detail={"code": "DEV_AUTH_DISABLED"})
        return store.issue_platform_owner_invite(
            payload.display_name,
            ttl_seconds=payload.ttl_seconds,
        )

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
            secure=False,
            samesite="lax",
            max_age=SESSION_TTL_SECONDS,
            path="/",
        )
        return bootstrap

    @app.post("/api/logout")
    async def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @app.get("/api/github/status")
    async def github_status(request: Request, workspace_id: str) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        return app.state.github_connections.status(actor_id, workspace_id)

    @app.post("/api/github/app-manifest/start")
    async def github_app_manifest_start(
        payload: GitHubInstallStart,
        request: Request,
    ) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        return app.state.github_connections.start_manifest_registration(
            actor_id=actor_id,
            workspace_id=payload.workspace_id,
            conversation_id=payload.conversation_id,
        )

    @app.post("/api/github/app-manifest/legacy-launch")
    async def github_app_manifest_legacy_launch(state: str):
        app.state.github_connections.manifest_launch(state)
        if not state or len(state) > 200 or not all(
            ch.isalnum() or ch in {"_", "-"} for ch in state
        ):
            raise HTTPException(
                status_code=400,
                detail={"code": "GITHUB_APP_MANIFEST_STATE_INVALID"},
            )
        return RedirectResponse(
            url=f"projectshub://browser/github?state={state}",
            status_code=303,
        )

    @app.get("/api/github/app-manifest/launch")
    async def github_app_manifest_launch(state: str):
        payload = app.state.github_connections.manifest_launch(state)
        action = html.escape(payload["action_url"], quote=True)
        manifest = html.escape(payload["manifest"], quote=True)
        opaque_state = html.escape(payload["state"], quote=True)
        body = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Projects Hub → GitHub</title></head>
<body>
<form id="github" method="post" action="{action}">
<input type="hidden" name="manifest" value="{manifest}">
<input type="hidden" name="state" value="{opaque_state}">
<noscript><button type="submit">Продолжить в GitHub</button></noscript>
</form>
<script>document.getElementById("github").submit()</script>
</body></html>"""
        response = HTMLResponse(body)
        response.headers["cache-control"] = "no-store"
        response.headers["referrer-policy"] = "no-referrer"
        response.headers["content-security-policy"] = (
            "default-src 'none'; script-src 'unsafe-inline'; "
            "form-action https://github.com; base-uri 'none'"
        )
        return response

    @app.get("/api/github/app-manifest/callback")
    async def github_app_manifest_callback(
        code: str,
        state: str,
    ):
        result = await app.state.github_connections.complete_manifest_registration(
            state=state,
            code=code,
        )
        log.info(
            "github app registered",
            extra={
                "event": "github_app_manifest",
                "workspace_id": result["workspace_id"],
                "result": "registered",
            },
        )
        return RedirectResponse(url=result["install_url"], status_code=303)

    @app.post("/api/github/install/start")
    async def github_install_start(
        payload: GitHubInstallStart,
        request: Request,
    ) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        return app.state.github_connections.start_install(
            actor_id=actor_id,
            workspace_id=payload.workspace_id,
            conversation_id=payload.conversation_id,
        )

    @app.get("/api/github/install/callback")
    async def github_install_callback(
        installation_id: int,
        state: str,
        setup_action: str | None = None,
    ):
        result = await app.state.github_connections.complete_install(
            state=state,
            installation_id=installation_id,
        )
        log.info(
            "github installation connected",
            extra={
                "event": "github_installation",
                "workspace_id": result["workspace_id"],
                "installation_id": installation_id,
                "result": "connected",
            },
        )
        return RedirectResponse(url="/api/github/return", status_code=303)

    @app.get("/api/github/return")
    async def github_return():
        body = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GitHub подключён</title>
<style>body{margin:0;background:#0a0a0c;color:#f2f2f4;font:16px system-ui;display:grid;place-items:center;min-height:100vh}main{max-width:420px;padding:28px;text-align:center}a{display:inline-block;margin-top:18px;padding:12px 18px;border-radius:14px;background:#f2f2f4;color:#0a0a0c;text-decoration:none;font-weight:700}</style>
</head><body><main><h1>GitHub подключён</h1><p>Возвращаю вас в Projects Hub.</p>
<a href="projectshub://github/connected">Вернуться в Projects Hub</a></main>
<script>location.href="projectshub://github/connected"</script></body></html>"""
        response = HTMLResponse(body)
        response.headers["cache-control"] = "no-store"
        response.headers["referrer-policy"] = "no-referrer"
        response.headers["content-security-policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "navigate-to projectshub:"
        )
        return response

    @app.post("/api/github/repositories/{repository_id}/bind")
    async def github_repository_bind(
        repository_id: int,
        payload: GitHubRepositoryBind,
        request: Request,
    ) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        return app.state.github_connections.bind_repository(
            actor_id=actor_id,
            workspace_id=payload.workspace_id,
            repository_id=repository_id,
            project_id=payload.project_id,
            role=payload.role,
            access_mode=payload.access_mode,
            allowed_paths=payload.allowed_paths,
        )

    @app.post("/api/github/webhook")
    async def github_webhook(request: Request) -> dict[str, Any]:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > 1_000_000:
                    raise HTTPException(
                        status_code=413,
                        detail={"code": "PAYLOAD_TOO_LARGE"},
                    )
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "INVALID_ARGUMENT"},
                )
        body = await request.body()
        return await app.state.github_connections.webhook(
            body=body,
            signature=request.headers.get("x-hub-signature-256"),
            delivery_id=request.headers.get("x-github-delivery"),
            event_name=request.headers.get("x-github-event"),
        )

    @app.post("/api/device/capabilities")
    async def update_device_capabilities(
        payload: DeviceCapabilitiesUpdate,
        request: Request,
    ) -> dict[str, Any]:
        return {
            "device": app.state.device_commands.update_capabilities(
                authorization=request.headers.get("authorization"),
                capabilities=payload.capabilities,
            )
        }

    @app.post("/api/devices/register")
    async def register_device(
        payload: DeviceRegister,
        request: Request,
    ) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        result = app.state.device_commands.register_device(
            actor_id=actor_id,
            workspace_id=payload.workspace_id,
            display_name=payload.display_name,
            platform=payload.platform,
            capabilities=payload.capabilities,
        )
        log.info(
            "device registered",
            extra={
                "event": "device_registered",
                "actor_id": actor_id,
                "workspace_id": payload.workspace_id,
                "device_id": result["device"]["id"],
                "platform": payload.platform,
            },
        )
        return result

    @app.get("/api/devices")
    async def list_devices(
        request: Request,
        workspace_id: str,
    ) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        return {
            "devices": app.state.device_commands.list_devices(
                actor_id=actor_id,
                workspace_id=workspace_id,
            )
        }

    @app.post("/api/devices/{device_id}/disable")
    async def disable_device(
        device_id: str,
        request: Request,
        workspace_id: str = Body(embed=True),
    ) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        app.state.device_commands.disable_device(
            actor_id=actor_id,
            workspace_id=workspace_id,
            device_id=device_id,
        )
        return {"ok": True, "device_id": device_id}

    @app.get("/api/device/commands/next")
    async def next_device_command(
        request: Request,
        wait_ms: int = 25_000,
    ) -> dict[str, Any]:
        return await app.state.device_commands.next_command(
            authorization=request.headers.get("authorization"),
            wait_ms=max(0, min(wait_ms, 25_000)),
        )

    @app.post("/api/device/commands/{command_id}/receipt")
    async def device_command_receipt(
        command_id: str,
        payload: DeviceReceipt,
        request: Request,
    ) -> dict[str, Any]:
        result = app.state.device_commands.receipt(
            authorization=request.headers.get("authorization"),
            command_id=command_id,
            claim_token=payload.claim_token,
            payload_sha256=payload.payload_sha256,
            status=payload.status,
            result=payload.result,
        )
        log.info(
            "device command receipt",
            extra={
                "event": "device_command_receipt",
                "command_id": command_id,
                "device_id": result.get("device_id"),
                "capability": result.get("capability"),
                "result": result.get("status"),
            },
        )
        return result

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

    @app.get("/api/event-cards")
    async def event_cards(
        request: Request,
        workspace_id: str,
        project_id: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        return {"items": app.state.readiness.list_event_cards(
            actor_id=actor_id,
            workspace_id=workspace_id,
            project_id=project_id,
            limit=limit,
        )}

    @app.get("/api/tasks")
    async def tasks(
        request: Request,
        workspace_id: str,
        project_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        return {"items": app.state.readiness.list_tasks(
            actor_id=actor_id,
            workspace_id=workspace_id,
            project_id=project_id,
            limit=limit,
        )}

    @app.post("/api/tasks/{task_id}/state")
    async def task_state(task_id: str, payload: TaskStateChange, request: Request) -> dict[str, Any]:
        return app.state.readiness.set_task_state(
            actor_id=actor_id_from_request(request),
            workspace_id=payload.workspace_id,
            task_id=task_id,
            state=payload.state,
        )

    @app.get("/api/development/backlog")
    async def development_backlog(
        request: Request,
        workspace_id: str,
        project_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return {
            "items": app.state.development.list_backlog(
                actor_id=actor_id_from_request(request),
                workspace_id=workspace_id,
                project_id=project_id,
                limit=limit,
            )
        }

    @app.get("/api/development/codex-status")
    async def development_codex_status(
        request: Request,
        workspace_id: str,
    ) -> dict[str, Any]:
        return await app.state.development.codex_status(
            actor_id=actor_id_from_request(request),
            workspace_id=workspace_id,
        )

    @app.get("/api/development/executions")
    async def development_executions(
        request: Request,
        workspace_id: str,
        project_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        return {
            "items": app.state.development.list_executions(
                actor_id=actor_id,
                workspace_id=workspace_id,
                project_id=project_id,
                limit=limit,
            )
        }

    @app.get("/api/development/executions/latest")
    async def development_execution_latest(
        request: Request,
        workspace_id: str,
        sync: bool = True,
    ) -> dict[str, Any]:
        return await app.state.development.status(
            actor_id=actor_id_from_request(request),
            workspace_id=workspace_id,
            execution_id=None,
            sync=sync,
        )

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

    def public_source(item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: item.get(key)
            for key in (
                "id",
                "conversation_id",
                "workspace_id",
                "client_source_id",
                "status",
                "audio_bytes",
                "audio_chunks",
                "transcript_revision",
                "captured_at_ms",
                "updated_at_ms",
            )
        }

    @app.get("/api/sources/{source_id}")
    async def source(source_id: str, request: Request) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        item = public_source(store.get_source(actor_id, source_id))
        item.update(store.voice_source_recovery_verdict(actor_id, source_id))
        return {"source": item}

    @app.get("/api/sources/{source_id}/audio")
    async def source_audio(
        source_id: str,
        request: Request,
        start_bytes: int | None = None,
        end_bytes: int | None = None,
    ) -> Response:
        actor_id = actor_id_from_request(request)
        source_item = store.get_source(actor_id, source_id)
        path = store.voice_source_audio_path(actor_id, source_id)
        headers = {"Cache-Control": "no-store, private"}
        if start_bytes is None and end_bytes is None:
            return FileResponse(
                path,
                media_type="audio/L16;rate=16000",
                headers=headers,
            )
        total = int(source_item["audio_bytes"])
        start = 0 if start_bytes is None else int(start_bytes)
        end = total if end_bytes is None else int(end_bytes)
        if (
            start < 0
            or end <= start
            or end > total
            or start % 2
            or end % 2
            or end - start > 32 * 1024 * 1024
        ):
            raise HTTPException(status_code=400, detail={"code": "INVALID_AUDIO_RANGE"})
        with path.open("rb") as handle:
            handle.seek(start)
            payload = handle.read(end - start)
        if len(payload) != end - start:
            raise HTTPException(status_code=409, detail={"code": "AUDIO_RANGE_INCOMPLETE"})
        headers["X-Projects-Hub-Audio-Start"] = str(start)
        headers["X-Projects-Hub-Audio-End"] = str(end)
        return Response(
            content=payload,
            media_type="audio/L16;rate=16000",
            headers=headers,
        )

    @app.get("/api/conversations/{conversation_id}/sources/by-client/{client_source_id}")
    async def source_by_client(
        conversation_id: str,
        client_source_id: str,
        request: Request,
    ) -> dict[str, Any]:
        if not client_source_id.startswith("local_") or len(client_source_id) != 38:
            raise HTTPException(status_code=400, detail={"code": "INVALID_ARGUMENT"})
        actor_id = actor_id_from_request(request)
        return {
            "source": public_source(
                store.get_source_by_client(actor_id, conversation_id, client_source_id)
            )
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
    ) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        payload = await _live_start_payload(request)
        conversation, resource_id, actor = live_context(actor_id, conversation_id)
        try:
            started = await host().start(
                resource_id=resource_id,
                actor=actor,
                model=settings.model,
                history=store.recent_conversation_history(actor_id, conversation_id),
                conversation_id=conversation_id,
                audio_mode=payload.audio_mode,
                recovery_only=payload.recovery_only,
                client_source_id=payload.client_source_id,
                client_version=payload.client_version,
                client_timezone=payload.client_timezone,
                backend_version=__version__,
                backend_release_sha=settings.release_sha,
                attempt_id=payload.attempt_id,
            )
        except Exception as exc:
            code = str(getattr(exc, "code", type(exc).__name__))
            log.warning(
                "live start failed",
                extra={
                    "event": "live_session",
                    "conversation_id": conversation_id,
                    "result": "start_failed",
                    "code": code,
                    "exception_type": type(exc).__name__,
                },
            )
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
        return {
            **started,
            "conversation": conversation,
            "transport": "wss",
            "socket_url": (
                f"/api/live/{conversation_id}/sessions/"
                f"{started['session_id']}/socket"
            ),
        }

    @app.post("/api/live/{conversation_id}/sessions/{session_id}/socket-ticket")
    async def live_socket_ticket(
        conversation_id: str,
        session_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        _conversation, resource_id, actor = live_context(actor_id, conversation_id)
        try:
            ticket = host().issue_socket_ticket(
                session_id=session_id,
                resource_id=resource_id,
                actor=actor,
            )
        except Exception as exc:
            raise _error(exc) from exc
        return {
            **ticket,
            "socket_url": f"/api/live/{conversation_id}/sessions/{session_id}/socket",
        }

    @app.websocket("/api/live/{conversation_id}/sessions/{session_id}/socket")
    async def live_socket(
        websocket: WebSocket,
        conversation_id: str,
        session_id: str,
    ) -> None:
        from live_interaction import LiveError
        from live_interaction.socket_transport import (
            SOCKET_PROTOCOL,
            same_origin,
            serve_socket,
            socket_ticket as parse_socket_ticket,
        )

        if websocket.scope.get("query_string") or not same_origin(
            websocket.headers.get("origin"),
            websocket.headers.get("host"),
        ):
            await websocket.close(code=1008)
            return

        actor_id = parse_session(
            websocket.cookies.get(COOKIE_NAME),
            settings.session_secret,
        )
        if not actor_id:
            await websocket.close(code=1008)
            return
        try:
            store.bootstrap(actor_id)
            _conversation, resource_id, _actor = live_context(actor_id, conversation_id)
            ticket = parse_socket_ticket(websocket.scope.get("subprotocols", []))
            binding = host().open_socket(
                session_id=session_id,
                resource_id=resource_id,
                ticket=ticket,
            )
        except (HTTPException, StoreError, LiveError):
            await websocket.close(code=1008)
            return

        try:
            await websocket.accept(subprotocol=SOCKET_PROTOCOL)
        except Exception:
            await binding.close()
            raise

        async def receive() -> str | bytes | None:
            try:
                message = await websocket.receive()
            except WebSocketDisconnect:
                return None
            if message["type"] == "websocket.disconnect":
                return None
            if message.get("bytes") is not None:
                return message["bytes"]
            return message.get("text")

        async def send(payload: str | bytes) -> None:
            if isinstance(payload, bytes):
                await websocket.send_bytes(payload)
            else:
                await websocket.send_text(payload)

        async def close(code: int, reason: str) -> None:
            await websocket.close(code=code, reason=reason)

        await serve_socket(binding, receive=receive, send=send, close=close)

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
