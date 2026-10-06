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
PURPOSES = {"requirements", "edge_cases", "architecture", "code_review", "ideas"}
JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


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
                    purpose TEXT NOT NULL,
                    question TEXT NOT NULL,
                    source_snapshot_json TEXT NOT NULL,
                    evidence_bundle TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provider_task_id TEXT,
                    result_markdown TEXT NOT NULL DEFAULT '',
                    result_json TEXT,
                    error_code TEXT,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    finished_at_ms INTEGER,
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
                    blocking INTEGER NOT NULL CHECK(blocking IN (0,1)),
                    alternatives_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    disposition TEXT,
                    answer_text TEXT,
                    answered_by_actor_id TEXT REFERENCES actors(id),
                    deferred_until_ms INTEGER,
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
            "blocking": bool(row["blocking"]),
            "alternatives": json.loads(row["alternatives_json"]),
            "state": row["state"],
            "disposition": row["disposition"],
            "answer_text": row["answer_text"],
            "answered_by_actor_id": row["answered_by_actor_id"],
            "deferred_until_ms": row["deferred_until_ms"],
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
            questions = self.store.db.execute(
                "SELECT * FROM collaboration_questions WHERE analysis_id=? ORDER BY created_at_ms,id",
                (row["id"],),
            ).fetchall()
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "note_id": row["note_id"],
            "initiating_actor_id": row["initiating_actor_id"],
            "addressed_to_actor_id": row["addressed_to_actor_id"],
            "model": row["model_alias"],
            "purpose": row["purpose"],
            "question": row["question"],
            "sources": json.loads(row["source_snapshot_json"]),
            "input_sha256": row["input_sha256"],
            "status": row["status"],
            "provider_task_id": row["provider_task_id"],
            "result_markdown": row["result_markdown"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error_code": row["error_code"],
            "questions": [self._question_public(item) for item in questions],
            "created_at_ms": int(row["created_at_ms"]),
            "updated_at_ms": int(row["updated_at_ms"]),
            "finished_at_ms": row["finished_at_ms"],
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
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                for index, raw in enumerate(raw_questions):
                    if not isinstance(raw, dict):
                        raise StoreError("ANALYTICS_INVALID_RESULT", "Question payload is invalid")
                    prompt = str(raw.get("prompt") or "").strip()
                    if not prompt or len(prompt) > 2000:
                        raise StoreError("ANALYTICS_INVALID_RESULT", "Question prompt is invalid")
                    blocking = bool(raw.get("blocking"))
                    qid = "q_" + hashlib.sha256(
                        f"{row['id']}:{index}:{prompt}".encode("utf-8")
                    ).hexdigest()[:32]
                    alternatives = ["answer", "unknown", "skip", "later"]
                    self.store.db.execute(
                        """INSERT INTO collaboration_questions(
                               id,analysis_id,workspace_id,project_id,asked_by_actor_id,
                               addressed_to_actor_id,addressed_role,prompt,shared_context,
                               blocking,alternatives_json,state,created_at_ms,updated_at_ms)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            qid, row["id"], row["workspace_id"], row["project_id"],
                            row["initiating_actor_id"], addressed, access["role"], prompt,
                            summary, int(blocking), _canonical(alternatives), "open", now, now,
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
                self.store.db.execute("COMMIT")
            except Exception:
                self.store.db.execute("ROLLBACK")
                raise
            created = [
                self._question_public(item)
                for item in self.store.db.execute(
                    "SELECT * FROM collaboration_questions WHERE analysis_id=? ORDER BY created_at_ms,id",
                    (row["id"],),
                ).fetchall()
            ]
        return {"summary": summary, "questions": created}

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
    ) -> dict[str, Any]:
        if not COMMAND_RE.fullmatch(str(command_id or "")):
            raise StoreError("INVALID_ARGUMENT", "analysis command_id is invalid")
        if model not in ANALYSIS_MODELS:
            raise StoreError("ANALYTICS_MODEL_UNAVAILABLE", "Requested consultant is unavailable")
        if purpose not in PURPOSES:
            raise StoreError("INVALID_ARGUMENT", "Analysis purpose is invalid")
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
                       status,created_at_ms,updated_at_ms)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    analysis_id, workspace_id, project_id, note_id, actor_id,
                    addressed_to_actor_id, command_id, request_sha, model, purpose,
                    clean_question, _canonical(sources), evidence,
                    hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
                    "dispatching", now, now,
                ),
            )
        prompt = (
            "Analyze only the supplied evidence. Return STRICT JSON and nothing else: "
            '{"summary":"shared context <=1200 chars","questions":['
            '{"prompt":"one contextual question","blocking":true}]}'
            ". Produce 1-4 minimal questions whose answers materially affect the requested analysis. "
            "Do not ask for facts already present in evidence. "
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

    async def refresh_analysis(
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
        with self.store._lock:
            row = self._analysis_row(analysis_id)
            if provider_status == "completed":
                markdown = self._extract_markdown(payload)
                parsed = self._materialize_questions(row, markdown) if markdown else None
                if not markdown or not parsed:
                    self.store.db.execute(
                        """UPDATE collaboration_analyses SET status='failed',
                           result_markdown=?,error_code='ANALYTICS_INVALID_RESULT',
                           updated_at_ms=?,finished_at_ms=? WHERE id=?""",
                        (markdown, now, now, analysis_id),
                    )
                else:
                    self.store.db.execute(
                        """UPDATE collaboration_analyses SET status='completed',
                           result_markdown=?,result_json=?,error_code=NULL,
                           updated_at_ms=?,finished_at_ms=? WHERE id=?""",
                        (markdown, _canonical(parsed), now, now, analysis_id),
                    )
            elif provider_status in {"failed", "cancelled", "interrupted"}:
                self.store.db.execute(
                    """UPDATE collaboration_analyses SET status=?,error_code=?,
                       updated_at_ms=?,finished_at_ms=? WHERE id=?""",
                    (
                        "cancelled" if provider_status == "cancelled" else "failed",
                        str(payload.get("errorCategory") or provider_status),
                        now, now, analysis_id,
                    ),
                )
            else:
                self.store.db.execute(
                    "UPDATE collaboration_analyses SET status='running',updated_at_ms=? WHERE id=?",
                    (now, analysis_id),
                )
            row = self._analysis_row(analysis_id)
        return self._analysis_public(actor_id, workspace_id, row)

    def inbox(
        self, *, actor_id: str, workspace_id: str, limit: int = 30
    ) -> list[dict[str, Any]]:
        self.store._membership(actor_id, workspace_id)
        self._sync_owner_development_questions()
        bounded = max(1, min(int(limit), 100))
        with self.store._lock:
            analysis_rows = self.store.db.execute(
                """SELECT q.* FROM collaboration_questions q
                   JOIN project_grants g
                     ON g.project_id=q.project_id
                    AND g.actor_id=q.addressed_to_actor_id
                   JOIN projects p ON p.id=q.project_id
                   WHERE q.workspace_id=? AND q.addressed_to_actor_id=?
                     AND g.revoked_at_ms IS NULL
                     AND p.status='active'""",
                (workspace_id, actor_id),
            ).fetchall()
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
        now = _now_ms()
        items = [
            item
            for item in items
            if not (
                item["state"] == "deferred"
                and item.get("deferred_until_ms") is not None
                and int(item["deferred_until_ms"]) > now
            )
        ]
        state_rank = {"open": 0, "deferred": 1}
        items.sort(
            key=lambda item: (
                state_rank.get(str(item["state"]), 2),
                int(item["created_at_ms"]),
                str(item["id"]),
            )
        )
        return items[:bounded]

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
                    deferred_until = int(deferred_until) if deferred_until is not None else _now_ms() + 86_400_000
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
            self.store.project_access(actor_id, workspace_id, analysis["project_id"])
            existing = self.store.db.execute(
                """SELECT * FROM collaboration_answer_commands
                   WHERE actor_id=? AND analysis_id=? AND command_id=?""",
                (actor_id, analysis_id, command_id),
            ).fetchone()
            if existing:
                if existing["payload_sha256"] != payload_sha:
                    raise StoreError("COLLABORATION_COMMAND_CONFLICT", "answer command_id conflict")
                return json.loads(existing["receipt_json"])
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
                job = self.store.db.execute(
                    "SELECT * FROM collaboration_jobs WHERE analysis_id=? AND kind='analysis_followup'",
                    (analysis_id,),
                ).fetchone()
                continuation_job_id = str(job["id"]) if job else None
                if all_resolved and not job:
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
                    "analysis_id": analysis_id,
                    "questions": questions,
                    "continuation": (
                        "queued" if all_resolved else
                        "deferred" if has_deferred else
                        "blocked" if has_unresolved else "not_required"
                    ),
                    "continuation_job_id": continuation_job_id,
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
                await self.refresh_analysis(
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
            return analyses_advanced + len(rows)

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
