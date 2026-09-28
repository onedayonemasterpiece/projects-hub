from __future__ import annotations

import json
from typing import Any
import uuid

import httpx


class IdentityError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SupabaseIdentityVerifier:
    def __init__(
        self,
        *,
        base_url: str,
        publishable_key: str,
        provider: str,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.publishable_key = publishable_key
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def verify(self, access_token: str) -> dict[str, str | None]:
        token = str(access_token or "").strip()
        if not token or len(token) > 8192:
            raise IdentityError("UNAUTHENTICATED", "Identity token is invalid")

        headers = {
            "apikey": self.publishable_key,
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = await client.get(self.base_url + "/auth/v1/user", headers=headers)
        except httpx.HTTPError as exc:
            raise IdentityError(
                "IDENTITY_PROVIDER_UNAVAILABLE",
                "Identity provider is unavailable",
            ) from exc

        if response.status_code in {401, 403}:
            raise IdentityError("UNAUTHENTICATED", "Identity token was rejected")
        if response.status_code != 200:
            raise IdentityError(
                "IDENTITY_PROVIDER_UNAVAILABLE",
                "Identity provider verification failed",
            )
        if len(response.content) > 64 * 1024:
            raise IdentityError("IDENTITY_PROVIDER_INVALID", "Identity response is too large")
        try:
            user: Any = json.loads(response.content)
        except (ValueError, UnicodeError) as exc:
            raise IdentityError("IDENTITY_PROVIDER_INVALID", "Identity response is invalid") from exc
        if not isinstance(user, dict):
            raise IdentityError("IDENTITY_PROVIDER_INVALID", "Identity response is invalid")

        subject = str(user.get("id") or "").strip()
        try:
            uuid.UUID(subject)
        except (ValueError, TypeError, AttributeError) as exc:
            raise IdentityError("IDENTITY_PROVIDER_INVALID", "Identity subject is invalid") from exc

        app_metadata = user.get("app_metadata") if isinstance(user.get("app_metadata"), dict) else {}
        providers = app_metadata.get("providers")
        provider_set = (
            {str(item) for item in providers if isinstance(item, str)}
            if isinstance(providers, list)
            else set()
        )
        primary = str(app_metadata.get("provider") or "")
        if self.provider not in provider_set and primary != self.provider:
            raise IdentityError("UNAUTHENTICATED", "Unexpected identity provider")

        user_metadata = user.get("user_metadata") if isinstance(user.get("user_metadata"), dict) else {}
        display_name = ""
        for key in ("full_name", "name", "display_name", "preferred_username", "login"):
            value = user_metadata.get(key)
            if isinstance(value, str) and value.strip():
                display_name = value.strip()
                break
        email = user.get("email")
        if not display_name and isinstance(email, str) and email.strip():
            display_name = email.split("@", 1)[0]
        if not display_name:
            display_name = "Пользователь"

        return {
            "provider": "supabase:" + self.provider,
            "subject": subject,
            "display_name": display_name[:80],
            "email": email.strip()[:320] if isinstance(email, str) and email.strip() else None,
        }
