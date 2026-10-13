"""Standard Webhooks delivery with DNS-pinned HTTPS and no proxy/redirects."""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
from urllib.parse import urlsplit

from standardwebhooks.webhooks import Webhook


class CallbackError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def validate_secret(value: str) -> None:
    try:
        if not isinstance(value, str) or not value.startswith('whsec_'):
            raise ValueError()
        key = base64.b64decode(value[6:], validate=True)
        if not 24 <= len(key) <= 64:
            raise ValueError()
    except (ValueError, TypeError):
        raise CallbackError('invalid_secret') from None


def callback_parts(url: str):
    try:
        parts = urlsplit(url)
        if (parts.scheme != 'https' or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.fragment or parts.port not in (None, 443)
                or len(url) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in url)):
            raise ValueError()
        return parts
    except ValueError:
        raise CallbackError('invalid_url') from None


def public_addresses(host: str) -> list[str]:
    try:
        addresses = list(dict.fromkeys(r[4][0] for r in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
        if not addresses:
            raise CallbackError('invalid_url')
        for address in addresses:
            ip = ipaddress.ip_address(address)
            # Reject mapped/transition IPv6 forms as well as local and mixed DNS.
            if (not ip.is_global or ip.is_multicast or getattr(ip, 'ipv4_mapped', None)
                    or getattr(ip, 'sixtofour', None) or getattr(ip, 'teredo', None)):
                raise CallbackError('invalid_url')
        return addresses
    except (OSError, ValueError):
        raise CallbackError('connection_refused') from None


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        # Resolve exactly once, validate the whole answer, and connect to the IP.
        # The original host remains the SNI and certificate verification name.
        address = public_addresses(self.host)[0]
        raw = socket.create_connection((address, self.port), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def signed_headers(subscription_id: str, message_id: str, body: bytes, secret: str,
                   old_secret: str | None = None, signed_at: datetime | None = None) -> dict:
    signed_at = signed_at or datetime.now(timezone.utc)
    headers = {'Content-Type': 'application/json', 'webhook-id': message_id,
               'webhook-timestamp': str(int(signed_at.timestamp())),
               'X-MCP-Subscription-Id': subscription_id}
    headers['webhook-signature'] = Webhook(secret).sign(message_id, signed_at, body.decode('utf-8'))
    if old_secret:
        headers['webhook-signature'] += ' ' + Webhook(old_secret).sign(message_id, signed_at, body.decode('utf-8'))
    return headers


def _post(url: str, body: bytes, headers: dict) -> tuple[int, bytes]:
    parts = callback_parts(url)
    connection = PinnedHTTPSConnection(parts.hostname, timeout=10, context=ssl.create_default_context())
    try:
        path = parts.path or '/'
        if parts.query:
            path += '?' + parts.query
        connection.request('POST', path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read(65537)
        if len(data) > 65536:
            raise CallbackError('challenge_failed')
        return response.status, data
    except (TimeoutError, socket.timeout):
        raise CallbackError('timeout') from None
    except ssl.SSLError:
        raise CallbackError('tls_error') from None
    except (OSError, http.client.HTTPException):
        raise CallbackError('connection_refused') from None
    finally:
        connection.close()


class WebhookSender:
    async def send(self, url: str, subscription_id: str, message_id: str, payload: dict,
                   secret: str, old_secret: str | None = None) -> tuple[int, bytes]:
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        if len(body) > 262144:
            raise CallbackError('payload_too_large')
        headers = signed_headers(subscription_id, message_id, body, secret, old_secret)
        try:
            return await asyncio.wait_for(asyncio.to_thread(_post, url, body, headers), timeout=12)
        except TimeoutError:
            raise CallbackError('timeout') from None

    async def verify(self, url: str, subscription_id: str, secret: str) -> None:
        challenge = secrets.token_urlsafe(32)
        status, body = await self.send(url, subscription_id, 'msg_verification_' + secrets.token_hex(16),
                                       {'type': 'verification', 'challenge': challenge}, secret)
        try:
            returned = json.loads(body).get('challenge')
            if not 200 <= status < 300 or not isinstance(returned, str) or not secrets.compare_digest(challenge, returned):
                raise ValueError()
        except (ValueError, AttributeError):
            raise CallbackError('challenge_failed') from None
