from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from projects_hub.analytics import ANALYSIS_MODEL_OPTIONS, CODEX_ANALYSIS_LADDER, AnalyticsService
from projects_hub.app import create_app
from projects_hub.auth import COOKIE_NAME, issue_session
from projects_hub.board import BoardService
from projects_hub.board_api import BOARD_PROTOCOL, CLIENT_PREFIX, TICKET_PREFIX
from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_resources import ConversationScope
from projects_hub.settings import Settings
from projects_hub.store import DurableStore


class FakeBridge:
    def __init__(self):
        self.consults = []
        self.councils = []

    async def consult(self, **kwargs):
        self.consults.append(kwargs)
        return {"status": "running", "taskId": "dvt_" + "a" * 32}

    async def council(self, **kwargs):
        self.councils.append(dict(kwargs))
        return {
            "status": "running",
            "taskId": "dvt_" + "c" * 32,
            "resourcePolicy": (
                "nvidia_dual_slot_kimi_deepseek_full_debate"
                if kwargs.get("tier") == "pro"
                else "free_only_no_nvidia"
            ),
            "nvidiaSlotCapacity": 2,
            "requiresExplicitUserConfirmation": False,
        }

    async def read_task(self, _task_id):
        return {
            "status": "completed",
            "latestTurn": {
                "finalResponse": "# Result\n\n<script>alert('x')</script>\n\nRecommendation."
            },
        }

    async def cancel_task(self, task_id):
        return {"status": "interrupted", "taskId": task_id}

    async def close(self):
        return None


class FakeHub:
    def __init__(self):
        self.messages = []

    async def publish(self, board_id, payload, **_kwargs):
        self.messages.append((board_id, payload))


def setup(tmp_path: Path):
    store = DurableStore(tmp_path / "data")
    boot = store.ensure_dev_workspace("Analytics API")
    actor = boot["actor"]["id"]
    workspace = boot["workspace"]["id"]
    project = boot["projects"][0]["id"]
    board = BoardService(store)
    board_id = board.open_board(actor, workspace, project)["id"]
    board.apply_command(
        actor_id=actor,
        workspace_id=workspace,
        board_id=board_id,
        command_id="cmd_analysis_seed",
        operation="create",
        object_id="obj_analysis_seed",
        expected_object_revision=None,
        payload={"text": "Risk: provider timeout", "style": {"color": "yellow"}},
        execution_origin="mira",
    )
    bridge = FakeBridge()
    analytics = AnalyticsService(store, board, bridge=bridge)
    return store, boot, actor, workspace, project, board, board_id, bridge, analytics


def test_analysis_http_report_is_text_only_and_publish_broadcasts(tmp_path: Path):
    store, boot, actor, workspace, project, board, board_id, bridge, analytics = setup(tmp_path)
    settings = Settings(
        data_dir=tmp_path / "data",
        static_dir=tmp_path / "missing-ui",
        session_secret="s" * 64,
        dev_auth=True,
        cookie_secure=False,
    )
    app = create_app(settings, store=store, analytics=analytics)
    try:
        with TestClient(app, base_url="http://testserver") as client:
            client.cookies.set(COOKIE_NAME, issue_session(actor, settings.session_secret))
            start = client.post(
                "/api/analysis/runs",
                json={
                    "workspace_id": workspace,
                    "project_id": project,
                    "board_id": board_id,
                    "object_ids": ["obj_analysis_seed"],
                    "command_id": "analysis_http_01",
                    "model": "kimi_k3",
                    "purpose": "edge_cases",
                    "question": "Find the main edge case.",
                },
            )
            assert start.status_code == 200, start.text
            run_id = start.json()["id"]
            assert bridge.consults[0]["evidence_bundle"]
            refreshed = client.post(
                f"/api/analysis/runs/{run_id}/refresh",
                json={"workspace_id": workspace},
            )
            assert refreshed.status_code == 200
            assert refreshed.json()["status"] == "completed"

            report = client.get(
                f"/api/analysis/runs/{run_id}/report.md",
                params={"workspace_id": workspace},
            )
            assert report.status_code == 200
            assert "<script>alert('x')</script>" in report.text
            assert report.headers["content-type"].startswith("text/markdown")
            assert report.headers["cache-control"] == "no-store"
            assert report.headers["x-content-type-options"] == "nosniff"
            assert "default-src 'none'" in report.headers["content-security-policy"]
            assert report.headers["referrer-policy"] == "no-referrer"
            assert "attachment" in report.headers["content-disposition"]

            ticket = client.post(
                f"/api/boards/{board_id}/socket-ticket",
                json={"workspace_id": workspace, "client_instance_id": "analytics-listener-1"},
            ).json()
            protocols = [
                BOARD_PROTOCOL,
                TICKET_PREFIX + ticket["ticket"],
                CLIENT_PREFIX + "analytics-listener-1",
            ]
            with client.websocket_connect(
                f"/api/boards/{board_id}/socket",
                subprotocols=protocols,
                headers={"origin": "http://testserver"},
            ) as socket:
                assert socket.receive_json()["type"] == "snapshot"
                published = client.post(
                    f"/api/analysis/runs/{run_id}/publish",
                    json={
                        "workspace_id": workspace,
                        "command_id": "analysis_publish_http_01",
                        "object_id": "obj_analysis_http_report",
                    },
                )
                assert published.status_code == 200, published.text
                event = socket.receive_json()
                assert event["type"] == "event"
                assert event["event"]["after"]["reference"] == {
                    "kind": "analysis_run",
                    "id": run_id,
                }
    finally:
        store.close()


@pytest.mark.asyncio
async def test_mira_analysis_tool_uses_same_session_and_broadcasts_publish(tmp_path: Path):
    store, boot, actor, workspace, project, board, board_id, _bridge, analytics = setup(tmp_path)
    hub = FakeHub()
    try:
        conversation = store.create_conversation(actor, workspace, project)
        binding = ConversationScope(workspace, actor, conversation["id"]).resource_binding()
        adapter = ProjectsHubLiveAdapter(
            store,
            board=board,
            board_hub=hub,
            analytics=analytics,
        )
        initialized = adapter.initialize(
            resource_id=binding,
            actor={"subject": actor, "tenant_id": workspace},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
        )
        functions = {item["name"] for item in initialized["configuration"]["functions"]}
        assert "board_analysis" in functions
        assert initialized["response"]["analysis_enabled"] is True
        session = SimpleNamespace(state=initialized["state"])

        started = await adapter.execute_tool(
            session,
            {
                "name": "board_analysis",
                "args": {
                    "action": "start",
                    "object_ids": ["obj_analysis_seed"],
                    "model": "kimi_k3",
                    "purpose": "requirements",
                    "question": "Check the selected risk.",
                },
            },
        )
        assert started["status"] == "running"
        assert started["ui_command"]["kind"] == "analysis"

        completed = await adapter.execute_tool(
            session,
            {
                "name": "board_analysis",
                "args": {"action": "status", "run_id": started["id"]},
            },
        )
        assert completed["status"] == "completed"

        receipt = await adapter.execute_tool(
            session,
            {
                "name": "board_analysis",
                "args": {"action": "publish", "run_id": started["id"]},
            },
        )
        assert receipt["status"] == "saved"
        assert hub.messages[-1][0] == board_id
        assert hub.messages[-1][1]["event"]["after"]["reference"]["id"] == started["id"]
    finally:
        store.close()



def test_nvidia_council_http_auto_dispatches_without_confirm_endpoint(tmp_path: Path):
    store, boot, actor, workspace, project, _board, board_id, bridge, analytics = setup(tmp_path)
    settings = Settings(
        data_dir=tmp_path / "data",
        static_dir=tmp_path / "missing-ui",
        session_secret="s" * 64,
        dev_auth=True,
        cookie_secure=False,
    )
    app = create_app(settings, store=store, analytics=analytics)
    try:
        with TestClient(app, base_url="http://testserver") as client:
            client.cookies.set(COOKIE_NAME, issue_session(actor, settings.session_secret))
            started = client.post(
                "/api/analysis/runs",
                json={
                    "workspace_id": workspace,
                    "project_id": project,
                    "board_id": board_id,
                    "object_ids": ["obj_analysis_seed"],
                    "command_id": "analysis_nvidia_http_01",
                    "model": "council_pro",
                    "purpose": "architecture",
                    "question": "Compare two risks.",
                },
            )
            assert started.status_code == 200, started.text
            run = started.json()
            assert run["status"] == "running"
            assert "confirmation_plan" not in run
            assert len(bridge.councils) == 1
            assert bridge.councils[0]["tier"] == "pro"

            obsolete = client.post(
                f"/api/analysis/runs/{run['id']}/confirm",
                json={"workspace_id": workspace},
            )
            assert obsolete.status_code == 404
            assert len(bridge.councils) == 1
    finally:
        store.close()

@pytest.mark.asyncio

@pytest.mark.asyncio
async def test_mira_nvidia_council_starts_without_confirmation_action(tmp_path: Path):
    store, _boot, actor, workspace, project, board, board_id, bridge, analytics = setup(tmp_path)
    try:
        conversation = store.create_conversation(actor, workspace, project)
        binding = ConversationScope(workspace, actor, conversation["id"]).resource_binding()
        adapter = ProjectsHubLiveAdapter(store, board=board, analytics=analytics)
        initialized = adapter.initialize(
            resource_id=binding,
            actor={"subject": actor, "tenant_id": workspace},
            model="gemini-3.8-live",
            conversation_id=conversation["id"],
        )
        analysis_fn = next(
            item for item in initialized["configuration"]["functions"]
            if item["name"] == "board_analysis"
        )
        assert "confirm" not in analysis_fn["parameters"]["properties"]["action"]["enum"]
        assert "confirmed" not in analysis_fn["parameters"]["properties"]
        assert analysis_fn["parameters"]["properties"]["model"]["enum"] == list(ANALYSIS_MODEL_OPTIONS)
        assert list(CODEX_ANALYSIS_LADDER) == analysis_fn["parameters"]["properties"]["model"]["enum"][:7]
        session = SimpleNamespace(state=initialized["state"])

        started = await adapter.execute_tool(
            session,
            {
                "name": "board_analysis",
                "args": {
                    "action": "start",
                    "object_ids": ["obj_analysis_seed"],
                    "model": "council_pro",
                    "purpose": "architecture",
                    "question": "Compare two risks.",
                },
            },
        )
        assert started["status"] == "running"
        assert started["ui_command"]["kind"] == "analysis"
        assert len(bridge.councils) == 1
        assert bridge.councils[0]["tier"] == "pro"
    finally:
        store.close()

