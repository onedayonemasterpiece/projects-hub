from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
import uuid
from typing import Any

from .store import DurableStore, StoreError


OBJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
COMMAND_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
ALLOWED_TYPES = {"sticky", "document_card"}
ALLOWED_OPERATIONS = {"create", "update", "move", "delete", "restore"}
ALLOWED_COLORS = {"yellow", "pink", "blue", "green", "orange", "violet"}
MAX_TEXT = 8000
MAX_REFERENCE = 2000
MAX_OBJECTS = 1000
TAIL_LIMIT = 500
COORD_LIMIT = 100000.0
MIN_SIZE = 40.0
MAX_SIZE = 4000.0


def _now_ms() -> int:
    return round(time.time() * 1000)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _finite(value: Any, field: str, *, positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise StoreError("INVALID_ARGUMENT", f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise StoreError("INVALID_ARGUMENT", f"{field} must be finite")
    if positive and number <= 0:
        raise StoreError("INVALID_ARGUMENT", f"{field} must be positive")
    return number


def _safe_reference(value: Any) -> dict[str, Any] | None:
    if value in (None, {}):
        return None
    if not isinstance(value, dict):
        raise StoreError("INVALID_ARGUMENT", "reference must be an object")
    kind = str(value.get("kind") or "")
    if kind in {"analysis_run", "document"}:
        identifier = str(value.get("id") or "")
        if not OBJECT_ID_RE.fullmatch(identifier):
            raise StoreError("INVALID_ARGUMENT", "reference id is invalid")
        return {"kind": kind, "id": identifier}
    if kind == "https":
        url = str(value.get("url") or "")
        if len(url) > MAX_REFERENCE or not url.startswith("https://"):
            raise StoreError("INVALID_ARGUMENT", "only bounded HTTPS references are allowed")
        return {"kind": "https", "url": url}
    raise StoreError("INVALID_ARGUMENT", "reference kind is not allowed")


class BoardService:
    """Authoritative board state; renderer and Live tools are projections of this model."""

    def __init__(self, store: DurableStore) -> None:
        self.store = store
        self._init_schema()

    def _init_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS boards(
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL UNIQUE REFERENCES projects(id),
            schema_version INTEGER NOT NULL DEFAULT 1,
            seq INTEGER NOT NULL DEFAULT 0,
            created_at_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS board_objects(
            id TEXT PRIMARY KEY,
            board_id TEXT NOT NULL REFERENCES boards(id),
            type TEXT NOT NULL,
            object_revision INTEGER NOT NULL,
            x REAL NOT NULL,
            y REAL NOT NULL,
            width REAL NOT NULL,
            height REAL NOT NULL,
            z INTEGER NOT NULL,
            style_json TEXT NOT NULL,
            text TEXT NOT NULL,
            reference_json TEXT,
            created_by TEXT NOT NULL REFERENCES actors(id),
            created_at_ms INTEGER NOT NULL,
            updated_by TEXT NOT NULL REFERENCES actors(id),
            updated_at_ms INTEGER NOT NULL,
            deleted_at_ms INTEGER
        );
        CREATE INDEX IF NOT EXISTS board_objects_board_idx
            ON board_objects(board_id, deleted_at_ms, z, id);
        CREATE TABLE IF NOT EXISTS board_events(
            event_id TEXT PRIMARY KEY,
            board_id TEXT NOT NULL REFERENCES boards(id),
            board_seq INTEGER NOT NULL,
            object_id TEXT NOT NULL,
            resulting_revision INTEGER NOT NULL,
            initiating_actor_id TEXT NOT NULL REFERENCES actors(id),
            execution_origin TEXT NOT NULL,
            server_time_ms INTEGER NOT NULL,
            command_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            changed_fields_json TEXT NOT NULL,
            before_json TEXT,
            after_json TEXT,
            UNIQUE(board_id, board_seq)
        );
        CREATE INDEX IF NOT EXISTS board_events_object_idx
            ON board_events(board_id, object_id, board_seq DESC);
        CREATE TABLE IF NOT EXISTS board_commands(
            board_id TEXT NOT NULL REFERENCES boards(id),
            actor_id TEXT NOT NULL REFERENCES actors(id),
            command_id TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            receipt_json TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL,
            PRIMARY KEY(board_id, actor_id, command_id)
        );
        CREATE TABLE IF NOT EXISTS board_socket_tickets(
            token_sha256 TEXT PRIMARY KEY,
            board_id TEXT NOT NULL REFERENCES boards(id),
            actor_id TEXT NOT NULL REFERENCES actors(id),
            client_instance_id TEXT NOT NULL,
            expires_at_ms INTEGER NOT NULL,
            consumed_at_ms INTEGER,
            created_at_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS board_socket_tickets_expiry_idx
            ON board_socket_tickets(expires_at_ms, consumed_at_ms);
        """
        with self.store._lock:
            self.store.db.executescript(schema)

    def _board_row(self, board_id: str) -> Any:
        row = self.store.db.execute(
            """SELECT b.*,p.workspace_id,p.name AS project_name
               FROM boards b JOIN projects p ON p.id=b.project_id
               WHERE b.id=?""",
            (board_id,),
        ).fetchone()
        if not row:
            raise StoreError("BOARD_NOT_FOUND", "Board is not available")
        return row

    def open_board(
        self,
        actor_id: str,
        workspace_id: str,
        project_id: str,
        *,
        create_if_allowed: bool = True,
    ) -> dict[str, Any]:
        access = self.store.project_access(actor_id, workspace_id, project_id)
        with self.store._lock:
            row = self.store.db.execute(
                "SELECT id,project_id,schema_version,seq,created_at_ms FROM boards WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if row:
                return dict(row)
            if not create_if_allowed or access["role"] == "viewer":
                raise StoreError("BOARD_NOT_FOUND", "Project board has not been created")
            now = _now_ms()
            board_id = _new_id("brd")
            self.store.db.execute(
                "INSERT INTO boards(id,project_id,schema_version,seq,created_at_ms) VALUES(?,?,1,0,?)",
                (board_id, project_id, now),
            )
            return {
                "id": board_id,
                "project_id": project_id,
                "schema_version": 1,
                "seq": 0,
                "created_at_ms": now,
            }

    @staticmethod
    def _public_object(row: Any) -> dict[str, Any]:
        ref = json.loads(row["reference_json"]) if row["reference_json"] else None
        return {
            "id": row["id"],
            "type": row["type"],
            "object_revision": int(row["object_revision"]),
            "geometry": {
                "x": float(row["x"]),
                "y": float(row["y"]),
                "width": float(row["width"]),
                "height": float(row["height"]),
                "z": int(row["z"]),
            },
            "style": json.loads(row["style_json"]),
            "text": row["text"],
            "reference": ref,
            "created_by": row["created_by"],
            "created_at_ms": int(row["created_at_ms"]),
            "updated_by": row["updated_by"],
            "updated_at_ms": int(row["updated_at_ms"]),
            "deleted_at_ms": row["deleted_at_ms"],
        }

    def snapshot(self, actor_id: str, workspace_id: str, board_id: str) -> dict[str, Any]:
        with self.store._lock:
            board = self._board_row(board_id)
            self.store.project_access(actor_id, workspace_id, board["project_id"])
            rows = self.store.db.execute(
                """SELECT * FROM board_objects
                   WHERE board_id=? AND deleted_at_ms IS NULL
                   ORDER BY z,id""",
                (board_id,),
            ).fetchall()
            return {
                "board": {
                    "id": board["id"],
                    "project_id": board["project_id"],
                    "schema_version": int(board["schema_version"]),
                    "seq": int(board["seq"]),
                },
                "objects": [self._public_object(row) for row in rows],
            }

    def _validate_geometry(self, payload: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
        source = dict(current or {})
        source.update(payload or {})
        x = _finite(source.get("x", 0), "x")
        y = _finite(source.get("y", 0), "y")
        width = _finite(source.get("width", 280), "width", positive=True)
        height = _finite(source.get("height", 200), "height", positive=True)
        if abs(x) > COORD_LIMIT or abs(y) > COORD_LIMIT:
            raise StoreError("INVALID_ARGUMENT", "object coordinates exceed the supported board range")
        if not MIN_SIZE <= width <= MAX_SIZE or not MIN_SIZE <= height <= MAX_SIZE:
            raise StoreError("INVALID_ARGUMENT", "object dimensions exceed the supported board range")
        try:
            z = int(source.get("z", 0))
        except (TypeError, ValueError) as exc:
            raise StoreError("INVALID_ARGUMENT", "z must be an integer") from exc
        if abs(z) > 1_000_000:
            raise StoreError("INVALID_ARGUMENT", "z exceeds the supported range")
        return {"x": x, "y": y, "width": width, "height": height, "z": z}

    @staticmethod
    def _validate_style(value: Any) -> dict[str, Any]:
        style = dict(value or {}) if isinstance(value, dict) else {}
        color = str(style.get("color") or "yellow")
        if color not in ALLOWED_COLORS:
            raise StoreError("INVALID_ARGUMENT", "sticky color is not allowed")
        return {"color": color}

    def _event_public(self, row: Any) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "board_seq": int(row["board_seq"]),
            "object_id": row["object_id"],
            "resulting_revision": int(row["resulting_revision"]),
            "initiating_actor_id": row["initiating_actor_id"],
            "execution_origin": row["execution_origin"],
            "server_time_ms": int(row["server_time_ms"]),
            "command_id": row["command_id"],
            "operation": row["operation"],
            "changed_fields": json.loads(row["changed_fields_json"]),
            "before": json.loads(row["before_json"]) if row["before_json"] else None,
            "after": json.loads(row["after_json"]) if row["after_json"] else None,
        }

    def apply_command(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        board_id: str,
        command_id: str,
        operation: str,
        object_id: str,
        expected_object_revision: int | None,
        payload: dict[str, Any] | None,
        execution_origin: str = "direct_ui",
    ) -> dict[str, Any]:
        if not COMMAND_ID_RE.fullmatch(str(command_id or "")):
            raise StoreError("INVALID_ARGUMENT", "command_id is invalid")
        if operation not in ALLOWED_OPERATIONS:
            raise StoreError("INVALID_ARGUMENT", "board operation is not allowed")
        if not OBJECT_ID_RE.fullmatch(str(object_id or "")):
            raise StoreError("INVALID_ARGUMENT", "object_id is invalid")
        if execution_origin not in {"direct_ui", "mira", "analysis_publish", "undo"}:
            raise StoreError("INVALID_ARGUMENT", "execution origin is invalid")
        payload = dict(payload or {})
        envelope = {
            "operation": operation,
            "object_id": object_id,
            "expected_object_revision": expected_object_revision,
            "payload": payload,
            "execution_origin": execution_origin,
        }
        digest = _sha(envelope)
        now = _now_ms()

        with self.store._lock:
            board = self._board_row(board_id)
            self.store.project_access(
                actor_id, workspace_id, board["project_id"], require_role="editor"
            )
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                previous_cmd = self.store.db.execute(
                    """SELECT payload_sha256,receipt_json FROM board_commands
                       WHERE board_id=? AND actor_id=? AND command_id=?""",
                    (board_id, actor_id, command_id),
                ).fetchone()
                if previous_cmd:
                    if previous_cmd["payload_sha256"] != digest:
                        raise StoreError("COMMAND_CONFLICT", "command_id was already used with another payload")
                    receipt = json.loads(previous_cmd["receipt_json"])
                    self.store.db.execute("COMMIT")
                    return receipt

                row = self.store.db.execute(
                    "SELECT * FROM board_objects WHERE id=? AND board_id=?",
                    (object_id, board_id),
                ).fetchone()
                before = self._public_object(row) if row else None

                if operation == "create":
                    if row:
                        raise StoreError("OBJECT_EXISTS", "Object already exists")
                    count = int(self.store.db.execute(
                        "SELECT COUNT(*) FROM board_objects WHERE board_id=? AND deleted_at_ms IS NULL",
                        (board_id,),
                    ).fetchone()[0])
                    if count >= MAX_OBJECTS:
                        raise StoreError("BOARD_LIMIT", "Board object limit reached")
                    object_type = str(payload.get("type") or "sticky")
                    if object_type not in ALLOWED_TYPES:
                        raise StoreError("INVALID_ARGUMENT", "object type is not allowed")
                    text = str(payload.get("text") or "")
                    if len(text) > MAX_TEXT:
                        raise StoreError("INVALID_ARGUMENT", "object text is too long")
                    geometry = self._validate_geometry(dict(payload.get("geometry") or {}))
                    style = self._validate_style(payload.get("style"))
                    reference = _safe_reference(payload.get("reference"))
                    revision = 1
                    self.store.db.execute(
                        """INSERT INTO board_objects
                           (id,board_id,type,object_revision,x,y,width,height,z,style_json,text,
                            reference_json,created_by,created_at_ms,updated_by,updated_at_ms,deleted_at_ms)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                        (
                            object_id, board_id, object_type, revision,
                            geometry["x"], geometry["y"], geometry["width"], geometry["height"], geometry["z"],
                            _canonical(style), text,
                            _canonical(reference) if reference is not None else None,
                            actor_id, now, actor_id, now,
                        ),
                    )
                    changed = ["create"]
                else:
                    if not row:
                        raise StoreError("OBJECT_NOT_FOUND", "Object is not available")
                    current_revision = int(row["object_revision"])
                    if expected_object_revision is None or int(expected_object_revision) != current_revision:
                        raise StoreError(
                            "OBJECT_CONFLICT",
                            f"Expected object revision {expected_object_revision}, current is {current_revision}",
                        )
                    if operation != "restore" and row["deleted_at_ms"] is not None:
                        raise StoreError("OBJECT_DELETED", "Object is deleted")
                    revision = current_revision + 1
                    object_type = row["type"]
                    text = row["text"]
                    geometry = {
                        "x": row["x"], "y": row["y"], "width": row["width"],
                        "height": row["height"], "z": row["z"],
                    }
                    style = json.loads(row["style_json"])
                    reference = json.loads(row["reference_json"]) if row["reference_json"] else None
                    deleted_at = row["deleted_at_ms"]
                    changed: list[str] = []

                    if operation == "move":
                        geometry = self._validate_geometry(dict(payload.get("geometry") or {}), geometry)
                        changed.append("geometry")
                    elif operation == "update":
                        if "text" in payload:
                            text = str(payload["text"])
                            if len(text) > MAX_TEXT:
                                raise StoreError("INVALID_ARGUMENT", "object text is too long")
                            changed.append("text")
                        if "style" in payload:
                            style = self._validate_style(payload["style"])
                            changed.append("style")
                        if "reference" in payload:
                            reference = _safe_reference(payload["reference"])
                            changed.append("reference")
                        if "geometry" in payload:
                            geometry = self._validate_geometry(dict(payload["geometry"] or {}), geometry)
                            changed.append("geometry")
                        if not changed:
                            raise StoreError("INVALID_ARGUMENT", "update has no supported fields")
                    elif operation == "delete":
                        deleted_at = now
                        changed.append("deleted_at_ms")
                    elif operation == "restore":
                        deleted_at = None
                        changed.append("deleted_at_ms")

                    self.store.db.execute(
                        """UPDATE board_objects
                           SET object_revision=?,x=?,y=?,width=?,height=?,z=?,style_json=?,text=?,
                               reference_json=?,updated_by=?,updated_at_ms=?,deleted_at_ms=?
                           WHERE id=? AND board_id=?""",
                        (
                            revision, geometry["x"], geometry["y"], geometry["width"], geometry["height"],
                            geometry["z"], _canonical(style), text,
                            _canonical(reference) if reference is not None else None,
                            actor_id, now, deleted_at, object_id, board_id,
                        ),
                    )

                after_row = self.store.db.execute(
                    "SELECT * FROM board_objects WHERE id=? AND board_id=?",
                    (object_id, board_id),
                ).fetchone()
                after = self._public_object(after_row)
                current_seq = int(self.store.db.execute(
                    "SELECT seq FROM boards WHERE id=?", (board_id,)
                ).fetchone()["seq"])
                board_seq = current_seq + 1
                self.store.db.execute(
                    "UPDATE boards SET seq=? WHERE id=?", (board_seq, board_id)
                )
                event_id = _new_id("bev")
                event_payload = (
                    event_id, board_id, board_seq, object_id, revision, actor_id,
                    execution_origin, now, command_id, operation, _canonical(changed),
                    _canonical(before) if before is not None else None, _canonical(after),
                )
                self.store.db.execute(
                    """INSERT INTO board_events
                       (event_id,board_id,board_seq,object_id,resulting_revision,initiating_actor_id,
                        execution_origin,server_time_ms,command_id,operation,changed_fields_json,
                        before_json,after_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    event_payload,
                )
                event = {
                    "event_id": event_id,
                    "board_seq": board_seq,
                    "object_id": object_id,
                    "resulting_revision": revision,
                    "initiating_actor_id": actor_id,
                    "execution_origin": execution_origin,
                    "server_time_ms": now,
                    "command_id": command_id,
                    "operation": operation,
                    "changed_fields": changed,
                    "before": before,
                    "after": after,
                }
                receipt = {
                    "status": "saved",
                    "board_id": board_id,
                    "board_seq": board_seq,
                    "object_id": object_id,
                    "object_revision": revision,
                    "event": event,
                }
                self.store.db.execute(
                    """INSERT INTO board_commands
                       (board_id,actor_id,command_id,payload_sha256,receipt_json,created_at_ms)
                       VALUES(?,?,?,?,?,?)""",
                    (board_id, actor_id, command_id, digest, _canonical(receipt), now),
                )
                self.store.db.execute("COMMIT")
                return receipt
            except Exception:
                self.store.db.execute("ROLLBACK")
                raise

    def tail(
        self,
        actor_id: str,
        workspace_id: str,
        board_id: str,
        *,
        after_seq: int,
        limit: int = 200,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), TAIL_LIMIT))
        after_seq = max(0, int(after_seq))
        with self.store._lock:
            board = self._board_row(board_id)
            self.store.project_access(actor_id, workspace_id, board["project_id"])
            rows = self.store.db.execute(
                """SELECT * FROM board_events
                   WHERE board_id=? AND board_seq>? ORDER BY board_seq LIMIT ?""",
                (board_id, after_seq, limit + 1),
            ).fetchall()
            needs_snapshot = len(rows) > limit
            rows = rows[:limit]
            return {
                "events": [self._event_public(row) for row in rows],
                "current_seq": int(board["seq"]),
                "needs_snapshot": needs_snapshot,
            }

    def history(
        self,
        actor_id: str,
        workspace_id: str,
        board_id: str,
        object_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self.store._lock:
            board = self._board_row(board_id)
            self.store.project_access(actor_id, workspace_id, board["project_id"])
            rows = self.store.db.execute(
                """SELECT * FROM board_events
                   WHERE board_id=? AND object_id=? ORDER BY board_seq DESC LIMIT ?""",
                (board_id, object_id, limit),
            ).fetchall()
            return [self._event_public(row) for row in rows]

    def search(
        self,
        actor_id: str,
        workspace_id: str,
        board_id: str,
        query: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        normalized = " ".join(str(query or "").casefold().split())
        if not normalized:
            return []
        limit = max(1, min(int(limit), 50))
        terms = [part for part in normalized.split(" ") if part][:8]
        with self.store._lock:
            board = self._board_row(board_id)
            self.store.project_access(actor_id, workspace_id, board["project_id"])
            rows = self.store.db.execute(
                """SELECT o.*,a.display_name AS author_name
                   FROM board_objects o JOIN actors a ON a.id=o.created_by
                   WHERE o.board_id=? AND o.deleted_at_ms IS NULL""",
                (board_id,),
            ).fetchall()
            results = []
            for row in rows:
                style = json.loads(row["style_json"])
                hay = " ".join(
                    [
                        str(row["text"] or ""),
                        str(row["type"] or ""),
                        str(style.get("color") or ""),
                        str(row["author_name"] or ""),
                        str(row["id"] or ""),
                    ]
                ).casefold()
                matched = [term for term in terms if term in hay]
                if not matched:
                    continue
                obj = self._public_object(row)
                results.append(
                    {
                        "object_id": row["id"],
                        "type": row["type"],
                        "title": (row["text"] or "")[:80],
                        "snippet": (row["text"] or "")[:240],
                        "bbox": obj["geometry"],
                        "object_revision": int(row["object_revision"]),
                        "board_seq": int(board["seq"]),
                        "match_terms": matched,
                    }
                )
            results.sort(key=lambda item: (-len(item["match_terms"]), item["object_id"]))
            return results[:limit]

    def undo_last(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        board_id: str,
        command_id: str,
    ) -> dict[str, Any]:
        with self.store._lock:
            board = self._board_row(board_id)
            self.store.project_access(
                actor_id, workspace_id, board["project_id"], require_role="editor"
            )
            event = self.store.db.execute(
                """SELECT * FROM board_events
                   WHERE board_id=? AND initiating_actor_id=? AND execution_origin!='undo'
                   ORDER BY board_seq DESC LIMIT 1""",
                (board_id, actor_id),
            ).fetchone()
            if not event:
                raise StoreError("NOTHING_TO_UNDO", "There is no action to undo")
            current = self.store.db.execute(
                "SELECT * FROM board_objects WHERE board_id=? AND id=?",
                (board_id, event["object_id"]),
            ).fetchone()
            if not current or int(current["object_revision"]) != int(event["resulting_revision"]):
                raise StoreError("UNDO_CONFLICT", "Object changed after the action being undone")
            before = json.loads(event["before_json"]) if event["before_json"] else None
            expected = int(current["object_revision"])

        if before is None:
            return self.apply_command(
                actor_id=actor_id, workspace_id=workspace_id, board_id=board_id,
                command_id=command_id, operation="delete", object_id=event["object_id"],
                expected_object_revision=expected, payload={}, execution_origin="undo",
            )
        payload = {
            "text": before["text"],
            "style": before["style"],
            "reference": before["reference"],
            "geometry": before["geometry"],
        }
        receipt = self.apply_command(
            actor_id=actor_id, workspace_id=workspace_id, board_id=board_id,
            command_id=command_id, operation="update", object_id=event["object_id"],
            expected_object_revision=expected, payload=payload, execution_origin="undo",
        )
        if before.get("deleted_at_ms") is not None:
            receipt = self.apply_command(
                actor_id=actor_id, workspace_id=workspace_id, board_id=board_id,
                command_id=command_id + ":del", operation="delete", object_id=event["object_id"],
                expected_object_revision=receipt["object_revision"], payload={}, execution_origin="undo",
            )
        return receipt

    def issue_socket_ticket(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        board_id: str,
        client_instance_id: str,
        ttl_seconds: int = 60,
    ) -> dict[str, Any]:
        if not 10 <= int(ttl_seconds) <= 120:
            raise StoreError("INVALID_ARGUMENT", "Board ticket TTL is invalid")
        client = str(client_instance_id or "")[:128]
        if len(client) < 8:
            raise StoreError("INVALID_ARGUMENT", "client_instance_id is invalid")
        with self.store._lock:
            board = self._board_row(board_id)
            self.store.project_access(actor_id, workspace_id, board["project_id"])
            token = secrets.token_urlsafe(32)
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            now = _now_ms()
            expires = now + int(ttl_seconds) * 1000
            self.store.db.execute(
                """INSERT INTO board_socket_tickets
                   (token_sha256,board_id,actor_id,client_instance_id,expires_at_ms,consumed_at_ms,created_at_ms)
                   VALUES(?,?,?,?,?,NULL,?)""",
                (digest, board_id, actor_id, client, expires, now),
            )
            return {"ticket": token, "expires_at_ms": expires, "board_id": board_id}

    def consume_socket_ticket(
        self,
        *,
        ticket: str,
        board_id: str,
        client_instance_id: str,
    ) -> dict[str, Any]:
        digest = hashlib.sha256(str(ticket or "").encode("utf-8")).hexdigest()
        now = _now_ms()
        with self.store._lock:
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.store.db.execute(
                    "SELECT * FROM board_socket_tickets WHERE token_sha256=?",
                    (digest,),
                ).fetchone()
                if (
                    not row
                    or row["board_id"] != board_id
                    or row["client_instance_id"] != client_instance_id
                    or row["consumed_at_ms"] is not None
                    or int(row["expires_at_ms"]) < now
                ):
                    raise StoreError("BOARD_TICKET_INVALID", "Board socket ticket is invalid or expired")
                self.store.db.execute(
                    "UPDATE board_socket_tickets SET consumed_at_ms=? WHERE token_sha256=?",
                    (now, digest),
                )
                board = self._board_row(board_id)
                self.store.project_access(row["actor_id"], board["workspace_id"], board["project_id"])
                self.store.db.execute("COMMIT")
                return {
                    "actor_id": row["actor_id"],
                    "workspace_id": board["workspace_id"],
                    "project_id": board["project_id"],
                    "board_id": board_id,
                    "client_instance_id": row["client_instance_id"],
                }
            except Exception:
                self.store.db.execute("ROLLBACK")
                raise
