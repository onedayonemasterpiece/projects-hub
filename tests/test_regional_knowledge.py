from pathlib import Path
from types import SimpleNamespace

import pytest

from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.live_resources import ConversationScope
from projects_hub.regional_knowledge import (
    RegionalKnowledgeAdapter,
    RegionalKnowledgeContractError,
    RegionalKnowledgeUnavailable,
    normalize_knowledge_search,
)
from projects_hub.store import DurableStore, StoreError


class KnowledgeProvider:
    def __init__(self, result=None, error=None):
        self.result = result or {
            "mode": "hybrid",
            "evidence": [
                {
                    "id": "chunk-1",
                    "title": "Кёнигсберг. Источник",
                    "text": "Подтверждённый фрагмент из источника.",
                    "url": "https://knowledge.example/evidence/chunk-1",
                    "metadata": {
                        "document_id": "doc-1",
                        "pages": ["12", "13"],
                        "illustrations": [{"illustration_id": "ill-1"}],
                        "footnotes": [{"region_id": "foot-1"}],
                    },
                }
            ],
        }
        self.error = error
        self.calls = []

    async def search_evidence(
        self,
        *,
        query,
        actor_sub,
        workspace_id,
        max_evidence,
    ):
        self.calls.append(
            {
                "query": query,
                "actor_sub": actor_sub,
                "workspace_id": workspace_id,
                "max_evidence": max_evidence,
            }
        )
        if self.error is not None:
            raise self.error
        return self.result


def _live_context(tmp_path: Path, factory=None):
    store = DurableStore(tmp_path)
    boot = store.ensure_dev_workspace("Knowledge")
    actor_id = boot["actor"]["id"]
    workspace_id = boot["workspace"]["id"]
    project_id = boot["projects"][0]["id"]
    conversation = store.create_conversation(actor_id, workspace_id, project_id)
    binding = ConversationScope(
        workspace_id,
        actor_id,
        conversation["id"],
    ).resource_binding()
    adapter = ProjectsHubLiveAdapter(
        store,
        regional_knowledge_factory=factory,
    )
    initialized = adapter.initialize(
        resource_id=binding,
        actor={"subject": actor_id, "tenant_id": workspace_id},
        model="gemini-3.8-live",
        conversation_id=conversation["id"],
    )
    session = SimpleNamespace(state=initialized["state"])
    return store, actor_id, workspace_id, adapter, initialized, session


def test_knowledge_result_preserves_evidence_provenance():
    value = normalize_knowledge_search(
        {
            "mode": "lexical_degraded",
            "evidence": [
                {
                    "id": "chunk-1",
                    "title": "Источник",
                    "text": "Факт.",
                    "url": "https://knowledge.example/evidence/chunk-1",
                    "metadata": {
                        "document_id": "doc-1",
                        "pages": ["42"],
                        "illustrations": [{"illustration_id": "ill-42"}],
                    },
                }
            ],
        },
        max_evidence=3,
    )
    assert value["mode"] == "lexical_degraded"
    assert value["evidence"][0]["metadata"]["document_id"] == "doc-1"
    assert value["evidence"][0]["metadata"]["pages"] == ["42"]
    assert value["evidence"][0]["metadata"]["illustrations"] == [
        {"illustration_id": "ill-42"}
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {
            "mode": "hybrid",
            "evidence": [
                {
                    "id": "chunk",
                    "title": "Source",
                    "text": "Evidence",
                    "url": "http://knowledge.example/evidence/chunk",
                }
            ],
        },
        {"mode": "hybrid", "evidence": [], "unexpected": True},
        {
            "mode": "hybrid",
            "evidence": [
                {
                    "id": "chunk",
                    "title": "Source",
                    "text": "x" * 8001,
                    "url": "https://knowledge.example/evidence/chunk",
                }
            ],
        },
    ],
)
def test_knowledge_result_rejects_untrusted_or_unbounded_shapes(payload):
    with pytest.raises(RegionalKnowledgeContractError):
        normalize_knowledge_search(payload, max_evidence=3)


@pytest.mark.asyncio
async def test_live_knowledge_tool_is_absent_without_user_delegation(tmp_path: Path):
    store, _actor_id, _workspace_id, adapter, initialized, session = _live_context(
        tmp_path
    )
    try:
        names = {
            item["name"] for item in initialized["configuration"]["functions"]
        }
        assert "knowledge_search" not in names
        assert initialized["response"]["regional_knowledge_enabled"] is False

        with pytest.raises(StoreError) as raised:
            await adapter.execute_tool(
                session,
                {
                    "name": "knowledge_search",
                    "args": {"query": "Кафедральный собор"},
                },
            )
        assert raised.value.code == "TOOL_NOT_AVAILABLE"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_live_knowledge_tool_uses_current_actor_workspace_and_evidence(
    tmp_path: Path,
):
    provider = KnowledgeProvider()
    holder = {}

    def factory(actor_id, workspace_id):
        holder["actor_id"] = actor_id
        holder["workspace_id"] = workspace_id
        return RegionalKnowledgeAdapter(
            provider=provider,
            actor_sub=actor_id,
            workspace_id=workspace_id,
        )

    store, actor_id, workspace_id, adapter, initialized, session = _live_context(
        tmp_path,
        factory,
    )
    try:
        names = {
            item["name"] for item in initialized["configuration"]["functions"]
        }
        assert "knowledge_search" in names
        assert initialized["response"]["regional_knowledge_enabled"] is True

        result = await adapter.execute_tool(
            session,
            {
                "name": "knowledge_search",
                "args": {
                    "query": "  история Кафедрального собора  ",
                    "max_evidence": 99,
                },
            },
        )
        assert holder == {
            "actor_id": actor_id,
            "workspace_id": workspace_id,
        }
        assert provider.calls == [
            {
                "query": "история Кафедрального собора",
                "actor_sub": actor_id,
                "workspace_id": workspace_id,
                "max_evidence": 5,
            }
        ]
        assert result["evidence"][0]["metadata"]["pages"] == ["12", "13"]
        assert result["evidence"][0]["url"].startswith("https://")
    finally:
        store.close()


def test_live_knowledge_factory_cannot_cross_actor_boundary(tmp_path: Path):
    provider = KnowledgeProvider()

    def wrong_actor(_actor_id, workspace_id):
        return RegionalKnowledgeAdapter(
            provider=provider,
            actor_sub="someone-else",
            workspace_id=workspace_id,
        )

    store = DurableStore(tmp_path)
    try:
        boot = store.ensure_dev_workspace("Knowledge")
        actor_id = boot["actor"]["id"]
        workspace_id = boot["workspace"]["id"]
        project_id = boot["projects"][0]["id"]
        conversation = store.create_conversation(
            actor_id,
            workspace_id,
            project_id,
        )
        binding = ConversationScope(
            workspace_id,
            actor_id,
            conversation["id"],
        ).resource_binding()
        adapter = ProjectsHubLiveAdapter(
            store,
            regional_knowledge_factory=wrong_actor,
        )
        with pytest.raises(StoreError) as raised:
            adapter.initialize(
                resource_id=binding,
                actor={"subject": actor_id, "tenant_id": workspace_id},
                model="gemini-3.8-live",
                conversation_id=conversation["id"],
            )
        assert raised.value.code == "FORBIDDEN"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_knowledge_provider_failure_degrades_only_capability(tmp_path: Path):
    provider = KnowledgeProvider(error=RuntimeError("dependency down"))

    def factory(actor_id, workspace_id):
        return RegionalKnowledgeAdapter(
            provider=provider,
            actor_sub=actor_id,
            workspace_id=workspace_id,
        )

    store, _actor_id, _workspace_id, adapter, _initialized, session = _live_context(
        tmp_path,
        factory,
    )
    try:
        with pytest.raises(StoreError) as raised:
            await adapter.execute_tool(
                session,
                {
                    "name": "knowledge_search",
                    "args": {"query": "история"},
                },
            )
        assert raised.value.code == "KNOWLEDGE_UNAVAILABLE"
        assert store.list_projects(
            session.state["actor_id"],
            session.state["workspace_id"],
        )
    finally:
        store.close()
