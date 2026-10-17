"""Canonical native conversation history does not require a presentation UI."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from live_interaction.audio_v2 import DurablePCMProjection, SourceDescriptor
from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_resources import ConversationScope
from projects_hub.store import DurableStore
from test_encoded_voice_store import voice, opened, epoch, chunk


def adapter_context(store, encoded, actor, conversation):
    workspace = conversation["workspace_id"]
    adapter = ProjectsHubLiveAdapter(store, encoded_store_factory=lambda _: encoded)
    setup = adapter.initialize(
        resource_id=ConversationScope(workspace, actor, conversation["id"]).resource_binding(),
        actor={"subject": actor, "tenant_id": workspace}, model="gemini-3.8-live",
        conversation_id=conversation["id"],
    )
    return adapter, SimpleNamespace(id="live_native_timeline_fixture", state=setup["state"],
                                    closed=False, stop_reason=None)


async def speech(adapter, session, encoded, bound, packet):
    await adapter.activate_audio_source(session, bound)
    adapter.input(session, {"activity_start": True})
    receipt = await encoded.commit_chunk(bound, epoch(), packet)
    await adapter.input_audio(session, DurablePCMProjection(receipt, b"\0\0" * 1600, 16000, packet.capture_start_us))
    adapter.input(session, {"activity_end": True})


@pytest.mark.asyncio
async def test_native_transcripts_restore_without_any_webview_post(voice, tmp_path):
    store, encoded, actor, conversation, _ = voice
    adapter, session = adapter_context(store, encoded, actor, conversation)
    bound = await opened(encoded)
    await speech(adapter, session, encoded, bound, chunk(1))
    user = {"type": "input_transcript", "text": "Запомни два пункта", "provider_at": 12}
    answer = {"type": "output_transcript", "text": "Сейчас проверю.", "provider_at": 13}
    adapter.on_event(session, user)
    adapter.on_event(session, answer)
    adapter.on_event(session, {"type": "turn_complete"})
    assert user["timeline_message"]["turn_id"] == answer["timeline_message"]["turn_id"]
    reopened = DurableStore(tmp_path)
    try:
        messages = reopened.list_conversation_timeline_messages(
            actor_id=actor, conversation_id=conversation["id"], workspace_id=conversation["workspace_id"])
        assert {item["role"]: item["text"] for item in messages} == {
            "user": "Запомни два пункта", "assistant": "Сейчас проверю."}
        assert all(item["source_id"] == bound.server_source_id for item in messages)
        assert not list(tmp_path.glob("audio/*.pcm"))
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_output_tail_keeps_original_source_while_fresh_capture_rotates(voice):
    store, encoded, actor, conversation, _ = voice
    adapter, session = adapter_context(store, encoded, actor, conversation)
    old = await opened(encoded)
    await speech(adapter, session, encoded, old, chunk(1))
    adapter.on_event(session, {"type": "input_transcript", "text": "Первый запрос"})
    first = {"type": "output_transcript", "text": "Проверяю"}
    adapter.on_event(session, first)
    new = await encoded.open_source(SourceDescriptor("capture-2", old.source.started_at_unix_ms + 1000))
    await encoded.register_epoch(new, epoch())
    await speech(adapter, session, encoded, new, replace(chunk(1), source_id="capture-2"))
    tail = {"type": "output_transcript", "text": "Проверяю первый запрос."}
    adapter.on_event(session, tail)
    assert tail["timeline_message"]["id"] == first["timeline_message"]["id"]
    assert tail["timeline_message"]["source_id"] == old.server_source_id
    assert session.state["_current_utterance"]["committed"] is False, "an old response cannot commit fresh speech"
    adapter.on_event(session, {"type": "turn_complete"})
    second = {"type": "input_transcript", "text": "Второй запрос"}
    adapter.on_event(session, second)
    assert second["timeline_message"]["source_id"] == new.server_source_id
    assert second["timeline_message"]["turn_id"] != first["timeline_message"]["turn_id"]


@pytest.mark.asyncio
async def test_native_timeline_and_original_transcript_have_one_commit(voice, monkeypatch):
    store, encoded, actor, conversation, _ = voice
    bound = await opened(encoded)
    before = store.get_source(actor, bound.server_source_id)
    def fail(**kwargs):
        raise RuntimeError("fixture failed timeline commit")
    with monkeypatch.context() as context:
        context.setattr(store, "upsert_conversation_timeline_message", fail)
        with pytest.raises(RuntimeError, match="fixture failed"):
            store.append_voice_timeline_event(actor_id=actor, source_id=bound.server_source_id,
                                              turn_id="turn_live_fixture", kind="input_transcript", text="test")
    after = store.get_source(actor, bound.server_source_id)
    assert after["transcript"] == before["transcript"]
    assert after["transcript_revision"] == before["transcript_revision"]
    assert store.db.execute("SELECT COUNT(*) FROM source_events WHERE source_id=?", (bound.server_source_id,)).fetchone()[0] == 0
