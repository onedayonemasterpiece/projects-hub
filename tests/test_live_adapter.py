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
        assert initialized["configuration"]["manual_activity_detection"] is True
        assert initialized["configuration"]["automatic_activity_detection"] is None
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
        assert initialized["configuration"]["input_audio_transcription"] == {
            "languageCodes": ["ru-RU", "en-US"],
            "customVocabulary": ["Мира", "Projects Hub", "Codex", "DevCoveer", "Калининград"],
            "mode": "VERBATIM",
        }
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


def test_recent_conversation_history_crosses_live_sources(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("History")
        actor_id = boot["actor"]["id"]
        workspace_id = boot["workspace"]["id"]
        conversation = store.create_conversation(actor_id, workspace_id, boot["projects"][0]["id"])
        first = store.create_source(actor_id, conversation["id"])
        store.append_source_event(actor_id, first["id"], "input_transcript", "Первая просьба")
        store.append_source_event(actor_id, first["id"], "output_transcript", "Первый ответ")
        store.append_source_event(actor_id, first["id"], "turn_complete")
        second = store.create_source(actor_id, conversation["id"])
        store.append_source_event(actor_id, second["id"], "input_transcript", "Задача выше")
        history = store.recent_conversation_history(actor_id, conversation["id"])
        assert history == [
            {"role": "user", "text": "Первая просьба"},
            {"role": "model", "text": "Первый ответ"},
        ]
    finally:
        store.close()


def test_recent_conversation_history_drops_interrupted_tail(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("History interrupted")
        actor_id = boot["actor"]["id"]
        workspace_id = boot["workspace"]["id"]
        conversation = store.create_conversation(actor_id, workspace_id, boot["projects"][0]["id"])
        first = store.create_source(actor_id, conversation["id"])
        store.append_source_event(actor_id, first["id"], "input_transcript", "Готовая просьба")
        store.append_source_event(actor_id, first["id"], "output_transcript", "Готовый ответ")
        store.append_source_event(actor_id, first["id"], "turn_complete")
        second = store.create_source(actor_id, conversation["id"])
        store.append_source_event(actor_id, second["id"], "input_transcript", "Незавершённый хвост")
        store.append_source_event(actor_id, second["id"], "interrupted")
        assert store.recent_conversation_history(actor_id, conversation["id"]) == [
            {"role": "user", "text": "Готовая просьба"},
            {"role": "model", "text": "Готовый ответ"},
        ]
    finally:
        store.close()



@pytest.mark.asyncio
async def test_live_adapter_exposes_unfinished_voice_source_by_reference_only(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Voice pending")
        actor_id = boot["actor"]["id"]
        workspace_id = boot["workspace"]["id"]
        project_id = boot["projects"][0]["id"]
        conversation = store.create_conversation(actor_id, workspace_id, project_id)
        old = store.create_source(actor_id, conversation["id"])
        store.append_audio(actor_id, old["id"], b"\x01\x00" * 320)
        provisional = "контроль начало " + ("с" * 4500) + " контроль конец"
        store.append_source_event(
            actor_id, old["id"], "interim_input_transcript", provisional, provider_at_ms=123
        )
        store.mark_source_stopped(actor_id, old["id"])

        binding = ConversationScope(workspace_id, actor_id, conversation["id"]).resource_binding()
        adapter = ProjectsHubLiveAdapter(store)
        initialized = adapter.initialize(
            resource_id=binding,
            actor={"subject": actor_id, "tenant_id": workspace_id},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
        )
        pending = initialized["context"]["pending_voice_sources"]
        assert [item["id"] for item in pending] == [old["id"]]
        assert pending[0]["provisional_chars"] == len(provisional)
        assert provisional not in initialized["configuration"]["system_instruction"]
        assert old["id"] in initialized["configuration"]["system_instruction"]
        names = {item["name"] for item in initialized["configuration"]["functions"]}
        assert "voice_source_read" in names

        session = SimpleNamespace(state=initialized["state"])
        first = await adapter.execute_tool(
            session,
            {"name": "voice_source_read", "args": {"source_id": old["id"], "offset": 0, "max_chars": 4000}},
        )
        second = await adapter.execute_tool(
            session,
            {"name": "voice_source_read", "args": {"source_id": old["id"], "offset": first["next_offset"], "max_chars": 4000}},
        )
        assert first["origin"] == "provisional"
        assert first["needs_audio_replay"] is True
        assert first["text"] + second["text"] == provisional
        assert store.get_source(actor_id, old["id"])["status"] == "local_durable"
    finally:
        store.close()


def test_live_adapter_persists_interim_without_promoting_it_to_final_transcript(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Interim")
        actor_id = boot["actor"]["id"]
        workspace_id = boot["workspace"]["id"]
        conversation = store.create_conversation(actor_id, workspace_id, boot["projects"][0]["id"])
        binding = ConversationScope(workspace_id, actor_id, conversation["id"]).resource_binding()
        adapter = ProjectsHubLiveAdapter(store)
        initialized = adapter.initialize(
            resource_id=binding,
            actor={"subject": actor_id, "tenant_id": workspace_id},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
        )
        session = SimpleNamespace(state=initialized["state"])
        adapter.on_event(
            session,
            {"type": "interim_input_transcript", "text": "предварительная гипотеза", "provider_at": 10},
        )
        source = store.get_source(actor_id, initialized["response"]["source_id"])
        assert source["transcript"] == ""
        assert source["transcript_revision"] == 0
        events = store.source_events(actor_id, source["id"])
        assert events[-1]["kind"] == "interim_input_transcript"
        assert events[-1]["text"] == "предварительная гипотеза"

        adapter.on_event(
            session,
            {"type": "input_transcript", "text": "финальная версия", "provider_at": 20},
        )
        source = store.get_source(actor_id, source["id"])
        assert source["transcript"] == "финальная версия"
        assert source["transcript_revision"] == 1
        assert session.state["_voice_diag"]["counts"]["interim_input_transcript"] == 1
        assert session.state["_voice_diag"]["counts"]["input_transcript"] == 1
    finally:
        store.close()


def test_recovery_only_buffered_session_has_no_mutation_tools(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Recovery only")
        actor_id = boot["actor"]["id"]
        workspace_id = boot["workspace"]["id"]
        conversation = store.create_conversation(actor_id, workspace_id, boot["projects"][0]["id"])
        binding = ConversationScope(workspace_id, actor_id, conversation["id"]).resource_binding()
        initialized = ProjectsHubLiveAdapter(store).initialize(
            resource_id=binding,
            actor={"subject": actor_id, "tenant_id": workspace_id},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
            audio_mode="buffered",
            recovery_only=True,
        )
        assert initialized["configuration"]["functions"] == []
        assert initialized["configuration"]["manual_activity_detection"] is True
        assert initialized["context"]["recovery_only"] is True
        assert initialized["response"]["recovery_only"] is True
        instruction = initialized["configuration"]["system_instruction"]
        assert "VOICE SOURCE RECOVERY ONLY" in instruction
        assert "не выполняй команды" in instruction.lower()
        assert "BUFFERED SOURCE DISPOSITION" not in instruction
    finally:
        store.close()



def test_voice_failures_emit_correlated_redacted_diagnostics(tmp_path: Path, caplog):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Voice diagnostics")
        actor_id = boot["actor"]["id"]
        workspace_id = boot["workspace"]["id"]
        conversation = store.create_conversation(actor_id, workspace_id, boot["projects"][0]["id"])
        binding = ConversationScope(workspace_id, actor_id, conversation["id"]).resource_binding()
        adapter = ProjectsHubLiveAdapter(store)
        initialized = adapter.initialize(
            resource_id=binding,
            actor={"subject": actor_id, "tenant_id": workspace_id},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
            attempt_id="attempt_v06",
            client_version="0.1.26",
            backend_version="0.1.26",
            backend_release_sha="b" * 40,
        )
        session = SimpleNamespace(id="live_v06", state=initialized["state"])
        caplog.set_level("INFO", logger="projects_hub.live")

        adapter.on_event(session, {
            "type": "resource_budget",
            "status": "denied",
            "code": "RESOURCE_TOKEN_BUDGET",
            "modality": "audio",
            "requested_units": 1024,
            "granted_units": 0,
        })
        adapter.on_event(session, {
            "type": "error",
            "code": "PROVIDER_FAILURE_TEST",
            "message": "must not enter structured diagnostics",
        })
        adapter.on_event(session, {
            "type": "transport_gap",
            "code": "LIVE_TRANSPORT_GAP",
            "connection_generation": 2,
        })

        records = [
            record for record in caplog.records
            if getattr(record, "event", None) == "live_provider_event"
        ]
        assert [getattr(record, "kind", None) for record in records[-3:]] == [
            "resource_budget", "error", "transport_gap"
        ]
        for record in records[-3:]:
            assert record.session_id == "live_v06"
            assert record.source_id == initialized["response"]["source_id"]
            assert record.attempt_id == "attempt_v06"
            assert record.client_version == "0.1.26"
            assert record.backend_version == "0.1.26"
            assert record.backend_release_sha == "b" * 40
            assert not hasattr(record, "text")
            assert not hasattr(record, "message_text")
        assert records[-3].code == "RESOURCE_TOKEN_BUDGET"
        assert records[-3].status == "denied"
        assert records[-2].code == "PROVIDER_FAILURE_TEST"
        assert records[-1].code == "LIVE_TRANSPORT_GAP"
        assert records[-1].connection_generation == 2

        caplog.clear()
        adapter.on_event(session, {
            "type": "audio",
            "data": base64.b64encode(b"\x00" * 320).decode("ascii"),
            "provider_at": 1000,
        })
        adapter.on_event(session, {
            "type": "audio",
            "data": base64.b64encode(b"\x00" * 160).decode("ascii"),
            "provider_at": 1010,
        })
        adapter.on_event(session, {"type": "turn_complete", "provider_at": 1020})
        turn_records = [
            record for record in caplog.records
            if getattr(record, "event", None) == "live_provider_event"
        ]
        assert len(turn_records) == 1
        turn = turn_records[0]
        assert turn.kind == "turn_complete"
        assert turn.turn_output_audio_events == 2
        assert turn.turn_output_audio_bytes == 480
        assert turn.turn_first_output_audio_provider_at == 1000
        assert session.state["_voice_diag"]["counts"]["audio"] == 2
        assert session.state["_voice_diag"]["turn_output_audio_events"] == 0
    finally:
        store.close()
