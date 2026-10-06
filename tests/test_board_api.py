from pathlib import Path

from fastapi.testclient import TestClient

from projects_hub.auth import COOKIE_NAME, issue_session
from projects_hub.app import create_app
from projects_hub.board_api import BOARD_PROTOCOL, CLIENT_PREFIX, TICKET_PREFIX
from projects_hub.settings import Settings
from projects_hub.store import DurableStore


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        static_dir=tmp_path / "missing-ui",
        session_secret="board-api-secret-" * 4,
        dev_auth=True,
        cookie_secure=False,
    )


def test_board_http_and_wss_collaboration(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    settings = _settings(tmp_path)
    app = create_app(settings, store=store)
    try:
        boot = store.ensure_dev_workspace("Board API")
        actor = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        project = boot["projects"][0]["id"]
        with TestClient(app, base_url="http://testserver") as client:
            client.cookies.set(COOKIE_NAME, issue_session(actor, settings.session_secret))
            opened = client.post(
                f"/api/projects/{project}/board/open",
                json={"workspace_id": workspace},
            )
            assert opened.status_code == 200, opened.text
            board_id = opened.json()["id"]

            def ticket(client_id: str):
                response = client.post(
                    f"/api/boards/{board_id}/socket-ticket",
                    json={"workspace_id": workspace, "client_instance_id": client_id},
                )
                assert response.status_code == 200, response.text
                value = response.json()
                return [
                    BOARD_PROTOCOL,
                    TICKET_PREFIX + value["ticket"],
                    CLIENT_PREFIX + client_id,
                ]

            with client.websocket_connect(
                f"/api/boards/{board_id}/socket",
                subprotocols=ticket("client-one-0001"),
                headers={"origin": "http://testserver"},
            ) as first:
                snapshot = first.receive_json()
                assert snapshot["type"] == "snapshot"
                assert snapshot["board"]["seq"] == 0

                with client.websocket_connect(
                    f"/api/boards/{board_id}/socket",
                    subprotocols=ticket("client-two-0002"),
                    headers={"origin": "http://testserver"},
                ) as second:
                    assert second.receive_json()["type"] == "snapshot"
                    second.send_json(
                        {
                            "type": "command",
                            "command_id": "cmd_socket_001",
                            "operation": "create",
                            "object_id": "obj_socket1",
                            "expected_object_revision": None,
                            "payload": {"text": "Browser must not write"},
                        }
                    )
                    denied = second.receive_json()
                    assert denied["type"] == "error"
                    assert denied["code"] == "BOARD_VOICE_ONLY"
                    assert denied["command_id"] == "cmd_socket_001"

            denied_http = client.post(
                f"/api/boards/{board_id}/commands",
                json={
                    "workspace_id": workspace,
                    "command_id": "cmd_http_denied",
                    "operation": "create",
                    "object_id": "obj_http_denied",
                    "expected_object_revision": None,
                    "payload": {"text": "Browser must not write"},
                },
            )
            assert denied_http.status_code == 400
            assert denied_http.json()["error"]["code"] == "BOARD_VOICE_ONLY"

            snapshot = client.get(
                f"/api/boards/{board_id}/snapshot",
                params={"workspace_id": workspace},
            )
            assert snapshot.status_code == 200
            assert snapshot.json()["board"]["seq"] == 0
            assert snapshot.json()["objects"] == []
    finally:
        store.close()


def test_board_socket_rejects_query_ticket_and_foreign_origin(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    settings = _settings(tmp_path)
    app = create_app(settings, store=store)
    try:
        boot = store.ensure_dev_workspace("Board security")
        actor = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        project = boot["projects"][0]["id"]
        with TestClient(app, base_url="http://testserver") as client:
            client.cookies.set(COOKIE_NAME, issue_session(actor, settings.session_secret))
            board_id = client.post(
                f"/api/projects/{project}/board/open",
                json={"workspace_id": workspace},
            ).json()["id"]
            value = client.post(
                f"/api/boards/{board_id}/socket-ticket",
                json={"workspace_id": workspace, "client_instance_id": "client-security-1"},
            ).json()
            protocols = [
                BOARD_PROTOCOL,
                TICKET_PREFIX + value["ticket"],
                CLIENT_PREFIX + "client-security-1",
            ]
            import pytest
            with pytest.raises(Exception):
                with client.websocket_connect(
                    f"/api/boards/{board_id}/socket?ticket={value['ticket']}",
                    subprotocols=protocols,
                    headers={"origin": "http://testserver"},
                ):
                    pass
            # The first rejected connection must not consume the ticket.
            with pytest.raises(Exception):
                with client.websocket_connect(
                    f"/api/boards/{board_id}/socket",
                    subprotocols=protocols,
                    headers={"origin": "https://evil.example"},
                ):
                    pass
            with client.websocket_connect(
                f"/api/boards/{board_id}/socket",
                subprotocols=protocols,
                headers={"origin": "http://testserver"},
            ) as socket:
                assert socket.receive_json()["type"] == "snapshot"
    finally:
        store.close()
