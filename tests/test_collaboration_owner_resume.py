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
        self.reads: list[str] = []
        self.turn_id = "turn-before"
        self.updated_at = 1
        self.lose_next_continue_response = False
        self.fail_reads = 0

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

    async def read_task(self, task_id: str, **kwargs):
        self.reads.append(task_id)
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise RuntimeError("simulated readback interruption")
        return {
            "status": "completed",
            "task": {
                "taskId": task_id,
                "taskReference": task_id,
                "updatedAt": self.updated_at,
            },
            "latestTurn": {
                "turnId": self.turn_id,
                "status": "completed",
                "finalResponse": "REVIEW_VERDICT: ACCEPTED",
            },
        }

    async def continue_codex_task(self, task_id: str, **kwargs):
        self.calls.append({"task_id": task_id, **kwargs})
        self.updated_at += 1
        self.turn_id = f"turn-after-{len(self.calls)}"
        if self.lose_next_continue_response:
            self.lose_next_continue_response = False
            # The provider accepted the new turn, but the client loses the
            # response and the first reconciliation read also fails.
            self.fail_reads = 1
            raise RuntimeError("simulated lost continue response")
        return {
            "status": "running",
            "taskId": task_id,
            "turnId": self.turn_id,
        }

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


@pytest.mark.asyncio
async def test_lost_owner_resume_response_reconciles_across_new_command_without_second_dispatch(tmp_path: Path):
    store, service, development, devcoveer, actor, workspace = make_state(tmp_path)
    try:
        question = next(
            item for item in service.inbox(actor_id=actor, workspace_id=workspace)
            if item["source_kind"] == "owner_development"
        )
        devcoveer.lose_next_continue_response = True

        with pytest.raises(StoreError) as first:
            await service.answer_owner_development_question(
                actor_id=actor,
                workspace_id=workspace,
                question_id=question["id"],
                command_id="owner.reply.lost1",
                disposition="answer",
                body="Сохраняем тот же scope и продолжаем существующее ревью.",
            )
        assert first.value.code == "DEVELOPMENT_RESUME_OUTCOME_UNKNOWN"
        assert len(devcoveer.calls) == 1
        with store._lock:
            resume = store.db.execute(
                """SELECT status,answer_text FROM development_owner_resumes
                   WHERE execution_id='devexec_waiting'"""
            ).fetchone()
            execution = store.db.execute(
                "SELECT status,phase FROM task_executions WHERE id='devexec_waiting'"
            ).fetchone()
        assert resume["status"] == "dispatch_unknown"
        assert dict(execution) == {"status": "blocked", "phase": "needs_owner"}

        # The UI may generate a new command id on retry. Provider readback
        # proves that the previous continuation already created a new turn, so
        # no second continue_task call is allowed.
        retried = await service.answer_owner_development_question(
            actor_id=actor,
            workspace_id=workspace,
            question_id=question["id"],
            command_id="owner.reply.lost2",
            disposition="answer",
            body="Сохраняем тот же scope и продолжаем существующее ревью.",
        )
        assert retried["continuation"] == "resumed"
        assert retried["execution"]["id"] == "devexec_waiting"
        assert len(devcoveer.calls) == 1
        with store._lock:
            rows = store.db.execute(
                """SELECT command_id,status FROM development_owner_resumes
                   WHERE execution_id='devexec_waiting'"""
            ).fetchall()
        assert [(row["command_id"], row["status"]) for row in rows] == [
            ("owner.reply.lost1", "applied")
        ]
    finally:
        await service.close()
        await development.close()
        store.close()


@pytest.mark.asyncio
async def test_background_reconciles_unknown_owner_resume_after_service_restart(tmp_path: Path):
    store, service, development, devcoveer, actor, workspace = make_state(tmp_path)
    replacement: DevelopmentService | None = None
    try:
        question = next(
            item for item in service.inbox(actor_id=actor, workspace_id=workspace)
            if item["source_kind"] == "owner_development"
        )
        devcoveer.lose_next_continue_response = True
        with pytest.raises(StoreError) as first:
            await service.answer_owner_development_question(
                actor_id=actor,
                workspace_id=workspace,
                question_id=question["id"],
                command_id="owner.reply.restart1",
                disposition="answer",
                body="Ответ принят; после рестарта продолжить тот же quality thread.",
            )
        assert first.value.code == "DEVELOPMENT_RESUME_OUTCOME_UNKNOWN"
        assert len(devcoveer.calls) == 1

        await development.close()
        replacement = DevelopmentService(
            store,
            ReadinessService(store),
            devcoveer=devcoveer,  # type: ignore[arg-type]
        )
        advanced = await replacement.advance_active_once()
        assert advanced >= 1
        with store._lock:
            execution = store.db.execute(
                """SELECT status,phase,quality_task_id,implementation_task_id
                   FROM task_executions WHERE id='devexec_waiting'"""
            ).fetchone()
            resume = store.db.execute(
                """SELECT status FROM development_owner_resumes
                   WHERE execution_id='devexec_waiting'"""
            ).fetchone()
        assert execution["status"] == "running"
        assert execution["phase"] == "reviewing"
        assert execution["quality_task_id"] == "dvt_quality"
        assert execution["implementation_task_id"] == "dvt_impl"
        assert resume["status"] == "applied"
        assert len(devcoveer.calls) == 1

        # The collaboration worker synchronizes the accepted answer back to the
        # common question object after development reconciliation.
        service.development = replacement
        service._sync_owner_development_questions()
        synced = next(
            item for item in service.inbox(actor_id=actor, workspace_id=workspace)
            if item["id"] == question["id"]
        )
        assert synced["state"] == "resolved"
        assert synced["disposition"] == "answer"
        assert synced["answer_text"] == "Ответ принят; после рестарта продолжить тот же quality thread."
    finally:
        await service.close()
        if replacement is not None:
            await replacement.close()
        else:
            await development.close()
        store.close()
