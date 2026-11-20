"""GUI worker adapter: runs the screen-driven worker in ../gui_worker for one subgoal.

Unlike the files/sheet workers (a ReAct agent over VM tools), the GUI worker runs its own
observe -> decide -> act loop on THIS machine's screen and returns a result object. This module
turns one Subgoal into a GuiTask, runs it, and maps the result back to the graph's Step history
and a DONE:/FAILED: finding, so the existing check node works unchanged.

It needs no VM. Its model key is read from gui_worker/.env (OPENAI_API_KEY).
"""
import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path

GUI_DIR = Path(__file__).resolve().parents[2] / "gui_worker"

# Configuration (host/.env). The reviewer model checks "done" and writes lessons; it must differ
# from the acting model so the worker never grades its own work.
GUI_PROVIDER = os.getenv("GUI_PROVIDER", "openai")
GUI_MODEL = os.getenv("GUI_MODEL", "gpt-5.4-mini")
GUI_REVIEW_MODEL = os.getenv("GUI_REVIEW_MODEL", "gpt-5.5")
GUI_EXECUTE = os.getenv("GUI_EXECUTE", "1") == "1"          # 0 = propose one action, act on nothing
GUI_MAX_STEPS = int(os.getenv("GUI_MAX_STEPS", "16"))         # 1-20
GUI_MAX_TOKENS = int(os.getenv("GUI_MAX_TOKENS", "128000"))
GUI_CALL_TOKEN_CAP = int(os.getenv("GUI_CALL_TOKEN_CAP", "16000"))
GUI_TIMEOUT = int(os.getenv("GUI_TIMEOUT", "240"))            # seconds, <= 600

HELP = ("operate applications and websites through the screen on this Windows PC (browser, "
        "Gmail, YouTube, desktop apps): clicks, typing, navigation. Slower and less reliable than "
        "tool workers; use it when no other worker's tools can do the job")


def _modules():
    """Import the GUI worker lazily, so the host still runs without its Windows-only packages."""
    if str(GUI_DIR) not in sys.path:
        sys.path.insert(0, str(GUI_DIR))
    import app_tools, model_planner, provider_client, reflect, verify, worker_api  # noqa: E401
    return app_tools, model_planner, provider_client, reflect, verify, worker_api


def _ask_approval(pending) -> bool:
    print("\nAPPROVAL NEEDED: the GUI worker wants to", pending.get("reason"))
    print("  intent:", pending.get("intent"), "| screenshot:", pending.get("screenshot"))
    if not sys.stdin.isatty():
        print("  No interactive terminal: declined.")
        return False
    return input("Approve this one action? [y/N] ").strip().lower() in ("y", "yes")


def run_gui_subgoal(instruction: str, expected_output: str, overall_goal: str,
                    lessons: list[str], task_id: str, token_allowance: int) -> dict:
    """Blocking: run the GUI worker for one subgoal and return its result dict."""
    app_tools, model_planner, provider_client, reflect, verify, worker_api = _modules()
    provider_client.check_independent_reflector(GUI_PROVIDER, GUI_MODEL, GUI_PROVIDER, GUI_REVIEW_MODEL)
    max_tokens = min(GUI_MAX_TOKENS, token_allowance)
    if max_tokens < GUI_CALL_TOKEN_CAP:
        return {"status": "token_limit", "reason": "task token budget too small for a GUI step",
                "tokens_used": 0, "steps_summary": []}
    import win32gui
    goal = f"{instruction}\nDone means: {expected_output}"
    # The overall request is context only; lessons from the orchestrator join the stored ones.
    context = {"overall_goal": overall_goal}
    if lessons:
        context["lessons"] = list(lessons)
    approved, resumes = [], 0
    with ExitStack() as stack:
        actor = stack.enter_context(provider_client.open_backend(GUI_PROVIDER, GUI_MODEL))
        reviewer = stack.enter_context(provider_client.open_backend(GUI_PROVIDER, GUI_REVIEW_MODEL))
        handle = win32gui.GetForegroundWindow()
        while True:
            task = worker_api.GuiTask(task_id, goal, handle, max_steps=GUI_MAX_STEPS,
                                      max_tokens=max_tokens, call_token_cap=GUI_CALL_TOKEN_CAP,
                                      timeout_seconds=GUI_TIMEOUT, scope="desktop",
                                      allowed_apps=sorted(app_tools.APP_REGISTRY),
                                      approved_targets=tuple(approved))
            planner = model_planner.ModelPlanner(actor, context=dict(context))
            result = worker_api.execute_gui_task(task, planner, execute=GUI_EXECUTE,
                                                 done_checker=verify.make_done_checker(reviewer))
            # Irreversible clicks (Send, Delete, Gönder ...) wait for the person at the terminal.
            if result.get("status") == "awaiting_approval" and resumes < 3:
                pending = result["pending_approval"]
                if _ask_approval(pending):
                    approved.append(pending["target"])
                    resumes += 1
                    context["resumed_after_approval"] = {
                        "approved_target": pending["target"],
                        "earlier_steps": (result.get("steps_summary") or [])[-6:]}
                    if pending.get("window_handle"):
                        try:
                            app_tools.focus_window(pending["window_handle"])
                            handle = pending["window_handle"]
                        except Exception:
                            pass
                    time.sleep(2)
                    continue
            # Lesson memory: counters always; a reviewer reflection after a failed/wasteful run.
            if GUI_EXECUTE:
                result["reflection"] = reflect.reflect_on_run(result, reviewer, goal=goal)
            return result


def gui_findings(result: dict, subgoal_id: int) -> tuple[list[dict], str, int]:
    """Map a GUI result to (Step list, DONE:/FAILED: summary, tokens) for the orchestrator."""
    steps = []
    for entry in result.get("steps_summary") or []:
        action = dict(entry.get("action") or {})
        name = action.pop("action", "?")
        outcome = entry.get("effect") or entry.get("reason") or ""
        steps.append({"subgoal_id": subgoal_id, "worker": "gui", "tool": name, "args": action,
                      "observation": f"{entry.get('result')}: {outcome}"[:500]})
    status = result.get("status")
    tokens = int(result.get("tokens_used") or 0) + int((result.get("reflection") or {}).get("tokens_used") or 0)
    evidence = (result.get("evidence") or {}).get("log_path") or result.get("log_path")
    if status == "needs_verification":
        checked = any(c.get("goal_met") is True for c in result.get("done_checks") or [])
        summary = (f"DONE: GUI worker completed the subgoal in {result.get('actions_executed', 0)} actions"
                   f"{'; reviewer confirmed it on the final screen' if checked else ''}; log {evidence}")
    elif status == "awaiting_approval":
        target = (result.get("pending_approval") or {}).get("target")
        summary = f"FAILED: GUI worker stopped before an irreversible action that was not approved ({target})"
    elif status == "proposed":
        summary = "FAILED: GUI_EXECUTE=0, the GUI worker only proposed an action"
    else:
        summary = f"FAILED: GUI worker ended with {status}: {str(result.get('reason') or '')[:200]}"
    return steps, summary, tokens
