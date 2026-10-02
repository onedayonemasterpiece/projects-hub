"""Transport-agnostic expert review adapter for owning product cases.

The provider is expected to be an already authenticated, resource-bound client
for the owning service. Projects Hub validates the projection and expert policy,
but never owns the domain truth.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Protocol


_RESOLUTIONS = {
    "prefer_left",
    "prefer_right",
    "both_valid_scope",
    "both_valid_temporal",
    "unresolved",
    "needs_more_sources",
    "wrong_poi_link",
}
_STATUSES = {
    "open",
    "assigned",
    "in_review",
    "resolved",
    "deferred",
    "superseded",
}
_RELATIONS = {
    "contradiction",
    "scope_difference",
    "temporal_sequence",
    "source_disagreement",
    "uncertain",
    "identity_ambiguity",
}
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,299}$")


class ExpertReviewError(ValueError):
    pass


class ExpertReviewAccessError(PermissionError):
    pass


class ExpertReviewConflict(RuntimeError):
    pass


class ExpertReviewProvider(Protocol):
    async def list_cases(
        self,
        *,
        actor_sub: str,
        workspace_ids: tuple[str, ...],
        statuses: tuple[str, ...],
    ) -> list[dict[str, Any]]: ...

    async def get_case(
        self,
        review_case_id: str,
        *,
        actor_sub: str,
        workspace_ids: tuple[str, ...],
    ) -> dict[str, Any]: ...

    async def accept_case(
        self,
        review_case_id: str,
        *,
        actor_sub: str,
        expertise_snapshot: dict[str, Any],
        expected_revision: int,
        command_id: str,
    ) -> dict[str, Any]: ...

    async def resolve_case(
        self,
        review_case_id: str,
        *,
        actor_sub: str,
        expected_revision: int,
        resolution: str,
        rationale: str,
        confidence: float | None,
        command_id: str,
    ) -> dict[str, Any]: ...

    async def request_research(
        self,
        review_case_id: str,
        *,
        actor_sub: str,
        expected_revision: int,
        rationale: str,
        command_id: str,
    ) -> dict[str, Any]: ...


def _clean_set(values: Iterable[str], *, maximum: int = 100) -> frozenset[str]:
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        if len(text) > 160:
            raise ExpertReviewError("expertise_value_too_long")
        if text not in result:
            result.append(text)
        if len(result) > maximum:
            raise ExpertReviewError("too_many_expertise_values")
    return frozenset(result)


def _id(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 300 or not _TOKEN.fullmatch(text):
        raise ExpertReviewError(f"{field}_invalid")
    return text


@dataclass(frozen=True)
class ExpertProfile:
    subject: str
    verification_state: str
    geography: frozenset[str] = frozenset()
    periods: frozenset[str] = frozenset()
    subjects: frozenset[str] = frozenset()
    languages: frozenset[str] = frozenset()
    workspace_ids: frozenset[str] = frozenset()
    institutional_roles: frozenset[str] = frozenset()

    @classmethod
    def verified(
        cls,
        *,
        subject: str,
        geography: Iterable[str] = (),
        periods: Iterable[str] = (),
        subjects: Iterable[str] = (),
        languages: Iterable[str] = (),
        workspace_ids: Iterable[str] = (),
        institutional_roles: Iterable[str] = (),
    ) -> "ExpertProfile":
        return cls(
            subject=_id(subject, "subject"),
            verification_state="verified",
            geography=_clean_set(geography),
            periods=_clean_set(periods),
            subjects=_clean_set(subjects),
            languages=_clean_set(languages),
            workspace_ids=_clean_set(workspace_ids),
            institutional_roles=_clean_set(institutional_roles),
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "verification_state": self.verification_state,
            "geography": sorted(self.geography),
            "periods": sorted(self.periods),
            "subjects": sorted(self.subjects),
            "languages": sorted(self.languages),
            "institutional_roles": sorted(self.institutional_roles),
        }


def normalize_review_case(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExpertReviewError("review_case_invalid")
    allowed = {
        "contract_version",
        "review_case_id",
        "poi_id",
        "poi_name",
        "conflict_id",
        "status",
        "relation",
        "claims",
        "required_expertise",
        "required_reviews",
        "scope",
        "detector_suggestion",
        "case_revision",
    }
    if set(value) - allowed:
        raise ExpertReviewError("review_case_unknown_field")
    if value.get("contract_version") != "poi.review_case.v1":
        raise ExpertReviewError("review_case_contract_invalid")

    review_case_id = _id(value.get("review_case_id"), "review_case_id")
    poi_id = _id(value.get("poi_id"), "poi_id")
    conflict_id = value.get("conflict_id")
    if conflict_id is not None:
        conflict_id = _id(conflict_id, "conflict_id")
    status = str(value.get("status") or "")
    if status not in _STATUSES:
        raise ExpertReviewError("review_case_status_invalid")
    relation = str(value.get("relation") or "")
    if relation not in _RELATIONS:
        raise ExpertReviewError("review_case_relation_invalid")

    claims_raw = value.get("claims")
    if not isinstance(claims_raw, list) or not 2 <= len(claims_raw) <= 4:
        raise ExpertReviewError("review_case_claims_invalid")
    claims: list[dict[str, Any]] = []
    for raw in claims_raw:
        if not isinstance(raw, dict) or set(raw) - {
            "claim_id", "text", "verification_score", "evidence_refs"
        }:
            raise ExpertReviewError("review_case_claim_invalid")
        claim_id = _id(raw.get("claim_id"), "claim_id")
        text = str(raw.get("text") or "").strip()
        if not text or len(text) > 1000:
            raise ExpertReviewError("review_case_claim_text_invalid")
        score = raw.get("verification_score")
        if score is not None:
            try:
                score = int(score)
            except (TypeError, ValueError):
                raise ExpertReviewError("review_case_score_invalid") from None
            if not 0 <= score <= 100:
                raise ExpertReviewError("review_case_score_invalid")
        evidence = raw.get("evidence_refs")
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 50:
            raise ExpertReviewError("review_case_evidence_invalid")
        evidence_refs = []
        for ref in evidence:
            ref = str(ref).strip()
            if not ref or len(ref) > 1000:
                raise ExpertReviewError("review_case_evidence_invalid")
            evidence_refs.append(ref)
        claims.append(
            {
                "claim_id": claim_id,
                "text": text,
                "verification_score": score,
                "evidence_refs": evidence_refs,
            }
        )

    required = value.get("required_expertise") or {}
    if not isinstance(required, dict) or set(required) - {
        "geography", "period", "subject", "languages"
    }:
        raise ExpertReviewError("required_expertise_invalid")
    required_expertise = {
        "geography": sorted(_clean_set(required.get("geography") or ())),
        "period": sorted(_clean_set(required.get("period") or ())),
        "subject": sorted(_clean_set(required.get("subject") or ())),
        "languages": sorted(_clean_set(required.get("languages") or ())),
    }
    try:
        required_reviews = int(value.get("required_reviews", 1))
        case_revision = int(value.get("case_revision", 1))
    except (TypeError, ValueError):
        raise ExpertReviewError("review_case_revision_invalid") from None
    if not 1 <= required_reviews <= 3 or case_revision < 1:
        raise ExpertReviewError("review_case_revision_invalid")

    scope = value.get("scope") or {}
    if not isinstance(scope, dict) or set(scope) - {
        "visibility", "owner_sub", "workspace_id"
    }:
        raise ExpertReviewError("review_case_scope_invalid")
    visibility = str(scope.get("visibility") or "")
    if visibility not in {"private", "workspace", "public"}:
        raise ExpertReviewError("review_case_scope_invalid")
    owner_sub = (
        _id(scope["owner_sub"], "owner_sub")
        if scope.get("owner_sub") is not None
        else None
    )
    workspace_id = (
        _id(scope["workspace_id"], "workspace_id")
        if scope.get("workspace_id") is not None
        else None
    )
    if visibility == "private" and not owner_sub:
        raise ExpertReviewError("review_case_scope_invalid")
    if visibility == "workspace" and not workspace_id:
        raise ExpertReviewError("review_case_scope_invalid")

    return {
        "contract_version": "poi.review_case.v1",
        "review_case_id": review_case_id,
        "poi_id": poi_id,
        "poi_name": (
            str(value.get("poi_name")).strip()[:500]
            if value.get("poi_name") is not None
            else None
        ),
        "conflict_id": conflict_id,
        "status": status,
        "relation": relation,
        "claims": claims,
        "required_expertise": required_expertise,
        "required_reviews": required_reviews,
        "scope": {
            "visibility": visibility,
            "owner_sub": owner_sub,
            "workspace_id": workspace_id,
        },
        "detector_suggestion": value.get("detector_suggestion"),
        "case_revision": case_revision,
    }


def _scope_allowed(profile: ExpertProfile, case: dict[str, Any]) -> bool:
    scope = case["scope"]
    if scope["visibility"] == "public":
        return True
    if scope["visibility"] == "private":
        return scope.get("owner_sub") == profile.subject
    if scope["visibility"] == "workspace":
        return scope.get("workspace_id") in profile.workspace_ids
    return False


def expert_is_eligible(profile: ExpertProfile, case: dict[str, Any]) -> bool:
    if profile.verification_state != "verified":
        return False
    case = normalize_review_case(case)
    if not _scope_allowed(profile, case):
        return False
    required = case["required_expertise"]
    return (
        set(required["geography"]).issubset(profile.geography)
        and set(required["period"]).issubset(profile.periods)
        and set(required["subject"]).issubset(profile.subjects)
        and set(required["languages"]).issubset(profile.languages)
    )


def _command_id(value: str) -> str:
    return _id(value, "command_id")


class ExpertReviewAdapter:
    def __init__(
        self,
        *,
        provider: ExpertReviewProvider,
        profile: ExpertProfile,
    ) -> None:
        self.provider = provider
        self.profile = profile

    @property
    def _workspaces(self) -> tuple[str, ...]:
        return tuple(sorted(self.profile.workspace_ids))

    def _require_eligible(self, case: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_review_case(case)
        if not expert_is_eligible(self.profile, normalized):
            raise ExpertReviewAccessError("expert_not_eligible_for_case")
        return normalized

    async def list_assigned(
        self,
        statuses: Iterable[str] = ("assigned", "in_review", "open"),
    ) -> list[dict[str, Any]]:
        normalized_statuses = tuple(dict.fromkeys(str(v) for v in statuses))
        if not normalized_statuses or any(
            value not in _STATUSES for value in normalized_statuses
        ):
            raise ExpertReviewError("review_status_filter_invalid")
        cases = await self.provider.list_cases(
            actor_sub=self.profile.subject,
            workspace_ids=self._workspaces,
            statuses=normalized_statuses,
        )
        output = []
        for value in cases:
            try:
                output.append(self._require_eligible(value))
            except ExpertReviewAccessError:
                continue
        return output

    async def get(self, review_case_id: str) -> dict[str, Any]:
        review_case_id = _id(review_case_id, "review_case_id")
        case = await self.provider.get_case(
            review_case_id,
            actor_sub=self.profile.subject,
            workspace_ids=self._workspaces,
        )
        normalized = self._require_eligible(case)
        if normalized["review_case_id"] != review_case_id:
            raise ExpertReviewConflict("review_case_readback_mismatch")
        return normalized

    async def accept(
        self,
        review_case_id: str,
        *,
        expected_revision: int,
        command_id: str,
    ) -> dict[str, Any]:
        case = await self.get(review_case_id)
        if case["case_revision"] != expected_revision:
            raise ExpertReviewConflict("stale_review_case_revision")
        await self.provider.accept_case(
            review_case_id,
            actor_sub=self.profile.subject,
            expertise_snapshot=self.profile.snapshot(),
            expected_revision=expected_revision,
            command_id=_command_id(command_id),
        )
        readback = await self.get(review_case_id)
        if readback["case_revision"] < expected_revision:
            raise ExpertReviewConflict("review_case_readback_regressed")
        return readback

    async def resolve(
        self,
        review_case_id: str,
        *,
        expected_revision: int,
        resolution: str,
        rationale: str,
        command_id: str,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        if resolution not in _RESOLUTIONS:
            raise ExpertReviewError("resolution_invalid")
        rationale = str(rationale or "").strip()
        if not 3 <= len(rationale) <= 4000:
            raise ExpertReviewError("rationale_invalid")
        if confidence is not None:
            confidence = float(confidence)
            if not 0 <= confidence <= 1:
                raise ExpertReviewError("confidence_invalid")

        case = await self.get(review_case_id)
        if case["case_revision"] != expected_revision:
            raise ExpertReviewConflict("stale_review_case_revision")

        receipt = await self.provider.resolve_case(
            review_case_id,
            actor_sub=self.profile.subject,
            expected_revision=expected_revision,
            resolution=resolution,
            rationale=rationale,
            confidence=confidence,
            command_id=_command_id(command_id),
        )
        if not isinstance(receipt, dict) or not receipt.get("receipt_id"):
            raise ExpertReviewConflict("review_resolution_receipt_missing")

        readback = await self.provider.get_case(
            review_case_id,
            actor_sub=self.profile.subject,
            workspace_ids=self._workspaces,
        )
        normalized = normalize_review_case(readback)
        if normalized["review_case_id"] != review_case_id:
            raise ExpertReviewConflict("review_case_readback_mismatch")
        if normalized["case_revision"] <= expected_revision:
            raise ExpertReviewConflict("review_case_readback_not_advanced")
        return {
            "receipt": receipt,
            "case": self._require_eligible(normalized)
            if normalized["status"] not in {"resolved", "superseded"}
            else normalized,
        }

    async def request_research(
        self,
        review_case_id: str,
        *,
        expected_revision: int,
        rationale: str,
        command_id: str,
    ) -> dict[str, Any]:
        rationale = str(rationale or "").strip()
        if not 3 <= len(rationale) <= 4000:
            raise ExpertReviewError("rationale_invalid")
        case = await self.get(review_case_id)
        if case["case_revision"] != expected_revision:
            raise ExpertReviewConflict("stale_review_case_revision")
        receipt = await self.provider.request_research(
            review_case_id,
            actor_sub=self.profile.subject,
            expected_revision=expected_revision,
            rationale=rationale,
            command_id=_command_id(command_id),
        )
        if not isinstance(receipt, dict) or not receipt.get("receipt_id"):
            raise ExpertReviewConflict("research_receipt_missing")
        readback = await self.provider.get_case(
            review_case_id,
            actor_sub=self.profile.subject,
            workspace_ids=self._workspaces,
        )
        normalized = normalize_review_case(readback)
        if normalized["case_revision"] <= expected_revision:
            raise ExpertReviewConflict("review_case_readback_not_advanced")
        return {"receipt": receipt, "case": normalized}
