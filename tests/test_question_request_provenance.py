from __future__ import annotations

import pytest

from test_mcp_events import setup, live_fixture, DELIVERY, REQUEST_EVENT
from projects_hub.collaboration import CollaborationService
from projects_hub.collaboration_analysis import CollaborationAnalysisService
from projects_hub.store import StoreError


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["matching", "null", "wrong_sha", "stale_revision", "revoked"])
async def test_question_request_exposes_current_provenance_and_checks_checkpoint(setup, case):
    store, auth, events, receiver, owner, service, bindings = live_fixture(setup)
    binding = bindings[0]
    project, note_id = owner["project_id"], binding["note_id"]
    store.db.execute("UPDATE project_notes SET repository_sha=? WHERE id=?", ("a" * 40, note_id))
    events.bindings.bind(owner["actor_id"], project, note_id, service["client_id"],
                        binding["slack_workspace_id"], binding["slack_channel_id"],
                        binding["slack_thread_ts"], expected_revision=1, allow_questions=True)
    await events.subscribe(service["id"], REQUEST_EVENT, {"project_id": project}, DELIVERY)
    analysis = CollaborationAnalysisService(store, CollaborationService(store, None))
    events.analysis_service = analysis
    analysis.question_events = events
    try:
        request = events.start_question_session(owner["actor_id"], project, note_id, "provenance-start")
        saved = events.get_project_request(service["id"], project, request["request_id"])
        note = store.db.execute("SELECT * FROM project_notes WHERE id=?", (note_id,)).fetchone()
        assert saved["source_kind"] == "project_note"
        assert saved["source_id"] == note_id
        assert saved["source_revision"] == note["revision"]
        assert saved["source_repository_sha"] == "a" * 40
        assert saved["source_repository_full_name"] == note["repository_full_name"]
        assert saved["source_repository_path"] == note["repository_path"]
        assert saved["questions_allowed"] is True
        if case in ("stale_revision", "revoked"):
            if case == "stale_revision":
                store.db.execute("UPDATE project_notes SET revision=revision+1 WHERE id=?", (note_id,))
            else:
                store.db.execute("UPDATE mcp_discussion_bindings SET questions_allowed=0 WHERE id=?", (binding["id"],))
            with pytest.raises(StoreError):
                events.get_project_request(service["id"], project, request["request_id"])
            return
        payload = dict(kind="question_package", external_analysis_id="provenance-analysis", source_kind="project_note",
                       source_id=note_id, source_revision=1,
                       source_repository_sha=None if case == "null" else ("b" if case == "wrong_sha" else "a") * 40,
                       purpose="requirements", summary="One choice", questions=[dict(
                           external_question_id="provenance-question", prompt="Which?", context="Existing note",
                           reason="Clarify", priority="high", blocking=True, deadline_ms=None)])
        if case == "wrong_sha":
            with pytest.raises(StoreError, match="repository checkpoint"):
                events.record_request_result(service["id"], project, request["request_id"], request["eventId"], payload)
        else:
            response = events.record_request_result(service["id"], project, request["request_id"], request["eventId"], payload)
            assert response["source_repository_sha"] == "a" * 40
            assert response["questions_allowed"] is True
    finally:
        await analysis.close()
