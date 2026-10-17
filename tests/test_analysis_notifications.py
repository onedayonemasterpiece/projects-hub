import pytest
from fastapi.testclient import TestClient
from test_mcp_events import setup
from test_question_session_results import ready_result
from projects_hub.analysis_notifications import recent_analysis_completions
from projects_hub.app import create_app
from projects_hub.device_commands import DeviceCommandService
from projects_hub.settings import Settings


@pytest.mark.asyncio
async def test_result_feed_is_personal_read_only_and_authorized(setup, tmp_path):
    store, events, service, analysis, repository, ready, payload = await ready_result(setup)
    actor = ready['context_actor_id']
    workspace = store.db.execute('SELECT workspace_id FROM projects WHERE id=?', (ready['project_id'],)).fetchone()[0]
    get = lambda: recent_analysis_completions(analysis, actor_id=actor, workspace_id=workspace)
    assert get() == []
    await events.bindings.record_result_async(service['id'], ready, payload)
    before = list(store.db.execute('SELECT * FROM collaboration_attention_state'))
    items = get()
    assert len(items) == 1 and items[0]['analysis_id'] == ready['analysis_id']
    assert items[0]['id'].startswith('analysis_event_')
    assert get() == items
    assert list(store.db.execute('SELECT * FROM collaboration_attention_state')) == before
    # This event predates any Slack delivery confirmation: the verified in-app
    # result is available independently, while Slack links remain receipt-gated.
    devices = DeviceCommandService(store)
    registered = devices.register_device(actor_id=actor, workspace_id=workspace,
        display_name='Result phone', platform='android', capabilities=['calendar.read_events'])
    app = create_app(Settings(data_dir=tmp_path/'data',static_dir=tmp_path/'missing',
        session_secret='s'*48,dev_auth=True,cookie_secure=False),store=store,
        device_commands=devices,collaboration_analysis=analysis)
    with TestClient(app, base_url='http://localhost') as client:
        assert client.get('/api/device/analysis/completed').status_code == 401
        assert client.get('/api/device/analysis/completed',headers={'authorization':'Device bad.invalid'}).status_code == 401
        headers={'authorization':'Device '+registered['device_token']}
        response=client.get('/api/device/analysis/completed?actor_id=someone-else&workspace_id=other',headers=headers)
        assert response.status_code == 200 and response.json() == {'items':items}
        store.db.execute("UPDATE collaboration_events SET addressed_to_actor_id=NULL WHERE kind='question_result_ready'")
        assert client.get('/api/device/analysis/completed',headers=headers).json() == {'items':[]}
        store.db.execute("UPDATE collaboration_events SET addressed_to_actor_id=? WHERE kind='question_result_ready'",(actor,))
        store.db.execute('UPDATE project_grants SET revoked_at_ms=1 WHERE actor_id=? AND project_id=?',(actor,ready['project_id']))
        assert client.get('/api/device/analysis/completed',headers=headers).json() == {'items':[]}


@pytest.mark.asyncio
async def test_result_feed_bounds_window_and_requires_verified_artifact(setup):
    store, events, service, analysis, repository, ready, payload = await ready_result(setup)
    try:
        await events.bindings.record_result_async(service['id'], ready, payload)
        workspace = store.db.execute('SELECT workspace_id FROM projects WHERE id=?',(ready['project_id'],)).fetchone()[0]
        get=lambda: recent_analysis_completions(analysis, actor_id=ready['context_actor_id'],workspace_id=workspace)
        event=store.db.execute("SELECT * FROM collaboration_events WHERE kind='question_result_ready'").fetchone()
        for i in range(25):
            store.db.execute('''INSERT INTO collaboration_events(workspace_id,project_id,actor_id,kind,object_kind,object_id,parent_object_id,addressed_to_actor_id,summary,created_at_ms)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',tuple(event[k] for k in ('workspace_id','project_id','actor_id','kind','object_kind','object_id','parent_object_id','addressed_to_actor_id','summary','created_at_ms')))
        result=get()
        assert len(result)==20
        assert int(result[0]['id'].split('_')[-1]) > int(result[-1]['id'].split('_')[-1])
        store.db.execute("UPDATE collaboration_analyses SET result_json='{}' WHERE id=?",(ready['analysis_id'],))
        assert get()==[]
        store.db.execute("UPDATE collaboration_events SET created_at_ms=1 WHERE kind='question_result_ready'")
        assert get()==[]
    finally:
        await analysis.close()
