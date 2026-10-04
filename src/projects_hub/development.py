from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
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
                    repository_full_name TEXT,
                    request_key TEXT
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
            if "request_key" not in columns:
                self.store.db.execute(
                    "ALTER TABLE task_executions ADD COLUMN request_key TEXT"
                )
            self.store.db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS task_executions_request_key_idx
                   ON task_executions(actor_id,request_key)
                   WHERE request_key IS NOT NULL"""
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

    def _migrate_legacy_executions_locked(self) -> None:
        now = _now_ms()
        active_rows = self.store.db.execute(
            """SELECT * FROM task_executions
               WHERE status IN ('starting','running','waiting_capacity','dispatch_unknown')
               ORDER BY actor_id,created_at_ms DESC"""
        ).fetchall()
        newest_by_actor: set[str] = set()
        for row in active_rows:
            item = dict(row)
            actor_id = str(item["actor_id"])
            existing_stage = self.store.db.execute(
                "SELECT id FROM task_execution_stages WHERE execution_id=? LIMIT 1",
                (item["id"],),
            ).fetchone()
            if not existing_stage and item.get("devcoveer_task_id"):
                profile = str(item.get("model_profile") or "gpt-5.6-sol:medium")
                if ":" in profile:
                    model, effort = profile.rsplit(":", 1)
                else:
                    model, effort = profile, "medium"
                self.store.db.execute(
                    """INSERT OR IGNORE INTO task_execution_stages(
                           id,execution_id,stage,cycle,model,reasoning_effort,
                           devcoveer_task_id,status,summary,review_verdict,
                           token_usage_json,started_at_ms,finished_at_ms,
                           created_at_ms,updated_at_ms,dispatch_key,dispatch_kind,
                           transition_claimed_at_ms
                       ) VALUES(?,?,?,?,?,?,?,'running','',NULL,NULL,?,NULL,?,?,?,'existing',NULL)""",
                    (
                        "devstage_" + uuid.uuid4().hex,
                        item["id"],
                        "implementation",
                        0,
                        model,
                        effort,
                        item["devcoveer_task_id"],
                        item.get("started_at_ms") or item.get("created_at_ms") or now,
                        item.get("created_at_ms") or now,
                        now,
                        f"legacy:{item['id']}:implementation:0",
                    ),
                )
                self.store.db.execute(
                    """UPDATE task_executions
                       SET phase='implementing',
                           phase_detail='Продолжается ранее запущенная разработка',
                           updated_at_ms=?
                       WHERE id=?""",
                    (now, item["id"]),
                )
            if actor_id not in newest_by_actor:
                newest_by_actor.add(actor_id)
                self.store.db.execute(
                    "UPDATE task_executions SET active_slot=1 WHERE id=?",
                    (item["id"],),
                )
            else:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='needs_owner',phase='needs_owner',
                           phase_detail='Найден параллельный legacy execution; требуется сверка',
                           error_code='LEGACY_CONCURRENT_EXECUTION',
                           active_slot=NULL,finished_at_ms=COALESCE(finished_at_ms,?),
                           updated_at_ms=?
                       WHERE id=?""",
                    (now, now, item["id"]),
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

    def _project_binding(
        self,
        actor_id: str,
        workspace_id: str,
        project_id: str,
    ) -> tuple[str, str]:
        connections = [
            item
            for item in self.store.list_repository_connections(actor_id, workspace_id)
            if item.get("project_id") == project_id
            and item.get("state") == "available"
            and item.get("installation_state") == "active"
            and item.get("access_mode") == "app_managed_write"
        ]
        if len(connections) != 1:
            raise StoreError(
                "DEVELOPMENT_REPOSITORY_BINDING_REQUIRED",
                "Development requires exactly one active writable repository bound to the project",
            )
        full_name = str(connections[0].get("full_name") or "").strip()
        if "/" not in full_name:
            raise StoreError(
                "DEVELOPMENT_REPOSITORY_BINDING_INVALID",
                "Bound development repository is invalid",
            )
        return full_name.rsplit("/", 1)[-1], full_name

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

If material fixes are required, report the concrete rework required without editing files and end with the exact line:
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
    def _acceptance_prompt(spec_path: str) -> str:
        return f"""Final post-delivery acceptance for:
{spec_path}

The implementation thread reports that delivery is complete. Independently verify the delivered state using read-only authoritative evidence: current Git/GitHub state, required CI results, deployed runtime/health/readback, and signed Android release/update manifest when the specification requires Android changes. Re-run or inspect the browser/emulator acceptance required by the specification where available. Do not trust the delivery prose alone and do not modify source or production in this turn.

If every required Definition of Done item is supported by actual evidence, end with exactly:
ACCEPTANCE_VERDICT: ACCEPTED

If any required evidence is missing, failed, stale, or contradicts delivery, end with exactly:
ACCEPTANCE_VERDICT: REWORK_REQUIRED

Before the verdict, list the concrete evidence checked and any missing/failed item."""

    @staticmethod
    def _terminal_verdict(summary: str, prefix: str) -> str | None:
        lines = [line.strip().upper() for line in summary.splitlines() if line.strip()]
        if not lines:
            return None
        final = lines[-1]
        if final == f"{prefix}: ACCEPTED":
            return "accepted"
        if final == f"{prefix}: REWORK_REQUIRED":
            return "rework_required"
        return None

    @classmethod
    def _review_verdict(cls, summary: str) -> str | None:
        return cls._terminal_verdict(summary, "REVIEW_VERDICT")

    @classmethod
    def _acceptance_verdict(cls, summary: str) -> str | None:
        return cls._terminal_verdict(summary, "ACCEPTANCE_VERDICT")

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

    @staticmethod
    def _stage_dispatch_key(execution_id: str, stage: str, cycle: int) -> str:
        return f"ph:{execution_id}:{stage}:{int(cycle)}"

    @staticmethod
    def _dispatch_prompt(dispatch_key: str, prompt: str) -> str:
        return f"PROJECTS_HUB_DISPATCH_KEY: {dispatch_key}\n\n{prompt}"

    def _record_stage(
        self,
        *,
        execution_id: str,
        stage: str,
        cycle: int,
        model: str,
        reasoning_effort: str,
        dispatch_kind: str,
        devcoveer_task_id: str | None = None,
        status: str = "dispatching",
    ) -> dict[str, Any]:
        now = _now_ms()
        dispatch_key = self._stage_dispatch_key(execution_id, stage, cycle)
        with self.store._lock:
            self.store.db.execute(
                """INSERT OR IGNORE INTO task_execution_stages(
                       id,execution_id,stage,cycle,model,reasoning_effort,
                       devcoveer_task_id,status,summary,review_verdict,
                       token_usage_json,started_at_ms,finished_at_ms,
                       created_at_ms,updated_at_ms,dispatch_key,dispatch_kind,
                       transition_claimed_at_ms
                   ) VALUES(?,?,?,?,?,?,?,?, '',NULL,NULL,?,NULL,?,?,?,?,NULL)""",
                (
                    "devstage_" + uuid.uuid4().hex,
                    execution_id,
                    stage,
                    int(cycle),
                    model,
                    reasoning_effort,
                    devcoveer_task_id,
                    status,
                    now,
                    now,
                    now,
                    dispatch_key,
                    dispatch_kind,
                ),
            )
            row = self.store.db.execute(
                "SELECT * FROM task_execution_stages WHERE dispatch_key=?",
                (dispatch_key,),
            ).fetchone()
        if not row:
            raise StoreError("DEVELOPMENT_STAGE_WRITE_FAILED", "Development stage was not persisted")
        return dict(row)

    def _stage_row(self, stage_id: str) -> dict[str, Any]:
        with self.store._lock:
            row = self.store.db.execute(
                "SELECT * FROM task_execution_stages WHERE id=?",
                (stage_id,),
            ).fetchone()
        if not row:
            raise StoreError("DEVELOPMENT_STAGE_MISSING", "Development stage is unavailable")
        return dict(row)

    def _active_stage(self, execution_id: str) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store.db.execute(
                """SELECT * FROM task_execution_stages
                   WHERE execution_id=?
                     AND status IN ('dispatching','dispatch_unknown','running',
                                    'transitioning','waiting_capacity')
                   ORDER BY created_at_ms DESC LIMIT 1""",
                (execution_id,),
            ).fetchone()
        return dict(row) if row else None

    def _claim_stage_completion(
        self,
        *,
        stage_id: str,
        summary: str,
        review_verdict: str | None,
        token_usage: dict[str, Any] | None,
    ) -> bool:
        now = _now_ms()
        with self.store._lock:
            self.store.db.execute(
                """UPDATE task_execution_stages
                   SET status='transitioning',summary=?,review_verdict=?,
                       token_usage_json=?,transition_claimed_at_ms=?,updated_at_ms=?
                   WHERE id=? AND status='running'""",
                (
                    summary[:12000],
                    review_verdict,
                    json.dumps(token_usage, separators=(",", ":")) if token_usage else None,
                    now,
                    now,
                    stage_id,
                ),
            )
            changed = int(self.store.db.execute("SELECT changes()").fetchone()[0])
        return changed == 1

    def _complete_stage(self, stage_id: str) -> None:
        now = _now_ms()
        with self.store._lock:
            self.store.db.execute(
                """UPDATE task_execution_stages
                   SET status='completed',finished_at_ms=COALESCE(finished_at_ms,?),
                       transition_claimed_at_ms=NULL,updated_at_ms=?
                   WHERE id=?""",
                (now, now, stage_id),
            )

    def _finish_stage_terminal(
        self,
        *,
        stage_id: str,
        status: str,
        summary: str,
        token_usage: dict[str, Any] | None,
    ) -> None:
        now = _now_ms()
        with self.store._lock:
            self.store.db.execute(
                """UPDATE task_execution_stages
                   SET status=?,summary=?,token_usage_json=?,
                       finished_at_ms=?,updated_at_ms=?
                   WHERE id=?""",
                (
                    status,
                    summary[:12000],
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
        item.pop("active_slot", None)
        item.pop("request_key", None)
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
            stage.pop("transition_claimed_at_ms", None)
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

    @staticmethod
    def _phase_from_result(result: dict[str, Any], status: str) -> tuple[str, str]:
        if status == "completed":
            return "ready", "Готово"
        if status in {"failed", "cancelled"}:
            return status, "Исполнение завершилось ошибкой" if status == "failed" else "Исполнение отменено"
        latest = result.get("latestTurn") if isinstance(result.get("latestTurn"), dict) else {}
        phase = str(latest.get("progressPhase") or result.get("progressPhase") or "").strip().lower()
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
        if clean in {"cancelled", "canceled", "interrupted"}:
            return "cancelled"
        return "running"

    @staticmethod
    def _phase_for_stage(stage: str, cycle: int, result: dict[str, Any] | None = None) -> tuple[str, str]:
        runtime_phase, runtime_detail = DevelopmentService._phase_from_result(result or {}, "running")
        if stage == "design":
            return "designing", "Сильная модель проектирует задачу, edge cases, тесты и DoD"
        if stage == "implementation":
            if runtime_phase in {"testing", "ci"}:
                return runtime_phase, runtime_detail
            return "implementing", "Разработчик реализует принятую постановку"
        if stage == "review":
            return "reviewing", f"Сильная модель проводит ревью, цикл {cycle}"
        if stage == "rework":
            if runtime_phase in {"testing", "ci"}:
                return runtime_phase, runtime_detail
            return "reworking", f"Разработчик исправляет замечания, цикл {cycle}"
        if stage == "delivery":
            if runtime_phase in {"ci", "publishing", "releasing", "deploying"}:
                return runtime_phase, runtime_detail
            return "delivering", "Принятая реализация поставляется и проверяется"
        if stage == "acceptance":
            if runtime_phase in {"testing", "ci"}:
                return runtime_phase, runtime_detail
            return "accepting", "Сильная модель проверяет фактически поставленный результат"
        return runtime_phase, runtime_detail

    def _mark_execution_terminal(
        self,
        *,
        execution_id: str,
        status: str,
        phase: str,
        detail: str,
        summary: str,
        error_code: str | None,
    ) -> None:
        now = _now_ms()
        with self.store._lock:
            self.store.db.execute(
                """UPDATE task_executions
                   SET status=?,phase=?,phase_detail=?,result_summary=?,
                       error_code=?,active_slot=NULL,finished_at_ms=?,updated_at_ms=?
                   WHERE id=?""",
                (status, phase, detail, summary[:12000], error_code, now, now, execution_id),
            )

    def _mark_needs_owner(
        self,
        *,
        execution_id: str,
        detail: str,
        error_code: str,
        summary: str = "",
    ) -> None:
        self._mark_execution_terminal(
            execution_id=execution_id,
            status="needs_owner",
            phase="needs_owner",
            detail=detail,
            summary=summary,
            error_code=error_code,
        )

    async def _dispatch_stage(
        self,
        *,
        execution: dict[str, Any],
        stage: dict[str, Any],
        prompt: str,
        access: str,
        thread_id: str | None = None,
    ) -> None:
        dispatch_key = str(stage["dispatch_key"])
        model = str(stage["model"])
        effort = str(stage["reasoning_effort"])
        keyed_prompt = self._dispatch_prompt(dispatch_key, prompt)
        task_id = thread_id or str(stage.get("devcoveer_task_id") or "") or None
        try:
            if stage["dispatch_kind"] == "start":
                result = await self.devcoveer.start_codex_task(
                    project=str(execution["project_hint"]),
                    prompt=keyed_prompt,
                    model=model,
                    reasoning_effort=effort,
                )
                task_id = str(
                    result.get("taskId")
                    or result.get("taskReference")
                    or result.get("threadId")
                    or ""
                ).strip()
                if not task_id:
                    raise DevCoveerError("DevCoveer did not return a task reference")
            else:
                if not task_id:
                    raise DevCoveerError("Continuation thread is unavailable")
                await self.devcoveer.continue_codex_task(
                    task_id,
                    project=str(execution["project_hint"]),
                    prompt=keyed_prompt,
                    access=access,
                    model=model,
                    reasoning_effort=effort,
                )
        except DevCoveerUnknownOutcome:
            now = _now_ms()
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_execution_stages
                       SET status='dispatch_unknown',updated_at_ms=? WHERE id=?""",
                    (now, stage["id"]),
                )
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='dispatch_unknown',phase='reconciling',
                           phase_detail='Сверяю неизвестный результат запуска',
                           updated_at_ms=? WHERE id=?""",
                    (now, execution["id"]),
                )
            return
        except DevCoveerError as exc:
            self._finish_stage_terminal(
                stage_id=str(stage["id"]),
                status="failed",
                summary=str(exc),
                token_usage=None,
            )
            self._mark_execution_terminal(
                execution_id=str(execution["id"]),
                status="failed",
                phase="failed",
                detail="DevCoveer не подтвердил запуск стадии",
                summary=str(exc),
                error_code="DEVCOVEER_DISPATCH_FAILED",
            )
            return

        now = _now_ms()
        phase, detail = self._phase_for_stage(str(stage["stage"]), int(stage["cycle"]))
        with self.store._lock:
            self.store.db.execute(
                """UPDATE task_execution_stages
                   SET status='running',devcoveer_task_id=?,started_at_ms=COALESCE(started_at_ms,?),
                       updated_at_ms=? WHERE id=?""",
                (task_id, now, now, stage["id"]),
            )
            quality_task = task_id if stage["stage"] == "design" else execution.get("quality_task_id")
            implementation_task = (
                task_id if stage["stage"] == "implementation" else execution.get("implementation_task_id")
            )
            self.store.db.execute(
                """UPDATE task_executions
                   SET status='running',phase=?,phase_detail=?,
                       devcoveer_task_id=?,quality_task_id=COALESCE(?,quality_task_id),
                       implementation_task_id=COALESCE(?,implementation_task_id),
                       started_at_ms=COALESCE(started_at_ms,?),updated_at_ms=?
                   WHERE id=?""",
                (
                    phase,
                    detail,
                    task_id,
                    quality_task,
                    implementation_task,
                    now,
                    now,
                    execution["id"],
                ),
            )

    async def _reconcile_dispatch(
        self,
        execution: dict[str, Any],
        stage: dict[str, Any],
    ) -> None:
        now = _now_ms()
        age_ms = max(0, now - int(stage.get("updated_at_ms") or stage.get("created_at_ms") or now))
        if stage["status"] == "dispatching" and age_ms < 60_000:
            return
        if stage["status"] == "dispatching":
            with self.store._lock:
                self.store.db.execute(
                    "UPDATE task_execution_stages SET status='dispatch_unknown',updated_at_ms=? WHERE id=?",
                    (now, stage["id"]),
                )
            stage["status"] = "dispatch_unknown"

        if stage["dispatch_kind"] == "start":
            try:
                found = await self.devcoveer.list_tasks(
                    project=str(execution["project_hint"]),
                    search=str(stage["dispatch_key"]),
                    limit=10,
                )
            except DevCoveerError:
                return
            matches = [
                item for item in found.get("tasks", [])
                if isinstance(item, dict)
                and str(stage["dispatch_key"]) in (
                    str(item.get("name") or "") + "\n" + str(item.get("preview") or "")
                )
            ]
            if len(matches) == 1:
                task_id = str(
                    matches[0].get("taskId")
                    or matches[0].get("taskReference")
                    or matches[0].get("threadId")
                    or ""
                ).strip()
                if task_id:
                    now = _now_ms()
                    phase, detail = self._phase_for_stage(str(stage["stage"]), int(stage["cycle"]))
                    with self.store._lock:
                        self.store.db.execute(
                            """UPDATE task_execution_stages
                               SET status='running',devcoveer_task_id=?,updated_at_ms=? WHERE id=?""",
                            (task_id, now, stage["id"]),
                        )
                        fields = []
                        params: list[Any] = []
                        if stage["stage"] == "design":
                            fields.append("quality_task_id=?")
                            params.append(task_id)
                        if stage["stage"] == "implementation":
                            fields.append("implementation_task_id=?")
                            params.append(task_id)
                        extra = ("," + ",".join(fields)) if fields else ""
                        self.store.db.execute(
                            f"""UPDATE task_executions
                                SET status='running',phase=?,phase_detail=?,
                                    devcoveer_task_id=?{extra},updated_at_ms=?
                                WHERE id=?""",
                            (phase, detail, task_id, *params, now, execution["id"]),
                        )
                    return
            if len(matches) > 1:
                self._mark_needs_owner(
                    execution_id=str(execution["id"]),
                    detail="DevCoveer вернул несколько запусков для одного dispatch key",
                    error_code="DEVCOVEER_DUPLICATE_DISPATCH",
                )
                return
        else:
            task_id = str(stage.get("devcoveer_task_id") or "")
            if task_id:
                try:
                    result = await self.devcoveer.read_task(
                        task_id,
                        project=str(execution["project_hint"]),
                        detail="summary",
                    )
                except DevCoveerError:
                    return
                latest = result.get("latestTurn") if isinstance(result.get("latestTurn"), dict) else {}
                started = latest.get("startedAt")
                started_ms = int(started * 1000) if isinstance(started, (int, float)) else 0
                baseline = int(stage.get("started_at_ms") or stage.get("created_at_ms") or 0)
                if started_ms >= baseline - 5_000:
                    with self.store._lock:
                        self.store.db.execute(
                            "UPDATE task_execution_stages SET status='running',updated_at_ms=? WHERE id=?",
                            (now, stage["id"]),
                        )
                        self.store.db.execute(
                            "UPDATE task_executions SET status='running',updated_at_ms=? WHERE id=?",
                            (now, execution["id"]),
                        )
                    return

        if age_ms >= 180_000:
            self._mark_needs_owner(
                execution_id=str(execution["id"]),
                detail="Не удалось однозначно сверить неизвестный результат dispatch",
                error_code="DEVCOVEER_DISPATCH_UNKNOWN",
            )

    async def _advance_transition(
        self,
        execution: dict[str, Any],
        stage: dict[str, Any],
    ) -> None:
        stage_name = str(stage["stage"])
        cycle = int(stage.get("cycle") or 0)
        summary = str(stage.get("summary") or "")
        verdict = str(stage.get("review_verdict") or "") or None
        implementation_model, implementation_effort = str(execution["model_profile"]).rsplit(":", 1)
        spec_path = str(execution.get("spec_path") or "")

        if stage_name == "acceptance":
            acceptance = self._acceptance_verdict(summary)
            if acceptance == "accepted":
                self._complete_stage(str(stage["id"]))
                now = _now_ms()
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE task_executions
                           SET status='completed',phase='ready',
                               phase_detail='Готово, поставлено и независимо принято',
                               result_summary=?,error_code=NULL,active_slot=NULL,
                               finished_at_ms=?,updated_at_ms=? WHERE id=?""",
                        (summary[:12000], now, now, execution["id"]),
                    )
                    for task_id in json.loads(execution["task_ids_json"]):
                        self.store.db.execute(
                            "UPDATE tasks SET state='done',updated_at_ms=? WHERE id=?",
                            (now, task_id),
                        )
                return
            self._complete_stage(str(stage["id"]))
            self._mark_needs_owner(
                execution_id=str(execution["id"]),
                detail="Финальная приёмка поставки не прошла",
                error_code="DELIVERY_ACCEPTANCE_FAILED",
                summary=summary,
            )
            return

        next_stage = ""
        next_cycle = cycle
        next_model = ""
        next_effort = ""
        next_prompt = ""
        dispatch_kind = "continue"
        access = "read"
        thread_id: str | None = None

        if stage_name == "design":
            next_stage = "implementation"
            next_cycle = 0
            next_model, next_effort = implementation_model, implementation_effort
            next_prompt = self._implementation_prompt(
                self._project_name(
                    str(execution["actor_id"]),
                    str(execution["workspace_id"]),
                    str(execution["project_id"]),
                ),
                spec_path,
            )
            dispatch_kind = "start"
            access = "write"
        elif stage_name in {"implementation", "rework"}:
            next_stage = "review"
            next_cycle = 1 if stage_name == "implementation" else cycle
            next_model, next_effort = QUALITY_MODEL, QUALITY_EFFORT
            next_prompt = self._review_prompt(spec_path, next_cycle)
            thread_id = str(execution.get("quality_task_id") or "") or None
            access = "read"
        elif stage_name == "review":
            if verdict == "accepted":
                next_stage = "delivery"
                next_model, next_effort = implementation_model, implementation_effort
                next_prompt = self._delivery_prompt(spec_path)
                thread_id = str(execution.get("implementation_task_id") or "") or None
                access = "write"
            elif verdict == "rework_required":
                if cycle >= MAX_REWORK_CYCLES:
                    self._complete_stage(str(stage["id"]))
                    self._mark_needs_owner(
                        execution_id=str(execution["id"]),
                        detail="После допустимых циклов ревью остались существенные замечания",
                        error_code="REVIEW_REWORK_LIMIT",
                        summary=summary,
                    )
                    return
                next_stage = "rework"
                next_cycle = cycle + 1
                next_model, next_effort = implementation_model, implementation_effort
                next_prompt = self._rework_prompt(spec_path, summary, next_cycle)
                thread_id = str(execution.get("implementation_task_id") or "") or None
                access = "write"
            else:
                self._complete_stage(str(stage["id"]))
                self._mark_needs_owner(
                    execution_id=str(execution["id"]),
                    detail="Ревью завершилось без однозначного финального verdict",
                    error_code="REVIEW_VERDICT_MISSING",
                    summary=summary,
                )
                return
        elif stage_name == "delivery":
            next_stage = "acceptance"
            next_model, next_effort = QUALITY_MODEL, QUALITY_EFFORT
            next_prompt = self._acceptance_prompt(spec_path)
            thread_id = str(execution.get("quality_task_id") or "") or None
            access = "read"
        else:
            self._mark_needs_owner(
                execution_id=str(execution["id"]),
                detail="Неизвестная стадия execution",
                error_code="DEVELOPMENT_STAGE_INVALID",
                summary=summary,
            )
            return

        try:
            remaining = await self._require_stage_capacity(
                actor_id=str(execution["actor_id"]),
                workspace_id=str(execution["workspace_id"]),
                model=next_model,
                reasoning_effort=next_effort,
            )
        except StoreError as exc:
            if exc.code == "CODEX_CAPACITY_RESERVED":
                now = _now_ms()
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE task_execution_stages
                           SET status='waiting_capacity',transition_claimed_at_ms=NULL,
                               updated_at_ms=? WHERE id=?""",
                        (now, stage["id"]),
                    )
                    self.store.db.execute(
                        """UPDATE task_executions
                           SET status='waiting_capacity',phase='capacity_wait',
                               phase_detail='Жду доступную Codex capacity выше резерва 10%',
                               error_code=NULL,updated_at_ms=? WHERE id=?""",
                        (now, execution["id"]),
                    )
                return
            self._complete_stage(str(stage["id"]))
            self._mark_needs_owner(
                execution_id=str(execution["id"]),
                detail="Следующая стадия требует недоступную модель/режим",
                error_code=exc.code,
                summary=summary,
            )
            return

        next_row = self._record_stage(
            execution_id=str(execution["id"]),
            stage=next_stage,
            cycle=next_cycle,
            model=next_model,
            reasoning_effort=next_effort,
            dispatch_kind=dispatch_kind,
            devcoveer_task_id=thread_id,
            status="dispatching",
        )
        self._complete_stage(str(stage["id"]))
        now = _now_ms()
        with self.store._lock:
            self.store.db.execute(
                """UPDATE task_executions
                   SET status='running',phase='dispatching',
                       phase_detail=?,quota_remaining_percent=?,
                       result_summary=?,error_code=NULL,updated_at_ms=?
                   WHERE id=?""",
                (
                    f"Запускается стадия {next_stage}",
                    remaining,
                    summary[:12000],
                    now,
                    execution["id"],
                ),
            )
        execution = dict(
            self._execution_row(
                actor_id=str(execution["actor_id"]),
                workspace_id=str(execution["workspace_id"]),
                execution_id=str(execution["id"]),
            )
        )
        await self._dispatch_stage(
            execution=execution,
            stage=next_row,
            prompt=next_prompt,
            access=access,
            thread_id=thread_id,
        )

    async def _sync_execution(self, execution: dict[str, Any]) -> None:
        if str(execution.get("status")) not in ACTIVE_EXECUTION_STATES:
            return
        stage = self._active_stage(str(execution["id"]))
        if stage is None:
            self._mark_needs_owner(
                execution_id=str(execution["id"]),
                detail="Не найден активный stage; execution сохранён для ручной сверки",
                error_code="DEVELOPMENT_STAGE_MISSING",
            )
            return

        if stage["status"] in {"dispatching", "dispatch_unknown"}:
            await self._reconcile_dispatch(execution, stage)
            return
        if stage["status"] in {"transitioning", "waiting_capacity"}:
            await self._advance_transition(execution, stage)
            return

        task_id = str(stage.get("devcoveer_task_id") or "")
        if not task_id:
            self._mark_needs_owner(
                execution_id=str(execution["id"]),
                detail="Stage не содержит DevCoveer task reference",
                error_code="DEVELOPMENT_STAGE_MISSING",
            )
            return
        try:
            result = await self.devcoveer.read_task(
                task_id,
                project=str(execution["project_hint"]),
                detail="summary",
            )
        except DevCoveerError:
            now = _now_ms()
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET phase_detail='Статус DevCoveer временно недоступен; повторю сверку',
                           updated_at_ms=? WHERE id=?""",
                    (now, execution["id"]),
                )
            return

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
            phase, detail = self._phase_for_stage(
                str(stage["stage"]),
                int(stage.get("cycle") or 0),
                result,
            )
            now = _now_ms()
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='running',phase=?,phase_detail=?,updated_at_ms=?
                       WHERE id=?""",
                    (phase, detail, now, execution["id"]),
                )
            return

        if turn_status in {"failed", "cancelled"}:
            self._finish_stage_terminal(
                stage_id=str(stage["id"]),
                status=turn_status,
                summary=summary,
                token_usage=token_usage,
            )
            self._mark_execution_terminal(
                execution_id=str(execution["id"]),
                status=turn_status,
                phase=turn_status,
                detail="Стадия разработки завершилась ошибкой"
                if turn_status == "failed" else "Стадия разработки отменена",
                summary=summary,
                error_code="DEVCOVEER_TASK_FAILED"
                if turn_status == "failed" else "DEVCOVEER_TASK_CANCELLED",
            )
            return

        verdict = None
        if stage["stage"] == "review":
            verdict = self._review_verdict(summary)
        elif stage["stage"] == "acceptance":
            verdict = self._acceptance_verdict(summary)
        if not self._claim_stage_completion(
            stage_id=str(stage["id"]),
            summary=summary,
            review_verdict=verdict,
            token_usage=token_usage,
        ):
            return
        fresh_execution = dict(
            self._execution_row(
                actor_id=str(execution["actor_id"]),
                workspace_id=str(execution["workspace_id"]),
                execution_id=str(execution["id"]),
            )
        )
        fresh_stage = self._stage_row(str(stage["id"]))
        await self._advance_transition(fresh_execution, fresh_stage)

    async def start(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        task_ids: list[str],
        model: str | None = None,
        reasoning_effort: str | None = None,
        request_key: str | None = None,
    ) -> dict[str, Any]:
        self._authorize_owner(actor_id, workspace_id)
        clean_request_key = str(request_key or "").strip()[:160] or None
        if clean_request_key:
            with self.store._lock:
                existing = self.store.db.execute(
                    """SELECT id FROM task_executions
                       WHERE actor_id=? AND request_key=?""",
                    (actor_id, clean_request_key),
                ).fetchone()
            if existing:
                return await self.status(
                    actor_id=actor_id,
                    workspace_id=workspace_id,
                    execution_id=str(existing["id"]),
                    sync=False,
                )

        tasks, project_id = self._selected_tasks(
            actor_id=actor_id,
            workspace_id=workspace_id,
            task_ids=task_ids,
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
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
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
        project_hint, repository_full_name = self._project_binding(
            actor_id,
            workspace_id,
            project_id,
        )
        now = _now_ms()
        execution_id = "devrun_" + uuid.uuid4().hex
        spec_path = f"docs/prompts/owner-development-{execution_id}.md"
        prompt = self._design_prompt(project_name, tasks, spec_path)
        task_ids_json = json.dumps([item["id"] for item in tasks], separators=(",", ":"))
        try:
            with self.store._lock:
                self.store.db.execute(
                    """INSERT INTO task_executions(
                           id,actor_id,workspace_id,project_id,task_ids_json,project_hint,
                           provider,model_profile,prompt,prompt_sha256,status,phase,phase_detail,
                           quota_remaining_percent,result_summary,error_code,
                           created_at_ms,updated_at_ms,started_at_ms,finished_at_ms,
                           review_cycle,spec_path,active_slot,repository_full_name,request_key
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'queued','Ожидает quality design',
                                ?,?,?, ?,?,?,NULL,0,?,1,?,?)""",
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
                        repository_full_name,
                        clean_request_key,
                    ),
                )
                for task in tasks:
                    if task.get("state") not in {"accepted", "done"}:
                        self.store.db.execute(
                            "UPDATE tasks SET state='accepted',updated_at_ms=? WHERE id=?",
                            (now, task["id"]),
                        )
        except sqlite3.IntegrityError as exc:
            raise StoreError(
                "DEVELOPMENT_EXECUTION_ACTIVE",
                "Another owner development execution is already active",
            ) from exc

        stage = self._record_stage(
            execution_id=execution_id,
            stage="design",
            cycle=0,
            model=QUALITY_MODEL,
            reasoning_effort=QUALITY_EFFORT,
            dispatch_kind="start",
            status="dispatching",
        )
        execution = dict(
            self._execution_row(
                actor_id=actor_id,
                workspace_id=workspace_id,
                execution_id=execution_id,
            )
        )
        await self._dispatch_stage(
            execution=execution,
            stage=stage,
            prompt=prompt,
            access="write",
        )
        return await self.status(
            actor_id=actor_id,
            workspace_id=workspace_id,
            execution_id=execution_id,
            sync=False,
        )

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
        if sync and item.get("status") in ACTIVE_EXECUTION_STATES:
            async with self._advance_lock:
                fresh = dict(
                    self._execution_row(
                        actor_id=actor_id,
                        workspace_id=workspace_id,
                        execution_id=str(item["id"]),
                    )
                )
                await self._sync_execution(fresh)
            row = self._execution_row(
                actor_id=actor_id,
                workspace_id=workspace_id,
                execution_id=str(item["id"]),
            )
        public = self._execution_public(row)
        public["update_check_recommended"] = public["status"] == "completed"
        return {"execution": public}

    async def start_background(self) -> None:
        if self._driver_task is not None and not self._driver_task.done():
            return
        self._driver_task = asyncio.create_task(
            self._driver_loop(),
            name="projects-hub-development-driver",
        )

    async def _driver_loop(self) -> None:
        try:
            while True:
                with self.store._lock:
                    rows = self.store.db.execute(
                        """SELECT * FROM task_executions
                           WHERE active_slot=1
                             AND status IN ('starting','running','waiting_capacity','dispatch_unknown')
                           ORDER BY updated_at_ms"""
                    ).fetchall()
                for row in rows:
                    try:
                        async with self._advance_lock:
                            await self._sync_execution(dict(row))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Preserve the durable state and try again on the next pass.
                        continue
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            return

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
