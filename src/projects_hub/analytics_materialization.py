from __future__ import annotations

import time
from typing import Any

from .analytics import AnalyticsService
from .github_connections import GitHubConnections
from .store import DurableStore, StoreError


ANALYSIS_GITHUB_ROOT = "docs/analysis"


def _now_ms() -> int:
    return round(time.time() * 1000)


class AnalysisMaterializer:
    """Deterministically mirrors completed analysis Markdown to an approved GitHub target."""

    def __init__(
        self,
        store: DurableStore,
        analytics: AnalyticsService,
        github: GitHubConnections,
    ) -> None:
        self.store = store
        self.analytics = analytics
        self.github = github
        self._init_schema()

    def _init_schema(self) -> None:
        with self.store._lock:
            self.store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS analysis_materializations(
                    run_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    repository_id INTEGER NOT NULL,
                    full_name TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    path TEXT NOT NULL,
                    private INTEGER NOT NULL CHECK(private IN (0,1)),
                    status TEXT NOT NULL,
                    content_sha TEXT,
                    commit_sha TEXT,
                    error_code TEXT,
                    reused INTEGER NOT NULL DEFAULT 0 CHECK(reused IN (0,1)),
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS analysis_materializations_project_idx
                    ON analysis_materializations(project_id,updated_at_ms DESC);
                """
            )

    def _row(self, run_id: str) -> Any | None:
        return self.store.db.execute(
            "SELECT * FROM analysis_materializations WHERE run_id=?",
            (run_id,),
        ).fetchone()

    @staticmethod
    def _public_row(row: Any) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "project_id": row["project_id"],
            "repository_id": int(row["repository_id"]),
            "full_name": row["full_name"],
            "branch": row["branch"],
            "path": row["path"],
            "private": bool(row["private"]),
            "status": row["status"],
            "content_sha": row["content_sha"],
            "commit_sha": row["commit_sha"],
            "error_code": row["error_code"],
            "reused": bool(row["reused"]),
            "created_at_ms": int(row["created_at_ms"]),
            "updated_at_ms": int(row["updated_at_ms"]),
        }

    def _record(
        self,
        *,
        run_id: str,
        workspace_id: str,
        project_id: str,
        connection: dict[str, Any],
        path: str,
        status: str,
        content_sha: str | None = None,
        commit_sha: str | None = None,
        error_code: str | None = None,
        reused: bool = False,
    ) -> dict[str, Any]:
        now = _now_ms()
        with self.store._lock:
            old = self._row(run_id)
            if old and int(old["repository_id"]) != int(connection["repository_id"]):
                raise StoreError(
                    "ANALYSIS_MATERIALIZATION_TARGET_LOCKED",
                    "Analysis run is already bound to a different GitHub target",
                )
            self.store.db.execute(
                """INSERT INTO analysis_materializations(
                       run_id,workspace_id,project_id,repository_id,full_name,branch,path,
                       private,status,content_sha,commit_sha,error_code,reused,
                       created_at_ms,updated_at_ms)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET
                       status=excluded.status,
                       content_sha=COALESCE(excluded.content_sha,analysis_materializations.content_sha),
                       commit_sha=COALESCE(excluded.commit_sha,analysis_materializations.commit_sha),
                       error_code=excluded.error_code,
                       reused=excluded.reused,
                       updated_at_ms=excluded.updated_at_ms""",
                (
                    run_id,
                    workspace_id,
                    project_id,
                    int(connection["repository_id"]),
                    str(connection["full_name"]),
                    str(connection["default_branch"]),
                    path,
                    1 if connection["private"] else 0,
                    status,
                    content_sha,
                    commit_sha,
                    error_code,
                    1 if reused else 0,
                    int(old["created_at_ms"]) if old else now,
                    now,
                ),
            )
            row = self._row(run_id)
            assert row is not None
            return self._public_row(row)

    def _target_path(self, run_id: str) -> str:
        return f"{ANALYSIS_GITHUB_ROOT}/{run_id}.md"

    def _eligible_connections(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        project_id: str,
        path: str,
    ) -> list[dict[str, Any]]:
        items = self.store.list_repository_connections(actor_id, workspace_id)
        return [
            item
            for item in items
            if item.get("state") == "available"
            and item.get("installation_state") == "active"
            and item.get("project_id") == project_id
            and item.get("role") == "generated_artifacts"
            and item.get("access_mode") == "app_managed_write"
            and GitHubConnections.repository_path_allowed(item, path)
        ]

    def status(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        run = self.analytics.get_run(
            actor_id=actor_id,
            workspace_id=workspace_id,
            run_id=run_id,
        )
        with self.store._lock:
            row = self._row(run_id)
            if row:
                return self._public_row(row)
        path = self._target_path(run_id)
        eligible = self._eligible_connections(
            actor_id=actor_id,
            workspace_id=workspace_id,
            project_id=run["project_id"],
            path=path,
        )
        return {
            "run_id": run_id,
            "project_id": run["project_id"],
            "status": "not_configured" if not eligible else "ready",
            "path": path,
            "eligible_repository_ids": [
                int(item["repository_id"]) for item in eligible
            ],
        }

    async def materialize(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        run_id: str,
        repository_id: int | None = None,
        allow_public: bool = False,
    ) -> dict[str, Any]:
        run = self.analytics.get_run(
            actor_id=actor_id,
            workspace_id=workspace_id,
            run_id=run_id,
        )
        if run["status"] != "completed" or not str(run.get("result_markdown") or ""):
            raise StoreError("ANALYSIS_NOT_READY", "Analysis report is not ready")

        project_id = str(run["project_id"])
        self.store.project_access(
            actor_id,
            workspace_id,
            project_id,
            require_analyze=True,
        )
        path = self._target_path(run_id)
        eligible = self._eligible_connections(
            actor_id=actor_id,
            workspace_id=workspace_id,
            project_id=project_id,
            path=path,
        )
        with self.store._lock:
            existing_target = self._row(run_id)
        if repository_id is not None:
            eligible = [
                item
                for item in eligible
                if int(item["repository_id"]) == int(repository_id)
            ]
        elif existing_target is not None:
            eligible = [
                item
                for item in eligible
                if int(item["repository_id"]) == int(existing_target["repository_id"])
            ]
        if not eligible:
            return {
                "run_id": run_id,
                "project_id": project_id,
                "status": "not_configured",
                "path": path,
            }
        if len(eligible) > 1:
            raise StoreError(
                "ANALYSIS_GITHUB_TARGET_REQUIRED",
                "Choose one approved generated-artifacts repository",
            )
        connection = eligible[0]
        with self.store._lock:
            old = self._row(run_id)
            if old and int(old["repository_id"]) != int(connection["repository_id"]):
                raise StoreError(
                    "ANALYSIS_MATERIALIZATION_TARGET_LOCKED",
                    "Analysis run is already bound to a different GitHub target",
                )

        if not bool(connection["private"]):
            self.store.require_workspace_owner(actor_id, workspace_id)
            if not allow_public:
                return self._record(
                    run_id=run_id,
                    workspace_id=workspace_id,
                    project_id=project_id,
                    connection=connection,
                    path=path,
                    status="confirmation_required",
                )

        self._record(
            run_id=run_id,
            workspace_id=workspace_id,
            project_id=project_id,
            connection=connection,
            path=path,
            status="pending",
        )

        markdown = str(run["result_markdown"])
        expected_sha: str | None = None
        try:
            existing = await self.github.read_repository_path(
                actor_id=actor_id,
                workspace_id=workspace_id,
                repository_id=int(connection["repository_id"]),
                path=path,
            )
            if existing.get("kind") == "file":
                expected_sha = str(existing.get("sha") or "") or None
                if str(existing.get("text") or "") == markdown:
                    return self._record(
                        run_id=run_id,
                        workspace_id=workspace_id,
                        project_id=project_id,
                        connection=connection,
                        path=path,
                        status="synced",
                        content_sha=expected_sha,
                        reused=True,
                    )
        except StoreError as exc:
            if exc.code != "GITHUB_NOT_FOUND":
                return self._record(
                    run_id=run_id,
                    workspace_id=workspace_id,
                    project_id=project_id,
                    connection=connection,
                    path=path,
                    status="failed",
                    error_code=exc.code,
                )

        try:
            written = await self.github.write_repository_text(
                actor_id=actor_id,
                workspace_id=workspace_id,
                repository_id=int(connection["repository_id"]),
                path=path,
                text=markdown,
                message=f"Materialize Projects Hub analysis {run_id}",
                expected_sha=expected_sha,
            )
        except StoreError as exc:
            return self._record(
                run_id=run_id,
                workspace_id=workspace_id,
                project_id=project_id,
                connection=connection,
                path=path,
                status="failed",
                error_code=exc.code,
            )

        return self._record(
            run_id=run_id,
            workspace_id=workspace_id,
            project_id=project_id,
            connection=connection,
            path=path,
            status="synced",
            content_sha=str(written.get("content_sha") or "") or None,
            commit_sha=str(written.get("commit_sha") or "") or None,
            reused=False,
        )
