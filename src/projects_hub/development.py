from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
import uuid
from typing import Any

from .devcoveer_client import DevCoveerClient, DevCoveerError
from .readiness import ReadinessService
from .store import DurableStore, StoreError

ACTIVE_EXECUTION_STATES = {"starting", "running"}
TERMINAL_EXECUTION_STATES = {"completed", "failed", "cancelled", "blocked"}
DEFAULT_CODEX_PROFILE = "gpt-6.1-medium"
QUALITY_MODEL = "gpt-6-astra"
QUALITY_EFFORT = "high"
MAX_REWORK_CYCLES = 2
MAX_INTERRUPTED_RESUME_ATTEMPTS = 1
MAX_INTERRUPTED_DESIGN_ATTEMPTS = 3
MAX_INTERRUPTED_REVIEW_ATTEMPTS = 4
MAX_INTERRUPTED_WRITE_CONTINUATIONS = 6
CANDIDATE_CHECK_DISCOVERY_GRACE_MS = 120_000
DELIVERY_CHECK_DISCOVERY_GRACE_MS = 120_000
MAX_DELIVERY_DEPLOY_ATTEMPTS = 3
PROJECTS_ROOT = Path(os.getenv("PROJECTS_HUB_PROJECTS_ROOT", "/home/dev/projects")).resolve()
BACKGROUND_SYNC_INTERVAL_SECONDS = 2.0
SELF_REPOSITORY_ENV = "PROJECTS_HUB_SELF_REPOSITORY"
SELF_DEVCOVEER_PROJECT_ENV = "PROJECTS_HUB_SELF_DEVCOVEER_PROJECT"

log = logging.getLogger("projects_hub.development")


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
        pipeline_mode: str = "codex_owner",
    ) -> None:
        if pipeline_mode not in {"codex_owner", "legacy"}:
            raise ValueError("Unknown owner-development pipeline")
        self.pipeline_mode = pipeline_mode
        self.store = store
        self.readiness = readiness
        self.devcoveer = devcoveer or DevCoveerClient()
        self._transition_lock: asyncio.Lock | None = None
        self._transition_loop: asyncio.AbstractEventLoop | None = None
        self._background_task: asyncio.Task[None] | None = None
        self._background_stop: asyncio.Event | None = None
        try:
            configured_interval = float(
                os.getenv(
                    "PROJECTS_HUB_DEVELOPMENT_SYNC_SECONDS",
                    str(BACKGROUND_SYNC_INTERVAL_SECONDS),
                )
            )
        except ValueError:
            configured_interval = BACKGROUND_SYNC_INTERVAL_SECONDS
        self._background_interval_seconds = max(
            0.5, min(configured_interval, 30.0)
        )
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
                    candidate_sha TEXT,
                    candidate_branch TEXT,
                    candidate_pr INTEGER,
                    candidate_published_at_ms INTEGER,
                    candidate_evidence_json TEXT,
                    delivery_merge_sha TEXT,
                    delivery_main_sha TEXT,
                    delivery_merged_at_ms INTEGER,
                    delivery_job_id TEXT,
                    delivery_deployed_at_ms INTEGER,
                    delivery_evidence_json TEXT,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    started_at_ms INTEGER,
                    finished_at_ms INTEGER
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
                    recovery_attempts INTEGER NOT NULL DEFAULT 0,
                    recovery_last_at_ms INTEGER,
                    started_at_ms INTEGER,
                    finished_at_ms INTEGER,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_execution_stages_execution_idx
                    ON task_execution_stages(execution_id,created_at_ms);
                CREATE TABLE IF NOT EXISTS development_owner_resumes(
                    execution_id TEXT NOT NULL REFERENCES task_executions(id),
                    command_id TEXT NOT NULL,
                    answer_sha256 TEXT NOT NULL,
                    answer_text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'dispatching',
                    provider_baseline_json TEXT,
                    provider_receipt_json TEXT,
                    quota_remaining_percent REAL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER,
                    PRIMARY KEY(execution_id, command_id)
                );
                """
            )
            columns = {
                str(row["name"])
                for row in self.store.db.execute(
                    "PRAGMA table_info(task_executions)"
                ).fetchall()
            }
            if "pipeline_mode" not in columns:
                # Existing in-flight deliveries keep their original resume path.
                self.store.db.execute(
                    "ALTER TABLE task_executions ADD COLUMN pipeline_mode TEXT NOT NULL DEFAULT 'legacy'"
                )
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
            for column_name, ddl in (
                ("candidate_sha", "TEXT"),
                ("candidate_branch", "TEXT"),
                ("candidate_pr", "INTEGER"),
                ("candidate_published_at_ms", "INTEGER"),
                ("candidate_evidence_json", "TEXT"),
                ("delivery_merge_sha", "TEXT"),
                ("delivery_main_sha", "TEXT"),
                ("delivery_merged_at_ms", "INTEGER"),
                ("delivery_job_id", "TEXT"),
                ("delivery_deployed_at_ms", "INTEGER"),
                ("delivery_evidence_json", "TEXT"),
            ):
                if column_name not in columns:
                    self.store.db.execute(
                        f"ALTER TABLE task_executions ADD COLUMN {column_name} {ddl}"
                    )
            stage_columns = {
                str(row["name"])
                for row in self.store.db.execute(
                    "PRAGMA table_info(task_execution_stages)"
                ).fetchall()
            }
            if "recovery_attempts" not in stage_columns:
                self.store.db.execute(
                    "ALTER TABLE task_execution_stages "
                    "ADD COLUMN recovery_attempts INTEGER NOT NULL DEFAULT 0"
                )
            if "recovery_last_at_ms" not in stage_columns:
                self.store.db.execute(
                    "ALTER TABLE task_execution_stages "
                    "ADD COLUMN recovery_last_at_ms INTEGER"
                )

            resume_columns = {
                str(row["name"])
                for row in self.store.db.execute(
                    "PRAGMA table_info(development_owner_resumes)"
                ).fetchall()
            }
            if "provider_baseline_json" not in resume_columns:
                self.store.db.execute(
                    "ALTER TABLE development_owner_resumes "
                    "ADD COLUMN provider_baseline_json TEXT"
                )
            if "provider_receipt_json" not in resume_columns:
                self.store.db.execute(
                    "ALTER TABLE development_owner_resumes "
                    "ADD COLUMN provider_receipt_json TEXT"
                )
            if "quota_remaining_percent" not in resume_columns:
                self.store.db.execute(
                    "ALTER TABLE development_owner_resumes "
                    "ADD COLUMN quota_remaining_percent REAL"
                )
            if "updated_at_ms" not in resume_columns:
                self.store.db.execute(
                    "ALTER TABLE development_owner_resumes "
                    "ADD COLUMN updated_at_ms INTEGER"
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

    def backlog_overview(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        project_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        tasks = self.list_backlog(
            actor_id=actor_id,
            workspace_id=workspace_id,
            project_id=project_id,
            limit=limit,
        )
        executions = self.list_executions(
            actor_id=actor_id,
            workspace_id=workspace_id,
            project_id=project_id,
            limit=1,
        )
        return {
            "tasks": tasks,
            "latest_execution": executions[0] if executions else None,
            "backlog_state_semantics": (
                "Backlog state accepted means approved/eligible. "
                "Execution state comes from latest_execution."
            ),
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
        ]
        self_repository = os.getenv(SELF_REPOSITORY_ENV, "").strip()
        self_project = os.getenv(SELF_DEVCOVEER_PROJECT_ENV, "").strip()

        def project_hint(item: dict[str, Any]) -> str:
            full_name = str(item.get("full_name") or "").strip()
            if self_repository and self_project and full_name == self_repository:
                return self_project
            return full_name.rsplit("/", 1)[-1]

        # Development follows the repository that owns product source, not a
        # repository that merely grants app-managed writes for notes/docs.
        owning = [
            item
            for item in connections
            if item.get("role") == "external_owning_repo"
        ]
        if len(owning) == 1:
            return project_hint(owning[0])

        # The self repository is authoritative even when normal app writes are
        # read-only. DevCoveer write access is independently owner-gated.
        self_matches = [
            item
            for item in connections
            if self_repository
            and str(item.get("full_name") or "").strip() == self_repository
        ]
        if len(self_matches) == 1:
            return project_hint(self_matches[0])

        writable = [
            item
            for item in connections
            if item.get("access_mode") == "app_managed_write"
            and item.get("role") != "project_docs"
        ]
        if len(writable) == 1:
            return project_hint(writable[0])

        normalized = "".join(ch.lower() for ch in project_name if ch.isalnum())
        exact = [
            item
            for item in connections
            if item.get("role") != "project_docs"
            and "".join(
                ch.lower()
                for ch in str(item.get("full_name") or "").rsplit("/", 1)[-1]
                if ch.isalnum()
            )
            == normalized
        ]
        if len(exact) == 1:
            return project_hint(exact[0])
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

    @classmethod
    def _implementation_prompt(
        cls,
        project_name: str,
        spec_path: str,
        execution_id: str,
    ) -> str:
        marker = cls._candidate_branch_marker(execution_id)
        return f"""Implement the owner-approved development specification for {project_name} at:
{spec_path}

Read the specification and current repository state first. Refresh/reconcile with the latest origin/main before declaring the candidate ready; resolve merge conflicts without dropping unrelated main work. Implement the requested product change, add/update tests, and perform the required debugging and browser/emulator checks from the spec. If the candidate changes Android/native code, ensure the project semantic version is the next unused version relative to fresh main before review so the signed main release can be published. Prepare a clean committed reviewable chatgpt/* branch whose name contains the stable execution marker {marker} (for example chatgpt/ownerdev-{marker}-short-topic); You own development, local tests, candidate publication and existing CI as an intelligent Codex engineer: commit/push the execution-specific branch, open or update its GitHub PR, check actual CI results, fix failing checks within scope and verify the PR is reviewable. Do NOT merge, deploy, publish a release or modify production until the independent quality reviewer accepts it. Do not create a new CI system. Stop after ready for review and report IMPLEMENTATION_READY with PR URL, exact branch/SHA, actual tests/CI results and remaining concerns."""

    @staticmethod
    def _review_prompt(
        spec_path: str,
        cycle: int,
        validation_evidence: str = "",
    ) -> str:
        evidence = (
            "\n\nDeterministic candidate/CI evidence from the durable orchestrator:\n"
            + validation_evidence
            if validation_evidence
            else ""
        )
        return f"""Review cycle {cycle} for the implementation of:
{spec_path}

You are the same quality/design thread that produced the specification. Re-read the specification, inspect the current implementation diff/PR and verification evidence, and perform an independent acceptance/code review. Check edge cases, regressions, architecture/requirements compliance, tests and required browser/emulator evidence. Do NOT implement fixes and do NOT merge/deploy/release.{evidence}

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

Read the updated specification and fix all material findings. Refresh/reconcile with latest origin/main and resolve any PR merge conflicts while preserving unrelated main work. If Android/native code differs from fresh main, keep the project semantic version at the next unused version before review. Re-run the available tests/debugging/browser/emulator checks. Keep the existing implementation scope and produce a clean committed chatgpt/* candidate. As the Native Codex engineer, commit/push the updated candidate, inspect the existing GitHub PR and run/check the project's normal CI. Do not merge, deploy or release until an independent ACCEPTED review. Never create a separate CI system. Stop once the updated PR and evidence are reviewable."""

    @staticmethod
    def _delivery_prompt(spec_path: str) -> str:
        return f"""The independent quality reviewer ACCEPTED the implementation of:
{spec_path}

You are the Native Codex engineer responsible for the ENTIRE final delivery. Read this project's own release/deploy instructions and AGENTS.md, then perform its existing normal delivery process yourself. Merge only the reviewed candidate. Check real GitHub CI through successful terminal results, resolve deployment problems within scope, and verify the actual published/live result through authoritative readback. Do not create a new CI/workflow/orchestrator. Never deploy a branch-only or worktree-only commit. For Projects Hub, deploy the exact merged SHA from fresh origin/main history; verify the running service, static assets and release metadata all match that immutable release, check health release_sha, and when Android native code changed publish the normal signed APK/update manifest and verify the release. For other projects use their own documented delivery path; do not invent a Projects Hub-specific pipeline.

Only if actual delivery succeeded, end your final answer with one single line:
DELIVERY_RECEIPT: {{"status":"delivered","ci":"passed","main_sha":"<40 lowercase hex digits>","evidence_url":"<actual GitHub CI or release URL>","verification":"<what you personally checked and where>","android_update":false,"android_release_url":null}}
Set android_update=true and android_release_url to the existing signed APK release URL ONLY if an Android app version actually changed. The facts must be checked, not guessed. If a step failed or remains ambiguous, do not emit DELIVERY_RECEIPT; report the concrete blocker for a bounded retry or truthful failure."""

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
        raw_candidate_evidence = item.pop("candidate_evidence_json", None)
        candidate_evidence = None
        if isinstance(raw_candidate_evidence, str) and raw_candidate_evidence:
            try:
                parsed_candidate = json.loads(raw_candidate_evidence)
                candidate_evidence = parsed_candidate if isinstance(parsed_candidate, dict) else None
            except ValueError:
                candidate_evidence = None
        item["candidate_evidence"] = candidate_evidence
        raw_delivery_evidence = item.pop("delivery_evidence_json", None)
        delivery_evidence = None
        if isinstance(raw_delivery_evidence, str) and raw_delivery_evidence:
            try:
                parsed_delivery = json.loads(raw_delivery_evidence)
                delivery_evidence = parsed_delivery if isinstance(parsed_delivery, dict) else None
            except ValueError:
                delivery_evidence = None
        item["delivery_evidence"] = delivery_evidence
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

    def _transition_guard(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._transition_lock is None or self._transition_loop is not loop:
            self._transition_lock = asyncio.Lock()
            self._transition_loop = loop
        return self._transition_lock

    def _owner_resume_effect_in_flight(
        self,
        actor_id: str,
        *,
        exclude_execution_id: str | None = None,
    ) -> Any | None:
        params: list[Any] = [actor_id]
        exclude = ""
        if exclude_execution_id is not None:
            exclude = " AND e.id<>?"
            params.append(exclude_execution_id)
        with self.store._lock:
            return self.store.db.execute(
                """SELECT e.id,r.command_id,r.status
                   FROM task_executions e
                   JOIN development_owner_resumes r ON r.execution_id=e.id
                   WHERE e.actor_id=?
                     AND r.status IN ('dispatching','dispatch_unknown')"""
                + exclude
                + """ ORDER BY r.created_at_ms DESC LIMIT 1""",
                tuple(params),
            ).fetchone()

    async def start(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        task_ids: list[str],
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        async with self._transition_guard():
            return await self._start_locked(
                actor_id=actor_id,
                workspace_id=workspace_id,
                task_ids=task_ids,
                model=model,
                reasoning_effort=reasoning_effort,
            )

    async def _start_locked(
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
        pending_resume = self._owner_resume_effect_in_flight(actor_id)
        if active or pending_resume:
            raise StoreError(
                "DEVELOPMENT_EXECUTION_ACTIVE",
                "Another owner development execution or unresolved external resume is already active",
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

        with self.store._lock:
            self.store.db.execute(
                "UPDATE task_executions SET pipeline_mode=? WHERE id=?",
                (self.pipeline_mode, execution_id),
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
                           error_code=NULL,finished_at_ms=NULL,
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
        if clean in {"interrupted", "aborted"}:
            return "interrupted"
        return "running"

    @classmethod
    def _task_status_from_result(cls, result: dict[str, Any]) -> str:
        """Prefer native turn/runtime terminal evidence over stale wrapper running."""

        candidates: list[Any] = []
        latest = result.get("latestTurn")
        if isinstance(latest, dict):
            candidates.append(latest.get("status"))

        runtime = result.get("runtimeStatus")
        if isinstance(runtime, dict):
            candidates.append(runtime.get("type") or runtime.get("status"))

        task = result.get("task")
        if isinstance(task, dict):
            task_latest = task.get("latestTurn")
            if isinstance(task_latest, dict):
                candidates.append(task_latest.get("status"))
            task_runtime = task.get("runtimeStatus")
            if isinstance(task_runtime, dict):
                candidates.append(
                    task_runtime.get("type") or task_runtime.get("status")
                )

        candidates.extend(
            [
                result.get("executionStatus"),
                result.get("status"),
                task.get("status") if isinstance(task, dict) else None,
            ]
        )

        for value in candidates:
            mapped = cls._map_task_status(value)
            if mapped != "running":
                return mapped
        return "running"

    def _design_artifact_readback(
        self,
        *,
        item: dict[str, Any],
        stage: dict[str, Any],
        result: dict[str, Any],
    ) -> str | None:
        if str(stage.get("stage") or "") != "design":
            return None
        spec_path = str(item.get("spec_path") or "").strip()
        task = result.get("task")
        cwd_raw = str(task.get("cwd") or "").strip() if isinstance(task, dict) else ""
        if not spec_path or not cwd_raw:
            return None
        try:
            cwd = Path(cwd_raw).resolve(strict=True)
            if cwd != PROJECTS_ROOT and PROJECTS_ROOT not in cwd.parents:
                return None
            candidate = (cwd / spec_path).resolve(strict=True)
            if cwd != candidate and cwd not in candidate.parents:
                return None
            if candidate.is_symlink() or not candidate.is_file():
                return None
            size = candidate.stat().st_size
            if size < 1024 or size > 2_000_000:
                return None
            body = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError):
            return None

        try:
            task_ids = [str(value) for value in json.loads(item["task_ids_json"])]
        except (TypeError, ValueError):
            return None
        folded = body.casefold()
        markers = [str(item["id"]), *task_ids, "definition of done"]
        if not all(marker.casefold() in folded for marker in markers):
            return None
        return (
            "Design brief verified by exact artifact readback after interrupted turn: "
            + spec_path
        )

    @staticmethod
    def _interrupted_resume_prompt(stage: str) -> str:
        return (
            f"Resume the same already-authorized {stage} stage after an interrupted "
            "native turn. Preserve the existing backlog scope, requirements, project "
            "target and Definition of Done. Continue from the saved task/thread state. "
            "Do not create a new product goal or broaden scope."
        )

    @staticmethod
    def _recovery_review_prompt(
        spec_path: str,
        cycle: int,
        validation_evidence: str = "",
    ) -> str:
        evidence = (
            "\n\nDeterministic candidate/CI evidence from the durable orchestrator:\n"
            + validation_evidence
            if validation_evidence
            else ""
        )
        return f"""Recovery review cycle {cycle} for the already-authorized implementation of:
{spec_path}

The original quality/review turn was interrupted and could not be resumed. Independently re-read the specification, inspect the current implementation diff/PR and verification evidence, and perform the same acceptance/code review. Do not implement fixes and do not merge/deploy/release.{evidence}

If acceptable, end with the exact line:
REVIEW_VERDICT: ACCEPTED

If material fixes are required, give concrete findings and end with:
REVIEW_VERDICT: REWORK_REQUIRED"""

    @staticmethod
    def _direct_result_data(payload: dict[str, Any]) -> dict[str, Any]:
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            raise DevCoveerError("DevCoveer direct operation returned no result")
        row = results[0]
        if not isinstance(row, dict) or row.get("status") != "ok":
            raise DevCoveerError("DevCoveer direct operation failed")
        data = row.get("data")
        if not isinstance(data, dict):
            raise DevCoveerError("DevCoveer direct operation returned no data")
        return data

    @classmethod
    def _direct_action_data(cls, payload: dict[str, Any]) -> dict[str, Any]:
        if isinstance(payload.get("results"), list):
            return cls._direct_result_data(payload)
        if payload.get("status") in {"ok", "running", "starting", "succeeded", "reconciled"}:
            return payload
        raise DevCoveerError("DevCoveer direct action failed")

    @staticmethod
    def _candidate_branch_marker(execution_id: str) -> str:
        raw = str(execution_id or "")
        if raw.startswith("devrun_"):
            raw = raw[len("devrun_") :]
        clean = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
        return f"devrun-{clean[:8]}" if clean else ""

    @staticmethod
    def _candidate_evidence_text(item: dict[str, Any]) -> str:
        raw = item.get("candidate_evidence_json")
        if not isinstance(raw, str) or not raw:
            return ""
        try:
            parsed = json.loads(raw)
        except ValueError:
            return ""
        if not isinstance(parsed, dict):
            return ""
        return json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )[:8000]

    def _store_candidate_state(
        self,
        *,
        execution_id: str,
        sha: str,
        branch: str,
        pr: int | None,
        published_at_ms: int | None,
        evidence: dict[str, Any],
    ) -> None:
        now = _now_ms()
        with self.store._lock:
            self.store.db.execute(
                """UPDATE task_executions
                   SET candidate_sha=?,candidate_branch=?,candidate_pr=?,
                       candidate_published_at_ms=?,candidate_evidence_json=?,
                       updated_at_ms=?
                   WHERE id=?""",
                (
                    sha or None,
                    branch or None,
                    pr,
                    published_at_ms,
                    json.dumps(
                        evidence,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )[:12000],
                    now,
                    execution_id,
                ),
            )

    async def _candidate_validation(
        self,
        *,
        item: dict[str, Any],
        stage: dict[str, Any],
    ) -> dict[str, Any]:
        project = str(item["project_hint"])
        await self.devcoveer.direct_project_action(
            project=project,
            operation="git_fetch",
            payload={
                "remote": "origin",
                "branch": "main",
            },
        )
        git_payload = await self.devcoveer.direct_project_probe(
            project=project,
            operation="git_state",
        )
        git_state = self._direct_result_data(git_payload)
        current_branch = str(git_state.get("branch") or "")
        current_sha = str(git_state.get("head") or "").lower()
        # A clean chatgpt/* checkout may belong to a different concurrent
        # development run. Reuse only this execution's marker or its already
        # persisted exact branch/SHA; never publish an unrelated HEAD.
        marker = self._candidate_branch_marker(str(item["id"]))
        stored_branch = str(item.get("candidate_branch") or "").strip()
        stored_sha = str(item.get("candidate_sha") or "").lower()
        def owns_candidate(branch: str, sha: str) -> bool:
            return bool(
                branch.startswith("chatgpt/")
                and (
                    marker in branch
                    or (branch == stored_branch and sha == stored_sha and len(stored_sha) == 40)
                )
            )
        current_clean = (
            git_state.get("head_state") == "branch"
            and owns_candidate(current_branch, current_sha)
            and re.fullmatch(r"[0-9a-f]{40}", current_sha) is not None
            and not (git_state.get("staged") or [])
            and not (git_state.get("unstaged") or [])
            and not (git_state.get("untracked") or [])
            and not (git_state.get("conflicts") or [])
        )

        branch = current_branch
        sha = current_sha
        candidate_source = "current_checkout" if current_clean else ""
        local_candidates: list[dict[str, Any]] = []
        if not current_clean:
            probe_payload: dict[str, Any] = {
                "prefix": "chatgpt/",
                "limit": 50,
            }
            if stored_branch.startswith("chatgpt/"):
                probe_payload["prefix"] = stored_branch
            elif marker:
                probe_payload["contains"] = marker
            branches_payload = await self.devcoveer.direct_project_probe(
                project=project,
                operation="branch_list",
                payload=probe_payload,
            )
            branches_data = self._direct_result_data(branches_payload)
            rows = branches_data.get("branches")
            if isinstance(rows, list):
                for raw in rows:
                    if not isinstance(raw, dict):
                        continue
                    candidate_branch = str(raw.get("branch") or "")
                    candidate_sha = str(raw.get("sha") or "").lower()
                    if not owns_candidate(candidate_branch, candidate_sha):
                        continue
                    if re.fullmatch(r"[0-9a-f]{40}", candidate_sha) is None:
                        continue
                    if stored_branch and candidate_branch != stored_branch:
                        continue
                    if not stored_branch and marker and marker not in candidate_branch:
                        continue
                    local_candidates.append(
                        {
                            "branch": candidate_branch,
                            "sha": candidate_sha,
                            "committed_unix": int(raw.get("committed_unix") or 0),
                        }
                    )
            if len(local_candidates) == 1:
                branch = local_candidates[0]["branch"]
                sha = local_candidates[0]["sha"]
                candidate_source = "local_branch_ref"

        clean = bool(candidate_source)
        if not clean:
            evidence = {
                "candidate_status": "not_publishable",
                "reason": "no unique clean committed chatgpt/* candidate branch was found",
                "branch": current_branch,
                "sha": current_sha,
                "branch_marker": self._candidate_branch_marker(str(item["id"])),
                "local_candidates": local_candidates,
                "staged": list(git_state.get("staged") or []),
                "unstaged": list(git_state.get("unstaged") or []),
                "untracked": list(git_state.get("untracked") or []),
                "conflicts": list(git_state.get("conflicts") or []),
            }
            self._store_candidate_state(
                execution_id=str(item["id"]),
                sha=sha,
                branch=branch,
                pr=None,
                published_at_ms=None,
                evidence=evidence,
            )
            item["candidate_evidence_json"] = json.dumps(
                evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            return {
                "ready": True,
                "successful": False,
                "detail": "Кандидат не зафиксирован полностью; передаю точный Git state на ревью",
                "evidence": evidence,
            }

        published_at = (
            int(item.get("candidate_published_at_ms") or 0)
            if str(item.get("candidate_sha") or "") == sha
            and str(item.get("candidate_branch") or "") == branch
            else 0
        )
        pr_number = (
            int(item.get("candidate_pr"))
            if item.get("candidate_pr") is not None
            and str(item.get("candidate_sha") or "") == sha
            and str(item.get("candidate_branch") or "") == branch
            else None
        )

        if not published_at:
            await self.devcoveer.direct_project_action(
                project=project,
                operation="git_push_existing",
                payload={
                    "branch": branch,
                    "expected_sha": sha,
                    "request_key": f"{item['id']}:push:{sha[:16]}",
                },
            )
            pr_payload = await self.devcoveer.direct_project_action(
                project=project,
                operation="github_pr_create",
                payload={
                    "head": branch,
                    "base": "main",
                    "expected_head_sha": sha,
                    "title": f"Owner development {str(item['id'])[:32]}",
                    "request_key": f"{item['id']}:pr:{sha[:16]}",
                    "body": (
                        "Durable owner-development candidate for "
                        + str(item["id"])
                        + ".\n\nSpecification: "
                        + str(item.get("spec_path") or "")
                    ),
                },
            )
            pr_result = self._direct_action_data(pr_payload)
            raw_pr = pr_result.get("number")
            if isinstance(raw_pr, int) and raw_pr > 0:
                pr_number = raw_pr
            published_at = _now_ms()

        status_payload = await self.devcoveer.direct_project_probe(
            project=project,
            operation="github_status",
            payload=(
                {"pr": pr_number, "failed_log_lines": 120}
                if pr_number
                else {"branch": branch, "failed_log_lines": 120}
            ),
        )
        github = self._direct_result_data(status_payload)
        remote_branch = github.get("branch") if isinstance(github.get("branch"), dict) else {}
        pr_state = github.get("pr") if isinstance(github.get("pr"), dict) else {}
        remote_sha = str(pr_state.get("head") or remote_branch.get("head") or "")
        if pr_number is None and isinstance(pr_state.get("number"), int):
            pr_number = int(pr_state["number"])
        mergeable_state = str(pr_state.get("mergeable_state") or "")
        checks = [
            {
                "name": str(row.get("name") or ""),
                "status": str(row.get("status") or ""),
                "conclusion": row.get("conclusion"),
            }
            for row in (github.get("checks") or [])
            if isinstance(row, dict)
        ]
        workflows = [
            {
                "name": str(row.get("name") or ""),
                "status": str(row.get("status") or ""),
                "conclusion": row.get("conclusion"),
                "run_id": row.get("id"),
            }
            for row in (github.get("workflow_runs") or [])
            if isinstance(row, dict)
        ]
        evidence = {
            "candidate_status": "published",
            "candidate_source": candidate_source,
            "branch": branch,
            "sha": sha,
            "remote_sha": remote_sha,
            "pr": pr_number,
            "pr_state": pr_state.get("state"),
            "mergeable_state": mergeable_state,
            "checks": checks,
            "workflow_runs": workflows,
        }
        self._store_candidate_state(
            execution_id=str(item["id"]),
            sha=sha,
            branch=branch,
            pr=pr_number,
            published_at_ms=published_at,
            evidence=evidence,
        )
        item["candidate_sha"] = sha
        item["candidate_branch"] = branch
        item["candidate_pr"] = pr_number
        item["candidate_published_at_ms"] = published_at
        item["candidate_evidence_json"] = json.dumps(
            evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

        if remote_sha != sha:
            return {
                "ready": False,
                "successful": False,
                "detail": "Кандидат опубликован; жду подтверждение exact SHA от GitHub",
                "evidence": evidence,
            }

        pending = any(row["status"] != "completed" for row in checks) or any(
            row["status"] != "completed" for row in workflows
        )
        if pending:
            return {
                "ready": False,
                "successful": False,
                "detail": "Кандидат опубликован; CI ещё выполняется",
                "evidence": evidence,
            }

        if mergeable_state == "dirty":
            evidence["ci_success"] = False
            self._store_candidate_state(
                execution_id=str(item["id"]),
                sha=sha,
                branch=branch,
                pr=pr_number,
                published_at_ms=published_at,
                evidence=evidence,
            )
            item["candidate_evidence_json"] = json.dumps(
                evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            return {
                "ready": True,
                "successful": False,
                "detail": "Кандидат конфликтует со свежим main; запускаю технический rework до ревью",
                "evidence": evidence,
            }

        if not checks and not workflows:
            if _now_ms() - published_at < CANDIDATE_CHECK_DISCOVERY_GRACE_MS:
                return {
                    "ready": False,
                    "successful": False,
                    "detail": "Кандидат опубликован; жду появления CI checks",
                    "evidence": evidence,
                }
            # Once the discovery grace has expired, no-check is a failed
            # candidate gate, not an infinite waiting state. The existing
            # technical rework path reconciles the branch against fresh main
            # and creates new CI evidence without requiring the owner.
            evidence["ci_success"] = False
            evidence["failure_reason"] = "missing_candidate_ci_checks"
            evidence["check_discovery_grace_ms"] = CANDIDATE_CHECK_DISCOVERY_GRACE_MS
            self._store_candidate_state(
                execution_id=str(item["id"]),
                sha=sha,
                branch=branch,
                pr=pr_number,
                published_at_ms=published_at,
                evidence=evidence,
            )
            item["candidate_evidence_json"] = json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            return {
                "ready": True,
                "successful": False,
                "detail": (
                    "У опубликованного кандидата не появились CI checks "
                    "за допустимый срок; запускаю технический rework"
                ),
                "evidence": evidence,
            }

        allowed_conclusions = {"success", "neutral", "skipped"}
        terminal_rows = [*checks, *workflows]
        successful = (
            remote_sha == sha
            and bool(terminal_rows)
            and all(
                str(row.get("conclusion") or "").lower()
                in allowed_conclusions
                for row in terminal_rows
            )
        )
        evidence["ci_success"] = successful
        self._store_candidate_state(
            execution_id=str(item["id"]),
            sha=sha,
            branch=branch,
            pr=pr_number,
            published_at_ms=published_at,
            evidence=evidence,
        )
        item["candidate_evidence_json"] = json.dumps(
            evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return {
            "ready": True,
            "successful": successful,
            "detail": (
                "Кандидат опубликован; CI зелёный и готов для независимого ревью"
                if successful
                else "Кандидат опубликован; terminal CI содержит ошибки"
            ),
            "evidence": evidence,
        }

    def _store_delivery_state(
        self,
        *,
        execution_id: str,
        merge_sha: str | None = None,
        main_sha: str | None = None,
        merged_at_ms: int | None = None,
        job_id: str | None = None,
        deployed_at_ms: int | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        sets: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("delivery_merge_sha", merge_sha),
            ("delivery_main_sha", main_sha),
            ("delivery_merged_at_ms", merged_at_ms),
            ("delivery_job_id", job_id),
            ("delivery_deployed_at_ms", deployed_at_ms),
        ):
            if value is not None:
                sets.append(f"{column}=?")
                values.append(value)
        if evidence is not None:
            sets.append("delivery_evidence_json=?")
            values.append(json.dumps(
                evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ))
        if not sets:
            return
        sets.append("updated_at_ms=?")
        values.append(_now_ms())
        values.append(execution_id)
        with self.store._lock:
            self.store.db.execute(
                f"UPDATE task_executions SET {','.join(sets)} WHERE id=?",
                tuple(values),
            )

    @staticmethod
    def _github_rows_success(github: dict[str, Any]) -> tuple[bool, bool]:
        rows = [
            row
            for row in [
                *(github.get("checks") or []),
                *(github.get("workflow_runs") or []),
            ]
            if isinstance(row, dict)
        ]
        pending = any(str(row.get("status") or "") != "completed" for row in rows)
        allowed = {"success", "neutral", "skipped"}
        success = bool(rows) and not pending and all(
            str(row.get("conclusion") or "").lower() in allowed
            for row in rows
        )
        return pending, success

    async def _start_post_merge_rework(
        self,
        *,
        item: dict[str, Any],
        evidence: dict[str, Any],
    ) -> bool:
        delivery_stage = self._active_stage(str(item["id"]))
        if delivery_stage and delivery_stage.get("stage") == "delivery":
            self._finish_stage(
                stage_id=str(delivery_stage["id"]),
                status="superseded",
                summary="Main CI/release failed after merge; autonomous recovery rework started.",
            )
        implementation_model, implementation_effort = str(
            item["model_profile"]
        ).rsplit(":", 1)
        remaining = await self._require_stage_capacity(
            actor_id=str(item["actor_id"]),
            workspace_id=str(item["workspace_id"]),
            model=implementation_model,
            reasoning_effort=implementation_effort,
        )
        cycle = int(item.get("review_cycle") or 0) + 1
        marker = self._dispatch_marker(str(item["id"]), "main-ci-rework", cycle, 1)
        prompt = f"""Autonomous post-merge recovery for already-authorized execution {item['id']}.
Specification: {item.get('spec_path') or ''}

The reviewed candidate was merged, but deterministic main CI/release evidence is not green. Refresh origin/main, create a new chatgpt/* recovery branch from the exact fresh main HEAD, inspect the failing CI/release evidence below, and fix only defects attributable to this execution. Preserve unrelated main work. If Android/native code changes or a prior release tag/version is already consumed, set the project semantic version to the next unused version. Run available deterministic local checks and commit a clean candidate.

Do NOT merge, deploy or release. Stop when the clean committed recovery candidate is ready for deterministic publication and independent review.

Main CI/release evidence:
{json.dumps(evidence, ensure_ascii=False, sort_keys=True)[:8000]}"""
        task_id, _ = await self._start_marked_codex_task(
            project=str(item["project_hint"]),
            marker=marker,
            prompt=prompt,
            model=implementation_model,
            reasoning_effort=implementation_effort,
            access="write",
        )
        self._record_stage(
            execution_id=str(item["id"]),
            stage="rework",
            cycle=cycle,
            model=implementation_model,
            reasoning_effort=implementation_effort,
            devcoveer_task_id=task_id,
        )
        now = _now_ms()
        with self.store._lock:
            self.store.db.execute(
                """UPDATE task_executions
                   SET status='running',phase='reworking',
                       phase_detail='Исправляю terminal main CI/release failure без участия владельца',
                       implementation_task_id=?,devcoveer_task_id=?,
                       quota_remaining_percent=?,review_cycle=?,
                       candidate_sha=NULL,candidate_branch=NULL,candidate_pr=NULL,
                       candidate_published_at_ms=NULL,candidate_evidence_json=NULL,
                       delivery_merge_sha=NULL,delivery_main_sha=NULL,
                       delivery_merged_at_ms=NULL,delivery_job_id=NULL,
                       delivery_deployed_at_ms=NULL,delivery_evidence_json=NULL,
                       error_code=NULL,finished_at_ms=NULL,updated_at_ms=?
                   WHERE id=?""",
                (task_id, task_id, remaining, cycle, now, item["id"]),
            )
        return True

    async def _begin_deterministic_delivery(
        self,
        *,
        item: dict[str, Any],
        review_summary: str,
        cycle: int,
    ) -> dict[str, Any]:
        candidate_sha = str(item.get("candidate_sha") or "")
        pr_number = item.get("candidate_pr")
        if len(candidate_sha) != 40 or not isinstance(pr_number, int):
            raise StoreError(
                "DELIVERY_CANDIDATE_MISSING",
                "Accepted review has no exact published candidate/PR receipt",
            )
        self._record_stage(
            execution_id=str(item["id"]),
            stage="delivery",
            cycle=cycle,
            model="deterministic",
            reasoning_effort="none",
            devcoveer_task_id=f"deterministic:{candidate_sha}",
        )
        now = _now_ms()
        with self.store._lock:
            self.store.db.execute(
                """UPDATE task_executions
                   SET status='running',phase='delivery_merge',
                       phase_detail='Ревью принято; deterministic delivery объединяет exact candidate',
                       devcoveer_task_id=?,result_summary=?,error_code=NULL,
                       finished_at_ms=NULL,updated_at_ms=?
                   WHERE id=?""",
                (f"deterministic:{candidate_sha}", review_summary, now, item["id"]),
            )
        row = self._execution_row(
            actor_id=str(item["actor_id"]),
            workspace_id=str(item["workspace_id"]),
            execution_id=str(item["id"]),
        )
        public = self._execution_public(row)
        public["update_check_recommended"] = False
        return {"execution": public}

    async def _advance_deterministic_delivery_locked(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        execution_id: str,
    ) -> bool:
        row = self._execution_row(
            actor_id=actor_id,
            workspace_id=workspace_id,
            execution_id=execution_id,
        )
        item = dict(row)
        project = str(item["project_hint"])
        candidate_sha = str(item.get("candidate_sha") or "")
        pr_number = item.get("candidate_pr")
        if len(candidate_sha) != 40 or not isinstance(pr_number, int):
            raise StoreError("DELIVERY_CANDIDATE_MISSING", "Missing exact candidate receipt")

        evidence: dict[str, Any] = {}
        raw = item.get("delivery_evidence_json")
        if isinstance(raw, str) and raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    evidence = parsed
            except ValueError:
                pass

        main_sha = str(item.get("delivery_main_sha") or "")
        if not main_sha:
            merge_payload = await self.devcoveer.direct_project_action(
                project=project,
                operation="github_pr_merge",
                payload={
                    "pr": int(pr_number),
                    "expected_head_sha": candidate_sha,
                    "expected_base": "main",
                    "method": "squash",
                    "request_key": f"{execution_id}:merge:{candidate_sha[:16]}",
                },
            )
            merge = self._direct_action_data(merge_payload)
            merge_sha = str(merge.get("merge_sha") or "")
            await self.devcoveer.direct_project_action(
                project=project,
                operation="git_fetch",
                payload={
                    "remote": "origin",
                    "branch": "main",
                },
            )
            remote_payload = await self.devcoveer.direct_project_probe(
                project=project,
                operation="remote_head",
                payload={"remote": "origin", "branch": "main"},
            )
            remote = self._direct_result_data(remote_payload)
            main_sha = str(
                remote.get("fresh_remote_sha")
                or remote.get("local_tracking_sha")
                or merge_sha
            )
            if len(main_sha) != 40:
                raise StoreError(
                    "DELIVERY_MAIN_SHA_MISSING",
                    "Merged PR did not yield a fresh exact main SHA",
                )
            merged_at = _now_ms()
            evidence.update({
                "candidate_sha": candidate_sha,
                "pr": int(pr_number),
                "merge_sha": merge_sha,
                "main_sha": main_sha,
            })
            self._store_delivery_state(
                execution_id=execution_id,
                merge_sha=merge_sha or main_sha,
                main_sha=main_sha,
                merged_at_ms=merged_at,
                evidence=evidence,
            )
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET phase='delivery_main_ci',
                           phase_detail='Exact candidate merged; жду terminal main CI/release',
                           updated_at_ms=? WHERE id=?""",
                    (_now_ms(), execution_id),
                )
            return True

        await self.devcoveer.direct_project_action(
            project=project,
            operation="git_fetch",
            payload={
                "remote": "origin",
                "branch": "main",
            },
        )
        remote_payload = await self.devcoveer.direct_project_probe(
            project=project,
            operation="remote_head",
            payload={"remote": "origin", "branch": "main"},
        )
        remote = self._direct_result_data(remote_payload)
        current_main = str(
            remote.get("fresh_remote_sha") or remote.get("local_tracking_sha") or ""
        )
        if len(current_main) == 40 and current_main != main_sha:
            main_sha = current_main
            evidence["main_advanced_to"] = main_sha
            self._store_delivery_state(
                execution_id=execution_id,
                main_sha=main_sha,
                evidence=evidence,
            )

        status_payload = await self.devcoveer.direct_project_probe(
            project=project,
            operation="github_status",
            payload={"branch": "main", "failed_log_lines": 160},
        )
        github = self._direct_result_data(status_payload)
        branch = github.get("branch") if isinstance(github.get("branch"), dict) else {}
        observed_main = str(branch.get("head") or "")
        pending, main_success = self._github_rows_success(github)
        evidence["main_status"] = github
        evidence["main_sha"] = main_sha
        self._store_delivery_state(
            execution_id=execution_id,
            main_sha=main_sha,
            evidence=evidence,
        )

        if observed_main != main_sha or pending:
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='running',phase='delivery_main_ci',
                           phase_detail='Жду exact main SHA и завершение CI/release workflows',
                           error_code=NULL,finished_at_ms=NULL,updated_at_ms=?
                       WHERE id=?""",
                    (_now_ms(), execution_id),
                )
            return False

        merged_at = int(item.get("delivery_merged_at_ms") or 0)
        if (
            _now_ms() - merged_at < DELIVERY_CHECK_DISCOVERY_GRACE_MS
            and not (github.get("checks") or github.get("workflow_runs"))
        ):
            return False

        if not main_success:
            await self._start_post_merge_rework(item=item, evidence=evidence)
            return True

        job_id = str(item.get("delivery_job_id") or "")
        if not job_id:
            delivery_stage = self._active_stage(execution_id)
            deploy_attempt = (
                int(delivery_stage.get("recovery_attempts") or 0) + 1
                if delivery_stage
                else 1
            )
            deploy_payload = await self.devcoveer.direct_project_action(
                project=project,
                operation="run_tool",
                payload={
                    "tool": "python_script",
                    "path": "deploy/devcoveer_install.py",
                    "args": ["--sha", main_sha],
                    "timeout_seconds": 1200,
                    "python_env": "system",
                    "execution_mode": "job",
                    "request_key": f"{execution_id}:deploy:{main_sha[:16]}:a{deploy_attempt}",
                },
            )
            deploy = self._direct_action_data(deploy_payload)
            job_id = str(deploy.get("job_id") or "")
            if not job_id:
                raise StoreError(
                    "DELIVERY_JOB_MISSING",
                    "Deterministic deploy did not return a durable job id",
                )
            evidence["deploy_job_id"] = job_id
            self._store_delivery_state(
                execution_id=execution_id,
                job_id=job_id,
                evidence=evidence,
            )
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='running',phase='deploying',
                           phase_detail='Main CI зелёный; выполняю durable exact-main deploy',
                           error_code=NULL,finished_at_ms=NULL,updated_at_ms=?
                       WHERE id=?""",
                    (_now_ms(), execution_id),
                )
            return True

        job_payload = await self.devcoveer.direct_project_action(
            project=project,
            operation="job_status",
            payload={"job_id": job_id, "tail_lines": 80},
        )
        job = self._direct_action_data(job_payload)
        job_status = str(job.get("status") or "")
        if job_status == "succeeded":
            # A green command exit alone does not prove the requested version
            # is running. The existing exact-SHA installer already emits a
            # structured health/readback receipt; consume it, do not invent
            # another deployment verifier or claim delivery from agent prose.
            result = job.get("result") if isinstance(job.get("result"), dict) else {}
            tail = job.get("output_tail") if isinstance(job.get("output_tail"), dict) else {}
            output = str(result.get("stdout") or tail.get("stdout") or "").strip()
            try:
                receipt = json.loads(output)
            except (TypeError, ValueError):
                receipt = {}
            health = receipt.get("health") if isinstance(receipt, dict) else {}
            if not isinstance(health, dict):
                health = {}
            verified = (
                isinstance(receipt, dict)
                and result.get("exit_code") == 0
                and receipt.get("ok") is True
                and receipt.get("verified_main_sha") == main_sha
                and receipt.get("release_sha") == main_sha
                and health.get("ok") is True
                and health.get("release_sha") == main_sha
            )
            evidence["deploy_receipt_verified"] = verified
            if not verified:
                # Existing bounded retry handles a wrong/missing receipt just
                # like a failed deploy. Never mark the backlog done.
                job_status = "receipt_unverified"
        evidence["deploy_job"] = job
        self._store_delivery_state(
            execution_id=execution_id,
            job_id=job_id,
            evidence=evidence,
        )
        if job_status in {"running", "starting", "initializing", "unknown"}:
            return False
        if job_status != "succeeded":
            delivery_stage = self._active_stage(execution_id)
            attempt = (
                self._mark_recovery_attempt(str(delivery_stage["id"]))
                if delivery_stage
                else MAX_DELIVERY_DEPLOY_ATTEMPTS
            )
            if attempt < MAX_DELIVERY_DEPLOY_ATTEMPTS:
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE task_executions
                           SET delivery_job_id=NULL,status='running',phase='deploying',
                               phase_detail='Повторяю exact-main deploy после terminal infrastructure failure',
                               error_code='DELIVERY_DEPLOY_RETRY',
                               finished_at_ms=NULL,updated_at_ms=?
                           WHERE id=?""",
                        (_now_ms(), execution_id),
                    )
                return True
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='failed',phase='failed',
                           phase_detail='Deterministic deploy исчерпал bounded retry',
                           error_code='DELIVERY_DEPLOY_FAILED',
                           finished_at_ms=?,updated_at_ms=?
                       WHERE id=?""",
                    (_now_ms(), _now_ms(), execution_id),
                )
            return True

        deployed_at = _now_ms()
        evidence["deployed_main_sha"] = main_sha
        self._store_delivery_state(
            execution_id=execution_id,
            deployed_at_ms=deployed_at,
            evidence=evidence,
        )
        delivery_stage = self._active_stage(execution_id)
        if delivery_stage and delivery_stage.get("stage") == "delivery":
            self._finish_stage(
                stage_id=str(delivery_stage["id"]),
                status="completed",
                summary=f"Deterministic delivery completed for main {main_sha}.",
            )
        with self.store._lock:
            self.store.db.execute(
                """UPDATE task_executions
                   SET status='completed',phase='ready',
                       phase_detail='Реализация принята, main CI/release зелёные и production проверен',
                       result_summary=?,error_code=NULL,finished_at_ms=?,updated_at_ms=?
                   WHERE id=?""",
                (
                    f"Delivered exact main {main_sha}; deploy job {job_id} succeeded.",
                    deployed_at,
                    deployed_at,
                    execution_id,
                ),
            )
            for task_id in json.loads(item["task_ids_json"]):
                self.store.db.execute(
                    "UPDATE tasks SET state='done',updated_at_ms=? WHERE id=?",
                    (deployed_at, task_id),
                )
        return True

    @staticmethod
    def _codex_delivery_receipt(summary: str) -> dict[str, Any] | None:
        """Parse Codex's final evidence, not a worker-generated CI simulation."""
        for line in reversed(summary.splitlines()):
            if not line.startswith("DELIVERY_RECEIPT: "):
                continue
            try:
                receipt = json.loads(line.removeprefix("DELIVERY_RECEIPT: ").strip())
            except (ValueError, TypeError):
                return None
            if not isinstance(receipt, dict):
                return None
            main_sha = str(receipt.get("main_sha") or "").lower()
            evidence_url = str(receipt.get("evidence_url") or "")
            verification = str(receipt.get("verification") or "")
            android_update = receipt.get("android_update")
            android_url = receipt.get("android_release_url")
            if (
                receipt.get("status") != "delivered"
                or receipt.get("ci") != "passed"
                or re.fullmatch(r"[0-9a-f]{40}", main_sha) is None
                or not evidence_url.startswith("https://")
                or len(verification.strip()) < 24
                or not isinstance(android_update, bool)
                or (android_update and (
                    not isinstance(android_url, str)
                    or not android_url.startswith("https://")
                ))
            ):
                return None
            return receipt
        return None

    def recent_completions(
        self, *, actor_id: str, workspace_id: str, days: int = 7, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Actor-private completed executions across projects; read only."""
        self._authorize_owner(actor_id, workspace_id)
        since_ms = _now_ms() - min(30, max(1, days)) * 86_400_000
        with self.store._lock:
            runs = self.store.db.execute(
                """SELECT id,project_id,task_ids_json,finished_at_ms,
                          delivery_main_sha,result_summary,delivery_evidence_json
                   FROM task_executions
                   WHERE actor_id=? AND workspace_id=?
                     AND status='completed' AND finished_at_ms>=?
                   ORDER BY finished_at_ms DESC LIMIT ?""",
                (actor_id, workspace_id, since_ms, min(30, max(1, limit))),
            ).fetchall()
            task_ids = list(dict.fromkeys(
                str(task_id)
                for run in runs
                for task_id in json.loads(run["task_ids_json"])
            ))
            task_titles: dict[str, str] = {}
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                tasks = self.store.db.execute(
                    f"SELECT id,title FROM tasks WHERE id IN ({placeholders})",
                    task_ids,
                ).fetchall()
                task_titles = {str(row["id"]): str(row["title"]) for row in tasks}
        projects = {
            str(project["id"]): str(project["name"])
            for project in self.store.list_projects(actor_id, workspace_id)
        }
        return [{
            "id": str(run["id"]),
            "project_id": str(run["project_id"]),
            "project_name": projects.get(str(run["project_id"]), "Проект"),
            "titles": [
                task_titles[task_id]
                for task_id in json.loads(run["task_ids_json"])
                if task_id in task_titles
            ],
            "finished_at_ms": int(run["finished_at_ms"]),
            "main_sha": str(run["delivery_main_sha"] or ""),
            "summary": str(run["result_summary"] or "")[:500],
            "android_update": bool(
                self._decode_development_delivery(run["delivery_evidence_json"])
                .get("android_update") is True
            ),
        } for run in runs]

    @staticmethod
    def _decode_development_delivery(raw: Any) -> dict[str, Any]:
        try:
            value = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    async def _advance_codex_owner_locked(
        self, *, actor_id: str, workspace_id: str, execution_id: str
    ) -> bool:
        """One active Codex turn, one next-stage dispatch. No separate CI/deploy engine."""
        row = self._execution_row(
            actor_id=actor_id, workspace_id=workspace_id, execution_id=execution_id
        )
        item = dict(row)
        if item["status"] not in ACTIVE_EXECUTION_STATES:
            return False
        stage = self._active_stage(execution_id)
        if stage is None:
            now = _now_ms()
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='failed',phase='failed',
                           phase_detail='Нет подтверждённой стадии Codex',
                           error_code='DEVELOPMENT_STAGE_MISSING',
                           finished_at_ms=?,updated_at_ms=? WHERE id=?""",
                    (now, now, execution_id),
                )
            return True

        task_id = str(stage["devcoveer_task_id"] or "")
        response = await self.devcoveer.read_task(
            task_id, project=str(item["project_hint"]), detail="summary"
        )
        state = self._task_status_from_result(response)
        latest = response.get("latestTurn")
        latest = latest if isinstance(latest, dict) else {}
        summary = str(
            response.get("finalResponse") or latest.get("finalResponse")
            or response.get("content") or ""
        ).strip()[:12000]
        name = str(stage["stage"])
        if state == "running":
            return False
        now = _now_ms()
        usage = self._token_usage(response)

        if state == "interrupted":
            attempts = self._mark_recovery_attempt(str(stage["id"]))
            if attempts <= 1:
                await self._require_stage_capacity(
                    actor_id=actor_id, workspace_id=workspace_id,
                    model=str(stage["model"]), reasoning_effort=str(stage["reasoning_effort"]),
                )
                await self.devcoveer.continue_codex_task(
                    task_id, project=str(item["project_hint"]),
                    prompt=self._interrupted_resume_prompt(name),
                    access="read" if name in {"design", "review"} else "write",
                )
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE task_executions SET status='running',phase=?,
                           phase_detail='Codex возобновил прерванный этап',
                           error_code=NULL,finished_at_ms=NULL,updated_at_ms=?
                           WHERE id=?""",
                        ({"design":"designing","review":"reviewing",
                          "rework":"reworking","delivery":"delivering"}
                         .get(name, "implementing"), now, execution_id),
                    )
                return True
            state = "failed"
        if state in {"failed", "cancelled"}:
            self._finish_stage(
                stage_id=str(stage["id"]), status=state,
                summary=summary or "Codex stage ended unsuccessfully",
                token_usage=usage,
            )
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions SET status=?,phase='failed',
                       phase_detail='Codex не завершил этап',
                       error_code='DEVCOVEER_STAGE_FAILED',
                       result_summary=?,finished_at_ms=?,updated_at_ms=? WHERE id=?""",
                    ("failed", summary, now, now, execution_id),
                )
            return True
        if state != "completed":
            return False

        model, effort = str(item["model_profile"]).rsplit(":", 1)
        project = str(item["project_hint"])
        spec = str(item["spec_path"] or "")
        cycle = int(stage["cycle"] or 0)
        followup: tuple[str, int, str, str, str, str, str] | None = None
        receipt: dict[str, Any] | None = None
        if name == "design":
            followup = (
                "implementation", 0, model, effort, "write",
                self._implementation_prompt(
                    self._project_name(actor_id, workspace_id, str(item["project_id"])),
                    spec, execution_id,
                ),
                "implementing",
            )
        elif name in {"implementation", "rework"}:
            followup = (
                "review", cycle, QUALITY_MODEL, QUALITY_EFFORT, "read",
                self._review_prompt(spec, cycle, summary),
                "reviewing",
            )
        elif name == "review":
            verdict = self._review_verdict(summary)
            if verdict == "accepted":
                followup = (
                    "delivery", cycle, model, effort, "write",
                    self._delivery_prompt(spec), "delivering",
                )
            elif verdict == "rework_required" and cycle < MAX_REWORK_CYCLES:
                followup = (
                    "rework", cycle + 1, model, effort, "write",
                    self._rework_prompt(spec, summary, cycle + 1), "reworking",
                )
            elif verdict is None and self._stage_attempt_count(
                execution_id, "review", cycle=cycle
            ) < 2:
                self._mark_recovery_attempt(str(stage["id"]))
                followup = (
                    "review", cycle, QUALITY_MODEL, QUALITY_EFFORT, "read",
                    "Please complete your independent review and end with exactly "
                    "REVIEW_VERDICT: ACCEPTED or REVIEW_VERDICT: REWORK_REQUIRED. "
                    "Do not change code. Original specification: " + spec,
                    "reviewing",
                )
        elif name == "delivery":
            receipt = self._codex_delivery_receipt(summary)
            if receipt is None and cycle < 1:
                followup = (
                    "delivery", cycle + 1, model, effort, "write",
                    "Complete the existing authorized delivery or report the exact "
                    "blocking failure. Confirm real merged main, successful CI, live/"
                    "published verification and signed Android release if necessary. "
                    "Finish with the exact verified DELIVERY_RECEIPT JSON line specified "
                    "earlier ONLY after all checks really passed. Do not create a CI engine.",
                    "delivering",
                )
        else:
            return False

        if followup is not None:
            next_name, next_cycle, next_model, next_effort, access, prompt, phase = followup
            await self._require_stage_capacity(
                actor_id=actor_id, workspace_id=workspace_id,
                model=next_model, reasoning_effort=next_effort,
            )
            if name == "design":
                started = await self.devcoveer.start_codex_task(
                    project=project, prompt=prompt, model=next_model,
                    reasoning_effort=next_effort, access=access,
                )
                next_task = str(started.get("taskId") or started.get("taskReference") or "")
                if not next_task:
                    raise StoreError("DEVCOVEER_INVALID_RESPONSE", "No implementation task reference")
            else:
                next_task = (
                    str(item.get("quality_task_id") or "")
                    if next_name == "review"
                    else str(item.get("implementation_task_id") or "")
                )
                if not next_task:
                    raise StoreError("DEVCOVEER_INVALID_RESPONSE", "Existing Codex thread missing")
                await self.devcoveer.continue_codex_task(
                    next_task, project=project, prompt=prompt,
                    access=access, model=next_model, reasoning_effort=next_effort,
                )
            self._finish_stage(
                stage_id=str(stage["id"]), status="completed",
                summary=summary, review_verdict=(
                    self._review_verdict(summary) if name == "review" else None
                ), token_usage=usage,
            )
            self._record_stage(
                execution_id=execution_id, stage=next_name, cycle=next_cycle,
                model=next_model, reasoning_effort=next_effort,
                devcoveer_task_id=next_task,
            )
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions SET status='running',phase=?,
                       phase_detail=?,devcoveer_task_id=?,
                       quality_task_id=COALESCE(?,quality_task_id),
                       implementation_task_id=COALESCE(?,implementation_task_id),
                       review_cycle=?,error_code=NULL,result_summary=?,
                       finished_at_ms=NULL,updated_at_ms=? WHERE id=?""",
                    (phase, {
                        "implementing":"Codex разрабатывает и проверяет кандидат",
                        "reviewing":"Astra High проверяет результат",
                        "reworking":"Codex исправляет замечания ревью",
                        "delivering":"Codex выполняет CI, merge, релиз и проверку поставки",
                    }[phase], next_task,
                     next_task if next_name == "review" else None,
                     next_task if next_name in {"implementation", "rework"} else None,
                     next_cycle, summary, now, execution_id),
                )
            return True

        self._finish_stage(
            stage_id=str(stage["id"]), status="completed",
            summary=summary, review_verdict=(
                self._review_verdict(summary) if name == "review" else None
            ), token_usage=usage,
        )
        if name == "delivery" and receipt is not None:
            # This is a Codex-delivered readback: no parallel CI/deploy service.
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions SET status='completed',phase='ready',
                       phase_detail='Codex поставил и проверил результат',
                       result_summary=?,delivery_main_sha=?,
                       delivery_deployed_at_ms=?,delivery_evidence_json=?,
                       error_code=NULL,finished_at_ms=?,updated_at_ms=?
                       WHERE id=?""",
                    (summary[:6000], receipt["main_sha"], now,
                     json.dumps(receipt, ensure_ascii=False), now, now, execution_id),
                )
                for task_id in json.loads(item["task_ids_json"]):
                    self.store.db.execute(
                        "UPDATE tasks SET state='done',updated_at_ms=? WHERE id=?",
                        (now, task_id),
                    )
            return True
        error = "REVIEW_REWORK_LIMIT" if name == "review" else "DELIVERY_UNVERIFIED"
        with self.store._lock:
            self.store.db.execute(
                """UPDATE task_executions SET status='failed',phase='failed',
                   phase_detail='Не пройдена приёмка или поставка не подтверждена',
                   error_code=?,result_summary=?,finished_at_ms=?,updated_at_ms=?
                   WHERE id=?""",
                (error, summary, now, now, execution_id),
            )
        return True

    def _mark_recovery_attempt(self, stage_id: str) -> int:
        now = _now_ms()
        with self.store._lock:
            row = self.store.db.execute(
                "SELECT recovery_attempts FROM task_execution_stages WHERE id=?",
                (stage_id,),
            ).fetchone()
            attempts = int(row["recovery_attempts"] or 0) + 1 if row else 1
            self.store.db.execute(
                """UPDATE task_execution_stages
                   SET recovery_attempts=?,recovery_last_at_ms=?,updated_at_ms=?
                   WHERE id=?""",
                (attempts, now, now, stage_id),
            )
        return attempts

    def _stage_attempt_count(
        self,
        execution_id: str,
        stage_name: str,
        *,
        cycle: int | None = None,
    ) -> int:
        with self.store._lock:
            if cycle is None:
                row = self.store.db.execute(
                    """SELECT COUNT(*) AS value
                       FROM task_execution_stages
                       WHERE execution_id=? AND stage=?""",
                    (execution_id, stage_name),
                ).fetchone()
            else:
                row = self.store.db.execute(
                    """SELECT COUNT(*) AS value
                       FROM task_execution_stages
                       WHERE execution_id=? AND stage=? AND cycle=?""",
                    (execution_id, stage_name, int(cycle)),
                ).fetchone()
        return int(row["value"] if row else 0)

    def _queue_interrupted_recovery(
        self,
        *,
        item: dict[str, Any],
        stage: dict[str, Any],
        summary: str,
    ) -> dict[str, Any]:
        self._finish_stage(
            stage_id=str(stage["id"]),
            status="interrupted",
            summary=summary or "DevCoveer stage was interrupted and could not resume.",
        )
        now = _now_ms()
        with self.store._lock:
            self.store.db.execute(
                """UPDATE task_executions
                   SET status='running',phase='recovering',
                       phase_detail='Техническое прерывание: автоматически восстанавливаю ту же задачу',
                       result_summary=?,error_code='DEVELOPMENT_STAGE_INTERRUPTED',
                       finished_at_ms=NULL,updated_at_ms=?
                   WHERE id=?""",
                (summary[:12000], now, item["id"]),
            )
        row = self._execution_row(
            actor_id=str(item["actor_id"]),
            workspace_id=str(item["workspace_id"]),
            execution_id=str(item["id"]),
        )
        public = self._execution_public(row)
        public["update_check_recommended"] = False
        return {"execution": public}

    def _latest_stage(self, execution_id: str) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store.db.execute(
                """SELECT * FROM task_execution_stages
                   WHERE execution_id=?
                   ORDER BY created_at_ms DESC LIMIT 1""",
                (execution_id,),
            ).fetchone()
        return dict(row) if row else None

    def _latest_review_summary(self, execution_id: str, cycle: int) -> str:
        with self.store._lock:
            row = self.store.db.execute(
                """SELECT summary FROM task_execution_stages
                   WHERE execution_id=? AND stage='review'
                     AND cycle=? AND status='completed'
                   ORDER BY created_at_ms DESC LIMIT 1""",
                (execution_id, int(cycle)),
            ).fetchone()
        return str(row["summary"] or "") if row else ""

    @staticmethod
    def _dispatch_marker(
        execution_id: str,
        stage_name: str,
        cycle: int,
        attempt: int,
    ) -> str:
        compact = str(execution_id).removeprefix("devrun_")[:16]
        safe_stage = re.sub(r"[^a-z0-9_-]+", "-", stage_name.lower()).strip("-")
        return f"ODR-{compact}-{safe_stage}-{int(cycle)}-a{int(attempt)}"

    @staticmethod
    def _task_reference(task: dict[str, Any]) -> str:
        return str(
            task.get("taskId")
            or task.get("taskReference")
            or task.get("threadId")
            or ""
        ).strip()

    async def _find_marked_task(
        self,
        *,
        project: str,
        marker: str,
        model: str,
        access: str,
    ) -> str | None:
        history = await self.devcoveer.list_codex_tasks(
            project=project,
            search=marker,
            limit=20,
        )
        rows = history.get("tasks")
        if not isinstance(rows, list):
            raise StoreError(
                "DEVELOPMENT_DISPATCH_UNKNOWN",
                "DevCoveer task history did not return a task list",
            )
        matches: list[str] = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            searchable = "\n".join(
                str(raw.get(key) or "")
                for key in ("name", "title", "preview")
            )
            if marker not in searchable:
                continue
            backend = str(raw.get("backend") or raw.get("provider") or "").lower()
            if backend and backend not in {"codex", "native_codex"}:
                continue
            candidate_model = str(raw.get("model") or "")
            if candidate_model and candidate_model != model:
                continue
            candidate_access = str(raw.get("access") or "")
            if candidate_access and candidate_access != access:
                continue
            reference = self._task_reference(raw)
            if reference and reference not in matches:
                matches.append(reference)
        if len(matches) > 1:
            raise StoreError(
                "DEVELOPMENT_DISPATCH_AMBIGUOUS",
                "More than one DevCoveer task matches the durable dispatch marker",
            )
        return matches[0] if matches else None

    async def _start_marked_codex_task(
        self,
        *,
        project: str,
        marker: str,
        prompt: str,
        model: str,
        reasoning_effort: str,
        access: str,
    ) -> tuple[str, dict[str, Any]]:
        try:
            existing = await self._find_marked_task(
                project=project,
                marker=marker,
                model=model,
                access=access,
            )
        except DevCoveerError as exc:
            raise StoreError(
                "DEVELOPMENT_DISPATCH_UNKNOWN",
                "DevCoveer task history is temporarily unavailable",
            ) from exc
        if existing:
            return existing, {
                "status": "reconciled",
                "taskId": existing,
                "dispatchMarker": marker,
            }

        marked_prompt = marker + "\n" + prompt
        try:
            started = await self.devcoveer.start_codex_task(
                project=project,
                prompt=marked_prompt,
                model=model,
                reasoning_effort=reasoning_effort,
                access=access,
            )
        except DevCoveerError as exc:
            try:
                reconciled = await self._find_marked_task(
                    project=project,
                    marker=marker,
                    model=model,
                    access=access,
                )
            except (DevCoveerError, StoreError) as history_error:
                raise StoreError(
                    "DEVELOPMENT_DISPATCH_UNKNOWN",
                    "DevCoveer start outcome is unknown and history readback failed",
                ) from history_error
            if not reconciled:
                raise StoreError(
                    "DEVELOPMENT_DISPATCH_UNKNOWN",
                    "DevCoveer start outcome is unknown; retry will reconcile before dispatch",
                ) from exc
            return reconciled, {
                "status": "reconciled",
                "taskId": reconciled,
                "dispatchMarker": marker,
            }

        task_id = str(
            started.get("taskId") or started.get("taskReference") or ""
        ).strip()
        if not task_id:
            try:
                reconciled = await self._find_marked_task(
                    project=project,
                    marker=marker,
                    model=model,
                    access=access,
                )
            except (DevCoveerError, StoreError) as history_error:
                raise StoreError(
                    "DEVELOPMENT_DISPATCH_UNKNOWN",
                    "DevCoveer returned no task id and history readback failed",
                ) from history_error
            if reconciled:
                return reconciled, {
                    "status": "reconciled",
                    "taskId": reconciled,
                    "dispatchMarker": marker,
                }
            raise StoreError(
                "DEVELOPMENT_DISPATCH_UNKNOWN",
                "DevCoveer returned no task id; retry will reconcile before dispatch",
            )
        return task_id, started

    def _recovery_write_prompt(
        self,
        *,
        item: dict[str, Any],
        stage: dict[str, Any],
        summary: str,
    ) -> str:
        logical_stage = str(stage.get("stage") or "")
        cycle = int(stage.get("cycle") or 0)
        execution_id = str(item["id"])
        spec_path = str(item.get("spec_path") or "")
        prior_task = str(stage.get("devcoveer_task_id") or "")
        if logical_stage == "delivery":
            return f"""Autonomous delivery recovery for already-authorized execution {execution_id}.
Specification: {spec_path}
Previous delivery thread {prior_task} ended in terminal interrupted state.

Do not start the product work over. Inspect the existing Git branch/worktree, PRs, CI runs, release tags/manifests and production receipts first. Continue idempotently from the latest durable result. Merge only the accepted implementation, wait for required CI, refresh origin/main and deploy only the exact merged SHA from fresh origin/main history. If Android changed, bump to the next unused product version, publish the normal signed Android release/update manifest and verify the self-update path. Verify running service, static assets and release metadata all resolve to the same immutable release. Do not broaden scope."""
        review_summary = self._latest_review_summary(execution_id, cycle)
        return f"""Autonomous write-stage recovery for already-authorized execution {execution_id}.
Specification: {spec_path}
Stage: {logical_stage}
Cycle: {cycle}
Previous write thread: {prior_task}
Previous turn ended in terminal interrupted state.

Do not recreate the implementation from scratch and do not broaden scope. Inspect git status/log/branches and the existing specification first. Locate and continue the existing implementation branch containing the work for this execution; preserve its commits and unrelated work. If the checkout is already on that branch, continue there. Complete only the remaining fixes, run the available deterministic tests/build/browser/emulator checks, and commit the recovered changes on the same implementation branch.

Previous stage summary:
{summary[:6000]}

Latest independent review findings for this cycle:
{review_summary[:6000]}

Do NOT merge, deploy or release. Stop when the existing implementation is again ready for independent review."""

    async def _recover_failed_candidate_gate_locked(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        item: dict[str, Any],
        stage: dict[str, Any],
        summary: str,
        validation: dict[str, Any],
    ) -> bool:
        evidence = validation.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {}
        evidence_text = json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )[:8000]
        recovery_summary = (
            summary
            + "\n\nDeterministic candidate gate requires technical recovery "
            "before independent review. Reconcile with fresh origin/main and fix "
            "only the candidate/CI problems below; preserve the authorized scope.\n"
            + evidence_text
        )[:12000]
        try:
            recovered = await self._start_write_recovery_continuation(
                actor_id=actor_id,
                workspace_id=workspace_id,
                item=item,
                stage=stage,
                summary=recovery_summary,
            )
        except StoreError as exc:
            now = _now_ms()
            if exc.code in {
                "CODEX_CAPACITY_RESERVED",
                "CODEX_MODEL_UNAVAILABLE",
                "CODEX_REASONING_UNAVAILABLE",
            }:
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE task_executions
                           SET status='running',phase='capacity_wait',
                               phase_detail='Candidate recovery ждёт доступной Codex capacity',
                               error_code=?,finished_at_ms=NULL,updated_at_ms=?
                           WHERE id=?""",
                        (exc.code, now, item["id"]),
                    )
                return False
            if exc.code == "DEVELOPMENT_DISPATCH_UNKNOWN":
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE task_executions
                           SET status='running',phase='recovering',
                               phase_detail='Сверяю неизвестный результат запуска candidate recovery',
                               error_code=?,finished_at_ms=NULL,updated_at_ms=?
                           WHERE id=?""",
                        (exc.code, now, item["id"]),
                    )
                return False
            raise
        except DevCoveerError:
            now = _now_ms()
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='running',phase='recovering',
                           phase_detail='Повторяю техническое восстановление candidate gate',
                           error_code='DEVELOPMENT_CANDIDATE_RECOVERY_RETRY',
                           finished_at_ms=NULL,updated_at_ms=?
                       WHERE id=?""",
                    (now, item["id"]),
                )
            return False

        if recovered:
            now = _now_ms()
            reason = (
                "Разрешаю конфликт кандидата со свежим main до независимого ревью"
                if str(evidence.get("mergeable_state") or "") == "dirty"
                else "Исправляю deterministic CI/candidate gate до независимого ревью"
            )
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET phase_detail=?,error_code=NULL,finished_at_ms=NULL,
                           updated_at_ms=? WHERE id=?""",
                    (reason, now, item["id"]),
                )
        return recovered

    async def _start_write_recovery_continuation(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        item: dict[str, Any],
        stage: dict[str, Any],
        summary: str,
    ) -> bool:
        logical_stage = str(stage.get("stage") or "")
        if logical_stage not in {"implementation", "rework", "delivery"}:
            return False
        cycle = int(stage.get("cycle") or 0)
        attempts = self._stage_attempt_count(
            str(item["id"]),
            logical_stage,
            cycle=cycle,
        )
        if attempts >= MAX_INTERRUPTED_WRITE_CONTINUATIONS:
            now = _now_ms()
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='failed',phase='failed',
                           phase_detail='Автоматическое восстановление write-stage исчерпало безопасный предел',
                           error_code='AUTONOMOUS_RECOVERY_EXHAUSTED',
                           result_summary=?,finished_at_ms=?,updated_at_ms=?
                       WHERE id=?""",
                    (summary[:12000], now, now, item["id"]),
                )
            return False

        implementation_model, implementation_effort = str(
            item["model_profile"]
        ).rsplit(":", 1)
        remaining = await self._require_stage_capacity(
            actor_id=actor_id,
            workspace_id=workspace_id,
            model=implementation_model,
            reasoning_effort=implementation_effort,
        )
        marker = self._dispatch_marker(
            str(item["id"]),
            logical_stage,
            cycle,
            attempts + 1,
        )
        task_id, _started = await self._start_marked_codex_task(
            project=str(item["project_hint"]),
            marker=marker,
            prompt=self._recovery_write_prompt(
                item=item,
                stage=stage,
                summary=summary,
            ),
            model=implementation_model,
            reasoning_effort=implementation_effort,
            access="write",
        )

        self._finish_stage(
            stage_id=str(stage["id"]),
            status="superseded",
            summary=(
                summary
                or "Interrupted write-stage superseded by autonomous continuation."
            ),
        )
        self._record_stage(
            execution_id=str(item["id"]),
            stage=logical_stage,
            cycle=cycle,
            model=implementation_model,
            reasoning_effort=implementation_effort,
            devcoveer_task_id=task_id,
        )
        phase, detail = self._phase_after_resume(logical_stage)
        detail = (
            "Автоматическое продолжение поставки после технического прерывания"
            if logical_stage == "delivery"
            else "Автоматический continuation продолжает существующую реализацию"
        )
        now = _now_ms()
        with self.store._lock:
            if logical_stage in {"implementation", "rework"}:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='running',phase=?,phase_detail=?,
                           implementation_task_id=?,devcoveer_task_id=?,
                           quota_remaining_percent=?,
                           candidate_sha=NULL,candidate_branch=NULL,candidate_pr=NULL,
                           candidate_published_at_ms=NULL,candidate_evidence_json=NULL,
                           error_code=NULL,finished_at_ms=NULL,updated_at_ms=?
                       WHERE id=?""",
                    (
                        phase,
                        detail,
                        task_id,
                        task_id,
                        remaining,
                        now,
                        item["id"],
                    ),
                )
            else:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='running',phase='delivering',phase_detail=?,
                           devcoveer_task_id=?,quota_remaining_percent=?,
                           error_code=NULL,finished_at_ms=NULL,updated_at_ms=?
                       WHERE id=?""",
                    (
                        detail,
                        task_id,
                        remaining,
                        now,
                        item["id"],
                    ),
                )
        return True

    async def _recover_interrupted_execution_locked(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        execution_id: str,
    ) -> bool:
        row = self._execution_row(
            actor_id=actor_id,
            workspace_id=workspace_id,
            execution_id=execution_id,
        )
        item = dict(row)
        project_name = self._project_name(
            actor_id,
            workspace_id,
            str(item["project_id"]),
        )
        desired_project_hint = self._project_hint(
            actor_id,
            workspace_id,
            str(item["project_id"]),
            project_name,
        )
        if desired_project_hint != str(item.get("project_hint") or ""):
            now = _now_ms()
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET project_hint=?,status='running',phase='recovering',
                           phase_detail='Восстанавливаю стадию в каноническом code checkout',
                           error_code='DEVELOPMENT_TARGET_CHANGED',
                           finished_at_ms=NULL,updated_at_ms=?
                       WHERE id=?""",
                    (desired_project_hint, now, execution_id),
                )
            item["project_hint"] = desired_project_hint

        stage = self._latest_stage(execution_id)
        if stage is None or str(stage.get("status") or "") not in {
            "interrupted",
            "superseded",
        }:
            return False

        logical_stage = str(stage.get("stage") or "")
        if logical_stage in {"implementation", "rework", "delivery"}:
            try:
                return await self._start_write_recovery_continuation(
                    actor_id=actor_id,
                    workspace_id=workspace_id,
                    item=item,
                    stage=stage,
                    summary=str(stage.get("summary") or item.get("result_summary") or ""),
                )
            except StoreError as exc:
                now = _now_ms()
                if exc.code in {
                    "CODEX_CAPACITY_RESERVED",
                    "CODEX_MODEL_UNAVAILABLE",
                    "CODEX_REASONING_UNAVAILABLE",
                }:
                    with self.store._lock:
                        self.store.db.execute(
                            """UPDATE task_executions
                               SET status='running',phase='capacity_wait',
                                   phase_detail='Автоматическое восстановление ждёт доступной Codex capacity',
                                   error_code=?,finished_at_ms=NULL,updated_at_ms=?
                               WHERE id=?""",
                            (exc.code, now, execution_id),
                        )
                    return False
                if exc.code == "DEVELOPMENT_DISPATCH_UNKNOWN":
                    with self.store._lock:
                        self.store.db.execute(
                            """UPDATE task_executions
                               SET status='running',phase='recovering',
                                   phase_detail='Проверяю неизвестный результат запуска continuation перед повтором',
                                   error_code=?,finished_at_ms=NULL,updated_at_ms=?
                               WHERE id=?""",
                            (exc.code, now, execution_id),
                        )
                    return False
                if exc.code == "DEVELOPMENT_DISPATCH_AMBIGUOUS":
                    with self.store._lock:
                        self.store.db.execute(
                            """UPDATE task_executions
                               SET status='failed',phase='failed',
                                   phase_detail='Обнаружены неоднозначные дубли recovery-dispatch',
                                   error_code=?,finished_at_ms=?,updated_at_ms=?
                               WHERE id=?""",
                            (exc.code, now, now, execution_id),
                        )
                    return False
                raise
            except DevCoveerError:
                now = _now_ms()
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE task_executions
                           SET status='running',phase='recovering',
                               phase_detail='Продолжаю автоматическое восстановление после сбоя DevCoveer',
                               error_code='DEVELOPMENT_RECOVERY_RETRY',
                               finished_at_ms=NULL,updated_at_ms=?
                           WHERE id=?""",
                        (now, execution_id),
                    )
                return False

        # Read-only/design recovery can safely re-enter the existing logic.
        with self.store._lock:
            self.store.db.execute(
                """UPDATE task_execution_stages
                   SET status='running',finished_at_ms=NULL,updated_at_ms=?
                   WHERE id=?""",
                (_now_ms(), stage["id"]),
            )
            self.store.db.execute(
                """UPDATE task_executions
                   SET status='running',phase='recovering',
                       phase_detail='Автоматически восстанавливаю стадию',
                       error_code=NULL,finished_at_ms=NULL,updated_at_ms=?
                   WHERE id=?""",
                (_now_ms(), execution_id),
            )
        await self._advance_execution_locked(
            actor_id=actor_id,
            workspace_id=workspace_id,
            execution_id=execution_id,
            sync=True,
        )
        return True

    @staticmethod
    def _phase_after_resume(stage_name: str) -> tuple[str, str]:
        if stage_name == "design":
            return "designing", "Проектирование продолжено после прерывания"
        if stage_name == "review":
            return "reviewing", "Ревью продолжено после прерывания"
        if stage_name == "rework":
            return "reworking", "Исправление замечаний продолжено после прерывания"
        if stage_name == "delivery":
            return "delivering", "Поставка продолжена после прерывания"
        return "implementing", "Реализация продолжена после прерывания"

    async def status(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        execution_id: str | None = None,
        sync: bool = True,
    ) -> dict[str, Any]:
        """Read durable execution state without advancing the workflow."""

        self._authorize_owner(actor_id, workspace_id)
        _ = sync
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
        public = self._execution_public(row)
        evidence = public.get("delivery_evidence")
        public["update_check_recommended"] = (
            public["status"] == "completed"
            and (
                row["pipeline_mode"] != "codex_owner"
                or (isinstance(evidence, dict) and evidence.get("android_update") is True)
            )
        )
        return {"execution": public}

    async def _advance_execution_locked(
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

        project_name = self._project_name(
            actor_id,
            workspace_id,
            str(item["project_id"]),
        )
        desired_project_hint = self._project_hint(
            actor_id,
            workspace_id,
            str(item["project_id"]),
            project_name,
        )
        stored_project_hint = str(item.get("project_hint") or "")
        if desired_project_hint != stored_project_hint:
            if (
                str(stage.get("stage") or "") == "design"
                and not str(item.get("implementation_task_id") or "").strip()
            ):
                remaining = await self._require_stage_capacity(
                    actor_id=actor_id,
                    workspace_id=workspace_id,
                    model=QUALITY_MODEL,
                    reasoning_effort=QUALITY_EFFORT,
                )
                restarted = await self.devcoveer.start_codex_task(
                    project=desired_project_hint,
                    prompt=str(item.get("prompt") or ""),
                    model=QUALITY_MODEL,
                    reasoning_effort=QUALITY_EFFORT,
                )
                restarted_task_id = str(
                    restarted.get("taskId")
                    or restarted.get("taskReference")
                    or ""
                ).strip()
                if not restarted_task_id:
                    raise StoreError(
                        "DEVCOVEER_INVALID_RESPONSE",
                        "DevCoveer did not return a recovery design task id",
                    )
                now = _now_ms()
                self._finish_stage(
                    stage_id=str(stage["id"]),
                    status="superseded",
                    summary=(
                        "Design stage superseded because the exact DevCoveer "
                        "project target changed."
                    ),
                )
                self._record_stage(
                    execution_id=str(item["id"]),
                    stage="design",
                    cycle=0,
                    model=QUALITY_MODEL,
                    reasoning_effort=QUALITY_EFFORT,
                    devcoveer_task_id=restarted_task_id,
                )
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE task_executions
                           SET project_hint=?,quality_task_id=?,devcoveer_task_id=?,
                               status='running',phase='designing',
                               phase_detail='Проектирование восстановлено в корректном рабочем checkout',
                               quota_remaining_percent=?,error_code=NULL,
                               finished_at_ms=NULL,updated_at_ms=?
                           WHERE id=?""",
                        (
                            desired_project_hint,
                            restarted_task_id,
                            restarted_task_id,
                            remaining,
                            now,
                            item["id"],
                        ),
                    )
                row = self._execution_row(
                    actor_id=actor_id,
                    workspace_id=workspace_id,
                    execution_id=str(item["id"]),
                )
                public = self._execution_public(row)
                public["update_check_recommended"] = False
                return {"execution": public}

            now = _now_ms()
            logical_stage = str(stage.get("stage") or "")
            self._finish_stage(
                stage_id=str(stage["id"]),
                status="interrupted",
                summary=(
                    "Stage interrupted because the exact DevCoveer checkout changed "
                    f"from {stored_project_hint} to {desired_project_hint}; autonomous "
                    "recovery will continue the same authorized execution in the new target."
                ),
            )
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET project_hint=?,status='running',phase='recovering',
                           phase_detail='Автоматически переношу текущую стадию в корректный checkout',
                           error_code='DEVELOPMENT_TARGET_CHANGED',
                           finished_at_ms=NULL,updated_at_ms=?
                       WHERE id=?""",
                    (desired_project_hint, now, item["id"]),
                )
            item["project_hint"] = desired_project_hint

            if logical_stage in {"implementation", "rework", "delivery"}:
                recovered = await self._start_write_recovery_continuation(
                    actor_id=actor_id,
                    workspace_id=workspace_id,
                    item=item,
                    stage=stage,
                    summary=(
                        "Exact DevCoveer target changed; continue the existing Git work "
                        "in the correct checkout without broadening scope."
                    ),
                )
                if recovered:
                    row = self._execution_row(
                        actor_id=actor_id,
                        workspace_id=workspace_id,
                        execution_id=str(item["id"]),
                    )
                    public = self._execution_public(row)
                    public["update_check_recommended"] = False
                    return {"execution": public}

            if logical_stage == "review":
                cycle = int(stage.get("cycle") or 0)
                review_attempt = (
                    self._stage_attempt_count(str(item["id"]), "review", cycle=cycle)
                    + 1
                )
                marker = self._dispatch_marker(
                    str(item["id"]), "review", cycle, review_attempt
                )
                review_task_id, _ = await self._start_marked_codex_task(
                    project=desired_project_hint,
                    marker=marker,
                    prompt=self._recovery_review_prompt(
                        str(item.get("spec_path") or ""),
                        cycle,
                        self._candidate_evidence_text(item),
                    ),
                    model=QUALITY_MODEL,
                    reasoning_effort=QUALITY_EFFORT,
                    access="read",
                )
                self._record_stage(
                    execution_id=str(item["id"]),
                    stage="review",
                    cycle=cycle,
                    model=QUALITY_MODEL,
                    reasoning_effort=QUALITY_EFFORT,
                    devcoveer_task_id=review_task_id,
                )
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE task_executions
                           SET quality_task_id=?,devcoveer_task_id=?,
                               status='running',phase='reviewing',
                               phase_detail='Read-only ревью перенесено в корректный checkout',
                               error_code=NULL,finished_at_ms=NULL,updated_at_ms=?
                           WHERE id=?""",
                        (review_task_id, review_task_id, now, item["id"]),
                    )
                row = self._execution_row(
                    actor_id=actor_id,
                    workspace_id=workspace_id,
                    execution_id=str(item["id"]),
                )
                public = self._execution_public(row)
                public["update_check_recommended"] = False
                return {"execution": public}

            # Unknown future stage types remain in technical recovery, never a false owner decision.
            row = self._execution_row(
                actor_id=actor_id,
                workspace_id=workspace_id,
                execution_id=str(item["id"]),
            )
            public = self._execution_public(row)
            public["update_check_recommended"] = False
            return {"execution": public}

        result = await self.devcoveer.read_task(
            str(stage["devcoveer_task_id"]),
            project=str(item["project_hint"]),
            detail="summary",
        )
        turn_status = self._task_status_from_result(result)
        latest = result.get("latestTurn") if isinstance(result.get("latestTurn"), dict) else {}
        summary = str(
            result.get("finalResponse")
            or latest.get("finalResponse")
            or result.get("content")
            or ""
        ).strip()[:12000]
        token_usage = self._token_usage(result)

        if turn_status == "interrupted":
            artifact_summary = self._design_artifact_readback(
                item=item,
                stage=stage,
                result=result,
            )
            if artifact_summary:
                turn_status = "completed"
                summary = artifact_summary
            else:
                logical_stage = str(stage.get("stage") or "")
                stage_id = str(stage["id"])
                recovery_attempts = int(stage.get("recovery_attempts") or 0)
                if recovery_attempts < MAX_INTERRUPTED_RESUME_ATTEMPTS:
                    self._mark_recovery_attempt(stage_id)
                    resume_access = (
                        "read" if logical_stage in {"design", "review"} else "write"
                    )
                    try:
                        resumed = await self.devcoveer.continue_codex_task(
                            str(stage["devcoveer_task_id"]),
                            project=str(item["project_hint"]),
                            prompt=self._interrupted_resume_prompt(logical_stage),
                            access=resume_access,
                        )
                    except DevCoveerError:
                        resumed = {"status": "interrupted"}
                    resumed_status = self._task_status_from_result(resumed)
                    if resumed_status not in {
                        "failed",
                        "cancelled",
                        "interrupted",
                    }:
                        phase, detail = self._phase_after_resume(logical_stage)
                        now = _now_ms()
                        with self.store._lock:
                            self.store.db.execute(
                                """UPDATE task_executions
                                   SET status='running',phase=?,phase_detail=?,
                                       error_code=NULL,finished_at_ms=NULL,
                                       updated_at_ms=? WHERE id=?""",
                                (phase, detail, now, item["id"]),
                            )
                        row = self._execution_row(
                            actor_id=actor_id,
                            workspace_id=workspace_id,
                            execution_id=str(item["id"]),
                        )
                        public = self._execution_public(row)
                        public["update_check_recommended"] = False
                        return {"execution": public}

                if logical_stage == "design":
                    attempts = self._stage_attempt_count(str(item["id"]), "design")
                    if attempts < MAX_INTERRUPTED_DESIGN_ATTEMPTS:
                        remaining = await self._require_stage_capacity(
                            actor_id=actor_id,
                            workspace_id=workspace_id,
                            model=QUALITY_MODEL,
                            reasoning_effort=QUALITY_EFFORT,
                        )
                        try:
                            restarted = await self.devcoveer.start_codex_task(
                                project=str(item["project_hint"]),
                                prompt=str(item.get("prompt") or ""),
                                model=QUALITY_MODEL,
                                reasoning_effort=QUALITY_EFFORT,
                                access="write",
                            )
                        except DevCoveerError:
                            restarted = {}
                        restarted_task_id = str(
                            restarted.get("taskId")
                            or restarted.get("taskReference")
                            or ""
                        ).strip()
                        if restarted_task_id:
                            self._finish_stage(
                                stage_id=stage_id,
                                status="superseded",
                                summary=(
                                    summary
                                    or "Interrupted design could not resume; safe design restart created."
                                ),
                                token_usage=token_usage,
                            )
                            self._record_stage(
                                execution_id=str(item["id"]),
                                stage="design",
                                cycle=int(stage.get("cycle") or 0),
                                model=QUALITY_MODEL,
                                reasoning_effort=QUALITY_EFFORT,
                                devcoveer_task_id=restarted_task_id,
                            )
                            now = _now_ms()
                            with self.store._lock:
                                self.store.db.execute(
                                    """UPDATE task_executions
                                       SET quality_task_id=?,devcoveer_task_id=?,
                                           status='running',phase='designing',
                                           phase_detail='Проектирование безопасно перезапущено после прерывания',
                                           quota_remaining_percent=?,error_code=NULL,
                                           finished_at_ms=NULL,updated_at_ms=?
                                       WHERE id=?""",
                                    (
                                        restarted_task_id,
                                        restarted_task_id,
                                        remaining,
                                        now,
                                        item["id"],
                                    ),
                                )
                            row = self._execution_row(
                                actor_id=actor_id,
                                workspace_id=workspace_id,
                                execution_id=str(item["id"]),
                            )
                            public = self._execution_public(row)
                            public["update_check_recommended"] = False
                            return {"execution": public}

                if logical_stage == "review":
                    cycle = int(stage.get("cycle") or 0)
                    attempts = self._stage_attempt_count(
                        str(item["id"]),
                        "review",
                        cycle=cycle,
                    )
                    if attempts < MAX_INTERRUPTED_REVIEW_ATTEMPTS:
                        remaining = await self._require_stage_capacity(
                            actor_id=actor_id,
                            workspace_id=workspace_id,
                            model=QUALITY_MODEL,
                            reasoning_effort=QUALITY_EFFORT,
                        )
                        try:
                            restarted = await self.devcoveer.start_codex_task(
                                project=str(item["project_hint"]),
                                prompt=self._recovery_review_prompt(
                                    str(item.get("spec_path") or ""),
                                    cycle,
                                    self._candidate_evidence_text(item),
                                ),
                                model=QUALITY_MODEL,
                                reasoning_effort=QUALITY_EFFORT,
                                access="read",
                            )
                        except DevCoveerError:
                            restarted = {}
                        restarted_task_id = str(
                            restarted.get("taskId")
                            or restarted.get("taskReference")
                            or ""
                        ).strip()
                        if restarted_task_id:
                            self._finish_stage(
                                stage_id=stage_id,
                                status="superseded",
                                summary=(
                                    summary
                                    or "Interrupted review could not resume; recovery review created."
                                ),
                                token_usage=token_usage,
                            )
                            self._record_stage(
                                execution_id=str(item["id"]),
                                stage="review",
                                cycle=cycle,
                                model=QUALITY_MODEL,
                                reasoning_effort=QUALITY_EFFORT,
                                devcoveer_task_id=restarted_task_id,
                            )
                            now = _now_ms()
                            with self.store._lock:
                                self.store.db.execute(
                                    """UPDATE task_executions
                                       SET quality_task_id=?,devcoveer_task_id=?,
                                           status='running',phase='reviewing',
                                           phase_detail='Ревью безопасно восстановлено после прерывания',
                                           quota_remaining_percent=?,error_code=NULL,
                                           finished_at_ms=NULL,updated_at_ms=?
                                       WHERE id=?""",
                                    (
                                        restarted_task_id,
                                        restarted_task_id,
                                        remaining,
                                        now,
                                        item["id"],
                                    ),
                                )
                            row = self._execution_row(
                                actor_id=actor_id,
                                workspace_id=workspace_id,
                                execution_id=str(item["id"]),
                            )
                            public = self._execution_public(row)
                            public["update_check_recommended"] = False
                            return {"execution": public}

                if logical_stage in {"implementation", "rework", "delivery"}:
                    try:
                        recovered = await self._start_write_recovery_continuation(
                            actor_id=actor_id,
                            workspace_id=workspace_id,
                            item=item,
                            stage=stage,
                            summary=summary,
                        )
                    except StoreError as exc:
                        if exc.code in {
                            "CODEX_CAPACITY_RESERVED",
                            "CODEX_MODEL_UNAVAILABLE",
                            "CODEX_REASONING_UNAVAILABLE",
                        }:
                            now = _now_ms()
                            with self.store._lock:
                                self.store.db.execute(
                                    """UPDATE task_executions
                                       SET status='running',phase='capacity_wait',
                                           phase_detail='Автоматическое восстановление ждёт доступной Codex capacity',
                                           error_code=?,finished_at_ms=NULL,updated_at_ms=?
                                       WHERE id=?""",
                                    (exc.code, now, item["id"]),
                                )
                            recovered = False
                        else:
                            raise
                    except DevCoveerError:
                        recovered = False
                    if recovered:
                        row = self._execution_row(
                            actor_id=actor_id,
                            workspace_id=workspace_id,
                            execution_id=str(item["id"]),
                        )
                        public = self._execution_public(row)
                        public["update_check_recommended"] = False
                        return {"execution": public}

                return self._queue_interrupted_recovery(
                    item=item,
                    stage=stage,
                    summary=summary,
                )

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
                           error_code=NULL,finished_at_ms=NULL,
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

        validation_evidence = ""
        if turn_status == "completed" and stage["stage"] in {"implementation", "rework"}:
            try:
                validation = await self._candidate_validation(item=item, stage=stage)
            except (DevCoveerError, StoreError) as exc:
                now = _now_ms()
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE task_executions
                           SET status='running',phase='validating',
                               phase_detail='Повторяю deterministic публикацию/CI readback',
                               error_code='DEVELOPMENT_VALIDATION_RETRY',
                               result_summary=?,finished_at_ms=NULL,updated_at_ms=?
                           WHERE id=?""",
                        (summary, now, item["id"]),
                    )
                row = self._execution_row(
                    actor_id=actor_id,
                    workspace_id=workspace_id,
                    execution_id=item["id"],
                )
                public = self._execution_public(row)
                public["update_check_recommended"] = False
                return {"execution": public}

            validation_evidence = json.dumps(
                validation["evidence"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )[:8000]
            if validation.get("ready") is not True:
                now = _now_ms()
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE task_executions
                           SET status='running',phase='validating',
                               phase_detail=?,error_code=NULL,result_summary=?,
                               finished_at_ms=NULL,updated_at_ms=?
                           WHERE id=?""",
                        (
                            str(validation.get("detail") or "Проверяется опубликованный кандидат"),
                            summary,
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

            if validation.get("successful") is not True:
                await self._recover_failed_candidate_gate_locked(
                    actor_id=actor_id,
                    workspace_id=workspace_id,
                    item=item,
                    stage=stage,
                    summary=summary,
                    validation=validation,
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
        if stage["stage"] == "review" and verdict == "accepted":
            try:
                delivery_validation = await self._candidate_validation(item=item, stage=stage)
            except (DevCoveerError, StoreError):
                now = _now_ms()
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE task_executions
                           SET status='running',phase='validating',
                               phase_detail='Ревью принято; повторяю deterministic delivery gate',
                               error_code='DEVELOPMENT_VALIDATION_RETRY',
                               result_summary=?,finished_at_ms=NULL,updated_at_ms=?
                           WHERE id=?""",
                        (summary, now, item["id"]),
                    )
                row = self._execution_row(
                    actor_id=actor_id,
                    workspace_id=workspace_id,
                    execution_id=item["id"],
                )
                public = self._execution_public(row)
                public["update_check_recommended"] = False
                return {"execution": public}

            if delivery_validation.get("ready") is not True:
                now = _now_ms()
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE task_executions
                           SET status='running',phase='validating',
                               phase_detail=?,error_code=NULL,result_summary=?,
                               finished_at_ms=NULL,updated_at_ms=?
                           WHERE id=?""",
                        (
                            str(delivery_validation.get("detail") or "Ревью принято; жду deterministic delivery gate"),
                            summary,
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

            if delivery_validation.get("successful") is not True:
                evidence_text = json.dumps(
                    delivery_validation.get("evidence") or {},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )[:8000]
                summary = (
                    summary
                    + "\n\nDeterministic delivery gate rejected ACCEPTED verdict. "
                    "Fix the terminal candidate/CI failures below before delivery:\n"
                    + evidence_text
                )[:12000]
                verdict = "rework_required"

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
                        str(item["id"]),
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
                               error_code=NULL,finished_at_ms=NULL,
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
                quality_restarted = False
                if quality_task_id:
                    try:
                        await self.devcoveer.continue_codex_task(
                            quality_task_id,
                            project=str(item["project_hint"]),
                            prompt=self._review_prompt(
                                spec_path, cycle, validation_evidence or self._candidate_evidence_text(item)
                            ),
                            access="read",
                            model=QUALITY_MODEL,
                            reasoning_effort=QUALITY_EFFORT,
                        )
                    except DevCoveerError:
                        quality_task_id = ""
                if not quality_task_id:
                    review_attempt = self._stage_attempt_count(
                        str(item["id"]), "review", cycle=cycle
                    ) + 1
                    marker = self._dispatch_marker(
                        str(item["id"]), "review", cycle, review_attempt
                    )
                    quality_task_id, _ = await self._start_marked_codex_task(
                        project=str(item["project_hint"]),
                        marker=marker,
                        prompt=self._recovery_review_prompt(
                            spec_path, cycle, validation_evidence or self._candidate_evidence_text(item)
                        ),
                        model=QUALITY_MODEL,
                        reasoning_effort=QUALITY_EFFORT,
                        access="read",
                    )
                    quality_restarted = True
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
                               phase_detail=?,quality_task_id=?,devcoveer_task_id=?,
                               quota_remaining_percent=?,result_summary=?,
                               error_code=NULL,finished_at_ms=NULL,
                               updated_at_ms=? WHERE id=?""",
                        (
                            (
                                f"Read-only ревью восстановлено новым quality-thread, цикл {cycle}"
                                if quality_restarted
                                else f"Сильная модель принимает реализацию, цикл {cycle}"
                            ),
                            quality_task_id,
                            quality_task_id,
                            remaining,
                            summary,
                            now,
                            item["id"],
                        ),
                    )

            elif stage["stage"] == "review":
                if verdict == "accepted":
                    row = self._execution_row(
                        actor_id=actor_id,
                        workspace_id=workspace_id,
                        execution_id=str(item["id"]),
                    )
                    return await self._begin_deterministic_delivery(
                        item=dict(row),
                        review_summary=summary,
                        cycle=cycle,
                    )
                elif verdict == "rework_required":
                    if cycle >= MAX_REWORK_CYCLES:
                        with self.store._lock:
                            self.store.db.execute(
                                """UPDATE task_executions
                                   SET status='failed',phase='failed',
                                       phase_detail=?,
                                       result_summary=?,error_code='REVIEW_REWORK_LIMIT',
                                       finished_at_ms=?,updated_at_ms=? WHERE id=?""",
                                (
                                    f"Автоматический quality loop исчерпал {MAX_REWORK_CYCLES} циклов без приёмки",
                                    summary,
                                    now,
                                    now,
                                    item["id"],
                                ),
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
                        rework_task_id = implementation_task_id
                        rework_restarted = False
                        if implementation_task_id:
                            try:
                                await self.devcoveer.continue_codex_task(
                                    implementation_task_id,
                                    project=str(item["project_hint"]),
                                    prompt=self._rework_prompt(spec_path, summary, next_cycle),
                                    access="write",
                                    model=implementation_model,
                                    reasoning_effort=implementation_effort,
                                )
                            except DevCoveerError:
                                rework_task_id = ""
                        if not rework_task_id:
                            rework_attempt = self._stage_attempt_count(
                                str(item["id"]), "rework", cycle=next_cycle
                            ) + 1
                            marker = self._dispatch_marker(
                                str(item["id"]), "rework", next_cycle, rework_attempt
                            )
                            rework_task_id, _ = await self._start_marked_codex_task(
                                project=str(item["project_hint"]),
                                marker=marker,
                                prompt=self._recovery_write_prompt(
                                    item=item,
                                    stage={
                                        "stage": "rework",
                                        "cycle": next_cycle,
                                        "devcoveer_task_id": implementation_task_id,
                                    },
                                    summary=summary,
                                ),
                                model=implementation_model,
                                reasoning_effort=implementation_effort,
                                access="write",
                            )
                            rework_restarted = True
                        self._record_stage(
                            execution_id=str(item["id"]),
                            stage="rework",
                            cycle=next_cycle,
                            model=implementation_model,
                            reasoning_effort=implementation_effort,
                            devcoveer_task_id=rework_task_id,
                        )
                        with self.store._lock:
                            self.store.db.execute(
                                """UPDATE task_executions
                                   SET status='running',phase='reworking',
                                       phase_detail=?,implementation_task_id=?,
                                       devcoveer_task_id=?,
                                       quota_remaining_percent=?,review_cycle=?,
                                       result_summary=?,error_code=NULL,
                                       finished_at_ms=NULL,updated_at_ms=? WHERE id=?""",
                                (
                                    (
                                        f"Rework восстановлен новым continuation, цикл {next_cycle}"
                                        if rework_restarted
                                        else f"Исправление замечаний, цикл {next_cycle}"
                                    ),
                                    rework_task_id,
                                    rework_task_id,
                                    remaining,
                                    next_cycle,
                                    summary,
                                    now,
                                    item["id"],
                                ),
                            )
                else:
                    remaining = await self._require_stage_capacity(
                        actor_id=actor_id,
                        workspace_id=workspace_id,
                        model=QUALITY_MODEL,
                        reasoning_effort=QUALITY_EFFORT,
                    )
                    restarted = await self.devcoveer.start_codex_task(
                        project=str(item["project_hint"]),
                        prompt=self._recovery_review_prompt(
                            spec_path, cycle, self._candidate_evidence_text(item)
                        ),
                        model=QUALITY_MODEL,
                        reasoning_effort=QUALITY_EFFORT,
                        access="read",
                    )
                    restarted_task_id = str(
                        restarted.get("taskId")
                        or restarted.get("taskReference")
                        or ""
                    ).strip()
                    if not restarted_task_id:
                        raise StoreError(
                            "DEVCOVEER_INVALID_RESPONSE",
                            "DevCoveer did not return a recovery review task id",
                        )
                    self._record_stage(
                        execution_id=str(item["id"]),
                        stage="review",
                        cycle=cycle,
                        model=QUALITY_MODEL,
                        reasoning_effort=QUALITY_EFFORT,
                        devcoveer_task_id=restarted_task_id,
                    )
                    with self.store._lock:
                        self.store.db.execute(
                            """UPDATE task_executions
                               SET status='running',phase='reviewing',
                                   phase_detail='Повторяю read-only ревью после неясного verdict',
                                   quality_task_id=?,devcoveer_task_id=?,
                                   quota_remaining_percent=?,result_summary=?,
                                   error_code=NULL,finished_at_ms=NULL,updated_at_ms=?
                               WHERE id=?""",
                            (
                                restarted_task_id,
                                restarted_task_id,
                                remaining,
                                summary,
                                now,
                                item["id"],
                            ),
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
            retryable_capacity = code in {
                "CODEX_CAPACITY_RESERVED",
                "CODEX_MODEL_UNAVAILABLE",
                "CODEX_REASONING_UNAVAILABLE",
            }
            with self.store._lock:
                if retryable_capacity:
                    self.store.db.execute(
                        """UPDATE task_execution_stages
                           SET status='running',finished_at_ms=NULL,updated_at_ms=?
                           WHERE id=?""",
                        (now, stage["id"]),
                    )
                    self.store.db.execute(
                        """UPDATE task_executions
                           SET status='running',phase='capacity_wait',
                               phase_detail='Следующая стадия автоматически ждёт доступной Codex capacity',
                               error_code=?,result_summary=?,finished_at_ms=NULL,
                               updated_at_ms=? WHERE id=?""",
                        (str(code)[:120], summary, now, item["id"]),
                    )
                elif isinstance(exc, DevCoveerError) or code == "DEVELOPMENT_DISPATCH_UNKNOWN":
                    self.store.db.execute(
                        """UPDATE task_execution_stages
                           SET status='running',finished_at_ms=NULL,updated_at_ms=?
                           WHERE id=?""",
                        (now, stage["id"]),
                    )
                    self.store.db.execute(
                        """UPDATE task_executions
                           SET status='running',phase='recovering',
                               phase_detail='Автоматически сверяю результат перехода между стадиями',
                               error_code=?,result_summary=?,finished_at_ms=NULL,
                               updated_at_ms=? WHERE id=?""",
                        (str(code)[:120], summary, now, item["id"]),
                    )
                else:
                    self.store.db.execute(
                        """UPDATE task_executions
                           SET status='failed',phase='failed',
                               phase_detail='Не удалось перейти к следующей стадии',
                               error_code=?,result_summary=?,finished_at_ms=?,
                               updated_at_ms=? WHERE id=?""",
                        (str(code)[:120], summary, now, now, item["id"]),
                    )

        row = self._execution_row(
            actor_id=actor_id,
            workspace_id=workspace_id,
            execution_id=item["id"],
        )
        public = self._execution_public(row)
        public["update_check_recommended"] = public["status"] == "completed"
        return {"execution": public}

    async def _resume_blocked_rework_limit_locked(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        execution_id: str,
    ) -> bool:
        """Resume the same authorized write thread after an older rework cap."""

        self._authorize_owner(actor_id, workspace_id)
        row = self._execution_row(
            actor_id=actor_id,
            workspace_id=workspace_id,
            execution_id=execution_id,
        )
        item = dict(row)
        current_cycle = int(item.get("review_cycle") or 0)
        if (
            item.get("status") != "blocked"
            or item.get("error_code") != "REVIEW_REWORK_LIMIT"
        ):
            return False
        if current_cycle >= MAX_REWORK_CYCLES:
            now = _now_ms()
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='failed',phase='failed',
                           phase_detail=?,
                           error_code='REVIEW_REWORK_LIMIT',
                           finished_at_ms=?,updated_at_ms=?
                       WHERE id=?""",
                    (
                        f"Автоматический quality loop исчерпал {MAX_REWORK_CYCLES} циклов без приёмки",
                        now,
                        now,
                        execution_id,
                    ),
                )
            return True

        with self.store._lock:
            review = self.store.db.execute(
                """SELECT * FROM task_execution_stages
                   WHERE execution_id=? AND stage='review'
                     AND status='completed'
                     AND review_verdict='rework_required'
                     AND cycle=?
                   ORDER BY created_at_ms DESC LIMIT 1""",
                (execution_id, current_cycle),
            ).fetchone()
        if review is None:
            return False

        implementation_task_id = str(
            item.get("implementation_task_id") or ""
        ).strip()
        if not implementation_task_id:
            return False

        implementation_model, implementation_effort = str(
            item["model_profile"]
        ).rsplit(":", 1)
        next_cycle = current_cycle + 1
        review_summary = str(review["summary"] or item.get("result_summary") or "")
        spec_path = str(item.get("spec_path") or "")

        try:
            remaining = await self._require_stage_capacity(
                actor_id=actor_id,
                workspace_id=workspace_id,
                model=implementation_model,
                reasoning_effort=implementation_effort,
            )
            await self.devcoveer.continue_codex_task(
                implementation_task_id,
                project=str(item["project_hint"]),
                prompt=self._rework_prompt(
                    spec_path,
                    review_summary,
                    next_cycle,
                ),
                access="write",
                model=implementation_model,
                reasoning_effort=implementation_effort,
            )
        except Exception as exc:
            now = _now_ms()
            code = exc.code if isinstance(exc, StoreError) else type(exc).__name__
            self._record_stage(
                execution_id=execution_id,
                stage="rework",
                cycle=next_cycle,
                model=implementation_model,
                reasoning_effort=implementation_effort,
                devcoveer_task_id=implementation_task_id,
            )
            recovery_stage = self._active_stage(execution_id)
            if recovery_stage is not None:
                self._finish_stage(
                    stage_id=str(recovery_stage["id"]),
                    status="interrupted",
                    summary=(
                        "Legacy rework continuation did not produce a confirmed running "
                        f"turn ({str(code)[:120]}). Autonomous recovery will reconcile Git "
                        "state before starting a replacement continuation."
                    ),
                )
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='running',phase='recovering',
                           phase_detail='Автоматически восстанавливаю rework после технического сбоя',
                           review_cycle=?,devcoveer_task_id=?,
                           error_code='DEVELOPMENT_STAGE_INTERRUPTED',
                           finished_at_ms=NULL,updated_at_ms=?
                       WHERE id=?""",
                    (next_cycle, implementation_task_id, now, execution_id),
                )
            return False

        self._record_stage(
            execution_id=execution_id,
            stage="rework",
            cycle=next_cycle,
            model=implementation_model,
            reasoning_effort=implementation_effort,
            devcoveer_task_id=implementation_task_id,
        )
        now = _now_ms()
        with self.store._lock:
            self.store.db.execute(
                """UPDATE task_executions
                   SET status='running',phase='reworking',
                       phase_detail=?,devcoveer_task_id=?,
                       quota_remaining_percent=?,review_cycle=?,
                       result_summary=?,error_code=NULL,finished_at_ms=NULL,
                       updated_at_ms=? WHERE id=?""",
                (
                    f"Исправление замечаний, цикл {next_cycle}",
                    implementation_task_id,
                    remaining,
                    next_cycle,
                    review_summary,
                    now,
                    execution_id,
                ),
            )
        return True

    @staticmethod
    def _owner_resume_marker(payload: dict[str, Any]) -> dict[str, Any]:
        task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
        latest = (
            payload.get("latestTurn")
            if isinstance(payload.get("latestTurn"), dict)
            else {}
        )
        return {
            "task_id": str(
                task.get("taskId")
                or task.get("taskReference")
                or payload.get("taskId")
                or payload.get("taskReference")
                or ""
            ),
            "task_updated_at": task.get("updatedAt"),
            "turn_id": str(
                latest.get("turnId")
                or latest.get("messageId")
                or payload.get("turnId")
                or ""
            ),
            "turn_status": str(latest.get("status") or payload.get("status") or ""),
        }

    @staticmethod
    def _owner_resume_provider_advanced(
        baseline: dict[str, Any],
        current: dict[str, Any],
    ) -> bool:
        before_turn = str(baseline.get("turn_id") or "")
        after_turn = str(current.get("turn_id") or "")
        return bool(after_turn and after_turn != before_turn)

    async def _apply_owner_resume_locked(
        self,
        *,
        row: Any,
        resume_row: Any,
        provider_receipt: dict[str, Any],
    ) -> dict[str, Any]:
        execution_id = str(row["id"])
        quality_task_id = str(row["quality_task_id"] or "").strip()
        cycle = int(row["review_cycle"] or 0)
        active_stage = self._active_stage(execution_id)
        if (
            active_stage is None
            or active_stage.get("stage") != "review"
            or str(active_stage.get("devcoveer_task_id") or "") != quality_task_id
        ):
            self._record_stage(
                execution_id=execution_id,
                stage="review",
                cycle=cycle,
                model=QUALITY_MODEL,
                reasoning_effort=QUALITY_EFFORT,
                devcoveer_task_id=quality_task_id,
            )
        now = _now_ms()
        remaining = resume_row["quota_remaining_percent"]
        if remaining is None:
            remaining = row["quota_remaining_percent"]
        with self.store._lock:
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                self.store.db.execute(
                    """UPDATE development_owner_resumes
                       SET status='applied',provider_receipt_json=?,updated_at_ms=?
                       WHERE execution_id=? AND command_id=?""",
                    (
                        json.dumps(
                            provider_receipt,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        now,
                        execution_id,
                        resume_row["command_id"],
                    ),
                )
                self.store.db.execute(
                    """UPDATE task_executions SET
                           status='running',phase='reviewing',
                           phase_detail='Ответ владельца принят; сильная модель повторяет ревью',
                           devcoveer_task_id=?,quota_remaining_percent=?,
                           error_code=NULL,finished_at_ms=NULL,updated_at_ms=?
                       WHERE id=?""",
                    (quality_task_id, remaining, now, execution_id),
                )
                self.store.db.execute("COMMIT")
            except Exception:
                self.store.db.execute("ROLLBACK")
                raise
        current = self._execution_row(
            actor_id=str(row["actor_id"]),
            workspace_id=str(row["workspace_id"]),
            execution_id=execution_id,
        )
        public = self._execution_public(current)
        public["update_check_recommended"] = False
        return {"execution": public, "resumed": True}

    async def _reconcile_owner_resume_locked(
        self,
        *,
        row: Any,
        resume_row: Any,
    ) -> dict[str, Any] | None:
        baseline_raw = str(resume_row["provider_baseline_json"] or "").strip()
        if not baseline_raw:
            return None
        try:
            baseline = json.loads(baseline_raw)
        except ValueError:
            return None
        try:
            payload = await self.devcoveer.read_task(
                str(row["quality_task_id"]),
                project=str(row["project_hint"]),
                detail="summary",
            )
        except Exception:
            return None
        current_marker = self._owner_resume_marker(payload)
        if not self._owner_resume_provider_advanced(baseline, current_marker):
            return None
        return await self._apply_owner_resume_locked(
            row=row,
            resume_row=resume_row,
            provider_receipt=current_marker,
        )

    async def resume_needs_owner(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        execution_id: str,
        command_id: str,
        answer: str,
    ) -> dict[str, Any]:
        """Resume a previously authorized run without creating a new execution."""

        self._authorize_owner(actor_id, workspace_id)
        clean_command = str(command_id or "").strip()
        clean_answer = str(answer or "").strip()
        if not clean_command or len(clean_command) > 160:
            raise StoreError("INVALID_ARGUMENT", "Owner resume command_id is invalid")
        if not clean_answer or len(clean_answer) > 12000:
            raise StoreError("INVALID_ARGUMENT", "Owner resume answer is invalid")
        answer_sha = hashlib.sha256(clean_answer.encode("utf-8")).hexdigest()

        async with self._transition_guard():
            row = self._execution_row(
                actor_id=actor_id,
                workspace_id=workspace_id,
                execution_id=execution_id,
            )
            with self.store._lock:
                existing = self.store.db.execute(
                    """SELECT * FROM development_owner_resumes
                       WHERE execution_id=? AND command_id=?""",
                    (execution_id, clean_command),
                ).fetchone()
            if existing:
                if existing["answer_sha256"] != answer_sha:
                    raise StoreError(
                        "DEVELOPMENT_RESUME_CONFLICT",
                        "Owner resume command was already used with another answer",
                    )
                if existing["status"] == "applied":
                    public = self._execution_public(row)
                    public["update_check_recommended"] = public["status"] == "completed"
                    return {"execution": public, "resumed": row["status"] == "running"}
                reconciled = await self._reconcile_owner_resume_locked(
                    row=row,
                    resume_row=existing,
                )
                if reconciled is not None:
                    return reconciled
                raise StoreError(
                    "DEVELOPMENT_RESUME_OUTCOME_UNKNOWN",
                    "The prior owner resume outcome is not confirmed; do not repeat it blindly",
                )

            # A lost response remains the same semantic mutation even if the UI
            # generates a fresh command id. Reconcile it before considering any
            # new dispatch, otherwise one owner answer can create two review turns.
            with self.store._lock:
                pending = self.store.db.execute(
                    """SELECT * FROM development_owner_resumes
                       WHERE execution_id=?
                         AND status IN ('dispatching','dispatch_unknown')
                       ORDER BY created_at_ms DESC LIMIT 1""",
                    (execution_id,),
                ).fetchone()
            if pending:
                reconciled = await self._reconcile_owner_resume_locked(
                    row=row,
                    resume_row=pending,
                )
                if reconciled is not None:
                    if pending["answer_sha256"] == answer_sha:
                        return reconciled
                    raise StoreError(
                        "DEVELOPMENT_NOT_WAITING_FOR_OWNER",
                        "A prior owner answer already resumed this execution",
                    )
                raise StoreError(
                    "DEVELOPMENT_RESUME_OUTCOME_UNKNOWN",
                    "A prior owner resume outcome is still unknown; refusing a second dispatch",
                )

            if row["status"] != "blocked" or row["phase"] != "needs_owner":
                raise StoreError(
                    "DEVELOPMENT_NOT_WAITING_FOR_OWNER",
                    "Development execution is not waiting for an owner answer",
                )
            if row["error_code"] not in {"REVIEW_REWORK_LIMIT", "REVIEW_VERDICT_MISSING"}:
                raise StoreError(
                    "DEVELOPMENT_OWNER_RESUME_UNSUPPORTED",
                    "This owner blocker cannot be safely resumed from a conversation answer",
                )
            with self.store._lock:
                active = self.store.db.execute(
                    """SELECT id FROM task_executions
                       WHERE actor_id=? AND id<>?
                         AND status IN ('starting','running')
                       ORDER BY created_at_ms DESC LIMIT 1""",
                    (actor_id, execution_id),
                ).fetchone()
            pending_other_resume = self._owner_resume_effect_in_flight(
                actor_id,
                exclude_execution_id=execution_id,
            )
            if active or pending_other_resume:
                raise StoreError(
                    "DEVELOPMENT_EXECUTION_ACTIVE",
                    "Another owner development execution or unresolved external resume is already active",
                )

            quality_task_id = str(row["quality_task_id"] or "").strip()
            if not quality_task_id:
                raise StoreError("DEVELOPMENT_STAGE_MISSING", "Quality thread is unavailable")
            remaining = await self._require_stage_capacity(
                actor_id=actor_id,
                workspace_id=workspace_id,
                model=QUALITY_MODEL,
                reasoning_effort=QUALITY_EFFORT,
            )
            try:
                before_payload = await self.devcoveer.read_task(
                    quality_task_id,
                    project=str(row["project_hint"]),
                    detail="summary",
                )
            except Exception as exc:
                raise StoreError(
                    "DEVELOPMENT_RESUME_RECONCILIATION_UNAVAILABLE",
                    "Quality thread cannot be read before owner resume dispatch",
                ) from exc
            baseline = self._owner_resume_marker(before_payload)
            now = _now_ms()
            with self.store._lock:
                self.store.db.execute(
                    """INSERT INTO development_owner_resumes(
                           execution_id,command_id,answer_sha256,answer_text,status,
                           provider_baseline_json,quota_remaining_percent,
                           created_at_ms,updated_at_ms)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        execution_id,
                        clean_command,
                        answer_sha,
                        clean_answer,
                        "dispatching",
                        json.dumps(
                            baseline,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        remaining,
                        now,
                        now,
                    ),
                )
            try:
                provider_response = await self.devcoveer.continue_codex_task(
                    quality_task_id,
                    project=str(row["project_hint"]),
                    prompt=f"""The platform owner answered a durable needs-owner question for the
already-authorized execution {execution_id}. Preserve the existing specification,
task_ids, project target and implementation scope. Do not start a new development
task and do not broaden scope.

Previous blocker: {row['error_code']}
Previous review summary:
{str(row['result_summary'] or '')[:8000]}

Owner answer:
{clean_answer}

Re-evaluate the existing implementation using this answer. If the implementation
can now be accepted, end with exactly:
REVIEW_VERDICT: ACCEPTED
If material implementation changes are still required, end with exactly:
REVIEW_VERDICT: REWORK_REQUIRED
Before the verdict give concise findings.""",
                    access="read",
                    model=QUALITY_MODEL,
                    reasoning_effort=QUALITY_EFFORT,
                )
            except Exception as exc:
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE development_owner_resumes
                           SET status='dispatch_unknown',updated_at_ms=?
                           WHERE execution_id=? AND command_id=?""",
                        (_now_ms(), execution_id, clean_command),
                    )
                    resume_row = self.store.db.execute(
                        """SELECT * FROM development_owner_resumes
                           WHERE execution_id=? AND command_id=?""",
                        (execution_id, clean_command),
                    ).fetchone()
                reconciled = await self._reconcile_owner_resume_locked(
                    row=row,
                    resume_row=resume_row,
                )
                if reconciled is not None:
                    return reconciled
                raise StoreError(
                    "DEVELOPMENT_RESUME_OUTCOME_UNKNOWN",
                    "Owner resume dispatch outcome is unknown; reconciliation is required",
                ) from exc

            with self.store._lock:
                resume_row = self.store.db.execute(
                    """SELECT * FROM development_owner_resumes
                       WHERE execution_id=? AND command_id=?""",
                    (execution_id, clean_command),
                ).fetchone()
            return await self._apply_owner_resume_locked(
                row=row,
                resume_row=resume_row,
                provider_receipt=self._owner_resume_marker(provider_response),
            )

    async def _recover_legacy_evidence_loop_locked(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        execution_id: str,
    ) -> bool:
        row = self._execution_row(
            actor_id=actor_id,
            workspace_id=workspace_id,
            execution_id=execution_id,
        )
        item = dict(row)
        status = str(item.get("status") or "")
        phase = str(item.get("phase") or "")
        has_implementation = bool(
            str(item.get("implementation_task_id") or "").strip()
        )
        has_candidate = bool(
            str(item.get("candidate_sha") or "").strip()
            or str(item.get("candidate_branch") or "").strip()
            or item.get("candidate_pr")
        )
        initial_legacy_failure = (
            status == "failed"
            and item.get("error_code") == "REVIEW_REWORK_LIMIT"
            and has_implementation
            and not str(item.get("candidate_evidence_json") or "").strip()
        )
        recoverable_missing_stage = (
            status == "blocked"
            and item.get("error_code") == "DEVELOPMENT_STAGE_MISSING"
            and has_implementation
            and has_candidate
        )
        continuing_validation = (
            status == "running"
            and phase == "validating"
            and self._active_stage(execution_id) is None
            and has_implementation
        )
        if not (
            initial_legacy_failure
            or recoverable_missing_stage
            or continuing_validation
        ):
            return False

        project_name = self._project_name(
            actor_id,
            workspace_id,
            str(item["project_id"]),
        )
        desired_project_hint = self._project_hint(
            actor_id,
            workspace_id,
            str(item["project_id"]),
            project_name,
        )
        if desired_project_hint != str(item.get("project_hint") or ""):
            item["project_hint"] = desired_project_hint

        with self.store._lock:
            write_stage_row = self.store.db.execute(
                """SELECT * FROM task_execution_stages
                   WHERE execution_id=? AND stage IN ('implementation','rework')
                   ORDER BY created_at_ms DESC LIMIT 1""",
                (execution_id,),
            ).fetchone()
        if write_stage_row is None:
            return False
        write_stage = dict(write_stage_row)

        now = _now_ms()
        with self.store._lock:
            self.store.db.execute(
                """UPDATE task_executions
                   SET project_hint=?,status='running',phase='validating',
                       phase_detail='Восстанавливаю ранее созданный кандидат и deterministic CI evidence',
                       error_code=NULL,finished_at_ms=NULL,updated_at_ms=?
                   WHERE id=?""",
                (desired_project_hint, now, execution_id),
            )

        try:
            validation = await self._candidate_validation(
                item=item,
                stage=write_stage,
            )
        except (DevCoveerError, StoreError) as exc:
            now = _now_ms()
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='running',phase='validating',
                           phase_detail='Повторяю восстановление candidate/CI evidence',
                           error_code='DEVELOPMENT_VALIDATION_RETRY',
                           finished_at_ms=NULL,updated_at_ms=?
                       WHERE id=?""",
                    (now, execution_id),
                )
            return False

        evidence = validation.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {}
        if evidence.get("candidate_status") == "not_publishable":
            try:
                return await self._start_write_recovery_continuation(
                    actor_id=actor_id,
                    workspace_id=workspace_id,
                    item=item,
                    stage=write_stage,
                    summary=str(item.get("result_summary") or ""),
                )
            except (DevCoveerError, StoreError):
                now = _now_ms()
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE task_executions
                           SET status='running',phase='recovering',
                               phase_detail='Автоматически восстанавливаю committed candidate',
                               error_code='DEVELOPMENT_CANDIDATE_RECOVERY_RETRY',
                               finished_at_ms=NULL,updated_at_ms=?
                           WHERE id=?""",
                        (now, execution_id),
                    )
                return False

        if validation.get("ready") is not True:
            now = _now_ms()
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE task_executions
                       SET status='running',phase='validating',
                           phase_detail=?,error_code=NULL,
                           finished_at_ms=NULL,updated_at_ms=?
                       WHERE id=?""",
                    (
                        str(validation.get("detail") or "Жду deterministic CI"),
                        now,
                        execution_id,
                    ),
                )
            return True

        if validation.get("successful") is not True:
            return await self._recover_failed_candidate_gate_locked(
                actor_id=actor_id,
                workspace_id=workspace_id,
                item=item,
                stage=write_stage,
                summary=str(item.get("result_summary") or ""),
                validation=validation,
            )

        remaining = await self._require_stage_capacity(
            actor_id=actor_id,
            workspace_id=workspace_id,
            model=QUALITY_MODEL,
            reasoning_effort=QUALITY_EFFORT,
        )
        evidence_text = json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )[:8000]
        review_cycle = 0
        review_attempt = self._stage_attempt_count(
            execution_id,
            "review",
            cycle=review_cycle,
        ) + 1
        marker = self._dispatch_marker(
            execution_id,
            "legacy-validation-review",
            review_cycle,
            review_attempt,
        )
        quality_task_id, _ = await self._start_marked_codex_task(
            project=desired_project_hint,
            marker=marker,
            prompt=self._recovery_review_prompt(
                str(item.get("spec_path") or ""),
                review_cycle,
                evidence_text,
            ),
            model=QUALITY_MODEL,
            reasoning_effort=QUALITY_EFFORT,
            access="read",
        )
        self._record_stage(
            execution_id=execution_id,
            stage="review",
            cycle=review_cycle,
            model=QUALITY_MODEL,
            reasoning_effort=QUALITY_EFFORT,
            devcoveer_task_id=quality_task_id,
        )
        now = _now_ms()
        with self.store._lock:
            self.store.db.execute(
                """UPDATE task_executions
                   SET status='running',phase='reviewing',
                       phase_detail='Повторное независимое ревью с восстановленным CI evidence',
                       quality_task_id=?,devcoveer_task_id=?,
                       quota_remaining_percent=?,review_cycle=0,
                       error_code=NULL,finished_at_ms=NULL,updated_at_ms=?
                   WHERE id=?""",
                (
                    quality_task_id,
                    quality_task_id,
                    remaining,
                    now,
                    execution_id,
                ),
            )
        return True

    async def advance_active_once(self) -> int:
        """Advance durable executions independently of any client/status read."""

        advanced = 0
        reconciled_execution_ids: set[str] = set()
        with self.store._lock:
            pending_resumes = self.store.db.execute(
                """SELECT r.execution_id,r.command_id,e.actor_id,e.workspace_id
                   FROM development_owner_resumes r
                   JOIN task_executions e ON e.id=r.execution_id
                   WHERE r.status IN ('dispatching','dispatch_unknown')
                     AND e.status='blocked' AND e.phase=?
                   ORDER BY r.created_at_ms ASC LIMIT 8""",
                ("needs_owner",),
            ).fetchall()
        for candidate in pending_resumes:
            async with self._transition_guard():
                row = self._execution_row(
                    actor_id=str(candidate["actor_id"]),
                    workspace_id=str(candidate["workspace_id"]),
                    execution_id=str(candidate["execution_id"]),
                )
                with self.store._lock:
                    resume_row = self.store.db.execute(
                        """SELECT * FROM development_owner_resumes
                           WHERE execution_id=? AND command_id=?""",
                        (candidate["execution_id"], candidate["command_id"]),
                    ).fetchone()
                if resume_row is None or resume_row["status"] == "applied":
                    continue
                reconciled = await self._reconcile_owner_resume_locked(
                    row=row,
                    resume_row=resume_row,
                )
                if reconciled is not None:
                    advanced += 1
                    reconciled_execution_ids.add(str(candidate["execution_id"]))

        with self.store._lock:
            rows = self.store.db.execute(
                """SELECT id,actor_id,workspace_id,status,phase,error_code,review_cycle
                   FROM task_executions
                   WHERE status IN ('starting','running')
                      OR (
                          status='blocked'
                          AND (
                              error_code='DEVELOPMENT_STAGE_INTERRUPTED'
                              OR error_code='REVIEW_REWORK_LIMIT'
                              OR (
                                  error_code='DEVELOPMENT_STAGE_MISSING'
                                  AND implementation_task_id IS NOT NULL
                                  AND (
                                      candidate_sha IS NOT NULL
                                      OR candidate_branch IS NOT NULL
                                      OR candidate_pr IS NOT NULL
                                  )
                              )
                          )
                      )
                      OR (
                          status='failed'
                          AND error_code='REVIEW_REWORK_LIMIT'
                          AND implementation_task_id IS NOT NULL
                          AND (
                              candidate_evidence_json IS NULL
                              OR candidate_evidence_json=''
                          )
                      )
                   ORDER BY created_at_ms ASC LIMIT 10"""
            ).fetchall()
        for candidate in rows:
            if str(candidate["id"]) in reconciled_execution_ids:
                # Reconciliation is one durable transition; provider-result
                # advancement belongs to the next bounded worker tick.
                continue
            async with self._transition_guard():
                with self.store._lock:
                    fresh = self.store.db.execute(
                        """SELECT status,phase,error_code,review_cycle
                           FROM task_executions WHERE id=?""",
                        (candidate["id"],),
                    ).fetchone()
                if not fresh:
                    continue
                # New owner executions use Codex for the entire delivery and
                # real CI. Retain the old deterministic path only to recover
                # pre-existing in-flight runs from earlier releases.
                with self.store._lock:
                    mode_row = self.store.db.execute(
                        "SELECT pipeline_mode FROM task_executions WHERE id=?",
                        (candidate["id"],),
                    ).fetchone()
                if mode_row and mode_row["pipeline_mode"] == "codex_owner":
                    if fresh["status"] in ACTIVE_EXECUTION_STATES:
                        if await self._advance_codex_owner_locked(
                            actor_id=str(candidate["actor_id"]),
                            workspace_id=str(candidate["workspace_id"]),
                            execution_id=str(candidate["id"]),
                        ):
                            advanced += 1
                    continue
                if (
                    fresh["status"] == "blocked"
                    and fresh["error_code"] == "DEVELOPMENT_STAGE_MISSING"
                ):
                    recovered = await self._recover_legacy_evidence_loop_locked(
                        actor_id=str(candidate["actor_id"]),
                        workspace_id=str(candidate["workspace_id"]),
                        execution_id=str(candidate["id"]),
                    )
                    if recovered:
                        advanced += 1
                    continue
                if (
                    fresh["status"] == "failed"
                    and fresh["error_code"] == "REVIEW_REWORK_LIMIT"
                ):
                    recovered = await self._recover_legacy_evidence_loop_locked(
                        actor_id=str(candidate["actor_id"]),
                        workspace_id=str(candidate["workspace_id"]),
                        execution_id=str(candidate["id"]),
                    )
                    if recovered:
                        advanced += 1
                    continue
                if (
                    fresh["status"] == "running"
                    and str(fresh["phase"] or "") == "validating"
                    and self._active_stage(str(candidate["id"])) is None
                ):
                    recovered = await self._recover_legacy_evidence_loop_locked(
                        actor_id=str(candidate["actor_id"]),
                        workspace_id=str(candidate["workspace_id"]),
                        execution_id=str(candidate["id"]),
                    )
                    if recovered:
                        advanced += 1
                    continue

                if (
                    fresh["status"] == "blocked"
                    and fresh["error_code"] == "DEVELOPMENT_STAGE_INTERRUPTED"
                ):
                    recovered = await self._recover_interrupted_execution_locked(
                        actor_id=str(candidate["actor_id"]),
                        workspace_id=str(candidate["workspace_id"]),
                        execution_id=str(candidate["id"]),
                    )
                    if recovered:
                        advanced += 1
                    continue
                if (
                    fresh["status"] == "blocked"
                    and fresh["error_code"] == "REVIEW_REWORK_LIMIT"
                ):
                    resumed = await self._resume_blocked_rework_limit_locked(
                        actor_id=str(candidate["actor_id"]),
                        workspace_id=str(candidate["workspace_id"]),
                        execution_id=str(candidate["id"]),
                    )
                    if resumed:
                        advanced += 1
                    continue
                if fresh["status"] not in ACTIVE_EXECUTION_STATES:
                    continue
                if str(fresh["phase"] or "") in {
                    "delivery_merge",
                    "delivery_main_ci",
                    "deploying",
                }:
                    progressed = await self._advance_deterministic_delivery_locked(
                        actor_id=str(candidate["actor_id"]),
                        workspace_id=str(candidate["workspace_id"]),
                        execution_id=str(candidate["id"]),
                    )
                    if progressed:
                        advanced += 1
                    continue
                if str(fresh["phase"] or "") in {"recovering", "capacity_wait"}:
                    latest = self._latest_stage(str(candidate["id"]))
                    if latest and str(latest.get("status") or "") == "interrupted":
                        recovered = await self._recover_interrupted_execution_locked(
                            actor_id=str(candidate["actor_id"]),
                            workspace_id=str(candidate["workspace_id"]),
                            execution_id=str(candidate["id"]),
                        )
                        if recovered:
                            advanced += 1
                        continue
                await self._advance_execution_locked(
                    actor_id=str(candidate["actor_id"]),
                    workspace_id=str(candidate["workspace_id"]),
                    execution_id=str(candidate["id"]),
                    sync=True,
                )
                advanced += 1
        return advanced

    async def _background_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.advance_active_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "development background sync failed: %s",
                    type(exc).__name__,
                )
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._background_interval_seconds,
                )
            except TimeoutError:
                pass

    async def start_background(self) -> None:
        if self._background_task is not None and not self._background_task.done():
            return
        stop_event = asyncio.Event()
        self._background_stop = stop_event
        self._background_task = asyncio.create_task(
            self._background_loop(stop_event),
            name="projects-hub-development-worker",
        )

    async def stop_background(self) -> None:
        task = self._background_task
        stop_event = self._background_stop
        self._background_task = None
        self._background_stop = None
        if stop_event is not None:
            stop_event.set()
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

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
        await self.stop_background()
        await self.devcoveer.close()
