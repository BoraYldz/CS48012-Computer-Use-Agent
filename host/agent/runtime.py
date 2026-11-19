"""Config, models, VM tools and run logging."""
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

load_dotenv()

VM_MCP_URL = os.getenv("VM_MCP_URL", "http://localhost:8765/mcp")
MAX_STEPS = int(os.getenv("MAX_STEPS_PER_SUBGOAL", "15"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS_PER_TASK", "200000"))
RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"

# Which tools each worker may use (least privilege). Harness tools are never listed.
WORKER_TOOLS = {
    "files": ["list_dir", "path_info", "make_dirs", "move_path", "copy_path",
              "read_text", "write_text", "read_pdf_text"],
    "sheet": ["list_dir", "path_info", "read_text", "read_pdf_text", "xlsx_read", "xlsx_write"],
}


def model(role: str) -> ChatOpenAI:
    name = os.getenv(f"{role.upper()}_MODEL", "qwen3:14b")
    return ChatOpenAI(model=name, base_url=os.getenv("LLM_BASE_URL"),
                      api_key=os.getenv("LLM_API_KEY", "none"), temperature=0)


class VM:
    """Connection to the tool server inside the VM."""

    def __init__(self, url: str = VM_MCP_URL):
        self.client = MultiServerMCPClient({"vm": {"url": url, "transport": "streamable_http"}})
        self.tools = {}

    async def connect(self):
        self.tools = {t.name: t for t in await self.client.get_tools()}
        return self

    def for_worker(self, capability: str):
        return [self.tools[n] for n in WORKER_TOOLS[capability]]

    async def call(self, name: str, **args):
        """Direct call (used by the harness and checkers). Returns parsed JSON when possible."""
        msg = await self.tools[name].ainvoke(
            {"type": "tool_call", "id": "harness", "name": name, "args": args})
        sc = (msg.artifact or {}).get("structured_content") if msg.artifact else None
        if sc is not None:  # FastMCP wraps non-dict returns as {"result": ...}
            return sc["result"] if set(sc) == {"result"} else sc
        text = "".join(b.get("text", "") if isinstance(b, dict) else str(b)
                       for b in (msg.content if isinstance(msg.content, list) else [msg.content]))
        if getattr(msg, "status", "success") == "error":
            raise RuntimeError(f"{name} failed: {text}")
        try:
            return json.loads(text)
        except ValueError:
            return text


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
