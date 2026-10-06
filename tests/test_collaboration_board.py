from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from projects_hub.app import create_app
from projects_hub.auth import COOKIE_NAME, issue_session
from projects_hub.board import BoardService
from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_resources import ConversationScope
from projects_hub.settings import Settings
from projects_hub.store import DurableStore, StoreError


def setup_board(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    boot = store.ensure_platform_owner("Board owner")
    actor = boot["actor"]["id"]
    workspace = boot["workspace"]["id"]
    project = boot["projects"][0]["id"]
    board = BoardService(store)
    return store, boot, actor, workspace, project, board


def test_one_canonical_board_revision_and_acl(tmp_path: Path):
    store, boot, actor, workspace, project, board = setup_board(tmp_path)
    try:
        first = board.open_board(actor, workspace, project)
        second = board.open_board(actor, workspace, project)
        assert first["id"] == second["id"]

        created = board.apply_command(
            actor_id=actor,
            workspace_id=workspace,
            board_id=first["id"],
            command_id="cmd.board.create.001",
            operation="create",
            object_id="obj_board_001",
            expected_object_revision=None,
            payload={
                "type": "sticky",
                "text": "Одна доска на проект",
                "style": {"color": "yellow"},
                "geometry": {
                    "x": 20, "y": 40, "width": 280, "height": 190, "z": 1,
                },
            },
            execution_origin="mira",
        )
        assert created["status"] == "saved"
        assert created["event"]["execution_origin"] == "mira"

        retried = board.apply_command(
            actor_id=actor,
            workspace_id=workspace,
            board_id=first["id"],
            command_id="cmd.board.create.001",
            operation="create",
            object_id="obj_board_001",
            expected_object_revision=None,
            payload={
                "type": "sticky",
                "text": "Одна доска на проект",
                "style": {"color": "yellow"},
                "geometry": {
                    "x": 20, "y": 40, "width": 280, "height": 190, "z": 1,
                },
            },
            execution_origin="mira",
        )
        assert retried == created

        moved = board.apply_command(
            actor_id=actor,
            workspace_id=workspace,
            board_id=first["id"],
            command_id="cmd.board.move.001",
            operation="move",
            object_id="obj_board_001",
            expected_object_revision=1,
            payload={"geometry": {"x": 80, "y": 100}},
            execution_origin="mira",
        )
        assert moved["object_revision"] == 2
        with pytest.raises(StoreError) as exc:
            board.apply_command(
                actor_id=actor,
                workspace_id=workspace,
                board_id=first["id"],
                command_id="cmd.board.stale.001",
                operation="update",
                object_id="obj_board_001",
                expected_object_revision=1,
                payload={"text": "stale"},
                execution_origin="mira",
            )
        assert exc.value.code == "OBJECT_CONFLICT"

        other = "usr_board_viewer"
        with store._lock:
            store.db.execute(
                "INSERT INTO actors(id,display_name,created_at_ms) VALUES(?,?,?)",
                (other, "Viewer", 1),
            )
            store.db.execute(
                "INSERT INTO memberships(actor_id,workspace_id,role) VALUES(?,?,?)",
                (other, workspace, "member"),
            )
        store.grant_project_access(
            actor_id=actor,
            workspace_id=workspace,
            project_id=project,
            target_actor_id=other,
            role="viewer",
        )
        assert board.snapshot(other, workspace, first["id"])["board"]["id"] == first["id"]
        with pytest.raises(StoreError):
            board.apply_command(
                actor_id=other,
                workspace_id=workspace,
                board_id=first["id"],
                command_id="cmd.board.viewer.001",
                operation="create",
                object_id="obj_denied_001",
                expected_object_revision=None,
                payload={"text": "denied"},
                execution_origin="mira",
            )
    finally:
        store.close()


def test_browser_board_mutation_endpoint_is_voice_only(tmp_path: Path):
    store, _boot, actor, workspace, project, _board = setup_board(tmp_path)
    settings = Settings(
        data_dir=tmp_path / "data",
        static_dir=tmp_path / "missing-ui",
        session_secret="board-current-secret-" * 4,
        dev_auth=True,
        cookie_secure=False,
    )
    app = create_app(settings, store=store)
    try:
        with TestClient(app, base_url="http://testserver") as client:
            client.cookies.set(COOKIE_NAME, issue_session(actor, settings.session_secret))
            opened = client.post(
                f"/api/projects/{project}/board/open",
                json={"workspace_id": workspace},
            )
            assert opened.status_code == 200
            board_id = opened.json()["id"]
            denied = client.post(
                f"/api/boards/{board_id}/commands",
                json={
                    "workspace_id": workspace,
                    "command_id": "cmd.board.http.001",
                    "operation": "create",
                    "object_id": "obj_http_001",
                    "expected_object_revision": None,
                    "payload": {"text": "browser edit"},
                },
            )
            assert denied.status_code == 400
            assert denied.json()["error"]["code"] == "BOARD_VOICE_ONLY"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_mira_board_tools_share_same_live_surface_and_viewport_context(tmp_path: Path):
    store, boot, actor, workspace, project, board = setup_board(tmp_path)
    try:
        conversation = store.create_conversation(actor, workspace, project)
        binding = ConversationScope(workspace, actor, conversation["id"]).resource_binding()
        adapter = ProjectsHubLiveAdapter(store, board=board)
        initialized = adapter.initialize(
            resource_id=binding,
            actor={"subject": actor, "tenant_id": workspace},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
            client_instance_id="client-board-tab-001",
        )
        session = SimpleNamespace(state=initialized["state"])
        names = {item["name"] for item in initialized["configuration"]["functions"]}
        assert {"board_navigate", "board_query", "board_edit", "board_history"} <= names

        opened = await adapter.execute_tool(
            session,
            {"name": "board_navigate", "args": {"action": "open"}},
        )
        created = await adapter.execute_tool(
            session,
            {
                "name": "board_edit",
                "args": {
                    "operation": "create",
                    "payload": {
                        "type": "sticky",
                        "text": "Голосовой стикер",
                        "style": {"color": "pink"},
                        "geometry": {
                            "x": 100, "y": 120, "width": 300, "height": 200, "z": 1,
                        },
                    },
                },
            },
        )
        assert created["event"]["execution_origin"] == "mira"
        focused = await adapter.execute_tool(
            session,
            {
                "name": "board_navigate",
                "args": {"action": "focus", "object_id": created["object_id"]},
            },
        )
        assert focused["ui_command"]["bbox"]["x"] == 100.0

        adapter.board_view_context.update(
            actor_id=actor,
            workspace_id=workspace,
            conversation_id=conversation["id"],
            project_id=project,
            board_id=opened["board_id"],
            client_instance_id="client-board-tab-001",
            board_seq=created["board_seq"],
            camera={"x": 0, "y": 0, "zoom": 1, "width": 800, "height": 600},
            visible_object_ids=[created["object_id"]],
            selected_object_ids=[],
            focused_object_id=created["object_id"],
        )
        resolved = await adapter.execute_tool(
            session,
            {"name": "board_query", "args": {"action": "view_context"}},
        )
        assert resolved["status"] == "current"
        assert resolved["objects"][0]["id"] == created["object_id"]
        assert resolved["objects"][0]["text"] == "Голосовой стикер"
    finally:
        store.close()
