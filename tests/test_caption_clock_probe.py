"""Offline clock evidence checks; no provider, no Android acceptance claims."""
import importlib.util
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import wave

import pytest


@pytest.fixture
def probe(monkeypatch):
    path = Path(__file__).parents[1] / "scripts" / "devcoveer_caption_clock_probe.py"
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location("caption_clock_probe_tested", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def observed(probe, second_generation=1, second_offset="1s"):
    # Prefixes contain both markers but deliberately omit each phrase's ending.
    phrases = ("Мира, проверка голосового разговора", "Мира, второй вопрос после")
    evidence = probe.SyntheticClockEvidence(("hello", "overlap"))
    evidence.sources["synthetic"] = {"replay_id": "provider_instance_1", "expected_portions": [0, 0, 1, 1],
        "portions": [{"capture_start_us": 5_000_000, "capture_end_us": 6_000_000},
                     {"capture_start_us": 20_000_000, "capture_end_us": 22_000_000}]}
    for generation, start, end, cstart, cend in ((1, 0, 16000, 5_000_000, 6_000_000),
                                               (second_generation, 16000, 48000, 20_000_000, 22_000_000)):
        evidence.observe({"type": "caption_audio_sent", "provider_connection_generation": generation,
            "stream_start_sample": start, "stream_end_sample": end, "capture_start_us": cstart,
            "capture_end_us": cend, "caption_scope": {"source_id": "synthetic"}})
    # Explicitly reverse callback order. Provider offsets, not the callback time,
    # select the two actual successful sent spans.
    for generation, start, end, phrase in ((second_generation, second_offset, "3s", phrases[1]),
                                          (1, "0s", "1s", phrases[0])):
        evidence.observe({"type": "caption_timing_probe", "provider_connection_generation": generation,
            "start_offset": start, "end_offset": end}, phrase)
    return evidence


def test_two_utterances_share_one_actual_connection_without_native_acceptance(probe):
    evidence = observed(probe)
    assert evidence.result()["all_replays_support_candidate_clock"] is False
    result = probe.connection_result(evidence)
    assert result["passed"] is True
    assert result["provider_generations_observed"] == [1]
    assert result["offset_clock"] == "unverified"
    assert "all_replays_support_candidate_clock" not in result


def test_utterances_split_across_provider_generations_cannot_prove_same_connection_clock(probe):
    assert probe.connection_result(observed(probe, second_generation=2))["passed"] is False


def test_utterance_local_reset_offset_does_not_prove_connection_pcm_clock(probe):
    result = probe.connection_result(observed(probe, second_offset="0s"))
    assert result["passed"] is False
    assert {row["utterance_index"] for row in result["unmapped_expected_markers"]} == {1}


def test_interim_prefix_and_final_suffix_without_offsets_prove_only_marker_clock(probe):
    evidence = observed(probe)
    evidence.observe({"type": "caption_timing_probe", "provider_connection_generation": 1,
                      "transcript_kind": "input_transcript"}, "слышишь меня?")
    result = probe.connection_result(evidence)
    assert result["passed"] is True
    assert len(result["mapped_probes"]) == 4  # Two disjoint markers per utterance.
    assert result["full_transcript_recognition_quality_verified"] is False
    assert result["final_transcripts_without_offsets"] == 1
    assert "start_offset" not in result["timing_probes"][-1]
    assert "слышишь меня" not in str(result)


def test_accumulated_text_with_markers_from_two_utterances_is_not_assigned_to_latest(probe):
    evidence = observed(probe)
    evidence.probes.clear()
    evidence.observe({"type": "caption_timing_probe", "provider_connection_generation": 1,
        "start_offset": "1s", "end_offset": "3s"},
        "Мира проверка голосового разговора. Мира второй вопрос после")
    assert probe.connection_result(evidence)["passed"] is False
    assert evidence.result()["mapped_probes"] == []


def test_synthetic_transcript_observation_is_opt_in_bounded_and_never_infers_finality(probe):
    text = "синтетический хвост " * 100
    event = {"type": "caption_timing_probe", "provider_connection_generation": 1,
             "transcript_kind": "input_transcript", "headers": {"authorization": "not-allowed"}}
    off = probe.SyntheticClockEvidence(("hello", "overlap"))
    off.observe(event, text)
    assert "synthetic_transcript_observations" not in off.result()
    on = probe.SyntheticClockEvidence(("hello", "overlap"), observe_synthetic_transcripts=True)
    for _ in range(probe.MAX_SYNTHETIC_OBSERVATIONS+1):
        on.observe(event, text)
    result = on.result()
    rows = result["synthetic_transcript_observations"]
    assert len(rows) == probe.MAX_SYNTHETIC_OBSERVATIONS
    assert result["synthetic_observation_truncated"] is True
    assert all(row["text_truncated"] and len(row["text"]) <= probe.MAX_SYNTHETIC_TEXT_CHARS for row in rows)
    assert all(row["assigned"] is False and row["native_capture"] is False for row in rows)
    assert all("observed_finished" not in row and "start_offset" not in row for row in rows)
    assert "not-allowed" not in str(result)


def test_wav_preflight_is_bounded_resampled_and_cannot_accept_silence(tmp_path, probe):
    for name in ("hello", "overlap"):
        with wave.open(str(tmp_path / (name + ".wav")), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(22050)
            output.writeframes(b"\x00\x04" * 22050)
    speech = probe.load_speech(tmp_path, ("hello", "overlap"))
    assert all(item["pcm_samples"] == 16000 and item["duration_us"] == 1_000_000 for item in speech)
    with wave.open(str(tmp_path / "hello.wav"), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 16000)
    with pytest.raises(ValueError, match="SILENT"):
        probe.load_speech(tmp_path, ("hello", "overlap"))


def test_provider_tee_preserves_callbacks_and_only_records_real_bounded_lifecycle(probe):
    evidence = probe.SyntheticClockEvidence(("hello", "overlap"))
    forwarded, seen = [], {}
    reader, guard = object(), object()
    events = [{"type": "ready", "untrusted": "not-allowed"},
              {"type": "input_transcript", "text": "not-allowed"},
              {"type": "turn_complete"}]

    async def provider(**kwargs):
        seen.update(kwargs)
        for event in events:
            kwargs["on_event"](event)
        return "provider-result"

    result = asyncio.run(probe.observed_provider_run(provider, evidence)(
        on_event=forwarded.append, reader=reader, resource_guard=guard))
    assert result == "provider-result"
    assert seen["reader"] is reader and seen["resource_guard"] is guard
    assert all(got is original for got, original in zip(forwarded, events))
    assert len(forwarded) == len(events)
    observed = evidence.result()
    assert observed["turn_complete_events_observed"] == 1
    assert [row["type"] for row in observed["provider_lifecycle"]] == ["ready", "turn_complete"]
    assert all(row["assigned"] is False for row in observed["provider_lifecycle"])
    assert "not-allowed" not in str(observed)
    assert all("observed_finished" not in row and "provider_connection_generation" not in row
               and "utterance_id" not in row for row in observed["provider_lifecycle"])
    for _ in range(probe.MAX_PROVIDER_LIFECYCLE_EVENTS):
        evidence.observe_provider_event({"type": "turn_complete"})
    assert len(evidence.provider_lifecycle) == probe.MAX_PROVIDER_LIFECYCLE_EVENTS
    assert evidence.provider_lifecycle_truncated is True


def test_misrecognized_first_phrase_still_collects_both_utterances_and_connections(tmp_path, monkeypatch, probe):
    from projects_hub import live_resources, live_transcription

    instances = []

    class Sidecar:
        """Simulated bad ASR; only exercises collection, never provider acceptance."""
        def __init__(self, **kwargs):
            # A second provider instance must follow an actually joined first.
            assert all(item.task.done() for item in instances)
            instances.append(self)
            self.available = True
            self.task = None
            self.sent_samples = 0
            self.activity_ends = 0

        def start(self, *, native):
            assert native is True
            self.task = asyncio.create_task(asyncio.Event().wait())

        def feed(self, message):
            if message.get("activity_end"):
                self.activity_ends += 1
                # Observed a2 misrecognition, unchanged required markers. The
                # offset resets too; arrival order must not create a PASS.
                self.diagnostic_observer({"type": "caption_timing_probe",
                    "provider_connection_generation": 1, "sent_pcm_samples": self.sent_samples,
                    "start_offset": "0s", "end_offset": "0.010s",
                    "transcript_kind": "interim_input_transcript", "assigned": False},
                    "тема разговора Слышишь меня?")
                self.diagnostic_observer({"type": "caption_timing_probe",
                    "provider_connection_generation": 1, "sent_pcm_samples": self.sent_samples,
                    "transcript_kind": "input_transcript", "assigned": False}, "Слышишь меня?")
            return True

        def feed_pcm(self, pcm, *, caption_scope, capture_start_us):
            before = self.sent_samples
            self.sent_samples += len(pcm)//2
            self.diagnostic_observer({"type": "caption_audio_sent", "provider_connection_generation": 1,
                "stream_start_sample": before, "stream_end_sample": self.sent_samples,
                "capture_start_us": capture_start_us, "capture_end_us": capture_start_us+len(pcm)*1_000_000//32000,
                "caption_scope": caption_scope})
            return True

        async def stop(self):
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.available = False

    monkeypatch.setattr(live_transcription, "CaptionSidecar", Sidecar)
    monkeypatch.setattr(live_resources, "live_resource_environment", lambda _env: {})
    monkeypatch.setattr(probe.importlib.metadata, "version", lambda _name: "test-fixture")
    monkeypatch.setattr(probe, "implementation_fingerprints", lambda: {})

    async def finalize_fixture_store(function, *args, **kwargs):
        # This test covers the collection schedule and strict clock verdict.
        # Threaded store finalization is outside its scope; keep the tiny test
        # store local to avoid dependence on executor shutdown in the sandbox.
        return function(*args, **kwargs)

    monkeypatch.setattr(probe.asyncio, "to_thread", finalize_fixture_store)
    args = SimpleNamespace(use_installed_provider_environment=False, observe_synthetic_transcripts=True,
                           data_dir=tmp_path/"data", report=tmp_path/"report.json",
                           timeout=3, observation_seconds=.001)
    speech = [{"name": name, "pcm": b"\x00\x04"*160, "duration_us": 10_000}
              for name in ("hello", "overlap")]
    report = {"passed": False, "connections": [], "native_capture": False,
              "native_pipeline_acceptance": False}
    asyncio.run(probe.run(args, speech, report))

    assert len(instances) == 2
    assert [item.activity_ends for item in instances] == [2, 2]
    assert all(item.task.done() for item in instances)
    assert not report.get("errors")  # Missing markers no longer cause a TimeoutError.
    assert report["passed"] is False and report["native_pipeline_acceptance"] is False
    assert len(report["connections"]) == 2
    for connection in report["connections"]:
        assert connection["all_inputs_collected"] is True
        assert connection["provider_task_joined"] is True
        assert len(connection["utterance_observations"]) == 2
        assert all(item["pcm_samples_sent"] == 160 and item["collection_complete"]
                   and not item["markers_mapped_at_window_end"] for item in connection["utterance_observations"])
        assert connection["passed"] is False and connection["mapped_probes"] == []
        assert len(connection["unmapped_expected_markers"]) == 4
        assert connection["final_transcripts_without_offsets"] == 2
        assert connection["offset_clock"] == "unverified"
        assert all(row["assigned"] is False for row in connection["synthetic_transcript_observations"])
