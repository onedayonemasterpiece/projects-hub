from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import time
from typing import Any, Awaitable, Callable

import httpx

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
        manifest_exchange: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.now = now or time.time
        self._manifest_exchange = manifest_exchange
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
            self.client = self._load_persisted_client()

    @property
    def configured(self) -> bool:
        return self.client is not None

    @property
    def bootstrap_available(self) -> bool:
        return bool(self.settings.public_origin)

    @property
    def _persisted_config_path(self) -> Path:
        return self.settings.data_dir / ".github-app.json"

    @staticmethod
    def _validate_persisted_config(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise RuntimeError("Persisted GitHub App configuration is invalid")
        app_id = int(payload.get("app_id") or 0)
        slug = str(payload.get("slug") or "").strip()
        private_key = str(payload.get("private_key") or "").strip()
        webhook_secret = str(payload.get("webhook_secret") or "").strip()
        if (
            app_id <= 0
            or not slug
            or len(slug) > 100
            or not all(ch.isalnum() or ch == "-" for ch in slug)
            or "BEGIN" not in private_key
            or "PRIVATE KEY" not in private_key
            or len(webhook_secret) < 20
        ):
            raise RuntimeError("Persisted GitHub App configuration is incomplete")
        return {
            "app_id": app_id,
            "slug": slug,
            "private_key": private_key,
            "webhook_secret": webhook_secret,
        }

    def _load_persisted_client(self) -> GitHubAppClient | None:
        path = self._persisted_config_path
        if not path.exists():
            return None
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError("Persisted GitHub App configuration must be a private regular file")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("Persisted GitHub App configuration is unreadable") from exc
        config = self._validate_persisted_config(payload)
        return GitHubAppClient(
            app_id=config["app_id"],
            slug=config["slug"],
            private_key=config["private_key"],
            webhook_secret=config["webhook_secret"],
        )

    def _persist_manifest_config(self, payload: dict[str, Any]) -> None:
        config = self._validate_persisted_config(
            {
                "app_id": payload.get("id"),
                "slug": payload.get("slug"),
                "private_key": payload.get("pem"),
                "webhook_secret": payload.get("webhook_secret"),
            }
        )
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        path = self._persisted_config_path
        temp = path.with_name(path.name + ".tmp")
        body = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, body.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, path)
        os.chmod(path, 0o600)
        self.client = GitHubAppClient(
            app_id=config["app_id"],
            slug=config["slug"],
            private_key=config["private_key"],
            webhook_secret=config["webhook_secret"],
        )

    async def _exchange_manifest_code(self, code: str) -> dict[str, Any]:
        if self._manifest_exchange is not None:
            return await self._manifest_exchange(code)
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            try:
                response = await client.post(
                    f"https://api.github.com/app-manifests/{code}/conversions",
                    headers={
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2026-03-10",
                        "User-Agent": "projects-hub",
                    },
                )
            except httpx.HTTPError as exc:
                raise StoreError("GITHUB_UNAVAILABLE", "GitHub App registration is unavailable") from exc
        if response.status_code not in {200, 201}:
            raise StoreError("GITHUB_ERROR", "GitHub App registration failed")
        try:
            payload = response.json()
        except ValueError as exc:
            raise StoreError("GITHUB_INVALID_RESPONSE", "GitHub App registration response is invalid") from exc
        if not isinstance(payload, dict):
            raise StoreError("GITHUB_INVALID_RESPONSE", "GitHub App registration response is invalid")
        return payload

    def _manifest_payload(self, *, suffix: str) -> dict[str, Any]:
        return {
            "name": f"projects-hub-{suffix}",
            "url": self.settings.public_origin,
            "hook_attributes": {
                "url": f"{self.settings.public_origin}/api/github/webhook",
                "active": True,
            },
            "redirect_url": f"{self.settings.public_origin}/api/github/app-manifest/callback",
            "setup_url": f"{self.settings.public_origin}/api/github/install/callback",
            "setup_on_update": True,
            "public": False,
            "default_permissions": {
                "metadata": "read",
                "contents": "write",
            },
        }

    def start_manifest_registration(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        if self.configured:
            return self.start_install(
                actor_id=actor_id,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
            )
        if not self.settings.public_origin:
            raise StoreError(
                "GITHUB_APP_NOT_CONFIGURED",
                "Public Projects Hub origin is required for GitHub setup",
            )
        state = secrets.token_urlsafe(32)
        expires_at_ms = round((self.now() + 20 * 60) * 1000)
        self.store.create_github_app_manifest_state(
            actor_id=actor_id,
            workspace_id=workspace_id,
            state_hash=self._state_hash(state),
            expires_at_ms=expires_at_ms,
            conversation_id=conversation_id,
        )
        manifest = self._manifest_payload(suffix=secrets.token_hex(4))
        return {
            "launch_url": f"{self.settings.public_origin}/api/github/app-manifest/launch?state={state}",
            "action_url": f"{self.settings.public_origin}/api/github/app-manifest/legacy-launch?state={state}",
            "manifest": json.dumps(manifest, separators=(",", ":"), ensure_ascii=False),
            "state": state,
            "expires_at_ms": expires_at_ms,
        }

    def manifest_launch(self, state: str) -> dict[str, Any]:
        if not state or len(state) > 500:
            raise StoreError(
                "GITHUB_APP_MANIFEST_STATE_INVALID",
                "GitHub App manifest state is invalid",
            )
        pending = self.store.github_app_manifest_state(self._state_hash(state))
        return {
            "action_url": "https://github.com/settings/apps/new",
            "manifest": json.dumps(
                self._manifest_payload(suffix=self._state_hash(state)[:8]),
                separators=(",", ":"),
                ensure_ascii=False,
            ),
            "state": state,
            "workspace_id": str(pending["workspace_id"]),
        }

    async def complete_manifest_registration(
        self,
        *,
        state: str,
        code: str,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        if (
            not state
            or len(state) > 500
            or not code
            or len(code) > 500
            or not all(ch.isalnum() or ch in {"_", "-"} for ch in code)
        ):
            raise StoreError(
                "GITHUB_APP_MANIFEST_STATE_INVALID",
                "GitHub App registration callback is invalid",
            )
        raw_hash = self._state_hash(state)
        if actor_id is None:
            actor_id = str(self.store.github_app_manifest_state(raw_hash)["actor_id"])
        pending = self.store.consume_github_app_manifest_state(
            actor_id=actor_id,
            state_hash=raw_hash,
        )
        payload = await self._exchange_manifest_code(code)
        try:
            self._persist_manifest_config(payload)
        except (OSError, RuntimeError, ValueError) as exc:
            raise StoreError(
                "GITHUB_APP_PERSIST_FAILED",
                "GitHub App credentials could not be stored securely",
            ) from exc
        install = self.start_install(
            actor_id=actor_id,
            workspace_id=str(pending["workspace_id"]),
            conversation_id=pending.get("conversation_id"),
        )
        return {
            "workspace_id": str(pending["workspace_id"]),
            "install_url": install["install_url"],
            "expires_at_ms": install["expires_at_ms"],
        }

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
        state: str,
        installation_id: int,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        client = self._require_client()
        if not state or len(state) > 500 or installation_id <= 0:
            raise StoreError(
                "GITHUB_INSTALL_STATE_INVALID",
                "GitHub installation callback is invalid",
            )
        if actor_id is None:
            actor_id = str(self.store.github_install_state(self._state_hash(state))["actor_id"])
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

    @staticmethod
    def _project_match_key(value: str) -> str:
        return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())

    def _auto_bind_exact_matches(self, actor_id: str, workspace_id: str) -> list[int]:
        if self.store.workspace_role(actor_id, workspace_id) != "owner":
            return []
        projects = [
            item
            for item in self.store.list_projects(actor_id, workspace_id)
            if item.get("status") == "active"
        ]
        projects_by_key: dict[str, list[str]] = {}
        for project in projects:
            key = self._project_match_key(str(project.get("name") or ""))
            if key:
                projects_by_key.setdefault(key, []).append(str(project["id"]))

        bound: list[int] = []
        for item in self.store.list_repository_connections(actor_id, workspace_id):
            if (
                item.get("state") != "available"
                or item.get("installation_state") != "active"
                or item.get("role") != "unassigned"
            ):
                continue
            repo_name = str(item.get("full_name") or "").rsplit("/", 1)[-1]
            matches = projects_by_key.get(self._project_match_key(repo_name), [])
            if len(matches) != 1:
                continue
            permissions = item.get("permissions") if isinstance(item.get("permissions"), dict) else {}
            access_mode = (
                "app_managed_write"
                if permissions.get("contents") == "write"
                else "read_only"
            )
            self.store.bind_repository_connection(
                actor_id=actor_id,
                workspace_id=workspace_id,
                repository_id=int(item["repository_id"]),
                project_id=matches[0],
                role="project_docs",
                access_mode=access_mode,
                allowed_paths=[],
            )
            bound.append(int(item["repository_id"]))
        return bound

    def status(self, actor_id: str, workspace_id: str) -> dict[str, Any]:
        auto_bound = self._auto_bind_exact_matches(actor_id, workspace_id)
        return {
            "configured": self.configured,
            "bootstrap_available": self.bootstrap_available,
            "auto_bound_repository_ids": auto_bound,
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
