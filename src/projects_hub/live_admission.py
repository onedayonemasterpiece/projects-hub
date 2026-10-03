from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any


def _actor_id(actor: Any) -> str:
    if isinstance(actor, dict):
        return str(actor.get("subject") or actor.get("id") or "")
    return str(getattr(actor, "subject", "") or getattr(actor, "id", "") or "")


def _session_actor_id(session: Any) -> str:
    state = getattr(session, "state", None)
    if isinstance(state, dict) and state.get("actor_id"):
        return str(state["actor_id"])
    return _actor_id(getattr(session, "actor", None))


class ProjectsHubAdmissionMixin:
    """Product admission only; WSS transport/lifecycle stays in live-interaction."""

    def __init__(self, *args: Any, max_sessions_per_actor: int = 2, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not isinstance(max_sessions_per_actor, int) or max_sessions_per_actor < 1:
            raise ValueError("max_sessions_per_actor must be positive")
        self.max_sessions_per_actor = max_sessions_per_actor
        self._projects_hub_admission_lock = asyncio.Lock()
        self._projects_hub_starting_by_actor: Counter[str] = Counter()
        self._projects_hub_starting_resources: set[tuple[str, str]] = set()
        self._projects_hub_starting_sources: set[tuple[str, str]] = set()

    async def start(
        self,
        *,
        resource_id: str,
        actor: Any,
        client_source_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        from live_interaction import LiveError

        actor_id = _actor_id(actor)
        if not actor_id:
            raise LiveError("INVALID_ARGUMENT", "Live actor identity is required")
        resource_key = (actor_id, resource_id)
        source_key = (actor_id, client_source_id) if client_source_id else None

        async with self._projects_hub_admission_lock:
            active_for_actor = sum(
                not session.closed and _session_actor_id(session) == actor_id
                for session in self.sessions.values()
            )
            starting_for_actor = self._projects_hub_starting_by_actor[actor_id]
            if active_for_actor + starting_for_actor >= self.max_sessions_per_actor:
                raise LiveError("LIVE_BUSY", "Live session limit reached for this actor")

            resource_active = any(
                not session.closed
                and _session_actor_id(session) == actor_id
                and getattr(session, "resource_id", None) == resource_id
                for session in self.sessions.values()
            )
            if resource_active or resource_key in self._projects_hub_starting_resources:
                raise LiveError(
                    "LIVE_BUSY",
                    "This conversation already has an active Live session",
                )

            if source_key is not None:
                source_active = any(
                    not session.closed
                    and _session_actor_id(session) == actor_id
                    and isinstance(getattr(session, "state", None), dict)
                    and session.state.get("client_source_id") == client_source_id
                    for session in self.sessions.values()
                )
                if source_active or source_key in self._projects_hub_starting_sources:
                    raise LiveError("LIVE_BUSY", "This buffered source is already being replayed")

            self._projects_hub_starting_by_actor[actor_id] += 1
            self._projects_hub_starting_resources.add(resource_key)
            if source_key is not None:
                self._projects_hub_starting_sources.add(source_key)

        try:
            return await super().start(
                resource_id=resource_id,
                actor=actor,
                client_source_id=client_source_id,
                **kwargs,
            )
        finally:
            async with self._projects_hub_admission_lock:
                remaining = self._projects_hub_starting_by_actor[actor_id] - 1
                if remaining > 0:
                    self._projects_hub_starting_by_actor[actor_id] = remaining
                else:
                    self._projects_hub_starting_by_actor.pop(actor_id, None)
                self._projects_hub_starting_resources.discard(resource_key)
                if source_key is not None:
                    self._projects_hub_starting_sources.discard(source_key)
