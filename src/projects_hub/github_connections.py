from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any, Callable

from .github_app import GitHubAppClient, GitHubAppError
from .settings import Settings
from .store import DurableStore, StoreError


class GitHubConnections:
    def __init__(
        self,
        store: DurableStore,
        settings: Settings,
        *,
        client: GitHubAppClient | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.now = now or time.time
        if client is not None:
            self.client = client
        elif settings.github_app_enabled:
            self.client = GitHubAppClient(
                app_id=int(settings.github_app_id or 0),
                slug=settings.github_app_slug,
                private_key=settings.github_app_private_key,
                webhook_secret=settings.github_app_webhook_secret,
            )
        else:
            self.client = None

    @property
    def configured(self) -> bool:
        return self.client is not None

    def _require_client(self) -> GitHubAppClient:
        if self.client is None:
            raise StoreError(
                "GITHUB_APP_NOT_CONFIGURED",
                "GitHub App is not configured",
            )
        return self.client

    @staticmethod
    def _state_hash(state: str) -> str:
        return hashlib.sha256(state.encode("utf-8")).hexdigest()

    def start_install(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        client = self._require_client()
        state = secrets.token_urlsafe(32)
        expires_at_ms = round((self.now() + 10 * 60) * 1000)
        self.store.create_github_install_state(
            actor_id=actor_id,
            workspace_id=workspace_id,
            state_hash=self._state_hash(state),
            expires_at_ms=expires_at_ms,
            conversation_id=conversation_id,
        )
        return {
            "install_url": client.install_url(state),
            "expires_at_ms": expires_at_ms,
        }

    async def complete_install(
        self,
        *,
        actor_id: str,
        state: str,
        installation_id: int,
    ) -> dict[str, Any]:
        client = self._require_client()
        if not state or len(state) > 500 or installation_id <= 0:
            raise StoreError(
                "GITHUB_INSTALL_STATE_INVALID",
                "GitHub installation callback is invalid",
            )
        installation = await client.get_installation(installation_id)
        if installation.get("suspended_at"):
            raise StoreError(
                "GITHUB_INSTALLATION_UNAVAILABLE",
                "GitHub installation is suspended",
            )
        repositories = await client.list_repositories(installation_id)
        completed = self.store.complete_github_installation(
            actor_id=actor_id,
            state_hash=self._state_hash(state),
            installation=installation,
            repositories=repositories,
        )
        pending = completed["pending"]
        workspace_id = str(pending["workspace_id"])
        stored = completed["installation"]
        connections = completed["connections"]
        return {
            "workspace_id": workspace_id,
            "conversation_id": pending.get("conversation_id"),
            "installation": {
                "installation_id": installation_id,
                "account_id": stored["account_id"],
                "account_login": stored["account_login"],
                "account_type": stored["account_type"],
                "state": stored["state"],
            },
            "repository_count": len(
                [item for item in connections if item["installation_id"] == installation_id]
            ),
        }

    def status(self, actor_id: str, workspace_id: str) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "installations": self.store.list_github_installations(
                actor_id,
                workspace_id,
            ),
            "repositories": self.store.list_repository_connections(
                actor_id,
                workspace_id,
            ),
        }

    def bind_repository(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        repository_id: int,
        project_id: str | None,
        role: str,
        access_mode: str,
        allowed_paths: list[str] | None,
    ) -> dict[str, Any]:
        return self.store.bind_repository_connection(
            actor_id=actor_id,
            workspace_id=workspace_id,
            repository_id=repository_id,
            project_id=project_id,
            role=role,
            access_mode=access_mode,
            allowed_paths=allowed_paths,
        )

    async def repository_token(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        repository_id: int,
        write: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        client = self._require_client()
        connection = self.store.get_repository_connection(
            actor_id,
            workspace_id,
            repository_id,
        )
        if connection["state"] != "available" or connection["installation_state"] != "active":
            raise StoreError(
                "GITHUB_REPOSITORY_UNAVAILABLE",
                "GitHub repository is unavailable",
            )
        if connection["role"] == "unassigned":
            raise StoreError(
                "GITHUB_REPOSITORY_UNBOUND",
                "GitHub repository is not bound to a project role",
            )
        if write:
            if (
                connection["access_mode"] != "app_managed_write"
                or connection["role"] == "external_owning_repo"
            ):
                raise StoreError(
                    "GITHUB_WRITE_POLICY_DENIED",
                    "Repository write is not allowed by Projects Hub policy",
                )
            permissions = {"contents": "write"}
        else:
            permissions = {"contents": "read"}
        token = await client.installation_token(
            int(connection["installation_id"]),
            repository_ids=[int(connection["repository_id"])],
            permissions=permissions,
        )
        return token, connection

    async def webhook(
        self,
        *,
        body: bytes,
        signature: str | None,
        delivery_id: str | None,
        event_name: str | None,
    ) -> dict[str, Any]:
        client = self._require_client()
        if len(body) > 1_000_000:
            raise StoreError("INVALID_ARGUMENT", "GitHub webhook payload is too large")
        if not client.verify_webhook(body, signature):
            raise StoreError("GITHUB_WEBHOOK_INVALID", "GitHub webhook signature is invalid")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise StoreError("INVALID_ARGUMENT", "GitHub webhook JSON is invalid") from exc
        if not isinstance(payload, dict):
            raise StoreError("INVALID_ARGUMENT", "GitHub webhook payload is invalid")
        delivery = str(delivery_id or "")
        event = str(event_name or "")[:100]
        action = str(payload.get("action") or "")[:100]
        if not delivery or not event:
            raise StoreError("GITHUB_WEBHOOK_INVALID", "GitHub webhook headers are missing")
        fresh = self.store.record_github_webhook_delivery(
            delivery_id=delivery,
            event_name=event,
            action=action,
        )
        if not fresh:
            return {"ok": True, "duplicate": True}

        installation = payload.get("installation") or {}
        installation_id = int(installation.get("id") or 0)
        if installation_id <= 0:
            return {"ok": True, "ignored": True}

        if event == "installation":
            if action == "deleted":
                self.store.set_github_installation_state(
                    installation_id=installation_id,
                    state="revoked",
                )
            elif action == "suspend":
                self.store.set_github_installation_state(
                    installation_id=installation_id,
                    state="suspended",
                )
            elif action in {"unsuspend", "new_permissions_accepted"}:
                record = self.store.github_installation(installation_id)
                if record:
                    verified = await client.get_installation(installation_id)
                    repositories = await client.list_repositories(installation_id)
                    self.store.set_github_installation_state(
                        installation_id=installation_id,
                        state="active",
                    )
                    self.store.sync_github_repositories(
                        workspace_id=str(record["workspace_id"]),
                        installation_id=installation_id,
                        repositories=repositories,
                    )
            return {"ok": True, "event": event, "action": action}

        if event == "installation_repositories":
            self.store.apply_github_repository_webhook(
                installation_id=installation_id,
                added=[
                    item
                    for item in (payload.get("repositories_added") or [])
                    if isinstance(item, dict)
                ],
                removed=[
                    item
                    for item in (payload.get("repositories_removed") or [])
                    if isinstance(item, dict)
                ],
            )
            return {"ok": True, "event": event, "action": action}

        return {"ok": True, "ignored": True}
