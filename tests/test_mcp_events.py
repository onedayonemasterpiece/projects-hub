from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import secrets
import socket
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest
from standardwebhooks.webhooks import Webhook, WebhookVerificationError

from projects_hub.app import create_app
from projects_hub.auth import COOKIE_NAME, issue_session
from projects_hub.mcp_api import EVENT_DEFINITION, REQUEST_EVENT_DEFINITION
from projects_hub.mcp_auth import MCPAuth, REDIRECT, SCOPE, challenge, digest
from projects_hub.logging_config import JsonFormatter
from projects_hub.mcp_events import EVENT, REQUEST_EVENT, MCPEvents
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


def test_oauth_discovery_never_falls_through_to_built_spa(setup, tmp_path):
    store, _, _, _, _, _ = setup
    static = tmp_path / 'ui'
    static.mkdir()
    (static / 'index.html').write_text('<!doctype html><title>Projects Hub</title>')
    app = create_app(Settings(data_dir=store.data_dir, static_dir=static,
                            session_secret='s'*48, public_origin=ORIGIN), store=store)
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get('/project/example').headers['content-type'].startswith('text/html')
        metadata = client.get('/.well-known/oauth-authorization-server')
        assert metadata.status_code == 200 and metadata.json()['issuer'] == ORIGIN
        oidc = client.get('/.well-known/openid-configuration')
        assert oidc.status_code == 404 and oidc.json() == {'error': 'not_found'}
        assert oidc.headers['cache-control'] == 'no-store'
        assert client.get('/.well-known/oauth-protected-resource').json()['resource'] == ORIGIN+'/mcp'


def test_unauthenticated_mcp_logs_only_enumerated_protocol_facts(setup, tmp_path, caplog):
    store, _, _, _, _, _ = setup
    app = create_app(Settings(data_dir=store.data_dir, static_dir=tmp_path/'no-ui',
                            session_secret='s'*48, public_origin=ORIGIN, release_sha='onboarding-release'), store=store)
    caplog.set_level(logging.INFO, logger='projects_hub.mcp')
    private = 'private-bearer-and-payload'
    with TestClient(app, base_url=ORIGIN) as client:
        # App startup replaces root handlers with production JSON logging.
        logging.getLogger('projects_hub.mcp').addHandler(caplog.handler)
        for method, version, expected in [('initialize', '2025-11-25', ('initialize', '2025-11-25')),
                                          (private, private, ('other', 'unknown'))]:
            denied = client.post('/mcp', headers={**edge_headers(), 'Authorization': 'Bearer '+private},
                json={'jsonrpc': '2.0', 'id': 1, 'method': method,
                      'params': {'protocolVersion': version, 'nonce': private, 'clientInfo': {'name': private}}})
            assert denied.status_code == 401 and 'resource_metadata=' in denied.headers['www-authenticate']
            record = next(r for r in reversed(caplog.records) if getattr(r, 'event', None) == 'mcp_auth_boundary')
            output = JsonFormatter().format(record)
            assert private not in output
            facts = json.loads(output)
            assert (facts['rpc_method'], facts['mcp_protocol']) == expected
            assert facts['reason'] == 'invalid_credential' and facts['backend_release_sha'] == 'onboarding-release'
        assert client.post('/mcp', headers=edge_headers(), content=b'invalid json').status_code == 401
        assert client.get('/mcp', headers=edge_headers()).status_code == 401


def test_catalog_logs_describe_wire_response_without_private_request_data(setup, tmp_path, caplog):
    store, _, _, _, _, tokens = setup
    app = create_app(Settings(data_dir=store.data_dir, static_dir=tmp_path/'no-ui',
                            session_secret='s'*48, public_origin=ORIGIN, release_sha='catalog-release'), store=store)
    caplog.set_level(logging.INFO, logger='projects_hub.mcp')
    with TestClient(app, base_url=ORIGIN) as client:
        client.headers['x-request-id'] = 'catalog-correlation'
        logging.getLogger('projects_hub.mcp').addHandler(caplog.handler)
        for method, field in [('tools/list', 'tools'), ('events/list', 'events')]:
            response = rpc(client, tokens['access_token'], method)
            assert response.status_code == 200
            catalog = response.json()['result'][field]
            record = next(r for r in reversed(caplog.records) if getattr(r, 'event', None) == 'mcp_catalog')
            output = JsonFormatter().format(record)
            facts = json.loads(output)
            assert facts['rpc_method'] == method and facts['mcp_protocol'] == '2026-07-28'
            assert facts['catalog_names'] == [item['name'] for item in catalog]
            assert facts['catalog_count'] == len(catalog)
            assert facts['catalog_sha256'] == hashlib.sha256(json.dumps(catalog, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            assert facts['backend_release_sha'] == 'catalog-release' and facts['status'] == 200
            assert facts['request_id'] == 'catalog-correlation'
            assert tokens['access_token'] not in output and 'session_secret' not in output
        count = sum(getattr(r, 'event', None) == 'mcp_catalog' for r in caplog.records)
        assert rpc(client, 'private-invalid-token', 'tools/list').status_code == 401
        assert sum(getattr(r, 'event', None) == 'mcp_catalog' for r in caplog.records) == count


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
        assert {t['name'] for t in rpc(client, token, 'tools/list').json()['result']['tools']} == {'get_test_request', 'record_test_response','get_project_request','record_request_result','start_question_session','link_voice_analysis_sources'}
        assert rpc(client, token, 'events/list').json()['result']['events'] == [REQUEST_EVENT_DEFINITION,EVENT_DEFINITION]
        subscribed = rpc(client, token, 'events/subscribe', dict(name=EVENT, arguments={'project_id': grant['project_id']}, delivery=DELIVERY))
        assert subscribed.status_code == 200, subscribed.text
        assert subscribed.json()['result']['id'].startswith('sub_')
        request = app.state.mcp_events.create_request(grant['actor_id'], grant['project_id'], 'sdk-contract-test')
        context = {'request_id': request['request_id'], 'project_id': request['project_id'], 'actor_id': request['actor_id']}
        read = rpc(client, token, 'tools/call', {'name': 'get_test_request', 'arguments': context}).json()['result']
        assert read['structuredContent']['project_id'] == grant['project_id']
        nonce = read['structuredContent']['nonce']
        saved = rpc(client, token, 'tools/call', {'name': 'record_test_response', 'arguments': {**context, 'nonce': nonce, 'response': 'ACK'}}).json()['result']
        assert saved['structuredContent']['state'] == 'completed'
        spoof = rpc(client, token, 'tools/call', {'name': 'record_test_response', 'arguments': {**context, 'nonce': nonce, 'response': 'ACK', 'actor_id': 'other'}})
        assert spoof.json()['result']['isError'] is True
        bad_sub = rpc(client, token, 'events/subscribe', dict(name=EVENT, arguments={'project_id': grant['project_id']}, delivery={**DELIVERY, 'secret': 'bad'}))
        assert bad_sub.json()['error']['code'] == -32602
        client.cookies.set(COOKIE_NAME, issue_session(grant['actor_id'], 's'*48))
        assert client.post('/mcp', json={}, headers=edge_headers()).status_code == 401
        for path in ['/mcp/oauth/consent', '/api/conversations', '/api/mcp/grants/'+grant['id']+'/revoke']:
            assert client.post(path, json={}, headers=edge_headers()).status_code == 403
        assert client.post('/mcp/oauth/register', json={'redirect_uris': ['https://evil.example.com']}, headers=edge_headers()).status_code == 400
        stopped = rpc(client, token, 'events/unsubscribe', dict(name=EVENT, arguments={'project_id': grant['project_id']}, delivery={'url': DELIVERY['url']}))
        assert stopped.status_code == 200 and stopped.json()['result']['resultType'] == 'complete'


@pytest.mark.parametrize('authentication', ['basic', 'post'])
def test_oauth_http_service_pkce_secret_replay_and_revocation(setup, tmp_path, authentication):
    store, _, _, receiver, grant, _ = setup
    app = create_app(Settings(data_dir=store.data_dir, static_dir=tmp_path/'no-ui',
                            session_secret='s'*48, public_origin=ORIGIN), store=store)
    app.state.mcp_events.sender = receiver
    with TestClient(app, base_url=ORIGIN) as client:
        auth = app.state.mcp_auth
        registered = auth.provision_service(grant['actor_id'], 'ph_service_chatgpt', [REDIRECT])
        metadata = client.get('/.well-known/oauth-authorization-server').json()
        assert metadata['code_challenge_methods_supported'] == ['S256']
        assert metadata['token_endpoint_auth_methods_supported'] == ['client_secret_basic', 'client_secret_post']
        assert 'registration_endpoint' not in metadata
        assert metadata['authorization_response_iss_parameter_supported'] is True
        resource = client.get('/.well-known/oauth-protected-resource/mcp').json()['resource']
        assert client.post('/mcp/oauth/register', json={'redirect_uris': [REDIRECT]}, headers=edge_headers()).status_code == 400
        params = dict(client_id=registered['client_id'], redirect_uri=REDIRECT, response_type='code',
                      code_challenge=challenge('v'*43), code_challenge_method='S256', resource=resource, scope=SCOPE, state='opaque-state')
        # No login invite, owner cookie, or per-user consent. Only a provisioned
        # integration can obtain tokens, by proving its configured secret.
        accepted = client.get('/mcp/oauth/authorize', params=params, follow_redirects=False)
        assert accepted.status_code == 303
        callback = parse_qs(urlsplit(accepted.headers['location']).query)
        assert callback['state'] == ['opaque-state'] and callback['iss'] == [ORIGIN]
        exchange = dict(grant_type='authorization_code', client_id=params['client_id'], resource=resource,
                        code=callback['code'][0], redirect_uri=REDIRECT, code_verifier='v'*43)
        assert client.post('/mcp/oauth/token', data=exchange, headers=edge_headers()).status_code == 401
        assert client.post('/mcp/oauth/token', data={**exchange, 'client_secret': 'wrong'}, headers=edge_headers()).status_code == 401
        headers = edge_headers()
        if authentication == 'basic':
            headers['Authorization'] = 'Basic '+base64.b64encode((registered['client_id']+':'+registered['client_secret']).encode()).decode()
        else:
            exchange['client_secret'] = registered['client_secret']
        for altered in [{**exchange, 'code_verifier': 'w'*43}, {**exchange, 'resource': ORIGIN}, {**exchange, 'client_id': 'other'}]:
            assert client.post('/mcp/oauth/token', data=altered, headers=headers).status_code in (400, 401)
        issued = client.post('/mcp/oauth/token', data=exchange, headers=headers).json()
        token = issued['access_token']
        assert client.post('/mcp/oauth/token', data=exchange, headers=headers).status_code == 400
        assert rpc(client, token, 'server/discover').status_code == 200
        granted = auth.authenticate('Bearer '+token)
        assert granted['principal_type'] == 'service' and granted['actor_id'] != grant['actor_id']
        assert store.db.execute('SELECT 1 FROM memberships WHERE actor_id=?', (granted['actor_id'],)).fetchone() is None
        assert store.db.execute('SELECT 1 FROM login_invites WHERE actor_id=?', (granted['actor_id'],)).fetchone() is None
        subscription = rpc(client, token, 'events/subscribe', dict(name=EVENT, arguments={'project_id': grant['project_id']}, delivery=DELIVERY))
        assert subscription.status_code == 200 and 'id' in subscription.json()['result']
        created = app.state.mcp_events.create_request(grant['actor_id'], grant['project_id'], 'http-service-test')
        context = {key: created[key] for key in ('request_id', 'project_id', 'actor_id')}
        read = rpc(client, token, 'tools/call', dict(name='get_test_request', arguments=context)).json()['result']['structuredContent']
        assert read['actor_id'] == grant['actor_id']
        assert rpc(client, token, 'tools/call', dict(name='get_test_request', arguments={**context, 'project_id': 'wrong'})).json()['result']['isError']
        saved = rpc(client, token, 'tools/call', dict(name='record_test_response', arguments={**context, 'nonce': read['nonce'], 'response': 'ACK'})).json()['result']['structuredContent']
        assert saved['response_actor_id'] == granted['actor_id'] and saved['actor_id'] == grant['actor_id']
        refresh = dict(grant_type='refresh_token', refresh_token=issued['refresh_token'], client_id=registered['client_id'], resource=resource)
        if authentication == 'post':
            refresh['client_secret'] = registered['client_secret']
        refreshed = client.post('/mcp/oauth/token', data=refresh, headers=headers).json()
        assert rpc(client, refreshed['access_token'], 'server/discover').status_code == 200
        revoke = dict(token=refreshed['access_token'], client_id=registered['client_id'])
        assert client.post('/mcp/oauth/revoke', data=revoke, headers=edge_headers()).status_code == 401
        if authentication == 'post':
            revoke['client_secret'] = registered['client_secret']
        revoked = client.post('/mcp/oauth/revoke', data=revoke, headers=headers)
        assert revoked.status_code == 200
        assert rpc(client, token, 'server/discover').status_code == 401
        assert rpc(client, refreshed['access_token'], 'server/discover').status_code == 401


@pytest.mark.asyncio
async def test_service_scope_future_projects_response_author_and_disable(setup):
    store, auth, events, receiver, owner_grant, _ = setup
    owner_id = owner_grant['actor_id']
    registered = auth.provision_service(owner_id, 'ph_service_future', [REDIRECT])
    result = auth.authorize_service(dict(client_id=registered['client_id'], redirect_uri=REDIRECT, response_type='code',
        state='service-test', code_challenge=challenge('v'*43), code_challenge_method='S256', resource=auth.resource, scope=SCOPE))
    tokens = auth.exchange(dict(grant_type='authorization_code', client_id=registered['client_id'], client_secret=registered['client_secret'],
        code=result['code'], redirect_uri=REDIRECT, code_verifier='v'*43, resource=auth.resource))
    service = auth.authenticate('Bearer '+tokens['access_token'])
    assert registered['client_secret'] not in str([dict(r) for r in store.db.execute('SELECT * FROM mcp_service_clients')])
    # Another tenant/project is created after linking; no individual user login
    # or grant is needed. Only active projects and the test tools are in scope.
    other = store.ensure_dev_workspace('Other tenant')
    project = other['projects'][0]['id']
    await events.subscribe(service['id'], EVENT, {'project_id': project}, DELIVERY)
    request = events.create_request(other['actor']['id'], project, 'service-future-project')
    read = events.get_request(service['id'], request['request_id'])
    saved = events.record_response(service['id'], request['request_id'], read['nonce'], 'ACK '+read['nonce'])
    assert saved['response_actor_id'] == service['actor_id']
    assert saved['response_actor_id'] not in (owner_id, other['actor']['id'])
    # A separate pending intent is cancelled after global integration revocation.
    pending = events.create_request(other['actor']['id'], project, 'service-before-disable')
    with pytest.raises(StoreError):
        auth.disable_service(other['actor']['id'], registered['client_id'])
    auth.disable_service(owner_id, registered['client_id'])
    with pytest.raises(StoreError):
        auth.authenticate('Bearer '+tokens['access_token'])
    with pytest.raises(StoreError):
        events.get_request(service['id'], pending['request_id'])
    while await events.deliver_one():
        pass
    assert not receiver.events
    assert store.db.execute('SELECT state FROM mcp_outbox WHERE request_id=?', (pending['request_id'],)).fetchone()[0] == 'cancelled'


def test_service_registration_is_admin_only_stable_and_not_browser_identity(setup):
    store, auth, _, _, grant, _ = setup
    other = store.ensure_dev_workspace('Other')
    with pytest.raises(StoreError):
        auth.provision_service(other['actor']['id'], 'ph_service_chatgpt', [REDIRECT])
    registered = auth.provision_service(grant['actor_id'], 'ph_service_chatgpt', [REDIRECT])
    with pytest.raises(StoreError, match='already exists'):
        auth.provision_service(grant['actor_id'], registered['client_id'], [REDIRECT])
    auth.validate_service_secret(registered['client_id'], registered['client_secret'])
    params = dict(client_id=registered['client_id'], redirect_uri=REDIRECT, response_type='code',
        state='state', code_challenge=challenge('v'*43), code_challenge_method='S256', resource=auth.resource, scope=SCOPE)
    for change in [{'client_id': grant['client_id']}, {'redirect_uri': 'https://evil.example.com'}, {'scope': 'all'}, {'resource': ORIGIN}]:
        with pytest.raises(StoreError):
            auth.authorize_service({**params, **change})
    with pytest.raises(StoreError):
        store.require_platform_owner(auth.service_client(registered['client_id'])['actor_id'])


@pytest.mark.asyncio
async def test_saved_user_context_is_parameter_not_login_and_cannot_be_substituted(setup):
    store, auth, events, receiver, owner, _ = setup
    registered = auth.provision_service(owner['actor_id'], 'ph_service_context', [REDIRECT])
    result = auth.authorize_service(dict(client_id=registered['client_id'], redirect_uri=REDIRECT, response_type='code',
        state='service-context', code_challenge=challenge('v'*43), code_challenge_method='S256', resource=auth.resource, scope=SCOPE))
    tokens = auth.exchange(dict(grant_type='authorization_code', client_id=registered['client_id'], client_secret=registered['client_secret'],
        code=result['code'], redirect_uri=REDIRECT, code_verifier='v'*43, resource=auth.resource))
    service = auth.authenticate('Bearer '+tokens['access_token'])
    user = store.ensure_dev_workspace('Context participant')['actor']['id']
    project = owner['project_id']
    workspace = store.db.execute('SELECT workspace_id FROM projects WHERE id=?', (project,)).fetchone()[0]
    store.db.execute('INSERT INTO memberships VALUES(?,?,?)', (user, workspace, 'viewer'))
    store.grant_project_access(actor_id=owner['actor_id'], workspace_id=workspace, project_id=project, target_actor_id=user, role='viewer')
    await events.subscribe(service['id'], EVENT, {'project_id': project}, DELIVERY)
    request = events.create_request(owner['actor_id'], project, 'context-user-test', context_actor_id=user)
    assert await events.deliver_one()
    assert receiver.events[0][0]['data']['actor_id'] == user
    context = {'project_id': project, 'actor_id': user}
    read = events.get_request(service['id'], request['request_id'], **context)
    assert read['actor_id'] == user and read['actor_name'] == 'Context participant'
    for mismatch in [{'actor_id': owner['actor_id']}, {'project_id': 'wrong-project'}]:
        with pytest.raises(StoreError):
            events.get_request(service['id'], request['request_id'], **{**context, **mismatch})
        with pytest.raises(StoreError):
            events.record_response(service['id'], request['request_id'], read['nonce'], 'ACK', **{**context, **mismatch})
    with pytest.raises(StoreError, match='different user context'):
        events.create_request(owner['actor_id'], project, 'context-user-test', context_actor_id=owner['actor_id'])
    saved = events.record_response(service['id'], request['request_id'], read['nonce'], 'ACK', **context)
    assert saved['actor_id'] == user and saved['response_actor_id'] == service['actor_id']
    # Human-context permission loss stops new deliveries even though the service
    # credential remains valid. No user token or OAuth connection was minted.
    pending = events.create_request(owner['actor_id'], project, 'context-user-revoke', context_actor_id=user)
    store.revoke_project_access(actor_id=owner['actor_id'], workspace_id=workspace, project_id=project, target_actor_id=user)
    assert await events.deliver_one() and len(receiver.events) == 1
    assert store.db.execute('SELECT state FROM mcp_outbox WHERE request_id=?', (pending['request_id'],)).fetchone()[0] == 'cancelled'
    with pytest.raises(StoreError):
        events.get_request(service['id'], request['request_id'], **context)
    assert auth.authenticate('Bearer '+tokens['access_token'])['id'] == service['id']
    assert store.db.execute('SELECT COUNT(*) FROM mcp_grants WHERE actor_id=?', (user,)).fetchone()[0] == 0


@pytest.mark.parametrize('operation', ['provision-service', 'provision-tunnel'])
def test_service_provision_cli_private_file_no_stdout_secret_or_implicit_rotation(setup, tmp_path, monkeypatch, capsys, operation):
    import runpy
    import sys
    store, auth, _, _, _, _ = setup
    settings = Settings(data_dir=store.data_dir, static_dir=tmp_path/'no-ui', session_secret='s'*48, public_origin=ORIGIN)
    monkeypatch.setattr(Settings, 'from_env', lambda: settings)
    credential_file = tmp_path/'connection.json'
    script = Path(__file__).resolve().parents[1]/'scripts/mcp_test_request.py'
    main = runpy.run_path(str(script))['main']
    monkeypatch.setattr(sys, 'argv', [str(script), operation, '--client-id', 'ph_service_cli', '--credential-file', str(credential_file)])
    assert main() == 0
    output = capsys.readouterr().out
    credentials = json.loads(credential_file.read_text())
    secret_field = 'client_secret' if operation == 'provision-service' else 'access_token'
    assert credentials[secret_field] not in output and secret_field not in output
    assert credential_file.stat().st_mode & 0o777 == 0o600
    if operation == 'provision-service':
        auth.validate_service_secret(credentials['client_id'], credentials['client_secret'])
    else:
        assert auth.authenticate('Bearer '+credentials['access_token'])['principal_type'] == 'service'
    with pytest.raises(FileExistsError):
        main()
    assert json.loads(credential_file.read_text()) == credentials
    if operation == 'provision-service':
        auth.validate_service_secret(credentials['client_id'], credentials['client_secret'])
    else:
        assert auth.authenticate('Bearer '+credentials['access_token'])['principal_type'] == 'service'


def test_private_tunnel_credential_survives_restart_and_remains_revocable_service_scope(setup, tmp_path):
    store, auth, _, _, owner, _ = setup
    with pytest.raises(StoreError):
        auth.provision_tunnel('not-an-admin', 'ph_service_tunnel')
    issued = auth.provision_tunnel(owner['actor_id'], 'ph_service_tunnel')
    token = issued['access_token']
    principal = auth.authenticate('Bearer '+token)
    assert principal['principal_type'] == 'service' and principal['actor_id'] != owner['actor_id']
    row = store.db.execute('SELECT * FROM mcp_tokens WHERE grant_id=?', (principal['id'],)).fetchone()
    assert row['token_hash'] == digest(token) and token not in json.dumps(dict(row))
    assert row['expires_ms'] == issued['expires_at_ms'] == principal['expires_ms']
    assert store.db.execute('SELECT COUNT(*) FROM memberships WHERE actor_id=?', (principal['actor_id'],)).fetchone()[0] == 0
    reopened = DurableStore(store.data_dir)
    try:
        restored = MCPAuth(reopened, ORIGIN)
        assert restored.authenticate('Bearer '+token)['id'] == principal['id']
        app = create_app(Settings(data_dir=reopened.data_dir, static_dir=tmp_path/'no-ui',
                                session_secret='s'*48, public_origin=ORIGIN), store=reopened)
        with TestClient(app, base_url=ORIGIN) as client:
            assert rpc(client, token, 'server/discover').status_code == 200
            assert len(rpc(client, token, 'tools/list').json()['result']['tools']) == 6
        restored.project_grant(principal['id'], owner['project_id'])
        restored.disable_service(owner['actor_id'], issued['client_id'])
        with pytest.raises(StoreError):
            restored.authenticate('Bearer '+token)
    finally:
        reopened.close()


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


@pytest.mark.asyncio
async def test_stable_contract_preserves_legacy_subscription_and_one_emission(setup):
    store,auth,events,receiver,owner,_=setup
    project,actor=owner['project_id'],owner['actor_id']
    legacy=await events.subscribe(owner['id'],EVENT,{'project_id':project},DELIVERY)
    stable=await events.subscribe(owner['id'],REQUEST_EVENT,{'project_id':project},DELIVERY)
    assert legacy['id']!=stable['id']
    assert (await events.subscribe(owner['id'],REQUEST_EVENT,{'project_id':project},DELIVERY))['id']==stable['id']
    request=events.create_request(actor,project,'stable-test-one',event_name=REQUEST_EVENT)
    assert await events.deliver_one()
    payload=receiver.events[0][0]
    assert payload['name']==REQUEST_EVENT and payload['data']['mode']=='test'
    assert 'nonce' not in payload['data'] and 'actor_id' not in payload['data']
    assert store.db.execute('SELECT COUNT(*) FROM mcp_outbox WHERE request_id=?',(request['request_id'],)).fetchone()[0]==1
    context=events.get_project_request(owner['id'],project,request['request_id'])
    result=dict(kind='test_ack',nonce=context['nonce'],text='ACK '+context['nonce'])
    for bad in ({**result,'command':'execute'}, {**result,'kind':'discussion_reply'}, {**result,'nonce':'wrong'}):
        with pytest.raises(StoreError):
            events.record_request_result(owner['id'],project,request['request_id'],request['eventId'],bad)
    with pytest.raises(StoreError):
        events.record_request_result(owner['id'],project,request['request_id'],'evt-wrong',result)
    saved=events.record_request_result(owner['id'],project,request['request_id'],request['eventId'],result)
    assert saved['response_id'] and saved['backend_state']=='stored'
    assert events.record_request_result(owner['id'],project,request['request_id'],request['eventId'],result)==saved
    with pytest.raises(StoreError):
        events.record_request_result(owner['id'],project,request['request_id'],request['eventId'],{**result,'text':'changed'})
    reopened=DurableStore(store.data_dir)
    try:
        restored=MCPEvents(reopened,MCPAuth(reopened,ORIGIN),'s'*48,receiver)
        assert restored.get_project_request(owner['id'],project,request['request_id'])==saved
        assert (await restored.subscribe(owner['id'],REQUEST_EVENT,{'project_id':project},DELIVERY))['id']==stable['id']
        assert not await restored.deliver_one()
        assert reopened.db.execute('SELECT state FROM mcp_subscriptions WHERE id=?',(legacy['id'],)).fetchone()[0]=='active'
    finally:
        reopened.close()


def live_fixture(setup):
    from projects_hub.collaboration import CollaborationService
    store,auth,events,receiver,owner,_=setup
    CollaborationService(store,None)
    credential=auth.provision_tunnel(owner['actor_id'],'ph_service_live_test')
    service=auth.authenticate('Bearer '+credential['access_token'])
    project=owner['project_id']
    workspace=store.db.execute('SELECT workspace_id FROM projects WHERE id=?',(project,)).fetchone()[0]
    bindings=[]
    for index in (1,2):
        note_id=f'note_discussion_{index}'
        store.db.execute('''INSERT INTO project_notes(id,workspace_id,project_id,author_actor_id,title,body,source_text,
            author_roles_json,audience,status,repository_id,repository_full_name,repository_path,command_id,request_sha256,created_at_ms,updated_at_ms)
            VALUES(?,?,?,?,?,?,?,'[]','project','ready',123,'example/docs',?,?,?,1,1)''',
            (note_id,workspace,project,owner['actor_id'],f'Discussion {index}',f'Context {index}',f'Context {index}',f'docs/{index}.md',f'fixture-note-{index}',f'fixture-{index}'))
        bindings.append(events.bindings.bind(owner['actor_id'],project,note_id,service['client_id'],'T0123456789','C0123456789',f'179190000{index}.000001'))
    return store,auth,events,receiver,owner,service,bindings




@pytest.mark.asyncio
async def test_question_session_owner_opt_in_dot_package_without_consult_or_redirect(setup):
    from projects_hub.collaboration import CollaborationService
    from projects_hub.collaboration_analysis import CollaborationAnalysisService

    store,auth,events,receiver,owner,service,bindings=live_fixture(setup)
    project=owner['project_id']
    binding=bindings[0]
    note_id=binding['note_id']
    note=store.db.execute('SELECT * FROM project_notes WHERE id=?',(note_id,)).fetchone()
    await events.subscribe(service['id'],REQUEST_EVENT,{'project_id':project},DELIVERY)
    with pytest.raises(StoreError) as denied:
        events.start_question_session(owner['actor_id'],project,note_id,'questions.start.first')
    assert denied.value.code=='QUESTION_ROUTE_REQUIRED'
    opted=events.bindings.bind(
        owner['actor_id'],project,note_id,service['client_id'],
        binding['slack_workspace_id'],binding['slack_channel_id'],binding['slack_thread_ts'],
        expected_revision=1,allow_questions=True,
    )
    assert opted['revision']==2 and opted['questions_allowed']==1
    request=events.start_question_session(owner['actor_id'],project,note_id,'questions.start.first')
    assert request['request_id']
    assert events.start_question_session(owner['actor_id'],project,note_id,'questions.start.first')['request_id']==request['request_id']
    assert await events.deliver_one()
    read=events.get_project_request(service['id'],project,request['request_id'])
    assert read['request_kind']=='question_start'
    assert read['source_revision']==note['revision']
    assert read['allowed_result_kinds']==['question_package']
    assert read['slack']=={'workspace_id':opted['slack_workspace_id'],'channel_id':opted['slack_channel_id'],'thread_ts':opted['slack_thread_ts']}
    analysis=CollaborationAnalysisService(store,CollaborationService(store,None))
    events.analysis_service=analysis
    payload=dict(
        kind='question_package',external_analysis_id='analysis-generated-by-dot-01',
        source_kind='project_note',source_id=note_id,source_revision=note['revision'],
        source_repository_sha=None,purpose='requirements',summary='One product decision',
        questions=[dict(external_question_id='question-generated-by-dot-01',
                        prompt='One board or multiple boards?',
                        context='Owner prefers one active project board.',
                        reason='Navigation scope needs clarification.',
                        priority='critical',blocking=True,deadline_ms=None)],
    )
    with pytest.raises(StoreError):
        events.record_request_result(service['id'],project,request['request_id'],request['eventId'],
                                     {**payload,'source_revision':note['revision']+1})
    saved=events.record_request_result(service['id'],project,request['request_id'],request['eventId'],payload)
    assert saved['question_package']['model']=='dot'
    assert saved['question_package']['producer_actor_id']==service['actor_id']
    assert saved['destinations']['slack_state']=='pending'
    assert saved['analysis_id']==saved['question_package']['id']
    assert events.record_request_result(service['id'],project,request['request_id'],request['eventId'],payload)['analysis_id']==saved['analysis_id']
    with pytest.raises(StoreError):
        events.record_request_result(service['id'],project,request['request_id'],request['eventId'],
                                     {**payload,'summary':'Different question package'})
    workspace=store.db.execute('SELECT workspace_id FROM projects WHERE id=?',(project,)).fetchone()[0]
    receipt=analysis.answer_questions(
        actor_id=owner['actor_id'],workspace_id=workspace,
        analysis_id=saved['analysis_id'],command_id='questions.answer.first',
        responses=[{'question_id':saved['question_package']['questions'][0]['id'],
                    'disposition':'answer','body':'One board.'}],
    )
    assert receipt['continuation']=='ready_to_complete'
    analysis.question_events=events
    completed=analysis.control_question_session(
        actor_id=owner['actor_id'],workspace_id=workspace,analysis_id=saved['analysis_id'],
        command_id='questions.complete.first',action='complete',
    )
    assert completed['continuation']=='ready_for_dot'
    assert completed['continuation_request_id']
    assert store.db.execute(
        'SELECT COUNT(*) FROM collaboration_jobs WHERE analysis_id=?',(saved['analysis_id'],)
    ).fetchone()[0]==0
    await analysis.close()

@pytest.mark.asyncio
async def test_two_bound_discussions_same_subscription_independent_destinations_restart(setup):
    store,auth,events,receiver,owner,service,bindings=live_fixture(setup)
    project=owner['project_id']
    sub=await events.subscribe(service['id'],REQUEST_EVENT,{'project_id':project},DELIVERY)
    requests=[events.create_request(owner['actor_id'],project,f'live-bound-{i}',event_name=REQUEST_EVENT,binding_id=b['id']) for i,b in enumerate(bindings)]
    reopened=DurableStore(store.data_dir)
    try:
        restored=MCPEvents(reopened,MCPAuth(reopened,ORIGIN),'s'*48,receiver)
        assert (await restored.subscribe(service['id'],REQUEST_EVENT,{'project_id':project},DELIVERY))['id']==sub['id']
        for i,(request,binding) in enumerate(zip(requests,bindings)):
            assert await restored.deliver_one()
            read=restored.get_project_request(service['id'],project,request['request_id'])
            assert read['mode']=='live' and read['note_id']==binding['note_id'] and read['slack']['thread_ts']==binding['slack_thread_ts']
            with pytest.raises(StoreError):
                restored.get_project_request(owner['id'],project,request['request_id']) # test-only principal is not the bound client
            with pytest.raises(StoreError):
                restored.get_request(service['id'],request['request_id']) # legacy wrapper cannot read live
            result=dict(kind='discussion_reply',text=f'Bound answer {i}')
            saved=restored.record_request_result(service['id'],project,request['request_id'],request['eventId'],result)
            assert saved['destinations']['backend_state']=='stored' and saved['destinations']['mira_state']=='available'
            assert saved['destinations']['slack_state']=='pending'
            assert restored.record_request_result(service['id'],project,request['request_id'],request['eventId'],result)==saved
            assert store.db.execute('SELECT COUNT(*) FROM project_discussion_entries WHERE note_id=?',(binding['note_id'],)).fetchone()[0]==1
            from projects_hub.collaboration import CollaborationService
            collaboration=CollaborationService(reopened,None)
            workspace=reopened.db.execute('SELECT workspace_id FROM projects WHERE id=?',(project,)).fetchone()[0]
            mira_read=collaboration.list_replies(actor_id=owner['actor_id'],workspace_id=workspace,note_id=binding['note_id'])
            assert mira_read[0]['body']==result['text'] and mira_read[0]['author']['id']==service['actor_id']
        assert len(receiver.events)==2 and not await restored.deliver_one()
        assert store.db.execute('SELECT COUNT(*) FROM mcp_test_requests').fetchone()[0]==2 # saving own replies did not create request loops
        assert store.db.execute('SELECT COUNT(*) FROM mcp_binding_history').fetchone()[0]==2
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_slack_claim_unknown_send_reconcile_and_no_confirmed_repeat(setup):
    store,auth,events,receiver,owner,service,bindings=live_fixture(setup)
    project=owner['project_id'];binding=bindings[0]
    await events.subscribe(service['id'],REQUEST_EVENT,{'project_id':project},DELIVERY)
    req=events.create_request(owner['actor_id'],project,'slack-claim-test',event_name=REQUEST_EVENT,binding_id=binding['id'])
    def write(result):
        return events.record_request_result(service['id'],project,req['request_id'],req['eventId'],result)
    saved=write(dict(kind='discussion_reply',text='Answer for Slack'))
    response_id=saved['response_id']
    claim=write(dict(kind='slack_claim',response_id=response_id))
    claim_id=claim['destinations']['claim_id']
    receipt=dict(kind='slack_receipt',response_id=response_id,claim_id=claim_id,status='unconfirmed',message_id='',error='timeout',**claim['slack'])
    uncertain=write(receipt)
    assert uncertain['destinations']['backend_state']=='stored' and uncertain['destinations']['slack_state']=='unconfirmed'
    with pytest.raises(StoreError,match='reconcile'):
        write(dict(kind='slack_claim',response_id=response_id))
    reopened=DurableStore(store.data_dir)
    try:
        restored=MCPEvents(reopened,MCPAuth(reopened,ORIGIN),'s'*48,receiver)
        assert restored.get_project_request(service['id'],project,req['request_id'])==uncertain
        with pytest.raises(StoreError,match='reconcile'):
            restored.record_request_result(service['id'],project,req['request_id'],req['eventId'],dict(kind='slack_claim',response_id=response_id))
    finally:
        reopened.close()
    with pytest.raises(StoreError):
        write({**receipt,'status':'confirmed','message_id':'1791911111.123456','error':'','thread_ts':bindings[1]['slack_thread_ts']})
    confirmed=write({**receipt,'status':'confirmed','message_id':'1791911111.123456','error':''})
    assert confirmed['destinations']['slack_state']=='confirmed'
    assert write(dict(kind='slack_claim',response_id=response_id))==confirmed
    assert write({**receipt,'status':'confirmed','message_id':'1791911111.123456','error':''})==confirmed
    with pytest.raises(StoreError):
        write({**receipt,'status':'confirmed','message_id':'1791911112.123456','error':''})
    assert store.db.execute('SELECT COUNT(*) FROM project_discussion_entries').fetchone()[0]==1


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['binding','context_acl','owner_acl','client','project'])
async def test_live_revocation_stops_tools_and_delivery_without_context_leak(setup,change):
    store,auth,events,receiver,owner,service,bindings=live_fixture(setup)
    project=owner['project_id'];binding=bindings[0]
    await events.subscribe(service['id'],REQUEST_EVENT,{'project_id':project},DELIVERY)
    req=events.create_request(owner['actor_id'],project,'live-revoke-test',event_name=REQUEST_EVENT,binding_id=binding['id'])
    if change=='binding':
        events.bindings.revoke(owner['actor_id'],binding['id'],binding['revision'])
    elif change in ('context_acl','owner_acl'):
        store.db.execute('UPDATE project_grants SET revoked_at_ms=1 WHERE project_id=? AND actor_id=?',(project,owner['actor_id']))
    elif change=='client':
        auth.disable_service(owner['actor_id'],service['client_id'])
    else:
        store.db.execute("UPDATE projects SET status='archived' WHERE id=?",(project,))
    assert await events.deliver_one() and not receiver.events
    with pytest.raises(StoreError):
        events.get_project_request(service['id'],project,req['request_id'])
    with pytest.raises(StoreError):
        events.record_request_result(service['id'],project,req['request_id'],req['eventId'],dict(kind='discussion_reply',text='Unauthorized'))
    assert store.db.execute('SELECT COUNT(*) FROM project_discussion_entries').fetchone()[0]==0


@pytest.mark.asyncio
async def test_binding_version_unique_route_and_atomic_response_failure(setup):
    store,auth,events,receiver,owner,service,bindings=live_fixture(setup)
    project=owner['project_id'];binding=bindings[0]
    with pytest.raises(StoreError,match='already bound'):
        events.bindings.bind(owner['actor_id'],project,bindings[1]['note_id'],service['client_id'],
                            binding['slack_workspace_id'],binding['slack_channel_id'],binding['slack_thread_ts'],expected_revision=1)
    assert store.db.execute('SELECT COUNT(*) FROM mcp_binding_history').fetchone()[0]==2
    await events.subscribe(service['id'],REQUEST_EVENT,{'project_id':project},DELIVERY)
    req=events.create_request(owner['actor_id'],project,'atomic-discussion',event_name=REQUEST_EVENT,binding_id=binding['id'])
    # Fail the second destination write, after the note reply was inserted.
    store.db.execute("CREATE TRIGGER fail_response BEFORE INSERT ON mcp_response_destinations BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    with pytest.raises(Exception,match='fixture failure'):
        events.record_request_result(service['id'],project,req['request_id'],req['eventId'],dict(kind='discussion_reply',text='Atomic reply'))
    assert store.db.execute('SELECT COUNT(*) FROM project_discussion_entries').fetchone()[0]==0
    assert store.db.execute('SELECT state FROM mcp_test_requests WHERE id=?',(req['request_id'],)).fetchone()[0]=='awaiting_response'
    store.db.execute('DROP TRIGGER fail_response')
    with pytest.raises(StoreError,match='revision'):
        events.bindings.bind(owner['actor_id'],project,binding['note_id'],service['client_id'],'T0123456789','C0123456789','1791900010.000001')
    updated=events.bindings.bind(owner['actor_id'],project,binding['note_id'],service['client_id'],'T0123456789','C0123456789','1791900010.000001',expected_revision=1)
    assert updated['revision']==2
    assert await events.deliver_one() and not receiver.events # stale route never follows the new destination
    with pytest.raises(StoreError):
        events.get_project_request(service['id'],project,req['request_id'])


def test_stable_tools_use_sdk_strict_result_schema_and_saved_context(setup,tmp_path):
    store,auth,events,receiver,owner,tokens=setup
    project=owner['project_id']
    asyncio.run(events.subscribe(owner['id'],REQUEST_EVENT,{'project_id':project},DELIVERY))
    req=events.create_request(owner['actor_id'],project,'stable-sdk-test',event_name=REQUEST_EVENT)
    app=create_app(Settings(data_dir=store.data_dir,static_dir=tmp_path/'no-ui',session_secret='s'*48,public_origin=ORIGIN),store=store)
    with TestClient(app,base_url=ORIGIN) as client:
        token=tokens['access_token']
        read=rpc(client,token,'tools/call',{'name':'get_project_request','arguments':{'project_id':project,'request_id':req['request_id']}}).json()['result']['structuredContent']
        assert read['actor_id']==owner['actor_id'] and read['mode']=='test'
        arguments={'project_id':project,'request_id':req['request_id'],'event_id':req['eventId'],
                   'result':{'kind':'test_ack','nonce':read['nonce'],'text':'SDK ACK'}}
        invalid=rpc(client,token,'tools/call',{'name':'record_request_result','arguments':{**arguments,'result':{**arguments['result'],'execute':'code'}}}).json()
        assert 'error' in invalid or invalid['result']['isError']
        saved=rpc(client,token,'tools/call',{'name':'record_request_result','arguments':arguments}).json()['result']['structuredContent']
        assert saved['response_id'] and saved['state']=='completed'


@pytest.mark.asyncio
async def test_explicit_slack_rejection_can_retry_only_unfinished_destination(setup):
    store,auth,events,receiver,owner,service,bindings=live_fixture(setup)
    project=owner['project_id'];binding=bindings[0]
    await events.subscribe(service['id'],REQUEST_EVENT,{'project_id':project},DELIVERY)
    req=events.create_request(owner['actor_id'],project,'slack-error-retry',event_name=REQUEST_EVENT,binding_id=binding['id'])
    def write(result):
        return events.record_request_result(service['id'],project,req['request_id'],req['eventId'],result)
    saved=write(dict(kind='discussion_reply',text='One durable reply'))
    first=write(dict(kind='slack_claim',response_id=saved['response_id']))
    error=dict(kind='slack_receipt',response_id=saved['response_id'],claim_id=first['destinations']['claim_id'],status='error',message_id='',error='not_in_channel',**first['slack'])
    rejected=write(error)
    assert rejected['destinations']['slack_state']=='error' and rejected['destinations']['backend_state']=='stored'
    retry=write(dict(kind='slack_claim',response_id=saved['response_id']))
    assert retry['destinations']['claim_id']!=first['destinations']['claim_id']
    with pytest.raises(StoreError):
        write(error) # delayed receipt cannot finish a newer claim
    unknown=write({**error,'claim_id':retry['destinations']['claim_id'],'error':'internal_error'})
    assert unknown['destinations']['slack_state']=='unconfirmed'
    assert store.db.execute('SELECT COUNT(*) FROM project_discussion_entries').fetchone()[0]==1
