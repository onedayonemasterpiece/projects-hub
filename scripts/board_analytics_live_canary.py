#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path

from projects_hub.analytics import AnalyticsService
from projects_hub.analytics_client import AnalyticsBridgeClient
from projects_hub.board import BoardService
from projects_hub.store import DurableStore


async def read_existing_task(task_id: str) -> dict:
    bridge = AnalyticsBridgeClient()
    try:
        payload = await bridge.read_task(task_id)
        return {
            "task_id": task_id,
            "payload": payload,
        }
    finally:
        await bridge.close()


async def run(model: str = "council_free") -> dict:
    with tempfile.TemporaryDirectory(prefix="projects-hub-council-canary-") as tmp:
        store = DurableStore(Path(tmp))
        bridge = AnalyticsBridgeClient()
        try:
            boot = store.ensure_dev_workspace("Board analytics live canary")
            actor = boot["actor"]["id"]
            workspace = boot["workspace"]["id"]
            project = boot["projects"][0]["id"]
            board = BoardService(store)
            board_id = board.open_board(actor, workspace, project)["id"]
            board.apply_command(
                actor_id=actor,
                workspace_id=workspace,
                board_id=board_id,
                command_id="cmd_council_canary_sticky",
                operation="create",
                object_id="obj_council_canary",
                expected_object_revision=None,
                payload={
                    "type": "sticky",
                    "text": (
                        "Synthetic acceptance evidence only. Decision: use one WebGL board. "
                        "Risk: two browser tabs must not share a focus client ID."
                    ),
                    "style": {"color": "blue"},
                },
            )

            if not await bridge.safe_council_available():
                raise RuntimeError(
                    "Installed DevCoveer does not expose provided-only council isolation"
                )

            service = AnalyticsService(store, board, bridge=bridge)
            run_row = await service.start_single(
                actor_id=actor,
                workspace_id=workspace,
                project_id=project,
                board_id=board_id,
                object_ids=["obj_council_canary"],
                command_id="analysis_council_live_canary",
                model=model,
                purpose="edge_cases",
                question=(
                    "Using only the supplied synthetic sticky, identify one concrete failure "
                    "mode and one verification step. Keep the answer concise."
                ),
            )

            deadline = time.monotonic() + 180.0
            while run_row["status"] not in {"completed", "failed", "cancelled", "blocked"}:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Council canary timed out in status {run_row['status']}"
                    )
                await asyncio.sleep(2.0)
                run_row = await service.refresh(
                    actor_id=actor,
                    workspace_id=workspace,
                    run_id=run_row["id"],
                )

            if run_row["status"] != "completed":
                raise RuntimeError(
                    f"Council canary ended in {run_row['status']}: {run_row['error_code']}"
                )
            if "# Multi-model council" not in run_row["result_markdown"]:
                raise RuntimeError("Council canary produced no attributed Markdown report")
            if not isinstance(run_row.get("result"), dict):
                raise RuntimeError("Council canary produced no structured result")

            usage = run_row["result"].get("usage")
            if isinstance(usage, dict):
                actual = usage.get("actual")
                if (
                    model == "council_free"
                    and isinstance(actual, dict)
                    and int(actual.get("nvidiaCalls") or 0) != 0
                ):
                    raise RuntimeError("Free council unexpectedly used NVIDIA")
                if (
                    model == "council_pro"
                    and isinstance(actual, dict)
                    and int(actual.get("nvidiaCalls") or 0) < 2
                ):
                    raise RuntimeError("NVIDIA council did not exercise both product participants")

            return {
                "status": run_row["status"],
                "run_id": run_row["id"],
                "provider_task_id": run_row["provider_task_id"],
                "model": run_row["model"],
                "markdown_chars": len(run_row["result_markdown"]),
                "source_changed": run_row["source_changed"],
                "usage": usage,
            }
        finally:
            await bridge.close()
            store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--read-task")
    parser.add_argument(
        "--model",
        choices=("council_free", "council_pro"),
        default="council_free",
    )
    args = parser.parse_args()
    result = (
        asyncio.run(read_existing_task(args.read_task))
        if args.read_task
        else asyncio.run(run(args.model))
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
