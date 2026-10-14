from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from typing import Any

from .analytics_client import AnalyticsBridgeClient, AnalyticsBridgeError
from .collaboration import CollaborationService, COMMAND_RE
from .development import DevelopmentService
from .store import DurableStore, StoreError


TERMINAL_ANALYSIS = {"completed", "failed", "cancelled"}
QUESTION_STATES = {"open", "resolved", "skipped", "unknown", "deferred"}
DISPOSITIONS = {"answer", "skip", "unknown", "later"}
ANALYSIS_MODELS = {"kimi_k3", "deepseek"}
MAX_ANALYSIS_REPAIR_ATTEMPTS = 1
PURPOSES = {"requirements", "edge_cases", "architecture", "code_review", "ideas"}
JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)

ANALYSIS_ORIGINS = {"mira", "slack"}
SOURCE_KINDS = {"project_note", "project_document", "project_task", "feature_idea", "board_object"}
QUESTION_PRIORITIES = {"critical", "high", "normal", "low"}
CONTINUATION_KINDS = {"analysis_followup", "external_continuation"}
PACKAGE_STATES = {
    "active",
    "paused",
    "waiting_answers",
    "all_answered",
    "completed",
    "partially_completed",
    "cancelled",
}


def _now_ms() -> int:
    return round(time.time() * 1000)


def _id(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class CollaborationAnalysisService:
    """Provided-only analysis -> typed questions -> durable bounded continuation."""

    def __init__(
        self,
        store: DurableStore,
        collaboration: CollaborationService,
        *,
        bridge: AnalyticsBridgeClient | None = None,
        development: DevelopmentService | None = None,
        poll_seconds: float = 2.0,
    ) -> None:
        self.store = store
        self.collaboration = collaboration
        self.bridge = bridge or AnalyticsBridgeClient()
        self.development = development
        self.question_events = None  # Set by the existing MCP/Events integration.
        self.poll_seconds = max(0.5, min(float(poll_seconds), 30.0))
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self._lock = asyncio.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self.store._lock:
            self.store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS collaboration_analyses(
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    note_id TEXT NOT NULL REFERENCES project_notes(id),
                    initiating_actor_id TEXT NOT NULL REFERENCES actors(id),
                    addressed_to_actor_id TEXT NOT NULL REFERENCES actors(id),
                    command_id TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    model_alias TEXT NOT NULL,
                    producer_actor_id TEXT REFERENCES actors(id),
                    external_analysis_id TEXT,
                    source_verified INTEGER NOT NULL DEFAULT 0,
                    purpose TEXT NOT NULL,
                    question TEXT NOT NULL,
                    source_snapshot_json TEXT NOT NULL,
                    evidence_bundle TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provider_task_id TEXT,
                    initial_provider_task_id TEXT,
                    repair_attempt_count INTEGER NOT NULL DEFAULT 0,
                    repair_model_alias TEXT,
                    result_markdown TEXT NOT NULL DEFAULT '',
                    result_json TEXT,
                    error_code TEXT,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    finished_at_ms INTEGER,
                    origin TEXT NOT NULL DEFAULT 'mira',
                    source_kind TEXT NOT NULL DEFAULT 'project_note',
                    source_id TEXT NOT NULL DEFAULT '',
                    source_revision INTEGER,
                    source_repository_sha TEXT,
                    scope_json TEXT NOT NULL DEFAULT '{}',
                    slack_workspace_id TEXT,
                    slack_channel_id TEXT,
                    slack_thread_ts TEXT,
                    slack_binding_revision INTEGER,
                    package_state TEXT NOT NULL DEFAULT 'active',
                    package_paused_at_ms INTEGER,
                    package_last_offered_at_ms INTEGER,
                    package_filter_important_only INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(initiating_actor_id, project_id, command_id)
                );
                CREATE INDEX IF NOT EXISTS collaboration_analyses_project_idx
                    ON collaboration_analyses(project_id,created_at_ms DESC);

                CREATE TABLE IF NOT EXISTS collaboration_questions(
                    id TEXT PRIMARY KEY,
                    analysis_id TEXT NOT NULL REFERENCES collaboration_analyses(id),
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    asked_by_actor_id TEXT NOT NULL REFERENCES actors(id),
                    addressed_to_actor_id TEXT NOT NULL REFERENCES actors(id),
                    addressed_role TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    shared_context TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    external_question_id TEXT,
                    blocking INTEGER NOT NULL CHECK(blocking IN (0,1)),
                    priority TEXT NOT NULL DEFAULT 'normal',
                    deadline_ms INTEGER,
                    alternatives_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    disposition TEXT,
                    answer_text TEXT,
                    answered_by_actor_id TEXT REFERENCES actors(id),
                    deferred_until_ms INTEGER,
                    expected_revision INTEGER,
                    stale_since_ms INTEGER,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS collaboration_questions_addressee_idx
                    ON collaboration_questions(addressed_to_actor_id,state,created_at_ms);

                CREATE TABLE IF NOT EXISTS collaboration_answer_commands(
                    actor_id TEXT NOT NULL REFERENCES actors(id),
                    analysis_id TEXT NOT NULL REFERENCES collaboration_analyses(id),
                    command_id TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(actor_id,analysis_id,command_id)
                );

                CREATE TABLE IF NOT EXISTS owner_development_questions(
                    id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL UNIQUE,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    addressed_to_actor_id TEXT NOT NULL REFERENCES actors(id),
                    prompt TEXT NOT NULL,
                    shared_context TEXT NOT NULL,
                    state TEXT NOT NULL,
                    disposition TEXT,
                    answer_text TEXT,
                    deferred_until_ms INTEGER,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS owner_development_questions_addressee_idx
                    ON owner_development_questions(addressed_to_actor_id,state,created_at_ms);
                CREATE TABLE IF NOT EXISTS owner_development_answer_commands(
                    actor_id TEXT NOT NULL REFERENCES actors(id),
                    question_id TEXT NOT NULL REFERENCES owner_development_questions(id),
                    command_id TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(actor_id,question_id,command_id)
                );

                CREATE TABLE IF NOT EXISTS collaboration_jobs(
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    analysis_id TEXT REFERENCES collaboration_analyses(id),
                    kind TEXT NOT NULL,
                    requesting_actor_id TEXT NOT NULL REFERENCES actors(id),
                    owner_actor_id TEXT REFERENCES actors(id),
                    owner_execution_id TEXT,
                    status TEXT NOT NULL,
                    request_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    provider_task_id TEXT,
                    result_markdown TEXT NOT NULL DEFAULT '',
                    external_receipt_json TEXT,
                    error_code TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    finished_at_ms INTEGER
                );
                CREATE INDEX IF NOT EXISTS collaboration_jobs_active_idx
                    ON collaboration_jobs(status,created_at_ms);

                """
            )
            analysis_columns = {
                str(row["name"])
                for row in self.store.db.execute(
                    "PRAGMA table_info(collaboration_analyses)"
                ).fetchall()
            }
            if "initial_provider_task_id" not in analysis_columns:
                self.store.db.execute(
                    "ALTER TABLE collaboration_analyses "
                    "ADD COLUMN initial_provider_task_id TEXT"
                )
            if "repair_attempt_count" not in analysis_columns:
                self.store.db.execute(
                    "ALTER TABLE collaboration_analyses "
                    "ADD COLUMN repair_attempt_count INTEGER NOT NULL DEFAULT 0"
                )
            if "repair_model_alias" not in analysis_columns:
                self.store.db.execute(
                    "ALTER TABLE collaboration_analyses "
                    "ADD COLUMN repair_model_alias TEXT"
                )
            # New fields for external analysis origin and package state
            for col, defn in (
                ("origin", "TEXT NOT NULL DEFAULT 'mira'"),
                ("source_kind", "TEXT NOT NULL DEFAULT 'project_note'"),
                ("source_id", "TEXT NOT NULL DEFAULT ''"),
                ("source_revision", "INTEGER"),
                ("source_repository_sha", "TEXT"),
                ("scope_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("slack_workspace_id", "TEXT"),
                ("slack_channel_id", "TEXT"),
                ("slack_thread_ts", "TEXT"),
                ("slack_binding_revision", "INTEGER"),
                ("package_state", "TEXT NOT NULL DEFAULT 'active'"),
                ("package_paused_at_ms", "INTEGER"),
                ("package_last_offered_at_ms", "INTEGER"),
                ("package_filter_important_only", "INTEGER NOT NULL DEFAULT 0"),
                ("producer_actor_id", "TEXT REFERENCES actors(id)"),
                ("external_analysis_id", "TEXT"),
                ("source_verified", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if col not in analysis_columns:
                    self.store.db.execute(
                        f"ALTER TABLE collaboration_analyses ADD COLUMN {col} {defn}"
                    )

            question_columns = {
                str(row["name"])
                for row in self.store.db.execute(
                    "PRAGMA table_info(collaboration_questions)"
                ).fetchall()
            }
            for col, defn in (
                ("priority", "TEXT NOT NULL DEFAULT 'normal'"),
                ("deadline_ms", "INTEGER"),
                ("expected_revision", "INTEGER"),
                ("stale_since_ms", "INTEGER"),
                ("reason", "TEXT NOT NULL DEFAULT ''"),
                ("external_question_id", "TEXT"),
            ):
                if col not in question_columns:
                    self.store.db.execute(
                        f"ALTER TABLE collaboration_questions ADD COLUMN {col} {defn}"
                    )
            # Recreate priority index if needed
            self.store.db.execute(
                "CREATE INDEX IF NOT EXISTS collaboration_questions_priority_idx "
                "ON collaboration_questions(analysis_id,blocking DESC,priority,deadline_ms,created_at_ms)"
            )

            # Indexes referencing added columns are created only after migration.
            self.store.db.execute(
                "CREATE INDEX IF NOT EXISTS collaboration_analyses_package_idx "
                "ON collaboration_analyses(package_state,updated_at_ms)"
            )

    def _analysis_row(self, analysis_id: str) -> Any:
        row = self.store.db.execute(
            "SELECT * FROM collaboration_analyses WHERE id=?", (analysis_id,)
        ).fetchone()
        if not row:
            raise StoreError("ANALYSIS_NOT_FOUND", "Collaboration analysis is not available")
        return row

    def _question_public(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "source_kind": "analysis",
            "analysis_id": row["analysis_id"],
            "project_id": row["project_id"],
            "asked_by_actor_id": row["asked_by_actor_id"],
            "addressed_to_actor_id": row["addressed_to_actor_id"],
            "addressed_role": row["addressed_role"],
            "prompt": row["prompt"],
            "shared_context": row["shared_context"],
            "reason": row["reason"],
            "external_question_id": row["external_question_id"],
            "blocking": bool(row["blocking"]),
            "priority": row["priority"],
            "deadline_ms": row["deadline_ms"],
            "alternatives": json.loads(row["alternatives_json"]),
            "state": row["state"],
            "disposition": row["disposition"],
            "answer_text": row["answer_text"],
            "answered_by_actor_id": row["answered_by_actor_id"],
            "deferred_until_ms": row["deferred_until_ms"],
            "expected_revision": row["expected_revision"],
            "stale_since_ms": row["stale_since_ms"],
            "created_at_ms": int(row["created_at_ms"]),
            "updated_at_ms": int(row["updated_at_ms"]),
        }

    def _owner_question_public(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "source_kind": "owner_development",
            "analysis_id": f"development:{row['execution_id']}",
            "execution_id": row["execution_id"],
            "project_id": row["project_id"],
            "asked_by_actor_id": row["addressed_to_actor_id"],
            "addressed_to_actor_id": row["addressed_to_actor_id"],
            "addressed_role": "owner",
            "prompt": row["prompt"],
            "shared_context": row["shared_context"],
            "blocking": True,
            "alternatives": ["answer", "unknown", "skip", "later"],
            "state": row["state"],
            "disposition": row["disposition"],
            "answer_text": row["answer_text"],
            "answered_by_actor_id": row["addressed_to_actor_id"] if row["answer_text"] else None,
            "deferred_until_ms": row["deferred_until_ms"],
            "created_at_ms": int(row["created_at_ms"]),
            "updated_at_ms": int(row["updated_at_ms"]),
        }

    def _sync_owner_development_questions(self) -> int:
        if self.development is None:
            return 0
        now = _now_ms()
        created = 0
        with self.store._lock:
            rows = self.store.db.execute(
                """SELECT id,actor_id,workspace_id,project_id,phase_detail,
                          result_summary,error_code
                   FROM task_executions
                   WHERE status='blocked' AND phase='needs_owner'
                     AND error_code IN ('REVIEW_REWORK_LIMIT','REVIEW_VERDICT_MISSING')
                   ORDER BY updated_at_ms"""
            ).fetchall()
            for row in rows:
                qid = "devq_" + hashlib.sha256(
                    str(row["id"]).encode("utf-8")
                ).hexdigest()[:32]
                exists = self.store.db.execute(
                    "SELECT 1 FROM owner_development_questions WHERE execution_id=?",
                    (row["id"],),
                ).fetchone()
                if exists:
                    continue
                prompt = str(row["phase_detail"] or "Нужно решение владельца").strip()
                context = str(row["result_summary"] or "").strip()[:4000]
                self.store.db.execute(
                    """INSERT INTO owner_development_questions(
                           id,execution_id,workspace_id,project_id,addressed_to_actor_id,
                           prompt,shared_context,state,created_at_ms,updated_at_ms)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        qid, row["id"], row["workspace_id"], row["project_id"],
                        row["actor_id"], prompt, context, "open", now, now,
                    ),
                )
                self.store.db.execute(
                    """INSERT INTO collaboration_events(
                           workspace_id,project_id,actor_id,kind,object_kind,object_id,
                           parent_object_id,addressed_to_actor_id,summary,created_at_ms)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        row["workspace_id"], row["project_id"], row["actor_id"],
                        "question_addressed", "question", qid, row["id"], row["actor_id"],
                        prompt[:240], now,
                    ),
                )
                created += 1

            # A confirmed owner-resume may have been reconciled by the
            # development worker after a lost HTTP/MCP response. Preserve the
            # accepted answer on the common question object before the generic
            # "resolved elsewhere" cleanup below.
            applied_resumes = self.store.db.execute(
                """SELECT q.id AS question_id,r.answer_text,r.created_at_ms
                   FROM owner_development_questions q
                   JOIN development_owner_resumes r
                     ON r.execution_id=q.execution_id
                   WHERE q.state IN ('open','deferred')
                     AND r.status='applied'
                   ORDER BY r.created_at_ms DESC"""
            ).fetchall()
            seen_questions: set[str] = set()
            for applied in applied_resumes:
                question_id = str(applied["question_id"])
                if question_id in seen_questions:
                    continue
                seen_questions.add(question_id)
                self.store.db.execute(
                    """UPDATE owner_development_questions
                       SET state='resolved',disposition='answer',answer_text=?,
                           deferred_until_ms=NULL,updated_at_ms=?
                       WHERE id=? AND state IN ('open','deferred')""",
                    (applied["answer_text"], now, question_id),
                )

            # If the execution was resumed by another authorized surface, the old
            # question stops being actionable without inventing an answer.
            self.store.db.execute(
                """UPDATE owner_development_questions
                   SET state='resolved',updated_at_ms=?
                   WHERE state IN ('open','deferred')
                     AND execution_id IN (
                       SELECT q.execution_id
                       FROM owner_development_questions q
                       LEFT JOIN task_executions e ON e.id=q.execution_id
                       WHERE e.id IS NULL
                          OR e.status!='blocked'
                          OR e.phase!='needs_owner'
                     )""",
                (now,),
            )
        return created

    def _analysis_public(self, actor_id: str, workspace_id: str, row: Any) -> dict[str, Any]:
        self.store.project_access(actor_id, workspace_id, row["project_id"])
        with self.store._lock:
            if row["model_alias"] == "dot":
                self._check_question_source(row)
            questions = self.store.db.execute(
                "SELECT * FROM collaboration_questions WHERE analysis_id=? ORDER BY created_at_ms,id",
                (row["id"],),
            ).fetchall()
            receipts = []
            for item in self.store.db.execute(
                "SELECT command_id,receipt_json FROM collaboration_answer_commands WHERE analysis_id=? ORDER BY created_at_ms,command_id",
                (row["id"],),
            ).fetchall():
                receipt = json.loads(item["receipt_json"])
                receipt.setdefault("receipt_id", item["command_id"])
                receipt.setdefault("kind", "answers")
                receipts.append(receipt)
            answers = [r for r in receipts if r["kind"] == "answers"]
            controls = [r for r in receipts if r["kind"] == "session_control"]
            complete = next((r for r in controls if r.get("action") == "complete"), None)
            continuation = complete.get("continuation") if complete else (
                "paused" if row["package_state"] == "paused" else "waiting_answers")
            request_id = complete.get("continuation_request_id") if complete else None
            if request_id and self.question_events is not None:
                request = self.store.db.execute(
                    "SELECT event_id,state FROM mcp_test_requests WHERE id=?", (request_id,)
                ).fetchone()
                if request:
                    delivery = self.question_events._question_delivery(request_id, request["event_id"])
                    continuation = ("completed" if request["state"] == "completed" else
                                    "ready_for_dot" if delivery["delivery_state"] == "queued"
                                    else "waiting_event_transport")
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "note_id": row["note_id"],
            "answer_receipts": answers,
            "receipt_ids": [r["receipt_id"] for r in answers],
            "session_receipts": controls,
            "continuation": continuation,
            "continuation_request_id": request_id,
            "initiating_actor_id": row["initiating_actor_id"],
            "addressed_to_actor_id": row["addressed_to_actor_id"],
            "model": row["model_alias"],
            "producer_actor_id": row["producer_actor_id"],
            "external_analysis_id": row["external_analysis_id"],
            "source_verified": bool(row["source_verified"]),
            "purpose": row["purpose"],
            "question": row["question"],
            "sources": json.loads(row["source_snapshot_json"]),
            "input_sha256": row["input_sha256"],
            "status": row["status"],
            "provider_task_id": row["provider_task_id"],
            "initial_provider_task_id": row["initial_provider_task_id"],
            "repair_attempt_count": int(row["repair_attempt_count"] or 0),
            "repair_model": row["repair_model_alias"],
            "result_markdown": row["result_markdown"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error_code": row["error_code"],
            "questions": [self._question_public(item) for item in questions],
            "created_at_ms": int(row["created_at_ms"]),
            "updated_at_ms": int(row["updated_at_ms"]),
            "finished_at_ms": row["finished_at_ms"],
            "origin": row["origin"],
            "source_kind": row["source_kind"],
            "source_id": row["source_id"],
            "source_revision": row["source_revision"],
            "source_repository_sha": row["source_repository_sha"],
            "scope": json.loads(row["scope_json"]),
            "slack_workspace_id": row["slack_workspace_id"],
            "slack_channel_id": row["slack_channel_id"],
            "slack_thread_ts": row["slack_thread_ts"],
            "slack_binding_revision": row["slack_binding_revision"],
            "package_state": row["package_state"],
            "package_paused_at_ms": row["package_paused_at_ms"],
            "package_last_offered_at_ms": row["package_last_offered_at_ms"],
            "package_filter_important_only": bool(row["package_filter_important_only"]),
        }

    def _freeze_note(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        project_id: str,
        note_id: str,
    ) -> tuple[list[dict[str, Any]], str]:
        self.store.project_access(
            actor_id, workspace_id, project_id, require_analyze=True
        )
        note = self.collaboration.get_note(
            actor_id=actor_id, workspace_id=workspace_id, note_id=note_id
        )
        if note["project_id"] != project_id:
            raise StoreError("PROJECT_FORBIDDEN", "Note belongs to another project")
        replies = self.collaboration.list_replies(
            actor_id=actor_id, workspace_id=workspace_id, note_id=note_id
        )
        sources = [
            {
                "kind": "project_note",
                "id": note["id"],
                "revision": note["revision"],
                "repository_sha": note["repository"]["sha"],
                "title": note["title"],
                "body": note["body"],
                "author": note["author"],
                "author_roles": note["author_roles"],
            },
            *[
                {
                    "kind": "project_note_reply",
                    "id": item["id"],
                    "note_id": note_id,
                    "body": item["body"],
                    "author": item["author"],
                    "created_at_ms": item["created_at_ms"],
                }
                for item in replies
            ],
        ]
        evidence = _canonical(
            {
                "schema": "projects-hub-collaboration-evidence-v1",
                "workspace_id": workspace_id,
                "project_id": project_id,
                "sources": sources,
            }
        )
        if len(evidence.encode("utf-8")) > 60_000:
            raise StoreError("ANALYTICS_INPUT_TOO_LARGE", "Collaboration evidence is too large")
        return sources, evidence

    @staticmethod
    def _extract_markdown(payload: dict[str, Any]) -> str:
        latest = payload.get("latestTurn")
        if isinstance(latest, dict):
            value = latest.get("finalResponse")
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("finalResponse", "result_markdown", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _question_payload(markdown: str) -> dict[str, Any] | None:
        candidates = [markdown.strip()]
        fenced = JSON_FENCE_RE.search(markdown)
        if fenced:
            candidates.insert(0, fenced.group(1))
        first, last = markdown.find("{"), markdown.rfind("}")
        if 0 <= first < last:
            candidates.append(markdown[first : last + 1])
        for candidate in candidates:
            try:
                value = json.loads(candidate)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict) and isinstance(value.get("questions"), list):
                return value
        return None

    def _materialize_questions(self, row: Any, markdown: str) -> dict[str, Any] | None:
        parsed = self._question_payload(markdown)
        if not parsed:
            return None
        summary = str(parsed.get("summary") or "").strip()[:4000]
        raw_questions = parsed.get("questions") or []
        if not 1 <= len(raw_questions) <= 6:
            return None
        now = _now_ms()
        addressed = str(row["addressed_to_actor_id"])
        access = self.store.project_access(
            addressed, row["workspace_id"], row["project_id"]
        )
        created: list[dict[str, Any]] = []
        with self.store._lock:
            existing = self.store.db.execute(
                "SELECT COUNT(*) AS n FROM collaboration_questions WHERE analysis_id=?",
                (row["id"],),
            ).fetchone()
            if int(existing["n"] or 0) > 0:
                return {
                    "summary": summary,
                    "questions": [
                        self._question_public(item)
                        for item in self.store.db.execute(
                            "SELECT * FROM collaboration_questions WHERE analysis_id=? ORDER BY created_at_ms,id",
                            (row["id"],),
                        ).fetchall()
                    ],
                }
            # SAVEPOINT is valid both as a top-level atomic boundary and
            # when this path is entered while the shared SQLite connection
            # already has a durable transaction in progress.
            savepoint = "collaboration_materialize_questions"
            self.store.db.execute(f"SAVEPOINT {savepoint}")
            try:
                for index, raw in enumerate(raw_questions):
                    if not isinstance(raw, dict):
                        raise StoreError("ANALYTICS_INVALID_RESULT", "Question payload is invalid")
                    prompt = str(raw.get("prompt") or "").strip()
                    if not prompt or len(prompt) > 2000:
                        raise StoreError("ANALYTICS_INVALID_RESULT", "Question prompt is invalid")
                    blocking = bool(raw.get("blocking"))
                    priority = str(raw.get("priority") or "normal")
                    if priority not in QUESTION_PRIORITIES:
                        priority = "normal"
                    deadline_ms = raw.get("deadline_ms")
                    if deadline_ms is not None:
                        try:
                            deadline_ms = int(deadline_ms)
                            if deadline_ms < now:
                                deadline_ms = None
                        except (TypeError, ValueError):
                            deadline_ms = None
                    qid = "q_" + hashlib.sha256(
                        f"{row['id']}:{index}:{prompt}".encode("utf-8")
                    ).hexdigest()[:32]
                    alternatives = ["answer", "unknown", "skip", "later"]
                    self.store.db.execute(
                        """INSERT INTO collaboration_questions(
                               id,analysis_id,workspace_id,project_id,asked_by_actor_id,
                               addressed_to_actor_id,addressed_role,prompt,shared_context,
                               blocking,priority,deadline_ms,alternatives_json,state,created_at_ms,updated_at_ms)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            qid, row["id"], row["workspace_id"], row["project_id"],
                            row["initiating_actor_id"], addressed, access["role"], prompt,
                            summary, int(blocking), priority, deadline_ms, _canonical(alternatives), "open", now, now,
                        ),
                    )
                    self.store.db.execute(
                        """INSERT INTO collaboration_events(
                               workspace_id,project_id,actor_id,kind,object_kind,object_id,
                               parent_object_id,addressed_to_actor_id,summary,created_at_ms)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (
                            row["workspace_id"], row["project_id"], row["initiating_actor_id"],
                            "question_addressed", "question", qid, row["note_id"], addressed,
                            prompt[:240], now,
                        ),
                    )
                blocking_count = self.store.db.execute(
                    """SELECT COUNT(*) AS n FROM collaboration_questions
                       WHERE analysis_id=? AND blocking=1""",
                    (row["id"],),
                ).fetchone()["n"]
                if int(blocking_count or 0) == 0:
                    answers = [
                        self._question_public(item)
                        for item in self.store.db.execute(
                            "SELECT * FROM collaboration_questions WHERE analysis_id=? ORDER BY created_at_ms,id",
                            (row["id"],),
                        ).fetchall()
                    ]
                    self.store.db.execute(
                        """INSERT OR IGNORE INTO collaboration_jobs(
                               id,workspace_id,project_id,analysis_id,kind,
                               requesting_actor_id,status,request_key,payload_json,
                               created_at_ms,updated_at_ms)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            _id("cj"), row["workspace_id"], row["project_id"], row["id"],
                            "analysis_followup", row["initiating_actor_id"], "queued",
                            f"collaboration-continuation:{row['id']}",
                            _canonical({"answers": answers}), now, now,
                        ),
                    )
                self.store.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception:
                self.store.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.store.db.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            created = [
                self._question_public(item)
                for item in self.store.db.execute(
                    "SELECT * FROM collaboration_questions WHERE analysis_id=? ORDER BY created_at_ms,id",
                    (row["id"],),
                ).fetchall()
            ]
        return {"summary": summary, "questions": created}


    def publish_dot_package(
        self, *, initiating_actor_id: str, producer_actor_id: str,
        workspace_id: str, project_id: str, note_id: str,
        addressed_to_actor_id: str, command_id: str, external_analysis_id: str,
        origin: str, source_kind: str, source_id: str, source_revision: int,
        summary: str, purpose: str, questions: list[dict[str, Any]],
        binding_id: str, binding_revision: int,
        slack_workspace_id: str, slack_channel_id: str, slack_thread_ts: str,
        source_repository_sha: str | None = None,
    ) -> dict[str, Any]:
        """Import dot-produced questions; never dispatch a provider or continuation job.

        Authorization is a precondition enforced by the MCP service grant +
        owner-approved bound route. This method additionally checks project,
        addressed actor, frozen note revision, immutable command and provenance.
        """
        if (not COMMAND_RE.fullmatch(command_id) or not COMMAND_RE.fullmatch(external_analysis_id)
                or origin not in ANALYSIS_ORIGINS or source_kind not in SOURCE_KINDS
                or not isinstance(source_id, str) or not 1 <= len(source_id) <= 250
                or type(source_revision) is not int or source_revision < 1
                or purpose not in PURPOSES or not isinstance(questions, list)
                or len(questions) > 6 or not isinstance(summary, str)
                or len(summary) > 4000 or type(binding_revision) is not int
                or binding_revision < 1 or not all(isinstance(x, str) and x for x in (
                    binding_id, slack_workspace_id, slack_channel_id, slack_thread_ts))):
            raise StoreError("INVALID_ARGUMENT", "Invalid external question package")
        clean_questions: list[dict[str, Any]] = []
        external_ids: set[str] = set()
        for raw in questions:
            if not isinstance(raw, dict):
                raise StoreError("INVALID_ARGUMENT", "Invalid question")
            external_qid = raw.get("external_question_id")
            prompt = raw.get("prompt")
            context = raw.get("context")
            reason = raw.get("reason")
            priority = raw.get("priority", "normal")
            blocking = raw.get("blocking")
            deadline = raw.get("deadline_ms")
            if (not isinstance(external_qid, str) or not COMMAND_RE.fullmatch(external_qid)
                    or external_qid in external_ids or not isinstance(prompt, str)
                    or not 1 <= len(prompt.strip()) <= 2000 or not isinstance(context, str)
                    or not 1 <= len(context.strip()) <= 1200 or not isinstance(reason, str)
                    or not 1 <= len(reason.strip()) <= 1000 or priority not in QUESTION_PRIORITIES
                    or type(blocking) is not bool or (deadline is not None and
                        (type(deadline) is not int or deadline <= 0))):
                raise StoreError("INVALID_ARGUMENT", "Invalid external question fields")
            external_ids.add(external_qid)
            clean_questions.append(dict(
                external_question_id=external_qid, prompt=prompt.strip(),
                context=context.strip(), reason=reason.strip(),
                blocking=blocking, priority=priority, deadline_ms=deadline,
            ))
        request = dict(
            project_id=project_id, note_id=note_id, addressed_to_actor_id=addressed_to_actor_id,
            external_analysis_id=external_analysis_id, origin=origin,
            source_kind=source_kind, source_id=source_id, source_revision=source_revision,
            source_repository_sha=source_repository_sha, purpose=purpose,
            summary=summary, questions=clean_questions, binding_id=binding_id,
            binding_revision=binding_revision, slack_workspace_id=slack_workspace_id,
            slack_channel_id=slack_channel_id, slack_thread_ts=slack_thread_ts,
        )
        request_sha = _sha(request)
        now = _now_ms()
        with self.store._lock:
            self.store.project_access(initiating_actor_id, workspace_id, project_id, require_analyze=True)
            addressee = self.store.project_access(addressed_to_actor_id, workspace_id, project_id)
            existing = self.store.db.execute(
                """SELECT * FROM collaboration_analyses WHERE initiating_actor_id=?
                   AND project_id=? AND command_id=?""",
                (initiating_actor_id, project_id, command_id),
            ).fetchone()
            if existing:
                if existing["model_alias"] != "dot" or existing["request_sha256"] != request_sha:
                    raise StoreError("ANALYSIS_COMMAND_CONFLICT", "External question command changed")
                return self._analysis_public(initiating_actor_id, workspace_id, existing)
            sources, evidence = self._freeze_note(
                actor_id=initiating_actor_id, workspace_id=workspace_id,
                project_id=project_id, note_id=note_id,
            )
            anchor_revision = int(sources[0]["revision"])
            if source_kind == "project_note" and (source_id != note_id or anchor_revision != source_revision):
                raise StoreError("QUESTION_SOURCE_STALE", "Question source revision has changed")
            if source_kind != "project_note" and not source_repository_sha:
                # A non-note anchor must retain a concrete remote source checkpoint.
                raise StoreError("INVALID_ARGUMENT", "External source requires an immutable checkpoint")
            if not self.store.db.execute(
                    "SELECT 1 FROM actors WHERE id=?", (producer_actor_id,)).fetchone():
                raise StoreError("PROJECT_FORBIDDEN", "Unrecognized service author")
            analysis_id = "ca_" + hashlib.sha256(
                f"{project_id}:{initiating_actor_id}:{command_id}".encode("utf-8")
            ).hexdigest()[:32]
            fields = dict(
                id=analysis_id, workspace_id=workspace_id, project_id=project_id,
                note_id=note_id, initiating_actor_id=initiating_actor_id,
                addressed_to_actor_id=addressed_to_actor_id, command_id=command_id,
                request_sha256=request_sha, model_alias="dot", producer_actor_id=producer_actor_id,
                external_analysis_id=external_analysis_id, source_verified=int(source_kind == "project_note"),
                purpose=purpose, question=summary[:4000] or "External analysis questions",
                source_snapshot_json=_canonical(sources), evidence_bundle=evidence,
                input_sha256=hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
                status="completed", result_json=_canonical({"summary":summary, "questions":clean_questions}),
                created_at_ms=now, updated_at_ms=now, finished_at_ms=now,
                origin=origin, source_kind=source_kind, source_id=source_id,
                source_revision=source_revision, source_repository_sha=source_repository_sha,
                scope_json=_canonical({"note_anchor_revision":anchor_revision,"binding_id":binding_id}),
                slack_workspace_id=slack_workspace_id, slack_channel_id=slack_channel_id,
                slack_thread_ts=slack_thread_ts, slack_binding_revision=binding_revision,
                package_state="waiting_answers",
            )
            self.store.db.execute("SAVEPOINT dot_package")
            try:
                self.store.db.execute(
                    "INSERT INTO collaboration_analyses(" + ",".join(fields) + ") VALUES(" +
                    ",".join("?" for _ in fields) + ")", tuple(fields.values()),
                )
                for item in clean_questions:
                    qid = "q_" + hashlib.sha256(
                        f"{analysis_id}:{item['external_question_id']}".encode("utf-8")
                    ).hexdigest()[:32]
                    qfields = dict(
                        id=qid, analysis_id=analysis_id, workspace_id=workspace_id,
                        project_id=project_id, asked_by_actor_id=producer_actor_id,
                        addressed_to_actor_id=addressed_to_actor_id, addressed_role=addressee["role"],
                        prompt=item["prompt"], shared_context=item["context"], reason=item["reason"],
                        external_question_id=item["external_question_id"], blocking=int(item["blocking"]),
                        priority=item["priority"], deadline_ms=item["deadline_ms"],
                        expected_revision=anchor_revision,
                        alternatives_json=_canonical(["answer","unknown","skip","later"]),
                        state="open", created_at_ms=now, updated_at_ms=now,
                    )
                    self.store.db.execute(
                        "INSERT INTO collaboration_questions(" + ",".join(qfields) + ") VALUES(" +
                        ",".join("?" for _ in qfields) + ")", tuple(qfields.values()),
                    )
                    self.store.db.execute(
                        """INSERT INTO collaboration_events(
                           workspace_id,project_id,actor_id,kind,object_kind,object_id,
                           parent_object_id,addressed_to_actor_id,summary,created_at_ms)
                           VALUES(?,?,?,'question_addressed','question',?,?,?,?,?)""",
                        (workspace_id,project_id,producer_actor_id,qid,note_id,
                         addressed_to_actor_id,item["prompt"][:240],now),
                    )
                self.store.db.execute("RELEASE SAVEPOINT dot_package")
            except BaseException:
                self.store.db.execute("ROLLBACK TO SAVEPOINT dot_package")
                self.store.db.execute("RELEASE SAVEPOINT dot_package")
                raise
            created = self._analysis_row(analysis_id)
            return self._analysis_public(initiating_actor_id, workspace_id, created)

    async def start_analysis(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        project_id: str,
        note_id: str,
        addressed_to_actor_id: str,
        command_id: str,
        model: str,
        purpose: str,
        question: str,
        origin: str = "mira",
        source_kind: str = "project_note",
        source_id: str = "",
        source_revision: int | None = None,
        source_repository_sha: str | None = None,
        scope: dict[str, Any] | None = None,
        slack_workspace_id: str | None = None,
        slack_channel_id: str | None = None,
        slack_thread_ts: str | None = None,
        slack_binding_revision: int | None = None,
    ) -> dict[str, Any]:
        if not COMMAND_RE.fullmatch(str(command_id or "")):
            raise StoreError("INVALID_ARGUMENT", "analysis command_id is invalid")
        if model not in ANALYSIS_MODELS:
            raise StoreError("ANALYTICS_MODEL_UNAVAILABLE", "Requested consultant is unavailable")
        if purpose not in PURPOSES:
            raise StoreError("INVALID_ARGUMENT", "Analysis purpose is invalid")
        if origin not in ANALYSIS_ORIGINS:
            raise StoreError("INVALID_ARGUMENT", "Analysis origin is invalid")
        if source_kind not in SOURCE_KINDS:
            raise StoreError("INVALID_ARGUMENT", "Analysis source kind is invalid")
        self.store.project_access(
            addressed_to_actor_id, workspace_id, project_id
        )
        clean_question = str(question or "").strip()
        if not clean_question or len(clean_question) > 4000:
            raise StoreError("INVALID_ARGUMENT", "Analysis question is invalid")
        sources, evidence = self._freeze_note(
            actor_id=actor_id,
            workspace_id=workspace_id,
            project_id=project_id,
            note_id=note_id,
        )
        # Use provided source info or fall back to note
        effective_source_id = source_id or note_id
        effective_source_revision = source_revision
        effective_source_repository_sha = source_repository_sha
        effective_scope = scope or {}
        request = {
            "project_id": project_id,
            "note_id": note_id,
            "addressed_to_actor_id": addressed_to_actor_id,
            "source_refs": [
                (item["kind"], item["id"], item.get("revision"), item.get("repository_sha"))
                for item in sources
            ],
            "model": model,
            "purpose": purpose,
            "question": clean_question,
            "origin": origin,
            "source_kind": source_kind,
            "source_id": effective_source_id,
            "source_revision": effective_source_revision,
            "source_repository_sha": effective_source_repository_sha,
            "scope": effective_scope,
        }
        request_sha = _sha(request)
        now = _now_ms()
        with self.store._lock:
            existing = self.store.db.execute(
                """SELECT * FROM collaboration_analyses
                   WHERE initiating_actor_id=? AND project_id=? AND command_id=?""",
                (actor_id, project_id, command_id),
            ).fetchone()
            if existing:
                if existing["request_sha256"] != request_sha:
                    raise StoreError("ANALYSIS_COMMAND_CONFLICT", "analysis command_id conflict")
                return self._analysis_public(actor_id, workspace_id, existing)
            analysis_id = _id("ca")
            self.store.db.execute(
                """INSERT INTO collaboration_analyses(
                       id,workspace_id,project_id,note_id,initiating_actor_id,
                       addressed_to_actor_id,command_id,request_sha256,model_alias,
                       purpose,question,source_snapshot_json,evidence_bundle,input_sha256,
                       status,created_at_ms,updated_at_ms,
                       origin,source_kind,source_id,source_revision,source_repository_sha,
                       scope_json,slack_workspace_id,slack_channel_id,slack_thread_ts,
                       slack_binding_revision,package_state)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                          ?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    analysis_id, workspace_id, project_id, note_id, actor_id,
                    addressed_to_actor_id, command_id, request_sha, model, purpose,
                    clean_question, _canonical(sources), evidence,
                    hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
                    "dispatching", now, now,
                    origin, source_kind, effective_source_id, effective_source_revision,
                    effective_source_repository_sha, _canonical(effective_scope),
                    slack_workspace_id, slack_channel_id, slack_thread_ts,
                    slack_binding_revision, "active",
                ),
            )
        prompt = (
            "Analyze only the supplied evidence. Return STRICT JSON and nothing else: "
            '{"summary":"shared context <=1200 chars","questions":['
            '{"prompt":"one contextual question","blocking":true,"priority":"high","deadline_ms":1700000000000}]}'
            ". Produce 1-6 minimal questions whose answers materially affect the requested analysis. "
            "Each question must have: prompt (string, required), blocking (boolean), priority (critical|high|normal|low, default normal), "
            "deadline_ms (optional, Unix ms timestamp). Do not ask for facts already present in evidence. "
            f"Task: {clean_question}"
        )
        try:
            response = await self.bridge.consult(
                model=model,
                purpose=purpose,
                question=prompt,
                evidence_bundle=evidence,
                request_key=f"collab-analysis:{analysis_id}",
            )
        except AnalyticsBridgeError as exc:
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE collaboration_analyses
                       SET status='dispatch_unknown',error_code=?,updated_at_ms=? WHERE id=?""",
                    ("ANALYTICS_BRIDGE_INTERRUPTED", _now_ms(), analysis_id),
                )
            raise StoreError(
                "ANALYTICS_DISPATCH_UNKNOWN",
                "Analysis dispatch outcome is unknown and can be reconciled safely",
            ) from exc
        task_id = response.get("taskId") or response.get("taskReference")
        status = str(response.get("status") or "running")
        with self.store._lock:
            self.store.db.execute(
                """UPDATE collaboration_analyses
                   SET provider_task_id=?,status=?,error_code=NULL,updated_at_ms=? WHERE id=?""",
                (
                    str(task_id) if isinstance(task_id, str) else None,
                    "running" if status in {"running", "completed"} else status,
                    _now_ms(), analysis_id,
                ),
            )
        if status == "completed":
            return await self.refresh_analysis(
                actor_id=actor_id, workspace_id=workspace_id, analysis_id=analysis_id
            )
        return self.get_analysis(
            actor_id=actor_id, workspace_id=workspace_id, analysis_id=analysis_id
        )

    def get_analysis(
        self, *, actor_id: str, workspace_id: str, analysis_id: str
    ) -> dict[str, Any]:
        with self.store._lock:
            row = self._analysis_row(analysis_id)
        return self._analysis_public(actor_id, workspace_id, row)

    @staticmethod
    def _repair_model(original_model: str) -> str:
        return "deepseek" if original_model == "kimi_k3" else "kimi_k3"

    async def _dispatch_analysis_repair(
        self,
        *,
        snapshot: dict[str, Any],
        invalid_markdown: str,
    ) -> dict[str, Any]:
        attempt = int(snapshot.get("repair_attempt_count") or 0) + 1
        if attempt > MAX_ANALYSIS_REPAIR_ATTEMPTS:
            raise StoreError(
                "ANALYTICS_INVALID_RESULT",
                "Strong analysis result remained invalid after bounded repair",
            )

        workspace_id = str(snapshot["workspace_id"])
        project_id = str(snapshot["project_id"])
        initiating_actor_id = str(snapshot["initiating_actor_id"])
        addressed_to_actor_id = str(snapshot["addressed_to_actor_id"])

        self.store.project_access(
            initiating_actor_id,
            workspace_id,
            project_id,
            require_analyze=True,
        )
        self.store.project_access(
            addressed_to_actor_id,
            workspace_id,
            project_id,
        )

        repair_model = self._repair_model(str(snapshot["model_alias"]))
        repair_prompt = (
            "The previous strong-analysis response did not satisfy the required "
            "typed question schema. Re-run the analysis from the supplied frozen "
            "evidence. Return STRICT JSON and nothing else with this exact shape: "
            '{"summary":"shared context <=1200 chars","questions":['
            '{"prompt":"one contextual question","blocking":true}]}. '
            "Return 1-4 minimal questions whose answers materially affect the "
            "requested task. Do not omit questions. Do not ask for facts already "
            "present in evidence. "
            f"Original task: {snapshot['question']}"
        )
        request_key = f"collab-analysis:{snapshot['id']}:repair:{attempt}"
        try:
            response = await self.bridge.consult(
                model=repair_model,
                purpose=str(snapshot["purpose"]),
                question=repair_prompt,
                evidence_bundle=str(snapshot["evidence_bundle"]),
                request_key=request_key,
            )
        except AnalyticsBridgeError as exc:
            now = _now_ms()
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE collaboration_analyses
                       SET status='failed',
                           result_markdown=?,
                           repair_attempt_count=?,
                           repair_model_alias=?,
                           error_code='ANALYTICS_REPAIR_DISPATCH_UNKNOWN',
                           updated_at_ms=?,finished_at_ms=?
                       WHERE id=?""",
                    (
                        invalid_markdown,
                        attempt,
                        repair_model,
                        now,
                        now,
                        snapshot["id"],
                    ),
                )
            raise StoreError(
                "ANALYTICS_REPAIR_DISPATCH_UNKNOWN",
                "Analysis repair dispatch outcome is unknown; refusing blind retry",
            ) from exc

        task_id = response.get("taskId") or response.get("taskReference")
        status = str(response.get("status") or "running")
        now = _now_ms()
        with self.store._lock:
            self.store.db.execute(
                """UPDATE collaboration_analyses
                   SET initial_provider_task_id=COALESCE(
                           initial_provider_task_id,provider_task_id
                       ),
                       provider_task_id=?,
                       repair_attempt_count=?,
                       repair_model_alias=?,
                       status=?,
                       result_markdown=?,
                       result_json=NULL,
                       error_code=NULL,
                       updated_at_ms=?,
                       finished_at_ms=NULL
                   WHERE id=?""",
                (
                    str(task_id) if isinstance(task_id, str) else None,
                    attempt,
                    repair_model,
                    "running" if status in {"running", "completed"} else status,
                    invalid_markdown,
                    now,
                    snapshot["id"],
                ),
            )

        if status == "completed":
            return await self._refresh_analysis_locked(
                actor_id=initiating_actor_id,
                workspace_id=workspace_id,
                analysis_id=str(snapshot["id"]),
            )
        return self.get_analysis(
            actor_id=initiating_actor_id,
            workspace_id=workspace_id,
            analysis_id=str(snapshot["id"]),
        )

    async def refresh_analysis(
        self, *, actor_id: str, workspace_id: str, analysis_id: str
    ) -> dict[str, Any]:
        """Explicit reconciliation entry point serialized with the durable worker."""
        async with self._lock:
            return await self._refresh_analysis_locked(
                actor_id=actor_id,
                workspace_id=workspace_id,
                analysis_id=analysis_id,
            )

    async def _refresh_analysis_locked(
        self, *, actor_id: str, workspace_id: str, analysis_id: str
    ) -> dict[str, Any]:
        with self.store._lock:
            row = self._analysis_row(analysis_id)
            self.store.project_access(actor_id, workspace_id, row["project_id"])
            if row["status"] in TERMINAL_ANALYSIS:
                return self._analysis_public(actor_id, workspace_id, row)
            snapshot = dict(row)
        task_id = snapshot.get("provider_task_id")
        if not task_id:
            return self.get_analysis(
                actor_id=actor_id, workspace_id=workspace_id, analysis_id=analysis_id
            )
        try:
            payload = await self.bridge.read_task(str(task_id))
        except AnalyticsBridgeError:
            return self.get_analysis(
                actor_id=actor_id, workspace_id=workspace_id, analysis_id=analysis_id
            )

        provider_status = str(
            payload.get("executionStatus") or payload.get("status") or "running"
        )
        now = _now_ms()

        if provider_status == "completed":
            markdown = self._extract_markdown(payload)
            with self.store._lock:
                row = self._analysis_row(analysis_id)
                current = dict(row)
            parsed: dict[str, Any] | None = None
            if markdown:
                try:
                    parsed = self._materialize_questions(row, markdown)
                except StoreError as exc:
                    if exc.code != "ANALYTICS_INVALID_RESULT":
                        raise
            if parsed:
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE collaboration_analyses SET status='completed',
                           result_markdown=?,result_json=?,error_code=NULL,
                           updated_at_ms=?,finished_at_ms=? WHERE id=?""",
                        (
                            markdown,
                            _canonical(parsed),
                            now,
                            now,
                            analysis_id,
                        ),
                    )
                    row = self._analysis_row(analysis_id)
                return self._analysis_public(actor_id, workspace_id, row)

            if int(current.get("repair_attempt_count") or 0) < MAX_ANALYSIS_REPAIR_ATTEMPTS:
                return await self._dispatch_analysis_repair(
                    snapshot=current,
                    invalid_markdown=markdown,
                )

            with self.store._lock:
                self.store.db.execute(
                    """UPDATE collaboration_analyses SET status='failed',
                       result_markdown=?,error_code='ANALYTICS_INVALID_RESULT',
                       updated_at_ms=?,finished_at_ms=? WHERE id=?""",
                    (markdown, now, now, analysis_id),
                )
                row = self._analysis_row(analysis_id)
            return self._analysis_public(actor_id, workspace_id, row)

        with self.store._lock:
            row = self._analysis_row(analysis_id)
            if provider_status in {"failed", "cancelled", "interrupted"}:
                self.store.db.execute(
                    """UPDATE collaboration_analyses SET status=?,error_code=?,
                       updated_at_ms=?,finished_at_ms=? WHERE id=?""",
                    (
                        "cancelled" if provider_status == "cancelled" else "failed",
                        str(payload.get("errorCategory") or provider_status),
                        now,
                        now,
                        analysis_id,
                    ),
                )
            else:
                self.store.db.execute(
                    "UPDATE collaboration_analyses "
                    "SET status='running',updated_at_ms=? WHERE id=?",
                    (now, analysis_id),
                )
            row = self._analysis_row(analysis_id)
        return self._analysis_public(actor_id, workspace_id, row)

    def inbox(
        self, *, actor_id: str, workspace_id: str, limit: int = 30, analysis_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.store._membership(actor_id, workspace_id)
        self._sync_owner_development_questions()
        bounded = max(1, min(int(limit), 100))
        with self.store._lock:
            base_query = """SELECT q.* FROM collaboration_questions q
                JOIN project_grants g
                  ON g.project_id=q.project_id
                 AND g.actor_id=q.addressed_to_actor_id
                JOIN projects p ON p.id=q.project_id
                WHERE q.workspace_id=? AND q.addressed_to_actor_id=?
                  AND g.revoked_at_ms IS NULL
                  AND p.status='active'"""
            params = [workspace_id, actor_id]
            if analysis_id:
                base_query += " AND q.analysis_id=?"
                params.append(analysis_id)
            analysis_rows = self.store.db.execute(base_query, tuple(params)).fetchall()
            readable_sessions: dict[str, bool] = {}
            visible_rows = []
            for question in analysis_rows:
                session_id = question["analysis_id"]
                if session_id not in readable_sessions:
                    session = self._analysis_row(session_id)
                    try:
                        if session["model_alias"] == "dot":
                            self._check_question_session(actor_id, workspace_id, session)
                        readable_sessions[session_id] = True
                    except StoreError:
                        readable_sessions[session_id] = False
                if readable_sessions[session_id]:
                    visible_rows.append(question)
            analysis_rows = visible_rows
            owner_rows = self.store.db.execute(
                """SELECT q.* FROM owner_development_questions q
                   JOIN project_grants g
                     ON g.project_id=q.project_id
                    AND g.actor_id=q.addressed_to_actor_id
                   JOIN projects p ON p.id=q.project_id
                   WHERE q.workspace_id=? AND q.addressed_to_actor_id=?
                     AND g.revoked_at_ms IS NULL
                     AND p.status='active'""",
                (workspace_id, actor_id),
            ).fetchall()
        items = [
            *[self._question_public(row) for row in analysis_rows],
            *[self._owner_question_public(row) for row in owner_rows],
        ]
        with self.store._lock:
            for item in items:
                if item["source_kind"] != "analysis":
                    continue
                session = self._analysis_row(item["analysis_id"])
                item.update(
                    package_state=session["package_state"],
                    package_last_offered_at_ms=session["package_last_offered_at_ms"],
                    package_filter_important_only=bool(session["package_filter_important_only"]),
                )
        now = _now_ms()
        # Filter out deferred questions whose deferral time hasn't arrived
        items = [
            item
            for item in items
            if not (
                item["state"] == "deferred"
                and (item.get("deferred_until_ms") is None
                     or int(item["deferred_until_ms"]) > now)
            )
        ]
        # Priority sorting: blocking first, then by priority (critical > high > normal > low), then deadline, then creation time
        priority_rank = {"critical": 0, "high": 1, "normal": 2, "low": 3}
        state_rank = {"open": 0, "deferred": 1}
        items.sort(
            key=lambda item: (
                state_rank.get(str(item["state"]), 2),
                -int(item.get("blocking", 0)),  # blocking first (1 > 0)
                priority_rank.get(str(item.get("priority", "normal")), 2),
                int(item.get("deadline_ms") or 0) or 2**63,  # earlier deadline first, None last
                int(item["created_at_ms"]),
                str(item["id"]),
            )
        )
        return items[:bounded]

    def _check_question_session(self, actor_id: str, workspace_id: str, analysis: Any) -> None:
        self.store.project_access(actor_id, workspace_id, analysis["project_id"])
        if analysis["workspace_id"] != workspace_id or analysis["addressed_to_actor_id"] != actor_id:
            raise StoreError("PROJECT_FORBIDDEN", "Question session is addressed to another actor")
        self.store.project_access(
            analysis["initiating_actor_id"], workspace_id, analysis["project_id"], require_analyze=True
        )
        if analysis["model_alias"] == "dot":
            self._check_question_source(analysis)

    def _check_question_source(self, analysis: Any) -> None:
        """Shared read/write gate; a project-authorized reader need not be the addressee."""
        self.store.project_access(
            analysis["initiating_actor_id"], analysis["workspace_id"],
            analysis["project_id"], require_analyze=True,
        )
        self.store.project_access(
            analysis["addressed_to_actor_id"], analysis["workspace_id"], analysis["project_id"],
        )
        scope = json.loads(analysis["scope_json"])
        anchor = self.store.db.execute(
            "SELECT revision,status,project_id FROM project_notes WHERE id=?", (analysis["note_id"],)
        ).fetchone()
        if (not anchor or anchor["status"] != "ready"
                or anchor["project_id"] != analysis["project_id"]
                or int(anchor["revision"]) != scope.get("note_anchor_revision")):
            raise StoreError("QUESTION_SOURCE_STALE", "Question source has changed")
        binding = self.store.db.execute(
            "SELECT * FROM mcp_discussion_bindings WHERE id=?", (scope.get("binding_id"),)
        ).fetchone()
        if (not binding or binding["state"] != "active" or not binding["questions_allowed"]
                or binding["revision"] != analysis["slack_binding_revision"]
                or binding["project_id"] != analysis["project_id"]
                or binding["note_id"] != analysis["note_id"]
                or binding["slack_workspace_id"] != analysis["slack_workspace_id"]
                or binding["slack_channel_id"] != analysis["slack_channel_id"]
                or binding["slack_thread_ts"] != analysis["slack_thread_ts"]):
            raise StoreError("PROJECT_FORBIDDEN", "Question session authorization changed")

    def control_question_session(
        self, *, actor_id: str, workspace_id: str, analysis_id: str,
        command_id: str, action: str, important_only: bool | None = None,
        allow_partial: bool = False,
    ) -> dict[str, Any]:
        """Persist explicit session intent; only complete can request dot continuation."""
        if (not COMMAND_RE.fullmatch(str(command_id or ""))
                or action not in {"offer", "accept", "pause", "resume", "important_only", "complete"}
                or (important_only is not None and type(important_only) is not bool) or type(allow_partial) is not bool
                or (allow_partial and action != "complete")):
            raise StoreError("INVALID_ARGUMENT", "Invalid question session control")
        payload_sha = _sha(dict(action=action, important_only=important_only, allow_partial=allow_partial))
        now = _now_ms()
        with self.store._lock:
            analysis = self._analysis_row(analysis_id)
            self._check_question_session(actor_id, workspace_id, analysis)
            existing = self.store.db.execute(
                "SELECT * FROM collaboration_answer_commands WHERE actor_id=? AND analysis_id=? AND command_id=?",
                (actor_id, analysis_id, command_id),
            ).fetchone()
            if existing:
                if existing["payload_sha256"] != payload_sha:
                    raise StoreError("COLLABORATION_COMMAND_CONFLICT", "Session command changed")
                return json.loads(existing["receipt_json"])
            if analysis["model_alias"] != "dot":
                raise StoreError("INVALID_ARGUMENT", "Session controls require a dot question package")
            receipts = [json.loads(r["receipt_json"]) for r in self.store.db.execute(
                "SELECT receipt_json FROM collaboration_answer_commands WHERE analysis_id=? ORDER BY created_at_ms,command_id",
                (analysis_id,),
            ).fetchall()]
            completion = next((r for r in receipts if r.get("kind") == "session_control"
                               and r.get("action") == "complete"), None)
            if completion:
                if action == "complete" and completion.get("allow_partial") == allow_partial:
                    replay = dict(completion, receipt_id=command_id)
                    self.store.db.execute(
                        """INSERT INTO collaboration_answer_commands(
                           actor_id,analysis_id,command_id,payload_sha256,receipt_json,created_at_ms)
                           VALUES(?,?,?,?,?,?)""",
                        (actor_id, analysis_id, command_id, payload_sha, _canonical(replay), now),
                    )
                    return replay
                raise StoreError("QUESTION_SESSION_COMPLETED", "Question session is already complete")
            if analysis["package_state"] in {"completed", "cancelled", "partially_completed", "all_answered"}:
                raise StoreError("QUESTION_SESSION_COMPLETED", "Question session is already complete")
            questions = [self._question_public(r) for r in self.store.db.execute(
                "SELECT * FROM collaboration_questions WHERE analysis_id=? ORDER BY created_at_ms,id",
                (analysis_id,),
            ).fetchall()]
            unresolved = [q for q in questions if q["state"] in {"open", "deferred"}
                          or (q["blocking"] and q["state"] != "resolved")]
            if action == "complete" and unresolved and not allow_partial:
                raise StoreError("QUESTION_SESSION_UNRESOLVED", "Explicit partial completion is required")
            state = analysis["package_state"]
            paused = analysis["package_paused_at_ms"]
            offered = analysis["package_last_offered_at_ms"]
            filtered = bool(analysis["package_filter_important_only"])
            if action == "offer":
                offered = now
            elif action in {"accept", "resume"}:
                state, paused = "active", None
                if important_only is not None:
                    filtered = important_only
            elif action == "pause":
                state, paused = "paused", now
            elif action == "important_only" and important_only is not None:
                filtered = important_only
            elif action == "complete":
                state, paused = ("partially_completed" if unresolved else "all_answered"), None
            self.store.db.execute("SAVEPOINT question_session_control")
            try:
                self.store.db.execute(
                    """UPDATE collaboration_analyses SET package_state=?,package_paused_at_ms=?,
                       package_last_offered_at_ms=?,package_filter_important_only=?,updated_at_ms=? WHERE id=?""",
                    (state, paused, offered, int(filtered), now, analysis_id),
                )
                ready = None
                if action == "complete" and self.question_events is not None:
                    ready = self.question_events.enqueue_question_ready(self._analysis_row(analysis_id))
                receipt = dict(
                    receipt_id=command_id, kind="session_control", action=action,
                    analysis_id=analysis_id, package_state=state, important_only=filtered,
                    allow_partial=allow_partial, completion_scope="partial" if unresolved else "full",
                    unresolved_question_ids=[q["id"] for q in unresolved],
                    unresolved_blocking_question_ids=[q["id"] for q in unresolved if q["blocking"]],
                    questions=[q for q in questions if not filtered or q["blocking"]
                               or q["priority"] in {"critical", "high"}],
                    continuation=("ready_for_dot" if ready and ready.get("delivery_state") == "queued"
                                  else "waiting_event_transport") if action == "complete" else "waiting_answers",
                    continuation_request_id=ready["request_id"] if ready else None,
                    summary={"offer": "Пакет вопросов предложен.", "accept": "Ответы на пакет вопросов начаты.",
                             "pause": "Пакет вопросов отложен.", "resume": "Ответы на вопросы продолжены.",
                             "important_only": "Фильтр важных вопросов сохранён.",
                             "complete": "Сессия ответов завершена."}[action],
                )
                self.store.db.execute(
                    """INSERT INTO collaboration_answer_commands(
                       actor_id,analysis_id,command_id,payload_sha256,receipt_json,created_at_ms)
                       VALUES(?,?,?,?,?,?)""",
                    (actor_id, analysis_id, command_id, payload_sha, _canonical(receipt), now),
                )
                self.store.db.execute("RELEASE SAVEPOINT question_session_control")
            except BaseException:
                self.store.db.execute("ROLLBACK TO SAVEPOINT question_session_control")
                self.store.db.execute("RELEASE SAVEPOINT question_session_control")
                raise
            return receipt

    def answer_questions(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        analysis_id: str,
        command_id: str,
        responses: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not COMMAND_RE.fullmatch(str(command_id or "")):
            raise StoreError("INVALID_ARGUMENT", "answer command_id is invalid")
        if not responses or len(responses) > 6:
            raise StoreError("INVALID_ARGUMENT", "responses must contain 1-6 answers")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in responses:
            qid = str(raw.get("question_id") or "")
            disposition = str(raw.get("disposition") or "")
            body = str(raw.get("body") or "").strip()
            if not qid or qid in seen or disposition not in DISPOSITIONS:
                raise StoreError("INVALID_ARGUMENT", "question response is invalid")
            if disposition == "answer" and not body:
                raise StoreError("INVALID_ARGUMENT", "answer text is required")
            if len(body) > 12_000:
                raise StoreError("INVALID_ARGUMENT", "answer text is too large")
            deferred_until = raw.get("deferred_until_ms")
            if disposition == "later":
                try:
                    deferred_until = int(deferred_until) if deferred_until is not None else None
                except (TypeError, ValueError):
                    raise StoreError("INVALID_ARGUMENT", "deferred_until_ms is invalid") from None
            else:
                deferred_until = None
            normalized.append(
                {
                    "question_id": qid,
                    "disposition": disposition,
                    "body": body,
                    "deferred_until_ms": deferred_until,
                }
            )
            seen.add(qid)
        payload_sha = _sha(normalized)
        now = _now_ms()
        with self.store._lock:
            analysis = self._analysis_row(analysis_id)
            self._check_question_session(actor_id, workspace_id, analysis)
            existing = self.store.db.execute(
                """SELECT * FROM collaboration_answer_commands
                   WHERE actor_id=? AND analysis_id=? AND command_id=?""",
                (actor_id, analysis_id, command_id),
            ).fetchone()
            if existing:
                if existing["payload_sha256"] != payload_sha:
                    raise StoreError("COLLABORATION_COMMAND_CONFLICT", "answer command_id conflict")
                return json.loads(existing["receipt_json"])
            completed_intent = self.store.db.execute(
                """SELECT 1 FROM collaboration_answer_commands WHERE analysis_id=?
                   AND json_extract(receipt_json,'$.kind')='session_control'
                   AND json_extract(receipt_json,'$.action')='complete' LIMIT 1""",
                (analysis_id,),
            ).fetchone()
            if analysis["model_alias"] == "dot" and (completed_intent or analysis["package_state"] in {
                    "all_answered", "partially_completed", "awaiting_delivery", "completed", "cancelled"}):
                raise StoreError("QUESTION_SESSION_COMPLETED", "Question session is already complete")
            rows = self.store.db.execute(
                """SELECT * FROM collaboration_questions
                   WHERE analysis_id=? AND id IN (%s)"""
                % ",".join("?" for _ in normalized),
                (analysis_id, *[item["question_id"] for item in normalized]),
            ).fetchall()
            by_id = {str(row["id"]): row for row in rows}
            if len(by_id) != len(normalized):
                raise StoreError("QUESTION_NOT_FOUND", "One or more questions are unavailable")
            for item in normalized:
                row = by_id[item["question_id"]]
                if row["addressed_to_actor_id"] != actor_id:
                    raise StoreError("PROJECT_FORBIDDEN", "Question is addressed to another actor")
                if row["state"] not in {"open", "deferred"}:
                    raise StoreError("QUESTION_ALREADY_ANSWERED", "Question already has a disposition")
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                for item in normalized:
                    source_question = by_id[item["question_id"]]
                    if (
                        bool(source_question["blocking"])
                        and item["disposition"] in {"skip", "unknown"}
                    ):
                        state = "open"
                    else:
                        state = {
                            "answer": "resolved",
                            "skip": "skipped",
                            "unknown": "unknown",
                            "later": "deferred",
                        }[item["disposition"]]
                    self.store.db.execute(
                        """UPDATE collaboration_questions SET state=?,disposition=?,
                           answer_text=?,answered_by_actor_id=?,deferred_until_ms=?,
                           updated_at_ms=? WHERE id=?""",
                        (
                            state, item["disposition"], item["body"] or None, actor_id,
                            item["deferred_until_ms"], now, item["question_id"],
                        ),
                    )
                    self.store.db.execute(
                        """INSERT INTO collaboration_events(
                               workspace_id,project_id,actor_id,kind,object_kind,object_id,
                               parent_object_id,addressed_to_actor_id,summary,created_at_ms)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (
                            workspace_id, analysis["project_id"], actor_id,
                            "question_answered" if state == "resolved" else f"question_{state}",
                            "question", item["question_id"], analysis["note_id"],
                            analysis["initiating_actor_id"],
                            (item["body"] or item["disposition"])[:240], now,
                        ),
                    )
                blocking = self.store.db.execute(
                    """SELECT state FROM collaboration_questions
                       WHERE analysis_id=? AND blocking=1""",
                    (analysis_id,),
                ).fetchall()
                has_deferred = any(row["state"] == "deferred" for row in blocking)
                has_unresolved = any(row["state"] in {"open", "skipped", "unknown"} for row in blocking)
                all_resolved = all(row["state"] == "resolved" for row in blocking)
                is_dot = analysis["model_alias"] == "dot"
                all_questions = self.store.db.execute(
                    "SELECT state,blocking FROM collaboration_questions WHERE analysis_id=?",
                    (analysis_id,),
                ).fetchall()
                dot_ready = (all(row["state"] in {"resolved", "skipped", "unknown"}
                                 for row in all_questions) and
                             all(not row["blocking"] or row["state"] == "resolved"
                                 for row in all_questions))
                ready_event = None
                if is_dot:
                    self.store.db.execute(
                        "UPDATE collaboration_analyses SET package_state=?,updated_at_ms=? WHERE id=?",
                        (analysis["package_state"] if analysis["package_state"] == "paused" else "waiting_answers", now, analysis_id),
                    )
                job = None if is_dot else self.store.db.execute(
                    "SELECT * FROM collaboration_jobs WHERE analysis_id=? AND kind='analysis_followup'",
                    (analysis_id,),
                ).fetchone()
                continuation_job_id = str(job["id"]) if job else None
                if all_resolved and not job and not is_dot:
                    job_id = _id("cj")
                    continuation_job_id = job_id
                    answers = [
                        self._question_public(row)
                        for row in self.store.db.execute(
                            "SELECT * FROM collaboration_questions WHERE analysis_id=? ORDER BY created_at_ms,id",
                            (analysis_id,),
                        ).fetchall()
                    ]
                    self.store.db.execute(
                        """INSERT INTO collaboration_jobs(
                               id,workspace_id,project_id,analysis_id,kind,
                               requesting_actor_id,status,request_key,payload_json,
                               created_at_ms,updated_at_ms)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            job_id, workspace_id, analysis["project_id"], analysis_id,
                            "analysis_followup", analysis["initiating_actor_id"], "queued",
                            f"collaboration-continuation:{analysis_id}", _canonical({"answers": answers}),
                            now, now,
                        ),
                    )
                questions = [
                    self._question_public(row)
                    for row in self.store.db.execute(
                        "SELECT * FROM collaboration_questions WHERE analysis_id=? ORDER BY created_at_ms,id",
                        (analysis_id,),
                    ).fetchall()
                ]
                receipt = {
                    "receipt_id": command_id,
                    "kind": "answers",
                    "summary": "Ответы сохранены.",
                    "analysis_id": analysis_id,
                    "questions": questions,
                    "continuation": (
                        ("paused" if analysis["package_state"] == "paused" else
                         "ready_to_complete" if dot_ready else "waiting_answers") if is_dot else
                        "queued" if all_resolved else
                        "deferred" if has_deferred else
                        "blocked" if has_unresolved else "not_required"
                    ),
                    "continuation_job_id": continuation_job_id,
                    "continuation_request_id": ready_event["request_id"] if ready_event else None,
                }
                self.store.db.execute(
                    """INSERT INTO collaboration_answer_commands(
                           actor_id,analysis_id,command_id,payload_sha256,receipt_json,created_at_ms)
                       VALUES(?,?,?,?,?,?)""",
                    (actor_id, analysis_id, command_id, payload_sha, _canonical(receipt), now),
                )
                self.store.db.execute("COMMIT")
            except Exception:
                self.store.db.execute("ROLLBACK")
                raise
        return receipt

    async def answer_owner_development_question(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        question_id: str,
        command_id: str,
        disposition: str,
        body: str = "",
        deferred_until_ms: int | None = None,
    ) -> dict[str, Any]:
        if self.development is None:
            raise StoreError("TOOL_NOT_AVAILABLE", "Owner development is unavailable")
        if not COMMAND_RE.fullmatch(str(command_id or "")):
            raise StoreError("INVALID_ARGUMENT", "answer command_id is invalid")
        if disposition not in DISPOSITIONS:
            raise StoreError("INVALID_ARGUMENT", "question disposition is invalid")
        clean_body = str(body or "").strip()
        if disposition == "answer" and not clean_body:
            raise StoreError("INVALID_ARGUMENT", "answer text is required")
        if len(clean_body) > 12000:
            raise StoreError("INVALID_ARGUMENT", "answer text is too large")
        if disposition == "later" and deferred_until_ms is None:
            deferred_until_ms = _now_ms() + 86_400_000
        request = {
            "disposition": disposition,
            "body": clean_body,
            "deferred_until_ms": deferred_until_ms,
        }
        payload_sha = _sha(request)
        with self.store._lock:
            row = self.store.db.execute(
                "SELECT * FROM owner_development_questions WHERE id=?",
                (question_id,),
            ).fetchone()
            if not row or row["workspace_id"] != workspace_id:
                raise StoreError("QUESTION_NOT_FOUND", "Owner question is not available")
            self.store.project_access(actor_id, workspace_id, row["project_id"])
            if row["addressed_to_actor_id"] != actor_id:
                raise StoreError("PROJECT_FORBIDDEN", "Question is addressed to another actor")
            existing = self.store.db.execute(
                """SELECT * FROM owner_development_answer_commands
                   WHERE actor_id=? AND question_id=? AND command_id=?""",
                (actor_id, question_id, command_id),
            ).fetchone()
            if existing:
                if existing["payload_sha256"] != payload_sha:
                    raise StoreError("COLLABORATION_COMMAND_CONFLICT", "answer command_id conflict")
                return json.loads(existing["receipt_json"])
            if row["state"] not in {"open", "deferred"}:
                if (
                    disposition == "answer"
                    and row["state"] == "resolved"
                    and row["disposition"] == "answer"
                    and str(row["answer_text"] or "").strip() == clean_body
                ):
                    execution_row = self.store.db.execute(
                        "SELECT * FROM task_executions WHERE id=?",
                        (row["execution_id"],),
                    ).fetchone()
                    execution = (
                        self.development._execution_public(execution_row)
                        if execution_row is not None
                        else None
                    )
                    return {
                        "question_id": question_id,
                        "execution_id": row["execution_id"],
                        "state": "resolved",
                        "disposition": "answer",
                        "continuation": "resumed",
                        "execution": execution,
                    }
                raise StoreError("QUESTION_ALREADY_ANSWERED", "Question already has a disposition")
            snapshot = dict(row)

        resume = None
        if disposition == "answer":
            resume = await self.development.resume_needs_owner(
                actor_id=actor_id,
                workspace_id=workspace_id,
                execution_id=str(snapshot["execution_id"]),
                command_id=command_id,
                answer=clean_body,
            )

        state = {
            "answer": "resolved",
            "skip": "open",
            "unknown": "open",
            "later": "deferred",
        }[disposition]
        now = _now_ms()
        receipt = {
            "question_id": question_id,
            "execution_id": snapshot["execution_id"],
            "state": state,
            "disposition": disposition,
            "continuation": "resumed" if resume else (
                "deferred" if disposition == "later" else "blocked"
            ),
            "execution": resume["execution"] if resume else None,
        }
        with self.store._lock:
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                self.store.db.execute(
                    """UPDATE owner_development_questions
                       SET state=?,disposition=?,answer_text=?,deferred_until_ms=?,
                           updated_at_ms=? WHERE id=?""",
                    (
                        state, disposition, clean_body or None, deferred_until_ms,
                        now, question_id,
                    ),
                )
                self.store.db.execute(
                    """INSERT INTO owner_development_answer_commands(
                           actor_id,question_id,command_id,payload_sha256,receipt_json,created_at_ms)
                       VALUES(?,?,?,?,?,?)""",
                    (actor_id, question_id, command_id, payload_sha, _canonical(receipt), now),
                )
                self.store.db.execute(
                    """INSERT INTO collaboration_events(
                           workspace_id,project_id,actor_id,kind,object_kind,object_id,
                           parent_object_id,addressed_to_actor_id,summary,created_at_ms)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        workspace_id, snapshot["project_id"], actor_id,
                        "question_answered" if state == "resolved" else f"question_{state}",
                        "question", question_id, snapshot["execution_id"], actor_id,
                        (clean_body or disposition)[:240], now,
                    ),
                )
                self.store.db.execute("COMMIT")
            except Exception:
                self.store.db.execute("ROLLBACK")
                raise
        return receipt

    def _job_public(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "analysis_id": row["analysis_id"],
            "kind": row["kind"],
            "status": row["status"],
            "request_key": row["request_key"],
            "provider_task_id": row["provider_task_id"],
            "result_markdown": row["result_markdown"],
            "external_receipt": json.loads(row["external_receipt_json"]) if row["external_receipt_json"] else None,
            "error_code": row["error_code"],
            "attempt_count": int(row["attempt_count"]),
            "created_at_ms": int(row["created_at_ms"]),
            "updated_at_ms": int(row["updated_at_ms"]),
            "finished_at_ms": row["finished_at_ms"],
        }

    def job_status(
        self, *, actor_id: str, workspace_id: str, job_id: str
    ) -> dict[str, Any]:
        with self.store._lock:
            row = self.store.db.execute(
                "SELECT * FROM collaboration_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row:
                raise StoreError("COLLABORATION_JOB_NOT_FOUND", "Continuation is unavailable")
            self.store.project_access(actor_id, workspace_id, row["project_id"])
            return self._job_public(row)

    async def _advance_analysis_job(self, row: Any) -> None:
        now = _now_ms()
        analysis = self._analysis_row(str(row["analysis_id"]))
        if row["status"] == "queued":
            try:
                self.store.project_access(
                    str(row["requesting_actor_id"]),
                    str(row["workspace_id"]),
                    str(row["project_id"]),
                    require_analyze=True,
                )
            except StoreError as exc:
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE collaboration_jobs
                           SET status='failed',error_code=?,updated_at_ms=?,
                               finished_at_ms=?
                           WHERE id=? AND status='queued'""",
                        (exc.code, now, now, row["id"]),
                    )
                return
            payload = json.loads(row["payload_json"])
            evidence = str(analysis["evidence_bundle"]) + "\n\nTYPED ANSWERS:\n" + _canonical(payload)
            try:
                response = await self.bridge.consult(
                    model=str(analysis["model_alias"]),
                    purpose=str(analysis["purpose"]),
                    question=(
                        "Continue the original analysis using the typed answers. "
                        "Respect skip/unknown/later semantics and do not invent decisions. "
                        f"Original task: {analysis['question']}"
                    ),
                    evidence_bundle=evidence,
                    request_key=str(row["request_key"]),
                )
            except AnalyticsBridgeError:
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE collaboration_jobs SET attempt_count=attempt_count+1,
                           error_code='ANALYTICS_BRIDGE_INTERRUPTED',updated_at_ms=? WHERE id=?""",
                        (now, row["id"]),
                    )
                return
            task_id = response.get("taskId") or response.get("taskReference")
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE collaboration_jobs SET status='running',provider_task_id=?,
                       attempt_count=attempt_count+1,error_code=NULL,updated_at_ms=? WHERE id=?""",
                    (str(task_id) if isinstance(task_id, str) else None, now, row["id"]),
                )
            return

        if row["status"] == "running" and row["provider_task_id"]:
            try:
                payload = await self.bridge.read_task(str(row["provider_task_id"]))
            except AnalyticsBridgeError:
                return
            state = str(payload.get("executionStatus") or payload.get("status") or "running")
            if state == "completed":
                markdown = self._extract_markdown(payload)
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE collaboration_jobs SET status='completed',result_markdown=?,
                           external_receipt_json=?,error_code=NULL,updated_at_ms=?,finished_at_ms=?
                           WHERE id=?""",
                        (
                            markdown, _canonical({"provider_task_id": row["provider_task_id"], "status": state}),
                            now, now, row["id"],
                        ),
                    )
                    self.store.db.execute(
                        """INSERT INTO collaboration_events(
                               workspace_id,project_id,actor_id,kind,object_kind,object_id,
                               parent_object_id,addressed_to_actor_id,summary,created_at_ms)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (
                            row["workspace_id"], row["project_id"], row["requesting_actor_id"],
                            "continuation_completed", "analysis", row["analysis_id"],
                            analysis["note_id"], row["requesting_actor_id"],
                            (markdown.splitlines()[0] if markdown else "Анализ продолжен")[:240], now,
                        ),
                    )
            elif state in {"failed", "cancelled", "interrupted"}:
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE collaboration_jobs SET status='failed',error_code=?,
                           updated_at_ms=?,finished_at_ms=? WHERE id=?""",
                        (str(payload.get("errorCategory") or state), now, now, row["id"]),
                    )

    async def _advance_pending_analyses_once(self) -> int:
        """Poll already-dispatched analyses from the durable worker.

        Status/read APIs stay pure; provider progress is owned by this worker.
        """
        with self.store._lock:
            rows = self.store.db.execute(
                """SELECT id,workspace_id,project_id,initiating_actor_id
                   FROM collaboration_analyses
                   WHERE status='running' AND provider_task_id IS NOT NULL
                   ORDER BY created_at_ms LIMIT 8"""
            ).fetchall()
        advanced = 0
        for row in rows:
            try:
                await self._refresh_analysis_locked(
                    actor_id=str(row["initiating_actor_id"]),
                    workspace_id=str(row["workspace_id"]),
                    analysis_id=str(row["id"]),
                )
            except StoreError as exc:
                # Revocation or another durable authorization failure must stop
                # publication instead of spinning forever or leaking questions.
                with self.store._lock:
                    now = _now_ms()
                    self.store.db.execute(
                        """UPDATE collaboration_analyses
                           SET status='failed',error_code=?,updated_at_ms=?,
                               finished_at_ms=?
                           WHERE id=? AND status='running'""",
                        (exc.code, now, now, row["id"]),
                    )
            advanced += 1
        return advanced

    async def advance_jobs_once(self) -> int:
        async with self._lock:
            self._sync_owner_development_questions()
            notes_advanced = await self.collaboration.advance_pending_notes_once()
            # This bounded adapter reads only external verified Markdown; the
            # hourly ChatGPT task owns reasoning and GitHub publication.
            chatgpt_imported = await self.collaboration.chatgpt_analysis.poll_once()
            analyses_advanced = await self._advance_pending_analyses_once()
            with self.store._lock:
                rows = self.store.db.execute(
                    """SELECT * FROM collaboration_jobs
                       WHERE status IN ('queued','running')
                       ORDER BY created_at_ms LIMIT 8"""
                ).fetchall()
            for row in rows:
                if row["kind"] == "analysis_followup":
                    await self._advance_analysis_job(row)
            return notes_advanced + chatgpt_imported + analyses_advanced + len(rows)

    async def _loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.advance_jobs_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass

    async def start_background(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(self._stop), name="projects-hub-collaboration-worker")

    async def close(self) -> None:
        stop, task = self._stop, self._task
        self._stop = None
        self._task = None
        if stop is not None:
            stop.set()
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.bridge.close()
