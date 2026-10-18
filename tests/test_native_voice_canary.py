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
    assert canary.duration_us("1.123456789s") == 1123456
    assert canary.duration_us(1.5) is None
    assert canary.duration_us("1500") is None
    assert canary.duration_us("-1s") is None


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

