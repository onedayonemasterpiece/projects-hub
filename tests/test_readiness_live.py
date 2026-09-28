import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_resources import ConversationScope
from projects_hub.readiness import ReadinessService
from projects_hub.store import DurableStore


class AppliedCalendarDevice:
    def list_devices(self, *, actor_id: str, workspace_id: str):
        return [
            {
                "id": "dev_test",
                "display_name": "Android",
                "platform": "android",
                "capabilities": ["calendar.create_event"],
                "last_seen_at_ms": 1,
            }
        ]

    def create_calendar_command(self, **_kwargs):
        return {"id": "cmd_device_calendar"}

    async def wait_for_terminal(self, **_kwargs):
        await asyncio.sleep(0)
        return {
            "status": "applied",
            "device_id": "dev_test",
            "result": {
                "readback_verified": True,
                "event_id": "calendar-provider-42",
                "provider_status": "present",
            },
        }


@pytest.mark.asyncio
async def test_live_calendar_creates_readiness_card_and_tools_update_it(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    try:
        boot = store.ensure_dev_workspace("Readiness live")
        actor = boot["actor"]["id"]
        workspace = boot["workspace"]["id"]
        project = next(item["id"] for item in boot["projects"] if item["name"] == "Projects Hub")
        conversation = store.create_conversation(actor, workspace, project)
        binding = ConversationScope(workspace, actor, conversation["id"]).resource_binding()
        readiness = ReadinessService(store)
        adapter = ProjectsHubLiveAdapter(
            store,
            device_commands=AppliedCalendarDevice(),
            readiness=readiness,
        )
        initialized = adapter.initialize(
            resource_id=binding,
            actor={"subject": actor, "tenant_id": workspace},
            model="gemini-live",
            conversation_id=conversation["id"],
        )
        function_names = {
            item["name"] for item in initialized["configuration"]["functions"]
        }
        assert {
            "event_cards_list",
            "event_readiness_set",
            "task_create_follow_up",
            "task_set_state",
        }.issubset(function_names)

        session = SimpleNamespace(state=initialized["state"])
        created = await adapter.execute_tool(
            session,
            {
                "name": "calendar_create_event_on_device",
                "id": "provider-call-1",
                "args": {
                    "project_id": project,
                    "title": "Запись подкаста",
                    "starts_at": "2026-10-03T18:00:00+02:00",
                    "ends_at": "2026-10-03T19:00:00+02:00",
                    "timezone": "Europe/Kaliningrad",
                    "event_type": "podcast",
                },
            },
        )
        assert created["status"] == "applied"
        card = created["event_card"]
        assert card["event_type"] == "podcast"
        assert card["incomplete_count"] == 4
        assert card["device_event_id"] == "calendar-provider-42"

        listed = await adapter.execute_tool(
            session,
            {"name": "event_cards_list", "id": "provider-call-2", "args": {}},
        )
        assert [item["id"] for item in listed["events"]] == [card["id"]]

        updated = await adapter.execute_tool(
            session,
            {
                "name": "event_readiness_set",
                "id": "provider-call-3",
                "args": {
                    "event_id": card["id"],
                    "checklist_key": "questions",
                    "done": True,
                },
            },
        )
        assert next(
            item for item in updated["checklist"] if item["key"] == "questions"
        )["done"] is True

        task = await adapter.execute_tool(
            session,
            {
                "name": "task_create_follow_up",
                "id": "provider-call-4",
                "args": {
                    "event_id": card["id"],
                    "project_id": project,
                    "title": "Подготовить вступление",
                    "deadline": "2026-10-03T15:00:00+02:00",
                },
            },
        )
        assert task["state"] == "proposed"

        done = await adapter.execute_tool(
            session,
            {
                "name": "task_set_state",
                "id": "provider-call-5",
                "args": {"task_id": task["id"], "state": "done"},
            },
        )
        assert done["state"] == "done"
    finally:
        store.close()
