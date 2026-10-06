from __future__ import annotations

from pathlib import Path

import pytest

from projects_hub.analytics import AnalyticsService
from projects_hub.analytics_materialization import AnalysisMaterializer
from projects_hub.board import BoardService
from projects_hub.github_connections import GitHubConnections
from projects_hub.store import DurableStore, StoreError


class FakeBridge:
    async def consult(self, **_kwargs):
        return {"status": "running", "taskId": "dvt_" + "a" * 32}

    async def read_task(self, _task_id):
        return {
            "status": "completed",
            "executionStatus": "completed",
            "latestTurn": {
                "finalResponse": "# Report\n\nPrivate analysis body.\n"
            },
        }

    async def cancel_task(self, task_id):
        return {"status": "interrupted", "taskId": task_id}

    async def close(self):
        return None


class FakeGitHub:
    def __init__(self) -> None:
        self.files: dict[tuple[int, str], dict] = {}
        self.writes: list[dict] = []
        self.fail_write = False

    async def read_repository_path(
        self,
        *,
        actor_id,
        workspace_id,
        repository_id,
        path,
    ):
        key = (int(repository_id), path)
        if key not in self.files:
            raise StoreError("GITHUB_NOT_FOUND", "not found")
        value = self.files[key]
        return {
            "repository_id": int(repository_id),
            "full_name": "owner/repo",
            "project_id": "ignored",
            "default_branch": "main",
            "kind": "file",
            "path": path,
            "size": len(value["text"]),
            "sha": value["sha"],
            "text": value["text"],
        }

    async def write_repository_text(
        self,
        *,
        actor_id,
        workspace_id,
        repository_id,
        path,
        text,
        message,
        expected_sha=None,
    ):
        if self.fail_write:
            raise StoreError("GITHUB_UNAVAILABLE", "offline")
        key = (int(repository_id), path)
        current = self.files.get(key)
        if current is not None and expected_sha != current["sha"]:
            raise StoreError("GITHUB_CONFLICT", "stale sha")
        if current is None and expected_sha is not None:
            raise StoreError("GITHUB_CONFLICT", "unexpected sha")
        sha = f"blob-{len(self.writes) + 1}"
        commit = f"commit-{len(self.writes) + 1}"
        self.files[key] = {"sha": sha, "text": text}
        call = {
            "repository_id": int(repository_id),
            "path": path,
            "text": text,
            "message": message,
            "expected_sha": expected_sha,
        }
        self.writes.append(call)
        return {
            "repository_id": int(repository_id),
            "full_name": "owner/repo",
            "default_branch": "main",
            "private": True,
            "path": path,
            "content_sha": sha,
            "commit_sha": commit,
        }


def _setup(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    boot = store.ensure_dev_workspace("Materialization")
    actor = boot["actor"]["id"]
    workspace = boot["workspace"]["id"]
    project = boot["projects"][0]["id"]
    board = BoardService(store)
    board_id = board.open_board(actor, workspace, project)["id"]
    board.apply_command(
        actor_id=actor,
        workspace_id=workspace,
        board_id=board_id,
        command_id="cmd_materialization_seed",
        operation="create",
        object_id="obj_materialization_seed",
        expected_object_revision=None,
        payload={
            "type": "sticky",
            "text": "Analyze this",
            "style": {"color": "blue"},
        },
        execution_origin="mira",
    )
    analytics = AnalyticsService(store, board, bridge=FakeBridge())
    return store, actor, workspace, project, board_id, analytics


async def _completed_run(
    analytics: AnalyticsService,
    *,
    actor: str,
    workspace: str,
    project: str,
    board_id: str,
):
    run = await analytics.start_single(
        actor_id=actor,
        workspace_id=workspace,
        project_id=project,
        board_id=board_id,
        object_ids=["obj_materialization_seed"],
        command_id="analysis_materialize_001",
        model="kimi_k3",
        purpose="edge_cases",
        question="Find one risk.",
    )
    return await analytics.refresh(
        actor_id=actor,
        workspace_id=workspace,
        run_id=run["id"],
    )


def _connection(
    store: DurableStore,
    *,
    workspace: str,
    project: str,
    repository_id: int = 101,
    private: bool = True,
    role: str = "generated_artifacts",
    access_mode: str = "app_managed_write",
    allowed_paths: list[str] | None = None,
):
    allowed_paths = allowed_paths if allowed_paths is not None else ["docs/analysis"]
    with store._lock:
        store.db.execute(
            """INSERT INTO github_installations(
                   installation_id,workspace_id,account_id,account_login,account_type,
                   html_url,repository_selection,permissions_json,state,suspended_at_ms,
                   last_verified_at_ms,created_at_ms,updated_at_ms)
               VALUES(?,?,?,?,?,?,?,?,?,NULL,?,?,?)""",
            (
                repository_id,
                workspace,
                1,
                "owner",
                "User",
                "https://github.com/owner",
                "selected",
                '{"contents":"write"}',
                "active",
                1,
                1,
                1,
            ),
        )
        store.db.execute(
            """INSERT INTO repository_connections(
                   id,workspace_id,installation_id,repository_id,full_name,default_branch,
                   private,project_id,role,access_mode,allowed_paths_json,state,
                   last_verified_at_ms,created_at_ms,updated_at_ms)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"grc_{repository_id}",
                workspace,
                repository_id,
                repository_id,
                "owner/repo",
                "main",
                1 if private else 0,
                project,
                role,
                access_mode,
                __import__("json").dumps(allowed_paths, separators=(",", ":")),
                "available",
                1,
                1,
                1,
            ),
        )


@pytest.mark.asyncio
async def test_private_report_materialization_is_idempotent(tmp_path: Path):
    store, actor, workspace, project, board_id, analytics = _setup(tmp_path)
    github = FakeGitHub()
    try:
        run = await _completed_run(
            analytics,
            actor=actor,
            workspace=workspace,
            project=project,
            board_id=board_id,
        )
        _connection(store, workspace=workspace, project=project)
        service = AnalysisMaterializer(store, analytics, github)

        first = await service.materialize(
            actor_id=actor,
            workspace_id=workspace,
            run_id=run["id"],
        )
        assert first["status"] == "synced"
        assert first["path"] == f"docs/analysis/{run['id']}.md"
        assert first["reused"] is False
        assert len(github.writes) == 1
        assert github.writes[0]["text"] == run["result_markdown"]

        second = await service.materialize(
            actor_id=actor,
            workspace_id=workspace,
            run_id=run["id"],
        )
        assert second["status"] == "synced"
        assert second["reused"] is True
        assert len(github.writes) == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_public_repository_requires_explicit_owner_confirmation(tmp_path: Path):
    store, actor, workspace, project, board_id, analytics = _setup(tmp_path)
    github = FakeGitHub()
    try:
        run = await _completed_run(
            analytics,
            actor=actor,
            workspace=workspace,
            project=project,
            board_id=board_id,
        )
        _connection(
            store,
            workspace=workspace,
            project=project,
            private=False,
        )
        service = AnalysisMaterializer(store, analytics, github)
        blocked = await service.materialize(
            actor_id=actor,
            workspace_id=workspace,
            run_id=run["id"],
        )
        assert blocked["status"] == "confirmation_required"
        assert blocked["private"] is False
        assert github.writes == []

        confirmed = await service.materialize(
            actor_id=actor,
            workspace_id=workspace,
            run_id=run["id"],
            allow_public=True,
        )
        assert confirmed["status"] == "synced"
        assert len(github.writes) == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_github_failure_does_not_destroy_internal_report(tmp_path: Path):
    store, actor, workspace, project, board_id, analytics = _setup(tmp_path)
    github = FakeGitHub()
    github.fail_write = True
    try:
        run = await _completed_run(
            analytics,
            actor=actor,
            workspace=workspace,
            project=project,
            board_id=board_id,
        )
        _connection(store, workspace=workspace, project=project)
        service = AnalysisMaterializer(store, analytics, github)
        failed = await service.materialize(
            actor_id=actor,
            workspace_id=workspace,
            run_id=run["id"],
        )
        assert failed["status"] == "failed"
        assert failed["error_code"] == "GITHUB_UNAVAILABLE"
        internal = analytics.get_run(
            actor_id=actor,
            workspace_id=workspace,
            run_id=run["id"],
        )
        assert internal["status"] == "completed"
        assert internal["result_markdown"] == run["result_markdown"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_wrong_role_or_path_is_not_a_materialization_binding(tmp_path: Path):
    store, actor, workspace, project, board_id, analytics = _setup(tmp_path)
    github = FakeGitHub()
    try:
        run = await _completed_run(
            analytics,
            actor=actor,
            workspace=workspace,
            project=project,
            board_id=board_id,
        )
        _connection(
            store,
            workspace=workspace,
            project=project,
            role="project_docs",
            allowed_paths=["docs"],
        )
        service = AnalysisMaterializer(store, analytics, github)
        status = service.status(
            actor_id=actor,
            workspace_id=workspace,
            run_id=run["id"],
        )
        assert status["status"] == "not_configured"
        assert github.writes == []
    finally:
        store.close()


def test_repository_path_policy_is_prefix_bounded() -> None:
    connection = {"allowed_paths": ["docs/analysis"]}
    assert GitHubConnections.repository_path_allowed(
        connection, "docs/analysis/anr_123.md"
    )
    assert not GitHubConnections.repository_path_allowed(
        connection, "docs/analysis-private/anr_123.md"
    )
    assert not GitHubConnections.repository_path_allowed(
        connection, "docs/analysis/../secret.md"
    )
