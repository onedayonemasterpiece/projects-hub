from pathlib import Path

import pytest
from pydantic import ValidationError

from projects_hub.app import LiveStart
from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_resources import ConversationScope
from projects_hub.store import DurableStore


def test_live_start_audio_mode_is_bounded():
    assert LiveStart().audio_mode == "realtime"
    assert LiveStart(audio_mode="buffered").audio_mode == "buffered"
    with pytest.raises(ValidationError):
        LiveStart(audio_mode="other")


def test_buffered_mode_uses_shared_manual_activity_contract(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Buffered")
        actor_id = boot["actor"]["id"]
        workspace_id = boot["workspace"]["id"]
        project_id = next(p["id"] for p in boot["projects"] if p["name"] == "Projects Hub")
        conversation = store.create_conversation(actor_id, workspace_id, project_id)
        binding = ConversationScope(
            workspace_id,
            actor_id,
            conversation["id"],
        ).resource_binding()
        adapter = ProjectsHubLiveAdapter(store)
        initialized = adapter.initialize(
            resource_id=binding,
            actor={"subject": actor_id, "tenant_id": workspace_id},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
            audio_mode="buffered",
        )
        assert initialized["state"]["audio_mode"] == "buffered"
        assert initialized["response"]["audio_mode"] == "buffered"
        assert initialized["configuration"]["manual_activity_detection"] is True

        realtime = adapter.initialize(
            resource_id=binding,
            actor={"subject": actor_id, "tenant_id": workspace_id},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
        )
        assert realtime["configuration"]["manual_activity_detection"] is True
        assert realtime["configuration"]["automatic_activity_detection"] is None
    finally:
        store.close()
