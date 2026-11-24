"""Local MCP tool server (stdio). Every tool is a LocalVM method: one implementation, two transports.

Started by McpLocalBackend as `python -m agent.mcp_server` with AGENT_ROOT set. stdout is the protocol
channel: never print() here. Approval-gated tools ask the host through MCP elicitation; no answer = denied.
"""
import functools
import inspect
import json
import os
import typing
from contextvars import ContextVar

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool

from .local_runtime import LocalVM, format_tool_error
from .tool_output import serialize_tool_result


class ApprovalChoice(BaseModel):
    approve: bool = Field(description="Allow this action?")


_CTX: ContextVar = ContextVar("mcp_request_ctx", default=None)


def build_server(root: str) -> FastMCP:
    backend = LocalVM(root)
    mcp = FastMCP("cua-local")

    def approver(request: dict) -> bool:
        """Runs in a worker thread (see `tool`): hop to the event loop and elicit a yes/no from the host."""
        ctx = _CTX.get()
        if ctx is None:
            return False

        async def ask() -> bool:
            res = await ctx.elicit(
                message=json.dumps(request, ensure_ascii=False, default=str), schema=ApprovalChoice)
            return res.action == "accept" and bool(res.data and res.data.approve)

        try:
            return bool(anyio.from_thread.run(ask))
        except Exception:
            return False          # client without elicitation, cancelled, closed pipe: fail closed

    backend.approver = approver

    def register(name: str, fn) -> None:
        hints = typing.get_type_hints(fn)
        sig = inspect.signature(fn)
        params = [p.replace(annotation=hints.get(p.name, inspect.Parameter.empty))
                  for p in sig.parameters.values()]

        async def tool(**kwargs):
            token = _CTX.set(mcp.get_context())
            try:
                try:   # sync tool bodies run in a thread so the approver can await on the event loop
                    result = await anyio.to_thread.run_sync(functools.partial(fn, **kwargs))
                except Exception as exc:
                    raise ToolError(format_tool_error(name, exc)) from exc
                return serialize_tool_result(result)
            finally:
                _CTX.reset(token)

        tool.__name__ = name
        tool.__signature__ = sig.replace(parameters=params, return_annotation=str)
        tool.__annotations__ = {
            **{p.name: p.annotation for p in params if p.annotation is not inspect.Parameter.empty},
            "return": str,
        }
        # Same description bytes as the direct path: take them from the same LangChain constructor.
        description = StructuredTool.from_function(
            func=fn, name=name, description=fn.__doc__ or name).description
        mcp.add_tool(tool, name=name, description=description, structured_output=False)

    for tool_name, tool_fn in backend._functions.items():
        register(tool_name, tool_fn)
    return mcp


def main() -> None:
    root = os.environ.get("AGENT_ROOT", "").strip()
    if not root:
        raise SystemExit("AGENT_ROOT is required")
    build_server(root).run(transport="stdio")


if __name__ == "__main__":
    main()