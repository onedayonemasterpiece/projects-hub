from __future__ import annotations

import asyncio
import json

import pytest

from projects_hub.live_transcription import (
    CAPTION_MODEL,
    TRANSCRIBE_PCM_CHUNK_BYTES,
    CaptionSidecar,
    _BoundedJsonReader,
    _mirror_messages,
)


def test_mirror_only_audio_and_activity_boundaries():
    assert _mirror_messages({"activity_start": True}) == [{"type": "activity_start"}]
    assert _mirror_messages({"audio_base64": "AAAA"}) == [{"type": "audio", "data": "AAAA"}]
    assert _mirror_messages({"activity_end": True}) == [{"type": "activity_end"}]
    assert _mirror_messages({"audio_stream_end": True}) == [{"type": "audio_stream_end"}]
    assert _mirror_messages({"text": "semantic command"}) == []


@pytest.mark.asyncio
async def test_bounded_reader_never_blocks_main_input():
    reader = _BoundedJsonReader(max_pending_bytes=80)
    assert reader.feed({"type": "audio", "data": "AAAA"})
    assert not reader.feed({"type": "audio", "data": "x" * 100})
    first = json.loads((await reader.readline()).decode())
    assert first["type"] == "audio"
    reader.close()
    assert await reader.readline() == b""


@pytest.mark.asyncio
async def test_sidecar_is_smart_ui_only_and_maps_transcripts():
    seen = []
    starts = []
    mirrored = []

    async def fake_runner(**kwargs):
        start = json.loads((await kwargs["reader"].readline()).decode())
        starts.append(start)
        kwargs["on_event"]({"type": "ready", "model": CAPTION_MODEL})
        while True:
            raw = await kwargs["reader"].readline()
            if not raw:
                return
            item = json.loads(raw.decode())
            if item.get("type") == "stop":
                return
            mirrored.append(item)
            if item.get("type") == "audio":
                kwargs["on_event"]({"type": "interim_input_transcript", "text": "живой текст"})
            if item.get("type") == "activity_end":
                kwargs["on_event"]({"type": "input_transcript", "text": "готовый текст"})

    async def fake_provider(**_kwargs):
        raise AssertionError("fake runner must own provider invocation")

    sidecar = CaptionSidecar(
        environment={"X": "1"},
        binding="conversation:caption",
        vocabulary=["Мира", "Projects Hub"],
        emit=seen.append,
        provider_run=fake_provider,
        guarded_runner=fake_runner,
    )
    sidecar.start()
    await asyncio.sleep(0)
    assert sidecar.feed({"activity_start": True})
    assert sidecar.feed({"audio_base64": "AAAAAA=="})  # two complete PCM16 samples
    assert sidecar.feed({"activity_end": True})
    await asyncio.sleep(0.02)
    await sidecar.stop()

    config = starts[0]["configuration"]
    assert starts[0]["model"] == CAPTION_MODEL
    assert config["input_audio_transcription"]["mode"] == "SMART"
    assert config["input_audio_transcription"]["languageCodes"] == ["ru-RU", "en-US"]
    assert config["input_audio_transcription"]["customVocabulary"] == ["Мира", "Projects Hub"]
    assert [item["type"] for item in mirrored[:3]] == ["activity_start", "audio", "activity_end"]
    assert any(item["type"] == "caption_interim_transcript" and item["text"] == "живой текст" for item in seen)
    assert any(item["type"] == "caption_final_transcript" and item["text"] == "готовый текст" for item in seen)


@pytest.mark.asyncio
async def test_sidecar_failure_is_fail_open_and_reported_once():
    seen = []

    async def failing_runner(**_kwargs):
        raise RuntimeError("provider down")

    async def fake_provider(**_kwargs):
        return None

    sidecar = CaptionSidecar(
        environment={},
        binding="caption",
        vocabulary=[],
        emit=seen.append,
        provider_run=fake_provider,
        guarded_runner=failing_runner,
    )
    sidecar.start()
    await asyncio.sleep(0.01)
    assert sidecar.available is False
    assert sidecar.feed({"audio_base64": "AAAA"}) is False
    assert len([item for item in seen if item["type"] == "caption_unavailable"]) == 1


@pytest.mark.asyncio
async def test_sidecar_batches_main_pcm_to_google_recommended_100ms_chunks():
    seen = []
    mirrored = []

    async def fake_runner(**kwargs):
        await kwargs["reader"].readline()  # start
        kwargs["on_event"]({"type": "ready", "model": CAPTION_MODEL})
        while True:
            raw = await kwargs["reader"].readline()
            if not raw:
                return
            item = json.loads(raw.decode())
            if item.get("type") == "stop":
                return
            mirrored.append(item)

    async def fake_provider(**_kwargs):
        return None

    sidecar = CaptionSidecar(
        environment={},
        binding="caption",
        vocabulary=[],
        emit=seen.append,
        provider_run=fake_provider,
        guarded_runner=fake_runner,
    )
    sidecar.start()
    await asyncio.sleep(0)
    sidecar.feed({"activity_start": True})
    # Four 25 ms PCM fragments should become one 100 ms provider chunk.
    fragment = b"\x01\x00" * 400
    encoded = __import__("base64").b64encode(fragment).decode()
    for _ in range(4):
        assert sidecar.feed({"audio_base64": encoded})
    assert sidecar.feed({"activity_end": True})
    await asyncio.sleep(0.02)
    await sidecar.stop()

    audio = [item for item in mirrored if item["type"] == "audio"]
    assert len(audio) == 1
    decoded = __import__("base64").b64decode(audio[0]["data"])
    assert len(decoded) == TRANSCRIBE_PCM_CHUNK_BYTES
    assert [item["type"] for item in mirrored] == ["activity_start", "audio", "activity_end"]


def _native_scope(turn="A", sequence=1):
    return {"session_id": "session", "conversation_id": "conversation", "source_id": "source",
            "utterance_id": "utterance_" + turn, "turn_id": "turn_" + turn,
            "utterance_sequence": sequence}


@pytest.mark.asyncio
async def test_native_unverified_uses_no_caption_lease_but_reports_accepted_turns_and_unavailability():
    seen = []

    async def forbidden_runner(**kwargs):
        raise AssertionError("unverified native captions must not reserve a second provider")

    sidecar = CaptionSidecar(environment={}, binding="caption", vocabulary=["Мира"],
                            emit=seen.append, provider_run=forbidden_runner, guarded_runner=forbidden_runner)
    sidecar.start(native=True)
    assert sidecar.task is None
    sidecar.feed({"activity_start": True})
    assert not [event for event in seen if event["type"] == "caption_turn_started"]
    sidecar.feed_pcm(b"\x01\x00" * 800, caption_scope=_native_scope(), capture_start_us=500_000)
    sidecar.feed({"activity_end": True})
    sidecar._on_event({"type": "input_transcript", "text": "Мира, Projects Hub, Codex"})
    assert [event["type"] for event in seen] == ["caption_unavailable", "caption_turn_started", "caption_turn_ended"]
    assert seen[0]["code"] == "CAPTION_CLOCK_UNVERIFIED"
    assert seen[-1]["caption_scope"] == _native_scope()
    assert seen[-1]["capture_end_us"] == 550_000
    assert sidecar.reader.pending_bytes == 0
    await sidecar.stop()
    count = len(seen)
    sidecar.feed_pcm(b"\x01\x00" * 800, caption_scope=_native_scope("B", 2), capture_start_us=600_000)
    sidecar._on_event({"type": "input_transcript", "text": "late"})
    assert len(seen) == count


@pytest.mark.asyncio
async def test_native_probe_keeps_real_pcm_scope_and_ephemeral_text_out_of_public_events():
    seen, messages, probes = [], [], []

    async def fixture_runner(**kwargs):
        while raw := await kwargs["reader"].readline():
            item = json.loads(raw)
            messages.append(item)
            if item["type"] == "stop":
                return

    sidecar = CaptionSidecar(environment={}, binding="caption", vocabulary=["Мира"], emit=seen.append,
                            provider_run=fixture_runner, guarded_runner=fixture_runner)
    sidecar.diagnostic_observer = lambda event, text=None: probes.append((event, text))
    sidecar.start(native=True)
    sidecar.feed({"activity_start": True})
    sidecar.feed_pcm(b"\x01\x00" * 400, caption_scope=_native_scope(), capture_start_us=500_000)
    sidecar.feed_pcm(b"\x02\x00" * 400, caption_scope=_native_scope(), capture_start_us=525_000)
    sidecar.feed({"activity_end": True})
    sidecar.feed({"activity_start": True})
    sidecar.feed_pcm(b"\x03\x00" * 800, caption_scope=_native_scope("B", 2), capture_start_us=900_000)
    sidecar.feed({"activity_end": True})
    from live_interaction.caption_provenance import CaptionAudioLedger
    ledger = CaptionAudioLedger()
    ledger.connected()
    await asyncio.sleep(0)
    for item in messages:
        if item["type"] == "audio":
            sidecar._on_event(ledger.sent(item))
    event = ledger.transcript("input_transcript", {"text": "Мира, привет", "startOffset": "0s", "endOffset": "0.05s"})
    sidecar._on_event(event)
    await sidecar.stop()
    start = messages[0]["configuration"]
    assert start["input_audio_transcription"]["mode"] == "VERBATIM"
    assert start["native_caption_provenance"] == {"offset_clock": "unverified", "diagnostics": True}
    audio = [item for item in messages if item["type"] == "audio"]
    assert len(audio) == 2
    assert [item["caption_scope"] for item in audio] == [_native_scope(), _native_scope("B", 2)]
    assert [(item["capture_start_us"], item["capture_end_us"]) for item in audio] == [(500_000, 550_000), (900_000, 950_000)]
    assert probes[-1][1] == "Мира, привет"
    assert probes[-1][0]["type"] == "caption_timing_probe"
    assert probes[-1][0]["assigned"] is False
    assert "text" not in probes[-1][0]
    assert not [event for event in seen if event["type"] in {"caption_timing_probe", "caption_audio_sent",
                                                          "caption_interim_transcript", "caption_final_transcript"}]


def test_runtime_fanout_is_post_acceptance_fail_open_and_lifecycle_bound():
    runtime = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src/projects_hub/live_runtime.py"
    ).read_text(encoding="utf-8")
    assert 'binding=f"{resource_id}:caption:{session.id}"' in runtime
    assert "result = await super().input(" in runtime
    assert runtime.index("result = await super().input(") < runtime.index("sidecar.feed(message)")
    assert "await sidecar.stop()" in runtime
    assert '"caption_enabled": session.id in self._caption_sidecars' in runtime


def test_sidecar_uses_projects_hub_resource_consumer_with_separate_binding():
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src/projects_hub/live_transcription.py"
    ).read_text(encoding="utf-8")
    assert 'consumer="projects-hub"' in source
    assert 'consumer="projects-hub-caption"' not in source
