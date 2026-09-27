import sys
import types

import pytest

from projects_hub import ConversationScope, ProjectScope, run_project_dialogue
from projects_hub.live_resources import live_resource_environment


@pytest.mark.parametrize("value", ["", "../other", "a/b", "x" * 129, None])
def test_invalid_scope(value):
    with pytest.raises(ValueError):
        ProjectScope("tenant", "subject", value)


def test_binding_stable_and_isolated():
    a = ProjectScope("t", "u", "one")
    b = ProjectScope("t", "u", "two")
    c = ProjectScope("other", "u", "one")
    assert a.resource_binding() == a.resource_binding()
    assert len({a.resource_binding(), b.resource_binding(), c.resource_binding()}) == 3
    assert len(a.resource_binding()) == 64

    conv_a = ConversationScope("ws", "u", "conversation-one")
    conv_b = ConversationScope("ws", "u", "conversation-two")
    assert conv_a.resource_binding() != conv_b.resource_binding()
    assert len(conv_a.resource_binding()) == 64


@pytest.mark.asyncio
async def test_shared_controller_consumer_and_server_binding(monkeypatch):
    calls = []

    async def run_guarded(**kwargs):
        calls.append(kwargs)

    monkeypatch.setitem(
        sys.modules, "ai_resource_control", types.SimpleNamespace(run_guarded=run_guarded)
    )
    scope = ProjectScope("tenant", "user", "project")
    reader = object()
    events = []
    env = {
        "AI_RESOURCE_CONTROL_URL": "https://authority.example",
        "AI_RESOURCE_CONTROL_SERVICE_KEY": "service",
        "GOOGLE_API_KEY4": "fallback-four",
        "GOOGLE_API_KEY": "wrong",
        "UNRELATED_SECRET": "wrong",
    }
    await run_project_dialogue(
        scope=scope, environment=env, reader=reader, on_event=events.append
    )
    assert len(calls) == 1 and calls[0]["consumer"] == "projects-hub"
    assert calls[0]["binding"] == scope.resource_binding()
    assert calls[0]["reader"] is reader
    assert calls[0]["environment"] == {
        "AI_RESOURCE_CONTROL_URL": "https://authority.example",
        "AI_RESOURCE_CONTROL_SERVICE_KEY": "service",
        "AI_RESOURCE_CONTROL_FALLBACK_KEY": "fallback-four",
    }
    assert "load_key" not in calls[0]


def test_resource_environment_maps_devcoveer_authority_aliases_without_forwarding_them():
    result = live_resource_environment(
        {
            "AI_SUPABASE_URL": "https://authority.example",
            "AI_SUPABASE_SECRET_KEY": "service",
            "GOOGLE_API_KEY4": "four",
            "SUPABASE_URL": "product-db",
            "SUPABASE_KEY": "product-key",
        }
    )
    assert result == {
        "AI_RESOURCE_CONTROL_URL": "https://authority.example",
        "AI_RESOURCE_CONTROL_SERVICE_KEY": "service",
        "AI_RESOURCE_CONTROL_FALLBACK_KEY": "four",
    }


def test_explicit_shared_authority_names_win_over_host_aliases():
    result = live_resource_environment(
        {
            "AI_RESOURCE_CONTROL_URL": "https://explicit.example",
            "AI_RESOURCE_CONTROL_SERVICE_KEY": "explicit-service",
            "AI_SUPABASE_URL": "https://host.example",
            "AI_SUPABASE_SECRET_KEY": "host-service",
        }
    )
    assert result == {
        "AI_RESOURCE_CONTROL_URL": "https://explicit.example",
        "AI_RESOURCE_CONTROL_SERVICE_KEY": "explicit-service",
    }


def test_resource_environment_never_borrows_other_consumer_fallbacks():
    result = live_resource_environment(
        {
            "AI_RESOURCE_CONTROL_URL": "https://authority.example",
            "AI_RESOURCE_CONTROL_SERVICE_KEY": "service",
            "GOOGLE_API_KEY": "one",
            "GOOGLE_API_KEY2": "two",
            "GOOGLE_API_KEY3": "three",
            "GOOGLE_API_KEY4": "four",
            "GOOGLE_API_KEY5": "five",
        }
    )
    assert result == {
        "AI_RESOURCE_CONTROL_URL": "https://authority.example",
        "AI_RESOURCE_CONTROL_SERVICE_KEY": "service",
        "AI_RESOURCE_CONTROL_FALLBACK_KEY": "four",
    }


@pytest.mark.asyncio
async def test_missing_package_has_no_direct_key_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, "ai_resource_control", None)
    events = []
    await run_project_dialogue(
        scope=ProjectScope("t", "u", "p"),
        environment={"LIVE_API_KEY": "fixture"},
        reader=object(),
        on_event=events.append,
    )
    assert events[0]["code"] == "RESOURCE_PACKAGE_MISSING"


@pytest.mark.asyncio
async def test_scope_must_be_authorized_type():
    with pytest.raises(ValueError):
        await run_project_dialogue(
            scope={"project_id": "from_model"},
            environment={},
            reader=object(),
            on_event=lambda _: None,
        )
