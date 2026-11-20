"""Shared state: the contract every node and every teammate codes against."""
import operator
from typing import Annotated, Literal, TypedDict

Capability = Literal["files", "sheet"]  # Milestone 1. Later: "web", "doc", "research", "gui"


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


class AgentState(TypedDict, total=False):
    run_id: str
    goal: str                                        # user request, never edited
    subgoals: list[Subgoal]
    current: int                                     # index of the next subgoal
    history: Annotated[list[Step], operator.add]     # append-only
    findings: Annotated[list[str], operator.add]     # worker summaries, append-only
    checks: dict[str, CheckResult]
    lessons: list[str]                               # Reflexion memory (Milestone 2)
    attempt: int
    quick_fix_attempts: dict[str, int]          # count per subgoal, maximum 2
    quick_fix_evidence: dict[str, str]           # evidence that triggered the latest repair
    quick_fix_critic: dict[str, bool]             # whether the LLM critic supplied the repair
    repair_instruction: str                     # scoped instruction for the next worker run
    quick_fix_pending: bool
    step_counts: dict[str, int]                 # cumulative tool calls per subgoal
    tokens_used: int
    approvals: list[dict]                       # human-approval decisions (added after the run)
    status: Literal["running", "done", "failed", "stopped", "unknown"]
