from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from projects_hub.app import create_app
from projects_hub.auth import COOKIE_NAME, issue_session
from projects_hub.board import BoardService
from projects_hub.settings import Settings
from projects_hub.sharing_api import (
    GUEST_CLIENT_PREFIX,
    GUEST_PROTOCOL,
    GUEST_TICKET_PREFIX,
)
from projects_hub.store import DurableStore


def setup(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    boot = store.ensure_dev_workspace("Share owner")
    actor = boot["actor"]["id"]
    workspace = boot["workspace"]["id"]
    project = boot["projects"][0]["id"]
    board = BoardService(store)
    board_id = board.open_board(actor, workspace, project)["id"]
    board.apply_command(
        actor_id=actor,
        workspace_id=workspace,
        board_id=board_id,
        command_id="cmd_share_public_sticky",
        operation="create",
        object_id="obj_share_public",
        expected_object_revision=None,
        payload={
            "type": "sticky",
            "text": "Visible sticky",
            "style": {"color": "green"},
        },
    )
    board.apply_command(
        actor_id=actor,
        workspace_id=workspace,
        board_id=board_id,
        command_id="cmd_share_private_doc",
        operation="create",
        object_id="obj_share_private",
        expected_object_revision=None,
        payload={
            "type": "document_card",
            "text": "Secret analysis title",
            "style": {"color": "violet"},
            "reference": {"kind": "analysis_run", "id": "anr_private_report"},
        },
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        static_dir=tmp_path / "missing-ui",
        session_secret="s" * 64,
        dev_auth=True,
        cookie_secure=False,
    )
    app = create_app(settings, store=store)
    return store, app, settings, actor, workspace, project, board, board_id


def owner_client(app, settings, actor):
    client = TestClient(app, base_url="http://testserver")
    client.cookies.set(COOKIE_NAME, issue_session(actor, settings.session_secret))
    return client


def token_from_share_url(value: str) -> str:
    fragment = urlsplit(value).fragment
    assert fragment.startswith("token=")
    return unquote(fragment.removeprefix("token="))


def test_guest_projection_hides_private_document_and_direct_apis(tmp_path: Path):
    store, app, settings, actor, workspace, project, _board, board_id = setup(tmp_path)
    try:
        with owner_client(app, settings, actor) as owner:
            created = owner.post(
                f"/api/projects/{project}/shares",
                json={"workspace_id": workspace},
            )
            assert created.status_code == 200, created.text
            payload = created.json()
            assert payload["board_id"] == board_id
            assert payload["url"].startswith("/guest/board#token=")
            assert "?" not in payload["url"]
            assert payload["expires_at_ms"] - payload["created_at_ms"] == 7 * 24 * 60 * 60 * 1000
            token = token_from_share_url(payload["url"])

        with TestClient(app, base_url="http://testserver") as guest:
            exchanged = guest.post("/api/guest/exchange", json={"token": token})
            assert exchanged.status_code == 200, exchanged.text
            assert "session_token" not in exchanged.json()
            assert exchanged.headers["cache-control"] == "no-store"
            assert exchanged.headers["x-robots-tag"].startswith("noindex")
            assert exchanged.headers["referrer-policy"] == "no-referrer"

            snapshot = guest.get("/api/guest/board")
            assert snapshot.status_code == 200, snapshot.text
            assert snapshot.headers["cache-control"] == "no-store"
            objects = {item["id"]: item for item in snapshot.json()["objects"]}
            assert objects["obj_share_public"]["text"] == "Visible sticky"
            private = objects["obj_share_private"]
            assert private["text"] == "Закрытый документ"
            assert private["reference"] is None
            assert private["created_by"] == ""
            assert private["updated_by"] == ""

            assert guest.get(
                f"/api/boards/{board_id}/objects/obj_share_public/history",
                params={"workspace_id": workspace},
            ).status_code == 401
            assert guest.post(
                "/api/analysis/runs",
                json={
                    "workspace_id": workspace,
                    "project_id": project,
                    "board_id": board_id,
                    "object_ids": ["obj_share_public"],
                    "command_id": "analysis_guest_denied",
                    "model": "kimi_k3",
                    "purpose": "edge_cases",
                    "question": "Should fail.",
                },
            ).status_code == 401
            assert guest.post(
                f"/api/boards/{board_id}/commands",
                json={
                    "workspace_id": workspace,
                    "command_id": "cmd_guest_edit_denied",
                    "operation": "update",
                    "object_id": "obj_share_public",
                    "expected_object_revision": 1,
                    "payload": {"text": "should not write"},
                },
            ).status_code == 401
    finally:
        store.close()


def test_guest_wss_streams_durable_projection_and_revoke_closes_open_socket(tmp_path: Path):
    store, app, settings, actor, workspace, project, board, board_id = setup(tmp_path)
    try:
        with owner_client(app, settings, actor) as owner:
            share = owner.post(
                f"/api/projects/{project}/shares",
                json={"workspace_id": workspace},
            ).json()
            share_id = share["id"]
            token = token_from_share_url(share["url"])

        with TestClient(app, base_url="http://testserver") as guest:
            assert guest.post("/api/guest/exchange", json={"token": token}).status_code == 200
            ticket = guest.post(
                "/api/guest/board/socket-ticket",
                json={"client_instance_id": "guest-browser-one"},
            )
            assert ticket.status_code == 200, ticket.text
            issued = ticket.json()
            protocols = [
                GUEST_PROTOCOL,
                GUEST_TICKET_PREFIX + issued["ticket"],
                GUEST_CLIENT_PREFIX + "guest-browser-one",
            ]
            with guest.websocket_connect(
                "/api/guest/board/socket",
                subprotocols=protocols,
                headers={"origin": "http://testserver"},
            ) as socket:
                first = socket.receive_json()
                assert first["type"] == "snapshot"
                board.apply_command(
                    actor_id=actor,
                    workspace_id=workspace,
                    board_id=board_id,
                    command_id="cmd_share_stream_update",
                    operation="update",
                    object_id="obj_share_private",
                    expected_object_revision=1,
                    payload={"text": "Even newer secret title"},
                )
                event = socket.receive_json()
                assert event["type"] == "event"
                assert event["event"]["after"]["text"] == "Закрытый документ"
                assert event["event"]["after"]["reference"] is None
                assert "initiating_actor_id" not in event["event"]
                assert "command_id" not in event["event"]

                with owner_client(app, settings, actor) as owner:
                    revoked = owner.post(
                        f"/api/shares/{share_id}/revoke",
                        json={"workspace_id": workspace},
                    )
                    assert revoked.status_code == 200
                with pytest.raises(WebSocketDisconnect):
                    while True:
                        socket.receive_json()

            assert guest.get("/api/guest/board").status_code == 400
    finally:
        store.close()


def test_expired_share_cannot_exchange_or_continue(tmp_path: Path):
    store, app, settings, actor, workspace, project, _board, _board_id = setup(tmp_path)
    try:
        with owner_client(app, settings, actor) as owner:
            share = owner.post(
                f"/api/projects/{project}/shares",
                json={"workspace_id": workspace},
            ).json()
        token = token_from_share_url(share["url"])
        with store._lock:
            store.db.execute(
                "UPDATE board_share_grants SET expires_at_ms=1 WHERE id=?",
                (share["id"],),
            )
        with TestClient(app, base_url="http://testserver") as guest:
            denied = guest.post("/api/guest/exchange", json={"token": token})
            assert denied.status_code == 400
            assert denied.json()["error"]["code"] == "GUEST_TOKEN_INVALID"
    finally:
        store.close()
