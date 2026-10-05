from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel, Field

from .analytics import AnalyticsService
from .analytics_materialization import AnalysisMaterializer


class AnalysisStartRequest(BaseModel):
    workspace_id: str
    project_id: str
    board_id: str
    object_ids: list[str] = Field(min_length=1, max_length=12)
    command_id: str
    model: str = "kimi_k3"
    purpose: str
    question: str = Field(min_length=1, max_length=4000)


class AnalysisWorkspaceRequest(BaseModel):
    workspace_id: str


class AnalysisMaterializeRequest(BaseModel):
    workspace_id: str
    repository_id: int | None = None
    allow_public: bool = False


class AnalysisPublishRequest(BaseModel):
    workspace_id: str
    command_id: str
    object_id: str
    geometry: dict[str, Any] | None = None


def attach_analytics_routes(
    app: FastAPI,
    *,
    service: AnalyticsService,
    actor_id_from_request: Callable[[Request], str],
    board_hub: Any | None = None,
    materializer: AnalysisMaterializer | None = None,
) -> None:
    @app.post("/api/analysis/runs")
    async def analysis_start(
        payload: AnalysisStartRequest, request: Request
    ) -> dict[str, Any]:
        return await service.start_single(
            actor_id=actor_id_from_request(request),
            workspace_id=payload.workspace_id,
            project_id=payload.project_id,
            board_id=payload.board_id,
            object_ids=payload.object_ids,
            command_id=payload.command_id,
            model=payload.model,
            purpose=payload.purpose,
            question=payload.question,
        )

    @app.get("/api/analysis/runs")
    async def analysis_list(
        workspace_id: str,
        project_id: str,
        request: Request,
        limit: int = 20,
    ) -> dict[str, Any]:
        return {
            "items": service.list_runs(
                actor_id=actor_id_from_request(request),
                workspace_id=workspace_id,
                project_id=project_id,
                limit=limit,
            )
        }

    @app.get("/api/analysis/runs/{run_id}")
    async def analysis_get(
        run_id: str, workspace_id: str, request: Request
    ) -> dict[str, Any]:
        return service.get_run(
            actor_id=actor_id_from_request(request),
            workspace_id=workspace_id,
            run_id=run_id,
        )

    @app.post("/api/analysis/runs/{run_id}/refresh")
    async def analysis_refresh(
        run_id: str, payload: AnalysisWorkspaceRequest, request: Request
    ) -> dict[str, Any]:
        return await service.refresh(
            actor_id=actor_id_from_request(request),
            workspace_id=payload.workspace_id,
            run_id=run_id,
        )

    @app.post("/api/analysis/runs/{run_id}/confirm")
    async def analysis_confirm(
        run_id: str, payload: AnalysisWorkspaceRequest, request: Request
    ) -> dict[str, Any]:
        return await service.confirm_paid(
            actor_id=actor_id_from_request(request),
            workspace_id=payload.workspace_id,
            run_id=run_id,
        )

    @app.post("/api/analysis/runs/{run_id}/cancel")
    async def analysis_cancel(
        run_id: str, payload: AnalysisWorkspaceRequest, request: Request
    ) -> dict[str, Any]:
        return await service.cancel(
            actor_id=actor_id_from_request(request),
            workspace_id=payload.workspace_id,
            run_id=run_id,
        )

    @app.post("/api/analysis/runs/{run_id}/publish")
    async def analysis_publish(
        run_id: str, payload: AnalysisPublishRequest, request: Request
    ) -> dict[str, Any]:
        receipt = service.publish_to_board(
            actor_id=actor_id_from_request(request),
            workspace_id=payload.workspace_id,
            run_id=run_id,
            command_id=payload.command_id,
            object_id=payload.object_id,
            geometry=payload.geometry,
        )
        if board_hub is not None:
            await board_hub.publish(
                receipt["board_id"],
                {"type": "event", "event": receipt["event"]},
            )
        return receipt

    @app.get("/api/analysis/runs/{run_id}/materialization")
    async def analysis_materialization_status(
        run_id: str,
        workspace_id: str,
        request: Request,
    ) -> dict[str, Any]:
        if materializer is None:
            return {
                "run_id": run_id,
                "status": "not_configured",
            }
        return materializer.status(
            actor_id=actor_id_from_request(request),
            workspace_id=workspace_id,
            run_id=run_id,
        )

    @app.post("/api/analysis/runs/{run_id}/materialize")
    async def analysis_materialize(
        run_id: str,
        payload: AnalysisMaterializeRequest,
        request: Request,
    ) -> dict[str, Any]:
        if materializer is None:
            return {
                "run_id": run_id,
                "status": "not_configured",
            }
        return await materializer.materialize(
            actor_id=actor_id_from_request(request),
            workspace_id=payload.workspace_id,
            run_id=run_id,
            repository_id=payload.repository_id,
            allow_public=payload.allow_public,
        )

    @app.get("/api/analysis/runs/{run_id}/report.md")
    async def analysis_report(
        run_id: str, workspace_id: str, request: Request
    ) -> Response:
        run = service.get_run(
            actor_id=actor_id_from_request(request),
            workspace_id=workspace_id,
            run_id=run_id,
        )
        markdown = str(run.get("result_markdown") or "")
        if run["status"] != "completed" or not markdown:
            from .store import StoreError
            raise StoreError("ANALYSIS_NOT_READY", "Analysis report is not ready")
        return Response(
            markdown,
            media_type="text/markdown; charset=utf-8",
            headers={
                "content-disposition": f'attachment; filename="{run_id}.md"',
                "cache-control": "no-store",
                "content-security-policy": "default-src 'none'; sandbox",
                "referrer-policy": "no-referrer",
                "x-content-type-options": "nosniff",
            },
        )
