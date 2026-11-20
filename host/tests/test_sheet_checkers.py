"""Deterministic spreadsheet checkers, the sheet-state contract, and real LangGraph runs of the
Sheet worker with a scripted model (no LLM, no network)."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from langchain_core.messages import AIMessage

import agent.graph as graph
from agent.checkers import check_spec
from agent.local_runtime import LocalVM
from agent.planning import apply_explicit_location_anchor
from agent.runtime import RunLog
from agent.sheet_contracts import snapshot_sheet_contract, workbook_overview
from tests.test_graph_approval_integration import ScriptedModel, call


def run(coro):
    return asyncio.run(coro)


class CheckerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vm = LocalVM(Path(self.tmp.name) / "ws")
        self.addCleanup(self.tmp.cleanup)
        self.vm.xlsx_write("sales.xlsx", [["Region", "Revenue"], ["N", 10.5], ["S", 20], ["N", 4.5]], sheet="Sales")
        self.vm.xlsx_write("sales.xlsx", [["Metric", "Value"], ["Total", 35], ["Text", "35"], ["F", "=SUM(Sales!B2:B4)"]], sheet="Out")

    def chk(self, *assertions):
        return run(check_spec(self.vm, {"checks": list(assertions)}, capability="sheet"))

    def a(self, kind, **kw):
        return {"kind": kind, "path": "sales.xlsx", **kw}

    def test_cell_equals_pass_fail_and_text_vs_number(self):
        self.assertEqual(self.chk(self.a("xlsx_cell_equals", sheet="Out", cell="B2", value=35))["status"], "pass")
        bad = self.chk(self.a("xlsx_cell_equals", sheet="Out", cell="B2", value=36))
        self.assertEqual(bad["status"], "fail")
        text = self.chk(self.a("xlsx_cell_equals", sheet="Out", cell="B3", value=35))
        self.assertEqual(text["status"], "fail")
        self.assertIn("stored as text", text["evidence"])
        self.assertEqual(self.chk(self.a("xlsx_cell_equals", sheet="Out", cell="B3", value="35"))["status"], "pass")

    def test_cell_equals_uses_computed_formula_value(self):
        self.assertEqual(self.chk(self.a("xlsx_cell_equals", sheet="Out", cell="B4", value=35))["status"], "pass")

    def test_missing_sheet_is_fail_not_unknown_and_bad_cell_is_unknown(self):
        r = self.chk(self.a("xlsx_cell_equals", sheet="Nope", cell="A1", value=1))
        self.assertEqual(r["status"], "fail")
        self.assertIn("missing", r["evidence"])
        self.assertEqual(self.chk(self.a("xlsx_cell_equals", sheet="Out", cell="ZZZ", value=1))["status"], "unknown")
        self.assertEqual(self.chk(self.a("xlsx_cell_equals", sheet="Out", cell="A1"))["status"], "unknown")

    def test_formula_present(self):
        ok = self.chk(self.a("xlsx_formula_present", sheet="Out", cell="B4", contains="sum(sales!$b$2:b4)"))
        self.assertEqual(ok["status"], "pass")
        self.assertEqual(self.chk(self.a("xlsx_formula_present", sheet="Out", cell="B2"))["status"], "fail")
        self.assertEqual(self.chk(self.a("xlsx_formula_present", sheet="Out", cell="B4", contains="AVERAGE"))["status"], "fail")

    def test_data_row_count(self):
        self.assertEqual(self.chk(self.a("xlsx_data_row_count", sheet="Sales", count=3))["status"], "pass")
        r = self.chk(self.a("xlsx_data_row_count", sheet="Sales", count=2))
        self.assertEqual(r["status"], "fail")
        self.assertIn("3 data row", r["evidence"])

    def test_aggregate_is_recomputed_from_source_not_from_the_planner(self):
        spec = self.a("xlsx_aggregate_matches", sheet="Out", cell="B2", source_path="sales.xlsx",
                      source_sheet="Sales", column="Revenue", op="sum")
        self.assertEqual(self.chk(spec)["status"], "pass")
        self.vm.xlsx_set_cells("sales.xlsx", "Out", {"B2": 34})
        wrong = self.chk(spec)
        self.assertEqual(wrong["status"], "fail")
        self.assertIn("recomputed", wrong["evidence"])
        self.assertIn("35", wrong["evidence"])
        for op, cell_value in (("avg", 35 / 3), ("max", 20), ("min", 4.5), ("count", 3)):
            self.vm.xlsx_set_cells("sales.xlsx", "Out", {"B2": cell_value})
            self.assertEqual(self.chk({**spec, "op": op})["status"], "pass", op)
        self.assertEqual(self.chk({**spec, "column": "Profit"})["status"], "unknown")

    def test_snapshot_protects_the_sheet_the_check_does_not_name(self):
        snap = run(snapshot_sheet_contract(self.vm, {"id": 0, "capability": "sheet", "check_spec": {"checks": [
            self.a("xlsx_cell_equals", sheet="Out", cell="B2", value=35)]}}))
        self.assertEqual(snap[0]["protected_sheets"], ["Sales"])

    def test_assertion_unavailable_to_files_worker(self):
        r = run(check_spec(self.vm, {"checks": [self.a("xlsx_cell_equals", sheet="Out", cell="B2", value=35)]}, capability="files"))
        self.assertEqual(r["status"], "unknown")

    def test_location_anchor_also_fixes_source_path(self):
        sg = [{"id": 0, "check_spec": {"checks": [{"kind": "xlsx_aggregate_matches", "path": "t.xlsx", "source_path": "s.xlsx"}]}}]
        apply_explicit_location_anchor("In Desktop, total the data", sg)
        c = sg[0]["check_spec"]["checks"][0]
        self.assertEqual((c["path"], c["source_path"]), ("Desktop/t.xlsx", "Desktop/s.xlsx"))


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vm = LocalVM(Path(self.tmp.name) / "ws")
        self.addCleanup(self.tmp.cleanup)
        self.vm.xlsx_write("b.xlsx", [["a"], [1]], sheet="Data")
        self.vm.xlsx_write("b.xlsx", [["n"], ["x"]], sheet="Notes")

    def sg(self, *checks):
        return {"id": 3, "capability": "sheet", "instruction": "edit b.xlsx", "check_spec": {"checks": list(checks)}}

    def test_untouched_sheets_are_protected_and_snapshot_is_idempotent(self):
        sg = self.sg({"kind": "xlsx_sheet_exists", "path": "b.xlsx", "sheet": "Data"})
        events = run(snapshot_sheet_contract(self.vm, sg))
        self.assertEqual(events[0]["protected_sheets"], ["Notes"])
        self.assertEqual(run(snapshot_sheet_contract(self.vm, sg)), [])
        self.assertEqual(sum(a["kind"] == "xlsx_sheets_preserved" for a in sg["check_spec"]["checks"]), 1)
        self.assertEqual(run(check_spec(self.vm, sg["check_spec"], capability="sheet"))["status"], "pass")
        self.vm.xlsx_set_cells("b.xlsx", "Notes", {"A2": "tampered"})
        res = run(check_spec(self.vm, sg["check_spec"], capability="sheet"))
        self.assertEqual(res["status"], "fail")
        self.assertIn("COLLATERAL DAMAGE", res["evidence"])
        self.assertIn("Notes", res["evidence"])

    def test_deleted_protected_sheet_is_detected(self):
        sg = self.sg({"kind": "xlsx_sheet_exists", "path": "b.xlsx", "sheet": "Data"})
        run(snapshot_sheet_contract(self.vm, sg))
        self.vm.approver = lambda r: True
        self.vm.harness_remove_path("b.xlsx")
        self.vm.xlsx_write("b.xlsx", [["a"], [1]], sheet="Data")
        res = run(check_spec(self.vm, sg["check_spec"], capability="sheet"))
        self.assertEqual(res["status"], "fail")
        self.assertIn("deleted", res["evidence"])

    def test_source_workbook_is_fully_protected_but_a_named_target_is_not(self):
        self.vm.xlsx_write("src.xlsx", [["v"], [1]], sheet="S")
        sg = self.sg({"kind": "xlsx_aggregate_matches", "path": "out.xlsx", "cell": "A1",
                      "source_path": "src.xlsx", "column": "v", "op": "sum"})
        events = run(snapshot_sheet_contract(self.vm, sg))
        self.assertEqual([(e["path"], e["protected_sheets"]) for e in events], [("src.xlsx", ["S"])])   # out.xlsx not there yet
        same = self.sg({"kind": "xlsx_aggregate_matches", "path": "b.xlsx", "sheet": "Notes", "cell": "A1",
                        "source_path": "b.xlsx", "source_sheet": "Data", "column": "a", "op": "sum"})
        ev = run(snapshot_sheet_contract(self.vm, same))
        self.assertEqual(ev[0]["protected_sheets"], ["Data"])          # same workbook: only Notes may change

    def test_workbook_not_yet_created_is_skipped_and_overview_says_so(self):
        sg = self.sg({"kind": "xlsx_sheet_exists", "path": "later.xlsx", "sheet": "X"})
        self.assertEqual(run(snapshot_sheet_contract(self.vm, sg)), [])
        self.assertIn("later.xlsx: does not exist", run(workbook_overview(self.vm, sg)))

    def test_overview_lists_headers(self):
        sg = self.sg({"kind": "xlsx_sheet_exists", "path": "b.xlsx", "sheet": "Data"})
        text = run(workbook_overview(self.vm, sg))
        self.assertIn('"name": "Data"', text)
        self.assertIn('"header": ["a"]', text)


def sheet_plan(*checks, instruction="Edit prices.xlsx"):
    return [{"id": 0, "capability": "sheet", "instruction": instruction,
             "expected_output": "prices updated", "check_spec": {"checks": list(checks), "rationale": "x"}}]


class SheetGraphTests(unittest.TestCase):
    """Real LangGraph run with a scripted worker model."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "ws"
        self.vm = LocalVM(self.root)
        asyncio.run(self.vm.connect())
        self.vm.xlsx_write("prices.xlsx", [["Item", "Price"], ["Tea", 10], ["Coffee", 20]], sheet="Prices")
        self.vm.xlsx_write("prices.xlsx", [["Note"], ["keep"]], sheet="Notes")
        self.runs = Path(self.temp.name) / "runs"
        mock.patch("agent.runtime.RUNS_DIR", self.runs).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.temp.cleanup)
        self.prompts, self.tools_seen = [], []

    def _run(self, scripts, plan, goal="Change the Coffee price to 25."):
        it = iter(scripts)

        def fake_model(role):
            if role == "critic":
                raise RuntimeError("critic unavailable in this test")
            return ScriptedModel(script=next(it))

        with mock.patch.object(graph, "model", fake_model):
            return asyncio.run(graph.run_goal(goal, self.vm, RunLog("sheet-itest"), "sheet-itest", plan_override=plan))

    def _events(self):
        return [json.loads(l) for l in (self.runs / "sheet-itest.jsonl").read_text().splitlines()]

    PRICE_CHECK = {"kind": "xlsx_cell_equals", "path": "prices.xlsx", "sheet": "Prices", "cell": "B3", "value": 25}

    def test_happy_path_passes_with_contract_and_overview(self):
        final = self._run([[call("xlsx_set_cells", path="prices.xlsx", sheet="Prices", cells={"B3": 25}),
                            AIMessage(content="DONE")]], sheet_plan(self.PRICE_CHECK))
        self.assertEqual(final["status"], "done", final["checks"])
        kinds = [a["kind"] for a in final["subgoals"][0]["check_spec"]["checks"]]
        self.assertIn("xlsx_sheets_preserved", kinds)
        snap = [e for e in self._events() if e["event"] == "sheet_contract_snapshot"]
        self.assertEqual(snap[0]["protected_sheets"], ["Notes"])
        self.assertEqual(self.vm.xlsx_read("prices.xlsx", sheet="Prices")["rows"][2], ["Coffee", 25])

    def test_worker_that_clobbers_another_sheet_fails_the_subgoal(self):
        bad = [call("xlsx_set_cells", path="prices.xlsx", sheet="Prices", cells={"B3": 25}),
               call("xlsx_set_cells", path="prices.xlsx", sheet="Notes", cells={"A2": "oops"}),
               AIMessage(content="DONE")]
        final = self._run([bad, [AIMessage(content="nothing to do")], [AIMessage(content="nothing to do")]],
                          sheet_plan(self.PRICE_CHECK))
        self.assertEqual(final["status"], "failed")
        self.assertIn("COLLATERAL DAMAGE", final["checks"]["0"]["evidence"])

    def test_false_success_claim_is_not_accepted(self):
        final = self._run([[AIMessage(content="DONE: updated the price")]] * 3, sheet_plan(self.PRICE_CHECK))
        self.assertNotEqual(final["status"], "done")          # worker said DONE; the verifier saw B3 == 20
        self.assertEqual(final["checks"]["0"]["status"], "fail")

    def test_quick_fix_repairs_a_wrong_value_once(self):
        wrong = [call("xlsx_set_cells", path="prices.xlsx", sheet="Prices", cells={"B3": 52}), AIMessage(content="DONE")]
        fix = [call("xlsx_set_cells", path="prices.xlsx", sheet="Prices", cells={"B3": 25}), AIMessage(content="fixed")]
        final = self._run([wrong, fix], sheet_plan(self.PRICE_CHECK))
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["quick_fix_attempts"], {"0": 1})
        self.assertIn("52", [e for e in self._events() if e["event"] == "quick_fix"][0]["evidence"])

    def test_denied_sheet_replacement_is_final_and_data_survives(self):
        self.vm.approver = lambda req: False
        plan = sheet_plan({"kind": "xlsx_values_present", "path": "prices.xlsx", "sheet": "Prices", "values": ["Matcha"]},
                          instruction="Replace sheet Prices in prices.xlsx with Matcha")
        attempt = [call("xlsx_write", path="prices.xlsx", rows=[["Item", "Price"], ["Matcha", 9]], sheet="Prices"),
                   AIMessage(content="Replaced.")]
        final = self._run([attempt, attempt, attempt], plan, goal="Replace sheet Prices")
        self.assertEqual(final["status"], "failed")
        self.assertEqual(len(final["approvals"]), 1)
        self.assertFalse(final["approvals"][0]["approved"])
        self.assertEqual(self.vm.xlsx_read("prices.xlsx", sheet="Prices")["rows"][1], ["Tea", 10])
        self.assertIn("human denied approval", (self.runs / "sheet-itest.jsonl").read_text())

    def test_sheet_worker_gets_its_own_prompt_tools_and_overview(self):
        captured = {}
        real = graph.create_react_agent

        def spy(model_, tools, prompt=None, **kw):
            captured["tools"] = [t.name for t in tools]
            captured["prompt"] = prompt
            return real(model_, tools, prompt=prompt, **kw)

        with mock.patch.object(graph, "create_react_agent", spy):
            self._run([[AIMessage(content="DONE")]] * 3, sheet_plan(self.PRICE_CHECK))
        self.assertIn("xlsx_aggregate", captured["tools"])
        self.assertNotIn("delete_path", captured["tools"])
        self.assertNotIn("move_files", captured["tools"])
        self.assertIn("spreadsheet specialist", captured["prompt"])
        self.assertNotIn("move_files", captured["prompt"])


if __name__ == "__main__":
    unittest.main()
