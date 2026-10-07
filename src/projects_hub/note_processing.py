from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# Adapted from the proven my-data-hub voice-intake text summary contract at
# e7972f7339b0abe28634bd1782c0d517c65ab06e. This module deliberately reuses
# only text -> structured JSON semantics; it does not introduce audio capture,
# ASR, TTS, or another conversational router.
SUMMARY_PROMPT_VERSION = "projects-hub-note-summary-v1"
SOURCE_CONTRACT = "my-data-hub:voice-summary-v2@e7972f7339b0abe28634bd1782c0d517c65ab06e"
DEFAULT_MODEL = "gemini-3.1-flash-lite"
MAX_OUTPUT_TOKENS = 16_384
TEXT_CHARACTERS_PER_TOKEN = 2
LIMITER_CONTRACT = "google_ai_project_model_atomic_v1"
BUCKET_STRATEGY = "rolling_60s_pacific_day_v2"


class NoteProcessingError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoteTask(StrictModel):
    text: str = Field(min_length=1, max_length=10_000)
    owner: str | None = Field(default=None, max_length=500)
    deadline: str | None = Field(default=None, max_length=200)
    explicitly_stated: bool = True


class NoteSummary(StrictModel):
    title: str = Field(min_length=1, max_length=240)
    short_summary: str = Field(min_length=1, max_length=20_000)
    detailed_summary: str = Field(min_length=1, max_length=120_000)
    theses: list[str] = Field(default_factory=list, max_length=300)
    ideas: list[str] = Field(default_factory=list, max_length=300)
    decisions: list[str] = Field(default_factory=list, max_length=300)
    tasks: list[NoteTask] = Field(default_factory=list, max_length=300)
    facts: list[str] = Field(default_factory=list, max_length=300)
    entities: list[str] = Field(default_factory=list, max_length=300)
    related_projects: list[str] = Field(default_factory=list, max_length=300)
    open_questions: list[str] = Field(default_factory=list, max_length=300)
    contradictions: list[str] = Field(default_factory=list, max_length=300)
    uncertain_fragments: list[str] = Field(default_factory=list, max_length=300)
    tags: list[str] = Field(default_factory=list, max_length=100)

    @field_validator(
        "theses",
        "ideas",
        "decisions",
        "facts",
        "entities",
        "related_projects",
        "open_questions",
        "contradictions",
        "uncertain_fragments",
        "tags",
    )
    @classmethod
    def validate_strings(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 10_000 for value in values):
            raise ValueError("summary collections must contain bounded non-empty strings")
        return values


SUMMARY_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "short_summary": {"type": "string"},
        "detailed_summary": {"type": "string"},
        "theses": {"type": "array", "items": {"type": "string"}},
        "ideas": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "owner": {"type": ["string", "null"]},
                    "deadline": {"type": ["string", "null"]},
                    "explicitly_stated": {"type": "boolean"},
                },
                "required": ["text", "owner", "deadline", "explicitly_stated"],
                "additionalProperties": False,
            },
        },
        "facts": {"type": "array", "items": {"type": "string"}},
        "entities": {"type": "array", "items": {"type": "string"}},
        "related_projects": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "contradictions": {"type": "array", "items": {"type": "string"}},
        "uncertain_fragments": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "title",
        "short_summary",
        "detailed_summary",
        "theses",
        "ideas",
        "decisions",
        "tasks",
        "facts",
        "entities",
        "related_projects",
        "open_questions",
        "contradictions",
        "uncertain_fragments",
        "tags",
    ],
    "additionalProperties": False,
}


SUMMARY_PROMPT = """Ты структурируешь один явно выбранный фрагмент общей проектной заметки Projects Hub.
Верни только JSON по заданной схеме.

Правила:
- не теряй уникальные идеи и факты исходного текста;
- не выдавай гипотезу, рекомендацию или размышление за принятое решение;
- не придумывай сроки, ответственных, факты или связи;
- отделяй задачи от идей, решения от предложений, факты от интерпретаций;
- owner и deadline в tasks оставляй null, если они не были явно названы;
- explicitly_stated=true только для прямо сформулированной автором задачи;
- модельная рекомендация может быть task только с explicitly_stated=false и не становится поручением;
- сохраняй противоречия и неуверенность;
- title должен быть конкретным и пригодным как заголовок Markdown;
- detailed_summary должен позволять участнику понять материал без личной переписки автора;
- не включай в результат ничего из соседней личной переписки, которой нет во входном тексте;
- язык ответа — русский.
"""


@dataclass(frozen=True, slots=True)
class ProcessedNote:
    summary: NoteSummary
    model: str
    prompt_version: str
    request_uid: str
    limiter: dict[str, Any]


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _section(title: str, values: list[str]) -> list[str]:
    if not values:
        return []
    return [f"## {title}", "", *[f"- {value.strip()}" for value in values], ""]


def _task_lines(summary: NoteSummary) -> list[str]:
    result: list[str] = []
    for task in summary.tasks:
        qualifiers: list[str] = []
        if task.owner:
            qualifiers.append(f"ответственный: {task.owner}")
        if task.deadline:
            qualifiers.append(f"срок: {task.deadline}")
        if not task.explicitly_stated:
            qualifiers.append("предложено моделью; не принято как поручение")
        suffix = f" ({'; '.join(qualifiers)})" if qualifiers else ""
        result.append(task.text.strip() + suffix)
    return result


def render_note_markdown(
    *,
    note_id: str,
    project_id: str,
    project_name: str,
    author_id: str,
    author_display_name: str,
    author_roles: list[str],
    audience: str,
    created_at_ms: int,
    revision: int,
    source_text: str,
    processed: ProcessedNote,
) -> str:
    summary = processed.summary
    frontmatter = [
        "---",
        f"note_id: {note_id}",
        f"project_id: {project_id}",
        f"project: {_json_text(project_name)}",
        f"author_id: {author_id}",
        f"author_display_name: {_json_text(author_display_name)}",
        f"author_roles: {_json_text(author_roles)}",
        f"audience: {audience}",
        f"revision: {revision}",
        f"created_at_ms: {created_at_ms}",
        f"processing_model: {processed.model}",
        f"processing_prompt_version: {processed.prompt_version}",
        f"processing_request_uid: {processed.request_uid}",
        f"processing_source_contract: {_json_text(SOURCE_CONTRACT)}",
        "---",
        "",
    ]
    body: list[str] = [
        *frontmatter,
        f"# {summary.title.strip()}",
        "",
        "## Кратко",
        "",
        summary.short_summary.strip(),
        "",
        "## Подробно",
        "",
        summary.detailed_summary.strip(),
        "",
    ]
    for title, values in (
        ("Основные тезисы", summary.theses),
        ("Идеи и гипотезы", summary.ideas),
        ("Решения автора", summary.decisions),
        ("Задачи и предложения", _task_lines(summary)),
        ("Факты и конкретика", summary.facts),
        ("Упомянутые сущности", summary.entities),
        ("Связанные проекты", summary.related_projects),
        ("Открытые вопросы", summary.open_questions),
        ("Противоречия", summary.contradictions),
        ("Неопределённые фрагменты", summary.uncertain_fragments),
    ):
        body.extend(_section(title, values))
    body.extend(
        [
            "## Исходный текст заметки",
            "",
            source_text.strip(),
            "",
        ]
    )
    return "\n".join(body).rstrip() + "\n"


class GeminiNoteProcessor:
    """Bounded text-only Gemini processor using the canonical Google AI limiter."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        model: str | None = None,
    ) -> None:
        self.environment = os.environ if environment is None else environment
        self.model = str(
            model
            or self.environment.get("PROJECTS_HUB_NOTE_TEXT_MODEL")
            or DEFAULT_MODEL
        ).strip()
        self._client = client
        self._owns_client = client is None

    def _limiter_origin(self) -> str:
        return str(
            self.environment.get("GOOGLE_AI_LIMITER_SUPABASE_URL")
            or self.environment.get("AI_SUPABASE_URL")
            or self.environment.get("AI_RESOURCE_CONTROL_URL")
            or ""
        ).strip().rstrip("/")

    def _limiter_key(self) -> str:
        return str(
            self.environment.get("GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY")
            or self.environment.get("AI_SUPABASE_SECRET_KEY")
            or self.environment.get("AI_RESOURCE_CONTROL_SERVICE_KEY")
            or ""
        ).strip()

    def _candidate_envs(self) -> tuple[str, ...]:
        configured = str(self.environment.get("GOOGLE_AI_NORMAL_KEY_ENVS") or "").strip()
        values = tuple(part.strip() for part in configured.split(",") if part.strip())
        if values:
            return values
        return ("GOOGLE_API_KEY4",)

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
        return self._client

    async def _supabase(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        allow_empty: bool = False,
    ) -> Any:
        origin = self._limiter_origin()
        key = self._limiter_key()
        if not origin or not key:
            raise NoteProcessingError(
                "NOTE_PROCESSOR_NOT_CONFIGURED",
                "Shared Google AI limiter binding is unavailable",
            )
        client = await self._http()
        try:
            response = await client.request(
                method,
                origin + "/rest/v1/" + path,
                params=params,
                json=body,
                headers={
                    "apikey": key,
                    "Authorization": "Bearer " + key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise NoteProcessingError(
                "NOTE_LIMITER_UNAVAILABLE",
                "Shared Google AI limiter is unavailable",
                retryable=True,
            ) from exc
        if response.status_code >= 300:
            raise NoteProcessingError(
                "NOTE_LIMITER_UNAVAILABLE",
                "Shared Google AI limiter rejected the request",
                retryable=response.status_code >= 500,
            )
        if allow_empty and not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            if allow_empty and not response.text.strip():
                return None
            raise NoteProcessingError(
                "NOTE_LIMITER_INVALID_RESPONSE",
                "Shared Google AI limiter returned invalid JSON",
            ) from exc

    async def _rpc(
        self,
        name: str,
        body: dict[str, Any],
        *,
        allow_empty: bool = False,
    ) -> Any:
        return await self._supabase(
            "POST",
            "rpc/" + name,
            body=body,
            allow_empty=allow_empty,
        )

    async def _preflight(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        capabilities = await self._rpc("google_ai_limiter_capabilities", {})
        if (
            not isinstance(capabilities, dict)
            or capabilities.get("limiter_contract") != LIMITER_CONTRACT
            or capabilities.get("bucket_strategy") != BUCKET_STRATEGY
            or capabilities.get("quota_scope_enforced") is not True
        ):
            raise NoteProcessingError(
                "NOTE_LIMITER_CONTRACT_MISMATCH",
                "Shared Google AI limiter contract is incompatible",
            )
        limits = await self._supabase(
            "GET",
            "google_ai_model_limits",
            params={
                "select": "model,rpm,tpm,rpd,tpm_reserve_extra",
                "model": "eq." + self.model,
                "limit": "2",
            },
        )
        if not isinstance(limits, list) or len(limits) != 1:
            raise NoteProcessingError(
                "NOTE_MODEL_LIMIT_NOT_FOUND",
                "Gemini note model is not registered in the shared limiter",
            )
        limit = limits[0]
        candidate_envs = self._candidate_envs()
        keys = await self._supabase(
            "GET",
            "google_ai_api_keys",
            params={
                "select": "id,env_var_name,key_alias,quota_scope,is_active,priority",
                "provider": "eq.google",
                "is_active": "eq.true",
                "order": "priority.asc,id.asc",
            },
        )
        if not isinstance(keys, list):
            raise NoteProcessingError(
                "NOTE_LIMITER_INVALID_RESPONSE",
                "Google AI key registry is unavailable",
            )
        candidates = [
            item
            for item in keys
            if isinstance(item, dict)
            and item.get("env_var_name") in candidate_envs
            and isinstance(self.environment.get(str(item.get("env_var_name"))), str)
            and str(self.environment.get(str(item.get("env_var_name"))) or "").strip()
        ]
        if not candidates:
            raise NoteProcessingError(
                "NOTE_PROCESSOR_KEY_UNAVAILABLE",
                "No registered Google key is available to the note processor",
            )
        return dict(limit), candidates

    @staticmethod
    def _reservation_tpm(limit: dict[str, Any], prompt: str) -> int:
        try:
            tpm = int(limit["tpm"])
            reserve_extra = int(limit.get("tpm_reserve_extra") or 0)
        except (KeyError, TypeError, ValueError) as exc:
            raise NoteProcessingError(
                "NOTE_MODEL_LIMIT_INVALID",
                "Gemini note model limit is invalid",
            ) from exc
        text_tokens = max(1, math.ceil(len(prompt) / TEXT_CHARACTERS_PER_TOKEN))
        completion_margin = max(reserve_extra, math.ceil(MAX_OUTPUT_TOKENS * 0.25))
        requested = text_tokens + MAX_OUTPUT_TOKENS + completion_margin
        if requested > tpm:
            raise NoteProcessingError(
                "NOTE_TEXT_TOO_LARGE",
                "Project note exceeds the configured Gemini token budget",
            )
        return requested

    async def _reserve(
        self,
        *,
        note_id: str,
        attempt_no: int,
        prompt: str,
    ) -> tuple[str, dict[str, Any], str]:
        limit, candidates = await self._preflight()
        request_uid = str(uuid.uuid5(uuid.NAMESPACE_URL, "projects-hub-note:" + note_id))
        reserved = self._reservation_tpm(limit, prompt)
        result = await self._rpc(
            "google_ai_reserve",
            {
                "p_request_uid": request_uid,
                "p_attempt_no": int(attempt_no),
                "p_consumer": "projects-hub.note-text.v1",
                "p_account_name": "projects-hub",
                "p_model": self.model,
                "p_reserved_tpm": reserved,
                "p_candidate_key_ids": [str(item["id"]) for item in candidates],
            },
        )
        if not isinstance(result, dict) or result.get("ok") is not True:
            reason = str((result or {}).get("blocked_reason") or "unavailable")
            raise NoteProcessingError(
                "NOTE_PROCESSOR_CAPACITY",
                "Gemini note processing is waiting for shared capacity",
                retryable=reason in {"rpm", "tpm", "rpd", "provider_429", "unavailable"},
            )
        if (
            result.get("limiter_contract") != LIMITER_CONTRACT
            or result.get("bucket_strategy") != BUCKET_STRATEGY
        ):
            raise NoteProcessingError(
                "NOTE_LIMITER_CONTRACT_MISMATCH",
                "Gemini note limiter receipt is incompatible",
            )
        env_name = str(result.get("env_var_name") or "")
        candidate = next(
            (item for item in candidates if item.get("env_var_name") == env_name),
            None,
        )
        api_key = str(self.environment.get(env_name) or "").strip()
        if candidate is None or not api_key:
            await self._release_unsent(request_uid, attempt_no, "note_key_missing")
            raise NoteProcessingError(
                "NOTE_PROCESSOR_KEY_UNAVAILABLE",
                "Reserved Google key is unavailable",
            )
        public = {
            "reserved_tpm": reserved,
            "key_alias": "key:…" + str(result.get("key_alias") or "")[-6:],
            "quota_scope_alias": "scope:…" + str(result.get("quota_scope") or "")[-6:],
            "contract": LIMITER_CONTRACT,
            "bucket_strategy": BUCKET_STRATEGY,
        }
        return request_uid, public, api_key

    async def _release_unsent(self, request_uid: str, attempt_no: int, reason: str) -> None:
        try:
            await self._rpc(
                "google_ai_release_unsent_v2",
                {
                    "p_request_uid": request_uid,
                    "p_attempt_no": int(attempt_no),
                    "p_reason": reason[:120],
                },
                allow_empty=True,
            )
        except Exception:
            pass

    async def _finalize(
        self,
        *,
        request_uid: str,
        attempt_no: int,
        usage: dict[str, int] | None,
        status: str,
        error_code: str | None = None,
    ) -> None:
        await self._rpc(
            "google_ai_finalize",
            {
                "p_request_uid": request_uid,
                "p_attempt_no": int(attempt_no),
                "p_usage_input_tokens": usage.get("input_tokens") if usage else None,
                "p_usage_output_tokens": usage.get("output_tokens") if usage else None,
                "p_usage_total_tokens": usage.get("total_tokens") if usage else None,
                "p_duration_ms": 0,
                "p_provider_status": status,
                "p_error_type": "provider" if error_code else None,
                "p_error_code": error_code,
                "p_error_message": error_code,
            },
            allow_empty=True,
        )

    @staticmethod
    def _response_text(body: Any) -> str:
        if not isinstance(body, dict):
            raise ValueError("provider response is not an object")
        candidates = body.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("provider response has no candidates")
        first = candidates[0]
        parts = ((first.get("content") or {}).get("parts") or []) if isinstance(first, dict) else []
        texts = [
            str(part.get("text"))
            for part in parts
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        if not texts:
            raise ValueError("provider response has no text")
        return "".join(texts)

    @staticmethod
    def _usage(body: Any) -> dict[str, int] | None:
        meta = body.get("usageMetadata") if isinstance(body, dict) else None
        if not isinstance(meta, dict):
            return None
        def number(name: str) -> int:
            value = meta.get(name)
            return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
        return {
            "input_tokens": number("promptTokenCount"),
            "output_tokens": number("candidatesTokenCount") + number("thoughtsTokenCount"),
            "total_tokens": number("totalTokenCount"),
        }

    async def summarize(
        self,
        *,
        note_id: str,
        project_name: str,
        author_display_name: str,
        author_roles: list[str],
        source_text: str,
        suggested_title: str = "",
        attempt_no: int = 1,
    ) -> ProcessedNote:
        prompt = (
            SUMMARY_PROMPT
            + "\nКОНТЕКСТ ПРОЕКТА:\n"
            + project_name.strip()
            + "\n\nАВТОР:\n"
            + author_display_name.strip()
            + "\nРОЛИ АВТОРА:\n"
            + _json_text(author_roles)
            + ("\nПРЕДЛОЖЕННЫЙ ЗАГОЛОВОК:\n" + suggested_title.strip() if suggested_title.strip() else "")
            + "\n\nИСХОДНЫЙ ТЕКСТ ЗАМЕТКИ:\n"
            + source_text.strip()
        )
        request_uid, public_limiter, api_key = await self._reserve(
            note_id=note_id,
            attempt_no=attempt_no,
            prompt=prompt,
        )
        try:
            await self._rpc(
                "google_ai_mark_sent",
                {"p_request_uid": request_uid, "p_attempt_no": int(attempt_no)},
                allow_empty=True,
            )
        except Exception as exc:
            await self._release_unsent(request_uid, attempt_no, "note_mark_sent_failed")
            raise NoteProcessingError(
                "NOTE_LIMITER_UNAVAILABLE",
                "Could not mark the Gemini note request as sent",
                retryable=True,
            ) from exc

        client = await self._http()
        endpoint = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            + quote(self.model, safe="-._~")
            + ":generateContent"
        )
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": MAX_OUTPUT_TOKENS,
                "responseMimeType": "application/json",
                "responseJsonSchema": SUMMARY_JSON_SCHEMA,
            },
        }
        started = time.monotonic()
        try:
            response = await client.post(
                endpoint,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "x-goog-api-key": api_key,
                    "X-Goog-Request-Params": "model=models/" + self.model,
                },
                json=body,
                timeout=180.0,
            )
        except (httpx.HTTPError, TimeoutError) as exc:
            await self._finalize(
                request_uid=request_uid,
                attempt_no=attempt_no,
                usage=None,
                status="failed",
                error_code="provider_network_error",
            )
            raise NoteProcessingError(
                "NOTE_PROCESSOR_UNAVAILABLE",
                "Gemini note processing request failed",
                retryable=True,
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            await self._finalize(
                request_uid=request_uid,
                attempt_no=attempt_no,
                usage=None,
                status="failed",
                error_code="provider_invalid_json",
            )
            raise NoteProcessingError(
                "NOTE_PROCESSOR_INVALID_RESPONSE",
                "Gemini note processor returned invalid JSON",
            ) from exc
        usage = self._usage(payload)
        if response.status_code == 429:
            try:
                await self._rpc(
                    "google_ai_report_provider_429",
                    {
                        "p_request_uid": request_uid,
                        "p_attempt_no": int(attempt_no),
                        "p_retry_after_ms": None,
                    },
                    allow_empty=True,
                )
            finally:
                await self._finalize(
                    request_uid=request_uid,
                    attempt_no=attempt_no,
                    usage=usage,
                    status="failed",
                    error_code="provider_429",
                )
            raise NoteProcessingError(
                "NOTE_PROCESSOR_CAPACITY",
                "Gemini note processing is temporarily rate limited",
                retryable=True,
            )
        if not 200 <= response.status_code < 300:
            await self._finalize(
                request_uid=request_uid,
                attempt_no=attempt_no,
                usage=usage,
                status="failed",
                error_code="http_" + str(response.status_code),
            )
            raise NoteProcessingError(
                "NOTE_PROCESSOR_UNAVAILABLE",
                "Gemini note processing was rejected by the provider",
                retryable=response.status_code >= 500,
            )
        try:
            text = self._response_text(payload).strip()
            fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.I | re.S)
            if fenced:
                text = fenced.group(1)
            value = json.loads(text)
            summary = NoteSummary.model_validate(value)
        except (ValueError, TypeError, ValidationError) as exc:
            await self._finalize(
                request_uid=request_uid,
                attempt_no=attempt_no,
                usage=usage,
                status="failed",
                error_code="response_schema_invalid",
            )
            raise NoteProcessingError(
                "NOTE_PROCESSOR_INVALID_RESPONSE",
                "Gemini note processor response did not match SummaryPayload",
            ) from exc

        await self._finalize(
            request_uid=request_uid,
            attempt_no=attempt_no,
            usage=usage,
            status="succeeded",
        )
        public_limiter["actual_tpm"] = usage["total_tokens"] if usage else None
        public_limiter["duration_ms"] = max(0, int((time.monotonic() - started) * 1000))
        return ProcessedNote(
            summary=summary,
            model=self.model,
            prompt_version=SUMMARY_PROMPT_VERSION,
            request_uid=request_uid,
            limiter=public_limiter,
        )

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None
