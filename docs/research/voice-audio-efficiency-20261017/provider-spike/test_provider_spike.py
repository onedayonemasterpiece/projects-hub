"""Offline runner checks. SDK and sockets are synthetic; no credentials or network.

The sanitized SDK reference files are deliberately not imported. These tests
exercise the runner's hook against the inspected SDK RPC interface, and use the
real shared provider for queue/lifecycle integration through fake WebSockets.
"""
import asyncio
from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types

import pytest


class FakeResourceError(RuntimeError):
    resource_failure = True

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FakeConfig:
    fallback_key: str = "fixture-fallback-never-used"
    grant_tokens: int = 1024

    @classmethod
    def from_env(cls, consumer, environment):
        assert consumer == "projects-hub" and environment == {}
        return cls()


class FakeLease:
    def __init__(self, control):
        self.control = control
        self.used_estimated_tokens = self.observed_input_tokens = self.observed_output_tokens = 0
        self.revoked = False
        self.key_reads = 0

    def check(self):
        assert not self.revoked

    def key(self):
        self.key_reads += 1
        return "fixture-key"

    async def before_send(self, payload):
        self.check()
        self.used_estimated_tokens += 1  # synthetic observation, not the SDK estimator

    def observe(self, event):
        if event.get("type") == "usage":
            self.observed_input_tokens += event["metadata"].get("promptTokenCount", 0)
            self.observed_output_tokens += event["metadata"].get("responseTokenCount", 0)

    async def release(self):
        self.revoked = True
        await self.control.rpc("release", {})


class FakeControl:
    instances = []

    def __init__(self, config, **kwargs):
        self.config = config
        self.calls, self.replies = [], []
        self.acquire_calls = 0
        self.closed = False
        self.instances.append(self)

    async def rpc(self, operation, payload, **kwargs):
        self.calls.append((operation, payload.get("p_tokens")))
        reply = self.replies.pop(0) if self.replies else {"tokens": payload.get("p_tokens", 0)}
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            reply = await reply()
        return reply, 0.0

    async def acquire(self, *args, **kwargs):
        self.acquire_calls += 1
        await self.rpc("acquire", {"p_tokens": self.config.grant_tokens}, version=2)
        return FakeLease(self)

    async def close(self):
        self.closed = True


@pytest.fixture
def spike(monkeypatch):
    FakeControl.instances.clear()
    package = types.ModuleType("ai_resource_control")
    package.__path__ = []
    client = types.ModuleType("ai_resource_control.client")
    live = types.ModuleType("ai_resource_control.live")
    client.__file__ = live.__file__ = __file__
    client.Config, client.Control, client.ResourceError = FakeConfig, FakeControl, FakeResourceError

    async def run_guarded(*, control, reader, on_event, provider_run, **kwargs):
        lease = await control.acquire("gemini-3.8-live")
        def emit(event):
            lease.observe(event)
            on_event(event)
        try:
            await provider_run(load_key=lease.key, reader=reader, on_event=emit, resource_guard=lease)
        finally:
            await lease.release()

    package.client, package.live, package.run_guarded = client, live, run_guarded
    for name, module in (("ai_resource_control", package), ("ai_resource_control.client", client),
                         ("ai_resource_control.live", live)):
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).with_name("provider_spike.py")
    spec = importlib.util.spec_from_file_location("offline_provider_spike", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_initial_and_incremental_reservations_are_capped_before_rpc(spike):
    control = spike.SpikeControl(FakeConfig())
    await control.rpc("acquire", {"p_tokens": 1024})
    await control.rpc("grant", {"p_tokens": 148976})
    with pytest.raises(FakeResourceError, match="SPIKE_AUTHORITY_RESERVATION_CAP"):
        await control.rpc("grant", {"p_tokens": 1})
    assert len(control.calls) == 2
    assert control.reserved_units == 150000
    assert control.initial_reserved_units == 1024
    assert control.additional_reserved_units == 148976


@pytest.mark.asyncio
async def test_new_grant_counts_receipt_without_carry_or_budget_callback(spike):
    control = spike.SpikeControl(FakeConfig())
    await control.rpc("acquire", {"p_tokens": 1024})
    await control.rpc("grant", {"p_tokens": 1024})
    runtime = spike.SpikeRun(control, "idle", b"\0\0", .01)
    runtime.on_event({"type": "resource_budget", "status": "granted", "granted_units": 1029})
    assert control.reserved_units == 2048
    assert control.summary()["provider_observed_input_tokens"] is None
    assert not control.summary()["provider_billing_guaranteed"]


def test_unreported_usage_fields_remain_unknown(spike):
    control = spike.SpikeControl(FakeConfig())
    control.lease = FakeLease(control)
    runtime = spike.SpikeRun(control, "idle", b"\0\0", .01)
    runtime.on_event({"type": "usage", "metadata": {"cachedContentTokenCount": 17}})
    assert control.summary()["provider_usage_events"] == 1
    assert control.summary()["provider_observed_input_tokens"] is None
    assert control.summary()["provider_observed_output_tokens"] is None
    runtime.on_event({"type": "usage", "metadata": {"promptTokenCount": 0}})
    assert control.summary()["provider_observed_input_tokens"] == 0
    assert control.summary()["provider_observed_output_tokens"] is None


@pytest.mark.asyncio
async def test_definitive_denial_allows_right_sizing_without_double_count(spike):
    control = spike.SpikeControl(FakeConfig())
    control.replies = [FakeResourceError("RESOURCE_TOKEN_BUDGET")]
    with pytest.raises(FakeResourceError):
        await control.rpc("grant", {"p_tokens": 1024})
    await control.rpc("grant", {"p_tokens": 512})
    assert control.reserved_units == 512
    assert control.unknown_reserved_units == 0


@pytest.mark.asyncio
async def test_unknown_response_retains_request_and_forbids_more_admission(spike):
    control = spike.SpikeControl(FakeConfig())
    control.replies = [FakeResourceError("RESOURCE_CONTROL_UNAVAILABLE")]
    with pytest.raises(FakeResourceError):
        await control.rpc("grant", {"p_tokens": 1024})
    with pytest.raises(FakeResourceError, match="SPIKE_RESERVATION_UNCERTAIN"):
        await control.rpc("grant", {"p_tokens": 1})
    with pytest.raises(FakeResourceError, match="SPIKE_RESERVATION_UNCERTAIN"):
        control.connection_start()
    await control.rpc("release", {})
    assert control.calls == [("grant", 1024), ("release", None)]
    assert control.unknown_reserved_units == 1024
    assert not control.summary()["authority_accounting_complete"]
    assert control.release_confirmed


@pytest.mark.asyncio
async def test_cancellation_of_mutating_rpc_keeps_unknown_reservation(spike):
    control = spike.SpikeControl(FakeConfig())
    entered = asyncio.Event()
    async def wait_forever():
        entered.set()
        await asyncio.Event().wait()
    control.replies = [wait_forever]
    task = asyncio.create_task(control.rpc("grant", {"p_tokens": 1024}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert control.unknown_reserved_units == 1024
    with pytest.raises(FakeResourceError, match="SPIKE_RESERVATION_UNCERTAIN"):
        await control.rpc("grant", {"p_tokens": 1024})
    assert len(control.calls) == 1


@pytest.mark.asyncio
async def test_concurrent_requests_cannot_both_pass_the_remaining_cap(spike):
    control = spike.SpikeControl(FakeConfig())
    entered, finish = asyncio.Event(), asyncio.Event()
    async def delayed():
        entered.set()
        await finish.wait()
        return {"tokens": 90000}
    control.replies = [delayed]
    first = asyncio.create_task(control.rpc("grant", {"p_tokens": 90000}))
    await entered.wait()
    second = asyncio.create_task(control.rpc("grant", {"p_tokens": 90000}))
    await asyncio.sleep(0)
    assert len(control.calls) == 1
    finish.set()
    await first
    with pytest.raises(FakeResourceError, match="SPIKE_AUTHORITY_RESERVATION_CAP"):
        await second
    assert len(control.calls) == 1


@pytest.mark.asyncio
async def test_single_acquire_and_no_outage_fallback(spike):
    control = spike.SpikeControl(FakeConfig())
    lease = await control.acquire("gemini-3.8-live", binding="fixture")
    assert control.config.fallback_key == ""
    assert control.lease is lease
    with pytest.raises(FakeResourceError, match="SPIKE_SECOND_ACQUIRE_FORBIDDEN"):
        await control.acquire("gemini-3.8-live", binding="fixture")
    assert control.acquire_calls == 1


def test_seventh_connect_is_refused_before_socket_creation_and_hook_restored(spike, monkeypatch):
    import websockets
    calls = []
    def connect(*args, **kwargs):
        calls.append(True)
        return object()
    monkeypatch.setattr(websockets, "connect", connect)
    control = spike.SpikeControl(FakeConfig())
    with pytest.raises(FakeResourceError, match="SPIKE_PROVIDER_CONNECTION_CAP"):
        with spike.bounded_connections(control):
            for _ in range(7):
                websockets.connect("wss://fixture.invalid")
    assert len(calls) == 6
    assert control.connection_attempts == 6
    assert websockets.connect is connect


@pytest.mark.asyncio
async def test_parent_cancellation_joins_active_provider_child(spike, monkeypatch, capsys):
    entered, closed = asyncio.Event(), asyncio.Event()
    async def provider_run(**kwargs):
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            closed.set()
    monkeypatch.setattr(spike.provider, "run", provider_run)
    runtime = spike.SpikeRun(spike.SpikeControl(FakeConfig()), "pacing", b"\0\0", .01)
    runtime.emit = runtime.on_event
    task = asyncio.create_task(spike.pacing(runtime, b"\0\0", 0, "cancel"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, .5)
    assert closed.is_set()
    assert runtime.current.task.done()
    assert "fixture-key" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cancellation_arriving_during_normal_stop_is_not_swallowed(spike):
    session = spike.Session("stopping", spike.CONFIG)
    entered, closed = asyncio.Event(), asyncio.Event()
    async def provider_wait():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            closed.set()
    session.task = asyncio.create_task(provider_wait())
    await entered.wait()
    stop = asyncio.create_task(session.stop())
    await asyncio.sleep(0)
    stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stop, .5)
    assert closed.is_set() and session.task.done()


@pytest.mark.asyncio
async def test_source_mismatch_stops_before_environment_or_sdk_acquire(spike, monkeypatch, capsys):
    attribution = spike.source_attribution()
    actual = attribution["sources"]["provider"]
    assert actual["sha256"] == hashlib.sha256(Path(actual["path"]).read_bytes()).hexdigest()
    assert actual["matches_expected"]
    assert not attribution["sources"]["resource_client"]["matches_expected"]
    def forbidden():
        raise AssertionError("environment must not be read")
    monkeypatch.setattr(spike, "load_env", forbidden)
    monkeypatch.setattr(sys, "argv", ["provider_spike.py", "all", "--pcm", "not-opened.pcm"])
    assert await spike.main() == 2
    assert not FakeControl.instances
    assert "SPIKE_SOURCE_MISMATCH" in capsys.readouterr().out


def test_checkpoint_handle_is_never_dumped_and_nonresumable_clears_it(spike, capsys):
    session = spike.Session("fixture", spike.CONFIG)
    secret_sentinel = "fixture-private-handle-must-stay-in-memory"
    session.on_event({"type": "resumption_checkpoint", "handle": secret_sentinel})
    assert session.handle == secret_sentinel
    session.dump("fixture")
    assert secret_sentinel not in capsys.readouterr().out
    assert secret_sentinel not in json.dumps(session.events)
    session.on_event({"type": "resumption_state", "resumable": False})
    assert session.handle is None and session.handle_event_index == -1


@pytest.mark.asyncio
async def test_all_scenarios_use_one_lease_and_real_shared_provider_with_fake_sockets(spike, monkeypatch, tmp_path, capsys):
    import websockets
    sockets = []
    class Socket:
        def __init__(self, number):
            self.number, self.closed = number, False
            self.queue = asyncio.Queue()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            await self.close()
        async def send(self, payload):
            message = json.loads(payload)
            if "setup" in message:
                await self.queue.put({"setupComplete": {}})
                await self.queue.put({"sessionResumptionUpdate": {"resumable": True, "newHandle": "fixture-initial-handle"}})
                if self.number == 4:
                    await self.queue.put({"goAway": {"timeLeft": "0s"}})
            if "activityEnd" in message.get("realtimeInput", {}) or "clientContent" in message:
                await self.queue.put({
                    "sessionResumptionUpdate": {"resumable": True, "newHandle": "fixture-fresh-private-handle"},
                    "usageMetadata": {"promptTokenCount": 17, "responseTokenCount": 7},
                    "serverContent": {"inputTranscription": {"text": "Blue penguin forty two"},
                                      "outputTranscription": {"text": "Blue penguin, forty two"},
                                      "turnComplete": True}})
        async def recv(self):
            message = await self.queue.get()
            if message is None:
                raise ConnectionError("fixture closed")
            return json.dumps(message)
        async def close(self):
            if not self.closed:
                self.closed = True
                await self.queue.put(None)
    def connect(*args, **kwargs):
        socket = Socket(len(sockets) + 1)
        sockets.append(socket)
        return socket
    monkeypatch.setattr(websockets, "connect", connect)
    real_resume = spike.resume
    async def fast_resume(runtime, handle, delay):
        return await real_resume(runtime, handle, 0)
    monkeypatch.setattr(spike, "resume", fast_resume)
    monkeypatch.setattr(spike, "source_attribution", lambda: {"sources": {"fixture": {"matches_expected": True}}})
    monkeypatch.setattr(spike, "load_env", lambda: {})
    pcm = tmp_path / "fixture.pcm"
    pcm.write_bytes(b"\0\0" * 1600)
    monkeypatch.setattr(sys, "argv", ["provider_spike.py", "all", "--pcm", str(pcm), "--minutes", "0.01"])
    assert await asyncio.wait_for(spike.main(), 3) == 0
    control = FakeControl.instances[0]
    assert len(FakeControl.instances) == 1
    assert control.acquire_calls == control.acquire_attempts == 1
    assert control.connection_attempts == len(sockets) == 5
    assert control.lease.key_reads == 4
    assert control.lease.revoked and control.release_confirmed and control.closed
    assert all(socket.closed for socket in sockets)
    output = capsys.readouterr().out
    assert "fixture-key" not in output and "fixture-fresh-private-handle" not in output
    receipts = [json.loads(line) for line in output.splitlines()]
    pacing = [item for item in receipts if item.get("label") in ("realtime", "burst")]
    assert len(pacing) == 2
    assert all(item["provider_audio_chunks"] == 1 and item["audio_turn_generation"] == 1 for item in pacing)
    assert all("input_dropped" not in item["kinds"] for item in pacing)
    resumed = next(item for item in receipts if item.get("label") == "resume")
    assert resumed["checkpoint_resumed"] and resumed["recalled_code_word"] and resumed["recalled_number"]
    idle = next(item for item in receipts if item.get("label") == "idle")
    assert idle["resumed_mode"] == "checkpoint"
