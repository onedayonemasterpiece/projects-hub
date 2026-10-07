"""Import verified ChatGPT scheduled analyses from bound private project repositories.

ChatGPT's hourly scheduled task owns the expensive reasoning and writes a companion
Markdown file. This adapter is deliberately deterministic: it only verifies a
frozen source blob, imports the result and emits one ACL-scoped event. It never
runs another model or grants a ChatGPT analysis authority to create tasks.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import re
import time
from pathlib import PurePosixPath
from typing import Any

import yaml

from .store import DurableStore, StoreError
from .github_connections import GitHubConnections

ROUTING_PATH = "automation/chatgpt-note-analysis.yaml"
RESULT_SCHEMA = "projects-hub-chatgpt-v1"
ROUTE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{3,79}$")
NOTE_PATTERN = re.compile(r"^note_[a-f0-9]{32}$")
SHA_PATTERN = re.compile(r"^[a-f0-9]{40}$")
META_KEYS = {
    "analysis_schema", "route_id", "note_id", "project_id", "source_path",
    "source_sha", "audience", "status", "provider", "generated_at_utc",
}
POLL_INTERVAL_SECONDS = 120


def _ms() -> int:
    return int(time.time() * 1000)


def _safe_path(value: Any) -> str:
    path = str(value or "")
    if (
        not path.startswith("docs/notes/")
        or len(path) > 300
        or "\\" in path
        or "%" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or str(PurePosixPath(path)) != path
        or not path.endswith(".md")
    ):
        raise ValueError("analysis route path is outside docs/notes")
    return path


def _result_path(source: str) -> str:
    if source.endswith(".chatgpt-analysis.md"):
        raise ValueError("analysis output cannot be used as its input")
    return source[:-3] + ".chatgpt-analysis.md"


def parse_routing_yaml(text: str, *, repository: str) -> list[dict[str, str]]:
    if not text or len(text.encode("utf-8")) > 100_000:
        raise ValueError("routing manifest size is invalid")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError("routing manifest YAML is invalid") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("unsupported routing manifest schema")
    if not document.get("enabled"):
        return []
    if (
        document.get("source_repository") != repository
        or document.get("require_private_repository") is not True
        or document.get("require_source_blob_match") is not True
    ):
        raise ValueError("routing manifest lacks strict repository controls")
    routes = document.get("routes")
    if not isinstance(routes, list) or len(routes) > 50:
        raise ValueError("routing manifest route count is invalid")
    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in routes:
        if not isinstance(raw, dict):
            raise ValueError("invalid route")
        if raw.get("enabled") is not True:
            continue
        route_id = str(raw.get("id") or "")
        note_id = str(raw.get("note_id") or "")
        project_id = str(raw.get("project_id") or "")
        if (
            not ROUTE_PATTERN.fullmatch(route_id)
            or not NOTE_PATTERN.fullmatch(note_id)
            or not project_id.startswith("prj_")
            or len(project_id) > 80
        ):
            raise ValueError("routing identity is invalid")
        source = _safe_path(raw.get("source_path"))
        output = _safe_path(raw.get("result_path"))
        if output != _result_path(source):
            raise ValueError("analysis output must be the companion file")
        if route_id in seen or note_id in seen:
            raise ValueError("duplicate analysis routing")
        seen.add(route_id)
        seen.add(note_id)
        selected.append({
            "route_id": route_id, "note_id": note_id, "project_id": project_id,
            "source_path": source, "result_path": output,
        })
    return selected


def parse_analysis_markdown(text: str) -> tuple[dict[str, str], str]:
    if not text or len(text.encode("utf-8")) > 100_000:
        raise ValueError("ChatGPT analysis size is invalid")
    lines = text.splitlines()
    if len(lines) < 14 or lines[0] != "---":
        raise ValueError("ChatGPT analysis frontmatter is missing")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("ChatGPT analysis frontmatter is not closed") from exc
    if end > 20:
        raise ValueError("ChatGPT analysis metadata is too large")
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        name, separator, value = line.partition(":")
        if not separator or name not in META_KEYS or name in fields:
            raise ValueError("ChatGPT analysis metadata is invalid")
        value = value.strip()
        if not value or len(value) > 500:
            raise ValueError("ChatGPT analysis metadata value is invalid")
        fields[name] = value
    if fields.keys() != META_KEYS:
        raise ValueError("ChatGPT analysis metadata is incomplete")
    if (
        fields["analysis_schema"] != RESULT_SCHEMA
        or fields["provider"] != "chatgpt_scheduled"
        or fields["status"] != "completed"
        or fields["audience"] != "project"
        or not SHA_PATTERN.fullmatch(fields["source_sha"])
    ):
        raise ValueError("ChatGPT analysis metadata contract does not match")
    try:
        datetime.fromisoformat(fields["generated_at_utc"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("ChatGPT analysis time is invalid") from exc
    body = "\n".join(lines[end + 1:]).strip()
    if len(body) < 80 or not body.startswith("# Глубокий анализ ChatGPT"):
        raise ValueError("ChatGPT analysis is incomplete")
    return fields, body


class ChatGPTNoteAnalysisSync:
    """Bounded repository readback and durable personal-notification projection."""

    def __init__(
        self,
        store: DurableStore,
        github: GitHubConnections,
        *,
        interval_seconds: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.store = store
        self.github = github
        self.interval_seconds = max(10.0, float(interval_seconds))
        self._last_poll = 0.0
        with store._lock:
            store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS note_chatgpt_analyses(
                    note_id TEXT NOT NULL REFERENCES project_notes(id),
                    source_sha TEXT NOT NULL,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    route_id TEXT NOT NULL,
                    repository_id INTEGER NOT NULL,
                    result_path TEXT NOT NULL,
                    result_sha TEXT NOT NULL,
                    generated_at_utc TEXT NOT NULL,
                    result_markdown TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(note_id, source_sha)
                );
                CREATE INDEX IF NOT EXISTS note_chatgpt_analyses_latest_idx
                    ON note_chatgpt_analyses(note_id, created_at_ms DESC);
                """
            )

    def for_note(
        self, *, actor_id: str, workspace_id: str, note_id: str,
    ) -> dict[str, Any] | None:
        with self.store._lock:
            note = self.store.db.execute(
                "SELECT project_id FROM project_notes WHERE id=? AND workspace_id=?",
                (note_id, workspace_id),
            ).fetchone()
            if note is None:
                raise StoreError("NOTE_NOT_FOUND", "Note is not available")
        self.store.project_access(actor_id, workspace_id, str(note["project_id"]))
        with self.store._lock:
            row = self.store.db.execute(
                """SELECT * FROM note_chatgpt_analyses WHERE note_id=?
                   ORDER BY created_at_ms DESC,source_sha DESC LIMIT 1""",
                (note_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "status": "completed",
            "note_id": row["note_id"],
            "route_id": row["route_id"],
            "source_sha": row["source_sha"],
            "result_sha": row["result_sha"],
            "repository_path": row["result_path"],
            "generated_at_utc": row["generated_at_utc"],
            "markdown": row["result_markdown"],
        }

    def _register(
        self,
        *,
        note: Any,
        route: dict[str, str],
        source_sha: str,
        result_sha: str,
        generated_at: str,
        body: str,
    ) -> bool:
        now = _ms()
        with self.store._lock:
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                cur = self.store.db.execute(
                    """INSERT OR IGNORE INTO note_chatgpt_analyses(
                           note_id,source_sha,workspace_id,project_id,route_id,
                           repository_id,result_path,result_sha,generated_at_utc,
                           result_markdown,created_at_ms)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        note["id"], source_sha, note["workspace_id"],
                        note["project_id"], route["route_id"],
                        int(note["repository_id"]), route["result_path"],
                        result_sha, generated_at, body, now,
                    ),
                )
                inserted = cur.rowcount == 1
                if inserted:
                    self.store.db.execute(
                        """INSERT INTO collaboration_events(
                               workspace_id,project_id,actor_id,kind,object_kind,
                               object_id,parent_object_id,addressed_to_actor_id,
                               summary,created_at_ms)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (
                            note["workspace_id"], note["project_id"],
                            note["author_actor_id"], "note_chatgpt_analyzed",
                            "note", note["id"], note["id"],
                            note["author_actor_id"],
                            ("ChatGPT завершил анализ заметки: " + str(note["title"]))[:240],
                            now,
                        ),
                    )
                self.store.db.execute("COMMIT")
                return inserted
            except Exception:
                self.store.db.execute("ROLLBACK")
                raise

    async def _read(self, *, note: Any, path: str) -> dict[str, Any]:
        return await self.github.read_repository_path(
            actor_id=str(note["author_actor_id"]),
            workspace_id=str(note["workspace_id"]),
            repository_id=int(note["repository_id"]),
            path=path,
        )

    async def poll_once(self, *, force: bool = False) -> int:
        if not force and time.monotonic() - self._last_poll < self.interval_seconds:
            return 0
        self._last_poll = time.monotonic()
        with self.store._lock:
            rows = self.store.db.execute(
                """SELECT * FROM project_notes
                   WHERE status='ready' AND audience='project'
                   ORDER BY updated_at_ms DESC LIMIT 500"""
            ).fetchall()

        groups: dict[tuple[str, str, int], list[Any]] = {}
        for row in rows:
            key = (str(row["workspace_id"]), str(row["project_id"]), int(row["repository_id"]))
            groups.setdefault(key, []).append(row)
        imported = 0
        for group in list(groups.values())[:10]:
            # Acceptance canaries may leave historical notes by revoked actors.
            # Pick a currently authorized actor for the manifest read rather
            # than allowing the newest revoked author to block the whole group.
            example = None
            for candidate in group:
                try:
                    self.store.project_access(
                        str(candidate["author_actor_id"]),
                        str(candidate["workspace_id"]),
                        str(candidate["project_id"]),
                        require_role="editor",
                    )
                except StoreError:
                    continue
                example = candidate
                break
            if example is None:
                continue
            owner = str(example["author_actor_id"])
            ws = str(example["workspace_id"])
            project = str(example["project_id"])
            try:
                connections = self.store.list_repository_connections(owner, ws)
                connection = next(
                    (
                        item for item in connections
                        if int(item["repository_id"]) == int(example["repository_id"])
                        and item["project_id"] == project
                        and item.get("private") is True
                        and item.get("role") == "project_docs"
                        and item.get("access_mode") == "app_managed_write"
                        and item.get("state") == "available"
                    ),
                    None,
                )
                if connection is None:
                    continue
                manifest = await self._read(note=example, path=ROUTING_PATH)
                if manifest.get("kind") != "file" or not isinstance(manifest.get("text"), str):
                    continue
                routes = parse_routing_yaml(
                    str(manifest["text"]),
                    repository=str(example["repository_full_name"]),
                )
            except (StoreError, ValueError, TypeError, KeyError, AttributeError):
                continue

            by_id = {str(note["id"]): note for note in group}
            for route in routes[:20]:
                note = by_id.get(route["note_id"])
                if (
                    note is None
                    or str(note["project_id"]) != route["project_id"]
                    or str(note["repository_path"]) != route["source_path"]
                ):
                    continue
                try:
                    self.store.project_access(
                        str(note["author_actor_id"]), ws, project, require_role="editor"
                    )
                    source = await self._read(note=note, path=route["source_path"])
                    if source.get("kind") != "file" or not SHA_PATTERN.fullmatch(str(source.get("sha") or "")):
                        continue
                    result = await self._read(note=note, path=route["result_path"])
                    if result.get("kind") != "file":
                        continue
                    meta, body = parse_analysis_markdown(str(result.get("text") or ""))
                    if any((
                        meta["route_id"] != route["route_id"],
                        meta["note_id"] != route["note_id"],
                        meta["project_id"] != route["project_id"],
                        meta["source_path"] != route["source_path"],
                        meta["source_sha"] != source["sha"],
                    )):
                        continue
                    result_sha = str(result.get("sha") or "")
                    if not SHA_PATTERN.fullmatch(result_sha):
                        continue
                    # A note could be edited between the initial source GET
                    # and reading its companion result. Reject that TOCTOU
                    # race rather than importing an already-stale analysis.
                    source_readback = await self._read(
                        note=note, path=route["source_path"]
                    )
                    if source_readback.get("sha") != source["sha"]:
                        continue
                    if self._register(
                        note=note,
                        route=route,
                        source_sha=meta["source_sha"],
                        result_sha=result_sha,
                        generated_at=meta["generated_at_utc"],
                        body=body,
                    ):
                        imported += 1
                except (StoreError, ValueError, TypeError, KeyError, AttributeError):
                    continue
        return imported
