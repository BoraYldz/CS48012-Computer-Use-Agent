# Keep one offline demonstration for learning. Its decisions are scripted, so it exercises the worker without needing a model or spending API tokens.
# Learning edition: comments explain the code; runtime behavior is unchanged.

# This docstring describes the purpose of the module, class, or function. It is readable through
# Python help() and is kept unchanged.
"""Offline demo: launch Notepad, open Find, dismiss it, then scroll the editor."""
# Import argparse so we can read flags and positional arguments supplied in PowerShell.
import argparse
# Import json so we can convert between Python objects and JSON text.
import json
# Import time so we can measure elapsed time and give the desktop brief pauses.
import time
# Import uuid so we can create unique observation or run identifiers.
import uuid
# Import win32gui so we can inspect windows and manage which window is in front.
import win32gui

# Import GuiTask, execute_gui_task from worker_api; this makes those names available without
# copying their implementation.
from worker_api import GuiTask, execute_gui_task


# Choose the next action by the number of previous actions. This is deliberately scripted for the
# offline demo.
def planner(goal, observation, history, token_cap):
    # Enter this branch only when not history. The indented statements below are conditional.
    if not history:
        # Store the one proposed or normalized action.
        action = {"action": "launch_app", "app_id": "notepad"}
    # Enter this branch only when len(history) == 1. The indented statements below are
    # conditional.
    elif len(history) == 1:
        # Store the one proposed or normalized action.
        action = {"action": "hotkey", "keys": ["ctrl", "f"]}
    # Enter this branch only when len(history) == 2. The indented statements below are
    # conditional.
    elif len(history) == 2:
        # Store the one proposed or normalized action.
        action = {"action": "press_key", "key": "esc"}
    # Enter this branch only when len(history) == 3. The indented statements below are
    # conditional.
    elif len(history) == 3:
        # Store current elements that look like a text editor.
        editors = [e for e in observation["elements"] if e["type"] in ("Document", "Edit")]
        # Store the one proposed or normalized action.
        action = ({"action": "scroll", "element_id": editors[0]["id"], "direction": "down", "amount": 3}
                  # Continue the condition from the previous line before deciding whether this
                  # branch should run.
                  if len(editors) == 1 else {"action": "blocked", "reason": "No unique editor"})
    # Handle the remaining case when the preceding condition did not match.
    else:
        # Store the one proposed or normalized action.
        action = {"action": "done"}
    # Return this value to the caller and finish this function call.
    return {"observation_id": observation["observation_id"], "action": action, "tokens_used": 0}


# Run the entry-point code only when this file is launched directly, not when another module
# imports it.
if __name__ == "__main__":
    # Store the command-line argument parser for this offline demo.
    args_parser = argparse.ArgumentParser(description=__doc__)
    # Declare a command-line option and how argparse should interpret its value.
    args_parser.add_argument("--execute", action="store_true")
    # Store the parsed command-line options.
    args = args_parser.parse_args()
    # Show this diagnostic or result to the person running the command. Never print credentials.
    print("Switch to a controlled test window in 5 seconds.")
    # Show this diagnostic or result to the person running the command. Never print credentials.
    print("Mode:", "REAL app launch, shortcut and scrolling" if args.execute else "PROPOSAL ONLY")
    # Wait briefly without changing the desktop; this gives the app time to update.
    time.sleep(5)
    # Store the validated request describing one subtask and its limits.
    task = GuiTask(uuid.uuid4().hex, "Demonstrate extended GUI tools", win32gui.GetForegroundWindow(),
        # Supply this named argument or setting to the multiline call above. The name before =
        # tells the receiving function what it controls.
        allowed_apps=("notepad",), max_steps=5, timeout_seconds=120)
    # Show this diagnostic or result to the person running the command. Never print credentials.
    print(json.dumps(execute_gui_task(task, planner, execute=args.execute), indent=2))
