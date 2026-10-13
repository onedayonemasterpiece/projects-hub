"""Owner-approved discussion routes; existing notes/replies are the Mira context.

There is no Slack sender here. The dot uses its existing Slack connector after
claiming a send, then records its receipt. An unknown send is never retried
without reconciliation. All operations recheck the binding and current ACL.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3

from .store import StoreError, _now_ms


def valid_ts(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[0-9]{10}\.[0-9]{6}', value))


class MCPBindings:
    def __init__(self, store, auth, events):
        self.store, self.auth, self.events = store, auth, events
        store.db.executescript('''
            CREATE TABLE IF NOT EXISTS mcp_discussion_bindings(
                id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),
                note_id TEXT NOT NULL REFERENCES project_notes(id),owner_actor_id TEXT NOT NULL REFERENCES actors(id),
                client_id TEXT NOT NULL REFERENCES mcp_service_clients(client_id),
                slack_workspace_id TEXT NOT NULL,slack_channel_id TEXT NOT NULL,slack_thread_ts TEXT NOT NULL,
                mira_note_id TEXT NOT NULL REFERENCES project_notes(id),permission TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('active','revoked')),revision INTEGER NOT NULL,
                created_ms INTEGER NOT NULL,updated_ms INTEGER NOT NULL,
                UNIQUE(project_id,note_id),UNIQUE(slack_workspace_id,slack_channel_id,slack_thread_ts)
            );
            CREATE TABLE IF NOT EXISTS mcp_binding_history(
                binding_id TEXT NOT NULL REFERENCES mcp_discussion_bindings(id),revision INTEGER NOT NULL,
                actor_id TEXT NOT NULL REFERENCES actors(id),route_json TEXT NOT NULL,created_ms INTEGER NOT NULL,
                PRIMARY KEY(binding_id,revision)
            );
            CREATE TABLE IF NOT EXISTS mcp_response_destinations(
                response_id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE REFERENCES mcp_test_requests(id),
                reply_id TEXT NOT NULL,backend_state TEXT NOT NULL,mira_state TEXT NOT NULL,
                slack_state TEXT NOT NULL CHECK(slack_state IN ('pending','claimed','unconfirmed','error','confirmed')),
                claim_id TEXT,slack_message_id TEXT,error_code TEXT,updated_ms INTEGER NOT NULL
            );
        ''')

    def _note(self, actor_id, project_id, note_id, *, editor=False):
        project = self.store.db.execute('SELECT workspace_id FROM projects WHERE id=? AND status=\'active\'', (project_id,)).fetchone()
        if not project:
            raise StoreError('REQUEST_UNAVAILABLE', 'Discussion unavailable')
        self.store.project_access(actor_id, project['workspace_id'], project_id, **({'require_role':'editor'} if editor else {}))
        note = self.store.db.execute('SELECT * FROM project_notes WHERE id=? AND project_id=? AND workspace_id=? AND status=\'ready\'',
                                     (note_id, project_id, project['workspace_id'])).fetchone()
        if not note:
            raise StoreError('REQUEST_UNAVAILABLE', 'Discussion unavailable')
        return note

    def _audit(self, row, actor_id):
        self.store.db.execute('INSERT INTO mcp_binding_history VALUES(?,?,?,?,?)',
                             (row['id'],row['revision'],actor_id,json.dumps(dict(row),sort_keys=True),_now_ms()))

    def bind(self, owner_id, project_id, note_id, client_id, workspace_id, channel_id, thread_ts, *, expected_revision=0):
        self.store.require_platform_owner(owner_id)
        if (not isinstance(workspace_id,str) or not re.fullmatch(r'T[A-Z0-9]{8,}',workspace_id)
                or not isinstance(channel_id,str) or not re.fullmatch(r'[CG][A-Z0-9]{8,}',channel_id)
                or not valid_ts(thread_ts) or type(expected_revision) is not int or expected_revision<0):
            raise StoreError('INVALID_ARGUMENT','Invalid confirmed Slack destination or revision')
        with self.store._lock:
            self._note(owner_id, project_id, note_id, editor=True)
            self.auth.service_client(client_id)
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                old=self.store.db.execute('SELECT * FROM mcp_discussion_bindings WHERE project_id=? AND note_id=?',(project_id,note_id)).fetchone()
                route=(client_id,workspace_id,channel_id,thread_ts)
                if old and old['state']=='active' and tuple(old[k] for k in ('client_id','slack_workspace_id','slack_channel_id','slack_thread_ts'))==route:
                    self.store.db.execute('COMMIT')
                    return dict(old)
                if (old['revision'] if old else 0)!=expected_revision:
                    raise StoreError('BINDING_CONFLICT','Expected current binding revision')
                binding_id=old['id'] if old else 'mcp_binding_'+secrets.token_hex(16)
                now=_now_ms()
                self.store.db.execute('''INSERT INTO mcp_discussion_bindings VALUES(?,?,?,?,?,?,?,?,?,'discussion_reply','active',?,?,?)
                    ON CONFLICT(id) DO UPDATE SET owner_actor_id=excluded.owner_actor_id,client_id=excluded.client_id,
                    slack_workspace_id=excluded.slack_workspace_id,slack_channel_id=excluded.slack_channel_id,
                    slack_thread_ts=excluded.slack_thread_ts,state='active',revision=excluded.revision,updated_ms=excluded.updated_ms''',
                    (binding_id,project_id,note_id,owner_id,client_id,workspace_id,channel_id,thread_ts,note_id,
                     expected_revision+1,old['created_ms'] if old else now,now))
                row=self.store.db.execute('SELECT * FROM mcp_discussion_bindings WHERE id=?',(binding_id,)).fetchone()
                self._audit(row,owner_id)
                self.store.db.execute('COMMIT')
            except sqlite3.IntegrityError:
                self.store.db.execute('ROLLBACK')
                raise StoreError('BINDING_CONFLICT','Discussion or Slack route is already bound') from None
            except BaseException:
                self.store.db.execute('ROLLBACK')
                raise
        self.events._log('MCP discussion binding saved',event='mcp_binding',binding_id=binding_id,project_id=project_id,
                         binding_revision=row['revision'],result='active')
        return dict(row)

    def revoke(self, owner_id, binding_id, expected_revision):
        with self.store._lock:
            self.store.require_platform_owner(owner_id)
            row=self.store.db.execute('SELECT * FROM mcp_discussion_bindings WHERE id=?',(binding_id,)).fetchone()
            if not row or row['revision']!=expected_revision:
                raise StoreError('BINDING_CONFLICT','Expected current binding revision')
            self._note(owner_id,row['project_id'],row['note_id'],editor=True)
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                self.store.db.execute("UPDATE mcp_discussion_bindings SET state='revoked',revision=revision+1,updated_ms=? WHERE id=?",(_now_ms(),binding_id))
                row=self.store.db.execute('SELECT * FROM mcp_discussion_bindings WHERE id=?',(binding_id,)).fetchone()
                self._audit(row,owner_id)
                self.store.db.execute('COMMIT')
            except BaseException:
                self.store.db.execute('ROLLBACK');raise
        self.events._log('MCP discussion binding revoked',event='mcp_binding',binding_id=binding_id,result='revoked')
        return dict(row)

    def for_owner(self, owner_id, project_id, binding_id, context_actor_id):
        self.store.require_platform_owner(owner_id)
        row=self.store.db.execute('SELECT * FROM mcp_discussion_bindings WHERE id=? AND project_id=? AND state=\'active\'',(binding_id,project_id)).fetchone()
        if not row or row['owner_actor_id']!=owner_id:
            raise StoreError('REQUEST_UNAVAILABLE','Discussion binding unavailable')
        self._note(owner_id,project_id,row['note_id'],editor=True)
        self._note(context_actor_id,project_id,row['note_id'])
        return dict(row)

    def access(self, grant_id, project_id, binding_id, context_actor_id, revision):
        try:
            principal=self.auth.project_grant(grant_id,project_id)
            row=self.store.db.execute('SELECT * FROM mcp_discussion_bindings WHERE id=? AND project_id=? AND state=\'active\'',(binding_id,project_id)).fetchone()
            if (not row or row['revision']!=revision or row['client_id']!=principal['client_id']
                    or row['permission']!='discussion_reply' or row['mira_note_id']!=row['note_id']):
                raise StoreError('REQUEST_UNAVAILABLE','Binding unavailable')
            self._note(row['owner_actor_id'],project_id,row['note_id'],editor=True)
            self._note(context_actor_id,project_id,row['note_id'])
            self.auth.service_client(row['client_id'])
            return dict(row)
        except StoreError:
            raise StoreError('REQUEST_UNAVAILABLE','Discussion unavailable') from None

    def _binding(self, grant_id, request):
        return self.access(grant_id,request['project_id'],request['binding_id'],request['context_actor_id'] or request['actor_id'],request['binding_revision'])

    def get_request(self, grant_id, request):
        binding=self._binding(grant_id,request)
        note=self._note(request['context_actor_id'] or request['actor_id'],request['project_id'],binding['note_id'])
        replies=self.store.db.execute('SELECT id,author_actor_id,body,created_at_ms FROM project_discussion_entries WHERE note_id=? ORDER BY created_at_ms DESC,id DESC LIMIT 50',(note['id'],)).fetchall()
        destination=self.store.db.execute('SELECT * FROM mcp_response_destinations WHERE request_id=?',(request['id'],)).fetchone()
        return dict(project_id=request['project_id'],request_id=request['id'],event_id=request['event_id'],mode='live',
                    state=request['state'],actor_id=request['context_actor_id'] or request['actor_id'],expires_at=self._iso(request['expires_ms']),
                    binding_id=binding['id'],binding_revision=binding['revision'],note_id=note['id'],
                    discussion={'title':note['title'],'body':note['body'],'revision':note['revision'],'replies':[dict(r) for r in reversed(replies)]},
                    allowed_result_kinds=['discussion_reply','slack_claim','slack_receipt'],
                    result=json.loads(request['result_json']) if request['result_json'] else None,
                    response_id=destination['response_id'] if destination else None,
                    destinations=dict(destination) if destination else {'backend_state':'awaiting_result','mira_state':'awaiting_result','slack_state':'pending'},
                    slack={'workspace_id':binding['slack_workspace_id'],'channel_id':binding['slack_channel_id'],'thread_ts':binding['slack_thread_ts']})

    @staticmethod
    def _iso(ms):
        from .mcp_events import iso
        return iso(ms)

    def record_result(self, grant_id, request, result):
        if not isinstance(result,dict):
            raise StoreError('INVALID_ARGUMENT','Typed result required')
        binding=self._binding(grant_id,request)
        principal=self.auth.project_grant(grant_id,request['project_id'])
        kind=result.get('kind')
        response_id=self.events.response_id(request['id'])
        now=_now_ms()
        self.store.db.execute('BEGIN IMMEDIATE')
        try:
            # Reload under the transaction: concurrent retries cannot append twice.
            current=self.store.db.execute('SELECT * FROM mcp_test_requests WHERE id=?',(request['id'],)).fetchone()
            destination=self.store.db.execute('SELECT * FROM mcp_response_destinations WHERE request_id=?',(request['id'],)).fetchone()
            if kind=='discussion_reply':
                if set(result)!={'kind','text'} or not isinstance(result['text'],str) or not 1<=len(result['text'].strip())<=12000:
                    raise StoreError('INVALID_ARGUMENT','Strict discussion_reply text required')
                text=result['text'].strip()
                canonical=json.dumps(dict(kind=kind,text=text),ensure_ascii=False,sort_keys=True)
                if current['state']=='completed':
                    if current['result_json']!=canonical or current['response_actor_id']!=principal['actor_id']:
                        raise StoreError('RESPONSE_CONFLICT','Different result already saved')
                else:
                    note=self._note(binding['owner_actor_id'],request['project_id'],binding['note_id'],editor=True)
                    reply_id='reply_'+hashlib.sha256(response_id.encode()).hexdigest()[:32]
                    self.store.db.execute('INSERT INTO project_discussion_entries VALUES(?,?,?,?,?,?,?,?,?)',
                        (reply_id,note['workspace_id'],request['project_id'],note['id'],principal['actor_id'],text,response_id,hashlib.sha256(text.encode()).hexdigest(),now))
                    self.store.db.execute('''INSERT INTO collaboration_events(workspace_id,project_id,actor_id,kind,object_kind,object_id,parent_object_id,addressed_to_actor_id,summary,created_at_ms)
                        VALUES(?,?,?,'note_replied','reply',?,?,?,?,?)''',
                        (note['workspace_id'],request['project_id'],principal['actor_id'],reply_id,note['id'],request['context_actor_id'] or request['actor_id'],text[:240],now))
                    self.store.db.execute("UPDATE mcp_test_requests SET state='completed',response=?,response_actor_id=?,response_ms=?,response_id=?,result_json=? WHERE id=?",
                        (text,principal['actor_id'],now,response_id,canonical,request['id']))
                    self.store.db.execute("INSERT INTO mcp_response_destinations VALUES(?,?,?,'stored','available','pending',NULL,NULL,NULL,?)",(response_id,request['id'],reply_id,now))
            elif kind in ('slack_claim','slack_receipt'):
                if not destination or result.get('response_id')!=response_id:
                    raise StoreError('INVALID_ARGUMENT','Save a correlated discussion reply first')
                if kind=='slack_claim':
                    if set(result)!={'kind','response_id'}:
                        raise StoreError('INVALID_ARGUMENT','Strict slack_claim required')
                    if destination['slack_state'] in ('claimed','unconfirmed'):
                        raise StoreError('SLACK_RECONCILIATION_REQUIRED','Read the bound thread and reconcile the previous claim before any send')
                    if destination['slack_state']!='confirmed':
                        self.store.db.execute("UPDATE mcp_response_destinations SET slack_state='claimed',claim_id=?,error_code=NULL,updated_ms=? WHERE request_id=?",('claim_'+secrets.token_hex(16),now,request['id']))
                else:
                    fields={'kind','response_id','claim_id','status','message_id','error','workspace_id','channel_id','thread_ts'}
                    if (set(result)!=fields or not all(isinstance(v,str) for v in result.values())
                            or result['claim_id']!=destination['claim_id'] or result['status'] not in ('confirmed','unconfirmed','error')
                            or any(result[k]!=binding['slack_'+k] for k in ('workspace_id','channel_id','thread_ts'))):
                        raise StoreError('INVALID_ARGUMENT','Invalid receipt or substituted destination')
                    status=result['status']
                    if status=='confirmed':
                        if not valid_ts(result['message_id']) or result['message_id']==binding['slack_thread_ts'] or result['error']:
                            raise StoreError('INVALID_ARGUMENT','Confirmed Slack response message ID required')
                    elif result['message_id']:
                        raise StoreError('INVALID_ARGUMENT','Unconfirmed result cannot claim a message ID')
                    if status=='error' and result['error'] not in ('channel_not_found','not_in_channel','no_permission','msg_too_long','invalid_arguments','restricted_action','ratelimited'):
                        status='unconfirmed'
                    if destination['slack_state']=='confirmed':
                        if status!='confirmed' or result['message_id']!=destination['slack_message_id']:
                            raise StoreError('RESPONSE_CONFLICT','Confirmed Slack receipt cannot be overwritten')
                    else:
                        self.store.db.execute('UPDATE mcp_response_destinations SET slack_state=?,slack_message_id=?,error_code=?,updated_ms=? WHERE request_id=?',
                            (status,result['message_id'] or None,result['error'][:80] or None,now,request['id']))
            else:
                raise StoreError('INVALID_ARGUMENT','Unknown or unauthorized result kind')
            self.store.db.execute('COMMIT')
        except BaseException:
            self.store.db.execute('ROLLBACK');raise
        current=self.store.db.execute('SELECT * FROM mcp_test_requests WHERE id=?',(request['id'],)).fetchone()
        destination=self.store.db.execute('SELECT * FROM mcp_response_destinations WHERE request_id=?',(request['id'],)).fetchone()
        self.events._log('MCP request result recorded',event='mcp_project_result',request_id=request['id'],event_id=request['event_id'],
                         response_id=response_id,binding_id=binding['id'],binding_revision=binding['revision'],
                         result_kind=kind,slack_state=destination['slack_state'],mira_state=destination['mira_state'],result='stored')
        return self.get_request(grant_id,current)
