"""Startup-to-question flow with durable test data; not physical voice acceptance."""
import base64
from types import SimpleNamespace

import pytest

from test_question_session_controls import package
from projects_hub.live_adapter import ProjectsHubLiveAdapter, _startup_functions
from projects_hub.live_resources import ConversationScope
from projects_hub.store import StoreError


@pytest.mark.asyncio
async def test_pending_questions_are_executable_in_core_through_explicit_completion(tmp_path):
    store, service, events, bridge, owner, binding, note, analysis, args = await package(tmp_path)
    actor, workspace = args["actor_id"], args["workspace_id"]
    try:
        conversation = store.create_conversation(actor, workspace, binding["project_id"])
        adapter = ProjectsHubLiveAdapter(store, collaboration=service.collaboration,
                                         collaboration_analysis=service, question_events=events)
        def initialize():
            return adapter.initialize(resource_id=ConversationScope(workspace, actor, conversation["id"]).resource_binding(),
                                      actor={"subject": actor, "tenant_id": workspace},
                                      model="gemini-3.8-live", conversation_id=conversation["id"])
        initialized = initialize()
        names = {f["name"] for f in initialized["configuration"]["functions"]}
        assert {"activate_capability", "collaboration_questions_inbox", "collaboration_questions_answer"} <= names
        assert len(names) <= 9
        assert initialized["context"]["pending_question_count"] == 2
        assert "session_action=accept" in initialized["configuration"]["system_instruction"]
        assert "не придумывай системное ограничение" in initialized["configuration"]["system_instruction"]
        session = SimpleNamespace(state=initialized["state"], capability="core")
        inbox = await adapter.execute_tool(session, {"name": "collaboration_questions_inbox", "args": {}})
        assert len(inbox["questions"]) == 2
        assert store.db.execute("SELECT COUNT(*) FROM collaboration_answer_commands").fetchone()[0] == 0
        adapter.input(session, {"activity_start": True})
        adapter.input(session, {"audio_base64": base64.b64encode(b"\x01\x00" * 320).decode()})
        adapter.input(session, {"activity_end": True})
        async def control(action, **flags):
            return await adapter.execute_tool(session, {"id": "control-" + action, "name": "collaboration_questions_answer",
                "args": {"analysis_id": analysis["id"], "session_action": action, **flags}})
        await control("accept")
        await control("important_only", important_only=True)
        await control("pause")
        restarted = initialize()
        assert restarted["context"]["pending_question_count"] == 0
        assert {"collaboration_questions_inbox", "collaboration_questions_answer"} <= {
            f["name"] for f in restarted["configuration"]["functions"]}
        resumed = await control("resume")
        assert resumed["important_only"] is True
        question = next(q for q in analysis["questions"] if q["blocking"])
        answer = await adapter.execute_tool(session, {"id": "answer-first", "name": "collaboration_questions_answer",
            "args": {"analysis_id": analysis["id"], "responses": [
                {"question_id": question["id"], "disposition": "answer", "body": "Fixture decision"}]}})
        assert store.db.execute("SELECT COUNT(*) FROM mcp_test_requests").fetchone()[0] == 0
        await control("complete", allow_partial=True)
        assert store.db.execute("SELECT COUNT(*) FROM mcp_test_requests").fetchone()[0] == 1
        store.db.execute("UPDATE mcp_discussion_bindings SET questions_allowed=0 WHERE id=?", (binding["id"],))
        with pytest.raises(StoreError):
            await adapter.execute_tool(session, {"name": "collaboration_questions_inbox",
                "args": {"analysis_id": analysis["id"]}})
        assert not bridge.consults
    finally:
        await service.close()
        store.close()


def test_question_startup_preserves_budget_and_owner_router():
    ordinary = {f["name"] for f in _startup_functions(owner_development=True)}
    question = {f["name"] for f in _startup_functions(owner_development=True, pending_questions=True)}
    assert len(ordinary) <= 9 and len(question) <= 9
    assert "activate_capability" in question
    assert "development_execute_backlog" in ordinary
    assert "development_execute_backlog" not in question
    assert "collaboration_questions_answer" in question
