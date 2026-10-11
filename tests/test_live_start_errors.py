from pathlib import Path

from fastapi.testclient import TestClient

from projects_hub.app import create_app, _error
from projects_hub.settings import Settings
from projects_hub.store import DurableStore


class ResourceDenial(RuntimeError):
    code = "RESOURCE_TOKEN_BUDGET"
    retry_after_ms = 60000


def test_resource_retry_metadata_is_numeric_and_bounded():
    for value in (True, "3000", -1, float("nan"), float("inf"), 86400001):
        error = ResourceDenial("budget")
        error.retry_after_ms = value
        assert "retry_after_ms" not in _error(error).detail


def test_live_start_preserves_resource_code_and_retry_through_http(tmp_path: Path):
    class DeniedHost:
        async def start(self, **_kwargs):
            raise ResourceDenial("resource admission refused")

        async def stop_all(self):
            pass

    store = DurableStore(tmp_path / "data")
    settings = Settings(
        data_dir=tmp_path / "data", static_dir=tmp_path / "missing-ui",
        session_secret="test-secret", dev_auth=True, cookie_secure=False,
    )
    app = create_app(settings, store=store, live_host=DeniedHost())
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            assert client.post("/api/dev/login", json={}).status_code == 200
            bootstrap = client.get("/api/bootstrap").json()
            conversation = client.post("/api/conversations", json={
                "workspace_id": bootstrap["workspace"]["id"],
                "focus_project_id": bootstrap["projects"][0]["id"],
            }).json()
            response = client.post(f"/api/live/{conversation['id']}/sessions", json={})
            assert response.status_code == 503
            assert response.json()["detail"] == {
                "code": "RESOURCE_TOKEN_BUDGET", "message": "resource admission refused", "retry_after_ms": 60000,
            }
    finally:
        store.close()
