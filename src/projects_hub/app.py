from __future__ import annotations

from contextlib import asynccontextmanager
import importlib.util
import logging
import time
import uuid
from typing import Any, Literal

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import COOKIE_NAME, SESSION_TTL_SECONDS, issue_session, parse_session
from .identity import IdentityError, SupabaseIdentityVerifier
from .github_app import GitHubAppError
from .github_connections import GitHubConnections
from .device_commands import DeviceCommandService
from .live_resources import ConversationScope
from .live_runtime import build_live_host
from .logging_config import configure_logging
from .settings import Settings
from .store import DurableStore, StoreError

log = logging.getLogger("projects_hub.api")


class DevLogin(BaseModel):
    display_name: str = Field(default="Pilot user", max_length=80)


class AuthExchange(BaseModel):
    access_token: str = Field(min_length=20, max_length=8192)


class ConversationCreate(BaseModel):
    workspace_id: str
    focus_project_id: str | None = None


class LiveStart(BaseModel):
    audio_mode: Literal["realtime", "buffered"] = "realtime"
    client_source_id: str | None = Field(
        default=None,
        pattern=r"^local_[0-9a-f]{32}$",
        max_length=38,
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


class DeviceReceipt(BaseModel):
    claim_token: str = Field(min_length=20, max_length=200)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["applied", "rejected", "failed"]
    result: dict[str, Any] = Field(default_factory=dict)


def _http_for_code(code: str) -> int:
    if code in {"UNAUTHENTICATED", "DEVICE_UNAUTHENTICATED"}:
        return 401
    if code in {
        "FORBIDDEN",
        "GITHUB_FORBIDDEN",
        "GITHUB_WRITE_POLICY_DENIED",
        "GITHUB_WRITE_PERMISSION_MISSING",
        "GITHUB_WEBHOOK_INVALID",
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


def create_app(
    settings: Settings | None = None,
    *,
    store: DurableStore | None = None,
    live_host: Any | None = None,
    identity_verifier: Any | None = None,
    github_connections: Any | None = None,
    device_commands: DeviceCommandService | None = None,
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
    app.state.identity_verifier = identity_verifier
    app.state.github_connections = github_connections or GitHubConnections(store, settings)
    app.state.device_commands = device_commands or DeviceCommandService(store)
    if app.state.identity_verifier is None and settings.public_auth_enabled:
        app.state.identity_verifier = SupabaseIdentityVerifier(
            base_url=settings.auth_supabase_url,
            publishable_key=settings.auth_supabase_publishable_key,
            provider=settings.auth_provider,
        )

    def host() -> Any:
        if app.state.live_host is None:
            app.state.live_host = build_live_host(
                store,
                device_commands=app.state.device_commands,
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

    @app.exception_handler(StoreError)
    async def store_error(_request: Request, exc: StoreError):
        http = _error(exc)
        return JSONResponse(status_code=http.status_code, content={"error": http.detail})

    @app.exception_handler(IdentityError)
    async def identity_error(_request: Request, exc: IdentityError):
        return JSONResponse(
            status_code=_http_for_code(exc.code),
            content={"error": {"code": exc.code, "message": str(exc)[:300]}},
        )

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
                "public_yandex+loopback_dev"
                if settings.public_auth_enabled and settings.dev_auth
                else "public_yandex"
                if settings.public_auth_enabled
                else "loopback_dev"
                if settings.dev_auth
                else "session"
            ),
            "release_sha": settings.release_sha,
            "static_ready": settings.static_dir.is_dir(),
            "live_interaction_available": importlib.util.find_spec("live_interaction") is not None,
            "resource_control_available": importlib.util.find_spec("ai_resource_control") is not None,
            "github_app_configured": settings.github_app_enabled,
        }

    @app.get("/api/auth/config")
    async def auth_config() -> dict[str, Any]:
        if settings.public_auth_enabled:
            return {
                "mode": "yandex_pkce",
                "supabase_url": settings.auth_supabase_url,
                "publishable_key": settings.auth_supabase_publishable_key,
                "provider": settings.auth_provider,
                "redirect_url": settings.public_origin + "/",
            }
        return {
            "mode": "loopback_dev" if settings.dev_auth else "disabled",
        }

    @app.post("/api/auth/exchange")
    async def auth_exchange(
        payload: AuthExchange,
        response: Response,
        request: Request,
    ) -> dict[str, Any]:
        if not settings.public_auth_enabled or not _public_edge_request(request, settings):
            raise HTTPException(status_code=404, detail={"code": "PUBLIC_AUTH_DISABLED"})
        verifier = app.state.identity_verifier
        if verifier is None:
            raise HTTPException(status_code=503, detail={"code": "IDENTITY_PROVIDER_UNAVAILABLE"})
        identity = await verifier.verify(payload.access_token)
        bootstrap = store.ensure_external_workspace(
            provider=str(identity["provider"]),
            subject=str(identity["subject"]),
            display_name=str(identity.get("display_name") or "Пользователь"),
            email=identity.get("email"),
        )
        token = issue_session(bootstrap["actor"]["id"], settings.session_secret)
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=SESSION_TTL_SECONDS,
            path="/",
        )
        log.info(
            "public identity exchanged",
            extra={"event": "auth_exchange", "result": "ok"},
        )
        return bootstrap

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
        request: Request,
        installation_id: int,
        state: str,
        setup_action: str | None = None,
    ):
        actor_id = actor_id_from_request(request)
        result = await app.state.github_connections.complete_install(
            actor_id=actor_id,
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
        return RedirectResponse(url="/?github=connected", status_code=303)

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
        return {"source": public_source(store.get_source(actor_id, source_id))}

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
                client_source_id=payload.client_source_id,
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
