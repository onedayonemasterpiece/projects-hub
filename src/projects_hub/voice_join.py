"""Explicit semantic source membership; existing question sessions own the lifecycle."""
import json
import re
import secrets

from .store import StoreError, _now_ms
from .voice_analysis import canonical, fingerprint


class VoiceJoins:
    def __init__(self, bridge):
        self.bridge, self.store, self.events = bridge, bridge.store, bridge.events
        self.store.db.executescript('''
            CREATE TABLE IF NOT EXISTS voice_case_members(intent_id TEXT PRIMARY KEY, root_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS voice_case_versions(root_id TEXT NOT NULL, revision INTEGER NOT NULL,
                manifest_json TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(root_id,revision));
            CREATE TABLE IF NOT EXISTS voice_request_inputs(request_id TEXT PRIMARY KEY, root_id TEXT NOT NULL, revision INTEGER NOT NULL, prior_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS voice_superseded_requests(request_id TEXT PRIMARY KEY, root_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS voice_link_commands(project_id TEXT NOT NULL, command_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL, result_json TEXT NOT NULL, PRIMARY KEY(project_id,command_id));
        ''')

        for row in self.store.db.execute('SELECT id FROM voice_analysis_intents WHERE owner_id=? AND project_id=?',(bridge.config.get('owner_id'),bridge.config.get('project_id'))).fetchall():
            self.register(row['id'])

    def intent(self, intent_id):
        c = self.bridge.config
        row = self.store.db.execute('SELECT * FROM voice_analysis_intents WHERE id=? AND owner_id=? AND project_id=?',
                                    (intent_id,c['owner_id'],c['project_id'])).fetchone()
        if not row:
            raise StoreError('REQUEST_UNAVAILABLE','Voice source is not an accepted owner input')
        return row

    def register(self, intent_id):
        row = self.intent(intent_id)
        self.store.db.execute('INSERT OR IGNORE INTO voice_case_members VALUES(?,?)',(intent_id,intent_id))
        self.store.db.execute('INSERT OR IGNORE INTO voice_case_versions VALUES(?,1,?,?)',
            (intent_id,canonical(json.loads(row['payload_json'])['input_manifest']),'Original verified input'))

    def case(self, intent_id):
        self.intent(intent_id)
        root = self.store.db.execute('SELECT root_id FROM voice_case_members WHERE intent_id=?',(intent_id,)).fetchone()[0]
        version = self.store.db.execute('SELECT * FROM voice_case_versions WHERE root_id=? ORDER BY revision DESC LIMIT 1',(root,)).fetchone()
        return self.intent(root), version

    def freeze(self, request_id, binding_id):
        row = self.store.db.execute('SELECT id FROM voice_analysis_intents WHERE binding_id=?',(binding_id,)).fetchone()
        if row:
            root, version = self.case(row['id'])
            self.store.db.execute('INSERT OR IGNORE INTO voice_request_inputs VALUES(?,?,?,?)',
                                  (request_id,root['id'],version['revision'],canonical(self.history(root['note_id']))))

    def superseded(self, request_id):
        row = self.store.db.execute('SELECT root_id FROM voice_superseded_requests WHERE request_id=?',(request_id,)).fetchone()
        if not row:
            return None
        root = self.intent(row['root_id'])
        latest = self.store.db.execute('SELECT id FROM mcp_test_requests WHERE binding_id=? AND id NOT IN (SELECT request_id FROM voice_superseded_requests) ORDER BY created_ms DESC,rowid DESC LIMIT 1',(root['binding_id'],)).fetchone()
        return dict(state='superseded',allowed_result_kinds=[],canonical_intent_id=root['id'],canonical_request_id=latest['id'] if latest else None)

    def assert_current(self, request_id):
        if self.superseded(request_id):
            raise StoreError('VOICE_REQUEST_SUPERSEDED','Read the canonical request; this source was explicitly linked')

    def context(self, request, original):
        root, version = self.case(original['intent_id'])
        frozen = self.store.db.execute('SELECT * FROM voice_request_inputs WHERE request_id=?',(request['id'],)).fetchone()
        # Requests made before this feature retain their original manifest, never latest input.
        manifest = original['input_manifest']
        revision = 1
        if frozen:
            revision = frozen['revision']
            manifest = json.loads(self.store.db.execute('SELECT manifest_json FROM voice_case_versions WHERE root_id=? AND revision=?',
                                                       (frozen['root_id'],revision)).fetchone()[0])
        prior = json.loads(frozen['prior_json']) if frozen else []
        triggers = []
        for member in self.store.db.execute('SELECT i.id,i.payload_json FROM voice_case_members m JOIN voice_analysis_intents i ON i.id=m.intent_id WHERE m.root_id=? ORDER BY i.id',(root['id'],)).fetchall():
            value=json.loads(member['payload_json'])
            if value['source'] in manifest:
                triggers.append(dict(intent_id=member['id'],source=value['source'],trigger=value['trigger']))
        return dict(canonical_intent_id=root['id'],case_revision=version['revision'],input_manifest_revision=revision,
                    triggered_sources=triggers,
                    input_manifest=manifest,prior_rounds=prior,
                    linked_intent_ids=[r[0] for r in self.store.db.execute('SELECT intent_id FROM voice_case_members WHERE root_id=? ORDER BY intent_id',(root['id'],)).fetchall()])

    def history(self, note_id):
        prior = []
        root = self.store.db.execute('SELECT id FROM voice_analysis_intents WHERE note_id=?',(note_id,)).fetchone()
        notes = [note_id]
        if root:
            notes = [r[0] for r in self.store.db.execute('SELECT i.note_id FROM voice_case_members m JOIN voice_analysis_intents i ON i.id=m.intent_id WHERE m.root_id=?',(root['id'],)).fetchall()]
        for a in self.store.db.execute("SELECT id,package_state,scope_json,result_json,result_markdown FROM collaboration_analyses WHERE note_id IN ("+','.join('?' for _ in notes)+") AND model_alias='dot' ORDER BY created_at_ms,id",tuple(notes)).fetchall():
            result=json.loads(a['result_json'])
            prior.append(dict(analysis_id=a['id'],state=a['package_state'],
                result_summary=result.get('summary'),result_markdown=a['result_markdown'],artifact_url=result.get('artifact_url'),
                answer_receipts=[dict(command_id=r['command_id'],actor_id=r['actor_id'],receipt=json.loads(r['receipt_json']))
                for r in self.store.db.execute('SELECT * FROM collaboration_answer_commands WHERE analysis_id=? ORDER BY created_at_ms,command_id',(a['id'],)).fetchall()]))
        return prior

    def _authorize(self, grant_id, project_id, root):
        self.bridge._access()
        if not root['request_id']:
            raise StoreError('VOICE_INPUT_PENDING','Source acceptance is not complete; let intake retry before linking')
        if project_id != root['project_id']:
            raise StoreError('PROJECT_FORBIDDEN','Voice inbox mismatch')
        self.events.bindings.access(grant_id,project_id,root['binding_id'],root['owner_id'],1)

    def _check_support_intents(self, root, supporting_source_refs, selected_ids):
        for ref in supporting_source_refs:
            for known in self.store.db.execute('SELECT id,payload_json FROM voice_analysis_intents WHERE owner_id=? AND project_id=?',(root['owner_id'],root['project_id'])).fetchall():
                if json.loads(known['payload_json'])['source'] == ref:
                    known_root,_ = self.case(known['id'])
                    if known_root['id'] != root['id'] and known['id'] not in selected_ids:
                        raise StoreError('VOICE_CASE_CONFLICT','This supporting source has its own intent; select its ID and expected revision explicitly')

    async def link(self, grant_id, *, project_id, target_intent_id, source_intents,
                   supporting_source_refs, expected_case_revision, command_id, reason):
        self.bridge._access()
        if (not isinstance(project_id,str) or not isinstance(target_intent_id,str)
                or not isinstance(command_id,str) or not 8 <= len(command_id) <= 160
                or not isinstance(reason,str) or not 1 <= len(reason.strip()) <= 2000
                or type(expected_case_revision) is not int or expected_case_revision < 1
                or not isinstance(source_intents,list) or not isinstance(supporting_source_refs,list)
                or not 1 <= len(source_intents)+len(supporting_source_refs) <= 31):
            raise StoreError('INVALID_ARGUMENT','Provide bounded explicit sources, current revision and semantic reason')
        if any(not isinstance(s,dict) or set(s)!={'intent_id','expected_revision'} or not isinstance(s['intent_id'],str)
               or type(s['expected_revision']) is not int or s['expected_revision']<1 for s in source_intents):
            raise StoreError('INVALID_ARGUMENT','Each source intent needs its expected case revision')
        if len({s['intent_id'] for s in source_intents}) != len(source_intents) or target_intent_id in [s['intent_id'] for s in source_intents]:
            raise StoreError('INVALID_ARGUMENT','Distinct source intents required')
        payload = dict(project_id=project_id,target_intent_id=target_intent_id,source_intents=source_intents,
            supporting_source_refs=supporting_source_refs,expected_case_revision=expected_case_revision,reason=reason)
        digest = fingerprint(payload)
        root, version = self.case(target_intent_id)
        self._authorize(grant_id,project_id,root)
        old = self.store.db.execute('SELECT * FROM voice_link_commands WHERE project_id=? AND command_id=?',(project_id,command_id)).fetchone()
        if old:
            if old['payload_hash'] != digest:
                raise StoreError('VOICE_SOURCE_CONFLICT','Command already records a different semantic selection')
            return json.loads(old['result_json'])
        manifest = json.loads(version['manifest_json'])
        for s in source_intents:
            source, source_version = self.case(s['intent_id'])
            self._authorize(grant_id,project_id,source)
            manifest.extend(json.loads(source_version['manifest_json']))
        selected_ids = {s['intent_id'] for s in source_intents}
        self._check_support_intents(root,supporting_source_refs,selected_ids)
        manifest.extend(supporting_source_refs)
        manifest = list({canonical(s):s for s in manifest}.values())
        if not source_intents and manifest == json.loads(version['manifest_json']):
            raise StoreError('VOICE_SOURCE_CONFLICT','All selected supporting inputs are already linked; no new review was created')
        # Reuse the exact source-namespace validation. Supporting notes need no command phrase.
        check = json.loads(root['payload_json']); check['input_manifest'] = manifest
        self.bridge.validate(check)
        workspace = self.bridge._access()
        for source in manifest:
            transcript = await self.bridge._source(source,workspace)
            if source in supporting_source_refs and re.search(r'(?<!\w)задай\s+мне\s+вопросы(?!\w)',transcript,re.I):
                raise StoreError('VOICE_TRIGGER_ACCEPTANCE_REQUIRED','This source contains an intake trigger candidate. Wait for its accepted intent and select its ID/revision explicitly, not as an untriggered supporting ref')
        with self.store._lock:
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                root, version = self.case(target_intent_id)
                self._authorize(grant_id,project_id,root)
                old = self.store.db.execute('SELECT * FROM voice_link_commands WHERE project_id=? AND command_id=?',(project_id,command_id)).fetchone()
                if old:
                    if old['payload_hash'] != digest:
                        raise StoreError('VOICE_SOURCE_CONFLICT','Command already records another selection')
                    result = json.loads(old['result_json'])
                    self.store.db.execute('COMMIT'); return result
                if root['id'] != target_intent_id or version['revision'] != expected_case_revision:
                    raise StoreError('VOICE_CASE_CONFLICT','Read the canonical case and retry with its current revision')
                self._check_support_intents(root,supporting_source_refs,selected_ids)
                donors = []
                for selected in source_intents:
                    donor, dv = self.case(selected['intent_id'])
                    self._authorize(grant_id,project_id,donor)
                    if donor['id'] != selected['intent_id'] or dv['revision'] != selected['expected_revision']:
                        raise StoreError('VOICE_CASE_CONFLICT','Source case changed; read its canonical case')
                    history = self.store.db.execute('SELECT id,scope_json,package_state FROM collaboration_analyses WHERE note_id=?',(donor['note_id'],)).fetchall()
                    for old_round in history:
                        safely_declined = (old_round['package_state']=='completed' and json.loads(old_round['scope_json']).get('disposition')=='not_requested'
                            and not self.store.db.execute('SELECT 1 FROM collaboration_questions WHERE analysis_id=?',(old_round['id'],)).fetchone()
                            and not self.store.db.execute('SELECT 1 FROM collaboration_answer_commands WHERE analysis_id=?',(old_round['id'],)).fetchone())
                        if not safely_declined:
                            raise StoreError('VOICE_HISTORY_CONFLICT','Source already has independent question history. Review and reconcile those answers explicitly; no history was merged or discarded')
                    if self.store.db.execute('SELECT count(*) FROM voice_case_members WHERE root_id=?',(donor['id'],)).fetchone()[0] != 1:
                        raise StoreError('VOICE_CASE_CONFLICT','Select individual unlinked sources; nested cases require explicit reconciliation')
                    donors.append(donor)
                if self.bridge.get(root['id'])['projection_terminal'] and not any(self.store.db.execute("SELECT 1 FROM mcp_test_requests WHERE binding_id=? AND state='awaiting_response'",(d['binding_id'],)).fetchone() for d in donors):
                    raise StoreError('VOICE_ACTIVE_TRIGGER_REQUIRED','This case is complete. Select a new accepted triggered voice intent as donor, plus any supporting refs; its active delivery will refresh all linked notes')
                new_revision = version['revision']+1
                self.store.db.execute('INSERT INTO voice_case_versions VALUES(?,?,?,?)',(root['id'],new_revision,canonical(manifest),reason))
                pending = self.store.db.execute("SELECT * FROM mcp_test_requests WHERE binding_id=? AND state='awaiting_response' AND id NOT IN (SELECT request_id FROM voice_superseded_requests) ORDER BY created_ms DESC LIMIT 1",(root['binding_id'],)).fetchone()
                for donor in donors:
                    self.store.db.execute('UPDATE voice_case_members SET root_id=? WHERE intent_id=?',(root['id'],donor['id']))
                    for req in self.store.db.execute("SELECT id FROM mcp_test_requests WHERE binding_id=? AND state='awaiting_response'",(donor['binding_id'],)).fetchall():
                        self._supersede(req['id'],root['id'])
                request_id = None
                if pending and pending['request_kind'] == 'question_start' and not pending['analysis_id']:
                    self._supersede(pending['id'],root['id'])
                    request_id = self._new_request(root,new_revision,pending)
                elif not pending:
                    active = self.store.db.execute("SELECT 1 FROM collaboration_analyses WHERE note_id=? AND package_state NOT IN ('completed','partially_completed','cancelled') LIMIT 1",(root['note_id'],)).fetchone()
                    if not active:
                        base = self.store.db.execute('SELECT * FROM mcp_test_requests WHERE id=?',(root['request_id'],)).fetchone()
                        request_id = self._new_request(root,new_revision,base,fresh=True)
                result = dict(canonical_intent_id=root['id'],case_revision=new_revision,request_id=request_id,
                              state='review_requested' if request_id else 'queued_until_round_completion',input_manifest=manifest)
                self.store.db.execute('INSERT INTO voice_link_commands VALUES(?,?,?,?)',(project_id,command_id,digest,canonical(result)))
                self.store.db.execute('COMMIT')
            except BaseException:
                self.store.db.execute('ROLLBACK'); raise
        return result

    def after_result(self, request):
        """Queue newer inputs only after the old frozen result is durably saved."""
        row = self.store.db.execute('SELECT id FROM voice_analysis_intents WHERE binding_id=?',(request['binding_id'],)).fetchone()
        if not row:
            return
        root, version = self.case(row['id'])
        frozen = self.store.db.execute('SELECT revision FROM voice_request_inputs WHERE request_id=?',(request['id'],)).fetchone()
        if version['revision'] > (frozen['revision'] if frozen else 1):
            command = 'voice.inputs.'+root['id']+'.'+str(version['revision'])
            if not self.store.db.execute('SELECT 1 FROM mcp_test_requests WHERE command_id=? AND binding_id=?',(command,root['binding_id'])).fetchone():
                self._new_request(root,version['revision'],request,fresh=True)

    def _supersede(self, request_id, root_id):
        self.store.db.execute('INSERT OR IGNORE INTO voice_superseded_requests VALUES(?,?)',(request_id,root_id))
        self.store.db.execute("UPDATE mcp_test_requests SET state='completed' WHERE id=?",(request_id,))
        self.store.db.execute("UPDATE mcp_outbox SET state='cancelled',lease_until_ms=NULL,last_error='voice_source_linked' WHERE request_id=?",(request_id,))

    def _new_request(self, root, revision, base, fresh=False):
        request = dict(base)
        now = _now_ms()
        request.update(id='mcp_test_'+secrets.token_hex(16),event_id='evt_'+secrets.token_hex(16),
            command_id='voice.inputs.'+root['id']+'.'+str(revision),nonce=secrets.token_urlsafe(32),
            created_ms=now,expires_ms=now+7*86400_000,state='awaiting_response',response=None,
            response_actor_id=None,response_ms=None,response_id=None,result_json=None)
        if fresh:
            request.update(request_kind='question_start',analysis_id=None)
        self.store.db.execute('INSERT INTO mcp_test_requests('+','.join(request)+') VALUES('+','.join('?' for _ in request)+')',tuple(request.values()))
        self.freeze(request['id'],root['binding_id'])
        for sub in self.store.db.execute("SELECT * FROM mcp_subscriptions WHERE project_id=? AND event_name='projects_hub.request.created' AND state='active' AND expires_ms>?",(root['project_id'],now)).fetchall():
            try:
                self.events.bindings.access(sub['grant_id'],root['project_id'],root['binding_id'],root['owner_id'],1)
            except StoreError:
                continue
            self.store.db.execute("INSERT INTO mcp_outbox(event_id,subscription_id,request_id,state,next_ms) VALUES(?,?,?,'pending',?)",(request['event_id'],sub['id'],request['id'],now))
        return request['id']
