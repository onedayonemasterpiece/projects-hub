from __future__ import annotations

import asyncio

import pytest

from projects_hub.live_resources import ConversationScope
from projects_hub.store import DurableStore, StoreError
from test_live_wss import Provider, live_client, make_host, start


@pytest.fixture
def checkpoint_context(tmp_path):
    store = DurableStore(tmp_path / "data")
    boot = store.ensure_dev_workspace("Checkpoint lifecycle")
    actor_id = boot["actor"]["id"]
    workspace_id = boot["workspace"]["id"]
    conversation = store.create_conversation(actor_id, workspace_id, boot["projects"][0]["id"])
    args = {
        "resource_id": ConversationScope(workspace_id, actor_id, conversation["id"]).resource_binding(),
        "actor": {"subject": actor_id, "tenant_id": workspace_id},
        "conversation_id": conversation["id"],
        "audio_mode": "realtime",
    }
    try:
        yield store, Provider(), args
    finally:
        store.close()


def checkpoint(store, args, *, model="gemini-3.8-live", actor_id=None):
    return store.usable_live_checkpoint(
        actor_id or args["actor"]["subject"], args["conversation_id"],
        model=model, max_age_ms=100 * 60 * 1000,
    )


async def explicit_stop(host, session, args):
    return await host.stop(
        session_id=session.id, resource_id=args["resource_id"], actor=args["actor"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["backend_restart", "detached_restart"])
async def test_recovery_reuses_checkpoint_through_real_host_lifecycle(checkpoint_context, recovery):
    store, provider, args = checkpoint_context
    host = make_host(store, provider)
    try:
        first = await host.start(**args)
        old = host.sessions[first["session_id"]]
        host.adapter.on_checkpoint(old, "original-checkpoint")
        if recovery == "backend_restart":
            # This is exactly the host call made by app.py lifespan shutdown.
            await host.stop_all()
            host = make_host(store, provider)
        else:
            # Existing product admission reclaims an app's detached WSS session.
            host._socket_states[old.id].used_wss = True
            host._socket_states[old.id].claim = None

        second = await host.start(**args)
        starts = [item for item in provider.inputs if item["type"] == "start"]
        assert starts[-1]["resume_handle"] == "original-checkpoint"
        assert second["resumed_from_checkpoint"] is True
        assert old.id not in host.sessions
    finally:
        await host.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("late_checkpoint", [False, True])
async def test_explicit_stop_cannot_restore_inherited_or_late_checkpoint(checkpoint_context, late_checkpoint):
    store, provider, args = checkpoint_context
    host = make_host(store, provider)
    try:
        first = await host.start(**args)
        host.adapter.on_checkpoint(host.sessions[first["session_id"]], "inherited-checkpoint")
        await host.stop_all()
        host = make_host(store, provider)
        second = await host.start(**args)
        resumed = host.sessions[second["session_id"]]
        if late_checkpoint:
            host.adapter.on_checkpoint(resumed, "current-checkpoint")

        await explicit_stop(host, resumed, args)
        if late_checkpoint:
            # An in-flight provider callback must not make Stop reversible.
            host.adapter.on_checkpoint(resumed, "late-after-stop")
        assert checkpoint(store, args) is None
        await host.start(**args)
        starts = [item for item in provider.inputs if item["type"] == "start"]
        assert "resume_handle" not in starts[-1]
    finally:
        await host.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["backend_restart", "detached_restart"])
async def test_retired_provider_checkpoint_cannot_replace_successor(checkpoint_context, recovery):
    store, provider, args = checkpoint_context
    host = make_host(store, provider)
    try:
        first = await host.start(**args)
        old = host.sessions[first["session_id"]]
        old_adapter = host.adapter
        old_adapter.on_checkpoint(old, "old-checkpoint")
        if recovery == "backend_restart":
            await host.stop_all()
            host = make_host(store, provider)
        else:
            host._socket_states[old.id].used_wss = True
            host._socket_states[old.id].claim = None
        second = await host.start(**args)
        current = host.sessions[second["session_id"]]
        host.adapter.on_checkpoint(current, "successor-checkpoint")
        old_adapter.on_checkpoint(old, "late-retired-checkpoint")
        assert checkpoint(store, args)["handle"] == "successor-checkpoint"
        # Repeating cleanup of the predecessor must not invalidate its successor.
        old_adapter.on_stopped(old)
        assert checkpoint(store, args)["handle"] == "successor-checkpoint"
    finally:
        await host.stop_all()


@pytest.mark.asyncio
async def test_explicit_stop_wins_while_automatic_reclaim_is_awaiting_cleanup(checkpoint_context):
    store, provider, args = checkpoint_context
    host = make_host(store, provider)
    entered_cleanup = asyncio.Event()
    release_cleanup = asyncio.Event()
    replacement_task = None
    original_discard = host._discard
    first_cleanup = True

    async def paused_discard(session, graceful=False):
        nonlocal first_cleanup
        if first_cleanup:
            first_cleanup = False
            entered_cleanup.set()
            await release_cleanup.wait()
        await original_discard(session, graceful=graceful)

    try:
        first = await host.start(**args)
        old = host.sessions[first["session_id"]]
        host.adapter.on_checkpoint(old, "must-not-resume")
        host._socket_states[old.id].used_wss = True
        host._socket_states[old.id].claim = None
        host._discard = paused_discard
        replacement_task = asyncio.create_task(host.start(**args))
        await asyncio.wait_for(entered_cleanup.wait(), timeout=2)
        await explicit_stop(host, old, args)
        assert checkpoint(store, args) is None
        release_cleanup.set()
        await replacement_task
        starts = [item for item in provider.inputs if item["type"] == "start"]
        assert "resume_handle" not in starts[-1]
    finally:
        release_cleanup.set()
        if replacement_task is not None:
            await asyncio.gather(replacement_task, return_exceptions=True)
        host._discard = original_discard
        await host.stop_all()


@pytest.mark.asyncio
async def test_checkpoint_lookup_keeps_actor_model_and_expiry_boundaries(checkpoint_context):
    store, provider, args = checkpoint_context
    host = make_host(store, provider)
    try:
        started = await host.start(**args)
        session = host.sessions[started["session_id"]]
        host.adapter.on_checkpoint(session, "private-checkpoint")
        assert checkpoint(store, args, actor_id="different-actor") is None
        assert checkpoint(store, args, model="gemini-3.8-live-extended-thinking") is None
        assert store.usable_live_checkpoint(
            args["actor"]["subject"], args["conversation_id"],
            model="gemini-3.8-live", max_age_ms=-1,
        ) is None
        with pytest.raises(StoreError):
            store.save_live_checkpoint(
                "different-actor", args["conversation_id"], model="gemini-3.8-live",
                handle="forged", session_id="other-session",
            )
        assert checkpoint(store, args)["handle"] == "private-checkpoint"
    finally:
        await host.stop_all()


def test_http_client_cannot_request_internal_reclaim_reason(live_client):
    client, conversation_id, _, host = live_client
    started = start(live_client)
    session = host.sessions[started["session_id"]]
    host.adapter.on_checkpoint(session, "must-stop")
    response = client.post(
        f"/api/live/{conversation_id}/sessions/{session.id}/stop",
        json={"reason": "detached_reclaim"},
    )
    assert response.status_code == 200, response.text
    assert host.adapter.store.usable_live_checkpoint(
        session.state["actor_id"], conversation_id,
        model=session.model, max_age_ms=100 * 60 * 1000,
    ) is None
