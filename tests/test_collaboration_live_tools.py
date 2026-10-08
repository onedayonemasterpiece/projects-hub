from __future__ import annotations

from types import SimpleNamespace

import pytest

from projects_hub.collaboration import CollaborationService
from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_resources import ConversationScope
from projects_hub.store import DurableStore


class NoopGitHub:
    pass


class NoopNoteProcessor:
    async def close(self):
        return None


@pytest.mark.asyncio
async def test_mira_dispatches_brief_seen_and_general_news_tools(tmp_path):
    store = DurableStore(tmp_path / "data")
    boot = store.ensure_platform_owner("Owner")
    actor = boot["actor"]["id"]
    workspace = boot["workspace"]["id"]
    project = boot["projects"][0]["id"]
    collaboration = CollaborationService(
        store,
        NoopGitHub(),  # type: ignore[arg-type]
        note_processor=NoopNoteProcessor(),  # type: ignore[arg-type]
    )
    try:
        conversation = store.create_conversation(actor, workspace, project)
        adapter = ProjectsHubLiveAdapter(store, collaboration=collaboration)
        initialized = adapter.initialize(
            resource_id=ConversationScope(
                workspace, actor, conversation["id"]
            ).resource_binding(),
            actor={"subject": actor, "tenant_id": workspace},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
        )
        session = SimpleNamespace(state=initialized["state"], capability="collaboration")
        names = {
            item["name"] for item in initialized["configuration"]["functions"]
        }
        assert "activate_capability" in names
        assert "collaboration" in initialized["state"]["allowed_capabilities"]
        assert {"collaboration_brief_seen", "collaboration_general_news_set"} <= set(
            __import__("projects_hub.live_capabilities", fromlist=["BUNDLES"]).BUNDLES["collaboration"]
        )

        disabled = await adapter.execute_tool(
            session,
            {
                "name": "collaboration_general_news_set",
                "args": {"enabled": False},
            },
        )
        assert disabled["general_news_enabled"] is False

        seen = await adapter.execute_tool(
            session,
            {
                "name": "collaboration_brief_seen",
                "args": {
                    "personal_through_id": 0,
                    "general_through_id": 0,
                },
            },
        )
        assert seen["personal_cursor"] == 0
        assert seen["general_cursor"] == 0
        assert seen["general_news_enabled"] is False
    finally:
        await collaboration.close()
        store.close()


@pytest.mark.asyncio
async def test_mira_explicit_collaboration_view_is_read_only_and_questions_remain_voice_driven(tmp_path):
    from projects_hub.live_capabilities import BUNDLES
    from projects_hub.store import StoreError

    store = DurableStore(tmp_path / "data")
    boot = store.ensure_platform_owner("Owner")
    actor, workspace = boot["actor"]["id"], boot["workspace"]["id"]
    project = boot["projects"][0]["id"]
    collaboration = CollaborationService(
        store, NoopGitHub(), note_processor=NoopNoteProcessor(),  # type: ignore[arg-type]
    )
    try:
        conversation = store.create_conversation(actor, workspace, project)
        adapter = ProjectsHubLiveAdapter(store, collaboration=collaboration)
        initialized = adapter.initialize(
            resource_id=ConversationScope(workspace, actor, conversation["id"]).resource_binding(),
            actor={"subject": actor, "tenant_id": workspace},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
        )
        instruction = initialized["configuration"]["system_instruction"]
        assert "collaboration_questions_inbox" in instruction
        assert "задай голосом" in instruction
        assert "unknown" in instruction and "skip" in instruction and "later" in instruction
        assert "collaboration_view" in BUNDLES["collaboration"]
        assert "collaboration_view" in BUNDLES["notes"]
        session = SimpleNamespace(state=initialized["state"], capability="collaboration")
        before = collaboration.timeline(actor_id=actor, workspace_id=workspace)

        opened = await adapter.execute_tool(
            session, {"name": "collaboration_view", "args": {"view": "questions"}},
        )
        assert opened == {
            "ui_command": {"kind": "collaboration", "action": "show", "view": "questions"}
        }
        closed = await adapter.execute_tool(
            session, {"name": "collaboration_view", "args": {"view": "close"}},
        )
        assert closed == {"ui_command": {"kind": "collaboration", "action": "close"}}
        assert collaboration.timeline(actor_id=actor, workspace_id=workspace) == before
        with pytest.raises(StoreError) as missing_id:
            await adapter.execute_tool(
                session, {"name": "collaboration_view", "args": {"view": "note"}},
            )
        assert missing_id.value.code == "INVALID_ARGUMENT"
        # No forged note ID may cause an open-card receipt.
        with pytest.raises(StoreError):
            await adapter.execute_tool(
                session, {"name": "collaboration_view",
                          "args": {"view": "note", "note_id": "note_does_not_exist"}},
            )
    finally:
        await collaboration.close()
        store.close()
