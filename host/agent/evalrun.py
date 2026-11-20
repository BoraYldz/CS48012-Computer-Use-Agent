"""Evaluation runner: N runs per task -> success, pass^k, false-success, steps, tokens.

    python -m agent.cli eval files --runs 3                 # core tasks x 3
    python -m agent.cli eval sheet --runs 3                 # Excel worker suite x 3
    python -m agent.cli eval files --runs 1 --only t01,t09  # quick smoke
    python -m agent.cli eval files --runs 3 --bound on      # ablation: planner-bound move tool

Terminology
  passed         final STATE correct (independent ground-truth check) AND status honest
                 (status == done where expected, != done where the task must be refused)
  pass^k         task counts only if ALL k runs passed (consistency, not luck)
  false success  graph reported "done" but `passed` is False  <- verifier/planner failure
  false fail     graph did not report done although the final state was correct
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from tasks import files_suite, sheet_suite

from .graph import run_goal
from .runtime import AGENT_SCOPE, RUNS_DIR, RunLog, max_steps, unload_models


SUITES = {"files": files_suite, "sheet": sheet_suite}


async def run_one(vm, task, k: int) -> dict:
    base = await files_suite.reset(vm, task)       # generic: wipes only _eval/<task id>/ and re-runs task.setup
    policy = task.approve
    vm.approver = lambda req, _p=policy: _p          # approval policy for this task
    run_id = f"eval-{task.id}-r{k}-{uuid.uuid4().hex[:6]}"
    t0 = time.time()
    crashed = None
    try:
        final = await run_goal(task.request(base), vm, RunLog(run_id), run_id)
    except Exception as exc:  # a crash is a failed run, not a failed eval
        final = {"status": "crash", "approvals": list(vm.approval_log)}
        crashed = f"{type(exc).__name__}: {str(exc)[:200]}"
    elapsed = time.time() - t0
    status = final.get("status")
    ctx = {"approvals": final.get("approvals", []), "status": status}
    try:
        state_ok, detail = await task.check(vm, base, ctx)
    except Exception as exc:
        state_ok, detail = False, f"check crashed: {type(exc).__name__}: {exc}"
    status_ok = task.expect_done is None or ((status == "done") == task.expect_done)
    passed = bool(state_ok and status_ok)
    return {
        "task": task.id, "run": k, "run_id": run_id, "status": status,
        "state_ok": state_ok, "status_ok": status_ok, "passed": passed,
        "false_success": status == "done" and not passed,
        "false_fail": status != "done" and state_ok and task.expect_done is True,
        "detail": detail if state_ok else f"{detail}" + ("" if status_ok else f" | status={status}, expected done={task.expect_done}"),
        "steps": sum((final.get("step_counts") or {}).values()),
        "tokens": final.get("tokens_used", 0),
        "quick_fixes": sum((final.get("quick_fix_attempts") or {}).values()),
        "approvals": len(ctx["approvals"]),
        "seconds": round(elapsed, 1),
        "crash": crashed,
    }


def aggregate(rows: list[dict], runs: int) -> dict:
    by_task: dict[str, list[dict]] = {}
    for r in rows:
        by_task.setdefault(r["task"], []).append(r)
    per_task = {}
    for tid, rs in by_task.items():
        per_task[tid] = {
            "passes": sum(r["passed"] for r in rs), "runs": len(rs),
            f"pass^{runs}": all(r["passed"] for r in rs) and len(rs) == runs,
            "false_success": sum(r["false_success"] for r in rs),
            "mean_steps": round(sum(r["steps"] for r in rs) / len(rs), 1),
            "mean_tokens": round(sum(r["tokens"] for r in rs) / len(rs)),
            "mean_seconds": round(sum(r["seconds"] for r in rs) / len(rs), 1),
            "quick_fixes": sum(r["quick_fixes"] for r in rs),
        }
    n = len(rows) or 1
    return {
        "tasks": len(by_task), "runs_total": len(rows),
        "success_rate": round(sum(r["passed"] for r in rows) / n, 3),
        f"pass^{runs}_rate": round(sum(v[f"pass^{runs}"] for v in per_task.values()) / (len(per_task) or 1), 3),
        "false_success": sum(r["false_success"] for r in rows),
        "false_fail": sum(r["false_fail"] for r in rows),
        "crashes": sum(1 for r in rows if r["crash"]),
        "mean_steps": round(sum(r["steps"] for r in rows) / n, 1),
        "mean_tokens": round(sum(r["tokens"] for r in rows) / n),
        "per_task": per_task,
    }


def markdown(summary: dict, config: dict, runs: int) -> str:
    key = f"pass^{runs}"
    lines = [f"**Config:** {json.dumps(config)}", "",
             f"| task | passes | {key} | false-success | steps | tokens | sec |",
             "|---|---|---|---|---|---|---|"]
    for tid, v in summary["per_task"].items():
        lines.append(f"| {tid} | {v['passes']}/{v['runs']} | {'yes' if v[key] else 'NO'} | "
                     f"{v['false_success']} | {v['mean_steps']} | {v['mean_tokens']} | {v['mean_seconds']} |")
    lines += ["", f"**Success rate** {summary['success_rate']:.0%} · **{key} rate** {summary[key + '_rate']:.0%} · "
              f"**false successes** {summary['false_success']} · false fails {summary['false_fail']} · "
              f"crashes {summary['crashes']} · mean steps {summary['mean_steps']} · mean tokens {summary['mean_tokens']}"]
    return "\n".join(lines)


async def run_suite(vm, runs: int, only: str | None, tag: str, bound: str | None, suite: str = "files") -> dict:
    if bound:
        os.environ["BOUND_MOVE"] = "on" if bound == "on" else "off"
    tasks = SUITES[suite].select(only=only, tag=tag)
    if not tasks:
        raise SystemExit(f"no tasks matched only={only!r} tag={tag!r}")
    config = {
        "suite": suite, "scope": AGENT_SCOPE, "root": str(vm.root), "runs": runs,
        "bound_move": os.getenv("BOUND_MOVE", "off"),
        "worker_model": os.getenv("WORKER_MODEL", os.getenv("DEFAULT_MODEL", "qwen3:8b")),
        "planner_model": os.getenv("PLANNER_MODEL", os.getenv("DEFAULT_MODEL", "qwen3:8b")),
        "max_steps": {"files": max_steps("files"), "sheet": max_steps("sheet")},
    }
    print(f"eval: {len(tasks)} task(s) x {runs} run(s) under {vm.root}/{files_suite.EVAL_ROOT}/")
    rows: list[dict] = []
    try:
        for task in tasks:
            for k in range(1, runs + 1):
                row = await run_one(vm, task, k)
                rows.append(row)
                mark = "PASS" if row["passed"] else "FAIL"
                print(f"\n>>> {task.id} run {k}: {mark} (status={row['status']}, steps={row['steps']}, "
                      f"tokens={row['tokens']}, {row['seconds']}s) {row['detail']}\n")
    finally:
        unload_models()
    summary = aggregate(rows, runs)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    RUNS_DIR.mkdir(exist_ok=True)
    out = RUNS_DIR / f"eval-{suite}-{stamp}"
    out.with_suffix(".json").write_text(json.dumps({"config": config, "summary": summary, "rows": rows},
                                                   indent=2, ensure_ascii=False))
    md = markdown(summary, config, runs)
    out.with_suffix(".md").write_text(md)
    print(md)
    print(f"\nsaved: {out.with_suffix('.json')} and {out.with_suffix('.md')}")
    return summary
