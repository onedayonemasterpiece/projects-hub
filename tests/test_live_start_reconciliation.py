"""Lost HTTP responses cannot multiply real shared-host provider sessions."""
import asyncio
import json

import pytest

from live_interaction import LiveError
from projects_hub.live_resources import ConversationScope
from projects_hub.store import DurableStore
from test_live_wss import Provider, make_host


@pytest.fixture
def request_context(tmp_path):
    store = DurableStore(tmp_path)
    boot = store.ensure_dev_workspace("Native retry")
    actor_id = boot["actor"]["id"]
    workspace_id = boot["workspace"]["id"]
    conversation = store.create_conversation(actor_id, workspace_id)
    args = dict(resource_id=ConversationScope(workspace_id, actor_id, conversation["id"]).resource_binding(),
                actor={"subject": actor_id, "tenant_id": workspace_id},
                conversation_id=conversation["id"], audio_mode="realtime",
                attempt_id="native-start-1", client_instance_id="native-install-1")
    yield store, args
    store.close()


class ReadyGateProvider(Provider):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.ready = asyncio.Event()

    async def run(self, *, load_key, reader, on_event):
        self.inputs.append(json.loads(await reader.readline()))
        self.entered.set()
        await self.ready.wait()
        on_event({"type": "ready", "model": "gemini-3.8-live"})
        while raw := await reader.readline():
            self.inputs.append(json.loads(raw))
            if self.inputs[-1]["type"] == "stop":
                return


@pytest.mark.asyncio
async def test_cancelled_http_waiter_reconciles_one_provider(request_context):
    store, args = request_context
    provider = ReadyGateProvider()
    host = make_host(store, provider)
    try:
        first = asyncio.create_task(host.start(**args))
        await asyncio.wait_for(provider.entered.wait(), 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        retry = asyncio.create_task(host.start(**args))
        provider.ready.set()
        result = await retry
        assert result["start_reconciled"] is True
        assert len([x for x in provider.inputs if x["type"] == "start"]) == 1
        assert host.size() == 1
        assert (await host.start(**args))["session_id"] == result["session_id"]
    finally:
        await host.stop_all()


@pytest.mark.asyncio
async def test_concurrent_duplicate_does_not_reclaim_the_started_session(request_context):
    store, args = request_context
    provider = Provider()
    host = make_host(store, provider)
    try:
        first, second = await asyncio.gather(host.start(**args), host.start(**args))
        assert first["session_id"] == second["session_id"]
        socket = host._socket_states[first["session_id"]]
        socket.used_wss = True
        socket.claim = None
        assert (await host.start(**args))["session_id"] == first["session_id"]
        # /sessions derives fresh history server-side; a lost HTTP response
        # must remain reconcilable after the original provider has added text.
        assert (await host.start(**args, history=[{"role": "user", "text": "new server history"}]))["session_id"] == first["session_id"]
        assert len([x for x in provider.inputs if x["type"] == "start"]) == 1
    finally:
        await host.stop_all()


@pytest.mark.asyncio
async def test_retry_cannot_change_body_or_revive_explicit_stop(request_context):
    store, args = request_context
    provider = Provider()
    host = make_host(store, provider)
    try:
        first = await host.start(**args)
        with pytest.raises(LiveError) as conflict:
            await host.start(**{**args, "audio_mode": "buffered"})
        assert conflict.value.code == "LIVE_ATTEMPT_CONFLICT"
        await host.stop(session_id=first["session_id"], resource_id=args["resource_id"], actor=args["actor"])
        with pytest.raises(LiveError) as closed:
            await host.start(**args)
        assert closed.value.code == "LIVE_ATTEMPT_CLOSED"
        assert len([x for x in provider.inputs if x["type"] == "start"]) == 1
        fresh = await host.start(**{**args, "attempt_id": "native-start-2"})
        assert fresh["session_id"] != first["session_id"]
    finally:
        await host.stop_all()


@pytest.mark.asyncio
async def test_same_attempt_is_not_rebound_to_another_tenant(request_context):
    store, args = request_context
    provider = Provider()
    host = make_host(store, provider)
    try:
        await host.start(**args)
        with pytest.raises(LiveError) as denied:
            await host.start(**{**args, "actor": {**args["actor"], "tenant_id": "other-workspace"}})
        assert denied.value.code == "FORBIDDEN"
        assert len([x for x in provider.inputs if x["type"] == "start"]) == 1
    finally:
        await host.stop_all()
