from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class DevCoveerError(RuntimeError):
    pass


class DevCoveerUnknownOutcome(DevCoveerError):
    """A mutating request lost its reply; caller must reconcile before retrying."""


class DevCoveerClient:
    """Allowlisted short-lived client for the local DevCoveer MCP control plane."""

    def __init__(
        self,
        command: str = "/home/dev/.local/bin/codex-mcp-server",
    ) -> None:
        self.command = command

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

    async def _call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        mutating: bool = False,
        timeout_seconds: float = 45.0,
    ) -> dict[str, Any]:
        allowlisted = {
            "codex_status",
            "list_models",
            "list_tasks",
            "start_task",
            "continue_task",
            "read_task",
        }
        if name not in allowlisted:
            raise DevCoveerError("DevCoveer operation is not allowlisted")
        if not Path(self.command).is_file():
            raise DevCoveerError("DevCoveer MCP entrypoint is unavailable")

        parameters = StdioServerParameters(command=self.command, args=[])
        try:
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await asyncio.wait_for(session.initialize(), timeout=20)
                    result = await asyncio.wait_for(
                        session.call_tool(name, arguments),
                        timeout=timeout_seconds,
                    )
                    return self._payload(result)
        except asyncio.TimeoutError as exc:
            if mutating:
                raise DevCoveerUnknownOutcome(
                    f"DevCoveer {name} reply timed out; outcome must be reconciled"
                ) from exc
            raise DevCoveerError(f"DevCoveer {name} timed out") from exc
        except DevCoveerError:
            raise
        except (ConnectionError, BrokenPipeError, EOFError) as exc:
            if mutating:
                raise DevCoveerUnknownOutcome(
                    f"DevCoveer {name} connection ended before a receipt"
                ) from exc
            raise DevCoveerError(f"DevCoveer {name} connection was interrupted") from exc
        except Exception as exc:
            raise DevCoveerError(f"DevCoveer {name} unavailable") from exc

    async def status(self) -> dict[str, Any]:
        quota = await self._call("codex_status", {}, timeout_seconds=30)
        models = await self._call(
            "list_models",
            {"provider": "codex", "verified_only": False},
            timeout_seconds=30,
        )
        return {"quota": quota, "models": models.get("models", [])}

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
            mutating=True,
        )

    async def continue_codex_task(
        self,
        task_id: str,
        *,
        project: str,
        prompt: str,
        access: str,
        model: str,
        reasoning_effort: str,
    ) -> dict[str, Any]:
        return await self._call(
            "continue_task",
            {
                "task": task_id,
                "project": project,
                "prompt": prompt,
                "access": access,
                "provider": "codex",
                "model": model,
                "reasoning_effort": reasoning_effort,
            },
            mutating=True,
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
        return await self._call("read_task", args, timeout_seconds=30)

    async def list_tasks(
        self,
        *,
        project: str,
        search: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        return await self._call(
            "list_tasks",
            {"project": project, "search": search, "limit": max(1, min(limit, 100))},
            timeout_seconds=30,
        )

    async def close(self) -> None:
        return None
