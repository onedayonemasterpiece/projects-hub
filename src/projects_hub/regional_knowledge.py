"""Projects Hub boundary for the user-authorized Regional Knowledge resource.

This module intentionally contains no OAuth token acquisition and no Knowledge HTTP/MCP
transport. A concrete provider must already be bound to the current user's delegated
Knowledge grant. That keeps Projects Hub from forwarding its own bearer token or using
a service role to impersonate the user.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
import time
from uuid import uuid4
from typing import Any, Protocol
from urllib.parse import urlsplit


_MAX_QUERY_CHARS = 1_000
_MAX_EVIDENCE = 5
_MAX_ITEM_TEXT_CHARS = 8_000
_MAX_TOTAL_TEXT_CHARS = 24_000
_MAX_METADATA_BYTES = 12_000
KNOWLEDGE_CODEC = 'rkb.knowledge-search.v2'


@dataclass(frozen=True)
class KnowledgeReadContext:
    request_id: str
    turn_generation: int
    deadline_monotonic: float
    min_data_seq: int = 0

    def __post_init__(self):
        if (type(self.request_id) is not str or not 1<=len(self.request_id)<=256
                or type(self.turn_generation) is not int or self.turn_generation<0
                or type(self.min_data_seq) is not int or self.min_data_seq<0
                or type(self.deadline_monotonic) not in (int,float) or not math.isfinite(self.deadline_monotonic)):
            raise RegionalKnowledgeInputError('knowledge_read_context_invalid')


class RegionalKnowledgeInputError(ValueError):
    pass


class RegionalKnowledgeContractError(ValueError):
    pass


class RegionalKnowledgeAccessError(PermissionError):
    pass


class RegionalKnowledgeUnavailable(RuntimeError):
    pass


class RegionalKnowledgeProvider(Protocol):
    """Authenticated provider for exactly one Regional Knowledge resource."""

    async def search_evidence(
        self,
        *,
        query: str,
        actor_sub: str,
        workspace_id: str,
        max_evidence: int,
        context: KnowledgeReadContext,
    ) -> dict[str, Any]: ...


def _bounded_scalar_tree(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        raise RegionalKnowledgeContractError("knowledge_metadata_too_deep")
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value,float) and not math.isfinite(value):
            raise RegionalKnowledgeContractError('knowledge_metadata_nonfinite')
        return value
    if isinstance(value, str):
        if len(value) > 3_000:
            raise RegionalKnowledgeContractError("knowledge_metadata_string_too_large")
        return value
    if isinstance(value, list):
        if len(value) > 64:
            raise RegionalKnowledgeContractError("knowledge_metadata_list_too_large")
        return [_bounded_scalar_tree(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > 64:
            raise RegionalKnowledgeContractError("knowledge_metadata_object_too_large")
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if not key or len(key) > 160:
                raise RegionalKnowledgeContractError("knowledge_metadata_key_invalid")
            result[key] = _bounded_scalar_tree(item, depth=depth + 1)
        return result
    raise RegionalKnowledgeContractError("knowledge_metadata_type_invalid")


def _bounded_metadata(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RegionalKnowledgeContractError("knowledge_metadata_invalid")
    result = _bounded_scalar_tree(value)
    encoded = json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_METADATA_BYTES:
        raise RegionalKnowledgeContractError("knowledge_metadata_too_large")
    return result


def _text(value: Any, field: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise RegionalKnowledgeContractError(f"{field}_invalid")
    result = value.strip()
    if (not result and not allow_empty) or len(value) > maximum:
        raise RegionalKnowledgeContractError(f"{field}_invalid")
    return value


def normalize_knowledge_search(
    value: Any,
    *,
    max_evidence: int,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RegionalKnowledgeContractError("knowledge_result_invalid")
    fields={'codec','request_id','turn_generation','status','reason','mode','evidence','min_data_seq',
        'observed_data_seq','observed_projection_seq','prior_write_pending','context_gap','continuation'}
    if set(value) != fields or value['codec'] != KNOWLEDGE_CODEC:
        raise RegionalKnowledgeContractError("knowledge_result_unknown_field")
    try:size=len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode())
    except (ValueError,TypeError,RecursionError):
        raise RegionalKnowledgeContractError('knowledge_response_invalid') from None
    if size>256*1024:
        raise RegionalKnowledgeContractError('knowledge_response_too_large')
    status=value['status']
    if status not in {'ok','degraded','unavailable','cancelled','invalid_query','denied'}:
        raise RegionalKnowledgeContractError('knowledge_status_invalid')
    for field in ('turn_generation','min_data_seq','observed_data_seq','observed_projection_seq'):
        if value[field] is None and field.startswith('observed_'):
            continue
        if type(value[field]) is not int or value[field]<0:
            raise RegionalKnowledgeContractError('knowledge_freshness_invalid')
    if type(value['request_id']) is not str or not 1<=len(value['request_id'])<=256:
        raise RegionalKnowledgeContractError('knowledge_request_invalid')
    if any(type(value[field]) is not bool for field in ('context_gap','prior_write_pending')):
        raise RegionalKnowledgeContractError('knowledge_freshness_invalid')
    reason=value['reason']
    if (status=='ok' and reason is not None or status!='ok' and
            (type(reason) is not str or not 1<=len(reason)<=128)):
        raise RegionalKnowledgeContractError('knowledge_reason_invalid')

    mode = value['mode']
    if mode not in {"hybrid", "lexical_degraded",None}:
        raise RegionalKnowledgeContractError("knowledge_mode_invalid")

    raw_evidence = value.get("evidence")
    if not isinstance(raw_evidence, list) or len(raw_evidence) > max_evidence:
        raise RegionalKnowledgeContractError("knowledge_evidence_count_invalid")
    if status in {'unavailable','cancelled','invalid_query','denied'} and (raw_evidence or mode is not None):
        raise RegionalKnowledgeContractError('knowledge_failure_cannot_be_empty_success')
    if status in {'ok','degraded'} and mode is None:
        raise RegionalKnowledgeContractError('knowledge_mode_invalid')
    if ((status=='ok' and (mode!='hybrid' or value['context_gap'] or value['prior_write_pending']))
            or status in {'ok','degraded'} and (value['observed_data_seq'] is None or value['observed_projection_seq'] is None
            or min(value['observed_data_seq'],value['observed_projection_seq'])<value['min_data_seq'])):
        raise RegionalKnowledgeContractError('knowledge_freshness_invalid')
    if (value['context_gap'] or value['prior_write_pending']) and raw_evidence:
        raise RegionalKnowledgeContractError('knowledge_context_gap_has_evidence')

    total_text = 0
    evidence: list[dict[str, Any]] = []
    for raw in raw_evidence:
        if not isinstance(raw, dict):
            raise RegionalKnowledgeContractError("knowledge_evidence_invalid")
        if set(raw) - {"id", "title", "text", "url", "metadata"}:
            raise RegionalKnowledgeContractError("knowledge_evidence_unknown_field")
        item_id = _text(raw.get("id"), "knowledge_evidence_id", 2048)
        title = _text(raw.get("title"), "knowledge_evidence_title", 1_000)
        _text(raw.get("text"), "knowledge_evidence_text", _MAX_ITEM_TEXT_CHARS)
        body = raw['text']
        total_text += len(body)
        if total_text > _MAX_TOTAL_TEXT_CHARS:
            raise RegionalKnowledgeContractError("knowledge_evidence_text_too_large")

        url = _text(raw.get("url"), "knowledge_evidence_url", 2_048)
        parsed = urlsplit(url)
        if re.fullmatch(r'urn:rkb:(passage|story|variant):[A-Za-z0-9._:%~-]+',url) is None and (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            raise RegionalKnowledgeContractError("knowledge_evidence_url_invalid")

        item: dict[str, Any] = {
            "id": item_id,
            "title": title,
            "text": body,
            "url": url,
        }
        metadata = _bounded_metadata(raw.get("metadata"))
        if metadata is not None:
            item["metadata"] = metadata
        evidence.append(item)

    continuation=_bounded_metadata(value['continuation'])
    return {**value,'evidence':evidence,'continuation':continuation}


@dataclass(frozen=True, slots=True)
class RegionalKnowledgeAdapter:
    """Actor/workspace-bound read capability for Mira."""

    provider: RegionalKnowledgeProvider
    actor_sub: str
    workspace_id: str

    def __post_init__(self) -> None:
        if not self.actor_sub or len(self.actor_sub) > 300:
            raise RegionalKnowledgeInputError("knowledge_actor_invalid")
        if not self.workspace_id or len(self.workspace_id) > 300:
            raise RegionalKnowledgeInputError("knowledge_workspace_invalid")

    async def search(
        self,
        query: str,
        *,
        max_evidence: int = 3,
        context: KnowledgeReadContext | None = None,
    ) -> dict[str, Any]:
        if type(query) is not str or not query.strip() or len(query) > _MAX_QUERY_CHARS or len(query.encode())>4096:
            raise RegionalKnowledgeInputError("knowledge_query_invalid")
        try:
            if type(max_evidence) is not int:raise ValueError
            limit = max_evidence
        except (TypeError, ValueError):
            raise RegionalKnowledgeInputError("knowledge_max_evidence_invalid") from None
        limit = max(1, min(limit, _MAX_EVIDENCE))
        context=context or KnowledgeReadContext(uuid4().hex,0,time.monotonic()+2)
        if time.monotonic()>=context.deadline_monotonic:
            raise RegionalKnowledgeUnavailable('knowledge_deadline_exceeded')

        try:
            result = await self.provider.search_evidence(
                query=query,
                actor_sub=self.actor_sub,
                workspace_id=self.workspace_id,
                max_evidence=limit,
                context=context,
            )
        except RegionalKnowledgeAccessError:
            raise
        except PermissionError as exc:
            raise RegionalKnowledgeAccessError("knowledge_access_denied") from exc
        except (RegionalKnowledgeInputError, RegionalKnowledgeContractError):
            raise
        except Exception as exc:
            raise RegionalKnowledgeUnavailable("regional_knowledge_unavailable") from exc

        normalized=normalize_knowledge_search(result,max_evidence=limit)
        if (normalized['request_id']!=context.request_id or normalized['turn_generation']!=context.turn_generation
                or normalized['min_data_seq']!=context.min_data_seq or time.monotonic()>=context.deadline_monotonic):
            raise RegionalKnowledgeUnavailable('knowledge_context_gap')
        return normalized
