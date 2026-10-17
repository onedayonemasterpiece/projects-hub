import hashlib
import json

import pytest

from test_mcp_events import setup, live_fixture, DELIVERY, REQUEST_EVENT
from projects_hub.collaboration import CollaborationService
from projects_hub.collaboration_analysis import CollaborationAnalysisService
from projects_hub.voice_analysis import VoiceAnalysis
from projects_hub.store import StoreError


class Repository:
    def __init__(self, transcript):
        self.source = '# Заметка\n\n## Полная расшифровка\n\n'+transcript+'\n\n## Техническая фиксация\nreceipt'
        self.files = {}
        self.refs = []
        self.writes = 0

    async def read_repository_path(self, **kwargs):
        if 'ref' in kwargs:
            self.refs.append(kwargs['ref'])
            return dict(kind='file',text=self.source,sha='a'*40,full_name='owner/intake')
        text = self.files[kwargs['path']]
        return dict(kind='file',text=text,sha=hashlib.sha1(text.encode()).hexdigest(),full_name='owner/private')

    async def write_repository_text(self, **kwargs):
        path, text = kwargs['path'], kwargs['text']
        if path in self.files and self.files[path] != text:
            raise StoreError('GITHUB_WRITE_CONFLICT','Different content')
        self.files[path] = text
        self.writes += 1
        return dict(kind='file',text=text,sha=hashlib.sha1(text.encode()).hexdigest(),full_name='owner/private',commit_sha='b'*40)


def voice_fixture(setup, transcript='Идея лекции. задай мне вопросы'):
    store, auth, events, receiver, owner, service, _ = live_fixture(setup)
    repository = Repository(transcript)
    collaboration = CollaborationService(store, repository)
    collaboration._project_docs_repository = lambda *args: dict(repository_id=123,full_name='owner/private',private=True)
    analysis = CollaborationAnalysisService(store, collaboration)
    events.analysis_service, analysis.question_events = analysis, events
    config = dict(token='t'*40,owner_id=owner['actor_id'],project_id=owner['project_id'],client_id=service['client_id'],
                  source_repository_id=321,source_repository='owner/intake',source_prefix='intake')
    adapter = VoiceAnalysis(events,collaboration,config)
    source = dict(repository='owner/intake',path='intake/session.md',commit_sha='c'*40,
                  transcript_sha256=hashlib.sha256(transcript.encode()).hexdigest())
    payload = dict(intent_id='voice:'+hashlib.sha256(json.dumps(source,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
        owner_id=owner['actor_id'],session_id='session-001',source=source,input_manifest=[source],
        trigger=dict(phrase='задай мне вопросы',start=transcript.index('задай мне вопросы'),end=transcript.index('задай мне вопросы')+len('задай мне вопросы')))
    return store,events,owner,service,analysis,repository,adapter,payload


def package(read, name='first-round', questions=True):
    return dict(kind='question_package',external_analysis_id=name,source_kind='project_note',source_id=read['note_id'],
        source_revision=read['source_revision'],source_repository_sha=read['source_repository_sha'],purpose='ideas',summary='Уточнить контекст' if questions else 'Это цитата, а не команда владельца.',
        questions=[dict(external_question_id=name+'-question',prompt='Какая аудитория '+name+'?',context='Лекция',reason='Выбрать содержание',blocking=True,priority='high',deadline_ms=None)] if questions else [])


@pytest.mark.asyncio
async def test_voice_no_slack_immutable_source_replay_and_result(setup):
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    try:
        accepted=await bridge.accept(payload)
        assert accepted['state']=='accepted'
        assert repo.refs==['c'*40]
        assert await bridge.accept(payload)==accepted
        assert repo.writes==1
        assert store.db.execute('SELECT count(*) FROM mcp_test_requests').fetchone()[0]==1
        # Source remains pending durably without any subscription.
        assert store.db.execute('SELECT count(*) FROM mcp_outbox').fetchone()[0]==0
        await events.subscribe(service['id'],REQUEST_EVENT,{'project_id':owner['project_id']},DELIVERY)
        assert await events.deliver_one()
        read=events.get_project_request(service['id'],owner['project_id'],accepted['request_id'])
        assert read['slack'] is None and read['external_source']['input_manifest']==payload['input_manifest']
        saved=events.record_request_result(service['id'],owner['project_id'],read['request_id'],read['event_id'],package(read))
        workspace=store.db.execute('SELECT workspace_id FROM projects WHERE id=?',(owner['project_id'],)).fetchone()[0]
        args=dict(actor_id=owner['actor_id'],workspace_id=workspace,analysis_id=saved['analysis_id'])
        analysis.answer_questions(**args,command_id='voice-answer-001',responses=[dict(question_id=saved['question_package']['questions'][0]['id'],disposition='answer',body='Взрослые')])
        complete=analysis.control_question_session(**args,command_id='voice-complete-001',action='complete')
        ready=events.get_project_request(service['id'],owner['project_id'],complete['continuation_request_id'])
        result=await events.record_request_result_async(service['id'],owner['project_id'],ready['request_id'],ready['event_id'],dict(kind='analysis_result',source_revision=1,summary='Готово',result_markdown='## Решения\n\nАудитория: взрослые.',used_receipts=['voice-answer-001']))
        assert result['destinations']['mira_state']=='available'
        assert result['destinations']['slack_state']=='not_required'
        assert result['question_package']['package_state']=='completed'
        projection=bridge.get(payload['intent_id'])['projection']
        assert 'Аудитория: взрослые' in projection['markdown']
        assert projection['revision']==hashlib.sha256(projection['markdown'].encode()).hexdigest()
        with pytest.raises(StoreError):
            events.record_request_result(service['id'],owner['project_id'],ready['request_id'],ready['event_id'],dict(kind='slack_claim',response_id=result['response_id']))
    finally:
        await analysis.close()


@pytest.mark.asyncio
async def test_quoted_trigger_can_finish_without_questions(setup):
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup, 'Он сказал «задай мне вопросы», это цитата.')
    events=bridge.events
    try:
        accepted=await bridge.accept(payload)
        service=service['id']
        read=events.get_project_request(service,bridge.config['project_id'],accepted['request_id'])
        saved=events.record_request_result(service,bridge.config['project_id'],read['request_id'],read['event_id'],package(read,questions=False))
        assert saved['question_package']['package_state']=='completed'
        assert saved['question_package']['questions']==[]
        assert 'цитата' in bridge.get(payload['intent_id'])['projection']['markdown']
    finally:
        await analysis.close()


@pytest.mark.asyncio
async def test_voice_source_and_owner_cannot_be_substituted(setup):
    *_,analysis,repo,bridge,payload=voice_fixture(setup)
    try:
        for bad in ({**payload,'owner_id':'other'}, {**payload,'intent_id':'voice:'+'0'*64},
                    {**payload,'trigger':{**payload['trigger'],'start':0}}):
            with pytest.raises(StoreError):
                await bridge.accept(bad)
        assert repo.writes==0
        with pytest.raises(StoreError):
            bridge.authorize('Bearer wrong')
    finally:
        await analysis.close()

@pytest.mark.asyncio
async def test_restart_reconciles_anchor_and_keeps_source_revision(setup):
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    try:
        accepted=await bridge.accept(payload)
        # Simulate crash after request creation before linking the intent receipt.
        store.db.execute('UPDATE voice_analysis_intents SET request_id=NULL')
        restored=VoiceAnalysis(events,bridge.collaboration,bridge.config)
        assert (await restored.accept(payload))['request_id']==accepted['request_id']
        assert store.db.execute('SELECT count(*) FROM mcp_test_requests').fetchone()[0]==1
        assert store.db.execute('SELECT revision FROM project_notes WHERE id LIKE \'note_voice_%\'').fetchone()[0]==1
        # A changed private anchor must not be overwritten on recovery.
        store.db.execute('UPDATE voice_analysis_intents SET request_id=NULL')
        path=next(iter(repo.files))
        repo.files[path]+='\nHuman edit'
        with pytest.raises(StoreError,match='Different content'):
            await restored.accept(payload)
        assert repo.files[path].endswith('Human edit')
    finally:
        await analysis.close()


@pytest.mark.asyncio
async def test_shared_inbox_is_rejected_before_source_read(setup):
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    try:
        workspace=store.db.execute('SELECT workspace_id FROM projects WHERE id=?',(owner['project_id'],)).fetchone()[0]
        # Existing actor grants must not widen the proposed private intake audience.
        store.db.execute("INSERT INTO project_grants VALUES(?,?, 'viewer',0,0,1,NULL)",(service['actor_id'],owner['project_id']))
        with pytest.raises(StoreError,match='owner-only'):
            await bridge.accept(payload)
        assert not repo.refs and not repo.files
    finally:
        await analysis.close()

@pytest.mark.asyncio
async def test_expired_voice_request_redrive_keeps_logical_identity(setup):
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    try:
        accepted=await bridge.accept(payload)
        await events.subscribe(service['id'],REQUEST_EVENT,{'project_id':owner['project_id']},DELIVERY)
        assert await events.deliver_one()
        store.db.execute('UPDATE mcp_test_requests SET expires_ms=0 WHERE id=?',(accepted['request_id'],))
        assert bridge.get(payload['intent_id'])['needs_redrive']
        renewed=await bridge.accept(payload)
        assert not renewed['needs_redrive']
        assert renewed['request_id']==accepted['request_id'] and renewed['event_id']==accepted['event_id']
        assert repo.writes==1
        assert await events.deliver_one()
        assert store.db.execute('SELECT count(*) FROM mcp_test_requests').fetchone()[0]==1
    finally:
        await analysis.close()


def test_actual_http_ingress_is_attached_and_authenticated(setup, tmp_path):
    from fastapi.testclient import TestClient
    from projects_hub.app import create_app
    from projects_hub.settings import Settings
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    settings=Settings(data_dir=tmp_path/'unused',static_dir=tmp_path/'static',session_secret='s'*48,
                      voice_analysis_config=bridge.config)
    assert bridge.config['token'] not in repr(settings)
    app=create_app(settings,store=store,collaboration=bridge.collaboration,collaboration_analysis=analysis)
    client=TestClient(app)
    path='/api/integrations/voice-analysis/intents'
    assert client.post(path,json=payload).status_code==401
    headers={'authorization':'Bearer '+bridge.config['token']}
    assert client.post(path,content=b'x'*32769,headers=headers).status_code==413
    saved=client.post(path,json=payload,headers=headers)
    assert saved.status_code==200, saved.text
    assert saved.json()['request_id']
    read=client.get(path+'/'+payload['intent_id'],headers=headers)
    assert read.status_code==200 and read.json()['source']==payload['source']
    assert client.get(path+'/'+payload['intent_id']).status_code==401


def test_binding_file_private_and_not_in_settings_repr(tmp_path):
    from projects_hub.settings import Settings
    path=tmp_path/'voice.json'
    path.write_text(json.dumps(dict(token='synthetic-private-token-'+'x'*32)))
    path.chmod(0o600)
    env=dict(PROJECTS_HUB_SESSION_SECRET='s'*48,PROJECTS_HUB_VOICE_ANALYSIS_CONFIG_FILE=str(path))
    settings=Settings.from_env(env)
    assert settings.voice_analysis_config['token'].startswith('synthetic-private')
    assert 'synthetic-private' not in repr(settings)
    path.chmod(0o644)
    with pytest.raises(RuntimeError,match='private'):
        Settings.from_env(env)
