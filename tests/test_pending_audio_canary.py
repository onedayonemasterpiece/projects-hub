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

