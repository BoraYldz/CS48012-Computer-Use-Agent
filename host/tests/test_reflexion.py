"""Reflexion trials in the real graph with scripted models: recovery, skip reasons, early stop. No LLM."""
import asyncio
import copy
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from langchain_core.messages import AIMessage

import agent.graph as graph
from agent import memory, rollback, sheet_tools
from agent.local_runtime import LocalVM
from agent.runtime import RunLog
from tests.test_graph_approval_integration import PLAN as DELETE_PLAN, ScriptedModel, call


def plan_for(capability, check):
    return [{"id": 0, "capability": capability, "instruction": "Produce the requested value.",
             "expected_output": "value written", "check_spec": {"checks": [check], "rationale": "x"}}]


PLAN = plan_for("files", {"kind": "text_equals", "path": "Out/a.txt", "text": "good"})
NOOP = [AIMessage(content="nothing done")]
LESSON = memory.Reflection(failed_step=1, cause="Wrong content.",
                           next_time="Write the exact text with write_text.")


def writes(text, *extra):
    return [call("write_text", path="Out/a.txt", content=text), *extra, AIMessage(content="done")]


BAD = writes("bad", call("write_text", path="junk.txt", content="junk"))
GOOD = writes("good")


class RecordingModel(ScriptedModel):
    sink: Any

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.pos == 0:
            self.sink.append(str(messages[-1].content))
        return super()._generate(messages, stop, run_manager, **kwargs)


class FakeCritic:
    def __init__(self):
        self.reflections = 0

    def with_structured_output(self, schema, include_raw):
        critic = self

        class Structured:
            async def ainvoke(self, messages):
                if schema is not memory.Reflection:
                    raise RuntimeError("no Quick Fix critic in this test")
                critic.reflections += 1
                return {"parsed": LESSON, "raw": None}

        return Structured()


class ReflexionGraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "ws"
        self.vm = LocalVM(self.root)
        asyncio.run(self.vm.connect())
        self.runs = Path(self.temp.name) / "runs"
        self.addCleanup(mock.patch.stopall)
        mock.patch("agent.runtime.RUNS_DIR", self.runs).start()
        mock.patch.object(graph, "AGENT_MODE", "local").start()
        mock.patch.object(graph, "AGENT_SCOPE", "sandbox").start()
        mock.patch.dict("os.environ", {"MAX_ATTEMPTS": "3", "REFLEXION_LESSONS": "on"}).start()
        self.critic, self.messages, self.models = FakeCritic(), [], []

    def run_graph(self, scripts, plan=PLAN, goal="Write good into Out/a.txt."):
        it = iter(scripts)

        def fake_model(role, **kwargs):
            self.models.append((role, kwargs.get("seed_offset", 0)))
            if role == "critic":
                return self.critic
            return RecordingModel(script=next(it), sink=self.messages)

        with mock.patch.object(graph, "model", fake_model):
            return asyncio.run(graph.run_goal(goal, self.vm, RunLog("rx"), "rx",
                                              plan_override=copy.deepcopy(plan)))

    def events(self, name=None):
        lines = (self.runs / "rx.jsonl").read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines]
        return [r for r in records if r["event"] == name] if name else records

    def skip_reason(self):
        return self.events("reflect_skipped")[-1]["reason"]

    # ---------- recovery ----------
    def test_fail_then_pass(self):
        final = self.run_graph([BAD, NOOP, GOOD])
        self.assertEqual(final["status"], "done", final["checks"])
        self.assertFalse((self.root / "junk.txt").exists())
        self.assertEqual([t["status"] for t in final["trials"]], ["failed", "done"])
        self.assertEqual([t["steps"] for t in final["trials"]], [2, 1])
        self.assertEqual((len(final["history"]), len(final["findings"])), (1, 1))
        self.assertEqual(len(final["lessons"]), 1)
        self.assertNotIn("Write the exact text", self.messages[0])
        self.assertIn("Write the exact text", self.messages[2])
        self.assertIn(("worker", 100), self.models)
        self.assertEqual(len(self.events("rollback")), 1)
        self.assertEqual(self.events("reflect_skipped"), [])
        self.assertIsNone(self.vm._snapshot)

    def test_lessons_off(self):
        with mock.patch.dict("os.environ", {"REFLEXION_LESSONS": "off"}):
            final = self.run_graph([BAD, NOOP, GOOD])
        self.assertEqual(final["status"], "done")
        self.assertEqual((final["lessons"], self.critic.reflections), ([], 0))

    def test_plan_is_restored_for_each_trial(self):
        sheet_tools.write(self.vm._p("p.xlsx"), [["Item", "Price"], ["Tea", 10]], "Prices")
        sheet_tools.write(self.vm._p("p.xlsx"), [["Note"], ["keep"]], "Notes")
        plan = plan_for("sheet", {"kind": "xlsx_cell_equals", "path": "p.xlsx", "sheet": "Prices",
                                  "cell": "B2", "value": 25})
        fix = [call("xlsx_set_cells", path="p.xlsx", sheet="Prices", cells={"B2": 25}),
               AIMessage(content="done")]
        final = self.run_graph([NOOP, NOOP, fix], plan=plan)
        self.assertEqual(final["status"], "done", final["checks"])
        self.assertEqual(len(final["planned_subgoals"][0]["check_spec"]["checks"]), 1)
        kinds = [a["kind"] for a in final["subgoals"][0]["check_spec"]["checks"]]
        self.assertEqual(kinds.count("xlsx_sheets_preserved"), 1)

    # ---------- stopping ----------
    def test_done_first_trial(self):
        final = self.run_graph([GOOD])
        self.assertEqual([t["status"] for t in final["trials"]], ["done"])
        self.assertEqual(self.events("rollback") + self.events("reflect_skipped"), [])

    def test_early_stop_on_identical_trial(self):
        final = self.run_graph(itertools.repeat(NOOP))
        self.assertEqual((final["status"], len(final["trials"])), ("failed", 2))
        self.assertTrue(self.skip_reason().startswith("same failure"))
        self.assertEqual(len(self.events("rollback")), 1)

    def test_trials_run_out(self):
        with mock.patch.dict("os.environ", {"MAX_ATTEMPTS": "2"}):
            final = self.run_graph([writes("one"), NOOP, writes("two"), NOOP])
        self.assertEqual(self.skip_reason(), "no trials left")
        self.assertEqual(len(final["trials"]), 2)
        self.assertEqual((self.root / "Out" / "a.txt").read_text(), "two")      # last state is left

    def test_denied_is_never_retried(self):
        self.vm.write_text("Work/a.tmp", "a")
        self.vm.approver = lambda req: False
        attempt = [call("delete_path", path="Work/a.tmp"), AIMessage(content="deleted")]
        final = self.run_graph([attempt], plan=DELETE_PLAN, goal="Delete the .tmp files in Work.")
        self.assertEqual(final["status"], "failed")
        self.assertEqual(self.skip_reason(), "human denied approval")
        self.assertEqual(self.events("rollback"), [])

    def test_policy_block_is_never_retried(self):
        escape = [call("write_text", path="../escape.txt", content="x"), AIMessage(content="done")]
        self.run_graph([escape])
        self.assertEqual(self.skip_reason(), "sandbox policy block")
        self.assertEqual(self.events("rollback"), [])

    def test_unknown_and_stopped_are_never_retried(self):
        unchecked = copy.deepcopy(PLAN)
        unchecked[0]["check_spec"]["checks"] = []
        self.run_graph([NOOP], plan=unchecked)
        self.assertEqual(self.skip_reason(), "status is unknown")
        with mock.patch.object(graph, "MAX_TOKENS", -1):
            self.run_graph([NOOP])
        self.assertEqual(self.skip_reason(), "status is stopped")
        self.assertEqual(self.events("rollback"), [])

    # ---------- rollback and snapshot ----------
    def test_workspace_rollback_denied_stops(self):
        with mock.patch.object(graph, "AGENT_SCOPE", "workspace"):
            final = self.run_graph([BAD, NOOP])
        self.assertEqual((final["status"], len(final["trials"])), ("failed", 1))
        self.assertEqual(self.skip_reason(), "rollback: denied")
        self.assertTrue((self.root / "junk.txt").exists())
        self.assertEqual([a["tool"] for a in final["approvals"]], ["rollback"])

    def test_workspace_rollback_approved(self):
        self.vm.approver = lambda req: True
        with mock.patch.object(graph, "AGENT_SCOPE", "workspace"):
            final = self.run_graph([BAD, NOOP, GOOD])
        self.assertEqual(final["status"], "done")
        self.assertEqual([a["tool"] for a in final["approvals"]], ["rollback"])

    def test_refused_snapshot_and_vm_mode_run_once(self):
        self.vm.write_text("existing.txt", "x")
        for patcher, text in ((mock.patch.object(rollback, "MAX_FILES", 0), "snapshot refused"),
                              (mock.patch.object(graph, "AGENT_MODE", "vm"), "needs the local")):
            with patcher:
                final = self.run_graph([NOOP, NOOP])
            self.assertEqual(final["trials"], [])
            self.assertIn(text, self.skip_reason())

    def test_snapshot_discarded_after_a_crash(self):
        with self.assertRaises(StopIteration if False else Exception):
            self.run_graph([])
        self.assertIsNone(self.vm._snapshot)

    def test_one_attempt_is_unchanged(self):
        snapshot = mock.MagicMock()
        with mock.patch.dict("os.environ", {"MAX_ATTEMPTS": "1"}), \
                mock.patch.dict(self.vm._functions, {"harness_snapshot": snapshot}):
            final = self.run_graph([BAD, NOOP])
        snapshot.assert_not_called()
        self.assertEqual(final["status"], "failed")
        self.assertTrue((self.root / "junk.txt").exists())
        new = {"trial_start", "reflect", "lesson", "rollback", "trial_end", "reflect_skipped"}
        self.assertFalse(new & {r["event"] for r in self.events()})
        self.assertNotIn("trials", json.loads(graph.summarize(final)))


class BlockReasonTests(unittest.TestCase):
    STATE = {"status": "failed", "subgoals": [{"id": 0, "capability": "files"}], "current": 0,
             "checks": {"0": {"status": "fail", "evidence": "e"}}, "step_counts": {"0": 1}}

    def reason(self, **changes):
        return graph.reflect_block_reason({**self.STATE, **changes}, "sig", 3)

    def test_reasons(self):
        self.assertEqual(self.reason(), "")
        self.assertIn("gui", self.reason(subgoals=[{"id": 0, "capability": "gui"}]))
        self.assertEqual(self.reason(checks={}), "no deterministic failure evidence")
        self.assertEqual(self.reason(step_counts={}), "no worker ran")
        self.assertEqual(self.reason(tokens_used=graph.MAX_TOKENS), "token cap reached")


if __name__ == "__main__":
    unittest.main()