import asyncio
from dataclasses import replace
import threading

import pytest

from live_interaction.audio_v2 import (
    CodecEpoch, DurablePCMProjection, EncodedChunk, EPOCH_END, SourceDescriptor, OutputChunk,
)
from projects_hub.encoded_voice_store import (
    EncodedStorageLimits, EncodedVoiceIO, ProductEncodedAudioStore,
)
from projects_hub.store import DurableStore, StoreError


@pytest.fixture
def voice(tmp_path):
    store = DurableStore(tmp_path)
    boot = store.ensure_dev_workspace("Compressed voice")
    actor = boot["actor"]["id"]
    conversation = store.create_conversation(actor, boot["workspace"]["id"])
    io = EncodedVoiceIO()
    encoded = ProductEncodedAudioStore(store, actor_id=actor, conversation_id=conversation["id"], io=io)
    yield store, encoded, actor, conversation, io
    io.close()
    store.close()


def epoch(name="epoch-1"):
    return CodecEpoch(name, "aac", "adts", 1, 1, 16000, 16000, 0, b"")


def chunk(number, *, name="epoch-1", payload=None, flags=0, start=None):
    return EncodedChunk("capture-1", name, number,
                        (number - 1) * 100_000 if start is None else start,
                        100_000, flags, payload or bytes([number]) * 20)


async def opened(encoded):
    bound = await encoded.open_source(SourceDescriptor("capture-1", 1_760_000_000_000))
    await encoded.register_epoch(bound, epoch())
    return bound


def test_output_original_event_and_played_cursor_survive_restart(voice, tmp_path, caplog):
    caplog.set_level("INFO", logger="projects_hub.live")
    store, _, actor, conversation, io = voice

    async def run():
        output = ProductEncodedAudioStore(store, actor_id=actor, conversation_id=conversation["id"],
                                          io=io, direction="downlink")
        bound = await opened(output)
        first = OutputChunk(chunk(1), 71)
        last = OutputChunk(chunk(2, flags=EPOCH_END), 72)
        await output.commit_output(bound, epoch(), first)
        await output.commit_output(bound, epoch(), last)
        await output.seal_source(bound, 2, 200_000)
        await output.mark_played(bound, 100_000)
        await output.mark_played(bound, 90_000)
        with pytest.raises(StoreError, match="different original"):
            await output.commit_output(bound, epoch(), OutputChunk(chunk(1), 999))
        with pytest.raises(StoreError, match="canonical event"):
            await output.commit_chunk(bound, epoch(), chunk(1))
        reopened_store = DurableStore(tmp_path)
        try:
            reopened = ProductEncodedAudioStore(reopened_store, actor_id=actor, conversation_id=conversation["id"],
                                                 io=io, direction="downlink")
            recovered = await reopened.resume_source("capture-1")
            status = await reopened.output_status(recovered)
            assert status.source.server_durable_seq == 2
            assert status.played_us == 100_000
            assert status.duration_us == 200_000 and status.complete and not status.cancelled
            assert await reopened.epoch(recovered, "epoch-1") == epoch()
            assert await reopened.read_output(recovered, after_seq=0, max_encoded_bytes=20,
                                              max_duration_us=200_000) == [first]
            assert await reopened.read_output(recovered, after_seq=1, max_encoded_bytes=20,
                                              max_duration_us=200_000) == [last]
            assert [item.source.source.source_id for item in await reopened.pending_outputs()] == ["capture-1"]
            await reopened.mark_played(recovered, 200_000)
            assert await reopened.pending_outputs() == []
            with pytest.raises(StoreError, match="exceeds"):
                await reopened.mark_played(recovered, 210_000)
            assert not list(tmp_path.glob("audio/*.pcm"))
        finally:
            reopened_store.close()

    asyncio.run(run())
    emitted = [record for record in caplog.records if record.name == "projects_hub.live"]
    durable = [r for r in emitted if getattr(r, "event", None) == "live_output_durable"]
    positions = [r for r in emitted if getattr(r, "event", None) == "live_output_playhead"]
    assert {r.kind for r in durable} == {"first_chunk", "terminal"}
    assert all(r.status == "server_durable" for r in durable)
    assert {r.duration_ms for r in positions} == {100, 200}
    assert all(r.status == "client_reported" for r in positions)
    assert all("payload" not in r.__dict__ and "text" not in r.__dict__ for r in emitted)


def test_new_live_session_manifest_excludes_yesterdays_unplayed_output_but_preserves_explicit_replay(voice):
    store, _, actor, conversation, io = voice
    conversation_id = conversation["id"]

    async def run():
        def output_for(session=None):
            return ProductEncodedAudioStore(
                store, actor_id=actor, conversation_id=conversation_id,
                io=io, direction="downlink", output_session_id=session,
            )

        old, today = "out_live_yesterday_1", "out_live_today_1"
        base = output_for()
        originals = {}
        for source_id in (old, today):
            bound = await base.open_source(SourceDescriptor(source_id, 0))
            await base.register_epoch(bound, epoch())
            payload = EncodedChunk(source_id, "epoch-1", 1, 0, 100_000, EPOCH_END, b"original-voice-" + source_id.encode())
            original = OutputChunk(payload, 42)
            await base.commit_output(bound, epoch(), original)
            await base.seal_source(bound, 1, 100_000)
            await base.mark_played(bound, 20_000)
            originals[source_id] = original

        # New Start never receives yesterday's audio in its automatic manifest.
        current = output_for("live_today")
        assert [item.source.source.source_id for item in await current.pending_outputs()] == [today]
        # A new socket in the SAME logical Live session preserves recovery.
        reattached = output_for("live_today")
        assert [item.source.source.source_id for item in await reattached.pending_outputs()] == [today]
        assert [item.source.source.source_id for item in await output_for("live_yesterday").pending_outputs()] == [old]

        # Original history, exact encoded bytes and real played cursor survive.
        archived = await current.resume_source(old)
        status = await current.output_status(archived)
        assert status.played_us == 20_000 and not status.cancelled
        assert await current.read_output(archived) == [originals[old]]
        assert len(await base.pending_outputs()) == 2  # archive is still present

    asyncio.run(run())


def test_cancelled_output_keeps_original_and_actual_played_position(voice):
    store, uplink, actor, conversation, io = voice

    async def run():
        output = ProductEncodedAudioStore(store, actor_id=actor, conversation_id=conversation["id"],
                                          io=io, direction="downlink")
        bound = await opened(output)
        original = OutputChunk(chunk(1, flags=EPOCH_END), 23)
        await output.commit_output(bound, epoch(), original)
        await output.mark_played(bound, 20_000)
        await output.cancel_output(bound)
        await output.seal_source(bound, 1, 100_000)
        status = await output.output_status(bound)
        assert status.cancelled and status.complete and status.played_us == 20_000
        assert await output.pending_outputs() == []
        assert await output.read_output(bound, after_seq=0, max_encoded_bytes=20,
                                         max_duration_us=100_000) == [original]
        with pytest.raises(StoreError, match="binding"):
            await uplink.resume_source("capture-1")
        with pytest.raises(StoreError, match="manifest"):
            await output.pending_outputs(limit=9)
        assert store.list_pending_voice_sources(actor, conversation["id"]) == []

    asyncio.run(run())


def test_output_replay_never_decodes_past_a_missing_original(voice):
    store, _, actor, conversation, io = voice

    async def run():
        output = ProductEncodedAudioStore(store, actor_id=actor, conversation_id=conversation["id"],
                                          io=io, direction="downlink")
        bound = await opened(output)
        later = OutputChunk(chunk(2, flags=EPOCH_END), 24)
        await output.commit_output(bound, epoch(), later)
        assert await output.read_output(bound) == []
        first = OutputChunk(chunk(1), 23)
        await output.commit_output(bound, epoch(), first)
        assert await output.read_output(bound) == [first, later]

    asyncio.run(run())


def test_hole_does_not_ack_after_restart_and_original_is_not_pcm(voice, tmp_path):
    store, encoded, actor, conversation, io = voice

    async def run():
        bound = await opened(encoded)
        second = await encoded.commit_chunk(bound, epoch(), chunk(2))
        assert second.server_durable_seq == 0
        reopened_store = DurableStore(tmp_path)
        try:
            reopened = ProductEncodedAudioStore(reopened_store, actor_id=actor, conversation_id=conversation["id"], io=io)
            recovered = await reopened.open_source(bound.source)
            assert recovered.server_durable_seq == 0
            first = await reopened.commit_chunk(recovered, epoch(), chunk(1))
            assert first.server_durable_seq == 2
            assert first.newly_committed
            repeated = await reopened.commit_chunk(recovered, epoch(), chunk(2))
            assert not repeated.newly_committed
            assert repeated.server_durable_seq == 2
            original = reopened_store.get_source(actor, recovered.server_source_id)
            assert (original["audio_bytes"], original["audio_chunks"], original["audio_format"]) == (40, 2, "encoded-v2")
            assert original["audio_projection_bytes"] == 0
            assert original["audio_duration_us"] == 200_000
            assert not list(tmp_path.glob("audio/*.pcm"))
            assert [row["payload"] for row in reopened.read_source_chunks(recovered.server_source_id)] == [chunk(1).payload, chunk(2).payload]
        finally:
            reopened_store.close()

    asyncio.run(run())


@pytest.mark.parametrize("change", [
    {"payload": b"different"}, {"capture_start_us": 1}, {"duration_us": 90_000}, {"flags": EPOCH_END},
])
def test_ack_retry_cannot_change_original_bytes_or_time(voice, change):
    store, encoded, actor, _, _ = voice

    async def run():
        bound = await opened(encoded)
        await encoded.commit_chunk(bound, epoch(), chunk(1))
        with pytest.raises(StoreError, match="different original"):
            await encoded.commit_chunk(bound, epoch(), replace(chunk(1), **change))
        assert store.get_source(actor, bound.server_source_id)["audio_bytes"] == 20
        assert encoded.read_source_chunks(bound.server_source_id)[0]["payload"] == chunk(1).payload

    asyncio.run(run())


def test_source_and_epoch_identity_are_actor_conversation_and_time_bound(voice):
    store, encoded, actor, conversation, io = voice

    async def run():
        bound = await opened(encoded)
        with pytest.raises(StoreError, match="capture time"):
            await encoded.open_source(replace(bound.source, started_at_unix_ms=42))
        with pytest.raises(StoreError, match="reinterpreted"):
            await encoded.register_epoch(bound, replace(epoch(), pre_skip=1024))
        other_conv = store.create_conversation(actor, conversation["workspace_id"])
        other = ProductEncodedAudioStore(store, actor_id=actor, conversation_id=other_conv["id"], io=io)
        with pytest.raises(StoreError) as different_conversation:
            await other.commit_chunk(bound, epoch(), chunk(1))
        assert different_conversation.value.code == "FORBIDDEN"
        stranger = ProductEncodedAudioStore(store, actor_id="actor-stranger", conversation_id=conversation["id"], io=io)
        with pytest.raises(StoreError) as different_actor:
            await stranger.commit_chunk(bound, epoch(), chunk(1))
        assert different_actor.value.code == "SOURCE_NOT_FOUND"

    asyncio.run(run())


def test_source_complete_requires_contiguous_bytes_exact_end_and_drained_codec(voice):
    store, encoded, _, _, _ = voice

    async def run():
        bound = await opened(encoded)
        await encoded.commit_chunk(bound, epoch(), chunk(2, flags=EPOCH_END))
        seal = await encoded.seal_source(bound, 2, 200_000)
        assert seal == {"server_durable_seq": 0, "complete": False}
        await encoded.commit_chunk(bound, epoch(), chunk(1))
        state = store.db.execute("SELECT * FROM encoded_voice_sources WHERE source_id=?", (bound.server_source_id,)).fetchone()
        assert (state["contiguous_seq"], state["complete"]) == (2, 1)
        with pytest.raises(StoreError, match="immutable"):
            await encoded.seal_source(bound, 3, 300_000)
        with pytest.raises(StoreError):
            await encoded.commit_chunk(bound, epoch(), chunk(3))

    asyncio.run(run())


def test_durable_prefix_without_codec_tail_is_not_complete(voice):
    _, encoded, _, _, _ = voice

    async def run():
        bound = await opened(encoded)
        await encoded.commit_chunk(bound, epoch(), chunk(1))
        with pytest.raises(StoreError, match="last valid"):
            await encoded.seal_source(bound, 1, 110_000)
        seal = await encoded.seal_source(bound, 1, 100_000)
        assert seal["server_durable_seq"] == 1
        assert seal["complete"] is False

    asyncio.run(run())


def test_out_of_order_codec_end_cannot_truncate_a_committed_later_packet(voice):
    _, encoded, _, _, _ = voice

    async def run():
        bound = await opened(encoded)
        await encoded.commit_chunk(bound, epoch(), chunk(3))
        with pytest.raises(StoreError, match="precedes"):
            await encoded.commit_chunk(bound, epoch(), chunk(2, flags=EPOCH_END))

    asyncio.run(run())


def test_codec_epochs_cannot_interleave_or_hide_timeline_gaps(voice):
    _, encoded, _, _, _ = voice

    async def run():
        bound = await opened(encoded)
        await encoded.commit_chunk(bound, epoch(), chunk(1))
        with pytest.raises(StoreError, match="continuous"):
            await encoded.commit_chunk(bound, epoch(), chunk(2, start=150_000))
        await encoded.register_epoch(bound, epoch("epoch-2"))
        await encoded.commit_chunk(bound, epoch("epoch-2"), chunk(3, name="epoch-2"))
        with pytest.raises(StoreError, match="interleave"):
            await encoded.commit_chunk(bound, epoch(), chunk(4))

    asyncio.run(run())


def test_quota_refusal_preserves_saved_original_and_durable_prefix(voice):
    store, encoded, actor, _, _ = voice
    encoded.limits = EncodedStorageLimits(source_bytes=100, total_bytes=40)

    async def run():
        bound = await opened(encoded)
        await encoded.commit_chunk(bound, epoch(), chunk(1))
        await encoded.commit_chunk(bound, epoch(), chunk(2))
        with pytest.raises(StoreError) as full:
            await encoded.commit_chunk(bound, epoch(), chunk(3))
        assert full.value.code == "SOURCE_STORAGE_FULL"
        assert (await encoded.open_source(bound.source)).server_durable_seq == 2
        assert store.get_source(actor, bound.server_source_id)["audio_bytes"] == 40
        assert len(encoded.read_source_chunks(bound.server_source_id)) == 2
        assert len(encoded.read_source_chunks(bound.server_source_id, max_bytes=20)) == 1

    asyncio.run(run())


def test_private_projection_is_fenced_without_permanent_pcm_copy(voice):
    store, encoded, actor, conversation, _ = voice

    async def run():
        bound = await opened(encoded)
        commit = await encoded.commit_chunk(bound, epoch(), chunk(1))
        with pytest.raises(StoreError, match="capture span"):
            encoded.claim_projection(DurablePCMProjection(commit, b"\0\0" * 2000, 16000, 0))
        projection = DurablePCMProjection(commit, b"\0\0" * 1600, 16000, 0)
        receipt = await encoded.io.run(encoded.claim_projection, projection)
        assert receipt["audio_bytes"] == 3200
        with pytest.raises(StoreError) as duplicate:
            encoded.claim_projection(projection)
        assert duplicate.value.code == "SOURCE_DELIVERY_UNKNOWN"
        source = store.get_source(actor, bound.server_source_id)
        assert source["audio_bytes"] == 20
        assert source["audio_projection_bytes"] == 3200
        for operation in (
            lambda: store.append_audio(actor, bound.server_source_id, b"\0\0"),
            lambda: store.reset_source_for_replay(actor, bound.server_source_id),
            lambda: store.voice_source_audio_path(actor, conversation["id"], bound.server_source_id),
        ):
            with pytest.raises(StoreError):
                operation()

    asyncio.run(run())


def test_stateful_decoder_can_project_two_ranges_from_one_durable_chunk(voice):
    store, encoded, actor, _, _ = voice

    async def run():
        bound = await opened(encoded)
        first = await encoded.commit_chunk(bound, epoch(), chunk(1))
        second = await encoded.commit_chunk(bound, epoch(), chunk(2))
        # The first packet leaves 10 ms inside the shared resampler.
        encoded.claim_projection(DurablePCMProjection(first, b"\1\0" * 1440, 16000, 0))
        encoded.claim_projection(DurablePCMProjection(second, b"\2\0" * 160, 16000, 90_000))
        encoded.claim_projection(DurablePCMProjection(second, b"\3\0" * 1600, 16000, 100_000))
        assert store.get_source(actor, bound.server_source_id)["audio_projection_bytes"] == 6400
        with pytest.raises(StoreError, match="overlaps"):
            encoded.claim_projection(DurablePCMProjection(second, b"\4\0" * 160, 16000, 95_000))

    asyncio.run(run())


def test_cancelled_waiter_keeps_disk_slot_until_real_commit_completion():
    async def run():
        io = EncodedVoiceIO(workers=1, pending=1)
        started, finish = threading.Event(), threading.Event()
        second_started = threading.Event()

        def first():
            started.set()
            finish.wait(2)

        try:
            task = asyncio.create_task(io.run(first))
            await asyncio.to_thread(started.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            second = asyncio.create_task(io.run(second_started.set))
            await asyncio.sleep(0.01)
            assert not second_started.is_set()
            assert io.slots.locked()
            finish.set()
            await asyncio.wait_for(second, 1)
            assert second_started.is_set()
        finally:
            finish.set()
            io.close()

    asyncio.run(run())
