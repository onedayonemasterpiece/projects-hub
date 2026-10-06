from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field

from .board_view_context import BoardViewContextStore


class CameraPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float
    y: float
    zoom: float
    width: float
    height: float


class BoardViewContextPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    project_id: str
    board_id: str
    client_instance_id: str = Field(min_length=8, max_length=128)
    board_seq: int = Field(ge=0)
    camera: CameraPayload
    visible_object_ids: list[str] = Field(default_factory=list, max_length=40)
    selected_object_ids: list[str] = Field(default_factory=list, max_length=4)
    focused_object_id: str | None = Field(default=None, max_length=128)


def attach_board_view_context_routes(
    app: FastAPI,
    *,
    service: BoardViewContextStore,
    actor_id_from_request: Callable[[Request], str],
) -> None:
    @app.put("/api/conversations/{conversation_id}/board-view-context")
    async def board_view_context(
        conversation_id: str,
        payload: BoardViewContextPayload,
        request: Request,
    ) -> dict[str, Any]:
        return service.update(
            actor_id=actor_id_from_request(request),
            workspace_id=payload.workspace_id,
            conversation_id=conversation_id,
            project_id=payload.project_id,
            board_id=payload.board_id,
            client_instance_id=payload.client_instance_id,
            board_seq=payload.board_seq,
            camera=payload.camera.model_dump(),
            visible_object_ids=payload.visible_object_ids,
            selected_object_ids=payload.selected_object_ids,
            focused_object_id=payload.focused_object_id,
        )
