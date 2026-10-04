from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class DevCoveerError(RuntimeError):
    pass


class DevCoveerClient:
    """Narrow local client for owner-approved Projects Hub development runs."""

    COMMAND = Path("/home/dev/.local/bin/codex-mcp-server")

    def __init__(self, command: Path | None = None) -> None:
        self.command = command or self.COMMAND

    @property
    def available(self) -> bool:
        return self.command.is_file()

    @staticmethod
    def _payload(result: Any) -> dict[str, Any]:
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            return structured
        content = getattr(result, "content", None)
        if isinstance(content, list):
            for block in content:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    try:
                        parsed = json.loads(text)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(parsed, dict):
                        return parsed
        raise DevCoveerError("DevCoveer returned no structured result")

    async def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.available:
            raise DevCoveerError("DevCoveer local MCP is unavailable")
        params = StdioServerParameters(command=str(self.command), args=[])
        try:
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await asyncio.wait_for(session.initialize(), timeout=15)
                    result = await asyncio.wait_for(
                        session.call_tool(tool, arguments),
                        timeout=45,
                    )
        except (TimeoutError, OSError, ExceptionGroup) as exc:
            raise DevCoveerError("DevCoveer local MCP call failed") from exc
        payload = self._payload(result)
        if getattr(result, "is_error", False):
            raise DevCoveerError(str(payload.get("content") or payload.get("message") or "DevCoveer error")[:400])
        return payload

    async def codex_status(self) -> dict[str, Any]:
        return await self._call("codex_status", {})

    async def list_codex_models(self) -> dict[str, Any]:
        return await self._call(
            "list_models",
            {"provider": "codex", "verified_only": False},
        )

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

    async def read_task(self, task_id: str, *, project: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {"task": task_id, "detail": "summary"}
        if project:
            args["project"] = project
        return await self._call("read_task", args)
