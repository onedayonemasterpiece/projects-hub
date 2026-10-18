"""Read-only, personal result feed for the existing paired Android scheduler."""
from __future__ import annotations

import time
from typing import Any
from .store import StoreError


def recent_analysis_completions(service: Any, *, actor_id: str, workspace_id: str) -> list[dict[str, Any]]:
    store = service.store
    store._membership(actor_id, workspace_id)
    with store._lock:
        rows = store.db.execute(
            """SELECT e.id,e.object_id,e.created_at_ms,p.name AS project_name
               FROM collaboration_events e
               JOIN projects p ON p.id=e.project_id
               JOIN project_grants g ON g.project_id=e.project_id AND g.actor_id=?
               WHERE e.workspace_id=? AND e.addressed_to_actor_id=?
                 AND e.kind='question_result_ready' AND e.object_kind='analysis'
                 AND g.revoked_at_ms IS NULL AND e.created_at_ms>=?
               ORDER BY e.id DESC LIMIT 20""",
            (actor_id, workspace_id, actor_id, int(time.time() * 1000) - 7 * 86400000),
        ).fetchall()
        items = []
        for row in rows:
            try:
                analysis = service.get_analysis(actor_id=actor_id, workspace_id=workspace_id,
                                                analysis_id=row["object_id"])
            except StoreError:
                # Revoked source/project access must not leak through an old event.
                continue
            result = analysis.get("result") or {}
            if not analysis.get("result_markdown") or not result.get("artifact_sha"):
                continue
            items.append({"id": "analysis_event_" + str(row["id"]),
                          "analysis_id": row["object_id"], "title": row["project_name"],
                          "finished_at_ms": row["created_at_ms"]})
        return items
