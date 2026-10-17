"""Large native transcripts retain exact durable text behind a bounded scoped ref."""
from __future__ import annotations

import json

import httpx
import pytest

from projects_hub.app import create_app
from projects_hub.auth import COOKIE_NAME, issue_session
from projects_hub.settings import Settings
from projects_hub.store import DurableStore, StoreError
from test_encoded_voice_store import voice, opened, chunk
from test_native_timeline import adapter_context, speech


@pytest.mark.parametrize("alphabet", ["russian", "non_bmp_emoji"])
@pytest.mark.parametrize("kind,role", [("input_transcript", "user"), ("output_transcript", "assistant")])
@pytest.mark.asyncio
async def test_large_native_projection_keeps_exact_text_and_targeted_get_is_scope_bound(voice, tmp_path, alphabet, kind, role):
    text = (("Привет, Мира! " * 10_000)[:119_999] + "Я") if alphabet == "russian" else "\U0001f600" * 120_000
    assert len(text) == 120_000
    assert len(json.dumps({"text": text}).encode("utf-8")) > 16_384
    store, encoded, actor, conversation, _ = voice
    adapter, session = adapter_context(store, encoded, actor, conversation)
    source = await opened(encoded)
    await speech(adapter, session, encoded, source, chunk(1))
    if kind == "output_transcript":
        adapter.on_event(session, {"type": "input_transcript", "text": "Исходный запрос"})
    event = {"type": kind, "text": text, "provider_at": 123}
    adapter.on_event(session, event)

    assert "timeline_message" not in event and "text" not in event
    reference = event["timeline_ref"]
    assert reference["conversation_id"] == conversation["id"]
    assert reference["source_id"] == source.server_source_id
    assert reference["role"] == role
    assert set(reference) == {"id", "conversation_id", "turn_id", "source_id", "role", "revision", "transcript_revision"}
    assert len(json.dumps(event).encode("utf-8")) <= 16_384
    assert len(json.dumps({"type": "event", "seq": 1, "event": event}).encode("utf-8")) <= 16_384
    assert len(json.dumps(event, ensure_ascii=False).encode("utf-8")) <= 16_384

    workspace = conversation["workspace_id"]
    peer_boot = store.ensure_external_workspace(provider="fixture", subject="native-ref-peer", display_name="Peer")
    peer_actor, peer_workspace = peer_boot["actor"]["id"], peer_boot["workspace"]["id"]
    with store._lock:
        # Both actors are valid members: membership alone must not reveal a
        # private conversation, nor let a request substitute another workspace.
        store.db.execute("INSERT INTO memberships(actor_id,workspace_id,role) VALUES(?,?,?)", (peer_actor, workspace, "member"))
        store.db.execute("INSERT INTO memberships(actor_id,workspace_id,role) VALUES(?,?,?)", (actor, peer_workspace, "member"))
    other_conversation = store.create_conversation(actor, workspace)
    other_message = store.upsert_conversation_timeline_message(
        actor_id=actor, conversation_id=other_conversation["id"], workspace_id=workspace,
        message_id="msg_other_native_projection", turn_id="turn_other_native_projection", role="user",
        text="Other private conversation", source_id=None, transcript_revision=0, revision=1, blocks=[],
    )

    # A new database connection is the authority for both direct readback and
    # the real authenticated route; the transient event is insufficient to
    # reconstruct this text and is never posted back by a presentation client.
    reopened = DurableStore(tmp_path)
    try:
        canonical = reopened.get_conversation_timeline_message(
            actor_id=actor, conversation_id=conversation["id"], workspace_id=workspace, message_id=reference["id"],
        )
        assert canonical["text"] == text
        assert all(canonical[key] == value for key, value in reference.items())
        original = reopened.db.execute("SELECT text FROM source_events WHERE source_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                                       (source.server_source_id, kind)).fetchone()
        assert original["text"] == text
        if kind == "input_transcript":
            assert reopened.get_source(actor, source.server_source_id)["transcript"] == text

        settings = Settings(data_dir=tmp_path, static_dir=tmp_path / "no-ui", session_secret="native-ref-test-" * 4,
                            dev_auth=False, cookie_secure=False)
        app = create_app(settings, store=reopened)
        # ASGI transport exercises the HTTP/auth boundary without starting the
        # unrelated background/MCP workers or opening any external connection.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            def path(conv=conversation["id"], message=reference["id"]):
                return f"/api/conversations/{conv}/timeline/messages/{message}"

            unauthenticated = await client.get(path(), params={"workspace_id": workspace})
            assert unauthenticated.status_code == 401
            client.cookies.set(COOKIE_NAME, issue_session(actor, settings.session_secret))
            accepted = await client.get(path(), params={"workspace_id": workspace})
            assert accepted.status_code == 200
            assert accepted.json() == canonical
            assert accepted.json()["text"] == text

            denied_cases = (
                (peer_actor, workspace, conversation["id"], reference["id"]),
                (actor, peer_workspace, conversation["id"], reference["id"]),
                (actor, workspace, other_conversation["id"], reference["id"]),
                (actor, workspace, conversation["id"], other_message["id"]),
                (actor, workspace, conversation["id"], "msg_unknown_native_projection"),
            )
            for request_actor, request_workspace, request_conversation, request_message in denied_cases:
                with pytest.raises(StoreError) as unavailable:
                    reopened.get_conversation_timeline_message(actor_id=request_actor, workspace_id=request_workspace,
                                                               conversation_id=request_conversation, message_id=request_message)
                assert unavailable.value.code == "TIMELINE_MESSAGE_NOT_FOUND"
                client.cookies.clear()
                client.cookies.set(COOKIE_NAME, issue_session(request_actor, settings.session_secret))
                denied = await client.get(path(request_conversation, request_message), params={"workspace_id": request_workspace})
                assert denied.status_code == 404
                assert denied.json()["error"]["code"] == "TIMELINE_MESSAGE_NOT_FOUND"
                assert "text" not in denied.json() and "timeline_message" not in denied.json()
                assert len(denied.content) < 1024
    finally:
        reopened.close()
