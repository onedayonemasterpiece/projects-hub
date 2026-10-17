import pytest
from types import SimpleNamespace
from test_mcp_events import setup
from test_voice_analysis import voice_fixture
from projects_hub.request_readiness import read_request_with_readiness
from projects_hub.github_connections import GitHubConnections
from projects_hub.github_app import GitHubAppError
from projects_hub.store import StoreError


@pytest.mark.asyncio
async def test_readiness_is_bound_read_only_and_revocation_rechecked(setup):
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    original=store.get_repository_connection
    try:
        accepted=await bridge.accept(payload)
        calls=[]
        def connection(actor,workspace,repo_id):
            assert actor==owner['actor_id']
            return dict(repository_id=repo_id,full_name='owner/private' if repo_id==123 else 'owner/intake',
                private=True,role='project_docs' if repo_id==123 else 'source_dataset',access_mode='app_managed_write',
                state='available',installation_state='active',project_id=owner['project_id'],allowed_paths=['docs/notes'],default_branch='main')
        store.get_repository_connection=connection
        async def audience(**kw):
            calls.append(kw['repository_id'])
            return dict(owner_only=True,status='verified',other_collaborator_count=0)
        repo.repository_audience=audience
        read=await read_request_with_readiness(events,service['id'],owner['project_id'],accepted['request_id'])
        ready=read['binding_readiness']
        assert ready['owner_only'] is True and ready['automatic_changes'] is False
        assert calls==[123,321]
        assert [r['purpose'] for r in ready['repositories']]==['request_repository','configured_voice_source']
        assert 'owner_id' not in ready and 'workspace' not in ready
        # Recheck auth after network I/O. No stale content escapes a revoked binding.
        async def revoked(**kw):
            store.db.execute("UPDATE mcp_voice_bindings SET state='revoked' WHERE id=?",(read['binding_id'],))
            return dict(owner_only=True,status='verified')
        repo.repository_audience=revoked
        with pytest.raises(StoreError):
            await read_request_with_readiness(events,service['id'],owner['project_id'],accepted['request_id'])
    finally:
        store.get_repository_connection=original
        await analysis.close()


class AudienceClient:
    def __init__(self,pages): self.pages,self.calls=pages,[]
    async def installation_token(self,*args,**kw):
        assert kw==dict(repository_ids=[123],permissions={'metadata':'read'})
        return 'test-only'
    async def repository_metadata(self,**kw):
        return dict(id=123,full_name='owner/private',private=True)
    async def repository_collaborator_page(self,**kw):
        self.calls.append(kw['page'])
        value=self.pages[min(kw['page']-1,len(self.pages)-1)]
        if isinstance(value,Exception): raise value
        return value


def audience_service(pages,private=True,account_type='User'):
    client=AudienceClient(pages)
    store=SimpleNamespace(
        get_repository_connection=lambda *a:dict(repository_id=123,installation_id=7,full_name='owner/private',private=private,
            project_id='p',role='project_docs',state='available',installation_state='active'),
        project_access=lambda *a:None,
        github_installation=lambda *a:dict(account_login='owner',account_type=account_type))
    service=object.__new__(GitHubConnections)
    service.store,service.client=store,client
    return service,client


@pytest.mark.asyncio
@pytest.mark.parametrize('pages,expected,status',[
    ([[{'login':'owner'}]],True,'verified'),
    ([[{'login':'other'}]],False,'verified'),
    ([GitHubAppError('GITHUB_FORBIDDEN','denied',status=403)],None,'unavailable'),
    ([GitHubAppError('GITHUB_NOT_FOUND','missing',status=404)],None,'unavailable'),
    ([[{'bad':'value'}]],None,'invalid_response'),
    ([[{'login':str(i)} for i in range(100)],[]],False,'verified'),
    ([[{'login':str(i)} for i in range(100)]],None,'incomplete_pagination'),
])
async def test_audience_verified_or_explicit_unknown(pages,expected,status):
    service,client=audience_service(pages)
    result=await service.repository_audience(actor_id='a',workspace_id='w',repository_id=123)
    assert result['owner_only'] is expected and result['status']==status
    assert 'login' not in str(result) and 'other' not in str(result.get('collaborators',''))


@pytest.mark.asyncio
async def test_audience_bounded_and_no_personal_owner_inference_for_org():
    pages=[[{'login':str(page*100+i)} for i in range(100)] for page in range(5)]
    service,client=audience_service(pages)
    result=await service.repository_audience(actor_id='a',workspace_id='w',repository_id=123)
    assert result['owner_only'] is None and result['status']=='pagination_limit'
    assert len(client.calls)==5
    service,client=audience_service([[]],account_type='Organization')
    result=await service.repository_audience(actor_id='a',workspace_id='w',repository_id=123)
    assert result['owner_only'] is None and not client.calls

@pytest.mark.asyncio
@pytest.mark.parametrize('metadata,expected,status',[
    (dict(id=123,full_name='owner/private',private=False),False,'public_repository'),
    (dict(id=456,full_name='owner/private',private=True),None,'identity_unverified'),
    (dict(id=123,full_name='changed/private',private=True),None,'identity_unverified'),
])
async def test_audience_uses_fresh_visibility_and_identity(metadata,expected,status):
    service,client=audience_service([[{'login':'owner'}]])
    async def fresh(**kwargs): return metadata
    client.repository_metadata=fresh
    result=await service.repository_audience(actor_id='a',workspace_id='w',repository_id=123)
    assert result['owner_only'] is expected and result['status']==status
    assert not client.calls


@pytest.mark.asyncio
async def test_existing_shared_request_is_not_eligible_inbox_and_no_discovery(setup):
    from test_mcp_events import live_fixture, DELIVERY, REQUEST_EVENT
    store,auth,events,receiver,owner,service,bindings=live_fixture(setup)
    await events.subscribe(service['id'],REQUEST_EVENT,{'project_id':owner['project_id']},DELIVERY)
    request=events.create_request(owner['actor_id'],owner['project_id'],'readiness.shared',
        event_name='projects_hub.request.created',binding_id=bindings[0]['id'])
    store.db.execute("INSERT INTO actors VALUES('other','Other',1)")
    store.db.execute("INSERT INTO project_grants VALUES('other',?,'viewer',0,0,1,NULL)",(owner['project_id'],))
    read=await read_request_with_readiness(events,service['id'],owner['project_id'],request['request_id'])
    ready=read['binding_readiness']
    assert ready['owner_only'] is False
    assert len(ready['repositories'])==1 and ready['repositories'][0]['repository_id']==123
    assert ready['repositories'][0]['audience']['owner_only'] is None
    assert 'Other' not in str(ready)
    with pytest.raises(StoreError):
        await read_request_with_readiness(events,service['id'],'another-project',request['request_id'])

@pytest.mark.asyncio
async def test_missing_repository_id_is_unavailable_without_discovery(setup):
    from test_mcp_events import live_fixture, DELIVERY, REQUEST_EVENT
    store,auth,events,receiver,owner,service,bindings=live_fixture(setup)
    await events.subscribe(service['id'],REQUEST_EVENT,{'project_id':owner['project_id']},DELIVERY)
    request=events.create_request(owner['actor_id'],owner['project_id'],'readiness.missing',
        event_name='projects_hub.request.created',binding_id=bindings[0]['id'])
    store.db.execute('UPDATE project_notes SET repository_id=0 WHERE id=?',(bindings[0]['note_id'],))
    read=await read_request_with_readiness(events,service['id'],owner['project_id'],request['request_id'])
    assert read['binding_readiness']['repositories'][0]['state']=='unavailable'


@pytest.mark.asyncio
async def test_metadata_reader_sends_only_scoped_read_requests():
    from projects_hub.github_app import GitHubAppClient
    import httpx
    urls=[]
    def handler(request):
        assert request.method=='GET' and request.headers['authorization']=='Bearer test-only'
        urls.append(str(request.url))
        return httpx.Response(200,json=[] if 'collaborators' in str(request.url) else dict(id=123,full_name='owner/private',private=True))
    client=GitHubAppClient(app_id=1,slug='fixture',private_key='not-used',webhook_secret='not-used',transport=httpx.MockTransport(handler))
    await client.repository_metadata(token='test-only',repository_id=123)
    await client.repository_collaborator_page(token='test-only',full_name='owner/private',page=2)
    assert urls==['https://api.github.com/repositories/123','https://api.github.com/repos/owner/private/collaborators?affiliation=all&per_page=100&page=2']
