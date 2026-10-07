from __future__ import annotations

import json
from pathlib import Path

import pytest

from projects_hub.collaboration import CollaborationService
from projects_hub.collaboration_analysis import CollaborationAnalysisService
from projects_hub.note_processing import NoteSummary, ProcessedNote
from projects_hub.store import DurableStore, StoreError


class FakeNoteProcessor:
    async def summarize(self, **kwargs):
        source = str(kwargs["source_text"])
        return ProcessedNote(
            summary=NoteSummary(
                title=str(kwargs.get("suggested_title") or "Note"),
                short_summary=source,
                detailed_summary=source,
                theses=[],
                ideas=[],
                decisions=[],
                tasks=[],
                facts=[],
                entities=[],
                related_projects=[],
                open_questions=[],
                contradictions=[],
                uncertain_fragments=[],
                tags=[],
            ),
            model="gemini-test",
            prompt_version="test-v1",
            request_uid="req-test",
            limiter={"contract": "test"},
        )

    async def close(self):
        return None


class FakeGitHub:
    def __init__(self, store: DurableStore) -> None:
        self.store = store
        self.files: dict[tuple[int, str], dict[str, str]] = {}

    async def write_repository_text(self, **kwargs):
        key = (int(kwargs["repository_id"]), str(kwargs["path"]))
        current = self.files.setdefault(
            key,
            {"text": str(kwargs["text"]), "sha": "sha-note"},
        )
        return {
            "repository_id": int(kwargs["repository_id"]),
            "full_name": "example/project-docs",
            "project_id": kwargs["project_id"],
            "default_branch": "main",
            "kind": "file",
            "path": kwargs["path"],
            "text": current["text"],
            "sha": current["sha"],
        }

    async def read_repository_path(self, **kwargs):
        connection = self.store.get_repository_connection(
            kwargs["actor_id"], kwargs["workspace_id"], int(kwargs["repository_id"])
        )
        item = self.files[(int(kwargs["repository_id"]), str(kwargs["path"]))]
        return {
            "repository_id": int(kwargs["repository_id"]),
            "full_name": connection["full_name"],
            "project_id": connection["project_id"],
            "default_branch": connection["default_branch"],
            "kind": "file",
            "path": kwargs["path"],
            "text": item["text"],
            "sha": item["sha"],
        }


class FakeBridge:
    def __init__(self) -> None:
        self.consults: list[dict] = []
        self.reads: list[str] = []
        self.closed = False
        self.analysis_payload = {
            "executionStatus": "completed",
            "latestTurn": {
                "finalResponse": json.dumps(
                    {
                        "summary": "Нужно уточнить две вещи перед продолжением.",
                        "questions": [
                            {
                                "prompt": "Какой вариант интерфейса считаем обязательным?",
                                "blocking": True,
                            },
                            {
                                "prompt": "Нужна ли дополнительная иллюстрация?",
                                "blocking": False,
                            },
                        ],
                    },
                    ensure_ascii=False,
                )
            },
        }
        self.followup_payload = {
            "executionStatus": "completed",
            "latestTurn": {
                "finalResponse": "# Продолжение\n\nРешение уточнено по ответам."
            },
        }

    async def consult(self, **kwargs):
        self.consults.append(dict(kwargs))
        return {
            "status": "running",
            "taskId": "provider-analysis-1"
            if len(self.consults) == 1
            else "provider-followup-1",
        }

    async def read_task(self, task_id: str):
        self.reads.append(task_id)
        return (
            self.analysis_payload
            if task_id == "provider-analysis-1"
            else self.followup_payload
        )

    async def close(self):
        self.closed = True


def setup(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
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
    github = FakeGitHub(store)
    collaboration = CollaborationService(
        store,
        github,  # type: ignore[arg-type]
        note_processor=FakeNoteProcessor(),  # type: ignore[arg-type]
    )
    invite = collaboration.invite_participant(
        owner_actor_id=actor_a,
        workspace_id=workspace_id,
        project_id=project_id,
        display_name="Participant B",
        role="editor",
    )
    actor_b = invite["actor_id"]
    bridge = FakeBridge()
    analysis = CollaborationAnalysisService(
        store,
        collaboration,
        bridge=bridge,  # type: ignore[arg-type]
        poll_seconds=0.01,
    )
    return store, collaboration, analysis, bridge, actor_a, actor_b, workspace_id, project_id


@pytest.mark.asyncio
async def test_analysis_questions_grouped_answer_and_restart_safe_continuation(tmp_path: Path):
    (
        store, collaboration, service, bridge,
        actor_a, actor_b, workspace_id, project_id,
    ) = setup(tmp_path)
    note = await collaboration.create_note(
        actor_id=actor_a,
        workspace_id=workspace_id,
        project_id=project_id,
        command_id="cmd.note.analysis",
        title="UX decision",
        body="Нужно выбрать минимальный путь коллаборации.",
    )

    run = await service.start_analysis(
        actor_id=actor_a,
        workspace_id=workspace_id,
        project_id=project_id,
        note_id=note["id"],
        addressed_to_actor_id=actor_b,
        command_id="cmd.analysis.0001",
        model="kimi_k3",
        purpose="requirements",
        question="Найди только вопросы, без которых нельзя уверенно продолжить.",
    )
    assert run["status"] == "running"
    assert bridge.consults[0]["request_key"].startswith("collab-analysis:")
    assert bridge.consults[0]["evidence_bundle"].find("UX decision") >= 0

    completed = await service.refresh_analysis(
        actor_id=actor_a,
        workspace_id=workspace_id,
        analysis_id=run["id"],
    )
    assert completed["status"] == "completed"
    assert len(completed["questions"]) == 2
    assert all(item["addressed_to_actor_id"] == actor_b for item in completed["questions"])

    inbox = service.inbox(actor_id=actor_b, workspace_id=workspace_id)
    assert [item["id"] for item in inbox] == [
        item["id"] for item in completed["questions"]
    ]

    blocking = next(item for item in inbox if item["blocking"])
    optional = next(item for item in inbox if not item["blocking"])
    receipt = service.answer_questions(
        actor_id=actor_b,
        workspace_id=workspace_id,
        analysis_id=run["id"],
        command_id="cmd.answers.0001",
        responses=[
            {
                "question_id": blocking["id"],
                "disposition": "answer",
                "body": "Inline-виджет обязателен.",
            },
            {
                "question_id": optional["id"],
                "disposition": "unknown",
                "body": "",
            },
        ],
    )
    assert receipt["continuation"] == "queued"

    # Same semantic command is idempotent.
    again = service.answer_questions(
        actor_id=actor_b,
        workspace_id=workspace_id,
        analysis_id=run["id"],
        command_id="cmd.answers.0001",
        responses=[
            {
                "question_id": blocking["id"],
                "disposition": "answer",
                "body": "Inline-виджет обязателен.",
            },
            {
                "question_id": optional["id"],
                "disposition": "unknown",
                "body": "",
            },
        ],
    )
    assert again == receipt

    with store._lock:
        jobs = store.db.execute(
            "SELECT * FROM collaboration_jobs WHERE analysis_id=?", (run["id"],)
        ).fetchall()
    assert len(jobs) == 1
    job_id = jobs[0]["id"]

    # Status reads do not advance.
    snapshot = service.job_status(
        actor_id=actor_a, workspace_id=workspace_id, job_id=job_id
    )
    assert snapshot["status"] == "queued"
    assert len(bridge.consults) == 1

    # Simulated backend restart: a new service sees the same durable queued job.
    await service.close()
    bridge2 = FakeBridge()
    bridge2.consults.append(dict(bridge.consults[0]))
    restarted = CollaborationAnalysisService(
        store,
        collaboration,
        bridge=bridge2,  # type: ignore[arg-type]
        poll_seconds=0.01,
    )
    await restarted.advance_jobs_once()
    running = restarted.job_status(
        actor_id=actor_a, workspace_id=workspace_id, job_id=job_id
    )
    assert running["status"] == "running"
    assert bridge2.consults[-1]["request_key"] == f"collaboration-continuation:{run['id']}"

    await restarted.advance_jobs_once()
    done = restarted.job_status(
        actor_id=actor_a, workspace_id=workspace_id, job_id=job_id
    )
    assert done["status"] == "completed"
    assert done["external_receipt"]["provider_task_id"] == "provider-followup-1"
    brief = collaboration.personal_brief(
        actor_id=actor_a, workspace_id=workspace_id
    )
    assert any(item["kind"] == "continuation_completed" for item in brief["personal"])

    await restarted.close()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disposition", "expected"),
    [("later", "deferred"), ("unknown", "blocked"), ("skip", "blocked")],
)
async def test_blocking_question_non_answer_does_not_continue(
    tmp_path: Path, disposition: str, expected: str
):
    (
        store, collaboration, service, _bridge,
        actor_a, actor_b, workspace_id, project_id,
    ) = setup(tmp_path)
    note = await collaboration.create_note(
        actor_id=actor_a,
        workspace_id=workspace_id,
        project_id=project_id,
        command_id=f"cmd.note.{disposition}",
        title="Blocking decision",
        body="Нужен ответ участника.",
    )
    run = await service.start_analysis(
        actor_id=actor_a,
        workspace_id=workspace_id,
        project_id=project_id,
        note_id=note["id"],
        addressed_to_actor_id=actor_b,
        command_id=f"cmd.analysis.{disposition}",
        model="deepseek",
        purpose="edge_cases",
        question="Сформируй блокирующий вопрос.",
    )
    completed = await service.refresh_analysis(
        actor_id=actor_a, workspace_id=workspace_id, analysis_id=run["id"]
    )
    blocking = next(item for item in completed["questions"] if item["blocking"])
    receipt = service.answer_questions(
        actor_id=actor_b,
        workspace_id=workspace_id,
        analysis_id=run["id"],
        command_id=f"cmd.answer.{disposition}",
        responses=[
            {
                "question_id": blocking["id"],
                "disposition": disposition,
                "body": "",
            }
        ],
    )
    assert receipt["continuation"] == expected
    inbox_after = service.inbox(actor_id=actor_b, workspace_id=workspace_id)
    if disposition == "later":
        assert all(item["id"] != blocking["id"] for item in inbox_after)
    else:
        still_open = next(item for item in inbox_after if item["id"] == blocking["id"])
        assert still_open["state"] == "open"
        assert still_open["disposition"] == disposition
    with store._lock:
        assert store.db.execute(
            "SELECT COUNT(*) AS n FROM collaboration_jobs WHERE analysis_id=?",
            (run["id"],),
        ).fetchone()["n"] == 0
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_question_cannot_be_answered_by_another_project_actor(tmp_path: Path):
    (
        store, collaboration, service, _bridge,
        actor_a, actor_b, workspace_id, project_id,
    ) = setup(tmp_path)
    note = await collaboration.create_note(
        actor_id=actor_a,
        workspace_id=workspace_id,
        project_id=project_id,
        command_id="cmd.note.aclq",
        title="ACL",
        body="Question ACL.",
    )
    run = await service.start_analysis(
        actor_id=actor_a,
        workspace_id=workspace_id,
        project_id=project_id,
        note_id=note["id"],
        addressed_to_actor_id=actor_b,
        command_id="cmd.analysis.aclq",
        model="kimi_k3",
        purpose="requirements",
        question="Ask one question.",
    )
    completed = await service.refresh_analysis(
        actor_id=actor_a, workspace_id=workspace_id, analysis_id=run["id"]
    )
    question = completed["questions"][0]

    actor_c = "usr_other_editor"
    with store._lock:
        store.db.execute(
            "INSERT INTO actors(id,display_name,created_at_ms) VALUES(?,?,?)",
            (actor_c, "Participant C", 1),
        )
        store.db.execute(
            "INSERT INTO memberships(actor_id,workspace_id,role) VALUES(?,?,?)",
            (actor_c, workspace_id, "member"),
        )
    store.grant_project_access(
        actor_id=actor_a,
        workspace_id=workspace_id,
        project_id=project_id,
        target_actor_id=actor_c,
        role="editor",
    )
    with pytest.raises(StoreError) as exc:
        service.answer_questions(
            actor_id=actor_c,
            workspace_id=workspace_id,
            analysis_id=run["id"],
            command_id="cmd.answer.wrongactor",
            responses=[
                {
                    "question_id": question["id"],
                    "disposition": "answer",
                    "body": "Not mine.",
                }
            ],
        )
    assert exc.value.code == "PROJECT_FORBIDDEN"
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_durable_worker_materializes_initial_analysis_without_status_refresh(tmp_path: Path):
    (
        store, collaboration, service, bridge,
        actor_a, actor_b, workspace_id, project_id,
    ) = setup(tmp_path)
    try:
        note = await collaboration.create_note(
            actor_id=actor_a,
            workspace_id=workspace_id,
            project_id=project_id,
            command_id="cmd.note.worker-analysis",
            title="Worker analysis",
            body="Анализ должен завершаться без открытого клиента.",
        )
        started = await service.start_analysis(
            actor_id=actor_a,
            workspace_id=workspace_id,
            project_id=project_id,
            note_id=note["id"],
            addressed_to_actor_id=actor_b,
            command_id="cmd.analysis.worker1",
            model="kimi_k3",
            purpose="requirements",
            question="Сформируй минимальные вопросы.",
        )
        assert started["status"] == "running"
        assert bridge.reads == []

        advanced = await service.advance_jobs_once()
        assert advanced >= 1
        completed = service.get_analysis(
            actor_id=actor_a,
            workspace_id=workspace_id,
            analysis_id=started["id"],
        )
        assert completed["status"] == "completed"
        assert bridge.reads == ["provider-analysis-1"]
        inbox = service.inbox(actor_id=actor_b, workspace_id=workspace_id)
        assert any(item["analysis_id"] == started["id"] for item in inbox)
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_revoked_project_grant_hides_addressed_questions_and_denies_answer(tmp_path: Path):
    (
        store, collaboration, service, _bridge,
        actor_a, actor_b, workspace_id, project_id,
    ) = setup(tmp_path)
    try:
        note = await collaboration.create_note(
            actor_id=actor_a,
            workspace_id=workspace_id,
            project_id=project_id,
            command_id="cmd.note.revoke-question",
            title="Revoke question",
            body="Вопрос не должен пережить отзыв доступа как читаемый объект.",
        )
        started = await service.start_analysis(
            actor_id=actor_a,
            workspace_id=workspace_id,
            project_id=project_id,
            note_id=note["id"],
            addressed_to_actor_id=actor_b,
            command_id="cmd.analysis.revoke1",
            model="kimi_k3",
            purpose="requirements",
            question="Задай вопрос участнику.",
        )
        await service.advance_jobs_once()
        before = service.inbox(actor_id=actor_b, workspace_id=workspace_id)
        question = next(item for item in before if item["analysis_id"] == started["id"])

        store.revoke_project_access(
            actor_id=actor_a,
            workspace_id=workspace_id,
            project_id=project_id,
            target_actor_id=actor_b,
        )
        after = service.inbox(actor_id=actor_b, workspace_id=workspace_id)
        assert all(item["project_id"] != project_id for item in after)

        with pytest.raises(StoreError) as exc:
            service.answer_questions(
                actor_id=actor_b,
                workspace_id=workspace_id,
                analysis_id=started["id"],
                command_id="cmd.answer.revoked1",
                responses=[
                    {
                        "question_id": question["id"],
                        "disposition": "answer",
                        "body": "Этот ответ уже не должен приниматься.",
                    }
                ],
            )
        assert exc.value.code == "PROJECT_FORBIDDEN"
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_queued_analysis_continuation_rechecks_requester_grant_before_dispatch(tmp_path: Path):
    (
        store, collaboration, service, bridge,
        actor_a, actor_b, workspace_id, project_id,
    ) = setup(tmp_path)
    try:
        note = await collaboration.create_note(
            actor_id=actor_a,
            workspace_id=workspace_id,
            project_id=project_id,
            command_id="cmd.note.continuation-revoke",
            title="Continuation revoke",
            body="Continuation must use current authorization, not only frozen evidence.",
        )
        run = await service.start_analysis(
            actor_id=actor_a,
            workspace_id=workspace_id,
            project_id=project_id,
            note_id=note["id"],
            addressed_to_actor_id=actor_b,
            command_id="cmd.analysis.continuation-revoke",
            model="kimi_k3",
            purpose="requirements",
            question="Задай минимальные вопросы.",
        )
        completed = await service.refresh_analysis(
            actor_id=actor_a,
            workspace_id=workspace_id,
            analysis_id=run["id"],
        )
        responses = [
            {
                "question_id": item["id"],
                "disposition": "answer",
                "body": "Достаточный ответ для продолжения.",
            }
            for item in completed["questions"]
        ]
        receipt = service.answer_questions(
            actor_id=actor_b,
            workspace_id=workspace_id,
            analysis_id=run["id"],
            command_id="cmd.answers.continuation-revoke",
            responses=responses,
        )
        assert receipt["continuation"] == "queued"
        assert len(bridge.consults) == 1

        # Revoke the initiating/requesting actor after evidence and answers were
        # durably accepted but before the external continuation dispatch.
        with store._lock:
            store.db.execute(
                """UPDATE project_grants SET revoked_at_ms=?
                   WHERE actor_id=? AND project_id=?""",
                (1_900_000_000_000, actor_a, project_id),
            )

        await service.advance_jobs_once()
        with store._lock:
            job = store.db.execute(
                "SELECT status,error_code FROM collaboration_jobs WHERE analysis_id=?",
                (run["id"],),
            ).fetchone()
        assert job["status"] == "failed"
        assert job["error_code"] == "PROJECT_FORBIDDEN"
        assert len(bridge.consults) == 1
    finally:
        await service.close()
        store.close()
