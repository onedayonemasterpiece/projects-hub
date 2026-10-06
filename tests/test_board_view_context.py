from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import projects_hub.board_view_context as view_module
from projects_hub.app import create_app
from projects_hub.auth import COOKIE_NAME, issue_session
from projects_hub.board import BoardService
from projects_hub.board_view_context import BoardViewContextStore
from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_resources import ConversationScope
from projects_hub.settings import Settings
from projects_hub.store import DurableStore


def _setup(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    boot = store.ensure_dev_workspace("Board view context")
    actor = boot["actor"]["id"]
    workspace = boot["workspace"]["id"]
    project = boot["projects"][0]["id"]
    conversation = store.create_conversation(actor, workspace, project)
    board = BoardService(store)
    opened = board.open_board(actor, workspace, project)
    board_id = opened["id"]
    first = board.apply_command(
        actor_id=actor,
        workspace_id=workspace,
        board_id=board_id,
        command_id="cmd_view_ctx_first",
        operation="create",
        object_id="obj_view_ctx_first",
        expected_object_revision=None,
        payload={
            "type": "sticky",
            "text": "Canonical first text",
            "style": {"color": "blue"},
            "geometry": {"x": 10, "y": 20, "width": 280, "height": 180, "z": 1},
        },
        execution_origin="mira",
    )
    second = board.apply_command(
        actor_id=actor,
        workspace_id=workspace,
        board_id=board_id,
        command_id="cmd_view_ctx_second",
        operation="create",
        object_id="obj_view_ctx_second",
        expected_object_revision=None,
        payload={
            "type": "sticky",
            "text": "Canonical second text",
            "style": {"color": "green"},
            "geometry": {"x": 800, "y": 900, "width": 300, "height": 200, "z": 2},
        },
        execution_origin="mira",
    )
    view = BoardViewContextStore(store, board)
    return store, boot, actor, workspace, project, conversation, board, board_id, view, first, second


def _live_session(
    store: DurableStore,
    board: BoardService,
    view: BoardViewContextStore,
    *,
    actor: str,
    workspace: str,
    conversation_id: str,
    client_instance_id: str,
):
    adapter = ProjectsHubLiveAdapter(
        store,
        board=board,
        board_view_context=view,
    )
    binding = ConversationScope(workspace, actor, conversation_id).resource_binding()
    initialized = adapter.initialize(
        resource_id=binding,
        actor={"subject": actor, "tenant_id": workspace},
        model="gemini-3.8-live",
        conversation_id=conversation_id,
        client_instance_id=client_instance_id,
    )
    return adapter, SimpleNamespace(state=initialized["state"]), initialized


@pytest.mark.asyncio
async def test_view_context_is_scoped_per_browser_tab_and_resolves_canonical_objects(tmp_path: Path):
    (
        store,
        _boot,
        actor,
        workspace,
        project,
        conversation,
        board,
        board_id,
        view,
        first,
        second,
    ) = _setup(tmp_path)
    try:
        view.update(
            actor_id=actor,
            workspace_id=workspace,
            conversation_id=conversation["id"],
            project_id=project,
            board_id=board_id,
            client_instance_id="client-tab-a",
            board_seq=first["board_seq"],
            camera={"x": 0, "y": 0, "zoom": 1, "width": 600, "height": 500},
            visible_object_ids=["obj_view_ctx_first", "obj_unknown_from_client"],
            selected_object_ids=["obj_view_ctx_first"],
            focused_object_id="obj_view_ctx_first",
        )
        view.update(
            actor_id=actor,
            workspace_id=workspace,
            conversation_id=conversation["id"],
            project_id=project,
            board_id=board_id,
            client_instance_id="client-tab-b",
            board_seq=second["board_seq"],
            camera={"x": -700, "y": -800, "zoom": 1, "width": 600, "height": 500},
            visible_object_ids=["obj_view_ctx_second"],
        )

        adapter_a, session_a, initialized_a = _live_session(
            store,
            board,
            view,
            actor=actor,
            workspace=workspace,
            conversation_id=conversation["id"],
            client_instance_id="client-tab-a",
        )
        adapter_b, session_b, _initialized_b = _live_session(
            store,
            board,
            view,
            actor=actor,
            workspace=workspace,
            conversation_id=conversation["id"],
            client_instance_id="client-tab-b",
        )

        schema = next(
            item for item in initialized_a["configuration"]["functions"]
            if item["name"] == "board_query"
        )
        assert schema["parameters"]["properties"]["action"]["enum"] == [
            "search",
            "view_context",
        ]
        instruction = initialized_a["configuration"]["system_instruction"]
        assert "board_query action=view_context" in instruction
        assert "не угадывай объект" in instruction

        context_a = await adapter_a.execute_tool(
            session_a,
            {"name": "board_query", "args": {"action": "view_context"}},
        )
        context_b = await adapter_b.execute_tool(
            session_b,
            {"name": "board_query", "args": {"action": "view_context"}},
        )

        assert [item["id"] for item in context_a["objects"]] == ["obj_view_ctx_first"]
        assert context_a["objects"][0]["text"] == "Canonical first text"
        assert context_a["objects"][0]["object_revision"] == 1
        assert context_a["objects"][0]["selected"] is True
        assert context_a["objects"][0]["focused"] is True
        assert context_a["stale_object_ids"] == ["obj_unknown_from_client"]

        assert [item["id"] for item in context_b["objects"]] == ["obj_view_ctx_second"]
        assert context_b["objects"][0]["text"] == "Canonical second text"
        assert all(item["id"] != "obj_view_ctx_first" for item in context_b["objects"])
    finally:
        store.close()


@pytest.mark.asyncio
async def test_missing_or_expired_view_context_fails_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (
        store,
        _boot,
        actor,
        workspace,
        project,
        conversation,
        board,
        board_id,
        _view,
        _first,
        _second,
    ) = _setup(tmp_path)
    clock = {"now": 10_000}
    monkeypatch.setattr(view_module, "_now_ms", lambda: clock["now"])
    view = BoardViewContextStore(store, board, ttl_ms=5_000)
    try:
        adapter, session, _initialized = _live_session(
            store,
            board,
            view,
            actor=actor,
            workspace=workspace,
            conversation_id=conversation["id"],
            client_instance_id="client-expiring",
        )
        missing = await adapter.execute_tool(
            session,
            {"name": "board_query", "args": {"action": "view_context"}},
        )
        assert missing["status"] == "unavailable"

        view.update(
            actor_id=actor,
            workspace_id=workspace,
            conversation_id=conversation["id"],
            project_id=project,
            board_id=board_id,
            client_instance_id="client-expiring",
            board_seq=1,
            camera={"x": 0, "y": 0, "zoom": 1, "width": 400, "height": 300},
            visible_object_ids=["obj_view_ctx_first"],
        )
        clock["now"] = 16_000
        expired = await adapter.execute_tool(
            session,
            {"name": "board_query", "args": {"action": "view_context"}},
        )
        assert expired["status"] == "unavailable"
        assert expired["reason"] == "no_current_view_context"
    finally:
        store.close()


def test_view_context_http_rejects_forged_semantic_fields(tmp_path: Path):
    store, _boot, actor, workspace, project, conversation, _board, board_id, _view, _first, _second = _setup(tmp_path)
    settings = Settings(
        data_dir=tmp_path / "data",
        static_dir=tmp_path / "missing-ui",
        session_secret="s" * 64,
        dev_auth=True,
        cookie_secure=False,
    )
    app = create_app(settings, store=store)
    try:
        with TestClient(app, base_url="http://testserver") as client:
            client.cookies.set(COOKIE_NAME, issue_session(actor, settings.session_secret))
            response = client.put(
                f"/api/conversations/{conversation['id']}/board-view-context",
                json={
                    "workspace_id": workspace,
                    "project_id": project,
                    "board_id": board_id,
                    "client_instance_id": "client-forged",
                    "board_seq": 1,
                    "camera": {"x": 0, "y": 0, "zoom": 1, "width": 400, "height": 300},
                    "visible_object_ids": ["obj_view_ctx_first"],
                    "selected_object_ids": [],
                    "focused_object_id": None,
                    "text": "FORGED CLIENT TEXT",
                },
            )
            assert response.status_code == 422
    finally:
        store.close()
