from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class DevCoveerError(RuntimeError):
    pass


class DevCoveerClient:
    """Small allowlisted client for the local DevCoveer MCP control plane."""

    def __init__(
        self,
        command: str = "/home/dev/.local/bin/codex-mcp-server",
    ) -> None:
        self.command = command
        self._lock = asyncio.Lock()
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def _connect(self) -> ClientSession:
        if self._session is not None:
            return self._session
        if not Path(self.command).is_file():
            raise DevCoveerError("DevCoveer MCP entrypoint is unavailable")
        stack = AsyncExitStack()
        try:
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(StdioServerParameters(command=self.command, args=[]))
            )
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await asyncio.wait_for(session.initialize(), timeout=20)
            listed = await asyncio.wait_for(session.list_tools(), timeout=20)
            names = {tool.name for tool in listed.tools}
            required = {"codex_status", "list_models", "start_task", "read_task"}
            if not required.issubset(names):
                raise DevCoveerError(
                    "DevCoveer runtime is missing owner execution capabilities"
                )
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        self._session = session
        return session

    @staticmethod
    def _payload(result: Any) -> dict[str, Any]:
        if getattr(result, "is_error", False):
            raise DevCoveerError("DevCoveer operation failed")
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            return structured
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                continue
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
        raise DevCoveerError("DevCoveer returned no structured result")

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in {"codex_status", "list_models", "start_task", "read_task"}:
            raise DevCoveerError("DevCoveer operation is not allowlisted")
        async with self._lock:
            try:
                session = await self._connect()
                result = await asyncio.wait_for(
                    session.call_tool(name, arguments),
                    timeout=35,
                )
                return self._payload(result)
            except (ConnectionError, BrokenPipeError, EOFError):
                stack, self._stack = self._stack, None
                self._session = None
                if stack is not None:
                    await stack.aclose()
                raise DevCoveerError("DevCoveer connection was interrupted") from None

    async def status(self) -> dict[str, Any]:
        quota = await self._call("codex_status", {})
        models = await self._call(
            "list_models",
            {"provider": "codex", "verified_only": False},
        )
        return {
            "quota": quota,
            "models": models.get("models", []),
        }

    async def start_codex_task(
        self,
        *,
        project: str,
        prompt: str,
        model: str,
        reasoning_effort: str,
    ) -> dict[str, Any]:
        return await self._call(
            "start_task",
            {
                "project": project,
                "prompt": prompt,
                "access": "write",
                "provider": "codex",
                "model": model,
                "reasoning_effort": reasoning_effort,
            },
        )

    async def read_task(
        self,
        task_id: str,
        *,
        project: str | None = None,
        detail: str = "summary",
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"task": task_id, "detail": detail}
        if project:
            args["project"] = project
        return await self._call("read_task", args)

    async def close(self) -> None:
        async with self._lock:
            stack, self._stack = self._stack, None
            self._session = None
        if stack is not None:
            await stack.aclose()
