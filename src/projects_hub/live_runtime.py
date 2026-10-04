from __future__ import annotations

import os
from typing import Any, Callable

from .device_commands import DeviceCommandService
from .expert_reviews import ExpertReviewAdapter
from .github_connections import GitHubConnections
from .regional_knowledge import RegionalKnowledgeAdapter
from .live_adapter import ProjectsHubLiveAdapter
from .live_admission import ProjectsHubAdmissionMixin
from .live_resources import live_resource_environment
from .readiness import ReadinessService
from .store import DurableStore


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
    device_commands: DeviceCommandService | None = None,
    readiness: ReadinessService | None = None,
    github_connections: GitHubConnections | None = None,
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
    except ImportError as exc:
        raise RuntimeError("LIVE_INTERACTION_PACKAGE_MISSING") from exc

    env = dict(os.environ if environment is None else environment)
    max_sessions = _live_max_sessions(env)
    max_sessions_per_actor = _live_max_sessions_per_actor(env, max_sessions)
    device_commands = device_commands or DeviceCommandService(store)
    readiness = readiness or ReadinessService(store)

    async def managed_runner(*, session: Any, reader: Any, on_event: Any) -> None:
        try:
            from ai_resource_control import run_guarded
        except ImportError as exc:
            raise RuntimeError("RESOURCE_PACKAGE_MISSING") from exc
        await run_guarded(
            consumer="projects-hub",
            environment=live_resource_environment(env),
            reader=reader,
            on_event=on_event,
            provider_run=provider_run,
            binding=session.resource_id,
        )

    class ProjectsHubLiveSocketSessionHost(
        ProjectsHubAdmissionMixin,
        LiveSocketSessionHost,
    ):
        pass

    return ProjectsHubLiveSocketSessionHost(
        adapter_factory=lambda **shared: ProjectsHubLiveAdapter(
            store,
            device_commands=device_commands,
            readiness=readiness,
            github_connections=github_connections,
            expert_reviews_factory=expert_reviews_factory,
            regional_knowledge_factory=regional_knowledge_factory,
            **shared,
        ),
        managed_runner=managed_runner,
        max_sessions=max_sessions,
        max_sessions_per_actor=max_sessions_per_actor,
        client_liveness_timeout_ms=75_000,
    )