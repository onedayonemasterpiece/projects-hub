import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_resources import ConversationScope
from projects_hub.store import DurableStore


@pytest.mark.asyncio
async def test_live_adapter_persists_audio_transcript_and_verified_memory(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Live")
        actor_id = boot["actor"]["id"]
        workspace_id = boot["workspace"]["id"]
        project_id = boot["projects"][0]["id"]
        conversation = store.create_conversation(actor_id, workspace_id, project_id)
        binding = ConversationScope(workspace_id, actor_id, conversation["id"]).resource_binding()
        adapter = ProjectsHubLiveAdapter(store)
        initialized = adapter.initialize(
            resource_id=binding,
            actor={"subject": actor_id, "tenant_id": workspace_id},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
        )
        assert initialized["configuration"]["functions"]
        assert initialized["response"]["focus_project_id"] == project_id

        session = SimpleNamespace(state=initialized["state"])
        pcm = b"\x01\x00" * 320
        adapter.input(session, {"audio_base64": base64.b64encode(pcm).decode("ascii")})
        source = store.get_source(actor_id, initialized["response"]["source_id"])
        assert source["audio_bytes"] == len(pcm)

        full = "важный источник " + ("д" * 5000)
        adapter.on_event(
            session,
            {"type": "input_transcript", "text": full, "provider_at": 123},
        )
        assert store.get_source(actor_id, source["id"])["transcript"] == full

        result = await adapter.execute_tool(
            session,
            {
                "name": "memory_commit_voice_source",
                "id": "provider-call-1",
                "args": {
                    "project_id": project_id,
                    "title": "Важное решение",
                    "kind": "decision",
                    "semantic_notes": "Нужно сохранить.",
                },
            },
        )
        assert result["status"] == "archived"
        assert result["transcript_revision"] == 1

        repeated = await adapter.execute_tool(
            session,
            {
                "name": "memory_commit_voice_source",
                "id": "provider-call-after-reconnect",
                "args": {
                    "project_id": project_id,
                    "title": "Важное решение",
                    "kind": "decision",
                    "semantic_notes": "Нужно сохранить.",
                },
            },
        )
        assert repeated == result
        assert len(store.list_memories(actor_id, workspace_id, project_id)) == 1
    finally:
        store.close()


def test_system_instruction_has_runtime_scoped_capability_tour(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Capabilities")
        actor_id = boot["actor"]["id"]
        workspace_id = boot["workspace"]["id"]
        project_id = boot["projects"][0]["id"]
        conversation = store.create_conversation(actor_id, workspace_id, project_id)
        binding = ConversationScope(workspace_id, actor_id, conversation["id"]).resource_binding()
        initialized = ProjectsHubLiveAdapter(store).initialize(
            resource_id=binding,
            actor={"subject": actor_id, "tenant_id": workspace_id},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
        )
        instruction = initialized["configuration"]["system_instruction"]
        assert "# CAPABILITY TOUR" in instruction
        assert "configuration.functions" in instruction
        assert "что ты умеешь" in instruction
        assert "не подключённые capabilities" in instruction
    finally:
        store.close()


@pytest.mark.asyncio
async def test_runtime_versions_tool_reports_exact_session_versions(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Versions")
        actor_id = boot["actor"]["id"]
        workspace_id = boot["workspace"]["id"]
        project_id = boot["projects"][0]["id"]
        conversation = store.create_conversation(actor_id, workspace_id, project_id)
        binding = ConversationScope(workspace_id, actor_id, conversation["id"]).resource_binding()
        adapter = ProjectsHubLiveAdapter(store)
        initialized = adapter.initialize(
            resource_id=binding,
            actor={"subject": actor_id, "tenant_id": workspace_id},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
            client_version="0.1.18",
            client_timezone="Europe/Kaliningrad",
            backend_version="0.1.19",
            backend_release_sha="a" * 40,
        )
        names = {item["name"] for item in initialized["configuration"]["functions"]}
        assert "runtime_versions_get" in names
        result = await adapter.execute_tool(
            SimpleNamespace(state=initialized["state"]),
            {"name": "runtime_versions_get", "args": {}},
        )
        assert result == {
            "client_kind": "android",
            "android_version": "0.1.18",
            "backend_version": "0.1.19",
            "backend_release_sha": "a" * 40,
        }
    finally:
        store.close()


@pytest.mark.asyncio
async def test_calendar_rejects_offset_that_contradicts_client_timezone(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Timezone")
        actor_id = boot["actor"]["id"]
        workspace_id = boot["workspace"]["id"]
        project_id = boot["projects"][0]["id"]
        conversation = store.create_conversation(actor_id, workspace_id, project_id)
        binding = ConversationScope(workspace_id, actor_id, conversation["id"]).resource_binding()
        initialized = ProjectsHubLiveAdapter(store).initialize(
            resource_id=binding,
            actor={"subject": actor_id, "tenant_id": workspace_id},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
            client_timezone="Europe/Kaliningrad",
        )
        assert initialized["context"]["client_timezone"] == "Europe/Kaliningrad"
        assert initialized["configuration"]["input_audio_transcription"]["languageCodes"] == ["ru-RU"]
        session = SimpleNamespace(state=initialized["state"])
        with pytest.raises(Exception, match="offset does not match client timezone"):
            await ProjectsHubLiveAdapter(store).execute_tool(
                session,
                {
                    "name": "calendar_create_event_on_device",
                    "args": {
                        "title": "Электричка",
                        "starts_at": "2026-10-04T13:45:00+00:00",
                        "ends_at": "2026-10-04T14:45:00+00:00",
                        "timezone": "Europe/Kaliningrad",
                    },
                },
            )
    finally:
        store.close()
