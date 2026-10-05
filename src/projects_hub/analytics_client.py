from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class AnalyticsBridgeError(RuntimeError):
    pass


class AnalyticsBridgeClient:
    """Narrow product-analysis client; it cannot start development tasks."""

    REQUIRED_ISOLATION_FIELDS = {"context_mode", "evidence_bundle", "request_key"}
    ALLOWED_TOOLS = {"consult_model", "read_task", "cancel_task", "council_run"}

    def __init__(
        self,
        command: str = "/home/dev/.local/bin/codex-mcp-server",
        *,
        project_hint: str = "projects-hub",
    ) -> None:
        self.command = command
        self.project_hint = project_hint
        self._lock = asyncio.Lock()
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tool_schemas: dict[str, dict[str, Any]] = {}

    async def _connect(self) -> ClientSession:
        if self._session is not None:
            return self._session
        if not Path(self.command).is_file():
            raise AnalyticsBridgeError("Analytics bridge is unavailable")
        stack = AsyncExitStack()
        try:
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(StdioServerParameters(command=self.command, args=[]))
            )
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await asyncio.wait_for(session.initialize(), timeout=20)
            listed = await asyncio.wait_for(session.list_tools(), timeout=20)
            schemas: dict[str, dict[str, Any]] = {}
            for tool in listed.tools:
                name = str(getattr(tool, "name", "") or "")
                schema = getattr(tool, "inputSchema", None)
                if schema is None:
                    schema = getattr(tool, "input_schema", None)
                schemas[name] = schema if isinstance(schema, dict) else {}
            consult = schemas.get("consult_model")
            if not consult:
                raise AnalyticsBridgeError("Safe consultant capability is unavailable")
            properties = consult.get("properties")
            if not isinstance(properties, dict) or not self.REQUIRED_ISOLATION_FIELDS.issubset(properties):
                raise AnalyticsBridgeError(
                    "Analytics bridge does not provide tenant-safe provided-context isolation"
                )
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        self._session = session
        self._tool_schemas = schemas
        return session

    @staticmethod
    def _payload(result: Any) -> dict[str, Any]:
        if getattr(result, "is_error", False):
            structured = getattr(result, "structured_content", None)
            if isinstance(structured, dict):
                return structured
            raise AnalyticsBridgeError("Analytics bridge operation failed")
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
        raise AnalyticsBridgeError("Analytics bridge returned no structured result")

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.ALLOWED_TOOLS:
            raise AnalyticsBridgeError("Analytics bridge operation is not allowlisted")
        async with self._lock:
            try:
                session = await self._connect()
                if name not in self._tool_schemas:
                    raise AnalyticsBridgeError(f"Analytics capability {name} is unavailable")
                result = await asyncio.wait_for(
                    session.call_tool(name, arguments),
                    timeout=40,
                )
                return self._payload(result)
            except (ConnectionError, BrokenPipeError, EOFError, TimeoutError, asyncio.TimeoutError):
                stack, self._stack = self._stack, None
                self._session = None
                self._tool_schemas = {}
                if stack is not None:
                    await stack.aclose()
                raise AnalyticsBridgeError("Analytics bridge connection was interrupted") from None

    async def consult(
        self,
        *,
        model: str,
        purpose: str,
        question: str,
        evidence_bundle: str,
        request_key: str,
    ) -> dict[str, Any]:
        return await self._call(
            "consult_model",
            {
                "project": self.project_hint,
                "model": model,
                "purpose": purpose,
                "question": question,
                "context_mode": "provided_only",
                "evidence_bundle": evidence_bundle,
                "request_key": request_key,
            },
        )

    async def council(
        self,
        *,
        prompt: str,
        evidence_bundle: str,
        request_key: str,
    ) -> dict[str, Any]:
        if not await self.safe_council_available():
            raise AnalyticsBridgeError(
                "Tenant-safe council capability is unavailable"
            )
        return await self._call(
            "council_run",
            {
                "project": self.project_hint,
                "prompt": prompt,
                "context_mode": "provided_only",
                "evidence_bundle": evidence_bundle,
                "request_key": request_key,
                "tier": "free",
                "rounds": 2,
                "mode": "debate",
            },
        )

    async def read_task(self, task_id: str) -> dict[str, Any]:
        return await self._call(
            "read_task",
            {"task": task_id, "project": self.project_hint, "detail": "summary"},
        )

    async def cancel_task(self, task_id: str) -> dict[str, Any] | None:
        await self._connect()
        if "cancel_task" not in self._tool_schemas:
            return None
        return await self._call(
            "cancel_task",
            {"task": task_id, "project": self.project_hint},
        )

    async def safe_council_available(self) -> bool:
        await self._connect()
        schema = self._tool_schemas.get("council_run")
        if not isinstance(schema, dict):
            return False
        properties = schema.get("properties")
        return isinstance(properties, dict) and self.REQUIRED_ISOLATION_FIELDS.issubset(properties)

    async def close(self) -> None:
        async with self._lock:
            stack, self._stack = self._stack, None
            self._session = None
            self._tool_schemas = {}
        if stack is not None:
            await stack.aclose()
