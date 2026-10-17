"""Narrow, opt-in Record bridge. Source snapshots are not board mutations.

The deployment binding is authority; transcript text and client owner labels are not.
No provider call or Slack route is needed to create an immutable private anchor.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse

from .store import StoreError, _now_ms

PATH = '/api/integrations/voice-analysis/intents'


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class VoiceAnalysis:
    def __init__(self, events, collaboration, config):
        self.events, self.store, self.collaboration, self.config = events, events.store, collaboration, config
        events.voice_analysis = self
        self.store.db.executescript('''
            CREATE TABLE IF NOT EXISTS voice_analysis_intents(
                id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, owner_id TEXT NOT NULL,
                project_id TEXT NOT NULL, note_id TEXT, binding_id TEXT, request_id TEXT,
                created_ms INTEGER NOT NULL);
        ''')

        from .voice_join import VoiceJoins
        self.joins = VoiceJoins(self)

    def authorize(self, authorization):
        token = self.config.get('token', '')
        if not token or len(token) < 32 or not secrets.compare_digest(authorization, 'Bearer ' + token):
            raise StoreError('VOICE_AUTH_REQUIRED', 'Voice integration unavailable')
        self._access()

    def _access(self):
        c = self.config
        if not all(c.get(k) for k in ('owner_id', 'project_id', 'client_id', 'source_repository_id', 'source_repository', 'source_prefix')):
            raise StoreError('VOICE_BINDING_REQUIRED', 'Private inbox binding is not configured')
        self.events.auth.project_owner(c['owner_id'], c['project_id'])
        self.events.auth.service_client(c['client_id'])
        project = self.store.db.execute('SELECT workspace_id FROM projects WHERE id=?', (c['project_id'],)).fetchone()
        self.store.project_access(c['owner_id'], project['workspace_id'], c['project_id'], require_analyze=True)
        # Imported private speech must not leak to another project member.
        if self.store.db.execute("SELECT 1 FROM project_grants WHERE project_id=? AND actor_id!=? AND revoked_at_ms IS NULL",(c['project_id'],c['owner_id'])).fetchone():
            raise StoreError('PROJECT_FORBIDDEN','Voice inbox must be owner-only')
        return project['workspace_id']

    def validate(self, payload):
        expected = {'intent_id','owner_id','session_id','source','input_manifest','trigger'}
        if not isinstance(payload, dict) or set(payload) != expected:
            raise StoreError('INVALID_ARGUMENT', 'Strict voice intent required')
        if payload['owner_id'] != self.config['owner_id']:
            raise StoreError('PROJECT_FORBIDDEN', 'Voice owner does not match trusted binding')
        if not isinstance(payload['intent_id'], str) or not re.fullmatch(r'voice:[a-f0-9]{64}', payload['intent_id']):
            raise StoreError('INVALID_ARGUMENT', 'Invalid voice intent ID')
        if not isinstance(payload['session_id'], str) or not 1 <= len(payload['session_id']) <= 160:
            raise StoreError('INVALID_ARGUMENT', 'Invalid session ID')
        manifest = payload['input_manifest']
        if not isinstance(manifest, list) or not 1 <= len(manifest) <= 32 or payload['source'] not in manifest:
            raise StoreError('INVALID_ARGUMENT', 'Frozen input manifest required')
        if len({canonical(x) for x in manifest}) != len(manifest):
            raise StoreError('INVALID_ARGUMENT', 'Duplicate input sources')
        for src in manifest:
            if not isinstance(src, dict) or set(src) != {'repository','path','commit_sha','transcript_sha256'}:
                raise StoreError('INVALID_ARGUMENT', 'Immutable source reference required')
            if (src['repository'] != self.config['source_repository'] or not isinstance(src['path'],str)
                    or not src['path'].startswith(self.config['source_prefix'].rstrip('/') + '/')
                    or any(part in ('', '.', '..') for part in src['path'].split('/')) or '\\' in src['path']):
                raise StoreError('PROJECT_FORBIDDEN', 'Source outside approved namespace')
            if not re.fullmatch(r'[a-f0-9]{40}', str(src['commit_sha'])) or not re.fullmatch(r'[a-f0-9]{64}', str(src['transcript_sha256'])):
                raise StoreError('INVALID_ARGUMENT', 'Immutable source hashes required')
        expected_id = 'voice:' + hashlib.sha256(json.dumps(payload['source'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if payload['intent_id'] != expected_id:
            raise StoreError('VOICE_SOURCE_CONFLICT','Intent identity must match immutable source')
        trigger = payload['trigger']
        if (not isinstance(trigger, dict) or set(trigger) != {'phrase','start','end'}
                or not isinstance(trigger['phrase'],str) or len(trigger['phrase']) > 100
                or type(trigger['start']) is not int or type(trigger['end']) is not int
                or not 0 <= trigger['start'] < trigger['end']):
            raise StoreError('INVALID_ARGUMENT', 'Final transcript trigger offsets required')

    async def _source(self, source, workspace):
        github = self.collaboration.github
        if github is None:
            raise StoreError('VOICE_SOURCE_UNAVAILABLE', 'Source reader unavailable')
        item = await github.read_repository_path(actor_id=self.config['owner_id'], workspace_id=workspace,
            repository_id=int(self.config['source_repository_id']), path=source['path'], ref=source['commit_sha'])
        if item.get('kind') != 'file' or item.get('full_name') != source['repository']:
            raise StoreError('VOICE_SOURCE_CONFLICT', 'Source repository mismatch')
        text = item.get('text', '')
        try:
            transcript = text.split('\n## Полная расшифровка\n\n', 1)[1].rsplit('\n\n## Техническая фиксация\n', 1)[0].strip()
        except IndexError:
            raise StoreError('VOICE_SOURCE_CONFLICT', 'Final transcript missing') from None
        if hashlib.sha256(transcript.encode()).hexdigest() != source['transcript_sha256']:
            raise StoreError('VOICE_SOURCE_CONFLICT', 'Final transcript hash mismatch')
        return transcript

    async def accept(self, payload):
        workspace = self._access()
        self.validate(payload)
        data = canonical(payload)
        with self.store._lock:
            old = self.store.db.execute('SELECT * FROM voice_analysis_intents WHERE id=?', (payload['intent_id'],)).fetchone()
            if old and old['payload_json'] != data:
                raise StoreError('VOICE_SOURCE_CONFLICT', 'Intent ID already has another immutable input')
            if old and old['request_id']:
                root, _ = self.joins.case(payload['intent_id'])
                self.redrive(root['binding_id'])
                return self.get(payload['intent_id'])
        transcripts = [await self._source(src, workspace) for src in payload['input_manifest']]
        transcript = transcripts[payload['input_manifest'].index(payload['source'])]
        trigger = payload['trigger']
        candidate = transcript[trigger['start']:trigger['end']]
        if candidate != trigger['phrase'] or not re.fullmatch(r'задай\s+мне\s+вопросы', candidate, re.I):
            raise StoreError('VOICE_SOURCE_CONFLICT', 'Trigger does not match final transcript')
        # This is only a lexical candidate. Dot checks negation/quotation before questions.
        c = self.config
        repository = self.collaboration._project_docs_repository(c['owner_id'], workspace, c['project_id'])
        self.collaboration._require_project_audience_repository(repository)
        # A private repository alone is insufficient if unrelated project members can read it.
        # Deployment must bind a private owner inbox; actual project ACL still gates every operation.
        ident = hashlib.sha256(payload['intent_id'].encode()).hexdigest()
        note_id, binding_id = 'note_voice_' + ident[:32], 'voice_binding_' + ident[:32]
        path = 'docs/notes/voice/' + ident + '.md'
        body = '\n\n'.join('Источник: ' + src['repository'] + '/' + src['path'] + '@' + src['commit_sha'] + '\n\n' + text
                          for src, text in zip(payload['input_manifest'], transcripts))
        markdown = '# Голосовая заметка\n\n' + body + '\n\n<!-- frozen-input ' + canonical(payload['input_manifest']) + ' -->\n'
        verified = await self.collaboration.github.write_repository_text(
            actor_id=c['owner_id'], workspace_id=workspace, repository_id=int(repository['repository_id']),
            project_id=c['project_id'], path=path, text=markdown, message='docs: preserve voice analysis source')
        if verified.get('kind') != 'file' or verified.get('text') != markdown or not re.fullmatch(r'[a-f0-9]{40}',str(verified.get('sha',''))):
            raise StoreError('GITHUB_READBACK_MISMATCH', 'Voice anchor publication not verified')
        self._access()
        now = _now_ms()
        with self.store._lock:
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                old = self.store.db.execute('SELECT * FROM voice_analysis_intents WHERE id=?',(payload['intent_id'],)).fetchone()
                if old and old['payload_json'] != data:
                    raise StoreError('VOICE_SOURCE_CONFLICT', 'Conflicting concurrent intent')
                self.store.db.execute('INSERT OR IGNORE INTO voice_analysis_intents VALUES(?,?,?,?,?,?,NULL,?)',
                    (payload['intent_id'],data,c['owner_id'],c['project_id'],note_id,binding_id,now))
                self.joins.register(payload['intent_id'])
                fields = dict(id=note_id,workspace_id=workspace,project_id=c['project_id'],author_actor_id=c['owner_id'],
                    title='Голосовая заметка '+payload['session_id'],body=body,source_text=body,markdown=markdown,
                    author_roles_json='["owner"]',audience='project',status='ready',repository_id=int(repository['repository_id']),
                    repository_full_name=repository['full_name'],repository_path=path,repository_sha=verified['sha'],
                    repository_commit_sha=verified.get('commit_sha'),command_id='voice.'+ident,request_sha256=fingerprint(payload),
                    revision=1,created_at_ms=now,updated_at_ms=now)
                self.store.db.execute('INSERT OR IGNORE INTO project_notes('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+')',tuple(fields.values()))
                self.store.db.execute('INSERT OR IGNORE INTO mcp_voice_bindings VALUES(?,?,?,?,?,NULL,NULL,NULL,?,\'discussion_reply\',\'active\',1,?,?,1)',
                    (binding_id,c['project_id'],note_id,c['owner_id'],c['client_id'],note_id,now,now))
                self.store.db.execute('COMMIT')
            except BaseException:
                self.store.db.execute('ROLLBACK'); raise
        request = self.events.create_request(c['owner_id'],c['project_id'],'voice.'+ident,event_name='projects_hub.request.created',
            binding_id=binding_id,request_kind='question_start',origin='voice')
        with self.store._lock:
            self.store.db.execute('UPDATE voice_analysis_intents SET request_id=? WHERE id=?',(request['request_id'],payload['intent_id']))
        return self.get(payload['intent_id'])

    def redrive(self, binding_id):
        """Renew only expired unfinished logical requests, retaining their IDs/results.

        POST retry is a deliberate durable redrive; GET never mutates delivery state.
        Existing verified subscriptions/ACL are required to enqueue a notification.
        """
        now = _now_ms()
        with self.store._lock:
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                requests = self.store.db.execute("SELECT * FROM mcp_test_requests WHERE binding_id=? AND state='awaiting_response' AND expires_ms<=?",(binding_id,now)).fetchall()
                for request in requests:
                    self.events.bindings.for_owner(self.config['owner_id'],self.config['project_id'],binding_id,self.config['owner_id'])
                    self.store.db.execute('UPDATE mcp_test_requests SET expires_ms=? WHERE id=?',(now+7*86400_000,request['id']))
                    for sub in self.store.db.execute("SELECT * FROM mcp_subscriptions WHERE project_id=? AND event_name='projects_hub.request.created' AND state='active' AND expires_ms>?",(self.config['project_id'],now)).fetchall():
                        try:
                            self.events.bindings.access(sub['grant_id'],request['project_id'],binding_id,request['actor_id'],request['binding_revision'])
                        except StoreError:
                            continue
                        self.store.db.execute("""INSERT INTO mcp_outbox(event_id,subscription_id,request_id,state,next_ms)
                            VALUES(?,?,?,'pending',?) ON CONFLICT(event_id,subscription_id) DO UPDATE
                            SET state='pending',next_ms=excluded.next_ms,lease_until_ms=NULL,last_error=NULL""",
                            (request['event_id'],sub['id'],request['id'],now))
                self.store.db.execute('COMMIT')
            except BaseException:
                self.store.db.execute('ROLLBACK'); raise

    def context(self, request_id):
        request = self.store.db.execute('SELECT * FROM mcp_test_requests WHERE id=?',(request_id,)).fetchone()
        if not request:
            return None
        row = self.store.db.execute('SELECT payload_json FROM voice_analysis_intents WHERE binding_id=?',(request['binding_id'],)).fetchone()
        if not row:
            return None
        context = json.loads(row['payload_json'])
        context.update(self.joins.context(request,context))
        # Candidates only: temporal proximity does not merge topics or mutate this snapshot.
        context['related_source_candidates'] = [json.loads(r['payload_json'])['source'] for r in self.store.db.execute(
            'SELECT payload_json FROM voice_analysis_intents WHERE owner_id=? AND project_id=? AND binding_id!=? ORDER BY created_ms DESC,id DESC LIMIT 10',
            (self.config['owner_id'],self.config['project_id'],request['binding_id'])).fetchall()]
        context['related_intent_candidates'] = []
        for candidate in self.store.db.execute('SELECT id,payload_json FROM voice_analysis_intents WHERE owner_id=? AND project_id=? AND binding_id!=? ORDER BY created_ms DESC,id DESC LIMIT 10',
                (self.config['owner_id'],self.config['project_id'],request['binding_id'])).fetchall():
            root, version = self.joins.case(candidate['id'])
            context['related_intent_candidates'].append(dict(intent_id=candidate['id'],canonical_intent_id=root['id'],case_revision=version['revision'],source=json.loads(candidate['payload_json'])['source']))
        context['trigger_semantics'] = 'Candidate only. Inspect the full transcript; a quotation, negation or discussion of this feature is not a command. An empty question_package with explanation completes a voice candidate without questions.'
        return context

    def get(self, intent_id):
        # Projection, revision and terminal flag must describe one database snapshot.
        with self.store._lock:
            self.store.db.execute('SAVEPOINT voice_projection_read')
            try:
                result = self._get(intent_id)
                self.store.db.execute('RELEASE SAVEPOINT voice_projection_read')
                return result
            except BaseException:
                self.store.db.execute('ROLLBACK TO SAVEPOINT voice_projection_read')
                self.store.db.execute('RELEASE SAVEPOINT voice_projection_read')
                raise

    def _get(self, intent_id):
        self._access()
        row = self.store.db.execute('SELECT * FROM voice_analysis_intents WHERE id=? AND owner_id=? AND project_id=?',
            (intent_id,self.config['owner_id'],self.config['project_id'])).fetchone()
        if not row:
            raise StoreError('REQUEST_UNAVAILABLE','Voice intent unavailable')
        payload = json.loads(row['payload_json'])
        root, case_version = self.joins.case(intent_id)
        lines = ['## Вопросы от аналитика']
        for analysis in self.store.db.execute('SELECT * FROM collaboration_analyses WHERE note_id=? AND model_alias=\'dot\' ORDER BY created_at_ms,id',(root['note_id'],)).fetchall():
            lines.extend(['', 'Раунд '+analysis['id']+'; '+analysis['package_state']])
            for question in self.store.db.execute('SELECT prompt,state FROM collaboration_questions WHERE analysis_id=? ORDER BY id',(analysis['id'],)).fetchall():
                lines.append('- '+question['prompt']+' ['+question['state']+']')
            if json.loads(analysis['scope_json']).get('disposition') == 'not_requested':
                lines.append('Аналитика не запрошена: '+json.loads(analysis['result_json']).get('summary',''))
            if analysis['result_markdown']:
                lines.extend(['', '## Анализ и результаты', analysis['result_markdown']])
                result = json.loads(analysis['result_json'])
                if result.get('artifact_url'):
                    lines.append('Проверенный документ: '+result['artifact_url'])
        markdown = '\n'.join(lines)+'\n'
        request = self.store.db.execute('SELECT event_id FROM mcp_test_requests WHERE id=?',(row['request_id'],)).fetchone()
        pending = bool(self.store.db.execute("SELECT 1 FROM mcp_test_requests WHERE binding_id=? AND state='awaiting_response' LIMIT 1",(root['binding_id'],)).fetchone())
        rounds = self.store.db.execute("SELECT package_state FROM collaboration_analyses WHERE note_id=? AND model_alias='dot'",(root['note_id'],)).fetchall()
        terminal = bool(rounds) and not pending and all(r['package_state'] in ('completed','partially_completed','cancelled') for r in rounds)
        return dict(intent_id=intent_id,source=payload['source'],state='accepted',request_id=row['request_id'],
            projection_terminal=terminal,case_id=root['id'],input_revision=case_version['revision'],
            member_intent_ids=[r[0] for r in self.store.db.execute('SELECT intent_id FROM voice_case_members WHERE root_id=? ORDER BY intent_id',(root['id'],)).fetchall()],
            canonical_intent_id=root['id'],case_revision=case_version['revision'],
            event_id=request['event_id'] if request else None,
            needs_redrive=bool(self.store.db.execute("SELECT 1 FROM mcp_test_requests WHERE binding_id=? AND state='awaiting_response' AND expires_ms<=?",(root['binding_id'],_now_ms())).fetchone()),
            projection=dict(revision=hashlib.sha256(markdown.encode()).hexdigest(),markdown=markdown))


def attach_voice_analysis_routes(app, settings, events, collaboration):
    config = dict(getattr(settings, 'voice_analysis_config', None) or {})
    bridge = VoiceAnalysis(events, collaboration, config)
    app.state.voice_analysis = bridge

    async def handle(request: Request):
        try:
            bridge.authorize(request.headers.get('authorization',''))
            if request.method == 'GET':
                return JSONResponse(bridge.get(request.path_params['intent_id']))
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 32768:
                    return JSONResponse({'error':{'code':'INVALID_ARGUMENT'}},status_code=413)
            return JSONResponse(await bridge.accept(json.loads(raw)))
        except (ValueError,TypeError):
            return JSONResponse({'error':{'code':'INVALID_ARGUMENT'}},status_code=400)
        except StoreError as exc:
            status = 401 if exc.code=='VOICE_AUTH_REQUIRED' else 403 if exc.code=='PROJECT_FORBIDDEN' else 409 if exc.code=='VOICE_SOURCE_CONFLICT' else 400 if exc.code=='INVALID_ARGUMENT' else 503
            return JSONResponse({'error':{'code':exc.code}},status_code=status)
    app.add_api_route(PATH,handle,methods=['POST'])
    app.add_api_route(PATH+'/{intent_id}',handle,methods=['GET'])
