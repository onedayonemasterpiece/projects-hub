from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class DevCoveerError(RuntimeError):
    pass


class DevCoveerUnknownOutcome(DevCoveerError):
    """A mutating request lost its reply; caller must reconcile before retrying."""


@dataclass
class _Request:
    name: str
    arguments: dict[str, Any]
    mutating: bool
    timeout_seconds: float
    future: asyncio.Future[dict[str, Any]]


class DevCoveerClient:
    """Single-owner local DevCoveer MCP actor.

    One dedicated asyncio task owns the stdio/AnyIO contexts for their full
    lifetime. HTTP, Live and background-driver callers only enqueue typed calls.
    This keeps native Codex app-server turns alive while avoiding cross-task
    context-manager ownership.
    """

    def __init__(
        self,
        command: str = "/home/dev/.local/bin/codex-mcp-server",
    ) -> None:
        self.command = command
        self._queue: asyncio.Queue[_Request | None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
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

    async def start(self) -> None:
        if self._closed:
            raise DevCoveerError("DevCoveer client is closed")
        async with self._start_lock:
            if self._worker is not None and not self._worker.done():
                return
            if not Path(self.command).is_file():
                raise DevCoveerError("DevCoveer MCP entrypoint is unavailable")
            self._queue = asyncio.Queue()
            self._worker = asyncio.create_task(
                self._run(),
                name="projects-hub-devcoveer-client",
            )

    async def _run(self) -> None:
        assert self._queue is not None
        parameters = StdioServerParameters(command=self.command, args=[])
        try:
            while not self._closed:
                reconnect = False
                try:
                    async with stdio_client(parameters) as (read_stream, write_stream):
                        async with ClientSession(read_stream, write_stream) as session:
                            await asyncio.wait_for(session.initialize(), timeout=20)
                            while not self._closed:
                                request = await self._queue.get()
                                if request is None:
                                    return
                                if request.future.cancelled():
                                    continue
                                try:
                                    result = await asyncio.wait_for(
                                        session.call_tool(request.name, request.arguments),
                                        timeout=request.timeout_seconds,
                                    )
                                    payload = self._payload(result)
                                except asyncio.TimeoutError:
                                    error: Exception
                                    if request.mutating:
                                        error = DevCoveerUnknownOutcome(
                                            f"DevCoveer {request.name} reply timed out; "
                                            "outcome must be reconciled"
                                        )
                                    else:
                                        error = DevCoveerError(
                                            f"DevCoveer {request.name} timed out"
                                        )
                                    if not request.future.done():
                                        request.future.set_exception(error)
                                    reconnect = True
                                    break
                                except DevCoveerError as exc:
                                    if not request.future.done():
                                        request.future.set_exception(exc)
                                except (ConnectionError, BrokenPipeError, EOFError) as exc:
                                    error = (
                                        DevCoveerUnknownOutcome(
                                            f"DevCoveer {request.name} connection ended "
                                            "before a receipt"
                                        )
                                        if request.mutating
                                        else DevCoveerError(
                                            f"DevCoveer {request.name} connection was interrupted"
                                        )
                                    )
                                    if not request.future.done():
                                        request.future.set_exception(error)
                                    reconnect = True
                                    break
                                except Exception as exc:
                                    if not request.future.done():
                                        request.future.set_exception(
                                            DevCoveerError(
                                                f"DevCoveer {request.name} unavailable"
                                            )
                                        )
                                    reconnect = True
                                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The control-plane process may be restarting. Keep the
                    # actor alive and retry the session; queued calls remain.
                    reconnect = True
                if reconnect and not self._closed:
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            # Fail anything still queued on shutdown rather than hanging callers.
            while self._queue is not None:
                try:
                    request = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if request is not None and not request.future.done():
                    request.future.set_exception(DevCoveerError("DevCoveer client stopped"))

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
        await self.start()
        assert self._queue is not None
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        await self._queue.put(
            _Request(
                name=name,
                arguments=arguments,
                mutating=mutating,
                timeout_seconds=timeout_seconds,
                future=future,
            )
        )
        return await future

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
        self._closed = True
        queue = self._queue
        worker = self._worker
        if queue is not None:
            await queue.put(None)
        if worker is not None and not worker.done():
            try:
                await asyncio.wait_for(worker, timeout=10)
            except asyncio.TimeoutError:
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
        self._worker = None
        self._queue = None
