"""Shared state: the contract every node and every teammate codes against."""
import operator
from typing import Annotated, Literal, TypedDict

Capability = Literal["files", "sheet", "gui"]  # Later: "web", "doc", "research"


class Subgoal(TypedDict):
    id: int
    capability: Capability
    instruction: str          # what the worker must do
    expected_output: str      # what "done" looks like, in words


class Step(TypedDict):
    subgoal_id: int
    worker: str
    tool: str
    args: dict
    observation: str


class CheckResult(TypedDict):
    status: Literal["pass", "fail", "unknown"]
    detail: str


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
    tokens_used: int
    status: Literal["running", "done", "failed", "stopped"]
