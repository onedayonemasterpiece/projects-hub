import copy

import pytest

from projects_hub.expert_reviews import (
    ExpertProfile,
    ExpertReviewAccessError,
    ExpertReviewAdapter,
    ExpertReviewConflict,
    expert_is_eligible,
    normalize_review_case,
)


CASE_ID = "poi_review_123"
EXPERT = "expert:one"
OTHER = "expert:other"


def case(
    *,
    revision=1,
    status="open",
    owner=EXPERT,
    visibility="private",
    workspace_id=None,
    subjects=None,
    geography=None,
    required_reviews=2,
):
    return {
        "contract_version": "poi.review_case.v1",
        "review_case_id": CASE_ID,
        "poi_id": "poi_123",
        "poi_name": "Королевские ворота",
        "conflict_id": "poi_conflict_123",
        "status": status,
        "relation": "uncertain",
        "claims": [
            {
                "claim_id": "claim_left",
                "text": "Построены в 1843 году.",
                "verification_score": 90,
                "evidence_refs": ["knowledge://evidence/left"],
            },
            {
                "claim_id": "claim_right",
                "text": "Построены в 1850 году.",
                "verification_score": 88,
                "evidence_refs": ["knowledge://evidence/right"],
            },
        ],
        "required_expertise": {
            "geography": geography or ["kaliningrad_oblast"],
            "period": [],
            "subject": subjects or ["construction"],
            "languages": [],
        },
        "required_reviews": required_reviews,
        "scope": {
            "visibility": visibility,
            "owner_sub": owner if visibility != "public" else None,
            "workspace_id": workspace_id,
        },
        "detector_suggestion": {"relation": "uncertain"},
        "case_revision": revision,
    }


def profile(subject=EXPERT, *, workspaces=(), subjects=("construction",)):
    return ExpertProfile.verified(
        subject=subject,
        geography=["kaliningrad_oblast"],
        subjects=subjects,
        workspace_ids=workspaces,
    )


class FakeProvider:
    def __init__(self, value):
        self.value = copy.deepcopy(value)
        self.calls = []
        self.receipts = {}

    async def list_cases(self, **kwargs):
        self.calls.append(("list", kwargs))
        return [copy.deepcopy(self.value)]

    async def get_case(self, review_case_id, **kwargs):
        self.calls.append(("get", review_case_id, kwargs))
        return copy.deepcopy(self.value)

    async def accept_case(self, review_case_id, **kwargs):
        self.calls.append(("accept", review_case_id, kwargs))
        self.value["status"] = "assigned"
        self.value["case_revision"] += 1
        return {"receipt_id": "accept_receipt"}

    async def resolve_case(self, review_case_id, **kwargs):
        self.calls.append(("resolve", review_case_id, kwargs))
        command_id = kwargs["command_id"]
        receipt = self.receipts.get(command_id)
        if receipt is None:
            receipt = {"receipt_id": "resolve_receipt", "command_id": command_id}
            self.receipts[command_id] = receipt
            self.value["status"] = "resolved"
            self.value["case_revision"] += 1
        return copy.deepcopy(receipt)

    async def request_research(self, review_case_id, **kwargs):
        self.calls.append(("research", review_case_id, kwargs))
        self.value["status"] = "deferred"
        self.value["case_revision"] += 1
        return {"receipt_id": "research_receipt"}


def test_normalizer_keeps_only_typed_review_contract():
    normalized = normalize_review_case(case())
    assert normalized["review_case_id"] == CASE_ID
    assert normalized["required_reviews"] == 2
    assert normalized["required_expertise"]["subject"] == ["construction"]

    broken = case()
    broken["unknown"] = "model-added"
    with pytest.raises(ValueError):
        normalize_review_case(broken)


def test_expertise_and_scope_are_both_required():
    value = case()
    assert expert_is_eligible(profile(), value)
    assert not expert_is_eligible(profile(OTHER), value)
    assert not expert_is_eligible(profile(subjects=("architecture",)), value)

    workspace_case = case(
        visibility="workspace",
        owner=None,
        workspace_id="ws-one",
    )
    assert expert_is_eligible(
        profile(workspaces=("ws-one",)),
        workspace_case,
    )
    assert not expert_is_eligible(profile(), workspace_case)


@pytest.mark.asyncio
async def test_list_filters_provider_case_against_verified_expertise():
    provider = FakeProvider(case())
    adapter = ExpertReviewAdapter(provider=provider, profile=profile())
    rows = await adapter.list_assigned()
    assert [row["review_case_id"] for row in rows] == [CASE_ID]

    blocked = ExpertReviewAdapter(
        provider=provider,
        profile=profile(subjects=("architecture",)),
    )
    assert await blocked.list_assigned() == []


@pytest.mark.asyncio
async def test_get_fails_closed_if_provider_returns_inaccessible_private_case():
    provider = FakeProvider(case(owner=OTHER))
    adapter = ExpertReviewAdapter(provider=provider, profile=profile())
    with pytest.raises(ExpertReviewAccessError):
        await adapter.get(CASE_ID)


@pytest.mark.asyncio
async def test_accept_sends_verified_expertise_snapshot_and_requires_revision():
    provider = FakeProvider(case())
    adapter = ExpertReviewAdapter(provider=provider, profile=profile())
    readback = await adapter.accept(
        CASE_ID,
        expected_revision=1,
        command_id="cmd_accept_1",
    )
    assert readback["status"] == "assigned"
    assert readback["case_revision"] == 2
    call = [item for item in provider.calls if item[0] == "accept"][0]
    snapshot = call[2]["expertise_snapshot"]
    assert snapshot["verification_state"] == "verified"
    assert snapshot["subjects"] == ["construction"]

    with pytest.raises(ExpertReviewConflict):
        await adapter.accept(
            CASE_ID,
            expected_revision=1,
            command_id="cmd_accept_stale",
        )


@pytest.mark.asyncio
async def test_resolve_requires_receipt_and_advanced_readback():
    provider = FakeProvider(case())
    adapter = ExpertReviewAdapter(provider=provider, profile=profile())
    result = await adapter.resolve(
        CASE_ID,
        expected_revision=1,
        resolution="prefer_left",
        rationale="Источник слева имеет точную страницу и атрибуцию.",
        confidence=0.8,
        command_id="cmd_resolve_1",
    )
    assert result["receipt"]["receipt_id"] == "resolve_receipt"
    assert result["case"]["status"] == "resolved"
    assert result["case"]["case_revision"] == 2


@pytest.mark.asyncio
async def test_request_research_is_first_class_typed_action():
    provider = FakeProvider(case())
    adapter = ExpertReviewAdapter(provider=provider, profile=profile())
    result = await adapter.request_research(
        CASE_ID,
        expected_revision=1,
        rationale="Нужен независимый источник до 1945 года.",
        command_id="cmd_research_1",
    )
    assert result["receipt"]["receipt_id"] == "research_receipt"
    assert result["case"]["status"] == "deferred"
    assert result["case"]["case_revision"] == 2


@pytest.mark.asyncio
async def test_resolve_rejects_invalid_resolution_before_provider_mutation():
    provider = FakeProvider(case())
    adapter = ExpertReviewAdapter(provider=provider, profile=profile())
    with pytest.raises(ValueError):
        await adapter.resolve(
            CASE_ID,
            expected_revision=1,
            resolution="invent_new_truth",
            rationale="No.",
            command_id="cmd_bad",
        )
    assert not any(call[0] == "resolve" for call in provider.calls)
