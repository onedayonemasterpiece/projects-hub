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


class FailOnceWithCleanupProvider(Provider):
    def __init__(self):
        super().__init__()
        self.cleanup_started = asyncio.Event()
        self.cleanup_allowed = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def run(self, *, load_key, reader, on_event):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if not self.inputs:
                self.inputs.append(json.loads(await reader.readline()))
                try:
                    on_event({"type": "error", "code": "LIVE_PROVIDER_SETUP_TIMEOUT",
                              "message": "Synthetic transient setup failure"})
                    await asyncio.Event().wait()
                finally:
                    self.cleanup_started.set()
                    await self.cleanup_allowed.wait()
            else:
                await super().run(load_key=load_key, reader=reader, on_event=on_event)
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_failed_start_retries_close_only_after_provider_cleanup(request_context):
    store, args = request_context
    provider = FailOnceWithCleanupProvider()
    host = make_host(store, provider)
    waiters = []
    try:
        first = asyncio.create_task(host.start(**args))
        waiters.append(first)
        await asyncio.wait_for(provider.cleanup_started.wait(), 1)
        retry = asyncio.create_task(host.start(**args))
        waiters.append(retry)
        await asyncio.sleep(0)
        assert not first.done() and not retry.done()
        assert provider.active == 1
        assert len(provider.inputs) == 1
        with pytest.raises(LiveError) as busy:
            await host.start(**{**args, "attempt_id": "premature-new-attempt"})
        assert busy.value.code == "LIVE_BUSY"
        assert len(provider.inputs) == 1

        provider.cleanup_allowed.set()
        failures = await asyncio.wait_for(asyncio.gather(*waiters, return_exceptions=True), 1)
        assert all(isinstance(error, LiveError) and error.code == "LIVE_PROVIDER_SETUP_TIMEOUT"
                   for error in failures)
        assert provider.active == 0 and host.size() == 0
        # The first waiters retain the actual failure. A later retry receives
        # the terminal code already understood by the installed native APK.
        with pytest.raises(LiveError) as closed:
            await host.start(**args)
        assert closed.value.code == "LIVE_ATTEMPT_CLOSED"
        assert len(provider.inputs) == 1
        fresh = await host.start(**{**args, "attempt_id": "native-start-after-cleanup"})
        assert host.sessions[fresh["session_id"]].closed is False
        assert provider.active == 1 and provider.max_active == 1
        assert len([item for item in provider.inputs if item["type"] == "start"]) == 2
    finally:
        provider.cleanup_allowed.set()
        await asyncio.gather(*waiters, return_exceptions=True)
        await host.stop_all()


@pytest.mark.asyncio
async def test_failed_start_with_live_session_is_not_declared_cleaned_up(request_context, monkeypatch):
    store, args = request_context
    provider = Provider()
    host = make_host(store, provider)
    issue_ticket = host.issue_socket_ticket

    def fail_ticket_once(**kwargs):
        monkeypatch.setattr(host, "issue_socket_ticket", issue_ticket)
        raise LiveError("LIVE_SOCKET_TICKET_UNAVAILABLE", "Synthetic failure after provider-ready")

    monkeypatch.setattr(host, "issue_socket_ticket", fail_ticket_once)
    try:
        with pytest.raises(LiveError) as first:
            await host.start(**args)
        assert first.value.code == "LIVE_SOCKET_TICKET_UNAVAILABLE"
        session = next(iter(host.sessions.values()))
        assert not session.closed and not session.task.done()
        with pytest.raises(LiveError) as retry:
            await host.start(**args)
        assert retry.value.code == "LIVE_SOCKET_TICKET_UNAVAILABLE"
        assert len([item for item in provider.inputs if item["type"] == "start"]) == 1
        await host.stop(session_id=session.id, resource_id=args["resource_id"], actor=args["actor"])
        assert session.task.done()
        with pytest.raises(LiveError) as closed:
            await host.start(**args)
        assert closed.value.code == "LIVE_ATTEMPT_CLOSED"
        assert len([item for item in provider.inputs if item["type"] == "start"]) == 1
    finally:
        await host.stop_all()
