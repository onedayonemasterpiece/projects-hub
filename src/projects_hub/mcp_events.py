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


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms/1000, timezone.utc).isoformat().replace('+00:00', 'Z')


def subscription_id(grant_id: str, url: str, project_id: str) -> str:
    identity = json.dumps([grant_id, url, EVENT, {'project_id': project_id}], sort_keys=True, separators=(',', ':'))
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

    def _encrypt(self, value: str) -> str:
        return self.cipher.encrypt(value.encode()).decode()

    def _log(self, message: str, **fields) -> None:
        log.info(message, extra={'backend_release_sha': self.release_sha, **fields})

    def _decrypt(self, value: str) -> str:
        return self.cipher.decrypt(value.encode()).decode()

    def _access(self, grant_id: str, project_id: str) -> dict:
        grant = self.auth.grant(grant_id)
        if grant['project_id'] != project_id:
            raise StoreError('PROJECT_FORBIDDEN', 'Project outside integration scope')
        return grant

    def identity(self, grant_id: str, name: str, arguments: dict, delivery: dict) -> tuple[str, str, str]:
        if name != EVENT or set(arguments) != {'project_id'} or not isinstance(arguments['project_id'], str):
            raise StoreError('INVALID_ARGUMENT', 'Event requires exactly one project_id')
        project = arguments['project_id']
        self._access(grant_id, project)
        url = delivery.get('url')
        if delivery.get('mode', 'webhook') != 'webhook' or not isinstance(url, str):
            raise StoreError('INVALID_ARGUMENT', 'HTTPS webhook delivery required')
        callback_parts(url)
        return subscription_id(grant_id, url, project), project, url

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
            self.store.db.execute('''INSERT INTO mcp_subscriptions VALUES(?,?,?,?,?,?,?,?,?, 'active',?)
                ON CONFLICT(id) DO UPDATE SET secret_enc=excluded.secret_enc,
                old_secret_enc=excluded.old_secret_enc,rotation_until_ms=excluded.rotation_until_ms,
                verified_ms=excluded.verified_ms,expires_ms=excluded.expires_ms,state='active',updated_ms=excluded.updated_ms''',
                (sub_id, grant_id, project, url, self._encrypt(secret), previous, until,
                 old['verified_ms'] if cached else now, expires, now))
        self._log('MCP subscription verified', event='mcp_subscription', subscription_id=sub_id,
                  project_id=project, result='active')
        return dict(id=sub_id, refreshBefore=iso(expires), cursor=None, truncated=False)

    def unsubscribe(self, grant_id: str, name: str, arguments: dict, delivery: dict) -> dict:
        sub_id, _, _ = self.identity(grant_id, name, arguments, delivery)
        with self.store._lock:
            self.store.db.execute("UPDATE mcp_subscriptions SET state='stopped',updated_ms=? WHERE id=?", (_now_ms(), sub_id))
        self._log('MCP subscription stopped', event='mcp_subscription', subscription_id=sub_id, result='stopped')
        return {}

    def create_request(self, actor_id: str, project_id: str, command_id: str, ttl_seconds: int = 900) -> dict:
        if not isinstance(command_id, str) or not 8 <= len(command_id) <= 160 or not 60 <= ttl_seconds <= 3600:
            raise StoreError('INVALID_ARGUMENT', 'Command id or request lifetime invalid')
        with self.store._lock:
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                self.auth.project_owner(actor_id, project_id)
                old = self.store.db.execute('SELECT id FROM mcp_test_requests WHERE actor_id=? AND project_id=? AND command_id=?',
                                            (actor_id, project_id, command_id)).fetchone()
                if old:
                    self.store.db.execute('COMMIT')
                    return self.status(actor_id, old['id'])
                now = _now_ms()
                subscriptions = self.store.db.execute("SELECT * FROM mcp_subscriptions WHERE project_id=? AND state='active' AND expires_ms>?", (project_id, now)).fetchall()
                eligible = []
                for sub in subscriptions:
                    try:
                        self._access(sub['grant_id'], project_id)
                        eligible.append(sub)
                    except StoreError:
                        continue
                if not eligible:
                    raise StoreError('NO_SUBSCRIPTION', 'Connect and approve a verified project subscription first')
                request_id, event_id = 'mcp_test_' + secrets.token_hex(16), 'evt_' + secrets.token_hex(16)
                self.store.db.execute('''INSERT INTO mcp_test_requests
                    (id,project_id,actor_id,command_id,nonce,created_ms,expires_ms,state,event_id)
                    VALUES(?,?,?,?,?,?,?,'awaiting_response',?)''',
                    (request_id, project_id, actor_id, command_id, secrets.token_urlsafe(32), now, now + ttl_seconds*1000, event_id))
                for sub in eligible:
                    self.store.db.execute("INSERT INTO mcp_outbox(event_id,subscription_id,request_id,state,next_ms) VALUES(?,?,?,'pending',?)", (event_id, sub['id'], request_id, now))
                self.store.db.execute('COMMIT')
            except BaseException:
                self.store.db.execute('ROLLBACK')
                raise
        self._log('MCP test requested', event='mcp_test_created', request_id=request_id,
                  event_id=event_id, project_id=project_id)
        return self.status(actor_id, request_id)

    def _request(self, grant_id: str, request_id: str):
        row = self.store.db.execute('SELECT * FROM mcp_test_requests WHERE id=?', (request_id,)).fetchone()
        if not row:
            raise StoreError('REQUEST_UNAVAILABLE', 'Test request unavailable')
        try:
            grant = self._access(grant_id, row['project_id'])
        except StoreError:
            raise StoreError('REQUEST_UNAVAILABLE', 'Test request unavailable') from None
        if row['expires_ms'] <= _now_ms():
            raise StoreError('REQUEST_UNAVAILABLE', 'Test request unavailable')
        return row, grant

    def get_request(self, grant_id: str, request_id: str) -> dict:
        with self.store._lock:
            row, grant = self._request(grant_id, request_id)
            access = self.auth.project_owner(grant['actor_id'], row['project_id'])
            return dict(request_id=row['id'], project_id=row['project_id'], eventId=row['event_id'],
                        nonce=row['nonce'], state=row['state'], created_at=iso(row['created_ms']),
                        expires_at=iso(row['expires_ms']), project_name=access['project_name'])

    def record_response(self, grant_id: str, request_id: str, nonce: str, response: str) -> dict:
        if not isinstance(nonce, str) or not isinstance(response, str) or not 1 <= len(response) <= 4000:
            raise StoreError('INVALID_ARGUMENT', 'Response must contain 1–4000 characters')
        with self.store._lock:
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                row, grant = self._request(grant_id, request_id)
                if not secrets.compare_digest(row['nonce'].encode(), nonce.encode()):
                    raise StoreError('NONCE_MISMATCH', 'Nonce does not match request')
                if row['state'] == 'completed':
                    if row['response'] != response or row['response_actor_id'] != grant['actor_id']:
                        raise StoreError('RESPONSE_CONFLICT', 'A different response is already saved')
                else:
                    self.store.db.execute("UPDATE mcp_test_requests SET state='completed',response=?,response_actor_id=?,response_ms=? WHERE id=?",
                                          (response, grant['actor_id'], _now_ms(), request_id))
                self.store.db.execute('COMMIT')
            except BaseException:
                self.store.db.execute('ROLLBACK')
                raise
        self._log('MCP test response saved', event='mcp_test_response', request_id=request_id,
                  event_id=row['event_id'], result='completed')
        return self.status(grant['actor_id'], request_id)

    def status(self, actor_id: str, request_id: str) -> dict:
        with self.store._lock:
            row = self.store.db.execute('SELECT * FROM mcp_test_requests WHERE id=?', (request_id,)).fetchone()
            if not row:
                raise StoreError('REQUEST_UNAVAILABLE', 'Test request unavailable')
            self.auth.project_owner(actor_id, row['project_id'])
            deliveries = [dict(r) for r in self.store.db.execute('''SELECT event_id,subscription_id,state,attempts,
                accepted_ms,last_status,last_error FROM mcp_outbox WHERE request_id=?''', (request_id,))]
            return dict(request_id=row['id'], project_id=row['project_id'], eventId=row['event_id'],
                        created_at=iso(row['created_ms']), expires_at=iso(row['expires_ms']), state=row['state'],
                        response=row['response'], response_actor_id=row['response_actor_id'],
                        response_at=iso(row['response_ms']) if row['response_ms'] else None, deliveries=deliveries)

    async def deliver_one(self) -> bool:
        now = _now_ms()
        with self.store._lock:
            row = self.store.db.execute('''SELECT o.*,s.grant_id,s.project_id,s.url,s.secret_enc,
                s.old_secret_enc,s.rotation_until_ms,s.state AS sub_state,s.expires_ms AS sub_expires,
                r.created_ms,r.expires_ms AS request_expires,r.state AS request_state
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
        event = dict(eventId=event_id, name=EVENT, timestamp=iso(row['created_ms']), cursor=None,
                     data=dict(request_id=row['request_id'], project_id=row['project_id'], summary='A Projects Hub transport test was requested.'))
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
