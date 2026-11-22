"""Compact failure digest for an eval run: paste its output instead of raw JSONL logs.

    python -m agent.digest                       # newest runs/eval-*.json
    python -m agent.digest runs/eval-files-....json --only t02,t04

For every failed run it prints the ground-truth detail, the planned subgoals with their
check_spec, the worker tool calls, and what the deterministic checker said.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runtime import RUNS_DIR


def _clip(value, n: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 3] + "..."


def _find_subgoals(rec: dict) -> list[dict]:
    for value in rec.values():
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value) \
                and any("check_spec" in v for v in value):
            return value
    return []


def digest_run(run_id: str, width: int = 260) -> list[str]:
    path = RUNS_DIR / f"{run_id}.jsonl"
    if not path.exists():
        return [f"  (no log file {path.name})"]
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ev = rec.get("event", "")
        if ev == "plan":
            for sg in _find_subgoals(rec):
                checks = (sg.get("check_spec") or {}).get("checks", [])
                out.append(f"  PLAN [{sg.get('capability')}] {_clip(sg.get('instruction', ''), width)}")
                out.append(f"       checks={_clip(checks, width)}")
        elif ev == "tool_start":
            out.append(f"  TOOL {rec.get('tool')} {_clip(rec.get('args'), width)}")
        elif ev == "worker_tool_error":
            out.append(f"  TOOL_ERROR {_clip(rec, width)}")
        elif ev == "worker_end":
            out.append(f"  WORKER_SAYS {_clip(rec.get('summary', ''), width)}")
        elif ev == "check_subgoal":
            out.append(f"  CHECK {rec.get('status')} :: {_clip(rec.get('evidence', ''), width)}")
        elif ev in ("quick_fix", "quick_fix_blocked", "planner_error", "planner_timeout", "worker_limit",
                    "worker_loop_detected"):
            out.append(f"  {ev.upper()} {_clip({k: v for k, v in rec.items() if k not in ('t', 'event')}, width)}")
        elif ev.startswith("plan_") and ev.endswith("normalization") or ev in ("plan_spreadsheet_workflow_normalization",):
            out.append(f"  REWRITE {ev} {_clip({k: v for k, v in rec.items() if k not in ('t', 'event')}, width)}")
        elif ev == "run_end":
            out.append(f"  RUN_END {_clip({k: v for k, v in rec.items() if k not in ('t', 'event')}, width)}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("report", nargs="?", default=None)
    ap.add_argument("--only", default=None, help="comma separated task ids or prefixes (t02,s12)")
    ap.add_argument("--width", type=int, default=260)
    a = ap.parse_args()
    report = Path(a.report) if a.report else max(RUNS_DIR.glob("eval-*.json"), key=lambda p: p.stat().st_mtime)
    data = json.loads(report.read_text(encoding="utf-8"))
    wanted = {w.strip() for w in a.only.split(",")} if a.only else None
    print(f"# {report.name}  config={_clip(data.get('config', {}), 400)}")
    for row in data["rows"]:
        if row["passed"]:
            continue
        if wanted and not any(row["task"] == w or row["task"].startswith(w) for w in wanted):
            continue
        print(f"\n## {row['task']} run{row['run']} status={row['status']} false_success={row['false_success']} "
              f"steps={row['steps']} qf={row['quick_fixes']} :: {_clip(row['detail'], 400)}")
        print("\n".join(digest_run(row["run_id"], a.width)))


if __name__ == "__main__":
    main()
