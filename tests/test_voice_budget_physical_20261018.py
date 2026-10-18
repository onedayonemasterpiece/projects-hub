"""Regression for the physical Android 2026-10-18 briefing-budget incident.

No provider credentials, real recordings, production store or external writes.
The resource guard bills exact UTF-8 setup JSON bytes, not model tokens.
"""
import json
from types import SimpleNamespace

import pytest

from live_interaction.provider import setup_config
from projects_hub.live_adapter import ProjectsHubLiveAdapter, _startup_functions
from projects_hub.live_resources import ConversationScope
from projects_hub.store import DurableStore, StoreError


def _started(tmp_path, *, collaboration):
    store = DurableStore(tmp_path)
    boot = store.ensure_platform_owner("Physical RC budget regression")
    actor = boot["actor"]["id"]
    workspace = boot["workspace"]["id"]
    conversation = store.create_conversation(actor, workspace, boot["projects"][0]["id"])
    adapter = ProjectsHubLiveAdapter(store, collaboration=collaboration)
    initialized = adapter.initialize(
        resource_id=ConversationScope(workspace, actor, conversation["id"]).resource_binding(),
        actor={"subject": actor, "tenant_id": workspace},
        model="gemini-3.8-live", conversation_id=conversation["id"],
        client_version="0.1.62-rc.72",
    )
    return store, adapter, initialized


@pytest.mark.asyncio
async def test_owner_brief_is_direct_without_expensive_second_provider_setup(tmp_path):
    collaboration = SimpleNamespace(personal_brief=lambda **kwargs: {"personal": []})
    store, adapter, initialized = _started(tmp_path, collaboration=collaboration)
    try:
        names = [f["name"] for f in initialized["configuration"]["functions"]]
        assert "activate_capability" in names
        assert "collaboration_personal_brief" in names
        assert "backlog_list" in names
        assert "backlog_create" in names
        assert "development_execute_backlog" not in names
        assert len(names) <= 9
        assert "collaboration_personal_brief НАПРЯМУЮ" in initialized["configuration"]["system_instruction"]
        assert "owner_development" in initialized["context"]["allowed_capabilities"]
        session = SimpleNamespace(state=initialized["state"], capability="core")
        brief = await adapter.execute_tool(session, {"name": "collaboration_personal_brief", "args": {}})
        assert brief["personal_unread_count"] == 0
        with pytest.raises(StoreError, match="Function not in active capability"):
            await adapter.execute_tool(session, {"name": "development_execute_backlog", "args": {}})
        setup = setup_config("gemini-3.8-live", initialized["context"],
                             configuration=initialized["configuration"])
        billed_units = len(json.dumps(setup, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        assert billed_units < 27000, billed_units  # one startup, not startup + 31k reconfigure
    finally:
        store.close()


def test_without_collaboration_existing_owner_direct_tools_are_preserved():
    names = [f["name"] for f in _startup_functions(
        owner_development=True, collaboration_available=False)]
    assert "development_execute_backlog" in names
    assert "collaboration_personal_brief" not in names
    assert len(names) <= 9


def test_brief_and_questions_fit_existing_nine_tool_budget():
    names = [f["name"] for f in _startup_functions(
        owner_development=True, pending_questions=True,
        collaboration_available=True)]
    assert len(names) <= 9
    assert {"collaboration_personal_brief", "collaboration_questions_inbox",
            "collaboration_questions_answer"} <= set(names)
    both = [f["name"] for f in _startup_functions(
        owner_development=True, pending_questions=True, personal_brief=True,
        collaboration_available=True)]
    assert len(both) <= 9
    assert {"collaboration_personal_brief", "collaboration_brief_seen",
            "collaboration_questions_answer", "collaboration_questions_inbox"} <= set(both)
    assert "runtime_versions_get" not in both
