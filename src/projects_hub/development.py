from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from typing import Any

from .devcoveer_client import (
    DevCoveerClient,
    DevCoveerError,
    DevCoveerUnknownOutcome,
)
from .readiness import ReadinessService
from .store import DurableStore, StoreError

ACTIVE_EXECUTION_STATES = {
    "starting",
    "running",
    "waiting_capacity",
    "dispatch_unknown",
}
TERMINAL_EXECUTION_STATES = {"completed", "failed", "cancelled", "needs_owner"}
ACTIVE_STAGE_STATES = {
    "dispatching",
    "dispatch_unknown",
    "running",
    "transitioning",
    "waiting_capacity",
}
DEFAULT_CODEX_PROFILE = "gpt-6.1-medium"
QUALITY_MODEL = "gpt-6-astra"
QUALITY_EFFORT = "high"
MAX_REWORK_CYCLES = 2


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
        self._driver_task: asyncio.Task[None] | None = None
        self._advance_lock = asyncio.Lock()
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
                    finished_at_ms INTEGER,
                    active_slot INTEGER,
                    repository_full_name TEXT
                );
                CREATE INDEX IF NOT EXISTS task_executions_owner_idx
                    ON task_executions(actor_id,workspace_id,status,updated_at_ms DESC);
                CREATE INDEX IF NOT EXISTS task_executions_project_idx
                    ON task_executions(actor_id,workspace_id,project_id,updated_at_ms DESC);
                CREATE TABLE IF NOT EXISTS task_execution_stages(
                    id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL REFERENCES task_executions(id),
                    stage TEXT NOT NULL,
                    cycle INTEGER NOT NULL DEFAULT 0,
                    model TEXT NOT NULL,
                    reasoning_effort TEXT NOT NULL,
                    devcoveer_task_id TEXT,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    review_verdict TEXT,
                    token_usage_json TEXT,
                    started_at_ms INTEGER,
                    finished_at_ms INTEGER,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    dispatch_key TEXT,
                    dispatch_kind TEXT NOT NULL DEFAULT 'start',
                    transition_claimed_at_ms INTEGER
                );
                CREATE INDEX IF NOT EXISTS task_execution_stages_execution_idx
                    ON task_execution_stages(execution_id,created_at_ms);
                CREATE UNIQUE INDEX IF NOT EXISTS task_execution_stages_dispatch_idx
                    ON task_execution_stages(dispatch_key)
                    WHERE dispatch_key IS NOT NULL;
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
            if "quality_task_id" not in columns:
                self.store.db.execute(
                    "ALTER TABLE task_executions ADD COLUMN quality_task_id TEXT"
                )
            if "implementation_task_id" not in columns:
                self.store.db.execute(
                    "ALTER TABLE task_executions ADD COLUMN implementation_task_id TEXT"
                )
            if "review_cycle" not in columns:
                self.store.db.execute(
                    "ALTER TABLE task_executions ADD COLUMN review_cycle INTEGER NOT NULL DEFAULT 0"
                )
            if "spec_path" not in columns:
                self.store.db.execute(
                    "ALTER TABLE task_executions ADD COLUMN spec_path TEXT"
                )

            if "active_slot" not in columns:
                self.store.db.execute(
                    "ALTER TABLE task_executions ADD COLUMN active_slot INTEGER"
                )
            if "repository_full_name" not in columns:
                self.store.db.execute(
                    "ALTER TABLE task_executions ADD COLUMN repository_full_name TEXT"
                )
            stage_columns = {
                str(row["name"])
                for row in self.store.db.execute(
                    "PRAGMA table_info(task_execution_stages)"
                ).fetchall()
            }
            if "dispatch_key" not in stage_columns:
                self.store.db.execute(
                    "ALTER TABLE task_execution_stages ADD COLUMN dispatch_key TEXT"
                )
            if "dispatch_kind" not in stage_columns:
                self.store.db.execute(
                    "ALTER TABLE task_execution_stages ADD COLUMN dispatch_kind TEXT NOT NULL DEFAULT 'start'"
                )
            if "transition_claimed_at_ms" not in stage_columns:
                self.store.db.execute(
                    "ALTER TABLE task_execution_stages ADD COLUMN transition_claimed_at_ms INTEGER"
                )
            self.store.db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS task_execution_stages_dispatch_idx
                   ON task_execution_stages(dispatch_key)
                   WHERE dispatch_key IS NOT NULL"""
            )
            self._migrate_legacy_executions_locked()
            self.store.db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS task_executions_owner_active_idx
                   ON task_executions(actor_id,active_slot)
                   WHERE active_slot=1"""
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

    def create_backlog_task(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        project_id: str,
        task_key: str,
        title: str,
        description: str = "",
        acceptance_criteria: list[str] | None = None,
    ) -> dict[str, Any]:
        self._authorize_owner(actor_id, workspace_id)
        if not self.store._project_row(workspace_id, project_id):
            raise StoreError("PROJECT_NOT_FOUND", "Project is not available")
        clean_title = title.strip()[:180]
        if not clean_title:
            raise StoreError("INVALID_ARGUMENT", "Backlog title is required")
        criteria = [
            str(item).strip()[:500]
            for item in (acceptance_criteria or [])
            if str(item).strip()
        ][:10]
        clean_description = description.strip()[:4000]
        if criteria:
            suffix = "\n\nAcceptance:\n" + "\n".join(
                f"- {item}" for item in criteria
            )
            clean_description = (clean_description + suffix).strip()[:6000]
        now = _now_ms()
        with self.store._lock:
            existing = self.store.db.execute(
                "SELECT * FROM tasks WHERE task_key=?",
                (task_key,),
            ).fetchone()
            if existing:
                return dict(existing)
            task_id = "tsk_" + uuid.uuid4().hex
            self.store.db.execute(
                """INSERT INTO tasks(
                       id,task_key,actor_id,workspace_id,project_id,event_card_id,
                       title,description,assignee_role,deadline,kind,state,
                       created_at_ms,updated_at_ms
                   ) VALUES(?,?,?,?,?,NULL,?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    task_key,
                    actor_id,
                    workspace_id,
                    project_id,
                    clean_title,
                    clean_description,
                    "development",
                    None,
                    "development",
                    "proposed",
                    now,
                    now,
                ),
            )
            return dict(
                self.store.db.execute(
                    "SELECT * FROM tasks WHERE id=?",
                    (task_id,),
                ).fetchone()
            )

    def list_backlog(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        project_id: str | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        self._authorize_owner(actor_id, workspace_id)
        bounded = max(1, min(int(limit), 100))
        with self.store._lock:
            if project_id:
                if not self.store._project_row(workspace_id, project_id):
                    raise StoreError("PROJECT_NOT_FOUND", "Project is not available")
                rows = self.store.db.execute(
                    """SELECT * FROM tasks
                       WHERE actor_id=? AND workspace_id=? AND project_id=?
                         AND kind='development'
                       ORDER BY updated_at_ms DESC LIMIT ?""",
                    (actor_id, workspace_id, project_id, bounded),
                ).fetchall()
            else:
                rows = self.store.db.execute(
                    """SELECT * FROM tasks
                       WHERE actor_id=? AND workspace_id=? AND kind='development'
                       ORDER BY updated_at_ms DESC LIMIT ?""",
                    (actor_id, workspace_id, bounded),
                ).fetchall()
        return [dict(row) for row in rows]

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
                    WHERE actor_id=? AND workspace_id=? AND kind='development'
                      AND id IN ({placeholders})""",
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
    def _backlog_text(tasks: list[dict[str, Any]]) -> str:
        rows = []
        for index, task in enumerate(tasks, 1):
            title = str(task.get("title") or "").strip()
            description = str(task.get("description") or "").strip()
            rows.append(
                f"{index}. [{task['id']}] {title}"
                + (f"\n   {description}" if description else "")
            )
        return "\n".join(rows)

    @classmethod
    def _design_prompt(
        cls,
        project_name: str,
        tasks: list[dict[str, Any]],
        spec_path: str,
    ) -> str:
        return f"""You are the quality/design thread for an owner-approved development batch in {project_name}.

Backlog:
{cls._backlog_text(tasks)}

Do NOT implement product code, merge, deploy or release in this turn. Perform a systematic engineering analysis first: inspect the current code and project requirements, identify hidden constraints and edge cases, define regression risks, test cases and a concrete Definition of Done. Update relevant project documentation when needed and write the complete implementation brief to exactly:
{spec_path}

The brief must be sufficient for a separate implementation thread to work without guessing. Include when browser acceptance, Android emulator/device acceptance, CI, deployment or signed Android release are required. Keep scope to the selected backlog tasks and preserve .devcoveer critical requirements. End with a concise summary and the exact spec path."""

    @staticmethod
    def _implementation_prompt(project_name: str, spec_path: str) -> str:
        return f"""Implement the owner-approved development specification for {project_name} at:
{spec_path}

Read the specification and current repository state first. Implement the requested product change, add/update tests, and perform the required debugging and browser/emulator checks from the spec. Prepare a reviewable branch/PR and CI evidence, but DO NOT merge, deploy, publish a release or modify production yet. Stop when the change is ready for independent quality review. Report branch/PR, tests, debugging evidence and any blocker."""

    @staticmethod
    def _review_prompt(spec_path: str, cycle: int) -> str:
        return f"""Review cycle {cycle} for the implementation of:
{spec_path}

You are the same quality/design thread that produced the specification. Re-read the specification, inspect the current implementation diff/PR and verification evidence, and perform an independent acceptance/code review. Check edge cases, regressions, architecture/requirements compliance, tests and required browser/emulator evidence. Do NOT implement fixes and do NOT merge/deploy/release.

If the implementation is acceptable, end with the exact line:
REVIEW_VERDICT: ACCEPTED

If material fixes are required, update the specification with the concrete rework required and end with the exact line:
REVIEW_VERDICT: REWORK_REQUIRED

Before the verdict, give concise actionable findings."""

    @staticmethod
    def _rework_prompt(spec_path: str, review_summary: str, cycle: int) -> str:
        return f"""Rework cycle {cycle}. The independent quality thread found issues in the implementation of:
{spec_path}

Review findings:
{review_summary}

Read the updated specification and fix all material findings. Re-run the required tests/debugging/browser/emulator checks. Keep the existing implementation thread and scope. Do NOT merge, deploy or release. Stop when the change is again ready for independent review and report the updated evidence."""

    @staticmethod
    def _delivery_prompt(spec_path: str) -> str:
        return f"""Independent quality review ACCEPTED the implementation of:
{spec_path}

Now finish delivery using the repository's normal path. Merge/publish only the accepted implementation, run required CI, deploy and verify production when applicable. If native Android changed, produce the normal signed Android release/update manifest and verify the release; if only backend/PWA changed, deploy and verify that path instead. Do not broaden scope. Report the actual delivered version/release, production verification and any genuine blocker."""

    @staticmethod
    def _review_verdict(summary: str) -> str | None:
        upper = summary.upper()
        if "REVIEW_VERDICT: ACCEPTED" in upper:
            return "accepted"
        if "REVIEW_VERDICT: REWORK_REQUIRED" in upper:
            return "rework_required"
        return None

    @staticmethod
    def _token_usage(result: dict[str, Any]) -> dict[str, Any] | None:
        value = result.get("tokenUsage")
        if not isinstance(value, dict):
            latest = result.get("latestTurn")
            value = latest.get("tokenUsage") if isinstance(latest, dict) else None
        if not isinstance(value, dict):
            return None
        clean: dict[str, Any] = {}
        for key in (
            "inputTokens",
            "cachedInputTokens",
            "outputTokens",
            "reasoningOutputTokens",
            "totalTokens",
        ):
            raw = value.get(key)
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                clean[key] = raw
        return clean or None

    def _record_stage(
        self,
        *,
        execution_id: str,
        stage: str,
        cycle: int,
        model: str,
        reasoning_effort: str,
        devcoveer_task_id: str,
    ) -> None:
        now = _now_ms()
        with self.store._lock:
            self.store.db.execute(
                """INSERT INTO task_execution_stages(
                       id,execution_id,stage,cycle,model,reasoning_effort,
                       devcoveer_task_id,status,summary,review_verdict,
                       token_usage_json,started_at_ms,finished_at_ms,
                       created_at_ms,updated_at_ms
                   ) VALUES(?,?,?,?,?,?,?,'running','',NULL,NULL,?,NULL,?,?)""",
                (
                    "devstage_" + uuid.uuid4().hex,
                    execution_id,
                    stage,
                    int(cycle),
                    model,
                    reasoning_effort,
                    devcoveer_task_id,
                    now,
                    now,
                    now,
                ),
            )

    def _active_stage(self, execution_id: str) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store.db.execute(
                """SELECT * FROM task_execution_stages
                   WHERE execution_id=? AND status='running'
                   ORDER BY created_at_ms DESC LIMIT 1""",
                (execution_id,),
            ).fetchone()
        return dict(row) if row else None

    def _finish_stage(
        self,
        *,
        stage_id: str,
        status: str,
        summary: str,
        review_verdict: str | None = None,
        token_usage: dict[str, Any] | None = None,
    ) -> None:
        now = _now_ms()
        with self.store._lock:
            self.store.db.execute(
                """UPDATE task_execution_stages
                   SET status=?,summary=?,review_verdict=?,token_usage_json=?,
                       finished_at_ms=?,updated_at_ms=?
                   WHERE id=?""",
                (
                    status,
                    summary[:12000],
                    review_verdict,
                    json.dumps(token_usage, separators=(",", ":")) if token_usage else None,
                    now,
                    now,
                    stage_id,
                ),
            )

    async def _require_stage_capacity(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        model: str,
        reasoning_effort: str,
    ) -> float:
        status = await self.codex_status(actor_id=actor_id, workspace_id=workspace_id)
        remaining = status.get("remaining_percent")
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
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item.get("availability") != "eol"
        }
        record = available.get(model)
        if record is None:
            raise StoreError("CODEX_MODEL_UNAVAILABLE", f"{model} is unavailable")
        efforts = {str(value) for value in record.get("reasoning_efforts") or []}
        if reasoning_effort not in efforts:
            raise StoreError(
                "CODEX_REASONING_UNAVAILABLE",
                f"{reasoning_effort} is unavailable for {model}",
            )
        return float(remaining)

    def _execution_public(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        item["task_ids"] = json.loads(item.pop("task_ids_json"))
        item.pop("prompt", None)
        item.pop("prompt_sha256", None)
        with self.store._lock:
            stage_rows = self.store.db.execute(
                """SELECT * FROM task_execution_stages
                   WHERE execution_id=? ORDER BY created_at_ms""",
                (item["id"],),
            ).fetchall()
        stages = []
        usage_by_model: dict[str, dict[str, int]] = {}
        for stage_row in stage_rows:
            stage = dict(stage_row)
            raw_usage = stage.pop("token_usage_json", None)
            usage = None
            if isinstance(raw_usage, str) and raw_usage:
                try:
                    parsed = json.loads(raw_usage)
                    usage = parsed if isinstance(parsed, dict) else None
                except ValueError:
                    usage = None
            stage["token_usage"] = usage
            stages.append(stage)
            if usage:
                model = str(stage.get("model") or "")
                bucket = usage_by_model.setdefault(model, {})
                for key, value in usage.items():
                    if isinstance(value, int) and not isinstance(value, bool):
                        bucket[key] = bucket.get(key, 0) + value
        item["stages"] = stages
        item["token_usage_by_model"] = usage_by_model
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

        quality_record = available.get(QUALITY_MODEL)
        quality_efforts = {
            str(value) for value in (quality_record or {}).get("reasoning_efforts") or []
        }
        if quality_record is None or QUALITY_EFFORT not in quality_efforts:
            raise StoreError(
                "QUALITY_MODEL_UNAVAILABLE",
                f"{QUALITY_MODEL}:{QUALITY_EFFORT} is required for design/review",
            )

        project_name = self._project_name(actor_id, workspace_id, project_id)
        project_hint = self._project_hint(
            actor_id,
            workspace_id,
            project_id,
            project_name,
        )
        now = _now_ms()
        execution_id = "devrun_" + uuid.uuid4().hex
        spec_path = f"docs/prompts/owner-development-{execution_id}.md"
        prompt = self._design_prompt(project_name, tasks, spec_path)
        task_ids_json = json.dumps([item["id"] for item in tasks], separators=(",", ":"))
        with self.store._lock:
            self.store.db.execute(
                """INSERT INTO task_executions(
                       id,actor_id,workspace_id,project_id,task_ids_json,project_hint,
                       provider,model_profile,prompt,prompt_sha256,status,phase,phase_detail,
                       quota_remaining_percent,result_summary,error_code,
                       created_at_ms,updated_at_ms,started_at_ms,finished_at_ms,
                       review_cycle,spec_path
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'queued','Ожидает quality design',
                            ?,?,?, ?,?,?,NULL,0,?)""",
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
                    now,
                    spec_path,
                ),
            )

        try:
            result = await self.devcoveer.start_codex_task(
                project=project_hint,
                prompt=prompt,
                model=QUALITY_MODEL,
                reasoning_effort=QUALITY_EFFORT,
            )
            quality_task_id = str(
                result.get("taskId")
                or result.get("taskReference")
                or ""
            ).strip()
            if not quality_task_id:
                raise StoreError("DEVCOVEER_INVALID_RESPONSE", "DevCoveer did not return a quality task id")
            now = _now_ms()
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='running',phase='designing',
                           phase_detail='Сильная модель проектирует задачу',
                           quality_task_id=?,devcoveer_task_id=?,
                           started_at_ms=?,updated_at_ms=?
                       WHERE id=?""",
                    (quality_task_id, quality_task_id, now, now, execution_id),
                )
                for task in tasks:
                    if task.get("state") not in {"accepted", "done"}:
                        self.store.db.execute(
                            "UPDATE tasks SET state='accepted',updated_at_ms=? WHERE id=?",
                            (now, task["id"]),
                        )
            self._record_stage(
                execution_id=execution_id,
                stage="design",
                cycle=0,
                model=QUALITY_MODEL,
                reasoning_effort=QUALITY_EFFORT,
                devcoveer_task_id=quality_task_id,
            )
        except Exception as exc:
            now = _now_ms()
            code = exc.code if isinstance(exc, StoreError) else type(exc).__name__
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='failed',phase='failed',
                           phase_detail='Не удалось запустить quality design',
                           error_code=?,finished_at_ms=?,updated_at_ms=?
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
    def _phase_from_result(result: dict[str, Any], status: str) -> tuple[str, str]:
        if status == "completed":
            return "ready", "Готово"
        if status in {"failed", "cancelled"}:
            return status, "Исполнение завершилось ошибкой" if status == "failed" else "Исполнение отменено"

        phase = str(result.get("progressPhase") or "").strip().lower()
        labels = {
            "preparing": "Codex готовит запуск",
            "planning": "Codex анализирует и планирует",
            "implementing": "Codex вносит изменения",
            "testing": "Идут тесты",
            "ci": "Проверяется CI",
            "publishing": "Изменения публикуются",
            "releasing": "Собирается релиз",
            "deploying": "Идёт развёртывание",
            "ready": "Готово",
            "failed": "Исполнение завершилось ошибкой",
            "cancelled": "Исполнение отменено",
        }
        if phase in labels:
            return phase, labels[phase]
        return "planning", "Codex анализирует и планирует"

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
        if not sync or item.get("status") not in ACTIVE_EXECUTION_STATES:
            public = self._execution_public(row)
            public["update_check_recommended"] = public["status"] == "completed"
            return {"execution": public}

        stage = self._active_stage(str(item["id"]))
        if stage is None:
            now = _now_ms()
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='blocked',phase='blocked',
                           phase_detail='Нет активной стадии исполнения',
                           error_code='DEVELOPMENT_STAGE_MISSING',
                           finished_at_ms=?,updated_at_ms=?
                       WHERE id=?""",
                    (now, now, item["id"]),
                )
            row = self._execution_row(
                actor_id=actor_id,
                workspace_id=workspace_id,
                execution_id=item["id"],
            )
            public = self._execution_public(row)
            public["update_check_recommended"] = False
            return {"execution": public}

        result = await self.devcoveer.read_task(
            str(stage["devcoveer_task_id"]),
            project=str(item["project_hint"]),
            detail="summary",
        )
        turn_status = self._map_task_status(
            result.get("status")
            or result.get("executionStatus")
            or (result.get("task") or {}).get("status")
        )
        latest = result.get("latestTurn") if isinstance(result.get("latestTurn"), dict) else {}
        summary = str(
            result.get("finalResponse")
            or latest.get("finalResponse")
            or result.get("content")
            or ""
        ).strip()[:12000]
        token_usage = self._token_usage(result)

        if turn_status == "running":
            runtime_phase, runtime_detail = self._phase_from_result(result, "running")
            logical = str(stage["stage"])
            if logical == "design":
                phase, detail = "designing", "Сильная модель проектирует задачу и DoD"
            elif logical == "review":
                phase, detail = "reviewing", f"Сильная модель проводит ревью, цикл {stage['cycle']}"
            elif logical == "rework":
                if runtime_phase in {"testing", "ci"}:
                    phase, detail = runtime_phase, runtime_detail
                else:
                    phase, detail = "reworking", f"Разработчик исправляет замечания ревью, цикл {stage['cycle']}"
            elif logical == "delivery":
                if runtime_phase in {"ci", "publishing", "releasing", "deploying"}:
                    phase, detail = runtime_phase, runtime_detail
                else:
                    phase, detail = "delivering", "Принятая реализация публикуется и проверяется"
            else:
                phase, detail = runtime_phase, runtime_detail
                if phase in {"planning", "preparing"}:
                    phase, detail = "implementing", "Разработчик готовит реализацию"
            now = _now_ms()
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='running',phase=?,phase_detail=?,
                           updated_at_ms=? WHERE id=?""",
                    (phase, detail, now, item["id"]),
                )
            row = self._execution_row(
                actor_id=actor_id,
                workspace_id=workspace_id,
                execution_id=item["id"],
            )
            public = self._execution_public(row)
            public["update_check_recommended"] = False
            return {"execution": public}

        verdict = self._review_verdict(summary) if stage["stage"] == "review" else None
        self._finish_stage(
            stage_id=str(stage["id"]),
            status=turn_status,
            summary=summary,
            review_verdict=verdict,
            token_usage=token_usage,
        )

        now = _now_ms()
        if turn_status in {"failed", "cancelled"}:
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status=?,phase=?,phase_detail=?,result_summary=?,
                           error_code=?,finished_at_ms=?,updated_at_ms=?
                       WHERE id=?""",
                    (
                        turn_status,
                        turn_status,
                        "Стадия разработки завершилась ошибкой"
                        if turn_status == "failed"
                        else "Стадия разработки отменена",
                        summary,
                        "DEVCOVEER_TASK_FAILED" if turn_status == "failed" else "DEVCOVEER_TASK_CANCELLED",
                        now,
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
            public["update_check_recommended"] = False
            return {"execution": public}

        implementation_model, implementation_effort = str(item["model_profile"]).rsplit(":", 1)
        spec_path = str(item.get("spec_path") or "")
        cycle = int(stage.get("cycle") or 0)

        try:
            if stage["stage"] == "design":
                remaining = await self._require_stage_capacity(
                    actor_id=actor_id,
                    workspace_id=workspace_id,
                    model=implementation_model,
                    reasoning_effort=implementation_effort,
                )
                started = await self.devcoveer.start_codex_task(
                    project=str(item["project_hint"]),
                    prompt=self._implementation_prompt(
                        self._project_name(actor_id, workspace_id, str(item["project_id"])),
                        spec_path,
                    ),
                    model=implementation_model,
                    reasoning_effort=implementation_effort,
                )
                implementation_task_id = str(
                    started.get("taskId") or started.get("taskReference") or ""
                ).strip()
                if not implementation_task_id:
                    raise StoreError(
                        "DEVCOVEER_INVALID_RESPONSE",
                        "DevCoveer did not return an implementation task id",
                    )
                self._record_stage(
                    execution_id=str(item["id"]),
                    stage="implementation",
                    cycle=0,
                    model=implementation_model,
                    reasoning_effort=implementation_effort,
                    devcoveer_task_id=implementation_task_id,
                )
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE task_executions
                           SET status='running',phase='implementing',
                               phase_detail='Разработчик реализует принятую постановку',
                               implementation_task_id=?,devcoveer_task_id=?,
                               quota_remaining_percent=?,result_summary=?,
                               updated_at_ms=? WHERE id=?""",
                        (
                            implementation_task_id,
                            implementation_task_id,
                            remaining,
                            summary,
                            now,
                            item["id"],
                        ),
                    )

            elif stage["stage"] in {"implementation", "rework"}:
                remaining = await self._require_stage_capacity(
                    actor_id=actor_id,
                    workspace_id=workspace_id,
                    model=QUALITY_MODEL,
                    reasoning_effort=QUALITY_EFFORT,
                )
                quality_task_id = str(item.get("quality_task_id") or "").strip()
                if not quality_task_id:
                    raise StoreError("DEVELOPMENT_STAGE_MISSING", "Quality thread is unavailable")
                await self.devcoveer.continue_codex_task(
                    quality_task_id,
                    project=str(item["project_hint"]),
                    prompt=self._review_prompt(spec_path, cycle),
                    access="read",
                    model=QUALITY_MODEL,
                    reasoning_effort=QUALITY_EFFORT,
                )
                self._record_stage(
                    execution_id=str(item["id"]),
                    stage="review",
                    cycle=cycle,
                    model=QUALITY_MODEL,
                    reasoning_effort=QUALITY_EFFORT,
                    devcoveer_task_id=quality_task_id,
                )
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE task_executions
                           SET status='running',phase='reviewing',
                               phase_detail=?,devcoveer_task_id=?,
                               quota_remaining_percent=?,result_summary=?,
                               updated_at_ms=? WHERE id=?""",
                        (
                            f"Сильная модель принимает реализацию, цикл {cycle}",
                            quality_task_id,
                            remaining,
                            summary,
                            now,
                            item["id"],
                        ),
                    )

            elif stage["stage"] == "review":
                if verdict == "accepted":
                    remaining = await self._require_stage_capacity(
                        actor_id=actor_id,
                        workspace_id=workspace_id,
                        model=implementation_model,
                        reasoning_effort=implementation_effort,
                    )
                    implementation_task_id = str(item.get("implementation_task_id") or "").strip()
                    if not implementation_task_id:
                        raise StoreError("DEVELOPMENT_STAGE_MISSING", "Implementation thread is unavailable")
                    await self.devcoveer.continue_codex_task(
                        implementation_task_id,
                        project=str(item["project_hint"]),
                        prompt=self._delivery_prompt(spec_path),
                        access="write",
                        model=implementation_model,
                        reasoning_effort=implementation_effort,
                    )
                    self._record_stage(
                        execution_id=str(item["id"]),
                        stage="delivery",
                        cycle=cycle,
                        model=implementation_model,
                        reasoning_effort=implementation_effort,
                        devcoveer_task_id=implementation_task_id,
                    )
                    with self.store._lock:
                        self.store.db.execute(
                            """UPDATE task_executions
                               SET status='running',phase='delivering',
                                   phase_detail='Ревью принято; идёт поставка',
                                   devcoveer_task_id=?,quota_remaining_percent=?,
                                   result_summary=?,updated_at_ms=? WHERE id=?""",
                            (
                                implementation_task_id,
                                remaining,
                                summary,
                                now,
                                item["id"],
                            ),
                        )
                elif verdict == "rework_required":
                    if cycle >= MAX_REWORK_CYCLES:
                        with self.store._lock:
                            self.store.db.execute(
                                """UPDATE task_executions
                                   SET status='blocked',phase='needs_owner',
                                       phase_detail='После двух циклов ревью остались существенные замечания',
                                       result_summary=?,error_code='REVIEW_REWORK_LIMIT',
                                       finished_at_ms=?,updated_at_ms=? WHERE id=?""",
                                (summary, now, now, item["id"]),
                            )
                    else:
                        next_cycle = cycle + 1
                        remaining = await self._require_stage_capacity(
                            actor_id=actor_id,
                            workspace_id=workspace_id,
                            model=implementation_model,
                            reasoning_effort=implementation_effort,
                        )
                        implementation_task_id = str(item.get("implementation_task_id") or "").strip()
                        if not implementation_task_id:
                            raise StoreError("DEVELOPMENT_STAGE_MISSING", "Implementation thread is unavailable")
                        await self.devcoveer.continue_codex_task(
                            implementation_task_id,
                            project=str(item["project_hint"]),
                            prompt=self._rework_prompt(spec_path, summary, next_cycle),
                            access="write",
                            model=implementation_model,
                            reasoning_effort=implementation_effort,
                        )
                        self._record_stage(
                            execution_id=str(item["id"]),
                            stage="rework",
                            cycle=next_cycle,
                            model=implementation_model,
                            reasoning_effort=implementation_effort,
                            devcoveer_task_id=implementation_task_id,
                        )
                        with self.store._lock:
                            self.store.db.execute(
                                """UPDATE task_executions
                                   SET status='running',phase='reworking',
                                       phase_detail=?,devcoveer_task_id=?,
                                       quota_remaining_percent=?,review_cycle=?,
                                       result_summary=?,updated_at_ms=? WHERE id=?""",
                                (
                                    f"Исправление замечаний, цикл {next_cycle}",
                                    implementation_task_id,
                                    remaining,
                                    next_cycle,
                                    summary,
                                    now,
                                    item["id"],
                                ),
                            )
                else:
                    with self.store._lock:
                        self.store.db.execute(
                            """UPDATE task_executions
                               SET status='blocked',phase='needs_owner',
                                   phase_detail='Ревью завершилось без однозначного verdict',
                                   result_summary=?,error_code='REVIEW_VERDICT_MISSING',
                                   finished_at_ms=?,updated_at_ms=? WHERE id=?""",
                            (summary, now, now, item["id"]),
                        )

            elif stage["stage"] == "delivery":
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE task_executions
                           SET status='completed',phase='ready',
                               phase_detail='Готово и поставлено',
                               result_summary=?,error_code=NULL,
                               finished_at_ms=?,updated_at_ms=? WHERE id=?""",
                        (summary, now, now, item["id"]),
                    )
                    for task_id in json.loads(item["task_ids_json"]):
                        self.store.db.execute(
                            "UPDATE tasks SET state='done',updated_at_ms=? WHERE id=?",
                            (now, task_id),
                        )
            else:
                raise StoreError("DEVELOPMENT_STAGE_INVALID", "Unknown development stage")

        except Exception as exc:
            now = _now_ms()
            code = exc.code if isinstance(exc, StoreError) else type(exc).__name__
            blocked = code in {
                "CODEX_CAPACITY_RESERVED",
                "CODEX_MODEL_UNAVAILABLE",
                "CODEX_REASONING_UNAVAILABLE",
            }
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status=?,phase=?,phase_detail=?,error_code=?,
                           result_summary=?,finished_at_ms=?,updated_at_ms=?
                       WHERE id=?""",
                    (
                        "blocked" if blocked else "failed",
                        "capacity_wait" if blocked else "failed",
                        "Следующая стадия ожидает доступной Codex capacity"
                        if blocked
                        else "Не удалось перейти к следующей стадии",
                        str(code)[:120],
                        summary,
                        now,
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


    async def close(self) -> None:
        await self.devcoveer.close()
