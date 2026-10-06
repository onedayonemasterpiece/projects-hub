from __future__ import annotations

from pathlib import Path

import pytest

from projects_hub.collaboration import CollaborationService
from projects_hub.note_processing import NoteSummary, ProcessedNote
from projects_hub.store import DurableStore, StoreError


class FakeNoteProcessor:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail_once = fail_once
        self.closed = False

    async def summarize(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.fail_once:
            self.fail_once = False
            from projects_hub.note_processing import NoteProcessingError
            raise NoteProcessingError(
                "NOTE_PROCESSOR_UNAVAILABLE",
                "temporary processor failure",
                retryable=True,
            )
        source = str(kwargs["source_text"])
        title = str(kwargs.get("suggested_title") or "Project note")
        return ProcessedNote(
            summary=NoteSummary(
                title=title,
                short_summary=source,
                detailed_summary=source,
                theses=["One personal timeline"],
                ideas=[],
                decisions=[],
                tasks=[],
                facts=[],
                entities=[],
                related_projects=[],
                open_questions=[],
                contradictions=[],
                uncertain_fragments=[],
                tags=["collaboration"],
            ),
            model="gemini-test",
            prompt_version="test-v1",
            request_uid="req-test",
            limiter={"contract": "test"},
        )

    async def close(self):
        self.closed = True


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
            "commit_sha": "commit-test",
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
    processor = FakeNoteProcessor()
    service = CollaborationService(
        store,
        github,  # type: ignore[arg-type]
        note_processor=processor,  # type: ignore[arg-type]
    )
    return store, service, github, processor, owner, project_id


@pytest.mark.asyncio
async def test_two_participant_note_roundtrip_reply_and_acl(tmp_path: Path):
    store, service, github, processor, owner, project_id = _bound_store(tmp_path)
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
    assert "## Основные тезисы" in stored["text"]
    assert note["source_text"] == "Use one personal timeline."
    assert note["structured"]["title"] == "Decision"
    assert note["status"] == "ready"
    assert note["processing"]["model"] == "gemini-test"
    assert note["repository"]["commit_sha"] == "commit-test"
    assert len(processor.calls) == 1
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
    assert len(processor.calls) == 1

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
    store, service, _github, _processor, owner, project_id = _bound_store(tmp_path)
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


@pytest.mark.asyncio
async def test_note_source_survives_processor_failure_and_same_command_resumes(tmp_path: Path):
    store = DurableStore(tmp_path / "retry")
    owner = store.ensure_platform_owner("Owner Retry")
    actor = owner["actor"]["id"]
    workspace = owner["workspace"]["id"]
    project = owner["projects"][0]["id"]
    now = 1_800_000_000_000
    with store._lock:
        store.db.execute(
            """INSERT INTO github_installations(
                   installation_id,workspace_id,account_id,account_login,account_type,
                   html_url,repository_selection,permissions_json,state,suspended_at_ms,
                   last_verified_at_ms,created_at_ms,updated_at_ms)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                88, workspace, 102, "example", "Organization",
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
                "repo-retry", workspace, 88, 502, "example/private-docs", "main",
                1, project, "project_docs", "app_managed_write", '["docs/notes"]',
                "available", now, now, now,
            ),
        )
    github = FakeGitHubConnections(store)
    processor = FakeNoteProcessor(fail_once=True)
    service = CollaborationService(
        store,
        github,  # type: ignore[arg-type]
        note_processor=processor,  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(StoreError) as exc:
            await service.create_note(
                actor_id=actor,
                workspace_id=workspace,
                project_id=project,
                command_id="cmd.note.retry1",
                title="Retry note",
                body="Исходный текст должен сохраниться до модели.",
            )
        assert exc.value.code == "NOTE_PROCESSOR_UNAVAILABLE"
        with store._lock:
            saved = store.db.execute(
                "SELECT * FROM project_notes WHERE command_id='cmd.note.retry1'"
            ).fetchone()
        assert saved is not None
        assert saved["source_text"] == "Исходный текст должен сохраниться до модели."
        assert saved["status"] == "blocked"
        assert saved["repository_sha"] == ""
        assert github.write_count == 0

        ready = await service.create_note(
            actor_id=actor,
            workspace_id=workspace,
            project_id=project,
            command_id="cmd.note.retry1",
            title="Retry note",
            body="Исходный текст должен сохраниться до модели.",
        )
        assert ready["id"] == saved["id"]
        assert ready["status"] == "ready"
        assert len(processor.calls) == 2
        assert github.write_count == 1
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_note_intent_survives_missing_binding_and_same_command_resumes(tmp_path: Path):
    store = DurableStore(tmp_path / "missing-binding")
    owner = store.ensure_platform_owner("Owner Missing Binding")
    actor = owner["actor"]["id"]
    workspace = owner["workspace"]["id"]
    project = owner["projects"][0]["id"]
    github = FakeGitHubConnections(store)
    processor = FakeNoteProcessor()
    service = CollaborationService(
        store,
        github,  # type: ignore[arg-type]
        note_processor=processor,  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(StoreError) as exc:
            await service.create_note(
                actor_id=actor,
                workspace_id=workspace,
                project_id=project,
                command_id="cmd.note.binding",
                title="Binding recovery",
                body="Этот исходный текст нельзя потерять из-за отсутствующего binding.",
            )
        assert exc.value.code == "GITHUB_PROJECT_DOCS_REQUIRED"
        with store._lock:
            intent = store.db.execute(
                "SELECT * FROM project_note_intents WHERE command_id='cmd.note.binding'"
            ).fetchone()
            materialized = store.db.execute(
                "SELECT 1 FROM project_notes WHERE command_id='cmd.note.binding'"
            ).fetchone()
        assert intent is not None
        assert intent["source_text"] == "Этот исходный текст нельзя потерять из-за отсутствующего binding."
        assert intent["status"] == "blocked"
        assert intent["error_code"] == "GITHUB_PROJECT_DOCS_REQUIRED"
        assert materialized is None
        assert processor.calls == []
        assert github.write_count == 0

        now = 1_800_000_000_000
        with store._lock:
            store.db.execute(
                """INSERT INTO github_installations(
                       installation_id,workspace_id,account_id,account_login,account_type,
                       html_url,repository_selection,permissions_json,state,suspended_at_ms,
                       last_verified_at_ms,created_at_ms,updated_at_ms)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    90, workspace, 110, "example", "Organization",
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
                    "repo-after-bind", workspace, 90, 510, "example/private-after-bind", "main",
                    1, project, "project_docs", "app_managed_write", '["docs/notes"]',
                    "available", now, now, now,
                ),
            )

        ready = await service.create_note(
            actor_id=actor,
            workspace_id=workspace,
            project_id=project,
            command_id="cmd.note.binding",
            title="Binding recovery",
            body="Этот исходный текст нельзя потерять из-за отсутствующего binding.",
        )
        assert ready["id"] == intent["id"]
        assert ready["status"] == "ready"
        assert ready["audience"] == "project"
        assert len(processor.calls) == 1
        assert github.write_count == 1
        with store._lock:
            intent_after = store.db.execute(
                "SELECT * FROM project_note_intents WHERE id=?",
                (ready["id"],),
            ).fetchone()
        assert intent_after["status"] == "ready"
        assert intent_after["error_code"] is None
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_project_note_refuses_public_repository_before_processing(tmp_path: Path):
    store = DurableStore(tmp_path / "public-binding")
    owner = store.ensure_platform_owner("Owner Public Binding")
    actor = owner["actor"]["id"]
    workspace = owner["workspace"]["id"]
    project = owner["projects"][0]["id"]
    now = 1_800_000_000_000
    with store._lock:
        store.db.execute(
            """INSERT INTO github_installations(
                   installation_id,workspace_id,account_id,account_login,account_type,
                   html_url,repository_selection,permissions_json,state,suspended_at_ms,
                   last_verified_at_ms,created_at_ms,updated_at_ms)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                91, workspace, 111, "example", "Organization",
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
                "repo-public", workspace, 91, 511, "example/public-docs", "main",
                0, project, "project_docs", "app_managed_write", '["docs/notes"]',
                "available", now, now, now,
            ),
        )
    github = FakeGitHubConnections(store)
    processor = FakeNoteProcessor()
    service = CollaborationService(
        store,
        github,  # type: ignore[arg-type]
        note_processor=processor,  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(StoreError) as exc:
            await service.create_note(
                actor_id=actor,
                workspace_id=workspace,
                project_id=project,
                command_id="cmd.note.public",
                title="Must stay project-private",
                body="Этот текст предназначен только участникам проекта.",
            )
        assert exc.value.code == "NOTE_AUDIENCE_REPOSITORY_MISMATCH"
        with store._lock:
            intent = store.db.execute(
                "SELECT * FROM project_note_intents WHERE command_id='cmd.note.public'"
            ).fetchone()
            materialized = store.db.execute(
                "SELECT 1 FROM project_notes WHERE command_id='cmd.note.public'"
            ).fetchone()
        assert intent is not None
        assert intent["audience"] == "project"
        assert intent["status"] == "blocked"
        assert intent["error_code"] == "NOTE_AUDIENCE_REPOSITORY_MISMATCH"
        assert materialized is None
        assert processor.calls == []
        assert github.write_count == 0
    finally:
        await service.close()
        store.close()
