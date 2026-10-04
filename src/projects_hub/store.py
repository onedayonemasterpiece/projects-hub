from __future__ import annotations

import hashlib
import json
import os
import secrets
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
        CREATE TABLE IF NOT EXISTS platform_owner(
            slot INTEGER PRIMARY KEY CHECK(slot=1),
            actor_id TEXT NOT NULL UNIQUE REFERENCES actors(id),
            created_at_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS login_invites(
            token_sha256 TEXT PRIMARY KEY,
            actor_id TEXT NOT NULL REFERENCES actors(id),
            expires_at_ms INTEGER NOT NULL,
            used_at_ms INTEGER,
            created_at_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS login_invites_actor_idx
            ON login_invites(actor_id, expires_at_ms);
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
        CREATE TABLE IF NOT EXISTS github_install_states(
            state_hash TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            actor_id TEXT NOT NULL REFERENCES actors(id),
            conversation_id TEXT REFERENCES conversations(id),
            expires_at_ms INTEGER NOT NULL,
            consumed_at_ms INTEGER,
            created_at_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS github_install_states_actor_idx
            ON github_install_states(actor_id, expires_at_ms);
        CREATE TABLE IF NOT EXISTS github_app_manifest_states(
            state_hash TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            actor_id TEXT NOT NULL REFERENCES actors(id),
            conversation_id TEXT REFERENCES conversations(id),
            expires_at_ms INTEGER NOT NULL,
            consumed_at_ms INTEGER,
            created_at_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS github_app_manifest_states_actor_idx
            ON github_app_manifest_states(actor_id, expires_at_ms);
        CREATE TABLE IF NOT EXISTS github_installations(
            installation_id INTEGER PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            account_id INTEGER NOT NULL,
            account_login TEXT NOT NULL,
            account_type TEXT NOT NULL,
            html_url TEXT NOT NULL,
            repository_selection TEXT NOT NULL,
            permissions_json TEXT NOT NULL,
            state TEXT NOT NULL,
            suspended_at_ms INTEGER,
            last_verified_at_ms INTEGER NOT NULL,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS github_installations_workspace_idx
            ON github_installations(workspace_id, state, updated_at_ms DESC);
        CREATE TABLE IF NOT EXISTS repository_connections(
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            installation_id INTEGER NOT NULL REFERENCES github_installations(installation_id),
            repository_id INTEGER NOT NULL,
            full_name TEXT NOT NULL,
            default_branch TEXT NOT NULL,
            private INTEGER NOT NULL,
            project_id TEXT REFERENCES projects(id),
            role TEXT NOT NULL,
            access_mode TEXT NOT NULL,
            allowed_paths_json TEXT NOT NULL,
            state TEXT NOT NULL,
            last_verified_at_ms INTEGER NOT NULL,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL,
            UNIQUE(workspace_id, repository_id)
        );
        CREATE INDEX IF NOT EXISTS repository_connections_workspace_idx
            ON repository_connections(workspace_id, state, full_name);
        CREATE TABLE IF NOT EXISTS github_webhook_deliveries(
            delivery_id TEXT PRIMARY KEY,
            event_name TEXT NOT NULL,
            action TEXT NOT NULL,
            received_at_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS devices(
            id TEXT PRIMARY KEY,
            actor_id TEXT NOT NULL REFERENCES actors(id),
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            session_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            platform TEXT NOT NULL,
            capabilities_json TEXT NOT NULL,
            credential_hash TEXT NOT NULL,
            state TEXT NOT NULL,
            last_seen_at_ms INTEGER NOT NULL,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS devices_actor_workspace_idx
            ON devices(actor_id, workspace_id, state, updated_at_ms DESC);
        CREATE TABLE IF NOT EXISTS device_commands(
            id TEXT PRIMARY KEY,
            actor_id TEXT NOT NULL REFERENCES actors(id),
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            project_id TEXT REFERENCES projects(id),
            device_id TEXT NOT NULL REFERENCES devices(id),
            device_session_id TEXT NOT NULL,
            capability TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            claim_hash TEXT,
            claimed_at_ms INTEGER,
            result_json TEXT,
            result_sha256 TEXT,
            expires_at_ms INTEGER NOT NULL,
            finished_at_ms INTEGER,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS device_commands_pending_idx
            ON device_commands(device_id, device_session_id, status, created_at_ms);
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

    def ensure_platform_owner(self, display_name: str = "Владелец") -> dict[str, Any]:
        """Create or return the one explicitly designated platform owner."""
        name = (display_name or "Владелец").strip()[:80] or "Владелец"
        now = _now_ms()
        with self._lock:
            row = self.db.execute(
                "SELECT actor_id FROM platform_owner WHERE slot=1"
            ).fetchone()
            if row:
                return self.bootstrap(row["actor_id"])

            actor_id = _id("usr")
            workspace_id = _id("ws")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute(
                    "SELECT actor_id FROM platform_owner WHERE slot=1"
                ).fetchone()
                if row:
                    self.db.execute("COMMIT")
                    return self.bootstrap(row["actor_id"])
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
                self.db.execute(
                    "INSERT INTO platform_owner(slot,actor_id,created_at_ms) VALUES(1,?,?)",
                    (actor_id, now),
                )
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return self.bootstrap(actor_id, workspace_id)

    def issue_platform_owner_invite(
        self,
        display_name: str = "Владелец",
        *,
        ttl_seconds: int = 15 * 60,
    ) -> dict[str, Any]:
        ttl_seconds = int(ttl_seconds)
        if not 60 <= ttl_seconds <= 24 * 60 * 60:
            raise StoreError("INVALID_ARGUMENT", "Invite TTL is outside the supported bound")
        owner = self.ensure_platform_owner(display_name)
        token = secrets.token_urlsafe(32)
        token_sha256 = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = _now_ms()
        expires_at_ms = now + ttl_seconds * 1000
        with self._lock:
            self.db.execute(
                "DELETE FROM login_invites WHERE used_at_ms IS NOT NULL OR expires_at_ms<?",
                (now,),
            )
            self.db.execute(
                "DELETE FROM login_invites WHERE actor_id=? AND used_at_ms IS NULL",
                (owner["actor"]["id"],),
            )
            self.db.execute(
                """INSERT INTO login_invites
                   (token_sha256,actor_id,expires_at_ms,used_at_ms,created_at_ms)
                   VALUES(?,?,?,?,?)""",
                (token_sha256, owner["actor"]["id"], expires_at_ms, None, now),
            )
        return {
            "token": token,
            "expires_at_ms": expires_at_ms,
            "actor_id": owner["actor"]["id"],
            "workspace_id": owner["workspace"]["id"],
        }

    def consume_login_invite(self, token: str) -> dict[str, Any]:
        value = str(token or "").strip()
        if not 20 <= len(value) <= 200:
            raise StoreError("INVITE_INVALID", "Invite code is invalid or expired")
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        now = _now_ms()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute(
                    """SELECT actor_id,expires_at_ms,used_at_ms
                       FROM login_invites WHERE token_sha256=?""",
                    (digest,),
                ).fetchone()
                if (
                    row is None
                    or row["used_at_ms"] is not None
                    or int(row["expires_at_ms"]) < now
                ):
                    raise StoreError("INVITE_INVALID", "Invite code is invalid or expired")
                actor_id = str(row["actor_id"])
                self.db.execute(
                    "UPDATE login_invites SET used_at_ms=? WHERE token_sha256=? AND used_at_ms IS NULL",
                    (now, digest),
                )
                if self.db.execute("SELECT changes()").fetchone()[0] != 1:
                    raise StoreError("INVITE_INVALID", "Invite code is invalid or expired")
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return self.bootstrap(actor_id)

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

    def workspace_role(self, actor_id: str, workspace_id: str) -> str:
        with self._lock:
            return str(self._membership(actor_id, workspace_id)["role"])

    def require_workspace_owner(self, actor_id: str, workspace_id: str) -> None:
        role = self.workspace_role(actor_id, workspace_id)
        if role != "owner":
            raise StoreError("FORBIDDEN", "Workspace owner permission is required")

    def require_platform_owner(self, actor_id: str) -> None:
        with self._lock:
            row = self.db.execute(
                "SELECT actor_id FROM platform_owner WHERE slot=1"
            ).fetchone()
            if not row or row["actor_id"] != actor_id:
                raise StoreError("FORBIDDEN", "Platform owner permission is required")

    def create_github_app_manifest_state(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        state_hash: str,
        expires_at_ms: int,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        if len(state_hash) != 64:
            raise StoreError("INVALID_ARGUMENT", "GitHub App manifest state is invalid")
        now = _now_ms()
        if expires_at_ms <= now or expires_at_ms > now + 60 * 60 * 1000:
            raise StoreError("INVALID_ARGUMENT", "GitHub App manifest state expiry is invalid")
        with self._lock:
            self.require_platform_owner(actor_id)
            self.require_workspace_owner(actor_id, workspace_id)
            if conversation_id is not None:
                conversation = self.get_conversation(actor_id, conversation_id)
                if conversation["workspace_id"] != workspace_id:
                    raise StoreError("FORBIDDEN", "Conversation workspace mismatch")
            self.db.execute(
                """INSERT INTO github_app_manifest_states
                   (state_hash,workspace_id,actor_id,conversation_id,expires_at_ms,
                    consumed_at_ms,created_at_ms)
                   VALUES(?,?,?,?,?,NULL,?)""",
                (
                    state_hash,
                    workspace_id,
                    actor_id,
                    conversation_id,
                    int(expires_at_ms),
                    now,
                ),
            )
        return {
            "workspace_id": workspace_id,
            "conversation_id": conversation_id,
            "expires_at_ms": int(expires_at_ms),
        }

    def github_app_manifest_state(self, state_hash: str) -> dict[str, Any]:
        if len(state_hash) != 64:
            raise StoreError("GITHUB_APP_MANIFEST_STATE_INVALID", "GitHub App manifest state is invalid")
        now = _now_ms()
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM github_app_manifest_states WHERE state_hash=?",
                (state_hash,),
            ).fetchone()
            if (
                not row
                or row["consumed_at_ms"] is not None
                or int(row["expires_at_ms"]) < now
            ):
                raise StoreError(
                    "GITHUB_APP_MANIFEST_STATE_INVALID",
                    "GitHub App manifest state is invalid or expired",
                )
            return dict(row)

    def consume_github_app_manifest_state(
        self,
        *,
        actor_id: str,
        state_hash: str,
    ) -> dict[str, Any]:
        now = _now_ms()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute(
                    "SELECT * FROM github_app_manifest_states WHERE state_hash=?",
                    (state_hash,),
                ).fetchone()
                if (
                    not row
                    or row["actor_id"] != actor_id
                    or row["consumed_at_ms"] is not None
                    or int(row["expires_at_ms"]) < now
                ):
                    raise StoreError(
                        "GITHUB_APP_MANIFEST_STATE_INVALID",
                        "GitHub App manifest state is invalid or expired",
                    )
                self.require_platform_owner(actor_id)
                self.require_workspace_owner(actor_id, row["workspace_id"])
                self.db.execute(
                    "UPDATE github_app_manifest_states SET consumed_at_ms=? WHERE state_hash=?",
                    (now, state_hash),
                )
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return dict(row)

    def create_github_install_state(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        state_hash: str,
        expires_at_ms: int,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        if len(state_hash) != 64:
            raise StoreError("INVALID_ARGUMENT", "GitHub installation state is invalid")
        now = _now_ms()
        if expires_at_ms <= now or expires_at_ms > now + 20 * 60 * 1000:
            raise StoreError("INVALID_ARGUMENT", "GitHub installation state expiry is invalid")
        with self._lock:
            self.require_workspace_owner(actor_id, workspace_id)
            if conversation_id is not None:
                conversation = self.get_conversation(actor_id, conversation_id)
                if conversation["workspace_id"] != workspace_id:
                    raise StoreError("FORBIDDEN", "Conversation workspace mismatch")
            self.db.execute(
                """INSERT INTO github_install_states
                   (state_hash,workspace_id,actor_id,conversation_id,expires_at_ms,
                    consumed_at_ms,created_at_ms)
                   VALUES(?,?,?,?,?,NULL,?)""",
                (
                    state_hash,
                    workspace_id,
                    actor_id,
                    conversation_id,
                    int(expires_at_ms),
                    now,
                ),
            )
        return {
            "workspace_id": workspace_id,
            "conversation_id": conversation_id,
            "expires_at_ms": int(expires_at_ms),
        }

    def github_install_state(self, state_hash: str) -> dict[str, Any]:
        if len(state_hash) != 64:
            raise StoreError("GITHUB_INSTALL_STATE_INVALID", "GitHub installation state is invalid")
        now = _now_ms()
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM github_install_states WHERE state_hash=?",
                (state_hash,),
            ).fetchone()
            if (
                not row
                or row["consumed_at_ms"] is not None
                or int(row["expires_at_ms"]) < now
            ):
                raise StoreError(
                    "GITHUB_INSTALL_STATE_INVALID",
                    "GitHub installation state is invalid or expired",
                )
            return dict(row)

    def consume_github_install_state(
        self,
        *,
        actor_id: str,
        state_hash: str,
    ) -> dict[str, Any]:
        now = _now_ms()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute(
                    "SELECT * FROM github_install_states WHERE state_hash=?",
                    (state_hash,),
                ).fetchone()
                if (
                    not row
                    or row["actor_id"] != actor_id
                    or row["consumed_at_ms"] is not None
                    or int(row["expires_at_ms"]) < now
                ):
                    raise StoreError(
                        "GITHUB_INSTALL_STATE_INVALID",
                        "GitHub installation state is invalid or expired",
                    )
                self.require_workspace_owner(actor_id, row["workspace_id"])
                self.db.execute(
                    "UPDATE github_install_states SET consumed_at_ms=? WHERE state_hash=?",
                    (now, state_hash),
                )
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return dict(row)

    def complete_github_installation(
        self,
        *,
        actor_id: str,
        state_hash: str,
        installation: dict[str, Any],
        repositories: list[dict[str, Any]],
    ) -> dict[str, Any]:
        installation_id = int(installation.get("id") or 0)
        account = installation.get("account") or {}
        account_id = int(account.get("id") or 0)
        account_login = str(account.get("login") or "").strip()[:200]
        account_type = str(account.get("type") or "").strip()[:40]
        html_url = str(installation.get("html_url") or "").strip()[:1000]
        selection = str(installation.get("repository_selection") or "selected").strip()[:40]
        permissions = installation.get("permissions") or {}
        if (
            len(state_hash) != 64
            or installation_id <= 0
            or account_id <= 0
            or not account_login
            or account_type not in {"User", "Organization", "Enterprise"}
            or not isinstance(permissions, dict)
        ):
            raise StoreError(
                "GITHUB_INVALID_INSTALLATION",
                "GitHub installation metadata is invalid",
            )
        now = _now_ms()
        if installation.get("suspended_at"):
            raise StoreError(
                "GITHUB_INSTALLATION_UNAVAILABLE",
                "GitHub installation is suspended",
            )

        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                pending = self.db.execute(
                    "SELECT * FROM github_install_states WHERE state_hash=?",
                    (state_hash,),
                ).fetchone()
                if (
                    not pending
                    or pending["actor_id"] != actor_id
                    or pending["consumed_at_ms"] is not None
                    or int(pending["expires_at_ms"]) < now
                ):
                    raise StoreError(
                        "GITHUB_INSTALL_STATE_INVALID",
                        "GitHub installation state is invalid or expired",
                    )
                workspace_id = str(pending["workspace_id"])
                self.require_workspace_owner(actor_id, workspace_id)

                existing = self.db.execute(
                    """SELECT workspace_id,created_at_ms
                       FROM github_installations WHERE installation_id=?""",
                    (installation_id,),
                ).fetchone()
                if existing and existing["workspace_id"] != workspace_id:
                    raise StoreError(
                        "GITHUB_INSTALLATION_CONFLICT",
                        "GitHub installation is already bound to another workspace",
                    )
                created = existing["created_at_ms"] if existing else now
                self.db.execute(
                    """INSERT INTO github_installations
                       (installation_id,workspace_id,account_id,account_login,account_type,
                        html_url,repository_selection,permissions_json,state,suspended_at_ms,
                        last_verified_at_ms,created_at_ms,updated_at_ms)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(installation_id) DO UPDATE SET
                         account_id=excluded.account_id,
                         account_login=excluded.account_login,
                         account_type=excluded.account_type,
                         html_url=excluded.html_url,
                         repository_selection=excluded.repository_selection,
                         permissions_json=excluded.permissions_json,
                         state='active',
                         suspended_at_ms=NULL,
                         last_verified_at_ms=excluded.last_verified_at_ms,
                         updated_at_ms=excluded.updated_at_ms""",
                    (
                        installation_id,
                        workspace_id,
                        account_id,
                        account_login,
                        account_type,
                        html_url,
                        selection,
                        json.dumps(permissions, sort_keys=True, separators=(",", ":")),
                        "active",
                        None,
                        now,
                        created,
                        now,
                    ),
                )

                ids: list[int] = []
                for repository in repositories:
                    repository_id, full_name, default_branch, private = self._repository_fields(repository)
                    ids.append(repository_id)
                    old = self.db.execute(
                        """SELECT id,project_id,role,access_mode,allowed_paths_json,created_at_ms
                           FROM repository_connections
                           WHERE workspace_id=? AND repository_id=?""",
                        (workspace_id, repository_id),
                    ).fetchone()
                    self.db.execute(
                        """INSERT INTO repository_connections
                           (id,workspace_id,installation_id,repository_id,full_name,
                            default_branch,private,project_id,role,access_mode,
                            allowed_paths_json,state,last_verified_at_ms,created_at_ms,updated_at_ms)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(workspace_id,repository_id) DO UPDATE SET
                             installation_id=excluded.installation_id,
                             full_name=excluded.full_name,
                             default_branch=excluded.default_branch,
                             private=excluded.private,
                             state='available',
                             last_verified_at_ms=excluded.last_verified_at_ms,
                             updated_at_ms=excluded.updated_at_ms""",
                        (
                            old["id"] if old else _id("repo"),
                            workspace_id,
                            installation_id,
                            repository_id,
                            full_name,
                            default_branch,
                            private,
                            old["project_id"] if old else None,
                            old["role"] if old else "unassigned",
                            old["access_mode"] if old else "read_only",
                            old["allowed_paths_json"] if old else "[]",
                            "available",
                            now,
                            old["created_at_ms"] if old else now,
                            now,
                        ),
                    )
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    self.db.execute(
                        f"""UPDATE repository_connections
                            SET state='unavailable',updated_at_ms=?
                            WHERE workspace_id=? AND installation_id=?
                              AND repository_id NOT IN ({placeholders})""",
                        (now, workspace_id, installation_id, *ids),
                    )
                else:
                    self.db.execute(
                        """UPDATE repository_connections
                           SET state='unavailable',updated_at_ms=?
                           WHERE workspace_id=? AND installation_id=?""",
                        (now, workspace_id, installation_id),
                    )

                self.db.execute(
                    "UPDATE github_install_states SET consumed_at_ms=? WHERE state_hash=?",
                    (now, state_hash),
                )
                stored = dict(
                    self.db.execute(
                        "SELECT * FROM github_installations WHERE installation_id=?",
                        (installation_id,),
                    ).fetchone()
                )
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

            connections = self.list_repository_connections_internal(workspace_id)
            return {
                "pending": dict(pending),
                "installation": stored,
                "connections": connections,
            }

    def upsert_github_installation(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        installation: dict[str, Any],
    ) -> dict[str, Any]:
        installation_id = int(installation.get("id") or 0)
        account = installation.get("account") or {}
        account_id = int(account.get("id") or 0)
        account_login = str(account.get("login") or "").strip()[:200]
        account_type = str(account.get("type") or "").strip()[:40]
        html_url = str(installation.get("html_url") or "").strip()[:1000]
        selection = str(installation.get("repository_selection") or "selected").strip()[:40]
        permissions = installation.get("permissions") or {}
        if (
            installation_id <= 0
            or account_id <= 0
            or not account_login
            or account_type not in {"User", "Organization", "Enterprise"}
            or not isinstance(permissions, dict)
        ):
            raise StoreError("GITHUB_INVALID_INSTALLATION", "GitHub installation metadata is invalid")
        now = _now_ms()
        suspended = installation.get("suspended_at")
        state = "suspended" if suspended else "active"
        with self._lock:
            self.require_workspace_owner(actor_id, workspace_id)
            existing = self.db.execute(
                "SELECT workspace_id,created_at_ms FROM github_installations WHERE installation_id=?",
                (installation_id,),
            ).fetchone()
            if existing and existing["workspace_id"] != workspace_id:
                raise StoreError(
                    "GITHUB_INSTALLATION_CONFLICT",
                    "GitHub installation is already bound to another workspace",
                )
            created = existing["created_at_ms"] if existing else now
            self.db.execute(
                """INSERT INTO github_installations
                   (installation_id,workspace_id,account_id,account_login,account_type,
                    html_url,repository_selection,permissions_json,state,suspended_at_ms,
                    last_verified_at_ms,created_at_ms,updated_at_ms)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(installation_id) DO UPDATE SET
                     account_id=excluded.account_id,
                     account_login=excluded.account_login,
                     account_type=excluded.account_type,
                     html_url=excluded.html_url,
                     repository_selection=excluded.repository_selection,
                     permissions_json=excluded.permissions_json,
                     state=excluded.state,
                     suspended_at_ms=excluded.suspended_at_ms,
                     last_verified_at_ms=excluded.last_verified_at_ms,
                     updated_at_ms=excluded.updated_at_ms""",
                (
                    installation_id,
                    workspace_id,
                    account_id,
                    account_login,
                    account_type,
                    html_url,
                    selection,
                    json.dumps(permissions, sort_keys=True, separators=(",", ":")),
                    state,
                    now if suspended else None,
                    now,
                    created,
                    now,
                ),
            )
            return dict(
                self.db.execute(
                    "SELECT * FROM github_installations WHERE installation_id=?",
                    (installation_id,),
                ).fetchone()
            )

    @staticmethod
    def _repository_fields(repository: dict[str, Any]) -> tuple[int, str, str, int]:
        repository_id = int(repository.get("id") or 0)
        full_name = str(repository.get("full_name") or "").strip()[:300]
        default_branch = str(repository.get("default_branch") or "main").strip()[:200]
        private = 1 if bool(repository.get("private")) else 0
        if repository_id <= 0 or not full_name or "/" not in full_name or not default_branch:
            raise StoreError("GITHUB_INVALID_REPOSITORY", "GitHub repository metadata is invalid")
        return repository_id, full_name, default_branch, private

    def sync_github_repositories(
        self,
        *,
        workspace_id: str,
        installation_id: int,
        repositories: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        now = _now_ms()
        with self._lock:
            installation = self.db.execute(
                "SELECT workspace_id,state FROM github_installations WHERE installation_id=?",
                (int(installation_id),),
            ).fetchone()
            if not installation or installation["workspace_id"] != workspace_id:
                raise StoreError("GITHUB_INSTALLATION_NOT_FOUND", "GitHub installation is unavailable")
            if installation["state"] != "active":
                raise StoreError("GITHUB_INSTALLATION_UNAVAILABLE", "GitHub installation is not active")
            ids: list[int] = []
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for repository in repositories:
                    repository_id, full_name, default_branch, private = self._repository_fields(repository)
                    ids.append(repository_id)
                    old = self.db.execute(
                        """SELECT id,project_id,role,access_mode,allowed_paths_json,created_at_ms
                           FROM repository_connections
                           WHERE workspace_id=? AND repository_id=?""",
                        (workspace_id, repository_id),
                    ).fetchone()
                    connection_id = old["id"] if old else _id("repo")
                    self.db.execute(
                        """INSERT INTO repository_connections
                           (id,workspace_id,installation_id,repository_id,full_name,
                            default_branch,private,project_id,role,access_mode,
                            allowed_paths_json,state,last_verified_at_ms,created_at_ms,updated_at_ms)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(workspace_id,repository_id) DO UPDATE SET
                             installation_id=excluded.installation_id,
                             full_name=excluded.full_name,
                             default_branch=excluded.default_branch,
                             private=excluded.private,
                             state='available',
                             last_verified_at_ms=excluded.last_verified_at_ms,
                             updated_at_ms=excluded.updated_at_ms""",
                        (
                            connection_id,
                            workspace_id,
                            int(installation_id),
                            repository_id,
                            full_name,
                            default_branch,
                            private,
                            old["project_id"] if old else None,
                            old["role"] if old else "unassigned",
                            old["access_mode"] if old else "read_only",
                            old["allowed_paths_json"] if old else "[]",
                            "available",
                            now,
                            old["created_at_ms"] if old else now,
                            now,
                        ),
                    )
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    self.db.execute(
                        f"""UPDATE repository_connections
                            SET state='unavailable',updated_at_ms=?
                            WHERE workspace_id=? AND installation_id=?
                              AND repository_id NOT IN ({placeholders})""",
                        (now, workspace_id, int(installation_id), *ids),
                    )
                else:
                    self.db.execute(
                        """UPDATE repository_connections
                           SET state='unavailable',updated_at_ms=?
                           WHERE workspace_id=? AND installation_id=?""",
                        (now, workspace_id, int(installation_id)),
                    )
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return self.list_repository_connections_internal(workspace_id)

    def list_repository_connections_internal(self, workspace_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT c.*,i.account_id,i.account_login,i.account_type,i.html_url AS installation_url,
                      i.permissions_json,i.state AS installation_state
               FROM repository_connections c
               JOIN github_installations i ON i.installation_id=c.installation_id
               WHERE c.workspace_id=?
               ORDER BY c.full_name,c.repository_id""",
            (workspace_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["private"] = bool(item["private"])
            item["allowed_paths"] = json.loads(item.pop("allowed_paths_json") or "[]")
            item["permissions"] = json.loads(item.pop("permissions_json") or "{}")
            result.append(item)
        return result

    def list_repository_connections(
        self,
        actor_id: str,
        workspace_id: str,
    ) -> list[dict[str, Any]]:
        with self._lock:
            self._membership(actor_id, workspace_id)
            return self.list_repository_connections_internal(workspace_id)

    @staticmethod
    def _normalize_allowed_paths(paths: list[str] | None) -> list[str]:
        if not paths:
            return []
        if len(paths) > 32:
            raise StoreError("INVALID_ARGUMENT", "Too many allowed repository paths")
        clean: list[str] = []
        for raw in paths:
            value = str(raw).strip().strip("/")
            parts = value.split("/")
            if (
                not value
                or len(value) > 300
                or "\\" in value
                or any(part in {"", ".", ".."} for part in parts)
            ):
                raise StoreError("INVALID_ARGUMENT", "Repository path restriction is invalid")
            if value not in clean:
                clean.append(value)
        return clean

    def bind_repository_connection(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        repository_id: int,
        project_id: str | None,
        role: str,
        access_mode: str,
        allowed_paths: list[str] | None,
    ) -> dict[str, Any]:
        roles = {
            "memory_store",
            "project_docs",
            "source_dataset",
            "external_owning_repo",
            "generated_artifacts",
        }
        if role not in roles:
            raise StoreError("INVALID_ARGUMENT", "Repository role is invalid")
        if access_mode not in {"read_only", "app_managed_write"}:
            raise StoreError("INVALID_ARGUMENT", "Repository access mode is invalid")
        if role == "external_owning_repo" and access_mode != "read_only":
            raise StoreError(
                "GITHUB_WRITE_POLICY_DENIED",
                "External owning repositories are read-only in this release",
            )
        paths = self._normalize_allowed_paths(allowed_paths)
        now = _now_ms()
        with self._lock:
            self.require_workspace_owner(actor_id, workspace_id)
            row = self.db.execute(
                """SELECT c.*,i.permissions_json,i.state AS installation_state
                   FROM repository_connections c
                   JOIN github_installations i ON i.installation_id=c.installation_id
                   WHERE c.workspace_id=? AND c.repository_id=?""",
                (workspace_id, int(repository_id)),
            ).fetchone()
            if not row or row["state"] != "available" or row["installation_state"] != "active":
                raise StoreError("GITHUB_REPOSITORY_UNAVAILABLE", "GitHub repository is unavailable")
            if project_id is not None:
                project = self._project_row(workspace_id, project_id)
                if not project or project["status"] != "active":
                    raise StoreError("PROJECT_NOT_FOUND", "Project is not available")
            permissions = json.loads(row["permissions_json"] or "{}")
            if access_mode == "app_managed_write" and permissions.get("contents") != "write":
                raise StoreError(
                    "GITHUB_WRITE_PERMISSION_MISSING",
                    "GitHub App installation does not grant Contents write",
                )
            self.db.execute(
                """UPDATE repository_connections
                   SET project_id=?,role=?,access_mode=?,allowed_paths_json=?,
                       updated_at_ms=?
                   WHERE workspace_id=? AND repository_id=?""",
                (
                    project_id,
                    role,
                    access_mode,
                    json.dumps(paths, ensure_ascii=False, separators=(",", ":")),
                    now,
                    workspace_id,
                    int(repository_id),
                ),
            )
            return next(
                item
                for item in self.list_repository_connections_internal(workspace_id)
                if int(item["repository_id"]) == int(repository_id)
            )

    def get_repository_connection(
        self,
        actor_id: str,
        workspace_id: str,
        repository_id: int,
    ) -> dict[str, Any]:
        with self._lock:
            self._membership(actor_id, workspace_id)
            for item in self.list_repository_connections_internal(workspace_id):
                if int(item["repository_id"]) == int(repository_id):
                    return item
        raise StoreError("GITHUB_REPOSITORY_NOT_FOUND", "GitHub repository is not connected")

    def list_github_installations(
        self,
        actor_id: str,
        workspace_id: str,
    ) -> list[dict[str, Any]]:
        with self._lock:
            self._membership(actor_id, workspace_id)
            rows = self.db.execute(
                """SELECT installation_id,account_id,account_login,account_type,html_url,
                          repository_selection,state,suspended_at_ms,last_verified_at_ms
                   FROM github_installations
                   WHERE workspace_id=?
                   ORDER BY account_login,installation_id""",
                (workspace_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def github_installation(self, installation_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM github_installations WHERE installation_id=?",
                (int(installation_id),),
            ).fetchone()
            return dict(row) if row else None

    def record_github_webhook_delivery(
        self,
        *,
        delivery_id: str,
        event_name: str,
        action: str,
    ) -> bool:
        if not delivery_id or len(delivery_id) > 200:
            raise StoreError("INVALID_ARGUMENT", "GitHub webhook delivery id is invalid")
        with self._lock:
            try:
                self.db.execute(
                    """INSERT INTO github_webhook_deliveries
                       (delivery_id,event_name,action,received_at_ms)
                       VALUES(?,?,?,?)""",
                    (
                        delivery_id,
                        event_name[:100],
                        action[:100],
                        _now_ms(),
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def set_github_installation_state(
        self,
        *,
        installation_id: int,
        state: str,
    ) -> None:
        if state not in {"active", "suspended", "revoked"}:
            raise StoreError("INVALID_ARGUMENT", "GitHub installation state is invalid")
        now = _now_ms()
        with self._lock:
            row = self.db.execute(
                "SELECT workspace_id FROM github_installations WHERE installation_id=?",
                (int(installation_id),),
            ).fetchone()
            if not row:
                return
            self.db.execute(
                """UPDATE github_installations
                   SET state=?,suspended_at_ms=?,updated_at_ms=?
                   WHERE installation_id=?""",
                (
                    state,
                    now if state == "suspended" else None,
                    now,
                    int(installation_id),
                ),
            )
            if state != "active":
                self.db.execute(
                    """UPDATE repository_connections
                       SET state='unavailable',updated_at_ms=?
                       WHERE installation_id=?""",
                    (now, int(installation_id)),
                )

    def apply_github_repository_webhook(
        self,
        *,
        installation_id: int,
        added: list[dict[str, Any]],
        removed: list[dict[str, Any]],
    ) -> None:
        now = _now_ms()
        with self._lock:
            installation = self.db.execute(
                "SELECT workspace_id,state FROM github_installations WHERE installation_id=?",
                (int(installation_id),),
            ).fetchone()
            if not installation:
                return
            workspace_id = str(installation["workspace_id"])
            if installation["state"] != "active":
                return
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for repository in added:
                    repository_id, full_name, default_branch, private = self._repository_fields(repository)
                    old = self.db.execute(
                        """SELECT id,project_id,role,access_mode,allowed_paths_json,created_at_ms
                           FROM repository_connections
                           WHERE workspace_id=? AND repository_id=?""",
                        (workspace_id, repository_id),
                    ).fetchone()
                    self.db.execute(
                        """INSERT INTO repository_connections
                           (id,workspace_id,installation_id,repository_id,full_name,
                            default_branch,private,project_id,role,access_mode,
                            allowed_paths_json,state,last_verified_at_ms,created_at_ms,updated_at_ms)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(workspace_id,repository_id) DO UPDATE SET
                             installation_id=excluded.installation_id,
                             full_name=excluded.full_name,
                             default_branch=excluded.default_branch,
                             private=excluded.private,
                             state='available',
                             last_verified_at_ms=excluded.last_verified_at_ms,
                             updated_at_ms=excluded.updated_at_ms""",
                        (
                            old["id"] if old else _id("repo"),
                            workspace_id,
                            int(installation_id),
                            repository_id,
                            full_name,
                            default_branch,
                            private,
                            old["project_id"] if old else None,
                            old["role"] if old else "unassigned",
                            old["access_mode"] if old else "read_only",
                            old["allowed_paths_json"] if old else "[]",
                            "available",
                            now,
                            old["created_at_ms"] if old else now,
                            now,
                        ),
                    )
                for repository in removed:
                    repository_id = int(repository.get("id") or 0)
                    if repository_id > 0:
                        self.db.execute(
                            """UPDATE repository_connections
                               SET state='unavailable',updated_at_ms=?
                               WHERE installation_id=? AND repository_id=?""",
                            (now, int(installation_id), repository_id),
                        )
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    @staticmethod
    def _decode_json_object(value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _device_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        capabilities_raw = item.pop("capabilities_json", "[]")
        item.pop("credential_hash", None)
        try:
            capabilities = json.loads(capabilities_raw)
        except json.JSONDecodeError:
            capabilities = []
        item["capabilities"] = [
            str(value)
            for value in capabilities
            if isinstance(value, str)
        ] if isinstance(capabilities, list) else []
        return item

    @staticmethod
    def _device_command_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        payload_raw = item.pop("payload_json", "{}")
        result_raw = item.pop("result_json", None)
        item.pop("claim_hash", None)
        item["payload"] = DurableStore._decode_json_object(payload_raw)
        item["result"] = (
            DurableStore._decode_json_object(result_raw)
            if result_raw
            else None
        )
        return item

    def register_device(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        display_name: str,
        platform: str,
        capabilities: list[str],
        credential_hash: str,
        session_id: str,
    ) -> dict[str, Any]:
        name = display_name.strip()[:120]
        if not name:
            raise StoreError("INVALID_ARGUMENT", "Device display name is required")
        if platform not in {"android"}:
            raise StoreError("INVALID_ARGUMENT", "Unsupported device platform")
        if (
            len(credential_hash) != 64
            or len(session_id) < 20
            or len(session_id) > 160
        ):
            raise StoreError("INVALID_ARGUMENT", "Device credential binding is invalid")
        clean_capabilities = sorted(set(str(value) for value in capabilities))
        if not clean_capabilities or len(clean_capabilities) > 32:
            raise StoreError("INVALID_ARGUMENT", "Device capabilities are invalid")
        now = _now_ms()
        device_id = _id("dev")
        with self._lock:
            self._membership(actor_id, workspace_id)
            self.db.execute(
                """INSERT INTO devices(
                       id,actor_id,workspace_id,session_id,display_name,platform,
                       capabilities_json,credential_hash,state,last_seen_at_ms,
                       created_at_ms,updated_at_ms
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    device_id,
                    actor_id,
                    workspace_id,
                    session_id,
                    name,
                    platform,
                    json.dumps(
                        clean_capabilities,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    credential_hash,
                    "active",
                    now,
                    now,
                    now,
                ),
            )
            row = self.db.execute(
                "SELECT * FROM devices WHERE id=?",
                (device_id,),
            ).fetchone()
            return self._device_public(row)

    def list_devices(
        self,
        actor_id: str,
        workspace_id: str,
        *,
        capability: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            self._membership(actor_id, workspace_id)
            rows = self.db.execute(
                """SELECT * FROM devices
                   WHERE actor_id=? AND workspace_id=? AND state='active'
                   ORDER BY updated_at_ms DESC,id""",
                (actor_id, workspace_id),
            ).fetchall()
            result = [self._device_public(row) for row in rows]
            if capability:
                result = [
                    item
                    for item in result
                    if capability in item["capabilities"]
                ]
            return result

    def device_auth_record(self, device_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM devices WHERE id=?",
                (device_id,),
            ).fetchone()
            return dict(row) if row else None

    def touch_device(
        self,
        *,
        device_id: str,
        session_id: str,
    ) -> None:
        now = _now_ms()
        with self._lock:
            self.db.execute(
                """UPDATE devices SET last_seen_at_ms=?,updated_at_ms=?
                   WHERE id=? AND session_id=? AND state='active'""",
                (now, now, device_id, session_id),
            )

    def update_device_capabilities(
        self,
        *,
        device_id: str,
        session_id: str,
        capabilities: list[str],
    ) -> dict[str, Any]:
        clean = sorted(set(str(value).strip() for value in capabilities if str(value).strip()))
        if not clean or len(clean) > 32:
            raise StoreError("INVALID_ARGUMENT", "Device capabilities are invalid")
        now = _now_ms()
        with self._lock:
            self.db.execute(
                """UPDATE devices
                   SET capabilities_json=?,updated_at_ms=?,last_seen_at_ms=?
                   WHERE id=? AND session_id=? AND state='active'""",
                (
                    json.dumps(clean, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                    device_id,
                    session_id,
                ),
            )
            if self.db.execute("SELECT changes()").fetchone()[0] != 1:
                raise StoreError("DEVICE_NOT_FOUND", "Device is unavailable")
            row = self.db.execute(
                "SELECT * FROM devices WHERE id=?",
                (device_id,),
            ).fetchone()
            return self._device_public(row)

    def disable_device(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        device_id: str,
    ) -> None:
        now = _now_ms()
        with self._lock:
            self._membership(actor_id, workspace_id)
            row = self.db.execute(
                """SELECT id FROM devices
                   WHERE id=? AND actor_id=? AND workspace_id=?""",
                (device_id, actor_id, workspace_id),
            ).fetchone()
            if not row:
                raise StoreError("DEVICE_NOT_FOUND", "Device is not available")
            self.db.execute(
                "UPDATE devices SET state='revoked',updated_at_ms=? WHERE id=?",
                (now, device_id),
            )
            self.db.execute(
                """UPDATE device_commands
                   SET status='cancelled',finished_at_ms=?,updated_at_ms=?
                   WHERE device_id=? AND status='pending'""",
                (now, now, device_id),
            )

    def _refresh_device_command_timeouts(self, now: int | None = None) -> None:
        current = now if now is not None else _now_ms()
        self.db.execute(
            """UPDATE device_commands
               SET status='expired',finished_at_ms=?,updated_at_ms=?
               WHERE status='pending' AND expires_at_ms<=?""",
            (current, current, current),
        )
        self.db.execute(
            """UPDATE device_commands
               SET status='outcome_unknown',finished_at_ms=?,updated_at_ms=?
               WHERE status='claimed' AND claimed_at_ms IS NOT NULL
                 AND claimed_at_ms<=?""",
            (current, current, current - 120_000),
        )

    def create_device_command(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        project_id: str | None,
        device_id: str,
        capability: str,
        payload: dict[str, Any],
        payload_sha256: str,
        command_id: str,
        expires_at_ms: int,
    ) -> dict[str, Any]:
        now = _now_ms()
        if (
            not command_id.startswith("cmd_")
            or len(command_id) > 160
            or len(payload_sha256) != 64
            or not capability
            or len(capability) > 100
            or expires_at_ms <= now
            or expires_at_ms > now + 60 * 60 * 1000
        ):
            raise StoreError("INVALID_ARGUMENT", "Device command is invalid")
        encoded_payload = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock:
            self._membership(actor_id, workspace_id)
            if project_id is not None:
                project = self._project_row(workspace_id, project_id)
                if not project or project["status"] != "active":
                    raise StoreError("PROJECT_NOT_FOUND", "Project is not available")
            device = self.db.execute(
                """SELECT * FROM devices
                   WHERE id=? AND actor_id=? AND workspace_id=? AND state='active'""",
                (device_id, actor_id, workspace_id),
            ).fetchone()
            if not device:
                raise StoreError("DEVICE_NOT_FOUND", "Device is not available")
            capabilities = json.loads(device["capabilities_json"] or "[]")
            if capability not in capabilities:
                raise StoreError(
                    "DEVICE_CAPABILITY_NOT_AVAILABLE",
                    "Device does not grant this capability",
                )
            existing = self.db.execute(
                "SELECT * FROM device_commands WHERE id=?",
                (command_id,),
            ).fetchone()
            if existing:
                if (
                    existing["actor_id"] != actor_id
                    or existing["workspace_id"] != workspace_id
                    or existing["device_id"] != device_id
                    or existing["device_session_id"] != device["session_id"]
                    or existing["capability"] != capability
                    or existing["payload_sha256"] != payload_sha256
                ):
                    raise StoreError(
                        "DEVICE_COMMAND_CONFLICT",
                        "Command id is already bound to another device action",
                    )
                self._refresh_device_command_timeouts(now)
                return self._device_command_public(
                    self.db.execute(
                        "SELECT * FROM device_commands WHERE id=?",
                        (command_id,),
                    ).fetchone()
                )
            self.db.execute(
                """INSERT INTO device_commands(
                       id,actor_id,workspace_id,project_id,device_id,device_session_id,
                       capability,payload_json,payload_sha256,status,claim_hash,
                       claimed_at_ms,result_json,result_sha256,expires_at_ms,
                       finished_at_ms,created_at_ms,updated_at_ms
                   ) VALUES(?,?,?,?,?,?,?,?,?,'pending',NULL,NULL,NULL,NULL,?,NULL,?,?)""",
                (
                    command_id,
                    actor_id,
                    workspace_id,
                    project_id,
                    device_id,
                    device["session_id"],
                    capability,
                    encoded_payload,
                    payload_sha256,
                    int(expires_at_ms),
                    now,
                    now,
                ),
            )
            return self._device_command_public(
                self.db.execute(
                    "SELECT * FROM device_commands WHERE id=?",
                    (command_id,),
                ).fetchone()
            )

    def claim_next_device_command(
        self,
        *,
        device_id: str,
        session_id: str,
        claim_hash: str,
    ) -> dict[str, Any] | None:
        if len(claim_hash) != 64:
            raise StoreError("INVALID_ARGUMENT", "Device command claim is invalid")
        now = _now_ms()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._refresh_device_command_timeouts(now)
                device = self.db.execute(
                    """SELECT id FROM devices
                       WHERE id=? AND session_id=? AND state='active'""",
                    (device_id, session_id),
                ).fetchone()
                if not device:
                    raise StoreError("DEVICE_UNAUTHENTICATED", "Device session is unavailable")
                row = self.db.execute(
                    """SELECT * FROM device_commands
                       WHERE device_id=? AND device_session_id=?
                         AND status='pending' AND expires_at_ms>?
                       ORDER BY created_at_ms,id
                       LIMIT 1""",
                    (device_id, session_id, now),
                ).fetchone()
                if not row:
                    self.db.execute("COMMIT")
                    return None
                self.db.execute(
                    """UPDATE device_commands
                       SET status='claimed',claim_hash=?,claimed_at_ms=?,updated_at_ms=?
                       WHERE id=? AND status='pending'""",
                    (claim_hash, now, now, row["id"]),
                )
                claimed = self.db.execute(
                    "SELECT * FROM device_commands WHERE id=?",
                    (row["id"],),
                ).fetchone()
                self.db.execute("COMMIT")
                return self._device_command_public(claimed)
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def get_device_command(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        command_id: str,
    ) -> dict[str, Any]:
        with self._lock:
            self._membership(actor_id, workspace_id)
            self._refresh_device_command_timeouts()
            row = self.db.execute(
                """SELECT * FROM device_commands
                   WHERE id=? AND actor_id=? AND workspace_id=?""",
                (command_id, actor_id, workspace_id),
            ).fetchone()
            if not row:
                raise StoreError("DEVICE_COMMAND_NOT_FOUND", "Device command is unavailable")
            return self._device_command_public(row)

    def complete_device_command(
        self,
        *,
        device_id: str,
        session_id: str,
        command_id: str,
        claim_hash: str,
        payload_sha256: str,
        status: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if status not in {"applied", "rejected", "failed"}:
            raise StoreError("INVALID_ARGUMENT", "Device command result status is invalid")
        if len(claim_hash) != 64 or len(payload_sha256) != 64:
            raise StoreError("INVALID_ARGUMENT", "Device command receipt is invalid")
        encoded_result = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        result_sha = hashlib.sha256(encoded_result.encode("utf-8")).hexdigest()
        now = _now_ms()
        with self._lock:
            self._refresh_device_command_timeouts(now)
            row = self.db.execute(
                """SELECT * FROM device_commands
                   WHERE id=? AND device_id=? AND device_session_id=?""",
                (command_id, device_id, session_id),
            ).fetchone()
            if not row:
                raise StoreError("DEVICE_COMMAND_NOT_FOUND", "Device command is unavailable")
            if row["payload_sha256"] != payload_sha256:
                raise StoreError(
                    "DEVICE_COMMAND_CONFLICT",
                    "Device command payload digest mismatch",
                )
            if row["status"] in {"applied", "rejected", "failed"}:
                if row["status"] == status and row["result_sha256"] == result_sha:
                    return self._device_command_public(row)
                raise StoreError(
                    "DEVICE_COMMAND_CONFLICT",
                    "Device command already has another terminal result",
                )
            if row["status"] == "outcome_unknown":
                raise StoreError(
                    "DEVICE_COMMAND_OUTCOME_UNKNOWN",
                    "Device command outcome must be reconciled explicitly",
                )
            if row["status"] != "claimed" or row["claim_hash"] != claim_hash:
                raise StoreError(
                    "DEVICE_COMMAND_CLAIM_INVALID",
                    "Device command claim is invalid",
                )
            self.db.execute(
                """UPDATE device_commands
                   SET status=?,result_json=?,result_sha256=?,finished_at_ms=?,updated_at_ms=?
                   WHERE id=?""",
                (
                    status,
                    encoded_result,
                    result_sha,
                    now,
                    now,
                    command_id,
                ),
            )
            return self._device_command_public(
                self.db.execute(
                    "SELECT * FROM device_commands WHERE id=?",
                    (command_id,),
                ).fetchone()
            )

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

    def recent_conversation_history(
        self,
        actor_id: str,
        conversation_id: str,
        *,
        max_turns: int = 8,
        max_chars: int = 700,
    ) -> list[dict[str, str]]:
        bounded_turns = max(1, min(int(max_turns), 12))
        bounded_chars = max(80, min(int(max_chars), 1000))
        with self._lock:
            self.get_conversation(actor_id, conversation_id)
            rows = self.db.execute(
                """SELECT e.kind,e.text
                   FROM source_events e
                   JOIN sources s ON s.id=e.source_id
                   WHERE s.conversation_id=? AND s.actor_id=?
                     AND e.kind IN ('input_transcript','output_transcript','turn_complete','interrupted')
                   ORDER BY e.id DESC LIMIT 160""",
                (conversation_id, actor_id),
            ).fetchall()
        rows = list(reversed(rows))
        turns: list[dict[str, str]] = []
        active_role: str | None = None
        active_text = ""

        def commit() -> None:
            nonlocal active_role, active_text
            text = active_text.strip()
            if active_role and text:
                if len(text) > bounded_chars:
                    text = text[: bounded_chars // 2].rstrip() + " … " + text[-(bounded_chars // 2 - 3):].lstrip()
                turns.append({"role": active_role, "text": text})
            active_role = None
            active_text = ""

        pending_turn: list[dict[str, str]] = []

        def commit_turn() -> None:
            nonlocal pending_turn
            commit()
            if turns:
                pending_turn.append(turns.pop())
            turns.extend(pending_turn)
            pending_turn = []

        def discard_turn() -> None:
            nonlocal active_role, active_text, pending_turn
            active_role = None
            active_text = ""
            pending_turn = []

        for row in rows:
            kind = str(row["kind"])
            if kind == "turn_complete":
                commit_turn()
                continue
            if kind == "interrupted":
                discard_turn()
                continue
            role = "user" if kind == "input_transcript" else "model"
            fragment = str(row["text"] or "").strip()
            if not fragment:
                continue
            if role != active_role:
                commit()
                if turns:
                    pending_turn.append(turns.pop())
                active_role = role
            if not active_text:
                active_text = fragment
            elif fragment.startswith(active_text):
                active_text = fragment
            elif not active_text.endswith(fragment):
                active_text = (active_text + " " + fragment).strip()
        # Deliberately do not commit an unterminated tail. A source/session can
        # stop after user speech but before the provider acknowledges turn_complete;
        # replaying that tail into the next provider session makes a fresh greeting
        # look like permission to answer the stale request.
        return turns[-bounded_turns:]

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
