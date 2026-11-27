"""CLI.

    python -m agent.cli ping                 # check the configured local or VM backend
    python -m agent.cli run "your request"   # free-form request inside the sandbox
    python -m agent.cli task 16 [--reset]    # task setup -> run -> deterministic check
    python -m agent.cli task reflexion       # fault-injected Reflexion test (local mode)
    ... --attempts 3 [--no-lessons]          # Reflexion trials for run / task / eval
    python -m agent.cli eval files --runs 3  # evaluation suite (see agent/evalrun.py)
    python -m agent.cli eval sheet --runs 3  # Excel worker suite (tasks/sheet_suite.py)

Destructive actions (delete_path, overwrite) ask for approval on the terminal;
pass --auto-approve to approve them without asking (used for unattended runs only).

In VM mode, `run` also works without the VM tool server: then only the gui worker is
available, and it operates this PC's own screen (Windows only; GUI_WORKER=off disables it).
"""
import argparse
import asyncio
import os
import subprocess
import uuid
from pathlib import Path

from .graph import run_goal, summarize
from .runtime import AGENT_MODE, AGENT_SCOPE, VM_MCP_URL, create_backend, RunLog, unload_models

RESET = Path(__file__).resolve().parent.parent / "reset_vm.ps1"
_open_backends: list = []


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def terminal_approver(request: dict) -> bool:
    """Human-in-the-loop gate: show exactly what will happen and wait for y/N."""
    print(f"\n  APPROVAL REQUIRED -> {request['summary']}")
    try:
        return input("  Allow this action? [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False  # no terminal attached: fail closed


async def _main():
    ap = argparse.ArgumentParser(prog="agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping")
    r = sub.add_parser("run"); r.add_argument("request"); r.add_argument("--auto-approve", action="store_true")
    t = sub.add_parser("task"); t.add_argument("id", help="task ID (16, quickfix or reflexion)"); t.add_argument("--reset", action="store_true")
    t.add_argument("--auto-approve", action="store_true")
    e = sub.add_parser("eval"); e.add_argument("suite", choices=["files", "sheet"])
    e.add_argument("--runs", type=int, default=3); e.add_argument("--only", default=None)
    e.add_argument("--tag", choices=["core", "extra", "heldout", "all"], default="core")
    e.add_argument("--bound", choices=["on", "off"], default=None, help="ablation: planner-bound move tool")
    e.add_argument("--fastpath", choices=["on", "off"], default=None, help="ablation: phrase-matching deterministic planner")
    for p in (r, t, e):
        p.add_argument("--attempts", type=int, choices=[1, 2, 3], default=None,
                       help="Reflexion trials (default: MAX_ATTEMPTS, else 1)")
        p.add_argument("--no-lessons", action="store_true", help="retry after rollback without lessons")
    a = ap.parse_args()
    if getattr(a, "attempts", None):
        os.environ["MAX_ATTEMPTS"] = str(a.attempts)
    if getattr(a, "no_lessons", False):
        os.environ["REFLEXION_LESSONS"] = "off"

    if a.cmd == "task" and a.reset:
        if AGENT_MODE == "vm":
            subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File", str(RESET)], check=True)
        else:
            print("Local mode: no machine-wide reset. Task setup resets only its own task fixtures inside the configured allowed root.")

    if a.cmd == "task" and a.id in {"quickfix", "reflexion"}:
        os.environ["LOCAL_TRANSPORT"] = "direct"     # fault injector lives in the in-process LocalVM
    if a.cmd == "task" and a.id == "reflexion" and not a.attempts:
        os.environ["MAX_ATTEMPTS"] = "3"
    try:
        vm = await create_backend().connect()
    except Exception as error:
        # VM mode without a reachable tool server: `run` can still use the gui worker alone.
        if a.cmd != "run" or AGENT_MODE != "vm":
            raise SystemExit(f"tool backend not reachable ({type(error).__name__}: {error})")
        vm = None
        print(f"[no tool server at {VM_MCP_URL}: only the gui worker is available; "
              "it will use this PC's screen]")
    else:
        _open_backends.append(vm)
        if hasattr(vm, "approver"):
            vm.approver = (lambda req: True) if getattr(a, "auto_approve", False) else terminal_approver

    if a.cmd == "eval":
        from .evalrun import run_suite
        await run_suite(vm, runs=a.runs, only=a.only, tag=a.tag, bound=a.bound, fastpath=a.fastpath, suite=a.suite)
        return

    if a.cmd == "ping":
        print(f"mode: {AGENT_MODE}")
        print("backend:", type(vm).__name__)
        if AGENT_MODE == "local":
            print(f"scope: {AGENT_SCOPE}")
            print("allowed root:", vm.root)
        print("tools:", sorted(vm.tools))
        print("root contents:", await vm.call("list_dir", path="."))
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
    final = await run_goal(
        task.REQUEST, vm, log, run_id,
        plan_override=getattr(task, "PLAN", None),
    )
    passed, detail = await task.check(vm)          # deterministic: code, not a model
    validate_run = getattr(task, "validate_run", None)
    if callable(validate_run):
        run_passed, run_detail = validate_run(final)
        passed = passed and run_passed
        detail = f"{detail}; {run_detail}"
    log.write("check_task", task=a.id, passed=passed, detail=detail,
              agent_status=final.get("status"), tokens=final.get("tokens_used"))
    # A graph can finish its planned subgoals while a stronger task-level
    # acceptance check still fails. Report the overall task as failed rather
    # than leaving the misleading graph-only status "done".
    final = {**final, "status": "done" if passed else "failed",
             "task_check": {"status": "pass" if passed else "fail", "detail": detail}}
    print(summarize(final))
    print(f"\nTASK {a.id}: {'PASS' if passed else 'FAIL'} - {detail}")
    print(f"log: {log.path}")


async def main():
    try:
        await _main()
    finally:
        for backend in _open_backends:
            if hasattr(backend, "aclose"):
                await backend.aclose()
        # Ollama keeps the model in RAM for ~5 min after the last call; free it now.
        if os.getenv("UNLOAD_MODEL_ON_EXIT", "true").strip().lower() not in {"0", "false", "no"}:
            unload_models()


if __name__ == "__main__":
    asyncio.run(main())
