"""No network/provider: immutable source replay and independent clock evidence."""
from dataclasses import replace
import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest


@pytest.fixture
def canary(monkeypatch):
    path = Path(__file__).parents[1] / "scripts" / "devcoveer_native_voice_canary.py"
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location("native_voice_canary_tested", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def original_db(tmp_path, canary):
    from live_interaction.audio_codec import PyAVAudioCodecFactory
    from live_interaction.audio_v2 import EncodedChunk, OPUS_PROFILE, PORTION_START, PORTION_END, EPOCH_END
    from projects_hub.encoded_voice_store import initialize_encoded_voice_schema
    path = tmp_path / "projects-hub.sqlite3"
    factory = PyAVAudioCodecFactory()
    encoder = factory.encoder(OPUS_PROFILE, "original_epoch", sample_rate=16000)
    encoded = encoder.encode(b"\x10\x01" * 3200, sample_rate=16000, end=True)
    descriptor = json.dumps(replace(encoder.epoch, decoded_sample_rate=16000).as_dict(), separators=(",", ":"), sort_keys=True)
    encoder.close()
    chunks, start = [], 0
    for i, (payload, duration, end) in enumerate(encoded):
        flags = (PORTION_START if i == 0 else 0) | (PORTION_END | EPOCH_END if end else 0)
        chunks.append(EncodedChunk("original", "original_epoch", i + 1, start, duration, flags, payload))
        start += duration
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sources(id TEXT PRIMARY KEY,client_source_id TEXT,audio_direction TEXT,conversation_id TEXT)")
        initialize_encoded_voice_schema(db)
        db.execute("INSERT INTO sources VALUES('original','device_original','uplink','conversation_original')")
        db.execute("INSERT INTO encoded_voice_sources(source_id,started_at_unix_ms,contiguous_seq,sealed_seq,capture_end_us,complete) VALUES(?,?,?,?,?,1)",
                   ("original", 123456789, len(chunks), len(chunks), start))
        db.execute("INSERT INTO encoded_voice_epochs(source_id,epoch_id,descriptor_json,descriptor_sha256,end_seq) VALUES(?,?,?,?,?)",
                   ("original", "original_epoch", descriptor, canary.digest(descriptor.encode()), len(chunks)))
        db.executemany("INSERT INTO encoded_voice_chunks(source_id,chunk_seq,epoch_id,capture_start_us,duration_us,flags,payload_sha256,payload) VALUES(?,?,?,?,?,?,?,?)",
            [("original", c.chunk_seq, c.epoch_id, c.capture_start_us, c.duration_us, c.flags, c.payload_sha256, c.payload) for c in chunks])
    return path, chunks


def test_original_bytes_epoch_and_capture_clock_survive_readonly_load(tmp_path, canary):
    path, chunks = original_db(tmp_path, canary)
    before = path.read_bytes()
    recording = canary.load_recording(path, "device_original")
    assert recording.chunks == tuple(chunks)
    assert recording.portions() == [{"capture_start_us": 0, "capture_end_us": 200000}]
    assert recording.receipt()["encoded_bytes"] == sum(len(c.payload) for c in chunks)
    assert path.read_bytes() == before
    with canary.open_original(path) as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("UPDATE encoded_voice_sources SET complete=0")


@pytest.mark.parametrize("mutation,code", [
    ("UPDATE encoded_voice_chunks SET payload=x'00' WHERE chunk_seq=1", "CHUNK_DIGEST"),
    ("UPDATE encoded_voice_epochs SET descriptor_sha256='changed'", "EPOCH_DIGEST"),
    ("UPDATE encoded_voice_sources SET complete=0", "COMPLETE_INPUT"),
    ("UPDATE encoded_voice_chunks SET capture_start_us=capture_start_us+10 WHERE chunk_seq=2", "CAPTURE_CLOCK"),
])
def test_corrupt_or_incomplete_original_is_not_a_provider_input(tmp_path, canary, mutation, code):
    path, _ = original_db(tmp_path, canary)
    with sqlite3.connect(path) as db:
        db.execute(mutation)
    with pytest.raises(ValueError, match=code):
        canary.load_recording(path, "original")


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic_mode", [False, True])
@pytest.mark.parametrize("whole_phrases,turns,outputs,interrupt_mode,passed", [
    (False, 2, 2, None, False),
    (True, 1, 2, None, False),
    (True, 2, 1, None, False),
    (True, 2, 2, None, True),
    (True, 2, 1, "valid", True),
    (False, 2, 1, "valid", False),
    (True, 2, 0, "duplicate", False),
    (True, 2, 1, "same_ended", False),
    (True, 2, 1, "unknown", False),
    (True, 2, 1, "wrong_generation", False),
    (True, 2, 1, "bool_generation", False),
    (True, 2, 1, "wrong_response", False),
    (True, 2, 0, "bool_response", False),
    (True, 2, 1, "stale_audio", False),
    (True, 2, 1, "epoch_only", False),
])
async def test_replay_retains_independent_predicates_on_timeout(
        tmp_path, canary, monkeypatch, whole_phrases, turns, outputs, interrupt_mode, passed, diagnostic_mode):
    from live_interaction.audio_v2 import AudioFrame, DOWNLINK_MAGIC, EPOCH_END, PORTION_START, PORTION_END
    path, _ = original_db(tmp_path, canary)
    # Keep the encoded original intact and mark two explicit speech portions.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE encoded_voice_chunks SET flags=flags|? WHERE chunk_seq=1", (PORTION_END,))
        db.execute("UPDATE encoded_voice_chunks SET flags=flags|? WHERE chunk_seq=2", (PORTION_START,))
    recording = canary.load_recording(path, "original")
    phrases = canary.DEFAULT_PHRASES[:2]
    final_texts = phrases if whole_phrases else ("Слышишь меня?", "Эта запись сделана без соединений и должна сохраниться.")

    async def inline_record_io(function, *args, **kwargs):
        # This is a protocol/receipt regression, not an executor scheduling gate.
        # Run the real write/flush/fsync and ACK ordering on the test thread.
        return function(*args, **kwargs)

    monkeypatch.setattr(canary.asyncio, "to_thread", inline_record_io)

    class Transport:
        """Transport is controlled; replay, receive, wait and record writes run."""
        def __init__(self):
            self.incoming = asyncio.Queue()
            self.frames = []
            self.source_id = None
            self.output_acks = []

        def __aiter__(self):
            return self

        async def __anext__(self):
            value = await self.incoming.get()
            if value is None:
                raise StopAsyncIteration
            return value

        def reply(self, value):
            self.incoming.put_nowait(json.dumps(value))

        def output_epoch(self, i):
            self.reply({"type": "output_epoch", "source_id": f"synthetic_output_{i}",
                "server_source_id": f"server_output_{i}", "stream_ref": i + 1, "epoch_ref": i + 1,
                "response_id": i + 1, "provider_response_id": i + 1,
                "connection_generation": 1})

        async def send(self, value):
            if isinstance(value, bytes):
                frame = AudioFrame.decode(value)
                self.frames.append(frame)
                self.reply({"type": "audio_ack", "source_id": self.source_id, "chunk_seq": frame.chunk_seq})
                return
            control = json.loads(value)
            kind = control["type"]
            if kind == "source_open":
                self.source_id = control["source_id"]
                self.reply({"type": "event", "event": {"type": "ready", "seq": 10}})
                self.reply({"type": "source_bound", "source_id": self.source_id,
                            "source_ref": 1, "server_source_id": "synthetic_server_source"})
            elif kind == "codec_epoch":
                self.reply({"type": "epoch_bound", "source_id": self.source_id,
                            "epoch_id": control["descriptor"]["epoch_id"], "epoch_ref": 1})
            elif kind == "source_seal":
                self.reply({"type": "source_sealed", "source_id": self.source_id,
                            "complete": True, "server_durable_seq": len(recording.chunks)})
                seq = 11
                for text in final_texts:
                    self.reply({"type": "event", "event": {"type": "input_transcript", "seq": seq, "text": text}})
                    seq += 1
                for _ in range(turns):
                    self.reply({"type": "event", "event": {"type": "turn_complete", "seq": seq}})
                    seq += 1
                for i in range(outputs):
                    self.output_epoch(i)
                    self.incoming.put_nowait(AudioFrame(DOWNLINK_MAGIC, EPOCH_END, i + 1, i + 1, 1,
                        0, 20_000, 1, seq, recording.chunks[-1].payload).encode())
                    seq += 1
                if interrupt_mode:
                    i = 0 if interrupt_mode == "same_ended" else outputs
                    if interrupt_mode not in {"unknown", "same_ended"}:
                        self.output_epoch(i)
                        if interrupt_mode != "epoch_only":
                            self.incoming.put_nowait(AudioFrame(DOWNLINK_MAGIC, 0, i + 1, i + 1, 1,
                                0, 20_000, 1, 10 if interrupt_mode == "stale_audio" else seq,
                                recording.chunks[0].payload).encode())
                    interrupt = {"type": "output_interrupt", "source_id": f"synthetic_output_{i}",
                        "provider_response_id": (999 if interrupt_mode == "wrong_response" else
                                                 True if interrupt_mode == "bool_response" else i + 1),
                        "connection_generation": (2 if interrupt_mode == "wrong_generation" else
                                                  True if interrupt_mode == "bool_generation" else 1)}
                    self.reply(interrupt)
                    if interrupt_mode == "duplicate":
                        self.reply(interrupt)
            elif kind == "source_unbind":
                self.reply({"type": "source_unbound", "source_id": self.source_id})
            elif kind == "output_ack":
                self.output_acks.append(control)

        async def close(self):
            self.incoming.put_nowait(None)

    transport = Transport()
    retained = {"passed": False}
    report = {"passed": False, "primary_passed": False, "replays": [retained],
              "caption_qualification": {"requested": False, "status": "not_requested"},
              "host_sessions_after_close": 0, "caption_sidecars_after_close": 0}
    with (tmp_path / "received.bin").open("wb") as output:
        wire = canary.ReplaySocket(transport, output, canary.CaptionEvidence(phrases), connection_generation=1)
        try:
            operation = wire.replay(recording, phrases, [0, 1], "g1", 1,
                {"live_window_bytes": 65536, "live_window_duration_us": 1_000_000}, .1,
                receipt=retained, diagnostic_continue_on_transcript_mismatch=diagnostic_mode)
            # Diagnostic continuation needs both real turn finals and terminal
            # output receipts. Missing verbatim words are NEVER accepted.
            diagnostic_complete = (diagnostic_mode and turns >= 2
                and outputs + int(interrupt_mode in {"valid", "duplicate"}) >= 2)
            if passed or diagnostic_complete:
                assert await operation is retained
            else:
                with pytest.raises(TimeoutError):
                    await operation
        finally:
            await wire.close()
    canary.update_acceptance(report)
    report_path = tmp_path / "partial.json"
    canary.save_report(report_path, report)
    durable = json.loads(report_path.read_text())
    result = durable["replays"][0]
    assert durable["primary_passed"] is False  # One replay is never the two-replay acceptance.
    assert durable["passed"] is False
    assert result["passed"] is passed
    assert result["phase"] == ("complete" if passed else "diagnostic_transcript_mismatch"
                               if diagnostic_complete else "await_turn_result")
    assert result.get("diagnostic_only") is ((diagnostic_mode and not passed)
                                             if passed or diagnostic_complete else None)
    assert result.get("error_type") == (None if passed or diagnostic_complete else "TimeoutError")
    assert result["client_frames_sent"] == result["server_durable_seq"] == len(recording.chunks)
    assert result["source_sealed"] is True
    assert result["main_phrase_matches"] == [whole_phrases, whole_phrases]
    assert result["missing_phrase_indices"] == ([] if whole_phrases else [0, 1])
    assert result["main_transcript_matches"] is whole_phrases
    assert result["turn_complete_count"] == turns
    assert result["turn_complete_required"] == result["output_ended_source_required"] == 2
    assert result["turn_complete_received"] is (turns >= 2)
    assert result["output_ended_source_count"] == outputs
    assert result["output_encoded_tail_received"] is (outputs >= 2)
    credited_interrupt = interrupt_mode in {"valid", "duplicate", "same_ended"}
    terminal_count = outputs + int(credited_interrupt and interrupt_mode != "same_ended")
    assert result["output_interrupted_source_count"] == int(credited_interrupt)
    assert result["output_interrupt_duplicate_count"] == int(interrupt_mode == "duplicate")
    assert result["output_terminal_source_count"] == terminal_count
    assert result["output_terminal_source_required"] == 2
    assert result["output_terminal_received"] is (terminal_count >= 2)
    assert result["output_playback_proven"] is False
    assert result["output_interrupt_unattributed_source_count"] == int(interrupt_mode in {"stale_audio", "epoch_only"})
    expected_rejection = {"unknown": "unknown_source", "wrong_response": "provider_response_id",
                          "bool_response": "provider_response_id",
                          "wrong_generation": "connection_generation", "bool_generation": "connection_generation"}.get(interrupt_mode)
    assert result["output_interrupt_rejections"] == ({expected_rejection: 1} if expected_rejection else {})
    if credited_interrupt:
        i = 0 if interrupt_mode == "same_ended" else outputs
        assert result["output_interrupt_receipts"] == [{"source_id": f"synthetic_output_{i}",
            "server_source_id": f"server_output_{i}", "provider_response_id": i + 1,
            "connection_generation": 1}]
    else:
        assert result["output_interrupt_receipts"] == []
    extra_audio = int(interrupt_mode is not None and interrupt_mode not in {"unknown", "same_ended", "epoch_only"})
    assert len(transport.output_acks) == outputs + extra_audio  # No ACK is fabricated for an interrupt.
    assert result["event_counts"] == {"input_transcript": 2, "turn_complete": turns}
    text = " ".join(final_texts)
    assert result["main_transcript_chars"] == len(text)
    assert result["normalized_transcript_sha256"] == canary.digest(canary.normalized(text).encode())
    assert all(phrase not in report_path.read_text() for phrase in phrases)
    assert [(f.flags, f.start_us, f.duration_us, f.payload) for f in transport.frames] == [
        (c.flags, c.capture_start_us, c.duration_us, c.payload) for c in recording.chunks]


def evidence(canary):
    phrases = ("Мира первая проверка голоса", "Мира другой более длинный контрольный вопрос")
    observer = canary.CaptionEvidence(phrases)
    # Same capture clock repeats after native transport reconnect; provider PCM
    # stream clock continues. Different portion durations distinguish the models.
    for n in (0, 1):
        source = f"source_{n}"
        observer.sources[source] = {"replay_id": f"g{n+1}", "expected_portions": [0, 1],
            "portions": [{"capture_start_us": 5_000_000, "capture_end_us": 6_000_000},
                         {"capture_start_us": 9_000_000, "capture_end_us": 11_000_000}]}
        base = n * 48000
        for start, end, cstart, cend in ((base, base+16000, 5_000_000, 6_000_000),
                                       (base+16000, base+48000, 9_000_000, 11_000_000)):
            observer.observe({"type": "caption_audio_sent", "provider_connection_generation": 1,
                "stream_start_sample": start, "stream_end_sample": end,
                "capture_start_us": cstart, "capture_end_us": cend,
                "caption_scope": {"source_id": source, "utterance_id": f"u{cstart}"}})
    return observer, phrases


def test_clock_hypothesis_uses_content_and_original_spans_not_arrival_order(canary):
    observer, phrases = evidence(canary)
    # B final can arrive before A; second replay can arrive before the first.
    for left, right, phrase in ((4, 6, phrases[1]), (1, 3, phrases[1]), (3, 4, phrases[0]), (0, 1, phrases[0])):
        observer.observe({"type": "caption_timing_probe", "provider_connection_generation": 1,
            "start_offset": f"{left}s", "end_offset": f"{right}s", "assigned": False,
            "offset_clock": "unverified", "observed_finished": True}, phrase)
    result = observer.result()
    assert result["all_replays_support_candidate_clock"] is True
    assert result["offset_clock"] == "unverified"
    assert result["production_clock_configuration_changed"] is False
    assert len(result["mapped_probes"]) == 4
    assert phrases[0] not in json.dumps(result, ensure_ascii=False)


def test_source_local_offsets_cannot_masquerade_as_continuing_provider_clock(canary):
    observer, phrases = evidence(canary)
    for _ in range(2):
        for left, right, phrase in ((0, 1, phrases[0]), (1, 3, phrases[1])):
            observer.observe({"type": "caption_timing_probe", "provider_connection_generation": 1,
                "start_offset": f"{left}s", "end_offset": f"{right}s"}, phrase)
    assert observer.result()["all_replays_support_candidate_clock"] is False


def test_late_caption_wrong_known_portion_does_not_match_even_with_same_length(canary):
    observer, phrases = evidence(canary)
    observer.observe({"type": "caption_timing_probe", "provider_connection_generation": 1,
        "start_offset": "1s", "end_offset": "3s"}, phrases[0])
    assert observer.result()["mapped_probes"] == []


@pytest.mark.parametrize("start,end,phrase_index,mapped", [
    ("3.608812500s", "5.528812500s", 1, True),
    ("3.608750s", "5.528812500s", 1, False),
    ("3.608812499s", "5.528812500s", 1, False),
    ("0s", "3.608812501s", 0, False),
])
def test_57741_sample_boundary_preserves_exact_offsets_and_rejects_crossing(
        canary, start, end, phrase_index, mapped):
    phrases = ("Мира первая проверка голоса", "Мира второй контрольный вопрос")
    observer = canary.CaptionEvidence(phrases)
    observer.sources["source"] = {"replay_id": "g1", "expected_portions": [0, 1],
        "portions": [{"capture_start_us": 5_000_000, "capture_end_us": 8_608_812},
                     {"capture_start_us": 20_000_000, "capture_end_us": 23_385_812}]}
    # Real probe lengths: 57,741 samples end on a half microsecond. The next
    # source portion starts on that exact PCM boundary, with a capture gap.
    for left, right, cstart, cend in ((0, 57_741, 5_000_000, 8_608_812),
                                    (57_741, 111_914, 20_000_000, 23_385_812)):
        observer.observe({"type": "caption_audio_sent", "provider_connection_generation": 1,
            "stream_start_sample": left, "stream_end_sample": right,
            "capture_start_us": cstart, "capture_end_us": cend,
            "caption_scope": {"source_id": "source", "utterance_id": f"u{cstart}"}})
    observer.observe({"type": "caption_timing_probe", "provider_connection_generation": 1,
        "start_offset": start, "end_offset": end}, phrases[phrase_index])
    result = observer.result()
    assert result["matched_replays"] == ([("g1", phrase_index)] if mapped else [])
    if mapped:
        assert result["mapped_probes"][0]["start_offset"] == start
        assert result["mapped_probes"][0]["end_offset"] == end
    assert result["all_replays_support_candidate_clock"] is False
    assert result["offset_clock"] == "unverified"


def test_probe_keeps_wire_offset_only_and_never_arbitrary_payload(canary):
    observer, _ = evidence(canary)
    observer.observe({"type": "caption_timing_probe", "provider_connection_generation": True,
        "start_offset": "0.125s", "end_offset": "1.125s", "observed_finished": "true",
        "authorization": "credential", "headers": {"x-key": "credential"}, "text": "private",
        "caption_scope": {"source_id": "safe", "token": "credential", "turn_id": "unsafe value"}}, "known phrase")
    encoded = json.dumps(observer.result())
    assert all(value not in encoded for value in ("credential", "private", "unsafe value", "known phrase"))
    assert "provider_connection_generation" not in observer.probes[-1]
    assert "observed_finished" not in observer.probes[-1]
    assert observer.probes[-1]["start_offset"] == "0.125s"


def test_duration_units_are_explicit_and_do_not_guess_seconds_vs_ms(canary):
    assert canary.duration_ns("1.123456789s") == 1123456789
    assert canary.duration_ns(1.5) is None
    assert canary.duration_ns("1500") is None
    assert canary.duration_ns("-1s") is None


def test_cli_preflight_reads_explicit_utp_receipt_from_real_artifact_layout(tmp_path, canary, capsys):
    evidence_dir = tmp_path / "smoke-evidence"
    store_dir = evidence_dir / "fixture-data"
    store_dir.mkdir(parents=True)
    original_db(store_dir, canary)
    (evidence_dir / "audio-verification.json").write_text('{"passed":true}')
    receipt = tmp_path / "app/build/outputs/connected_android_test_additional_output/debugAndroidTest/connected/emulator/voice-e2e/voice-flow.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"passed":true,"conversation_id":"conversation_original"}')
    assert not (evidence_dir / "voice-flow.json").exists()
    assert canary.main(["--fixture-evidence", str(evidence_dir), "--voice-flow", str(receipt),
                       "--source", "device_original"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["provider_calls_attempted"] is False
    assert result["fixture_receipts"]["voice-flow.json"]["path"] == str(receipt.resolve())
    assert result["fixture_receipts"]["voice-flow.json"]["sha256"] == canary.digest(receipt.read_bytes())


@pytest.mark.parametrize("flow,code", [
    ({"passed": "true", "conversation_id": "conversation_original"}, "NOT_PASSED"),
    ({"passed": True, "conversation_id": "different_run"}, "CONVERSATION_MISMATCH"),
])
def test_preflight_cannot_borrow_receipt_from_another_run_or_truthy_status(tmp_path, canary, flow, code):
    (tmp_path / "audio-verification.json").write_text('{"passed":true}')
    receipt = tmp_path / "voice-flow.json"
    receipt.write_text(json.dumps(flow))
    with pytest.raises(ValueError, match=code):
        canary.fixture_receipts(tmp_path, receipt, "conversation_original")


def test_optional_observer_overflow_sticks_as_failed_clock_evidence(canary):
    observer, phrases = evidence(canary)
    for left, right, phrase in ((0, 1, phrases[0]), (1, 3, phrases[1]), (3, 4, phrases[0]), (4, 6, phrases[1])):
        observer.observe({"type": "caption_timing_probe", "provider_connection_generation": 1,
                          "start_offset": f"{left}s", "end_offset": f"{right}s"}, phrase)
    assert observer.result()["all_replays_support_candidate_clock"] is True
    observer.probes.extend({"type": "caption_timing_probe"} for _ in range(10000-len(observer.probes)))
    observer.observe({"type": "caption_timing_probe"}, phrases[0])
    result = observer.result()
    assert len(observer.probes) == 10000
    assert result["observer_error"] == "CANARY_PROBE_BOUND"
    assert result["all_replays_support_candidate_clock"] is False


def test_cleanup_failure_is_sanitized_and_receipt_survives_setup_failure(tmp_path, canary):
    from devcoveer_canary_support import cleanup_step, run_reported
    import time
    path = tmp_path / "failed.json"
    report = {"passed": False, "provider_calls_attempted": False}

    async def refused_close():
        raise RuntimeError("Authorization: private-value")

    async def failed_setup(value):
        assert json.loads(path.read_text())["passed"] is False
        await cleanup_step(refused_close(), value, "socket_close", time.monotonic()+1)
        raise ValueError("private setup response")

    run_reported(path, report, failed_setup)
    result = json.loads(path.read_text())
    assert result["passed"] is False
    assert {e["error_type"] for e in result["errors"]} == {"RuntimeError", "ValueError"}
    assert "private" not in path.read_text()
    assert "Authorization" not in path.read_text()


def test_async_deadline_does_not_wait_for_late_cancellation_acknowledgement(canary):
    from devcoveer_canary_support import bounded

    async def scenario():
        release, cancelled = asyncio.Event(), asyncio.Event()

        async def late_close():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()

        task = asyncio.create_task(late_close())
        with pytest.raises(TimeoutError, match="CANARY_ASYNC_DEADLINE"):
            await bounded(task, .01)
        await asyncio.wait_for(cancelled.wait(), .1)
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, .1)

    asyncio.run(scenario())


def test_installed_provider_environment_uses_candidate_allowlist_and_one_fixed_path(canary, monkeypatch):
    import devcoveer_canary_support as support
    installer = support.load_candidate_installer()
    read_paths = []

    def installed_provider_file(path):
        read_paths.append(path)
        return {"AI_RESOURCE_CONTROL_URL": "https://test.invalid", "AI_RESOURCE_CONTROL_SERVICE_KEY": "synthetic-test-key",
                "PROJECTS_HUB_DEV_AUTH": "true", "UNRELATED_SECRET": "not-imported"}

    monkeypatch.setattr(installer, "_parse_env", installed_provider_file)
    monkeypatch.setattr(support, "load_candidate_installer", lambda: installer)
    monkeypatch.setattr(support.os, "environ", {})
    support.use_installed_provider_environment()
    assert read_paths == [installer.PROVIDER_ENV]
    assert set(support.os.environ) == {"AI_RESOURCE_CONTROL_URL", "AI_RESOURCE_CONTROL_SERVICE_KEY"}


def test_success_is_not_published_before_loop_cleanup_completes(tmp_path, canary, monkeypatch):
    import devcoveer_canary_support as support
    path = tmp_path / "cleanup-failure.json"

    async def checks_passed(report):
        report["passed"] = True

    async def failed_loop_shutdown(report):
        assert json.loads(path.read_text())["passed"] is False
        raise TimeoutError("CANARY_LOOP_TASKS_UNJOINED")

    monkeypatch.setattr(support, "_finish_loop", failed_loop_shutdown)
    support.run_reported(path, {"passed": False}, checks_passed)
    result = json.loads(path.read_text())
    assert result["passed"] is False
    assert result["checks_passed_before_loop_shutdown"] is True
    assert result["errors"][0]["error_code"] == "CANARY_LOOP_TASKS_UNJOINED"


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [False, True])
async def test_caption_qualification_opt_in_controls_actual_sidecar_start(canary, requested):
    from projects_hub.live_transcription import CaptionSidecar
    starts, events = [], []

    async def inspect_start(**kwargs):
        # Observe the real sidecar's admission boundary without any provider call.
        starts.append(json.loads(await kwargs["reader"].readline()))

    sidecar = CaptionSidecar(environment={}, binding="canary", vocabulary=[], emit=events.append,
                            provider_run=inspect_start, guarded_runner=inspect_start)
    observer = canary.CaptionEvidence(("Мира первая проверка", "Мира второй вопрос"))
    qualification = canary.configure_caption_qualification(sidecar, observer, requested=requested)
    try:
        sidecar.start(native=True)
        assert sidecar.native_offset_clock == "unverified"
        assert events[0] == {"type": "caption_unavailable", "code": "CAPTION_CLOCK_UNVERIFIED"}
        if requested:
            assert sidecar.diagnostic_observer.__self__ is observer
            await asyncio.wait_for(sidecar.task, 1)
            assert len(starts) == 1
            config = starts[0]["configuration"]
            assert config["input_audio_transcription"]["mode"] == "VERBATIM"
            assert config["native_caption_provenance"] == {"offset_clock": "unverified", "diagnostics": True}
            assert qualification["status"] == "unverified"
        else:
            assert sidecar.diagnostic_observer is None
            assert sidecar.task is None
            assert starts == []
            assert qualification["status"] == "not_requested"
        assert qualification["observer_enabled"] is requested
    finally:
        await sidecar.stop()


@pytest.mark.parametrize("requested", [False, True])
def test_missing_optional_observer_has_an_explicit_result_without_aborting_primary(canary, requested):
    observer = canary.CaptionEvidence(("первая проверка", "второй вопрос"))
    result = canary.configure_caption_qualification(None, observer, requested=requested)
    assert result["observer_enabled"] is False
    assert result["status"] == ("failed" if requested else "not_requested")
    assert (result.get("code") == "CANARY_TIMESTAMP_OBSERVER_UNAVAILABLE") is requested


@pytest.mark.parametrize("qualify,primary_checks,caption_status,failure,primary_passed,passed", [
    (False, True, "not_requested", None, True, True),
    (True, True, "failed", None, True, False),
    (True, True, "unverified", None, True, False),
    (True, True, "passed", None, True, True),
    (True, False, "passed", None, False, False),
    (False, True, "not_requested", "live_stop", False, False),
    (True, True, "passed", "loop_shutdown", False, False),
    (False, True, "not_requested", "remaining_sidecar", False, False),
])
def test_cli_reports_primary_and_requested_caption_gates_after_cleanup(
        tmp_path, canary, monkeypatch, capsys, qualify, primary_checks, caption_status,
        failure, primary_passed, passed):
    import devcoveer_canary_support as support
    from live_interaction.audio_v2 import PORTION_START, PORTION_END
    evidence_dir = tmp_path / "smoke-evidence"
    store_dir = evidence_dir / "fixture-data"
    store_dir.mkdir(parents=True)
    original, _ = original_db(store_dir, canary)
    # Two explicit portions in one intact synthetic original; the real CLI still
    # validates its hashes, capture clock and matching Android fixture receipts.
    with sqlite3.connect(original) as db:
        db.execute("UPDATE encoded_voice_chunks SET flags=flags|? WHERE chunk_seq=1", (PORTION_END,))
        db.execute("UPDATE encoded_voice_chunks SET flags=flags|? WHERE chunk_seq=2", (PORTION_START,))
    (evidence_dir / "audio-verification.json").write_text('{"passed":true}')
    flow = tmp_path / "voice-flow.json"
    flow.write_text('{"passed":true,"conversation_id":"conversation_original"}')
    path = tmp_path / "report.json"
    saved = []
    save_report = canary.save_report

    def capture_save(destination, report):
        save_report(destination, report)
        saved.append(json.loads(destination.read_text()))

    monkeypatch.setattr(canary, "save_report", capture_save)
    monkeypatch.setattr(support, "save_report", capture_save)

    async def replay_receipt(args, recording, report):
        # The operation is controlled; report/CLI and final loop cleanup are real.
        assert args.qualify_captions is qualify
        assert len(recording.portions()) == 2
        report.update(primary_checks_passed_before_cleanup=primary_checks,
                      host_sessions_after_close=0,
                      caption_sidecars_after_close=1 if failure == "remaining_sidecar" else 0)
        report["caption_qualification"]["status"] = caption_status
        if failure == "live_stop":
            support.record_error(report, TimeoutError("CANARY_ASYNC_DEADLINE"), phase="live_stop")
        canary.update_acceptance(report, loop_shutdown_pending=True)
        assert report["primary_passed"] is False
        assert report["passed"] is False
        canary.save_report(path, report)

    if failure == "loop_shutdown":
        async def failed_shutdown(report):
            assert json.loads(path.read_text())["primary_passed"] is False
            raise TimeoutError("CANARY_LOOP_TASKS_UNJOINED")
        monkeypatch.setattr(support, "_finish_loop", failed_shutdown)
    monkeypatch.setattr(canary, "run_provider", replay_receipt)
    arguments = ["--fixture-evidence", str(evidence_dir), "--voice-flow", str(flow),
                 "--source", "device_original", "--phrase", "первая проверка", "--phrase", "второй вопрос",
                 "--data-dir", str(tmp_path / "fresh-data"), "--report", str(path), "--run-provider"]
    if qualify:
        arguments.append("--qualify-captions")
    assert canary.main(arguments) == (0 if passed else 1)
    report = json.loads(path.read_text())
    summary = json.loads(capsys.readouterr().out)
    assert len(saved) >= 4
    assert all(item["passed"] is False and item["primary_passed"] is False for item in saved[:-1])
    assert report["checks_passed_before_loop_shutdown"] is False
    for result in (report, summary):
        assert result["primary_passed"] is primary_passed
        assert result["passed"] is passed
        assert result["caption_qualification"]["requested"] is qualify
        assert result["caption_qualification"]["status"] == caption_status
        assert result["provider_calls_attempted"] is False
