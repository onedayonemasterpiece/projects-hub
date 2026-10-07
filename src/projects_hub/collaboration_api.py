from __future__ import annotations

from typing import Any, Callable, Literal

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from .collaboration import CollaborationService


class NoteCreate(BaseModel):
    workspace_id: str
    project_id: str
    command_id: str = Field(min_length=8, max_length=128)
    title: str = Field(min_length=1, max_length=240)
    body: str = Field(min_length=1, max_length=60_000)


class ReplyCreate(BaseModel):
    workspace_id: str
    command_id: str = Field(min_length=8, max_length=128)
    body: str = Field(min_length=1, max_length=12_000)


class BriefSeen(BaseModel):
    workspace_id: str
    personal_through_id: int | None = Field(default=None, ge=0)
    general_through_id: int | None = Field(default=None, ge=0)


class GeneralNewsPreference(BaseModel):
    workspace_id: str
    enabled: bool


class ChatGPTSyncRequest(BaseModel):
    workspace_id: str


class ParticipantInvite(BaseModel):
    workspace_id: str
    display_name: str = Field(min_length=1, max_length=80)
    role: Literal["viewer", "editor"] = "editor"
    ttl_seconds: int = Field(default=24 * 60 * 60, ge=60, le=7 * 24 * 60 * 60)


def attach_collaboration_routes(
    app: FastAPI,
    *,
    service: CollaborationService,
    actor_id_from_request: Callable[[Request], str],
) -> None:
    @app.post("/api/collaboration/notes")
    async def create_note(payload: NoteCreate, request: Request) -> dict[str, Any]:
        return await service.create_note(
            actor_id=actor_id_from_request(request),
            workspace_id=payload.workspace_id,
            project_id=payload.project_id,
            command_id=payload.command_id,
            title=payload.title,
            body=payload.body,
        )

    @app.get("/api/collaboration/notes")
    async def list_notes(
        workspace_id: str,
        project_id: str,
        request: Request,
        limit: int = 30,
    ) -> dict[str, Any]:
        return {
            "items": service.list_notes(
                actor_id=actor_id_from_request(request),
                workspace_id=workspace_id,
                project_id=project_id,
                limit=limit,
            )
        }

    @app.get("/api/collaboration/notes/{note_id}")
    async def get_note(note_id: str, workspace_id: str, request: Request) -> dict[str, Any]:
        return service.get_note(
            actor_id=actor_id_from_request(request),
            workspace_id=workspace_id,
            note_id=note_id,
        )

    @app.get("/api/collaboration/notes/{note_id}/readback")
    async def note_readback(
        note_id: str, workspace_id: str, request: Request
    ) -> dict[str, Any]:
        return await service.repository_readback(
            actor_id=actor_id_from_request(request),
            workspace_id=workspace_id,
            note_id=note_id,
        )

    @app.get("/api/collaboration/notes/{note_id}/replies")
    async def list_replies(
        note_id: str, workspace_id: str, request: Request
    ) -> dict[str, Any]:
        return {
            "items": service.list_replies(
                actor_id=actor_id_from_request(request),
                workspace_id=workspace_id,
                note_id=note_id,
            )
        }

    @app.post("/api/collaboration/notes/{note_id}/replies")
    async def reply(
        note_id: str, payload: ReplyCreate, request: Request
    ) -> dict[str, Any]:
        return service.reply(
            actor_id=actor_id_from_request(request),
            workspace_id=payload.workspace_id,
            note_id=note_id,
            command_id=payload.command_id,
            body=payload.body,
        )

    @app.get("/api/collaboration/timeline")
    async def timeline(
        workspace_id: str,
        request: Request,
        after_id: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        return {
            "items": service.timeline(
                actor_id=actor_id_from_request(request),
                workspace_id=workspace_id,
                after_id=after_id,
                limit=limit,
            )
        }

    @app.get("/api/collaboration/brief")
    async def personal_brief(workspace_id: str, request: Request) -> dict[str, Any]:
        return service.personal_brief(
            actor_id=actor_id_from_request(request),
            workspace_id=workspace_id,
        )

    @app.post("/api/collaboration/brief/seen")
    async def brief_seen(payload: BriefSeen, request: Request) -> dict[str, Any]:
        return service.mark_brief_seen(
            actor_id=actor_id_from_request(request),
            workspace_id=payload.workspace_id,
            personal_through_id=payload.personal_through_id,
            general_through_id=payload.general_through_id,
        )

    @app.post("/api/collaboration/preferences/general-news")
    async def general_news_preference(
        payload: GeneralNewsPreference,
        request: Request,
    ) -> dict[str, Any]:
        return service.set_general_news(
            actor_id=actor_id_from_request(request),
            workspace_id=payload.workspace_id,
            enabled=payload.enabled,
        )

    @app.post("/api/collaboration/chatgpt/sync")
    async def sync_chatgpt_analysis(
        payload: ChatGPTSyncRequest,
        request: Request,
    ) -> dict[str, Any]:
        # Maintenance/acceptance control. Ordinary project participants only
        # read published analyses; they cannot enumerate configured routes.
        actor = actor_id_from_request(request)
        service.store.require_workspace_owner(actor, payload.workspace_id)
        imported = await service.chatgpt_analysis.poll_once(force=True)
        return {"imported": imported, "status": "checked"}

    @app.post("/api/projects/{project_id}/participants/invite")
    async def invite_participant(
        project_id: str, payload: ParticipantInvite, request: Request
    ) -> dict[str, Any]:
        return service.invite_participant(
            owner_actor_id=actor_id_from_request(request),
            workspace_id=payload.workspace_id,
            project_id=project_id,
            display_name=payload.display_name,
            role=payload.role,
            ttl_seconds=payload.ttl_seconds,
        )
