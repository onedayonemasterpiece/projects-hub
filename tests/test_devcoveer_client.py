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
            "list_tasks",
            "start_task",
            "continue_task",
            "read_task",
            "direct_ops_v2",
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
async def test_list_tasks_is_bounded_and_filterable():
    client = DevCoveerClient(command="/tmp/not-used")
    captured: list[tuple[str, dict]] = []

    async def fake_call(name: str, arguments: dict):
        captured.append((name, dict(arguments)))
        return {"status": "ok", "tasks": []}

    client._call = fake_call  # type: ignore[method-assign]
    result = await client.list_codex_tasks(
        project="projects-hub-owner",
        search="ODR-fe82",
        limit=500,
    )

    assert result["tasks"] == []
    assert captured == [
        (
            "list_tasks",
            {
                "project": "projects-hub-owner",
                "limit": 100,
                "search": "ODR-fe82",
            },
        )
    ]


@pytest.mark.asyncio
async def test_direct_ops_v2_envelope_is_narrow_and_typed():
    client = DevCoveerClient(command="/tmp/not-used")
    captured: list[tuple[str, dict]] = []

    async def fake_call(name: str, arguments: dict):
        captured.append((name, dict(arguments)))
        return {"status": "ok"}

    client._call = fake_call  # type: ignore[method-assign]

    await client.direct_project_probe(
        project="projects-hub-owner",
        operation="git_state",
    )
    await client.direct_project_action(
        project="projects-hub-owner",
        operation="git_push_existing",
        payload={"expected_sha": "a" * 40},
    )

    assert captured == [
        (
            "direct_ops_v2",
            {
                "project": "projects-hub-owner",
                "plane": "project_probe",
                "operation": "git_state",
                "payload": {},
            },
        ),
        (
            "direct_ops_v2",
            {
                "project": "projects-hub-owner",
                "plane": "project_action",
                "operation": "git_push_existing",
                "payload": {"expected_sha": "a" * 40},
            },
        ),
    ]


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



@pytest.mark.asyncio
async def test_start_task_can_be_explicit_native_codex_read():
    client = DevCoveerClient(command="/tmp/not-used")
    captured: list[tuple[str, dict]] = []

    async def fake_call(name: str, arguments: dict):
        captured.append((name, dict(arguments)))
        return {"status": "running", "taskId": "dvt_read"}

    client._call = fake_call  # type: ignore[method-assign]
    result = await client.start_codex_task(
        project="projects-hub-owner",
        prompt="review only",
        model="gpt-6-astra",
        reasoning_effort="high",
        access="read",
    )

    assert result["taskId"] == "dvt_read"
    assert captured == [
        (
            "start_task",
            {
                "project": "projects-hub-owner",
                "prompt": "review only",
                "access": "read",
                "provider": "codex",
                "model": "gpt-6-astra",
                "reasoning_effort": "high",
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
