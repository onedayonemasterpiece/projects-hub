from __future__ import annotations

import hashlib

import pytest

from test_mcp_events import setup, live_fixture, DELIVERY, REQUEST_EVENT
from projects_hub.collaboration import CollaborationService
from projects_hub.collaboration_analysis import CollaborationAnalysisService
from projects_hub.store import StoreError


class ResultRepository:
    def __init__(self):
        self.source_sha = "a" * 40
        self.files = {}
        self.writes = 0
        self.reads = 0
        self.change_during_write = False

    async def read_repository_path(self, **kwargs):
        self.reads += 1
        return dict(kind="file", sha=self.source_sha, text="Original source")

    async def write_repository_text(self, **kwargs):
        self.writes += 1
        path, text = kwargs["path"], kwargs["text"]
        if path in self.files and self.files[path] != text:
            raise StoreError("GITHUB_WRITE_CONFLICT", "Different result exists")
        self.files[path] = text
        if self.change_during_write:
            self.source_sha = "b" * 40
        blob = ("blob " + str(len(text.encode())) + "\0").encode() + text.encode()
        return dict(kind="file", sha=hashlib.sha1(blob).hexdigest(), text=text,
                    full_name="example/docs", path=path, default_branch="main")


async def ready_result(setup):
    store, auth, events, receiver, owner, service, bindings = live_fixture(setup)
    binding = bindings[0]
    project = owner["project_id"]
    note_id = binding["note_id"]
    store.db.execute("UPDATE project_notes SET repository_sha=? WHERE id=?", ("a" * 40, note_id))
    events.bindings.bind(owner["actor_id"], project, note_id, service["client_id"],
                         binding["slack_workspace_id"], binding["slack_channel_id"],
                         binding["slack_thread_ts"], expected_revision=1, allow_questions=True)
    await events.subscribe(service["id"], REQUEST_EVENT, {"project_id": project}, DELIVERY)
    repository = ResultRepository()
    analysis = CollaborationAnalysisService(store, CollaborationService(store, repository))
    analysis.collaboration._project_docs_repository = lambda *args: dict(repository_id=123, private=True)
    events.analysis_service = analysis
    analysis.question_events = events
    request = events.start_question_session(owner["actor_id"], project, note_id, "result-session-start")
    saved = events.record_request_result(service["id"], project, request["request_id"], request["eventId"], dict(
        kind="question_package", external_analysis_id="result-analysis-dot", source_kind="project_note",
        source_id=note_id, source_revision=1, source_repository_sha="a" * 40,
        purpose="requirements", summary="A decision is needed", questions=[dict(
            external_question_id="result-question-dot", prompt="Which approach?", context="Existing scope",
            reason="Avoid rework", priority="high", blocking=True, deadline_ms=None)]))
    workspace = store.db.execute("SELECT workspace_id FROM projects WHERE id=?", (project,)).fetchone()[0]
    receipt = analysis.answer_questions(actor_id=owner["actor_id"], workspace_id=workspace,
        analysis_id=saved["analysis_id"], command_id="result-answer-001", responses=[dict(
            question_id=saved["question_package"]["questions"][0]["id"], disposition="answer",
            body="Private answer transcript that must not enter provenance")])
    receipt = analysis.control_question_session(actor_id=owner["actor_id"], workspace_id=workspace,
        analysis_id=saved["analysis_id"], command_id="result-complete-001", action="complete")
    ready = store.db.execute("SELECT * FROM mcp_test_requests WHERE id=?",
                             (receipt["continuation_request_id"],)).fetchone()
    payload = dict(kind="analysis_result", source_revision=1, summary="Decision incorporated",
                   result_markdown="# Revised result\n\nUse the agreed approach.",
                   used_receipts=["result-answer-001"])
    return store, events, service, analysis, repository, ready, payload


@pytest.mark.asyncio
async def test_result_publication_verified_and_retry_has_no_network(setup):
    store, events, service, analysis, repository, ready, payload = await ready_result(setup)
    saved = await events.bindings.record_result_async(service["id"], ready, payload)
    result = saved["result"]
    assert result["artifact_sha"] and result["artifact_path"] != "docs/1.md"
    assert result["artifact_url"].startswith("https://github.com/example/docs/blob/main/")
    assert result["provenance"]["anchor_sha"] == "a" * 40
    assert saved["answer_receipts"][0]["command_id"] == "result-answer-001"
    assert saved["question_package"]["result_markdown"] == payload["result_markdown"]
    assert saved["destinations"]["slack_state"] == "pending"
    assert saved["destinations"]["mira_state"] == "available"
    assert store.db.execute("SELECT COUNT(*) FROM collaboration_events WHERE kind='question_result_ready'").fetchone()[0] == 1
    assert "Private answer transcript" not in next(iter(repository.files.values()))
    writes, reads = repository.writes, repository.reads
    again = await events.bindings.record_result_async(service["id"], ready, payload)
    assert again["result"] == result
    assert (repository.writes, repository.reads) == (writes, reads)
    with pytest.raises(StoreError, match="Different result"):
        await events.bindings.record_result_async(service["id"], ready, {**payload, "summary": "Changed"})
    await analysis.close()


@pytest.mark.asyncio
async def test_result_rejects_client_artifact_claim_and_sync_bypass(setup):
    store, events, service, analysis, repository, ready, payload = await ready_result(setup)
    with pytest.raises(StoreError):
        await events.bindings.record_result_async(service["id"], ready, {**payload, "artifact_sha": "c" * 40})
    with pytest.raises(StoreError) as caught:
        events.bindings.record_result(service["id"], ready, payload)
    assert caught.value.code == "RESULT_PUBLICATION_REQUIRED"
    assert repository.writes == 0
    await analysis.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change_during_write", [False, True])
async def test_result_stale_repository_never_becomes_stored(setup, change_during_write):
    store, events, service, analysis, repository, ready, payload = await ready_result(setup)
    repository.change_during_write = change_during_write
    if not change_during_write:
        repository.source_sha = "b" * 40
    with pytest.raises(StoreError) as caught:
        await events.bindings.record_result_async(service["id"], ready, payload)
    assert caught.value.code == "QUESTION_SOURCE_STALE"
    assert store.db.execute("SELECT state FROM mcp_test_requests WHERE id=?", (ready["id"],)).fetchone()[0] == "awaiting_response"
    assert not store.db.execute("SELECT 1 FROM mcp_response_destinations WHERE request_id=?", (ready["id"],)).fetchone()
    assert repository.writes == int(change_during_write)
    await analysis.close()


@pytest.mark.asyncio
async def test_result_requires_every_answer_receipt_and_current_acl(setup):
    store, events, service, analysis, repository, ready, payload = await ready_result(setup)
    with pytest.raises(StoreError):
        await events.bindings.record_result_async(service["id"], ready, {**payload, "used_receipts": []})
    store.db.execute("UPDATE mcp_discussion_bindings SET questions_allowed=0 WHERE id=?", (ready["binding_id"],))
    with pytest.raises(StoreError):
        await events.bindings.record_result_async(service["id"], ready, payload)
    assert repository.writes == 0
    await analysis.close()


@pytest.mark.asyncio
async def test_partial_result_preserves_unresolved_blockers_after_slack_receipt(setup):
    store, events, service, analysis, repository, ready, payload = await ready_result(setup)
    store.db.execute("UPDATE collaboration_analyses SET package_state='partially_completed' WHERE id=?", (ready["analysis_id"],))
    store.db.execute("UPDATE collaboration_questions SET state='open',disposition='unknown' WHERE analysis_id=?", (ready["analysis_id"],))
    saved = await events.bindings.record_result_async(service["id"], ready, payload)
    assert saved["result"]["provenance"]["completion_scope"] == "partial"
    assert saved["result"]["provenance"]["unresolved_questions"][0]["blocking"]
    claimed = events.bindings.record_result(service["id"], ready, dict(kind="slack_claim", response_id=saved["response_id"]))
    route = claimed["slack"]
    confirmed = events.bindings.record_result(service["id"], ready, dict(
        kind="slack_receipt", response_id=saved["response_id"],
        claim_id=claimed["destinations"]["claim_id"], status="confirmed",
        message_id="1791900099.000002", error="", **route))
    assert confirmed["question_package"]["package_state"] == "partially_completed"
    assert confirmed["destinations"]["mira_state"] == "available"
    assert store.db.execute("SELECT COUNT(*) FROM collaboration_events WHERE kind='question_result_ready'").fetchone()[0] == 1
    await analysis.close()


@pytest.mark.asyncio
async def test_result_rejects_public_repository_before_publication(setup):
    store, events, service, analysis, repository, ready, payload = await ready_result(setup)
    analysis.collaboration._project_docs_repository = lambda *args: dict(repository_id=123, private=False)
    with pytest.raises(StoreError) as caught:
        await events.bindings.record_result_async(service["id"], ready, payload)
    assert caught.value.code == "NOTE_AUDIENCE_REPOSITORY_MISMATCH"
    assert repository.writes == repository.reads == 0
    await analysis.close()

@pytest.mark.asyncio
async def test_followup_round_links_prior_receipts_and_replays(setup):
    store, events, service, analysis, repository, ready, result = await ready_result(setup)
    try:
        read=events.get_project_request(service['id'],ready['project_id'],ready['id'])
        payload=dict(kind='question_package',external_analysis_id='followup-analysis-001',source_kind='project_note',
            source_id=read['note_id'],source_revision=read['source_revision'],source_repository_sha=read['source_repository_sha'],
            purpose='requirements',summary='One remaining decision',questions=[dict(external_question_id='followup-question-001',
            prompt='Which deadline?',context='Chosen approach',reason='Schedule delivery',blocking=True,priority='high',deadline_ms=None)])
        saved=events.record_request_result(service['id'],ready['project_id'],ready['id'],ready['event_id'],payload)
        scope=saved['question_package']['scope']
        assert scope['parent_analysis_id']==ready['analysis_id']
        assert scope['round_number']==2
        assert any(r['command_id']=='result-answer-001' for r in scope['parent_answer_receipts'])
        assert saved['analysis_id']!=ready['analysis_id']
        assert events.record_request_result(service['id'],ready['project_id'],ready['id'],ready['event_id'],payload)['analysis_id']==saved['analysis_id']
        with pytest.raises(StoreError):
            events.record_request_result(service['id'],ready['project_id'],ready['id'],ready['event_id'],{**payload,'summary':'Changed'})
        assert store.db.execute('SELECT COUNT(*) FROM collaboration_analyses').fetchone()[0]==2
    finally:
        await analysis.close()
