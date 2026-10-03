"""Projects Hub boundary for the user-authorized Regional Knowledge resource.

This module intentionally contains no OAuth token acquisition and no Knowledge HTTP/MCP
transport. A concrete provider must already be bound to the current user's delegated
Knowledge grant. That keeps Projects Hub from forwarding its own bearer token or using
a service role to impersonate the user.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Protocol
from urllib.parse import urlsplit


_MAX_QUERY_CHARS = 1_000
_MAX_EVIDENCE = 5
_MAX_ITEM_TEXT_CHARS = 8_000
_MAX_TOTAL_TEXT_CHARS = 24_000
_MAX_METADATA_BYTES = 12_000


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
    ) -> dict[str, Any]: ...


def _bounded_scalar_tree(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        raise RegionalKnowledgeContractError("knowledge_metadata_too_deep")
    if value is None or isinstance(value, (bool, int, float)):
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
    ).encode("utf-8")
    if len(encoded) > _MAX_METADATA_BYTES:
        raise RegionalKnowledgeContractError("knowledge_metadata_too_large")
    return result


def _text(value: Any, field: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise RegionalKnowledgeContractError(f"{field}_invalid")
    result = value.strip()
    if (not result and not allow_empty) or len(result) > maximum:
        raise RegionalKnowledgeContractError(f"{field}_invalid")
    return result


def normalize_knowledge_search(
    value: Any,
    *,
    max_evidence: int,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RegionalKnowledgeContractError("knowledge_result_invalid")
    if set(value) - {"evidence", "mode"}:
        raise RegionalKnowledgeContractError("knowledge_result_unknown_field")

    mode = str(value.get("mode") or "")
    if mode not in {"hybrid", "lexical_degraded"}:
        raise RegionalKnowledgeContractError("knowledge_mode_invalid")

    raw_evidence = value.get("evidence")
    if not isinstance(raw_evidence, list) or len(raw_evidence) > max_evidence:
        raise RegionalKnowledgeContractError("knowledge_evidence_count_invalid")

    total_text = 0
    evidence: list[dict[str, Any]] = []
    for raw in raw_evidence:
        if not isinstance(raw, dict):
            raise RegionalKnowledgeContractError("knowledge_evidence_invalid")
        if set(raw) - {"id", "title", "text", "url", "metadata"}:
            raise RegionalKnowledgeContractError("knowledge_evidence_unknown_field")
        item_id = _text(raw.get("id"), "knowledge_evidence_id", 500)
        title = _text(raw.get("title"), "knowledge_evidence_title", 1_000)
        body = _text(raw.get("text"), "knowledge_evidence_text", _MAX_ITEM_TEXT_CHARS)
        total_text += len(body)
        if total_text > _MAX_TOTAL_TEXT_CHARS:
            raise RegionalKnowledgeContractError("knowledge_evidence_text_too_large")

        url = _text(raw.get("url"), "knowledge_evidence_url", 2_048)
        parsed = urlsplit(url)
        if (
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

    return {"evidence": evidence, "mode": mode}


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
    ) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query or len(query) > _MAX_QUERY_CHARS:
            raise RegionalKnowledgeInputError("knowledge_query_invalid")
        try:
            limit = int(max_evidence)
        except (TypeError, ValueError):
            raise RegionalKnowledgeInputError("knowledge_max_evidence_invalid") from None
        limit = max(1, min(limit, _MAX_EVIDENCE))

        try:
            result = await self.provider.search_evidence(
                query=query,
                actor_sub=self.actor_sub,
                workspace_id=self.workspace_id,
                max_evidence=limit,
            )
        except RegionalKnowledgeAccessError:
            raise
        except PermissionError as exc:
            raise RegionalKnowledgeAccessError("knowledge_access_denied") from exc
        except (RegionalKnowledgeInputError, RegionalKnowledgeContractError):
            raise
        except Exception as exc:
            raise RegionalKnowledgeUnavailable("regional_knowledge_unavailable") from exc

        return normalize_knowledge_search(result, max_evidence=limit)
