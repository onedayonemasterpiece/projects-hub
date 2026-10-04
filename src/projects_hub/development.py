from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any

from .devcoveer_client import DevCoveerClient, DevCoveerError
from .readiness import ReadinessService
from .store import DurableStore, StoreError

ACTIVE_EXECUTION_STATES = {"starting", "running"}
TERMINAL_EXECUTION_STATES = {"completed", "failed", "cancelled"}
DEFAULT_CODEX_PROFILE = "gpt-6.1-medium"
DEVCOVEER_PROJECT = "projects-hub"


def _now_ms() -> int:
    return round(time.time() * 1000)


class DevelopmentService:
    """Owner-only execution layer over the existing durable project backlog."""

    def __init__(
        self,
        store: DurableStore,
        readiness: ReadinessService,
        *,
        devcoveer: DevCoveerClient | None = None,
    ) -> None:
        self.store = store
        self.readiness = readiness
        self.devcoveer = devcoveer or DevCoveerClient()
        with self.store._lock:
            self.store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_executions(
                    id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL REFERENCES actors(id),
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    task_ids_json TEXT NOT NULL,
                    project_hint TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model_profile TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    prompt_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT 'queued',
                    phase_detail TEXT NOT NULL DEFAULT '',
                    devcoveer_task_id TEXT,
                    quota_remaining_percent REAL,
                    result_summary TEXT NOT NULL DEFAULT '',
                    error_code TEXT,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    started_at_ms INTEGER,
                    finished_at_ms INTEGER
                );
                CREATE INDEX IF NOT EXISTS task_executions_owner_idx
                    ON task_executions(actor_id,workspace_id,status,updated_at_ms DESC);
                CREATE INDEX IF NOT EXISTS task_executions_project_idx
                    ON task_executions(actor_id,workspace_id,project_id,updated_at_ms DESC);
                """
            )
            columns = {
                str(row["name"])
                for row in self.store.db.execute(
                    "PRAGMA table_info(task_executions)"
                ).fetchall()
            }
            if "phase" not in columns:
                self.store.db.execute(
                    "ALTER TABLE task_executions ADD COLUMN phase TEXT NOT NULL DEFAULT 'queued'"
                )
            if "phase_detail" not in columns:
                self.store.db.execute(
                    "ALTER TABLE task_executions ADD COLUMN phase_detail TEXT NOT NULL DEFAULT ''"
                )

    def _authorize_owner(self, actor_id: str, workspace_id: str) -> None:
        self.store.require_platform_owner(actor_id)
        self.store.require_workspace_owner(actor_id, workspace_id)

    async def codex_status(self, *, actor_id: str, workspace_id: str) -> dict[str, Any]:
        self._authorize_owner(actor_id, workspace_id)
        try:
            combined = await self.devcoveer.status()
        except DevCoveerError as exc:
            raise StoreError("DEVCOVEER_UNAVAILABLE", str(exc)) from exc
        payload = combined.get("quota") if isinstance(combined.get("quota"), dict) else {}
        admission = payload.get("admission") if isinstance(payload.get("admission"), dict) else {}
        profile = payload.get("default_profile") if isinstance(payload.get("default_profile"), dict) else {}
        models = []
        for item in combined.get("models", []):
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            models.append({
                "id": item.get("id"),
                "display_name": item.get("displayName"),
                "reasoning_efforts": list(item.get("reasoningEfforts") or []),
                "default_reasoning_effort": item.get("defaultReasoningEffort"),
                "availability": item.get("availability"),
            })
        return {
            "status": payload.get("status"),
            "observed_at": payload.get("observed_at"),
            "remaining_percent": admission.get("effective_remaining_percent"),
            "eligible": admission.get("eligible") is True,
            "reserve_percent": admission.get("reserve_percent"),
            "reason": admission.get("reason"),
            "profile": {
                "requested": profile.get("requested"),
                "model": profile.get("model"),
                "reasoning_effort": profile.get("reasoning_effort"),
                "catalog_available": profile.get("catalog_available") is True,
            },
            "models": models,
        }

    def _selected_tasks(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        task_ids: list[str],
    ) -> tuple[list[dict[str, Any]], str]:
        clean_ids = list(dict.fromkeys(str(item).strip() for item in task_ids if str(item).strip()))
        if not 1 <= len(clean_ids) <= 5:
            raise StoreError("INVALID_ARGUMENT", "Choose between 1 and 5 backlog tasks")
        placeholders = ",".join("?" for _ in clean_ids)
        with self.store._lock:
            self.readiness._authorize(actor_id, workspace_id, None)
            rows = self.store.db.execute(
                f"""SELECT * FROM tasks
                    WHERE actor_id=? AND workspace_id=? AND id IN ({placeholders})""",
                (actor_id, workspace_id, *clean_ids),
            ).fetchall()
        tasks = [dict(row) for row in rows]
        by_id = {item["id"]: item for item in tasks}
        if set(by_id) != set(clean_ids):
            raise StoreError("TASK_NOT_FOUND", "One or more backlog tasks are unavailable")
        ordered = [by_id[item] for item in clean_ids]
        project_ids = {str(item.get("project_id") or "") for item in ordered}
        if "" in project_ids or len(project_ids) != 1:
            raise StoreError(
                "DEVELOPMENT_PROJECT_REQUIRED",
                "Selected backlog tasks must belong to one project",
            )
        if any(item.get("state") in {"done", "rejected"} for item in ordered):
            raise StoreError(
                "DEVELOPMENT_TASK_TERMINAL",
                "Completed or rejected backlog tasks cannot be started",
            )
        return ordered, next(iter(project_ids))

    def _project_name(self, actor_id: str, workspace_id: str, project_id: str) -> str:
        for project in self.store.list_projects(actor_id, workspace_id):
            if project["id"] == project_id:
                return str(project["name"])
        raise StoreError("PROJECT_NOT_FOUND", "Project is not available")

    def _project_hint(
        self,
        actor_id: str,
        workspace_id: str,
        project_id: str,
        project_name: str,
    ) -> str:
        connections = [
            item
            for item in self.store.list_repository_connections(actor_id, workspace_id)
            if item.get("project_id") == project_id
            and item.get("state") == "available"
            and item.get("installation_state") == "active"
            and item.get("access_mode") == "app_managed_write"
        ]
        if len(connections) == 1:
            return str(connections[0]["full_name"]).rsplit("/", 1)[-1]
        normalized = "".join(ch.lower() for ch in project_name if ch.isalnum())
        exact = [
            item for item in connections
            if "".join(
                ch.lower()
                for ch in str(item.get("full_name") or "").rsplit("/", 1)[-1]
                if ch.isalnum()
            ) == normalized
        ]
        if len(exact) == 1:
            return str(exact[0]["full_name"]).rsplit("/", 1)[-1]
        return project_name

    @staticmethod
    def _prompt(project_name: str, tasks: list[dict[str, Any]]) -> str:
        rows = []
        for index, task in enumerate(tasks, 1):
            title = str(task.get("title") or "").strip()
            description = str(task.get("description") or "").strip()
            rows.append(
                f"{index}. [{task['id']}] {title}"
                + (f"\n   {description}" if description else "")
            )
        backlog = "\n".join(rows)
        return f"""Implement the following owner-approved Projects Hub backlog work for project {project_name}.

{backlog}

Work to a concrete, verifiable product result. Preserve the project's .devcoveer requirements and existing architecture; reuse proven components rather than introducing parallel mechanisms. Keep scope to these backlog items. Add or update tests. Use the repository's normal CI and delivery path. If native Android changes are required, finish through the normal signed Android release/update path; if only backend/PWA changes are required, deploy and verify those instead. Do not weaken critical requirements. At the end report what was actually delivered, verification performed, deployment/release state, and any genuine blocker that remains."""

    def _execution_public(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        item["task_ids"] = json.loads(item.pop("task_ids_json"))
        item.pop("prompt", None)
        item.pop("prompt_sha256", None)
        return item

    def _execution_row(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        execution_id: str,
    ) -> Any:
        with self.store._lock:
            row = self.store.db.execute(
                """SELECT * FROM task_executions
                   WHERE id=? AND actor_id=? AND workspace_id=?""",
                (execution_id, actor_id, workspace_id),
            ).fetchone()
        if not row:
            raise StoreError("DEVELOPMENT_EXECUTION_NOT_FOUND", "Development execution is unavailable")
        return row

    async def start(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        task_ids: list[str],
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        self._authorize_owner(actor_id, workspace_id)
        tasks, project_id = self._selected_tasks(
            actor_id=actor_id,
            workspace_id=workspace_id,
            task_ids=task_ids,
        )
        with self.store._lock:
            active = self.store.db.execute(
                """SELECT id FROM task_executions
                   WHERE actor_id=? AND status IN ('starting','running')
                   ORDER BY created_at_ms DESC LIMIT 1""",
                (actor_id,),
            ).fetchone()
        if active:
            raise StoreError(
                "DEVELOPMENT_EXECUTION_ACTIVE",
                "Another owner development execution is already active",
            )

        status = await self.codex_status(actor_id=actor_id, workspace_id=workspace_id)
        remaining = status.get("remaining_percent")
        profile = status.get("profile") or {}
        if (
            status.get("eligible") is not True
            or not isinstance(remaining, (int, float))
            or float(remaining) <= 10.0
        ):
            raise StoreError(
                "CODEX_CAPACITY_RESERVED",
                "Native Codex capacity is unavailable or at the 10% reserve",
            )

        available = {
            str(item["id"]): item
            for item in status.get("models", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
            and item.get("availability") != "eol"
        }
        selected_model = str(model or "").strip()
        selected_effort = str(reasoning_effort or "").strip()
        if not selected_model and profile.get("catalog_available") is True:
            selected_model = str(profile.get("model") or "").strip()
            selected_effort = selected_effort or str(profile.get("reasoning_effort") or "").strip()
        if not selected_model:
            return {
                "status": "model_selection_required",
                "remaining_percent": float(remaining),
                "models": list(available.values()),
                "profile": profile,
            }
        record = available.get(selected_model)
        if record is None:
            raise StoreError("CODEX_MODEL_UNAVAILABLE", "Requested native Codex model is unavailable")
        efforts = {str(value) for value in record.get("reasoning_efforts") or []}
        if not selected_effort:
            selected_effort = str(record.get("default_reasoning_effort") or "").strip()
        if selected_effort not in efforts:
            raise StoreError(
                "CODEX_REASONING_UNAVAILABLE",
                "Requested reasoning effort is unavailable for this model",
            )

        project_name = self._project_name(actor_id, workspace_id, project_id)
        project_hint = self._project_hint(
            actor_id,
            workspace_id,
            project_id,
            project_name,
        )
        prompt = self._prompt(project_name, tasks)
        now = _now_ms()
        execution_id = "devrun_" + uuid.uuid4().hex
        task_ids_json = json.dumps([item["id"] for item in tasks], separators=(",", ":"))
        with self.store._lock:
            self.store.db.execute(
                """INSERT INTO task_executions(
                       id,actor_id,workspace_id,project_id,task_ids_json,project_hint,
                       provider,model_profile,prompt,prompt_sha256,status,
                       quota_remaining_percent,result_summary,error_code,
                       created_at_ms,updated_at_ms,started_at_ms,finished_at_ms
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?, ?,? ,?, ?,?,?,?)""",
                (
                    execution_id,
                    actor_id,
                    workspace_id,
                    project_id,
                    task_ids_json,
                    project_hint,
                    "codex",
                    selected_model + ":" + selected_effort,
                    prompt,
                    hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "starting",
                    float(remaining),
                    "",
                    None,
                    now,
                    now,
                    None,
                    None,
                ),
            )

        try:
            result = await self.devcoveer.start_codex_task(
                project=project_hint,
                prompt=prompt,
                model=selected_model,
                reasoning_effort=selected_effort,
            )
            devcoveer_task_id = str(
                result.get("taskId")
                or result.get("taskReference")
                or ""
            ).strip()
            if not devcoveer_task_id:
                raise StoreError("DEVCOVEER_INVALID_RESPONSE", "DevCoveer did not return a task id")
            now = _now_ms()
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='running',devcoveer_task_id=?,started_at_ms=?,updated_at_ms=?
                       WHERE id=?""",
                    (devcoveer_task_id, now, now, execution_id),
                )
                for task in tasks:
                    if task.get("state") not in {"accepted", "done"}:
                        self.store.db.execute(
                            "UPDATE tasks SET state='accepted',updated_at_ms=? WHERE id=?",
                            (now, task["id"]),
                        )
        except Exception as exc:
            now = _now_ms()
            code = exc.code if isinstance(exc, StoreError) else type(exc).__name__
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='failed',error_code=?,finished_at_ms=?,updated_at_ms=?
                       WHERE id=?""",
                    (str(code)[:120], now, now, execution_id),
                )
            raise

        return await self.status(
            actor_id=actor_id,
            workspace_id=workspace_id,
            execution_id=execution_id,
            sync=False,
        )

    @staticmethod
    def _map_task_status(value: Any) -> str:
        clean = str(value or "").strip().lower()
        if clean in {"completed", "succeeded", "success"}:
            return "completed"
        if clean in {"failed", "error"}:
            return "failed"
        if clean in {"cancelled", "canceled"}:
            return "cancelled"
        return "running"

    async def status(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        execution_id: str | None = None,
        sync: bool = True,
    ) -> dict[str, Any]:
        self._authorize_owner(actor_id, workspace_id)
        if execution_id is None:
            with self.store._lock:
                row = self.store.db.execute(
                    """SELECT * FROM task_executions
                       WHERE actor_id=? AND workspace_id=?
                       ORDER BY created_at_ms DESC LIMIT 1""",
                    (actor_id, workspace_id),
                ).fetchone()
            if not row:
                return {"execution": None}
        else:
            row = self._execution_row(
                actor_id=actor_id,
                workspace_id=workspace_id,
                execution_id=execution_id,
            )

        item = dict(row)
        if (
            sync
            and item.get("status") in ACTIVE_EXECUTION_STATES
            and item.get("devcoveer_task_id")
        ):
            result = await self.devcoveer.read_task(
                str(item["devcoveer_task_id"]),
                project=str(item["project_hint"]),
                detail="full",
            )
            next_status = self._map_task_status(
                result.get("status")
                or result.get("executionStatus")
                or (result.get("task") or {}).get("status")
            )
            summary = str(
                result.get("finalResponse")
                or result.get("content")
                or ""
            ).strip()[:8000]
            now = _now_ms()
            finished = now if next_status in TERMINAL_EXECUTION_STATES else None
            error_code = None
            if next_status == "failed":
                error_code = str(
                    result.get("errorCategory")
                    or (result.get("task") or {}).get("errorCategory")
                    or "DEVCOVEER_TASK_FAILED"
                )[:120]
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status=?,result_summary=?,error_code=?,
                           finished_at_ms=COALESCE(?,finished_at_ms),updated_at_ms=?
                       WHERE id=?""",
                    (
                        next_status,
                        summary,
                        error_code,
                        finished,
                        now,
                        item["id"],
                    ),
                )
            row = self._execution_row(
                actor_id=actor_id,
                workspace_id=workspace_id,
                execution_id=item["id"],
            )

        public = self._execution_public(row)
        public["update_check_recommended"] = public["status"] == "completed"
        return {"execution": public}

    def list_executions(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        project_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self._authorize_owner(actor_id, workspace_id)
        bounded = max(1, min(int(limit), 50))
        with self.store._lock:
            if project_id:
                rows = self.store.db.execute(
                    """SELECT * FROM task_executions
                       WHERE actor_id=? AND workspace_id=? AND project_id=?
                       ORDER BY created_at_ms DESC LIMIT ?""",
                    (actor_id, workspace_id, project_id, bounded),
                ).fetchall()
            else:
                rows = self.store.db.execute(
                    """SELECT * FROM task_executions
                       WHERE actor_id=? AND workspace_id=?
                       ORDER BY created_at_ms DESC LIMIT ?""",
                    (actor_id, workspace_id, bounded),
                ).fetchall()
        return [self._execution_public(row) for row in rows]
