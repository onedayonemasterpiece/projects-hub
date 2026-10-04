from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from projects_hub.github_connections import GitHubConnections
from projects_hub.settings import Settings
from projects_hub.store import DurableStore, StoreError


class FakeGitHubClient:
    def __init__(self) -> None:
        self.token_calls: list[dict] = []
        self.repositories = [
            {
                "id": 101,
                "full_name": "onedayonemasterpiece/projects-hub",
                "default_branch": "main",
                "private": True,
            },
            {
                "id": 202,
                "full_name": "onedayonemasterpiece/wonderful-lections",
                "default_branch": "main",
                "private": True,
            },
        ]

    def install_url(self, state: str) -> str:
        return f"https://github.com/apps/projects-hub/installations/new?state={state}"

    async def get_installation(self, installation_id: int):
        if installation_id != 77:
            raise RuntimeError("unexpected installation")
        return {
            "id": 77,
            "app_id": 9001,
            "account": {"id": 501, "login": "onedayonemasterpiece", "type": "User"},
            "html_url": "https://github.com/settings/installations/77",
            "repository_selection": "selected",
            "permissions": {"metadata": "read", "contents": "write"},
            "suspended_at": None,
        }

    async def list_repositories(self, installation_id: int):
        assert installation_id == 77
        return list(self.repositories)

    async def repository_contents(self, *, token, full_name, path="", ref=""):
        assert token.startswith("ghs_APPID_JWT_")
        assert full_name in {
            "onedayonemasterpiece/projects-hub",
            "onedayonemasterpiece/wonderful-lections",
        }
        assert ref == "main"
        if not path:
            return {
                "kind": "directory",
                "path": "",
                "entries": [
                    {"name": "README.md", "path": "README.md", "type": "file", "size": 42}
                ],
                "truncated": False,
            }
        assert path == "README.md"
        return {
            "kind": "file",
            "path": "README.md",
            "size": 42,
            "sha": "abc123",
            "text": "# Projects Hub\n",
        }

    async def installation_token(
        self,
        installation_id: int,
        *,
        repository_ids=None,
        permissions=None,
    ):
        self.token_calls.append(
            {
                "installation_id": installation_id,
                "repository_ids": repository_ids,
                "permissions": permissions,
            }
        )
        return "ghs_APPID_JWT_" + ("x" * 40)

    def verify_webhook(self, body: bytes, signature: str | None) -> bool:
        expected = "sha256=" + hmac.new(b"webhook-test-secret", body, hashlib.sha256).hexdigest()
        return bool(signature and hmac.compare_digest(signature, expected))


def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        static_dir=tmp_path / "dist",
        session_secret="s" * 48,
        dev_auth=True,
        cookie_secure=False,
        github_app_id=9001,
        github_app_slug="projects-hub",
        github_app_private_key="test-only",
        github_app_webhook_secret="webhook-test-secret",
    )


def setup(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    boot = store.ensure_dev_workspace("Owner")
    client = FakeGitHubClient()
    service = GitHubConnections(store, settings(tmp_path), client=client, now=time.time)
    return store, boot, client, service


@pytest.mark.asyncio
async def test_install_state_is_owner_bound_single_use_and_exact_matches_auto_bind(tmp_path: Path):
    store, boot, _client, service = setup(tmp_path)
    try:
        owner = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        started = service.start_install(actor_id=owner, workspace_id=workspace)
        parsed = urlparse(started["install_url"])
        state = parse_qs(parsed.query)["state"][0]
        assert len(state) >= 32
        assert state not in {
            row["state_hash"]
            for row in store.db.execute("SELECT state_hash FROM github_install_states")
        }

        result = await service.complete_install(
            state=state,
            installation_id=77,
        )
        assert result["repository_count"] == 2
        status = service.status(owner, workspace)
        assert len(status["installations"]) == 1
        assert {item["repository_id"] for item in status["repositories"]} == {101, 202}
        assert {item["role"] for item in status["repositories"]} == {"project_docs"}
        assert {item["access_mode"] for item in status["repositories"]} == {"app_managed_write"}
        assert set(status["auto_bound_repository_ids"]) == {101, 202}

        root = await service.read_repository_path(
            actor_id=owner,
            workspace_id=workspace,
            repository_id=101,
            path="",
        )
        assert root["kind"] == "directory"
        assert root["entries"][0]["path"] == "README.md"

        readme = await service.read_repository_path(
            actor_id=owner,
            workspace_id=workspace,
            repository_id=101,
            path="README.md",
        )
        assert readme["kind"] == "file"
        assert readme["text"] == "# Projects Hub\n"

        with pytest.raises(StoreError) as replay:
            await service.complete_install(
                state=state,
                installation_id=77,
            )
        assert replay.value.code == "GITHUB_INSTALL_STATE_INVALID"
    finally:
        store.close()


def test_only_workspace_owner_can_start_or_bind_installation(tmp_path: Path):
    store, boot, _client, service = setup(tmp_path)
    try:
        owner = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        member = "usr_member"
        now = 1
        store.db.execute(
            "INSERT INTO actors(id,display_name,created_at_ms) VALUES(?,?,?)",
            (member, "Member", now),
        )
        store.db.execute(
            "INSERT INTO memberships(actor_id,workspace_id,role) VALUES(?,?,?)",
            (member, workspace, "member"),
        )
        with pytest.raises(StoreError) as error:
            service.start_install(actor_id=member, workspace_id=workspace)
        assert error.value.code == "FORBIDDEN"

        # Owner creates verified metadata directly for this policy-focused unit case.
        installation = {
            "id": 77,
            "account": {"id": 501, "login": "owner", "type": "User"},
            "html_url": "https://github.com/settings/installations/77",
            "repository_selection": "selected",
            "permissions": {"contents": "write"},
            "suspended_at": None,
        }
        store.upsert_github_installation(
            actor_id=owner,
            workspace_id=workspace,
            installation=installation,
        )
        store.sync_github_repositories(
            workspace_id=workspace,
            installation_id=77,
            repositories=[{
                "id": 101,
                "full_name": "onedayonemasterpiece/projects-hub",
                "default_branch": "main",
                "private": True,
            }],
        )
        with pytest.raises(StoreError) as bind:
            service.bind_repository(
                actor_id=member,
                workspace_id=workspace,
                repository_id=101,
                project_id=None,
                role="project_docs",
                access_mode="read_only",
                allowed_paths=[],
            )
        assert bind.value.code == "FORBIDDEN"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_binding_never_upgrades_external_repo_and_tokens_are_numeric_repo_scoped(tmp_path: Path):
    store, boot, client, service = setup(tmp_path)
    try:
        owner = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        installation = await client.get_installation(77)
        store.upsert_github_installation(
            actor_id=owner,
            workspace_id=workspace,
            installation=installation,
        )
        store.sync_github_repositories(
            workspace_id=workspace,
            installation_id=77,
            repositories=client.repositories,
        )
        project = next(item["id"] for item in boot["projects"] if item["name"] == "Projects Hub")

        with pytest.raises(StoreError) as denied:
            service.bind_repository(
                actor_id=owner,
                workspace_id=workspace,
                repository_id=202,
                project_id=project,
                role="external_owning_repo",
                access_mode="app_managed_write",
                allowed_paths=[],
            )
        assert denied.value.code == "GITHUB_WRITE_POLICY_DENIED"

        connection = service.bind_repository(
            actor_id=owner,
            workspace_id=workspace,
            repository_id=101,
            project_id=project,
            role="project_docs",
            access_mode="app_managed_write",
            allowed_paths=["docs/product"],
        )
        assert connection["allowed_paths"] == ["docs/product"]

        token, _ = await service.repository_token(
            actor_id=owner,
            workspace_id=workspace,
            repository_id=101,
            write=True,
        )
        assert token.startswith("ghs_APPID_JWT_")
        assert client.token_calls[-1] == {
            "installation_id": 77,
            "repository_ids": [101],
            "permissions": {"contents": "write"},
        }
    finally:
        store.close()


@pytest.mark.asyncio
async def test_webhook_removal_and_suspension_revoke_access_idempotently(tmp_path: Path):
    store, boot, client, service = setup(tmp_path)
    try:
        owner = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        store.upsert_github_installation(
            actor_id=owner,
            workspace_id=workspace,
            installation=await client.get_installation(77),
        )
        store.sync_github_repositories(
            workspace_id=workspace,
            installation_id=77,
            repositories=client.repositories,
        )

        payload = json.dumps(
            {
                "action": "removed",
                "installation": {"id": 77},
                "repositories_added": [],
                "repositories_removed": [{"id": 101}],
            },
            separators=(",", ":"),
        ).encode()
        signature = "sha256=" + hmac.new(
            b"webhook-test-secret",
            payload,
            hashlib.sha256,
        ).hexdigest()
        first = await service.webhook(
            body=payload,
            signature=signature,
            delivery_id="delivery-1",
            event_name="installation_repositories",
        )
        second = await service.webhook(
            body=payload,
            signature=signature,
            delivery_id="delivery-1",
            event_name="installation_repositories",
        )
        assert first["ok"] is True
        assert second == {"ok": True, "duplicate": True}
        rows = {row["repository_id"]: row for row in service.status(owner, workspace)["repositories"]}
        assert rows[101]["state"] == "unavailable"
        assert rows[202]["state"] == "available"

        suspend = json.dumps(
            {"action": "suspend", "installation": {"id": 77}},
            separators=(",", ":"),
        ).encode()
        suspend_signature = "sha256=" + hmac.new(
            b"webhook-test-secret",
            suspend,
            hashlib.sha256,
        ).hexdigest()
        await service.webhook(
            body=suspend,
            signature=suspend_signature,
            delivery_id="delivery-2",
            event_name="installation",
        )
        status = service.status(owner, workspace)
        assert status["installations"][0]["state"] == "suspended"
        assert all(item["state"] == "unavailable" for item in status["repositories"])

        with pytest.raises(StoreError) as invalid:
            await service.webhook(
                body=suspend,
                signature="sha256=" + ("0" * 64),
                delivery_id="delivery-3",
                event_name="installation",
            )
        assert invalid.value.code == "GITHUB_WEBHOOK_INVALID"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_install_callback_rolls_back_state_and_metadata_if_catalogue_persistence_fails(tmp_path: Path):
    store, boot, client, service = setup(tmp_path)
    try:
        owner = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        started = service.start_install(actor_id=owner, workspace_id=workspace)
        state = parse_qs(urlparse(started["install_url"]).query)["state"][0]

        client.repositories = [
            {
                "id": 0,
                "full_name": "broken",
                "default_branch": "",
                "private": True,
            }
        ]
        with pytest.raises(StoreError) as first:
            await service.complete_install(
                actor_id=owner,
                state=state,
                installation_id=77,
            )
        assert first.value.code == "GITHUB_INVALID_REPOSITORY"
        state_row = store.db.execute(
            "SELECT consumed_at_ms FROM github_install_states"
        ).fetchone()
        assert state_row["consumed_at_ms"] is None
        assert store.db.execute(
            "SELECT COUNT(*) FROM github_installations"
        ).fetchone()[0] == 0
        assert store.db.execute(
            "SELECT COUNT(*) FROM repository_connections"
        ).fetchone()[0] == 0

        client.repositories = [
            {
                "id": 101,
                "full_name": "onedayonemasterpiece/projects-hub",
                "default_branch": "main",
                "private": True,
            }
        ]
        completed = await service.complete_install(
            actor_id=owner,
            state=state,
            installation_id=77,
        )
        assert completed["repository_count"] == 1
        state_row = store.db.execute(
            "SELECT consumed_at_ms FROM github_install_states"
        ).fetchone()
        assert state_row["consumed_at_ms"] is not None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_manifest_flow_bootstraps_and_persists_github_app_without_manual_secrets(tmp_path: Path):
    data_dir = tmp_path / "data"
    store = DurableStore(data_dir)
    boot = store.ensure_platform_owner("Owner")
    dynamic = Settings(
        data_dir=data_dir,
        static_dir=tmp_path / "dist",
        session_secret="s" * 48,
        dev_auth=True,
        cookie_secure=False,
        public_origin="https://projects-hub.kenigevents.ru",
    )

    async def exchange(code: str):
        assert code == "manifestcode123"
        return {
            "id": 9001,
            "slug": "projects-hub-abcd1234",
            "pem": "-----BEGIN RSA PRIVATE KEY-----\ntest-only\n-----END RSA PRIVATE KEY-----",
            "webhook_secret": "webhook-secret-from-github-12345",
        }

    service = GitHubConnections(
        store,
        dynamic,
        now=time.time,
        manifest_exchange=exchange,
    )
    try:
        owner = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        started = service.start_manifest_registration(
            actor_id=owner,
            workspace_id=workspace,
        )
        assert started["action_url"].endswith(
            "/api/github/app-manifest/legacy-launch?state=" + started["state"]
        )
        assert started["launch_url"].endswith(
            "/api/github/app-manifest/launch?state=" + started["state"]
        )
        launched = service.manifest_launch(started["state"])
        assert launched["state"] == started["state"]
        manifest = json.loads(launched["manifest"])
        assert manifest["url"] == "https://projects-hub.kenigevents.ru"
        assert manifest["redirect_url"].endswith("/api/github/app-manifest/callback")
        assert manifest["setup_url"].endswith("/api/github/install/callback")
        assert manifest["setup_on_update"] is True
        assert manifest["hook_attributes"]["url"].endswith("/api/github/webhook")
        assert manifest["default_permissions"] == {
            "metadata": "read",
            "contents": "write",
        }
        assert "default_events" not in manifest

        completed = await service.complete_manifest_registration(
            state=started["state"],
            code="manifestcode123",
        )
        assert completed["install_url"].startswith(
            "https://github.com/apps/projects-hub-abcd1234/installations/new"
        )
        assert service.configured is True
        persisted = data_dir / ".github-app.json"
        assert persisted.is_file()
        assert persisted.stat().st_mode & 0o077 == 0

        reloaded = GitHubConnections(store, dynamic, now=time.time)
        assert reloaded.configured is True
        assert reloaded.status(owner, workspace)["bootstrap_available"] is True

        with pytest.raises(StoreError) as replay:
            await service.complete_manifest_registration(
                state=started["state"],
                code="manifestcode123",
            )
        assert replay.value.code == "GITHUB_APP_MANIFEST_STATE_INVALID"
    finally:
        store.close()
