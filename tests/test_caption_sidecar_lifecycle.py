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
