import asyncio
import hashlib
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from projects_hub.device_commands import DeviceCommandService
from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_resources import ConversationScope
from projects_hub.store import DurableStore, StoreError


CALENDAR_READ_ARGS = {
    "starts_at": "2026-10-02T00:00:00+02:00",
    "ends_at": "2026-10-03T00:00:00+02:00",
    "limit": 20,
}


CALENDAR_ARGS = {
    "title": "Встреча по проекту",
    "starts_at": "2026-10-02T14:00:00+02:00",
    "ends_at": "2026-10-02T15:00:00+02:00",
    "timezone": "Europe/Kaliningrad",
    "description": "Обсудить следующий шаг",
    "location": "Калининград",
}


def setup(tmp_path: Path):
    store = DurableStore(tmp_path)
    boot = store.ensure_dev_workspace("Device")
    actor = boot["actor"]["id"]
    workspace = boot["workspace"]["id"]
    project = next(item["id"] for item in boot["projects"] if item["name"] == "Projects Hub")
    service = DeviceCommandService(store)
    registration = service.register_device(
        actor_id=actor,
        workspace_id=workspace,
        display_name="Pixel",
        platform="android",
        capabilities=["calendar.create_event"],
    )
    return store, boot, actor, workspace, project, service, registration


@pytest.mark.asyncio
async def test_calendar_command_claim_receipt_and_idempotent_readback(tmp_path: Path):
    store, _boot, actor, workspace, project, service, registration = setup(tmp_path)
    try:
        token = registration["device_token"]
        device_id = registration["device"]["id"]
        auth = "Device " + token

        stored = store.device_auth_record(device_id)
        assert stored is not None
        assert token not in str(stored)
        assert stored["credential_hash"] != hashlib.sha256(token.encode()).hexdigest()

        command = service.create_calendar_command(
            actor_id=actor,
            workspace_id=workspace,
            project_id=project,
            command_id="cmd_" + ("a" * 40),
            args=CALENDAR_ARGS,
        )
        assert command["status"] == "pending"
        assert command["device_id"] == device_id
        assert command["payload"]["requires_user_confirmation"] is True

        claimed = await service.next_command(
            authorization=auth,
            wait_ms=0,
        )
        assert claimed["command"]["command_id"] == command["id"]
        assert claimed["command"]["capability"] == "calendar.create_event"
        assert claimed["command"]["payload"]["title"] == CALENDAR_ARGS["title"]
        claim_token = claimed["claim_token"]

        with pytest.raises(StoreError) as digest_error:
            service.receipt(
                authorization=auth,
                command_id=command["id"],
                claim_token=claim_token,
                payload_sha256="0" * 64,
                status="applied",
                result={"readback_verified": True, "event_id": "42"},
            )
        assert digest_error.value.code == "DEVICE_COMMAND_CONFLICT"

        with pytest.raises(StoreError) as readback_error:
            service.receipt(
                authorization=auth,
                command_id=command["id"],
                claim_token=claim_token,
                payload_sha256=claimed["command"]["payload_sha256"],
                status="applied",
                result={"readback_verified": False, "event_id": "42"},
            )
        assert readback_error.value.code == "DEVICE_READBACK_REQUIRED"

        receipt = service.receipt(
            authorization=auth,
            command_id=command["id"],
            claim_token=claim_token,
            payload_sha256=claimed["command"]["payload_sha256"],
            status="applied",
            result={
                "readback_verified": True,
                "event_id": "calendar-event-42",
                "provider_status": "present",
            },
        )
        assert receipt["status"] == "applied"
        assert receipt["result"]["readback_verified"] is True
        assert receipt["result"]["event_id"] == "calendar-event-42"

        repeated = service.receipt(
            authorization=auth,
            command_id=command["id"],
            claim_token=claim_token,
            payload_sha256=claimed["command"]["payload_sha256"],
            status="applied",
            result={
                "readback_verified": True,
                "event_id": "calendar-event-42",
                "provider_status": "present",
            },
        )
        assert repeated == receipt
    finally:
        store.close()


@pytest.mark.asyncio
async def test_live_function_waits_for_same_android_receipt(tmp_path: Path):
    store, _boot, actor, workspace, project, service, registration = setup(tmp_path)
    try:
        conversation = store.create_conversation(actor, workspace, project)
        binding = ConversationScope(workspace, actor, conversation["id"]).resource_binding()
        adapter = ProjectsHubLiveAdapter(
            store,
            device_commands=service,
        )
        initialized = adapter.initialize(
            resource_id=binding,
            actor={"subject": actor, "tenant_id": workspace},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
        )
        names = {item["name"] for item in initialized["configuration"]["functions"]}
        assert "devices_list_capabilities" in names
        assert "calendar_create_event_on_device" in names
        assert "calendar_list_events_on_device" in names

        session = SimpleNamespace(state=initialized["state"])
        call = {
            "name": "calendar_create_event_on_device",
            "id": "provider-calendar-1",
            "args": {
                **CALENDAR_ARGS,
                "project_id": project,
                "device_id": registration["device"]["id"],
            },
        }
        task = asyncio.create_task(adapter.execute_tool(session, call))
        claimed = None
        for _ in range(20):
            claimed = await service.next_command(
                authorization="Device " + registration["device_token"],
                wait_ms=0,
            )
            if claimed["command"] is not None:
                break
            await asyncio.sleep(0.01)
        assert claimed is not None
        assert claimed["command"] is not None

        service.receipt(
            authorization="Device " + registration["device_token"],
            command_id=claimed["command"]["command_id"],
            claim_token=claimed["claim_token"],
            payload_sha256=claimed["command"]["payload_sha256"],
            status="applied",
            result={
                "readback_verified": True,
                "event_id": "event-99",
                "provider_status": "present",
            },
        )
        result = await asyncio.wait_for(task, timeout=2)
        assert result["status"] == "applied"
        assert result["result"]["event_id"] == "event-99"
        assert "device_token" not in str(result)
        assert "claim_token" not in str(result)
    finally:
        store.close()


def test_multiple_devices_require_explicit_target_and_revoked_device_stops_commands(tmp_path: Path):
    store, _boot, actor, workspace, project, service, registration = setup(tmp_path)
    try:
        service.register_device(
            actor_id=actor,
            workspace_id=workspace,
            display_name="Tablet",
            platform="android",
            capabilities=["calendar.create_event"],
        )
        with pytest.raises(StoreError) as ambiguous:
            service.create_calendar_command(
                actor_id=actor,
                workspace_id=workspace,
                project_id=project,
                command_id="cmd_" + ("b" * 40),
                args=CALENDAR_ARGS,
            )
        assert ambiguous.value.code == "DEVICE_SELECTION_REQUIRED"

        service.disable_device(
            actor_id=actor,
            workspace_id=workspace,
            device_id=registration["device"]["id"],
        )
        with pytest.raises(StoreError) as auth_error:
            service.authenticate("Device " + registration["device_token"])
        assert auth_error.value.code == "DEVICE_UNAUTHENTICATED"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_claimed_command_becomes_outcome_unknown_not_retried(tmp_path: Path):
    store, _boot, actor, workspace, project, service, registration = setup(tmp_path)
    try:
        command = service.create_calendar_command(
            actor_id=actor,
            workspace_id=workspace,
            project_id=project,
            command_id="cmd_" + ("c" * 40),
            args={**CALENDAR_ARGS, "device_id": registration["device"]["id"]},
        )
        claimed = await service.next_command(
            authorization="Device " + registration["device_token"],
            wait_ms=0,
        )
        assert claimed["command"]["command_id"] == command["id"]

        old = round(time.time() * 1000) - 130_000
        store.db.execute(
            "UPDATE device_commands SET claimed_at_ms=? WHERE id=?",
            (old, command["id"]),
        )
        result = await service.wait_for_terminal(
            actor_id=actor,
            workspace_id=workspace,
            command_id=command["id"],
            timeout_seconds=0,
        )
        assert result["status"] == "outcome_unknown"
        assert result["reconciliation_required"] is True

        next_value = await service.next_command(
            authorization="Device " + registration["device_token"],
            wait_ms=0,
        )
        assert next_value == {"command": None}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_calendar_read_command_returns_bounded_device_events(tmp_path: Path):
    store, _boot, actor, workspace, project, service, registration = setup(tmp_path)
    try:
        auth = "Device " + registration["device_token"]
        updated = service.update_capabilities(
            authorization=auth,
            capabilities=["calendar.create_event", "calendar.read_events"],
        )
        assert "calendar.read_events" in updated["capabilities"]

        command = service.create_calendar_read_command(
            actor_id=actor,
            workspace_id=workspace,
            project_id=project,
            command_id="cmd_" + ("b" * 40),
            args=CALENDAR_READ_ARGS,
        )
        claimed = await service.next_command(authorization=auth, wait_ms=0)
        assert claimed["command"]["capability"] == "calendar.read_events"
        assert claimed["command"]["payload"]["limit"] == 20

        receipt = service.receipt(
            authorization=auth,
            command_id=command["id"],
            claim_token=claimed["claim_token"],
            payload_sha256=claimed["command"]["payload_sha256"],
            status="applied",
            result={
                "readback_verified": True,
                "provider_status": "present",
                "count": 1,
                "events": [{
                    "event_id": "42",
                    "title": "Встреча",
                    "starts_at": "2026-10-02T14:00:00+02:00",
                    "ends_at": "2026-10-02T15:00:00+02:00",
                    "all_day": False,
                    "location": "",
                }],
            },
        )
        assert receipt["status"] == "applied"
        assert receipt["result"]["events"][0]["title"] == "Встреча"

        with pytest.raises(StoreError) as too_wide:
            service.create_calendar_read_command(
                actor_id=actor,
                workspace_id=workspace,
                project_id=project,
                command_id="cmd_" + ("c" * 40),
                args={
                    "starts_at": "2026-10-01T00:00:00+02:00",
                    "ends_at": "2026-11-15T00:00:00+02:00",
                },
            )
        assert too_wide.value.code == "INVALID_ARGUMENT"
    finally:
        store.close()



@pytest.mark.asyncio
async def test_calendar_live_tool_returns_pending_without_40_second_voice_stall(tmp_path: Path):
    store, _boot, actor, workspace, project, service, registration = setup(tmp_path)
    try:
        conversation = store.create_conversation(actor, workspace, project)
        binding = ConversationScope(workspace, actor, conversation["id"]).resource_binding()
        adapter = ProjectsHubLiveAdapter(store, device_commands=service)
        initialized = adapter.initialize(
            resource_id=binding,
            actor={"subject": actor, "tenant_id": workspace},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
        )
        seen = {}

        async def return_claimed(**kwargs):
            seen["timeout_seconds"] = kwargs["timeout_seconds"]
            return {
                "command_id": kwargs["command_id"],
                "device_id": registration["device"]["id"],
                "capability": "calendar.create_event",
                "status": "claimed",
                "pending_device_confirmation": True,
            }

        service.wait_for_terminal = return_claimed
        session = SimpleNamespace(state=initialized["state"])
        result = await adapter.execute_tool(
            session,
            {
                "name": "calendar_create_event_on_device",
                "id": "provider-calendar-pending",
                "args": {
                    **CALENDAR_ARGS,
                    "project_id": project,
                    "device_id": registration["device"]["id"],
                },
            },
        )
        assert seen["timeout_seconds"] == 8.0
        assert result["status"] == "claimed"
        assert result["pending_device_confirmation"] is True
    finally:
        store.close()
