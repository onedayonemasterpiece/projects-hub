from __future__ import annotations

import asyncio

import pytest

from projects_hub.analytics_client import AnalyticsBridgeClient, AnalyticsBridgeError


class CatalogClient(AnalyticsBridgeClient):
    def __init__(self, models):
        super().__init__(command="/does/not/matter")
        self.models = models
        self.calls = []

    async def safe_council_available(self) -> bool:
        return True

    async def _call(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "list_models":
            return {"status": "ok", "models": list(self.models)}
        if name == "council_run":
            return {"status": "running", "taskId": "dvt_" + "4" * 32}
        raise AssertionError(name)


def _model(selection: str, *, free: bool = True, toolcall: bool = True):
    return {
        "id": selection,
        "selection": selection,
        "provider": "opencode",
        "free": free,
        "routingRecommended": True,
        "capabilities": {"toolcall": toolcall, "reasoning": True},
    }


def test_council_selects_two_current_free_models_from_live_catalog() -> None:
    client = CatalogClient(
        [
            _model("opencode/obsolete-free"),
            _model("opencode/big-pickle"),
            _model("opencode/nemotron-3.5-lightning-free"),
            _model("opencode/nemotron-3-ultra-free"),
            _model("opencode/paid", free=False),
            _model("opencode/no-tools", toolcall=False),
        ]
    )
    result = asyncio.run(
        client.council(
            prompt="Review.",
            evidence_bundle='{"sources":[]}',
            request_key="analysis:test-council-catalog",
        )
    )
    assert result["status"] == "running"
    assert [name for name, _args in client.calls] == ["list_models", "council_run"]
    args = client.calls[-1][1]
    assert args["tier"] == "free"
    assert args["rounds"] == 2
    assert args["context_mode"] == "provided_only"
    assert args["participants"] == [
        {"provider": "opencode", "model": "opencode/nemotron-3-ultra-free"},
        {"provider": "opencode", "model": "opencode/nemotron-3.5-lightning-free"},
    ]
    assert len(args["participants"]) == 2


def test_council_fails_closed_when_two_free_models_are_not_available() -> None:
    client = CatalogClient([_model("opencode/big-pickle")])
    with pytest.raises(AnalyticsBridgeError, match="At least two"):
        asyncio.run(
            client.council(
                prompt="Review.",
                evidence_bundle='{"sources":[]}',
                request_key="analysis:test-council-too-small",
            )
        )
    assert [name for name, _args in client.calls] == ["list_models"]


def test_model_catalog_is_read_only_allowlisted_capability() -> None:
    assert "list_models" in AnalyticsBridgeClient.ALLOWED_TOOLS
    assert "start_task" not in AnalyticsBridgeClient.ALLOWED_TOOLS
