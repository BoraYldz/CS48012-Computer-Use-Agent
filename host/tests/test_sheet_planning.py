"""Planner-output normalizers for sheet plans + numeric coercion (regressions from the first Ollama smoke test)."""
import tempfile
import unittest
from pathlib import Path

from agent.local_runtime import LocalVM
from agent.plan_validation import anchor_sheet_paths_to_goal, normalize_sheet_checks


def sg(*checks, instruction="x", cap="sheet"):
    return {"id": 0, "capability": cap, "instruction": instruction, "expected_output": "y",
            "check_spec": {"checks": list(checks)}}


class PlanNormalizerTests(unittest.TestCase):
    def test_short_path_is_restored_from_goal(self):          # s04: worker looked for 'prices.xlsx' at the root
        goal = "In _eval/s04/prices.xlsx, sheet Prices, change Coffee to 25."
        plan = [sg({"kind": "xlsx_rows_present", "path": "prices.xlsx", "rows": [["Coffee", 25]]},
                   instruction="Edit the 'Prices' sheet in prices.xlsx to set Coffee.")]
        anchor_sheet_paths_to_goal(goal, plan)
        self.assertEqual(plan[0]["check_spec"]["checks"][0]["path"], "_eval/s04/prices.xlsx")
        self.assertIn("_eval/s04/prices.xlsx", plan[0]["instruction"])

    def test_path_anchor_leaves_other_cases_alone(self):
        goal = "Copy _eval/a/x.xlsx and _eval/b/x.xlsx"            # ambiguous basename -> untouched
        plan = [sg({"kind": "xlsx_sheet_exists", "path": "x.xlsx"})]
        self.assertEqual(anchor_sheet_paths_to_goal(goal, plan), [])
        files_plan = [sg({"kind": "path_exists", "path": "x.txt"}, cap="files")]
        self.assertEqual(anchor_sheet_paths_to_goal("do _eval/q/x.txt", files_plan), [])

    def test_unjustified_assertions_are_dropped(self):         # s01: formula + self-referential aggregate
        goal = "Create _eval/s01/inventory.xlsx with sheet Stock: header Item, Qty, then Pen, 120."
        plan = [sg({"kind": "xlsx_formula_present", "path": "p", "cell": "B2"},
                   {"kind": "xlsx_aggregate_matches", "path": "p", "sheet": "Stock", "cell": "B2",
                    "source_path": "p", "source_sheet": "Stock", "column": "Qty", "op": "sum"},
                   {"kind": "xlsx_cell_equals", "path": "p", "cell": "B2", "value": 120},
                   {"kind": "xlsx_rows_present", "path": "p", "rows": [["Pen", "120"]]})]
        normalize_sheet_checks(goal, plan)
        kinds = [a["kind"] for a in plan[0]["check_spec"]["checks"]]
        self.assertEqual(kinds, ["xlsx_rows_present", "xlsx_values_present"])   # guessed B2 -> value check
        self.assertEqual(plan[0]["check_spec"]["checks"][0]["rows"], [["Pen", 120]])   # "120" -> 120

    def test_guessed_cells_are_downgraded_to_value_checks_not_left_empty(self):   # s04 run 2: status=unknown
        goal = "In t.xlsx, sheet Prices, change the price of Coffee to 25 and of Water to 6."
        plan = [sg({"kind": "xlsx_cell_equals", "path": "t.xlsx", "sheet": "Prices", "cell": "B2", "value": 25},
                    {"kind": "xlsx_cell_equals", "path": "t.xlsx", "sheet": "Prices", "cell": "B3", "value": "6"})]
        normalize_sheet_checks(goal, plan)
        self.assertEqual(plan[0]["check_spec"]["checks"],
                         [{"kind": "xlsx_values_present", "path": "t.xlsx", "sheet": "Prices", "values": [25, 6]}])

    def test_formula_contains_is_dropped_when_request_only_gives_an_example(self):   # s08: demanded SUM(B2:C2)
        goal = "add a Total formula adding Math and Physics for the 4 students (for example =B2+C2)"
        plan = [sg({"kind": "xlsx_formula_present", "path": "g.xlsx", "cell": "D2", "contains": "SUM(B2:C2)"},
                   {"kind": "xlsx_formula_present", "path": "g.xlsx", "cell": "D3", "contains": "B2+C2"},
                   {"kind": "xlsx_data_row_count", "path": "g.xlsx", "count": "4"},
                   {"kind": "xlsx_data_row_count", "path": "g.xlsx", "count": None})]
        normalize_sheet_checks(goal, plan)
        c = plan[0]["check_spec"]["checks"]
        self.assertNotIn("contains", c[0])
        self.assertEqual(c[1]["contains"], "B2+C2")          # spelled in the request -> kept
        self.assertEqual([x["count"] for x in c if x["kind"] == "xlsx_data_row_count"], [4])

    def test_group_aggregate_survives_in_same_workbook(self):
        goal = "Add sheet Totals with the total revenue per region from sheet Sales"
        plan = [sg({"kind": "xlsx_group_aggregate_matches", "path": "s.xlsx", "sheet": "Totals", "source_path": "s.xlsx",
                    "source_sheet": "Sales", "column": "Revenue", "group_by": "Region", "op": "sum"})]
        normalize_sheet_checks(goal, plan)
        self.assertEqual(len(plan[0]["check_spec"]["checks"]), 1)

    def test_justified_assertions_survive(self):
        goal = "In t.xlsx set cell B3 to 25, add a Total formula column and put the sum of Revenue from s.xlsx into A1."
        plan = [sg({"kind": "xlsx_cell_equals", "path": "t.xlsx", "cell": "B3", "value": "25"},
                   {"kind": "xlsx_formula_present", "path": "t.xlsx", "cell": "D2"},
                   {"kind": "xlsx_aggregate_matches", "path": "t.xlsx", "cell": "A1", "source_path": "s.xlsx",
                    "column": "Revenue", "op": "sum"})]
        normalize_sheet_checks(goal, plan)
        checks = plan[0]["check_spec"]["checks"]
        self.assertEqual([a["kind"] for a in checks], ["xlsx_cell_equals", "xlsx_formula_present", "xlsx_aggregate_matches"])
        self.assertEqual(checks[0]["value"], 25)


class CoercionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vm = LocalVM(Path(self.tmp.name) / "ws")
        self.addCleanup(self.tmp.cleanup)

    def test_numeric_text_becomes_number_but_ids_and_dates_stay_text(self):   # s09: worker wrote ["A","1"]
        msg = self.vm.xlsx_write("w.xlsx", [["Name", "Score", "Id", "Date"], ["A", "1", "007", "2026-11-18"], ["B", "2.5", "-3", "1e3"]], sheet="S")
        self.assertIn("converted 3", msg)
        rows = self.vm.xlsx_read("w.xlsx")["rows"]
        self.assertEqual(rows[1], ["A", 1, "007", "2026-11-18"])
        self.assertEqual(rows[2], ["B", 2.5, -3, "1e3"])
        self.vm.xlsx_append_rows("w.xlsx", "S", [["C", "4", "x", "y"]])
        self.vm.xlsx_set_cells("w.xlsx", "S", {"B2": "9"})
        rows = self.vm.xlsx_read("w.xlsx")["rows"]
        self.assertEqual((rows[1][1], rows[3][1]), (9, 4))


JUNK = {"is_dir": False, "text": "Sales", "values": [], "rows": [], "source_dir": "x/", "destination_dir": "x/",
        "extension": None, "expected_files": None, "snapshot_error": None, "recursive": False, "preserved_files": None,
        "cell": "B7", "value": 123.5, "tolerance": 0.005, "contains": "SUM(B2:C2)", "count": 5, "column": "Revenue",
        "group_by": "Region", "op": "sum|avg|min|max|count", "source_path": "W", "source_sheet": "optional",
        "preserved_sheets": None}


class RealPlannerOutputTests(unittest.TestCase):
    """Regression: the actual s06 plan from qwen3:8b (prompt examples copied into every field)."""
    W = "_eval/s06/sales.xlsx"
    GOAL = f"Read {W} (sheet Sales). Add a new sheet Totals with the header Region, Revenue and one row per region."

    def a(self, kind, sheet, **kw):
        base = {**JUNK, "source_path": self.W, "kind": kind, "path": self.W, "sheet": sheet}
        return {**base, **kw}

    def plan(self):
        return [
            sg(self.a("xlsx_sheet_exists", "Sales"), self.a("xlsx_values_present", "Sales"),
               instruction="Inspect the Sales sheet in sales.xlsx to confirm it has a Region column."),
            sg(self.a("xlsx_sheet_exists", "Totals"), instruction="Create a new sheet named Totals in the workbook."),
            sg(self.a("xlsx_group_aggregate_matches", "Totals", source_sheet="Sales", op="sum"),
               self.a("xlsx_data_row_count", "Totals", source_sheet="Sales"),
               instruction="Compute the total revenue per region from Sales and populate Totals."),
            sg(self.a("xlsx_sheet_exists", "Sales"), self.a("xlsx_values_present", "Sales"),
               instruction="Ensure the Sales sheet remains unchanged."),
        ]

    def test_junk_plan_collapses_to_one_clean_subgoal(self):
        from agent.plan_validation import consolidate_sheet_subgoals
        plan = self.plan()
        anchor_sheet_paths_to_goal(self.GOAL, plan)
        normalize_sheet_checks(self.GOAL, plan)
        consolidate_sheet_subgoals(plan)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["id"], 0)
        checks = plan[0]["check_spec"]["checks"]
        self.assertEqual([c["kind"] for c in checks], ["xlsx_sheet_exists", "xlsx_group_aggregate_matches"])
        group = checks[1]
        self.assertEqual({k: group[k] for k in ("sheet", "source_path", "source_sheet", "column", "group_by", "op")},
                         {"sheet": "Totals", "source_path": self.W, "source_sheet": "Sales",
                          "column": "Revenue", "group_by": "Region", "op": "sum"})
        for junk in ("cell", "value", "contains", "count", "text", "is_dir"):
            self.assertNotIn(junk, group)
        self.assertIn("Create a new sheet", plan[0]["instruction"])
        self.assertIn("populate Totals", plan[0]["instruction"])

    def test_placeholder_op_drops_the_group_check_instead_of_guessing(self):
        plan = self.plan()
        plan[2]["check_spec"]["checks"][0]["op"] = "sum|avg|min|max|count"
        normalize_sheet_checks(self.GOAL, plan)
        self.assertEqual([c["kind"] for c in plan[2]["check_spec"]["checks"]], [])

    def test_bare_filename_in_instruction_is_restored_even_when_check_paths_are_full(self):
        plan = [sg({"kind": "xlsx_sheet_exists", "path": self.W, "sheet": "Sales"},
                   instruction="Inspect sales.xlsx for a Region column")]
        anchor_sheet_paths_to_goal(self.GOAL, plan)
        self.assertEqual(plan[0]["instruction"], f"Inspect {self.W} for a Region column")

    def test_row_count_is_kept_only_when_the_request_states_it(self):
        goal = "Add four rows to t.xlsx sheet S"
        plan = [sg({"kind": "xlsx_data_row_count", "path": "t.xlsx", "sheet": "S", "count": 4},
                   {"kind": "xlsx_data_row_count", "path": "t.xlsx", "sheet": "S", "count": 5})]
        normalize_sheet_checks(goal, plan)
        self.assertEqual([c["count"] for c in plan[0]["check_spec"]["checks"]], [4])

    def test_files_then_sheet_subgoals_are_not_merged(self):
        from agent.plan_validation import consolidate_sheet_subgoals
        plan = [sg({"kind": "path_exists", "path": "Q4.xlsx"}, instruction="Copy template.xlsx to Q4.xlsx", cap="files"),
                sg({"kind": "xlsx_cell_equals", "path": "Q4.xlsx", "sheet": "Budget", "cell": "B2", "value": 5000},
                   instruction="In Q4.xlsx set B2 to 5000")]
        consolidate_sheet_subgoals(plan)
        self.assertEqual([x["capability"] for x in plan], ["files", "sheet"])


if __name__ == "__main__":
    unittest.main()
