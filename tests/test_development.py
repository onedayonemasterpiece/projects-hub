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
        self.candidate_head = "c" * 40
        self.candidate_branch = "chatgpt/test-owner-development"
        self.checkout_candidate = True
        self.local_candidate_available = True
        self.ci_pending_once = False
        self.ci_failure = False
        self._ci_reads = 0
        self.main_head = "d" * 40
        self.main_ci_failure = False
        self.pr_mergeable_state = "clean"
        self.deploy_job_id = "job_" + "1" * 24
        self.deploy_job_status = "succeeded"
        self.deploy_exit_code = 0

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

    async def list_codex_tasks(
        self,
        *,
        project: str,
        search: str | None = None,
        limit: int = 20,
    ):
        self.calls.append(
            (
                "list_tasks",
                {"project": project, "search": search, "limit": limit},
            )
        )
        return {"status": "ok", "tasks": []}

    async def direct_project_probe(
        self,
        *,
        project: str,
        operation: str,
        payload: dict | None = None,
    ):
        payload = dict(payload or {})
        self.calls.append(
            ("direct_probe", {"project": project, "operation": operation, "payload": payload})
        )
        if operation == "git_state":
            data = {
                "head_state": "branch",
                "branch": self.candidate_branch if self.checkout_candidate else "main",
                "head": self.candidate_head if self.checkout_candidate else self.main_head,
                "staged": [],
                "unstaged": [],
                "untracked": [],
                "conflicts": [],
                "clean": True,
            }
        elif operation == "branch_list":
            rows = []
            marker = str(payload.get("contains") or "")
            prefix = str(payload.get("prefix") or "")
            if (
                self.local_candidate_available
                and (not prefix or self.candidate_branch.startswith(prefix))
                and (not marker or marker in self.candidate_branch)
            ):
                rows.append(
                    {
                        "branch": self.candidate_branch,
                        "sha": self.candidate_head,
                        "committed_unix": 1791377302,
                        "subject": "candidate",
                        "current": self.checkout_candidate,
                    }
                )
            data = {
                "branches": rows,
                "current_branch": self.candidate_branch if self.checkout_candidate else "main",
                "complete": True,
                "truncated": False,
            }
        elif operation == "remote_head":
            data = {
                "remote": "origin",
                "branch": "main",
                "fresh_remote_sha": self.main_head,
                "local_tracking_sha": self.main_head,
            }
        elif operation == "github_status":
            if payload.get("branch") == "main":
                failure = self.main_ci_failure
                data = {
                    "repository": "onedayonemasterpiece/projects-hub",
                    "branch": {"name": "main", "head": self.main_head},
                    "checks": [
                        {"name": "backend", "status": "completed", "conclusion": "failure" if failure else "success"},
                        {"name": "pwa", "status": "completed", "conclusion": "failure" if failure else "success"},
                    ],
                    "workflow_runs": [
                        {"id": 777, "name": "Projects Hub contracts", "status": "completed", "conclusion": "failure" if failure else "success"}
                    ],
                }
            else:
                self._ci_reads += 1
                pending = self.ci_pending_once and self._ci_reads == 1
                failure = self.ci_failure
                checks = [
                    {"name": "backend", "status": "in_progress" if pending else "completed", "conclusion": None if pending else ("failure" if failure else "success")},
                    {"name": "pwa", "status": "in_progress" if pending else "completed", "conclusion": None if pending else ("failure" if failure else "success")},
                ]
                workflows = [
                    {"id": 123, "name": "Projects Hub contracts", "status": "in_progress" if pending else "completed", "conclusion": None if pending else ("failure" if failure else "success")}
                ]
                if payload.get("pr"):
                    data = {
                        "repository": "onedayonemasterpiece/projects-hub",
                        "pr": {
                            "number": int(payload["pr"]),
                            "state": "open",
                            "draft": False,
                            "head": self.candidate_head,
                            "head_ref": self.candidate_branch,
                            "base_ref": "main",
                            "mergeable_state": self.pr_mergeable_state,
                        },
                        "checks": checks,
                        "workflow_runs": workflows,
                    }
                else:
                    data = {
                        "repository": "onedayonemasterpiece/projects-hub",
                        "branch": {"name": self.candidate_branch, "head": self.candidate_head},
                        "checks": checks,
                        "workflow_runs": workflows,
                    }
        else:
            raise AssertionError(operation)
        return {"results": [{"status": "ok", "data": data}]}

    async def direct_project_action(
        self,
        *,
        project: str,
        operation: str,
        payload: dict | None = None,
    ):
        payload = dict(payload or {})
        args = {"project": project, "operation": operation, "payload": payload}
        self.calls.append(("direct_action", args))
        if operation == "git_fetch":
            return {"status": "ok", "action": operation, "repository": "onedayonemasterpiece/projects-hub", "remote": "origin", "branch": payload.get("branch"), "fetched_sha": self.main_head}
        if operation == "git_push_existing":
            return {"status": "ok", "action": operation, "branch": self.candidate_branch, "commit_sha": self.candidate_head}
        if operation == "github_pr_create":
            return {"status": "ok", "action": operation, "number": 120, "head_sha": self.candidate_head, "reused": True}
        if operation == "github_pr_merge":
            return {"status": "ok", "action": operation, "number": int(payload["pr"]), "head_sha": self.candidate_head, "merge_sha": self.main_head, "method": payload.get("method", "squash")}
        if operation == "run_tool":
            return {"status": "running", "action": "job_start", "job_id": self.deploy_job_id, "reused": False}
        if operation == "job_status":
            result = {"status": self.deploy_job_status, "action": "job_status", "job_id": self.deploy_job_id}
            if self.deploy_job_status == "succeeded":
                result["result"] = {"exit_code": self.deploy_exit_code}
            return result
        raise AssertionError(operation)

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
        self._implementation_start_count = 0
        self._recovery_quality_task = "dvt_" + "r" * 32
        self._recovery_implementation_task = "dvt_" + "w" * 32

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
        else:
            self._implementation_start_count += 1
            if self._implementation_start_count >= 2:
                self.calls.append(
                    (
                        "start",
                        {
                            "project": project,
                            "prompt": prompt,
                            "model": model,
                            "reasoning_effort": reasoning_effort,
                            "access": access,
                            "task": self._recovery_implementation_task,
                        },
                    )
                )
                return {
                    "status": "running",
                    "taskId": self._recovery_implementation_task,
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
        if task_id == self._recovery_implementation_task:
            return self._completed(
                "Recovered existing implementation branch and completed remaining fixes.",
                90,
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


class LostRecoveryDispatchDevCoveer(InterruptedStageDevCoveer):
    def __init__(self) -> None:
        super().__init__("implementation", resume_success=False)
        self.lost_marker: str | None = None
        self.lost_task_id = "dvt_" + "l" * 32
        self.recovery_start_calls = 0

    async def list_codex_tasks(
        self,
        *,
        project: str,
        search: str | None = None,
        limit: int = 20,
    ):
        self.calls.append(
            (
                "list_tasks",
                {"project": project, "search": search, "limit": limit},
            )
        )
        if self.lost_marker and search == self.lost_marker:
            return {
                "status": "ok",
                "tasks": [
                    {
                        "taskId": self.lost_task_id,
                        "backend": "codex",
                        "model": "gpt-6.1-sol",
                        "access": "write",
                        "name": "projects-hub-owner: " + self.lost_marker,
                    }
                ],
            }
        return {"status": "ok", "tasks": []}

    async def start_codex_task(
        self,
        *,
        project: str,
        prompt: str,
        model: str,
        reasoning_effort: str,
        access: str = "write",
    ):
        if model != "gpt-6-astra" and prompt.startswith("ODR-"):
            self.recovery_start_calls += 1
            self.lost_marker = prompt.splitlines()[0]
            raise DevCoveerError("response lost after accepted dispatch")
        return await super().start_codex_task(
            project=project,
            prompt=prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            access=access,
        )

    async def read_task(
        self,
        task_id: str,
        *,
        project: str | None = None,
        detail: str = "summary",
    ):
        if task_id == self.lost_task_id:
            self.calls.append(
                ("read", {"task": task_id, "project": project, "detail": detail})
            )
            return self._completed(
                "Recovered after lost dispatch receipt; ready for review.",
                90,
            )
        return await super().read_task(
            task_id,
            project=project,
            detail=detail,
        )


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



def test_development_target_prefers_external_owning_repo_over_notes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store, readiness, boot, project = setup(tmp_path)
    owner = boot["actor"]["id"]
    workspace = boot["workspace"]["id"]
    try:
        store.bind_repository_connection(
            actor_id=owner,
            workspace_id=workspace,
            repository_id=101,
            project_id=project["id"],
            role="external_owning_repo",
            access_mode="read_only",
            allowed_paths=[],
        )
        store.sync_github_repositories(
            workspace_id=workspace,
            installation_id=77,
            repositories=[
                {
                    "id": 101,
                    "full_name": "onedayonemasterpiece/projects-hub",
                    "default_branch": "main",
                    "private": False,
                },
                {
                    "id": 102,
                    "full_name": "onedayonemasterpiece/projects-hub-notes",
                    "default_branch": "main",
                    "private": True,
                },
            ],
        )
        store.bind_repository_connection(
            actor_id=owner,
            workspace_id=workspace,
            repository_id=102,
            project_id=project["id"],
            role="project_docs",
            access_mode="app_managed_write",
            allowed_paths=["docs/notes"],
        )
        monkeypatch.setenv(
            development_module.SELF_REPOSITORY_ENV,
            "onedayonemasterpiece/projects-hub",
        )
        monkeypatch.setenv(
            development_module.SELF_DEVCOVEER_PROJECT_ENV,
            "projects-hub-owner",
        )

        service = DevelopmentService(
            store,
            readiness,
            devcoveer=FakeDevCoveer(),
        )
        assert (
            service._project_hint(
                owner,
                workspace,
                project["id"],
                project["name"],
            )
            == "projects-hub-owner"
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_interrupted_recovery_reconciles_stale_notes_target_to_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store, readiness, boot, project = setup(tmp_path)
    owner = boot["actor"]["id"]
    workspace = boot["workspace"]["id"]
    fake = InterruptedStageDevCoveer("none", resume_success=False)
    try:
        store.bind_repository_connection(
            actor_id=owner,
            workspace_id=workspace,
            repository_id=101,
            project_id=project["id"],
            role="external_owning_repo",
            access_mode="read_only",
            allowed_paths=[],
        )
        store.sync_github_repositories(
            workspace_id=workspace,
            installation_id=77,
            repositories=[
                {
                    "id": 101,
                    "full_name": "onedayonemasterpiece/projects-hub",
                    "default_branch": "main",
                    "private": False,
                },
                {
                    "id": 102,
                    "full_name": "onedayonemasterpiece/projects-hub-notes",
                    "default_branch": "main",
                    "private": True,
                },
            ],
        )
        store.bind_repository_connection(
            actor_id=owner,
            workspace_id=workspace,
            repository_id=102,
            project_id=project["id"],
            role="project_docs",
            access_mode="app_managed_write",
            allowed_paths=["docs/notes"],
        )
        monkeypatch.setenv(
            development_module.SELF_REPOSITORY_ENV,
            "onedayonemasterpiece/projects-hub",
        )
        monkeypatch.setenv(
            development_module.SELF_DEVCOVEER_PROJECT_ENV,
            "projects-hub-owner",
        )

        service = DevelopmentService(store, readiness, devcoveer=fake)
        task = create_backlog(service, boot, project)[0]
        started = await service.start(
            actor_id=owner,
            workspace_id=workspace,
            task_ids=[task["id"]],
        )
        execution_id = started["execution"]["id"]
        await service.advance_active_once()

        stage = service._active_stage(execution_id)
        assert stage is not None and stage["stage"] == "implementation"
        service._finish_stage(
            stage_id=stage["id"],
            status="interrupted",
            summary="Legacy continuation landed in the notes checkout.",
        )
        with store._lock:
            store.db.execute(
                """UPDATE task_executions
                   SET project_hint='projects-hub-notes',
                       status='running',phase='recovering',
                       error_code='DEVELOPMENT_TARGET_CHANGED'
                   WHERE id=?""",
                (execution_id,),
            )

        await service.advance_active_once()
        current = await service.status(
            actor_id=owner,
            workspace_id=workspace,
            execution_id=execution_id,
        )

        assert current["execution"]["project_hint"] == "projects-hub-owner"
        assert current["execution"]["status"] == "running"
        assert current["execution"]["phase"] == "implementing"
        recovery_starts = [
            args
            for name, args in fake.calls
            if name == "start"
            and args["model"] == "gpt-6.1-sol"
            and args["task"] == fake._recovery_implementation_task
        ]
        assert len(recovery_starts) == 1
        assert recovery_starts[0]["project"] == "projects-hub-owner"
    finally:
        await service.close()
        store.close()


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
        assert delivery["execution"]["phase"] == "delivery_merge"
        implementation_continuations = [
            arguments for name, arguments in fake.calls
            if name == "continue" and arguments["task"] == fake._implementation_task
        ]
        assert implementation_continuations == []

        await service.advance_active_once()
        main_ci = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=run_id,
        )
        assert main_ci["execution"]["phase"] == "delivery_main_ci"
        assert main_ci["execution"]["delivery_main_sha"] == fake.main_head

        await service.advance_active_once()
        deploying = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=run_id,
        )
        assert deploying["execution"]["phase"] == "deploying"
        assert deploying["execution"]["delivery_job_id"] == fake.deploy_job_id

        await service.advance_active_once()
        done = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=run_id,
            sync=True,
        )
        assert done["execution"]["status"] == "completed"
        assert done["execution"]["phase"] == "ready"
        assert done["execution"]["delivery_main_sha"] == fake.main_head
        assert done["execution"]["delivery_job_id"] == fake.deploy_job_id
        assert done["execution"]["delivery_deployed_at_ms"] is not None
        assert done["execution"]["delivery_evidence"]["deployed_main_sha"] == fake.main_head
        assert done["execution"]["update_check_recommended"] is True
        assert [stage["stage"] for stage in done["execution"]["stages"]] == [
            "design", "implementation", "review", "delivery"
        ]
        assert done["execution"]["token_usage_by_model"]["gpt-6-astra"]["totalTokens"] == 150
        assert done["execution"]["token_usage_by_model"]["gpt-6.1-sol"]["totalTokens"] == 200
        assert "deterministic" not in done["execution"]["token_usage_by_model"]

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
        for _ in range(8):
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
            "delivery_merge",
            "delivery_main_ci",
            "deploying",
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
        assert started["execution"]["project_hint"] == "Projects Hub"

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
            "Projects Hub",
            "projects-hub-owner",
        ]
        assert all(item["model"] == "gpt-6-astra" for item in starts)
    finally:
        await service.close()
        store.close()



@pytest.mark.asyncio
async def test_completed_write_publishes_exact_candidate_and_waits_for_ci(
    tmp_path: Path,
):
    store, readiness, boot, project = setup(tmp_path)
    fake = FakeDevCoveer()
    fake.ci_pending_once = True
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        task = create_backlog(service, boot, project)[0]
        started = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[task["id"]],
        )
        execution_id = started["execution"]["id"]

        await service.advance_active_once()  # design -> implementation
        await service.advance_active_once()  # implementation completed -> validation pending

        waiting = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
        )
        assert waiting["execution"]["status"] == "running"
        assert waiting["execution"]["phase"] == "validating"
        assert waiting["execution"]["candidate_sha"] == fake.candidate_head
        assert waiting["execution"]["candidate_branch"] == fake.candidate_branch
        assert waiting["execution"]["candidate_pr"] == 120
        assert waiting["execution"]["candidate_evidence"]["checks"][0]["status"] == "in_progress"
        active = service._active_stage(execution_id)
        assert active is not None and active["stage"] == "implementation"

        push_calls = [
            args for name, args in fake.calls
            if name == "direct_action" and args["operation"] == "git_push_existing"
        ]
        pr_calls = [
            args for name, args in fake.calls
            if name == "direct_action" and args["operation"] == "github_pr_create"
        ]
        assert len(push_calls) == 1
        assert push_calls[0]["payload"]["expected_sha"] == fake.candidate_head
        assert push_calls[0]["payload"]["branch"] == fake.candidate_branch
        assert push_calls[0]["payload"]["request_key"].endswith(
            ":push:" + fake.candidate_head[:16]
        )
        assert len(pr_calls) == 1
        assert pr_calls[0]["payload"]["request_key"].endswith(
            ":pr:" + fake.candidate_head[:16]
        )

        await service.advance_active_once()  # CI terminal -> review
        reviewing = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
        )
        assert reviewing["execution"]["phase"] == "reviewing"
        push_calls = [
            args for name, args in fake.calls
            if name == "direct_action" and args["operation"] == "git_push_existing"
        ]
        assert len(push_calls) == 1

        review_calls = [
            args for name, args in fake.calls
            if name == "continue"
            and args["access"] == "read"
            and "Review cycle" in args["prompt"]
        ]
        assert review_calls
        prompt = review_calls[-1]["prompt"]
        assert fake.candidate_head in prompt
        assert '"backend"' in prompt
        assert '"status":"completed"' in prompt
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_legacy_evidence_loop_recovers_noncurrent_candidate_branch(
    tmp_path: Path,
):
    store, readiness, boot, project = setup(tmp_path)
    fake = FakeDevCoveer()
    fake.checkout_candidate = False
    fake.candidate_branch = "chatgpt/voice-theme-devrun-fe82f497-20261006"
    fake.candidate_head = "b" * 40
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        task = create_backlog(service, boot, project)[0]
        started = await service.start(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            task_ids=[task["id"]],
        )
        execution_id = started["execution"]["id"]
        fake.candidate_branch = (
            "chatgpt/voice-theme-"
            + service._candidate_branch_marker(execution_id)
            + "-20261006"
        )

        await service.advance_active_once()  # design -> implementation
        stage = service._active_stage(execution_id)
        assert stage is not None and stage["stage"] == "implementation"
        service._finish_stage(
            stage_id=stage["id"],
            status="completed",
            summary="Committed candidate exists locally but old validation evidence was unavailable.",
        )
        with store._lock:
            store.db.execute(
                """UPDATE task_executions
                   SET status='failed',phase='failed',
                       review_cycle=12,error_code='REVIEW_REWORK_LIMIT',
                       result_summary='Old evidence-only quality loop exhausted',
                       candidate_sha=NULL,candidate_branch=NULL,candidate_pr=NULL,
                       candidate_published_at_ms=NULL,candidate_evidence_json=NULL,
                       finished_at_ms=123,updated_at_ms=123
                   WHERE id=?""",
                (execution_id,),
            )

        advanced = await service.advance_active_once()
        current = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
        )

        assert advanced == 1
        assert current["execution"]["status"] == "running"
        assert current["execution"]["phase"] == "reviewing"
        assert current["execution"]["review_cycle"] == 0
        assert current["execution"]["error_code"] is None
        assert current["execution"]["candidate_sha"] == fake.candidate_head
        assert current["execution"]["candidate_branch"] == fake.candidate_branch
        assert current["execution"]["candidate_pr"] == 120
        assert (
            current["execution"]["candidate_evidence"]["candidate_source"]
            == "local_branch_ref"
        )

        branch_probes = [
            args
            for name, args in fake.calls
            if name == "direct_probe" and args["operation"] == "branch_list"
        ]
        assert len(branch_probes) == 1
        assert branch_probes[0]["payload"]["contains"] == "devrun-" + execution_id.removeprefix("devrun_")[:8]

        push_calls = [
            args
            for name, args in fake.calls
            if name == "direct_action" and args["operation"] == "git_push_existing"
        ]
        assert len(push_calls) == 1
        assert push_calls[0]["payload"]["branch"] == fake.candidate_branch
        assert push_calls[0]["payload"]["expected_sha"] == fake.candidate_head

        reviews = [
            stage
            for stage in current["execution"]["stages"]
            if stage["stage"] == "review" and stage["status"] == "running"
        ]
        assert reviews and reviews[-1]["cycle"] == 0
        review_starts = [
            args
            for name, args in fake.calls
            if name == "start"
            and args["model"] == "gpt-6-astra"
            and args["access"] == "read"
            and "legacy-validation-review" in args["prompt"]
        ]
        assert review_starts
    finally:
        await service.close()
        store.close()


def test_implementation_prompt_requires_stable_execution_branch_marker():
    prompt = DevelopmentService._implementation_prompt(
        "Projects Hub",
        "docs/prompts/test.md",
        "devrun_fe82f497d32546ceb7f2c8ac073af944",
    )
    assert "devrun-fe82f497" in prompt
    assert "chatgpt/ownerdev-devrun-fe82f497-short-topic" in prompt


@pytest.mark.asyncio
async def test_review_accepted_cannot_bypass_failed_deterministic_ci(tmp_path: Path):
    store, readiness, boot, project = setup(tmp_path)
    fake = FakeDevCoveer()
    fake.ci_failure = True
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
        reviewing = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
        )
        assert reviewing["execution"]["phase"] == "reviewing"
        assert reviewing["execution"]["candidate_evidence"]["ci_success"] is False

        await service.advance_active_once()
        after = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
        )
        assert after["execution"]["status"] == "running"
        assert after["execution"]["phase"] == "reworking"
        assert after["execution"]["review_cycle"] == 1
        assert not any(stage["stage"] == "delivery" for stage in after["execution"]["stages"])
        completed_review = [
            stage for stage in after["execution"]["stages"]
            if stage["stage"] == "review" and stage["status"] == "completed"
        ][0]
        assert completed_review["review_verdict"] == "rework_required"
        assert "Deterministic delivery gate rejected ACCEPTED verdict" in completed_review["summary"]
        assert '"conclusion":"failure"' in completed_review["summary"]
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
async def test_interrupted_implementation_starts_safe_continuation(tmp_path: Path):
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

        assert current["execution"]["status"] == "running"
        assert current["execution"]["phase"] == "implementing"
        assert current["execution"]["error_code"] is None
        assert (
            current["execution"]["implementation_task_id"]
            == fake._recovery_implementation_task
        )
        starts_after = len([1 for name, _ in fake.calls if name == "start"])
        assert starts_after == starts_before + 1

        implementation_stages = [
            stage
            for stage in current["execution"]["stages"]
            if stage["stage"] == "implementation"
        ]
        assert [stage["status"] for stage in implementation_stages] == [
            "superseded",
            "running",
        ]
        assert (
            implementation_stages[-1]["devcoveer_task_id"]
            == fake._recovery_implementation_task
        )

        resume_calls = [
            args
            for name, args in fake.calls
            if name == "continue"
            and args["prompt"].startswith("Resume the same already-authorized")
        ]
        assert len(resume_calls) == 1
        assert resume_calls[0]["task"] == fake._implementation_task
        assert resume_calls[0]["access"] == "write"

        recovery_start = [
            args
            for name, args in fake.calls
            if name == "start" and args["task"] == fake._recovery_implementation_task
        ][0]
        assert recovery_start["access"] == "write"
        assert "Do not recreate the implementation from scratch" in recovery_start["prompt"]

        await service.advance_active_once()
        after = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
        )
        assert after["execution"]["phase"] == "reviewing"
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_lost_recovery_dispatch_is_reconciled_without_duplicate_write(
    tmp_path: Path,
):
    store, readiness, boot, project = setup(tmp_path)
    fake = LostRecoveryDispatchDevCoveer()
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
        current = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
        )

        assert fake.recovery_start_calls == 1
        assert current["execution"]["status"] == "running"
        assert current["execution"]["phase"] == "implementing"
        assert current["execution"]["implementation_task_id"] == fake.lost_task_id

        await service.advance_active_once()
        assert fake.recovery_start_calls == 1
        after = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
        )
        assert after["execution"]["phase"] == "reviewing"
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_legacy_interrupted_block_is_unblocked_by_worker(tmp_path: Path):
    store, readiness, boot, project = setup(tmp_path)
    fake = InterruptedStageDevCoveer("none", resume_success=False)
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
        stage = service._active_stage(execution_id)
        assert stage is not None and stage["stage"] == "implementation"
        service._finish_stage(
            stage_id=stage["id"],
            status="interrupted",
            summary="Native write turn ended before review.",
        )
        with store._lock:
            store.db.execute(
                """UPDATE task_executions
                   SET status='blocked',phase='needs_owner',
                       error_code='DEVELOPMENT_STAGE_INTERRUPTED',
                       finished_at_ms=123,updated_at_ms=123
                   WHERE id=?""",
                (execution_id,),
            )

        advanced = await service.advance_active_once()
        current = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
        )

        assert advanced == 1
        assert current["execution"]["status"] == "running"
        assert current["execution"]["phase"] == "implementing"
        assert current["execution"]["error_code"] is None
        assert (
            current["execution"]["implementation_task_id"]
            == fake._recovery_implementation_task
        )
        assert current["execution"]["finished_at_ms"] is None
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_capacity_wait_retries_without_owner_action(tmp_path: Path):
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

        fake.remaining = 9.0
        await service.advance_active_once()
        waiting = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
        )
        assert waiting["execution"]["status"] == "running"
        assert waiting["execution"]["phase"] == "capacity_wait"
        assert waiting["execution"]["error_code"] == "CODEX_CAPACITY_RESERVED"
        assert waiting["execution"]["finished_at_ms"] is None
        assert waiting["execution"]["stages"][0]["status"] == "running"

        fake.remaining = 85.0
        await service.advance_active_once()
        resumed = await service.status(
            actor_id=boot["actor"]["id"],
            workspace_id=boot["workspace"]["id"],
            execution_id=execution_id,
        )
        assert resumed["execution"]["status"] == "running"
        assert resumed["execution"]["phase"] == "implementing"
        assert resumed["execution"]["error_code"] is None
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

        assert advanced == 1
        assert current["execution"]["status"] == "failed"
        assert current["execution"]["phase"] == "failed"
        assert current["execution"]["error_code"] == "REVIEW_REWORK_LIMIT"
        assert current["execution"]["review_cycle"] == max_cycle
        assert len([1 for name, _ in fake.calls if name == "continue"]) == continues_before
    finally:
        await service.close()
        store.close()




@pytest.mark.asyncio
async def test_missing_review_verdict_restarts_read_only_review(tmp_path: Path):
    class MissingVerdictDevCoveer(FakeDevCoveer):
        async def read_task(self, task_id: str, *, project=None, detail="summary"):
            self.calls.append(("read", {"task": task_id, "project": project, "detail": detail}))
            if task_id == self._quality_task:
                self._quality_reads += 1
                if self._quality_reads == 1:
                    return self._completed(
                        "Design complete. Spec: docs/prompts/owner-development-test.md",
                        100,
                    )
                if self._quality_reads == 2:
                    return self._completed("Review finished but malformed output.", 50)
            return await super().read_task(task_id, project=project, detail=detail)

    store, readiness, boot, project = setup(tmp_path)
    fake = MissingVerdictDevCoveer()
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
        assert current["execution"]["error_code"] is None
        review_stages = [
            stage for stage in current["execution"]["stages"]
            if stage["stage"] == "review"
        ]
        assert [stage["status"] for stage in review_stages] == [
            "completed",
            "running",
        ]
        starts = [args for name, args in fake.calls if name == "start"]
        assert starts[-1]["model"] == "gpt-6-astra"
        assert starts[-1]["access"] == "read"
    finally:
        await service.close()
        store.close()


def test_owner_development_has_no_technical_needs_owner_fallback():
    source = Path("src/projects_hub/development.py").read_text(encoding="utf-8")
    assert "phase='needs_owner'" not in source


@pytest.mark.asyncio
async def test_marked_dispatch_without_task_id_reconciles_history(tmp_path: Path):
    store, readiness, boot, project = setup(tmp_path)
    fake = FakeDevCoveer()
    try:
        service = DevelopmentService(store, readiness, devcoveer=fake)
        marker = "ODR-no-id-reconcile"
        existing = "dvt_" + "n" * 32
        calls = 0

        async def history(*, project: str, search: str | None = None, limit: int = 20):
            nonlocal calls
            calls += 1
            if calls >= 2:
                return {
                    "status": "ok",
                    "tasks": [{
                        "taskId": existing,
                        "backend": "codex",
                        "model": "gpt-6.1-sol",
                        "access": "write",
                        "name": marker,
                    }],
                }
            return {"status": "ok", "tasks": []}

        async def start_without_id(**kwargs):
            fake.calls.append(("start-no-id", kwargs))
            return {"status": "running"}

        fake.list_codex_tasks = history  # type: ignore[method-assign]
        fake.start_codex_task = start_without_id  # type: ignore[method-assign]

        task_id, result = await service._start_marked_codex_task(
            project="projects-hub-owner",
            marker=marker,
            prompt="continue safely",
            model="gpt-6.1-sol",
            reasoning_effort="medium",
            access="write",
        )

        assert task_id == existing
        assert result["status"] == "reconciled"
        assert calls == 2
        assert len([1 for name, _ in fake.calls if name == "start-no-id"]) == 1
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
