"""Shared state: the contract every node and every teammate codes against."""
import operator
from typing import Annotated, Literal, TypedDict

Capability = Literal["files", "sheet", "gui"]  # Later: "web", "doc", "research"


class Subgoal(TypedDict):
    id: int
    capability: Capability
    instruction: str          # what the worker must do
    expected_output: str      # what "done" looks like, in words
    check_spec: dict          # machine-checkable postconditions for this subgoal


class Step(TypedDict):
    subgoal_id: int
    worker: str
    tool: str
    args: dict
    observation: str


class CheckResult(TypedDict):
    status: Literal["pass", "fail", "unknown"]
    detail: str
    evidence: str

class Lesson(TypedDict):
    attempt: int              # trial that produced it
    subgoal_id: int
    capability: Capability
    lesson: str

class Trial(TypedDict):
    attempt: int              # 0-based
    status: str
    steps: int
    quick_fixes: int
    tokens: int               # includes the reflection that closed this trial
    signature: str
    stop: str                 # why no further trial ran; "" if one followed

class ResetList(list):
    """Returned by a node to replace an append-only field instead of extending it (new trial)."""


def append_or_reset(old: list, new: list) -> list:
    return list(new) if isinstance(new, ResetList) else old + new


class AgentState(TypedDict, total=False):
    run_id: str
    goal: str                                        # user request, never edited
    subgoals: list[Subgoal]
    planned_subgoals: list[Subgoal]                  # frozen copy of the plan, restored each trial
    current: int                                     # index of the next subgoal
    history: Annotated[list[Step], append_or_reset]
    findings: Annotated[list[str], append_or_reset]  # worker summaries
    checks: dict[str, CheckResult]
    lessons: list[Lesson]                            # Reflexion memory, one run only
    attempt: int                                     # trial index, 0-based
    trials: Annotated[list[Trial], operator.add]
    quick_fix_instructions: dict[str, list[str]]     # per subgoal, this trial
    quick_fix_attempts: dict[str, int]          # count per subgoal, maximum 2
    quick_fix_evidence: dict[str, str]           # evidence that triggered the latest repair
    quick_fix_critic: dict[str, bool]             # whether the LLM critic supplied the repair
    repair_instruction: str                     # scoped instruction for the next worker run
    quick_fix_pending: bool
    step_counts: dict[str, int]                 # cumulative tool calls per subgoal
    tokens_used: int
    gui_results: dict[str, dict]                # GUI worker verdict per subgoal (status, reviewer_confirmed)
    approvals: list[dict]                       # human-approval decisions (added after the run)
    status: Literal["running", "done", "failed", "stopped", "unknown"]
