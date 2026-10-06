from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .auth import parse_session
from .board import BoardService
from .store import StoreError


BOARD_PROTOCOL = "projects-hub-board-v1"
TICKET_PREFIX = "projects-hub-board-ticket."
CLIENT_PREFIX = "projects-hub-board-client."
MAX_SOCKET_TEXT_BYTES = 48 * 1024
MAX_PRESENCE_TEXT_BYTES = 4 * 1024
MAX_BOARD_PEERS = 32
PEER_QUEUE = 64
BOARD_AUTH_RECHECK_SECONDS = 1.0


class BoardOpen(BaseModel):
    workspace_id: str
    create_if_allowed: bool = True


class BoardTicketRequest(BaseModel):
    workspace_id: str
    client_instance_id: str = Field(min_length=8, max_length=128)


class BoardUiAckRequest(BaseModel):
    workspace_id: str
    token: str = Field(min_length=8, max_length=128)
    action: str
    ok: bool


class BoardCommandRequest(BaseModel):
    workspace_id: str
    command_id: str
    operation: str
    object_id: str
    expected_object_revision: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


@dataclass
class _Peer:
    peer_id: str
    websocket: WebSocket
    actor_id: str
    workspace_id: str
    board_id: str
    client_instance_id: str
    queue: asyncio.Queue[dict[str, Any]]


class BoardSocketHub:
    """Ephemeral fan-out only; durable ordering lives in BoardService."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._peers: dict[str, dict[str, _Peer]] = {}

    async def add(self, peer: _Peer) -> None:
        async with self._lock:
            bucket = self._peers.setdefault(peer.board_id, {})
            if len(bucket) >= MAX_BOARD_PEERS:
                raise StoreError("BOARD_CAPACITY", "Board socket capacity is full")
            bucket[peer.peer_id] = peer

    async def remove(self, peer: _Peer) -> None:
        async with self._lock:
            bucket = self._peers.get(peer.board_id)
            if not bucket:
                return
            bucket.pop(peer.peer_id, None)
            if not bucket:
                self._peers.pop(peer.board_id, None)

    async def publish(
        self,
        board_id: str,
        payload: dict[str, Any],
        *,
        except_peer_id: str | None = None,
    ) -> None:
        async with self._lock:
            peers = list(self._peers.get(board_id, {}).values())
        slow: list[_Peer] = []
        for peer in peers:
            if peer.peer_id == except_peer_id:
                continue
            try:
                peer.queue.put_nowait(payload)
            except asyncio.QueueFull:
                slow.append(peer)
        for peer in slow:
            await self.remove(peer)
            try:
                await peer.websocket.close(code=1013, reason="slow board client")
            except Exception:
                pass


def _same_origin(origin: str | None, host: str | None) -> bool:
    if not origin or not host:
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.netloc.casefold() == host.casefold()


def _subprotocol(scope: dict[str, Any], prefix: str) -> str | None:
    for value in scope.get("subprotocols", []) or []:
        if isinstance(value, str) and value.startswith(prefix):
            return value[len(prefix):]
    return None


async def _writer(peer: _Peer, service: BoardService) -> None:
    while True:
        payload = await peer.queue.get()
        # Shared board data must be authorized at delivery time, not only when
        # the socket was opened.
        service.snapshot(peer.actor_id, peer.workspace_id, peer.board_id)
        await peer.websocket.send_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )


def attach_board_routes(
    app: FastAPI,
    *,
    service: BoardService,
    actor_id_from_request: Callable[[Request], str],
    session_secret: str,
    cookie_name: str,
) -> BoardSocketHub:
    hub = BoardSocketHub()

    @app.post("/api/projects/{project_id}/board/open")
    async def board_open(
        project_id: str, payload: BoardOpen, request: Request
    ) -> dict[str, Any]:
        actor_id = actor_id_from_request(request)
        return service.open_board(
            actor_id,
            payload.workspace_id,
            project_id,
            create_if_allowed=payload.create_if_allowed,
        )

    @app.get("/api/boards/{board_id}/snapshot")
    async def board_snapshot(
        board_id: str, workspace_id: str, request: Request
    ) -> dict[str, Any]:
        return service.snapshot(actor_id_from_request(request), workspace_id, board_id)

    @app.get("/api/boards/{board_id}/tail")
    async def board_tail(
        board_id: str,
        workspace_id: str,
        request: Request,
        after_seq: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        return service.tail(
            actor_id_from_request(request),
            workspace_id,
            board_id,
            after_seq=after_seq,
            limit=limit,
        )

    @app.get("/api/boards/{board_id}/search")
    async def board_search(
        board_id: str,
        workspace_id: str,
        q: str,
        request: Request,
        limit: int = 20,
    ) -> dict[str, Any]:
        return {
            "items": service.search(
                actor_id_from_request(request),
                workspace_id,
                board_id,
                q,
                limit=limit,
            )
        }

    @app.get("/api/boards/{board_id}/objects/{object_id}/history")
    async def board_history(
        board_id: str,
        object_id: str,
        workspace_id: str,
        request: Request,
        limit: int = 50,
    ) -> dict[str, Any]:
        return {
            "items": service.history(
                actor_id_from_request(request),
                workspace_id,
                board_id,
                object_id,
                limit=limit,
            )
        }

    @app.post("/api/boards/{board_id}/commands")
    async def board_command(
        board_id: str, payload: BoardCommandRequest, request: Request
    ) -> dict[str, Any]:
        # Keep a deterministic legacy response instead of silently accepting
        # an old direct-edit client. Board writes belong to Mira Live tools.
        actor_id_from_request(request)
        raise StoreError(
            "BOARD_VOICE_ONLY",
            "Board mutations are available only through Mira Live tools",
        )

    @app.post("/api/boards/{board_id}/ui-ack")
    async def board_ui_ack(
        board_id: str, payload: BoardUiAckRequest, request: Request
    ) -> dict[str, Any]:
        return service.record_ui_ack(
            actor_id=actor_id_from_request(request),
            workspace_id=payload.workspace_id,
            board_id=board_id,
            token=payload.token,
            action=payload.action,
            ok=payload.ok,
        )

    @app.post("/api/boards/{board_id}/socket-ticket")
    async def board_socket_ticket(
        board_id: str, payload: BoardTicketRequest, request: Request
    ) -> dict[str, Any]:
        issued = service.issue_socket_ticket(
            actor_id=actor_id_from_request(request),
            workspace_id=payload.workspace_id,
            board_id=board_id,
            client_instance_id=payload.client_instance_id,
        )
        return {**issued, "socket_url": f"/api/boards/{board_id}/socket", "protocol": BOARD_PROTOCOL}

    @app.websocket("/api/boards/{board_id}/socket")
    async def board_socket(websocket: WebSocket, board_id: str) -> None:
        if websocket.scope.get("query_string") or not _same_origin(
            websocket.headers.get("origin"), websocket.headers.get("host")
        ):
            await websocket.close(code=1008)
            return
        protocols = websocket.scope.get("subprotocols", []) or []
        if BOARD_PROTOCOL not in protocols:
            await websocket.close(code=1002)
            return
        ticket = _subprotocol(websocket.scope, TICKET_PREFIX)
        client_instance_id = _subprotocol(websocket.scope, CLIENT_PREFIX)
        actor_cookie = parse_session(websocket.cookies.get(cookie_name), session_secret)
        if not ticket or not client_instance_id or not actor_cookie:
            await websocket.close(code=1008)
            return
        try:
            scope = service.consume_socket_ticket(
                ticket=ticket,
                board_id=board_id,
                client_instance_id=client_instance_id,
            )
            if scope["actor_id"] != actor_cookie:
                raise StoreError("FORBIDDEN", "Socket ticket actor mismatch")
            # Re-authorize immediately before accepting.
            snapshot = service.snapshot(scope["actor_id"], scope["workspace_id"], board_id)
        except StoreError:
            await websocket.close(code=1008)
            return

        peer = _Peer(
            peer_id=f"{scope['actor_id']}:{client_instance_id}",
            websocket=websocket,
            actor_id=scope["actor_id"],
            workspace_id=scope["workspace_id"],
            board_id=board_id,
            client_instance_id=client_instance_id,
            queue=asyncio.Queue(maxsize=PEER_QUEUE),
        )
        try:
            await hub.add(peer)
        except StoreError:
            await websocket.close(code=1013)
            return

        try:
            await websocket.accept(subprotocol=BOARD_PROTOCOL)
            await websocket.send_text(
                json.dumps(
                    {"type": "snapshot", **snapshot},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            writer = asyncio.create_task(_writer(peer, service))
            try:
                while True:
                    try:
                        raw = await asyncio.wait_for(
                            websocket.receive_text(),
                            timeout=BOARD_AUTH_RECHECK_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        # Quiet sockets still recheck current grants on a bounded cadence.
                        service.snapshot(peer.actor_id, peer.workspace_id, board_id)
                        continue
                    except WebSocketDisconnect:
                        break
                    # Busy clients cannot keep a revoked project grant alive:
                    # every inbound frame is authorized before it is handled.
                    service.snapshot(peer.actor_id, peer.workspace_id, board_id)
                    if len(raw.encode("utf-8")) > MAX_SOCKET_TEXT_BYTES:
                        await websocket.close(code=1009)
                        break
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        await websocket.send_json({"type": "error", "code": "INVALID_JSON"})
                        continue
                    if not isinstance(message, dict):
                        continue
                    kind = message.get("type")
                    if kind == "command":
                        await peer.queue.put(
                            {
                                "type": "error",
                                "code": "BOARD_VOICE_ONLY",
                                "message": "Board mutations are available only through Mira Live tools",
                                "command_id": message.get("command_id"),
                            }
                        )
                    elif kind == "presence":
                        encoded = json.dumps(message, ensure_ascii=False)
                        if len(encoded.encode("utf-8")) > MAX_PRESENCE_TEXT_BYTES:
                            continue
                        payload = {
                            "type": "presence",
                            "actor_id": peer.actor_id,
                            "client_instance_id": peer.client_instance_id,
                            "cursor": message.get("cursor"),
                            "dragging_object_id": message.get("dragging_object_id"),
                        }
                        await hub.publish(board_id, payload, except_peer_id=peer.peer_id)
                    elif kind == "ping":
                        await peer.queue.put({"type": "pong"})
            finally:
                writer.cancel()
                try:
                    await writer
                except (asyncio.CancelledError, Exception):
                    pass
        except StoreError:
            try:
                await websocket.close(code=1008, reason="board access revoked")
            except Exception:
                pass
        finally:
            await hub.remove(peer)

    return hub
