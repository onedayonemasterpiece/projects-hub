"""Offline clock evidence checks; no provider, no Android acceptance claims."""
import importlib.util
from pathlib import Path
import sys
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
    assert probe.connection_result(observed(probe, second_offset="0s"))["passed"] is False


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
