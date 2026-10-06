from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import projects_hub.development as development_module
from projects_hub.devcoveer_client import DevCoveerError
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
        access: str = "write",
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
                    "access": access,
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


class InterruptedStageDevCoveer(FakeDevCoveer):
    def __init__(
        self,
        stage: str,
        *,
        resume_success: bool = False,
        cwd: Path | None = None,
    ) -> None:
        super().__init__()
        self.interrupted_stage = stage
        self.resume_success = resume_success
        self.cwd = cwd
        self._quality_start_count = 0
        self._recovery_quality_task = "dvt_" + "r" * 32

    @staticmethod
    def _interrupted(cwd: Path | None = None):
        task = {"status": "interrupted"}
        if cwd is not None:
            task["cwd"] = str(cwd)
        return {
            "status": "interrupted",
            "executionStatus": "interrupted",
            "progressPhase": "cancelled",
            "latestTurn": {
                "status": "interrupted",
                "progressPhase": "cancelled",
                "finalResponse": "",
            },
            "task": task,
        }

    async def start_codex_task(
        self,
        *,
        project: str,
        prompt: str,
        model: str,
        reasoning_effort: str,
        access: str = "write",
    ):
        if model == "gpt-6-astra":
            self._quality_start_count += 1
            if self._quality_start_count >= 2:
                self.calls.append(
                    (
                        "start",
                        {
                            "project": project,
                            "prompt": prompt,
                            "model": model,
                            "reasoning_effort": reasoning_effort,
                            "access": access,
                            "task": self._recovery_quality_task,
                        },
                    )
                )
                return {
                    "status": "running",
                    "taskId": self._recovery_quality_task,
                    "provider": "codex",
                    "model": model,
                }
        return await super().start_codex_task(
            project=project,
            prompt=prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            access=access,
        )

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
        if prompt.startswith("Resume the same already-authorized"):
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
            if self.resume_success:
                return {"status": "running", "taskId": task_id}
            raise DevCoveerError("resume failed")
        return await super().continue_codex_task(
            task_id,
            project=project,
            prompt=prompt,
            access=access,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    async def read_task(
        self,
        task_id: str,
        *,
        project: str | None = None,
        detail: str = "summary",
    ):
        self.calls.append(
            ("read", {"task": task_id, "project": project, "detail": detail})
        )
        if task_id == self._recovery_quality_task:
            return self._completed(
                "Recovery review passed.\nREVIEW_VERDICT: ACCEPTED",
                40,
            )
        if task_id == self._quality_task:
            self._quality_reads += 1
            if self.interrupted_stage == "design" and self._quality_reads == 1:
                return self._interrupted(self.cwd)
            if self._quality_reads == 1:
                return self._completed(
                    "Design complete. Spec: docs/prompts/owner-development-test.md",
                    100,
                )
            if self.interrupted_stage == "review" and self._quality_reads == 2:
                return self._interrupted(self.cwd)
            return self._completed(
                "Review passed.\nREVIEW_VERDICT: ACCEPTED",
                50,
            )

        self._implementation_reads += 1
        if (
            self.interrupted_stage == "implementation"
            and self._implementation_reads == 1
        ):
            return self._interrupted(self.cwd)
        return self._completed("Implemented and tested; ready for review.", 200)


class SlowStartDevCoveer(FakeDevCoveer):
    def __init__(self) -> None:
        super().__init__()
        self.start_entered = asyncio.Event()
        self.release_start = asyncio.Event()

    async def start_codex_task(
        self,
        *,
        project: str,
        prompt: str,
        model: str,
        reasoning_effort: str,
    ):
        if model == "gpt-6-astra":
            self.start_entered.set()
            await self.release_start.wait()
        return await super().start_codex_task(
            project=project,
            prompt=prompt,
            model=model,
            reasoning_effort=reasoning_effort,
        )


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
async def test_owner_runs_two_thread_quality_pipeline_to_delivery(tmp_path: Path):
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
        run_id = execution["execution"]["id"]
        assert execution["execution"]["phase"] == "designing"
        assert execution["execution"]["stages"][0]["stage"] == "design"

        starts = [arguments for name, arguments in fake.calls if name == "start"]
        assert len(starts) == 1
        assert starts[0]["model"] == "gpt-6-astra"
        assert starts[0]["reasoning_effort"] == "high"

        await service.advance_active_once()
        implementation = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=run_id,
            sync=True,
        )
        assert implementation["execution"]["phase"] == "implementing"
        starts = [arguments for name, arguments in fake.calls if name == "start"]
        assert len(starts) == 2
        assert starts[1]["model"] == "gpt-6.1-sol"
        assert starts[1]["reasoning_effort"] == "medium"

        await service.advance_active_once()
        review = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=run_id,
            sync=True,
        )
        assert review["execution"]["phase"] == "reviewing"
        quality_continuations = [
            arguments for name, arguments in fake.calls
            if name == "continue" and arguments["task"] == fake._quality_task
        ]
        assert len(quality_continuations) == 1
        assert quality_continuations[0]["access"] == "read"

        await service.advance_active_once()
        delivery = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=run_id,
            sync=True,
        )
        assert delivery["execution"]["phase"] == "delivering"
        implementation_continuations = [
            arguments for name, arguments in fake.calls
            if name == "continue" and arguments["task"] == fake._implementation_task
        ]
        assert len(implementation_continuations) == 1

        await service.advance_active_once()
        done = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=run_id,
            sync=True,
        )
        assert done["execution"]["status"] == "completed"
        assert done["execution"]["phase"] == "ready"
        assert done["execution"]["update_check_recommended"] is True
        assert [stage["stage"] for stage in done["execution"]["stages"]] == [
            "design", "implementation", "review", "delivery"
        ]
        assert done["execution"]["token_usage_by_model"]["gpt-6-astra"]["totalTokens"] == 150
        assert done["execution"]["token_usage_by_model"]["gpt-6.1-sol"]["totalTokens"] == 230

        current = service.list_backlog(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            project_id=project["id"],
        )
        assert {item["state"] for item in current} == {"done"}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_running_implementation_exposes_testing_phase(tmp_path: Path):
    store, readiness, boot, project = setup(tmp_path)
    fake = FakeDevCoveer(hold_implementation=True)
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        task = create_backlog(service, boot, project)[0]
        execution = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[task["id"]],
        )
        run_id = execution["execution"]["id"]

        await service.advance_active_once()
        await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=run_id,
            sync=True,
        )
        await service.advance_active_once()
        testing = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=run_id,
            sync=True,
        )
        assert testing["execution"]["status"] == "running"
        assert testing["execution"]["phase"] == "testing"
        assert testing["execution"]["phase_detail"] == "Идут тесты"
        assert testing["execution"]["stages"][-1]["stage"] == "implementation"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_review_reuses_two_threads_and_reworks_before_delivery(tmp_path: Path):
    store, readiness, boot, project = setup(tmp_path)
    fake = FakeDevCoveer(rework_once=True)
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        task = create_backlog(service, boot, project)[0]
        execution = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[task["id"]],
        )
        run_id = execution["execution"]["id"]

        phases = []
        for _ in range(6):
            await service.advance_active_once()
            current = await service.status(
                actor_id=boot["actor"]["id"],
                workspace_id=boot["workspace"]["id"],
                execution_id=run_id,
                sync=True,
            )
            phases.append(current["execution"]["phase"])

        assert phases == [
            "implementing",
            "reviewing",
            "reworking",
            "reviewing",
            "delivering",
            "ready",
        ]
        final = current["execution"]
        assert final["status"] == "completed"
        assert [stage["stage"] for stage in final["stages"]] == [
            "design", "implementation", "review", "rework", "review", "delivery"
        ]
        starts = [args for name, args in fake.calls if name == "start"]
        assert len(starts) == 2
        assert {item["task"] for name, item in fake.calls if name == "continue"} == {
            fake._quality_task,
            fake._implementation_task,
        }
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



@pytest.mark.asyncio
async def test_status_read_does_not_advance_running_execution(tmp_path: Path):
    store, readiness, boot, project = setup(tmp_path)
    fake = FakeDevCoveer()
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        task = create_backlog(service, boot, project)[0]
        started = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[task["id"]],
        )
        execution_id = started["execution"]["id"]

        before_reads = len([1 for name, _ in fake.calls if name == "read"])
        first = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
            sync=True,
        )
        second = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
            sync=True,
        )

        assert first["execution"]["phase"] == "designing"
        assert second["execution"]["phase"] == "designing"
        assert len([1 for name, _ in fake.calls if name == "read"]) == before_reads
        assert len([1 for name, _ in fake.calls if name == "start"]) == 1

        await service.advance_active_once()
        after = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
        )
        assert after["execution"]["phase"] == "implementing"
        assert len([1 for name, _ in fake.calls if name == "start"]) == 2
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_status_poll_during_start_never_creates_missing_stage(tmp_path: Path):
    store, readiness, boot, project = setup(tmp_path)
    fake = SlowStartDevCoveer()
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        task = create_backlog(service, boot, project)[0]
        launch = asyncio.create_task(
            service.start(
                actor_id=boot["actor"]["id"],
                workspace_id=boot["workspace"]["id"],
                task_ids=[task["id"]],
            )
        )
        await asyncio.wait_for(fake.start_entered.wait(), timeout=1)

        with store._lock:
            row = store.db.execute(
                "SELECT id FROM task_executions ORDER BY created_at_ms DESC LIMIT 1"
            ).fetchone()
        assert row is not None
        snapshot = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=str(row["id"]),
            sync=True,
        )
        assert snapshot["execution"]["status"] == "starting"
        assert snapshot["execution"]["error_code"] is None
        assert snapshot["execution"]["finished_at_ms"] is None
        assert snapshot["execution"]["stages"] == []

        fake.release_start.set()
        started = await asyncio.wait_for(launch, timeout=1)
        execution = started["execution"]
        assert execution["status"] == "running"
        assert execution["phase"] == "designing"
        assert execution["error_code"] is None
        assert execution["finished_at_ms"] is None
        assert len(execution["stages"]) == 1
    finally:
        fake.release_start.set()
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_background_worker_progresses_without_status_reads(tmp_path: Path):
    store, readiness, boot, project = setup(tmp_path)
    fake = FakeDevCoveer()
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        service._background_interval_seconds = 0.01
        task = create_backlog(service, boot, project)[0]
        started = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[task["id"]],
        )
        execution_id = started["execution"]["id"]
        await service.start_background()

        deadline = asyncio.get_running_loop().time() + 2.0
        while True:
            snapshot = await service.status(
                actor_id=boot["actor"]["id"],
                workspace_id=boot["workspace"]["id"],
                execution_id=execution_id,
            )
            if snapshot["execution"]["status"] == "completed":
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(snapshot)
            await asyncio.sleep(0.01)

        assert snapshot["execution"]["phase"] == "ready"
        assert [
            stage["stage"] for stage in snapshot["execution"]["stages"]
        ] == ["design", "implementation", "review", "delivery"]
        assert service.list_backlog(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            project_id=project["id"],
        )[0]["state"] == "done"
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_backlog_overview_keeps_running_execution_visible_after_reopen(
    tmp_path: Path,
):
    store, readiness, boot, project = setup(tmp_path)
    fake = FakeDevCoveer()
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        task = create_backlog(service, boot, project)[0]
        started = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[task["id"]],
        )

        overview = service.backlog_overview(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            project_id=project["id"],
        )

        assert overview["tasks"][0]["state"] == "accepted"
        assert overview["latest_execution"]["id"] == started["execution"]["id"]
        assert overview["latest_execution"]["status"] == "running"
        assert overview["latest_execution"]["phase"] == "designing"
        assert "accepted" in overview["backlog_state_semantics"]
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_self_development_retargets_design_before_implementation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, readiness, boot, project = setup(tmp_path)
    fake = FakeDevCoveer()
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        task = create_backlog(service, boot, project)[0]
        started = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[task["id"]],
        )
        execution_id = started["execution"]["id"]
        assert started["execution"]["project_hint"] == "projects-hub"

        monkeypatch.setenv(
            "PROJECTS_HUB_SELF_REPOSITORY",
            "onedayonemasterpiece/projects-hub",
        )
        monkeypatch.setenv(
            "PROJECTS_HUB_SELF_DEVCOVEER_PROJECT",
            "projects-hub-owner",
        )

        await service.advance_active_once()
        recovered = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
        )

        assert recovered["execution"]["project_hint"] == "projects-hub-owner"
        assert recovered["execution"]["status"] == "running"
        assert recovered["execution"]["phase"] == "designing"
        assert recovered["execution"]["error_code"] is None
        assert recovered["execution"]["finished_at_ms"] is None
        assert [stage["status"] for stage in recovered["execution"]["stages"]] == [
            "superseded",
            "running",
        ]
        starts = [arguments for name, arguments in fake.calls if name == "start"]
        assert [item["project"] for item in starts] == [
            "projects-hub",
            "projects-hub-owner",
        ]
        assert all(item["model"] == "gpt-6-astra" for item in starts)
    finally:
        await service.close()
        store.close()



@pytest.mark.asyncio
async def test_interrupted_design_uses_verified_brief_readback_without_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    project_root = tmp_path / "projects"
    cwd = project_root / "projects-hub-owner"
    cwd.mkdir(parents=True)
    monkeypatch.setattr(development_module, "PROJECTS_ROOT", project_root.resolve())

    store, readiness, boot, project = setup(tmp_path)
    fake = InterruptedStageDevCoveer("design", cwd=cwd)
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        task = create_backlog(service, boot, project)[0]
        started = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[task["id"]],
        )
        execution = started["execution"]
        spec_path = cwd / execution["spec_path"]
        spec_path.parent.mkdir(parents=True)
        spec_path.write_text(
            (
                "# Implementation brief\n\n"
                f"Execution: {execution['id']}\n"
                f"Task: {task['id']}\n\n"
                "## Definition of Done\n"
                + ("Verified bounded design evidence.\n" * 80)
            ),
            encoding="utf-8",
        )

        await service.advance_active_once()
        current = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution["id"],
        )

        assert current["execution"]["phase"] == "implementing"
        assert current["execution"]["status"] == "running"
        assert current["execution"]["stages"][0]["status"] == "completed"
        starts = [args for name, args in fake.calls if name == "start"]
        assert len(starts) == 2
        assert [entry["model"] for entry in starts] == [
            "gpt-6-astra",
            "gpt-6.1-sol",
        ]
        assert not any(
            name == "continue"
            and args["prompt"].startswith("Resume the same already-authorized")
            for name, args in fake.calls
        )
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_interrupted_review_resumes_same_quality_thread_once(tmp_path: Path):
    store, readiness, boot, project = setup(tmp_path)
    fake = InterruptedStageDevCoveer("review", resume_success=True)
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        task = create_backlog(service, boot, project)[0]
        started = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[task["id"]],
        )
        execution_id = started["execution"]["id"]

        await service.advance_active_once()
        await service.advance_active_once()
        await service.advance_active_once()
        current = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
        )

        assert current["execution"]["status"] == "running"
        assert current["execution"]["phase"] == "reviewing"
        review = current["execution"]["stages"][-1]
        assert review["stage"] == "review"
        assert review["status"] == "running"
        assert review["recovery_attempts"] == 1
        quality_starts = [
            args
            for name, args in fake.calls
            if name == "start" and args["model"] == "gpt-6-astra"
        ]
        assert len(quality_starts) == 1
        resume_calls = [
            args
            for name, args in fake.calls
            if name == "continue"
            and args["prompt"].startswith("Resume the same already-authorized")
        ]
        assert len(resume_calls) == 1
        assert resume_calls[0]["task"] == fake._quality_task
        assert resume_calls[0]["access"] == "read"
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_interrupted_review_falls_back_to_one_read_only_review(tmp_path: Path):
    store, readiness, boot, project = setup(tmp_path)
    fake = InterruptedStageDevCoveer("review", resume_success=False)
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        task = create_backlog(service, boot, project)[0]
        started = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[task["id"]],
        )
        execution_id = started["execution"]["id"]

        await service.advance_active_once()
        await service.advance_active_once()
        await service.advance_active_once()
        current = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
        )

        assert current["execution"]["status"] == "running"
        assert current["execution"]["phase"] == "reviewing"
        review_stages = [
            stage
            for stage in current["execution"]["stages"]
            if stage["stage"] == "review"
        ]
        assert [stage["status"] for stage in review_stages] == [
            "superseded",
            "running",
        ]
        starts = [args for name, args in fake.calls if name == "start"]
        assert starts[-1]["task"] == fake._recovery_quality_task
        assert starts[-1]["model"] == "gpt-6-astra"
        assert starts[-1]["access"] == "read"
        assert current["execution"]["quality_task_id"] == fake._recovery_quality_task
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_interrupted_implementation_never_starts_second_write_task(tmp_path: Path):
    store, readiness, boot, project = setup(tmp_path)
    fake = InterruptedStageDevCoveer("implementation", resume_success=False)
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        task = create_backlog(service, boot, project)[0]
        started = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[task["id"]],
        )
        execution_id = started["execution"]["id"]

        await service.advance_active_once()
        starts_before = len([1 for name, _ in fake.calls if name == "start"])
        await service.advance_active_once()
        current = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
        )

        assert current["execution"]["status"] == "blocked"
        assert current["execution"]["phase"] == "needs_owner"
        assert (
            current["execution"]["error_code"]
            == "DEVELOPMENT_STAGE_INTERRUPTED"
        )
        starts_after = len([1 for name, _ in fake.calls if name == "start"])
        assert starts_after == starts_before
        resume_calls = [
            args
            for name, args in fake.calls
            if name == "continue"
            and args["prompt"].startswith("Resume the same already-authorized")
        ]
        assert len(resume_calls) == 1
        assert resume_calls[0]["task"] == fake._implementation_task
        assert resume_calls[0]["access"] == "write"
    finally:
        await service.close()
        store.close()



@pytest.mark.asyncio
async def test_old_review_limit_block_auto_resumes_same_write_thread(tmp_path: Path):
    store, readiness, boot, project = setup(tmp_path)
    fake = FakeDevCoveer()
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        task = create_backlog(service, boot, project)[0]
        started = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[task["id"]],
        )
        execution_id = started["execution"]["id"]

        await service.advance_active_once()
        await service.advance_active_once()

        review = service._active_stage(execution_id)
        assert review is not None
        assert review["stage"] == "review"
        service._finish_stage(
            stage_id=review["id"],
            status="completed",
            summary="Two concrete defects remain.",
            review_verdict="rework_required",
        )
        with store._lock:
            store.db.execute(
                "UPDATE task_execution_stages SET cycle=2 WHERE id=?",
                (review["id"],),
            )
            store.db.execute(
                """UPDATE task_executions
                   SET status='blocked',phase='needs_owner',
                       phase_detail='Legacy two-cycle cap',
                       review_cycle=2,result_summary=?,
                       error_code='REVIEW_REWORK_LIMIT',
                       finished_at_ms=123,updated_at_ms=123
                   WHERE id=?""",
                ("Two concrete defects remain.", execution_id),
            )

        starts_before = len([1 for name, _ in fake.calls if name == "start"])
        continues_before = len([1 for name, _ in fake.calls if name == "continue"])
        advanced = await service.advance_active_once()
        current = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
        )

        assert advanced == 1
        assert current["execution"]["status"] == "running"
        assert current["execution"]["phase"] == "reworking"
        assert current["execution"]["review_cycle"] == 3
        assert current["execution"]["error_code"] is None
        assert current["execution"]["finished_at_ms"] is None
        assert len([1 for name, _ in fake.calls if name == "start"]) == starts_before

        continuation_calls = [
            args for name, args in fake.calls if name == "continue"
        ]
        assert len(continuation_calls) == continues_before + 1
        assert continuation_calls[-1]["task"] == fake._implementation_task
        assert continuation_calls[-1]["access"] == "write"
        assert "rework cycle 3" in continuation_calls[-1]["prompt"].lower()

        last_stage = current["execution"]["stages"][-1]
        assert last_stage["stage"] == "rework"
        assert last_stage["cycle"] == 3
        assert last_stage["status"] == "running"
        assert last_stage["devcoveer_task_id"] == fake._implementation_task
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_review_rework_limit_remains_bounded_at_new_maximum(tmp_path: Path):
    store, readiness, boot, project = setup(tmp_path)
    fake = FakeDevCoveer()
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        task = create_backlog(service, boot, project)[0]
        started = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[task["id"]],
        )
        execution_id = started["execution"]["id"]

        await service.advance_active_once()
        await service.advance_active_once()

        review = service._active_stage(execution_id)
        assert review is not None
        service._finish_stage(
            stage_id=review["id"],
            status="completed",
            summary="Still not accepted at bounded maximum.",
            review_verdict="rework_required",
        )
        max_cycle = development_module.MAX_REWORK_CYCLES
        with store._lock:
            store.db.execute(
                "UPDATE task_execution_stages SET cycle=? WHERE id=?",
                (max_cycle, review["id"]),
            )
            store.db.execute(
                """UPDATE task_executions
                   SET status='blocked',phase='needs_owner',
                       review_cycle=?,result_summary=?,
                       error_code='REVIEW_REWORK_LIMIT',
                       finished_at_ms=123,updated_at_ms=123
                   WHERE id=?""",
                (
                    max_cycle,
                    "Still not accepted at bounded maximum.",
                    execution_id,
                ),
            )

        continues_before = len([1 for name, _ in fake.calls if name == "continue"])
        advanced = await service.advance_active_once()
        current = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
        )

        assert advanced == 0
        assert current["execution"]["status"] == "blocked"
        assert current["execution"]["error_code"] == "REVIEW_REWORK_LIMIT"
        assert current["execution"]["review_cycle"] == max_cycle
        assert len([1 for name, _ in fake.calls if name == "continue"]) == continues_before
    finally:
        await service.close()
        store.close()



def test_terminal_native_status_overrides_stale_running_wrapper():
    stale_interrupted = {
        "status": "running",
        "executionStatus": "running",
        "latestTurn": {"status": "interrupted"},
        "task": {
            "status": "running",
            "runtimeStatus": {"type": "interrupted"},
        },
    }
    assert (
        DevelopmentService._task_status_from_result(stale_interrupted)
        == "interrupted"
    )

    stale_completed = {
        "status": "running",
        "latestTurn": {"status": "completed"},
        "task": {"runtimeStatus": {"type": "running"}},
    }
    assert (
        DevelopmentService._task_status_from_result(stale_completed)
        == "completed"
    )

    genuinely_running = {
        "status": "running",
        "executionStatus": "running",
        "latestTurn": {"status": "running"},
        "task": {"runtimeStatus": {"type": "running"}},
    }
    assert (
        DevelopmentService._task_status_from_result(genuinely_running)
        == "running"
    )



def test_delivery_prompt_requires_merged_main_before_production_deploy():
    prompt = DevelopmentService._delivery_prompt(
        "docs/prompts/owner-development-test.md"
    )
    lowered = prompt.lower()
    assert "never deploy a branch-only" in lowered
    assert "fresh origin/main history" in lowered
    assert "exact merged sha" in lowered
    assert "running service, static assets and release metadata" in lowered


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


@pytest.mark.asyncio
async def test_pending_unknown_owner_resume_blocks_new_development_start(tmp_path: Path):
    store, readiness, boot, project = setup(tmp_path)
    fake = FakeDevCoveer()
    service = DevelopmentService(store, readiness, devcoveer=fake)
    try:
        tasks = create_backlog(service, boot, project)
        first = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[tasks[0]["id"]],
        )
        execution_id = first["execution"]["id"]
        now = 1_800_000_000_500
        with store._lock:
            store.db.execute(
                """UPDATE task_executions
                   SET status='blocked',phase='needs_owner',
                       error_code='REVIEW_VERDICT_MISSING',
                       phase_detail='Owner answer dispatched with unknown outcome',
                       finished_at_ms=?,updated_at_ms=?
                   WHERE id=?""",
                (now, now, execution_id),
            )
            store.db.execute(
                """UPDATE task_execution_stages
                   SET status='completed',finished_at_ms=?,updated_at_ms=?
                   WHERE execution_id=? AND status='running'""",
                (now, now, execution_id),
            )
            store.db.execute(
                """INSERT INTO development_owner_resumes(
                       execution_id,command_id,answer_sha256,answer_text,status,
                       provider_baseline_json,quota_remaining_percent,
                       created_at_ms,updated_at_ms)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    execution_id,
                    "owner.pending.0001",
                    "a" * 64,
                    "Owner answer with unknown provider receipt.",
                    "dispatch_unknown",
                    '{"turn_id":"turn-before"}',
                    80.0,
                    now,
                    now,
                ),
            )
        calls_before = list(fake.calls)
        with pytest.raises(StoreError) as exc:
            await service.start(
                actor_id=boot["actor"]["id"],
                workspace_id=boot["workspace"]["id"],
                task_ids=[tasks[1]["id"]],
            )
        assert exc.value.code == "DEVELOPMENT_EXECUTION_ACTIVE"
        # The guard fires before quota/model/provider admission or any new task.
        assert fake.calls == calls_before
    finally:
        await service.close()
        store.close()
