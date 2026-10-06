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
    assert sidecar.feed({"audio_base64": "AAAA"})
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
