# Expose the same worker through three MCP tools. MCP carries requests between programs; it does not itself choose GUI actions or provide the vision model.
# Learning edition: comments explain how MCP calls the shared GUI worker.

# This docstring describes the purpose of the module, class, or function. It is readable through
# Python help() and is kept unchanged.
"""Local MCP transport for the same worker used by LangGraph. No raw shell tool."""
# Import argparse so we can read flags and positional arguments supplied in PowerShell.
import argparse
# Resources return JSON text; run ids are checked with a regular expression.
import json
import re
from pathlib import Path

# Persistent lessons and the reflector that writes them.
import memory
from reflect import reflect_on_run
from verify import make_done_checker
from contextlib import ExitStack

# Import MCPServer from mcp.server; this makes those names available without copying their
# implementation.
from mcp.server import MCPServer

# Import APP_REGISTRY, list_windows from app_tools; this makes those names available without
# copying their implementation.
from app_tools import APP_REGISTRY, list_windows
# The capability list follows the same contract used by the model and validator.
from action_contract import ACTION_FIELDS
# Import KEYS, SHORTCUTS from input_policy; this makes those names available without copying their
# implementation.
from input_policy import KEYS, SHORTCUTS
# ModelPlanner uses whichever backend the server host configured.
from model_planner import ModelPlanner
# CLI and MCP read credentials and construct clients in the same way.
from provider_client import (API_KEY_NAMES, DEFAULT_REFLECT_MODEL, check_independent_reflector,
                             open_backend)
# Import GuiTask, execute_gui_task from worker_api; this makes those names available without
# copying their implementation.
from worker_api import GuiTask, execute_gui_task


# Create and register our three MCP tools without starting a network call or desktop task yet.
def build_server(model=None, allow_execution=False, provider="gemini", reflect_model=None,
                 reflect_provider=None, allow_same_reflector=False):
    # Provider choice is host configuration, not something a model-selected task can change.
    if provider not in API_KEY_NAMES:
        # Catch invalid configuration before registering tools or accepting tasks.
        raise ValueError("Provider must be gemini or openai")
    # The reviewer is a different model from the actor; without one, only counters are updated.
    reflect_provider = reflect_provider or provider
    if reflect_model is None:
        reflect_model = DEFAULT_REFLECT_MODEL.get(reflect_provider)
    if reflect_provider not in API_KEY_NAMES:
        raise ValueError("Reflect provider must be gemini or openai")
    if model and reflect_model:
        check_independent_reflector(provider, model, reflect_provider, reflect_model, allow_same_reflector)
    # Store the MCP server object holding registered tools.
    server = MCPServer("Ali GUI Worker")

    # Register the following Python function as a discoverable MCP tool; its parameters describe
    # the call arguments.
    @server.tool()
    # Describe available actions, registered apps, and host execution mode without capturing the
    # screen.
    def gui_capabilities() -> dict:
        # This docstring describes the purpose of the module, class, or function. It is readable
        # through Python help() and is kept unchanged.
        """Read supported actions, app IDs, and execution mode; no screen capture."""
        # Return this value to the caller and finish this function call.
        return capabilities()

    # Read-only data is also published as MCP resources (Lecture 4: tools act, resources inform).
    # The tool versions stay for clients that only support tools.
    def capabilities():
        return {"apps": sorted(APP_REGISTRY), "keys": sorted(KEYS),
                # Include the shortcuts field in this structured record; the receiver reads it by
                # this key.
                "shortcuts": [list(k) for k in sorted(SHORTCUTS)],
                # Include the actions field in this structured record; the receiver reads it by
                # this key.
                "actions": list(ACTION_FIELDS),
                # Desktop scope allows full primary-screen capture and arbitrary visible app changes.
                "scopes": ["window", "desktop"], "default_scope": "window",
                # Hosts can describe this restriction when exposing tools to their orchestrator.
                "desktop_only_actions": ["click_at"],
                # Include the execution_enabled field in this structured record; the receiver
                # reads it by this key.
                "execution_enabled": allow_execution, "model_configured": bool(model),
                # Include the transport field in this structured record; the receiver reads it by
                # this key.
                "transport": "stdio", "monitor_support": "primary",
                # Help the orchestrator inspect which provider the host selected.
                "provider": provider, "model": model,
                # A different model reviews failed runs and updates lesson memory.
                "reflect_provider": reflect_provider, "reflect_model": reflect_model,
                # Irreversible-looking clicks pause with status awaiting_approval.
                "approval": "resend the request with approved_targets=[pending_approval.target]",
                "lessons_in_memory": len(memory.load_lessons()),
                "resources": ["gui://capabilities", "gui://windows", "gui://memory/lessons",
                              "gui://runs/{run_id}/result", "gui://runs/{run_id}/screenshot"]}

    @server.resource("gui://capabilities", mime_type="application/json",
                     description="Supported actions, apps, keys and execution mode")
    def capabilities_resource() -> str:
        return json.dumps(capabilities())

    @server.resource("gui://windows", mime_type="application/json",
                     description="Visible desktop windows and their handles, read live")
    def windows_resource() -> str:
        return json.dumps(list_windows(), ensure_ascii=False)

    @server.resource("gui://memory/lessons", mime_type="application/json",
                     description="Lesson memory: active and archived lessons with use counters and history")
    def lessons_resource() -> str:
        return json.dumps(memory.load_lessons(include_archived=True), ensure_ascii=False)

    @server.resource("gui://runs/{run_id}/result", mime_type="application/json",
                     description="Final result and evidence of one GUI run")
    def run_result_resource(run_id: str) -> str:
        return json.dumps(load_run_result(run_id), ensure_ascii=False)

    @server.resource("gui://runs/{run_id}/screenshot", mime_type="image/png",
                     description="Last screenshot of one GUI run")
    def run_screenshot_resource(run_id: str) -> bytes:
        path = (load_run_result(run_id).get("evidence") or {}).get("last_screenshot")
        if not path or not inside_outputs(path):
            raise ValueError("This run has no screenshot")
        return Path(path).read_bytes()

    # Register the following Python function as a discoverable MCP tool; its parameters describe
    # the call arguments.
    @server.tool()
    # Expose visible-window metadata so an orchestrator can choose actual window handles.
    def gui_windows() -> list[dict]:
        # This docstring describes the purpose of the module, class, or function. It is readable
        # through Python help() and is kept unchanged.
        """List visible desktop windows for selecting a task's target handles."""
        # Return this value to the caller and finish this function call.
        return list_windows()

    # Register the following Python function as a discoverable MCP tool; its parameters describe
    # the call arguments.
    @server.tool()
    # Validate an MCP task request, create the configured model client, and invoke the shared
    # worker API.
    def run_gui_subtask(request: dict, execute: bool = False, lessons: list[str] | None = None,
                        reflect: bool = True, check_done: bool = True) -> dict:
        # This docstring describes the purpose of the module, class, or function. It is readable
        # through Python help() and is kept unchanged.
        """Run one bounded GUI subtask; defaults to model proposal without actions.

        Request fields match GuiTask. Returns status and evidence, never verified
        success. Live actions also require the host's --allow-execution flag.
        This tool sends screenshots to the model chosen by the server host.
        scope="window" masks outside the selected window (the default).
        scope="desktop" sends the whole primary screen and allows switching apps;
        window_handle may be 0 in desktop scope when no app is in front.
        allowed_apps limits the named launch tools, not desktop-mode visibility.
        lessons: extra lessons for this task only; stored lessons are always used.
        reflect: after a failed or stalled live run, let the reflector update lesson memory.
        Lesson use counters are updated after every live run either way.
        check_done: before "done" is accepted, the reviewer model compares the final screen and
        typed-field read-backs with the goal; a failed check goes back to the agent to fix, and a
        second failure ends the run as "done_rejected".
        status "awaiting_approval": a person must approve pending_approval.target; then
        resend the request with approved_targets=[that target].
        """
        # Require both the MCP caller request and the server host setting before allowing live
        # actions.
        if execute and not allow_execution:
            # Return this value to the caller and finish this function call.
            return {"status": "blocked", "verified": False,
                    # Include the reason field: a short explanation for a blocked/error result.
                    "reason": "Host has not enabled desktop execution"}
        # Attempt the following operations while allowing the nearby except/finally blocks to
        # handle failure and cleanup.
        try:
            # Store the validated request describing one subtask and its limits.
            task = GuiTask(**request)
            # Enforce the task contract before observations or model calls start.
            task.validate()
        # Handle this failure path without continuing with an unsafe or incomplete operation.
        except (ValueError, TypeError):
            # Return this value to the caller and finish this function call.
            return {"status": "error", "verified": False, "reason": "Invalid GuiTask request"}
        # Enter this branch only when not model. The indented statements below are conditional.
        if not model:
            # Return this value to the caller and finish this function call.
            return {"status": "blocked", "verified": False, "reason": "No model configured by host"}
        # Report missing local setup without invoking the GUI worker.
        try:
            # The host-selected provider uses its own key and closes its client on exit.
            with ExitStack() as stack:
                backend = stack.enter_context(open_backend(provider, model))
                # The separate reviewer model (never the acting model) checks "done" and reflects.
                reviewer = (stack.enter_context(open_backend(reflect_provider, reflect_model))
                            if execute and reflect_model and (reflect or check_done) else None)
                # Both providers return the same strict action contract to the shared worker.
                extra = [str(text) for text in (lessons or [])][:10]
                planner = ModelPlanner(backend, context={"lessons": extra} if extra else None)
                # Existing budgets, desktop ownership checks, and evidence still apply.
                result = execute_gui_task(task, planner, execute=execute,
                                          done_checker=make_done_checker(reviewer)
                                          if reviewer is not None and check_done else None)
                # Memory update: lesson counters always; a reflection after a failed or stalled
                # attempt when reflect is true.
                if execute and result.get("log_path"):
                    result["reflection"] = reflect_on_run(result, reviewer, goal=task.goal,
                                                          use_model=reflect and reviewer is not None)
            return result
        # Credential-loading errors contain our own text, not a raw provider response.
        except ValueError:
            # Preserve the MCP result shape while explaining the local setup problem.
            return {"status": "blocked", "verified": False,
                    "reason": "Missing or invalid local provider configuration"}

    @server.tool()
    def gui_reflect(run_id: str, feedback: str | None = None) -> dict:
        """Update lesson memory from a finished run, e.g. after the orchestrator's own check.

        feedback: the checker's failure verdict. It turns the run's outcome into a failure for the
        lessons it used, and the reflector (which sees trajectory and screenshots) may add,
        revise or vote to retire one lesson, citing evidence. Without a failure signal only the
        counters change.
        """
        if not reflect_model:
            return {"status": "blocked", "reason": "No reflector model configured by host"}
        try:
            result = load_run_result(run_id)
        except ValueError as error:
            return {"status": "error", "reason": str(error)}
        try:
            # The reviewer, not the acting model, judges the run.
            with open_backend(reflect_provider, reflect_model) as reviewer:
                return reflect_on_run(result, reviewer, feedback=feedback)
        except ValueError:
            return {"status": "blocked", "reason": "Missing or invalid local provider configuration"}

    # Return the configured server to the caller; serving requests starts separately with run().
    return server


# Runs live in outputs/run_<32 hex>; never read a path the caller made up.
OUTPUTS = Path(__file__).resolve().parent / "outputs"


def inside_outputs(path):
    try:
        return Path(path).resolve().is_relative_to(OUTPUTS)
    except (OSError, ValueError):
        return False


def load_run_result(run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("run_id must be the 32-character id from a result")
    path = OUTPUTS / f"run_{run_id}" / "result.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError("No finished run with this id") from None


# Run the entry-point code only when this file is launched directly, not when another module
# imports it.
if __name__ == "__main__":
    # Store the command-line argument parser.
    parser = argparse.ArgumentParser(description=__doc__)
    # Declare a command-line option and how argparse should interpret its value.
    parser.add_argument("--model", default=None)
    # Select the API provider once for this server; request callers cannot override it.
    parser.add_argument("--provider", choices=sorted(API_KEY_NAMES), default="gemini")
    # Declare a command-line option and how argparse should interpret its value.
    parser.add_argument("--allow-execution", action="store_true")
    # A different model reviews failed runs; it must not be the acting model.
    parser.add_argument("--reflect-provider", choices=sorted(API_KEY_NAMES), default=None)
    parser.add_argument("--reflect-model", default=None)
    parser.add_argument("--allow-same-reflector", action="store_true")
    # Store the parsed command-line options.
    args = parser.parse_args()
    # Start the MCP server using the subprocess standard-input/output transport; it now waits for
    # host requests.
    build_server(args.model, args.allow_execution, args.provider, args.reflect_model,
                 args.reflect_provider, args.allow_same_reflector).run(transport="stdio")
