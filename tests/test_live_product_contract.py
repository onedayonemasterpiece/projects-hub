"""Specification regression checks only, not audio/device/provider acceptance."""
import json
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs" / "product"


def load(name):
    return json.loads((DOCS / name).read_text(encoding="utf-8"))


def contract():
    return load("live-contract.json")


def test_full_application_parity_and_voice_first_ui():
    c = contract()
    app = c["applications"]
    interaction = c["interaction"]
    assert app["pwa"] == app["android"] == "full_application"
    assert app["shared_core_ux"] and app["android_capabilities_superset"]
    assert not interaction["free_text_composer"]
    assert not interaction["ordinary_utterance_submit_form"]
    assert interaction["contextual_buttons"]
    assert interaction["fixed_question_limit"] is None
    assert not interaction["manual_project_selection_required"]


def test_one_central_live_agent_owns_all_semantics():
    c = contract()
    agent = c["central_agent"]
    assert c["spec_revision"] == 4
    assert agent["sole_semantic_orchestrator"]
    assert agent["online_input"] == "raw_audio_direct_to_live"
    assert agent["offline_input"] == "raw_audio_deliberate_buffered_replay_to_live"
    assert agent["input_transcription_owner"] == "live_provider_same_session"
    assert agent["project_routing_owner"] == "live_agent"
    assert agent["archive_disposition_owner"] == "live_agent_function_call"
    assert agent["vocabulary_semantic_owner"] == "live_agent"
    assert not agent["hidden_semantic_llm_inside_tools_allowed"]


def test_deterministic_layer_cannot_become_second_brain():
    layer = contract()["deterministic_layer"]
    allowed = set(layer["allowed_roles"])
    forbidden = set(layer["forbidden_semantic_roles"])
    assert {"audio_capture", "vad", "auth_acl", "idempotency", "readback_reconciliation"} <= allowed
    assert {"separate_asr_before_live", "project_intent_router", "pre_live_summarizer", "value_classifier"} <= forbidden


def test_offline_is_raw_audio_buffered_live_turn():
    offline = contract()["offline"]
    assert offline["agent_delivery"] == "raw_audio_to_central_live"
    assert not offline["separate_asr_before_live"]
    assert not offline["summary_only_delivery_allowed"]
    assert offline["manual_activity_boundaries_required"]
    assert offline["activity_sequence"] == ["activityStart", "buffered_pcm", "activityEnd"]
    assert not offline["allow_response_to_incomplete_packet"]


def test_archive_semantics_belong_to_agent_not_seconds():
    archive = contract()["archive"]
    assert archive["semantic_disposition_owner"] == "live_agent_function_call"
    assert archive["fixed_duration_archive_rule_seconds"] is None
    assert not archive["pending_agent_disposition_auto_delete"]
    assert not archive["source_is_summary"]


def test_vocabulary_semantics_belong_to_same_agent():
    vocab = contract()["vocabulary"]
    assert vocab["semantic_owner"] == "live_agent"
    assert vocab["agent_can_build_from_authorized_github_documents"]
    assert not vocab["hidden_background_llm"]
    assert not vocab["provider_input_transcription_alone_is_grounding_evidence"]
    assert vocab["source_provenance_required"]


def test_framework_gaps_are_explicit_not_silently_assumed_solved():
    req = contract()["framework_requirements"]
    assert req["direct_audio_to_live"]
    assert req["live_input_transcription_enabled_in_current_framework"]
    assert req["lossless_trusted_transcript_sink_required"]
    assert req["current_provider_transcript_projection_limit_chars"] == 2000
    assert req["current_session_event_ring_size"] == 320
    assert req["manual_activity_start_end_required"]
    assert req["conversation_scope_not_single_project_scope_required"]
    assert req["product_durable_capture_above_transport_required"]
    assert req["same_origin_wss_required"]
    assert req["one_use_socket_ticket_required"]
    assert req["binary_pcm_wss_required"]
    assert req["no_silent_http_audio_fallback"]
    assert req["damaged_turn_mutation_guard_required"]
    assert req["multi_user_actor_workspace_conversation_isolation_required"]


def test_product_registry_tracks_revision_four_and_35_gates():
    registry = load("contract.json")
    assert registry["spec_revision"] == 4
    assert registry["owner_revision"]["id"] == "U04"
    assert registry["central_agent_contract"] == "12-central-live-agent.md"
    gates = registry["release_gates"]
    assert len(gates) == 35
    assert [g["id"] for g in gates] == [f"G{i:02d}" for i in range(1, 36)]
    assert all(g["status"] == "not_run" for g in gates)
    assert {"G31", "G32", "G33", "G34", "G35"} <= set(contract()["additional_acceptance_gates"])


def test_docs_do_not_reintroduce_superseded_cognitive_pipeline():
    text = "\n".join(p.read_text(encoding="utf-8") for p in DOCS.glob("*.md"))
    forbidden = [
        "целостное ASR сохранённого аудио",
        "полный текст Live-агенту",
        "full_transcript_as_buffered_turn",
        "все завершённые офлайн-пакеты и эпизоды от 20 секунд речи архивируются",
    ]
    for phrase in forbidden:
        assert phrase not in text
    central = (DOCS / "12-central-live-agent.md").read_text(encoding="utf-8")
    assert "единственное когнитивное звено" in central.lower()
    assert "function calls" in central
    assert "activityStart" in central and "activityEnd" in central


def test_revision_four_docs_are_cross_linked():
    index = (DOCS / "README.md").read_text(encoding="utf-8")
    assert "12-central-live-agent.md" in index
    assert "10-conversation-memory.md" in index
    assert "11-routing-and-vocabulary.md" in index
    assert "16-wss-multi-user-reliability.md" in index
    reliability = (DOCS / "08-reliability.md").read_text(encoding="utf-8")
    for n in range(1, 36):
        assert f"G{n:02d}" in reliability