# Adapt our ordinary Python worker function into a LangGraph node. A node reads shared state and returns a small update rather than replacing the whole state.
# Learning edition: comments explain the code; runtime behavior is unchanged.

# This docstring describes the purpose of the module, class, or function. It is readable through
# Python help() and is kept unchanged.
"""A LangGraph node factory. Existing team state can adopt these two fields."""
# Import operator so we can use list addition as the LangGraph result reducer.
import operator
# Import Annotated, TypedDict from typing; this makes those names available without copying their
# implementation.
from typing import Annotated, TypedDict

# Import GuiTask, execute_gui_task from worker_api; this makes those names available without
# copying their implementation.
from worker_api import GuiTask, execute_gui_task


# Describe the fields our LangGraph node reads or updates. This TypedDict is a type description,
# not a runtime validator.
class GuiGraphState(TypedDict, total=False):
    # Describe gui_request with a type hint. The type hint alone does not validate incoming data.
    gui_request: dict
    # Describe gui_results with a type hint. The type hint alone does not validate incoming data.
    gui_results: Annotated[list[dict], operator.add]
    # Describe lessons with a type hint. The type hint alone does not validate incoming data.
    lessons: list[str]


# Build a node function that remembers the planner factory and host-selected execution mode.
def make_gui_node(planner_factory, *, execute=False, reflector=None, done_checker=None):
    # This docstring describes the purpose of the module, class, or function. It is readable
    # through Python help() and is kept unchanged.
    """planner_factory(context) creates a planner; keep clients outside graph state.

    `execute` is host configuration, not something an LLM sets in graph state.
    Route results to the checker/orchestrator; never mark checks as passed here.
    reflector(result, goal) -> dict, e.g. lambda r, g: reflect_on_run(r, reviewer, goal=g), runs
    after each live attempt and updates persistent lesson memory. Give it a reviewer backend
    for a DIFFERENT model than the planner's (provider_client.check_independent_reflector).
    A result with status "awaiting_approval" carries pending_approval; after a person approves,
    send the same request again with approved_targets=[pending_approval["target"]].
    """
    # Read one GUI request from graph state and return only the new GUI result list.
    def gui_node(state):
        # Store the validated request describing one subtask and its limits.
        task = GuiTask(**state["gui_request"])
        # Enforce the task contract before observations or model calls start.
        task.validate()
        # Store results already recorded for this task ID and attempt.
        # A resumed request (after a person's approval) is a new run, not a replay.
        previous = [r for r in state.get("gui_results", [])
                    if r["task_id"] == task.task_id and r["attempt"] == task.attempt
                    and tuple(r.get("approved_targets", ())) == tuple(task.approved_targets)]
        # Skip a node whose task ID and attempt already have a recorded result, avoiding a replay
        # of recorded effects.
        if previous:
            # Do not replay completed node effects when state already has a result.
            # Return this value to the caller and finish this function call.
            return {"gui_results": []}
        # Store additional task context such as lessons and attempt identity.
        context = {"lessons": state.get("lessons", []),
                   # Include the task_id field in this structured record; the receiver reads it by
                   # this key.
                   "task_id": task.task_id, "attempt": task.attempt}
        # Store the structured response that will be returned to the caller.
        # done_checker: verify.make_done_checker(reviewer) for a different model than the planner.
        result = execute_gui_task(task, planner_factory(context), execute=execute,
                                  done_checker=done_checker)
        result["approved_targets"] = list(task.approved_targets)
        # Reflexion: a failed or stalled attempt becomes one stored lesson (skipped otherwise).
        if reflector is not None and execute:
            result["reflection"] = reflector(result, task.goal)
        # Return this value to the caller and finish this function call.
        return {"gui_results": [result]}
    # Return the function itself, so LangGraph can call it later with its own state.
    return gui_node
