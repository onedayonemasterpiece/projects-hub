from pathlib import Path
from types import SimpleNamespace

import pytest

from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_resources import ConversationScope
from projects_hub.store import DurableStore, StoreError


class FakeBoardHub:
    def __init__(self):
        self.messages = []

    async def publish(self, board_id, payload, **_kwargs):
        self.messages.append((board_id, payload))


def _session(store: DurableStore, hub: FakeBoardHub):
    boot = store.ensure_dev_workspace("Board Live")
    actor_id = boot["actor"]["id"]
    workspace_id = boot["workspace"]["id"]
    project_id = boot["projects"][0]["id"]
    conversation = store.create_conversation(actor_id, workspace_id, project_id)
    binding = ConversationScope(workspace_id, actor_id, conversation["id"]).resource_binding()
    adapter = ProjectsHubLiveAdapter(store, board_hub=hub)
    initialized = adapter.initialize(
        resource_id=binding,
        actor={"subject": actor_id, "tenant_id": workspace_id},
        model="gemini-3.8-live",
        conversation_id=conversation["id"],
    )
    return (
        boot,
        adapter,
        SimpleNamespace(state=initialized["state"]),
        initialized,
    )


@pytest.mark.asyncio
async def test_board_tools_are_same_mira_surface_and_broadcast_saved_edits(tmp_path: Path):
    store = DurableStore(tmp_path)
    hub = FakeBoardHub()
    try:
        boot, adapter, session, initialized = _session(store, hub)
        project_id = boot["projects"][0]["id"]
        function_names = {
            item["name"] for item in initialized["configuration"]["functions"]
        }
        assert {
            "board_navigate",
            "board_query",
            "board_edit",
            "board_history",
        } <= function_names
        instruction = initialized["configuration"]["system_instruction"]
        assert "# BOARD" in instruction
        assert "та же самая Live-сессия Миры" in instruction
        assert "не создаёт второй ASR/LLM" in instruction

        opened = await adapter.execute_tool(
            session,
            {"name": "board_navigate", "args": {"action": "open"}},
        )
        assert opened["project_id"] == project_id
        assert opened["ui_command"]["action"] == "open"
        board_id = opened["board_id"]

        created = await adapter.execute_tool(
            session,
            {
                "name": "board_edit",
                "args": {
                    "operation": "create",
                    "payload": {
                        "type": "sticky",
                        "text": "Голосовой стикер про бюджет",
                        "style": {"color": "pink"},
                        "geometry": {
                            "x": 80,
                            "y": 120,
                            "width": 300,
                            "height": 210,
                            "z": 1,
                        },
                    },
                },
            },
        )
        assert created["status"] == "saved"
        assert created["event"]["execution_origin"] == "mira"
        assert hub.messages[-1][0] == board_id
        assert hub.messages[-1][1]["event"]["object_id"] == created["object_id"]

        found = await adapter.execute_tool(
            session,
            {
                "name": "board_query",
                "args": {"query": "бюджет"},
            },
        )
        assert found["items"][0]["object_id"] == created["object_id"]

        focused = await adapter.execute_tool(
            session,
            {
                "name": "board_navigate",
                "args": {
                    "action": "focus",
                    "object_id": created["object_id"],
                },
            },
        )
        assert focused["ui_ack_required"] is True
        assert focused["ui_command"]["action"] == "focus"
        assert focused["ui_command"]["bbox"]["x"] == 80.0

        history = await adapter.execute_tool(
            session,
            {
                "name": "board_history",
                "args": {"object_id": created["object_id"]},
            },
        )
        assert history["items"][0]["initiating_actor_id"] == boot["actor"]["id"]

        closed = await adapter.execute_tool(
            session,
            {"name": "board_navigate", "args": {"action": "close"}},
        )
        assert closed["ui_command"]["action"] == "close"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_board_live_edit_does_not_blindly_overwrite_stale_revision(tmp_path: Path):
    store = DurableStore(tmp_path)
    hub = FakeBoardHub()
    try:
        _boot, adapter, session, _initialized = _session(store, hub)
        await adapter.execute_tool(
            session,
            {"name": "board_navigate", "args": {"action": "open"}},
        )
        created = await adapter.execute_tool(
            session,
            {
                "name": "board_edit",
                "args": {
                    "operation": "create",
                    "object_id": "obj_live_conflict",
                    "payload": {"text": "Первая версия"},
                },
            },
        )
        changed = await adapter.execute_tool(
            session,
            {
                "name": "board_edit",
                "args": {
                    "operation": "update",
                    "object_id": created["object_id"],
                    "expected_object_revision": 1,
                    "payload": {"text": "Вторая версия"},
                },
            },
        )
        assert changed["object_revision"] == 2

        with pytest.raises(StoreError) as exc:
            await adapter.execute_tool(
                session,
                {
                    "name": "board_edit",
                    "args": {
                        "operation": "update",
                        "object_id": created["object_id"],
                        "expected_object_revision": 1,
                        "payload": {"text": "Устаревшая версия"},
                    },
                },
            )
        assert exc.value.code == "OBJECT_CONFLICT"
    finally:
        store.close()
