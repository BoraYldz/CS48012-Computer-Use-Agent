"""ToolBackend for local mode: tools live in a stdio MCP server subprocess (agent/mcp_server.py).

One persistent session per backend (the server owns the sandbox root and the approval gate). The session
lives inside a dedicated asyncio task so anyio's enter/exit-in-same-task rule always holds.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from langchain_core.tools import StructuredTool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ElicitResult

from .tool_output import decode_tool_result

HOST_DIR = Path(__file__).resolve().parent.parent
# Tools whose direct (LocalVM) result is structured data; call() decodes their JSON text back.
# Everything else (read_text, "ok", sheet messages, base64) is returned as the raw string.
JSON_RESULT_TOOLS = frozenset({
    "list_dir", "path_info", "move_files", "copy_files",
    "xlsx_info", "xlsx_read", "xlsx_query", "xlsx_aggregate",
})


class McpLocalBackend:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.tools: dict[str, Any] = {}
        # Same attribute surface as LocalVM, set by cli/graph.
        self.approver: Callable[[dict], bool] | None = None
        self.approval_log: list[dict] = []
        self.event_sink: Callable[..., Any] | None = None
        self._session: ClientSession | None = None
        self._runner: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._ready: asyncio.Future | None = None

    # ---------- lifecycle ----------
    async def connect(self) -> "McpLocalBackend":
        self._stop = asyncio.Event()
        self._ready = asyncio.get_running_loop().create_future()
        self._runner = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._ready, timeout=30)
        except BaseException:
            self._runner.cancel()
            raise
        return self

    async def _run(self) -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "agent.mcp_server"],
            env={**os.environ, "AGENT_ROOT": str(self.root), "FASTMCP_LOG_LEVEL": "WARNING"},
            cwd=str(HOST_DIR),
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, elicitation_callback=self._on_elicit) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self.tools = {t.name: self._wrap(t) for t in listed.tools}
                    self._session = session
                    self._ready.set_result(None)
                    await self._stop.wait()
        except Exception as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
        finally:
            self._session = None

    async def aclose(self) -> None:
        runner, self._runner = self._runner, None
        if runner is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(runner, timeout=10)
        except Exception:
            runner.cancel()

    async def __aenter__(self) -> "McpLocalBackend":
        return await self.connect()

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ---------- approval (server -> host elicitation) ----------
    async def _on_elicit(self, context, params) -> ElicitResult:
        try:
            request = json.loads(params.message)
            if not isinstance(request, dict) or "summary" not in request:
                raise ValueError
        except ValueError:
            request = {"tool": "?", "args": {}, "summary": str(params.message)}
        approved = False
        if self.approver:
            try:
                approved = bool(await asyncio.to_thread(self.approver, request))
            except Exception:
                approved = False
        record = {**request, "approved": approved}
        self.approval_log.append(record)
        if self.event_sink:
            try:
                self.event_sink("approval", **record)
            except Exception:
                pass
        if approved:
            return ElicitResult(action="accept", content={"approve": True})
        return ElicitResult(action="decline")

    # ---------- tools ----------
    async def _invoke(self, name: str, args: dict, *, strict: bool) -> str:
        if self._session is None:
            raise RuntimeError("MCP local backend is not connected")
        try:
            res = await self._session.call_tool(name, args)
        except Exception as exc:
            text = f"TOOL_ERROR [{name}]: MCP call failed: {type(exc).__name__}: {str(exc)[:200]}"
            if strict:
                raise RuntimeError(text) from exc
            return text
        text = "".join(c.text for c in res.content if getattr(c, "type", None) == "text")
        if res.isError:
            text = text.removeprefix(f"Error executing tool {name}: ")
            if not text.startswith("TOOL_ERROR"):          # protocol-level failure (validation, unknown tool)
                text = f"TOOL_ERROR [{name}]: {text[:300]}"
            if strict:
                raise RuntimeError(text)
        return text

    def _wrap(self, t) -> StructuredTool:
        async def run(**kwargs) -> str:
            return await self._invoke(t.name, kwargs, strict=False)
        return StructuredTool(name=t.name, description=t.description or t.name,
                              args_schema=t.inputSchema, coroutine=run)

    def for_worker(self, capability: str) -> list[Any]:
        from .runtime import WORKER_TOOLS

        if capability not in WORKER_TOOLS:
            raise ValueError(f"unknown worker capability: {capability}")
        return [self.tools[name] for name in WORKER_TOOLS[capability]]   # KeyError = server/client mismatch

    async def call(self, name: str, **args):
        """Direct call for the CLI, harness and checkers. Raises RuntimeError on a tool error."""
        text = await self._invoke(name, args, strict=True)
        return decode_tool_result(text) if name in JSON_RESULT_TOOLS else text