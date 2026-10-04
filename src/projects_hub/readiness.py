from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .store import DurableStore, StoreError


EVENT_TEMPLATES: dict[str, list[tuple[str, str]]] = {
    "generic": [
        ("goal", "Цель и ожидаемый результат понятны"),
        ("materials", "Нужные материалы доступны"),
    ],
    "podcast": [
        ("questions", "Вопросы подготовлены"),
        ("intro", "Приветствие и вступление подготовлены"),
        ("appearance", "Определён внешний вид / одежда"),
        ("materials", "Нужные материалы доступны"),
    ],
}

TASK_STATES = {"proposed", "accepted", "done", "snoozed", "rejected"}


def _now_ms() -> int:
    return round(time.time() * 1000)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class ReadinessService:
    """Small deterministic event-readiness/task layer for the Android MVP."""

    def __init__(self, store: DurableStore) -> None:
        self.store = store
        with self.store._lock:
            self.store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS event_cards(
                    id TEXT PRIMARY KEY,
                    event_key TEXT NOT NULL UNIQUE,
                    actor_id TEXT NOT NULL REFERENCES actors(id),
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    project_id TEXT REFERENCES projects(id),
                    device_event_id TEXT,
                    title TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    checklist_json TEXT NOT NULL,
                    ready INTEGER NOT NULL DEFAULT 0,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS event_cards_actor_workspace_idx
                    ON event_cards(actor_id,workspace_id,starts_at,updated_at_ms DESC);
                CREATE TABLE IF NOT EXISTS tasks(
                    id TEXT PRIMARY KEY,
                    task_key TEXT NOT NULL UNIQUE,
                    actor_id TEXT NOT NULL REFERENCES actors(id),
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    project_id TEXT REFERENCES projects(id),
                    event_card_id TEXT REFERENCES event_cards(id),
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    assignee_role TEXT NOT NULL,
                    deadline TEXT,
                    kind TEXT NOT NULL DEFAULT 'follow_up',
                    state TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_actor_workspace_idx
                    ON tasks(actor_id,workspace_id,state,updated_at_ms DESC);
                """
            )
            columns = {
                str(row["name"])
                for row in self.store.db.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "kind" not in columns:
                self.store.db.execute(
                    "ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'follow_up'"
                )
                self.store.db.execute(
                    """UPDATE tasks SET kind='development'
                       WHERE event_card_id IS NULL AND project_id IS NOT NULL"""
                )

    def _authorize(
        self,
        actor_id: str,
        workspace_id: str,
        project_id: str | None,
    ) -> None:
        self.store._membership(actor_id, workspace_id)
        if project_id and not self.store._project_row(workspace_id, project_id):
            raise StoreError("PROJECT_NOT_FOUND", "Project is not available")

    @staticmethod
    def _checklist(event_type: str) -> list[dict[str, Any]]:
        template = EVENT_TEMPLATES.get(event_type, EVENT_TEMPLATES["generic"])
        return [
            {"key": key, "label": label, "done": False}
            for key, label in template
        ]

    @staticmethod
    def _decode_checklist(raw: str) -> list[dict[str, Any]]:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [
            {
                "key": str(item.get("key") or ""),
                "label": str(item.get("label") or ""),
                "done": bool(item.get("done")),
            }
            for item in parsed
            if isinstance(item, dict)
        ]

    def _task_public(self, row: Any) -> dict[str, Any]:
        return dict(row)

    def _event_public(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        item["ready"] = bool(item["ready"])
        item["checklist"] = self._decode_checklist(item.pop("checklist_json", "[]"))
        item["tasks"] = [
            self._task_public(task)
            for task in self.store.db.execute(
                """SELECT * FROM tasks
                   WHERE event_card_id=?
                   ORDER BY created_at_ms,id""",
                (item["id"],),
            ).fetchall()
        ]
        item["incomplete_count"] = sum(
            1 for checklist_item in item["checklist"] if not checklist_item["done"]
        )
        return item

    def record_calendar_event(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        project_id: str | None,
        command_id: str,
        title: str,
        starts_at: str,
        event_type: str,
        device_event_id: str | None,
    ) -> dict[str, Any]:
        clean_type = event_type if event_type in EVENT_TEMPLATES else "generic"
        clean_title = title.strip()[:160]
        if not clean_title:
            raise StoreError("INVALID_ARGUMENT", "Event title is required")
        event_key = f"calendar:{command_id}"
        now = _now_ms()
        with self.store._lock:
            self._authorize(actor_id, workspace_id, project_id)
            existing = self.store.db.execute(
                "SELECT * FROM event_cards WHERE event_key=?",
                (event_key,),
            ).fetchone()
            if existing:
                return self._event_public(existing)
            event_id = _id("evt")
            self.store.db.execute(
                """INSERT INTO event_cards(
                       id,event_key,actor_id,workspace_id,project_id,device_event_id,
                       title,starts_at,event_type,status,checklist_json,ready,
                       created_at_ms,updated_at_ms
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    event_key,
                    actor_id,
                    workspace_id,
                    project_id,
                    device_event_id,
                    clean_title,
                    starts_at,
                    clean_type,
                    "scheduled",
                    json.dumps(
                        self._checklist(clean_type),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    0,
                    now,
                    now,
                ),
            )
            return self._event_public(
                self.store.db.execute(
                    "SELECT * FROM event_cards WHERE id=?",
                    (event_id,),
                ).fetchone()
            )

    def list_event_cards(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        project_id: str | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 20))
        with self.store._lock:
            self._authorize(actor_id, workspace_id, project_id)
            if project_id:
                rows = self.store.db.execute(
                    """SELECT * FROM event_cards
                       WHERE actor_id=? AND workspace_id=? AND project_id=?
                       ORDER BY starts_at,updated_at_ms DESC LIMIT ?""",
                    (actor_id, workspace_id, project_id, bounded),
                ).fetchall()
            else:
                rows = self.store.db.execute(
                    """SELECT * FROM event_cards
                       WHERE actor_id=? AND workspace_id=?
                       ORDER BY starts_at,updated_at_ms DESC LIMIT ?""",
                    (actor_id, workspace_id, bounded),
                ).fetchall()
            return [self._event_public(row) for row in rows]

    def set_readiness(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        event_id: str,
        checklist_key: str,
        done: bool,
    ) -> dict[str, Any]:
        with self.store._lock:
            self._authorize(actor_id, workspace_id, None)
            row = self.store.db.execute(
                """SELECT * FROM event_cards
                   WHERE id=? AND actor_id=? AND workspace_id=?""",
                (event_id, actor_id, workspace_id),
            ).fetchone()
            if not row:
                raise StoreError("EVENT_NOT_FOUND", "Event card is not available")
            checklist = self._decode_checklist(row["checklist_json"])
            changed = False
            for item in checklist:
                if item["key"] == checklist_key:
                    item["done"] = bool(done)
                    changed = True
                    break
            if not changed:
                raise StoreError("INVALID_ARGUMENT", "Checklist item is not available")
            ready = bool(checklist) and all(item["done"] for item in checklist)
            now = _now_ms()
            self.store.db.execute(
                """UPDATE event_cards
                   SET checklist_json=?,ready=?,updated_at_ms=?
                   WHERE id=?""",
                (
                    json.dumps(checklist, ensure_ascii=False, separators=(",", ":")),
                    int(ready),
                    now,
                    event_id,
                ),
            )
            return self._event_public(
                self.store.db.execute(
                    "SELECT * FROM event_cards WHERE id=?",
                    (event_id,),
                ).fetchone()
            )

    def create_follow_up_task(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        project_id: str | None,
        event_id: str | None,
        command_id: str,
        title: str,
        description: str = "",
        assignee_role: str = "owner",
        deadline: str | None = None,
    ) -> dict[str, Any]:
        clean_title = title.strip()[:180]
        if not clean_title:
            raise StoreError("INVALID_ARGUMENT", "Task title is required")
        now = _now_ms()
        with self.store._lock:
            self._authorize(actor_id, workspace_id, project_id)
            if event_id:
                event = self.store.db.execute(
                    """SELECT id,project_id FROM event_cards
                       WHERE id=? AND actor_id=? AND workspace_id=?""",
                    (event_id, actor_id, workspace_id),
                ).fetchone()
                if not event:
                    raise StoreError("EVENT_NOT_FOUND", "Event card is not available")
                if project_id is None:
                    project_id = event["project_id"]
            existing = self.store.db.execute(
                "SELECT * FROM tasks WHERE task_key=?",
                (command_id,),
            ).fetchone()
            if existing:
                return self._task_public(existing)
            task_id = _id("tsk")
            self.store.db.execute(
                """INSERT INTO tasks(
                       id,task_key,actor_id,workspace_id,project_id,event_card_id,
                       title,description,assignee_role,deadline,kind,state,
                       created_at_ms,updated_at_ms
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    command_id,
                    actor_id,
                    workspace_id,
                    project_id,
                    event_id,
                    clean_title,
                    description.strip()[:2000],
                    assignee_role.strip()[:80] or "owner",
                    deadline,
                    "follow_up",
                    "proposed",
                    now,
                    now,
                ),
            )
            return self._task_public(
                self.store.db.execute(
                    "SELECT * FROM tasks WHERE id=?",
                    (task_id,),
                ).fetchone()
            )

    def set_task_state(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        task_id: str,
        state: str,
    ) -> dict[str, Any]:
        if state not in TASK_STATES:
            raise StoreError("INVALID_ARGUMENT", "Task state is invalid")
        now = _now_ms()
        with self.store._lock:
            self._authorize(actor_id, workspace_id, None)
            row = self.store.db.execute(
                """SELECT * FROM tasks
                   WHERE id=? AND actor_id=? AND workspace_id=?""",
                (task_id, actor_id, workspace_id),
            ).fetchone()
            if not row:
                raise StoreError("TASK_NOT_FOUND", "Task is not available")
            self.store.db.execute(
                "UPDATE tasks SET state=?,updated_at_ms=? WHERE id=?",
                (state, now, task_id),
            )
            return self._task_public(
                self.store.db.execute(
                    "SELECT * FROM tasks WHERE id=?",
                    (task_id,),
                ).fetchone()
            )

    def list_tasks(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        project_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 50))
        with self.store._lock:
            self._authorize(actor_id, workspace_id, project_id)
            if project_id:
                rows = self.store.db.execute(
                    """SELECT * FROM tasks
                       WHERE actor_id=? AND workspace_id=? AND project_id=?
                       ORDER BY updated_at_ms DESC LIMIT ?""",
                    (actor_id, workspace_id, project_id, bounded),
                ).fetchall()
            else:
                rows = self.store.db.execute(
                    """SELECT * FROM tasks
                       WHERE actor_id=? AND workspace_id=?
                       ORDER BY updated_at_ms DESC LIMIT ?""",
                    (actor_id, workspace_id, bounded),
                ).fetchall()
            return [self._task_public(row) for row in rows]
