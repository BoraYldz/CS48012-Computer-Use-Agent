"""Eval metrics, rollback approval in eval, and the fault-injected Reflexion task. No LLM."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from langchain_core.messages import AIMessage

import agent.evalrun as evalrun
import agent.graph as graph
from agent.local_runtime import LocalVM
from agent.runtime import RunLog
from tasks import files_suite as fs, load
from tests.test_graph_approval_integration import ScriptedModel, call
from tests.test_reflexion import FakeCritic


def trial(status, steps):
    return {"attempt": 0, "status": status, "steps": steps, "quick_fixes": 1, "tokens": 5,
            "signature": "s", "stop": ""}


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vm = LocalVM(Path(self.temp.name) / "ws")
        asyncio.run(self.vm.connect())
        self.addCleanup(mock.patch.stopall)
        mock.patch("agent.runtime.RUNS_DIR", Path(self.temp.name) / "runs").start()
        mock.patch.dict("os.environ", {"MAX_ATTEMPTS": "3", "REFLEXION_LESSONS": "on"}).start()


class EvalTests(Base):
    def test_rollback_is_approved_and_hidden_from_task_checks(self):
        task = fs.select(only="t11")[0]          # fails on any agent approval request

        async def fake(goal, vm, log, run_id):
            self.assertTrue(vm.approver({"tool": "rollback"}))
            self.assertFalse(vm.approver({"tool": "delete_path"}))
            await task.solve(vm, "_eval/t11_prompt_injection")
            return {"status": "done", "tokens_used": 10, "step_counts": {"0": 1},
                    "approvals": [{"tool": "rollback", "approved": True}],
                    "trials": [trial("failed", 4), trial("done", 1)]}

        with mock.patch.object(evalrun, "run_goal", fake):
            row = asyncio.run(evalrun.run_one(self.vm, task, 1))
        self.assertTrue(row["passed"], row["detail"])
        self.assertEqual((row["approvals"], row["trials"], row["steps"], row["quick_fixes"]), (0, 2, 5, 2))
        self.assertTrue(row["first_failed"] and row["recovered"])

    def test_recovery_metrics_and_report(self):
        base = {"task": "a", "passed": True, "false_success": False, "false_fail": False, "steps": 1,
                "tokens": 1, "seconds": 1, "quick_fixes": 0, "crash": None}
        rows = [{**base, "trials": 2, "first_failed": True, "recovered": True},
                {**base, "passed": False, "trials": 3, "first_failed": True, "recovered": False},
                {**base, "reflexion_off": "snapshot refused"}]
        summary = evalrun.aggregate(rows, 3)
        self.assertEqual((summary["recovery_rate"], summary["mean_trials"]), (0.5, 2.0))
        self.assertEqual(summary["reflexion_off_runs"], 1)
        self.assertIn("recovered 1/2 (50%)", evalrun.markdown(summary, {}, 3))
        self.assertIsNone(evalrun.aggregate([base], 1)["recovery_rate"])


class ReflexionTaskTests(Base):
    def test_fault_forces_a_second_trial_that_passes(self):
        mock.patch.object(graph, "AGENT_MODE", "local").start()
        mock.patch.object(graph, "AGENT_SCOPE", "sandbox").start()
        task, critic = load("reflexion"), FakeCritic()
        script = [call("write_text", path=task.OUTPUT_PATH, content=task.EXPECTED_TEXT),
                  AIMessage(content="done")]

        def fake_model(role, **kwargs):
            return critic if role == "critic" else ScriptedModel(script=script)

        async def go():
            await task.setup(self.vm)
            final = await graph.run_goal(task.REQUEST, self.vm, RunLog("rt"), "rt",
                                         plan_override=copy.deepcopy(task.PLAN))
            return final, await task.check(self.vm)

        with mock.patch.object(graph, "model", fake_model):
            final, (passed, detail) = asyncio.run(go())
        self.assertTrue(passed, detail)
        self.assertEqual(task.validate_run(final)[0], True, task.validate_run(final)[1])
        self.assertEqual([t["status"] for t in final["trials"]], ["failed", "done"])
        self.assertEqual(self.vm._write_fault["remaining"], 0)


if __name__ == "__main__":
    unittest.main()