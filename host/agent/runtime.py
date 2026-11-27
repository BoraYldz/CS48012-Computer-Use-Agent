"""Configuration, model clients, local/VM tool backends and run logging."""
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Protocol

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
# Per-worker step caps (cumulative per subgoal, Quick Fix included). Specialists get their own budget:
# a spreadsheet subgoal is inspect -> (query/aggregate) -> write -> re-read, so it needs fewer steps.
WORKER_MAX_STEPS = {
    "files": MAX_STEPS,
    "sheet": int(os.getenv("MAX_STEPS_SHEET", "12")),
}


def max_steps(capability: str) -> int:
    return WORKER_MAX_STEPS.get(capability, MAX_STEPS)


MAX_TOKENS = int(os.getenv("MAX_TOKENS_PER_TASK", "200000"))
# Quick Fix is deliberately capped at two attempts per subgoal.
MAX_QUICK_FIXES = min(2, max(0, int(os.getenv("MAX_QUICK_FIXES_PER_SUBGOAL", "2"))))

def max_attempts() -> int:
    """Reflexion trials per task (1 = off). Read per call so eval arms and tests can switch it."""
    return min(3, max(1, int(os.getenv("MAX_ATTEMPTS", "1"))))


def lessons_enabled() -> bool:
    return os.getenv("REFLEXION_LESSONS", "on").strip().lower() not in {"0", "off", "false", "no"}

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"

# Which tools each worker may use (least privilege). Harness tools are never listed.
WORKER_TOOLS = {
    "files": ["list_dir", "path_info", "make_dirs", "move_path", "move_files", "copy_path", "copy_files",
              "read_text", "write_text", "read_pdf_text", "delete_path"],
    # Sheet worker: read-only file access for inputs (no move/copy/delete/write_text) plus the
    # spreadsheet tools. Replacing existing sheet data is approval-gated inside xlsx_write.
    "sheet": ["list_dir", "path_info", "read_text", "read_pdf_text",
              "xlsx_info", "xlsx_read", "xlsx_import_csv", "xlsx_query", "xlsx_aggregate",
              "xlsx_write", "xlsx_group_summary", "xlsx_append_rows", "xlsx_set_cells",
              "xlsx_update_row", "xlsx_add_formula_column", "xlsx_filter_to_sheet", "xlsx_add_column"],
}


def bound_move_enabled() -> bool:
    """BOUND_MOVE=on restores the planner-bound `move_expected_files` tool (ablation switch)."""
    return os.getenv("BOUND_MOVE", "off").strip().lower() in {"1", "true", "on", "yes"}


def plan_fastpath_enabled() -> bool:
    """PLAN_FASTPATH=on re-enables the phrase-matching deterministic planner (ablation only; default off)."""
    return os.getenv("PLAN_FASTPATH", "off").strip().lower() in {"1", "true", "on", "yes"}


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


def model(role: str, temperature: float | None = None, seed_offset: int = 0) -> ChatOpenAI:
    """Build a role-specific model client for any OpenAI-compatible endpoint.

    The planner has a completion cap (PLANNER_MAX_TOKENS, default 3000): a plan is ~1-2k tokens, so a
    runaway generation is cut off and retried instead of hanging for minutes.
    """
    name = os.getenv(f"{role.upper()}_MODEL", os.getenv("DEFAULT_MODEL", "qwen3:8b"))
    base_url = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
    kwargs = {
        "model": name,
        "base_url": base_url,
        "api_key": os.getenv("LLM_API_KEY", "ollama"),
        "temperature": float(os.getenv("LLM_TEMPERATURE", "0")) if temperature is None else temperature,
    }
    extra_body = {}
    cap = os.getenv(f"{role.upper()}_MAX_TOKENS", "3000" if role == "planner" else "")
    if cap.strip():
        kwargs["max_tokens"] = int(cap)
        # langchain-openai sends max_tokens as "max_completion_tokens", which Ollama ignores;
        # without this the cap never applied and a runaway plan filled the whole context.
        extra_body["max_tokens"] = int(cap)
    if os.getenv("LLM_SEED", "").strip():   # eval sets one seed per run so pass^k uses independent samples
        kwargs["seed"] = int(os.environ["LLM_SEED"]) + seed_offset
    # Current Ollama versions expose Qwen3's thinking switch most reliably on
    # the native /api/chat endpoint. Its /v1 compatibility behavior has varied;
    # use the OpenAI-compatible reasoning_effort hint when configured, and keep
    # the prompts concise for worker calls as an additional practical safeguard.
    if ("11434" in base_url and os.getenv("OLLAMA_THINKING", "false").lower() in {"0", "false", "no"}):
        extra_body["reasoning_effort"] = "none"
    if extra_body:
        kwargs["extra_body"] = extra_body
    return ChatOpenAI(**kwargs)


def create_backend():
    """Construct the configured local or MCP-backed tool server."""
    if AGENT_MODE == "local":
        transport = os.getenv("LOCAL_TRANSPORT", "mcp").strip().lower()   # read per call so tests can switch
        if transport == "direct":
            from .local_runtime import LocalVM
            return LocalVM(LOCAL_ROOT)
        if transport == "mcp":
            from .mcp_local import McpLocalBackend
            return McpLocalBackend(LOCAL_ROOT)
        raise ValueError(f"LOCAL_TRANSPORT must be 'mcp' or 'direct', got {transport!r}")
    if AGENT_MODE == "vm":
        return VM()
    raise ValueError(f"AGENT_MODE must be 'local' or 'vm', got {AGENT_MODE!r}")


# VM tools that can destroy existing data, and where each keeps its target sheet name (arg, default).
_SHEET_REPLACE_TOOLS = {"xlsx_write": ("sheet", "Sheet1"),
                        "xlsx_group_summary": ("target_sheet", "Summary"),
                        "xlsx_filter_to_sheet": ("target_sheet", None)}
_OVERWRITE_TOOLS = {"move_path", "copy_path"}
_GATED_TOOLS = set(_SHEET_REPLACE_TOOLS) | _OVERWRITE_TOOLS


class VM:
    """Connection to the tool server inside the VM."""

    def __init__(self, url: str = VM_MCP_URL):
        self.client = MultiServerMCPClient({"vm": {"url": url, "transport": "streamable_http"}})
        self.tools = {}
        # Human-approval gate, same contract as LocalVM: approver(request) -> bool; None = deny.
        # It runs HERE on the host, because the VM's server cannot reach the person's terminal.
        self.approver: Callable[[dict], bool] | None = None
        self.approval_log: list[dict] = []
        self.event_sink: Callable[..., Any] | None = None  # e.g. RunLog.write
        # Sheets this run created itself: rebuilding its own output destroys no user data.
        self._owned_sheets: set[tuple[str, str]] = set()

    async def connect(self):
        self.tools = {t.name: t for t in await self.client.get_tools()}
        return self

    def for_worker(self, capability: str):
        # Older tool servers may lack newer tools (e.g. delete_path); skip rather than crash.
        tools = [self.tools[n] for n in WORKER_TOOLS[capability] if n in self.tools]
        return [self._gated(t) if t.name in _GATED_TOOLS else t for t in tools]

    # ---------- approval gate (worker tools only; harness/checker calls via call() are not gated) ----------
    async def _sheet_rows(self, path: str, sheet: str) -> int:
        """Used rows of a sheet in the VM: 0 if the workbook or sheet does not exist, -1 if unknown."""
        info = await self.call("path_info", path=path)
        if not (isinstance(info, dict) and info.get("exists")):
            return 0
        try:
            overview = await self.call("xlsx_info", path=path)
        except Exception:
            return -1                      # exists but unreadable: treat as data at risk
        for entry in (overview or {}).get("sheets", []):
            if entry.get("name") == sheet:
                return int(entry.get("used_rows") or 0)
        return 0

    async def _destruction(self, name: str, args: dict) -> tuple[str | None, tuple[str, str] | None]:
        """(summary of what would be destroyed or None, sheet key this write creates fresh or None)."""
        if name in _OVERWRITE_TOOLS:
            if not args.get("overwrite"):
                return None, None          # the server itself refuses to overwrite
            info = await self.call("path_info", path=args.get("dst"))
            if isinstance(info, dict) and info.get("exists"):
                kind = "folder" if info.get("is_dir") else "file"
                return f"OVERWRITE {args.get('dst')}: the existing {kind} will be destroyed", None
            return None, None
        sheet_arg, default = _SHEET_REPLACE_TOOLS[name]
        path, sheet = args.get("path"), args.get(sheet_arg, default)
        if not path or sheet is None or not args.get("replace_sheet", True):
            return None, None
        if (path, sheet) in self._owned_sheets:
            return None, None
        rows = await self._sheet_rows(path, sheet)
        if rows == 0:
            return None, (path, sheet)
        what = f"its {rows} existing row(s)" if rows > 0 else "its existing contents"
        return f"REPLACE sheet '{sheet}' in {path}: {what} will be destroyed", None

    def _ask(self, tool: str, args: dict, summary: str) -> bool:
        request = {"tool": tool, "args": args, "summary": summary}
        try:
            approved = bool(self.approver(request)) if self.approver else False
        except Exception:
            approved = False               # fail closed
        record = {**request, "approved": approved}
        self.approval_log.append(record)
        if self.event_sink:
            try:
                self.event_sink("approval", **record)
            except Exception:
                pass
        return approved

    def _gated(self, tool):
        """Wrap one VM tool so destructive calls wait for a human yes before reaching the VM."""
        from langchain_core.tools import StructuredTool
        vm = self

        async def guarded(**kwargs):
            try:
                summary, fresh = await vm._destruction(tool.name, kwargs)
            except Exception as exc:
                summary, fresh = (f"{tool.name.upper()} {kwargs.get('path') or kwargs.get('dst')}: "
                                  f"could not check what would be destroyed ({type(exc).__name__})"), None
            if summary:
                shown = {k: v for k, v in kwargs.items() if k != "rows"}
                if not vm._ask(tool.name, shown, summary):
                    return (f"TOOL_ERROR [{tool.name}]: DENIED: the user did not approve {tool.name}. "
                            "Do not retry or look for another way to do this; stop and report that approval was denied.")
            out = await tool.ainvoke(kwargs)
            if fresh and not str(out).startswith("TOOL_ERROR"):
                vm._owned_sheets.add(fresh)
            return out

        return StructuredTool.from_function(coroutine=guarded, name=tool.name,
                                            description=tool.description, args_schema=tool.args_schema)

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
