from pathlib import Path

from projects_hub.readiness import ReadinessService
from projects_hub.store import DurableStore


def setup(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    boot = store.ensure_dev_workspace("Readiness")
    actor = boot["actor"]["id"]
    workspace = boot["workspace"]["id"]
    project = next(item["id"] for item in boot["projects"] if item["name"] == "Projects Hub")
    service = ReadinessService(store)
    return store, actor, workspace, project, service


def test_podcast_event_readiness_and_follow_up_are_durable_and_idempotent(tmp_path: Path):
    store, actor, workspace, project, service = setup(tmp_path)
    try:
        event = service.record_calendar_event(
            actor_id=actor,
            workspace_id=workspace,
            project_id=project,
            command_id="cmd-calendar-1",
            title="Запись подкаста",
            starts_at="2026-10-03T18:00:00+02:00",
            event_type="podcast",
            device_event_id="42",
        )
        assert event["event_type"] == "podcast"
        assert event["ready"] is False
        assert event["incomplete_count"] == 4
        assert [item["key"] for item in event["checklist"]] == [
            "questions", "intro", "appearance", "materials"
        ]

        same = service.record_calendar_event(
            actor_id=actor,
            workspace_id=workspace,
            project_id=project,
            command_id="cmd-calendar-1",
            title="Запись подкаста",
            starts_at="2026-10-03T18:00:00+02:00",
            event_type="podcast",
            device_event_id="42",
        )
        assert same["id"] == event["id"]
        assert len(service.list_event_cards(
            actor_id=actor,
            workspace_id=workspace,
        )) == 1

        for key in ("questions", "intro", "appearance", "materials"):
            event = service.set_readiness(
                actor_id=actor,
                workspace_id=workspace,
                event_id=event["id"],
                checklist_key=key,
                done=True,
            )
        assert event["ready"] is True
        assert event["incomplete_count"] == 0

        task = service.create_follow_up_task(
            actor_id=actor,
            workspace_id=workspace,
            project_id=project,
            event_id=event["id"],
            command_id="cmd-task-1",
            title="Подготовить запасное вступление",
            description="Короткий вариант на 20 секунд",
            deadline="2026-10-03T15:00:00+02:00",
        )
        repeated = service.create_follow_up_task(
            actor_id=actor,
            workspace_id=workspace,
            project_id=project,
            event_id=event["id"],
            command_id="cmd-task-1",
            title="Подготовить запасное вступление",
        )
        assert repeated["id"] == task["id"]
        assert task["state"] == "proposed"

        accepted = service.set_task_state(
            actor_id=actor,
            workspace_id=workspace,
            task_id=task["id"],
            state="accepted",
        )
        assert accepted["state"] == "accepted"
        done = service.set_task_state(
            actor_id=actor,
            workspace_id=workspace,
            task_id=task["id"],
            state="done",
        )
        assert done["state"] == "done"
    finally:
        store.close()
