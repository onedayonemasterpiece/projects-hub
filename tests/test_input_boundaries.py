from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from projects_hub.app import LiveInput
from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_resources import ConversationScope
from projects_hub.store import DurableStore, StoreError


def test_http_model_rejects_oversized_audio_before_adapter():
    with pytest.raises(ValidationError):
        LiveInput(audio_base64="a" * 16001)


def test_adapter_rejects_oversized_audio_before_durable_write(tmp_path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Bound")
        actor = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        conversation = store.create_conversation(actor, workspace)
        binding = ConversationScope(workspace, actor, conversation["id"]).resource_binding()
        adapter = ProjectsHubLiveAdapter(store)
        initialized = adapter.initialize(
            resource_id=binding,
            actor={"subject": actor, "tenant_id": workspace},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
        )
        session = SimpleNamespace(state=initialized["state"])
        source_id = initialized["response"]["source_id"]
        with pytest.raises(StoreError) as error:
            adapter.input(session, {"audio_base64": "a" * 16001})
        assert error.value.code == "INVALID_ARGUMENT"
        assert store.get_source(actor, source_id)["audio_bytes"] == 0
    finally:
        store.close()
