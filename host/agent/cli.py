"""CLI.

    python -m agent.cli ping                 # is the VM tool server reachable?
    python -m agent.cli run "your request"   # free-form request
    python -m agent.cli task 16 [--reset]    # suite task: (reset VM) -> setup -> run -> check

`run` also works without the VM tool server: then only the gui worker is available, and it
operates this PC's own screen.
"""
import argparse
import asyncio
import subprocess
import uuid
from pathlib import Path

from .graph import run_goal, summarize
from .runtime import VM, VM_MCP_URL, RunLog

RESET = Path(__file__).resolve().parent.parent / "reset_vm.ps1"


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def main():
    ap = argparse.ArgumentParser(prog="agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping")
    r = sub.add_parser("run"); r.add_argument("request")
    t = sub.add_parser("task"); t.add_argument("id"); t.add_argument("--reset", action="store_true")
    a = ap.parse_args()

    if a.cmd == "task" and a.reset:
        subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File", str(RESET)], check=True)

    try:
        vm = await VM().connect()
    except Exception as error:  # no tool server: only the gui worker can run
        if a.cmd != "run":
            raise SystemExit(f"VM tool server not reachable at {VM_MCP_URL} ({type(error).__name__})")
        vm = None
        print(f"[no tool server at {VM_MCP_URL}: only the gui worker is available; "
              "it will use this PC's screen]")

    if a.cmd == "ping":
        print("tools:", sorted(vm.tools))
        print("home:", await vm.call("list_dir", path="."))
        return

    if a.cmd == "run":
        run_id = new_id("run")
        final = await run_goal(a.request, vm, RunLog(run_id), run_id)
        print(summarize(final))
        return

    from tasks import load
    task = load(a.id)
    run_id = new_id(f"task{a.id}")
    log = RunLog(run_id)
    await task.setup(vm)
    final = await run_goal(task.REQUEST, vm, log, run_id)
    passed, detail = await task.check(vm)          # deterministic: code, not a model
    log.write("check_task", task=a.id, passed=passed, detail=detail,
              agent_status=final.get("status"), tokens=final.get("tokens_used"))
    print(summarize(final))
    print(f"\nTASK {a.id}: {'PASS' if passed else 'FAIL'} - {detail}")
    print(f"log: {log.path}")


if __name__ == "__main__":
    asyncio.run(main())
