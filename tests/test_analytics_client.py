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
            if arguments.get("tier") == "pro" and "paid_confirmation_token" not in arguments:
                return {
                    "status": "confirmation_required",
                    "paidConfirmationToken": "pcf_" + "a" * 32,
                    "confirmationExpiresAt": 9_999_999_999_999,
                    "costPolicy": "nvidia_full_debate_with_bounded_transient_retries",
                    "usagePlan": {"nvidiaCalls": 6, "totalCalls": 6},
                    "participants": arguments["participants"],
                }
            return {"status": "running", "taskId": "dvt_" + "4" * 32}
        raise AssertionError(name)


def _model(
    selection: str,
    *,
    provider: str = "opencode",
    free: bool = True,
    toolcall: bool = True,
):
    return {
        "id": selection,
        "selection": selection,
        "provider": provider,
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


def test_paid_council_requires_confirmation_and_replays_exact_two_nvidia_models() -> None:
    client = CatalogClient(
        [
            _model(
                "nvidia/moonshotai/kimi-k3",
                provider="nvidia",
                free=False,
            ),
            _model(
                "nvidia/deepseek-ai/deepseek-v4.1-flash",
                provider="nvidia",
                free=False,
            ),
        ]
    )
    first = asyncio.run(
        client.council(
            prompt="Review.",
            evidence_bundle='{"sources":[]}',
            request_key="analysis:test-paid-council",
            tier="pro",
        )
    )
    assert first["status"] == "confirmation_required"
    call = client.calls[-1][1]
    assert call["tier"] == "pro"
    assert call["rounds"] == 2
    assert call["context_mode"] == "provided_only"
    assert call["participants"] == [
        {"provider": "nvidia", "model": "nvidia/moonshotai/kimi-k3"},
        {
            "provider": "nvidia",
            "model": "nvidia/deepseek-ai/deepseek-v4.1-flash",
        },
    ]
    assert "paid_confirmation_token" not in call

    confirmed = asyncio.run(
        client.council(
            prompt="Review.",
            evidence_bundle='{"sources":[]}',
            request_key="analysis:test-paid-council",
            tier="pro",
            paid_confirmation_token="pcf_" + "a" * 32,
        )
    )
    assert confirmed["status"] == "running"
    second = client.calls[-1][1]
    assert second["participants"] == call["participants"]
    assert second["paid_confirmation_token"] == "pcf_" + "a" * 32


def test_paid_council_fails_closed_when_required_nvidia_model_is_missing() -> None:
    client = CatalogClient(
        [_model("nvidia/moonshotai/kimi-k3", provider="nvidia", free=False)]
    )
    with pytest.raises(AnalyticsBridgeError, match="Required paid council models"):
        asyncio.run(
            client.council(
                prompt="Review.",
                evidence_bundle='{"sources":[]}',
                request_key="analysis:test-paid-missing",
                tier="pro",
            )
        )
    assert [name for name, _args in client.calls] == ["list_models"]
