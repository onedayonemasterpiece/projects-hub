from __future__ import annotations

import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from projects_hub.github_app import GitHubAppClient, GitHubAppError


def keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private, public


@pytest.mark.asyncio
async def test_app_jwt_verifies_installation_and_scopes_token_to_numeric_repo():
    private, public = keypair()
    requests: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if request.url.path == "/app/installations/77":
            assert auth.startswith("Bearer ")
            claims = jwt.decode(
                auth.removeprefix("Bearer "),
                public,
                algorithms=["RS256"],
                options={"verify_aud": False},
            )
            assert claims["iss"] == "9001"
            return httpx.Response(
                200,
                json={
                    "id": 77,
                    "app_id": 9001,
                    "account": {"id": 501, "login": "owner", "type": "User"},
                    "html_url": "https://github.com/settings/installations/77",
                    "repository_selection": "selected",
                    "permissions": {"contents": "write"},
                    "suspended_at": None,
                },
            )
        if request.url.path == "/app/installations/77/access_tokens":
            claims = jwt.decode(
                auth.removeprefix("Bearer "),
                public,
                algorithms=["RS256"],
                options={"verify_aud": False},
            )
            assert claims["iss"] == "9001"
            body = json.loads(request.content)
            requests.append(body)
            return httpx.Response(
                201,
                json={
                    "token": "ghs_APPID_JWT_" + ("z" * 60),
                    "expires_at": "2026-09-28T06:00:00Z",
                },
            )
        if request.url.path == "/repositories/123":
            assert auth.startswith("Bearer ghs_APPID_JWT_")
            return httpx.Response(
                200,
                json={
                    "id": 123,
                    "full_name": "onedayonemasterpiece/projects-hub",
                    "default_branch": "main",
                    "private": True,
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = GitHubAppClient(
        app_id=9001,
        slug="projects-hub",
        private_key=private,
        webhook_secret="webhook-secret-for-tests",
        transport=httpx.MockTransport(handler),
        now=time.time,
    )
    installation = await client.get_installation(77)
    assert installation["id"] == 77
    repository = await client.get_repository(
        77,
        123,
        permissions={"contents": "read"},
    )
    assert repository["id"] == 123
    assert requests == [
        {
            "repository_ids": [123],
            "permissions": {"contents": "read"},
        }
    ]


@pytest.mark.asyncio
async def test_installation_must_belong_to_configured_app():
    private, _public = keypair()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": 77,
                "app_id": 9999,
                "account": {"id": 501, "login": "owner", "type": "User"},
                "permissions": {"contents": "read"},
            },
        )

    client = GitHubAppClient(
        app_id=9001,
        slug="projects-hub",
        private_key=private,
        webhook_secret="webhook-secret-for-tests",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GitHubAppError) as error:
        await client.get_installation(77)
    assert error.value.code == "GITHUB_INSTALLATION_MISMATCH"


def test_install_url_carries_opaque_state_and_webhook_hmac_is_exact():
    private, _public = keypair()
    client = GitHubAppClient(
        app_id=9001,
        slug="projects-hub",
        private_key=private,
        webhook_secret="webhook-secret-for-tests",
    )
    url = client.install_url("opaque state")
    assert url == (
        "https://github.com/apps/projects-hub/installations/new?"
        "state=opaque+state"
    )

    import hashlib
    import hmac

    body = b'{"action":"deleted"}'
    signature = "sha256=" + hmac.new(
        b"webhook-secret-for-tests",
        body,
        hashlib.sha256,
    ).hexdigest()
    assert client.verify_webhook(body, signature) is True
    assert client.verify_webhook(body + b"x", signature) is False
    assert client.verify_webhook(body, None) is False
