from __future__ import annotations

import base64
import hashlib
import hmac
import time

COOKIE_NAME = "projects_hub_session"
SESSION_TTL_SECONDS = 24 * 60 * 60


def _sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def issue_session(
    actor_id: str,
    secret: str,
    now: int | None = None,
    *,
    ttl_seconds: int = SESSION_TTL_SECONDS,
) -> str:
    ttl_seconds = int(ttl_seconds)
    if not 300 <= ttl_seconds <= 7 * 24 * 60 * 60:
        raise ValueError("session ttl is outside the supported bound")
    now = int(time.time() if now is None else now)
    expires = now + ttl_seconds
    payload = f"{actor_id}|{expires}".encode("utf-8")
    body = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{body}.{_sign(payload, secret)}"


def parse_session(token: str | None, secret: str, now: int | None = None) -> str | None:
    if not token or "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        actor_id, expires_text = raw.decode("utf-8").split("|", 1)
        expires = int(expires_text)
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(signature, _sign(raw, secret)):
        return None
    if expires < int(time.time() if now is None else now):
        return None
    return actor_id
