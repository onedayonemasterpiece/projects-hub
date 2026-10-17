from __future__ import annotations

import asyncio
import hashlib
import json
import time
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

    def __init__(
        self,
        *args: Any,
        max_sessions_per_actor: int = 2,
        detached_session_reclaim_ms: int = 15_000,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if not isinstance(max_sessions_per_actor, int) or max_sessions_per_actor < 1:
            raise ValueError("max_sessions_per_actor must be positive")
        self.max_sessions_per_actor = max_sessions_per_actor
        if not isinstance(detached_session_reclaim_ms, int) or detached_session_reclaim_ms < 1:
            raise ValueError("detached_session_reclaim_ms must be positive")
        self.detached_session_reclaim_ms = detached_session_reclaim_ms
        self._projects_hub_admission_lock = asyncio.Lock()
        self._projects_hub_starting_by_actor: Counter[str] = Counter()
        self._projects_hub_starting_resources: set[tuple[str, str]] = set()
        self._projects_hub_starting_sources: set[tuple[str, str]] = set()
        self._projects_hub_start_attempts: dict[tuple[str, str, str, str], tuple[str, asyncio.Task, float]] = {}
        self._projects_hub_closing = False

    async def _reclaim_detached_sessions(self, actor_id: str, resource_id: str) -> None:
        socket_states = getattr(self, "_socket_states", None)
        if not isinstance(socket_states, dict):
            return

        now_ms = round(time.time() * 1000)
        candidates: list[str] = []
        async with self._projects_hub_admission_lock:
            for session in tuple(self.sessions.values()):
                if session.closed or _session_actor_id(session) != actor_id:
                    continue
                state = socket_states.get(session.id)
                if state is None or getattr(state, "claim", None) is not None:
                    continue
                last_client_at_ms = int(getattr(session, "last_client_at_ms", 0) or 0)
                age_ms = max(0, now_ms - last_client_at_ms)
                same_resource = getattr(session, "resource_id", None) == resource_id
                replaceable_same_resource = same_resource and bool(
                    getattr(state, "used_wss", False)
                )
                if replaceable_same_resource or age_ms >= self.detached_session_reclaim_ms:
                    candidates.append(session.id)

        for session_id in candidates:
            # Re-check immediately before the stop so a successful WSS reconnect
            # wins over stale-session cleanup. A new /sessions POST for the same
            # resource replaces an abandoned transport, preserving its checkpoint;
            # browser reconnects use the socket-ticket endpoint instead.
            session = self.sessions.get(session_id)
            state = socket_states.get(session_id)
            if (
                session is None
                or session.closed
                or _session_actor_id(session) != actor_id
                or state is None
                or getattr(state, "claim", None) is not None
            ):
                continue
            age_ms = max(0, round(time.time() * 1000) - int(session.last_client_at_ms or 0))
            same_resource = getattr(session, "resource_id", None) == resource_id
            replaceable_same_resource = same_resource and bool(
                getattr(state, "used_wss", False)
            )
            if not replaceable_same_resource and age_ms < self.detached_session_reclaim_ms:
                continue
            diagnostic = getattr(self, "diagnostic", None)
            if callable(diagnostic):
                diagnostic(session, "detached_session_reclaimed", duration_ms=age_ms)
            await super().stop(
                session_id=session.id,
                resource_id=session.resource_id,
                actor=session.actor,
                reason="detached_reclaim",
            )

    async def start(
        self,
        *,
        resource_id: str,
        actor: Any,
        attempt_id: str | None = None,
        client_instance_id: str | None = None,
        client_source_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Reconcile a lost native bootstrap response without another provider.

        The explicit attempt belongs to an authenticated actor, conversation and
        client instance. A HTTP waiter may disappear; its bounded shared start
        continues and the same request can recover that one result and a fresh
        one-use ticket. An incompatible body cannot reuse the attempt identity.
        """
        from live_interaction import LiveError

        if self._projects_hub_closing:
            raise LiveError("LIVE_SESSION_CLOSED", "Live host is stopping")
        if not attempt_id or not client_instance_id:
            return await self._start_new(resource_id=resource_id, actor=actor,
                                         attempt_id=attempt_id, client_instance_id=client_instance_id,
                                         client_source_id=client_source_id, **kwargs)
        if (not isinstance(attempt_id, str) or not isinstance(client_instance_id, str)
                or len(attempt_id) > 96 or len(client_instance_id) > 128):
            raise LiveError("INVALID_ARGUMENT", "Invalid Live start identity")
        actor_id = _actor_id(actor)
        if not actor_id:
            raise LiveError("INVALID_ARGUMENT", "Live actor identity is required")
        key = (actor_id, resource_id, client_instance_id, attempt_id)
        fingerprint = hashlib.sha256(json.dumps(
            {"client_source_id": client_source_id,
             **{name: value for name, value in kwargs.items()
                if name not in {"history", "backend_version", "backend_release_sha"}}},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        async with self._projects_hub_admission_lock:
            now = time.monotonic()
            for prior_key, (_, prior_task, expires) in tuple(self._projects_hub_start_attempts.items()):
                # Keep the completed result, including failures and closed
                # sessions, for the bounded retry lifetime. In particular, a
                # delayed duplicate must not turn an explicit Stop into Start.
                if prior_task.done() and expires <= now:
                    self._projects_hub_start_attempts.pop(prior_key, None)
            prior = self._projects_hub_start_attempts.get(key)
            if prior and prior[0] != fingerprint:
                raise LiveError("LIVE_ATTEMPT_CONFLICT", "A Live start attempt cannot change its request")
            repeated = prior is not None
            if prior:
                task = prior[1]
            else:
                # Pending starts and completed retry receipts have independent
                # bounds. Capacity pressure never silently evicts a Stop fence.
                pending = sum(not value[1].done() for value in self._projects_hub_start_attempts.values())
                if (pending >= self.max_sessions * 2
                        or len(self._projects_hub_start_attempts) >= max(128, self.max_sessions * 64)):
                    raise LiveError("LIVE_BUSY", "Live start reconciliation capacity reached")
                task = asyncio.create_task(self._start_new(
                    resource_id=resource_id, actor=actor, attempt_id=attempt_id,
                    client_instance_id=client_instance_id, client_source_id=client_source_id, **kwargs,
                ), name="projects-hub-live-start")
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                self._projects_hub_start_attempts[key] = (fingerprint, task, now + 24 * 60 * 60)
        result = await asyncio.shield(task)
        active = self.sessions.get(result["session_id"])
        if active is None or active.closed or active.stop_reason == "explicit_stop":
            raise LiveError("LIVE_ATTEMPT_CLOSED", "This Live start attempt has ended; a new Start requires a new identity")
        if repeated:
            ticket = self.issue_socket_ticket(session_id=result["session_id"], resource_id=resource_id, actor=actor)
            return {**result, **ticket, "start_reconciled": True}
        return result

    async def stop_all(self) -> None:
        self._projects_hub_closing = True
        pending = [task for _, task, _ in self._projects_hub_start_attempts.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._projects_hub_start_attempts.clear()
        await super().stop_all()

    async def _start_new(
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

        await self._reclaim_detached_sessions(actor_id, resource_id)

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
