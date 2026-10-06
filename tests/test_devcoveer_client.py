from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import projects_hub.devcoveer_client as module
from projects_hub.devcoveer_client import DevCoveerClient


class FakeSession:
    def __init__(self, _read, _write, calls: list[str]) -> None:
        self.calls = calls
        self.enter_task = None

    async def __aenter__(self):
        self.enter_task = asyncio.current_task()
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        assert asyncio.current_task() is self.enter_task
        return False

    async def initialize(self):
        self.calls.append("initialize")

    async def list_tools(self):
        names = [
            "codex_status",
            "list_models",
            "start_task",
            "continue_task",
            "read_task",
        ]
        return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in names])

    async def call_tool(self, name, _arguments):
        self.calls.append(name)
        if name == "list_models":
            payload = {"models": []}
        else:
            payload = {"status": "ok"}
        return SimpleNamespace(is_error=False, structured_content=payload, content=[])


@pytest.mark.asyncio
async def test_worker_owns_stdio_context_across_request_and_lifespan_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    command = tmp_path / "codex-mcp-server"
    command.write_text("#!/bin/sh\n", encoding="utf-8")
    calls: list[str] = []
    stdio_tasks: dict[str, asyncio.Task] = {}

    @asynccontextmanager
    async def fake_stdio(_params):
        stdio_tasks["enter"] = asyncio.current_task()
        yield object(), object()
        stdio_tasks["exit"] = asyncio.current_task()
        assert stdio_tasks["exit"] is stdio_tasks["enter"]

    monkeypatch.setattr(module, "stdio_client", fake_stdio)
    monkeypatch.setattr(
        module,
        "ClientSession",
        lambda read, write: FakeSession(read, write, calls),
    )

    client = DevCoveerClient(command=str(command))
    request_task = asyncio.create_task(client.status())
    result = await request_task

    assert result["quota"]["status"] == "ok"
    assert result["models"] == []
    assert calls == ["initialize", "codex_status", "list_models"]
    assert stdio_tasks["enter"] is not request_task

    await client.close()
    assert stdio_tasks["exit"] is stdio_tasks["enter"]
    assert stdio_tasks["exit"] is not asyncio.current_task()


@pytest.mark.asyncio
async def test_start_task_remains_explicit_native_codex_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    command = tmp_path / "codex-mcp-server"
    command.write_text("#!/bin/sh\n", encoding="utf-8")
    captured: list[tuple[str, dict]] = []

    @asynccontextmanager
    async def fake_stdio(_params):
        yield object(), object()

    class CapturingSession(FakeSession):
        async def call_tool(self, name, arguments):
            captured.append((name, dict(arguments)))
            return SimpleNamespace(
                is_error=False,
                structured_content={"status": "running", "taskId": "dvt_test"},
                content=[],
            )

    monkeypatch.setattr(module, "stdio_client", fake_stdio)
    monkeypatch.setattr(
        module,
        "ClientSession",
        lambda read, write: CapturingSession(read, write, []),
    )

    client = DevCoveerClient(command=str(command))
    try:
        result = await client.start_codex_task(
            project="projects-hub",
            prompt="canary",
            model="gpt-6.1-sol",
            reasoning_effort="medium",
        )
    finally:
        await client.close()

    assert result["taskId"] == "dvt_test"
    assert captured == [
        (
            "start_task",
            {
                "project": "projects-hub",
                "prompt": "canary",
                "access": "write",
                "provider": "codex",
                "model": "gpt-6.1-sol",
                "reasoning_effort": "medium",
            },
        )
    ]


def test_command_uses_production_env_without_overriding_explicit_command(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "PROJECTS_HUB_DEVCOVEER_COMMAND",
        "/opt/projects-hub/run-devcoveer-mcp",
    )
    assert DevCoveerClient().command == "/opt/projects-hub/run-devcoveer-mcp"
    assert (
        DevCoveerClient(command="/tmp/direct-codex-mcp").command
        == "/tmp/direct-codex-mcp"
    )
