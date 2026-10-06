import asyncio
import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from projects_hub.app import create_app
from projects_hub.auth import COOKIE_NAME, issue_session
from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_capabilities import BUNDLES
from projects_hub.live_resources import ConversationScope
from projects_hub.settings import Settings
from projects_hub.store import DurableStore, StoreError
from live_tools import activate


@pytest.fixture
def context(tmp_path):
    store = DurableStore(tmp_path / 'data')
    boot = store.ensure_dev_workspace('Theme')
    actor, workspace = boot['actor']['id'], boot['workspace']['id']
    conversation = store.create_conversation(actor, workspace)
    adapter = ProjectsHubLiveAdapter(store)
    initialized = adapter.initialize(
        resource_id=ConversationScope(workspace, actor, conversation['id']).resource_binding(),
        actor={'subject': actor}, model='gemini-3.8-live', conversation_id=conversation['id'],
        attempt_id='attempt_test',
    )
    session = SimpleNamespace(id='session_test', state=initialized['state'],
                              context=initialized['context'], capability='core', closed=False)
    yield store, boot, adapter, session, initialized
    store.close()


def request(session, command='theme-test', theme='light', revision=0, turn='accepted-turn'):
    return dict(actor_id=session.state['actor_id'], conversation_id=session.state['conversation_id'],
                source_id=session.state['source_id'], turn_id=turn, command_id=command,
                theme=theme, expected_revision=revision)


def test_default_migration_restart_and_private_actor(context):
    store, boot, _, session, _ = context
    assert store.get_preferences(boot['actor']['id']) == {'theme': 'dark', 'revision': 0}
    assert store.db.execute('SELECT count(*) FROM actor_preferences').fetchone()[0] == 0
    result = store.set_theme(**request(session))
    assert result['changed'] and result['persistence_status'] == 'verified'
    second = store.ensure_external_workspace(provider='fixture', subject='second', display_name='B')
    assert store.get_preferences(second['actor']['id']) == {'theme': 'dark', 'revision': 0}
    reopened = DurableStore(store.data_dir)
    try:
        assert reopened.bootstrap(boot['actor']['id'])['preferences'] == {'theme': 'light', 'revision': 1}
        assert reopened.get_source(boot['actor']['id'], session.state['source_id'])['conversation_id'] == session.state['conversation_id']
        assert reopened.set_theme(**request(session)) == result
    finally:
        reopened.close()


def test_noop_roundtrip_exact_retry_conflict_and_superseded(context):
    store, _, _, session, _ = context
    light = store.set_theme(**request(session))
    noop = store.set_theme(**request(session, 'noop', 'light', 1, 'turn-two'))
    assert noop['changed'] is False and noop['revision'] == 1
    dark = store.set_theme(**request(session, 'dark', 'dark', 1, 'turn-three'))
    assert dark['revision'] == 2
    old = store.set_theme(**request(session))
    assert old['application_status'] == 'superseded'
    assert old['current'] == {'theme': 'dark', 'revision': 2}
    assert old['theme'] == light['theme']
    with pytest.raises(StoreError) as error:
        store.set_theme(**request(session, theme='dark'))
    assert error.value.code == 'COMMAND_CONFLICT'
    with pytest.raises(StoreError) as error:
        store.set_theme(**request(session, 'stale', 'light', 1))
    assert error.value.code == 'REVISION_CONFLICT'
    assert store.set_theme(**request(session, 'light-again', 'light', 2, 'turn-four'))['revision'] == 3


def test_concurrent_writes_and_duplicate_receipts(context):
    store, _, _, session, _ = context
    def write(command):
        try:
            return store.set_theme(**request(session, command))
        except StoreError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, ['one', 'two']))
    assert sum(isinstance(value, dict) for value in results) == 1
    assert 'REVISION_CONFLICT' in results
    winner = 'one' if isinstance(results[0], dict) else 'two'
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert all(value['revision'] == 1 for value in executor.map(write, [winner, winner]))
    assert store.db.execute('SELECT count(*) FROM preference_receipts').fetchone()[0] == 1


def test_transaction_failure_rolls_back_preference_and_receipt(context):
    store, _, _, session, _ = context
    store.db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON preference_receipts BEGIN SELECT RAISE(ABORT, 'fixture'); END")
    with pytest.raises(StoreError) as error:
        store.set_theme(**request(session))
    assert error.value.code == 'PREFERENCE_STORE_UNAVAILABLE'
    assert store.get_preferences(session.state['actor_id']) == {'theme': 'dark', 'revision': 0}
    assert store.db.execute('SELECT count(*) FROM preference_receipts').fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('args', [
    {'theme': 'system', 'expected_revision': 0}, {'theme': 'light', 'expected_revision': True},
    {'theme': 'light', 'expected_revision': 0.0}, {'theme': 'light', 'expected_revision': -1},
    {'theme': 'light', 'expected_revision': 0, 'actor_id': 'other'},
    {'theme': 'light', 'expected_revision': 0, 'css': 'x' * 9000}, {},
])
async def test_strict_setter_arguments(context, args):
    store, _, adapter, session, _ = context
    activate(adapter, session, 'preferences')
    with pytest.raises(StoreError) as error:
        await adapter.execute_tool(session, {'name': 'preferences_set_theme', 'args': args})
    assert error.value.code == 'INVALID_ARGUMENT'
    assert store.get_preferences(session.state['actor_id'])['revision'] == 0


@pytest.mark.asyncio
async def test_application_ack_and_late_transcription_duplicate(context):
    store, _, adapter, session, _ = context
    activate(adapter, session, 'preferences')
    events = []
    def emit(active, event):
        events.append(event)
        assert active is session
        adapter.acknowledge_preference(active, event['command_id'], event['theme'], event['revision'])
    adapter.emit = emit
    call = {'name': 'preferences_set_theme', 'id': 'provider-one', 'args': {'theme': 'light', 'expected_revision': 0}}
    result = await adapter.execute_tool(session, call)
    assert result['application_status'] == 'applied'
    adapter.on_event(session, {'type': 'input_transcript', 'text': 'late canonical'})
    again = await adapter.execute_tool(session, {**call, 'id': 'provider-two'})
    assert again == result and len(events) == 1
    assert adapter.acknowledge_preference(session, result['command_id'], 'light', 1)['application_status'] == 'applied'
    with pytest.raises(StoreError):
        adapter.acknowledge_preference(session, result['command_id'], 'dark', 1)
    assert store.db.execute('SELECT count(*) FROM preference_receipts').fetchone()[0] == 1
    adapter.on_stopped(session)
    assert not adapter._preference_applications


@pytest.mark.asyncio
async def test_missing_ack_pending_stop_and_no_store_lock(context):
    store, _, adapter, session, _ = context
    activate(adapter, session, 'preferences')
    emitted = asyncio.Event()
    adapter.emit = lambda *_: emitted.set()
    task = asyncio.create_task(adapter.execute_tool(session, {'name': 'preferences_set_theme', 'args': {'theme': 'light', 'expected_revision': 0}}))
    await emitted.wait()
    # Another session can persist while the first waits; no lock across await.
    other = store.set_theme(**request(session, 'other', 'dark', 1, 'other-turn'))
    assert other['revision'] == 2
    adapter.on_stopped(session)
    result = await asyncio.wait_for(task, .2)
    assert result['application_status'] == 'superseded'
    assert not adapter._preference_applications


@pytest.mark.asyncio
async def test_ack_timeout_is_bounded_and_truthful(context):
    _, _, adapter, session, _ = context
    activate(adapter, session, 'preferences')
    adapter.emit = lambda *_: None
    before = asyncio.get_running_loop().time()
    result = await adapter.execute_tool(session, {'name': 'preferences_set_theme', 'args': {'theme': 'light', 'expected_revision': 0}})
    elapsed = asyncio.get_running_loop().time() - before
    assert 2.9 <= elapsed < 3.5
    assert result['application_status'] == 'pending'
    assert result['persistence_status'] == 'verified'


@pytest.mark.asyncio
async def test_out_of_bundle_and_no_audio_fail_closed(context):
    store, _, adapter, session, _ = context
    with pytest.raises(StoreError) as error:
        await adapter.execute_tool(session, {'name': 'preferences_set_theme', 'args': {'theme': 'light', 'expected_revision': 0}})
    assert error.value.code == 'TOOL_NOT_AVAILABLE'
    session.capability = 'preferences'
    with pytest.raises(StoreError) as error:
        await adapter.execute_tool(session, {'name': 'preferences_set_theme', 'args': {'theme': 'light', 'expected_revision': 0}})
    assert error.value.code == 'FORBIDDEN'
    adapter.on_event(session, {'type': 'caption_final_transcript', 'text': 'Включи светлую тему'})
    assert store.get_preferences(session.state['actor_id'])['revision'] == 0


def test_every_bundle_and_recovery_restrictions(context):
    store, _, adapter, session, initialized = context
    assert {f['name'] for f in initialized['configuration']['functions']} == {'activate_capability', *BUNDLES['core']}
    original = initialized['configuration']
    for capability in adapter._allowed_capabilities(session):
        selected = activate(adapter, session, capability)
        assert selected['configuration']['voice'] == original['voice']
        assert selected['configuration']['input_audio_transcription'] == original['input_audio_transcription']
        assert selected['configuration']['manual_activity_detection'] is True
        assert selected['context']['conversation_id'] == session.state['conversation_id']
    session.state['recovery_only'] = True
    with pytest.raises(StoreError) as error:
        activate(adapter, session, 'preferences')
    assert error.value.code == 'TOOL_NOT_AVAILABLE'


def test_api_read_and_ack_origin_identity_binding(context, tmp_path):
    store, boot, adapter, session, _ = context
    settings = Settings(data_dir=store.data_dir, static_dir=tmp_path/'missing',
                        session_secret='s'*32, dev_auth=True, cookie_secure=False)
    class Host:
        def __init__(self): self.adapter = adapter
        def _get(self, session_id, resource_id, actor):
            if session_id != session.id or actor['subject'] != session.state['actor_id'] or resource_id != ConversationScope(session.state['workspace_id'], session.state['actor_id'], session.state['conversation_id']).resource_binding():
                raise StoreError('FORBIDDEN', 'Session binding mismatch')
            return session
    app = create_app(settings, store=store, live_host=Host())
    with TestClient(app) as client:
        assert client.get('/api/preferences').status_code == 401
        client.cookies.set(COOKIE_NAME, issue_session(boot['actor']['id'], settings.session_secret))
        assert client.get('/api/bootstrap').json()['preferences'] == {'theme': 'dark', 'revision': 0}
        assert client.get('/api/preferences').json() == {'actor_id': boot['actor']['id'], 'theme': 'dark', 'revision': 0}
        stored = store.set_theme(**request(session))
        adapter._preference_applications[session.id] = {**stored, 'event': asyncio.Event(), 'applied': False}
        url = f"/api/live/{session.state['conversation_id']}/sessions/{session.id}/preferences/theme-test/applied"
        payload = {'theme': 'light', 'revision': 1}
        assert client.post(url, json=payload).status_code == 403
        assert client.post(url, headers={'Origin': 'https://foreign.invalid'}, json=payload).status_code == 403
        assert client.post(url, headers={'Origin': 'http://testserver'}, json={**payload, 'actor_id': 'other'}).status_code == 422
        assert client.post(url, headers={'Origin': 'http://testserver'}, json={**payload, 'revision': True}).status_code == 422
        assert client.post(url, headers={'Origin': 'http://testserver'}, json={**payload, 'theme': 'dark'}).status_code == 403
        for _ in range(2):
            assert client.post(url, headers={'Origin': 'http://testserver'}, json=payload).json()['application_status'] == 'applied'
        other = store.ensure_external_workspace(provider='fixture', subject='other', display_name='Other')
        client.cookies.set(COOKIE_NAME, issue_session(other['actor']['id'], settings.session_secret))
        assert client.post(url, headers={'Origin': 'http://testserver'}, json=payload).status_code in (403, 404)
        assert client.get('/api/preferences').json() == {'actor_id': other['actor']['id'], 'theme': 'dark', 'revision': 0}

@pytest.mark.asyncio
async def test_real_shared_host_router_retains_turn_and_model(context):
    from live_interaction import LiveSocketSessionHost
    store, _, _, existing, _ = context
    connections = []
    async def provider(*, load_key, reader, on_event):
        setup = json.loads(await reader.readline())
        connections.append(setup)
        on_event({'type': 'ready', 'model': 'gemini-3.8-live'})
        while raw := await reader.readline():
            message = json.loads(raw)
            if message['type'] == 'stop': return
            if message['type'] == 'reconfigure':
                connections.append(message)
                on_event({'type': 'capability_ready', 'transition_id': message['transition_id'],
                          'capability': message['capability']})
    host = LiveSocketSessionHost(adapter_factory=lambda **hooks: ProjectsHubLiveAdapter(store, **hooks),
                                 provider_run=provider, key_resolver=lambda *_: 'fixture', ready_timeout_ms=500)
    state = existing.state
    binding = ConversationScope(state['workspace_id'], state['actor_id'], state['conversation_id']).resource_binding()
    actor = {'subject': state['actor_id'], 'tenant_id': state['workspace_id']}
    started = await host.start(resource_id=binding, actor=actor, model='gemini-3.8-live',
                               conversation_id=state['conversation_id'])
    active = host.sessions[started['session_id']]
    adapter = host.adapter
    try:
        adapter.input(active, {'activity_start': True})
        adapter.input(active, {'audio_base64': base64.b64encode(b'\x01\x00'*320).decode()})
        adapter.input(active, {'activity_end': True})
        turn = active.state['_current_utterance']['id']
        await host._handle_tool_calls(active, [{'name': 'activate_capability','id':'router-1',
                                               'args':{'capability':'preferences','intent':'Включи светлую тему'}}])
        assert active.capability == 'preferences'
        assert active.state['_capability_turn_id'] == turn
        assert active.state['conversation_id'] == state['conversation_id']
        event = asyncio.Event()
        original_emit = adapter.emit
        def emit(session, change):
            original_emit(session, change)
            adapter.acknowledge_preference(session, change['command_id'], change['theme'], change['revision'])
            event.set()
        adapter.adapter.emit = emit
        await host._handle_tool_calls(active, [{'name':'preferences_set_theme','id':'set-1',
                                               'args':{'theme':'light','expected_revision':0}}])
        assert event.is_set()
        assert store.get_preferences(state['actor_id']) == {'theme':'light','revision':1}
        await host._handle_tool_calls(active, [{'name':'activate_capability','id':'router-2',
                                               'args':{'capability':'memory','intent':'Продолжи проектную работу'}}])
        assert active.capability == 'memory'
        await host._handle_tool_calls(active, [{'name':'activate_capability','id':'router-denied',
                                               'args':{'capability':'owner_development','intent':'unauthorized'}}])
        assert active.capability == 'memory'
        assert any(e.get('code') == 'TOOL_NOT_AVAILABLE' for e in active.events)
        assert all(len(c['configuration']['functions']) <= 6 for c in connections if 'configuration' in c)
        assert any(c.get('continuation') for c in connections)
        # Guarded transport prevents a NEW mutation after a damaged open turn.
        socket_state = host._socket_states[active.id]
        socket_state.damaged = True
        active.capability = 'preferences'
        with pytest.raises(Exception) as error:
            await adapter.execute_tool(active, {'name':'preferences_set_theme','args':{'theme':'dark','expected_revision':1}})
        assert error.value.code == 'LIVE_INPUT_DAMAGED'
        assert store.get_preferences(state['actor_id'])['revision'] == 1
    finally:
        await host.stop_all()


@pytest.mark.asyncio
async def test_ordinary_actor_cannot_activate_owner_and_buffered_retry_is_stable(context):
    store, _, adapter, _, _ = context
    boot = store.ensure_external_workspace(provider='fixture', subject='ordinary', display_name='Ordinary')
    actor = boot['actor']['id']; workspace = boot['workspace']['id']
    conversation = store.create_conversation(actor, workspace)
    def start():
        initialized = adapter.initialize(resource_id=ConversationScope(workspace, actor, conversation['id']).resource_binding(),
            actor={'subject':actor},model='gemini-3.8-live',conversation_id=conversation['id'],
            audio_mode='buffered',client_source_id='local_'+'a'*32)
        return SimpleNamespace(id='buffered',state=initialized['state'],context=initialized['context'],capability='core',closed=False)
    first = start()
    assert 'owner_development' not in adapter._allowed_capabilities(first)
    with pytest.raises(StoreError): activate(adapter, first, 'owner_development')
    activate(adapter, first, 'preferences')
    saved = await adapter.execute_tool(first, {'name':'preferences_set_theme','args':{'theme':'light','expected_revision':0}})
    again = start()
    activate(adapter, again, 'preferences')
    retried = await adapter.execute_tool(again, {'name':'preferences_set_theme','id':'new-provider-id',
                                                'args':{'theme':'light','expected_revision':0}})
    assert retried['command_id'] == saved['command_id']
    assert retried['revision'] == 1


def test_additive_migration_preserves_existing_sources_and_commands(context):
    store, boot, _, session, _ = context
    source_id = session.state['source_id']
    store.db.execute("INSERT INTO commands VALUES(?,?,?,?,?,?,?,?)",
                     ('legacy-command', source_id, 'fixture', 'digest', 'verified', '{}', 1, 1))
    store.db.execute('DROP TABLE preference_receipts')
    store.db.execute('DROP TABLE actor_preferences')
    upgraded = DurableStore(store.data_dir)
    try:
        assert upgraded.get_preferences(boot['actor']['id']) == {'theme':'dark','revision':0}
        assert upgraded.get_source(boot['actor']['id'], source_id)['id'] == source_id
        assert upgraded.db.execute("SELECT status FROM commands WHERE id='legacy-command'").fetchone()[0] == 'verified'
        assert upgraded.set_theme(**request(session))['revision'] == 1
    finally:
        upgraded.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', ['not-json', '[]', False, {'actor_id':'other'}])
async def test_preference_get_rejects_non_object_and_extra_fields(context, raw):
    store, _, adapter, session, _ = context
    activate(adapter, session, 'preferences')
    with pytest.raises(StoreError) as error:
        await adapter.execute_tool(session, {'name':'preferences_get', 'args':raw})
    assert error.value.code == 'INVALID_ARGUMENT'
    assert store.get_preferences(session.state['actor_id'])['revision'] == 0
