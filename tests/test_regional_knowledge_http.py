"""Real HTTP codec and owner launch wiring; no provider/live service/grant effects."""
import asyncio
import base64
import json
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from projects_hub.regional_knowledge import (KNOWLEDGE_CODEC,KnowledgeReadContext,RegionalKnowledgeAdapter,
    RegionalKnowledgeContractError,RegionalKnowledgeAccessError,normalize_knowledge_search)
from projects_hub.regional_knowledge_http import (Delegation,RegionalKnowledgeHttpProvider,
    RegionalKnowledgeFactory,configured_knowledge_factory,failure)
from projects_hub.store import DurableStore
from test_regional_knowledge import v2,KnowledgeProvider,_live_context
from live_tools import execute

URL='http://127.0.0.1:8199/internal/projects-hub/knowledge-search'
CREDENTIAL='00000000-0000-0000-0000-000000000099'
TOKEN='rkbs_'+CREDENTIAL.replace('-','')+'.'+('x'*43)


def request_context():return KnowledgeReadContext(uuid4().hex,7,time.monotonic()+2,3)


def provider(client,*,current=lambda:True):
    return RegionalKnowledgeHttpProvider(endpoint=URL,service_id='projects-hub-reader',provider_tenant='ph.fixture',
        token=TOKEN,credential_id=CREDENTIAL,credential_epoch=1,binding=Delegation('usr_test','ws_test',str(uuid4()),1),current=current,client=client)


def ready(context):return v2({'mode':'hybrid','evidence':[dict(id='rkb-source:'+('a'*500),title='Source',
    text=' Exact source text.\n',url='urn:rkb:passage:00000000-0000-0000-0000-000000000001',
    metadata={'source_revisions':[{'source_id':'source','revision':1}]})]},context)


@pytest.mark.asyncio
async def test_http_provider_uses_only_dedicated_service_and_exact_bound_actor_context():
    calls=[];context=request_context()
    def handler(request):
        body=json.loads(request.content);calls.append((request,body))
        return httpx.Response(200,json=ready(context))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        p=provider(client)
        result=await RegionalKnowledgeAdapter(p,'usr_test','ws_test').search('exact query',context=context)
        assert result['status']=='ok' and result['evidence'][0]['text']==' Exact source text.\n'
        request,body=calls[0]
        assert request.headers['authorization']=='Bearer '+TOKEN and 'cookie' not in request.headers
        assert request.headers['x-rkb-service-credential']==CREDENTIAL and request.headers['x-rkb-service-epoch']=='1'
        assert request.url.host=='127.0.0.1' and body['provider_tenant']=='ph.fixture'
        assert body['request_id']==context.request_id and body['turn_generation']==7
        assert body['deadline_monotonic']==context.deadline_monotonic and body['min_data_seq']==3
        assert body['actor_sub']=='usr_test' and 'person_id' not in body and 'access_token' not in body
        with pytest.raises(RegionalKnowledgeAccessError):
            await p.search_evidence(query='x',actor_sub='usr_other',workspace_id='ws_test',max_evidence=3,context=context)
        assert len(calls)==1


@pytest.mark.asyncio
async def test_timeout_is_typed_unavailable_and_not_retried():
    calls=[];context=request_context()
    async def handler(request):calls.append(request);raise httpx.ReadTimeout('synthetic private failure')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        value=await RegionalKnowledgeAdapter(provider(client),'usr_test','ws_test').search('x',context=context)
        assert value['status']=='unavailable' and value['reason']=='deadline_exceeded'
        assert value['mode'] is None and value['evidence']==[] and len(calls)==1


@pytest.mark.asyncio
async def test_late_generation_or_revoked_local_scope_withholds_evidence():
    context=request_context();current=[True]
    def handler(request):
        current[0]=False
        return httpx.Response(200,json=ready(context))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RegionalKnowledgeAccessError):
            await RegionalKnowledgeAdapter(provider(client,current=lambda:current[0]),'usr_test','ws_test').search('x',context=context)
    def stale(request):return httpx.Response(200,json={**ready(context),'turn_generation':6})
    async with httpx.AsyncClient(transport=httpx.MockTransport(stale)) as client:
        with pytest.raises(Exception,match='context_gap'):
            await RegionalKnowledgeAdapter(provider(client),'usr_test','ws_test').search('x',context=context)


@pytest.mark.parametrize('case',['empty_success_failure','stale','missing_reason','nan','oversize','v1'])
def test_v2_cannot_collapse_failures_or_freshness_into_empty_success(case):
    context=request_context();value=ready(context)
    if case=='empty_success_failure':value.update(status='unavailable',reason='not_ready',evidence=[])
    if case=='stale':value['observed_projection_seq']=2
    if case=='missing_reason':value.update(status='degraded',reason=None)
    if case=='nan':value['evidence'][0]['metadata']['bad']=float('nan')
    if case=='oversize':value['evidence'][0]['text']=' '*8001+'x'
    if case=='v1':value={'mode':'hybrid','evidence':[]}
    with pytest.raises(RegionalKnowledgeContractError):normalize_knowledge_search(value,max_evidence=3)


def private(path,data):path.write_text(data);path.chmod(0o600);return path


def issued(workspace):
    return json.dumps(dict(codec='rkb.service-credential.v1',credential_id=CREDENTIAL,epoch=1,service_id='projects-hub-reader',
        service_token=TOKEN,scope=dict(operation='knowledge_search',provider='projects_hub',provider_tenant='ph.fixture',workspace_ids=[workspace]),
        expires_at=time.time()+86400,reason='synthetic service scope'))


@pytest.mark.asyncio
async def test_real_optional_factory_is_scoped_revocable_and_constructs_without_network(tmp_path,monkeypatch):
    store=DurableStore(tmp_path/'state');boot=store.ensure_dev_workspace('Synthetic')
    actor,workspace=boot['actor']['id'],boot['workspace']['id']
    secret=private(tmp_path/'service-token',issued(workspace))
    value=dict(codec='ph.rkb-provider.v2',endpoint=URL,service_id='projects-hub-reader',provider_tenant='ph.fixture',
        service_token_file=str(secret),service_credential_id=CREDENTIAL,service_credential_epoch=1,bindings=[dict(actor_sub=actor,workspace_id=workspace,link_id=str(uuid4()),link_revision=1)])
    config=private(tmp_path/'rkb.json',json.dumps(value))
    factory=configured_knowledge_factory(store,{'PROJECTS_HUB_RKB_CONFIG':str(config)})
    try:
        assert type(factory) is RegionalKnowledgeFactory
        assert factory(actor,workspace) is factory(actor,workspace)
        assert factory('usr_other',workspace) is None
        secret.write_text('revoked-'+TOKEN)
        assert factory(actor,workspace) is None
        async with httpx.AsyncClient() as unused:
            with pytest.raises(ValueError):provider(unused,current=None)
    finally:
        await factory.aclose();store.close()


@pytest.mark.asyncio
async def test_tool_lock_wait_budget_and_cancellation_do_not_change_utterance_commit(tmp_path):
    p=KnowledgeProvider();holder={}
    def factory(actor,workspace):return RegionalKnowledgeAdapter(p,actor,workspace)
    store,actor,workspace,adapter,initialized,session=_live_context(tmp_path,factory)
    session.id='session-fixture';session.tool_feedback_generation=4;session.audio_turn_generation=1
    try:
        adapter.on_event(session,{'type':'tool_call','calls':[{'name':'knowledge_search','id':'expired'}]})
        started,generation,utt=session.state['_knowledge_call_contexts']['expired']
        session.state['_knowledge_call_contexts']['expired']=(started-3,generation,utt)
        with pytest.raises(Exception,match='deadline'):
            await execute(adapter,session,{'id':'expired','name':'knowledge_search','args':{'query':'x'}})
        assert not p.calls
        # This mandatory voice commitment remains the existing path even when
        # the optional downstream search has already missed its deadline.
        assert any(value.get('committed') for value in session.state['_utterances'])
    finally:store.close()


@pytest.mark.asyncio
async def test_new_voice_turn_cancels_optional_read_and_keeps_original_utterance(tmp_path):
    entered=asyncio.Event();cancelled=asyncio.Event()
    class Slow(KnowledgeProvider):
        async def search_evidence(self,**kwargs):
            entered.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
    p=Slow()
    store,actor,workspace,adapter,initialized,session=_live_context(tmp_path,lambda actor,workspace:RegionalKnowledgeAdapter(p,actor,workspace))
    session.id='session-fixture';session.tool_feedback_generation=3;session.audio_turn_generation=1
    try:
        adapter.input(session,{'activity_start':True})
        utterance=session.state['_utterances'][-1]
        adapter.input(session,{'audio_base64':base64.b64encode(b'\x01\x00'*320).decode()})
        adapter.input(session,{'activity_end':True})
        session.audio_end_sent_generation=1
        adapter.on_event(session,{'type':'tool_call','calls':[{'name':'knowledge_search','id':'slow'}]})
        task=asyncio.create_task(execute(adapter,session,{'id':'slow','name':'knowledge_search','args':{'query':'source'}}))
        await asyncio.wait_for(entered.wait(),1)
        adapter.input(session,{'activity_start':True})
        result=await task
        assert cancelled.is_set() and result['status']=='cancelled' and result['evidence']==[]
        assert utterance['committed'] is True
        assert session.state['_utterances'][-1]['id']!=utterance['id']
    finally:store.close()


@pytest.mark.asyncio
async def test_computed_evidence_checkpoint_has_exact_committed_utterance_and_is_not_an_ack(tmp_path):
    from projects_hub.regional_knowledge_context import checkpoint
    p=KnowledgeProvider()
    store,actor,workspace,adapter,initialized,session=_live_context(tmp_path,lambda actor,workspace:RegionalKnowledgeAdapter(p,actor,workspace))
    session.id='session-fixture';session.tool_feedback_generation=3;session.audio_turn_generation=1
    try:
        adapter.input(session,{'activity_start':True});utterance=session.state['_utterances'][-1]
        adapter.input(session,{'audio_base64':base64.b64encode(b'\x01\x00'*320).decode()})
        adapter.input(session,{'activity_end':True});session.audio_end_sent_generation=1
        adapter.on_event(session,{'type':'tool_call','calls':[{'name':'knowledge_search','id':'exact'}]})
        result=await execute(adapter,session,{'id':'exact','name':'knowledge_search','args':{'query':'source'}})
        journal=store._knowledge_context_journal
        await journal.task
        rows=store.source_events(actor,utterance['source_id'])
        payloads=[json.loads(row['text']) for row in rows if row['kind']=='knowledge_context_v2']
        assert len(payloads)==1
        saved=payloads[0]
        assert saved['utterance_id']==utterance['id'] and saved['turn_generation']==3
        assert saved['disposition']=='computed' and saved['tool_call_id']=='exact'
        assert saved['evidence_refs'][0]['id']==result['evidence'][0]['id']
        assert 'audible' not in saved and 'transport_enqueued' not in saved
        orphan=dict(utterance,id='never-committed-exact-turn')
        assert checkpoint(store,session=session,context=request_context(),tool_call_id='orphan',utterance=orphan,query='x',result=result)
        await journal.task
        assert len([row for row in store.source_events(actor,utterance['source_id']) if row['kind']=='knowledge_context_v2'])==1
        assert initialized['context']['regional_knowledge_context']['status']=='context_gap'
        await journal.aclose()
    finally:store.close()


def test_actual_create_app_uses_optional_private_configuration(tmp_path,monkeypatch):
    from projects_hub.app import create_app
    store=DurableStore(tmp_path/'state');boot=store.ensure_dev_workspace('Synthetic')
    secret=private(tmp_path/'token',issued(boot['workspace']['id']))
    config=private(tmp_path/'config',json.dumps(dict(codec='ph.rkb-provider.v2',endpoint=URL,
        service_id='projects-hub-reader',provider_tenant='ph.fixture',service_token_file=str(secret),service_credential_id=CREDENTIAL,service_credential_epoch=1,
        bindings=[dict(actor_sub=boot['actor']['id'],workspace_id=boot['workspace']['id'],link_id=str(uuid4()),link_revision=1)])))
    monkeypatch.setenv('PROJECTS_HUB_RKB_CONFIG',str(config))
    from projects_hub.settings import Settings
    settings=Settings.from_env({'PROJECTS_HUB_SESSION_SECRET':'synthetic-session-'+('x'*40),'PROJECTS_HUB_DATA_DIR':str(tmp_path/'state')})
    app=create_app(settings,store=store,live_host=SimpleNamespace())
    assert type(app.state.regional_knowledge_factory) is RegionalKnowledgeFactory
    assert app.state.regional_knowledge_factory(boot['actor']['id'],boot['workspace']['id']) is not None
    # No HTTP calls occur during startup construction. Close the unused pool.
    asyncio.run(app.state.regional_knowledge_factory.aclose());store.close()
