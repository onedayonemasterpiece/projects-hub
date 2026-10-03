from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from live_interaction import LiveSocketSessionHost
from starlette.websockets import WebSocketDisconnect

from projects_hub.app import create_app
from projects_hub.auth import COOKIE_NAME, issue_session
from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_admission import ProjectsHubAdmissionMixin
from projects_hub.live_runtime import (
    _live_max_sessions,
    _live_max_sessions_per_actor,
)
from projects_hub.settings import Settings
from projects_hub.store import DurableStore


class Provider:
    def __init__(self) -> None:
        self.inputs: list[dict] = []

    async def run(self, *, load_key, reader, on_event) -> None:
        assert load_key() == "fixture-key-not-a-provider"
        self.inputs.append(json.loads(await reader.readline()))
        on_event({"type": "ready", "model": "gemini-3.8-live"})
        while raw := await reader.readline():
            value = json.loads(raw)
            self.inputs.append(value)
            if value["type"] == "stop":
                return
            if value["type"] == "activity_end":
                on_event({"type": "input_timing", "activity_end_sent_at": 1})
            if value["type"] == "audio_stream_end":
                on_event({"type": "input_timing", "audio_stream_end_sent_at": 1})
            if value["type"] == "text":
                on_event({"type": "output_transcript", "text": "fixture reply"})
                on_event(
                    {
                        "type": "audio",
                        "data": base64.b64encode(b"\x01\x00\x02\x00").decode(),
                        "mime_type": "audio/pcm;rate=24000",
                    }
                )
                on_event({"type": "turn_complete"})


class ProjectsHubTestHost(ProjectsHubAdmissionMixin, LiveSocketSessionHost):
    pass


def make_host(
    store: DurableStore,
    provider: Provider,
    *,
    max_sessions: int = 16,
    max_sessions_per_actor: int = 2,
):
    return ProjectsHubTestHost(
        adapter_factory=lambda **hooks: ProjectsHubLiveAdapter(store, **hooks),
        key_resolver=lambda *_: "fixture-key-not-a-provider",
        provider_run=provider.run,
        ready_timeout_ms=500,
        max_sessions=max_sessions,
        max_sessions_per_actor=max_sessions_per_actor,
        client_liveness_timeout_ms=75_000,
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        static_dir=tmp_path / "missing-ui",
        session_secret="s" * 32,
        dev_auth=True,
        cookie_secure=False,
    )


def _actor(store: DurableStore, subject: str, display_name: str):
    return store.ensure_external_workspace(
        provider="fixture",
        subject=subject,
        display_name=display_name,
    )


def _act_as(client: TestClient, settings: Settings, actor_id: str) -> None:
    client.cookies.clear()
    client.cookies.set(
        COOKIE_NAME,
        issue_session(actor_id, settings.session_secret),
    )


@pytest.fixture
def live_client(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    provider = Provider()
    host = make_host(store, provider)
    settings = _settings(tmp_path)
    app = create_app(settings, store=store, live_host=host)
    try:
        bootstrap = _actor(store, "single-user", "Single")
        conversation = store.create_conversation(
            bootstrap["actor"]["id"],
            bootstrap["workspace"]["id"],
            bootstrap["projects"][0]["id"],
        )
        with TestClient(app) as client:
            _act_as(client, settings, bootstrap["actor"]["id"])
            yield client, conversation["id"], provider, host
    finally:
        store.close()


def start(live_client):
    client, conversation_id, _, _ = live_client
    response = client.post(
        f"/api/live/{conversation_id}/sessions",
        json={"transport": "wss", "attempt_id": "attempt_contract"},
    )
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["transport"] == "wss"
    assert value["transport_protocol"] == "wl-live-v1"
    assert value["socket_url"].endswith("/socket")
    assert "?" not in value["socket_url"]
    return value


def socket(client, value, *, origin="http://testserver", path=None):
    return client.websocket_connect(
        path or value["socket_url"],
        subprotocols=["wl-live-v1", "wl-ticket." + value["socket_ticket"]],
        headers={"Origin": origin},
    )


def hello(ws, value):
    ws.send_json(
        {
            "type": "hello",
            "protocol": "wl-live-v1",
            "attempt_id": value["attempt_id"],
            "connection_generation": 1,
            "cursor": 0,
        }
    )
    assert ws.receive_json()["type"] == "hello_ack"


def test_projects_hub_wss_binary_push_and_no_http_fallback(live_client):
    client, conversation_id, provider, _ = live_client
    value = start(live_client)

    with socket(client, value) as ws:
        hello(ws, value)
        ws.send_bytes(struct.pack("!III", 0x574C4131, 1, 0) + b"\x01\x00\x02\x00")

        ack = None
        for _ in range(12):
            frame = ws.receive()
            if frame.get("text"):
                payload = json.loads(frame["text"])
                if payload.get("type") == "audio_ack":
                    ack = payload
                    break
        assert ack and ack["seq"] == 1

        rejected = client.post(
            f"/api/live/{conversation_id}/sessions/{value['session_id']}/input",
            json={"text": "never fallback"},
        )
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "LIVE_TRANSPORT_MISMATCH"

        ws.send_json({"type": "input", "message": {"audio_stream_end": True}})
        ws.send_json({"type": "input", "message": {"text": "reply"}})

        transcript = False
        binary = None
        for _ in range(16):
            frame = ws.receive()
            if frame.get("bytes"):
                binary = frame["bytes"]
                break
            event = json.loads(frame["text"]).get("event", {})
            transcript |= event.get("type") == "output_transcript"
        assert transcript and binary is not None
        assert struct.unpack("!III", binary[:12])[::2] == (0x574C4F31, 24000)
        assert binary[12:] == b"\x01\x00\x02\x00"
        ws.send_json({"type": "stop"})

    assert [item["type"] for item in provider.inputs][:4] == [
        "start",
        "audio",
        "audio_stream_end",
        "text",
    ]


def test_projects_hub_wss_origin_query_and_ticket_rotation(live_client):
    client, conversation_id, _, _ = live_client
    value = start(live_client)

    for origin, path in [
        ("https://evil.invalid", value["socket_url"]),
        ("http://testserver", value["socket_url"] + "?ticket=forbidden"),
    ]:
        with pytest.raises(WebSocketDisconnect):
            with socket(client, value, origin=origin, path=path):
                pass

    renewed_response = client.post(
        f"/api/live/{conversation_id}/sessions/{value['session_id']}/socket-ticket"
    )
    assert renewed_response.status_code == 200
    renewed = renewed_response.json()
    assert renewed["socket_ticket"] != value["socket_ticket"]

    with pytest.raises(WebSocketDisconnect):
        with socket(client, value):
            pass

    rotated = {**value, **renewed}
    with socket(client, rotated) as ws:
        hello(ws, value)
        ws.send_json({"type": "stop"})


def test_projects_hub_live_bootstrap_is_bounded_and_wss_only(live_client):
    client, conversation_id, _, _ = live_client
    base = f"/api/live/{conversation_id}/sessions"

    assert client.post(base, content=b"x" * 4097).status_code == 413
    assert client.post(base, json={"transport": "http"}).status_code == 400
    assert client.post(base, json={"model": "client-selected"}).status_code == 400


def test_live_capacity_configuration_is_bounded():
    assert _live_max_sessions({}) == 16
    assert _live_max_sessions({"PROJECTS_HUB_LIVE_MAX_SESSIONS": "4"}) == 4
    assert _live_max_sessions_per_actor({}, 16) == 2
    assert _live_max_sessions_per_actor(
        {"PROJECTS_HUB_LIVE_MAX_SESSIONS_PER_ACTOR": "3"},
        16,
    ) == 3

    for bad in ("0", "65", "not-a-number"):
        with pytest.raises(RuntimeError):
            _live_max_sessions({"PROJECTS_HUB_LIVE_MAX_SESSIONS": bad})

    for bad in ("0", "17", "not-a-number"):
        with pytest.raises(RuntimeError):
            _live_max_sessions_per_actor(
                {"PROJECTS_HUB_LIVE_MAX_SESSIONS_PER_ACTOR": bad},
                16,
            )


def test_two_actors_are_isolated_and_global_capacity_is_bounded(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    provider = Provider()
    host = make_host(
        store,
        provider,
        max_sessions=2,
        max_sessions_per_actor=2,
    )
    settings = _settings(tmp_path)
    app = create_app(settings, store=store, live_host=host)
    try:
        first = _actor(store, "user-one", "One")
        second = _actor(store, "user-two", "Two")
        conv1 = store.create_conversation(
            first["actor"]["id"],
            first["workspace"]["id"],
            first["projects"][0]["id"],
        )
        conv2 = store.create_conversation(
            second["actor"]["id"],
            second["workspace"]["id"],
            second["projects"][0]["id"],
        )

        with TestClient(app) as client:
            _act_as(client, settings, first["actor"]["id"])
            one = client.post(
                f"/api/live/{conv1['id']}/sessions",
                json={"transport": "wss", "attempt_id": "actor_one"},
            )
            assert one.status_code == 200

            _act_as(client, settings, second["actor"]["id"])
            cross = client.post(
                f"/api/live/{conv1['id']}/sessions/{one.json()['session_id']}/socket-ticket"
            )
            assert cross.status_code == 404

            two = client.post(
                f"/api/live/{conv2['id']}/sessions",
                json={"transport": "wss", "attempt_id": "actor_two"},
            )
            assert two.status_code == 200

            third = client.post(
                f"/api/live/{conv2['id']}/sessions",
                json={"transport": "wss", "attempt_id": "actor_two_over_capacity"},
            )
            assert third.status_code == 429
            assert third.json()["detail"]["code"] == "LIVE_BUSY"

            _act_as(client, settings, first["actor"]["id"])
            assert client.post(
                f"/api/live/{conv1['id']}/sessions/{one.json()['session_id']}/stop"
            ).status_code == 200
            _act_as(client, settings, second["actor"]["id"])
            assert client.post(
                f"/api/live/{conv2['id']}/sessions/{two.json()['session_id']}/stop"
            ).status_code == 200
    finally:
        store.close()


def test_actor_fairness_and_duplicate_buffered_source_admission(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    provider = Provider()
    host = make_host(
        store,
        provider,
        max_sessions=4,
        max_sessions_per_actor=2,
    )
    settings = _settings(tmp_path)
    app = create_app(settings, store=store, live_host=host)
    try:
        actor = _actor(store, "buffered-user", "Buffered")
        conversation = store.create_conversation(
            actor["actor"]["id"],
            actor["workspace"]["id"],
            actor["projects"][0]["id"],
        )
        base = f"/api/live/{conversation['id']}/sessions"
        source_id = "local_" + ("a" * 32)

        with TestClient(app) as client:
            _act_as(client, settings, actor["actor"]["id"])

            first = client.post(
                base,
                json={
                    "transport": "wss",
                    "attempt_id": "buffered_one",
                    "audio_mode": "buffered",
                    "client_source_id": source_id,
                },
            )
            assert first.status_code == 200

            duplicate = client.post(
                base,
                json={
                    "transport": "wss",
                    "attempt_id": "buffered_duplicate",
                    "audio_mode": "buffered",
                    "client_source_id": source_id,
                },
            )
            assert duplicate.status_code == 429
            assert duplicate.json()["detail"]["code"] == "LIVE_BUSY"

            second_distinct = client.post(
                base,
                json={
                    "transport": "wss",
                    "attempt_id": "buffered_two",
                    "audio_mode": "buffered",
                    "client_source_id": "local_" + ("b" * 32),
                },
            )
            assert second_distinct.status_code == 200

            actor_over_limit = client.post(
                base,
                json={
                    "transport": "wss",
                    "attempt_id": "buffered_three",
                    "audio_mode": "buffered",
                    "client_source_id": "local_" + ("c" * 32),
                },
            )
            assert actor_over_limit.status_code == 429
            assert actor_over_limit.json()["detail"]["code"] == "LIVE_BUSY"

            for started in (first, second_distinct):
                assert client.post(
                    f"{base}/{started.json()['session_id']}/stop"
                ).status_code == 200
    finally:
        store.close()
