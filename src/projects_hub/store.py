from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from typing import Any


class StoreError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _now_ms() -> int:
    return round(time.time() * 1000)


class DurableStore:
    """Single-host durable substrate for the first DevCoveer vertical.

    SQLite/WAL is intentionally behind this store boundary. Product contracts use
    IDs/revisions/commands rather than SQLite details so the production multi-instance
    store can move to PostgreSQL without changing Live tools or clients.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.audio_dir = self.data_dir / "audio"
        self.memory_dir = self.data_dir / "memory"
        self.source_dir = self.data_dir / "sources"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "projects-hub.sqlite3"
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def _init_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS actors(
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS external_identities(
            provider TEXT NOT NULL,
            subject TEXT NOT NULL,
            actor_id TEXT NOT NULL REFERENCES actors(id),
            email TEXT,
            display_name TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL,
            PRIMARY KEY(provider, subject)
        );
        CREATE INDEX IF NOT EXISTS external_identities_actor_idx
            ON external_identities(actor_id);
        CREATE TABLE IF NOT EXISTS workspaces(
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memberships(
            actor_id TEXT NOT NULL REFERENCES actors(id),
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            role TEXT NOT NULL,
            PRIMARY KEY(actor_id, workspace_id)
        );
        CREATE TABLE IF NOT EXISTS projects(
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at_ms INTEGER NOT NULL,
            UNIQUE(workspace_id, name)
        );
        CREATE TABLE IF NOT EXISTS conversations(
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            actor_id TEXT NOT NULL REFERENCES actors(id),
            focus_project_id TEXT REFERENCES projects(id),
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sources(
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id),
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            actor_id TEXT NOT NULL REFERENCES actors(id),
            client_source_id TEXT,
            status TEXT NOT NULL,
            audio_path TEXT NOT NULL,
            audio_bytes INTEGER NOT NULL DEFAULT 0,
            audio_chunks INTEGER NOT NULL DEFAULT 0,
            transcript TEXT NOT NULL DEFAULT '',
            transcript_revision INTEGER NOT NULL DEFAULT 0,
            captured_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL REFERENCES sources(id),
            kind TEXT NOT NULL,
            text TEXT,
            provider_at_ms INTEGER,
            payload_sha256 TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS source_events_source_idx
            ON source_events(source_id, id);
        CREATE TABLE IF NOT EXISTS commands(
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES sources(id),
            tool_name TEXT NOT NULL,
            args_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memories(
            id TEXT PRIMARY KEY,
            memory_key TEXT NOT NULL UNIQUE,
            source_id TEXT NOT NULL REFERENCES sources(id),
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            project_id TEXT REFERENCES projects(id),
            title TEXT NOT NULL,
            kind TEXT NOT NULL,
            semantic_notes TEXT NOT NULL,
            transcript_revision INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            markdown_path TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );
        """
        with self._lock:
            self.db.executescript(schema)
            columns = {
                row["name"]
                for row in self.db.execute("PRAGMA table_info(sources)").fetchall()
            }
            if "client_source_id" not in columns:
                self.db.execute("ALTER TABLE sources ADD COLUMN client_source_id TEXT")
            self.db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS sources_actor_client_source_idx
                   ON sources(actor_id, client_source_id)
                   WHERE client_source_id IS NOT NULL"""
            )
            self._migrate_memories_schema()
            self.db.execute(
                """CREATE INDEX IF NOT EXISTS memories_project_idx
                   ON memories(workspace_id, project_id, updated_at_ms DESC)"""
            )

    @staticmethod
    def _memory_key(
        source_id: str,
        project_id: str | None,
        title: str,
        kind: str,
    ) -> str:
        payload = json.dumps(
            [source_id, project_id or "", title, kind],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _write_verified(self, relative: str, body: str) -> str:
        path = self.data_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = body.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
        with tmp.open("wb", buffering=0) as handle:
            handle.write(encoded)
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        with path.open("rb") as handle:
            readback = handle.read()
        if hashlib.sha256(readback).hexdigest() != digest:
            raise StoreError("MEMORY_READBACK_FAILED", "Durable Markdown readback digest mismatch")
        return digest

    def _write_source_archive(self, source: dict[str, Any]) -> tuple[str, str]:
        relative = f"sources/{source['workspace_id']}/{source['id']}.md"
        body = (
            "---\n"
            f"source_id: {source['id']}\n"
            f"conversation_id: {source['conversation_id']}\n"
            f"workspace_id: {source['workspace_id']}\n"
            f"transcript_revision: {source['transcript_revision']}\n"
            f"captured_at_ms: {source['captured_at_ms']}\n"
            "---\n\n"
            "# Voice source\n\n"
            "## Provider input transcription\n\n"
            f"{source['transcript']}\n"
        )
        return relative, self._write_verified(relative, body)

    def _memory_document(
        self,
        *,
        memory_id: str,
        source: dict[str, Any],
        source_relative: str,
        source_sha256: str,
        project_id: str | None,
        project_name: str,
        title: str,
        kind: str,
        semantic_notes: str,
        revision: int,
    ) -> str:
        return (
            "---\n"
            f"memory_id: {memory_id}\n"
            f"source_id: {source['id']}\n"
            f"conversation_id: {source['conversation_id']}\n"
            f"project_id: {project_id or ''}\n"
            f"project: {json.dumps(project_name, ensure_ascii=False)}\n"
            f"kind: {kind}\n"
            f"transcript_revision: {source['transcript_revision']}\n"
            f"memory_revision: {revision}\n"
            f"captured_at_ms: {source['captured_at_ms']}\n"
            f"private_source_ref: {json.dumps(source_relative, ensure_ascii=False)}\n"
            f"private_source_sha256: {source_sha256}\n"
            "---\n\n"
            f"# {title}\n\n"
            "## Agent-confirmed memory\n\n"
            f"{semantic_notes}\n\n"
            "_The full provider transcript remains in the actor-private source archive._\n"
        )

    def _migrate_memories_schema(self) -> None:
        columns = {
            row["name"]
            for row in self.db.execute("PRAGMA table_info(memories)").fetchall()
        }
        if "memory_key" in columns:
            return
        legacy = [dict(row) for row in self.db.execute("SELECT * FROM memories").fetchall()]
        migrated: list[dict[str, Any]] = []
        for row in legacy:
            source_row = self.db.execute(
                "SELECT * FROM sources WHERE id=?",
                (row["source_id"],),
            ).fetchone()
            if not source_row:
                raise StoreError("MEMORY_MIGRATION_FAILED", "Legacy memory source is missing")
            source = dict(source_row)
            project = self._project_row(source["workspace_id"], row["project_id"])
            project_name = project["name"] if project else "Личное"
            source_relative, source_sha = self._write_source_archive(source)
            memory_key = self._memory_key(
                source["id"],
                row["project_id"],
                row["title"],
                row["kind"],
            )
            memory_relative = f"memory/{source['workspace_id']}/{row['id']}.md"
            body = self._memory_document(
                memory_id=row["id"],
                source=source,
                source_relative=source_relative,
                source_sha256=source_sha,
                project_id=row["project_id"],
                project_name=project_name,
                title=row["title"],
                kind=row["kind"],
                semantic_notes=row["semantic_notes"] or "—",
                revision=row["revision"],
            )
            digest = self._write_verified(memory_relative, body)
            old_path = self.data_dir / row["markdown_path"]
            if row["markdown_path"] != memory_relative and old_path.is_file():
                old_path.unlink()
            migrated.append(
                {
                    **row,
                    "memory_key": memory_key,
                    "markdown_path": memory_relative,
                    "content_sha256": digest,
                }
            )

        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("DROP INDEX IF EXISTS memories_project_idx")
            self.db.execute("ALTER TABLE memories RENAME TO memories_legacy")
            self.db.execute(
                """CREATE TABLE memories(
                    id TEXT PRIMARY KEY,
                    memory_key TEXT NOT NULL UNIQUE,
                    source_id TEXT NOT NULL REFERENCES sources(id),
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    project_id TEXT REFERENCES projects(id),
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    semantic_notes TEXT NOT NULL,
                    transcript_revision INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    markdown_path TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )"""
            )
            for row in migrated:
                self.db.execute(
                    """INSERT INTO memories
                       (id,memory_key,source_id,workspace_id,project_id,title,kind,
                        semantic_notes,transcript_revision,revision,markdown_path,
                        content_sha256,created_at_ms,updated_at_ms)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        row["id"],
                        row["memory_key"],
                        row["source_id"],
                        row["workspace_id"],
                        row["project_id"],
                        row["title"],
                        row["kind"],
                        row["semantic_notes"],
                        row["transcript_revision"],
                        row["revision"],
                        row["markdown_path"],
                        row["content_sha256"],
                        row["created_at_ms"],
                        row["updated_at_ms"],
                    ),
                )
            self.db.execute("DROP TABLE memories_legacy")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def ping(self) -> bool:
        with self._lock:
            return self.db.execute("SELECT 1").fetchone()[0] == 1

    def ensure_dev_workspace(self, display_name: str = "Pilot user") -> dict[str, Any]:
        name = (display_name or "Pilot user").strip()[:80]
        now = _now_ms()
        with self._lock:
            row = self.db.execute(
                "SELECT id FROM actors WHERE display_name=? ORDER BY created_at_ms LIMIT 1", (name,)
            ).fetchone()
            if row:
                actor_id = row["id"]
                membership = self.db.execute(
                    "SELECT workspace_id FROM memberships WHERE actor_id=? ORDER BY rowid LIMIT 1",
                    (actor_id,),
                ).fetchone()
                if membership:
                    return self.bootstrap(actor_id, membership["workspace_id"])
            actor_id = _id("usr")
            workspace_id = _id("ws")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    "INSERT INTO actors(id,display_name,created_at_ms) VALUES(?,?,?)",
                    (actor_id, name, now),
                )
                self.db.execute(
                    "INSERT INTO workspaces(id,name,created_at_ms) VALUES(?,?,?)",
                    (workspace_id, "Личное пространство", now),
                )
                self.db.execute(
                    "INSERT INTO memberships(actor_id,workspace_id,role) VALUES(?,?,?)",
                    (actor_id, workspace_id, "owner"),
                )
                for project_name in ("Projects Hub", "Wonderful Lections", "KenigEvents"):
                    self.db.execute(
                        "INSERT INTO projects(id,workspace_id,name,status,created_at_ms) VALUES(?,?,?,?,?)",
                        (_id("prj"), workspace_id, project_name, "active", now),
                    )
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return self.bootstrap(actor_id, workspace_id)

    def ensure_external_workspace(
        self,
        *,
        provider: str,
        subject: str,
        display_name: str,
        email: str | None = None,
    ) -> dict[str, Any]:
        provider = str(provider or "").strip()
        subject = str(subject or "").strip()
        name = (display_name or "Пользователь").strip()[:80] or "Пользователь"
        normalized_email = (email or "").strip()[:320] or None
        if not provider or len(provider) > 100 or not subject or len(subject) > 200:
            raise StoreError("INVALID_IDENTITY", "External identity is invalid")
        now = _now_ms()
        with self._lock:
            row = self.db.execute(
                "SELECT actor_id FROM external_identities WHERE provider=? AND subject=?",
                (provider, subject),
            ).fetchone()
            if row:
                actor_id = row["actor_id"]
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    self.db.execute(
                        "UPDATE actors SET display_name=? WHERE id=?",
                        (name, actor_id),
                    )
                    self.db.execute(
                        """UPDATE external_identities
                           SET email=?,display_name=?,updated_at_ms=?
                           WHERE provider=? AND subject=?""",
                        (normalized_email, name, now, provider, subject),
                    )
                    self.db.execute("COMMIT")
                except Exception:
                    self.db.execute("ROLLBACK")
                    raise
                return self.bootstrap(actor_id)

            actor_id = _id("usr")
            workspace_id = _id("ws")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    "INSERT INTO actors(id,display_name,created_at_ms) VALUES(?,?,?)",
                    (actor_id, name, now),
                )
                self.db.execute(
                    """INSERT INTO external_identities
                       (provider,subject,actor_id,email,display_name,created_at_ms,updated_at_ms)
                       VALUES(?,?,?,?,?,?,?)""",
                    (provider, subject, actor_id, normalized_email, name, now, now),
                )
                self.db.execute(
                    "INSERT INTO workspaces(id,name,created_at_ms) VALUES(?,?,?)",
                    (workspace_id, "Личное пространство", now),
                )
                self.db.execute(
                    "INSERT INTO memberships(actor_id,workspace_id,role) VALUES(?,?,?)",
                    (actor_id, workspace_id, "owner"),
                )
                for project_name in ("Projects Hub", "Wonderful Lections", "KenigEvents"):
                    self.db.execute(
                        "INSERT INTO projects(id,workspace_id,name,status,created_at_ms) VALUES(?,?,?,?,?)",
                        (_id("prj"), workspace_id, project_name, "active", now),
                    )
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return self.bootstrap(actor_id, workspace_id)

    def _membership(self, actor_id: str, workspace_id: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT role FROM memberships WHERE actor_id=? AND workspace_id=?",
            (actor_id, workspace_id),
        ).fetchone()
        if not row:
            raise StoreError("FORBIDDEN", "Workspace is not available to this actor")
        return row

    def bootstrap(self, actor_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            actor = self.db.execute(
                "SELECT id,display_name FROM actors WHERE id=?", (actor_id,)
            ).fetchone()
            if not actor:
                raise StoreError("UNAUTHENTICATED", "Unknown actor")
            if workspace_id is None:
                row = self.db.execute(
                    "SELECT workspace_id FROM memberships WHERE actor_id=? ORDER BY rowid LIMIT 1",
                    (actor_id,),
                ).fetchone()
                if not row:
                    raise StoreError("FORBIDDEN", "Actor has no workspace")
                workspace_id = row["workspace_id"]
            membership = self._membership(actor_id, workspace_id)
            workspace = self.db.execute(
                "SELECT id,name FROM workspaces WHERE id=?", (workspace_id,)
            ).fetchone()
            projects = [
                dict(row)
                for row in self.db.execute(
                    "SELECT id,name,status FROM projects WHERE workspace_id=? ORDER BY created_at_ms,id",
                    (workspace_id,),
                ).fetchall()
            ]
            return {
                "actor": dict(actor),
                "workspace": dict(workspace),
                "role": membership["role"],
                "projects": projects,
            }

    def create_conversation(
        self, actor_id: str, workspace_id: str, focus_project_id: str | None = None
    ) -> dict[str, Any]:
        now = _now_ms()
        with self._lock:
            self._membership(actor_id, workspace_id)
            if focus_project_id is not None and not self._project_row(workspace_id, focus_project_id):
                raise StoreError("PROJECT_NOT_FOUND", "Project is not available")
            conversation_id = _id("conv")
            self.db.execute(
                """INSERT INTO conversations
                   (id,workspace_id,actor_id,focus_project_id,created_at_ms,updated_at_ms)
                   VALUES(?,?,?,?,?,?)""",
                (conversation_id, workspace_id, actor_id, focus_project_id, now, now),
            )
            return self.get_conversation(actor_id, conversation_id)

    def get_conversation(self, actor_id: str, conversation_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute(
                """SELECT c.id,c.workspace_id,c.actor_id,c.focus_project_id,c.created_at_ms,c.updated_at_ms,
                          p.name AS focus_project_name
                   FROM conversations c
                   LEFT JOIN projects p ON p.id=c.focus_project_id
                   WHERE c.id=?""",
                (conversation_id,),
            ).fetchone()
            if not row or row["actor_id"] != actor_id:
                raise StoreError("CONVERSATION_NOT_FOUND", "Conversation is not available")
            self._membership(actor_id, row["workspace_id"])
            return dict(row)

    def _project_row(self, workspace_id: str, project_id: str | None) -> sqlite3.Row | None:
        if not project_id:
            return None
        return self.db.execute(
            "SELECT id,name,status FROM projects WHERE id=? AND workspace_id=?",
            (project_id, workspace_id),
        ).fetchone()

    def list_projects(self, actor_id: str, workspace_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._membership(actor_id, workspace_id)
            return [
                dict(row)
                for row in self.db.execute(
                    "SELECT id,name,status FROM projects WHERE workspace_id=? ORDER BY created_at_ms,id",
                    (workspace_id,),
                ).fetchall()
            ]

    def set_focus(self, actor_id: str, conversation_id: str, project_id: str) -> dict[str, Any]:
        now = _now_ms()
        with self._lock:
            conversation = self.get_conversation(actor_id, conversation_id)
            project = self._project_row(conversation["workspace_id"], project_id)
            if not project or project["status"] != "active":
                raise StoreError("PROJECT_NOT_FOUND", "Project is not available")
            self.db.execute(
                "UPDATE conversations SET focus_project_id=?,updated_at_ms=? WHERE id=?",
                (project_id, now, conversation_id),
            )
            return {"project_id": project_id, "project_name": project["name"], "revision": now}

    def create_source(
        self,
        actor_id: str,
        conversation_id: str,
        client_source_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now_ms()
        with self._lock:
            conversation = self.get_conversation(actor_id, conversation_id)
            if client_source_id:
                existing = self.db.execute(
                    """SELECT id,conversation_id FROM sources
                       WHERE actor_id=? AND client_source_id=?""",
                    (actor_id, client_source_id),
                ).fetchone()
                if existing:
                    if existing["conversation_id"] != conversation_id:
                        raise StoreError(
                            "CLIENT_SOURCE_CONFLICT",
                            "Local source is already bound to another conversation",
                        )
                    result = self.get_source(actor_id, existing["id"])
                    result["_reused"] = True
                    return result
            source_id = _id("src")
            relative = f"audio/{source_id}.pcm"
            self.db.execute(
                """INSERT INTO sources
                   (id,conversation_id,workspace_id,actor_id,client_source_id,status,
                    audio_path,captured_at_ms,updated_at_ms)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    source_id,
                    conversation_id,
                    conversation["workspace_id"],
                    actor_id,
                    client_source_id,
                    "capturing",
                    relative,
                    now,
                    now,
                ),
            )
            result = self.get_source(actor_id, source_id)
            result["_reused"] = False
            return result

    def get_source(self, actor_id: str, source_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if not row or row["actor_id"] != actor_id:
                raise StoreError("SOURCE_NOT_FOUND", "Source is not available")
            self._membership(actor_id, row["workspace_id"])
            return dict(row)

    def get_source_by_client(
        self,
        actor_id: str,
        conversation_id: str,
        client_source_id: str,
    ) -> dict[str, Any]:
        with self._lock:
            conversation = self.get_conversation(actor_id, conversation_id)
            row = self.db.execute(
                """SELECT * FROM sources
                   WHERE actor_id=? AND conversation_id=? AND client_source_id=?""",
                (actor_id, conversation_id, client_source_id),
            ).fetchone()
            if not row:
                raise StoreError("SOURCE_NOT_FOUND", "Local source is not available")
            if row["workspace_id"] != conversation["workspace_id"]:
                raise StoreError("FORBIDDEN", "Source workspace mismatch")
            return dict(row)

    def reset_source_for_replay(self, actor_id: str, source_id: str) -> dict[str, Any]:
        terminal = {"archived", "ephemeral_processed"}
        with self._lock:
            source = self.get_source(actor_id, source_id)
            if not source.get("client_source_id"):
                raise StoreError(
                    "INVALID_ARGUMENT",
                    "Only client-owned buffered sources may be reset for replay",
                )
            if source["status"] in terminal:
                return source
            path = self.data_dir / source["audio_path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb", buffering=0) as handle:
                handle.flush()
                os.fsync(handle.fileno())
            now = _now_ms()
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("DELETE FROM source_events WHERE source_id=?", (source_id,))
                self.db.execute(
                    """UPDATE sources
                       SET status='capturing',audio_bytes=0,audio_chunks=0,
                           transcript='',transcript_revision=0,updated_at_ms=?
                       WHERE id=?""",
                    (now, source_id),
                )
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return self.get_source(actor_id, source_id)

    def append_audio(self, actor_id: str, source_id: str, pcm: bytes) -> dict[str, int]:
        if not pcm:
            return {"audio_bytes": 0, "audio_chunks": 0}
        with self._lock:
            source = self.get_source(actor_id, source_id)
            path = self.data_dir / source["audio_path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab", buffering=0) as handle:
                handle.write(pcm)
                os.fsync(handle.fileno())
            now = _now_ms()
            self.db.execute(
                """UPDATE sources
                   SET audio_bytes=audio_bytes+?,audio_chunks=audio_chunks+1,updated_at_ms=?
                   WHERE id=?""",
                (len(pcm), now, source_id),
            )
            row = self.db.execute(
                "SELECT audio_bytes,audio_chunks FROM sources WHERE id=?", (source_id,)
            ).fetchone()
            return {"audio_bytes": row["audio_bytes"], "audio_chunks": row["audio_chunks"]}

    def append_source_event(
        self,
        actor_id: str,
        source_id: str,
        kind: str,
        text: str | None = None,
        provider_at_ms: int | None = None,
    ) -> int:
        now = _now_ms()
        exact = text if isinstance(text, str) else None
        digest = hashlib.sha256(
            json.dumps(
                [kind, exact, provider_at_ms],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self._lock:
            self.get_source(actor_id, source_id)
            self.db.execute(
                """INSERT INTO source_events
                   (source_id,kind,text,provider_at_ms,payload_sha256,created_at_ms)
                   VALUES(?,?,?,?,?,?)""",
                (source_id, kind, exact, provider_at_ms, digest, now),
            )
            if kind == "input_transcript" and exact is not None:
                self.db.execute(
                    """UPDATE sources
                       SET transcript=transcript||?,transcript_revision=transcript_revision+1,
                           status='agent_disposition_pending',updated_at_ms=?
                       WHERE id=?""",
                    (exact, now, source_id),
                )
            elif kind == "turn_complete":
                self.db.execute(
                    """UPDATE sources
                       SET status=CASE WHEN status='capturing' THEN 'live_turn_complete' ELSE status END,
                           updated_at_ms=? WHERE id=?""",
                    (now, source_id),
                )
            return self.db.execute(
                "SELECT transcript_revision FROM sources WHERE id=?", (source_id,)
            ).fetchone()[0]

    def mark_source_stopped(self, actor_id: str, source_id: str) -> None:
        now = _now_ms()
        with self._lock:
            source = self.get_source(actor_id, source_id)
            if source["status"] not in {"archived", "ephemeral_processed"}:
                self.db.execute(
                    """UPDATE sources
                       SET status=CASE
                           WHEN transcript_revision>0 THEN 'agent_disposition_pending'
                           ELSE 'local_durable'
                       END, updated_at_ms=? WHERE id=?""",
                    (now, source_id),
                )

    def finish_ephemeral(self, actor_id: str, source_id: str) -> dict[str, Any]:
        now = _now_ms()
        with self._lock:
            self.get_source(actor_id, source_id)
            memory_count = self.db.execute(
                "SELECT COUNT(*) FROM memories WHERE source_id=?",
                (source_id,),
            ).fetchone()[0]
            if memory_count:
                raise StoreError(
                    "SOURCE_ALREADY_ARCHIVED",
                    "Source already produced durable memory and cannot become ephemeral",
                )
            self.db.execute(
                "UPDATE sources SET status='ephemeral_processed',updated_at_ms=? WHERE id=?",
                (now, source_id),
            )
            return {"source_id": source_id, "status": "ephemeral_processed", "revision": now}

    def _command_result(self, command_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT status,result_json FROM commands WHERE id=?", (command_id,)
        ).fetchone()
        if row and row["status"] == "verified" and row["result_json"]:
            return json.loads(row["result_json"])
        return None

    def commit_memory(
        self,
        *,
        actor_id: str,
        source_id: str,
        command_id: str,
        project_id: str | None,
        title: str,
        kind: str,
        semantic_notes: str,
        args_sha256: str,
    ) -> dict[str, Any]:
        with self._lock:
            existing_result = self._command_result(command_id)
            if existing_result is not None:
                return existing_result
            source = self.get_source(actor_id, source_id)
            if source["status"] == "ephemeral_processed":
                raise StoreError("SOURCE_ALREADY_DISPOSED", "Ephemeral source is already closed")
            if source["transcript_revision"] <= 0 or not source["transcript"]:
                raise StoreError("SOURCE_TRANSCRIPT_PENDING", "Provider transcript is not durable yet")
            if project_id is not None:
                project = self._project_row(source["workspace_id"], project_id)
                if not project or project["status"] != "active":
                    raise StoreError("PROJECT_NOT_FOUND", "Project is not available")
                project_name = project["name"]
            else:
                project_name = "Личное"

            clean_title = (title or "Голосовая запись").strip()[:160]
            clean_kind = (kind or "note").strip()[:64]
            notes = (semantic_notes or "").strip()[:4000]
            if not notes:
                raise StoreError(
                    "INVALID_ARGUMENT",
                    "semantic_notes is required for durable memory",
                )

            memory_key = self._memory_key(source_id, project_id, clean_title, clean_kind)
            old = self.db.execute(
                """SELECT id,revision,transcript_revision,created_at_ms
                   FROM memories WHERE memory_key=?""",
                (memory_key,),
            ).fetchone()
            revision = (old["revision"] + 1) if old else 1
            memory_id = old["id"] if old else _id("mem")
            created = old["created_at_ms"] if old else _now_ms()

            source_relative, source_sha = self._write_source_archive(source)
            markdown_rel = f"memory/{source['workspace_id']}/{memory_id}.md"
            body = self._memory_document(
                memory_id=memory_id,
                source=source,
                source_relative=source_relative,
                source_sha256=source_sha,
                project_id=project_id,
                project_name=project_name,
                title=clean_title,
                kind=clean_kind,
                semantic_notes=notes,
                revision=revision,
            )
            digest = self._write_verified(markdown_rel, body)
            now = _now_ms()
            result = {
                "memory_id": memory_id,
                "source_id": source_id,
                "project_id": project_id,
                "project_name": project_name,
                "title": clean_title,
                "kind": clean_kind,
                "transcript_revision": source["transcript_revision"],
                "revision": revision,
                "sha256": digest,
                "source_sha256": source_sha,
                "status": "archived",
            }
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    """INSERT INTO commands
                       (id,source_id,tool_name,args_sha256,status,result_json,created_at_ms,updated_at_ms)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        command_id,
                        source_id,
                        "memory_commit_voice_source",
                        args_sha256,
                        "verified",
                        json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                        now,
                        now,
                    ),
                )
                if old:
                    self.db.execute(
                        """UPDATE memories SET project_id=?,title=?,kind=?,semantic_notes=?,
                           transcript_revision=?,revision=?,markdown_path=?,content_sha256=?,updated_at_ms=?
                           WHERE memory_key=?""",
                        (
                            project_id,
                            clean_title,
                            clean_kind,
                            notes,
                            source["transcript_revision"],
                            revision,
                            markdown_rel,
                            digest,
                            now,
                            memory_key,
                        ),
                    )
                else:
                    self.db.execute(
                        """INSERT INTO memories
                           (id,memory_key,source_id,workspace_id,project_id,title,kind,
                            semantic_notes,transcript_revision,revision,markdown_path,
                            content_sha256,created_at_ms,updated_at_ms)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            memory_id,
                            memory_key,
                            source_id,
                            source["workspace_id"],
                            project_id,
                            clean_title,
                            clean_kind,
                            notes,
                            source["transcript_revision"],
                            revision,
                            markdown_rel,
                            digest,
                            created,
                            now,
                        ),
                    )
                self.db.execute(
                    "UPDATE sources SET status='archived',updated_at_ms=? WHERE id=?",
                    (now, source_id),
                )
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return result

    def list_memories(
        self, actor_id: str, workspace_id: str, project_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        with self._lock:
            self._membership(actor_id, workspace_id)
            params: list[Any] = [workspace_id]
            where = "workspace_id=?"
            if project_id:
                if not self._project_row(workspace_id, project_id):
                    raise StoreError("PROJECT_NOT_FOUND", "Project is not available")
                where += " AND project_id=?"
                params.append(project_id)
            params.append(max(1, min(int(limit), 50)))
            rows = self.db.execute(
                f"""SELECT id,source_id,project_id,title,kind,semantic_notes,
                           transcript_revision,revision,content_sha256,updated_at_ms
                    FROM memories WHERE {where}
                    ORDER BY updated_at_ms DESC LIMIT ?""",
                tuple(params),
            ).fetchall()
            return [dict(row) for row in rows]

    def source_events(self, actor_id: str, source_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self.get_source(actor_id, source_id)
            return [
                dict(row)
                for row in self.db.execute(
                    """SELECT kind,text,provider_at_ms,payload_sha256,created_at_ms
                       FROM source_events WHERE source_id=? ORDER BY id""",
                    (source_id,),
                ).fetchall()
            ]
