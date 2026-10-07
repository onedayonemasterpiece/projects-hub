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
        session = SimpleNamespace(state=initialized["state"])
        names = {
            item["name"] for item in initialized["configuration"]["functions"]
        }
        assert "collaboration_brief_seen" in names
        assert "collaboration_general_news_set" in names

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
