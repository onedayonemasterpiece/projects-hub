from pathlib import Path

import time

from projects_hub.auth import COOKIE_NAME, SESSION_TTL_SECONDS, issue_session, parse_session

from fastapi.testclient import TestClient

from projects_hub.app import create_app
from projects_hub.settings import Settings
from projects_hub.store import DurableStore, StoreError


PUBLIC_ORIGIN = "https://projects-hub.kenigevents.ru"


def public_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        static_dir=tmp_path / "missing-ui",
        session_secret="s" * 48,
        dev_auth=True,
        cookie_secure=True,
        public_origin=PUBLIC_ORIGIN,
    )


def public_headers(*, origin: bool = True) -> dict[str, str]:
    headers = {
        "host": "projects-hub.kenigevents.ru",
        "x-forwarded-host": "projects-hub.kenigevents.ru",
        "x-forwarded-proto": "https",
        "x-forwarded-port": "443",
    }
    if origin:
        headers["origin"] = PUBLIC_ORIGIN
    return headers


def test_platform_owner_is_explicit_and_stable(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        first = store.ensure_platform_owner("Owner")
        again = store.ensure_platform_owner("Other display name")
        assert first["actor"]["id"] == again["actor"]["id"]
        assert first["workspace"]["id"] == again["workspace"]["id"]
        assert first["role"] == "owner"
        assert again["role"] == "owner"

        row = store.db.execute("SELECT actor_id FROM platform_owner WHERE slot=1").fetchone()
        assert row["actor_id"] == first["actor"]["id"]
    finally:
        store.close()


def test_invite_is_single_use_and_sets_own_secure_session(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    invite = store.issue_platform_owner_invite("Owner", ttl_seconds=600)
    app = create_app(public_settings(tmp_path), store=store)

    with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
        config = client.get("/api/auth/config")
        assert config.status_code == 200
        assert config.json() == {"mode": "first_party_invite"}

        denied = client.post(
            "/api/auth/invite",
            json={"token": invite["token"]},
            headers=public_headers(origin=False),
        )
        assert denied.status_code == 403

        exchanged = client.post(
            "/api/auth/invite",
            json={"token": invite["token"]},
            headers=public_headers(),
        )
        assert exchanged.status_code == 200
        assert exchanged.json()["actor"]["id"] == invite["actor_id"]
        assert exchanged.json()["workspace"]["id"] == invite["workspace_id"]
        cookie = exchanged.headers["set-cookie"]
        assert "projects_hub_session=" in cookie
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=lax" in cookie

        current = client.get("/api/bootstrap")
        assert current.status_code == 200
        assert current.json()["actor"]["id"] == invite["actor_id"]

        repeated = client.post(
            "/api/auth/invite",
            json={"token": invite["token"]},
            headers=public_headers(),
        )
        assert repeated.status_code == 401

        public_dev_login = client.post(
            "/api/dev/login",
            json={},
            headers=public_headers(),
        )
        assert public_dev_login.status_code == 404

    store.close()


def test_expired_invite_is_rejected(tmp_path: Path, monkeypatch):
    store = DurableStore(tmp_path)
    try:
        invite = store.issue_platform_owner_invite("Owner", ttl_seconds=60)
        store.db.execute(
            "UPDATE login_invites SET expires_at_ms=0 WHERE token_sha256=?",
            (__import__("hashlib").sha256(invite["token"].encode("utf-8")).hexdigest(),),
        )
        try:
            store.consume_login_invite(invite["token"])
        except StoreError as exc:
            assert exc.code == "INVITE_INVALID"
        else:
            raise AssertionError("expired invite was accepted")
    finally:
        store.close()


def test_dev_owner_invite_is_loopback_only(tmp_path: Path):
    settings = public_settings(tmp_path)
    store = DurableStore(tmp_path / "operator-data")
    app = create_app(settings, store=store)
    try:
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            issued = client.post(
                "/api/dev/owner-invite",
                json={"display_name": "Owner", "ttl_seconds": 600},
            )
            assert issued.status_code == 200
            assert len(issued.json()["token"]) >= 20

        with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
            blocked = client.post(
                "/api/dev/owner-invite",
                json={"display_name": "Owner"},
                headers=public_headers(),
            )
            assert blocked.status_code == 404
    finally:
        store.close()


def test_active_device_cookie_renews_without_telegram_code(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    owner = store.ensure_platform_owner("Owner")
    actor = owner["actor"]["id"]
    app = create_app(public_settings(tmp_path), store=store)
    now = int(time.time())
    assert SESSION_TTL_SECONDS == 7 * 24 * 60 * 60

    # Existing 24-hour cookies remain compatible and silently migrate on reopen.
    old_cookie = issue_session(actor, "s" * 48, now=now - 15 * 60 * 60, ttl_seconds=24 * 60 * 60)
    with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
        renewed = client.get("/api/bootstrap", headers={"Cookie": f"{COOKIE_NAME}={old_cookie}"})
        assert renewed.status_code == 200
        assert renewed.json()["actor"]["id"] == actor
        assert "set-cookie" in renewed.headers
        new_cookie = renewed.cookies.get(COOKIE_NAME)
        assert new_cookie and new_cookie != old_cookie
        assert parse_session(new_cookie, "s" * 48) == actor
        assert f"Max-Age={SESSION_TTL_SECONDS}" in renewed.headers["set-cookie"]
        assert "Secure" in renewed.headers["set-cookie"]
        assert "HttpOnly" in renewed.headers["set-cookie"]

        # Fresh sessions are not unnecessarily reissued on each request.
        fresh = client.get("/api/bootstrap", headers={"Cookie": f"{COOKIE_NAME}={new_cookie}"})
        assert fresh.status_code == 200
        assert "set-cookie" not in fresh.headers

        # An idle, expired cookie remains unauthorized: do not renew without proof.
        expired = issue_session(actor, "s" * 48, now=now - 9 * 24 * 60 * 60)
        blocked = client.get("/api/bootstrap", headers={"Cookie": f"{COOKIE_NAME}={expired}"})
        assert blocked.status_code == 401
        assert "set-cookie" not in blocked.headers

        # A forged cookie must not become a valid cookie.
        forged = old_cookie[:-1] + ("a" if old_cookie[-1] != "a" else "b")
        denied = client.get("/api/bootstrap", headers={"Cookie": f"{COOKIE_NAME}={forged}"})
        assert denied.status_code == 401
        assert "set-cookie" not in denied.headers
    store.close()
