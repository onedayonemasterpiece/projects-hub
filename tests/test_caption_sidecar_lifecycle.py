import asyncio

import pytest

from projects_hub.live_transcription import CaptionSidecar


@pytest.mark.asyncio
async def test_caption_sidecar_concurrent_stop_is_idempotent():
    seen = []

    async def fake_runner(**kwargs):
        await kwargs["reader"].readline()  # start
        while True:
            raw = await kwargs["reader"].readline()
            if not raw:
                return
            if b'"type":"stop"' in raw:
                return

    async def fake_provider(**_kwargs):
        return None

    sidecar = CaptionSidecar(
        environment={},
        binding="caption:test",
        vocabulary=[],
        emit=seen.append,
        provider_run=fake_provider,
        guarded_runner=fake_runner,
    )
    sidecar.start()
    await asyncio.sleep(0)
    await asyncio.gather(sidecar.stop(), sidecar.stop())

    assert sidecar._stopped is True
    assert sidecar.task is not None and sidecar.task.done()
    assert not [event for event in seen if event.get("type") == "caption_unavailable"]


@pytest.mark.asyncio
async def test_unverified_native_caption_clock_does_not_schedule_second_model_or_chatter():
    seen, calls = [], []

    async def no_extra_provider(**kwargs):
        calls.append(kwargs)
        raise AssertionError("Native unverified clock cannot start caption provider")

    sidecar = CaptionSidecar(
        environment={}, binding="caption:native:scope", vocabulary=[], emit=seen.append,
        provider_run=no_extra_provider, guarded_runner=no_extra_provider,
        native_offset_clock="unverified",
    )
    sidecar.start(native=True)
    assert sidecar.task is None
    for _ in range(50):
        assert sidecar.feed_pcm(b"\x01\x00" * 1600, caption_scope=None, capture_start_us=0) is False
        assert sidecar.feed({"activity_start": True}) is False
        assert sidecar.feed({"activity_end": True}) is False
    assert [event["type"] for event in seen] == ["caption_unavailable"]
    assert seen[0]["code"] == "CAPTION_CLOCK_UNVERIFIED"
    assert calls == []
    await sidecar.stop()
    assert sidecar._stopped
