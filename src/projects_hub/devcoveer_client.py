from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class DevCoveerError(RuntimeError):
    pass


class DevCoveerClient:
    """Allowlisted local DevCoveer client with one task-owned MCP session.

    A dedicated worker task owns the stdio subprocess and all AnyIO/MCP async
    contexts for their entire lifetime. FastAPI request tasks communicate with
    that worker through a queue. Application shutdown therefore never attempts
    to unwind a cancel scope from a different task, while native Codex turns keep
    the persistent bridge process they require between start/read/continue calls.
    """

    _ALLOWED = {"codex_status", "list_models", "start_task", "continue_task", "read_task"}
    _REQUIRED = {"codex_status", "list_models", "start_task", "continue_task", "read_task"}

    def __init__(
        self,
        command: str | None = None,
    ) -> None:
        self.command = (
            command
            or os.getenv("PROJECTS_HUB_DEVCOVEER_COMMAND")
            or "/home/dev/.local/bin/codex-mcp-server"
        )
        self._queue: asyncio.Queue[
            tuple[str, dict[str, Any], asyncio.Future[dict[str, Any]]] | None
        ] = asyncio.Queue()
        self._state_lock = asyncio.Lock()
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

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

    @staticmethod
    def _connection_error() -> DevCoveerError:
        return DevCoveerError("DevCoveer connection was interrupted or timed out")

    def _fail_pending(self, error: DevCoveerError) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if item is None:
                continue
            _name, _arguments, future = item
            if not future.done():
                future.set_exception(DevCoveerError(str(error)))

    async def _worker_loop(self) -> None:
        try:
            async with stdio_client(
                StdioServerParameters(command=self.command, args=[])
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await asyncio.wait_for(session.initialize(), timeout=20)
                    listed = await asyncio.wait_for(session.list_tools(), timeout=20)
                    names = {tool.name for tool in listed.tools}
                    if not self._REQUIRED.issubset(names):
                        raise DevCoveerError(
                            "DevCoveer runtime is missing owner execution capabilities"
                        )

                    while True:
                        item = await self._queue.get()
                        if item is None:
                            return
                        name, arguments, future = item
                        if future.cancelled():
                            continue
                        try:
                            result = await asyncio.wait_for(
                                session.call_tool(name, arguments),
                                timeout=35,
                            )
                            payload = self._payload(result)
                        except DevCoveerError as exc:
                            if not future.done():
                                future.set_exception(exc)
                            continue
                        except (
                            ConnectionError,
                            BrokenPipeError,
                            EOFError,
                            OSError,
                            TimeoutError,
                        ):
                            error = self._connection_error()
                            if not future.done():
                                future.set_exception(error)
                            self._fail_pending(error)
                            return
                        if not future.done():
                            future.set_result(payload)
        except DevCoveerError as exc:
            self._fail_pending(exc)
        except (
            ConnectionError,
            BrokenPipeError,
            EOFError,
            OSError,
            TimeoutError,
        ):
            self._fail_pending(self._connection_error())
        finally:
            async with self._state_lock:
                if self._worker is asyncio.current_task():
                    self._worker = None

    async def _ensure_worker(self) -> None:
        async with self._state_lock:
            if self._closed:
                raise DevCoveerError("DevCoveer client is closed")
            if self._worker is None or self._worker.done():
                if not Path(self.command).is_file():
                    raise DevCoveerError("DevCoveer MCP entrypoint is unavailable")
                self._worker = asyncio.create_task(
                    self._worker_loop(),
                    name="projects-hub-devcoveer-client",
                )

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self._ALLOWED:
            raise DevCoveerError("DevCoveer operation is not allowlisted")
        await self._ensure_worker()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._queue.put_nowait((name, arguments, future))
        return await future

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

    async def continue_codex_task(
        self,
        task_id: str,
        *,
        project: str,
        prompt: str,
        access: str = "write",
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "task": task_id,
            "project": project,
            "prompt": prompt,
            "access": access,
            "provider": "codex",
        }
        if model:
            args["model"] = model
        if reasoning_effort:
            args["reasoning_effort"] = reasoning_effort
        return await self._call("continue_task", args)

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
        async with self._state_lock:
            if self._closed:
                worker = self._worker
            else:
                self._closed = True
                worker = self._worker
                if worker is not None and not worker.done():
                    self._queue.put_nowait(None)
        if worker is not None:
            await worker
