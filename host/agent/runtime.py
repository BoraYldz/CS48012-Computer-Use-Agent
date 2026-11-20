"""Configuration, model clients, local/VM tool backends and run logging."""
import json
import os
import time
from pathlib import Path
from typing import Any, Protocol

import httpx
from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

from .local_runtime import resolve_local_root
from .tool_output import decode_tool_result

load_dotenv()

VM_MCP_URL = os.getenv("VM_MCP_URL", "http://localhost:8765/mcp")
AGENT_MODE = os.getenv("AGENT_MODE", "local").strip().lower()
AGENT_SCOPE = os.getenv("AGENT_SCOPE", "sandbox").strip().lower()
_DEFAULT_LOCAL_ROOT = Path(__file__).resolve().parent.parent / "local_sandbox"
_RAW_LOCAL_ROOT = os.getenv("AGENT_ROOT")
if AGENT_SCOPE == "workspace" and AGENT_MODE != "local":
    raise ValueError("AGENT_SCOPE=workspace is currently supported only with AGENT_MODE=local")
LOCAL_ROOT = resolve_local_root(_RAW_LOCAL_ROOT, AGENT_SCOPE, _DEFAULT_LOCAL_ROOT)
MAX_STEPS = int(os.getenv("MAX_STEPS_PER_SUBGOAL", "15"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS_PER_TASK", "200000"))
# Quick Fix is deliberately capped at two attempts per subgoal.
MAX_QUICK_FIXES = min(2, max(0, int(os.getenv("MAX_QUICK_FIXES_PER_SUBGOAL", "2"))))
RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"

# Which tools each worker may use (least privilege). Harness tools are never listed.
WORKER_TOOLS = {
    "files": ["list_dir", "path_info", "make_dirs", "move_path", "move_files", "copy_path",
              "read_text", "write_text", "read_pdf_text", "delete_path"],
    "sheet": ["list_dir", "path_info", "read_text", "read_pdf_text", "xlsx_read", "xlsx_write"],
}


def bound_move_enabled() -> bool:
    """BOUND_MOVE=on restores the planner-bound `move_expected_files` tool (ablation switch)."""
    return os.getenv("BOUND_MOVE", "off").strip().lower() in {"1", "true", "on", "yes"}


def unload_models() -> None:
    """Ask a local Ollama server to evict our models now (keep_alive=0); no-op otherwise.

    Ollama otherwise keeps the model resident for OLLAMA_KEEP_ALIVE (default 5 min).
    """
    base = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
    if "11434" not in base:
        return
    root = base.split("/v1")[0]
    names = {os.getenv(f"{r}_MODEL", os.getenv("DEFAULT_MODEL", "qwen3:8b"))
             for r in ("PLANNER", "WORKER", "CRITIC")}
    for name in names:
        try:
            httpx.post(f"{root}/api/generate", json={"model": name, "keep_alive": 0}, timeout=5)
        except Exception:
            pass  # best effort: never fail a run because cleanup failed


class ToolBackend(Protocol):
    """Common interface implemented by LocalVM and the MCP-backed VM."""
    tools: dict[str, Any]

    async def connect(self): ...
    def for_worker(self, capability: str) -> list[Any]: ...
    async def call(self, name: str, **args): ...


def model(role: str) -> ChatOpenAI:
    """Build a role-specific model client for any OpenAI-compatible endpoint."""
    name = os.getenv(f"{role.upper()}_MODEL", os.getenv("DEFAULT_MODEL", "qwen3:8b"))
    base_url = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
    kwargs = {
        "model": name,
        "base_url": base_url,
        "api_key": os.getenv("LLM_API_KEY", "ollama"),
        "temperature": float(os.getenv("LLM_TEMPERATURE", "0")),
    }
    # Current Ollama versions expose Qwen3's thinking switch most reliably on
    # the native /api/chat endpoint. Its /v1 compatibility behavior has varied;
    # use the OpenAI-compatible reasoning_effort hint when configured, and keep
    # the prompts concise for worker calls as an additional practical safeguard.
    if ("11434" in base_url and os.getenv("OLLAMA_THINKING", "false").lower() in {"0", "false", "no"}):
        kwargs["extra_body"] = {"reasoning_effort": "none"}
    return ChatOpenAI(**kwargs)


def create_backend():
    """Construct the configured local or MCP-backed tool server."""
    if AGENT_MODE == "local":
        from .local_runtime import LocalVM
        return LocalVM(LOCAL_ROOT)
    if AGENT_MODE == "vm":
        return VM()
    raise ValueError(f"AGENT_MODE must be 'local' or 'vm', got {AGENT_MODE!r}")


class VM:
    """Connection to the tool server inside the VM."""

    def __init__(self, url: str = VM_MCP_URL):
        self.client = MultiServerMCPClient({"vm": {"url": url, "transport": "streamable_http"}})
        self.tools = {}

    async def connect(self):
        self.tools = {t.name: t for t in await self.client.get_tools()}
        return self

    def for_worker(self, capability: str):
        # Older tool servers may lack newer tools (e.g. delete_path); skip rather than crash.
        return [self.tools[n] for n in WORKER_TOOLS[capability] if n in self.tools]

    async def call(self, name: str, **args):
        """Direct call (used by the harness and checkers). Returns parsed JSON when possible."""
        msg = await self.tools[name].ainvoke(
            {"type": "tool_call", "id": "harness", "name": name, "args": args})
        sc = (msg.artifact or {}).get("structured_content") if msg.artifact else None
        if sc is not None:  # FastMCP wraps non-dict returns as {"result": ...}
            result = sc["result"] if set(sc) == {"result"} else sc
            return decode_tool_result(result)
        text = "".join(b.get("text", "") if isinstance(b, dict) else str(b)
                       for b in (msg.content if isinstance(msg.content, list) else [msg.content]))
        if getattr(msg, "status", "success") == "error":
            raise RuntimeError(f"{name} failed: {text}")
        return decode_tool_result(text)


class RunLog:
    """One JSONL file per run: every node writes here."""

    def __init__(self, run_id: str):
        RUNS_DIR.mkdir(exist_ok=True)
        self.path = RUNS_DIR / f"{run_id}.jsonl"

    def write(self, event: str, **data):
        rec = {"t": round(time.time(), 3), "event": event, **data}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        print(f"[{event}] " + json.dumps(data, ensure_ascii=False, default=str)[:200])
