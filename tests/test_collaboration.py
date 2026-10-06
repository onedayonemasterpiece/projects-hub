from __future__ import annotations

from pathlib import Path

import pytest

from projects_hub.collaboration import CollaborationService
from projects_hub.store import DurableStore, StoreError


class FakeGitHubConnections:
    def __init__(self, store: DurableStore) -> None:
        self.store = store
        self.files: dict[tuple[int, str], dict[str, str]] = {}
        self.write_count = 0

    async def write_repository_text(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        repository_id: int,
        project_id: str,
        path: str,
        text: str,
        message: str,
    ) -> dict:
        access = self.store.project_access(
            actor_id, workspace_id, project_id, require_role="editor"
        )
        assert access["role"] in {"editor", "owner"}
        self.write_count += 1
        key = (repository_id, path)
        current = self.files.get(key)
        if current is not None and current["text"] != text:
            raise StoreError("GITHUB_WRITE_CONFLICT", "different content")
        if current is None:
            current = {
                "text": text,
                "sha": f"sha-{len(self.files) + 1}",
            }
            self.files[key] = current
        return {
            "repository_id": repository_id,
            "full_name": "example/project-docs",
            "project_id": project_id,
            "default_branch": "main",
            "kind": "file",
            "path": path,
            "text": current["text"],
            "sha": current["sha"],
        }

    async def read_repository_path(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        repository_id: int,
        path: str = "",
    ) -> dict:
        # The real GitHubConnections performs the same project ACL check.
        connection = self.store.get_repository_connection(
            actor_id, workspace_id, repository_id
        )
        self.store.project_access(
            actor_id, workspace_id, str(connection["project_id"])
        )
        current = self.files[(repository_id, path)]
        return {
            "repository_id": repository_id,
            "full_name": connection["full_name"],
            "project_id": connection["project_id"],
            "default_branch": connection["default_branch"],
            "kind": "file",
            "path": path,
            "text": current["text"],
            "sha": current["sha"],
        }


def _bound_store(tmp_path: Path):
    store = DurableStore(tmp_path)
    owner = store.ensure_platform_owner("Owner A")
    actor_a = owner["actor"]["id"]
    workspace_id = owner["workspace"]["id"]
    project_id = owner["projects"][0]["id"]
    now = 1_800_000_000_000
    with store._lock:
        store.db.execute(
            """INSERT INTO github_installations(
                   installation_id,workspace_id,account_id,account_login,account_type,
                   html_url,repository_selection,permissions_json,state,suspended_at_ms,
                   last_verified_at_ms,created_at_ms,updated_at_ms)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                77, workspace_id, 101, "example", "Organization",
                "https://github.com/example", "selected", '{"contents":"write"}',
                "active", None, now, now, now,
            ),
        )
        store.db.execute(
            """INSERT INTO repository_connections(
                   id,workspace_id,installation_id,repository_id,full_name,default_branch,
                   private,project_id,role,access_mode,allowed_paths_json,state,
                   last_verified_at_ms,created_at_ms,updated_at_ms)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "repo-1", workspace_id, 77, 501, "example/project-docs", "main",
                1, project_id, "project_docs", "app_managed_write", '["docs/notes"]',
                "available", now, now, now,
            ),
        )
    github = FakeGitHubConnections(store)
    service = CollaborationService(store, github)  # type: ignore[arg-type]
    return store, service, github, owner, project_id


@pytest.mark.asyncio
async def test_two_participant_note_roundtrip_reply_and_acl(tmp_path: Path):
    store, service, github, owner, project_id = _bound_store(tmp_path)
    actor_a = owner["actor"]["id"]
    workspace_id = owner["workspace"]["id"]

    note = await service.create_note(
        actor_id=actor_a,
        workspace_id=workspace_id,
        project_id=project_id,
        command_id="cmd.note.0001",
        title="Decision",
        body="Use one personal timeline.",
    )
    assert note["repository"]["path"].startswith("docs/notes/")
    stored = github.files[(501, note["repository"]["path"])]
    assert stored["text"].startswith("---\nnote_id:")
    assert "Use one personal timeline." in stored["text"]
    assert (await service.repository_readback(
        actor_id=actor_a, workspace_id=workspace_id, note_id=note["id"]
    ))["verified"] is True

    # Retry is idempotent and does not create a second repository write.
    again = await service.create_note(
        actor_id=actor_a,
        workspace_id=workspace_id,
        project_id=project_id,
        command_id="cmd.note.0001",
        title="Decision",
        body="Use one personal timeline.",
    )
    assert again["id"] == note["id"]
    assert github.write_count == 1

    invite = service.invite_participant(
        owner_actor_id=actor_a,
        workspace_id=workspace_id,
        project_id=project_id,
        display_name="Participant B",
        role="editor",
    )
    actor_b = invite["actor_id"]
    boot_b = store.bootstrap(actor_b, workspace_id)
    assert [item["id"] for item in boot_b["projects"]] == [project_id]
    assert boot_b["role"] == "member"

    # B has only Projects Hub identity/grant; no GitHub identity is needed.
    with store._lock:
        external = store.db.execute(
            "SELECT 1 FROM external_identities WHERE actor_id=?", (actor_b,)
        ).fetchone()
    assert external is None

    visible = service.get_note(
        actor_id=actor_b, workspace_id=workspace_id, note_id=note["id"]
    )
    assert visible["title"] == "Decision"
    reply = service.reply(
        actor_id=actor_b,
        workspace_id=workspace_id,
        note_id=note["id"],
        command_id="cmd.reply.0001",
        body="Согласна. Добавлю пример.",
    )
    assert reply["author"]["display_name"] == "Participant B"
    replies_for_a = service.list_replies(
        actor_id=actor_a, workspace_id=workspace_id, note_id=note["id"]
    )
    assert [item["id"] for item in replies_for_a] == [reply["id"]]

    retry_reply = service.reply(
        actor_id=actor_b,
        workspace_id=workspace_id,
        note_id=note["id"],
        command_id="cmd.reply.0001",
        body="Согласна. Добавлю пример.",
    )
    assert retry_reply["id"] == reply["id"]
    assert len(service.list_replies(
        actor_id=actor_a, workspace_id=workspace_id, note_id=note["id"]
    )) == 1

    actor_c = "usr_unauthorized"
    with store._lock:
        store.db.execute(
            "INSERT INTO actors(id,display_name,created_at_ms) VALUES(?,?,?)",
            (actor_c, "Participant C", 1),
        )
        store.db.execute(
            "INSERT INTO memberships(actor_id,workspace_id,role) VALUES(?,?,?)",
            (actor_c, workspace_id, "member"),
        )
    with pytest.raises(StoreError, match="Project is not available"):
        service.get_note(
            actor_id=actor_c, workspace_id=workspace_id, note_id=note["id"]
        )
    with pytest.raises(StoreError, match="Project is not available"):
        service.reply(
            actor_id=actor_c,
            workspace_id=workspace_id,
            note_id=note["id"],
            command_id="cmd.reply.0002",
            body="Should be denied.",
        )

    timeline_b = service.timeline(actor_id=actor_b, workspace_id=workspace_id)
    assert any(item["object_id"] == note["id"] for item in timeline_b)
    brief_a = service.personal_brief(actor_id=actor_a, workspace_id=workspace_id)
    assert any(item["object_id"] == reply["id"] for item in brief_a["personal"])

    store.close()


@pytest.mark.asyncio
async def test_note_command_conflict_is_rejected(tmp_path: Path):
    store, service, _github, owner, project_id = _bound_store(tmp_path)
    actor = owner["actor"]["id"]
    workspace = owner["workspace"]["id"]
    await service.create_note(
        actor_id=actor,
        workspace_id=workspace,
        project_id=project_id,
        command_id="cmd.note.conflict",
        title="One",
        body="A",
    )
    with pytest.raises(StoreError) as exc:
        await service.create_note(
            actor_id=actor,
            workspace_id=workspace,
            project_id=project_id,
            command_id="cmd.note.conflict",
            title="Two",
            body="B",
        )
    assert exc.value.code == "COLLABORATION_COMMAND_CONFLICT"
    store.close()
