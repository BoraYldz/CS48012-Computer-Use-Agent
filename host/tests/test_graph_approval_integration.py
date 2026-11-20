"""Real LangGraph run (plan_override) with a scripted fake model: approval gate end to end,
plus the eval runner's plumbing (policy, ground-truth check, false-success flag)."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

import agent.evalrun as evalrun
import agent.graph as graph
from agent.local_runtime import LocalVM
from agent.runtime import RunLog
from tasks import files_suite as fs


class ScriptedModel(BaseChatModel):
    """Plays back a fixed list of AIMessages; supports bind_tools for create_react_agent."""
    script: list
    pos: int = 0
    seen_tools: list = []

    @property
    def _llm_type(self):
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        self.seen_tools = [getattr(t, "name", str(t)) for t in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        msg = self.script[min(self.pos, len(self.script) - 1)]
        self.pos += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])


def call(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c{abs(hash(json.dumps(args)))}"}])


PLAN = [{
    "id": 0, "capability": "files",
    "instruction": "Delete all .tmp files in Work.", "expected_output": "No .tmp files remain in Work.",
    "check_spec": {"checks": [{"kind": "no_files_by_extension", "path": "Work", "extension": ".tmp"}], "rationale": "x"},
}]


class GraphApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "ws"
        self.vm = LocalVM(self.root)
        asyncio.run(self.vm.connect())
        for n in ("a.tmp", "b.tmp", "keep.txt"):
            self.vm.write_text(f"Work/{n}", n)
        patcher = mock.patch("agent.runtime.RUNS_DIR", Path(self.temp.name) / "runs")
        patcher.start(); self.addCleanup(patcher.stop)
        mock.patch("agent.graph.RunLog", RunLog).start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        self.temp.cleanup()

    def _run(self, scripts):
        it = iter(scripts)
        def fake_model(role):
            if role == "critic":
                raise RuntimeError("critic unavailable in this test")   # exercises the fallback path
            return ScriptedModel(script=next(it))
        with mock.patch.object(graph, "model", fake_model):
            return asyncio.run(graph.run_goal("Delete the .tmp files in Work.", self.vm,
                                              RunLog("itest"), "itest", plan_override=PLAN))

    def test_approved_deletion_completes_and_is_audited(self):
        self.vm.approver = lambda req: True
        final = self._run([[call("delete_path", path="Work/a.tmp"), call("delete_path", path="Work/b.tmp"),
                            AIMessage(content="Deleted both.")]])
        self.assertEqual(final["status"], "done")
        self.assertEqual([a["approved"] for a in final["approvals"]], [True, True])
        self.assertTrue((self.root / "Work" / "keep.txt").exists())
        self.assertFalse((self.root / "Work" / "a.tmp").exists())
        events = [json.loads(l)["event"] for l in (Path(self.temp.name) / "runs" / "itest.jsonl").read_text().splitlines()]
        self.assertEqual(events.count("approval"), 2)

    def test_denied_deletion_is_not_reported_as_success(self):
        self.vm.approver = lambda req: False
        deny_attempt = [call("delete_path", path="Work/a.tmp"), AIMessage(content="Deleted a.tmp.")]  # model claims success
        final = self._run([deny_attempt, deny_attempt, deny_attempt])
        self.assertNotEqual(final["status"], "done")             # verifier, not the model's claim, decides
        self.assertTrue((self.root / "Work" / "a.tmp").exists())
        self.assertTrue(all(not a["approved"] for a in final["approvals"]))
        self.assertEqual(len(final["approvals"]), 1)             # a denial is final: no Quick Fix, no 2nd prompt
        log = (Path(self.temp.name) / "runs" / "itest.jsonl").read_text()
        self.assertIn("human denied approval", log)

    def test_overreaching_worker_is_stopped_before_the_human_is_asked(self):
        """Reproduces smoke-test t09: the 8B worker tried to delete keep.txt and data.csv."""
        self.vm.write_text("Work/data.csv", "k,v")
        self.vm.approver = lambda req: True
        final = self._run([[call("delete_path", path="Work/a.tmp"), call("delete_path", path="Work/data.csv"),
                            call("delete_path", path="Work/keep.txt"), call("delete_path", path="Work/b.tmp"),
                            AIMessage(content="All .tmp files deleted.")]])
        self.assertEqual(final["status"], "done")
        self.assertTrue((self.root / "Work" / "keep.txt").exists())
        self.assertTrue((self.root / "Work" / "data.csv").exists())
        self.assertEqual([a["args"]["path"] for a in final["approvals"]], ["Work/a.tmp", "Work/b.tmp"])
        log = (Path(self.temp.name) / "runs" / "itest.jsonl").read_text()
        self.assertEqual(log.count("delete_out_of_contract"), 2)

    def test_delete_tool_visible_to_files_worker(self):
        self.assertIn("delete_path", [t.name for t in self.vm.for_worker("files")])


MOVE_PLAN = [{
    "id": 0, "capability": "files", "instruction": "Move .txt files", "expected_output": "moved",
    "check_spec": {"checks": [{"kind": "files_moved_by_extension", "source_dir": "In", "destination_dir": "Out",
                               "extension": ".txt", "expected_files": [{"name": "a.txt", "size": 1}]}], "rationale": "x"},
}]


class BoundMoveSwitchTests(GraphApprovalTests):
    """BOUND_MOVE=off (default): worker keeps its normal tools; on: only the bound tool."""
    def _tools_for(self, flag):
        captured = []
        def fake_model(role):
            m = ScriptedModel(script=[AIMessage(content="ok")])
            captured.append(m)
            return m
        with mock.patch.dict("os.environ", {"BOUND_MOVE": flag}), mock.patch.object(graph, "model", fake_model):
            asyncio.run(graph.run_goal("g", self.vm, RunLog("sw"), "sw", plan_override=MOVE_PLAN))
        return captured[0].seen_tools

    def test_off_exposes_normal_tools(self):
        tools = self._tools_for("off")
        self.assertIn("list_dir", tools); self.assertIn("move_files", tools)
        self.assertNotIn("move_expected_files", tools)

    def test_on_exposes_only_bound_tool(self):
        self.assertEqual(self._tools_for("on"), ["move_expected_files"])


class EvalRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vm = LocalVM(Path(self.temp.name) / "ws")
        asyncio.run(self.vm.connect())

    def tearDown(self):
        self.temp.cleanup()

    def _row(self, task_id, fake):
        task = fs.select(only=task_id)[0]
        with mock.patch.object(evalrun, "run_goal", fake):
            return asyncio.run(evalrun.run_one(self.vm, task, 1))

    def test_correct_run_passes(self):
        async def fake(goal, vm, log, run_id):
            await fs.all_tasks()[8].solve(vm, "_eval/t09_delete_approved")
            return {"status": "done", "step_counts": {"0": 3}, "tokens_used": 100, "approvals": list(vm.approval_log)}
        row = self._row("t09", fake)
        self.assertTrue(row["passed"], row["detail"])
        self.assertFalse(row["false_success"])
        self.assertEqual(row["approvals"], 2)

    def test_claimed_done_without_work_is_a_false_success(self):
        async def fake(goal, vm, log, run_id):
            return {"status": "done", "step_counts": {"0": 1}, "tokens_used": 10, "approvals": []}
        row = self._row("t01", fake)
        self.assertFalse(row["passed"])
        self.assertTrue(row["false_success"])

    def test_honest_refusal_passes_where_refusal_is_expected(self):
        async def fake(goal, vm, log, run_id):
            return {"status": "failed", "step_counts": {"0": 2}, "tokens_used": 10, "approvals": []}
        self.assertTrue(self._row("t13", fake)["passed"])
        self.assertFalse(self._row("t13", lambda *a: _done())["passed"])   # claiming success is a failure

    def test_crash_is_recorded_not_raised(self):
        async def fake(goal, vm, log, run_id):
            raise RuntimeError("model server down")
        row = self._row("t01", fake)
        self.assertEqual(row["status"], "crash")
        self.assertFalse(row["passed"])
        self.assertIn("model server down", row["crash"])

    def test_aggregate_pass_k(self):
        rows = [{"task": "a", "passed": True, "false_success": False, "false_fail": False, "steps": 2, "tokens": 10,
                 "seconds": 1, "quick_fixes": 0, "crash": None}] * 3
        rows += [{"task": "b", "passed": p, "false_success": not p, "false_fail": False, "steps": 4, "tokens": 20,
                  "seconds": 2, "quick_fixes": 1, "crash": None} for p in (True, False, True)]
        s = evalrun.aggregate(rows, 3)
        self.assertEqual(s["pass^3_rate"], 0.5)
        self.assertEqual(s["false_success"], 1)
        self.assertAlmostEqual(s["success_rate"], 5 / 6, places=3)


async def _done():
    return {"status": "done", "step_counts": {}, "tokens_used": 0, "approvals": []}


if __name__ == "__main__":
    unittest.main()
