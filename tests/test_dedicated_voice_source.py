import base64
import copy
import hashlib
import pytest
from test_mcp_events import setup
from test_voice_analysis import voice_fixture
from projects_hub.github_connections import GitHubConnections
from projects_hub.github_app import GitHubAppClient, GitHubAppError
from projects_hub.store import StoreError


def reader_fixture(setup):
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    connection=dict(repository_id=321,full_name='owner/intake',installation_id=7,project_id=None,
        role='unassigned',access_mode='read_only',allowed_paths=[],state='available',installation_state='active')
    store.get_repository_connection=lambda *a:copy.deepcopy(connection)
    store.github_installation=lambda *a:dict(installation_id=7,account_id=9,account_login='owner',account_type='User',state='active')
    class Client:
        def __init__(self): self.calls=[]; self.private=True; self.others=False; self.after_read=None
        async def installation_token(self,*a,**kw):
            self.calls.append(kw)
            assert kw['repository_ids']==[321]
            return 'fixture-token'
        async def repository_metadata(self,**kw):
            return dict(id=321,full_name='owner/intake',private=self.private)
        async def repository_collaborator_page(self,**kw): return [{'login':'other' if self.others else 'owner'}]
        async def repository_immutable_text(self,**kw):
            assert kw['path']==payload['source']['path'] and kw['commit_sha']=='c'*40
            if self.after_read: self.after_read()
            return dict(kind='file',text=repo.source,sha='a'*40,commit_sha=kw['commit_sha'])
    github=object.__new__(GitHubConnections)
    github.store,github.client=store,Client()
    bridge.collaboration.github=github
    return store,events,owner,service,analysis,bridge,payload,connection,github


@pytest.mark.asyncio
async def test_approved_unassigned_read_without_role_mutation_and_generic_denied(setup):
    store,events,owner,service,analysis,bridge,payload,connection,github=reader_fixture(setup)
    text=await bridge._source(payload['source'],bridge._access())
    assert text=='Идея лекции. задай мне вопросы'
    assert connection['role']=='unassigned' and connection['allowed_paths']==[]
    assert [x['permissions'] for x in github.client.calls]==[{'metadata':'read'},{'contents':'read'}]
    with pytest.raises(StoreError) as error:
        await github.read_repository_path(actor_id=owner['actor_id'],workspace_id=bridge._access(),repository_id=321,
            path=payload['source']['path'],ref='c'*40)
    assert error.value.code=='GITHUB_REPOSITORY_UNBOUND'
    await analysis.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[('path','intake/../secret'),('path','intake/%2e%2e/secret'),
    ('path','outside/note.md'),('repository','other/repo'),('commit_sha','main'),('transcript_sha256','bad')])
async def test_source_namespace_and_immutable_reference_rejected_before_network(setup,field,value):
    store,events,owner,service,analysis,bridge,payload,connection,github=reader_fixture(setup)
    with pytest.raises(StoreError):
        await bridge._source({**payload['source'],field:value},bridge._access())
    assert not github.client.calls
    await analysis.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['public','shared','unknown','hash','client','changed_config','changed_installation','project_revoked','client_revoked_after'])
async def test_audience_hash_and_revocation_fail_closed(setup,failure):
    store,events,owner,service,analysis,bridge,payload,connection,github=reader_fixture(setup)
    if failure=='public': github.client.private=False
    elif failure=='shared': github.client.others=True
    elif failure=='unknown':
        async def denied(**kw): raise GitHubAppError('GITHUB_FORBIDDEN','fixture',status=403)
        github.client.repository_metadata=denied
    elif failure=='hash': payload['source']['transcript_sha256']='0'*64
    elif failure=='client':
        store.db.execute('UPDATE mcp_service_clients SET enabled=0 WHERE client_id=?',(service['client_id'],))
    elif failure=='changed_config': github.client.after_read=lambda:bridge.config.update(source_prefix='other')
    elif failure=='changed_installation': github.client.after_read=lambda:connection.update(installation_id=99)
    elif failure=='project_revoked': github.client.after_read=lambda:store.db.execute('UPDATE project_grants SET revoked_at_ms=1 WHERE project_id=? AND actor_id=?',(owner['project_id'],owner['actor_id']))
    elif failure=='client_revoked_after': github.client.after_read=lambda:store.db.execute('UPDATE mcp_service_clients SET enabled=0 WHERE client_id=?',(service['client_id'],))
    with pytest.raises(StoreError): await bridge._source(payload['source'],bridge._access())
    assert connection['role']=='unassigned'
    await analysis.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['100644','120000','160000','parent_symlink','bad_blob'])
async def test_git_tree_reader_rejects_symlink_submodule_and_blob_mismatch(mode):
    client=object.__new__(GitHubAppClient)
    raw='## Реальная заметка'.encode()
    blob=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
    async def request(method,path,**kwargs):
        assert method=='GET'
        if '/commits/' in path: return dict(sha='c'*40,tree=dict(sha='d'*40))
        if path.endswith('/trees/'+'d'*40):
            return dict(sha='d'*40,truncated=False,tree=[dict(path='intake',type='tree',mode='120000' if mode=='parent_symlink' else '040000',sha='e'*40)])
        if path.endswith('/trees/'+'e'*40):
            return dict(sha='e'*40,truncated=False,tree=[dict(path='note.md',type='blob',mode=mode if mode in ('100644','120000','160000') else '100644',sha=blob)])
        assert path.endswith('/blobs/'+blob)
        return dict(sha=blob,size=len(raw),encoding='base64',content=base64.b64encode(b'x'*len(raw) if mode=='bad_blob' else raw).decode())
    client._json_request=request
    if mode=='100644':
        result=await client.repository_immutable_text(token='fixture',full_name='owner/repo',path='intake/note.md',commit_sha='c'*40)
        assert result['text']==raw.decode() and result['commit_sha']=='c'*40
    else:
        with pytest.raises(GitHubAppError):
            await client.repository_immutable_text(token='fixture',full_name='owner/repo',path='intake/note.md',commit_sha='c'*40)

@pytest.mark.asyncio
async def test_dedicated_readiness_audit_without_operational_binding(setup):
    store,events,owner,service,analysis,bridge,payload,connection,github=reader_fixture(setup)
    result=await github.voice_source_audience(capability=bridge.source_capability)
    assert result['owner_only'] is True and connection['role']=='unassigned'
    assert github.client.calls==[dict(repository_ids=[321],permissions={'metadata':'read'})]
    github.client.calls.clear()
    assert (await github.repository_audience(actor_id=owner['actor_id'],workspace_id=bridge._access(),repository_id=321))['owner_only'] is None
    assert not github.client.calls
    await analysis.close()

@pytest.mark.asyncio
async def test_supporting_refs_share_frozen_namespace_and_cannot_escape(setup):
    store,events,owner,service,analysis,bridge,payload,connection,github=reader_fixture(setup)
    supporting={**payload['source'],'path':'intake/support.md'}
    candidate={**payload,'input_manifest':[payload['source'],supporting]}
    bridge.validate(candidate)
    bridge.source_capability.authorize(supporting)
    outside={**supporting,'path':'other/support.md'}
    with pytest.raises(StoreError): bridge.validate({**candidate,'input_manifest':[payload['source'],outside]})
    with pytest.raises(StoreError): bridge.source_capability.authorize(outside)
    assert not github.client.calls
    await analysis.close()
