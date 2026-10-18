"""Paired product host test: real compressed wire, decoder and durable source store.

Only the external resource/provider runner is replaced. The fixture is a small
synthetic shared Opus vector with provenance, not a copy of SDK runtime code.
The candidate [audio] extra is required; missing native decode must not pass.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import sys
import types

import pytest

from live_interaction.audio_codec import PyAVAudioCodecFactory
from live_interaction.audio_v2 import AudioFrame, OPUS_PROFILE, RECOVERY, UPLINK_MAGIC
from live_interaction.socket_transport import serve_socket
from projects_hub.encoded_voice_store import EncodedVoiceIO, ProductEncodedAudioStore
from projects_hub.live_resources import ConversationScope
from projects_hub.live_runtime import build_live_host
from projects_hub.store import DurableStore


@pytest.mark.asyncio
async def test_production_v2_host_preserves_caption_boundaries_and_never_executes_recovery(tmp_path, monkeypatch):
    vector = json.loads((Path(__file__).parent / "fixtures/native-v2-caption-opus.json").read_text())
    assert PyAVAudioCodecFactory().av is not None, "Paired native voice acceptance requires live-interaction[audio]"
    store = DurableStore(tmp_path)
    boot = store.ensure_dev_workspace("Native caption boundary")
    actor_id = boot["actor"]["id"]
    conversation = store.create_conversation(actor_id, boot["workspace"]["id"])
    scope = ConversationScope(conversation["workspace_id"], actor_id, conversation["id"])
    observed = {"main": [], "caption": []}
    ended = {name: asyncio.Event() for name in observed}

    async def fixture_runner(*, consumer, environment, reader, on_event, provider_run, binding):
        assert consumer == "projects-hub" and environment == {}
        # This is the sole external boundary replacement. Calling the actual
        # provider here would require a resource lease and is forbidden in this test.
        name = "caption" if ":caption:" in binding else "main"
        start = json.loads(await reader.readline())
        assert start["type"] == "start"
        observed[name].append(start)
        assert start["configuration"]["manual_activity_detection"] is True
        on_event({"type": "ready", "model": start["model"]})
        while raw := await reader.readline():
            message = json.loads(raw)
            observed[name].append(message)
            if message["type"] == "stop":
                return
            if message["type"] == "activity_end":
                ended[name].set()
                on_event({"type": "input_timing", "activity_end_sent_at": 1})

    monkeypatch.setitem(sys.modules, "ai_resource_control", types.SimpleNamespace(run_guarded=fixture_runner))
    # Use the normal production factory, including its own codec and source-store
    # factories. No injected memory store, decoder or alternative host subclass.
    host = build_live_host(store, environment={})
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    sent, closes = [], []
    socket_task = None

    async def send(payload):
        sent.append(payload)
        await outgoing.put(payload)

    async def close(code, reason):
        closes.append((code, reason))

    async def receive_message(kind, **fields):
        async with asyncio.timeout(3):
            while True:
                payload = await outgoing.get()
                assert isinstance(payload, str), "Fixture provider emits no output audio"
                message = json.loads(payload)
                assert message.get("type") != "error", message
                if message.get("type") == kind and all(message.get(k) == v for k, v in fields.items()):
                    return message

    async def control(value):
        await incoming.put(json.dumps(value))

    async def open_source(source_id, *, recovery=False):
        await control({"type": "source_open", "source_id": source_id, "started_at_unix_ms": 1000,
                       "source_time_now_us": 100_000, "lane": "recovery" if recovery else "live"})
        bound = await receive_message("source_bound", source_id=source_id)
        await control({"type": "codec_epoch", "source_ref": bound["source_ref"], "descriptor": vector["epoch"]})
        epoch = await receive_message("epoch_bound", source_id=source_id)
        return bound, epoch["epoch_ref"]

    async def deliver(source_id, bound, epoch_ref, *, recovery=False):
        for chunk in vector["chunks"]:
            await incoming.put(AudioFrame(
                UPLINK_MAGIC, chunk["flags"] | (RECOVERY if recovery else 0), bound["source_ref"], epoch_ref,
                chunk["chunk_seq"], chunk["capture_start_us"], chunk["duration_us"], 17 if recovery else 1,
                0, base64.b64decode(chunk["payload_base64"], validate=True),
            ).encode())
        ack = await receive_message("audio_ack", source_id=source_id, chunk_seq=2)
        assert ack["stage"] == "server_durable"
        await control({"type": "source_seal", "source_ref": bound["source_ref"], "last_chunk_seq": 2,
                       "capture_end_us": 100_000})
        seal = await receive_message("source_sealed", source_id=source_id)
        assert seal["server_durable_seq"] == 2 and seal["complete"] is True

    def media(name):
        return [item for item in observed[name] if item["type"] in {"activity_start", "audio", "activity_end", "audio_stream_end"}]

    try:
        started = await host.start(resource_id=scope.resource_binding(), actor={"subject": actor_id},
                                   conversation_id=conversation["id"], audio_mode="realtime", recovery_only=False,
                                   attempt_id="native_caption_boundary", client_instance_id="native_fixture")
        assert started["caption_enabled"] is True
        session = host.sessions[started["session_id"]]
        # Native alignment is unverified in production. This test deliberately
        # observes the real accepted-PCM fanout without authorizing caption text.
        assert host._caption_sidecars[session.id].task is None
        host._caption_sidecars[session.id].diagnostic_observer = lambda event, text=None: None
        binding = host.open_socket(session_id=session.id, resource_id=scope.resource_binding(), ticket=started["socket_ticket"])
        socket_task = asyncio.create_task(serve_socket(binding, receive=incoming.get, send=send, close=close,
                                                       protocol=vector["protocol"]))
        await control({"type": "hello", "protocol": vector["protocol"], "attempt_id": started["attempt_id"],
                       "connection_generation": 1, "cursor": 0,
                       "capabilities": {"uplink_codecs": [OPUS_PROFILE], "downlink_codecs": [OPUS_PROFILE]}})
        hello = await receive_message("hello_ack")
        assert hello["protocol"] == "wl-live-v2"
        live, live_epoch = await open_source("local_native_live")
        recovery, recovery_epoch = await open_source("local_native_recovery", recovery=True)
        assert session.state["source_id"] == live["server_source_id"]

        # Recovery receipts prove original preservation while both model readers
        # remain empty of speech, including the source's embedded START/END flags.
        await deliver("local_native_recovery", recovery, recovery_epoch, recovery=True)
        assert media("main") == media("caption") == []
        assert session.state["source_id"] == live["server_source_id"]
        await deliver("local_native_live", live, live_epoch)
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in ended.values())), timeout=3)
        pcm_by_reader = {}
        for name in observed:
            events = media(name)
            assert events[0]["type"] == "activity_start" and events[-1]["type"] == "activity_end"
            assert [item["type"] for item in events].count("activity_start") == 1
            assert [item["type"] for item in events].count("activity_end") == 1
            assert all(item["type"] == "audio" for item in events[1:-1])
            pcm_by_reader[name] = b"".join(base64.b64decode(item["data"], validate=True) for item in events[1:-1])
        assert pcm_by_reader["main"] == pcm_by_reader["caption"]
        assert len(pcm_by_reader["main"]) == vector["decoded_pcm_samples"] * 2 == 3200
        assert any(pcm_by_reader["main"]), "Synthetic Opus vector must be decoded, not replaced with silence"
        caption_start = observed["caption"][0]["configuration"]
        assert caption_start["input_audio_transcription"]["mode"] == "VERBATIM"
        assert caption_start["native_caption_provenance"]["offset_clock"] == "unverified"
        projected = [item for item in media("caption") if item["type"] == "audio"]
        utterance = session.state["_current_utterance"]
        expected_turn = "turn_live_" + __import__("hashlib").sha256(
            f"{session.id}:{utterance['id']}".encode()).hexdigest()[:40]
        assert all(item["caption_scope"]["source_id"] == live["server_source_id"]
                   and item["caption_scope"]["utterance_id"] == utterance["id"]
                   and item["caption_scope"]["turn_id"] == expected_turn for item in projected)
        assert sum(item["capture_end_us"] - item["capture_start_us"] for item in projected) == 100_000

        # Open a separate product store connection after socket ACK/seal. Both
        # originals must be recoverable byte-for-byte without a private PCM file.
        reopened = DurableStore(tmp_path)
        inspection_io = EncodedVoiceIO()
        try:
            encoded = ProductEncodedAudioStore(reopened, actor_id=actor_id, conversation_id=conversation["id"], io=inspection_io)
            for source_id, bound, projected in (("local_native_live", live, 3200), ("local_native_recovery", recovery, 0)):
                recovered = await encoded.resume_source(source_id)
                assert recovered.server_durable_seq == 2 and recovered.server_source_id == bound["server_source_id"]
                stored = reopened.get_source(actor_id, recovered.server_source_id)
                assert stored["audio_format"] == "encoded-v2"
                assert stored["audio_projection_bytes"] == projected
                assert stored["audio_bytes"] == sum(len(base64.b64decode(chunk["payload_base64"])) for chunk in vector["chunks"])
                assert [row["payload"] for row in encoded.read_source_chunks(recovered.server_source_id)] == [
                    base64.b64decode(chunk["payload_base64"]) for chunk in vector["chunks"]]
            assert not list(tmp_path.rglob("*.pcm"))
        finally:
            inspection_io.close()
            reopened.close()
        assert not closes
    finally:
        if socket_task is not None and not socket_task.done():
            socket_task.cancel()
        if socket_task is not None:
            await asyncio.gather(socket_task, return_exceptions=True)
        await host.close()
        await host.close_audio_storage()
        store.close()
