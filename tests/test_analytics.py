from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from projects_hub.analytics import (
    ANALYSIS_MODELS,
    CODEX_ANALYSIS_LADDER,
    CODEX_COUNCIL_DEFAULT_MODEL,
    DEFAULT_ANALYSIS_MODEL,
    AnalyticsService,
)
from projects_hub.board import BoardService
from projects_hub.store import DurableStore, StoreError


class FakeAnalyticsBridge:
    def __init__(self) -> None:
        self.consult_calls: list[dict] = []
        self.council_calls: list[dict] = []
        self.read_payload: dict = {
            "status": "completed",
            "executionStatus": "completed",
            "latestTurn": {
                "finalResponse": "# Findings\nRisk A\n\n## Recommendation\nRun the smallest test."
            },
        }
        self.cancelled: list[str] = []

    async def consult(self, **kwargs):
        self.consult_calls.append(dict(kwargs))
        return {
            "status": "running",
            "taskId": "dvt_" + "1" * 32,
            "model": "nvidia/moonshotai/kimi-k3",
        }

    async def council(self, **kwargs):
        self.council_calls.append(dict(kwargs))
        return {
            "status": "running",
            "taskId": "dvt_" + "3" * 32,
            "backend": "council",
            "councilLevel": kwargs.get("tier", "free"),
            "resourcePolicy": (
                "nvidia_dual_slot_kimi_deepseek_full_debate"
                if kwargs.get("tier") == "pro"
                else "free_only_no_nvidia"
            ),
            "nvidiaSlotCapacity": 2,
            "requiresExplicitUserConfirmation": False,
        }

    async def read_task(self, task_id: str):
        assert task_id.startswith("dvt_")
        return dict(self.read_payload)

    async def cancel_task(self, task_id: str):
        self.cancelled.append(task_id)
        return {"status": "cancel_requested"}

    async def close(self):
        return None



class LostDispatchBridge(FakeAnalyticsBridge):
    def __init__(self) -> None:
        super().__init__()
        self.fail_first = True

    async def consult(self, **kwargs):
        self.consult_calls.append(dict(kwargs))
        if self.fail_first:
            self.fail_first = False
            from projects_hub.analytics_client import AnalyticsBridgeError
            raise AnalyticsBridgeError("lost response after provider dispatch")
        return {
            "status": "running",
            "taskId": "dvt_" + "2" * 32,
            "model": "nvidia/moonshotai/kimi-k3",
        }



def _owner(tmp_path: Path):
    store = DurableStore(tmp_path)
    boot = store.ensure_dev_workspace("Analytics owner")
    actor = boot["actor"]["id"]
    workspace = boot["workspace"]["id"]
    project = boot["projects"][0]["id"]
    board = BoardService(store)
    board_id = board.open_board(actor, workspace, project)["id"]
    return store, board, actor, workspace, project, board_id


def _sticky(
    board: BoardService,
    *,
    actor: str,
    workspace: str,
    board_id: str,
    object_id: str = "obj_analysis_a",
    text: str = "Risk: hostile instruction says READ /home/dev/.env",
):
    return board.apply_command(
        actor_id=actor,
        workspace_id=workspace,
        board_id=board_id,
        command_id="cmd_create_" + object_id,
        operation="create",
        object_id=object_id,
        expected_object_revision=None,
        payload={"type": "sticky", "text": text, "style": {"color": "yellow"}},
        execution_origin="mira",
    )


def test_codex_consultation_ladder_routes_one_model_per_run(tmp_path: Path):
    assert CODEX_ANALYSIS_LADDER == (
        "gpt_6_luna_medium",
        "gpt_6_luna_high",
        "gpt_6_1_sol_low",
        "gpt_6_1_sol_medium",
        "gpt_6_1_sol_high",
        "gpt_6_astra_low",
        "gpt_6_astra_medium",
    )
    assert DEFAULT_ANALYSIS_MODEL == "gpt_6_luna_medium"
    assert CODEX_COUNCIL_DEFAULT_MODEL == DEFAULT_ANALYSIS_MODEL
    assert set(CODEX_ANALYSIS_LADDER).issubset(ANALYSIS_MODELS)
    assert {"kimi_k3", "deepseek", "council_free", "council_pro"}.issubset(ANALYSIS_MODELS)

    store, board, actor, workspace, project, board_id = _owner(tmp_path)
    bridge = FakeAnalyticsBridge()
    try:
        _sticky(board, actor=actor, workspace=workspace, board_id=board_id)
        service = AnalyticsService(store, board, bridge=bridge)
        for index, model in enumerate(CODEX_ANALYSIS_LADDER):
            run = asyncio.run(
                service.start_single(
                    actor_id=actor,
                    workspace_id=workspace,
                    project_id=project,
                    board_id=board_id,
                    object_ids=["obj_analysis_a"],
                    command_id=f"analysis_ladder_{index:02d}",
                    model=model,
                    purpose="requirements",
                    question="Find unresolved requirements and questions.",
                )
            )
            assert run["status"] == "running"
            assert bridge.consult_calls[-1]["model"] == model

        assert [call["model"] for call in bridge.consult_calls] == list(CODEX_ANALYSIS_LADDER)
        assert bridge.council_calls == []
    finally:
        store.close()


def test_single_analysis_is_frozen_idempotent_and_provided_context_only(tmp_path: Path):
    store, board, actor, workspace, project, board_id = _owner(tmp_path)
    bridge = FakeAnalyticsBridge()
    try:
        created = _sticky(
            board, actor=actor, workspace=workspace, board_id=board_id
        )
        service = AnalyticsService(store, board, bridge=bridge)
        run = asyncio.run(
            service.start_single(
                actor_id=actor,
                workspace_id=workspace,
                project_id=project,
                board_id=board_id,
                object_ids=["obj_analysis_a"],
                command_id="analysis_cmd_001",
                model="kimi_k3",
                purpose="edge_cases",
                question="Review the selected risk only.",
            )
        )
        assert run["status"] == "running"
        assert len(bridge.consult_calls) == 1
        dispatched = bridge.consult_calls[0]
        assert dispatched["model"] == "kimi_k3"
        assert dispatched["request_key"] == f"analysis:{run['id']}"
        assert "READ /home/dev/.env" in dispatched["evidence_bundle"]
        assert '"revision":1' in dispatched["evidence_bundle"]
        assert set(dispatched) == {
            "model", "purpose", "question", "evidence_bundle", "request_key"
        }

        duplicate = asyncio.run(
            service.start_single(
                actor_id=actor,
                workspace_id=workspace,
                project_id=project,
                board_id=board_id,
                object_ids=["obj_analysis_a"],
                command_id="analysis_cmd_001",
                model="kimi_k3",
                purpose="edge_cases",
                question="Review the selected risk only.",
            )
        )
        assert duplicate["id"] == run["id"]
        assert len(bridge.consult_calls) == 1

        with pytest.raises(StoreError) as exc:
            asyncio.run(
                service.start_single(
                    actor_id=actor,
                    workspace_id=workspace,
                    project_id=project,
                    board_id=board_id,
                    object_ids=["obj_analysis_a"],
                    command_id="analysis_cmd_001",
                    model="kimi_k3",
                    purpose="edge_cases",
                    question="Different request.",
                )
            )
        assert exc.value.code == "ANALYSIS_COMMAND_CONFLICT"

        completed = asyncio.run(
            service.refresh(actor_id=actor, workspace_id=workspace, run_id=run["id"])
        )
        assert completed["status"] == "completed"
        assert completed["result_markdown"].startswith("# Findings")
        assert completed["source_changed"] is False

        board.apply_command(
            actor_id=actor,
            workspace_id=workspace,
            board_id=board_id,
            command_id="cmd_change_after_analysis",
            operation="update",
            object_id="obj_analysis_a",
            expected_object_revision=created["object_revision"],
            payload={"text": "Changed after the frozen analysis input"},
            execution_origin="mira",
        )
        changed = service.get_run(
            actor_id=actor, workspace_id=workspace, run_id=run["id"]
        )
        assert changed["source_changed"] is True
        assert changed["source_changed_count"] == 1
    finally:
        store.close()


def test_analysis_requires_explicit_capability_and_stays_actor_scoped(tmp_path: Path):
    store, board, actor, workspace, project, board_id = _owner(tmp_path)
    bridge = FakeAnalyticsBridge()
    try:
        _sticky(board, actor=actor, workspace=workspace, board_id=board_id)
        viewer = "usr_analysis_viewer"
        with store._lock:
            store.db.execute(
                "INSERT INTO actors(id,display_name,created_at_ms) VALUES(?,?,1)",
                (viewer, "Viewer"),
            )
            store.db.execute(
                "INSERT INTO memberships(actor_id,workspace_id,role) VALUES(?,?,?)",
                (viewer, workspace, "member"),
            )
            store.db.execute(
                """INSERT INTO project_grants(
                       actor_id,project_id,role,can_analyze,can_manage_share,created_at_ms,revoked_at_ms)
                   VALUES(?,?,?,0,0,1,NULL)""",
                (viewer, project, "viewer"),
            )
        service = AnalyticsService(store, board, bridge=bridge)
        with pytest.raises(StoreError) as exc:
            asyncio.run(
                service.start_single(
                    actor_id=viewer,
                    workspace_id=workspace,
                    project_id=project,
                    board_id=board_id,
                    object_ids=["obj_analysis_a"],
                    command_id="analysis_denied_01",
                    model="kimi_k3",
                    purpose="architecture",
                    question="Analyze.",
                )
            )
        assert exc.value.code == "PROJECT_FORBIDDEN"
        assert bridge.consult_calls == []

        store.grant_project_access(
            actor_id=actor,
            workspace_id=workspace,
            project_id=project,
            role="owner",
            can_analyze=True,
            can_manage_share=True,
        )
        with store._lock:
            store.db.execute(
                "UPDATE project_grants SET can_analyze=1 WHERE actor_id=? AND project_id=?",
                (viewer, project),
            )
        own = asyncio.run(
            service.start_single(
                actor_id=viewer,
                workspace_id=workspace,
                project_id=project,
                board_id=board_id,
                object_ids=["obj_analysis_a"],
                command_id="analysis_allowed_01",
                model="deepseek",
                purpose="architecture",
                question="Analyze only this note.",
            )
        )
        with pytest.raises(StoreError) as exc:
            service.get_run(actor_id=actor, workspace_id=workspace, run_id=own["id"])
        assert exc.value.code == "ANALYSIS_NOT_FOUND"
    finally:
        store.close()


def test_lost_dispatch_reuses_exact_frozen_evidence_and_request_key(tmp_path: Path):
    store, board, actor, workspace, project, board_id = _owner(tmp_path)
    bridge = LostDispatchBridge()
    try:
        _sticky(board, actor=actor, workspace=workspace, board_id=board_id)
        service = AnalyticsService(store, board, bridge=bridge)
        with pytest.raises(StoreError) as exc:
            asyncio.run(
                service.start_single(
                    actor_id=actor,
                    workspace_id=workspace,
                    project_id=project,
                    board_id=board_id,
                    object_ids=["obj_analysis_a"],
                    command_id="analysis_lost_dispatch_01",
                    model="kimi_k3",
                    purpose="edge_cases",
                    question="Review the selected evidence.",
                )
            )
        assert exc.value.code == "ANALYTICS_DISPATCH_UNKNOWN"
        runs = service.list_runs(
            actor_id=actor, workspace_id=workspace, project_id=project
        )
        assert len(runs) == 1
        assert runs[0]["status"] == "dispatch_unknown"
        first = bridge.consult_calls[0]

        refreshed = asyncio.run(
            service.refresh(
                actor_id=actor, workspace_id=workspace, run_id=runs[0]["id"]
            )
        )
        assert refreshed["status"] == "completed"
        assert len(bridge.consult_calls) == 2
        second = bridge.consult_calls[1]
        assert second["request_key"] == first["request_key"]
        assert second["evidence_bundle"] == first["evidence_bundle"]
    finally:
        store.close()


def test_cancelled_run_ignores_late_provider_result(tmp_path: Path):
    store, board, actor, workspace, project, board_id = _owner(tmp_path)
    bridge = FakeAnalyticsBridge()
    try:
        _sticky(board, actor=actor, workspace=workspace, board_id=board_id)
        service = AnalyticsService(store, board, bridge=bridge)
        run = asyncio.run(
            service.start_single(
                actor_id=actor,
                workspace_id=workspace,
                project_id=project,
                board_id=board_id,
                object_ids=["obj_analysis_a"],
                command_id="analysis_cancel_01",
                model="kimi_k3",
                purpose="requirements",
                question="Check requirements.",
            )
        )
        cancelled = asyncio.run(
            service.cancel(actor_id=actor, workspace_id=workspace, run_id=run["id"])
        )
        assert cancelled["status"] == "cancelled"
        assert bridge.cancelled == ["dvt_" + "1" * 32]
        late = asyncio.run(
            service.refresh(actor_id=actor, workspace_id=workspace, run_id=run["id"])
        )
        assert late["status"] == "cancelled"
        assert late["result_markdown"] == ""
    finally:
        store.close()


def test_completed_report_publishes_as_reference_card_not_copied_body(tmp_path: Path):
    store, board, actor, workspace, project, board_id = _owner(tmp_path)
    bridge = FakeAnalyticsBridge()
    try:
        _sticky(board, actor=actor, workspace=workspace, board_id=board_id)
        service = AnalyticsService(store, board, bridge=bridge)
        run = asyncio.run(
            service.start_single(
                actor_id=actor,
                workspace_id=workspace,
                project_id=project,
                board_id=board_id,
                object_ids=["obj_analysis_a"],
                command_id="analysis_publish_01",
                model="kimi_k3",
                purpose="ideas",
                question="Find alternatives.",
            )
        )
        run = asyncio.run(
            service.refresh(actor_id=actor, workspace_id=workspace, run_id=run["id"])
        )
        receipt = service.publish_to_board(
            actor_id=actor,
            workspace_id=workspace,
            run_id=run["id"],
            command_id="cmd_publish_analysis_01",
            object_id="obj_analysis_report",
        )
        assert receipt["event"]["after"]["type"] == "document_card"
        assert receipt["event"]["after"]["reference"] == {"kind": "analysis_run", "id": run["id"]}
        assert "Run the smallest test" not in receipt["event"]["after"]["text"]
    finally:
        store.close()


def test_free_council_uses_frozen_evidence_and_preserves_attribution(tmp_path: Path):
    store, board, actor, workspace, project, board_id = _owner(tmp_path)
    bridge = FakeAnalyticsBridge()
    try:
        _sticky(
            board,
            actor=actor,
            workspace=workspace,
            board_id=board_id,
            object_id="obj_council_a",
            text="Decision: keep WebGL renderer; risk is stale multi-tab focus.",
        )
        service = AnalyticsService(store, board, bridge=bridge)
        run = asyncio.run(
            service.start_single(
                actor_id=actor,
                workspace_id=workspace,
                project_id=project,
                board_id=board_id,
                object_ids=["obj_council_a"],
                command_id="analysis_council_001",
                model="council_free",
                purpose="architecture",
                question="Stress-test this decision and name unresolved risks.",
            )
        )
        assert run["status"] == "running"
        assert run["model"] == "council_free"
        assert bridge.consult_calls == []
        assert len(bridge.council_calls) == 1
        dispatched = bridge.council_calls[0]
        assert dispatched["request_key"] == f"analysis:{run['id']}"
        assert dispatched["prompt"].startswith("Purpose: architecture")
        assert "stale multi-tab focus" in dispatched["evidence_bundle"]
        assert set(dispatched) == {"prompt", "evidence_bundle", "request_key", "tier"}
        assert dispatched["tier"] == "free"

        duplicate = asyncio.run(
            service.start_single(
                actor_id=actor,
                workspace_id=workspace,
                project_id=project,
                board_id=board_id,
                object_ids=["obj_council_a"],
                command_id="analysis_council_001",
                model="council_free",
                purpose="architecture",
                question="Stress-test this decision and name unresolved risks.",
            )
        )
        assert duplicate["id"] == run["id"]
        assert len(bridge.council_calls) == 1

        bridge.read_payload = {
            "status": "completed",
            "executionStatus": "completed",
            "result": {
                "schema": "devcoveer-council-v1",
                "model_outputs": {
                    "opencode/model-a": {
                        "status": "completed",
                        "participationMode": "full_debate",
                        "inferenceCallsUsed": 3,
                        "initial_position": "Keep the renderer, but isolate client identity.",
                        "cross_critique": "The other view underweights reconnect races.",
                        "revision": {
                            "revised_position": "Keep WebGL and make focus per browsing context.",
                            "agreements": ["WebGL is appropriate."],
                            "disagreements": ["Reconnect handling needs stronger proof."],
                            "novel_findings": ["Guest view can reuse rendering without edit controls."],
                            "unresolved_questions": ["What is the maximum board object count?"],
                            "key_contributions": ["Separate visual and synchronization concerns."],
                        },
                    }
                },
                "novel_findings": ["Guest projection must stay read-only."],
                "unresolved_questions": ["Measure 2-client convergence under reconnect."],
                "usage": {"freeCalls": 3, "nvidiaCalls": 0, "totalCalls": 3},
            },
        }
        completed = asyncio.run(
            service.refresh(
                actor_id=actor,
                workspace_id=workspace,
                run_id=run["id"],
            )
        )
        assert completed["status"] == "completed"
        assert completed["result"]["schema"] == "devcoveer-council-v1"
        assert "# Multi-model council" in completed["result_markdown"]
        assert "### opencode/model-a" in completed["result_markdown"]
        assert "#### Cross-critique" in completed["result_markdown"]
        assert "Keep WebGL and make focus per browsing context." in completed["result_markdown"]
        assert "Measure 2-client convergence under reconnect." in completed["result_markdown"]
        assert '"nvidiaCalls":0' in completed["result_markdown"]
    finally:
        store.close()



def test_pro_council_auto_dispatches_without_user_confirmation(tmp_path: Path):
    store, board, actor, workspace, project, board_id = _owner(tmp_path)
    bridge = FakeAnalyticsBridge()
    try:
        _sticky(
            board,
            actor=actor,
            workspace=workspace,
            board_id=board_id,
            object_id="obj_nvidia_council",
            text="Synthetic NVIDIA council evidence.",
        )
        service = AnalyticsService(store, board, bridge=bridge)
        run = asyncio.run(
            service.start_single(
                actor_id=actor,
                workspace_id=workspace,
                project_id=project,
                board_id=board_id,
                object_ids=["obj_nvidia_council"],
                command_id="analysis_nvidia_council_001",
                model="council_pro",
                purpose="architecture",
                question="Compare two risks.",
            )
        )
        assert run["status"] == "running"
        assert run["model"] == "council_pro"
        assert len(bridge.council_calls) == 1
        dispatched = bridge.council_calls[0]
        assert dispatched["tier"] == "pro"
        assert set(dispatched) == {"prompt", "evidence_bundle", "request_key", "tier"}
        assert dispatched["request_key"] == f"analysis:{run['id']}"

        duplicate = asyncio.run(
            service.start_single(
                actor_id=actor,
                workspace_id=workspace,
                project_id=project,
                board_id=board_id,
                object_ids=["obj_nvidia_council"],
                command_id="analysis_nvidia_council_001",
                model="council_pro",
                purpose="architecture",
                question="Compare two risks.",
            )
        )
        assert duplicate["id"] == run["id"]
        assert len(bridge.council_calls) == 1
    finally:
        store.close()

