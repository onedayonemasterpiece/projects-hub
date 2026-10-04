from pathlib import Path

from fastapi.testclient import TestClient

from projects_hub.app import create_app
from projects_hub.settings import Settings
from projects_hub.store import DurableStore


class FakeLiveHost:
    def __init__(self):
        self.calls = []
        self.closed = False

    async def start(self, **kwargs):
        self.calls.append(("start", kwargs))
        return {
            "session_id": "live_test",
            "model": kwargs["model"],
            "conversation_id": kwargs["conversation_id"],
            "source_id": "src_fake",
        }

    async def input(self, **kwargs):
        self.calls.append(("input", kwargs))
        return {"ok": True, "session_id": kwargs["session_id"]}

    def events(self, **kwargs):
        self.calls.append(("events", kwargs))
        return {"events": [], "cursor": kwargs.get("after", 0), "has_more": False, "gap": False, "closed": False}

    async def stop(self, **kwargs):
        self.calls.append(("stop", kwargs))
        return {"ok": True, "session_id": kwargs["session_id"]}

    async def stop_all(self):
        self.closed = True


def test_dev_login_bootstrap_and_live_routes_are_actor_bound(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    fake = FakeLiveHost()
    settings = Settings(
        data_dir=tmp_path / "data",
        static_dir=tmp_path / "missing-ui",
        session_secret="test-secret",
        dev_auth=True,
        cookie_secure=False,
    )
    app = create_app(settings, store=store, live_host=fake)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/bootstrap").status_code == 401
        login = client.post("/api/dev/login", json={})
        assert login.status_code == 200
        boot = login.json()
        assert len(boot["projects"]) >= 1

        runtime = client.get("/api/runtime")
        assert runtime.status_code == 200
        assert runtime.json()["backend_version"] == "0.1.20"
        assert "release_sha" in runtime.json()["build"]

        bootstrap = client.get("/api/bootstrap")
        assert bootstrap.status_code == 200
        workspace_id = bootstrap.json()["workspace"]["id"]
        project_id = bootstrap.json()["projects"][0]["id"]

        conversation = client.post(
            "/api/conversations",
            json={"workspace_id": workspace_id, "focus_project_id": project_id},
        )
        assert conversation.status_code == 200
        conversation_id = conversation.json()["id"]

        started = client.post(f"/api/live/{conversation_id}/sessions", json={})
        assert started.status_code == 200
        assert started.json()["session_id"] == "live_test"

        sent = client.post(
            f"/api/live/{conversation_id}/sessions/live_test/input",
            json={"audio_base64": "AAAA"},
        )
        assert sent.status_code == 200

        events = client.get(f"/api/live/{conversation_id}/sessions/live_test/events?after=0")
        assert events.status_code == 200
        stopped = client.post(f"/api/live/{conversation_id}/sessions/live_test/stop", json={})
        assert stopped.status_code == 200

        wrong = client.post("/api/conversations", json={"workspace_id": "ws_other"})
        assert wrong.status_code == 403
    assert fake.closed
    store.close()


def test_dev_login_is_not_available_through_a_proxy_or_public_host(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    settings = Settings(
        data_dir=tmp_path / "data",
        static_dir=tmp_path / "missing-ui",
        session_secret="test-secret-that-is-long-enough-for-tests",
        dev_auth=True,
        cookie_secure=False,
    )
    app = create_app(settings, store=store, live_host=FakeLiveHost())
    try:
        with TestClient(app, base_url="http://example.test") as client:
            assert client.post("/api/dev/login", json={}).status_code == 404
        with TestClient(app, base_url="http://127.0.0.1") as client:
            response = client.post(
                "/api/dev/login",
                json={},
                headers={"x-forwarded-for": "127.0.0.1"},
            )
            assert response.status_code == 404
    finally:
        store.close()
