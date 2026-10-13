"""Resource-bound OAuth public-client + PKCE, using first-party owner consent.

No browser session is accepted as a machine credential. Tokens are opaque,
hashed at rest, and bound to one owner-approved project and MCP resource.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from urllib.parse import urlsplit

from .store import DurableStore, StoreError, _now_ms

SCOPE = 'projects-hub:test'
REDIRECT = 'https://chatgpt.com/connector_platform_oauth_redirect'


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def challenge(value: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()).decode().rstrip('=')


def valid_redirect(value: str) -> bool:
    return value == REDIRECT or bool(re.fullmatch(r'https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]{1,200}', value))


class MCPAuth:
    def __init__(self, store: DurableStore, origin: str):
        self.store = store
        self.origin = origin.rstrip('/')
        self.resource = self.origin + '/mcp'
        with store._lock:
            store.db.executescript('''
                CREATE TABLE IF NOT EXISTS mcp_clients(
                    id TEXT PRIMARY KEY, redirects_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mcp_consent_flows(
                    id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES mcp_clients(id),
                    redirect_uri TEXT NOT NULL, state TEXT NOT NULL, challenge TEXT NOT NULL,
                    expires_ms INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS mcp_grants(
                    id TEXT PRIMARY KEY, actor_id TEXT NOT NULL REFERENCES actors(id),
                    project_id TEXT NOT NULL REFERENCES projects(id), client_id TEXT NOT NULL,
                    created_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL,
                    revoked_ms INTEGER
                );
                CREATE TABLE IF NOT EXISTS mcp_codes(
                    code_hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES mcp_grants(id),
                    redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL,
                    expires_ms INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS mcp_tokens(
                    token_hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES mcp_grants(id),
                    kind TEXT NOT NULL, expires_ms INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0
                );
            ''')

    def register(self, payload: dict) -> dict:
        redirects = payload.get('redirect_uris')
        grants = payload.get('grant_types', ['authorization_code', 'refresh_token'])
        if (not isinstance(redirects, list) or not 1 <= len(redirects) <= 4
                or any(not isinstance(v, str) or not valid_redirect(v) for v in redirects)
                or payload.get('token_endpoint_auth_method', 'none') != 'none'
                or not isinstance(grants, list) or any(not isinstance(g, str) for g in grants)
                or set(grants) - {'authorization_code', 'refresh_token'}
                or 'authorization_code' not in grants
                or payload.get('response_types', ['code']) != ['code']):
            raise StoreError('invalid_client_metadata', 'Unsupported public OAuth client')
        redirects = sorted(set(redirects))
        client_id = 'ph_client_' + digest(json.dumps(redirects))[:32]
        with self.store._lock:
            self.store.db.execute('INSERT OR IGNORE INTO mcp_clients VALUES(?,?)', (client_id, json.dumps(redirects)))
        return dict(client_id=client_id, redirect_uris=redirects, token_endpoint_auth_method='none',
                    grant_types=['authorization_code', 'refresh_token'], response_types=['code'])

    def begin(self, params: dict) -> str:
        client_id = params.get('client_id', '')
        uri = params.get('redirect_uri', '')
        pkce = params.get('code_challenge', '')
        state = params.get('state', '')
        with self.store._lock:
            client = self.store.db.execute('SELECT * FROM mcp_clients WHERE id=?', (client_id,)).fetchone()
            if (not client or uri not in json.loads(client['redirects_json'])
                    or params.get('resource') != self.resource or params.get('scope', SCOPE) != SCOPE
                    or params.get('response_type') != 'code' or params.get('code_challenge_method') != 'S256'
                    or not re.fullmatch(r'[A-Za-z0-9_-]{43}', pkce)
                    or not 1 <= len(state) <= 2048):
                raise StoreError('invalid_request', 'Invalid authorization request')
            now = _now_ms()
            self.store.db.execute('DELETE FROM mcp_consent_flows WHERE expires_ms<?', (now,))
            if self.store.db.execute('SELECT COUNT(*) FROM mcp_consent_flows').fetchone()[0] >= 1000:
                raise StoreError('temporarily_unavailable', 'Too many pending consent requests')
            flow = secrets.token_urlsafe(32)
            self.store.db.execute('INSERT INTO mcp_consent_flows VALUES(?,?,?,?,?,?,0)',
                                 (flow, client_id, uri, state, pkce, now + 600_000))
        return flow

    def consent(self, flow: str, actor_id: str, project_id: str) -> dict:
        with self.store._lock:
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                row = self.store.db.execute('SELECT * FROM mcp_consent_flows WHERE id=?', (flow,)).fetchone()
                if not row or row['used'] or row['expires_ms'] <= _now_ms():
                    raise StoreError('invalid_request', 'Consent request expired or already used')
                self.project_owner(actor_id, project_id)
                now = _now_ms()
                grant_id = 'mcp_grant_' + secrets.token_hex(16)
                code = secrets.token_urlsafe(32)
                self.store.db.execute('INSERT INTO mcp_grants VALUES(?,?,?,?,?,?,NULL)',
                                     (grant_id, actor_id, project_id, row['client_id'], now, now + 30 * 86400_000))
                self.store.db.execute('INSERT INTO mcp_codes VALUES(?,?,?,?,?,0)',
                                     (digest(code), grant_id, row['redirect_uri'], row['challenge'], now + 60_000))
                self.store.db.execute('UPDATE mcp_consent_flows SET used=1 WHERE id=?', (flow,))
                self.store.db.execute('COMMIT')
                return dict(code=code, state=row['state'], redirect_uri=row['redirect_uri'], iss=self.origin, grant_id=grant_id)
            except BaseException:
                self.store.db.execute('ROLLBACK')
                raise

    def project_owner(self, actor_id: str, project_id: str) -> dict:
        row = self.store.db.execute('SELECT workspace_id FROM projects WHERE id=?', (project_id,)).fetchone()
        if not row:
            raise StoreError('PROJECT_FORBIDDEN', 'Project unavailable')
        return self.store.project_access(actor_id, row['workspace_id'], project_id, require_role='owner')

    def grant(self, grant_id: str) -> dict:
        with self.store._lock:
            row = self.store.db.execute('SELECT * FROM mcp_grants WHERE id=?', (grant_id,)).fetchone()
            if not row or row['revoked_ms'] is not None or row['expires_ms'] <= _now_ms():
                raise StoreError('invalid_token', 'Integration grant unavailable')
            self.project_owner(row['actor_id'], row['project_id'])
            return dict(row)

    def authenticate(self, header: str) -> dict:
        if not header.startswith('Bearer ') or not 20 <= len(header) <= 512:
            raise StoreError('invalid_token', 'Bearer token required')
        with self.store._lock:
            row = self.store.db.execute('SELECT * FROM mcp_tokens WHERE token_hash=? AND kind=?',
                                        (digest(header[7:]), 'access')).fetchone()
            if not row or row['used'] or row['expires_ms'] <= _now_ms():
                raise StoreError('invalid_token', 'Access token unavailable')
            return self.grant(row['grant_id'])

    def exchange(self, params: dict) -> dict:
        if params.get('resource') != self.resource or params.get('scope', SCOPE) != SCOPE:
            raise StoreError('invalid_target', 'Resource or scope mismatch')
        with self.store._lock:
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                kind = params.get('grant_type')
                if kind == 'authorization_code':
                    row = self.store.db.execute('SELECT * FROM mcp_codes WHERE code_hash=?',
                                                (digest(params.get('code', '')),)).fetchone()
                    verifier = params.get('code_verifier', '')
                    if (not row or row['used'] or row['expires_ms'] <= _now_ms()
                            or row['redirect_uri'] != params.get('redirect_uri')
                            or not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', verifier)
                            or not secrets.compare_digest(row['challenge'], challenge(verifier))):
                        raise StoreError('invalid_grant', 'Invalid authorization code or PKCE')
                    self.store.db.execute('UPDATE mcp_codes SET used=1 WHERE code_hash=?', (row['code_hash'],))
                elif kind == 'refresh_token':
                    row = self.store.db.execute('SELECT * FROM mcp_tokens WHERE token_hash=? AND kind=?',
                                                (digest(params.get('refresh_token', '')), 'refresh')).fetchone()
                    if not row or row['expires_ms'] <= _now_ms():
                        raise StoreError('invalid_grant', 'Invalid refresh token')
                    # Bind the client before treating reuse as a grant-wide compromise.
                    grant = self.grant(row['grant_id'])
                    if grant['client_id'] != params.get('client_id'):
                        raise StoreError('invalid_client', 'OAuth client mismatch')
                    if row['used']:
                        self.store.db.execute('UPDATE mcp_grants SET revoked_ms=? WHERE id=?', (_now_ms(), row['grant_id']))
                        self.store.db.execute('COMMIT')
                        raise StoreError('invalid_grant', 'Refresh token reuse revoked the grant')
                    self.store.db.execute('UPDATE mcp_tokens SET used=1 WHERE token_hash=?', (row['token_hash'],))
                else:
                    raise StoreError('unsupported_grant_type', 'Only code and refresh supported')
                grant = self.grant(row['grant_id'])
                if grant['client_id'] != params.get('client_id'):
                    raise StoreError('invalid_client', 'OAuth client mismatch')
                now = _now_ms()
                access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                expires = min(now + 3600_000, grant['expires_ms'])
                for token, token_kind, deadline in [(access, 'access', expires), (refresh, 'refresh', grant['expires_ms'])]:
                    self.store.db.execute('INSERT INTO mcp_tokens VALUES(?,?,?,?,0)',
                                         (digest(token), grant['id'], token_kind, deadline))
                self.store.db.execute('COMMIT')
                return dict(access_token=access, refresh_token=refresh, token_type='Bearer',
                            expires_in=max(0, (expires-now)//1000), scope=SCOPE)
            except BaseException:
                if self.store.db.in_transaction:
                    self.store.db.execute('ROLLBACK')
                raise

    def revoke(self, actor_id: str, grant_id: str) -> None:
        with self.store._lock:
            self.store.db.execute('UPDATE mcp_grants SET revoked_ms=? WHERE id=? AND actor_id=?',
                                 (_now_ms(), grant_id, actor_id))

    def revoke_token(self, token: str, client_id: str) -> None:
        with self.store._lock:
            self.store.db.execute('''UPDATE mcp_grants SET revoked_ms=? WHERE client_id=? AND id IN
                (SELECT grant_id FROM mcp_tokens WHERE token_hash=?)''', (_now_ms(), client_id, digest(token)))
