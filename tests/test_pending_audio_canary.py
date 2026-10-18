"""An observed UI event, silence or audio after release cannot fake pending audio."""
import base64
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace


def load():
    path = Path(__file__).parents[1] / "scripts" / "devcoveer_pending_audio_canary.py"
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("pending_audio_canary_tested", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def audio(silent=False):
    return {"type": "audio", "mime_type": "audio/pcm;rate=24000",
            "data": base64.b64encode((b"\0\0" if silent else b"\x00\x04") * 6000).decode()}


def pending():
    module = load()
    clock = [0.0]
    evidence = module.PendingEvidence(lambda: clock[0])
    evidence.call_id, evidence.call_count, evidence.non_blocking = "tool1", 1, True
    evidence.entered = 1.0
    evidence.input_completed_at = 0.0
    session = SimpleNamespace(events=[])
    return evidence, session, clock


def test_audio_is_required_after_shared_pending_and_before_actual_release():
    evidence, session, clock = pending()
    clock[0] = 2
    evidence.observe(session, audio())
    assert evidence.audio_metrics("pending")["pcm_bytes"] == 0
    session.events.append({"type": "tool_progress", "id": "tool1", "seq": 11})
    clock[0] = 4
    evidence.observe(session, audio())
    evidence.released, evidence.returned, evidence.result_verified = 10, 10.01, True
    session.events.append({"type": "tool_result", "id": "tool1", "seq": 14, "status": "ok"})
    clock[0] = 11
    evidence.observe(session, audio())
    evidence.observe(session, {"type": "turn_complete"})
    result = evidence.result()
    assert result["passed"] is True
    assert result["pending_provider_audio"]["duration_ms"] == 250
    assert result["completion_provider_audio"]["duration_ms"] == 250
    assert result["audio_chunks"][0]["tool_result_seq"] is None


def test_pending_ui_text_and_silence_do_not_pass():
    evidence, session, clock = pending()
    session.events.append({"type": "tool_progress", "id": "tool1", "seq": 11})
    clock[0] = 4
    evidence.observe(session, {"type": "output_transcript", "text": "Секундочку"})
    evidence.observe(session, audio(silent=True))
    evidence.released, evidence.returned, evidence.result_verified = 10, 10.01, True
    session.events.append({"type": "tool_result", "id": "tool1", "seq": 14, "status": "ok"})
    clock[0] = 11
    evidence.observe(session, audio())
    evidence.observe(session, {"type": "turn_complete"})
    result = evidence.result()
    assert result["completion_provider_audio"]["audible_signal"] is True
    assert result["pending_provider_audio"]["audible_signal"] is False
    assert result["passed"] is False


def test_pending_from_another_tool_cannot_be_used_as_this_calls_evidence():
    evidence, session, clock = pending()
    session.events.append({"type": "tool_progress", "id": "other", "seq": 11})
    clock[0] = 4
    evidence.observe(session, audio())
    assert evidence.audio_metrics("pending")["pcm_bytes"] == 0
    evidence.released, evidence.returned = 10, 10.01
    session.events.append({"type": "tool_progress", "id": "tool1", "seq": 12})
    clock[0] = 11
    evidence.observe(session, audio())
    assert evidence.audio_metrics("pending")["pcm_bytes"] == 0


def test_safe_prerequisite_read_is_preserved_before_the_single_delayed_target():
    module = load()
    evidence = module.PendingEvidence()
    session = SimpleNamespace(capability="repositories", non_blocking_tools={module.TOOL})
    executed = []

    async def original(session, call):
        executed.append(call["name"])
        return {"repositories": [], "requires_connection": True} if call["name"] == module.TOOL else {"projects": []}

    async def scenario():
        await module.execute_read_tool(evidence, original, session,
                                       {"name": "projects_list_accessible", "id": "before"}, 0)
        assert evidence.call_count == 0
        await module.execute_read_tool(evidence, original, session, {"name": module.TOOL, "id": "target"}, 0)

    asyncio.run(scenario())
    assert executed == ["projects_list_accessible", module.TOOL]
    assert evidence.call_count == 1 and evidence.prerequisite_calls == 1
    assert evidence.result_verified is True
    assert [item["phase"] for item in evidence.tool_calls] == ["entered", "returned", "entered", "returned"]
    assert callable(evidence.save_audio)


def test_unexpected_tool_receipt_contains_only_bounded_name_capability_and_code():
    import pytest
    from projects_hub.store import StoreError
    module = load()
    evidence = module.PendingEvidence()
    session = SimpleNamespace(capability="board", non_blocking_tools={module.TOOL}, events=[])
    call = {"name": "board_edit", "id": "private-call-id", "args": {"token": "private-value", "text": "private-text"}}
    evidence.observe(session, {"type": "tool_call", "calls": [call]})

    async def original(session, call):
        raise AssertionError("Unexpected mutating tool must never execute")

    with pytest.raises(StoreError):
        asyncio.run(module.execute_read_tool(evidence, original, session, call, 0))
    result = evidence.result()
    assert result["target_calls"] == 0
    assert result["tool_calls"][-1] == {"phase": "error", "name": "board_edit", "capability": "board", "code": "CANARY_UNEXPECTED_TOOL"}
    assert "private" not in json.dumps(result)
    for _ in range(40):
        evidence.tool_diagnostic(session, call, "provider_call")
    assert len(evidence.tool_calls) == 32
    assert "tool_diagnostic_bound" in evidence.errors


def test_request_wav_requires_expected_bytes_and_non_silent_pcm(tmp_path):
    import hashlib
    import wave
    import pytest
    module = load()
    path = tmp_path / "request.wav"
    pcm = b"\x00\x04" * 32000
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(pcm)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    actual, receipt = module.load_request_speech(path, digest)
    assert actual == pcm and receipt["pcm_bytes"] == len(pcm)
    assert receipt["native_capture"] is False
    with pytest.raises(ValueError, match="CANARY_REQUEST_WAV_SHA256_MISMATCH"):
        module.load_request_speech(path, "0" * 64)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 32000)
    with pytest.raises(ValueError, match="CANARY_REQUEST_WAV_SILENT"):
        module.load_request_speech(path, hashlib.sha256(path.read_bytes()).hexdigest())


def test_spoken_request_crosses_actual_durable_host_admission_before_router(tmp_path):
    # Exercise real adapter + shared host input, without starting any provider.
    # A waveform here checks admission/routing, not speech recognition quality.
    from live_interaction.session_host import LiveSessionHost, _Session, _QueueReader
    from projects_hub.live_adapter import ProjectsHubLiveAdapter
    from projects_hub.live_resources import ConversationScope
    from projects_hub.store import DurableStore
    module = load()
    store = DurableStore(tmp_path)

    async def scenario():
        boot = store.ensure_dev_workspace("Spoken pending admission regression")
        actor = {"subject": boot["actor"]["id"], "tenant_id": boot["workspace"]["id"]}
        conversation = store.create_conversation(actor["subject"], actor["tenant_id"])
        resource = ConversationScope(actor["tenant_id"], actor["subject"], conversation["id"]).resource_binding()
        host = LiveSessionHost(adapter_factory=lambda **shared: ProjectsHubLiveAdapter(store, **shared))
        initialized = host.adapter.initialize(resource_id=resource, actor=actor, model="gemini-3.8-live",
                                              conversation_id=conversation["id"], audio_mode="buffered")
        session = _Session(id="isolated_input_regression", resource_id=resource, actor=actor,
                           model="gemini-3.8-live", reader=_QueueReader(), state=initialized["state"],
                           manual_activity_detection=True)
        host.sessions[session.id] = session
        binding = {"session_id": session.id, "resource_id": resource, "actor": actor}
        call = {"id": "route", "name": "activate_capability",
                "args": {"capability": "repositories", "intent": module.REQUEST_PHRASE}}
        await host.input(**binding, message={"text": module.REQUEST_PHRASE})
        assert host.adapter.resolve_capability(session, call) is None
        assert session.state.get("_current_utterance") is None
        assert json.loads(await session.reader.readline())["type"] == "text"
        pcm = b"\x00\x04" * 3200
        intervals = []

        async def no_wait(seconds):
            intervals.append(seconds)

        await module.feed_request_audio(host, **binding, pcm=pcm, sleep=no_wait)
        utterance = session.state["_current_utterance"]
        assert utterance["pcm_accepted"] is True and utterance["activity_end"] is True
        source = store.get_source(actor["subject"], session.state["source_id"])
        assert source["audio_bytes"] == len(pcm)
        spec = host.adapter.resolve_capability(session, call)
        assert spec is not None and spec["capability"] == "repositories"
        assert session.state["_capability_turn_id"] == "buffered:" + source["id"]
        queued = [json.loads(await session.reader.readline()) for _ in range(4)]
        assert [item["type"] for item in queued] == ["activity_start", "audio", "audio", "activity_end"]
        assert b"".join(base64.b64decode(item["data"]) for item in queued if item["type"] == "audio") == pcm
        assert intervals == [.1, .1]

    try:
        asyncio.run(scenario())
    finally:
        store.close()


def test_completed_refusal_fails_promptly_but_inflight_router_is_allowed_to_finish():
    module = load()
    clock = [1.0]
    evidence = module.PendingEvidence(lambda: clock[0])
    evidence.input_completed_at = 1.0
    transition = SimpleNamespace(done=lambda: False)
    session = SimpleNamespace(events=[], pending_transition=transition)
    clock[0] = 2.0
    evidence.observe(session, {"type": "turn_complete"})
    clock[0] = 4.1
    evidence.check_pre_target_completion(session)
    assert not evidence.errors
    evidence.observe(session, {"type": "ready"})
    session.pending_transition = None
    evidence.check_pre_target_completion(session)
    assert not evidence.errors
    evidence.observe(session, {"type": "turn_complete"})
    clock[0] = 6.2
    evidence.check_pre_target_completion(session)
    assert evidence.errors == ["provider_turn_completed_without_target"]
    assert evidence.result()["passed"] is False


def test_prerequisite_failure_is_terminal_and_sanitized():
    import pytest
    from projects_hub.store import StoreError
    module = load()
    evidence = module.PendingEvidence()
    session = SimpleNamespace(capability="core", non_blocking_tools=set())

    async def original(session, call):
        raise StoreError("TOOL_NOT_AVAILABLE", "private failure detail")

    with pytest.raises(StoreError):
        asyncio.run(module.execute_read_tool(evidence, original, session,
                                             {"name": "activate_capability", "id": "private-id"}, 0))
    assert "tool_execution_error" in evidence.errors
    assert evidence.tool_calls[-1]["code"] == "TOOL_NOT_AVAILABLE"
    evidence.host_events([{"seq": 1, "type": "tool_result", "name": "activate_capability",
                           "status": "error", "code": "TOOL_NOT_AVAILABLE"}])
    assert "tool_result_error" in evidence.errors
    assert "private" not in json.dumps(evidence.result())

