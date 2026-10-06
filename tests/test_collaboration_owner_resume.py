from __future__ import annotations

from pathlib import Path

import pytest

from projects_hub.collaboration import CollaborationService
from projects_hub.collaboration_analysis import CollaborationAnalysisService
from projects_hub.development import DevelopmentService
from projects_hub.readiness import ReadinessService
from projects_hub.store import DurableStore, StoreError


class NoopGitHub:
    pass


class ResumeBridge:
    async def close(self):
        return None


class ResumeDevCoveer:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def status(self):
        return {
            "quota": {
                "status": "available",
                "admission": {
                    "eligible": True,
                    "effective_remaining_percent": 80.0,
                    "reserve_percent": 10.0,
                },
                "default_profile": {
                    "requested": "gpt-6.1-medium",
                    "model": "gpt-6.1-sol",
                    "reasoning_effort": "medium",
                    "catalog_available": True,
                },
            },
            "models": [{
                "id": "gpt-6-astra",
                "reasoningEfforts": ["high"],
                "availability": "live_catalog",
            }],
        }

    async def continue_codex_task(self, task_id: str, **kwargs):
        self.calls.append({"task_id": task_id, **kwargs})
        return {"status": "running", "taskId": task_id}

    async def close(self):
        return None


def make_state(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    boot = store.ensure_platform_owner("Owner")
    actor = boot["actor"]["id"]
    workspace = boot["workspace"]["id"]
    project = boot["projects"][0]["id"]
    devcoveer = ResumeDevCoveer()
    development = DevelopmentService(
        store,
        ReadinessService(store),
        devcoveer=devcoveer,  # type: ignore[arg-type]
    )
    now = 1_800_000_000_000
    with store._lock:
        store.db.execute(
            """INSERT INTO task_executions(
                   id,actor_id,workspace_id,project_id,task_ids_json,project_hint,
                   provider,model_profile,prompt,prompt_sha256,status,phase,phase_detail,
                   devcoveer_task_id,quota_remaining_percent,result_summary,error_code,
                   created_at_ms,updated_at_ms,started_at_ms,finished_at_ms,
                   quality_task_id,implementation_task_id,review_cycle,spec_path)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "devexec_waiting", actor, workspace, project, "[]", "projects-hub",
                "codex", "gpt-6.1-sol:medium", "existing", "0" * 64,
                "blocked", "needs_owner", "Нужно решение владельца",
                "dvt_quality", 80.0, "Ревью не дало однозначного verdict.",
                "REVIEW_VERDICT_MISSING", now, now, now, now,
                "dvt_quality", "dvt_impl", 0, "docs/prompts/existing.md",
            ),
        )
    collaboration = CollaborationService(store, NoopGitHub())  # type: ignore[arg-type]
    service = CollaborationAnalysisService(
        store,
        collaboration,
        bridge=ResumeBridge(),  # type: ignore[arg-type]
        development=development,
    )
    return store, service, development, devcoveer, actor, workspace


@pytest.mark.asyncio
async def test_owner_question_resumes_same_execution_once(tmp_path: Path):
    store, service, development, devcoveer, actor, workspace = make_state(tmp_path)
    try:
        question = next(
            item for item in service.inbox(actor_id=actor, workspace_id=workspace)
            if item["source_kind"] == "owner_development"
        )
        receipt = await service.answer_owner_development_question(
            actor_id=actor,
            workspace_id=workspace,
            question_id=question["id"],
            command_id="owner.reply.0001",
            disposition="answer",
            body="Сохраняем scope; критерии выполнены.",
        )
        assert receipt["continuation"] == "resumed"
        assert receipt["execution"]["id"] == "devexec_waiting"
        assert receipt["execution"]["status"] == "running"
        assert len(devcoveer.calls) == 1
        assert devcoveer.calls[0]["task_id"] == "dvt_quality"
        assert devcoveer.calls[0]["access"] == "read"

        retry = await service.answer_owner_development_question(
            actor_id=actor,
            workspace_id=workspace,
            question_id=question["id"],
            command_id="owner.reply.0001",
            disposition="answer",
            body="Сохраняем scope; критерии выполнены.",
        )
        assert retry == receipt
        assert len(devcoveer.calls) == 1
    finally:
        await service.close()
        await development.close()
        store.close()


@pytest.mark.asyncio
async def test_non_owner_cannot_answer_owner_question(tmp_path: Path):
    store, service, development, _devcoveer, actor, workspace = make_state(tmp_path)
    try:
        question = next(
            item for item in service.inbox(actor_id=actor, workspace_id=workspace)
            if item["source_kind"] == "owner_development"
        )
        with store._lock:
            project = store.db.execute(
                "SELECT project_id FROM owner_development_questions WHERE id=?",
                (question["id"],),
            ).fetchone()["project_id"]
            other = "usr_other"
            store.db.execute(
                "INSERT INTO actors(id,display_name,created_at_ms) VALUES(?,?,?)",
                (other, "Other", 1),
            )
            store.db.execute(
                "INSERT INTO memberships(actor_id,workspace_id,role) VALUES(?,?,?)",
                (other, workspace, "member"),
            )
        store.grant_project_access(
            actor_id=actor,
            workspace_id=workspace,
            project_id=project,
            target_actor_id=other,
            role="editor",
        )
        with pytest.raises(StoreError) as exc:
            await service.answer_owner_development_question(
                actor_id=other,
                workspace_id=workspace,
                question_id=question["id"],
                command_id="owner.reply.denied",
                disposition="answer",
                body="Не мой вопрос.",
            )
        assert exc.value.code == "PROJECT_FORBIDDEN"
    finally:
        await service.close()
        await development.close()
        store.close()


@pytest.mark.asyncio
async def test_owner_resume_refuses_when_another_execution_is_active(tmp_path: Path):
    store, service, development, devcoveer, actor, workspace = make_state(tmp_path)
    try:
        question = next(
            item for item in service.inbox(actor_id=actor, workspace_id=workspace)
            if item["source_kind"] == "owner_development"
        )
        with store._lock:
            base = store.db.execute(
                "SELECT * FROM task_executions WHERE id='devexec_waiting'"
            ).fetchone()
            store.db.execute(
                """INSERT INTO task_executions(
                       id,actor_id,workspace_id,project_id,task_ids_json,project_hint,
                       provider,model_profile,prompt,prompt_sha256,status,phase,phase_detail,
                       devcoveer_task_id,quota_remaining_percent,result_summary,error_code,
                       created_at_ms,updated_at_ms,started_at_ms,finished_at_ms,
                       quality_task_id,implementation_task_id,review_cycle,spec_path)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "devexec_other_active",
                    base["actor_id"],
                    base["workspace_id"],
                    base["project_id"],
                    "[]",
                    base["project_hint"],
                    "codex",
                    "gpt-6.1-sol:medium",
                    "other active execution",
                    "1" * 64,
                    "running",
                    "implementing",
                    "Other owner work is active",
                    "dvt_other_active",
                    80.0,
                    "",
                    None,
                    int(base["created_at_ms"]) + 1,
                    int(base["updated_at_ms"]) + 1,
                    int(base["started_at_ms"]) + 1,
                    None,
                    "dvt_other_quality",
                    "dvt_other_impl",
                    0,
                    "docs/prompts/other.md",
                ),
            )

        with pytest.raises(StoreError) as exc:
            await service.answer_owner_development_question(
                actor_id=actor,
                workspace_id=workspace,
                question_id=question["id"],
                command_id="owner.reply.busy",
                disposition="answer",
                body="Ответ достаточен, но другое owner execution уже активно.",
            )
        assert exc.value.code == "DEVELOPMENT_EXECUTION_ACTIVE"
        assert devcoveer.calls == []
        with store._lock:
            row = store.db.execute(
                "SELECT status,phase FROM task_executions WHERE id='devexec_waiting'"
            ).fetchone()
        assert dict(row) == {"status": "blocked", "phase": "needs_owner"}
    finally:
        await service.close()
        await development.close()
        store.close()
