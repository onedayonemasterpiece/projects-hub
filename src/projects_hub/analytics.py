from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any

from .analytics_client import AnalyticsBridgeClient, AnalyticsBridgeError
from .board import BoardService
from .store import DurableStore, StoreError


ANALYSIS_COMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
ANALYSIS_MODELS = {"kimi_k3", "deepseek"}
ANALYSIS_PURPOSES = {"requirements", "edge_cases", "architecture", "code_review", "ideas"}
MAX_ANALYSIS_OBJECTS = 12
MAX_ANALYSIS_QUESTION = 4000
MAX_EVIDENCE_BYTES = 60000
TERMINAL_STATES = {"completed", "failed", "cancelled", "blocked"}


def _now_ms() -> int:
    return round(time.time() * 1000)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _new_id() -> str:
    return "anr_" + uuid.uuid4().hex


class AnalyticsService:
    """Durable, provided-context-only analysis separated from owner development."""

    def __init__(
        self,
        store: DurableStore,
        board: BoardService,
        *,
        bridge: AnalyticsBridgeClient | None = None,
    ) -> None:
        self.store = store
        self.board = board
        self.bridge = bridge or AnalyticsBridgeClient()
        self._init_schema()

    def _init_schema(self) -> None:
        with self.store._lock:
            self.store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS analysis_runs(
                    id TEXT PRIMARY KEY,
                    initiating_actor_id TEXT NOT NULL REFERENCES actors(id),
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    board_id TEXT NOT NULL REFERENCES boards(id),
                    command_id TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    model_alias TEXT NOT NULL,
                    question TEXT NOT NULL,
                    source_snapshot_json TEXT NOT NULL,
                    evidence_bundle TEXT NOT NULL DEFAULT '',
                    input_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provider_task_id TEXT,
                    result_markdown TEXT NOT NULL DEFAULT '',
                    result_json TEXT,
                    error_code TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    finished_at_ms INTEGER,
                    UNIQUE(initiating_actor_id, project_id, command_id)
                );
                CREATE INDEX IF NOT EXISTS analysis_runs_actor_project_idx
                    ON analysis_runs(initiating_actor_id,project_id,created_at_ms DESC);
                CREATE INDEX IF NOT EXISTS analysis_runs_provider_idx
                    ON analysis_runs(provider_task_id);
                """
            )
            columns = {
                str(row["name"])
                for row in self.store.db.execute(
                    "PRAGMA table_info(analysis_runs)"
                ).fetchall()
            }
            if "evidence_bundle" not in columns:
                self.store.db.execute(
                    "ALTER TABLE analysis_runs ADD COLUMN evidence_bundle TEXT NOT NULL DEFAULT ''"
                )

    def _freeze_sources(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        project_id: str,
        board_id: str,
        object_ids: list[str],
    ) -> tuple[list[dict[str, Any]], str]:
        self.store.project_access(
            actor_id, workspace_id, project_id, require_analyze=True
        )
        unique_ids = list(dict.fromkeys(str(item) for item in object_ids))
        if not unique_ids or len(unique_ids) > MAX_ANALYSIS_OBJECTS:
            raise StoreError(
                "INVALID_ARGUMENT",
                f"Select between 1 and {MAX_ANALYSIS_OBJECTS} board objects",
            )
        snapshot = self.board.snapshot(actor_id, workspace_id, board_id)
        if snapshot["board"]["project_id"] != project_id:
            raise StoreError("PROJECT_FORBIDDEN", "Board does not belong to the requested project")
        by_id = {item["id"]: item for item in snapshot["objects"]}
        sources: list[dict[str, Any]] = []
        for object_id in unique_ids:
            item = by_id.get(object_id)
            if not item:
                raise StoreError("OBJECT_NOT_FOUND", f"Board object {object_id} is not available")
            sources.append(
                {
                    "kind": "board_object",
                    "id": item["id"],
                    "type": item["type"],
                    "revision": int(item["object_revision"]),
                    "text": item["text"],
                    "style": item["style"],
                    "reference": item["reference"],
                }
            )
        evidence = _canonical(
            {
                "schema": "projects-hub-analysis-evidence-v1",
                "project_id": project_id,
                "board_id": board_id,
                "board_seq": int(snapshot["board"]["seq"]),
                "sources": sources,
            }
        )
        if len(evidence.encode("utf-8")) > MAX_EVIDENCE_BYTES:
            raise StoreError("ANALYTICS_INPUT_TOO_LARGE", "Selected analysis evidence is too large")
        return sources, evidence

    def _row(self, run_id: str) -> Any:
        row = self.store.db.execute(
            "SELECT * FROM analysis_runs WHERE id=?", (run_id,)
        ).fetchone()
        if not row:
            raise StoreError("ANALYSIS_NOT_FOUND", "Analysis run is not available")
        return row

    def _source_change_count(
        self, actor_id: str, workspace_id: str, row: Any
    ) -> int | None:
        try:
            snapshot = self.board.snapshot(actor_id, workspace_id, row["board_id"])
        except StoreError:
            return None
        current = {
            item["id"]: int(item["object_revision"])
            for item in snapshot["objects"]
        }
        frozen = json.loads(row["source_snapshot_json"])
        count = 0
        for item in frozen:
            if current.get(item["id"]) != int(item["revision"]):
                count += 1
        return count

    def _public(self, actor_id: str, workspace_id: str, row: Any) -> dict[str, Any]:
        sources = json.loads(row["source_snapshot_json"])
        changed = self._source_change_count(actor_id, workspace_id, row)
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "board_id": row["board_id"],
            "command_id": row["command_id"],
            "purpose": row["purpose"],
            "model": row["model_alias"],
            "question": row["question"],
            "sources": sources,
            "input_sha256": row["input_sha256"],
            "status": row["status"],
            "provider_task_id": row["provider_task_id"],
            "result_markdown": row["result_markdown"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error_code": row["error_code"],
            "cancel_requested": bool(row["cancel_requested"]),
            "source_changed": None if changed is None else changed > 0,
            "source_changed_count": changed,
            "created_at_ms": int(row["created_at_ms"]),
            "updated_at_ms": int(row["updated_at_ms"]),
            "finished_at_ms": row["finished_at_ms"],
        }

    def get_run(
        self, *, actor_id: str, workspace_id: str, run_id: str
    ) -> dict[str, Any]:
        with self.store._lock:
            row = self._row(run_id)
            if row["initiating_actor_id"] != actor_id or row["workspace_id"] != workspace_id:
                raise StoreError("ANALYSIS_NOT_FOUND", "Analysis run is not available")
            self.store.project_access(actor_id, workspace_id, row["project_id"])
            return self._public(actor_id, workspace_id, row)

    def list_runs(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        project_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self.store.project_access(actor_id, workspace_id, project_id)
        bounded = max(1, min(int(limit), 50))
        with self.store._lock:
            rows = self.store.db.execute(
                """SELECT * FROM analysis_runs
                   WHERE initiating_actor_id=? AND workspace_id=? AND project_id=?
                   ORDER BY created_at_ms DESC LIMIT ?""",
                (actor_id, workspace_id, project_id, bounded),
            ).fetchall()
            return [self._public(actor_id, workspace_id, row) for row in rows]

    async def start_single(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        project_id: str,
        board_id: str,
        object_ids: list[str],
        command_id: str,
        model: str,
        purpose: str,
        question: str,
    ) -> dict[str, Any]:
        if not ANALYSIS_COMMAND_RE.fullmatch(str(command_id or "")):
            raise StoreError("INVALID_ARGUMENT", "analysis command_id is invalid")
        if model not in ANALYSIS_MODELS:
            raise StoreError("ANALYTICS_MODEL_UNAVAILABLE", "Requested consultant is not supported")
        if purpose not in ANALYSIS_PURPOSES:
            raise StoreError("INVALID_ARGUMENT", "Analysis purpose is not supported")
        clean_question = str(question or "").strip()
        if not clean_question or len(clean_question) > MAX_ANALYSIS_QUESTION:
            raise StoreError("INVALID_ARGUMENT", "Analysis question is invalid")

        sources, evidence = self._freeze_sources(
            actor_id=actor_id,
            workspace_id=workspace_id,
            project_id=project_id,
            board_id=board_id,
            object_ids=object_ids,
        )
        request = {
            "project_id": project_id,
            "board_id": board_id,
            "object_revisions": [{"id": item["id"], "revision": item["revision"]} for item in sources],
            "model": model,
            "purpose": purpose,
            "question": clean_question,
        }
        request_sha = _sha(request)
        now = _now_ms()

        with self.store._lock:
            existing = self.store.db.execute(
                """SELECT * FROM analysis_runs
                   WHERE initiating_actor_id=? AND project_id=? AND command_id=?""",
                (actor_id, project_id, command_id),
            ).fetchone()
            if existing:
                if existing["request_sha256"] != request_sha:
                    raise StoreError(
                        "ANALYSIS_COMMAND_CONFLICT",
                        "analysis command_id was already used with another request",
                    )
                return self._public(actor_id, workspace_id, existing)

            run_id = _new_id()
            self.store.db.execute(
                """INSERT INTO analysis_runs(
                       id,initiating_actor_id,workspace_id,project_id,board_id,command_id,
                       request_sha256,purpose,model_alias,question,source_snapshot_json,evidence_bundle,
                       input_sha256,status,created_at_ms,updated_at_ms)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, actor_id, workspace_id, project_id, board_id, command_id,
                    request_sha, purpose, model, clean_question, _canonical(sources), evidence,
                    hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
                    "dispatching", now, now,
                ),
            )

        try:
            response = await self.bridge.consult(
                model=model,
                purpose=purpose,
                question=clean_question,
                evidence_bundle=evidence,
                request_key=f"analysis:{run_id}",
            )
        except AnalyticsBridgeError as exc:
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE analysis_runs
                       SET status='dispatch_unknown',error_code=?,updated_at_ms=?
                       WHERE id=?""",
                    ("ANALYTICS_BRIDGE_INTERRUPTED", _now_ms(), run_id),
                )
            raise StoreError(
                "ANALYTICS_DISPATCH_UNKNOWN",
                "Analysis dispatch outcome is unknown; the saved run can be reconciled safely",
            ) from exc

        status = str(response.get("status") or "failed")
        task_id = response.get("taskId") or response.get("taskReference")
        if not isinstance(task_id, str):
            task_id = None
        if status not in {"running", "completed", "dispatch_unknown", "waiting_capacity"}:
            error = str(response.get("errorCategory") or "ANALYTICS_PROVIDER_FAILED")
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE analysis_runs
                       SET status='failed',provider_task_id=?,error_code=?,
                           updated_at_ms=?,finished_at_ms=?
                       WHERE id=?""",
                    (task_id, error, _now_ms(), _now_ms(), run_id),
                )
            return self.get_run(actor_id=actor_id, workspace_id=workspace_id, run_id=run_id)

        mapped = "dispatch_unknown" if status == "dispatch_unknown" else (
            "waiting_capacity" if status == "waiting_capacity" else "running"
        )
        with self.store._lock:
            self.store.db.execute(
                """UPDATE analysis_runs
                   SET status=?,provider_task_id=?,error_code=NULL,updated_at_ms=?
                   WHERE id=?""",
                (mapped, task_id, _now_ms(), run_id),
            )
        if status == "completed":
            return await self.refresh(
                actor_id=actor_id, workspace_id=workspace_id, run_id=run_id
            )
        return self.get_run(actor_id=actor_id, workspace_id=workspace_id, run_id=run_id)

    @staticmethod
    def _extract_markdown(payload: dict[str, Any]) -> str:
        latest = payload.get("latestTurn")
        if isinstance(latest, dict):
            value = latest.get("finalResponse")
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("finalResponse", "result_markdown"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        value = payload.get("content")
        if isinstance(value, str) and value.strip() and len(value.strip()) > 80:
            return value.strip()
        return ""

    async def _reconcile_without_task(self, row: Any) -> dict[str, Any]:
        evidence = str(row["evidence_bundle"] or "")
        if not evidence:
            raise AnalyticsBridgeError(
                "Frozen evidence is unavailable; refusing to reconstruct a different request"
            )
        if hashlib.sha256(evidence.encode("utf-8")).hexdigest() != row["input_sha256"]:
            raise AnalyticsBridgeError(
                "Frozen evidence integrity check failed; refusing provider redispatch"
            )
        return await self.bridge.consult(
            model=row["model_alias"],
            purpose=row["purpose"],
            question=row["question"],
            evidence_bundle=evidence,
            request_key=f"analysis:{row['id']}",
        )

    async def refresh(
        self, *, actor_id: str, workspace_id: str, run_id: str
    ) -> dict[str, Any]:
        with self.store._lock:
            row = self._row(run_id)
            if row["initiating_actor_id"] != actor_id or row["workspace_id"] != workspace_id:
                raise StoreError("ANALYSIS_NOT_FOUND", "Analysis run is not available")
            self.store.project_access(actor_id, workspace_id, row["project_id"])
            if row["status"] in TERMINAL_STATES:
                return self._public(actor_id, workspace_id, row)
            run_snapshot = dict(row)

        task_id = run_snapshot.get("provider_task_id")
        try:
            if not task_id:
                replay = await self._reconcile_without_task(run_snapshot)
                task_id = replay.get("taskId") or replay.get("taskReference")
                replay_status = str(replay.get("status") or "dispatch_unknown")
                with self.store._lock:
                    self.store.db.execute(
                        """UPDATE analysis_runs SET provider_task_id=?,status=?,updated_at_ms=?
                           WHERE id=?""",
                        (
                            task_id if isinstance(task_id, str) else None,
                            "dispatch_unknown" if replay_status == "dispatch_unknown" else "running",
                            _now_ms(),
                            run_id,
                        ),
                    )
                if not isinstance(task_id, str):
                    return self.get_run(
                        actor_id=actor_id, workspace_id=workspace_id, run_id=run_id
                    )
            payload = await self.bridge.read_task(str(task_id))
        except AnalyticsBridgeError:
            with self.store._lock:
                self.store.db.execute(
                    """UPDATE analysis_runs
                       SET status='dispatch_unknown',error_code='ANALYTICS_READBACK_UNAVAILABLE',
                           updated_at_ms=? WHERE id=?""",
                    (_now_ms(), run_id),
                )
            return self.get_run(actor_id=actor_id, workspace_id=workspace_id, run_id=run_id)

        provider_status = str(
            payload.get("executionStatus") or payload.get("status") or "running"
        )
        now = _now_ms()
        with self.store._lock:
            current = self._row(run_id)
            if bool(current["cancel_requested"]) or current["status"] == "cancelled":
                return self._public(actor_id, workspace_id, current)
            if provider_status == "completed":
                markdown = self._extract_markdown(payload)
                if not markdown:
                    self.store.db.execute(
                        """UPDATE analysis_runs
                           SET status='failed',error_code='ANALYTICS_EMPTY_RESULT',
                               updated_at_ms=?,finished_at_ms=? WHERE id=?""",
                        (now, now, run_id),
                    )
                else:
                    self.store.db.execute(
                        """UPDATE analysis_runs
                           SET status='completed',result_markdown=?,result_json=?,
                               error_code=NULL,updated_at_ms=?,finished_at_ms=? WHERE id=?""",
                        (
                            markdown,
                            _canonical(
                                {
                                    "summary": markdown.splitlines()[0][:500],
                                    "participant": current["model_alias"],
                                    "purpose": current["purpose"],
                                }
                            ),
                            now, now, run_id,
                        ),
                    )
            elif provider_status in {"failed", "cancelled", "interrupted"}:
                state = "cancelled" if provider_status == "cancelled" else "failed"
                self.store.db.execute(
                    """UPDATE analysis_runs
                       SET status=?,error_code=?,updated_at_ms=?,finished_at_ms=? WHERE id=?""",
                    (
                        state,
                        str(payload.get("errorCategory") or provider_status),
                        now, now, run_id,
                    ),
                )
            elif provider_status == "dispatch_unknown":
                self.store.db.execute(
                    """UPDATE analysis_runs SET status='dispatch_unknown',updated_at_ms=? WHERE id=?""",
                    (now, run_id),
                )
            else:
                self.store.db.execute(
                    """UPDATE analysis_runs SET status='running',updated_at_ms=? WHERE id=?""",
                    (now, run_id),
                )
            return self._public(actor_id, workspace_id, self._row(run_id))

    async def cancel(
        self, *, actor_id: str, workspace_id: str, run_id: str
    ) -> dict[str, Any]:
        now = _now_ms()
        with self.store._lock:
            row = self._row(run_id)
            if row["initiating_actor_id"] != actor_id or row["workspace_id"] != workspace_id:
                raise StoreError("ANALYSIS_NOT_FOUND", "Analysis run is not available")
            self.store.project_access(actor_id, workspace_id, row["project_id"])
            if row["status"] in TERMINAL_STATES:
                return self._public(actor_id, workspace_id, row)
            task_id = row["provider_task_id"]
            self.store.db.execute(
                """UPDATE analysis_runs
                   SET status='cancelled',cancel_requested=1,updated_at_ms=?,finished_at_ms=?
                   WHERE id=?""",
                (now, now, run_id),
            )
        if isinstance(task_id, str):
            try:
                await self.bridge.cancel_task(task_id)
            except AnalyticsBridgeError:
                pass
        return self.get_run(actor_id=actor_id, workspace_id=workspace_id, run_id=run_id)

    def publish_to_board(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        run_id: str,
        command_id: str,
        object_id: str,
        geometry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.store._lock:
            row = self._row(run_id)
            if row["initiating_actor_id"] != actor_id or row["workspace_id"] != workspace_id:
                raise StoreError("ANALYSIS_NOT_FOUND", "Analysis run is not available")
            self.store.project_access(
                actor_id, workspace_id, row["project_id"], require_role="editor"
            )
            if row["status"] != "completed" or not row["result_markdown"]:
                raise StoreError("ANALYSIS_NOT_READY", "Analysis report is not ready")
            title = row["result_markdown"].splitlines()[0].lstrip("# ").strip()
            if not title:
                title = "Аналитический отчёт"
            board_id = row["board_id"]
        return self.board.apply_command(
            actor_id=actor_id,
            workspace_id=workspace_id,
            board_id=board_id,
            command_id=command_id,
            operation="create",
            object_id=object_id,
            expected_object_revision=None,
            payload={
                "type": "document_card",
                "text": title[:500],
                "style": {"color": "violet"},
                "geometry": geometry or {"width": 340, "height": 180},
                "reference": {"kind": "analysis_run", "id": run_id},
            },
            execution_origin="analysis_publish",
        )

    async def close(self) -> None:
        await self.bridge.close()
