from __future__ import annotations

import asyncio
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class DevCoveerClient:
    """Narrow local client for owner-approved Projects Hub development work."""

    def __init__(self, command: str = "/home/dev/.local/bin/codex-mcp-server") -> None:
        self.command = command

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        params = StdioServerParameters(command=self.command, args=[])
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await asyncio.wait_for(session.initialize(), timeout=15)
                result = await asyncio.wait_for(
                    session.call_tool(name, arguments),
                    timeout=30,
                )
        payload = result.structured_content
        if not isinstance(payload, dict):
            raise RuntimeError(f"DevCoveer {name} returned no structured result")
        return payload

    async def codex_status(self) -> dict[str, Any]:
        return await self._call("codex_status", {})

    async def codex_models(self) -> dict[str, Any]:
        return await self._call("list_models", {"provider": "codex", "verified_only": False})

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

    async def read_task(self, task_id: str) -> dict[str, Any]:
        return await self._call(
            "read_task",
            {
                "task": task_id,
                "detail": "summary",
            },
        )
