from pathlib import Path

from fastapi.testclient import TestClient

from projects_hub.app import create_app
from projects_hub.device_commands import DeviceCommandService
from projects_hub.settings import Settings
from projects_hub.store import DurableStore


PUBLIC_ORIGIN = "https://projects-hub.kenigevents.ru"


def dev_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        static_dir=tmp_path / "missing",
        session_secret="s" * 48,
        dev_auth=True,
        cookie_secure=False,
    )


def public_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        static_dir=tmp_path / "missing",
        session_secret="s" * 48,
        dev_auth=False,
        cookie_secure=True,
        public_origin=PUBLIC_ORIGIN,
    )


def public_headers() -> dict[str, str]:
    return {
        "host": "projects-hub.kenigevents.ru",
        "x-forwarded-host": "projects-hub.kenigevents.ru",
        "x-forwarded-proto": "https",
        "x-forwarded-port": "443",
    }


def create_pending(
    service: DeviceCommandService,
    *,
    actor: str,
    workspace: str,
    project: str,
    device_id: str,
    suffix: str,
):
    return service.create_calendar_command(
        actor_id=actor,
        workspace_id=workspace,
        project_id=project,
        command_id="cmd_" + (suffix * 40),
        args={
            "device_id": device_id,
            "title": "Встреча",
            "starts_at": "2026-10-03T10:00:00+02:00",
            "ends_at": "2026-10-03T11:00:00+02:00",
            "timezone": "Europe/Kaliningrad",
        },
    )


def test_android_pairing_then_device_channel_needs_no_browser_cookie(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    service = DeviceCommandService(store)
    app = create_app(
        dev_settings(tmp_path),
        store=store,
        device_commands=service,
    )
    try:
        with TestClient(app, base_url="http://localhost") as client:
            boot = client.post("/api/dev/login", json={}).json()
            actor = boot["actor"]["id"]
            workspace = boot["workspace"]["id"]
            project = boot["projects"][0]["id"]

            registered = client.post(
                "/api/devices/register",
                json={
                    "workspace_id": workspace,
                    "display_name": "Pixel 9",
                    "platform": "android",
                    "capabilities": ["calendar.create_event"],
                },
            )
            assert registered.status_code == 200
            body = registered.json()
            device_id = body["device"]["id"]
            device_token = body["device_token"]
            assert device_token.startswith(device_id + ".")
            assert "credential_hash" not in body["device"]

            listed = client.get(
                "/api/devices",
                params={"workspace_id": workspace},
            )
            assert listed.status_code == 200
            serialized = listed.text.lower()
            assert device_token.lower() not in serialized
            assert "credential_hash" not in serialized

            command = create_pending(
                service,
                actor=actor,
                workspace=workspace,
                project=project,
                device_id=device_id,
                suffix="d",
            )

            client.cookies.clear()
            unauthenticated = client.get(
                "/api/device/commands/next",
                params={"wait_ms": 0},
            )
            assert unauthenticated.status_code == 401

            claimed = client.get(
                "/api/device/commands/next",
                params={"wait_ms": 0},
                headers={"authorization": "Device " + device_token},
            )
            assert claimed.status_code == 200
            claim = claimed.json()
            assert claim["command"]["command_id"] == command["id"]

            receipt = client.post(
                f"/api/device/commands/{command['id']}/receipt",
                json={
                    "claim_token": claim["claim_token"],
                    "payload_sha256": claim["command"]["payload_sha256"],
                    "status": "applied",
                    "result": {
                        "readback_verified": True,
                        "event_id": "event-http-1",
                        "provider_status": "present",
                    },
                },
                headers={"authorization": "Device " + device_token},
            )
            assert receipt.status_code == 200
            assert receipt.json()["status"] == "applied"
    finally:
        store.close()


def test_public_device_receipt_is_not_browser_origin_bound_but_still_device_authenticated(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    service = DeviceCommandService(store)
    boot = store.ensure_dev_workspace("Public Android")
    actor = boot["actor"]["id"]
    workspace = boot["workspace"]["id"]
    project = boot["projects"][0]["id"]
    registered = service.register_device(
        actor_id=actor,
        workspace_id=workspace,
        display_name="Pixel Public",
        platform="android",
        capabilities=["calendar.create_event"],
    )
    command = create_pending(
        service,
        actor=actor,
        workspace=workspace,
        project=project,
        device_id=registered["device"]["id"],
        suffix="e",
    )

    app = create_app(
        public_settings(tmp_path),
        store=store,
        device_commands=service,
        identity_verifier=object(),
    )
    try:
        with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
            claimed = client.get(
                "/api/device/commands/next",
                params={"wait_ms": 0},
                headers={
                    **public_headers(),
                    "authorization": "Device " + registered["device_token"],
                },
            )
            assert claimed.status_code == 200
            claim = claimed.json()

            denied = client.post(
                f"/api/device/commands/{command['id']}/receipt",
                json={
                    "claim_token": claim["claim_token"],
                    "payload_sha256": claim["command"]["payload_sha256"],
                    "status": "applied",
                    "result": {
                        "readback_verified": True,
                        "event_id": "event-http-2",
                    },
                },
                headers=public_headers(),
            )
            assert denied.status_code == 401

            accepted = client.post(
                f"/api/device/commands/{command['id']}/receipt",
                json={
                    "claim_token": claim["claim_token"],
                    "payload_sha256": claim["command"]["payload_sha256"],
                    "status": "applied",
                    "result": {
                        "readback_verified": True,
                        "event_id": "event-http-2",
                    },
                },
                headers={
                    **public_headers(),
                    "authorization": "Device " + registered["device_token"],
                },
            )
            assert accepted.status_code == 200
            assert accepted.json()["status"] == "applied"
    finally:
        store.close()
