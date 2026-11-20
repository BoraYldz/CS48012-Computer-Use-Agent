"""Orchestrator v1: plan -> worker -> check -> next subgoal / END.

Milestone 1: subgoals run in order, one worker at a time.
Milestone 2 adds the reflector between `check` (fail) and `worker` (retry).
"""
import asyncio
import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

from . import gui
from .runtime import MAX_STEPS, MAX_TOKENS, WORKER_TOOLS, VM, RunLog, model
from .state import AgentState

# One line per worker; the planner only sees the workers that are available in this run
# (files/sheet need the tool server, gui needs nothing but this PC's screen).
CAPABILITY_HELP = {
    "files": "list, create, move, rename, copy files and folders; read text and PDF files",
    "sheet": "read and write .xlsx spreadsheets (it can also read text/PDF files for input data)",
    "gui": gui.HELP,
}
ALL_CAPABILITIES = [*WORKER_TOOLS, "gui"]

PLANNER_PROMPT = """You are the orchestrator of a desktop agent working on a Windows PC.
Break the user's request into a short ordered list of subgoals (usually 1-4).
Each subgoal goes to ONE specialist worker:
{workers}
Paths are relative to the user's home folder (e.g. "Downloads/report.xlsx").
Keep subgoals concrete and checkable. Do not invent files that were not mentioned.
For the gui worker, write the instruction as one self-contained task in plain words and put
every exact value (addresses, names, text to type) in it."""

WORKER_PROMPT = """You are the {cap} worker of a desktop agent. Complete ONLY this subgoal using your tools.
Paths are relative to the user's home folder. Look before you act (list folders, read files).
Never overwrite or delete anything you were not asked to change.
When finished, reply with one short line starting with DONE: or FAILED: and what you did."""


class PlannedSubgoal(BaseModel):
    capability: str = Field(description=f"one of {ALL_CAPABILITIES}")
    instruction: str
    expected_output: str


class Plan(BaseModel):
    subgoals: list[PlannedSubgoal]


def _text(content) -> str:
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)


def _tokens(msg) -> int:
    u = getattr(msg, "usage_metadata", None) or {}
    return int(u.get("total_tokens", 0))


def build_graph(vm: VM | None, log: RunLog):
    # Without the tool server only the gui worker exists.
    available = [*(WORKER_TOOLS if vm is not None else []), "gui"]
    planner_prompt = PLANNER_PROMPT.format(
        workers="\n".join(f"- {cap}: {CAPABILITY_HELP[cap]}" for cap in available))

    async def plan(state: AgentState):
        llm = model("planner").with_structured_output(Plan, include_raw=True)
        out = await llm.ainvoke([("system", planner_prompt), ("user", state["goal"])])
        parsed: Plan | None = out["parsed"]
        # Zero-token gate: validate the plan in code before any worker runs.
        if parsed is None or not parsed.subgoals:
            raise ValueError(f"planner returned no valid plan: {out.get('parsing_error')}")
        bad = [s.capability for s in parsed.subgoals if s.capability not in available]
        if bad:
            raise ValueError(f"planner used unknown or unavailable capabilities: {bad}")
        subgoals = [{"id": i, **s.model_dump()} for i, s in enumerate(parsed.subgoals)]
        log.write("plan", subgoals=subgoals)
        return {"subgoals": subgoals, "current": 0,
                "tokens_used": state.get("tokens_used", 0) + _tokens(out["raw"])}

    async def gui_worker(state: AgentState, sg):
        # The GUI worker blocks while it drives the screen, so it runs in a thread.
        log.write("worker_start", subgoal=sg["id"], worker="gui")
        allowance = MAX_TOKENS - state.get("tokens_used", 0)
        try:
            result = await asyncio.to_thread(
                gui.run_gui_subgoal, sg["instruction"], sg["expected_output"], state["goal"],
                state.get("lessons", []), f"{state.get('run_id', 'run')}-sg{sg['id']}", allowance)
        except Exception as error:  # missing key or packages: a failed subgoal, not a crash
            result = {"status": "error", "reason": f"{type(error).__name__}: {error}"[:200]}
        steps, summary, tokens = gui.gui_findings(result, sg["id"])
        for step in steps:
            log.write("step", **step)
        log.write("worker_end", subgoal=sg["id"], summary=summary, tokens=tokens,
                  gui_status=result.get("status"), gui_run_id=result.get("run_id"))
        return {"history": steps, "findings": [f"[{sg['id']}] {summary}"],
                "tokens_used": state.get("tokens_used", 0) + tokens}

    async def worker(state: AgentState):
        sg = state["subgoals"][state["current"]]
        cap = sg["capability"]
        if cap == "gui":
            return await gui_worker(state, sg)
        agent = create_react_agent(model("worker"), vm.for_worker(cap),
                                   prompt=WORKER_PROMPT.format(cap=cap))
        msg = (f"Overall goal (context only): {state['goal']}\n"
               f"Your subgoal: {sg['instruction']}\nDone means: {sg['expected_output']}")
        log.write("worker_start", subgoal=sg["id"], worker=cap)
        try:
            res = await agent.ainvoke({"messages": [HumanMessage(msg)]},
                                      {"recursion_limit": 2 * MAX_STEPS + 1})
            msgs, error = res["messages"], None
        except GraphRecursionError:
            msgs, error = [], f"step limit ({MAX_STEPS}) reached"

        # Turn the ReAct transcript into logged steps.
        steps, tokens, results = [], 0, {m.tool_call_id: m.content for m in msgs if isinstance(m, ToolMessage)}
        for m in msgs:
            if isinstance(m, AIMessage):
                tokens += _tokens(m)
                for tc in m.tool_calls:
                    step = {"subgoal_id": sg["id"], "worker": cap, "tool": tc["name"],
                            "args": tc["args"], "observation": str(results.get(tc["id"], ""))[:500]}
                    steps.append(step)
                    log.write("step", **step)
        summary = error or (_text(msgs[-1].content) if msgs else "")
        log.write("worker_end", subgoal=sg["id"], summary=summary, tokens=tokens)
        return {"history": steps, "findings": [f"[{sg['id']}] {summary}"],
                "tokens_used": state.get("tokens_used", 0) + tokens}

    async def check(state: AgentState):
        # Milestone 1: subgoal check = worker did not hit a limit and did not report failure.
        # The deterministic TASK check runs in the harness after the graph ends.
        sg = state["subgoals"][state["current"]]
        last = state["findings"][-1]
        ok = "FAILED" not in last.upper() and "step limit" not in last
        result = {"status": "pass" if ok else "fail", "detail": last}
        log.write("check_subgoal", subgoal=sg["id"], **result)
        checks = {**state.get("checks", {}), str(sg["id"]): result}
        nxt = state["current"] + 1
        if not ok:
            return {"checks": checks, "status": "failed"}
        if state["tokens_used"] > MAX_TOKENS:
            return {"checks": checks, "status": "stopped"}
        if nxt >= len(state["subgoals"]):
            return {"checks": checks, "current": nxt, "status": "done"}
        return {"checks": checks, "current": nxt}

    def route(state: AgentState):
        return END if state.get("status") in ("done", "failed", "stopped") else "worker"

    g = StateGraph(AgentState)
    g.add_node("plan", plan)
    g.add_node("worker", worker)
    g.add_node("check", check)
    g.add_edge(START, "plan")
    g.add_edge("plan", "worker")
    g.add_edge("worker", "check")
    g.add_conditional_edges("check", route, ["worker", END])
    return g.compile()


async def run_goal(goal: str, vm: VM | None, log: RunLog, run_id: str) -> AgentState:
    graph = build_graph(vm, log)
    log.write("run_start", goal=goal)
    final = await graph.ainvoke({"run_id": run_id, "goal": goal, "history": [], "findings": [],
                                 "checks": {}, "lessons": [], "attempt": 1, "tokens_used": 0,
                                 "status": "running"})
    log.write("run_end", status=final.get("status"), tokens=final.get("tokens_used"))
    return final


def summarize(final: AgentState) -> str:
    return json.dumps({"status": final.get("status"), "tokens": final.get("tokens_used"),
                       "findings": final.get("findings")}, indent=2, ensure_ascii=False)
