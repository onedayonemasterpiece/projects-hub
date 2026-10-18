from pathlib import Path
import asyncio
import re

import httpx
import pytest
from fastapi.testclient import TestClient
from live_interaction import LiveError

from projects_hub.app import create_app, _error
from projects_hub.settings import Settings
from projects_hub.store import DurableStore


class ResourceDenial(RuntimeError):
    code = "RESOURCE_TOKEN_BUDGET"
    retry_after_ms = 60000


@pytest.mark.parametrize("failure", [
    TimeoutError("secret-provider-detail"),
    LiveError("TimeoutError", "secret-provider-detail"),
])
def test_provider_setup_timeout_is_retryable_android_safe_http_error(tmp_path: Path, failure):
    class TimedOutHost:
        async def start(self, **_kwargs):
            raise failure

    store = DurableStore(tmp_path / "data")
    settings = Settings(
        data_dir=tmp_path / "data", static_dir=tmp_path / "missing-ui",
        session_secret="test-secret", dev_auth=True, cookie_secure=False,
    )
    app = create_app(settings, store=store, live_host=TimedOutHost())

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
        ) as client:
            assert (await client.post("/api/dev/login", json={})).status_code == 200
            bootstrap = (await client.get("/api/bootstrap")).json()
            conversation = (await client.post("/api/conversations", json={
                "workspace_id": bootstrap["workspace"]["id"],
                "focus_project_id": bootstrap["projects"][0]["id"],
            })).json()
            response = await client.post(
                f"/api/live/{conversation['id']}/sessions", json={},
            )
            assert response.status_code == 503
            detail = response.json()["detail"]
            assert detail["code"] == "LIVE_PROVIDER_SETUP_TIMEOUT"
            assert re.fullmatch(r"[A-Z0-9_]{1,100}", detail["code"])
            assert detail["retry_after_ms"] == 2000
            assert "secret-provider-detail" not in response.text

    try:
        asyncio.run(exercise())
    finally:
        store.close()


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
