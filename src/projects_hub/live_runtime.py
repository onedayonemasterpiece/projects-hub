from __future__ import annotations

import os
from typing import Any

from .live_adapter import ProjectsHubLiveAdapter
from .live_resources import live_resource_environment
from .store import DurableStore


def build_live_host(store: DurableStore, *, environment: dict[str, str] | None = None) -> Any:
    """Build the canonical shared host lazily so offline store/API tests need no provider SDK."""

    try:
        from live_interaction.session_host import LiveSessionHost
        from live_interaction.provider import run as provider_run
    except ImportError as exc:
        raise RuntimeError("LIVE_INTERACTION_PACKAGE_MISSING") from exc

    env = dict(os.environ if environment is None else environment)

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

    return LiveSessionHost(
        adapter_factory=lambda **shared: ProjectsHubLiveAdapter(store, **shared),
        managed_runner=managed_runner,
        max_sessions=4,
        client_liveness_timeout_ms=75_000,
    )
