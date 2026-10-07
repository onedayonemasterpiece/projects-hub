from __future__ import annotations

import logging
import os
from typing import Any, Callable

from .device_commands import DeviceCommandService
from .development import DevelopmentService
from .expert_reviews import ExpertReviewAdapter
from .github_connections import GitHubConnections
from .collaboration import CollaborationService
from .collaboration_analysis import CollaborationAnalysisService
from .board import BoardService
from .board_view_context import BoardViewContextStore
from .regional_knowledge import RegionalKnowledgeAdapter
from .live_adapter import ProjectsHubLiveAdapter
from .live_admission import ProjectsHubAdmissionMixin
from .live_resources import live_resource_environment
from .live_transcription import CaptionSidecar
from .readiness import ReadinessService
from .store import DurableStore

log = logging.getLogger("projects_hub.live")


def _live_max_sessions(environment: dict[str, str]) -> int:
    raw = str(environment.get("PROJECTS_HUB_LIVE_MAX_SESSIONS") or "16").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("PROJECTS_HUB_LIVE_MAX_SESSIONS must be an integer") from exc
    if not 1 <= value <= 64:
        raise RuntimeError("PROJECTS_HUB_LIVE_MAX_SESSIONS must be between 1 and 64")
    return value


def _live_max_sessions_per_actor(environment: dict[str, str], global_max: int) -> int:
    raw = str(
        environment.get("PROJECTS_HUB_LIVE_MAX_SESSIONS_PER_ACTOR")
        or min(2, global_max)
    ).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            "PROJECTS_HUB_LIVE_MAX_SESSIONS_PER_ACTOR must be an integer"
        ) from exc
    if not 1 <= value <= global_max:
        raise RuntimeError(
            "PROJECTS_HUB_LIVE_MAX_SESSIONS_PER_ACTOR must be between 1 "
            "and PROJECTS_HUB_LIVE_MAX_SESSIONS"
        )
    return value


def build_live_host(
    store: DurableStore,
    *,
    environment: dict[str, str] | None = None,
    board: BoardService | None = None,
    board_hub: Any | None = None,
    board_view_context: BoardViewContextStore | None = None,
    device_commands: DeviceCommandService | None = None,
    readiness: ReadinessService | None = None,
    development: DevelopmentService | None = None,
    github_connections: GitHubConnections | None = None,
    collaboration: CollaborationService | None = None,
    collaboration_analysis: CollaborationAnalysisService | None = None,
    expert_reviews_factory: (
        Callable[[str, str], ExpertReviewAdapter | None] | None
    ) = None,
    regional_knowledge_factory: (
        Callable[[str, str], RegionalKnowledgeAdapter | None] | None
    ) = None,
) -> Any:
    """Build the canonical shared host lazily so offline store/API tests need no provider SDK."""

    try:
        from live_interaction import LiveSocketSessionHost
        from live_interaction.provider import run as provider_run
        from live_interaction.transcribe import run as transcribe_run
    except ImportError as exc:
        raise RuntimeError("LIVE_INTERACTION_PACKAGE_MISSING") from exc

    env = dict(os.environ if environment is None else environment)
    max_sessions = _live_max_sessions(env)
    max_sessions_per_actor = _live_max_sessions_per_actor(env, max_sessions)
    resource_environment = live_resource_environment(env)
    device_commands = device_commands or DeviceCommandService(store)
    readiness = readiness or ReadinessService(store)
    development = development or DevelopmentService(store, readiness)

    async def managed_runner(*, session: Any, reader: Any, on_event: Any) -> None:
        try:
            from ai_resource_control import run_guarded
        except ImportError as exc:
            raise RuntimeError("RESOURCE_PACKAGE_MISSING") from exc
        await run_guarded(
            consumer="projects-hub",
            environment=resource_environment,
            reader=reader,
            on_event=on_event,
            provider_run=provider_run,
            binding=session.resource_id,
        )

    class ProjectsHubLiveSocketSessionHost(
        ProjectsHubAdmissionMixin,
        LiveSocketSessionHost,
    ):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._caption_sidecars: dict[str, CaptionSidecar] = {}
            super().__init__(*args, **kwargs)

        def _caption_emit(self, session: Any, event: dict[str, Any]) -> None:
            if session.closed or self.sessions.get(session.id) is not session:
                return
            kind = str(event.get("type") or "")
            text = event.get("text") if isinstance(event.get("text"), str) else None
            log.info(
                "live caption event",
                extra={
                    "event": "live_caption_event",
                    "session_id": session.id,
                    "conversation_id": session.state.get("conversation_id"),
                    "source_id": session.state.get("source_id"),
                    "kind": kind,
                    "code": str(event.get("code") or "")[:80] or None,
                    "text_length": len(text) if text is not None else 0,
                },
            )
            self._emit(session, event)

        async def start(self, *, resource_id: str, actor: Any, **kwargs: Any) -> dict[str, Any]:
            result = await super().start(resource_id=resource_id, actor=actor, **kwargs)
            session = self._get(result["session_id"], resource_id, actor)
            if (
                session.state.get("audio_mode") == "realtime"
                and not session.state.get("recovery_only")
            ):
                sidecar = CaptionSidecar(
                    environment=resource_environment,
                    binding=f"{resource_id}:caption:{session.id}",
                    vocabulary=list(session.state.get("caption_vocabulary") or []),
                    emit=lambda event, active=session: self._caption_emit(active, event),
                    provider_run=transcribe_run,
                )
                self._caption_sidecars[session.id] = sidecar
                sidecar.start()
            return {
                **result,
                "caption_enabled": session.id in self._caption_sidecars,
                "caption_model": "gemini-3.5-transcribe-live"
                if session.id in self._caption_sidecars
                else None,
            }

        async def input(
            self,
            *,
            session_id: str,
            resource_id: str,
            message: dict[str, Any],
            actor: Any = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            result = await super().input(
                session_id=session_id,
                resource_id=resource_id,
                message=message,
                actor=actor,
                **kwargs,
            )
            sidecar = self._caption_sidecars.get(session_id)
            if sidecar is not None:
                sidecar.feed(message)
            return result

        async def _discard(self, session: Any, graceful: bool = False) -> None:
            sidecar = self._caption_sidecars.pop(session.id, None)
            if sidecar is not None:
                await sidecar.stop()
            await super()._discard(session, graceful=graceful)

    def live_diagnostic(record: dict[str, Any]) -> None:
        safe = {
            key: value
            for key, value in record.items()
            if key in {
                "event",
                "session_id",
                "attempt_id",
                "code",
                "connection_generation",
                "frame_seq",
                "pcm_bytes",
                "capture_age_ms",
                "audio_turn_open",
                "duration_ms",
            }
            and isinstance(value, (str, int, float, bool))
        }
        if str(safe.get("event") or "") == "socket_audio_accepted":
            safe["latency_stage"] = "capture_to_server"
            capture_age = safe.get("capture_age_ms")
            duration = safe.get("duration_ms")
            latency_alert = (
                isinstance(capture_age, (int, float))
                and not isinstance(capture_age, bool)
                and capture_age >= 1000
            ) or (
                isinstance(duration, (int, float))
                and not isinstance(duration, bool)
                and duration >= 250
            )
            safe["latency_alert"] = latency_alert
            (log.warning if latency_alert else log.info)(
                "live socket diagnostic",
                extra=safe,
            )
            return
        log.info("live socket diagnostic", extra=safe)

    return ProjectsHubLiveSocketSessionHost(
        diagnostic=live_diagnostic,
        adapter_factory=lambda **shared: ProjectsHubLiveAdapter(
            store,
            board=board,
            board_hub=board_hub,
            board_view_context=board_view_context,
            device_commands=device_commands,
            readiness=readiness,
            development=development,
            github_connections=github_connections,
            collaboration=collaboration,
            collaboration_analysis=collaboration_analysis,
            expert_reviews_factory=expert_reviews_factory,
            regional_knowledge_factory=regional_knowledge_factory,
            **shared,
        ),
        managed_runner=managed_runner,
        max_sessions=max_sessions,
        max_sessions_per_actor=max_sessions_per_actor,
        client_liveness_timeout_ms=30_000,
    )