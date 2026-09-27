from pathlib import Path

from projects_hub.auth import issue_session, parse_session
from projects_hub.live_resources import ConversationScope
from projects_hub.store import DurableStore


def test_signed_session_round_trip_and_expiry():
    token = issue_session("usr_test", "secret", now=100)
    assert parse_session(token, "secret", now=101) == "usr_test"
    assert parse_session(token, "wrong", now=101) is None
    assert parse_session(token, "secret", now=100 + 8 * 24 * 60 * 60) is None


def test_durable_source_memory_and_readback(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Test")
        actor = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        project = boot["projects"][0]["id"]
        conversation = store.create_conversation(actor, workspace, project)
        source = store.create_source(actor, conversation["id"])

        stats = store.append_audio(actor, source["id"], b"\x00\x01\x02\x03")
        assert stats == {"audio_bytes": 4, "audio_chunks": 1}
        assert (tmp_path / source["audio_path"]).read_bytes() == b"\x00\x01\x02\x03"

        transcript = "Точная длинная расшифровка " + ("х" * 5000)
        revision = store.append_source_event(
            actor, source["id"], "input_transcript", transcript, provider_at_ms=42
        )
        assert revision == 1
        assert store.get_source(actor, source["id"])["transcript"] == transcript
        assert store.source_events(actor, source["id"])[0]["text"] == transcript

        result = store.commit_memory(
            actor_id=actor,
            source_id=source["id"],
            command_id="cmd_one",
            project_id=project,
            title="Решение",
            kind="decision",
            semantic_notes="Зафиксировано из Live source.",
            args_sha256="a" * 64,
        )
        assert result["revision"] == 1
        assert result["transcript_revision"] == 1
        assert store.get_source(actor, source["id"])["status"] == "archived"

        same = store.commit_memory(
            actor_id=actor,
            source_id=source["id"],
            command_id="cmd_one",
            project_id=project,
            title="Другое название не должно переисполнить command",
            kind="note",
            semantic_notes="ignored",
            args_sha256="b" * 64,
        )
        assert same == result

        items = store.list_memories(actor, workspace, project)
        assert len(items) == 1
        memory_path = next((tmp_path / "memory" / workspace).glob("*.md"))
        text = memory_path.read_text(encoding="utf-8")
        assert transcript in text
        assert "Зафиксировано из Live source." in text
    finally:
        store.close()


def test_conversation_scope_is_not_project_scope(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Scope")
        actor = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        conversation = store.create_conversation(actor, workspace)
        a = ConversationScope(workspace, actor, conversation["id"]).resource_binding()
        other = store.create_conversation(actor, workspace)
        b = ConversationScope(workspace, actor, other["id"]).resource_binding()
        assert len(a) == 64 and a != b
    finally:
        store.close()
