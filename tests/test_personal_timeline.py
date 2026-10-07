from __future__ import annotations

from fastapi.testclient import TestClient

from projects_hub.app import create_app
from projects_hub.auth import COOKIE_NAME, issue_session
from projects_hub.settings import Settings
from projects_hub.store import DurableStore


def test_personal_conversation_timeline_is_reused_revisioned_and_private(tmp_path):
    store = DurableStore(tmp_path / "data")
    owner_boot = store.ensure_platform_owner("Timeline owner")
    owner = owner_boot["actor"]["id"]
    workspace = owner_boot["workspace"]["id"]
    project = owner_boot["projects"][0]["id"]
    other = "usr_timeline_other"
    with store._lock:
        store.db.execute(
            "INSERT INTO actors(id,display_name,created_at_ms) VALUES(?,?,?)",
            (other, "Other member", 1),
        )
        store.db.execute(
            "INSERT INTO memberships(actor_id,workspace_id,role) VALUES(?,?,?)",
            (other, workspace, "member"),
        )

    settings = Settings(
        data_dir=tmp_path / "data",
        static_dir=tmp_path / "missing-ui",
        session_secret="timeline-test-secret-" * 4,
        dev_auth=True,
        cookie_secure=False,
    )
    app = create_app(settings, store=store)
    try:
        with TestClient(app, base_url="http://testserver") as client:
            client.cookies.set(COOKIE_NAME, issue_session(owner, settings.session_secret))
            first = client.post(
                "/api/conversations/personal",
                json={"workspace_id": workspace, "focus_project_id": project},
            )
            assert first.status_code == 200
            conversation_id = first.json()["id"]

            # The dedicated UI personal conversation is reused across focus changes / devices,
            # while generic Live/offline resource conversations remain independent.
            second = client.post(
                "/api/conversations/personal",
                json={"workspace_id": workspace, "focus_project_id": None},
            )
            assert second.status_code == 200
            assert second.json()["id"] == conversation_id

            message_id = "msg_timeline_user_00000001"
            turn_id = "turn_timeline_00000001"
            provisional = client.put(
                f"/api/conversations/{conversation_id}/timeline/messages/{message_id}",
                json={
                    "workspace_id": workspace,
                    "turn_id": turn_id,
                    "role": "user",
                    "text": "предварительный текст",
                    "source_id": None,
                    "transcript_revision": 0,
                    "revision": 1,
                    "blocks": [],
                },
            )
            assert provisional.status_code == 200

            canonical = client.put(
                f"/api/conversations/{conversation_id}/timeline/messages/{message_id}",
                json={
                    "workspace_id": workspace,
                    "turn_id": turn_id,
                    "role": "user",
                    "text": "финальная расшифровка Live",
                    "source_id": None,
                    "transcript_revision": 1,
                    "revision": 2,
                    "blocks": [],
                },
            )
            assert canonical.status_code == 200
            assert canonical.json()["text"] == "финальная расшифровка Live"
            assert canonical.json()["transcript_revision"] == 1

            widget_id = "msg_timeline_widgets_000001"
            widget = client.put(
                f"/api/conversations/{conversation_id}/timeline/messages/{widget_id}",
                json={
                    "workspace_id": workspace,
                    "turn_id": "turn_timeline_widgets_0001",
                    "role": "assistant",
                    "text": "",
                    "transcript_revision": 0,
                    "revision": 1,
                    "blocks": [
                        {"kind": "collaboration_timeline"},
                        {"kind": "collaboration_questions"},
                        {"kind": "board", "project_id": project, "mode": "reference"},
                    ],
                },
            )
            assert widget.status_code == 200

            history = client.get(
                f"/api/conversations/{conversation_id}/timeline",
                params={"workspace_id": workspace},
            )
            assert history.status_code == 200
            items = history.json()["items"]
            assert [item["id"] for item in items] == [message_id, widget_id]
            assert items[0]["text"] == "финальная расшифровка Live"
            assert items[1]["blocks"][0]["kind"] == "collaboration_timeline"

            # Same revision with different data is not allowed to overwrite history.
            conflict = client.put(
                f"/api/conversations/{conversation_id}/timeline/messages/{message_id}",
                json={
                    "workspace_id": workspace,
                    "turn_id": turn_id,
                    "role": "user",
                    "text": "откат",
                    "transcript_revision": 1,
                    "revision": 2,
                    "blocks": [],
                },
            )
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "TIMELINE_MESSAGE_CONFLICT"

            # Workspace membership/project access does not grant another actor
            # access to the owner's private personal conversation.
            client.cookies.set(COOKIE_NAME, issue_session(other, settings.session_secret))
            denied = client.get(
                f"/api/conversations/{conversation_id}/timeline",
                params={"workspace_id": workspace},
            )
            assert denied.status_code == 404
            assert denied.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"
    finally:
        store.close()
