from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlencode

import httpx
import jwt


API_BASE = "https://api.github.com"
API_VERSION = "2026-03-10"


class GitHubAppError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class GitHubAppClient:
    def __init__(
        self,
        *,
        app_id: int,
        slug: str,
        private_key: str,
        webhook_secret: str,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.app_id = int(app_id)
        self.slug = slug
        self.private_key = private_key
        self.webhook_secret = webhook_secret.encode("utf-8")
        self.transport = transport
        self.now = now or time.time

    def install_url(self, state: str) -> str:
        return (
            f"https://github.com/apps/{quote(self.slug, safe='-')}/installations/new?"
            + urlencode({"state": state})
        )

    def verify_webhook(self, body: bytes, signature: str | None) -> bool:
        if not signature or not signature.startswith("sha256="):
            return False
        supplied = signature.removeprefix("sha256=").strip().lower()
        if len(supplied) != 64:
            return False
        expected = hmac.new(self.webhook_secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, supplied)

    def _app_jwt(self) -> str:
        now = int(self.now())
        return jwt.encode(
            {
                "iat": now - 60,
                "exp": now + 8 * 60,
                "iss": str(self.app_id),
            },
            self.private_key,
            algorithm="RS256",
        )

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "projects-hub",
        }

    async def _json_request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        async with httpx.AsyncClient(
            base_url=API_BASE,
            transport=self.transport,
            timeout=15.0,
            follow_redirects=False,
        ) as client:
            try:
                response = await client.request(
                    method,
                    path,
                    headers=self._headers(token),
                    json=body,
                )
            except httpx.HTTPError as exc:
                raise GitHubAppError(
                    "GITHUB_UNAVAILABLE",
                    "GitHub request failed",
                ) from exc
        if response.status_code in {401, 403}:
            raise GitHubAppError(
                "GITHUB_FORBIDDEN",
                "GitHub App authentication or permission was rejected",
                status=response.status_code,
            )
        if response.status_code == 404:
            raise GitHubAppError(
                "GITHUB_NOT_FOUND",
                "GitHub installation or repository is unavailable",
                status=404,
            )
        if response.status_code >= 400:
            raise GitHubAppError(
                "GITHUB_ERROR",
                f"GitHub request failed with HTTP {response.status_code}",
                status=response.status_code,
            )
        if response.status_code == 204:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubAppError(
                "GITHUB_INVALID_RESPONSE",
                "GitHub returned invalid JSON",
                status=response.status_code,
            ) from exc

    async def get_installation(self, installation_id: int) -> dict[str, Any]:
        payload = await self._json_request(
            "GET",
            f"/app/installations/{int(installation_id)}",
            token=self._app_jwt(),
        )
        if not isinstance(payload, dict) or int(payload.get("id") or 0) != int(installation_id):
            raise GitHubAppError(
                "GITHUB_INVALID_RESPONSE",
                "GitHub installation response is invalid",
            )
        if int(payload.get("app_id") or 0) != self.app_id:
            raise GitHubAppError(
                "GITHUB_INSTALLATION_MISMATCH",
                "Installation belongs to another GitHub App",
            )
        return payload

    async def installation_token(
        self,
        installation_id: int,
        *,
        repository_ids: list[int] | None = None,
        permissions: Mapping[str, str] | None = None,
    ) -> str:
        body: dict[str, Any] = {}
        if repository_ids is not None:
            ids = [int(item) for item in repository_ids]
            if not ids or len(ids) > 500 or any(item <= 0 for item in ids):
                raise GitHubAppError(
                    "INVALID_ARGUMENT",
                    "repository_ids are invalid",
                )
            body["repository_ids"] = ids
        if permissions is not None:
            allowed = {"read", "write"}
            clean = {
                str(key): str(value)
                for key, value in permissions.items()
                if str(value) in allowed
            }
            if clean != dict(permissions):
                raise GitHubAppError(
                    "INVALID_ARGUMENT",
                    "GitHub permission narrowing is invalid",
                )
            body["permissions"] = clean

        payload = await self._json_request(
            "POST",
            f"/app/installations/{int(installation_id)}/access_tokens",
            token=self._app_jwt(),
            body=body,
        )
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or len(token) < 20:
            raise GitHubAppError(
                "GITHUB_INVALID_RESPONSE",
                "GitHub installation token is absent",
            )
        return token

    async def list_repositories(self, installation_id: int) -> list[dict[str, Any]]:
        token = await self.installation_token(
            installation_id,
            permissions={"contents": "read"},
        )
        items: list[dict[str, Any]] = []
        page = 1
        while page <= 20:
            payload = await self._json_request(
                "GET",
                f"/installation/repositories?per_page=100&page={page}",
                token=token,
            )
            rows = payload.get("repositories") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise GitHubAppError(
                    "GITHUB_INVALID_RESPONSE",
                    "GitHub repository catalogue is invalid",
                )
            for row in rows:
                if isinstance(row, dict):
                    items.append(row)
            if len(rows) < 100:
                break
            page += 1
        return items

    async def get_repository(
        self,
        installation_id: int,
        repository_id: int,
        *,
        permissions: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        token = await self.installation_token(
            installation_id,
            repository_ids=[repository_id],
            permissions=permissions or {"contents": "read"},
        )
        payload = await self._json_request(
            "GET",
            f"/repositories/{int(repository_id)}",
            token=token,
        )
        if not isinstance(payload, dict) or int(payload.get("id") or 0) != int(repository_id):
            raise GitHubAppError(
                "GITHUB_INVALID_RESPONSE",
                "GitHub repository response is invalid",
            )
        return payload
