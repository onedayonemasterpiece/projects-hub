from live_tools import execute, bundle_setup
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projects_hub.app import create_app
from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_resources import ConversationScope
from projects_hub.settings import Settings
from projects_hub.store import DurableStore, StoreError


CLIENT_SOURCE = "local_" + "a" * 32


def setup_store(tmp_path: Path):
    store = DurableStore(tmp_path)
    boot = store.ensure_dev_workspace("Offline")
    actor = boot["actor"]["id"]
    workspace = boot["workspace"]["id"]
    project = next(item["id"] for item in boot["projects"] if item["name"] == "Projects Hub")
    conversation = store.create_conversation(actor, workspace, project)
    return store, boot, actor, workspace, project, conversation


def test_client_source_identity_reuses_one_source_and_resets_incomplete_replay(tmp_path: Path):
    store, _boot, actor, _workspace, _project, conversation = setup_store(tmp_path)
    try:
        first = store.create_source(actor, conversation["id"], CLIENT_SOURCE)
        assert first["_reused"] is False
        store.append_audio(actor, first["id"], b"first attempt")
        store.append_source_event(actor, first["id"], "input_transcript", "partial")
        assert store.get_source(actor, first["id"])["transcript_revision"] == 1

        second = store.create_source(actor, conversation["id"], CLIENT_SOURCE)
        assert second["_reused"] is True
        assert second["id"] == first["id"]

        reset = store.reset_source_for_replay(actor, first["id"])
        assert reset["id"] == first["id"]
        assert reset["audio_bytes"] == 0
        assert reset["audio_chunks"] == 0
        assert reset["transcript"] == ""
        assert reset["transcript_revision"] == 0
        assert reset["status"] == "capturing"
        assert (tmp_path / reset["audio_path"]).read_bytes() == b""
        assert store.source_events(actor, first["id"]) == []
    finally:
        store.close()


def test_client_source_cannot_move_between_conversations(tmp_path: Path):
    store, _boot, actor, workspace, _project, conversation = setup_store(tmp_path)
    try:
        store.create_source(actor, conversation["id"], CLIENT_SOURCE)
        other = store.create_conversation(actor, workspace)
        with pytest.raises(StoreError) as error:
            store.create_source(actor, other["id"], CLIENT_SOURCE)
        assert error.value.code == "CLIENT_SOURCE_CONFLICT"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_buffered_adapter_reuses_local_source_but_never_resets_terminal_memory(tmp_path: Path):
    store, _boot, actor, workspace, project, conversation = setup_store(tmp_path)
    try:
        binding = ConversationScope(workspace, actor, conversation["id"]).resource_binding()
        adapter = ProjectsHubLiveAdapter(store)
        initialized = adapter.initialize(
            resource_id=binding,
            actor={"subject": actor, "tenant_id": workspace},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
            audio_mode="buffered",
            client_source_id=CLIENT_SOURCE,
        )
        source_id = initialized["response"]["source_id"]
        assert initialized["response"]["source_reused"] is False
        assert initialized["configuration"]["manual_activity_detection"] is True
        assert "BUFFERED SOURCE DISPOSITION" in initialized["configuration"]["system_instruction"]

        store.append_audio(actor, source_id, b"audio")
        store.append_source_event(actor, source_id, "input_transcript", "remember this")
        result = store.commit_memory(
            actor_id=actor,
            source_id=source_id,
            command_id="cmd_terminal",
            project_id=project,
            title="Offline source",
            kind="note",
            semantic_notes="saved",
            args_sha256="a" * 64,
        )
        assert result["status"] == "archived"

        again = adapter.initialize(
            resource_id=binding,
            actor={"subject": actor, "tenant_id": workspace},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
            audio_mode="buffered",
            client_source_id=CLIENT_SOURCE,
        )
        assert again["response"]["source_id"] == source_id
        assert again["response"]["source_reused"] is True
        assert again["response"]["source_terminal"] is True
        assert again["response"]["source_status"] == "archived"
        assert store.get_source(actor, source_id)["transcript"] == "remember this"
        assert len(store.list_memories(actor, workspace, project)) == 1
    finally:
        store.close()


def test_api_reconciles_client_source_without_exposing_transcript(tmp_path: Path):
    store, _boot, actor, _workspace, _project, conversation = setup_store(tmp_path / "data")
    source = store.create_source(actor, conversation["id"], CLIENT_SOURCE)
    settings = Settings(
        data_dir=tmp_path / "data",
        static_dir=tmp_path / "missing",
        session_secret="s" * 48,
        dev_auth=True,
        cookie_secure=False,
    )
    app = create_app(settings, store=store, live_host=None)
    with TestClient(app, base_url="http://localhost") as client:
        login = client.post("/api/dev/login", json={"display_name": "Offline"})
        assert login.status_code == 200
        response = client.get(
            f"/api/conversations/{conversation['id']}/sources/by-client/{CLIENT_SOURCE}"
        )
        assert response.status_code == 200
        item = response.json()["source"]
        assert item["id"] == source["id"]
        assert item["client_source_id"] == CLIENT_SOURCE
        assert "transcript" not in item
        assert "audio_path" not in item
    store.close()
