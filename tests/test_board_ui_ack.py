from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projects_hub.auth import COOKIE_NAME, issue_session
from projects_hub.app import create_app
from projects_hub.board import BoardService
from projects_hub.settings import Settings
from projects_hub.store import DurableStore, StoreError


def test_ui_ack_is_actor_board_bound_and_idempotent(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Board UI ACK")
        actor = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        project = boot["projects"][0]["id"]
        service = BoardService(store)
        board_id = service.open_board(actor, workspace, project)["id"]
        token = "uif_" + "a" * 24

        first = service.record_ui_ack(
            actor_id=actor,
            workspace_id=workspace,
            board_id=board_id,
            token=token,
            action="focus",
            ok=True,
        )
        again = service.record_ui_ack(
            actor_id=actor,
            workspace_id=workspace,
            board_id=board_id,
            token=token,
            action="focus",
            ok=True,
        )
        assert first == again
        assert first["ok"] == 1

        with pytest.raises(StoreError) as exc:
            service.record_ui_ack(
                actor_id=actor,
                workspace_id=workspace,
                board_id=board_id,
                token=token,
                action="focus",
                ok=False,
            )
        assert exc.value.code == "UI_ACK_CONFLICT"
    finally:
        store.close()


def test_ui_ack_http_requires_authenticated_project_access(tmp_path: Path):
    data_dir = tmp_path / "data"
    store = DurableStore(data_dir)
    settings = Settings(
        data_dir=data_dir,
        static_dir=tmp_path / "missing-ui",
        session_secret="board-ui-ack-secret-" * 4,
        dev_auth=True,
        cookie_secure=False,
    )
    app = create_app(settings, store=store)
    try:
        boot = store.ensure_dev_workspace("Board UI HTTP")
        actor = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        project = boot["projects"][0]["id"]
        with TestClient(app, base_url="http://testserver") as client:
            client.cookies.set(COOKIE_NAME, issue_session(actor, settings.session_secret))
            board_id = client.post(
                f"/api/projects/{project}/board/open",
                json={"workspace_id": workspace},
            ).json()["id"]
            response = client.post(
                f"/api/boards/{board_id}/ui-ack",
                json={
                    "workspace_id": workspace,
                    "token": "uif_" + "b" * 24,
                    "action": "view_all",
                    "ok": True,
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["action"] == "view_all"
            assert response.json()["ok"] == 1
    finally:
        store.close()
