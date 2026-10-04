from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any, Callable

from .device_commands import DeviceCommandService
from .development import DevelopmentService
from .github_connections import GitHubConnections
from .expert_reviews import (
    ExpertReviewAccessError,
    ExpertReviewAdapter,
    ExpertReviewConflict,
    ExpertReviewError,
)
from .regional_knowledge import (
    RegionalKnowledgeAccessError,
    RegionalKnowledgeAdapter,
    RegionalKnowledgeContractError,
    RegionalKnowledgeInputError,
    RegionalKnowledgeUnavailable,
)
from .live_resources import ConversationScope
from .readiness import ReadinessService
from .store import DurableStore, StoreError

log = logging.getLogger("projects_hub.live")


def _functions(
    *,
    expert_reviews: bool = False,
    regional_knowledge: bool = False,
    owner_development: bool = False,
) -> list[dict[str, Any]]:
    functions = [
        {
            "name": "projects_list_accessible",
            "description": "List projects the current actor may use in this workspace. Use when project context is unclear.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "github_repositories_list",
            "description": "List repositories already connected and explicitly bound inside this workspace. This is read-only catalogue access and cannot grant or increase GitHub permissions.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "github_repository_read",
            "description": "Read the root/directory listing or one UTF-8 text file from a GitHub repository already connected and bound to this workspace. This is read-only and cannot grant access or write to GitHub.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repository_id": {"type": "integer", "minimum": 1},
                    "path": {
                        "type": "string",
                        "description": "Repository-relative path. Empty string reads the repository root."
                    }
                },
                "required": ["repository_id"]
            },
        },
        {
            "name": "devices_list_capabilities",
            "description": "List this actor's currently bound Android devices and their allowlisted local capabilities. Read-only; use before a device-local action when the target device is unclear.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "calendar_create_event_on_device",
            "description": "Ask a bound Android device to create one event in the user's personal calendar. The backend creates a durable device command and waits briefly for a device receipt. Never claim the event exists unless the returned status is applied with verified readback.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string"},
                    "project_id": {"type": "string"},
                    "title": {"type": "string"},
                    "starts_at": {
                        "type": "string",
                        "description": "RFC3339 timestamp with explicit UTC offset."
                    },
                    "ends_at": {
                        "type": "string",
                        "description": "RFC3339 timestamp with explicit UTC offset."
                    },
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone, for example Europe/Kaliningrad."
                    },
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                    "event_type": {
                        "type": "string",
                        "enum": ["generic", "podcast"],
                        "description": "Use podcast only for podcast/interview recording; otherwise generic."
                    }
                },
                "required": ["title", "starts_at", "ends_at", "timezone"]
            },
        },
        {
            "name": "calendar_list_events_on_device",
            "description": "Read a bounded time window from the user's personal Android calendar through a bound device. Use for questions like what is scheduled today/tomorrow/this week. This is read-only and never creates or edits events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string"},
                    "project_id": {"type": "string"},
                    "starts_at": {
                        "type": "string",
                        "description": "RFC3339 range start with explicit UTC offset."
                    },
                    "ends_at": {
                        "type": "string",
                        "description": "RFC3339 range end with explicit UTC offset; maximum range is 31 days."
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20
                    }
                },
                "required": ["starts_at", "ends_at"]
            },
        },
        {
            "name": "event_cards_list",
            "description": "List upcoming event-readiness cards and their checklist/task state. Use before discussing whether the user is ready for an event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20}
                }
            },
        },
        {
            "name": "event_readiness_set",
            "description": "Mark one checklist item for an event card as ready/not ready after the user confirms its real state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "checklist_key": {"type": "string"},
                    "done": {"type": "boolean"}
                },
                "required": ["event_id", "checklist_key", "done"]
            },
        },
        {
            "name": "task_create_follow_up",
            "description": "Create one concrete follow-up task for an event or project when readiness work is missing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "project_id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "assignee_role": {"type": "string"},
                    "deadline": {"type": "string"}
                },
                "required": ["title"]
            },
        },
        {
            "name": "task_set_state",
            "description": "Update one task after the user accepts, completes, postpones or rejects it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "state": {
                        "type": "string",
                        "enum": ["accepted", "done", "snoozed", "rejected"]
                    }
                },
                "required": ["task_id", "state"]
            },
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
            "description": "Create or update one durable memory result grounded in the current private voice source. The full provider transcript stays actor-private; project memory contains only your semantic_notes plus a private source reference. For one mixed utterance, call this once for each distinct project/result that should persist. The backend binds the current source and authorizes each project; never invent success.",
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
                        "description": "Required concise durable memory content grounded only in what the user said. For project memory, include only information appropriate for that target project; never copy unrelated parts of a mixed-project utterance.",
                    },
                },
                "required": ["title", "kind", "semantic_notes"],
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
    if regional_knowledge:
        functions.append(
            {
                "name": "knowledge_search",
                "description": (
                    "Search the current user's authorized Regional Knowledge "
                    "books and journals. Returns a small source-backed evidence "
                    "pack with stable URLs/provenance. Use when regional factual "
                    "evidence is useful; absence of a result is not proof that a "
                    "claim is false."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "maxLength": 1000},
                        "max_evidence": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 5,
                        },
                    },
                    "required": ["query"],
                },
            }
        )
    if expert_reviews:
        functions.extend(
            [
                {
                    "name": "expert_reviews_list_assigned",
                    "description": (
                        "List expert review cases the current verified expert is "
                        "allowed and qualified to review."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "statuses": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "enum": [
                                        "open",
                                        "assigned",
                                        "in_review",
                                        "deferred",
                                    ],
                                },
                            }
                        },                    },
                },
                {
                    "name": "expert_reviews_get",
                    "description": (
                        "Read one assigned/accessible expert review case with its "
                        "competing claims and evidence references."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "review_case_id": {"type": "string"}
                        },
                        "required": ["review_case_id"],
                    },
                },
                {
                    "name": "expert_reviews_accept",
                    "description": (
                        "Accept one expert review assignment using the current "
                        "verified expertise snapshot."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "review_case_id": {"type": "string"},
                            "expected_revision": {
                                "type": "integer",
                                "minimum": 1,
                            },
                        },
                        "required": [
                            "review_case_id",
                            "expected_revision",
                        ],
                    },
                },
                {
                    "name": "expert_reviews_resolve",
                    "description": (
                        "Submit a typed expert decision with rationale. The result "
                        "is trusted only after owning-service receipt and readback."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "review_case_id": {"type": "string"},
                            "expected_revision": {
                                "type": "integer",
                                "minimum": 1,
                            },
                            "resolution": {
                                "type": "string",
                                "enum": [
                                    "prefer_left",
                                    "prefer_right",
                                    "both_valid_scope",
                                    "both_valid_temporal",
                                    "unresolved",
                                    "needs_more_sources",
                                    "wrong_poi_link",
                                ],
                            },
                            "rationale": {"type": "string"},
                            "confidence": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                            },
                        },
                        "required": [
                            "review_case_id",
                            "expected_revision",
                            "resolution",
                            "rationale",
                        ],
                    },
                },
                {
                    "name": "expert_reviews_request_research",
                    "description": (
                        "Keep the case unresolved and request additional sources "
                        "with an expert rationale."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "review_case_id": {"type": "string"},
                            "expected_revision": {
                                "type": "integer",
                                "minimum": 1,
                            },
                            "rationale": {"type": "string"},
                        },
                        "required": [
                            "review_case_id",
                            "expected_revision",
                            "rationale",
                        ],
                    },
                },
            ]
        )
    if owner_development:
        functions.extend(
            [
                {
                    "name": "backlog_create",
                    "description": (
                        "Create one durable development backlog task for the current "
                        "or explicitly selected project. This only records work; it never "
                        "starts Codex. Use when the owner asks to add/capture a product or "
                        "engineering task in the backlog."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "string"},
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "acceptance_criteria": {
                                "type": "array",
                                "maxItems": 10,
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["title"],
                    },
                },
                {
                    "name": "backlog_list",
                    "description": (
                        "List durable backlog tasks for the current project/workspace. "
                        "Backlog is the primary work queue regardless of who later implements it."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                        },
                    },
                },
                {
                    "name": "development_codex_status",
                    "description": (
                        "Read native Codex quota/capacity, owner profile and the current "
                        "native Codex model catalogue. Read-only; never launches inference."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
                {
                    "name": "development_execute_backlog",
                    "description": (
                        "Start implementation for 1-5 existing durable backlog tasks. "
                        "CALL ONLY after the platform owner explicitly asks to implement/run "
                        "those tasks now. Discussion, prioritization or backlog creation alone "
                        "must never call this tool. The backend rechecks owner identity, Codex "
                        "capacity >10%, model availability and one-active-run policy."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_ids": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 5,
                                "uniqueItems": True,
                                "items": {"type": "string"},
                            },
                            "model": {
                                "type": "string",
                                "description": "Exact native Codex model ID only after the owner explicitly selects it when the default owner profile is unavailable."
                            },
                            "reasoning_effort": {
                                "type": "string",
                                "enum": ["low", "medium", "high", "xhigh", "max", "ultra"]
                            }
                        },
                        "required": ["task_ids"],
                    },
                },
                {
                    "name": "development_execution_status",
                    "description": (
                        "Read/synchronize the latest owner development run or a specific run. "
                        "Use when the owner asks what Codex is doing, whether it finished, "
                        "or what result was delivered."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "execution_id": {"type": "string"},
                        },
                    },
                },
            ]
        )
    return functions


SYSTEM_INSTRUCTION = """# ROLE
Ты центральный Live-агент Projects Hub. Ты сама слышишь аудио пользователя и остаёшься единственным семантическим оркестратором разговора.

# DIALOGUE
- Отвечай по-русски, кратко и естественно голосом.
- Не проси пользователя перепечатывать или повторять уже услышанное без необходимости.
- Если проект неясен, уточни его разговором или сначала прочитай доступные проекты.
- При смене проекта используй conversation_set_focus только после того, как поняла целевой проект.

# CAPABILITY TOUR
- Если пользователь спрашивает «что ты умеешь», «что можно сделать здесь» или явно просит рассказать о возможностях, дай короткий продуктовый обзор возможностей именно этой текущей Live-сессии.
- Опирайся на функции, реально переданные тебе в configuration.functions, и на текущий context; не перечисляй скрытые или не подключённые capabilities как доступные сейчас.
- Разделяй: что доступно прямо сейчас; что требует привязанного Android-устройства, подключённого GitHub repository, Regional Knowledge grant или expert grant.
- Не зачитывай названия function tools и не делай длинный технический список. Объясняй человеческими сценариями: поговорить и переключаться между проектами, помнить важное, работать с подключёнными репозиториями, календарём/подготовкой к событиям и только теми дополнительными источниками/экспертными функциями, которые реально доступны в этой сессии.
- Если capability недоступна, можно кратко сказать, что её можно подключить, но не обещай, что она уже работает.

# MEMORY
- Для явного «запомни/сохрани» и явно долговечной информации используй memory_commit_voice_source.
- Один voice source может относиться к нескольким проектам: сделай отдельный memory_commit_voice_source для каждого действительно нужного project/result.
- В semantic_notes передавай только память для конкретного target project. Полный provider transcript остаётся личным source и не копируется в project memory.
- Для вопроса о ранее сохранённом используй memory_read_project.
- Не архивируй каждую бытовую реплику автоматически.
- После хотя бы одного успешного memory_commit_voice_source не вызывай memory_finish_ephemeral для того же source.
- Никогда не говори, что что-то сохранено или изменено, пока function result не подтвердил это.
- Source transcript создаёт provider этой же Live-сессии; function tools не являются вторым AI.

# SECURITY
- Доступ определяет backend. Аргументы function call не могут расширять права или подключать новый repository.
- github_repositories_list показывает только уже подключённые и привязанные repositories. Если нужного repo нет, скажи, что его должен разрешить workspace owner через GitHub integration UI; не пытайся заменить это другим repo.
- Для чтения текущего проекта используй github_repository_read: сначала корень/каталог, затем нужный текстовый файл. Не утверждай, что прочитала repository, пока tool result не вернул фактический content.
- Device-local действие всё равно вызывается здесь, в backend-owned Live session. Android — только исполнитель typed command.
- Для календаря сначала используй devices_list_capabilities, если подходящий телефон неоднозначен. Для вопросов о расписании используй calendar_list_events_on_device; для создания — calendar_create_event_on_device.
- calendar_list_events_on_device только читает локальный CalendarContract выбранного Android и возвращает ограниченное окно до 31 дня. Не придумывай события, если device readback не вернулся.
- Говори «событие создано» только если calendar_create_event_on_device вернул status=applied и device readback. pending/claimed означает, что подтверждение на телефоне ещё ожидается; outcome_unknown означает, что итог надо сверить.
- Если tool отказал, объясни результат и продолжи разговор, не выдумывая успешное действие.

# REGIONAL KNOWLEDGE
- knowledge_search присутствует только когда backend подтвердил отдельный user-authorized grant к Regional Knowledge resource.
- Используй его для региональных фактов, когда полезны книги/журналы и provenance. Отвечай по evidence, сохраняй различие между источником и своим выводом.
- Отсутствие результата не доказывает ложность факта. Не выдавай snippets без evidence за проверенную истину.
- Projects Hub token не является Knowledge token; доступ и ACL проверяет сам Knowledge resource.

# EXPERT REVIEWS
- Expert-review tools присутствуют только когда backend подтвердил owning-service grant и verified expert profile.
- Не решай противоречие сама: объясняй evidence и вызывай typed expert tool только после явного решения эксперта.
- Verification score — сила evidence, а не вероятность истины.
- "Нужны ещё источники" является нормальным экспертным исходом.
- Объявляй решение сохранённым только после receipt/readback owning service.

# BACKLOG AND OWNER DEVELOPMENT
- Backlog — первичная сущность работы. task_create_follow_up создаёт durable task и может использоваться как обычная project backlog-задача даже без event_card.
- backlog_list показывает существующие задачи проекта; не создавай параллельный «самодоработочный» список.
- Обычное обсуждение, приоритизация, формулировка или добавление задачи в backlog НЕ разрешают запуск разработки.
- development_execute_backlog вызывай только если текущий platform owner явно попросил реализовать/запустить конкретную существующую задачу или выбранный набор задач прямо сейчас.
- Можно запускать 1–5 задач одного проекта одним execution. Задачи разных проектов запускай отдельными execution.
- Перед стартом backend сам проверяет owner, native Codex quota >10%, live model catalog и отсутствие другого активного owner-run. Если owner profile недоступен, сначала вызови development_codex_status, назови доступные native модели и попроси владельца явно выбрать модель/effort. Не выбирай Astra/другую модель сама и не обходи отказ.
- development_codex_status используй для вопросов об остатке лимита/доступности Codex; сообщай фактический remaining_percent и reset/status из tool result.
- development_execution_status используй для «что сейчас делает Codex», «закончилось ли», «какой результат». Не объявляй разработку завершённой раньше terminal status.
- ChatGPT/Codex, запущенные владельцем вне Projects Hub, остаются допустимыми способами выполнить ту же backlog-задачу; execution Миры — только один из путей исполнения backlog.

# EVENT READINESS
- После подтверждённого calendar event backend автоматически создаёт event card. Для записи подкаста передавай event_type=podcast, иначе generic.
- Перед событием используй event_cards_list и называй только фактические незакрытые пункты checklist.
- Меняй checklist через event_readiness_set только после подтверждения пользователя, не угадывай готовность.
- Если реально не хватает подготовки, предложи один конкретный follow-up и создавай его через task_create_follow_up после согласия.
- task_set_state отражает принятие/выполнение/откладывание/отказ; не объявляй task выполненной без tool result.
"""


class ProjectsHubLiveAdapter:
    def __init__(
        self,
        store: DurableStore,
        *,
        device_commands: DeviceCommandService | None = None,
        readiness: ReadinessService | None = None,
        development: DevelopmentService | None = None,
        github_connections: GitHubConnections | None = None,
        expert_reviews_factory: (
            Callable[[str, str], ExpertReviewAdapter | None] | None
        ) = None,
        regional_knowledge_factory: (
            Callable[[str, str], RegionalKnowledgeAdapter | None] | None
        ) = None,
        **_shared: Any,
    ):
        self.store = store
        self.device_commands = device_commands or DeviceCommandService(store)
        self.readiness = readiness or ReadinessService(store)
        self.development = development or DevelopmentService(store, self.readiness)
        self.github_connections = github_connections
        self.expert_reviews_factory = expert_reviews_factory
        self.regional_knowledge_factory = regional_knowledge_factory

    def _expert_reviews(
        self,
        actor_id: str,
        workspace_id: str,
    ) -> ExpertReviewAdapter | None:
        if self.expert_reviews_factory is None:
            return None
        adapter = self.expert_reviews_factory(actor_id, workspace_id)
        if adapter is not None and adapter.profile.subject != actor_id:
            raise StoreError(
                "FORBIDDEN",
                "Expert review profile does not match current actor",
            )
        return adapter

    def _regional_knowledge(
        self,
        actor_id: str,
        workspace_id: str,
    ) -> RegionalKnowledgeAdapter | None:
        if self.regional_knowledge_factory is None:
            return None
        adapter = self.regional_knowledge_factory(actor_id, workspace_id)
        if adapter is not None and (
            adapter.actor_sub != actor_id
            or adapter.workspace_id != workspace_id
        ):
            raise StoreError(
                "FORBIDDEN",
                "Regional Knowledge grant does not match current actor/workspace",
            )
        return adapter

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
            conversation_id=conversation_id,        ).resource_binding()
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
        expert_reviews = self._expert_reviews(
            actor_id,
            conversation["workspace_id"],
        )
        regional_knowledge = self._regional_knowledge(
            actor_id,
            conversation["workspace_id"],
        )
        owner_development = False
        try:
            self.store.require_platform_owner(actor_id)
            self.store.require_workspace_owner(actor_id, conversation["workspace_id"])
            owner_development = True
        except StoreError:
            owner_development = False

        system_instruction = SYSTEM_INSTRUCTION
        if audio_mode == "buffered":
            system_instruction += """
# BUFFERED SOURCE DISPOSITION
Этот Live-turn является одной законченной ранее записанной репликой.
До завершения ответа обязательно дай source одно терминальное disposition:
- один или несколько memory_commit_voice_source, если запись содержит долговечную память; для разных проектов/результатов делай отдельные вызовы;
- либо memory_finish_ephemeral, если после выполнения просьбы хранить её как память не нужно.
После успешного memory_commit_voice_source не вызывай memory_finish_ephemeral.
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
                "functions": _functions(
                    expert_reviews=expert_reviews is not None,
                    regional_knowledge=regional_knowledge is not None,
                    owner_development=owner_development,
                ),
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
                "expert_reviews_enabled": expert_reviews is not None,
                "regional_knowledge_enabled": regional_knowledge is not None,
                "owner_development_enabled": owner_development,
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

        if name == "backlog_list":
            self.store.require_platform_owner(actor_id)
            self.store.require_workspace_owner(actor_id, workspace_id)
            project_id = str(args.get("project_id") or "") or None
            if project_id is None:
                project_id = self.store.get_conversation(
                    actor_id,
                    conversation_id,
                ).get("focus_project_id")
            try:
                limit = int(args.get("limit", 20))
            except (TypeError, ValueError):
                limit = 20
            return {
                "tasks": self.development.list_backlog(
                    actor_id=actor_id,
                    workspace_id=workspace_id,
                    project_id=project_id,
                    limit=limit,
                )
            }

        if name == "backlog_create":
            self.store.require_platform_owner(actor_id)
            self.store.require_workspace_owner(actor_id, workspace_id)
            project_id = str(args.get("project_id") or "") or None
            if project_id is None:
                project_id = self.store.get_conversation(
                    actor_id,
                    conversation_id,
                ).get("focus_project_id")
            if not project_id:
                raise StoreError(
                    "DEVELOPMENT_PROJECT_REQUIRED",
                    "Choose a project before creating a development backlog task",
                )
            command_id, _args_sha = self._command_id(session, name, args)
            raw_criteria = args.get("acceptance_criteria")
            criteria = (
                [str(item) for item in raw_criteria]
                if isinstance(raw_criteria, list)
                else []
            )
            return self.development.create_backlog_task(
                actor_id=actor_id,
                workspace_id=workspace_id,
                project_id=str(project_id),
                task_key=command_id,
                title=str(args.get("title") or ""),
                description=str(args.get("description") or ""),
                acceptance_criteria=criteria,
            )

        if name == "development_codex_status":
            return await self.development.codex_status(
                actor_id=actor_id,
                workspace_id=workspace_id,
            )

        if name == "development_execute_backlog":
            raw_ids = args.get("task_ids")
            if not isinstance(raw_ids, list):
                raise StoreError("INVALID_ARGUMENT", "task_ids must be a list")
            return await self.development.start(
                actor_id=actor_id,
                workspace_id=workspace_id,
                task_ids=[str(item) for item in raw_ids],
                model=str(args.get("model") or "") or None,
                reasoning_effort=str(args.get("reasoning_effort") or "") or None,
            )

        if name == "development_execution_status":
            execution_id = str(args.get("execution_id") or "") or None
            return await self.development.status(
                actor_id=actor_id,
                workspace_id=workspace_id,
                execution_id=execution_id,
                sync=True,
            )

        if name == "github_repositories_list":
            rows = self.store.list_repository_connections(actor_id, workspace_id)
            repositories = [
                {
                    "repository_id": int(item["repository_id"]),
                    "full_name": item["full_name"],
                    "project_id": item["project_id"],
                    "role": item["role"],
                    "access_mode": item["access_mode"],
                    "allowed_paths": item["allowed_paths"],
                }
                for item in rows
                if item["state"] == "available"
                and item["installation_state"] == "active"
                and item["role"] != "unassigned"
            ]
            return {
                "repositories": repositories,
                "requires_connection": not repositories,
            }

        if name == "github_repository_read":
            if self.github_connections is None:
                raise StoreError("GITHUB_APP_NOT_CONFIGURED", "GitHub integration is unavailable")
            repository_id = int(args.get("repository_id") or 0)
            if repository_id <= 0:
                raise StoreError("INVALID_ARGUMENT", "repository_id is required")
            return await self.github_connections.read_repository_path(
                actor_id=actor_id,
                workspace_id=workspace_id,
                repository_id=repository_id,
                path=str(args.get("path") or ""),
            )

        if name == "devices_list_capabilities":
            return {
                "devices": [
                    {
                        "device_id": item["id"],
                        "display_name": item["display_name"],
                        "platform": item["platform"],
                        "capabilities": item["capabilities"],
                        "last_seen_at_ms": item["last_seen_at_ms"],
                    }
                    for item in self.device_commands.list_devices(
                        actor_id=actor_id,
                        workspace_id=workspace_id,
                    )
                ]
            }

        if name == "calendar_list_events_on_device":
            project_id = str(args.get("project_id") or "") or None
            if project_id is None:
                project_id = self.store.get_conversation(
                    actor_id,
                    conversation_id,
                ).get("focus_project_id")
            command_id, _args_sha = self._command_id(session, name, args)
            command = self.device_commands.create_calendar_read_command(
                actor_id=actor_id,
                workspace_id=workspace_id,
                project_id=project_id,
                command_id=command_id,
                args=args,
            )
            result = await self.device_commands.wait_for_terminal(
                actor_id=actor_id,
                workspace_id=workspace_id,
                command_id=command["id"],
                timeout_seconds=20.0,
            )
            log.info(
                "device command result",
                extra={
                    "event": "tool_result",
                    "tool": name,
                    "conversation_id": conversation_id,
                    "command_id": command["id"],
                    "device_id": result.get("device_id"),
                    "result": result.get("status"),
                },
            )
            return result

        if name == "calendar_create_event_on_device":
            project_id = str(args.get("project_id") or "") or None
            if project_id is None:
                project_id = self.store.get_conversation(
                    actor_id,
                    conversation_id,
                ).get("focus_project_id")
            command_id, _args_sha = self._command_id(session, name, args)
            command = self.device_commands.create_calendar_command(
                actor_id=actor_id,
                workspace_id=workspace_id,
                project_id=project_id,
                command_id=command_id,
                args=args,
            )
            result = await self.device_commands.wait_for_terminal(
                actor_id=actor_id,
                workspace_id=workspace_id,
                command_id=command["id"],
                timeout_seconds=40.0,
            )
            if (                result.get("status") == "applied"
                and isinstance(result.get("result"), dict)
                and result["result"].get("readback_verified") is True
            ):
                event_card = self.readiness.record_calendar_event(
                    actor_id=actor_id,
                    workspace_id=workspace_id,
                    project_id=project_id,
                    command_id=command["id"],
                    title=str(args.get("title") or ""),
                    starts_at=str(args.get("starts_at") or ""),
                    event_type=str(args.get("event_type") or "generic"),
                    device_event_id=str(result["result"].get("event_id") or "") or None,
                )
                result = {**result, "event_card": event_card}
            log.info(
                "device command result",
                extra={
                    "event": "tool_result",
                    "tool": name,
                    "conversation_id": conversation_id,
                    "command_id": command["id"],
                    "device_id": result.get("device_id"),
                    "result": result.get("status"),
                },
            )
            return result

        if name == "event_cards_list":
            project_id = str(args.get("project_id") or "") or None
            if project_id is None:
                project_id = self.store.get_conversation(actor_id, conversation_id).get("focus_project_id")
            try:
                limit = int(args.get("limit", 8))
            except (TypeError, ValueError):
                limit = 8
            return {"events": self.readiness.list_event_cards(
                actor_id=actor_id, workspace_id=workspace_id, project_id=project_id, limit=limit
            )}

        if name == "event_readiness_set":
            return self.readiness.set_readiness(
                actor_id=actor_id,
                workspace_id=workspace_id,
                event_id=str(args.get("event_id") or ""),
                checklist_key=str(args.get("checklist_key") or ""),
                done=bool(args.get("done")),
            )

        if name == "task_create_follow_up":
            project_id = str(args.get("project_id") or "") or None
            if project_id is None:
                project_id = self.store.get_conversation(actor_id, conversation_id).get("focus_project_id")
            command_id, _args_sha = self._command_id(session, name, args)
            return self.readiness.create_follow_up_task(
                actor_id=actor_id,
                workspace_id=workspace_id,
                project_id=project_id,
                event_id=str(args.get("event_id") or "") or None,
                command_id=command_id,
                title=str(args.get("title") or ""),
                description=str(args.get("description") or ""),
                assignee_role=str(args.get("assignee_role") or "owner"),
                deadline=str(args.get("deadline") or "") or None,
            )

        if name == "task_set_state":
            return self.readiness.set_task_state(
                actor_id=actor_id,
                workspace_id=workspace_id,
                task_id=str(args.get("task_id") or ""),
                state=str(args.get("state") or ""),
            )

        if name == "knowledge_search":
            adapter = self._regional_knowledge(actor_id, workspace_id)
            if adapter is None:
                raise StoreError(
                    "TOOL_NOT_AVAILABLE",
                    "Regional Knowledge is not connected for this actor",
                )
            try:
                return await adapter.search(
                    str(args.get("query") or ""),
                    max_evidence=args.get("max_evidence", 3),
                )
            except RegionalKnowledgeInputError as exc:
                raise StoreError("INVALID_ARGUMENT", str(exc)) from exc
            except RegionalKnowledgeAccessError as exc:
                raise StoreError("FORBIDDEN", str(exc)) from exc
            except RegionalKnowledgeContractError as exc:
                log.warning(
                    "regional knowledge contract rejected",
                    extra={
                        "event": "tool_result",
                        "tool": name,
                        "conversation_id": conversation_id,
                        "result": "invalid_contract",
                    },
                )
                raise StoreError("KNOWLEDGE_UNAVAILABLE", str(exc)) from exc
            except RegionalKnowledgeUnavailable as exc:
                raise StoreError("KNOWLEDGE_UNAVAILABLE", str(exc)) from exc

        if name.startswith("expert_reviews_"):
            adapter = self._expert_reviews(actor_id, workspace_id)
            if adapter is None:
                raise StoreError(
                    "TOOL_NOT_AVAILABLE",
                    "Expert reviews are not connected for this actor",
                )
            try:
                if name == "expert_reviews_list_assigned":
                    statuses = args.get("statuses")
                    return {
                        "review_cases": await adapter.list_assigned(
                            statuses
                            if isinstance(statuses, list)
                            else ("assigned", "in_review", "open")
                        )
                    }
                if name == "expert_reviews_get":
                    return await adapter.get(
                        str(args.get("review_case_id") or "")
                    )
                try:
                    expected_revision = int(args.get("expected_revision"))
                except (TypeError, ValueError):
                    raise ExpertReviewError(
                        "expected_revision_invalid"
                    ) from None
                command_id, _args_sha = self._command_id(
                    session,
                    name,
                    args,
                )
                review_case_id = str(
                    args.get("review_case_id") or ""
                )
                if name == "expert_reviews_accept":
                    return await adapter.accept(
                        review_case_id,
                        expected_revision=expected_revision,
                        command_id=command_id,
                    )
                if name == "expert_reviews_resolve":
                    confidence = args.get("confidence")
                    return await adapter.resolve(
                        review_case_id,
                        expected_revision=expected_revision,
                        resolution=str(args.get("resolution") or ""),
                        rationale=str(args.get("rationale") or ""),
                        confidence=(
                            float(confidence)
                            if confidence is not None
                            else None
                        ),
                        command_id=command_id,
                    )
                if name == "expert_reviews_request_research":
                    return await adapter.request_research(
                        review_case_id,
                        expected_revision=expected_revision,
                        rationale=str(args.get("rationale") or ""),
                        command_id=command_id,
                    )
                raise StoreError(
                    "TOOL_NOT_AVAILABLE",
                    f"Unknown expert review function: {name}",
                )
            except ExpertReviewAccessError as exc:
                raise StoreError("FORBIDDEN", str(exc)) from exc
            except ExpertReviewConflict as exc:
                raise StoreError("CONFLICT", str(exc)) from exc
            except ExpertReviewError as exc:
                raise StoreError("INVALID_ARGUMENT", str(exc)) from exc

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

        if name == "memory_finish_ephemeral":            return self.store.finish_ephemeral(actor_id, state["source_id"])

        raise StoreError("TOOL_NOT_AVAILABLE", f"Unknown function: {name}")