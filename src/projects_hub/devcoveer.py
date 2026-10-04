from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

DEFAULT_ENTRYPOINT = "/home/dev/.local/bin/codex-mcp-server"


class DevCoveerError(RuntimeError):
    pass


@dataclass(frozen=True)
class DevCoveerClient:
    entrypoint: str = DEFAULT_ENTRYPOINT

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        params = StdioServerParameters(command=self.entrypoint, args=[])
        try:
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await asyncio.wait_for(session.initialize(), timeout=20)
                    result = await asyncio.wait_for(
                        session.call_tool(name, arguments),
                        timeout=45 if name == "codex_status" else 30,
                    )
        except Exception as exc:
            raise DevCoveerError(f"DevCoveer {name} unavailable") from exc
        payload = result.structured_content
        if not isinstance(payload, dict):
            raise DevCoveerError(f"DevCoveer {name} returned no structured result")
        if result.is_error:
            raise DevCoveerError(str(payload.get("content") or payload.get("message") or name)[:500])
        return payload

    async def codex_status(self) -> dict[str, Any]:
        return await self._call("codex_status", {})

    async def start_task(
        self,
        *,
        project: str,
        prompt: str,
        access: str = "write",
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "project": project,
            "prompt": prompt,
            "access": access,
            "provider": "codex",
        }
        if model:
            args["model"] = model
        if reasoning_effort:
            args["reasoning_effort"] = reasoning_effort
        return await self._call("start_task", args)

    async def read_task(self, task_id: str) -> dict[str, Any]:
        return await self._call("read_task", {"task": task_id, "detail": "summary"})
