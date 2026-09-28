import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from projects_hub.app import create_app
from projects_hub.identity import IdentityError, SupabaseIdentityVerifier
from projects_hub.settings import (
    EXPECTED_AUTH_PROVIDER,
    EXPECTED_AUTH_SUPABASE_URL,
    EXPECTED_PUBLIC_ORIGIN,
    Settings,
)
from projects_hub.store import DurableStore


PUBLIC_KEY = "sb_publishable_" + ("x" * 40)
ACCESS_TOKEN = "access-" + ("t" * 80)


class FakeVerifier:
    def __init__(self):
        self.calls = 0

    async def verify(self, token: str):
        self.calls += 1
        assert token == ACCESS_TOKEN
        return {
            "provider": "supabase:custom:yandex",
            "subject": "11111111-1111-4111-8111-111111111111",
            "display_name": "Pilot",
            "email": "pilot@example.test",
        }


def public_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        static_dir=tmp_path / "missing-ui",
        session_secret="s" * 48,
        dev_auth=True,
        cookie_secure=True,
        public_origin=EXPECTED_PUBLIC_ORIGIN,
        auth_supabase_url=EXPECTED_AUTH_SUPABASE_URL,
        auth_supabase_publishable_key=PUBLIC_KEY,
        auth_provider=EXPECTED_AUTH_PROVIDER,
    )


def public_headers(
    *,
    origin: str | None = EXPECTED_PUBLIC_ORIGIN,
    xff: str | None = None,
) -> dict[str, str]:
    headers = {
        "host": "projects-hub.kenigevents.ru",
        "x-forwarded-host": "projects-hub.kenigevents.ru",
        "x-forwarded-proto": "https",
        "x-forwarded-port": "443",
    }
    if origin is not None:
        headers["origin"] = origin
    if xff is not None:
        headers["x-forwarded-for"] = xff
    return headers


def test_external_identity_is_stable_and_not_display_name_bound(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        first = store.ensure_external_workspace(
            provider="supabase:custom:yandex",
            subject="sub-one",
            display_name="Same name",
            email="one@example.test",
        )
        again = store.ensure_external_workspace(
            provider="supabase:custom:yandex",
            subject="sub-one",
            display_name="Renamed",
            email="new@example.test",
        )
        second = store.ensure_external_workspace(
            provider="supabase:custom:yandex",
            subject="sub-two",
            display_name="Renamed",
            email="two@example.test",
        )
        assert again["actor"]["id"] == first["actor"]["id"]
        assert again["workspace"]["id"] == first["workspace"]["id"]
        assert again["actor"]["display_name"] == "Renamed"
        assert second["actor"]["id"] != first["actor"]["id"]
        assert second["workspace"]["id"] != first["workspace"]["id"]
        row = store.db.execute(
            """SELECT email FROM external_identities
               WHERE provider=? AND subject=?""",
            ("supabase:custom:yandex", "sub-one"),
        ).fetchone()
        assert row["email"] == "new@example.test"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_supabase_verifier_accepts_only_configured_provider():
    subject = str(uuid.uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(
            EXPECTED_AUTH_SUPABASE_URL + "/auth/v1/user"
        )
        assert request.headers["authorization"] == "Bearer good-token"
        assert request.headers["apikey"] == PUBLIC_KEY
        return httpx.Response(
            200,
            json={
                "id": subject,
                "email": "pilot@example.test",
                "app_metadata": {
                    "provider": "custom:yandex",
                    "providers": ["custom:yandex"],
                },
                "user_metadata": {"name": "Yandex Pilot"},
            },
        )

    verifier = SupabaseIdentityVerifier(
        base_url=EXPECTED_AUTH_SUPABASE_URL,
        publishable_key=PUBLIC_KEY,
        provider=EXPECTED_AUTH_PROVIDER,
        transport=httpx.MockTransport(handler),
    )
    identity = await verifier.verify("good-token")
    assert identity == {
        "provider": "supabase:custom:yandex",
        "subject": subject,
        "display_name": "Yandex Pilot",
        "email": "pilot@example.test",
    }

    wrong = SupabaseIdentityVerifier(
        base_url=EXPECTED_AUTH_SUPABASE_URL,
        publishable_key=PUBLIC_KEY,
        provider=EXPECTED_AUTH_PROVIDER,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "id": subject,
                    "app_metadata": {
                        "provider": "email",
                        "providers": ["email"],
                    },
                    "user_metadata": {},
                },
            )
        ),
    )
    with pytest.raises(IdentityError) as error:
        await wrong.verify("wrong-provider-token")
    assert error.value.code == "UNAUTHENTICATED"


def test_public_config_and_exchange_require_exact_trusted_edge(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    verifier = FakeVerifier()
    app = create_app(
        public_settings(tmp_path),
        store=store,
        identity_verifier=verifier,
    )
    with TestClient(app, base_url=EXPECTED_PUBLIC_ORIGIN) as client:
        forged = client.get("/api/auth/config")
        assert forged.status_code == 403

        forwarded_user = client.get(
            "/api/auth/config",
            headers=public_headers(origin=None, xff="203.0.113.4"),
        )
        assert forwarded_user.status_code == 403

        config = client.get(
            "/api/auth/config",
            headers=public_headers(origin=None),
        )
        assert config.status_code == 200
        body = config.json()
        assert body == {
            "mode": "yandex_pkce",
            "supabase_url": EXPECTED_AUTH_SUPABASE_URL,
            "publishable_key": PUBLIC_KEY,
            "provider": EXPECTED_AUTH_PROVIDER,
            "redirect_url": EXPECTED_PUBLIC_ORIGIN + "/",
        }

        wrong_origin = client.post(
            "/api/auth/exchange",
            json={"access_token": ACCESS_TOKEN},
            headers=public_headers(origin="https://evil.example"),
        )
        assert wrong_origin.status_code == 403
        assert verifier.calls == 0

        exchanged = client.post(
            "/api/auth/exchange",
            json={"access_token": ACCESS_TOKEN},
            headers=public_headers(),
        )
        assert exchanged.status_code == 200
        assert verifier.calls == 1
        actor_id = exchanged.json()["actor"]["id"]
        workspace_id = exchanged.json()["workspace"]["id"]
        cookie = exchanged.headers["set-cookie"]
        assert "projects_hub_session=" in cookie
        assert ACCESS_TOKEN not in cookie
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=lax" in cookie
        assert "Max-Age=86400" in cookie

        current = client.get(
            "/api/bootstrap",
            headers=public_headers(origin=None),
        )
        assert current.status_code == 200
        assert current.json()["actor"]["id"] == actor_id
        assert current.json()["workspace"]["id"] == workspace_id

        repeated = client.post(
            "/api/auth/exchange",
            json={"access_token": ACCESS_TOKEN},
            headers=public_headers(),
        )
        assert repeated.status_code == 200
        assert repeated.json()["actor"]["id"] == actor_id
        assert repeated.json()["workspace"]["id"] == workspace_id

    store.close()


def test_loopback_dev_login_remains_separate_from_public_cookie(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    app = create_app(
        public_settings(tmp_path),
        store=store,
        identity_verifier=FakeVerifier(),
    )
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            "/api/dev/login",
            json={"display_name": "Local acceptance"},
        )
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "Secure" not in cookie
        health = client.get("/healthz")
        assert health.json()["auth_mode"] == "public_yandex+loopback_dev"
    store.close()
