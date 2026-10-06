from __future__ import annotations

from pathlib import Path

import pytest

from projects_hub.collaboration import CollaborationService
from projects_hub.store import DurableStore


class NoopGitHub:
    pass


def setup(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    owner = store.ensure_platform_owner("Owner A")
    actor_a = owner["actor"]["id"]
    workspace = owner["workspace"]["id"]
    project = owner["projects"][0]["id"]
    service = CollaborationService(store, NoopGitHub())  # type: ignore[arg-type]
    invite = service.invite_participant(
        owner_actor_id=actor_a,
        workspace_id=workspace,
        project_id=project,
        display_name="Participant B",
        role="editor",
    )
    return store, service, actor_a, invite["actor_id"], workspace, project


def add_event(
    store: DurableStore,
    *,
    workspace: str,
    project: str,
    actor: str,
    addressed_to: str | None,
    kind: str,
    object_id: str,
    summary: str,
):
    with store._lock:
        store.db.execute(
            """INSERT INTO collaboration_events(
                   workspace_id,project_id,actor_id,kind,object_kind,object_id,
                   parent_object_id,addressed_to_actor_id,summary,created_at_ms)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                workspace, project, actor, kind, "note", object_id,
                None, addressed_to, summary, 1_800_000_000_000,
            ),
        )


def test_personal_first_cursor_and_quiet_general_news(tmp_path: Path):
    store, service, actor_a, actor_b, workspace, project = setup(tmp_path)
    try:
        add_event(
            store,
            workspace=workspace,
            project=project,
            actor=actor_b,
            addressed_to=actor_a,
            kind="question_addressed",
            object_id="q_personal_1",
            summary="Нужно решение владельца",
        )
        add_event(
            store,
            workspace=workspace,
            project=project,
            actor=actor_b,
            addressed_to=None,
            kind="note_created",
            object_id="note_general_1",
            summary="Ольга добавила заметку",
        )
        add_event(
            store,
            workspace=workspace,
            project=project,
            actor=actor_a,
            addressed_to=None,
            kind="note_created",
            object_id="note_own",
            summary="Собственная заметка не новость",
        )

        first = service.personal_brief(actor_id=actor_a, workspace_id=workspace)
        assert [item["object_id"] for item in first["personal"]] == ["q_personal_1"]
        assert first["personal_unread_count"] == 1
        assert first["general_available"] is True
        assert first["general_count"] == 1
        assert first["general_preview"][0]["object_id"] == "note_general_1"

        service.mark_brief_seen(
            actor_id=actor_a,
            workspace_id=workspace,
            personal_through_id=first["personal_through_id"],
        )
        second = service.personal_brief(actor_id=actor_a, workspace_id=workspace)
        assert second["personal"] == []
        assert second["personal_unread_count"] == 0
        assert second["general_available"] is True

        service.set_general_news(
            actor_id=actor_a,
            workspace_id=workspace,
            enabled=False,
        )
        quiet = service.personal_brief(actor_id=actor_a, workspace_id=workspace)
        assert quiet["general_news_enabled"] is False
        assert quiet["general_available"] is False
        assert quiet["general_count"] == 0
        assert quiet["general_preview"] == []

        # Quiet mode does not suppress newly addressed personal work.
        add_event(
            store,
            workspace=workspace,
            project=project,
            actor=actor_b,
            addressed_to=actor_a,
            kind="question_addressed",
            object_id="q_personal_2",
            summary="Ещё один личный вопрос",
        )
        quiet_with_personal = service.personal_brief(
            actor_id=actor_a,
            workspace_id=workspace,
        )
        assert [item["object_id"] for item in quiet_with_personal["personal"]] == [
            "q_personal_2"
        ]
        assert quiet_with_personal["general_available"] is False
    finally:
        store.close()
