"""Live tool routing contracts, not physical microphone acceptance."""
from types import SimpleNamespace
import pytest
from projects_hub.collaboration import CollaborationService
from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_resources import ConversationScope
from projects_hub.store import DurableStore


@pytest.mark.asyncio
async def test_live_session_control_preserves_unspecified_filter_and_reads_result(tmp_path):
    store = DurableStore(tmp_path / "data")
    boot = store.ensure_platform_owner("Owner")
    actor, workspace = boot["actor"]["id"], boot["workspace"]["id"]
    project = boot["projects"][0]["id"]
    collaboration = CollaborationService(store, None)
    calls = []
    def control(**kwargs):
        calls.append(kwargs)
        return {"saved": True}
    analysis = SimpleNamespace(inbox=lambda **kw: [], control_question_session=control,
                               get_analysis=lambda **kw: {"result_markdown": "Verified result", **kw})
    adapter = ProjectsHubLiveAdapter(store, collaboration=collaboration,
                                     collaboration_analysis=analysis)
    conversation = store.create_conversation(actor, workspace, project)
    initialized = adapter.initialize(
        resource_id=ConversationScope(workspace, actor, conversation["id"]).resource_binding(),
        actor={"subject": actor, "tenant_id": workspace}, model="gemini-3.8-live",
        conversation_id=conversation["id"],
    )
    session = SimpleNamespace(state=initialized["state"], capability="collaboration")
    try:
        await adapter.execute_tool(session, {"name": "collaboration_questions_answer",
            "args": {"analysis_id": "analysis-one", "session_action": "resume"}})
        assert calls[-1]["important_only"] is None
        assert calls[-1]["actor_id"] == actor and calls[-1]["workspace_id"] == workspace
        await adapter.execute_tool(session, {"name": "collaboration_questions_answer",
            "args": {"analysis_id": "analysis-one", "session_action": "resume", "important_only": False}})
        assert calls[-1]["important_only"] is False
        result = await adapter.execute_tool(session, {"name": "collaboration_questions_inbox",
            "args": {"analysis_id": "analysis-one"}})
        assert result["analysis"]["result_markdown"] == "Verified result"
    finally:
        await collaboration.close()
        store.close()
