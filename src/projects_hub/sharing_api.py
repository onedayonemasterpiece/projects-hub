from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response, WebSocket
from pydantic import BaseModel, Field

from .sharing import (
    GUEST_SESSION_COOKIE,
    SharingService,
)
from .store import StoreError


GUEST_PROTOCOL = "projects-hub-guest-board-v1"
GUEST_TICKET_PREFIX = "projects-hub-guest-ticket."
GUEST_CLIENT_PREFIX = "projects-hub-guest-client."
MAX_GUEST_SOCKETS = 64
GUEST_POLL_SECONDS = 0.35
GUEST_HEARTBEAT_SECONDS = 10.0


class ShareCreateRequest(BaseModel):
    workspace_id: str


class ShareRevokeRequest(BaseModel):
    workspace_id: str


class GuestExchangeRequest(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class GuestTicketRequest(BaseModel):
    client_instance_id: str = Field(min_length=8, max_length=128)


def _same_origin(origin: str | None, host: str | None) -> bool:
    if not origin or not host:
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc.casefold() == host.casefold()
    )


def _subprotocol(scope: dict[str, Any], prefix: str) -> str | None:
    for value in scope.get("subprotocols", []) or []:
        if isinstance(value, str) and value.startswith(prefix):
            return value[len(prefix):]
    return None


def _guest_headers(response: Response) -> None:
    response.headers["cache-control"] = "no-store"
    response.headers["x-robots-tag"] = "noindex, nofollow, noarchive"
    response.headers["referrer-policy"] = "no-referrer"
    response.headers["x-content-type-options"] = "nosniff"


def attach_sharing_routes(
    app: FastAPI,
    *,
    service: SharingService,
    actor_id_from_request: Callable[[Request], str],
    cookie_secure: bool,
) -> None:
    capacity_lock = asyncio.Lock()
    guest_socket_count = 0

    async def acquire_socket() -> bool:
        nonlocal guest_socket_count
        async with capacity_lock:
            if guest_socket_count >= MAX_GUEST_SOCKETS:
                return False
            guest_socket_count += 1
            return True

    async def release_socket() -> None:
        nonlocal guest_socket_count
        async with capacity_lock:
            guest_socket_count = max(0, guest_socket_count - 1)

    @app.post("/api/projects/{project_id}/shares")
    async def create_share(
        project_id: str,
        payload: ShareCreateRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        _guest_headers(response)
        return service.create_share(
            actor_id=actor_id_from_request(request),
            workspace_id=payload.workspace_id,
            project_id=project_id,
        )

    @app.get("/api/projects/{project_id}/shares")
    async def list_shares(
        project_id: str,
        workspace_id: str,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        _guest_headers(response)
        return {
            "items": service.list_shares(
                actor_id=actor_id_from_request(request),
                workspace_id=workspace_id,
                project_id=project_id,
            )
        }

    @app.post("/api/shares/{share_id}/revoke")
    async def revoke_share(
        share_id: str,
        payload: ShareRevokeRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        _guest_headers(response)
        return service.revoke_share(
            actor_id=actor_id_from_request(request),
            workspace_id=payload.workspace_id,
            share_id=share_id,
        )

    @app.post("/api/guest/exchange")
    async def guest_exchange(
        payload: GuestExchangeRequest,
        response: Response,
    ) -> dict[str, Any]:
        value = service.exchange(payload.token)
        ttl_seconds = max(
            1,
            min(
                7 * 24 * 60 * 60,
                (int(value["expires_at_ms"]) - round(time.time() * 1000)) // 1000,
            ),
        )
        response.set_cookie(
            GUEST_SESSION_COOKIE,
            value.pop("session_token"),
            httponly=True,
            secure=cookie_secure,
            samesite="lax",
            max_age=ttl_seconds,
            path="/",
        )
        _guest_headers(response)
        return value

    @app.get("/api/guest/board")
    async def guest_board(request: Request, response: Response) -> dict[str, Any]:
        _guest_headers(response)
        return service.guest_snapshot(request.cookies.get(GUEST_SESSION_COOKIE))

    @app.post("/api/guest/board/socket-ticket")
    async def guest_socket_ticket(
        payload: GuestTicketRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        _guest_headers(response)
        value = service.issue_socket_ticket(
            raw_session=request.cookies.get(GUEST_SESSION_COOKIE),
            client_instance_id=payload.client_instance_id,
        )
        return {
            **value,
            "socket_url": "/api/guest/board/socket",
            "protocol": GUEST_PROTOCOL,
        }

    @app.websocket("/api/guest/board/socket")
    async def guest_board_socket(websocket: WebSocket) -> None:
        if websocket.scope.get("query_string"):
            await websocket.close(code=4400, reason="query credentials are forbidden")
            return
        headers = websocket.headers
        if not _same_origin(headers.get("origin"), headers.get("host")):
            await websocket.close(code=4403, reason="origin rejected")
            return
        ticket = _subprotocol(websocket.scope, GUEST_TICKET_PREFIX)
        client_id = _subprotocol(websocket.scope, GUEST_CLIENT_PREFIX)
        protocols = websocket.scope.get("subprotocols", []) or []
        if (
            GUEST_PROTOCOL not in protocols
            or not ticket
            or not client_id
        ):
            await websocket.close(code=4401, reason="guest ticket required")
            return
        try:
            scope = service.consume_socket_ticket(
                ticket=ticket,
                client_instance_id=client_id,
            )
        except StoreError:
            await websocket.close(code=4401, reason="guest ticket invalid")
            return
        if not await acquire_socket():
            await websocket.close(code=1013, reason="guest capacity full")
            return
        await websocket.accept(subprotocol=GUEST_PROTOCOL)
        try:
            snapshot = service.guest_snapshot_for_connection(
                grant_id=scope["grant_id"],
                session_sha=scope["session_sha256"],
            )
            await websocket.send_text(
                json.dumps(
                    {"type": "snapshot", **snapshot},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            last_seq = int(snapshot["board"]["seq"])
            last_heartbeat = time.monotonic()
            while True:
                await asyncio.sleep(GUEST_POLL_SECONDS)
                try:
                    tail = service.guest_tail_for_connection(
                        grant_id=scope["grant_id"],
                        session_sha=scope["session_sha256"],
                        after_seq=last_seq,
                    )
                except StoreError:
                    await websocket.close(code=4403, reason="guest access ended")
                    return
                for item in tail["events"]:
                    await websocket.send_text(
                        json.dumps(
                            item,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                    event = item.get("event") or {}
                    last_seq = max(last_seq, int(event.get("board_seq") or last_seq))
                last_seq = max(last_seq, int(tail["current_seq"]))
                if time.monotonic() - last_heartbeat >= GUEST_HEARTBEAT_SECONDS:
                    current = service.validate_connection(
                        grant_id=scope["grant_id"],
                        session_sha=scope["session_sha256"],
                    )
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "ping",
                                "expires_at_ms": int(current["expires_at_ms"]),
                            },
                            separators=(",", ":"),
                        )
                    )
                    last_heartbeat = time.monotonic()
        except Exception:
            try:
                await websocket.close()
            except Exception:
                pass
        finally:
            await release_socket()
