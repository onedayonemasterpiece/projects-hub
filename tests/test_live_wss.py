from __future__ import annotations

import asyncio
import base64
import json
import struct
from contextlib import ExitStack
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from live_interaction import LiveError, LiveSocketSessionHost
from starlette.websockets import WebSocketDisconnect

from projects_hub.app import create_app
from projects_hub.auth import COOKIE_NAME, issue_session
from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_admission import ProjectsHubAdmissionMixin
from projects_hub.live_resources import ConversationScope
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
        ws.close()

    stopped = client.post(
        f"/api/live/{conversation_id}/sessions/{value['session_id']}/stop",
        json={"reason": "test_cleanup"},
    )
    assert stopped.status_code == 200


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
        second_conversation = store.create_conversation(
            actor["actor"]["id"],
            actor["workspace"]["id"],
            actor["projects"][0]["id"],
        )
        third_conversation = store.create_conversation(
            actor["actor"]["id"],
            actor["workspace"]["id"],
            actor["projects"][0]["id"],
        )
        base = f"/api/live/{conversation['id']}/sessions"
        second_base = f"/api/live/{second_conversation['id']}/sessions"
        third_base = f"/api/live/{third_conversation['id']}/sessions"
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
                second_base,
                json={
                    "transport": "wss",
                    "attempt_id": "buffered_two",
                    "audio_mode": "buffered",
                    "client_source_id": "local_" + ("b" * 32),
                },
            )
            assert second_distinct.status_code == 200

            actor_over_limit = client.post(
                third_base,
                json={
                    "transport": "wss",
                    "attempt_id": "buffered_three",
                    "audio_mode": "buffered",
                    "client_source_id": "local_" + ("c" * 32),
                },
            )
            assert actor_over_limit.status_code == 429
            assert actor_over_limit.json()["detail"]["code"] == "LIVE_BUSY"

            assert client.post(
                f"{base}/{first.json()['session_id']}/stop"
            ).status_code == 200
            assert client.post(
                f"{second_base}/{second_distinct.json()['session_id']}/stop"
            ).status_code == 200
    finally:
        store.close()


def test_four_actors_hold_isolated_wss_sessions_and_global_capacity(tmp_path: Path):
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
        actors = [
            _actor(store, f"four-user-{index}", f"User {index}")
            for index in range(4)
        ]
        conversations = [
            store.create_conversation(
                actor["actor"]["id"],
                actor["workspace"]["id"],
                actor["projects"][0]["id"],
            )
            for actor in actors
        ]

        with TestClient(app) as client:
            started: list[dict] = []
            sockets = []
            with ExitStack() as stack:
                for index, (actor, conversation) in enumerate(
                    zip(actors, conversations, strict=True)
                ):
                    actor_id = actor["actor"]["id"]
                    _act_as(client, settings, actor_id)
                    response = client.post(
                        f"/api/live/{conversation['id']}/sessions",
                        json={
                            "transport": "wss",
                            "attempt_id": f"four_actor_{index}",
                        },
                    )
                    assert response.status_code == 200, response.text
                    value = response.json()
                    started.append(value)
                    ws = stack.enter_context(socket(client, value))
                    hello(ws, value)
                    sockets.append(ws)

                for index, actor in enumerate(actors):
                    _act_as(client, settings, actor["actor"]["id"])
                    other_index = (index + 1) % len(actors)
                    other = started[other_index]
                    other_conversation = conversations[other_index]
                    cross = client.post(
                        f"/api/live/{other_conversation['id']}/sessions/"
                        f"{other['session_id']}/socket-ticket"
                    )
                    assert cross.status_code == 404

                _act_as(client, settings, actors[0]["actor"]["id"])
                overflow = client.post(
                    f"/api/live/{conversations[0]['id']}/sessions",
                    json={
                        "transport": "wss",
                        "attempt_id": "four_actor_global_overflow",
                    },
                )
                assert overflow.status_code == 429
                assert overflow.json()["detail"]["code"] == "LIVE_BUSY"

                for ws in sockets:
                    ws.send_json({"type": "stop"})
    finally:
        store.close()


def test_same_actor_same_conversation_has_single_live_owner(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    provider = Provider()
    host = make_host(store, provider, max_sessions=4, max_sessions_per_actor=2)
    settings = _settings(tmp_path)
    app = create_app(settings, store=store, live_host=host)
    try:
        actor = _actor(store, "lease-user", "Lease")
        actor_id = actor["actor"]["id"]
        conversation = store.create_conversation(
            actor_id,
            actor["workspace"]["id"],
            actor["projects"][0]["id"],
        )
        second_conversation = store.create_conversation(
            actor_id,
            actor["workspace"]["id"],
            actor["projects"][0]["id"],
        )

        with TestClient(app) as client:
            _act_as(client, settings, actor_id)
            first = client.post(
                f"/api/live/{conversation['id']}/sessions",
                json={"transport": "wss", "attempt_id": "lease_first"},
            )
            assert first.status_code == 200, first.text

            duplicate = client.post(
                f"/api/live/{conversation['id']}/sessions",
                json={"transport": "wss", "attempt_id": "lease_duplicate"},
            )
            assert duplicate.status_code == 429
            assert duplicate.json()["detail"]["code"] == "LIVE_BUSY"

            parallel_other_conversation = client.post(
                f"/api/live/{second_conversation['id']}/sessions",
                json={"transport": "wss", "attempt_id": "lease_other_conversation"},
            )
            assert parallel_other_conversation.status_code == 200

            for conv, started in (
                (conversation, first),
                (second_conversation, parallel_other_conversation),
            ):
                assert client.post(
                    f"/api/live/{conv['id']}/sessions/{started.json()['session_id']}/stop"
                ).status_code == 200

            restarted = client.post(
                f"/api/live/{conversation['id']}/sessions",
                json={"transport": "wss", "attempt_id": "lease_restarted"},
            )
            assert restarted.status_code == 200
            assert client.post(
                f"/api/live/{conversation['id']}/sessions/{restarted.json()['session_id']}/stop"
            ).status_code == 200
    finally:
        store.close()


def test_same_conversation_start_race_is_reserved_before_provider_ready(tmp_path: Path):
    async def scenario():
        store = DurableStore(tmp_path / "data")
        provider = Provider()
        host = make_host(store, provider, max_sessions=4, max_sessions_per_actor=2)
        try:
            actor_record = _actor(store, "lease-race-user", "Lease Race")
            actor_id = actor_record["actor"]["id"]
            workspace_id = actor_record["workspace"]["id"]
            conversation = store.create_conversation(
                actor_id,
                workspace_id,
                actor_record["projects"][0]["id"],
            )
            resource_id = ConversationScope(
                workspace_id=workspace_id,
                subject_id=actor_id,
                conversation_id=conversation["id"],
            ).resource_binding()
            actor = {"subject": actor_id, "tenant_id": workspace_id}

            entered = asyncio.Event()
            release = asyncio.Event()
            original_initialize = host.adapter.initialize

            async def delayed_initialize(**kwargs):
                entered.set()
                await release.wait()
                return original_initialize(**kwargs)

            host.adapter.initialize = delayed_initialize

            first_task = asyncio.create_task(
                host.start(
                    resource_id=resource_id,
                    actor=actor,
                    conversation_id=conversation["id"],
                    audio_mode="realtime",
                    client_source_id=None,
                    attempt_id="lease_race_first",
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=1)

            with pytest.raises(LiveError) as duplicate:
                await host.start(
                    resource_id=resource_id,
                    actor=actor,
                    conversation_id=conversation["id"],
                    audio_mode="realtime",
                    client_source_id=None,
                    attempt_id="lease_race_duplicate",
                )
            assert duplicate.value.code == "LIVE_BUSY"

            release.set()
            started = await asyncio.wait_for(first_task, timeout=2)
            await host.stop(
                session_id=started["session_id"],
                resource_id=resource_id,
                actor=actor,
            )
        finally:
            store.close()

    asyncio.run(scenario())
