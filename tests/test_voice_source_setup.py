import copy
import pytest
from test_mcp_events import setup
from test_voice_analysis import voice_fixture
from projects_hub.voice_analysis_setup import VoiceSourceSetup
from projects_hub.store import StoreError


def fixture(setup):
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    connection=dict(repository_id=321,full_name='owner/intake',project_id=None,installation_id=7,
        role='unassigned',access_mode='read_only',allowed_paths=[],state='available',installation_state='active')
    store.get_repository_connection=lambda actor,workspace,repository_id: copy.deepcopy(connection)
    calls=[]
    async def audience(**kw):
        assert kw['repository_id']==321 and kw['_allow_unassigned'] is True
        return dict(owner_only=True,status='verified')
    def bind(**kw):
        calls.append(kw)
        assert kw['repository_id']==321 and kw['actor_id']==owner['actor_id']
        connection.update({k:kw[k] for k in ('project_id','role','access_mode','allowed_paths')})
        return copy.deepcopy(connection)
    repo.repository_audience,repo.bind_repository=audience,bind
    return store,owner,analysis,repo,bridge,connection,calls


@pytest.mark.asyncio
async def test_exact_connect_idempotent_and_no_targets(setup):
    store,owner,analysis,repo,bridge,connection,calls=fixture(setup)
    service=VoiceSourceSetup(bridge)
    assert service.status(owner['actor_id'])['state']=='source_binding_required'
    result=await service.connect(owner['actor_id'])
    assert result['state']=='ready' and result['can_connect_source'] is False
    assert len(calls)==1 and calls[0]['allowed_paths']==['intake']
    assert calls[0]['role']=='source_dataset' and calls[0]['access_mode']=='read_only'
    assert await service.connect(owner['actor_id'])==result and len(calls)==1
    assert bridge.config['token'] not in str(result)
    await analysis.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('audience',[dict(owner_only=None,status='unavailable'),dict(owner_only=False,status='verified')])
async def test_unknown_or_shared_audience_never_binds(setup,audience):
    store,owner,analysis,repo,bridge,connection,calls=fixture(setup)
    async def deny(**kw): return audience
    repo.repository_audience=deny
    with pytest.raises(StoreError,match='audience'):
        await VoiceSourceSetup(bridge).connect(owner['actor_id'])
    assert not calls and connection['role']=='unassigned'
    await analysis.close()


@pytest.mark.asyncio
async def test_wrong_owner_and_existing_other_binding_never_change(setup):
    store,owner,analysis,repo,bridge,connection,calls=fixture(setup)
    service=VoiceSourceSetup(bridge)
    with pytest.raises(StoreError): await service.connect('other')
    connection.update(role='project_docs',project_id=owner['project_id'])
    assert service.status(owner['actor_id'])['state']=='unavailable'
    with pytest.raises(StoreError): await service.connect(owner['actor_id'])
    assert not calls
    await analysis.close()


@pytest.mark.asyncio
async def test_concurrent_binding_or_acl_change_not_overwritten(setup):
    store,owner,analysis,repo,bridge,connection,calls=fixture(setup)
    async def changed(**kw):
        connection.update(role='external_owning_repo',allowed_paths=['different'])
        return dict(owner_only=True,status='verified')
    repo.repository_audience=changed
    with pytest.raises(StoreError): await VoiceSourceSetup(bridge).connect(owner['actor_id'])
    assert not calls and connection['allowed_paths']==['different']
    connection.update(role='unassigned')
    async def shared(**kw):
        store.db.execute("INSERT INTO actors VALUES('another','Another',1)")
        store.db.execute("INSERT INTO project_grants VALUES('another',?,'viewer',0,0,1,NULL)",(owner['project_id'],))
        return dict(owner_only=True,status='verified')
    repo.repository_audience=shared
    with pytest.raises(StoreError): await VoiceSourceSetup(bridge).connect(owner['actor_id'])
    assert not calls
    await analysis.close()


def test_actual_owner_cookie_routes_reject_bearer_and_targets(setup,tmp_path):
    from fastapi.testclient import TestClient
    from projects_hub.app import create_app
    from projects_hub.settings import Settings
    from projects_hub.auth import COOKIE_NAME, issue_session
    store,owner,analysis,repo,bridge,connection,calls=fixture(setup)
    settings=Settings(data_dir=tmp_path/'unused',static_dir=tmp_path/'static',session_secret='s'*48,
        public_origin='https://fixture.example',voice_analysis_config=bridge.config)
    app=create_app(settings,store=store,collaboration=bridge.collaboration,collaboration_analysis=analysis)
    client=TestClient(app,base_url='https://fixture.example', headers={
        'X-Forwarded-Host':'fixture.example','X-Forwarded-Proto':'https','X-Forwarded-Port':'443'})
    url='/api/voice-analysis/setup'
    assert client.get(url).status_code==401
    assert client.get(url,headers={'Authorization':'Bearer '+bridge.config['token']}).status_code==401
    client.cookies.set(COOKIE_NAME,issue_session(owner['actor_id'],settings.session_secret))
    assert client.get(url).json()['state']=='source_binding_required'
    assert client.post(url+'/connect-source',json={'repository_id':999},headers={'Origin':'https://fixture.example'}).status_code==400
    assert client.post(url+'/connect-source',headers={'Origin':'https://evil.example'}).status_code==403
    result=client.post(url+'/connect-source',headers={'Origin':'https://fixture.example'})
    assert result.status_code==200 and result.json()['state']=='ready'
    assert len(calls)==1

@pytest.mark.asyncio
async def test_configuration_change_during_audience_never_binds_new_target(setup):
    store,owner,analysis,repo,bridge,connection,calls=fixture(setup)
    async def changed(**kw):
        bridge.config['source_prefix']='different'
        return dict(owner_only=True,status='verified')
    repo.repository_audience=changed
    with pytest.raises(StoreError) as error:
        await VoiceSourceSetup(bridge).connect(owner['actor_id'])
    assert error.value.code=='VOICE_SETUP_CHANGED' and not calls
    await analysis.close()
