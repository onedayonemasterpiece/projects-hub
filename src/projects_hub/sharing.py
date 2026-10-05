from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from typing import Any
from urllib.parse import quote

from .board import BoardService
from .store import DurableStore, StoreError


SHARE_TTL_MS = 7 * 24 * 60 * 60 * 1000
GUEST_SESSION_COOKIE = "projects_hub_guest"
GUEST_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
GUEST_SOCKET_TICKET_TTL_MS = 30_000


def _now_ms() -> int:
    return round(time.time() * 1000)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SharingService:
    """Seven-day read-only guest grants with bearer tokens stored only as hashes."""

    def __init__(
        self,
        store: DurableStore,
        board: BoardService,
        *,
        public_origin: str = "",
    ) -> None:
        self.store = store
        self.board = board
        self.public_origin = public_origin.rstrip("/")
        self._init_schema()

    def _init_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS board_share_grants(
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            board_id TEXT NOT NULL REFERENCES boards(id),
            created_by TEXT NOT NULL REFERENCES actors(id),
            token_sha256 TEXT NOT NULL UNIQUE,
            created_at_ms INTEGER NOT NULL,
            expires_at_ms INTEGER NOT NULL,
            revoked_at_ms INTEGER
        );
        CREATE INDEX IF NOT EXISTS board_share_grants_project_idx
            ON board_share_grants(project_id, revoked_at_ms, expires_at_ms);
        CREATE TABLE IF NOT EXISTS board_guest_sessions(
            token_sha256 TEXT PRIMARY KEY,
            grant_id TEXT NOT NULL REFERENCES board_share_grants(id),
            expires_at_ms INTEGER NOT NULL,
            created_at_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS board_guest_sessions_grant_idx
            ON board_guest_sessions(grant_id, expires_at_ms);
        CREATE TABLE IF NOT EXISTS board_guest_socket_tickets(
            token_sha256 TEXT PRIMARY KEY,
            guest_session_sha256 TEXT NOT NULL REFERENCES board_guest_sessions(token_sha256),
            grant_id TEXT NOT NULL REFERENCES board_share_grants(id),
            client_instance_id TEXT NOT NULL,
            expires_at_ms INTEGER NOT NULL,
            consumed_at_ms INTEGER,
            created_at_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS board_guest_socket_tickets_expiry_idx
            ON board_guest_socket_tickets(expires_at_ms, consumed_at_ms);
        """
        with self.store._lock:
            self.store.db.executescript(schema)

    def _share_access(
        self, actor_id: str, workspace_id: str, project_id: str
    ) -> dict[str, Any]:
        return self.store.project_access(
            actor_id,
            workspace_id,
            project_id,
            require_role="editor",
            require_manage_share=True,
        )

    def create_share(
        self, *, actor_id: str, workspace_id: str, project_id: str
    ) -> dict[str, Any]:
        self._share_access(actor_id, workspace_id, project_id)
        board = self.board.open_board(
            actor_id, workspace_id, project_id, create_if_allowed=False
        )
        now = _now_ms()
        raw = secrets.token_urlsafe(32)
        grant_id = _id("shg")
        expires = now + SHARE_TTL_MS
        with self.store._lock:
            self.store.db.execute(
                """INSERT INTO board_share_grants(
                       id,workspace_id,project_id,board_id,created_by,token_sha256,
                       created_at_ms,expires_at_ms,revoked_at_ms)
                   VALUES(?,?,?,?,?,?,?,?,NULL)""",
                (
                    grant_id,
                    workspace_id,
                    project_id,
                    board["id"],
                    actor_id,
                    _hash(raw),
                    now,
                    expires,
                ),
            )
        path = "/guest/board#token=" + quote(raw, safe="")
        return {
            "id": grant_id,
            "project_id": project_id,
            "board_id": board["id"],
            "created_at_ms": now,
            "expires_at_ms": expires,
            "url": self.public_origin + path if self.public_origin else path,
            "warning": "Будущие изменения доски будут видны по ссылке до истечения срока или отзыва.",
        }

    def list_shares(
        self, *, actor_id: str, workspace_id: str, project_id: str
    ) -> list[dict[str, Any]]:
        self._share_access(actor_id, workspace_id, project_id)
        with self.store._lock:
            rows = self.store.db.execute(
                """SELECT id,project_id,board_id,created_at_ms,expires_at_ms,revoked_at_ms
                   FROM board_share_grants
                   WHERE workspace_id=? AND project_id=?
                   ORDER BY created_at_ms DESC""",
                (workspace_id, project_id),
            ).fetchall()
            return [dict(row) for row in rows]

    def revoke_share(
        self, *, actor_id: str, workspace_id: str, share_id: str
    ) -> dict[str, Any]:
        now = _now_ms()
        with self.store._lock:
            row = self.store.db.execute(
                "SELECT * FROM board_share_grants WHERE id=? AND workspace_id=?",
                (share_id, workspace_id),
            ).fetchone()
            if not row:
                raise StoreError("SHARE_NOT_FOUND", "Share is not available")
            self._share_access(actor_id, workspace_id, row["project_id"])
            if row["revoked_at_ms"] is None:
                self.store.db.execute(
                    "UPDATE board_share_grants SET revoked_at_ms=? WHERE id=?",
                    (now, share_id),
                )
            return {
                "id": share_id,
                "board_id": row["board_id"],
                "project_id": row["project_id"],
                "revoked_at_ms": row["revoked_at_ms"] or now,
            }

    def _active_grant_by_token(self, raw_token: str) -> Any:
        if not isinstance(raw_token, str) or not 20 <= len(raw_token) <= 200:
            raise StoreError("GUEST_TOKEN_INVALID", "Guest link is invalid")
        now = _now_ms()
        row = self.store.db.execute(
            """SELECT g.*,p.name AS project_name
               FROM board_share_grants g
               JOIN projects p ON p.id=g.project_id
               WHERE g.token_sha256=?""",
            (_hash(raw_token),),
        ).fetchone()
        if (
            not row
            or row["revoked_at_ms"] is not None
            or int(row["expires_at_ms"]) <= now
        ):
            raise StoreError("GUEST_TOKEN_INVALID", "Guest link is invalid or expired")
        return row

    def exchange(self, raw_token: str) -> dict[str, Any]:
        now = _now_ms()
        session = secrets.token_urlsafe(32)
        session_sha = _hash(session)
        with self.store._lock:
            grant = self._active_grant_by_token(raw_token)
            expires = min(int(grant["expires_at_ms"]), now + SHARE_TTL_MS)
            self.store.db.execute(
                """INSERT INTO board_guest_sessions(
                       token_sha256,grant_id,expires_at_ms,created_at_ms)
                   VALUES(?,?,?,?)""",
                (session_sha, grant["id"], expires, now),
            )
            return {
                "session_token": session,
                "expires_at_ms": expires,
                "grant_id": grant["id"],
                "board_id": grant["board_id"],
                "project_name": grant["project_name"],
            }

    def _guest_scope_by_hash(self, session_sha: str) -> dict[str, Any]:
        now = _now_ms()
        row = self.store.db.execute(
            """SELECT s.token_sha256 AS session_sha256,
                      s.expires_at_ms AS session_expires_at_ms,
                      g.id AS grant_id,g.workspace_id,g.project_id,g.board_id,
                      g.expires_at_ms,g.revoked_at_ms,p.name AS project_name,
                      b.schema_version,b.seq
               FROM board_guest_sessions s
               JOIN board_share_grants g ON g.id=s.grant_id
               JOIN projects p ON p.id=g.project_id
               JOIN boards b ON b.id=g.board_id
               WHERE s.token_sha256=?""",
            (session_sha,),
        ).fetchone()
        if (
            not row
            or row["revoked_at_ms"] is not None
            or int(row["expires_at_ms"]) <= now
            or int(row["session_expires_at_ms"]) <= now
        ):
            raise StoreError("GUEST_SESSION_INVALID", "Guest session is invalid or expired")
        return dict(row)

    def guest_scope(self, raw_session: str | None) -> dict[str, Any]:
        if not raw_session:
            raise StoreError("GUEST_SESSION_INVALID", "Guest session is required")
        with self.store._lock:
            return self._guest_scope_by_hash(_hash(raw_session))

    @staticmethod
    def _project_object(value: dict[str, Any]) -> dict[str, Any]:
        projected = {
            "id": value["id"],
            "type": value["type"],
            "object_revision": int(value["object_revision"]),
            "geometry": dict(value["geometry"]),
            "style": dict(value["style"]),
            "text": value["text"],
            "reference": None,
            "created_by": "",
            "created_at_ms": int(value["created_at_ms"]),
            "updated_by": "",
            "updated_at_ms": int(value["updated_at_ms"]),
            "deleted_at_ms": value.get("deleted_at_ms"),
        }
        if value["type"] == "document_card":
            projected["text"] = "Закрытый документ"
        return projected

    def guest_snapshot(self, raw_session: str | None) -> dict[str, Any]:
        with self.store._lock:
            scope = self._guest_scope_by_hash(_hash(raw_session or ""))
            rows = self.store.db.execute(
                """SELECT * FROM board_objects
                   WHERE board_id=? AND deleted_at_ms IS NULL
                   ORDER BY z,id""",
                (scope["board_id"],),
            ).fetchall()
            objects = [
                self._project_object(self.board._public_object(row))
                for row in rows
            ]
            return {
                "board": {
                    "id": scope["board_id"],
                    "project_id": scope["project_id"],
                    "project_name": scope["project_name"],
                    "schema_version": int(scope["schema_version"]),
                    "seq": int(scope["seq"]),
                },
                "objects": objects,
                "expires_at_ms": int(scope["expires_at_ms"]),
            }

    def project_event(
        self, *, grant_id: str, session_sha: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        with self.store._lock:
            scope = self._guest_scope_by_hash(session_sha)
            if scope["grant_id"] != grant_id:
                raise StoreError("GUEST_SESSION_INVALID", "Guest session scope changed")
            if payload.get("type") != "event":
                return None
            event = payload.get("event")
            if not isinstance(event, dict):
                return None
            projected = {
                "event_id": event.get("event_id"),
                "board_seq": event.get("board_seq"),
                "object_id": event.get("object_id"),
                "resulting_revision": event.get("resulting_revision"),
                "server_time_ms": event.get("server_time_ms"),
                "operation": event.get("operation"),
                "changed_fields": event.get("changed_fields") or [],
                "before": (
                    self._project_object(event["before"])
                    if isinstance(event.get("before"), dict)
                    else None
                ),
                "after": (
                    self._project_object(event["after"])
                    if isinstance(event.get("after"), dict)
                    else None
                ),
            }
            return {"type": "event", "event": projected}

    def issue_socket_ticket(
        self, *, raw_session: str | None, client_instance_id: str
    ) -> dict[str, Any]:
        now = _now_ms()
        with self.store._lock:
            session_sha = _hash(raw_session or "")
            scope = self._guest_scope_by_hash(session_sha)
            raw_ticket = secrets.token_urlsafe(32)
            expires = min(
                now + GUEST_SOCKET_TICKET_TTL_MS,
                int(scope["expires_at_ms"]),
                int(scope["session_expires_at_ms"]),
            )
            self.store.db.execute(
                """INSERT INTO board_guest_socket_tickets(
                       token_sha256,guest_session_sha256,grant_id,client_instance_id,
                       expires_at_ms,consumed_at_ms,created_at_ms)
                   VALUES(?,?,?,?,?,NULL,?)""",
                (
                    _hash(raw_ticket),
                    session_sha,
                    scope["grant_id"],
                    client_instance_id,
                    expires,
                    now,
                ),
            )
            return {
                "ticket": raw_ticket,
                "expires_at_ms": expires,
                "board_id": scope["board_id"],
            }

    def consume_socket_ticket(
        self, *, ticket: str, client_instance_id: str
    ) -> dict[str, Any]:
        now = _now_ms()
        digest = _hash(ticket)
        with self.store._lock:
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.store.db.execute(
                    "SELECT * FROM board_guest_socket_tickets WHERE token_sha256=?",
                    (digest,),
                ).fetchone()
                if (
                    not row
                    or row["consumed_at_ms"] is not None
                    or int(row["expires_at_ms"]) <= now
                    or row["client_instance_id"] != client_instance_id
                ):
                    raise StoreError("GUEST_TICKET_INVALID", "Guest socket ticket is invalid")
                scope = self._guest_scope_by_hash(row["guest_session_sha256"])
                if scope["grant_id"] != row["grant_id"]:
                    raise StoreError("GUEST_TICKET_INVALID", "Guest socket ticket scope changed")
                self.store.db.execute(
                    """UPDATE board_guest_socket_tickets
                       SET consumed_at_ms=? WHERE token_sha256=? AND consumed_at_ms IS NULL""",
                    (now, digest),
                )
                self.store.db.execute("COMMIT")
                return {
                    **scope,
                    "session_sha256": row["guest_session_sha256"],
                    "client_instance_id": client_instance_id,
                }
            except Exception:
                self.store.db.execute("ROLLBACK")
                raise

    def validate_connection(
        self, *, grant_id: str, session_sha: str
    ) -> dict[str, Any]:
        with self.store._lock:
            scope = self._guest_scope_by_hash(session_sha)
            if scope["grant_id"] != grant_id:
                raise StoreError("GUEST_SESSION_INVALID", "Guest session scope changed")
            return scope
