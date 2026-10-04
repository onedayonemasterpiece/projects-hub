from __future__ import annotations

import json
from pathlib import Path

import pytest

from projects_hub.development import DevelopmentService
from projects_hub.readiness import ReadinessService
from projects_hub.store import DurableStore, StoreError


class FakeDevelopmentService(DevelopmentService):
    def __init__(self, store, readiness, *, remaining=85.0, profile_available=False):
        self.remaining = remaining
        self.profile_available = profile_available
        self.calls = []
        super().__init__(store, readiness, command="/not-used")

    async def _call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "codex_status":
            return {
                "status": "available",
                "observed_at": "2026-10-04T08:00:00+00:00",
                "admission": {
                    "eligible": self.remaining > 10,
                    "effective_remaining_percent": self.remaining,
                    "reserve_percent": 10.0,
                    "reason": "quota_available" if self.remaining > 10 else "reserve_or_limit_reached",
                },
                "default_profile": {
                    "requested": "gpt-6.1-medium",
                    "model": "gpt-6.1-sol" if self.profile_available else None,
                    "reasoning_effort": "medium",
                    "catalog_available": self.profile_available,
                },
            }
        if name == "list_models":
            return {
                "models": [
                    {
                        "id": "gpt-6-astra",
                        "displayName": "GPT-6-Astra",
                        "reasoningEfforts": ["low", "medium", "high", "xhigh"],
                        "defaultReasoningEffort": "medium",
                        "availability": "live_catalog",
                    },
                    *([{
                        "id": "gpt-6.1-sol",
                        "displayName": "GPT-6.1-Sol",
                        "reasoningEfforts": ["medium", "high"],
                        "defaultReasoningEffort": "medium",
                        "availability": "live_catalog",
                    }] if self.profile_available else []),
                ]
            }
        if name == "start_task":
            return {
                "status": "running",
                "taskId": "dvt_" + "a" * 32,
                "provider": "codex",
                "model": arguments["model"],
            }
        if name == "read_task":
            return {
                "status": "completed",
                "content": "Implemented, tested and released.",
                "task": {"status": "completed"},
            }
        raise AssertionError(name)


def setup(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    boot = store.ensure_platform_owner("Owner")
    readiness = ReadinessService(store)
    owner = boot["actor"]["id"]
    workspace = boot["workspace"]["id"]
    project = next(item for item in boot["projects"] if item["name"] == "Projects Hub")

    store.upsert_github_installation(
        actor_id=owner,
        workspace_id=workspace,
        installation={
            "id": 77,
            "account": {"id": 501, "login": "owner", "type": "User"},
            "html_url": "https://github.com/settings/installations/77",
            "repository_selection": "selected",
            "permissions": {"metadata": "read", "contents": "write"},
            "suspended_at": None,
        },
    )
    store.sync_github_repositories(
        workspace_id=workspace,
        installation_id=77,
        repositories=[{
            "id": 101,
            "full_name": "onedayonemasterpiece/projects-hub",
            "default_branch": "main",
            "private": False,
        }],
    )
    store.bind_repository_connection(
        actor_id=owner,
        workspace_id=workspace,
        repository_id=101,
        project_id=project["id"],
        role="project_docs",
        access_mode="app_managed_write",
        allowed_paths=[],
    )
    first = readiness.create_follow_up_task(
        actor_id=owner,
        workspace_id=workspace,
        project_id=project["id"],
        event_id=None,
        command_id="backlog-one",
        title="Добавить быстрый полезный сценарий",
        description="Нужен проверяемый продуктовый результат.",
    )
    second = readiness.create_follow_up_task(
        actor_id=owner,
        workspace_id=workspace,
        project_id=project["id"],
        event_id=None,
        command_id="backlog-two",
        title="Покрыть сценарий тестом",
        description="Без параллельного backlog.",
    )
    return store, readiness, boot, [first, second]


@pytest.mark.asyncio
async def test_codex_status_reports_live_capacity_and_models(tmp_path: Path):
    store, readiness, boot, _tasks = setup(tmp_path)
    try:
        service = FakeDevelopmentService(store, readiness)
        result = await service.codex_status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
        )
        assert result["remaining_percent"] == 85
        assert result["eligible"] is True
        assert result["profile"]["catalog_available"] is False
        assert result["models"][0]["id"] == "gpt-6-astra"
        assert [name for name, _ in service.calls] == ["codex_status", "list_models"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_start_requires_explicit_model_when_owner_profile_is_unavailable(tmp_path: Path):
    store, readiness, boot, tasks = setup(tmp_path)
    try:
        service = FakeDevelopmentService(store, readiness)
        result = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[tasks[0]["id"]],
        )
        assert result["status"] == "model_selection_required"
        assert result["remaining_percent"] == 85
        assert result["models"][0]["id"] == "gpt-6-astra"
        assert not any(name == "start_task" for name, _ in service.calls)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_owner_can_run_multiple_existing_backlog_tasks_and_sync_status(tmp_path: Path):
    store, readiness, boot, tasks = setup(tmp_path)
    try:
        service = FakeDevelopmentService(store, readiness)
        execution = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[tasks[0]["id"], tasks[1]["id"]],
            model="gpt-6-astra",
            reasoning_effort="medium",
        )
        assert execution["execution"]["status"] == "running"
        assert execution["execution"]["task_ids"] == [tasks[0]["id"], tasks[1]["id"]]
        start = next(arguments for name, arguments in service.calls if name == "start_task")
        assert start["provider"] == "codex"
        assert start["model"] == "gpt-6-astra"
        assert start["reasoning_effort"] == "medium"
        assert "Approved" not in start["prompt"] or "backlog" in start["prompt"].lower()

        synced = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution["execution"]["id"],
            sync=True,
        )
        assert synced["execution"]["status"] == "completed"
        assert synced["execution"]["update_check_recommended"] is True
        assert "released" in synced["execution"]["result_summary"]

        current = readiness.list_tasks(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            project_id=tasks[0]["project_id"],
        )
        assert {item["state"] for item in current} == {"accepted"}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_codex_reserve_blocks_start(tmp_path: Path):
    store, readiness, boot, tasks = setup(tmp_path)
    try:
        service = FakeDevelopmentService(store, readiness, remaining=10.0)
        with pytest.raises(StoreError) as error:
            await service.start(
                actor_id=boot["actor"]["id"],
                workspace_id=boot["workspace"]["id"],
                task_ids=[tasks[0]["id"]],
                model="gpt-6-astra",
                reasoning_effort="medium",
            )
        assert error.value.code == "CODEX_CAPACITY_RESERVED"
        assert not any(name == "start_task" for name, _ in service.calls)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_non_platform_owner_cannot_read_or_start_development(tmp_path: Path):
    store, readiness, boot, tasks = setup(tmp_path)
    try:
        owner = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        member = "usr_member"
        store.db.execute(
            "INSERT INTO actors(id,display_name,created_at_ms) VALUES(?,?,?)",
            (member, "Member", 1),
        )
        store.db.execute(
            "INSERT INTO memberships(actor_id,workspace_id,role) VALUES(?,?,?)",
            (member, workspace, "owner"),
        )
        service = FakeDevelopmentService(store, readiness)
        with pytest.raises(StoreError) as error:
            await service.codex_status(actor_id=member, workspace_id=workspace)
        assert error.value.code == "FORBIDDEN"
        assert owner != member
    finally:
        store.close()
