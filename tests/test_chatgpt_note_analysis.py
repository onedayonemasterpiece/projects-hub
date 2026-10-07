from __future__ import annotations

import pytest

from projects_hub.chatgpt_note_analysis import (
    ROUTING_PATH,
    parse_analysis_markdown,
    parse_routing_yaml,
)
from projects_hub.store import StoreError

from test_collaboration import _bound_store


def route_yaml(note: dict, repository: str = "example/project-docs") -> str:
    path = note["repository"]["path"]
    return (
        "schema_version: 1\n"
        "enabled: true\n"
        "source_repository: " + repository + "\n"
        "require_private_repository: true\n"
        "require_source_blob_match: true\n"
        "routes:\n"
        "  - id: owner-deep-note\n"
        "    enabled: true\n"
        f"    project_id: {note['project_id']}\n"
        f"    note_id: {note['id']}\n"
        f"    source_path: {path}\n"
        f"    result_path: {path[:-3]}.chatgpt-analysis.md\n"
    )


def result_markdown(note: dict, source_sha: str) -> str:
    path = note["repository"]["path"]
    return (
        "---\n"
        "analysis_schema: projects-hub-chatgpt-v1\n"
        "route_id: owner-deep-note\n"
        f"note_id: {note['id']}\n"
        f"project_id: {note['project_id']}\n"
        f"source_path: {path}\n"
        f"source_sha: {source_sha}\n"
        "audience: project\n"
        "status: completed\n"
        "provider: chatgpt_scheduled\n"
        "generated_at_utc: 2026-10-07T21:00:00Z\n"
        "---\n\n"
        "# Глубокий анализ ChatGPT\n\n"
        "## Главные выводы\n\n"
        "Необходимо сохранить одну личную ленту, проверить права второго участника "
        "и не принимать предложения модели как утверждённые задачи.\n\n"
        "## Источники и редакции\n\n"
        f"Исходный документ {path}, blob {source_sha}.\n"
    )


def test_manifest_and_analysis_fail_closed_on_route_and_metadata_mismatch():
    sample = {
        "project_id": "prj_" + "a" * 32,
        "id": "note_" + "b" * 32,
        "repository": {"path": "docs/notes/bb/note-123.md"},
    }
    routes = parse_routing_yaml(route_yaml(sample), repository="example/project-docs")
    assert len(routes) == 1
    assert routes[0]["result_path"] == "docs/notes/bb/note-123.chatgpt-analysis.md"
    with pytest.raises(ValueError):
        parse_routing_yaml(
            route_yaml(sample).replace("docs/notes/bb/", "docs/notes/../"),
            repository="example/project-docs",
        )
    with pytest.raises(ValueError):
        parse_routing_yaml(
            route_yaml(sample),
            repository="another/private-repository",
        )
    with pytest.raises(ValueError):
        parse_analysis_markdown(result_markdown(sample, "badsha"))
    meta, body = parse_analysis_markdown(result_markdown(sample, "a" * 40))
    assert meta["note_id"] == sample["id"]
    assert "одну личную ленту" in body


@pytest.mark.asyncio
async def test_verified_chatgpt_companion_import_notifies_once_and_is_acl_scoped(tmp_path):
    store, service, github, _processor, owner, project_id = _bound_store(tmp_path)
    actor = owner["actor"]["id"]
    workspace = owner["workspace"]["id"]
    try:
        note = await service.create_note(
            actor_id=actor,
            workspace_id=workspace,
            project_id=project_id,
            command_id="cmd.note.chatgpt.import",
            title="Hourly deep review",
            body="One personal timeline and explicit project audience.",
        )
        source_path = note["repository"]["path"]
        result_path = source_path[:-3] + ".chatgpt-analysis.md"
        github.files[(501, source_path)]["sha"] = "a" * 40
        with store._lock:
            store.db.execute(
                "UPDATE project_notes SET repository_sha=? WHERE id=?",
                ("a" * 40, note["id"]),
            )
        github.files[(501, ROUTING_PATH)] = {
            "text": route_yaml(note), "sha": "f" * 40,
        }

        # No result means nothing is imported and no event is manufactured.
        assert await service.chatgpt_analysis.poll_once(force=True) == 0
        assert service.get_note(
            actor_id=actor, workspace_id=workspace, note_id=note["id"]
        )["chatgpt_analysis"] is None

        github.files[(501, result_path)] = {
            "text": result_markdown(note, "b" * 40),
            "sha": "c" * 40,
        }
        assert await service.chatgpt_analysis.poll_once(force=True) == 0

        github.files[(501, result_path)]["text"] = result_markdown(note, "a" * 40)
        assert await service.chatgpt_analysis.poll_once(force=True) == 1
        assert await service.chatgpt_analysis.poll_once(force=True) == 0
        result = service.get_note(
            actor_id=actor, workspace_id=workspace, note_id=note["id"]
        )["chatgpt_analysis"]
        assert result is not None
        assert result["source_sha"] == "a" * 40
        assert result["result_sha"] == "c" * 40
        assert "Необходимо сохранить одну личную ленту" in result["markdown"]

        brief = service.personal_brief(actor_id=actor, workspace_id=workspace)
        updates = [
            item for item in brief["personal"]
            if item["kind"] == "note_chatgpt_analyzed"
        ]
        assert len(updates) == 1
        assert updates[0]["object_id"] == note["id"]

        ungranted = "usr_ungranted_chatgpt"
        with store._lock:
            store.db.execute(
                "INSERT INTO actors(id,display_name,created_at_ms) VALUES(?,?,?)",
                (ungranted, "Other", 1),
            )
            store.db.execute(
                "INSERT INTO memberships(actor_id,workspace_id,role) VALUES(?,?,?)",
                (ungranted, workspace, "member"),
            )
        with pytest.raises(StoreError) as exc:
            service.chatgpt_analysis.for_note(
                actor_id=ungranted,
                workspace_id=workspace,
                note_id=note["id"],
            )
        assert exc.value.code == "PROJECT_FORBIDDEN"
    finally:
        await service.close()
        store.close()


def test_manual_chatgpt_import_endpoint_is_platform_owner_only(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from projects_hub.app import create_app
    from projects_hub.auth import COOKIE_NAME, issue_session
    from projects_hub.settings import Settings

    store, service, _github, _processor, owner, _project_id = _bound_store(tmp_path)
    actor = owner["actor"]["id"]
    workspace = owner["workspace"]["id"]
    other = "usr_chatgpt_sync_other"
    with store._lock:
        store.db.execute(
            "INSERT INTO actors(id,display_name,created_at_ms) VALUES(?,?,?)",
            (other, "Other member", 1),
        )
        store.db.execute(
            "INSERT INTO memberships(actor_id,workspace_id,role) VALUES(?,?,?)",
            (other, workspace, "member"),
        )

    calls = []
    async def fake_poll(*, force=False):
        calls.append(force)
        return 1
    monkeypatch.setattr(service.chatgpt_analysis, "poll_once", fake_poll)
    settings = Settings(
        data_dir=tmp_path,
        static_dir=tmp_path / "missing-ui",
        session_secret="chatgpt-import-test-secret-" * 3,
        dev_auth=True,
        cookie_secure=False,
    )
    app = create_app(settings, store=store, collaboration=service)
    try:
        with TestClient(app, base_url="http://testserver") as client:
            client.cookies.set(COOKIE_NAME, issue_session(actor, settings.session_secret))
            accepted = client.post(
                "/api/collaboration/chatgpt/sync",
                json={"workspace_id": workspace},
            )
            assert accepted.status_code == 200
            assert accepted.json() == {"imported": 1, "status": "checked"}
            assert True in calls

            client.cookies.set(COOKIE_NAME, issue_session(other, settings.session_secret))
            rejected = client.post(
                "/api/collaboration/chatgpt/sync",
                json={"workspace_id": workspace},
            )
            assert rejected.status_code == 403
            assert rejected.json()["error"]["code"] == "FORBIDDEN"
    finally:
        store.close()
