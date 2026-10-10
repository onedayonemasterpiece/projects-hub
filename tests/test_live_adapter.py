from live_tools import execute, bundle_setup
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_resources import ConversationScope
from projects_hub.store import DurableStore, StoreError


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

        result = await execute(adapter,
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

        repeated = await execute(adapter,
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



def test_initial_live_setup_has_bounded_authoritative_budget_and_core_routing(tmp_path: Path):
    import json
    from live_interaction.provider import setup_config
    from projects_hub.live_adapter import _live_instruction, _functions
    from projects_hub.live_capabilities import BUNDLES, OVERLAYS, ROUTER, PREFERENCES

    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Live budget")
        actor = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        conversation = store.create_conversation(actor, workspace, boot["projects"][0]["id"])
        adapter = ProjectsHubLiveAdapter(store)
        initialized = adapter.initialize(
            resource_id=ConversationScope(workspace, actor, conversation["id"]).resource_binding(),
            actor={"subject": actor, "tenant_id": workspace},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
        )
        startup = initialized["configuration"]["system_instruction"]
        assert "collaboration_questions_inbox" in startup
        assert "задай голосом" in startup
        assert all(word in startup for word in ("answer", "unknown", "skip", "later"))
        assert "# BOARD" not in startup
        assert "# BACKLOG AND OWNER DEVELOPMENT" not in startup
        setup = setup_config(
            "gemini-3.8-live",
            initialized["context"],
            configuration=initialized["configuration"],
        )
        # ai-resource-control counts setup JSON UTF-8 bytes, not provider tokens.
        estimated_units = len(json.dumps(setup, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        assert estimated_units < 21000, estimated_units

        domain_checks = {
            "board": "# BOARD",
            "collaboration": "# PROJECT COLLABORATION",
            "notes": "# PROJECT COLLABORATION",
            "memory": "# MEMORY",
            "repositories": "# SECURITY",
            "calendar": "# SECURITY",
            "readiness": "# EVENT READINESS",
            "knowledge": "# REGIONAL KNOWLEDGE",
            "expert_reviews": "# EXPERT REVIEWS",
            "owner_development": "# BACKLOG AND OWNER DEVELOPMENT",
            "preferences": "# ROUTER AND PERSONAL THEME",
        }
        for capability, section in domain_checks.items():
            instruction = _live_instruction(capability)
            assert section in instruction
            assert "# ROLE" in instruction
            assert "# CAPABILITY TOUR" in instruction
            if capability != "board":
                assert "# BOARD" not in instruction
            if capability != "owner_development":
                assert "# BACKLOG AND OWNER DEVELOPMENT" not in instruction
            declarations = [
                *(_functions(
                    expert_reviews=True, regional_knowledge=True, owner_development=True
                )),
                *PREFERENCES,
            ]
            function_bundle = [ROUTER, *(
                item for item in declarations if item["name"] in BUNDLES[capability]
            )]
            switched = {
                **initialized["configuration"],
                "functions": function_bundle,
                "system_instruction": instruction + "\n" + OVERLAYS[capability],
            }
            switched_setup = setup_config(
                "gemini-3.8-live",
                initialized["context"],
                configuration=switched,
            )
            switched_units = len(json.dumps(
                switched_setup, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8"))
            assert switched_units < 25000, (capability, switched_units)
        assert "не снимай blocker" in _live_instruction("core")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_owner_core_startup_exposes_theme_and_backlog_without_router(tmp_path: Path):
    from projects_hub.live_adapter import _startup_functions
    from projects_hub.live_capabilities import BUNDLES, ROUTER
    from projects_hub.store import StoreError
    from types import SimpleNamespace

    store = DurableStore(tmp_path)
    try:
        owner = store.ensure_platform_owner("Platform owner")
        actor_id, workspace_id = owner["actor"]["id"], owner["workspace"]["id"]
        conversation = store.create_conversation(
            actor_id, workspace_id, owner["projects"][0]["id"],
        )
        adapter = ProjectsHubLiveAdapter(store)
        init = adapter.initialize(
            resource_id=ConversationScope(
                workspace_id, actor_id, conversation["id"],
            ).resource_binding(),
            actor={"subject": actor_id, "tenant_id": workspace_id},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
        )
        names = {item["name"] for item in init["configuration"]["functions"]}
        assert len(init["configuration"]["functions"]) == 9
        assert {"preferences_get", "preferences_set_theme",
                "backlog_list", "backlog_create",
                "development_execute_backlog"} <= names
        assert set(BUNDLES["core"]) <= names
        assert "activate_capability" in names
        assert "development_execution_status" not in names
        assert init["response"]["owner_development_enabled"] is True
        session = SimpleNamespace(state=init["state"], capability="core")
        assert (await adapter.execute_tool(
            session, {"name": "preferences_get", "args": {}},
        ))["theme"] in {"light", "dark"}
        overview = await adapter.execute_tool(
            session, {"name": "backlog_list", "args": {}},
        )
        assert isinstance(overview.get("tasks"), list)
        assert "latest_execution" in overview

        # No privilege escalation through the session's exposed function names.
        dev = store.ensure_dev_workspace("Non owner")
        other = dev["actor"]["id"]
        other_workspace = dev["workspace"]["id"]
        other_conversation = store.create_conversation(
            other, other_workspace, dev["projects"][0]["id"],
        )
        other_init = adapter.initialize(
            resource_id=ConversationScope(
                other_workspace, other, other_conversation["id"],
            ).resource_binding(),
            actor={"subject": other, "tenant_id": other_workspace},
            model="gemini-3.8-live",
            conversation_id=other_conversation["id"],
        )
        other_names = {
            item["name"] for item in other_init["configuration"]["functions"]
        }
        assert {"preferences_get", "preferences_set_theme"} <= other_names
        assert not ({"backlog_list", "backlog_create",
                     "development_execute_backlog"} & other_names)
        other_session = SimpleNamespace(
            state=other_init["state"], capability="core",
        )
        with pytest.raises(StoreError, match="Function not in active capability"):
            await adapter.execute_tool(
                other_session, {"name": "backlog_list", "args": {}},
            )
    finally:
        store.close()


def test_voice_first_router_advertises_authorized_owner_development_and_theme(tmp_path: Path):
    from projects_hub.live_adapter import _live_instruction
    from projects_hub.live_capabilities import BUNDLES, ROUTER

    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_platform_owner("Owner")
        actor = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        conversation = store.create_conversation(
            actor, workspace, boot["projects"][0]["id"],
        )
        adapter = ProjectsHubLiveAdapter(store)
        state = adapter.initialize(
            resource_id=ConversationScope(
                workspace, actor, conversation["id"],
            ).resource_binding(),
            actor={"subject": actor, "tenant_id": workspace},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
        )
        core = state["configuration"]
        allowed = state["context"]["allowed_capabilities"]
        active_tools = {tool["name"] for tool in core["functions"]}
        assert "owner_development" in allowed
        assert "preferences" in allowed
        assert "activate_capability" in active_tools
        assert "development_execute_backlog" in active_tools
        assert "preferences_set_theme" in active_tools
        # A missing tool in the *active* bundle must not cause a false refusal.
        assert "owner_development" in core["system_instruction"]
        assert "development_execute_backlog" in core["system_instruction"]
        assert "preferences_set_theme" in core["system_instruction"]
        assert "не могу" in core["system_instruction"]
        assert "activate_capability" in ROUTER["description"]
        assert "owner_development" in ROUTER["description"]
        assert "development_execute_backlog" in BUNDLES["owner_development"]
        assert "backlog_list" in BUNDLES["owner_development"]
        assert "BACKLOG AND OWNER DEVELOPMENT" in _live_instruction("owner_development")
        assert "BACKLOG AND OWNER DEVELOPMENT" not in _live_instruction("core")
    finally:
        store.close()


def test_live_tool_declarations_exclude_google_unsupported_json_schema_keywords():
    from projects_hub.live_adapter import _functions
    from projects_hub.live_capabilities import PREFERENCES, ROUTER

    declarations = [
        ROUTER, *PREFERENCES,
        *_functions(
            expert_reviews=True,
            regional_knowledge=True,
            owner_development=True,
        ),
    ]
    forbidden = {"additionalProperties", "uniqueItems", "patternProperties",
                 "oneOf", "const", "$ref", "$schema"}
    def check(value):
        if isinstance(value, dict):
            assert not (set(value) & forbidden), value
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    for declaration in declarations:
        check(declaration["parameters"])


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
        result = await execute(adapter,
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
        transcription = initialized["configuration"]["input_audio_transcription"]
        assert transcription["languageCodes"] == ["ru-RU", "en-US"]
        assert transcription["mode"] == "VERBATIM"
        assert transcription["customVocabulary"][:5] == [
            "Мира",
            "Projects Hub",
            "Codex",
            "DevCoveer",
            "Калининград",
        ]
        assert len(transcription["customVocabulary"]) <= 100
        assert initialized["state"]["caption_vocabulary"] == transcription["customVocabulary"]
        session = SimpleNamespace(state=initialized["state"])
        with pytest.raises(Exception, match="offset does not match client timezone"):
            await execute(ProjectsHubLiveAdapter(store),
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
        selected, _ = bundle_setup(adapter, initialized, "memory")
        names = {item["name"] for item in selected["functions"]}
        assert "voice_source_read" in names

        session = SimpleNamespace(state=initialized["state"])
        first = await execute(adapter,
            session,
            {"name": "voice_source_read", "args": {"source_id": old["id"], "offset": 0, "max_chars": 4000}},
        )
        second = await execute(adapter,
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
            "type": "input_timing",
            "audio_chunks": 7,
            "max_stdin_delay_ms": 23,
            "max_ws_send_ms": 11,
            "audio_stream_end_sent_at": 990,
        })
        timing_records = [
            record for record in caplog.records
            if getattr(record, "event", None) == "live_provider_event"
        ]
        assert len(timing_records) == 1
        timing = timing_records[0]
        assert timing.kind == "input_timing"
        assert timing.audio_chunks == 7
        assert timing.max_stdin_delay_ms == 23
        assert timing.max_ws_send_ms == 11
        assert timing.audio_stream_end_sent_at == 990

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

@pytest.mark.asyncio
async def test_owner_codex_launch_uses_accepted_final_voice_not_function_args(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_platform_owner("Owner")
        actor, workspace = boot["actor"]["id"], boot["workspace"]["id"]
        project = boot["projects"][0]["id"]
        conv = store.create_conversation(actor, workspace, project)
        adapter = ProjectsHubLiveAdapter(store)
        init = adapter.initialize(
            resource_id=ConversationScope(workspace, actor, conv["id"]).resource_binding(),
            actor={"subject": actor, "tenant_id": workspace},
            model="gemini-3.8-live",
            conversation_id=conv["id"],
        )
        calls = []
        async def fake_start(**kwargs):
            calls.append(dict(kwargs))
            return {"execution": {"id": "devrun_test", "phase": "designing"}}
        adapter.development.start = fake_start
        session = SimpleNamespace(id="live-owner", state=init["state"], capability="core")
        def accepted_turn(text):
            adapter.input(session, {"activity_start": True})
            adapter.input(session, {"audio_base64": base64.b64encode(bytes([1, 0]) * 320).decode()})
            adapter.input(session, {"activity_end": True})
            adapter.on_event(session, {"type": "input_transcript", "text": text})

        # Background words and a model-selected tool are NOT authorization.
        accepted_turn("Мира, поставь задачу, запуск пока не разрешаю.")
        with pytest.raises(StoreError) as absent:
            await adapter.execute_tool(session, {
                "name": "development_execute_backlog",
                "args": {"task_ids": ["tsk_test"], "codex_user_opt_in": "Use Codex now"},
            })
        assert absent.value.code == "CODEX_USER_OPT_IN_REQUIRED"
        assert calls == []
        # A new real accepted spoken command does authorize this exact batch.
        accepted_turn("Мира, запусти через Codex эту разработку.")
        result = await adapter.execute_tool(session, {
            "name": "development_execute_backlog",
            "args": {"task_ids": ["tsk_test"]},
        })
        assert result["execution"]["phase"] == "designing"
        assert calls[0]["codex_user_opt_in"] == "Мира, запусти через Codex эту разработку."
        assert calls[0]["codex_opt_in_source_id"] == init["state"]["source_id"]
        assert calls[0]["task_ids"] == ["tsk_test"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_mira_backlog_and_execution_results_are_bounded_with_long_history(tmp_path: Path):
    """Regression for production: 30 historic review stages caused a 59 KB tool reply."""
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_platform_owner("Owner")
        actor, workspace = boot["actor"]["id"], boot["workspace"]["id"]
        project = boot["projects"][0]["id"]
        conversation = store.create_conversation(actor, workspace, project)
        adapter = ProjectsHubLiveAdapter(store)
        initialized = adapter.initialize(
            resource_id=ConversationScope(workspace, actor, conversation["id"]).resource_binding(),
            actor={"subject": actor, "tenant_id": workspace},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
        )
        session = SimpleNamespace(id="history-acceptance", state=initialized["state"], capability="core")
        stages = [
            {"stage": "review", "status": "completed", "model": "gpt-6-astra",
             "cycle": i, "review_verdict": "rework_required",
             "summary": "Развёрнутый исторический отчёт. " * 90}
            for i in range(30)
        ]
        full = {
            "id": "devrun_old", "status": "completed", "phase": "ready",
            "model_profile": "gpt-6.1-sol:medium",
            "task_ids": ["tsk_theme"], "project_id": project,
            "phase_detail": "Предыдущая разработка завершена",
            "stages": stages,
            "token_usage_by_model": {"gpt-6-astra": {"totalTokens": 60000}},
        }
        tasks = [
            {"id": f"tsk_{index}", "project_id": project,
             "title": f"Доработка плавающего острова {index}",
             "description": "Условия реализации " * 220,
             "state": "proposed"}
            for index in range(5)
        ]
        old_overview = {
            "tasks": tasks, "latest_execution": full,
            "backlog_state_semantics": "Backlog state accepted means approved/eligible.",
        }
        assert len(json.dumps(old_overview, ensure_ascii=False).encode()) > 50000
        adapter.development.backlog_overview = lambda **_kwargs: old_overview

        answer = await adapter.execute_tool(
            session, {"name": "backlog_list", "args": {}},
        )
        assert len(json.dumps(answer, ensure_ascii=False).encode()) < 4000
        assert [t["id"] for t in answer["tasks"]] == [t["id"] for t in tasks]
        assert all(len(t["description"]) <= 240 for t in answer["tasks"])
        assert answer["latest_execution"]["id"] == "devrun_old"
        assert answer["latest_execution"]["stage_count"] == 30
        assert len(answer["latest_execution"]["recent_stages"]) == 2
        assert "stages" not in answer["latest_execution"]
        assert len(old_overview["latest_execution"]["stages"]) == 30

        async def fake_status(**_kwargs):
            return {"execution": full}
        session.capability = "owner_development"
        adapter.development.status = fake_status
        readback = await adapter.execute_tool(
            session, {"name": "development_execution_status", "args": {}},
        )
        assert readback["execution"]["id"] == "devrun_old"
        assert len(json.dumps(readback, ensure_ascii=False).encode()) < 2500

        async def fake_start(**_kwargs):
            return {"execution": full}
        adapter.development.start = fake_start
        session.capability = "core"
        adapter.input(session, {"activity_start": True})
        adapter.input(session, {
            "audio_base64": base64.b64encode(bytes([1, 0]) * 480).decode(),
        })
        adapter.input(session, {"activity_end": True})
        adapter.on_event(session, {
            "type": "input_transcript",
            "text": "Мира, запусти через Codex существующую разработку.",
        })
        launched = await adapter.execute_tool(
            session, {"name": "development_execute_backlog",
                      "args": {"task_ids": ["tsk_island"]}},
        )
        assert launched["execution"]["id"] == "devrun_old"
        assert len(json.dumps(launched, ensure_ascii=False).encode()) < 2500
    finally:
        store.close()
