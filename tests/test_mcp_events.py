from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import secrets
import socket
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest
from standardwebhooks.webhooks import Webhook, WebhookVerificationError

from projects_hub.app import create_app
from projects_hub.auth import COOKIE_NAME, issue_session
from projects_hub.mcp_api import EVENT_DEFINITION
from projects_hub.mcp_auth import MCPAuth, REDIRECT, SCOPE, challenge, digest
from projects_hub.mcp_events import EVENT, MCPEvents
from projects_hub.mcp_webhooks import (CallbackError, PinnedHTTPSConnection, WebhookSender,
                                       callback_parts, public_addresses, signed_headers, validate_secret)
from projects_hub.settings import Settings
from projects_hub.store import DurableStore, StoreError, _now_ms

ORIGIN = 'https://projects-hub.kenigevents.ru'
SECRET = 'whsec_' + base64.b64encode(b'x' * 32).decode()
DELIVERY = {'mode': 'webhook', 'url': 'https://receiver.example.com/events', 'secret': SECRET}


class Receiver:
    def __init__(self):
        self.verifications = []
        self.events = []
        self.status = 202

    async def verify(self, url, sub_id, secret):
        self.verifications.append((url, sub_id))

    async def send(self, url, sub_id, event_id, payload, secret, old_secret=None):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        headers = signed_headers(sub_id, event_id, body, secret, old_secret)
        assert Webhook(secret).verify(body, headers) == payload
        self.events.append((payload, headers))
        return self.status, b'{}'


@pytest.fixture
def setup(tmp_path):
    store = DurableStore(tmp_path / 'data')
    boot = store.ensure_platform_owner('Owner')
    actor, project = boot['actor']['id'], boot['projects'][0]['id']
    auth = MCPAuth(store, ORIGIN)
    client = auth.register({'redirect_uris': [REDIRECT]})['client_id']
    verifier = 'a' * 43
    flow = auth.begin(dict(client_id=client, redirect_uri=REDIRECT, response_type='code',
                           state='random-state', code_challenge=challenge(verifier), code_challenge_method='S256',
                           resource=auth.resource, scope=SCOPE))
    code = auth.consent(flow, actor, project)['code']
    tokens = auth.exchange(dict(grant_type='authorization_code', client_id=client, code=code,
                                 redirect_uri=REDIRECT, code_verifier=verifier, resource=auth.resource))
    grant = auth.authenticate('Bearer ' + tokens['access_token'])
    receiver = Receiver()
    events = MCPEvents(store, auth, 's' * 48, receiver)
    yield store, auth, events, receiver, grant, tokens
    store.close()


@pytest.mark.asyncio
async def test_atomic_request_delivery_write_idempotency_restart(setup):
    store, auth, events, receiver, grant, tokens = setup
    sub = await events.subscribe(grant['id'], EVENT, {'project_id': grant['project_id']}, DELIVERY)
    request = events.create_request(grant['actor_id'], grant['project_id'], 'command-test-1')
    assert 'nonce' not in request
    assert events.create_request(grant['actor_id'], grant['project_id'], 'command-test-1') == request
    assert store.db.execute('SELECT COUNT(*) FROM mcp_test_requests').fetchone()[0] == 1
    assert await events.deliver_one()
    payload, headers = receiver.events[0]
    assert payload['eventId'] == request['eventId'] == headers['webhook-id']
    assert payload['name'] == EVENT and payload['cursor'] is None and 'nonce' not in payload['data']
    assert set(payload['data']) == set(EVENT_DEFINITION['payloadSchema']['properties'])
    assert events.status(grant['actor_id'], request['request_id'])['state'] == 'awaiting_response'
    read = events.get_request(grant['id'], request['request_id'])
    with pytest.raises(StoreError, match='Nonce'):
        events.record_response(grant['id'], request['request_id'], 'wrong', 'ACK')
    assert events.status(grant['actor_id'], request['request_id'])['state'] == 'awaiting_response'
    result = events.record_response(grant['id'], request['request_id'], read['nonce'], 'ACK')
    assert result['state'] == 'completed' and result['response_actor_id'] == grant['actor_id']
    assert events.record_response(grant['id'], request['request_id'], read['nonce'], 'ACK') == result
    with pytest.raises(StoreError, match='different response'):
        events.record_response(grant['id'], request['request_id'], read['nonce'], 'changed')
    # A real second SQLite connection reconstructs all state with the same key.
    reopened = DurableStore(store.data_dir)
    try:
        restarted = MCPEvents(reopened, MCPAuth(reopened, ORIGIN), 's' * 48, receiver)
        assert restarted.status(grant['actor_id'], request['request_id']) == result
        assert (await restarted.subscribe(grant['id'], EVENT, {'project_id': grant['project_id']}, DELIVERY))['id'] == sub['id']
        assert not await restarted.deliver_one()
        assert len(receiver.events) == 1
        raw = reopened.db.execute('SELECT secret_enc FROM mcp_subscriptions').fetchone()[0]
        assert SECRET not in raw and restarted._decrypt(raw) == SECRET
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_pending_outbox_survives_restart_and_retries_stable_event(setup):
    store, auth, events, receiver, grant, _ = setup
    await events.subscribe(grant['id'], EVENT, {'project_id': grant['project_id']}, DELIVERY)
    request = events.create_request(grant['actor_id'], grant['project_id'], 'command-test-2')
    reopened = DurableStore(store.data_dir)
    try:
        restarted = MCPEvents(reopened, MCPAuth(reopened, ORIGIN), 's'*48, receiver)
        receiver.status = 503
        assert await restarted.deliver_one()
        assert restarted.status(grant['actor_id'], request['request_id'])['deliveries'][0]['state'] == 'pending'
        reopened.db.execute('UPDATE mcp_outbox SET next_ms=0')
        receiver.status = 202
        assert await restarted.deliver_one()
        assert receiver.events[0][0] == receiver.events[1][0]
        assert receiver.events[0][1]['webhook-id'] == receiver.events[1][1]['webhook-id']
        assert restarted.status(grant['actor_id'], request['request_id'])['deliveries'][0]['attempts'] == 2
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('revocation', ['grant', 'project', 'unsubscribe', 'expiry'])
async def test_revocation_stops_delivery_and_acl_tools(setup, revocation):
    store, auth, events, receiver, grant, _ = setup
    await events.subscribe(grant['id'], EVENT, {'project_id': grant['project_id']}, DELIVERY)
    request = events.create_request(grant['actor_id'], grant['project_id'], 'revocation-test')
    if revocation == 'grant':
        auth.revoke(grant['actor_id'], grant['id'])
    elif revocation == 'project':
        store.db.execute('UPDATE project_grants SET revoked_at_ms=1 WHERE project_id=?', (grant['project_id'],))
    elif revocation == 'unsubscribe':
        for _ in range(2):
            assert events.unsubscribe(grant['id'], EVENT, {'project_id': grant['project_id']}, DELIVERY) == {}
    else:
        store.db.execute('UPDATE mcp_subscriptions SET expires_ms=0')
    assert await events.deliver_one()
    assert not receiver.events
    assert store.db.execute('SELECT state FROM mcp_outbox').fetchone()[0] == 'cancelled'
    if revocation in ('grant', 'project'):
        with pytest.raises(StoreError):
            events.get_request(grant['id'], request['request_id'])
        with pytest.raises(StoreError):
            events.record_response(grant['id'], request['request_id'], 'anything', 'ACK')


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [410, 413, 400])
async def test_terminal_callback_failures_do_not_retry(setup, status):
    store, _, events, receiver, grant, _ = setup
    await events.subscribe(grant['id'], EVENT, {'project_id': grant['project_id']}, DELIVERY)
    request = events.create_request(grant['actor_id'], grant['project_id'], 'terminal-status')
    receiver.status = status
    assert await events.deliver_one()
    assert not await events.deliver_one()
    result = events.status(grant['actor_id'], request['request_id'])
    assert result['deliveries'][0]['state'] == 'error'
    assert result['state'] == 'awaiting_response' and result['response'] is None


@pytest.mark.asyncio
async def test_project_filter_substitution_and_expired_read(setup):
    store, auth, events, receiver, grant, _ = setup
    other = store.ensure_dev_workspace('Other user')
    foreign_project = other['projects'][0]['id']
    with pytest.raises(StoreError):
        await events.subscribe(grant['id'], EVENT, {'project_id': foreign_project}, DELIVERY)
    with pytest.raises(StoreError):
        await events.subscribe(grant['id'], EVENT, {'project_id': grant['project_id'], 'actor_id': grant['actor_id']}, DELIVERY)
    with pytest.raises(StoreError):
        events.create_request(grant['actor_id'], grant['project_id'], 'no-subscription')
    assert store.db.execute('SELECT COUNT(*) FROM mcp_test_requests').fetchone()[0] == 0
    await events.subscribe(grant['id'], EVENT, {'project_id': grant['project_id']}, DELIVERY)
    with pytest.raises(StoreError):
        events.create_request(other['actor']['id'], foreign_project, 'project-filter-test')
    assert not receiver.events
    request = events.create_request(grant['actor_id'], grant['project_id'], 'expired-read-test')
    store.db.execute('UPDATE mcp_test_requests SET expires_ms=0')
    with pytest.raises(StoreError):
        events.get_request(grant['id'], request['request_id'])


@pytest.mark.asyncio
async def test_subscription_refresh_rotation_and_ttl(setup):
    store, _, events, receiver, grant, _ = setup
    args = {'project_id': grant['project_id']}
    first = await events.subscribe(grant['id'], EVENT, args, DELIVERY, ttl_ms=10000)
    row = store.db.execute('SELECT * FROM mcp_subscriptions').fetchone()
    assert 0 < row['expires_ms'] - _now_ms() <= 10000
    second = await events.subscribe(grant['id'], EVENT, args, DELIVERY, ttl_ms=None)
    assert second['id'] == first['id'] and second['refreshBefore'] is not None
    assert len(receiver.verifications) == 1
    new = 'whsec_' + base64.b64encode(b'y'*32).decode()
    await events.subscribe(grant['id'], EVENT, args, {**DELIVERY, 'secret': new})
    assert len(receiver.verifications) == 2
    row = store.db.execute('SELECT * FROM mcp_subscriptions').fetchone()
    assert events._decrypt(row['secret_enc']) == new
    assert events._decrypt(row['old_secret_enc']) == SECRET
    events.create_request(grant['actor_id'], grant['project_id'], 'rotate-secret-test')
    await events.deliver_one()
    payload, headers = receiver.events[0]
    assert len(headers['webhook-signature'].split()) == 2
    body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
    assert Webhook(SECRET).verify(body, headers) == payload


@pytest.mark.parametrize('url', ['http://example.com/x', 'https://user:pass@example.com', 'https://example.com:444', 'https://example.com/#fragment', 'https://example.com/\r\nX:evil'])
def test_callback_url_contract(url):
    with pytest.raises(CallbackError):
        callback_parts(url)


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.2', '169.254.169.254', '::1', '::ffff:8.8.8.8', '224.0.0.1'])
def test_callback_private_and_mapped_addresses(monkeypatch, address):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))])
    with pytest.raises(CallbackError):
        public_addresses('receiver.example.com')


def test_dns_pinning_preserves_tls_hostname(monkeypatch):
    class Context:
        def wrap_socket(self, raw, server_hostname):
            self.hostname = server_hostname
            return raw
    class Raw:
        def close(self):
            pass
    calls = []
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
    monkeypatch.setattr(socket, 'create_connection', lambda address, **kw: calls.append(address) or Raw())
    conn = PinnedHTTPSConnection('receiver.example.com', timeout=10)
    conn._context = context = Context()
    conn.connect()
    assert calls == [('8.8.8.8', 443)] and context.hostname == 'receiver.example.com'


def test_standard_webhooks_bytes_tampering_rotation():
    body = b'{"nonce":"\xd0\xb4\xd0\xb0"}'
    headers = signed_headers('sub_a', 'evt_a', body, SECRET)
    expected = 'v1,' + base64.b64encode(hmac.new(b'x'*32, ('evt_a.'+headers['webhook-timestamp']+'.').encode()+body, hashlib.sha256).digest()).decode()
    assert headers['webhook-signature'] == expected
    assert Webhook(SECRET).verify(body, headers) == {'nonce': 'да'}
    with pytest.raises(WebhookVerificationError):
        Webhook(SECRET).verify(body + b' ', headers)
    headers['webhook-id'] = 'changed'
    with pytest.raises(WebhookVerificationError):
        Webhook(SECRET).verify(body, headers)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,returned', [(302, None), (200, 'wrong'), (503, None)])
async def test_verification_requires_success_and_echo(status, returned):
    class WrongReceiver(WebhookSender):
        async def send(self, url, sub_id, message_id, payload, secret, old_secret=None):
            assert payload['type'] == 'verification' and message_id.startswith('msg_verification_')
            return status, json.dumps({'challenge': returned or payload['challenge']}).encode()
    with pytest.raises(CallbackError):
        await WrongReceiver().verify(DELIVERY['url'], 'sub_a', SECRET)


def test_oauth_tokens_are_hashed_resource_bound_and_replay_protected(setup):
    store, auth, events, receiver, grant, tokens = setup
    assert auth.authenticate('Bearer '+tokens['access_token'])['actor_id'] == grant['actor_id']
    assert tokens['access_token'] not in [r[0] for r in store.db.execute('SELECT token_hash FROM mcp_tokens')]
    params = dict(grant_type='refresh_token', client_id=grant['client_id'], refresh_token=tokens['refresh_token'], resource=auth.resource)
    with pytest.raises(StoreError):
        auth.exchange({**params, 'resource': ORIGIN+'/other'})
    refreshed = auth.exchange(params)
    assert auth.authenticate('Bearer '+refreshed['access_token'])['id'] == grant['id']
    with pytest.raises(StoreError, match='reuse revoked'):
        auth.exchange(params)
    with pytest.raises(StoreError):
        auth.authenticate('Bearer '+refreshed['access_token'])


def edge_headers():
    return {'host': 'projects-hub.kenigevents.ru', 'x-forwarded-host': 'projects-hub.kenigevents.ru',
            'x-forwarded-proto': 'https', 'x-forwarded-port': '443'}


def rpc(client, token, method, params=None):
    meta = {'io.modelcontextprotocol/protocolVersion': '2026-07-28',
            'io.modelcontextprotocol/clientCapabilities': {},
            'io.modelcontextprotocol/clientInfo': {'name': 'p0-contract-test', 'version': '1.0'}}
    return client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': {**(params or {}), '_meta': meta}},
                       headers={**edge_headers(), 'Authorization': 'Bearer '+token, 'MCP-Protocol-Version': '2026-07-28', 'Mcp-Method': method,
                                **({'Mcp-Name': params['name']} if params and 'name' in params and method == 'tools/call' else {}),
                                'Accept': 'application/json, text/event-stream'})


def test_sdk_discovery_catalog_subscription_tools_and_csrf(setup, tmp_path):
    store, _, _, receiver, grant, tokens = setup
    settings = Settings(data_dir=store.data_dir, static_dir=tmp_path/'no-ui', session_secret='s'*48,
                        public_origin=ORIGIN, cookie_secure=True)
    app = create_app(settings, store=store)
    app.state.mcp_events.sender = receiver
    with TestClient(app, base_url=ORIGIN) as client:
        unauth = client.post('/mcp', json={}, headers=edge_headers())
        assert unauth.status_code == 401 and 'resource_metadata=' in unauth.headers['www-authenticate']
        token = tokens['access_token']
        discovery = rpc(client, token, 'server/discover')
        assert discovery.status_code == 200, discovery.text
        result = discovery.json()['result']
        assert result['resultType'] == 'complete'
        assert result['supportedVersions'] == ['2026-07-28'] and result['capabilities']['events'] == {}
        assert {t['name'] for t in rpc(client, token, 'tools/list').json()['result']['tools']} == {'get_test_request', 'record_test_response'}
        assert rpc(client, token, 'events/list').json()['result']['events'] == [EVENT_DEFINITION]
        subscribed = rpc(client, token, 'events/subscribe', dict(name=EVENT, arguments={'project_id': grant['project_id']}, delivery=DELIVERY))
        assert subscribed.status_code == 200, subscribed.text
        assert subscribed.json()['result']['id'].startswith('sub_')
        request = app.state.mcp_events.create_request(grant['actor_id'], grant['project_id'], 'sdk-contract-test')
        read = rpc(client, token, 'tools/call', {'name': 'get_test_request', 'arguments': {'request_id': request['request_id']}}).json()['result']
        assert read['structuredContent']['project_id'] == grant['project_id']
        nonce = read['structuredContent']['nonce']
        saved = rpc(client, token, 'tools/call', {'name': 'record_test_response', 'arguments': {'request_id': request['request_id'], 'nonce': nonce, 'response': 'ACK'}}).json()['result']
        assert saved['structuredContent']['state'] == 'completed'
        spoof = rpc(client, token, 'tools/call', {'name': 'record_test_response', 'arguments': {'request_id': request['request_id'], 'nonce': nonce, 'response': 'ACK', 'actor_id': 'other'}})
        assert 'error' in spoof.json()
        bad_sub = rpc(client, token, 'events/subscribe', dict(name=EVENT, arguments={'project_id': grant['project_id']}, delivery={**DELIVERY, 'secret': 'bad'}))
        assert bad_sub.json()['error']['code'] == -32602
        client.cookies.set(COOKIE_NAME, issue_session(grant['actor_id'], 's'*48))
        assert client.post('/mcp', json={}, headers=edge_headers()).status_code == 401
        for path in ['/mcp/oauth/consent', '/api/conversations', '/api/mcp/grants/'+grant['id']+'/revoke']:
            assert client.post(path, json={}, headers=edge_headers()).status_code == 403
        assert client.post('/mcp/oauth/register', json={'redirect_uris': ['https://evil.example.com']}, headers=edge_headers()).status_code == 400
        stopped = rpc(client, token, 'events/unsubscribe', dict(name=EVENT, arguments={'project_id': grant['project_id']}, delivery={'url': DELIVERY['url']}))
        assert stopped.status_code == 200 and stopped.json()['result']['resultType'] == 'complete'


def test_oauth_http_consent_pkce_replay_and_revocation(setup, tmp_path):
    store, _, _, _, grant, _ = setup
    app = create_app(Settings(data_dir=store.data_dir, static_dir=tmp_path/'no-ui',
                            session_secret='s'*48, public_origin=ORIGIN), store=store)
    with TestClient(app, base_url=ORIGIN) as client:
        metadata = client.get('/.well-known/oauth-authorization-server').json()
        assert metadata['code_challenge_methods_supported'] == ['S256']
        assert metadata['token_endpoint_auth_methods_supported'] == ['none']
        assert metadata['authorization_response_iss_parameter_supported'] is True
        resource = client.get('/.well-known/oauth-protected-resource/mcp').json()['resource']
        registered = client.post('/mcp/oauth/register', json={'redirect_uris': [REDIRECT]}, headers=edge_headers()).json()
        params = dict(client_id=registered['client_id'], redirect_uri=REDIRECT, response_type='code',
                      code_challenge=challenge('v'*43), code_challenge_method='S256', resource=resource, scope=SCOPE, state='opaque-state')
        assert client.get('/mcp/oauth/authorize', params=params).status_code == 401
        assert store.db.execute('SELECT COUNT(*) FROM mcp_grants').fetchone()[0] == 1
        client.cookies.set(COOKIE_NAME, issue_session(grant['actor_id'], 's'*48))
        page = client.get('/mcp/oauth/authorize', params=params)
        assert page.status_code == 200 and 'Разрешить подключение' in page.text
        import re
        flow = re.search(r'name="flow_id" value="([^"]+)"', page.text)[1]
        headers = {**edge_headers(), 'Origin': ORIGIN}
        accepted = client.post('/mcp/oauth/consent', data={'flow_id': flow, 'project_id': grant['project_id']}, headers=headers, follow_redirects=False)
        assert accepted.status_code == 303
        callback = parse_qs(urlsplit(accepted.headers['location']).query)
        assert callback['state'] == ['opaque-state'] and callback['iss'] == [ORIGIN]
        exchange = dict(grant_type='authorization_code', client_id=params['client_id'], resource=resource,
                        code=callback['code'][0], redirect_uri=REDIRECT, code_verifier='v'*43)
        for altered in [{**exchange, 'code_verifier': 'w'*43}, {**exchange, 'resource': ORIGIN}, {**exchange, 'client_id': 'other'}]:
            assert client.post('/mcp/oauth/token', data=altered, headers=edge_headers()).status_code == 400
        token = client.post('/mcp/oauth/token', data=exchange, headers=edge_headers()).json()['access_token']
        assert client.post('/mcp/oauth/token', data=exchange, headers=edge_headers()).status_code == 400
        assert rpc(client, token, 'server/discover').status_code == 200
        granted = app.state.mcp_auth.authenticate('Bearer '+token)
        revoked = client.post('/api/mcp/grants/'+granted['id']+'/revoke', json={}, headers=headers)
        assert revoked.status_code == 200
        assert rpc(client, token, 'server/discover').status_code == 401


@pytest.mark.asyncio
async def test_failed_callback_never_activates_subscription(setup):
    store, auth, events, receiver, grant, _ = setup
    class Fails(Receiver):
        async def verify(self, *args):
            raise CallbackError('challenge_failed')
    events.sender = Fails()
    with pytest.raises(CallbackError):
        await events.subscribe(grant['id'], EVENT, {'project_id': grant['project_id']}, DELIVERY)
    assert store.db.execute('SELECT COUNT(*) FROM mcp_subscriptions').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_unknown_delivery_lease_recovery_and_retry_limit(setup):
    store, _, events, receiver, grant, _ = setup
    await events.subscribe(grant['id'], EVENT, {'project_id': grant['project_id']}, DELIVERY)
    request = events.create_request(grant['actor_id'], grant['project_id'], 'lease-recovery-test')
    store.db.execute("UPDATE mcp_outbox SET state='in_flight',attempts=1,lease_until_ms=0")
    receiver.status = 503
    for _ in range(4):
        assert await events.deliver_one()
        store.db.execute('UPDATE mcp_outbox SET next_ms=0')
    assert not await events.deliver_one()
    result = events.status(grant['actor_id'], request['request_id'])
    assert result['deliveries'][0]['state'] == 'error' and result['deliveries'][0]['attempts'] == 5


@pytest.mark.parametrize('secret', ['', 'x'*32, 'whsec_bad!', 'whsec_'+base64.b64encode(b'x'*23).decode(), 'whsec_'+base64.b64encode(b'x'*65).decode()])
def test_secret_contract(secret):
    with pytest.raises(CallbackError):
        validate_secret(secret)
