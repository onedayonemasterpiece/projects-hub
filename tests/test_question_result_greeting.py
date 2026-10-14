"""Real durable result -> core greeting context -> explicit presentation receipt."""
from types import SimpleNamespace

import pytest

from test_mcp_events import setup
from test_question_session_results import ready_result
from projects_hub.live_adapter import ProjectsHubLiveAdapter, _startup_functions
from projects_hub.live_resources import ConversationScope
from projects_hub.store import StoreError


async def result_context(setup, *, confirmed=True):
    store, events, service, analysis, repository, ready, payload = await ready_result(setup)
    saved = await events.bindings.record_result_async(service["id"], ready, payload)
    if confirmed:
        claimed = events.bindings.record_result(
            service["id"], ready, dict(kind="slack_claim", response_id=saved["response_id"]),
        )
        events.bindings.record_result(service["id"], ready, dict(
            kind="slack_receipt", response_id=saved["response_id"],
            claim_id=claimed["destinations"]["claim_id"], status="confirmed",
            message_id="1791900099.000002", error="", **claimed["slack"],
        ))
    actor = ready["context_actor_id"]
    project = ready["project_id"]
    workspace = store.db.execute("SELECT workspace_id FROM projects WHERE id=?", (project,)).fetchone()[0]
    conversation = store.create_conversation(actor, workspace, project)
    adapter = ProjectsHubLiveAdapter(
        store, collaboration=analysis.collaboration, collaboration_analysis=analysis, question_events=events,
    )
    def initialize(**options):
        return adapter.initialize(
            resource_id=ConversationScope(workspace, actor, conversation["id"]).resource_binding(),
            actor={"subject": actor, "tenant_id": workspace}, model="gemini-3.8-live",
            conversation_id=conversation["id"], **options,
        )
    return store, events, analysis, ready, adapter, initialize


def results(initialized):
    return [item for item in initialized["context"]["personal_brief"]["personal"]
            if item["kind"] == "question_result_ready"]


@pytest.mark.asyncio
async def test_confirmed_result_is_available_at_greeting_then_acknowledged_after_summary(setup):
    store, events, analysis, ready, adapter, initialize = await result_context(setup)
    try:
        first = initialize()
        assert first["context"]["pending_question_count"] == 0
        items = results(first)
        assert len(items) == 1
        assert items[0]["analysis_id"] == ready["analysis_id"]
        assert items[0]["summary"] == "Decision incorporated"
        assert items[0]["artifact_url"].startswith("https://github.com/")
        assert items[0]["availability"] == "available"
        assert first["context"]["personal_brief"]["announcement_status"] == "not_announced"
        names = {f["name"] for f in first["configuration"]["functions"]}
        assert "collaboration_brief_seen" in names and len(names) <= 9
        assert "важнее подсказки о теме" in first["configuration"]["system_instruction"]
        assert "Другой конкретный запрос пользователя приоритетнее" in first["configuration"]["system_instruction"]
        assert "Подробности предложи отдельно" in first["configuration"]["system_instruction"]
        # Reading/startup is not delivery and never consumes the unread result.
        assert results(initialize()) == items
        assert store.db.execute("SELECT mira_state FROM mcp_response_destinations WHERE request_id=?",
                                (ready["id"],)).fetchone()[0] == "available"
        session = SimpleNamespace(id="result-greeting", state=first["state"], capability="core")
        through = first["context"]["personal_brief"]["personal_through_id"]
        call = {"name": "collaboration_brief_seen", "args": {"personal_through_id": through}}
        with pytest.raises(StoreError):
            await adapter.execute_tool(session, call)
        adapter.on_event(session, {"type": "output_transcript",
                                  "text": "Результат анализа готов: решение учтено. Предыдущие ответы сохранены."})
        assert results(initialize()) == items  # Provider text alone is not acknowledgement.
        prefix_id = first["context"]["personal_brief"]["personal"][0]["id"]
        if prefix_id < through:
            prefix = await adapter.execute_tool(session, {"name": "collaboration_brief_seen",
                "args": {"personal_through_id": prefix_id}})
            assert prefix["personal_cursor"] == prefix_id
            assert results(initialize()) == items
        with pytest.raises(StoreError):
            await adapter.execute_tool(session, {"name": "collaboration_brief_seen",
                "args": {"personal_through_id": through + 100}})
        with pytest.raises(StoreError):
            await adapter.execute_tool(session, {"name": "collaboration_brief_seen",
                "args": {"personal_through_id": through, "general_through_id": 999}})
        ack = await adapter.execute_tool(session, call)
        assert ack["personal_cursor"] == through
        assert ack["announcement_status"] == "announced"
        assert ack["playback_status"] == "unverified"
        assert await adapter.execute_tool(session, call) == ack
        assert results(initialize()) == []
        assert store.db.execute("SELECT mira_state FROM mcp_response_destinations WHERE request_id=?",
                                (ready["id"],)).fetchone()[0] == "available"
    finally:
        await analysis.close()


@pytest.mark.asyncio
async def test_unconfirmed_result_and_recovery_do_not_announce(setup):
    store, events, analysis, ready, adapter, initialize = await result_context(setup, confirmed=False)
    try:
        assert results(initialize()) == []
        recovery = initialize(audio_mode="buffered", recovery_only=True)
        assert recovery["context"]["personal_brief"]["personal"] == []
        assert recovery["configuration"]["functions"] == []
        assert "PERSONAL RESULTS" not in recovery["configuration"]["system_instruction"]
    finally:
        await analysis.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "binding", "questions", "grant", "pending"])
async def test_result_projection_and_ack_recheck_current_authority(setup, change):
    store, events, analysis, ready, adapter, initialize = await result_context(setup)
    try:
        initial = initialize()
        assert results(initial)
        session = SimpleNamespace(id="result-revocation", state=initial["state"], capability="core")
        adapter.on_event(session, {"type": "output_transcript", "text": "Результат анализа готов."})
        if change == "source":
            store.db.execute("UPDATE project_notes SET revision=revision+1 WHERE id=(SELECT note_id FROM collaboration_analyses WHERE id=?)",
                             (ready["analysis_id"],))
        elif change == "binding":
            store.db.execute("UPDATE mcp_discussion_bindings SET revision=revision+1 WHERE id=?", (ready["binding_id"],))
        elif change == "questions":
            store.db.execute("UPDATE mcp_discussion_bindings SET questions_allowed=0 WHERE id=?", (ready["binding_id"],))
        elif change == "pending":
            store.db.execute("UPDATE collaboration_analyses SET package_state='awaiting_delivery' WHERE id=?", (ready["analysis_id"],))
        else:
            store.db.execute("UPDATE project_grants SET revoked_at_ms=1 WHERE actor_id=? AND project_id=?",
                             (ready["context_actor_id"], ready["project_id"]))
        with pytest.raises(StoreError):
            await adapter.execute_tool(session, {"name": "collaboration_brief_seen",
                "args": {"personal_through_id": initial["context"]["personal_brief"]["personal_through_id"]}})
        if change != "grant":
            assert results(initialize()) == []
        else:
            assert adapter._personal_startup_brief(ready["context_actor_id"],
                initial["state"]["workspace_id"])["personal"] == []
    finally:
        await analysis.close()


@pytest.mark.parametrize("owner", [True, False])
@pytest.mark.parametrize("pending", [True, False])
def test_personal_result_ack_fits_core_budget(owner, pending):
    names = {f["name"] for f in _startup_functions(
        owner_development=owner, pending_questions=pending, personal_brief=True,
    )}
    assert len(names) <= 9
    assert "activate_capability" in names
    assert "collaboration_brief_seen" in names
    assert "development_execute_backlog" not in names
    if pending:
        assert {"collaboration_questions_inbox", "collaboration_questions_answer"} <= names
