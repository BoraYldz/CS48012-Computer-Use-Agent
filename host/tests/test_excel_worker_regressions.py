"""Regression tests for spreadsheet workflow routing and safe write semantics."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook

from agent.checkers import check_spec
from agent.plan_validation import normalize_sheet_checks, normalize_spreadsheet_workflows
from agent.graph import PLANNER_PROMPT, SHEET_WORKER_PROMPT
from agent import sheet_tools


class FakeCheckerVM:
    def __init__(self, source_text="Region,Revenue\nNorth,10\nSouth,20\nNorth,5\n", target_rows=None):
        self.source_text = source_text
        self.target_rows = target_rows or [["Region", "Revenue"], ["North", 15], ["South", 20]]

    async def call(self, name, **kwargs):
        if name == "path_info":
            return {"exists": True, "is_dir": False}
        if name == "read_text":
            return self.source_text
        if name == "xlsx_read":
            path = kwargs.get("path")
            sheet = kwargs.get("sheet")
            if path.endswith("source.csv"):
                raise AssertionError("CSV checker should read CSV as text, not as XLSX")
            rows = ([["Region", "Revenue"], ["North", 10], ["South", 20], ["North", 5]]
                    if sheet == "Sales" else self.target_rows)
            names = ["Stock"] if path.endswith("inventory.xlsx") else ["Sales", "Totals"]
            return {
                "sheets": names,
                "sheet": sheet or names[0],
                "rows": rows,
                "total_rows": len(rows),
                "truncated": False,
            }
        raise AssertionError(f"Unexpected call: {name}")


class FakeFileHashVM:
    def __init__(self, hashes):
        self.hashes = hashes

    async def call(self, name, **kwargs):
        if name == "path_info":
            path = kwargs["path"]
            return {"exists": path in self.hashes, "is_dir": False}
        if name == "list_dir":
            directory = kwargs["path"]
            entries = []
            for path, digest in self.hashes.items():
                p = Path(path)
                parent = p.parent.as_posix() if p.parent.as_posix() != "." else "."
                if parent == directory:
                    entries.append({"path": path, "type": "file", "sha256": digest})
            return entries
        raise AssertionError(f"Unexpected call: {name}")


class SpreadsheetEngineRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_csv_import_preserves_turkish_text_numeric_types_and_formula_like_text(self):
        source = self.root / "sales.csv"
        output = self.root / "sales.xlsx"
        source.write_text(
            "Bölge,Ürün,Adet,Gelir\nİstanbul,Çay,10,125.5\nAnkara,Şeker,7,80\n"
            "İzmir,=1+1,3,45.25\n",
            encoding="utf-8",
        )

        sheet_tools.import_csv(source, output, "Sales")
        wb = load_workbook(output, data_only=False)
        try:
            ws = wb["Sales"]
            self.assertEqual(ws.max_row, 4)
            self.assertEqual(ws["A2"].value, "İstanbul")
            self.assertEqual(ws["C2"].value, 10)
            self.assertIsInstance(ws["C2"].value, int)
            self.assertEqual(ws["D2"].value, 125.5)
            self.assertEqual(ws["B4"].value, "=1+1")
            self.assertEqual(ws["B4"].data_type, "s")
        finally:
            wb.close()

        computed = sheet_tools.read(output, "Sales", "A1:D4", formulas=False)
        self.assertEqual(computed["rows"][3][1], "=1+1")

    def test_csv_import_refuses_to_overwrite_existing_workbook(self):
        source = self.root / "sales.csv"
        output = self.root / "sales.xlsx"
        source.write_text("Item,Qty\nPen,2\n", encoding="utf-8")
        wb = Workbook()
        wb.active["A1"] = "keep"
        wb.save(output)
        wb.close()

        with self.assertRaises(FileExistsError):
            sheet_tools.import_csv(source, output, "Sales")
        wb = load_workbook(output)
        try:
            self.assertEqual(wb.active["A1"].value, "keep")
        finally:
            wb.close()

    def test_write_false_does_not_append_to_existing_sheet(self):
        path = self.root / "report.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "Tasks"
        ws.append(["Task", "Owner"])
        ws.append(["Write report", "Ayşe"])
        wb.save(path)
        wb.close()

        with self.assertRaisesRegex(FileExistsError, "will not append or modify"):
            sheet_tools.write(path, [["Task", "Owner", "Done"], ["Write report", "Ayşe", "no"]],
                              "Tasks", replace_sheet=False)
        result = sheet_tools.read(path, "Tasks")
        self.assertEqual(result["rows"], [["Task", "Owner"], ["Write report", "Ayşe"]])

    def test_row_label_update_targets_existing_value_column(self):
        path = self.root / "prices.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "Prices"
        for row in [["Item", "Price"], ["Tea", 10], ["Coffee", 20], ["Juice", 15], ["Water", 5]]:
            ws.append(row)
        wb.save(path)
        wb.close()

        sheet_tools.update_row_by_value(path, "Prices", "Item", "Coffee", {"Price": 25})
        sheet_tools.update_row_by_value(path, "Prices", "Item", "Water", {"Price": 6})
        wb = load_workbook(path)
        try:
            ws = wb["Prices"]
            self.assertEqual(ws.max_column, 2)
            self.assertEqual(ws["B3"].value, 25)
            self.assertEqual(ws["B5"].value, 6)
        finally:
            wb.close()

    def test_formula_column_writes_one_real_formula_per_student_and_preserves_other_cells(self):
        path = self.root / "grades.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "Grades"
        for row in [["Student", "Math", "Physics"], ["Ada", 80, 90], ["Can", 70, 65],
                    ["Eda", 95, 88], ["Mert", 60, 72]]:
            ws.append(row)
        wb.save(path)
        wb.close()

        sheet_tools.add_formula_column(path, "Grades", "D", "Total", "=B{row}+C{row}")
        formulas = sheet_tools.read(path, "Grades", "D1:D5", formulas=True)["rows"]
        self.assertEqual(formulas, [["Total"], ["=B2+C2"], ["=B3+C3"], ["=B4+C4"], ["=B5+C5"]])
        computed = sheet_tools.read(path, "Grades", "D1:D5", formulas=False)["rows"]
        self.assertEqual(computed, [["Total"], [170], [135], [183], [132]])
        wb = load_workbook(path, data_only=False)
        try:
            self.assertEqual(wb["Grades"]["A2"].value, "Ada")
            self.assertEqual(wb["Grades"]["C5"].value, 72)
            self.assertEqual(wb["Grades"]["D2"].data_type, "f")
        finally:
            wb.close()


class SpreadsheetPlanningRegressionTests(unittest.TestCase):
    def test_workbook_creation_is_routed_from_files_to_sheet(self):
        goal = ("Create a workbook _eval/s01_create_table/inventory.xlsx with one sheet named Stock. "
                "Header Item, Qty, Price. Then rows Pen, 120, 1.5; Notebook, 45, 3.25; Stapler, 12, 8.9.")
        subgoals = [{"id": 0, "capability": "files", "instruction": "Create the output directory.",
                     "expected_output": "Directory created", "check_spec": {"checks": [
                         {"kind": "path_exists", "path": "_eval/s01_create_table/inventory.xlsx", "is_dir": False},
                         {"kind": "xlsx_sheet_exists", "path": "_eval/s01_create_table/inventory.xlsx", "sheet": "Stock"},
                     ]}}]
        changes = normalize_spreadsheet_workflows(goal, subgoals)
        self.assertTrue(changes)
        self.assertEqual(len(subgoals), 1)
        self.assertEqual(subgoals[0]["capability"], "sheet")
        self.assertIn("inventory.xlsx", subgoals[0]["instruction"])

    def test_csv_import_is_one_sheet_subgoal_with_source_comparison(self):
        goal = ("Convert _eval/s02/sales.csv into a new workbook _eval/s02/sales.xlsx "
                "with one sheet named Sales. Keep all rows and numeric columns.")
        subgoals = []
        normalize_spreadsheet_workflows(goal, subgoals)
        self.assertEqual(len(subgoals), 1)
        self.assertEqual(subgoals[0]["capability"], "sheet")
        tools = {a["kind"] for a in subgoals[0]["check_spec"]["checks"]}
        self.assertIn("xlsx_csv_matches", tools)
        self.assertIn("_eval/s02/sales.csv", subgoals[0]["instruction"])

    def test_copy_then_edit_inserts_files_copy_before_sheet_edit(self):
        goal = ("Make a copy of _eval/s13/template.xlsx named _eval/s13/Q4.xlsx, "
                "then in the copy (sheet Budget) set cell B2 to 5000. The template must stay unchanged.")
        subgoals = [{"id": 0, "capability": "sheet", "instruction": "Edit the workbook", "expected_output": "done",
                     "check_spec": {"checks": []}}]
        normalize_spreadsheet_workflows(goal, subgoals)
        normalize_sheet_checks(goal, subgoals)
        self.assertEqual([sg["capability"] for sg in subgoals], ["files", "sheet"])
        self.assertIn("copy", subgoals[0]["instruction"].lower())
        self.assertIn("file_content_matches", {a["kind"] for a in subgoals[0]["check_spec"]["checks"]})
        assertion = subgoals[1]["check_spec"]["checks"][-1]
        self.assertEqual((assertion["cell"], assertion["value"]), ("B2", 5000))
        self.assertEqual(assertion["source_path"], "_eval/s13/template.xlsx")

    def test_group_aggregate_placeholder_is_dropped_instead_of_guessed(self):
        goal = "Add total revenue per region to a new Totals sheet, ordered by region name A to Z."
        subgoals = [{"id": 0, "capability": "sheet", "instruction": "Calculate totals", "expected_output": "done",
                     "check_spec": {"checks": [{"kind": "xlsx_group_aggregate_matches", "path": "sales.xlsx",
                         "sheet": "Totals", "source_path": "sales.xlsx", "source_sheet": "Sales",
                         "column": "Revenue", "group_by": "Region", "op": "sum|avg|min|max|count"}]}}]
        changes = normalize_sheet_checks(goal, subgoals)
        self.assertEqual(subgoals[0]["check_spec"]["checks"], [])
        self.assertTrue(any(c.get("reason") == "op is the prompt placeholder, not a value" for c in changes))

    def test_sheet_worker_prompt_formats_formula_row_placeholder(self):
        rendered = SHEET_WORKER_PROMPT.format(cap="sheet", root_guidance="test paths")
        self.assertIn("formula template using {row}", rendered)
        self.assertIn("=B{row}+C{row}", rendered)

    def test_planner_prompt_shows_one_exact_aggregate_operation(self):
        self.assertIn('"op":"sum"', PLANNER_PROMPT)
        self.assertNotIn('"op":"sum|avg|min|max|count"', PLANNER_PROMPT)


class SpreadsheetCheckerRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_copy_checker_requires_matching_sha256(self):
        spec = {"checks": [{"kind": "file_content_matches", "path": "Q4.xlsx", "source_path": "template.xlsx"}]}
        good = await check_spec(FakeFileHashVM({"template.xlsx": "same", "Q4.xlsx": "same"}),
                                spec, capability="files")
        self.assertEqual(good["status"], "pass")
        bad = await check_spec(FakeFileHashVM({"template.xlsx": "original", "Q4.xlsx": "different"}),
                               spec, capability="files")
        self.assertEqual(bad["status"], "fail")
        self.assertIn("SHA-256 mismatch", bad["evidence"])

    async def test_csv_checker_rejects_numeric_text(self):
        vm = FakeCheckerVM(source_text="Item,Qty\nPen,2\n",
                           target_rows=[["Item", "Qty"], ["Pen", "2"]])
        spec = {"checks": [{"kind": "xlsx_csv_matches", "path": "inventory.xlsx", "sheet": "Stock",
                            "source_path": "items.csv"}]}
        result = await check_spec(vm, spec, capability="sheet")
        self.assertEqual(result["status"], "fail")
        self.assertIn("expected numeric", result["evidence"])

    async def test_csv_checker_accepts_numeric_cells_and_exact_rows(self):
        vm = FakeCheckerVM(source_text="Item,Qty\nPen,2\n",
                           target_rows=[["Item", "Qty"], ["Pen", 2]])
        spec = {"checks": [{"kind": "xlsx_csv_matches", "path": "inventory.xlsx", "sheet": "Stock",
                            "source_path": "items.csv"}]}
        result = await check_spec(vm, spec, capability="sheet")
        self.assertEqual(result["status"], "pass", result["detail"])

    async def test_group_aggregate_checker_accepts_correct_complete_sorted_totals(self):
        vm = FakeCheckerVM(target_rows=[["Region", "Revenue"], ["North", 15], ["South", 20]])
        spec = {"checks": [{"kind": "xlsx_group_aggregate_matches", "path": "sales.xlsx",
                            "sheet": "Totals", "source_path": "source.xlsx", "source_sheet": "Sales",
                            "column": "Revenue", "group_by": "Region", "op": "sum"}]}
        result = await check_spec(vm, spec, capability="sheet")
        self.assertEqual(result["status"], "pass", result["detail"])

    async def test_group_aggregate_checker_rejects_wrong_order(self):
        vm = FakeCheckerVM(target_rows=[["Region", "Revenue"], ["South", 20], ["North", 15]])
        spec = {"checks": [{"kind": "xlsx_group_aggregate_matches", "path": "sales.xlsx",
                            "sheet": "Totals", "source_path": "source.xlsx", "source_sheet": "Sales",
                            "column": "Revenue", "group_by": "Region", "op": "sum"}]}
        result = await check_spec(vm, spec, capability="sheet")
        self.assertEqual(result["status"], "fail")
        self.assertIn("A-Z order", result["evidence"])


if __name__ == "__main__":
    unittest.main()
