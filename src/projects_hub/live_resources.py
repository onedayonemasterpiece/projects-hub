"""Bind Projects Hub to shared Live admission without becoming an AI router."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Callable, Mapping

RESOURCE_ENV_NAMES = (
    "AI_RESOURCE_CONTROL_URL",
    "AI_RESOURCE_CONTROL_SERVICE_KEY",
    "GOOGLE_AI_LIMITER_SUPABASE_URL",
    "GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY",
    "AI_RESOURCE_LEDGER_ID",
)


def live_resource_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Expose only the shared authority contract plus this consumer's fallback.

    DevCoveer currently stores the canonical authority as AI_SUPABASE_URL /
    AI_SUPABASE_SECRET_KEY. Those host aliases are translated at this trusted
    backend boundary and are never forwarded under their source names.
    """

    result = {
        name: environment[name]
        for name in RESOURCE_ENV_NAMES
        if isinstance(environment.get(name), str) and environment[name].strip()
    }
    if "AI_RESOURCE_CONTROL_URL" not in result:
        host_url = str(environment.get("AI_SUPABASE_URL") or "").strip()
        if host_url:
            result["AI_RESOURCE_CONTROL_URL"] = host_url
    if "AI_RESOURCE_CONTROL_SERVICE_KEY" not in result:
        host_key = str(environment.get("AI_SUPABASE_SECRET_KEY") or "").strip()
        if host_key:
            result["AI_RESOURCE_CONTROL_SERVICE_KEY"] = host_key

    fallback = str(environment.get("GOOGLE_API_KEY4") or "").strip()
    if fallback:
        result["AI_RESOURCE_CONTROL_FALLBACK_KEY"] = fallback
    return result


def _valid_scope_part(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("invalid_scope")
    return value


@dataclass(frozen=True)
class ProjectScope:
    """Compatibility bootstrap. New Live product sessions use ConversationScope."""

    tenant_id: str
    subject_id: str
    project_id: str

    def __post_init__(self):
        for value in (self.tenant_id, self.subject_id, self.project_id):
            _valid_scope_part(value)

    def resource_binding(self) -> str:
        encoded = json.dumps(
            [self.tenant_id, self.subject_id, self.project_id], separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ConversationScope:
    """Actor/workspace/conversation resource accounting; project auth stays per tool."""

    workspace_id: str
    subject_id: str
    conversation_id: str

    def __post_init__(self):
        for value in (self.workspace_id, self.subject_id, self.conversation_id):
            _valid_scope_part(value)

    def resource_binding(self) -> str:
        encoded = json.dumps(
            [self.workspace_id, self.subject_id, self.conversation_id],
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


async def _run_guarded(
    *,
    binding: str,
    environment: Mapping[str, str],
    reader: Any,
    on_event: Callable[[dict], None],
) -> None:
    try:
        from ai_resource_control import run_guarded
    except ImportError:
        on_event(
            {
                "type": "error",
                "code": "RESOURCE_PACKAGE_MISSING",
                "message": "RESOURCE_PACKAGE_MISSING",
            }
        )
        return
    await run_guarded(
        consumer="projects-hub",
        environment=live_resource_environment(environment),
        reader=reader,
        on_event=on_event,
        binding=binding,
    )


async def run_project_dialogue(
    *,
    scope: ProjectScope,
    environment: Mapping[str, str],
    reader: Any,
    on_event: Callable[[dict], None],
) -> None:
    if not isinstance(scope, ProjectScope):
        raise ValueError("authorized_project_scope_required")
    await _run_guarded(
        binding=scope.resource_binding(),
        environment=environment,
        reader=reader,
        on_event=on_event,
    )


async def run_conversation_dialogue(
    *,
    scope: ConversationScope,
    environment: Mapping[str, str],
    reader: Any,
    on_event: Callable[[dict], None],
) -> None:
    if not isinstance(scope, ConversationScope):
        raise ValueError("authorized_conversation_scope_required")
    await _run_guarded(
        binding=scope.resource_binding(),
        environment=environment,
        reader=reader,
        on_event=on_event,
    )
