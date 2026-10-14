"""Deterministic transport/Live checks; these are not physical or idle-dot acceptance."""
import pytest
from jsonschema import validate
from projects_hub.collaboration import CollaborationService
from projects_hub.collaboration_analysis import CollaborationAnalysisService
from projects_hub.mcp_api import RESULT_SCHEMA
from projects_hub.mcp_events import REQUEST_EVENT, MCPEvents
from projects_hub.mcp_auth import MCPAuth
from projects_hub.store import DurableStore
from test_mcp_events import setup, live_fixture, DELIVERY, ORIGIN


def question_context(setup):
    store, auth, events, receiver, owner, service, bindings = live_fixture(setup)
    b = bindings[0]
    b = events.bindings.bind(
        owner['actor_id'], owner['project_id'], b['note_id'], service['client_id'],
        b['slack_workspace_id'], b['slack_channel_id'], b['slack_thread_ts'],
        expected_revision=b['revision'], allow_questions=True,
    )
    workspace = store.db.execute('SELECT workspace_id FROM projects WHERE id=?',
                                 (owner['project_id'],)).fetchone()[0]
    analysis = CollaborationAnalysisService(store, CollaborationService(store, None))
    events.analysis_service = analysis
    analysis.question_events = events
    return store, auth, events, receiver, owner, service, b, workspace, analysis


@pytest.mark.asyncio
async def test_expired_question_delivery_is_resumed_on_same_subscription(setup):
    store, auth, events, receiver, owner, service, binding, workspace, analysis = question_context(setup)
    project = owner['project_id']
    sub = await events.subscribe(service['id'], REQUEST_EVENT, {'project_id': project}, DELIVERY)
    request = events.start_question_session(owner['actor_id'], project, binding['note_id'], 'expiry-start')
    store.db.execute('UPDATE mcp_subscriptions SET expires_ms=0 WHERE id=?', (sub['id'],))
    assert await events.deliver_one()
    assert not receiver.events
    assert store.db.execute('SELECT state FROM mcp_outbox').fetchone()[0] == 'cancelled'
    assert (await events.subscribe(service['id'], REQUEST_EVENT, {'project_id': project}, DELIVERY))['id'] == sub['id']
    assert await events.deliver_one()
    assert len(receiver.events) == 1
    assert receiver.events[0][0]['eventId'] == request['eventId']
    await events.subscribe(service['id'], REQUEST_EVENT, {'project_id': project}, DELIVERY)
    assert not await events.deliver_one()
    await analysis.close()


@pytest.mark.asyncio
async def test_explicit_completion_waits_durably_for_renewed_transport(setup):
    store, auth, events, receiver, owner, service, binding, workspace, analysis = question_context(setup)
    project = owner['project_id']
    sub = await events.subscribe(service['id'], REQUEST_EVENT, {'project_id': project}, DELIVERY)
    request = events.start_question_session(owner['actor_id'], project, binding['note_id'], 'ready-renewal-start')
    read = events.get_project_request(service['id'], project, request['request_id'])
    payload = dict(kind='question_package', external_analysis_id='dot-renewal-analysis',
                   source_kind='project_note', source_id=binding['note_id'], source_revision=read['source_revision'],
                   source_repository_sha=None, purpose='requirements', summary='Clarify scope',
                   questions=[dict(external_question_id='dot-renewal-question',
                                   prompt='Which scope?', context='Existing note scope.', reason='Determine bounded result.',
                                   priority='critical', blocking=True, deadline_ms=None)])
    saved = events.record_request_result(service['id'], project, request['request_id'], request['eventId'], payload)
    aid = saved['analysis_id']
    qid = saved['question_package']['questions'][0]['id']
    answered = analysis.answer_questions(actor_id=owner['actor_id'], workspace_id=workspace,
        analysis_id=aid, command_id='renewal-answer',
        responses=[dict(question_id=qid, disposition='answer', body='The current scope.')])
    assert answered['continuation'] != 'ready_for_dot'
    store.db.execute('UPDATE mcp_subscriptions SET expires_ms=0 WHERE id=?', (sub['id'],))
    complete = analysis.control_question_session(actor_id=owner['actor_id'], workspace_id=workspace,
        analysis_id=aid, command_id='renewal-complete', action='complete')
    assert complete['continuation'] == 'waiting_event_transport'
    ready_id = complete['continuation_request_id']
    assert ready_id
    assert store.db.execute('SELECT count(*) FROM mcp_outbox WHERE request_id=?', (ready_id,)).fetchone()[0] == 0
    await analysis.close()
    reopened = DurableStore(store.data_dir)
    try:
        restored = MCPEvents(reopened, MCPAuth(reopened, ORIGIN), 's' * 48, receiver)
        await restored.subscribe(service['id'], REQUEST_EVENT, {'project_id': project}, DELIVERY)
        assert reopened.db.execute('SELECT count(*) FROM mcp_outbox WHERE request_id=?', (ready_id,)).fetchone()[0] == 1
        while await restored.deliver_one():
            pass
        emitted = [e[0] for e in receiver.events if e[0]['data'].get('request_id') == ready_id]
        assert len(emitted) == 1
        assert emitted[0]['data']['request_kind'] == 'questions_ready'
        assert 'answers' not in emitted[0]['data']
        await restored.subscribe(service['id'], REQUEST_EVENT, {'project_id': project}, DELIVERY)
        assert not await restored.deliver_one()
    finally:
        reopened.close()


def test_mcp_schema_accepts_server_published_analysis_result():
    validate(dict(kind='analysis_result', source_revision=1, summary='Bound result',
                  result_markdown='Substantive updated result.', used_receipts=['answer-command']),
             RESULT_SCHEMA)
