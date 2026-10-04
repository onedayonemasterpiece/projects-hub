from __future__ import annotations

from pathlib import Path

import pytest

from projects_hub.development import DevelopmentService
from projects_hub.live_adapter import _functions
from projects_hub.readiness import ReadinessService
from projects_hub.store import DurableStore, StoreError


class FakeDevCoveer:
    def __init__(
        self,
        *,
        remaining: float = 85.0,
        profile_available: bool = True,
        hold_implementation: bool = False,
        rework_once: bool = False,
    ) -> None:
        self.remaining = remaining
        self.profile_available = profile_available
        self.hold_implementation = hold_implementation
        self.rework_once = rework_once
        self.calls: list[tuple[str, dict]] = []
        self.closed = False
        self._quality_task = "dvt_" + "q" * 32
        self._implementation_task = "dvt_" + "i" * 32
        self._quality_reads = 0
        self._implementation_reads = 0

    async def status(self):
        self.calls.append(("status", {}))
        models = [
            {
                "id": "gpt-6-astra",
                "displayName": "GPT-6-Astra",
                "reasoningEfforts": ["low", "medium", "high", "xhigh"],
                "defaultReasoningEffort": "medium",
                "availability": "live_catalog",
            }
        ]
        if self.profile_available:
            models.insert(
                0,
                {
                    "id": "gpt-6.1-sol",
                    "displayName": "GPT-6.1-Sol",
                    "reasoningEfforts": ["medium", "high"],
                    "defaultReasoningEffort": "medium",
                    "availability": "live_catalog",
                },
            )
        return {
            "quota": {
                "status": "available",
                "observed_at": "2026-10-04T13:00:00+00:00",
                "admission": {
                    "eligible": self.remaining > 10,
                    "effective_remaining_percent": self.remaining,
                    "reserve_percent": 10.0,
                    "reason": (
                        "quota_available"
                        if self.remaining > 10
                        else "reserve_or_limit_reached"
                    ),
                },
                "default_profile": {
                    "requested": "gpt-6.1-medium",
                    "model": "gpt-6.1-sol" if self.profile_available else None,
                    "reasoning_effort": "medium",
                    "catalog_available": self.profile_available,
                },
            },
            "models": models,
        }

    async def start_codex_task(
        self,
        *,
        project: str,
        prompt: str,
        model: str,
        reasoning_effort: str,
    ):
        task_id = self._quality_task if model == "gpt-6-astra" else self._implementation_task
        self.calls.append(
            (
                "start",
                {
                    "project": project,
                    "prompt": prompt,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    "task": task_id,
                },
            )
        )
        return {"status": "running", "taskId": task_id, "provider": "codex", "model": model}

    async def continue_codex_task(
        self,
        task_id: str,
        *,
        project: str,
        prompt: str,
        access: str = "write",
        model: str | None = None,
        reasoning_effort: str | None = None,
    ):
        self.calls.append(
            (
                "continue",
                {
                    "task": task_id,
                    "project": project,
                    "prompt": prompt,
                    "access": access,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                },
            )
        )
        return {"status": "running", "taskId": task_id}

    @staticmethod
    def _completed(text: str, total: int):
        return {
            "status": "completed",
            "executionStatus": "completed",
            "progressPhase": "ready",
            "finalResponse": text,
            "latestTurn": {"finalResponse": text},
            "tokenUsage": {
                "inputTokens": total // 2,
                "cachedInputTokens": 0,
                "outputTokens": total // 2,
                "reasoningOutputTokens": 0,
                "totalTokens": total,
            },
            "task": {"status": "completed"},
        }

    async def read_task(
        self,
        task_id: str,
        *,
        project: str | None = None,
        detail: str = "summary",
    ):
        self.calls.append(("read", {"task": task_id, "project": project, "detail": detail}))
        if task_id == self._quality_task:
            self._quality_reads += 1
            if self._quality_reads == 1:
                return self._completed(
                    "Design complete. Spec: docs/prompts/owner-development-test.md",
                    100,
                )
            if self.rework_once and self._quality_reads == 2:
                return self._completed(
                    "Material edge case still fails.\nREVIEW_VERDICT: REWORK_REQUIRED",
                    60,
                )
            return self._completed(
                "Review passed.\nREVIEW_VERDICT: ACCEPTED",
                50,
            )

        self._implementation_reads += 1
        if self.hold_implementation and self._implementation_reads == 1:
            return {
                "status": "running",
                "executionStatus": "running",
                "progressPhase": "testing",
                "task": {"status": "running"},
            }
        if self._implementation_reads == 1:
            return self._completed("Implemented and tested; ready for review.", 200)
        if self.rework_once and self._implementation_reads == 2:
            return self._completed("Rework completed and retested.", 80)
        return self._completed("Merged, deployed and released.", 30)

    async def close(self):
        self.closed = True

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
    return store, readiness, boot, project


def create_backlog(service, boot, project):
    owner = boot["actor"]["id"]
    workspace = boot["workspace"]["id"]
    first = service.create_backlog_task(
        actor_id=owner,
        workspace_id=workspace,
        project_id=project["id"],
        task_key="backlog-one",
        title="Добавить быстрый полезный сценарий",
        description="Нужен проверяемый продуктовый результат.",
        acceptance_criteria=["Результат работает на телефоне"],
    )
    second = service.create_backlog_task(
        actor_id=owner,
        workspace_id=workspace,
        project_id=project["id"],
        task_key="backlog-two",
        title="Покрыть сценарий тестом",
        description="Без параллельного backlog.",
    )
    return [first, second]


@pytest.mark.asyncio
async def test_codex_status_reports_live_capacity_and_gpt61_profile(tmp_path: Path):
    store, readiness, boot, _project = setup(tmp_path)
    fake = FakeDevCoveer()
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        result = await service.codex_status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
        )
        assert result["remaining_percent"] == 85
        assert result["eligible"] is True
        assert result["profile"]["catalog_available"] is True
        assert result["profile"]["model"] == "gpt-6.1-sol"
        assert result["models"][0]["id"] == "gpt-6.1-sol"
        assert [name for name, _ in fake.calls] == ["status"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_start_fails_closed_when_owner_profile_is_unavailable(tmp_path: Path):
    store, readiness, boot, project = setup(tmp_path)
    fake = FakeDevCoveer(profile_available=False)
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        tasks = create_backlog(service, boot, project)
        result = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[tasks[0]["id"]],
        )
        assert result["status"] == "model_selection_required"
        assert result["remaining_percent"] == 85
        assert result["models"][0]["id"] == "gpt-6-astra"
        assert not any(name == "start" for name, _ in fake.calls)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_owner_runs_multiple_backlog_tasks_with_default_gpt61_and_completes(tmp_path: Path):
    store, readiness, boot, project = setup(tmp_path)
    fake = FakeDevCoveer()
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        tasks = create_backlog(service, boot, project)
        execution = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[tasks[0]["id"], tasks[1]["id"]],
        )
        assert execution["execution"]["status"] == "running"
        assert execution["execution"]["phase"] == "analysis"
        assert execution["execution"]["task_ids"] == [tasks[0]["id"], tasks[1]["id"]]

        started = next(arguments for name, arguments in fake.calls if name == "start")
        assert started["project"] == "projects-hub"
        assert started["model"] == "gpt-6.1-sol"
        assert started["reasoning_effort"] == "medium"
        assert "backlog" in started["prompt"].lower()

        synced = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution["execution"]["id"],
            sync=True,
        )
        assert synced["execution"]["status"] == "completed"
        assert synced["execution"]["phase"] == "ready"
        assert synced["execution"]["phase_detail"] == "Готово"
        assert synced["execution"]["update_check_recommended"] is True
        assert "released" in synced["execution"]["result_summary"]

        current = service.list_backlog(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            project_id=project["id"],
        )
        assert {item["state"] for item in current} == {"done"}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_running_execution_exposes_sanitized_testing_phase(tmp_path: Path):
    store, readiness, boot, project = setup(tmp_path)
    fake = FakeDevCoveer(progress_phase="testing", execution_status="running")
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        task = create_backlog(service, boot, project)[0]
        execution = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[task["id"]],
        )
        synced = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution["execution"]["id"],
            sync=True,
        )
        assert synced["execution"]["status"] == "running"
        assert synced["execution"]["phase"] == "testing"
        assert synced["execution"]["phase_detail"] == "Идут тесты"
        read = next(arguments for name, arguments in fake.calls if name == "read")
        assert read["detail"] == "summary"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_codex_reserve_blocks_start(tmp_path: Path):
    store, readiness, boot, project = setup(tmp_path)
    fake = FakeDevCoveer(remaining=10.0)
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        task = create_backlog(service, boot, project)[0]
        with pytest.raises(StoreError) as error:
            await service.start(
                actor_id=boot["actor"]["id"],
                workspace_id=boot["workspace"]["id"],
                task_ids=[task["id"]],
            )
        assert error.value.code == "CODEX_CAPACITY_RESERVED"
        assert not any(name == "start" for name, _ in fake.calls)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_non_platform_owner_cannot_read_or_start_development(tmp_path: Path):
    store, readiness, boot, project = setup(tmp_path)
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
        service = DevelopmentService(store, readiness, devcoveer=FakeDevCoveer())
        with pytest.raises(StoreError) as error:
            await service.codex_status(actor_id=member, workspace_id=workspace)
        assert error.value.code == "FORBIDDEN"
        assert owner != member
    finally:
        store.close()


def test_owner_development_tools_are_not_exposed_to_ordinary_users():
    ordinary = {item["name"] for item in _functions(owner_development=False)}
    owner = {item["name"] for item in _functions(owner_development=True)}
    assert "backlog_create" not in ordinary
    assert "development_codex_status" not in ordinary
    assert "development_execute_backlog" not in ordinary
    assert "development_execution_status" not in ordinary
    assert "backlog_list" not in ordinary
    assert {
        "backlog_create",
        "development_codex_status",
        "development_execute_backlog",
        "development_execution_status",
        "backlog_list",
    } <= owner
