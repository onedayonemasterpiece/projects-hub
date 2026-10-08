from live_tools import execute, bundle_setup
from pathlib import Path
from types import SimpleNamespace

import pytest

from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_resources import ConversationScope
from projects_hub.store import DurableStore


class FakeGitHubRead:
    async def read_repository_path(
        self,
        *,
        actor_id,
        workspace_id,
        repository_id,
        path="",
    ):
        return {
            "repository_id": repository_id,
            "full_name": "owner/projects-hub",
            "project_id": "project",
            "default_branch": "main",
            "kind": "file",
            "path": path,
            "size": 12,
            "sha": "abc123",
            "text": "# Projects Hub",
        }


@pytest.mark.asyncio
async def test_live_only_sees_bound_repository_catalogue_and_cannot_change_github_permissions(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("GitHub Live")
        actor = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        project = next(item["id"] for item in boot["projects"] if item["name"] == "Projects Hub")
        conversation = store.create_conversation(actor, workspace, project)

        installation = {
            "id": 77,
            "account": {"id": 501, "login": "owner", "type": "User"},
            "html_url": "https://github.com/settings/installations/77",
            "repository_selection": "selected",
            "permissions": {"metadata": "read", "contents": "write"},
            "suspended_at": None,
        }
        store.upsert_github_installation(
            actor_id=actor,
            workspace_id=workspace,
            installation=installation,
        )
        store.sync_github_repositories(
            workspace_id=workspace,
            installation_id=77,
            repositories=[
                {
                    "id": 101,
                    "full_name": "owner/projects-hub",
                    "default_branch": "main",
                    "private": True,
                },
                {
                    "id": 202,
                    "full_name": "owner/unassigned",
                    "default_branch": "main",
                    "private": True,
                },
            ],
        )
        store.bind_repository_connection(
            actor_id=actor,
            workspace_id=workspace,
            repository_id=101,
            project_id=project,
            role="project_docs",
            access_mode="read_only",
            allowed_paths=["docs/product"],
        )

        binding = ConversationScope(workspace, actor, conversation["id"]).resource_binding()
        adapter = ProjectsHubLiveAdapter(store, github_connections=FakeGitHubRead())
        initialized = adapter.initialize(
            resource_id=binding,
            actor={"subject": actor, "tenant_id": workspace},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
        )
        selected, _ = bundle_setup(adapter, initialized, "repositories")
        names = {item["name"] for item in selected["functions"]}
        assert "github_repositories_list" in names
        assert "github_repository_read" in names
        assert not any(
            name in names
            for name in {
                "github_repository_bind",
                "github_permissions_update",
                "github_installation_token",
                "github_connect_repository",
            }
        )

        session = SimpleNamespace(state=initialized["state"])
        result = await execute(adapter,
            session,
            {
                "name": "github_repositories_list",
                "id": "provider-github-catalogue",
                "args": {},
            },
        )
        assert result == {
            "repositories": [
                {
                    "repository_id": 101,
                    "full_name": "owner/projects-hub",
                    "project_id": project,
                    "role": "project_docs",
                    "access_mode": "read_only",
                    "allowed_paths": ["docs/product"],
                }
            ],
            "requires_connection": False,
        }
        assert "installation_id" not in str(result)
        assert "token" not in str(result).lower()
        read = await execute(adapter,
            session,
            {
                "name": "github_repository_read",
                "id": "provider-github-read",
                "args": {"repository_id": 101, "path": "README.md"},
            },
        )
        assert read["kind"] == "file"
        assert read["path"] == "README.md"
        assert read["text"] == "# Projects Hub"
    finally:
        store.close()


def test_browser_bundle_source_has_no_github_credentials_or_direct_github_api_client():
    root = Path(__file__).resolve().parents[1] / "web" / "src"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(root.glob("*.ts*"))
    )
    forbidden = (
        "@octokit",
        "api.github.com",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_WEBHOOK_SECRET",
        "installation_token",
        "ghs_APPID_JWT",
    )
    for marker in forbidden:
        assert marker not in source
