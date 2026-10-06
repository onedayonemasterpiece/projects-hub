from __future__ import annotations

import math
import threading
import time
from typing import Any

from .board import BoardService
from .store import DurableStore, StoreError


MAX_CONTEXTS = 512
MAX_VISIBLE_IDS = 40
MAX_SELECTED_IDS = 4
MAX_TEXT_CHARS = 160
CONTEXT_TTL_MS = 90_000


def _now_ms() -> int:
    return round(time.time() * 1000)


def _finite(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise StoreError("INVALID_ARGUMENT", f"{field} must be finite") from exc
    if not math.isfinite(result):
        raise StoreError("INVALID_ARGUMENT", f"{field} must be finite")
    if abs(result) > 1_000_000:
        raise StoreError("INVALID_ARGUMENT", f"{field} is outside the supported board range")
    return result


def _ids(values: Any, *, limit: int) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > limit:
        raise StoreError("INVALID_ARGUMENT", "board view object ids are invalid")
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value or len(value) > 128:
            raise StoreError("INVALID_ARGUMENT", "board view object id is invalid")
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


class BoardViewContextStore:
    """Bounded per-tab ephemeral viewport state.

    Client payload contains only geometry/IDs. Canonical text, style and revisions
    are resolved again through BoardService before Mira sees the context.
    """

    def __init__(
        self,
        store: DurableStore,
        board: BoardService,
        *,
        ttl_ms: int = CONTEXT_TTL_MS,
        max_contexts: int = MAX_CONTEXTS,
    ) -> None:
        self.store = store
        self.board = board
        self.ttl_ms = max(5_000, int(ttl_ms))
        self.max_contexts = max(8, int(max_contexts))
        self._lock = threading.RLock()
        self._items: dict[tuple[str, str, str], dict[str, Any]] = {}

    @staticmethod
    def _key(actor_id: str, conversation_id: str, client_instance_id: str) -> tuple[str, str, str]:
        return (actor_id, conversation_id, client_instance_id)

    def _prune(self, now: int) -> None:
        expired = [
            key for key, value in self._items.items()
            if int(value.get("updated_at_ms") or 0) + self.ttl_ms <= now
        ]
        for key in expired:
            self._items.pop(key, None)
        if len(self._items) <= self.max_contexts:
            return
        ordered = sorted(
            self._items.items(),
            key=lambda pair: int(pair[1].get("updated_at_ms") or 0),
        )
        for key, _value in ordered[: len(self._items) - self.max_contexts]:
            self._items.pop(key, None)

    def update(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        conversation_id: str,
        project_id: str,
        board_id: str,
        client_instance_id: str,
        board_seq: int,
        camera: dict[str, Any],
        visible_object_ids: Any,
        selected_object_ids: Any = None,
        focused_object_id: str | None = None,
    ) -> dict[str, Any]:
        conversation = self.store.get_conversation(actor_id, conversation_id)
        if conversation["workspace_id"] != workspace_id:
            raise StoreError("FORBIDDEN", "Conversation workspace mismatch")
        if conversation.get("focus_project_id") != project_id:
            raise StoreError("FORBIDDEN", "Board view project is not the active conversation project")
        board = self.board.snapshot(actor_id, workspace_id, board_id)
        if board["board"]["project_id"] != project_id:
            raise StoreError("FORBIDDEN", "Board view scope mismatch")
        clean_client = str(client_instance_id or "").strip()
        if not 8 <= len(clean_client) <= 128:
            raise StoreError("INVALID_ARGUMENT", "client_instance_id is invalid")
        try:
            clean_seq = max(0, int(board_seq))
        except (TypeError, ValueError) as exc:
            raise StoreError("INVALID_ARGUMENT", "board_seq is invalid") from exc
        if not isinstance(camera, dict):
            raise StoreError("INVALID_ARGUMENT", "camera is required")
        clean_camera = {
            "x": _finite(camera.get("x"), "camera.x"),
            "y": _finite(camera.get("y"), "camera.y"),
            "zoom": _finite(camera.get("zoom"), "camera.zoom"),
            "width": _finite(camera.get("width"), "camera.width"),
            "height": _finite(camera.get("height"), "camera.height"),
        }
        if not 0.05 <= clean_camera["zoom"] <= 10:
            raise StoreError("INVALID_ARGUMENT", "camera.zoom is outside the supported range")
        if clean_camera["width"] <= 0 or clean_camera["height"] <= 0:
            raise StoreError("INVALID_ARGUMENT", "camera viewport dimensions must be positive")

        visible = _ids(visible_object_ids, limit=MAX_VISIBLE_IDS)
        selected = _ids(selected_object_ids, limit=MAX_SELECTED_IDS)
        focused = str(focused_object_id or "").strip() or None
        if focused is not None and len(focused) > 128:
            raise StoreError("INVALID_ARGUMENT", "focused_object_id is invalid")
        now = _now_ms()
        value = {
            "workspace_id": workspace_id,
            "conversation_id": conversation_id,
            "project_id": project_id,
            "board_id": board_id,
            "client_instance_id": clean_client,
            "board_seq": clean_seq,
            "camera": clean_camera,
            "visible_object_ids": visible,
            "selected_object_ids": selected,
            "focused_object_id": focused,
            "updated_at_ms": now,
        }
        with self._lock:
            self._prune(now)
            self._items[self._key(actor_id, conversation_id, clean_client)] = value
        return {
            "ok": True,
            "board_id": board_id,
            "board_seq": clean_seq,
            "client_instance_id": clean_client,
            "visible_count": len(visible),
            "updated_at_ms": now,
        }

    def resolve(
        self,
        *,
        actor_id: str,
        workspace_id: str,
        conversation_id: str,
        project_id: str,
        client_instance_id: str,
    ) -> dict[str, Any]:
        now = _now_ms()
        key = self._key(actor_id, conversation_id, client_instance_id)
        with self._lock:
            self._prune(now)
            value = dict(self._items.get(key) or {})
        if not value:
            return {
                "status": "unavailable",
                "reason": "no_current_view_context",
                "project_id": project_id,
                "client_instance_id": client_instance_id,
            }
        if value["workspace_id"] != workspace_id or value["project_id"] != project_id:
            raise StoreError("FORBIDDEN", "Board view context scope mismatch")
        conversation = self.store.get_conversation(actor_id, conversation_id)
        if conversation.get("focus_project_id") != project_id:
            raise StoreError("FORBIDDEN", "Board view project is not active")
        snapshot = self.board.snapshot(actor_id, workspace_id, value["board_id"])
        by_id = {item["id"]: item for item in snapshot["objects"]}

        ordered_ids: list[str] = []
        for object_id in (
            list(value.get("selected_object_ids") or [])
            + ([value["focused_object_id"]] if value.get("focused_object_id") else [])
            + list(value.get("visible_object_ids") or [])
        ):
            if object_id and object_id not in ordered_ids:
                ordered_ids.append(object_id)

        objects: list[dict[str, Any]] = []
        stale_ids: list[str] = []
        for object_id in ordered_ids[:MAX_VISIBLE_IDS]:
            item = by_id.get(object_id)
            if item is None:
                stale_ids.append(object_id)
                continue
            objects.append(
                {
                    "id": item["id"],
                    "type": item["type"],
                    "object_revision": int(item["object_revision"]),
                    "text": str(item.get("text") or "")[:MAX_TEXT_CHARS],
                    "color": str((item.get("style") or {}).get("color") or ""),
                    "bbox": dict(item["geometry"]),
                    "selected": object_id in set(value.get("selected_object_ids") or []),
                    "focused": object_id == value.get("focused_object_id"),
                }
            )
        return {
            "status": "current",
            "project_id": project_id,
            "board_id": value["board_id"],
            "client_instance_id": client_instance_id,
            "reported_board_seq": int(value["board_seq"]),
            "current_board_seq": int(snapshot["board"]["seq"]),
            "camera": dict(value["camera"]),
            "objects": objects,
            "stale_object_ids": stale_ids,
            "truncated": len(ordered_ids) > MAX_VISIBLE_IDS,
            "age_ms": max(0, now - int(value["updated_at_ms"])),
        }

    def clear_session(
        self, *, actor_id: str, conversation_id: str, client_instance_id: str
    ) -> None:
        with self._lock:
            self._items.pop(self._key(actor_id, conversation_id, client_instance_id), None)
