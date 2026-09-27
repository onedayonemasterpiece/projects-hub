"""Specification regression checks only, not audio/device/provider acceptance."""
import json
from pathlib import Path
import re

DOCS = Path(__file__).resolve().parents[1] / "docs" / "product"


def contract():
    return json.loads((DOCS / "live-contract.json").read_text(encoding="utf-8"))


def test_full_application_parity_without_identical_os_promises():
    app = contract()["applications"]
    assert app["pwa"] == app["android"] == "full_application"
    assert app["shared_core_ux"] and app["android_capabilities_superset"]
    assert not app["identical_os_guarantees_claimed"]
    assert {"live_dialogue", "history", "offline_capture", "resume_conversation"} <= set(app["core_scenarios"])


def test_live_not_submission_form_and_no_fixed_question_limit():
    interaction = contract()["interaction"]
    assert interaction["mode"] == "continuous_live_dialogue"
    assert not interaction["free_text_composer"]
    assert not interaction["ordinary_utterance_submit_form"]
    assert interaction["contextual_buttons"]
    assert interaction["fixed_question_limit"] is None


def test_project_focus_is_not_a_required_manual_step():
    interaction = contract()["interaction"]
    assert interaction["voice_project_switch"]
    assert interaction["contextual_project_routing"]
    assert interaction["multi_project_conversation"]
    assert not interaction["manual_project_selection_required"]
    assert contract()["archive"]["mixed_project_source_default_audience"] == "personal"


def test_capture_is_durable_independent_of_model_response():
    capture = contract()["capture"]
    assert capture["source_audio_pipeline_count"] == 1
    assert capture["durable_before_provider_send"]
    assert capture["vad"] and capture["pre_roll_and_hangover"]
    assert not capture["silence_ends_logical_conversation"]
    assert not capture["provider_response_is_archive_receipt"]
    assert capture["checkpoint_target_status"] == "proposal_requires_device_measurement"


def test_buffered_turn_delivers_complete_source_not_summary():
    offline = contract()["offline"]
    assert offline["long_capture_supported"] and offline["resumable_upload"]
    assert offline["sealed_manifest_required"]
    assert offline["default_agent_delivery"] == "full_transcript_as_buffered_turn"
    for key in ("summary_only_delivery_allowed", "network_return_ends_utterance",
                "allow_response_to_incomplete_packet", "allow_raw_backlog_new_live_interleave"):
        assert not offline[key], key
    assert offline["preserve_original_conversation_and_capture_time"]


def test_new_conversation_never_implies_deleting_pending_source():
    spec = contract()
    assert not spec["interaction"]["new_conversation_deletes_previous"]
    assert spec["offline"]["new_conversation_policy"] == "archive_previous_and_await_resume"
    assert not spec["offline"]["old_answer_plays_in_new_conversation"]
    assert spec["offline"]["stale_action_requires_revalidation"]
    assert not spec["archive"]["pending_auto_ttl_delete"]


def test_short_meaningful_speech_is_not_filtered_by_duration():
    archive = contract()["archive"]
    assert archive["always_for_explicit_save"] and archive["always_for_meaningful_content"]
    assert archive["meaningful_minimum_duration_seconds"] == 0
    assert archive["always_for_sealed_offline_packet"]
    assert archive["conservative_archive_speech_seconds"] > 0
    assert not archive["threshold_splits_markdown"]
    assert archive["uncertain_policy"] == "retain"
    assert archive["source_format"] == "markdown_full_transcript"
    assert not archive["source_is_summary"]
    assert archive["source_index_required"]


def test_audio_cleanup_requires_archival_evidence():
    archive = contract()["archive"]
    assert {"manifest_complete", "source_transcript_verified", "archive_exact_commit_readback",
            "archive_current_main_readback", "conversation_links_durable", "backup_policy_satisfied"} <= set(archive["automatic_audio_cleanup_requires"])
    assert not archive["physical_loss_of_only_offline_copy_recoverable_claim"]


def test_vocabulary_has_scoped_provenance_and_no_circular_evidence():
    vocab = contract()["vocabulary"]
    assert set(vocab["scopes"]) == {"product", "project", "personal"}
    assert vocab["agent_can_build_from_authorized_github_documents"]
    assert vocab["automatic_grounded_nonconflicting_updates"] and vocab["voice_corrections"]
    assert vocab["source_provenance_required"]
    assert not vocab["own_asr_output_is_independent_evidence"]
    assert not vocab["old_transcripts_rewritten_on_refresh"]
    assert vocab["contextual_and_acoustic_compatibility_required"]
    for ref in vocab["source_refs"]:
        assert ref["repository"] and ref["path"]
        assert re.fullmatch(r"[0-9a-f]{40}", ref.get("commit", ref.get("blob_sha", "")))


def test_owner_revision_and_new_gates_are_connected():
    spec = contract()
    registry = json.loads((DOCS / "contract.json").read_text(encoding="utf-8"))
    assert registry["experience_contract"] == "live-contract.json"
    assert registry["owner_revision"]["spec_revision"] == spec["spec_revision"] == 2
    assert spec["owner_input"]["id"] == registry["owner_revision"]["id"] == "U02"
    assert spec["owner_input"]["voice_packet_id"] is None
    assert "U02" in (DOCS / "01-evidence.md").read_text(encoding="utf-8")
    gates = {g["id"] for g in registry["release_gates"]}
    assert set(spec["additional_acceptance_gates"]) <= gates
    assert len(spec["additional_acceptance_gates"]) == 10
    assert registry["platform_decision"]["both_full_applications"]
