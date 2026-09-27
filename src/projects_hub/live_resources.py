"""No new voice architecture: bind the shared SDK to an authorized project."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Callable, Mapping

RESOURCE_ENV_NAMES = (
    'AI_RESOURCE_CONTROL_URL',
    'AI_RESOURCE_CONTROL_SERVICE_KEY',
    'GOOGLE_AI_LIMITER_SUPABASE_URL',
    'GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY',
    'AI_RESOURCE_LEDGER_ID',
    'GOOGLE_API_KEY4',
)

def live_resource_environment(environment: Mapping[str,str]) -> dict[str,str]:
    return {
        name: environment[name]
        for name in RESOURCE_ENV_NAMES
        if isinstance(environment.get(name), str) and environment[name].strip()
    }

@dataclass(frozen=True)
class ProjectScope:
    """Construct on the backend AFTER authenticating project access, not from a tool call."""
    tenant_id: str
    subject_id: str
    project_id: str

    def __post_init__(self):
        for value in (self.tenant_id,self.subject_id,self.project_id):
            if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value):
                raise ValueError('invalid_project_scope')

    def resource_binding(self) -> str:
        # Pseudonymous, NOT anonymous. Never send project text or credentials.
        encoded=json.dumps([self.tenant_id,self.subject_id,self.project_id],separators=(',',':')).encode()
        return hashlib.sha256(encoded).hexdigest()

async def run_project_dialogue(*, scope: ProjectScope, environment: Mapping[str,str],
                               reader: Any, on_event: Callable[[dict],None]) -> None:
    if not isinstance(scope,ProjectScope):
        raise ValueError('authorized_project_scope_required')
    try:
        from ai_resource_control import run_guarded
    except ImportError:
        on_event({'type':'error','code':'RESOURCE_PACKAGE_MISSING','message':'RESOURCE_PACKAGE_MISSING'})
        return
    await run_guarded(consumer='projects-hub',environment=live_resource_environment(environment),reader=reader,
                      on_event=on_event,binding=scope.resource_binding())
