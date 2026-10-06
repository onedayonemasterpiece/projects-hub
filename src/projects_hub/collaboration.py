from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from typing import Any

from .github_connections import GitHubConnections
from .store import DurableStore, StoreError


COMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
MAX_TITLE = 240
MAX_BODY = 60_000
MAX_REPLY = 12_000


def _now_ms() -> int:
    return round(time.time() * 1000)


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class CollaborationService:
    """Shared project notes/discussion over Projects Hub ACL + bound repository."""

    def __init__(self, store: DurableStore, github: GitHubConnections) -> None:
        self.store = store
        self.github = github
        self._init_schema()

    def _init_schema(self) -> None:
        with self.store._lock:
            self.store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS project_notes(
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    author_actor_id TEXT NOT NULL REFERENCES actors(id),
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    markdown TEXT NOT NULL,
                    author_roles_json TEXT NOT NULL,
                    audience TEXT NOT NULL DEFAULT 'project',
                    repository_id INTEGER NOT NULL,
                    repository_full_name TEXT NOT NULL,
                    repository_path TEXT NOT NULL,
                    repository_sha TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    UNIQUE(author_actor_id, project_id, command_id)
                );
                CREATE INDEX IF NOT EXISTS project_notes_project_idx
                    ON project_notes(project_id, created_at_ms DESC, id);

                CREATE TABLE IF NOT EXISTS project_discussion_entries(
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    note_id TEXT NOT NULL REFERENCES project_notes(id),
                    author_actor_id TEXT NOT NULL REFERENCES actors(id),
                    body TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    UNIQUE(author_actor_id, note_id, command_id)
                );
                CREATE INDEX IF NOT EXISTS project_discussion_note_idx
                    ON project_discussion_entries(note_id, created_at_ms, id);

                CREATE TABLE IF NOT EXISTS collaboration_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    actor_id TEXT NOT NULL REFERENCES actors(id),
                    kind TEXT NOT NULL,
                    object_kind TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    parent_object_id TEXT,
                    addressed_to_actor_id TEXT REFERENCES actors(id),
                    summary TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS collaboration_events_workspace_idx
                    ON collaboration_events(workspace_id, id DESC);
                CREATE TABLE IF NOT EXISTS collaboration_attention_state(
                    actor_id TEXT NOT NULL REFERENCES actors(id),
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    personal_cursor INTEGER NOT NULL DEFAULT 0,
                    general_cursor INTEGER NOT NULL DEFAULT 0,
                    general_news_enabled INTEGER NOT NULL DEFAULT 1 CHECK(general_news_enabled IN (0,1)),
                    updated_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(actor_id,workspace_id)
                );
                """
            )

    @staticmethod
    def _clean_command(command_id: str) -> str:
        value = str(command_id or "").strip()
        if not COMMAND_RE.fullmatch(value):
            raise StoreError("INVALID_ARGUMENT", "command_id is invalid")
        return value

    def _actor(self, actor_id: str) -> dict[str, Any]:
        with self.store._lock:
            row = self.store.db.execute(
                "SELECT id,display_name FROM actors WHERE id=?", (actor_id,)
            ).fetchone()
            if not row:
                raise StoreError("FORBIDDEN", "Actor is not available")
            return dict(row)

    def _project_docs_repository(
        self, actor_id: str, workspace_id: str, project_id: str
    ) -> dict[str, Any]:
        candidates = [
            item
            for item in self.store.list_repository_connections(actor_id, workspace_id)
            if item.get("state") == "available"
            and item.get("installation_state") == "active"
            and item.get("project_id") == project_id
            and item.get("role") == "project_docs"
            and item.get("access_mode") == "app_managed_write"
        ]
        if len(candidates) != 1:
            raise StoreError(
                "GITHUB_PROJECT_DOCS_REQUIRED",
                "Project requires exactly one writable project_docs repository",
            )
        return candidates[0]

    @staticmethod
    def _note_identity(actor_id: str, project_id: str, command_id: str) -> tuple[str, str]:
        digest = hashlib.sha256(
            f"{actor_id}\n{project_id}\n{command_id}".encode("utf-8")
        ).hexdigest()
        return "note_" + digest[:32], f"docs/notes/{digest[:2]}/note-{digest[:24]}.md"

    @staticmethod
    def _markdown(
        *,
        note_id: str,
        project_id: str,
        title: str,
        body: str,
        author: dict[str, Any],
        roles: list[str],
        created_at_ms: int,
    ) -> str:
        return (
            "---\n"
            f"note_id: {note_id}\n"
            f"project_id: {project_id}\n"
            f"author_actor_id: {author['id']}\n"
            f"author_display_name: {json.dumps(author['display_name'], ensure_ascii=False)}\n"
            f"author_roles: {json.dumps(roles, ensure_ascii=False)}\n"
            "audience: project\n"
            f"created_at_ms: {created_at_ms}\n"
            "---\n\n"
            f"# {title}\n\n{body.rstrip()}\n"
        )

    def _public_note(self, actor_id: str, workspace_id: str, row: Any) -> dict[str, Any]:
        self.store.project_access(actor_id, workspace_id, row["project_id"])
        author = self._actor(str(row["author_actor_id"]))
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "author": author,
            "author_roles": json.loads(row["author_roles_json"]),
            "title": row["title"],
            "body": row["body"],
            "audience": row["audience"],
            "repository": {
                "repository_id": int(row["repository_id"]),
                "full_name": row["repository_full_name"],
                "path": row["repository_path"],
                "sha": row["repository_sha"],
            },
            "revision": int(row["revision"]),
            "created_at_ms": int(row["created_at_ms"]),
            "updated_at_ms": int(row["updated_at_ms"]),
        }

    def _public_reply(self, actor_id: str, workspace_id: str, row: Any) -> dict[str, Any]:
        self.store.project_access(actor_id, workspace_id, row["project_id"])
        return {
            "id": row["id"],
            "note_id": row["note_id"],
            "project_id": row["project_id"],
            "author": self._actor(str(row["author_actor_id"])),
            "body": row["body"],
            "created_at_ms": int(row["created_at_ms"]),
        }

    async def create_note(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        project_id: str,
        command_id: str,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        access = self.store.project_access(
            actor_id, workspace_id, project_id, require_role="editor"
        )
        command_id = self._clean_command(command_id)
        clean_title = str(title or "").strip()
        clean_body = str(body or "").strip()
        if not clean_title or len(clean_title) > MAX_TITLE:
            raise StoreError("INVALID_ARGUMENT", "note title is invalid")
        if not clean_body or len(clean_body) > MAX_BODY:
            raise StoreError("INVALID_ARGUMENT", "note body is invalid")
        request_sha = _sha({"title": clean_title, "body": clean_body})
        with self.store._lock:
            existing = self.store.db.execute(
                """SELECT * FROM project_notes
                   WHERE author_actor_id=? AND project_id=? AND command_id=?""",
                (actor_id, project_id, command_id),
            ).fetchone()
            if existing:
                if existing["request_sha256"] != request_sha:
                    raise StoreError(
                        "COLLABORATION_COMMAND_CONFLICT",
                        "command_id was already used with another note payload",
                    )
                return self._public_note(actor_id, workspace_id, existing)

        actor = self._actor(actor_id)
        roles = [str(access["role"])]
        workspace_role = self.store.workspace_role(actor_id, workspace_id)
        if workspace_role not in roles:
            roles.append(workspace_role)
        note_id, path = self._note_identity(actor_id, project_id, command_id)
        now = _now_ms()
        markdown = self._markdown(
            note_id=note_id,
            project_id=project_id,
            title=clean_title,
            body=clean_body,
            author=actor,
            roles=roles,
            created_at_ms=now,
        )
        repository = self._project_docs_repository(actor_id, workspace_id, project_id)
        verified = await self.github.write_repository_text(
            actor_id=actor_id,
            workspace_id=workspace_id,
            repository_id=int(repository["repository_id"]),
            project_id=project_id,
            path=path,
            text=markdown,
            message=f"docs: add project note {note_id}",
        )
        if verified.get("text") != markdown:
            raise StoreError(
                "GITHUB_READBACK_MISMATCH",
                "Repository readback does not match project note Markdown",
            )
        repository_sha = str(verified.get("sha") or "")
        if not repository_sha:
            raise StoreError("GITHUB_READBACK_MISMATCH", "Repository readback SHA is absent")

        with self.store._lock:
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                existing = self.store.db.execute(
                    """SELECT * FROM project_notes
                       WHERE author_actor_id=? AND project_id=? AND command_id=?""",
                    (actor_id, project_id, command_id),
                ).fetchone()
                if existing:
                    if existing["request_sha256"] != request_sha:
                        raise StoreError(
                            "COLLABORATION_COMMAND_CONFLICT",
                            "command_id was already used with another note payload",
                        )
                    self.store.db.execute("COMMIT")
                    return self._public_note(actor_id, workspace_id, existing)
                self.store.db.execute(
                    """INSERT INTO project_notes(
                           id,workspace_id,project_id,author_actor_id,title,body,markdown,
                           author_roles_json,audience,repository_id,repository_full_name,
                           repository_path,repository_sha,command_id,request_sha256,
                           revision,created_at_ms,updated_at_ms)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        note_id, workspace_id, project_id, actor_id, clean_title, clean_body,
                        markdown, json.dumps(roles, ensure_ascii=False), "project",
                        int(repository["repository_id"]), str(repository["full_name"]),
                        path, repository_sha, command_id, request_sha, 1, now, now,
                    ),
                )
                self.store.db.execute(
                    """INSERT INTO collaboration_events(
                           workspace_id,project_id,actor_id,kind,object_kind,object_id,
                           parent_object_id,addressed_to_actor_id,summary,created_at_ms)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        workspace_id, project_id, actor_id, "note_created", "note",
                        note_id, None, None, clean_title, now,
                    ),
                )
                row = self.store.db.execute(
                    "SELECT * FROM project_notes WHERE id=?", (note_id,)
                ).fetchone()
                self.store.db.execute("COMMIT")
            except Exception:
                self.store.db.execute("ROLLBACK")
                raise
        return self._public_note(actor_id, workspace_id, row)

    def get_note(
        self, *, actor_id: str, workspace_id: str, note_id: str
    ) -> dict[str, Any]:
        with self.store._lock:
            row = self.store.db.execute(
                "SELECT * FROM project_notes WHERE id=? AND workspace_id=?",
                (note_id, workspace_id),
            ).fetchone()
            if not row:
                raise StoreError("NOTE_NOT_FOUND", "Project note is not available")
        return self._public_note(actor_id, workspace_id, row)

    def list_notes(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        project_id: str,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        self.store.project_access(actor_id, workspace_id, project_id)
        bounded = max(1, min(int(limit), 100))
        with self.store._lock:
            rows = self.store.db.execute(
                """SELECT * FROM project_notes
                   WHERE workspace_id=? AND project_id=?
                   ORDER BY created_at_ms DESC,id DESC LIMIT ?""",
                (workspace_id, project_id, bounded),
            ).fetchall()
        return [self._public_note(actor_id, workspace_id, row) for row in rows]

    async def repository_readback(
        self, *, actor_id: str, workspace_id: str, note_id: str
    ) -> dict[str, Any]:
        note = self.get_note(actor_id=actor_id, workspace_id=workspace_id, note_id=note_id)
        repository = note["repository"]
        content = await self.github.read_repository_path(
            actor_id=actor_id,
            workspace_id=workspace_id,
            repository_id=int(repository["repository_id"]),
            path=str(repository["path"]),
        )
        return {
            "note_id": note_id,
            "verified": (
                content.get("kind") == "file"
                and content.get("sha") == repository["sha"]
                and content.get("text") == self._stored_markdown(note_id)
            ),
            "repository": {
                "repository_id": int(repository["repository_id"]),
                "full_name": content.get("full_name"),
                "path": content.get("path"),
                "sha": content.get("sha"),
            },
        }

    def _stored_markdown(self, note_id: str) -> str:
        with self.store._lock:
            row = self.store.db.execute(
                "SELECT markdown FROM project_notes WHERE id=?", (note_id,)
            ).fetchone()
            if not row:
                raise StoreError("NOTE_NOT_FOUND", "Project note is not available")
            return str(row["markdown"])

    def list_replies(
        self, *, actor_id: str, workspace_id: str, note_id: str
    ) -> list[dict[str, Any]]:
        note = self.get_note(actor_id=actor_id, workspace_id=workspace_id, note_id=note_id)
        with self.store._lock:
            rows = self.store.db.execute(
                """SELECT * FROM project_discussion_entries
                   WHERE note_id=? ORDER BY created_at_ms,id""",
                (note_id,),
            ).fetchall()
        return [self._public_reply(actor_id, workspace_id, row) for row in rows]

    def reply(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        note_id: str,
        command_id: str,
        body: str,
    ) -> dict[str, Any]:
        note = self.get_note(actor_id=actor_id, workspace_id=workspace_id, note_id=note_id)
        self.store.project_access(
            actor_id, workspace_id, note["project_id"], require_role="editor"
        )
        command_id = self._clean_command(command_id)
        clean_body = str(body or "").strip()
        if not clean_body or len(clean_body) > MAX_REPLY:
            raise StoreError("INVALID_ARGUMENT", "reply body is invalid")
        request_sha = _sha({"body": clean_body})
        reply_id = "reply_" + hashlib.sha256(
            f"{actor_id}\n{note_id}\n{command_id}".encode("utf-8")
        ).hexdigest()[:32]
        now = _now_ms()
        with self.store._lock:
            existing = self.store.db.execute(
                """SELECT * FROM project_discussion_entries
                   WHERE author_actor_id=? AND note_id=? AND command_id=?""",
                (actor_id, note_id, command_id),
            ).fetchone()
            if existing:
                if existing["request_sha256"] != request_sha:
                    raise StoreError(
                        "COLLABORATION_COMMAND_CONFLICT",
                        "command_id was already used with another reply payload",
                    )
                return self._public_reply(actor_id, workspace_id, existing)
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                self.store.db.execute(
                    """INSERT INTO project_discussion_entries(
                           id,workspace_id,project_id,note_id,author_actor_id,body,
                           command_id,request_sha256,created_at_ms)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        reply_id, workspace_id, note["project_id"], note_id, actor_id,
                        clean_body, command_id, request_sha, now,
                    ),
                )
                self.store.db.execute(
                    """INSERT INTO collaboration_events(
                           workspace_id,project_id,actor_id,kind,object_kind,object_id,
                           parent_object_id,addressed_to_actor_id,summary,created_at_ms)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        workspace_id, note["project_id"], actor_id, "note_replied", "reply",
                        reply_id, note_id, note["author"]["id"], clean_body[:240], now,
                    ),
                )
                row = self.store.db.execute(
                    "SELECT * FROM project_discussion_entries WHERE id=?", (reply_id,)
                ).fetchone()
                self.store.db.execute("COMMIT")
            except Exception:
                self.store.db.execute("ROLLBACK")
                raise
        return self._public_reply(actor_id, workspace_id, row)

    def timeline(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        after_id: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.store._membership(actor_id, workspace_id)
        bounded = max(1, min(int(limit), 100))
        with self.store._lock:
            rows = self.store.db.execute(
                """SELECT e.*,p.name AS project_name
                   FROM collaboration_events e
                   JOIN projects p ON p.id=e.project_id
                   JOIN project_grants g ON g.project_id=e.project_id AND g.actor_id=?
                   WHERE e.workspace_id=? AND e.id>? AND g.revoked_at_ms IS NULL
                   ORDER BY e.id ASC LIMIT ?""",
                (actor_id, workspace_id, max(0, int(after_id)), bounded),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "project_id": row["project_id"],
                "project_name": row["project_name"],
                "actor_id": row["actor_id"],
                "kind": row["kind"],
                "object_kind": row["object_kind"],
                "object_id": row["object_id"],
                "parent_object_id": row["parent_object_id"],
                "addressed_to_actor_id": row["addressed_to_actor_id"],
                "summary": row["summary"],
                "created_at_ms": int(row["created_at_ms"]),
            }
            for row in rows
        ]

    def _attention_state(self, actor_id: str, workspace_id: str) -> dict[str, Any]:
        self.store._membership(actor_id, workspace_id)
        with self.store._lock:
            row = self.store.db.execute(
                """SELECT personal_cursor,general_cursor,general_news_enabled,updated_at_ms
                   FROM collaboration_attention_state
                   WHERE actor_id=? AND workspace_id=?""",
                (actor_id, workspace_id),
            ).fetchone()
            if row:
                return {
                    "personal_cursor": int(row["personal_cursor"]),
                    "general_cursor": int(row["general_cursor"]),
                    "general_news_enabled": bool(row["general_news_enabled"]),
                    "updated_at_ms": int(row["updated_at_ms"]),
                }
            now = _now_ms()
            self.store.db.execute(
                """INSERT INTO collaboration_attention_state(
                       actor_id,workspace_id,personal_cursor,general_cursor,
                       general_news_enabled,updated_at_ms)
                   VALUES(?,?,0,0,1,?)""",
                (actor_id, workspace_id, now),
            )
            return {
                "personal_cursor": 0,
                "general_cursor": 0,
                "general_news_enabled": True,
                "updated_at_ms": now,
            }

    def personal_brief(
        self, *, actor_id: str, workspace_id: str, limit: int = 12
    ) -> dict[str, Any]:
        state = self._attention_state(actor_id, workspace_id)
        with self.store._lock:
            personal_rows = self.store.db.execute(
                """SELECT e.*,p.name AS project_name
                   FROM collaboration_events e
                   JOIN projects p ON p.id=e.project_id
                   JOIN project_grants g ON g.project_id=e.project_id AND g.actor_id=?
                   WHERE e.workspace_id=? AND e.id>?
                     AND e.addressed_to_actor_id=?
                     AND g.revoked_at_ms IS NULL
                   ORDER BY e.id ASC LIMIT ?""",
                (
                    actor_id,
                    workspace_id,
                    state["personal_cursor"],
                    actor_id,
                    max(1, min(int(limit), 50)),
                ),
            ).fetchall()
            general_rows = self.store.db.execute(
                """SELECT e.*,p.name AS project_name
                   FROM collaboration_events e
                   JOIN projects p ON p.id=e.project_id
                   JOIN project_grants g ON g.project_id=e.project_id AND g.actor_id=?
                   WHERE e.workspace_id=? AND e.id>?
                     AND e.addressed_to_actor_id IS NULL
                     AND e.actor_id!=?
                     AND g.revoked_at_ms IS NULL
                   ORDER BY e.id ASC LIMIT ?""",
                (
                    actor_id,
                    workspace_id,
                    state["general_cursor"],
                    actor_id,
                    max(1, min(int(limit), 50)),
                ),
            ).fetchall()

        def public(row: Any) -> dict[str, Any]:
            return {
                "id": int(row["id"]),
                "project_id": row["project_id"],
                "project_name": row["project_name"],
                "actor_id": row["actor_id"],
                "kind": row["kind"],
                "object_kind": row["object_kind"],
                "object_id": row["object_id"],
                "parent_object_id": row["parent_object_id"],
                "addressed_to_actor_id": row["addressed_to_actor_id"],
                "summary": row["summary"],
                "created_at_ms": int(row["created_at_ms"]),
            }

        personal = [public(row) for row in personal_rows]
        general = [public(row) for row in general_rows]
        enabled = bool(state["general_news_enabled"])
        return {
            "personal": personal,
            "personal_unread_count": len(personal),
            "personal_through_id": personal[-1]["id"] if personal else state["personal_cursor"],
            "general_available": enabled and bool(general),
            "general_count": len(general) if enabled else 0,
            "general_preview": general[:3] if enabled else [],
            "general_through_id": general[-1]["id"] if general else state["general_cursor"],
            "general_news_enabled": enabled,
        }

    def mark_brief_seen(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        personal_through_id: int | None = None,
        general_through_id: int | None = None,
    ) -> dict[str, Any]:
        state = self._attention_state(actor_id, workspace_id)
        personal = max(state["personal_cursor"], int(personal_through_id or 0))
        general = max(state["general_cursor"], int(general_through_id or 0))
        now = _now_ms()
        with self.store._lock:
            self.store.db.execute(
                """UPDATE collaboration_attention_state
                   SET personal_cursor=?,general_cursor=?,updated_at_ms=?
                   WHERE actor_id=? AND workspace_id=?""",
                (personal, general, now, actor_id, workspace_id),
            )
        return {
            "personal_cursor": personal,
            "general_cursor": general,
            "general_news_enabled": state["general_news_enabled"],
        }

    def set_general_news(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        state = self._attention_state(actor_id, workspace_id)
        now = _now_ms()
        with self.store._lock:
            self.store.db.execute(
                """UPDATE collaboration_attention_state
                   SET general_news_enabled=?,updated_at_ms=?
                   WHERE actor_id=? AND workspace_id=?""",
                (int(bool(enabled)), now, actor_id, workspace_id),
            )
        return {
            "personal_cursor": state["personal_cursor"],
            "general_cursor": state["general_cursor"],
            "general_news_enabled": bool(enabled),
        }

    def list_participants(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        project_id: str,
    ) -> list[dict[str, Any]]:
        self.store.project_access(actor_id, workspace_id, project_id)
        with self.store._lock:
            rows = self.store.db.execute(
                """SELECT a.id,a.display_name,g.role,g.can_analyze,g.can_manage_share
                   FROM project_grants g
                   JOIN actors a ON a.id=g.actor_id
                   JOIN projects p ON p.id=g.project_id
                   WHERE g.project_id=? AND p.workspace_id=?
                     AND g.revoked_at_ms IS NULL
                   ORDER BY CASE g.role WHEN 'owner' THEN 0 WHEN 'editor' THEN 1 ELSE 2 END,
                            a.display_name,a.id""",
                (project_id, workspace_id),
            ).fetchall()
        return [
            {
                "actor_id": row["id"],
                "display_name": row["display_name"],
                "role": row["role"],
                "can_analyze": bool(row["can_analyze"]),
                "can_manage_share": bool(row["can_manage_share"]),
            }
            for row in rows
        ]

    def invite_participant(
        self,
        *,
        owner_actor_id: str,
        workspace_id: str,
        project_id: str,
        display_name: str,
        role: str = "editor",
        ttl_seconds: int = 24 * 60 * 60,
    ) -> dict[str, Any]:
        if role not in {"viewer", "editor"}:
            raise StoreError("INVALID_ARGUMENT", "participant role is invalid")
        self.store.require_workspace_owner(owner_actor_id, workspace_id)
        self.store.project_access(
            owner_actor_id, workspace_id, project_id, require_role="owner"
        )
        name = str(display_name or "").strip()[:80]
        if not name:
            raise StoreError("INVALID_ARGUMENT", "display_name is required")
        ttl_seconds = max(60, min(int(ttl_seconds), 7 * 24 * 60 * 60))
        now = _now_ms()
        actor_id = "usr_" + secrets.token_hex(16)
        token = secrets.token_urlsafe(32)
        token_sha256 = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.store._lock:
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                self.store.db.execute(
                    "INSERT INTO actors(id,display_name,created_at_ms) VALUES(?,?,?)",
                    (actor_id, name, now),
                )
                self.store.db.execute(
                    "INSERT INTO memberships(actor_id,workspace_id,role) VALUES(?,?,?)",
                    (actor_id, workspace_id, "member"),
                )
                self.store.db.execute(
                    """INSERT INTO project_grants(
                           actor_id,project_id,role,can_analyze,can_manage_share,
                           created_at_ms,revoked_at_ms)
                       VALUES(?,?,?,?,?,?,NULL)""",
                    (actor_id, project_id, role, 0, 0, now),
                )
                self.store.db.execute(
                    """INSERT INTO login_invites(
                           token_sha256,actor_id,expires_at_ms,used_at_ms,created_at_ms)
                       VALUES(?,?,?,?,?)""",
                    (token_sha256, actor_id, now + ttl_seconds * 1000, None, now),
                )
                self.store.db.execute("COMMIT")
            except Exception:
                self.store.db.execute("ROLLBACK")
                raise
        return {
            "actor_id": actor_id,
            "display_name": name,
            "project_id": project_id,
            "role": role,
            "invite_token": token,
            "expires_at_ms": now + ttl_seconds * 1000,
        }
