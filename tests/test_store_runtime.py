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
        assert transcript not in text
        assert "Зафиксировано из Live source." in text
        source_path = next((tmp_path / "sources" / workspace).glob("*.md"))
        source_text = source_path.read_text(encoding="utf-8")
        assert transcript in source_text
        assert "private_source_ref" in text
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


def test_unfinished_voice_source_pages_preserve_provisional_and_final_text(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Voice recovery")
        actor = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        conversation = store.create_conversation(actor, workspace, boot["projects"][0]["id"])
        source = store.create_source(actor, conversation["id"])
        store.append_audio(actor, source["id"], b"\\x01\\x00" * 400)
        provisional = "начало provisional " + ("п" * 5200) + " конец provisional"
        assert store.append_source_event(
            actor, source["id"], "interim_input_transcript", provisional, provider_at_ms=100
        ) == 0
        page = store.voice_source_transcript_page(actor, conversation["id"], source["id"], offset=0, max_chars=4000)
        tail = store.voice_source_transcript_page(actor, conversation["id"], source["id"], offset=4000, max_chars=4000)
        assert page["origin"] == "provisional"
        assert page["needs_audio_replay"] is True
        assert page["text"] + tail["text"] == provisional

        final = "начало final " + ("ф" * 5200) + " середина final " + ("я" * 5200) + " конец final"
        assert store.append_source_event(
            actor, source["id"], "input_transcript", final, provider_at_ms=200
        ) == 1
        pieces = []
        offset = 0
        while True:
            item = store.voice_source_transcript_page(actor, conversation["id"], source["id"], offset=offset, max_chars=4000)
            pieces.append(item["text"])
            if item["next_offset"] is None:
                break
            offset = item["next_offset"]
        assert "".join(pieces) == final
        assert item["origin"] == "final"
        assert item["needs_audio_replay"] is False
    finally:
        store.close()


def test_voice_source_audio_path_is_actor_and_conversation_private(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Private audio")
        actor = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        conversation = store.create_conversation(actor, workspace, boot["projects"][0]["id"])
        source = store.create_source(actor, conversation["id"])
        store.append_audio(actor, source["id"], b"\\x01\\x00" * 64)
        assert store.voice_source_audio_path(actor, conversation["id"], source["id"]).read_bytes() == b"\\x01\\x00" * 64

        other_conversation = store.create_conversation(actor, workspace, boot["projects"][0]["id"])
        import pytest
        with pytest.raises(Exception, match="current conversation"):
            store.voice_source_audio_path(actor, other_conversation["id"], source["id"])

        other = store.ensure_dev_workspace("Other actor")
        with pytest.raises(Exception, match="not available"):
            store.voice_source_audio_path(other["actor"]["id"], conversation["id"], source["id"])
    finally:
        store.close()



def test_late_corrected_final_after_turn_boundary_remains_lossless(tmp_path: Path):
    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Late final")
        actor = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        conversation = store.create_conversation(actor, workspace, boot["projects"][0]["id"])
        source = store.create_source(actor, conversation["id"])

        provisional = (
            "НАЧАЛО " + ("а" * 2300)
            + " СЕРЕДИНА значение сорок два "
            + ("б" * 2300)
            + " КОНЕЦ"
        )
        corrected = provisional.replace("сорок два", "семьдесят три")
        assert len(corrected) > 4000

        assert store.append_source_event(
            actor, source["id"], "interim_input_transcript", provisional, provider_at_ms=100
        ) == 0
        assert store.append_source_event(
            actor, source["id"], "turn_complete", provider_at_ms=110
        ) == 0
        assert store.append_source_event(
            actor, source["id"], "input_transcript", corrected, provider_at_ms=120
        ) == 1

        saved = store.get_source(actor, source["id"])
        assert saved["transcript"] == corrected
        assert "сорок два" not in saved["transcript"]
        assert "семьдесят три" in saved["transcript"]
        assert saved["transcript"].startswith("НАЧАЛО")
        assert "СЕРЕДИНА" in saved["transcript"]
        assert saved["transcript"].endswith("КОНЕЦ")

        events = store.source_events(actor, source["id"])
        assert [item["kind"] for item in events[-3:]] == [
            "interim_input_transcript",
            "turn_complete",
            "input_transcript",
        ]
    finally:
        store.close()
