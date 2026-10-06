import base64
import json
from pathlib import Path
from types import SimpleNamespace

from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_resources import ConversationScope
from projects_hub.store import DurableStore


def _live(tmp_path: Path, label: str = "Utterance"):
    store = DurableStore(tmp_path)
    boot = store.ensure_dev_workspace(label)
    actor_id = boot["actor"]["id"]
    workspace_id = boot["workspace"]["id"]
    conversation = store.create_conversation(
        actor_id,
        workspace_id,
        boot["projects"][0]["id"],
    )
    binding = ConversationScope(
        workspace_id,
        actor_id,
        conversation["id"],
    ).resource_binding()
    adapter = ProjectsHubLiveAdapter(store)
    initialized = adapter.initialize(
        resource_id=binding,
        actor={"subject": actor_id, "tenant_id": workspace_id},
        model="gemini-3.8-live",
        conversation_id=conversation["id"],
    )
    session = SimpleNamespace(id="live_utterance_test", state=initialized["state"])
    return store, adapter, session, initialized, actor_id, workspace_id, conversation, binding


def _verdicts(store: DurableStore, actor_id: str, source_id: str):
    result = []
    for event in store.source_events(actor_id, source_id):
        if event["kind"] != "utterance_verdict":
            continue
        result.append(json.loads(event["text"]))
    return result


def _audio_message(pcm: bytes):
    return {"audio_base64": base64.b64encode(pcm).decode("ascii")}


def test_mid_utterance_disconnect_is_durable_and_server_marks_no_turn_closed(tmp_path: Path):
    store, adapter, session, initialized, actor_id, _workspace, conversation, binding = _live(tmp_path)
    try:
        assert initialized["configuration"]["manual_activity_detection"] is True
        assert initialized["configuration"]["automatic_activity_detection"] is None
        pcm = b"\x01\x00" * 800

        adapter.input(session, {"activity_start": True})
        adapter.input(session, _audio_message(pcm))
        adapter.on_stopped(session)

        source_id = initialized["response"]["source_id"]
        verdict = _verdicts(store, actor_id, source_id)[-1]
        assert verdict["verdict"] == "no_turn_closed"
        assert verdict["activity_end"] is False
        assert verdict["semantic_observed"] is False
        assert verdict["audio_start_bytes"] == 0
        assert verdict["audio_end_bytes"] == len(pcm)

        next_initialized = adapter.initialize(
            resource_id=binding,
            actor={"subject": actor_id, "tenant_id": conversation["workspace_id"]},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
        )
        pending = next_initialized["response"]["pending_voice_sources"]
        recovered = next(item for item in pending if item["id"] == source_id)
        assert recovered["utterance_verdict"] == "no_turn_closed"
        assert recovered["utterance_audio_start_bytes"] == 0
        assert recovered["utterance_audio_end_bytes"] == len(pcm)
    finally:
        store.close()


def test_activity_end_without_canonical_transcript_is_unknown_not_safe_replay(tmp_path: Path):
    store, adapter, session, initialized, actor_id, *_ = _live(tmp_path, "Ended")
    try:
        pcm = b"\x02\x00" * 640
        adapter.input(session, {"activity_start": True})
        adapter.input(session, _audio_message(pcm))
        adapter.input(session, {"activity_end": True})
        adapter.on_stopped(session)

        verdict = _verdicts(store, actor_id, initialized["response"]["source_id"])[-1]
        assert verdict["verdict"] == "turn_closed_no_transcript"
        assert verdict["activity_end"] is True
        assert verdict["committed"] is False
    finally:
        store.close()


def test_canonical_transcript_commits_utterance_and_stop_cannot_downgrade_it(tmp_path: Path):
    store, adapter, session, initialized, actor_id, *_ = _live(tmp_path, "Committed")
    try:
        pcm = b"\x03\x00" * 640
        adapter.input(session, {"activity_start": True})
        adapter.input(session, _audio_message(pcm))
        adapter.input(session, {"activity_end": True})
        adapter.on_event(
            session,
            {"type": "input_transcript", "text": "каноническая фраза", "provider_at": 10},
        )

        source_id = initialized["response"]["source_id"]
        before_stop = _verdicts(store, actor_id, source_id)
        assert before_stop[-1]["verdict"] == "turn_committed"

        adapter.on_stopped(session)
        after_stop = _verdicts(store, actor_id, source_id)
        assert after_stop[-1]["verdict"] == "turn_committed"
        assert len(after_stop) == len(before_stop)
    finally:
        store.close()


def test_any_provider_semantic_output_prevents_no_turn_closed_classification(tmp_path: Path):
    store, adapter, session, initialized, actor_id, *_ = _live(tmp_path, "Provider output")
    try:
        pcm = b"\x04\x00" * 640
        adapter.input(session, {"activity_start": True})
        adapter.input(session, _audio_message(pcm))
        adapter.on_event(
            session,
            {"type": "output_transcript", "text": "ответ уже начался", "provider_at": 12},
        )
        adapter.on_stopped(session)

        verdict = _verdicts(store, actor_id, initialized["response"]["source_id"])[-1]
        assert verdict["verdict"] == "turn_committed"
        assert verdict["semantic_observed"] is True
        assert verdict["committed"] is True
    finally:
        store.close()



def test_prior_unknown_cannot_steal_next_canonical_transcript(tmp_path: Path):
    store, adapter, session, initialized, actor_id, *_ = _live(tmp_path, "Sequential")
    try:
        first_pcm = b"\x05\x00" * 640
        second_pcm = b"\x06\x00" * 640

        adapter.input(session, {"activity_start": True})
        adapter.input(session, _audio_message(first_pcm))
        adapter.input(session, {"activity_end": True})

        # Opening the next utterance retires the first as an unresolved
        # ended turn, but it must not remain the target for the next canonical
        # transcript after the second activity_end.
        adapter.input(session, {"activity_start": True})
        adapter.input(session, _audio_message(second_pcm))
        adapter.input(session, {"activity_end": True})
        adapter.on_event(
            session,
            {"type": "input_transcript", "text": "вторая фраза", "provider_at": 20},
        )
        adapter.on_stopped(session)

        verdicts = _verdicts(store, actor_id, initialized["response"]["source_id"])
        by_sequence = {}
        for verdict in verdicts:
            by_sequence.setdefault(verdict["sequence"], []).append(verdict["verdict"])
        assert by_sequence[1][-1] == "turn_closed_no_transcript"
        assert by_sequence[2][-1] == "turn_committed"
    finally:
        store.close()


def test_late_provider_event_before_next_activity_end_stays_with_prior_turn(tmp_path: Path):
    store, adapter, session, initialized, actor_id, *_ = _live(tmp_path, "Late provider event")
    try:
        first_pcm = b"\x07\x00" * 640
        second_pcm = b"\x08\x00" * 640

        adapter.input(session, {"activity_start": True})
        adapter.input(session, _audio_message(first_pcm))
        adapter.input(session, {"activity_end": True})
        adapter.input(session, {"activity_start": True})
        adapter.input(session, _audio_message(second_pcm))

        # The second turn is still open, so this late model event belongs to
        # the prior ended turn rather than falsely committing the new speech.
        adapter.on_event(
            session,
            {"type": "output_transcript", "text": "поздний ответ", "provider_at": 21},
        )
        adapter.on_stopped(session)

        verdicts = _verdicts(store, actor_id, initialized["response"]["source_id"])
        by_sequence = {}
        for verdict in verdicts:
            by_sequence.setdefault(verdict["sequence"], []).append(verdict["verdict"])
        assert by_sequence[1][-1] == "turn_committed"
        assert by_sequence[2][-1] == "no_turn_closed"
    finally:
        store.close()
