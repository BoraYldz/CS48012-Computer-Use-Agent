# Provide a command-line entry point for a real model trial. Default mode still sends data to the model but stops before executing the proposed action.
# Learning edition: comments explain the entry points and provider selection.

# This docstring describes the purpose of the module, class, or function. It is readable through
# Python help() and is kept unchanged.
"""Manual model-driven trial. Default: ONE proposal; --execute enables actions."""
# Import argparse so we can read flags and positional arguments supplied in PowerShell.
import argparse
# Import json so we can convert between Python objects and JSON text.
import json
# Import time so we can measure elapsed time and give the desktop brief pauses.
import time
# The approval question is only asked when a person is at an interactive terminal.
import sys

# Lessons from earlier tasks, and the reflector that writes new ones.
import memory
from reflect import reflect_on_run
from verify import make_done_checker

# ModelPlanner handles action JSON; ProviderFailure carries safe API error metadata.
from model_planner import ModelPlanner, ProviderFailure
# Share provider selection and private credential loading with the MCP entry point.
from provider_client import (API_KEY_NAMES, DEFAULT_REFLECT_MODEL, check_independent_reflector,
                             open_backend)
# Opens the reviewer's client next to the actor's only when a reflection can happen.
from contextlib import ExitStack
# Import GuiTask, execute_gui_task from worker_api; this makes those names available without
# copying their implementation.
from worker_api import GuiTask, execute_gui_task
# CLI choices follow the actual app registry, including installed supported browsers.
from app_tools import APP_REGISTRY


# Read command-line options, prepare the client/task, run it, and print the result.
def main():
    # Store the command-line argument parser.
    parser = argparse.ArgumentParser(description=__doc__)
    # Declare a command-line option and how argparse should interpret its value.
    parser.add_argument("goal", nargs="?", help="Desktop subtask; omitted during API checks")
    # Default to the provider already used successfully in this project; no automatic provider fallback.
    parser.add_argument("--provider", choices=sorted(API_KEY_NAMES), default="openai")
    # This small OpenAI connection test does not capture or control the desktop.
    checks = parser.add_mutually_exclusive_group()
    # The plain connection check expects a short OK reply.
    checks.add_argument("--check-api", action="store_true", help="OpenAI text-only connection test")
    # The format check tests structured action JSON without any desktop observation or input.
    checks.add_argument("--check-action", action="store_true", help="OpenAI text-only action format test")
    # Declare a command-line option and how argparse should interpret its value.
    parser.add_argument("--model", help="OpenAI default: gpt-5.4-mini; Gemini requires an explicit model")
    # Declare a command-line option and how argparse should interpret its value.
    parser.add_argument("--execute", action="store_true")
    # CLI tasks control the desktop by default; --scope window retains the narrower capture option.
    parser.add_argument("--scope", choices=("window", "desktop"), default="desktop",
                        help="window: masked capture; desktop: primary screen and window switching")
    # Declare a command-line option and how argparse should interpret its value.
    parser.add_argument("--omni", action="store_true")
    # Declare a command-line option and how argparse should interpret its value.
    parser.add_argument("--steps", type=int, default=16)
    # Declare a command-line option and how argparse should interpret its value.
    parser.add_argument("--allow-app", action="append", default=None, choices=sorted(APP_REGISTRY),
                        help="Restrict launch tools to these app IDs; default: all registered apps")
    # These are explicit attempt limits; they do not change a provider's billing tier.
    parser.add_argument("--max-tokens", type=int, default=128000)
    # Reserve enough room for screenshot and UI-tree input in each model decision.
    parser.add_argument("--call-token-cap", type=int, default=16000)
    # This remains a cooperative timeout checked between blocking operations.
    parser.add_argument("--timeout", type=int, default=240)
    # Reflexion (Lecture 3): after a failed or stalled attempt, write one lesson to memory.py.
    parser.add_argument("--no-reflect", dest="reflect", action="store_false",
                        help="No model reflection; lesson counters are still updated")
    parser.add_argument("--reflect-token-cap", type=int, default=20000)
    # The reflector is a different model from the actor, so the agent does not grade its own work.
    parser.add_argument("--reflect-provider", choices=sorted(API_KEY_NAMES), default=None,
                        help="Default: the acting provider")
    parser.add_argument("--reflect-model", default=None,
                        help=f"Default for OpenAI: {DEFAULT_REFLECT_MODEL['openai']}; must differ from --model")
    parser.add_argument("--allow-same-reflector", action="store_true",
                        help="Allow the acting model to reflect on its own run (not recommended)")
    # The same reviewer model checks the final screen before "done" is accepted.
    parser.add_argument("--no-done-check", dest="done_check", action="store_false",
                        help="Accept the agent's done without the reviewer's screen check")
    parser.add_argument("--done-check-token-cap", type=int, default=16000)
    # Hard circuit breaker: at most 3 attempts, each one reflecting on the previous failure.
    parser.add_argument("--trials", type=int, choices=(1, 2, 3), default=1,
                        help="Retry a failed attempt up to 3 times in total, with its new lesson")
    # Pre-approve an irreversible target by its exact label, e.g. --approve "Button 'Send'".
    parser.add_argument("--approve", action="append", default=[], metavar="TARGET")
    # Store the parsed command-line options.
    args = parser.parse_args()
    # Use the already-tested OpenAI model unless the user explicitly selects another provider/model.
    if args.model is None:
        if args.provider != "openai":
            parser.error("Provide --model when selecting Gemini")
        args.model = "gpt-5.4-mini"
    # Explicit --allow-app flags still restrict the launcher; omitted flags enable only the fixed registry.
    if args.allow_app is None:
        args.allow_app = sorted(APP_REGISTRY)
    # Pick the reviewing model now, so a same-model setup is refused before any request.
    args.reflect_provider = args.reflect_provider or args.provider
    if args.reflect_model is None:
        args.reflect_model = DEFAULT_REFLECT_MODEL.get(args.reflect_provider)
    # The reviewer model reflects on failures and checks "done"; either use needs a reviewer.
    reflecting = args.execute and (args.reflect or args.done_check)
    if reflecting:
        try:
            check_independent_reflector(args.provider, args.model, args.reflect_provider,
                                        args.reflect_model, args.allow_same_reflector)
        except ValueError as error:
            parser.error(str(error))
    # Reject ambiguous commands before opening a client or accessing the desktop.
    checking = args.check_api or args.check_action
    # A protocol test must not also request a live GUI task.
    if checking and (args.provider != "openai" or args.goal or args.execute):
        # Keep this check deliberately small: OpenAI, text only, and no desktop goal.
        parser.error("API checks require --provider openai, with no goal or --execute")
    # A real GUI trial still needs a concrete task.
    if not checking and not args.goal:
        # argparse displays usage and exits without an API request.
        parser.error("Provide a goal, or use --provider openai with --check-api or --check-action")
    # Handle setup and provider failures without printing credentials or request bodies.
    try:
        # A single shared setup selects the SDK, key, official endpoint, and retry policy.
        with open_backend(args.provider, args.model) as backend:
            # The probe sends only a fixed short text prompt.
            if checking:
                # Let the person running this command know what will be sent.
                print("Sending one short text request to OpenAI; no screenshot or GUI actions.")
                # Output is limited to 128 plain-text or 256 format-test tokens, with no retry.
                reply = backend.check_action_format() if args.check_action else backend.check_connection()
                # OK confirms this key can generate text with the explicitly selected model.
                result = {"status": "ok" if reply.text.strip() == "OK" else "unexpected_response",
                          "provider": args.provider, "model": args.model,
                          "response": reply.text[:200], "tokens_used": reply.tokens_used,
                          "usage_complete": True}
                # A structured probe uses the same parser as a real GUI decision.
                if args.check_action:
                    # Parse the already received reply; this makes no second API request.
                    decision = ModelPlanner.parse_reply(reply, "format-check")
                    # Success requires exactly the requested done action, with no extra tool arguments.
                    result["status"] = "ok" if decision["action"] == {"action": "done"} else "unexpected_response"
                    # Show a structured diagnostic rather than the raw JSON text returned by the provider.
                    result.pop("response")
                    # This is only a format-test result; the worker never executes this synthetic action.
                    result["action"] = decision["action"]
                    # Record an explicit zero so this result cannot be mistaken for a live run.
                    result["actions_executed"] = 0
                    # Attach safe parsing information when the result was malformed.
                    if "parse_error" in decision:
                        # These fields describe error position/type without printing response content.
                        result["parse_error"] = decision["parse_error"]
                # Message phases and lengths help diagnose response selection without exposing text.
                if reply.response_metadata is not None:
                    # The backend constructed this metadata without API credentials or UI contents.
                    result["response_metadata"] = reply.response_metadata
            # A normal invocation runs the same worker regardless of the model provider.
            else:
                # The reviewer gets its own client: a different model reads the trajectory.
                with ExitStack() as stack:
                    reviewer = (stack.enter_context(open_backend(args.reflect_provider, args.reflect_model))
                                if reflecting else None)
                    # This helper owns the countdown and desktop task construction.
                    result = run_desktop_trial(args, backend, reviewer)
    # Local credential errors contain only our own short configuration message.
    except ValueError as error:
        # Do not print arbitrary SDK exceptions, which might include request data.
        result = {"status": "error", "reason": str(error)}
    # Translate safe provider metadata into the same style used by the GUI worker.
    except ProviderFailure as error:
        # An HTTP failure does not prove that the server used zero tokens.
        result = {"status": "error", "provider_error": error.safe_code,
                  "tokens_used": error.tokens_used, "usage_complete": error.usage_complete}
    # Show the result without logging any credential value.
    print(json.dumps(result, indent=2))


# Keeping this separate means neither API check enters the desktop trial code.
def run_desktop_trial(args, backend, reviewer=None):
    # Import win32gui so we can inspect windows and manage which window is in front.
    import win32gui
    # Show this diagnostic or result to the person running the command. Never print credentials.
    print(f"This run sends screenshots and UI labels to {args.provider} ({args.model}).")
    if reviewer is not None:
        roles = [name for name, on in (("checks 'done' on the final screen", args.done_check),
                                       ("writes reflections", args.reflect)) if on]
        print(f"A different model {' and '.join(roles)}: {args.reflect_provider} ({args.reflect_model}).")
    # Before "done" is accepted, the reviewer compares the final screenshot and field read-backs
    # with the goal; a failed check goes back to the agent to fix.
    done_checker = (make_done_checker(reviewer, args.done_check_token_cap)
                    if reviewer is not None and args.done_check else None)
    # Show this diagnostic or result to the person running the command. Never print credentials.
    print("Mode:", "LIVE ACTIONS" if args.execute else "PROPOSAL ONLY")
    # Echo the effective settings so an older copied command is obvious before the countdown.
    print(f"Limits: {args.steps} steps; {args.max_tokens:,} total tokens; "
          f"{args.call_token_cap:,} tokens per call; {args.timeout} seconds.")
    # A launch allowlist is permission, not the task's goal; display it before any model request.
    print("Launch apps permitted:", ", ".join(args.allow_app) or "(none)")
    # Show this diagnostic or result to the person running the command. Never print credentials.
    # Desktop mode needs no empty Notepad: desktop icons or an existing browser are valid starts.
    if args.scope == "desktop":
        # Background apps are visible in this mode, so prepare the screen before the countdown.
        print("Scope: PRIMARY DESKTOP. Close .env and private windows; the whole primary screen is sent.")
        # The foreground is allowed to change after each action, including profile selection.
        print("Prepare your test desktop in 5 seconds; leave the mouse and keyboard idle during the run.")
    else:
        # Keep the narrower workflow available for tasks confined to one application window.
        print("Focus your controlled test window in 5 seconds.")
    lessons = memory.lesson_texts()
    print(f"Active lessons in memory: {len(lessons)} (python memory.py list); "
          "relevant ones go into the system prompt")
    if args.execute and args.trials > 1:
        print(f"Up to {args.trials} attempts. A retry starts from the current screen; nothing is reset.")
    # Wait briefly without changing the desktop; this gives the app time to update.
    time.sleep(5)
    approved, context, attempts, resumes = list(args.approve), {}, [], 0
    handle, trial = win32gui.GetForegroundWindow(), 1
    while True:
        # Store the validated request describing one subtask and its limits.
        task = GuiTask("manual-trial", args.goal, handle, attempt=trial,
                       max_steps=args.steps, use_omni=args.omni, allowed_apps=args.allow_app,
                       # Forward the CLI values to the same contract used by LangGraph and MCP.
                       max_tokens=args.max_tokens, call_token_cap=args.call_token_cap,
                       # The task validator checks allowed ranges before any desktop work.
                       timeout_seconds=args.timeout, scope=args.scope,
                       approved_targets=tuple(approved))
        # A new planner per attempt reloads memory, so a lesson stored a moment ago is already in use.
        result = execute_gui_task(task, ModelPlanner(backend, context=context), execute=args.execute,
                                  done_checker=done_checker)
        # Tier-2 gate: show the pending click to the person at the terminal; nothing has run yet.
        if result.get("status") == "awaiting_approval":
            pending = result["pending_approval"]
            if resumes < 5 and ask_approval(pending):
                approved.append(pending["target"])
                resumes += 1
                # The resumed run sees what already happened and why it was paused.
                context = {"resumed_after_approval": {
                    "approved_target": pending["target"],
                    "earlier_steps": (result.get("steps_summary") or [])[-6:]}}
                handle = refocus(pending.get("window_handle")) or handle
                continue
            result["approval_decision"] = "declined"
            attempts.append(result)
            break
        # Memory update: lesson counters always; a reflection by the separate reviewer model
        # unless --no-reflect. The acting model never reviews its own run.
        if args.execute:
            result["reflection"] = reflect_on_run(result, reviewer, goal=args.goal,
                                                  token_cap=args.reflect_token_cap,
                                                  use_model=args.reflect and reviewer is not None)
            reflection = result["reflection"]
            change = reflection.get("reflection") or {}
            print(f"Memory: {reflection['status']}" +
                  (f" ({change['operation']} {change.get('lesson_id') or ''}) {change.get('lesson', '')}"
                   if change else ""))
        attempts.append(result)
        # Only an unfinished attempt is retried; errors and stops are not the agent's GUI choices.
        if (not args.execute or trial >= args.trials
                or result.get("status") in ("needs_verification", "error", "stopped", "cancelled", "busy")):
            break
        trial, context = trial + 1, {}
        print(f"Attempt {trial}/{args.trials} starts in 5 seconds.")
        time.sleep(5)
        handle = win32gui.GetForegroundWindow()
    final = attempts[-1]
    if len(attempts) > 1:
        final = {**final, "attempts": [
            {"attempt": i, "status": r.get("status"), "tokens_used": r.get("tokens_used"),
             "reflection": (r.get("reflection") or {}).get("status"),
             "lesson": ((r.get("reflection") or {}).get("reflection") or {}).get("lesson")}
            for i, r in enumerate(attempts, start=1)]}
    # Return the structured result to main(), which prints it once.
    return final


# Ask the person at the terminal. Without a terminal (e.g. piped input) the answer is no.
def ask_approval(pending):
    print("\nAPPROVAL NEEDED: the agent wants to", pending.get("reason"))
    print("  action:", json.dumps(pending.get("action")), "| intent:", pending.get("intent"))
    print("  screenshot:", pending.get("screenshot"))
    if not sys.stdin.isatty():
        print("  No interactive terminal: declined.")
        return False
    return input("Approve this one action? [y/N] ").strip().lower() in ("y", "yes")


# Typing the answer focused the terminal; put the task window back in front before resuming.
def refocus(handle):
    if not handle:
        return None
    print("Resuming in 3 seconds; leave the mouse and keyboard idle.")
    try:
        from app_tools import focus_window
        focus_window(handle)
    except Exception:
        return None
    time.sleep(3)
    return handle


# Run the entry-point code only when this file is launched directly, not when another module
# imports it.
if __name__ == "__main__":
    # Start this file's direct-run function now that the entry-point check has passed.
    main()
