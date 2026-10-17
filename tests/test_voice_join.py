import copy
import hashlib
import json

import pytest

from test_mcp_events import setup
from test_voice_analysis import voice_fixture, package
from projects_hub.store import StoreError


def another(payload, number=2):
    other=copy.deepcopy(payload)
    other['source']['path']=f'intake/session-{number}.md'
    other['input_manifest']=[other['source']]
    other['session_id']=f'session-{number}'
    other['intent_id']='voice:'+hashlib.sha256(json.dumps(other['source'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return other


def join_args(owner,payload,donor,**overrides):
    return dict(project_id=owner['project_id'],target_intent_id=payload['intent_id'],
        source_intents=[dict(intent_id=donor['intent_id'],expected_revision=1)] if donor else [],
        supporting_source_refs=[],expected_case_revision=1,command_id='link-voice-command-001',reason='Same lecture, supporting material',**overrides)


@pytest.mark.asyncio
async def test_join_pending_inputs_supersedes_dedups_freezes_and_projects(setup):
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    try:
        first=await bridge.accept(payload); donor=another(payload); second=await bridge.accept(donor)
        old=events.get_project_request(service['id'],owner['project_id'],first['request_id'])
        args=join_args(owner,payload,donor)
        joined=await bridge.joins.link(service['id'],**args)
        assert await bridge.joins.link(service['id'],**args)==joined
        assert joined['case_revision']==2
        for req in (first,second):
            redirected=events.get_project_request(service['id'],owner['project_id'],req['request_id'])
            assert redirected['state']=='superseded' and redirected['canonical_request_id']==joined['request_id']
            with pytest.raises(StoreError,match='canonical request'):
                events.record_request_result(service['id'],owner['project_id'],req['request_id'],req['event_id'],package(old))
        read=events.get_project_request(service['id'],owner['project_id'],joined['request_id'])
        assert len(read['external_source']['input_manifest'])==2
        assert read['service_principal']['client_id']==service['client_id']
        saved=events.record_request_result(service['id'],owner['project_id'],read['request_id'],read['event_id'],package(read))
        scope=store.db.execute('SELECT scope_json FROM collaboration_analyses WHERE id=?',(saved['analysis_id'],)).fetchone()[0]
        assert json.loads(scope)['voice_input_snapshot']['input_manifest_revision']==2
        assert bridge.get(payload['intent_id'])['projection']==bridge.get(donor['intent_id'])['projection']
        assert bridge.get(donor['intent_id'])['source']==donor['source']
        assert old['external_source']['input_manifest']==[payload['source']]
        with pytest.raises(StoreError):
            await bridge.joins.link(service['id'],**{**args,'reason':'different'})
    finally: store.close()


@pytest.mark.asyncio
async def test_join_open_round_preserves_questions_until_explicit_completion(setup):
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    try:
        first=await bridge.accept(payload); donor=another(payload); await bridge.accept(donor)
        read=events.get_project_request(service['id'],owner['project_id'],first['request_id'])
        saved=events.record_request_result(service['id'],owner['project_id'],read['request_id'],read['event_id'],package(read))
        joined=await bridge.joins.link(service['id'],**join_args(owner,payload,donor))
        assert joined['state']=='queued_until_round_completion' and joined['request_id'] is None
        still=events.get_project_request(service['id'],owner['project_id'],first['request_id'])
        assert still['external_source']['input_manifest']==[payload['source']]
        workspace=store.db.execute('SELECT workspace_id FROM projects WHERE id=?',(owner['project_id'],)).fetchone()[0]
        args=dict(actor_id=owner['actor_id'],workspace_id=workspace,analysis_id=saved['analysis_id'])
        analysis.answer_questions(**args,command_id='voice-answer-001',responses=[dict(question_id=saved['question_package']['questions'][0]['id'],disposition='answer',body='Взрослые')])
        complete=analysis.control_question_session(**args,command_id='voice-complete-001',action='complete')
        ready=events.get_project_request(service['id'],owner['project_id'],complete['continuation_request_id'])
        assert len(ready['external_source']['input_manifest'])==2
        assert ready['external_source']['prior_rounds'][0]['answer_receipts']
        result=await events.record_request_result_async(service['id'],owner['project_id'],ready['request_id'],ready['event_id'],dict(kind='analysis_result',source_revision=1,summary='Ready',result_markdown='Combined result',used_receipts=['voice-answer-001']))
        assert result['destinations']['mira_state']=='available'
        assert 'Combined result' in bridge.get(donor['intent_id'])['projection']['markdown']
    finally: store.close()


@pytest.mark.asyncio
async def test_supporting_untriggered_source_and_history_conflicts(setup):
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    try:
        first=await bridge.accept(payload)
        source=another(payload)['source']
        text='Продолжение той же лекции без команды.'
        source['transcript_sha256']=hashlib.sha256(text.encode()).hexdigest()
        original=repo.read_repository_path
        async def read(**kwargs):
            if kwargs.get('path')==source['path']:
                return dict(kind='file',text='\n## Полная расшифровка\n\n'+text+'\n\n## Техническая фиксация\nreceipt',full_name='owner/intake')
            return await original(**kwargs)
        repo.read_repository_path=read
        args=join_args(owner,payload,None); args['supporting_source_refs']=[source]
        joined=await bridge.joins.link(service['id'],**args)
        assert len(joined['input_manifest'])==2
        assert store.db.execute('SELECT count(*) FROM voice_analysis_intents').fetchone()[0]==1
        donor=another(payload,3); accepted=await bridge.accept(donor)
        d=events.get_project_request(service['id'],owner['project_id'],accepted['request_id'])
        events.record_request_result(service['id'],owner['project_id'],d['request_id'],d['event_id'],package(d,'independent'))
        args=join_args(owner,payload,donor);args.update(expected_case_revision=2,command_id='link-conflicting-history')
        with pytest.raises(StoreError,match='independent question history'):
            await bridge.joins.link(service['id'],**args)
        assert bridge.joins.case(payload['intent_id'])[1]['revision']==2
        args['source_intents']=[];args['supporting_source_refs']=[{**source,'path':'intake/../other.md'}]
        with pytest.raises(StoreError,match='namespace'):
            await bridge.joins.link(service['id'],**args)
    finally: store.close()


def test_join_tool_schema_is_exposed():
    from projects_hub.mcp_api import TOOLS
    tool=next(t for t in TOOLS if t['name']=='link_voice_analysis_sources')
    assert tool['inputSchema']['additionalProperties'] is False
    assert 'source_intents' in tool['inputSchema']['required']


@pytest.mark.asyncio
async def test_join_during_ready_result_defers_new_round_and_preserves_provenance(setup):
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    try:
        first=await bridge.accept(payload)
        read=events.get_project_request(service['id'],owner['project_id'],first['request_id'])
        saved=events.record_request_result(service['id'],owner['project_id'],read['request_id'],read['event_id'],package(read))
        workspace=store.db.execute('SELECT workspace_id FROM projects WHERE id=?',(owner['project_id'],)).fetchone()[0]
        args=dict(actor_id=owner['actor_id'],workspace_id=workspace,analysis_id=saved['analysis_id'])
        analysis.answer_questions(**args,command_id='voice-answer-001',responses=[dict(question_id=saved['question_package']['questions'][0]['id'],disposition='answer',body='Взрослые')])
        complete=analysis.control_question_session(**args,command_id='voice-complete-001',action='complete')
        ready=events.get_project_request(service['id'],owner['project_id'],complete['continuation_request_id'])
        donor=another(payload); await bridge.accept(donor)
        joined=await bridge.joins.link(service['id'],**join_args(owner,payload,donor))
        assert joined['state']=='queued_until_round_completion'
        result=await events.record_request_result_async(service['id'],owner['project_id'],ready['request_id'],ready['event_id'],dict(kind='analysis_result',source_revision=1,summary='Original result',result_markdown='Original source only',used_receipts=['voice-answer-001']))
        assert result['result']['provenance']['voice_input_snapshot']['input_manifest_revision']==1
        next_request=store.db.execute("SELECT id FROM mcp_test_requests WHERE binding_id=? AND state='awaiting_response'",(read['binding_id'],)).fetchone()[0]
        following=events.get_project_request(service['id'],owner['project_id'],next_request)
        assert following['request_kind']=='question_start'
        assert not bridge.get(payload['intent_id'])['projection_terminal']
        assert following['external_source']['input_manifest_revision']==2
        assert following['external_source']['prior_rounds'][0]['state']=='completed'
        assert following['external_source']['prior_rounds'][0]['result_markdown']=='Original source only'
        assert len(following['external_source']['input_manifest'])==2
        assert events.get_project_request(service['id'],owner['project_id'],ready['request_id'])['external_source']['input_manifest_revision']==1
        # Lost-result retries do not create additional review rounds.
        await events.record_request_result_async(service['id'],owner['project_id'],ready['request_id'],ready['event_id'],dict(kind='analysis_result',source_revision=1,summary='Original result',result_markdown='Original source only',used_receipts=['voice-answer-001']))
        assert store.db.execute("SELECT count(*) FROM mcp_test_requests WHERE binding_id=? AND state='awaiting_response'",(read['binding_id'],)).fetchone()[0]==1
    finally: store.close()


def test_join_through_authenticated_mcp_tools_call(setup,tmp_path):
    from fastapi.testclient import TestClient
    from projects_hub.app import create_app
    from projects_hub.settings import Settings
    from test_mcp_events import rpc, ORIGIN, edge_headers
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    token=events.auth.provision_tunnel(owner['actor_id'],'ph_service_voice_join_http')['access_token']
    service=events.auth.authenticate('Bearer '+token)
    bridge.config['client_id']=service['client_id']
    settings=Settings(data_dir=tmp_path/'unused',static_dir=tmp_path/'static',session_secret='s'*48,
                      public_origin=ORIGIN,voice_analysis_config=bridge.config)
    app=create_app(settings,store=store,collaboration=bridge.collaboration,collaboration_analysis=analysis)
    with TestClient(app,base_url=ORIGIN) as client:
        headers={**edge_headers(),'authorization':'Bearer '+bridge.config['token']}
        path='/api/integrations/voice-analysis/intents'
        assert client.post(path,json=payload,headers=headers).status_code==200
        donor=another(payload)
        assert client.post(path,json=donor,headers=headers).status_code==200
        args=join_args(owner,payload,donor)
        response=rpc(client,token,'tools/call',{'name':'link_voice_analysis_sources','arguments':args})
        assert response.status_code==200
        result=response.json()['result']
        assert not result.get('isError'),result
        assert result['structuredContent']['case_revision']==2
        request=rpc(client,token,'tools/call',{'name':'get_project_request','arguments':{
            'project_id':owner['project_id'],'request_id':result['structuredContent']['request_id']}}).json()['result']['structuredContent']
        assert len(request['external_source']['input_manifest'])==2
        assert request['service_principal']['client_id']==service['client_id']


@pytest.mark.asyncio
async def test_linked_source_restart_redrive_and_stale_revision(setup):
    from projects_hub.voice_analysis import VoiceAnalysis
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    try:
        await bridge.accept(payload); donor=another(payload); await bridge.accept(donor)
        joined=await bridge.joins.link(service['id'],**join_args(owner,payload,donor))
        bridge=VoiceAnalysis(events,bridge.collaboration,bridge.config)
        assert bridge.get(donor['intent_id'])['canonical_intent_id']==payload['intent_id']
        store.db.execute('UPDATE mcp_test_requests SET expires_ms=0 WHERE id=?',(joined['request_id'],))
        assert bridge.get(donor['intent_id'])['needs_redrive']
        await bridge.accept(donor)
        assert not bridge.get(donor['intent_id'])['needs_redrive']
        assert store.db.execute("SELECT count(*) FROM mcp_test_requests WHERE state='awaiting_response'").fetchone()[0]==1
        third=another(payload,3); await bridge.accept(third)
        args=join_args(owner,payload,third);args['command_id']='stale-case-command'
        with pytest.raises(StoreError,match='current revision'):
            await bridge.joins.link(service['id'],**args)
        assert bridge.get(third['intent_id'])['canonical_intent_id']==third['intent_id']
    finally: store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal_state',['completed','cancelled'])
async def test_direct_voice_result_without_questions_and_linked_review(setup,terminal_state):
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    try:
        accepted=await bridge.accept(payload)
        read=events.get_project_request(service['id'],owner['project_id'],accepted['request_id'])
        assert 'analysis_result' in read['allowed_result_kinds']
        result=dict(kind='analysis_result',source_revision=1,summary='Enough information',result_markdown='## Решения\nПервая версия.',used_receipts=[])
        saved=await events.record_request_result_async(service['id'],owner['project_id'],read['request_id'],read['event_id'],result)
        assert saved['destinations']['mira_state']=='available'
        assert saved['question_package']['package_state']=='completed'
        assert saved['question_package']['scope']['direct_voice_result']
        assert bridge.get(payload['intent_id'])['projection_terminal']
        assert bridge.get(payload['intent_id'])['input_revision']==1
        assert store.db.execute('SELECT count(*) FROM collaboration_questions').fetchone()[0]==0
        assert await events.record_request_result_async(service['id'],owner['project_id'],read['request_id'],read['event_id'],result)==saved
        store.db.execute('UPDATE collaboration_analyses SET package_state=? WHERE id=?',(terminal_state,saved['analysis_id']))
        donor=another(payload);await bridge.accept(donor)
        joined=await bridge.joins.link(service['id'],**join_args(owner,payload,donor))
        assert not bridge.get(payload['intent_id'])['projection_terminal']
        assert not bridge.get(donor['intent_id'])['projection_terminal']
        assert set(bridge.get(donor['intent_id'])['member_intent_ids'])=={payload['intent_id'],donor['intent_id']}
        following=events.get_project_request(service['id'],owner['project_id'],joined['request_id'])
        updated=await events.record_request_result_async(service['id'],owner['project_id'],following['request_id'],following['event_id'],{**result,'summary':'New combined result','result_markdown':'Вторая версия, оба источника.'})
        assert updated['analysis_id']!=saved['analysis_id']
        assert len(updated['result']['provenance']['voice_input_snapshot']['input_manifest'])==2
        assert store.db.execute('SELECT count(*) FROM collaboration_questions').fetchone()[0]==0
        assert 'Вторая версия' in bridge.get(donor['intent_id'])['projection']['markdown']
        assert bridge.get(donor['intent_id'])['projection_terminal']
        assert bridge.get(donor['intent_id'])['case_id']==payload['intent_id']
        assert bridge.get(donor['intent_id'])['input_revision']==2
        assert store.db.execute('SELECT result_markdown FROM collaboration_analyses WHERE id=?',(saved['analysis_id'],)).fetchone()[0]==result['result_markdown']
    finally: store.close()


@pytest.mark.asyncio
async def test_direct_voice_result_requires_prior_receipts_and_safe_publication_retry(setup):
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    try:
        accepted=await bridge.accept(payload)
        read=events.get_project_request(service['id'],owner['project_id'],accepted['request_id'])
        saved=events.record_request_result(service['id'],owner['project_id'],read['request_id'],read['event_id'],package(read))
        workspace=store.db.execute('SELECT workspace_id FROM projects WHERE id=?',(owner['project_id'],)).fetchone()[0]
        args=dict(actor_id=owner['actor_id'],workspace_id=workspace,analysis_id=saved['analysis_id'])
        analysis.answer_questions(**args,command_id='voice-answer-001',responses=[dict(question_id=saved['question_package']['questions'][0]['id'],disposition='answer',body='Взрослые')])
        complete=analysis.control_question_session(**args,command_id='voice-complete-001',action='complete')
        ready=events.get_project_request(service['id'],owner['project_id'],complete['continuation_request_id'])
        result=dict(kind='analysis_result',source_revision=1,summary='Original',result_markdown='Original',used_receipts=['voice-answer-001'])
        await events.record_request_result_async(service['id'],owner['project_id'],ready['request_id'],ready['event_id'],result)
        donor=another(payload);await bridge.accept(donor)
        joined=await bridge.joins.link(service['id'],**join_args(owner,payload,donor))
        next=events.get_project_request(service['id'],owner['project_id'],joined['request_id'])
        with pytest.raises(StoreError,match='prior answer receipts'):
            await events.record_request_result_async(service['id'],owner['project_id'],next['request_id'],next['event_id'],{**result,'used_receipts':[]})
        write=repo.write_repository_text
        async def fail(**kwargs):
            raise StoreError('TEMPORARY_FAILURE','Repository unavailable')
        repo.write_repository_text=fail
        with pytest.raises(StoreError,match='unavailable'):
            await events.record_request_result_async(service['id'],owner['project_id'],next['request_id'],next['event_id'],result)
        with pytest.raises(StoreError,match='already reserved'):
            events.record_request_result(service['id'],owner['project_id'],next['request_id'],next['event_id'],package(next,'late-questions'))
        with pytest.raises(StoreError,match='immutable result'):
            await events.record_request_result_async(service['id'],owner['project_id'],next['request_id'],next['event_id'],{**result,'summary':'Changed'})
        repo.write_repository_text=write
        final=await events.record_request_result_async(service['id'],owner['project_id'],next['request_id'],next['event_id'],result)
        assert final['destinations']['mira_state']=='available'
        assert final['result']['used_receipts']==['voice-answer-001']
        assert store.db.execute('SELECT count(*) FROM collaboration_analyses').fetchone()[0]==2
    finally: store.close()


@pytest.mark.asyncio
async def test_disabled_join_and_foreign_service_fail_closed(setup):
    from projects_hub.voice_analysis import VoiceAnalysis
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    try:
        await bridge.accept(payload); donor=another(payload);await bridge.accept(donor)
        args=join_args(owner,payload,donor)
        other=events.auth.provision_tunnel(owner['actor_id'],'ph_service_unbound_voice')
        principal=events.auth.authenticate('Bearer '+other['access_token'])
        with pytest.raises(StoreError):
            await bridge.joins.link(principal['id'],**args)
        off=VoiceAnalysis(events,bridge.collaboration,{})
        with pytest.raises(StoreError,match='not configured'):
            await off.joins.link(service['id'],**args)
        assert store.db.execute('SELECT count(*) FROM voice_case_versions WHERE revision>1').fetchone()[0]==0
    finally: store.close()


@pytest.mark.asyncio
async def test_completed_support_only_reopen_requires_active_triggered_donor(setup):
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    try:
        accepted=await bridge.accept(payload)
        read=events.get_project_request(service['id'],owner['project_id'],accepted['request_id'])
        await events.record_request_result_async(service['id'],owner['project_id'],read['request_id'],read['event_id'],dict(kind='analysis_result',source_revision=1,summary='Done',result_markdown='Done',used_receipts=[]))
        supporting=another(payload)['source']
        text='Дополнительные сведения без команды.'
        supporting['transcript_sha256']=hashlib.sha256(text.encode()).hexdigest()
        original_read=repo.read_repository_path
        async def read_support(**kwargs):
            if kwargs.get('path')==supporting['path']:
                return dict(kind='file',text='\n## Полная расшифровка\n\n'+text+'\n\n## Техническая фиксация\nreceipt',full_name='owner/intake')
            return await original_read(**kwargs)
        repo.read_repository_path=read_support
        args=join_args(owner,payload,None);args['supporting_source_refs']=[supporting]
        with pytest.raises(StoreError,match='new accepted triggered voice intent') as error:
            await bridge.joins.link(service['id'],**args)
        assert error.value.code=='VOICE_ACTIVE_TRIGGER_REQUIRED'
        assert bridge.get(payload['intent_id'])['projection_terminal']
        assert bridge.get(payload['intent_id'])['input_revision']==1
        donor=another(payload,3);await bridge.accept(donor)
        args['source_intents']=[dict(intent_id=donor['intent_id'],expected_revision=1)]
        joined=await bridge.joins.link(service['id'],**args)
        assert joined['state']=='review_requested' and len(joined['input_manifest'])==3
        assert not bridge.get(payload['intent_id'])['projection_terminal']
    finally: store.close()


@pytest.mark.asyncio
async def test_support_intent_accept_race_must_not_create_two_analyses(setup):
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    try:
        await bridge.accept(payload)
        donor=another(payload)
        original=bridge._source
        injected=False
        async def concurrent(source,workspace):
            nonlocal injected
            if source==donor['source'] and not injected:
                injected=True
                await bridge.accept(donor)
            return await original(source,workspace)
        bridge._source=concurrent
        args=join_args(owner,payload,None)
        args['supporting_source_refs']=[donor['source']]
        from projects_hub.store import StoreError
        with pytest.raises(StoreError) as error:
            await bridge.joins.link(service['id'],**args)
    finally: store.close()


@pytest.mark.asyncio
async def test_late_trigger_intake_cannot_follow_a_support_only_link(setup):
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    try:
        await bridge.accept(payload)
        donor=another(payload)
        args=join_args(owner,payload,None);args['supporting_source_refs']=[donor['source']]
        with pytest.raises(StoreError) as error:
            await bridge.joins.link(service['id'],**args)
        assert error.value.code=='VOICE_TRIGGER_ACCEPTANCE_REQUIRED'
        assert bridge.get(payload['intent_id'])['input_revision']==1
        await bridge.accept(donor)
        joined=await bridge.joins.link(service['id'],**join_args(owner,payload,donor))
        assert bridge.get(donor['intent_id'])['case_id']==payload['intent_id']
        assert store.db.execute("SELECT count(*) FROM mcp_test_requests WHERE state='awaiting_response'").fetchone()[0]==1
        assert joined['case_revision']==2
    finally: store.close()


@pytest.mark.asyncio
async def test_declined_false_trigger_can_be_context_without_losing_disposition(setup):
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    try:
        await bridge.accept(payload)
        donor=another(payload);accepted=await bridge.accept(donor)
        read=events.get_project_request(service['id'],owner['project_id'],accepted['request_id'])
        declined=events.record_request_result(service['id'],owner['project_id'],read['request_id'],read['event_id'],package(read,'quoted-candidate',questions=False))
        original=store.db.execute('SELECT scope_json,result_json FROM collaboration_analyses WHERE id=?',(declined['analysis_id'],)).fetchone()
        assert bridge.get(donor['intent_id'])['projection_terminal']
        joined=await bridge.joins.link(service['id'],**join_args(owner,payload,donor))
        canonical=events.get_project_request(service['id'],owner['project_id'],joined['request_id'])
        assert canonical['external_source']['prior_rounds'][0]['analysis_id']==declined['analysis_id']
        assert 'цитата' in canonical['external_source']['prior_rounds'][0]['result_summary']
        retained=store.db.execute('SELECT scope_json,result_json FROM collaboration_analyses WHERE id=?',(declined['analysis_id'],)).fetchone()
        assert tuple(retained)==tuple(original)
        assert store.db.execute('SELECT count(*) FROM collaboration_questions').fetchone()[0]==0
    finally: store.close()
