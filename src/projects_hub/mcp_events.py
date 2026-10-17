"""Projects Hub's small MCP Events P0 domain, on the existing durable store."""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import secrets

from cryptography.fernet import Fernet

from .mcp_auth import MCPAuth
from .mcp_webhooks import CallbackError, WebhookSender, callback_parts, validate_secret
from .store import DurableStore, StoreError, _now_ms

log = logging.getLogger('projects_hub.mcp')
EVENT = 'projects_hub.test_requested'
REQUEST_EVENT = 'projects_hub.request.created'


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms/1000, timezone.utc).isoformat().replace('+00:00', 'Z')


def subscription_id(grant_id: str, url: str, project_id: str, name: str = EVENT) -> str:
    identity = json.dumps([grant_id, url, name, {'project_id': project_id}], sort_keys=True, separators=(',', ':'))
    return 'sub_' + hashlib.sha256(identity.encode()).hexdigest()


class MCPEvents:
    def __init__(self, store: DurableStore, auth: MCPAuth, session_secret: str, sender=None, *, release_sha='development'):
        self.store, self.auth = store, auth
        self.sender = sender or WebhookSender()
        self.release_sha = release_sha
        # Domain separation; reuse the existing private deployment key without
        # minting a new credential. Secrets never enter tool responses or logs.
        key = hashlib.sha256(('projects-hub:mcp-webhook:v1:' + session_secret).encode()).digest()
        self.cipher = Fernet(base64.urlsafe_b64encode(key))
        with store._lock:
            store.db.executescript('''
                CREATE TABLE IF NOT EXISTS mcp_subscriptions(
                    id TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES mcp_grants(id),
                    project_id TEXT NOT NULL REFERENCES projects(id), url TEXT NOT NULL,
                    secret_enc TEXT NOT NULL, old_secret_enc TEXT, rotation_until_ms INTEGER,
                    verified_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('active','stopped')),
                    updated_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mcp_test_requests(
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    actor_id TEXT NOT NULL REFERENCES actors(id), command_id TEXT NOT NULL,
                    nonce TEXT NOT NULL UNIQUE, created_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('awaiting_response','completed')),
                    event_id TEXT NOT NULL UNIQUE, response TEXT, response_actor_id TEXT,
                    response_ms INTEGER, UNIQUE(actor_id,project_id,command_id)
                );
                CREATE TABLE IF NOT EXISTS mcp_outbox(
                    event_id TEXT NOT NULL, subscription_id TEXT NOT NULL REFERENCES mcp_subscriptions(id),
                    request_id TEXT NOT NULL REFERENCES mcp_test_requests(id),
                    state TEXT NOT NULL CHECK(state IN ('pending','in_flight','accepted','error','cancelled')),
                    attempts INTEGER NOT NULL DEFAULT 0, next_ms INTEGER NOT NULL,
                    lease_until_ms INTEGER, accepted_ms INTEGER, last_status INTEGER, last_error TEXT,
                    PRIMARY KEY(event_id,subscription_id)
                );
                CREATE INDEX IF NOT EXISTS mcp_outbox_pending ON mcp_outbox(state,next_ms);
            ''')
            # Additive migration: preserve old requests and use their requester
            # as context until an explicitly supplied contextual actor exists.
            if 'context_actor_id' not in {row['name'] for row in store.db.execute('PRAGMA table_info(mcp_test_requests)')}:
                store.db.execute('ALTER TABLE mcp_test_requests ADD COLUMN context_actor_id TEXT REFERENCES actors(id)')
            for table, column, definition in (
                ('mcp_subscriptions', 'event_name', f"TEXT NOT NULL DEFAULT '{EVENT}'"),
                ('mcp_test_requests', 'event_name', f"TEXT NOT NULL DEFAULT '{EVENT}'"),
                ('mcp_test_requests', 'mode', "TEXT NOT NULL DEFAULT 'test' CHECK(mode IN ('test','live'))"),
                ('mcp_test_requests', 'response_id', 'TEXT'),
                ('mcp_test_requests', 'result_json', 'TEXT'),
                ('mcp_test_requests', 'binding_id', 'TEXT'),
                ('mcp_test_requests', 'binding_revision', 'INTEGER'),
                ('mcp_test_requests', 'request_kind', "TEXT NOT NULL DEFAULT 'discussion'"),
                ('mcp_test_requests', 'source_revision', 'INTEGER'),
                ('mcp_test_requests', 'analysis_id', 'TEXT'),
                ('mcp_test_requests', 'initiation_origin', "TEXT NOT NULL DEFAULT 'mira'"),
            ):
                if column not in {r['name'] for r in store.db.execute(f'PRAGMA table_info({table})')}:
                    store.db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
            from .mcp_bindings import MCPBindings
            self.bindings = MCPBindings(store, auth, self)

    def voice_context(self, request_id):
        adapter = getattr(self, 'voice_analysis', None)
        return adapter.context(request_id) if adapter else None

    def _encrypt(self, value: str) -> str:
        return self.cipher.encrypt(value.encode()).decode()

    def _log(self, message: str, **fields) -> None:
        log.info(message, extra={'backend_release_sha': self.release_sha, **fields})

    def _decrypt(self, value: str) -> str:
        return self.cipher.decrypt(value.encode()).decode()

    def _access(self, grant_id: str, project_id: str) -> dict:
        return self.auth.project_grant(grant_id, project_id)

    def identity(self, grant_id: str, name: str, arguments: dict, delivery: dict) -> tuple[str, str, str]:
        if name not in (EVENT, REQUEST_EVENT) or set(arguments) != {'project_id'} or not isinstance(arguments['project_id'], str) or not arguments['project_id']:
            raise StoreError('INVALID_ARGUMENT', 'Event requires exactly one project_id')
        project = arguments['project_id']
        self._access(grant_id, project)
        url = delivery.get('url')
        if delivery.get('mode', 'webhook') != 'webhook' or not isinstance(url, str):
            raise StoreError('INVALID_ARGUMENT', 'HTTPS webhook delivery required')
        callback_parts(url)
        return subscription_id(grant_id, url, project, name), project, url

    async def subscribe(self, grant_id: str, name: str, arguments: dict, delivery: dict,
                        ttl_ms: int | None = 86400_000, cursor: str | None = None) -> dict:
        sub_id, project, url = self.identity(grant_id, name, arguments, delivery)
        secret = delivery.get('secret', '')
        validate_secret(secret)
        if ttl_ms is not None and (type(ttl_ms) is not int or ttl_ms <= 0):
            raise StoreError('INVALID_ARGUMENT', 'ttlMs must be positive or null')
        # P0 grants finite TTL even if the client asks for no expiration.
        lifetime = min(ttl_ms if ttl_ms is not None else 86400_000, 86400_000)
        now = _now_ms()
        with self.store._lock:
            old = self.store.db.execute('SELECT * FROM mcp_subscriptions WHERE id=?', (sub_id,)).fetchone()
            cached = bool(old and old['state'] == 'active' and old['verified_ms'] > now - 300_000
                          and secrets.compare_digest(self._decrypt(old['secret_enc']), secret))
        if not cached:
            await self.sender.verify(url, sub_id, secret)
        with self.store._lock:
            grant = self._access(grant_id, project)  # Recheck after network I/O.
            now = _now_ms()
            old = self.store.db.execute('SELECT * FROM mcp_subscriptions WHERE id=?', (sub_id,)).fetchone()
            previous, until = None, None
            if old:
                old_key = self._decrypt(old['secret_enc'])
                if old_key != secret:
                    previous, until = old['secret_enc'], now + 300_000
                elif old['rotation_until_ms'] and old['rotation_until_ms'] > now:
                    previous, until = old['old_secret_enc'], old['rotation_until_ms']
            expires = min(now + lifetime, grant['expires_ms'])
            self.store.db.execute('''INSERT INTO mcp_subscriptions
                (id,grant_id,project_id,url,secret_enc,old_secret_enc,rotation_until_ms,verified_ms,expires_ms,state,updated_ms,event_name)
                VALUES(?,?,?,?,?,?,?,?,?, 'active',?,?)
                ON CONFLICT(id) DO UPDATE SET secret_enc=excluded.secret_enc,
                old_secret_enc=excluded.old_secret_enc,rotation_until_ms=excluded.rotation_until_ms,
                verified_ms=excluded.verified_ms,expires_ms=excluded.expires_ms,state='active',updated_ms=excluded.updated_ms''',
                (sub_id, grant_id, project, url, self._encrypt(secret), previous, until,
                 old['verified_ms'] if cached else now, expires, now, name))
            if name == REQUEST_EVENT:
                self._backfill_question_requests(sub_id, grant_id, project, now)
        self._log('MCP subscription verified', event='mcp_subscription', subscription_id=sub_id,
                  project_id=project, result='active')
        return dict(id=sub_id, refreshBefore=iso(expires), cursor=None, truncated=False)

    def _backfill_question_requests(self, sub_id, grant_id, project, now):
        """Renewed transport resumes durable question requests without new grants."""
        requests = self.store.db.execute(
            """SELECT * FROM mcp_test_requests WHERE project_id=? AND event_name=?
               AND mode='live' AND request_kind IN ('question_start','questions_ready')
               AND state='awaiting_response' AND expires_ms>?""",
            (project, REQUEST_EVENT, now),
        ).fetchall()
        for request in requests:
            try:
                binding = self.bindings.access(
                    grant_id, project, request['binding_id'],
                    request['context_actor_id'] or request['actor_id'],
                    request['binding_revision'],
                )
                if not binding['questions_allowed']:
                    continue
            except StoreError:
                continue
            self.store.db.execute(
                """INSERT INTO mcp_outbox(event_id,subscription_id,request_id,state,next_ms)
                   VALUES(?,?,?,'pending',?)
                   ON CONFLICT(event_id,subscription_id) DO UPDATE SET
                   state='pending',next_ms=excluded.next_ms,lease_until_ms=NULL,last_error=NULL
                   WHERE mcp_outbox.state='cancelled'
                     AND mcp_outbox.last_error='access_or_lifetime_ended'""",
                (request['event_id'], sub_id, request['id'], now),
            )

    def _question_delivery(self, request_id, event_id):
        queued = self.store.db.execute(
            """SELECT 1 FROM mcp_outbox o JOIN mcp_subscriptions s ON s.id=o.subscription_id
               WHERE o.request_id=? AND o.state IN ('pending','in_flight','accepted')
                 AND s.state='active' AND s.expires_ms>? LIMIT 1""",
            (request_id, _now_ms()),
        ).fetchone()
        return dict(request_id=request_id, event_id=event_id,
                    delivery_state='queued' if queued else 'waiting_event_transport')

    def unsubscribe(self, grant_id: str, name: str, arguments: dict, delivery: dict) -> dict:
        sub_id, _, _ = self.identity(grant_id, name, arguments, delivery)
        with self.store._lock:
            self.store.db.execute("UPDATE mcp_subscriptions SET state='stopped',updated_ms=? WHERE id=?", (_now_ms(), sub_id))
        self._log('MCP subscription stopped', event='mcp_subscription', subscription_id=sub_id, result='stopped')
        return {}

    def create_request(self, actor_id: str, project_id: str, command_id: str, ttl_seconds: int = 900,
                       *, context_actor_id: str | None = None, event_name: str = EVENT,
                       binding_id: str | None = None, request_kind: str = 'discussion',
                       origin: str = 'mira') -> dict:
        if not isinstance(command_id, str) or not 8 <= len(command_id) <= 160 or not 60 <= ttl_seconds <= 3600:
            raise StoreError('INVALID_ARGUMENT', 'Command id or request lifetime invalid')
        with self.store._lock:
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                self.auth.project_owner(actor_id, project_id)
                context_actor_id = context_actor_id or actor_id
                project = self.store.db.execute('SELECT workspace_id FROM projects WHERE id=?', (project_id,)).fetchone()
                self.store.project_access(context_actor_id, project['workspace_id'], project_id)
                if (event_name not in (EVENT, REQUEST_EVENT) or (binding_id and event_name != REQUEST_EVENT)
                        or request_kind not in ('discussion','question_start')
                        or origin not in ('mira','slack','voice') or
                        (request_kind == 'question_start' and (not binding_id or event_name != REQUEST_EVENT))):
                    raise StoreError('INVALID_ARGUMENT', 'Unknown request event')
                binding = self.bindings.for_owner(actor_id, project_id, binding_id, context_actor_id) if binding_id else None
                if request_kind == 'question_start' and not binding['questions_allowed']:
                    raise StoreError('PROJECT_FORBIDDEN', 'Owner has not approved external question analysis')
                frozen_source_revision = int(self.bindings._note(actor_id,project_id,binding['note_id'])['revision']) if binding else None
                old = self.store.db.execute('SELECT id FROM mcp_test_requests WHERE actor_id=? AND project_id=? AND command_id=?',
                                            (actor_id, project_id, command_id)).fetchone()
                if old:
                    prior = self.store.db.execute('SELECT * FROM mcp_test_requests WHERE id=?', (old['id'],)).fetchone()
                    if (prior['context_actor_id'] or prior['actor_id']) != context_actor_id:
                        raise StoreError('REQUEST_CONFLICT', 'Command id already used with a different user context')
                    if (prior['event_name'] != event_name or prior['binding_id'] != binding_id
                            or prior['request_kind'] != request_kind or prior['initiation_origin'] != origin):
                        raise StoreError('REQUEST_CONFLICT', 'Command id already used for another route or mode')
                    self.store.db.execute('COMMIT')
                    return self.status(actor_id, old['id'])
                now = _now_ms()
                subscriptions = self.store.db.execute("SELECT * FROM mcp_subscriptions WHERE project_id=? AND event_name=? AND state='active' AND expires_ms>?", (project_id, event_name, now)).fetchall()
                eligible = []
                for sub in subscriptions:
                    try:
                        self._access(sub['grant_id'], project_id)
                        if binding:
                            self.bindings.access(sub['grant_id'], project_id, binding_id, context_actor_id, binding['revision'])
                        eligible.append(sub)
                    except StoreError:
                        continue
                if not eligible and origin != 'voice':
                    raise StoreError('NO_SUBSCRIPTION', 'Connect and approve a verified project subscription first')
                request_id, event_id = 'mcp_test_' + secrets.token_hex(16), 'evt_' + secrets.token_hex(16)
                self.store.db.execute('''INSERT INTO mcp_test_requests
                    (id,project_id,actor_id,command_id,nonce,created_ms,expires_ms,state,event_id,context_actor_id,event_name,mode,binding_id,binding_revision,request_kind,source_revision,initiation_origin)
                    VALUES(?,?,?,?,?,?,?,'awaiting_response',?,?,?,?,?,?,?,?,?)''',
                    (request_id, project_id, actor_id, command_id, secrets.token_urlsafe(32), now, now + (7*86400_000 if origin == 'voice' else ttl_seconds*1000), event_id, context_actor_id,
                     event_name, 'live' if binding else 'test', binding_id, binding['revision'] if binding else None,
                     request_kind,frozen_source_revision,origin))
                if getattr(self,'voice_analysis',None) and origin == 'voice':
                    self.voice_analysis.joins.freeze(request_id,binding_id)
                for sub in eligible:
                    self.store.db.execute("INSERT INTO mcp_outbox(event_id,subscription_id,request_id,state,next_ms) VALUES(?,?,?,'pending',?)", (event_id, sub['id'], request_id, now))
                self.store.db.execute('COMMIT')
            except BaseException:
                self.store.db.execute('ROLLBACK')
                raise
        self._log('MCP test requested', event='mcp_test_created', request_id=request_id,
                  event_id=event_id, project_id=project_id)
        return self.status(actor_id, request_id)


    def start_question_session(self, actor_id: str, project_id: str, note_id: str,
                               command_id: str, *, origin: str = 'mira') -> dict:
        """Mira/approved workspace actor requests dot analysis via existing Events.

        The owner-approved binding supplies both service identity and immutable
        Slack destination. Never infer a Slack thread from the latest message.
        """
        if origin not in ('mira', 'slack'):
            raise StoreError('INVALID_ARGUMENT', 'Invalid question origin')
        with self.store._lock:
            project = self.store.db.execute(
                "SELECT workspace_id FROM projects WHERE id=? AND status='active'",
                (project_id,),
            ).fetchone()
            if not project:
                raise StoreError('PROJECT_FORBIDDEN', 'Project unavailable')
            self.store.project_access(actor_id, project['workspace_id'], project_id, require_analyze=True)
            binding = self.store.db.execute(
                """SELECT * FROM mcp_discussion_bindings
                   WHERE project_id=? AND note_id=? AND state='active' AND questions_allowed=1""",
                (project_id, note_id),
            ).fetchone()
            if not binding:
                raise StoreError('QUESTION_ROUTE_REQUIRED', 'Owner-approved source and Slack thread are required')
            owner_id = binding['owner_actor_id']
            self.bindings._note(owner_id, project_id, note_id, editor=True)
        return self.create_request(
            owner_id, project_id, command_id, ttl_seconds=3600,
            context_actor_id=actor_id, event_name=REQUEST_EVENT, binding_id=binding['id'],
            request_kind='question_start', origin=origin,
        )


    def start_question_session_from_service(self, grant_id: str, project_id: str, binding_id: str,
                                            command_id: str, slack_workspace_id: str,
                                            slack_channel_id: str, slack_thread_ts: str) -> dict:
        """Dot's Slack-origin request: never accept an arbitrary destination."""
        with self.store._lock:
            principal=self.auth.project_grant(grant_id,project_id)
            binding=self.store.db.execute(
                "SELECT * FROM mcp_discussion_bindings WHERE id=? AND project_id=? AND state='active'",
                (binding_id,project_id),
            ).fetchone()
            if (not binding or not binding['questions_allowed'] or binding['client_id']!=principal['client_id']
                    or (binding['slack_workspace_id'],binding['slack_channel_id'],binding['slack_thread_ts']) !=
                       (slack_workspace_id,slack_channel_id,slack_thread_ts)):
                raise StoreError('PROJECT_FORBIDDEN','Question session requires exact approved Slack thread')
            owner_id=binding['owner_actor_id']
            self.bindings.access(grant_id,project_id,binding_id,owner_id,binding['revision'])
        return self.start_question_session(
            owner_id,project_id,binding['note_id'],command_id,origin='slack',
        )

    def enqueue_question_ready(self, analysis):
        """Create one typed continuation request inside the caller's answer transaction."""
        if analysis['model_alias'] != 'dot' or analysis['package_state'] not in ('all_answered', 'partially_completed'):
            raise StoreError('REQUEST_UNAVAILABLE', 'Question package not ready')
        owner, project = analysis['initiating_actor_id'], analysis['project_id']
        self.auth.project_owner(owner, project)
        binding_id = json.loads(analysis['scope_json'])['binding_id']
        binding = self.bindings.for_owner(owner, project, binding_id, analysis['addressed_to_actor_id'])
        if (not binding['questions_allowed'] or binding['revision'] != analysis['slack_binding_revision']
                or (binding['slack_workspace_id'],binding['slack_channel_id'],binding['slack_thread_ts']) !=
                   (analysis['slack_workspace_id'],analysis['slack_channel_id'],analysis['slack_thread_ts'])):
            raise StoreError('REQUEST_UNAVAILABLE', 'Question binding changed')
        command = 'questions.ready.' + analysis['id']
        old = self.store.db.execute(
            'SELECT * FROM mcp_test_requests WHERE actor_id=? AND project_id=? AND command_id=?',
            (owner, project, command),
        ).fetchone()
        if old:
            if old['request_kind'] != 'questions_ready' or old['analysis_id'] != analysis['id']:
                raise StoreError('REQUEST_CONFLICT', 'Question continuation already differs')
            return self._question_delivery(old['id'], old['event_id'])
        now = _now_ms()
        request_id = 'mcp_test_' + secrets.token_hex(16)
        event_id = 'evt_' + secrets.token_hex(16)
        self.store.db.execute(
            """INSERT INTO mcp_test_requests
               (id,project_id,actor_id,command_id,nonce,created_ms,expires_ms,state,event_id,
                context_actor_id,event_name,mode,binding_id,binding_revision,request_kind,
                source_revision,analysis_id,initiation_origin)
               VALUES(?,?,?,?,?,?,?,'awaiting_response',?,?,?,?,?,?,?,?,?,?)""",
            (request_id,project,owner,command,secrets.token_urlsafe(32),now,now+7*86400_000,
             event_id,analysis['addressed_to_actor_id'],REQUEST_EVENT,'live',
             binding_id,binding['revision'],'questions_ready',
             analysis['source_revision'],analysis['id'],analysis['origin']),
        )
        if getattr(self,'voice_analysis',None) and analysis['origin'] == 'voice':
            self.voice_analysis.joins.freeze(request_id,binding_id)
        for sub in self.store.db.execute(
            "SELECT * FROM mcp_subscriptions WHERE project_id=? AND event_name=? AND state='active' AND expires_ms>?",
            (project,REQUEST_EVENT,now),
        ).fetchall():
            try:
                self.bindings.access(sub['grant_id'],project,binding_id,
                                     analysis['addressed_to_actor_id'],binding['revision'])
            except StoreError:
                continue
            self.store.db.execute(
                "INSERT INTO mcp_outbox(event_id,subscription_id,request_id,state,next_ms) VALUES(?,?,?,'pending',?)",
                (event_id,sub['id'],request_id,now),
            )
        return self._question_delivery(request_id, event_id)

    def _context(self, row):
        actor_id = row['context_actor_id'] or row['actor_id']
        project = self.store.db.execute('SELECT workspace_id FROM projects WHERE id=?', (row['project_id'],)).fetchone()
        if not project:
            raise StoreError('REQUEST_UNAVAILABLE', 'Test request unavailable')
        self.store.project_access(actor_id, project['workspace_id'], row['project_id'])
        actor = self.store.db.execute('SELECT display_name FROM actors WHERE id=?', (actor_id,)).fetchone()
        return actor_id, actor['display_name']

    def _request(self, grant_id: str, request_id: str, project_id: str | None = None, actor_id: str | None = None):
        row = self.store.db.execute('SELECT * FROM mcp_test_requests WHERE id=?', (request_id,)).fetchone()
        if not row:
            raise StoreError('REQUEST_UNAVAILABLE', 'Test request unavailable')
        try:
            grant = self._access(grant_id, row['project_id'])
            context_actor_id, _ = self._context(row)
            if (project_id is not None and project_id != row['project_id']) or (actor_id is not None and actor_id != context_actor_id):
                raise StoreError('REQUEST_UNAVAILABLE', 'Request context mismatch')
        except StoreError:
            raise StoreError('REQUEST_UNAVAILABLE', 'Test request unavailable') from None
        if row['expires_ms'] <= _now_ms():
            raise StoreError('REQUEST_UNAVAILABLE', 'Test request unavailable')
        return row, grant

    def get_request(self, grant_id: str, request_id: str, *, project_id: str | None = None, actor_id: str | None = None) -> dict:
        with self.store._lock:
            row, grant = self._request(grant_id, request_id, project_id, actor_id)
            if row['mode'] != 'test':
                raise StoreError('REQUEST_UNAVAILABLE', 'Legacy tools only accept test requests')
            access = self.auth.project_owner(grant['actor_id'], row['project_id'])
            context_actor_id, actor_name = self._context(row)
            return dict(request_id=row['id'], project_id=row['project_id'], actor_id=context_actor_id, actor_name=actor_name, eventId=row['event_id'],
                        nonce=row['nonce'], state=row['state'], created_at=iso(row['created_ms']),
                        expires_at=iso(row['expires_ms']), project_name=access['project_name'])

    def record_response(self, grant_id: str, request_id: str, nonce: str, response: str,
                        *, project_id: str | None = None, actor_id: str | None = None) -> dict:
        if not isinstance(nonce, str) or not isinstance(response, str) or not 1 <= len(response) <= 4000:
            raise StoreError('INVALID_ARGUMENT', 'Response must contain 1–4000 characters')
        with self.store._lock:
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                row, grant = self._request(grant_id, request_id, project_id, actor_id)
                if row['mode'] != 'test':
                    raise StoreError('REQUEST_UNAVAILABLE', 'Legacy tools only accept test requests')
                if not secrets.compare_digest(row['nonce'].encode(), nonce.encode()):
                    raise StoreError('NONCE_MISMATCH', 'Nonce does not match request')
                if row['state'] == 'completed':
                    if row['response'] != response or row['response_actor_id'] != grant['actor_id']:
                        raise StoreError('RESPONSE_CONFLICT', 'A different response is already saved')
                else:
                    self.store.db.execute("UPDATE mcp_test_requests SET state='completed',response=?,response_actor_id=?,response_ms=?,response_id=?,result_json=? WHERE id=?",
                                          (response, grant['actor_id'], _now_ms(), self.response_id(request_id),
                                           json.dumps(dict(kind='test_ack', nonce=nonce, text=response), ensure_ascii=False, sort_keys=True), request_id))
                self.store.db.execute('COMMIT')
            except BaseException:
                self.store.db.execute('ROLLBACK')
                raise
        self._log('MCP test response saved', event='mcp_test_response', request_id=request_id,
                  event_id=row['event_id'], result='completed')
        return self.status(grant['actor_id'], request_id)

    @staticmethod
    def response_id(request_id: str) -> str:
        return 'mcp_response_' + hashlib.sha256(request_id.encode()).hexdigest()[:32]

    def get_project_request(self, grant_id: str, project_id: str, request_id: str) -> dict:
        with self.store._lock:
            row, _ = self._request(grant_id, request_id, project_id)
            if row['mode'] == 'live':
                if getattr(self,'voice_analysis',None):
                    redirected = self.voice_analysis.joins.superseded(row['id'])
                    if redirected:
                        self.bindings.access(grant_id,project_id,row['binding_id'],row['actor_id'],row['binding_revision'])
                        return dict(project_id=project_id,request_id=row['id'],event_id=row['event_id'],**redirected)
                return self.bindings.get_request(grant_id, row)
            data = self.get_request(grant_id, request_id, project_id=project_id)
            data.update(mode='test', event_id=row['event_id'], response_id=self.response_id(request_id) if row['state']=='completed' else None,
                        backend_state='stored' if row['state']=='completed' else 'awaiting_result',
                        allowed_result_kinds=['test_ack'],
                        result=json.loads(row['result_json']) if row['result_json'] else
                            dict(kind='test_ack',nonce=row['nonce'],text=row['response']) if row['state']=='completed' else None)
            return data

    async def record_request_result_async(self, grant_id: str, project_id: str,
                                          request_id: str, event_id: str, result: dict) -> dict:
        if not isinstance(result, dict) or result.get('kind') != 'analysis_result':
            return self.record_request_result(grant_id, project_id, request_id, event_id, result)
        with self.store._lock:
            row, _ = self._request(grant_id, request_id, project_id)
            if row['event_id'] != event_id or row['mode'] != 'live':
                raise StoreError('REQUEST_UNAVAILABLE', 'Request event mismatch')
            request = dict(row)
        # Repository I/O never holds the SQLite/process lock. The binding
        # handler rechecks authorization and exact source revision after readback.
        return await self.bindings.record_result_async(grant_id, request, result)

    def record_request_result(self, grant_id: str, project_id: str, request_id: str, event_id: str, result: dict) -> dict:
        with self.store._lock:
            row, _ = self._request(grant_id, request_id, project_id)
            if row['event_id'] != event_id:
                raise StoreError('REQUEST_UNAVAILABLE', 'Request event mismatch')
            if row['mode'] == 'live':
                return self.bindings.record_result(grant_id, row, result)
            if (not isinstance(result, dict) or set(result) != {'kind','nonce','text'} or result['kind'] != 'test_ack'
                    or not isinstance(result['nonce'], str) or not 1 <= len(result['nonce']) <= 128
                    or not isinstance(result['text'], str) or not 1 <= len(result['text']) <= 4000):
                raise StoreError('INVALID_ARGUMENT', 'Expected strict test_ack result')
            self.record_response(grant_id, request_id, result['nonce'], result['text'], project_id=project_id)
            return self.get_project_request(grant_id, project_id, request_id)

    def status(self, actor_id: str, request_id: str) -> dict:
        with self.store._lock:
            row = self.store.db.execute('SELECT * FROM mcp_test_requests WHERE id=?', (request_id,)).fetchone()
            if not row:
                raise StoreError('REQUEST_UNAVAILABLE', 'Test request unavailable')
            self.auth.project_owner(actor_id, row['project_id'])
            deliveries = [dict(r) for r in self.store.db.execute('''SELECT event_id,subscription_id,state,attempts,
                accepted_ms,last_status,last_error FROM mcp_outbox WHERE request_id=?''', (request_id,))]
            return dict(request_id=row['id'], project_id=row['project_id'], actor_id=row['context_actor_id'] or row['actor_id'], eventId=row['event_id'],
                        event_name=row['event_name'],mode=row['mode'],binding_id=row['binding_id'],binding_revision=row['binding_revision'],
                        request_kind=row['request_kind'],analysis_id=row['analysis_id'],
                        response_id=self.response_id(request_id) if row['state']=='completed' else None,
                        created_at=iso(row['created_ms']), expires_at=iso(row['expires_ms']), state=row['state'],
                        response=row['response'], response_actor_id=row['response_actor_id'],
                        response_at=iso(row['response_ms']) if row['response_ms'] else None, deliveries=deliveries)

    async def deliver_one(self) -> bool:
        now = _now_ms()
        with self.store._lock:
            row = self.store.db.execute('''SELECT o.*,s.grant_id,s.project_id,s.url,s.secret_enc,
                s.old_secret_enc,s.rotation_until_ms,s.state AS sub_state,s.expires_ms AS sub_expires,
                r.created_ms,r.expires_ms AS request_expires,r.state AS request_state,r.event_name,r.mode,r.binding_id,r.binding_revision,
                COALESCE(r.context_actor_id,r.actor_id) AS context_actor_id,r.request_kind,r.analysis_id
                FROM mcp_outbox o JOIN mcp_subscriptions s ON s.id=o.subscription_id
                JOIN mcp_test_requests r ON r.id=o.request_id
                WHERE (o.state='pending' AND o.next_ms<=?) OR (o.state='in_flight' AND o.lease_until_ms<=?)
                ORDER BY o.next_ms LIMIT 1''', (now, now)).fetchone()
            if not row:
                return False
            event_id, sub_id = row['event_id'], row['subscription_id']
            allowed = row['sub_state'] == 'active' and min(row['sub_expires'], row['request_expires']) > now and row['request_state'] == 'awaiting_response'
            try:
                self._access(row['grant_id'], row['project_id'])
                project = self.store.db.execute('SELECT workspace_id FROM projects WHERE id=?', (row['project_id'],)).fetchone()
                self.store.project_access(row['context_actor_id'], project['workspace_id'], row['project_id'])
                if row['mode'] == 'live':
                    self.bindings.access(row['grant_id'], row['project_id'], row['binding_id'], row['context_actor_id'], row['binding_revision'])
            except StoreError:
                allowed = False
            if not allowed:
                self.store.db.execute("UPDATE mcp_outbox SET state='cancelled',last_error='access_or_lifetime_ended' WHERE event_id=? AND subscription_id=?", (event_id, sub_id))
                return True
            claimed = self.store.db.execute("""UPDATE mcp_outbox SET state='in_flight',attempts=attempts+1,lease_until_ms=?
                WHERE event_id=? AND subscription_id=? AND
                ((state='pending' AND next_ms<=?) OR (state='in_flight' AND lease_until_ms<=?))""",
                (now + 30_000, event_id, sub_id, now, now))
            if claimed.rowcount != 1:
                return True
        data = dict(request_id=row['request_id'], project_id=row['project_id'], summary='A Projects Hub request was created.')
        if row['event_name'] == EVENT:
            data.update(actor_id=row['context_actor_id'])
        else:
            data.update(mode=row['mode'])
            if row['request_kind'] != 'discussion':
                data.update(request_kind=row['request_kind'])
                if row['analysis_id']:
                    data.update(analysis_id=row['analysis_id'])
        event = dict(eventId=event_id, name=row['event_name'], timestamp=iso(row['created_ms']), cursor=None, data=data)
        status, reason = None, None
        try:
            old_key = self._decrypt(row['old_secret_enc']) if row['old_secret_enc'] and row['rotation_until_ms'] > now else None
            status, _ = await self.sender.send(row['url'], sub_id, event_id, event, self._decrypt(row['secret_enc']), old_key)
            accepted = 200 <= status < 300
            reason = None if accepted else 'http_5xx' if status >= 500 else 'http_4xx'
        except CallbackError as exc:
            accepted, reason = False, exc.reason
        except Exception:
            # Do not log exception text: crypto/HTTP libraries may include secrets.
            accepted, reason = False, 'delivery_internal_error'
        attempts = row['attempts'] + 1
        retry = not accepted and attempts < 5 and (status is None or status >= 500 or status in (408, 425, 429))
        state = 'accepted' if accepted else 'pending' if retry else 'error'
        with self.store._lock:
            self.store.db.execute('''UPDATE mcp_outbox SET state=?,next_ms=?,lease_until_ms=NULL,
                accepted_ms=?,last_status=?,last_error=? WHERE event_id=? AND subscription_id=?''',
                (state, _now_ms() + min(60_000, 1000 * 2**attempts), _now_ms() if accepted else None,
                 status, reason, event_id, sub_id))
        self._log('MCP event delivery', event='mcp_event_delivery', request_id=row['request_id'],
                  event_id=event_id, subscription_id=sub_id, project_id=row['project_id'],
                  status=status, reason=reason, result=state, delivery_attempt=attempts)
        return True

    @asynccontextmanager
    async def running(self):
        async def worker():
            while True:
                if not await self.deliver_one():
                    await asyncio.sleep(1)
        task = asyncio.create_task(worker(), name='projects-hub-mcp-outbox')
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
