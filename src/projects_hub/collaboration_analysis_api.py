from __future__ import annotations

from typing import Any, Callable, Literal

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from .collaboration_analysis import CollaborationAnalysisService


class AnalysisStart(BaseModel):
    workspace_id: str
    project_id: str
    note_id: str
    addressed_to_actor_id: str
    command_id: str = Field(min_length=8, max_length=128)
    model: Literal["kimi_k3", "deepseek"] = "kimi_k3"
    purpose: Literal["requirements", "edge_cases", "architecture", "code_review", "ideas"] = "requirements"
    question: str = Field(min_length=1, max_length=4000)


class QuestionResponse(BaseModel):
    question_id: str
    disposition: Literal["answer", "skip", "unknown", "later"]
    body: str = Field(default="", max_length=12000)
    deferred_until_ms: int | None = None


class QuestionAnswers(BaseModel):
    workspace_id: str
    command_id: str = Field(min_length=8, max_length=128)
    responses: list[QuestionResponse] = Field(min_length=1, max_length=6)


def attach_collaboration_analysis_routes(
    app: FastAPI,
    *,
    service: CollaborationAnalysisService,
    actor_id_from_request: Callable[[Request], str],
) -> None:
    @app.post("/api/collaboration/analyses")
    async def start_analysis(payload: AnalysisStart, request: Request) -> dict[str, Any]:
        return await service.start_analysis(
            actor_id=actor_id_from_request(request),
            workspace_id=payload.workspace_id,
            project_id=payload.project_id,
            note_id=payload.note_id,
            addressed_to_actor_id=payload.addressed_to_actor_id,
            command_id=payload.command_id,
            model=payload.model,
            purpose=payload.purpose,
            question=payload.question,
        )

    @app.get("/api/collaboration/analyses/{analysis_id}")
    async def analysis_status(
        analysis_id: str,
        workspace_id: str,
        request: Request,
    ) -> dict[str, Any]:
        # Read only: it never advances provider or continuation state.
        return service.get_analysis(
            actor_id=actor_id_from_request(request),
            workspace_id=workspace_id,
            analysis_id=analysis_id,
        )

    @app.post("/api/collaboration/analyses/{analysis_id}/refresh")
    async def analysis_refresh(
        analysis_id: str,
        workspace_id: str,
        request: Request,
    ) -> dict[str, Any]:
        return await service.refresh_analysis(
            actor_id=actor_id_from_request(request),
            workspace_id=workspace_id,
            analysis_id=analysis_id,
        )

    @app.get("/api/collaboration/questions/inbox")
    async def question_inbox(
        workspace_id: str,
        request: Request,
        limit: int = 30,
    ) -> dict[str, Any]:
        return {
            "items": service.inbox(
                actor_id=actor_id_from_request(request),
                workspace_id=workspace_id,
                limit=limit,
            )
        }

    @app.post("/api/collaboration/analyses/{analysis_id}/answers")
    async def answer_questions(
        analysis_id: str,
        payload: QuestionAnswers,
        request: Request,
    ) -> dict[str, Any]:
        return service.answer_questions(
            actor_id=actor_id_from_request(request),
            workspace_id=payload.workspace_id,
            analysis_id=analysis_id,
            command_id=payload.command_id,
            responses=[item.model_dump() for item in payload.responses],
        )

    @app.get("/api/collaboration/jobs/{job_id}")
    async def collaboration_job_status(
        job_id: str,
        workspace_id: str,
        request: Request,
    ) -> dict[str, Any]:
        # Read only: durable worker owns transitions.
        return service.job_status(
            actor_id=actor_id_from_request(request),
            workspace_id=workspace_id,
            job_id=job_id,
        )
