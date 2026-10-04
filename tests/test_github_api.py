from pathlib import Path

from fastapi.testclient import TestClient

from projects_hub.app import create_app
from projects_hub.settings import Settings
from projects_hub.store import DurableStore


PUBLIC_ORIGIN = "https://projects-hub.kenigevents.ru"


class FakeGitHubConnections:
    configured = True

    def __init__(self):
        self.calls = []

    def status(self, actor_id, workspace_id):
        self.calls.append(("status", actor_id, workspace_id))
        return {
            "configured": True,
            "bootstrap_available": True,
            "installations": [],
            "repositories": [],
        }

    def start_manifest_registration(self, *, actor_id, workspace_id, conversation_id=None):
        self.calls.append(("manifest_start", actor_id, workspace_id, conversation_id))
        return {
            "launch_url": "https://projects-hub.kenigevents.ru/api/github/app-manifest/launch?state=manifest-state",
            "action_url": "https://github.com/settings/apps/new",
            "manifest": "{\"name\":\"projects-hub-test\"}",
            "state": "manifest-state",
            "expires_at_ms": 9999999999999,
        }

    def manifest_launch(self, state):
        self.calls.append(("manifest_launch", state))
        return {
            "action_url": "https://github.com/settings/apps/new",
            "manifest": "{\"name\":\"projects-hub-test\"}",
            "state": state,
            "workspace_id": "ws_fake",
        }

    async def complete_manifest_registration(self, *, state, code, actor_id=None):
        self.calls.append(("manifest_callback", actor_id, state, code))
        return {
            "workspace_id": "ws_fake",
            "install_url": "https://github.com/apps/projects-hub/installations/new?state=install-state",
            "expires_at_ms": 9999999999999,
        }

    def start_install(self, *, actor_id, workspace_id, conversation_id=None):
        self.calls.append(("start", actor_id, workspace_id, conversation_id))
        return {
            "install_url": "https://github.com/apps/projects-hub/installations/new?state=opaque",
            "expires_at_ms": 9999999999999,
        }

    async def complete_install(self, *, state, installation_id, actor_id=None):
        self.calls.append(("callback", actor_id, state, installation_id))
        return {
            "workspace_id": "ws_fake",
            "conversation_id": None,
            "installation": {
                "installation_id": installation_id,
                "account_id": 1,
                "account_login": "owner",
                "account_type": "User",
                "state": "active",
            },
            "repository_count": 1,
        }

    def bind_repository(
        self,
        *,
        actor_id,
        workspace_id,
        repository_id,
        project_id,
        role,
        access_mode,
        allowed_paths,
    ):
        self.calls.append(
            (
                "bind",
                actor_id,
                workspace_id,
                repository_id,
                project_id,
                role,
                access_mode,
                allowed_paths,
            )
        )
        return {
            "id": "repo_1",
            "installation_id": 77,
            "repository_id": repository_id,
            "full_name": "owner/repo",
            "default_branch": "main",
            "private": True,
            "project_id": project_id,
            "role": role,
            "access_mode": access_mode,
            "allowed_paths": allowed_paths,
            "state": "available",
            "installation_state": "active",
        }

    async def webhook(self, *, body, signature, delivery_id, event_name):
        self.calls.append(("webhook", signature, delivery_id, event_name, body))
        return {"ok": True}


def dev_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        static_dir=tmp_path / "missing",
        session_secret="s" * 48,
        dev_auth=True,
        cookie_secure=False,
    )


def public_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        static_dir=tmp_path / "missing",
        session_secret="s" * 48,
        dev_auth=True,
        cookie_secure=True,
        public_origin=PUBLIC_ORIGIN,
    )


def public_headers(*, origin: bool = False) -> dict[str, str]:
    headers = {
        "host": "projects-hub.kenigevents.ru",
        "x-forwarded-host": "projects-hub.kenigevents.ru",
        "x-forwarded-proto": "https",
        "x-forwarded-port": "443",
    }
    if origin:
        headers["origin"] = PUBLIC_ORIGIN
    return headers


def test_owner_http_flow_exposes_only_connection_metadata(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    github = FakeGitHubConnections()
    app = create_app(
        dev_settings(tmp_path),
        store=store,
        github_connections=github,
    )
    with TestClient(app, base_url="http://localhost") as client:
        login = client.post("/api/dev/login", json={})
        assert login.status_code == 200
        actor_id = login.json()["actor"]["id"]
        workspace_id = login.json()["workspace"]["id"]
        project_id = login.json()["projects"][0]["id"]

        status = client.get(
            "/api/github/status",
            params={"workspace_id": workspace_id},
        )
        assert status.status_code == 200
        assert status.json() == {
            "configured": True,
            "bootstrap_available": True,
            "installations": [],
            "repositories": [],
        }

        manifest = client.post(
            "/api/github/app-manifest/start",
            json={"workspace_id": workspace_id, "conversation_id": None},
        )
        assert manifest.status_code == 200
        assert manifest.json()["action_url"] == "https://github.com/settings/apps/new"
        assert manifest.json()["state"] == "manifest-state"

        external = TestClient(app, base_url="http://localhost")
        launch = external.get(
            "/api/github/app-manifest/launch",
            params={"state": "manifest-state"},
        )
        assert launch.status_code == 200
        assert 'method="post"' in launch.text
        assert "https://github.com/settings/apps/new" in launch.text

        manifest_callback = external.get(
            "/api/github/app-manifest/callback",
            params={"code": "manifestcode", "state": "manifest-state"},
            follow_redirects=False,
        )
        assert manifest_callback.status_code == 303
        assert manifest_callback.headers["location"].startswith(
            "https://github.com/apps/projects-hub/installations/new"
        )

        started = client.post(
            "/api/github/install/start",
            json={"workspace_id": workspace_id, "conversation_id": None},
        )
        assert started.status_code == 200
        assert started.json()["install_url"].startswith("https://github.com/apps/")

        callback = client.get(
            "/api/github/install/callback",
            params={
                "installation_id": 77,
                "state": "opaque",
                "setup_action": "install",
            },
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == "/?github=connected"

        bound = client.post(
            "/api/github/repositories/101/bind",
            json={
                "workspace_id": workspace_id,
                "project_id": project_id,
                "role": "project_docs",
                "access_mode": "read_only",
                "allowed_paths": ["docs/product"],
            },
        )
        assert bound.status_code == 200
        assert bound.json()["repository_id"] == 101
        assert bound.json()["role"] == "project_docs"

        assert ("status", actor_id, workspace_id) in github.calls
        assert any(call[0] == "callback" and call[3] == 77 for call in github.calls)
        assert any(call[0] == "bind" and call[3] == 101 for call in github.calls)
    store.close()


def test_public_github_webhook_does_not_require_browser_origin(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    github = FakeGitHubConnections()
    app = create_app(
        public_settings(tmp_path),
        store=store,
        github_connections=github,
    )
    with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
        response = client.post(
            "/api/github/webhook",
            content=b'{"action":"deleted","installation":{"id":77}}',
            headers={
                **public_headers(origin=False),
                "content-type": "application/json",
                "x-hub-signature-256": "sha256=" + ("a" * 64),
                "x-github-delivery": "delivery-1",
                "x-github-event": "installation",
            },
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        call = next(item for item in github.calls if item[0] == "webhook")
        assert call[1] == "sha256=" + ("a" * 64)
        assert call[2] == "delivery-1"
        assert call[3] == "installation"
    store.close()
