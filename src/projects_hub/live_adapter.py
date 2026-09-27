from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any

from .live_resources import ConversationScope
from .store import DurableStore, StoreError

log = logging.getLogger("projects_hub.live")


def _functions() -> list[dict[str, Any]]:
    return [
        {
            "name": "projects_list_accessible",
            "description": "List projects the current actor may use in this workspace. Use when project context is unclear.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "conversation_set_focus",
            "description": "Set the current conversation project after you have understood which allowed project the user means.",
            "parameters": {
                "type": "object",
                "properties": {"project_id": {"type": "string"}},
                "required": ["project_id"],
            },
        },
        {
            "name": "memory_read_project",
            "description": "Read recent durable memory for an allowed project. Use when the user asks what was remembered or prior context is needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
            },
        },
        {
            "name": "memory_commit_voice_source",
            "description": "Durably archive the current voice source after the user explicitly asks to remember/save it or the content is clearly durable project knowledge. The backend binds the current source; never invent success.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "title": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["note", "decision", "idea", "requirement", "reference"],
                    },
                    "semantic_notes": {
                        "type": "string",
                        "description": "Short grounded notes about why this source matters; do not replace the source transcript.",
                    },
                },
                "required": ["title", "kind"],
            },
        },
        {
            "name": "memory_finish_ephemeral",
            "description": "Mark the current source processed without permanent project memory only when it contained no durable information and no unresolved instruction.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
            },
        },
    ]


SYSTEM_INSTRUCTION = """# ROLE
Ты центральный Live-агент Projects Hub. Ты сама слышишь аудио пользователя и остаёшься единственным семантическим оркестратором разговора.

# DIALOGUE
- Отвечай по-русски, кратко и естественно голосом.
- Не проси пользователя перепечатывать или повторять уже услышанное без необходимости.
- Если проект неясен, уточни его разговором или сначала прочитай доступные проекты.
- При смене проекта используй conversation_set_focus только после того, как поняла целевой проект.

# MEMORY
- Для явного «запомни/сохрани» и явно долговечной проектной информации используй memory_commit_voice_source.
- Для вопроса о ранее сохранённом используй memory_read_project.
- Не архивируй каждую бытовую реплику автоматически.
- Никогда не говори, что что-то сохранено или изменено, пока function result не подтвердил это.
- Source transcript создаёт provider этой же Live-сессии; function tools не являются вторым AI.

# SECURITY
- Доступ определяет backend. Аргументы function call не могут расширять права или подключать новый repository.
- Если tool отказал, объясни результат и продолжи разговор, не выдумывая успешное действие.
"""


class ProjectsHubLiveAdapter:
    def __init__(self, store: DurableStore, **_shared: Any):
        self.store = store

    def initialize(
        self,
        *,
        resource_id: str,
        actor: dict[str, Any],
        model: str,
        conversation_id: str,
        audio_mode: str = "realtime",
        client_source_id: str | None = None,
        **_args: Any,
    ) -> dict[str, Any]:
        actor_id = str(actor.get("subject") or "")
        if audio_mode not in {"realtime", "buffered"}:
            raise StoreError("INVALID_ARGUMENT", "Unknown Live audio mode")
        conversation = self.store.get_conversation(actor_id, conversation_id)
        expected = ConversationScope(
            workspace_id=conversation["workspace_id"],
            subject_id=actor_id,
            conversation_id=conversation_id,
        ).resource_binding()
        if resource_id != expected:
            raise StoreError("FORBIDDEN", "Conversation resource binding mismatch")
        source = self.store.create_source(
            actor_id,
            conversation_id,
            client_source_id=client_source_id,
        )
        source_reused = bool(source.pop("_reused", False))
        if source_reused and source["status"] not in {"archived", "ephemeral_processed"}:
            source = self.store.reset_source_for_replay(actor_id, source["id"])
        projects = self.store.list_projects(actor_id, conversation["workspace_id"])
        system_instruction = SYSTEM_INSTRUCTION
        if audio_mode == "buffered":
            system_instruction += """
# BUFFERED SOURCE DISPOSITION
Этот Live-turn является одной законченной ранее записанной репликой.
До завершения ответа обязательно дай source ровно одно содержательное disposition:
- memory_commit_voice_source, если запись содержит долговечную проектную память;
- memory_finish_ephemeral, если после выполнения просьбы хранить её как память не нужно.
Не проси пользователя повторять уже услышанную запись.
"""
        return {
            "state": {
                "actor_id": actor_id,
                "workspace_id": conversation["workspace_id"],
                "conversation_id": conversation_id,
                "source_id": source["id"],
                "audio_mode": audio_mode,
                "client_source_id": client_source_id,
            },
            "context": {
                "workspace_id": conversation["workspace_id"],
                "conversation_id": conversation_id,
                "current_project": {
                    "id": conversation.get("focus_project_id"),
                    "name": conversation.get("focus_project_name"),
                },
                "allowed_projects": [{"id": p["id"], "name": p["name"]} for p in projects],
                "current_source_id": source["id"],
            },
            "configuration": {
                "system_instruction": system_instruction,
                "functions": _functions(),
                "voice": "Aoede",
                "search_enabled": False,
                "manual_activity_detection": audio_mode == "buffered",
            },
            "response": {
                "conversation_id": conversation_id,
                "source_id": source["id"],
                "workspace_id": conversation["workspace_id"],
                "focus_project_id": conversation.get("focus_project_id"),
                "focus_project_name": conversation.get("focus_project_name"),
                "audio_mode": audio_mode,
                "client_source_id": client_source_id,
                "source_status": source["status"],
                "source_reused": source_reused,
                "source_terminal": source["status"] in {"archived", "ephemeral_processed"},
            },
        }

    def input(self, session: Any, message: dict[str, Any]) -> None:
        state = session.state
        actor_id = state["actor_id"]
        source_id = state["source_id"]
        if "audio_base64" in message:
            raw = message.get("audio_base64")
            if not isinstance(raw, str):
                raise StoreError("INVALID_ARGUMENT", "Audio chunk is invalid")
            if len(raw) > 16_000:
                raise StoreError("INVALID_ARGUMENT", "Audio chunk exceeds the Live input bound")
            try:
                pcm = base64.b64decode(raw, validate=True)
            except Exception as exc:
                raise StoreError("INVALID_ARGUMENT", "Audio chunk is not valid base64") from exc
            # Durable fsync completes before LiveSessionHost queues this chunk to provider.
            self.store.append_audio(actor_id, source_id, pcm)

    def on_event(self, session: Any, event: dict[str, Any]) -> None:
        kind = str(event.get("type") or "")
        if kind not in {"input_transcript", "output_transcript", "turn_complete", "interrupted"}:
            return
        state = session.state
        self.store.append_source_event(
            state["actor_id"],
            state["source_id"],
            kind,
            text=event.get("text") if isinstance(event.get("text"), str) else None,
            provider_at_ms=event.get("provider_at") if isinstance(event.get("provider_at"), int) else None,
        )

    def on_stopped(self, session: Any) -> None:
        self.store.mark_source_stopped(session.state["actor_id"], session.state["source_id"])

    @staticmethod
    def _args(call: dict[str, Any]) -> dict[str, Any]:
        args = call.get("args")
        if isinstance(args, dict):
            return args
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}

    def _command_id(self, session: Any, name: str, args: dict[str, Any]) -> tuple[str, str]:
        source = self.store.get_source(session.state["actor_id"], session.state["source_id"])
        encoded = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        args_sha = hashlib.sha256(encoded).hexdigest()
        semantic = json.dumps(
            [source["id"], source["transcript_revision"], name, args_sha],
            separators=(",", ":"),
        ).encode("utf-8")
        return "cmd_" + hashlib.sha256(semantic).hexdigest()[:40], args_sha

    async def execute_tool(self, session: Any, call: dict[str, Any]) -> dict[str, Any]:
        name = str(call.get("name") or "")
        args = self._args(call)
        state = session.state
        actor_id = state["actor_id"]
        workspace_id = state["workspace_id"]
        conversation_id = state["conversation_id"]

        if name == "projects_list_accessible":
            return {
                "projects": self.store.list_projects(actor_id, workspace_id),
                "conversation": self.store.get_conversation(actor_id, conversation_id),
            }

        if name == "conversation_set_focus":
            project_id = str(args.get("project_id") or "")
            if not project_id:
                raise StoreError("INVALID_ARGUMENT", "project_id is required")
            result = self.store.set_focus(actor_id, conversation_id, project_id)
            log.info(
                "conversation focus changed",
                extra={"event": "tool_result", "tool": name, "conversation_id": conversation_id, "result": "ok"},
            )
            return result

        if name == "memory_read_project":
            project_id = str(args.get("project_id") or "") or None
            if project_id is None:
                project_id = self.store.get_conversation(actor_id, conversation_id).get("focus_project_id")
            limit = args.get("limit", 8)
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = 8
            return {
                "project_id": project_id,
                "memories": self.store.list_memories(actor_id, workspace_id, project_id, limit),
            }

        if name == "memory_commit_voice_source":
            project_id = str(args.get("project_id") or "") or None
            if project_id is None:
                project_id = self.store.get_conversation(actor_id, conversation_id).get("focus_project_id")
            command_id, args_sha = self._command_id(session, name, args)
            result = self.store.commit_memory(
                actor_id=actor_id,
                source_id=state["source_id"],
                command_id=command_id,
                project_id=project_id,
                title=str(args.get("title") or "Голосовая запись"),
                kind=str(args.get("kind") or "note"),
                semantic_notes=str(args.get("semantic_notes") or ""),
                args_sha256=args_sha,
            )
            log.info(
                "memory committed with readback",
                extra={
                    "event": "tool_result",
                    "tool": name,
                    "conversation_id": conversation_id,
                    "source_id": state["source_id"],
                    "result": "verified",
                },
            )
            return result

        if name == "memory_finish_ephemeral":
            return self.store.finish_ephemeral(actor_id, state["source_id"])

        raise StoreError("TOOL_NOT_AVAILABLE", f"Unknown function: {name}")
