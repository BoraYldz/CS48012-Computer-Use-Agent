"""Sheet engine: formula evaluator, read/write tools, calculator tools, approval gate (no LLM)."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from agent import sheet_tools as st
from agent.local_runtime import LocalVM
from agent.runtime import WORKER_TOOLS, max_steps
assert "xlsx_group_summary" in WORKER_TOOLS["sheet"]


def ev(formula, cells=None):
    cells = cells or {}
    def resolve(ref):
        ref = ref.replace("$", "")
        if ":" in ref:
            a, b = ref.split(":")
            col, r1, r2 = a[0], int(a[1:]), int(b[1:])
            return [cells.get(f"{col}{r}") for r in range(r1, r2 + 1)]
        return cells.get(ref)
    return st.evaluate(formula, resolve)


class EvaluatorTests(unittest.TestCase):
    def test_arithmetic_and_precedence(self):
        self.assertEqual(ev("=1+2*3"), 7)
        self.assertEqual(ev("=(1+2)*3"), 9)
        self.assertEqual(ev("=2^3^2"), 64)          # left-assoc like Excel: (2^3)^2
        self.assertEqual(ev("=-2^2"), 4)            # Excel: unary minus binds tighter than ^
        self.assertEqual(ev("=10/4"), 2.5)
        self.assertEqual(ev('="a"&"b"&1'), "ab1")

    def test_functions_and_ranges(self):
        cells = {"A1": 1, "A2": 2, "A3": 3, "A4": "text", "A5": None}
        self.assertEqual(ev("=SUM(A1:A5)", cells), 6)
        self.assertEqual(ev("=AVERAGE(A1:A3)", cells), 2)
        self.assertEqual(ev("=MAX(A1:A3, 10)", cells), 10)
        self.assertEqual(ev("=MIN(A1:A3)", cells), 1)
        self.assertEqual(ev("=COUNT(A1:A5)", cells), 3)
        self.assertEqual(ev("=ROUND(2.5,0)", cells), 3)      # half away from zero
        self.assertEqual(ev("=ROUND(-2.5,0)", cells), -3)
        self.assertEqual(ev("=ROUND(1.2345,2)", cells), 1.23)
        self.assertEqual(ev("=ABS(-4)"), 4)

    def test_if_is_lazy_so_untaken_branch_may_error(self):
        self.assertEqual(ev("=IF(B1=0,0,A1/B1)", {"A1": 5, "B1": 0}), 0)
        self.assertEqual(ev("=IF(B1=0,0,A1/B1)", {"A1": 6, "B1": 3}), 2)
        self.assertEqual(ev('=IF(A1>=10,"big","small")', {"A1": 12}), "big")

    def test_logical_functions(self):
        self.assertEqual(ev("=IF(AND(A1>0,A2>0),1,0)", {"A1": 1, "A2": 2}), 1)
        self.assertEqual(ev("=IF(AND(A1>0,A2>0),1,0)", {"A1": 1, "A2": -2}), 0)
        self.assertEqual(ev("=IF(OR(A1>5,A2>5),1,0)", {"A1": 1, "A2": 2}), 0)
        self.assertEqual(ev("=IF(NOT(A1>5),1,0)", {"A1": 1}), 1)

    def test_errors_are_reported_not_guessed(self):
        with self.assertRaisesRegex(st.FormulaError, "DIV"):
            ev("=1/0")
        with self.assertRaisesRegex(st.FormulaError, "unsupported"):
            ev("=VLOOKUP(1,A1:B2,2)")
        with self.assertRaises(st.FormulaError):
            ev("=1+")


class WorkbookToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vm = LocalVM(Path(self.tmp.name) / "ws")
        self.addCleanup(self.tmp.cleanup)

    def sheet(self, rows, path="w.xlsx", name="S"):
        self.vm.xlsx_write(path, rows, sheet=name)

    def test_formulas_are_computed_on_read_and_visible_as_text(self):
        self.sheet([["a", "b", "sum"], [1, 2, "=A2+B2"], [3, 4, "=SUM(A3:B3)"]])
        vals = self.vm.xlsx_read("w.xlsx")
        self.assertEqual([r[2] for r in vals["rows"]], ["sum", 3, 7])
        raw = self.vm.xlsx_read("w.xlsx", formulas=True)
        self.assertEqual([r[2] for r in raw["rows"]], ["sum", "=A2+B2", "=SUM(A3:B3)"])

    def test_uncomputable_formula_is_flagged_not_invented(self):
        self.sheet([["x"], ["=VLOOKUP(1,A1:A2,1)"]])
        out = self.vm.xlsx_read("w.xlsx")
        self.assertEqual(out["rows"][1][0], None)
        self.assertEqual(out["uncomputed_formula_cells"], ["A2"])

    def test_cross_sheet_reference_and_circularity(self):
        self.sheet([["v"], [5]], name="Data")
        self.sheet([["t"], ["=SUM(Data!A2:A2)*2"], ["=A3"]], name="Calc")
        out = self.vm.xlsx_read("w.xlsx", sheet="Calc")
        self.assertEqual(out["rows"][1][0], 10)
        self.assertEqual(out["rows"][2][0], None)               # A3 -> itself: circular, flagged
        self.assertIn("A3", out["uncomputed_formula_cells"])

    def test_range_and_truncation(self):
        self.sheet([[i, i * 10] for i in range(1, 31)])
        out = self.vm.xlsx_read("w.xlsx", cell_range="A5:B7")
        self.assertEqual(out["rows"], [[5, 50], [6, 60], [7, 70]])
        self.assertFalse(out["truncated"])
        small = self.vm.xlsx_read("w.xlsx", max_rows=10)
        self.assertEqual(len(small["rows"]), 10)
        self.assertTrue(small["truncated"])

    def test_info_lists_sheets_headers_and_sizes(self):
        self.sheet([["Name", "Score"], ["A", 1], ["B", 2]], name="Results")
        info = self.vm.xlsx_info("w.xlsx")["sheets"][0]
        self.assertEqual(info, {"name": "Results", "used_rows": 3, "columns": 2, "header": ["Name", "Score"]})

    def test_missing_workbook_and_sheet_errors_are_actionable(self):
        with self.assertRaisesRegex(FileNotFoundError, "does not exist"):
            self.vm.xlsx_read("nope.xlsx")
        self.sheet([["a"]])
        with self.assertRaisesRegex(KeyError, "available sheets"):
            self.vm.xlsx_set_cells("w.xlsx", "Missing", {"A1": 1})

    def test_write_new_workbook_has_exactly_the_requested_sheet(self):
        self.sheet([["a"]], name="Stock")
        self.assertEqual(self.vm.xlsx_info("w.xlsx")["sheets"][0]["name"], "Stock")
        self.assertEqual(len(self.vm.xlsx_info("w.xlsx")["sheets"]), 1)

    def test_replace_existing_data_needs_approval_and_fails_closed(self):
        st.write(self.vm._p("w.xlsx"), [["a"], [1], [2]], "S")          # pre-existing user data
        with self.assertRaisesRegex(PermissionError, "DENIED"):
            self.vm.xlsx_write("w.xlsx", [["z"]], sheet="S")
        self.assertEqual(self.vm.xlsx_read("w.xlsx")["rows"], [["a"], [1], [2]])
        seen = []
        self.vm.approver = lambda req: seen.append(req) or True
        self.vm.xlsx_write("w.xlsx", [["z"]], sheet="S")
        self.assertEqual(self.vm.xlsx_read("w.xlsx")["rows"], [["z"]])
        self.assertIn("REPLACE sheet 'S'", seen[0]["summary"])
        self.assertIn("3 existing row", seen[0]["summary"])   # header + 2 data rows

    def test_sheet_the_agent_created_itself_can_be_rebuilt_without_approval(self):   # s06: denied own sheet
        self.sheet([["a"], [1], [2]], name="Mine")
        self.vm.xlsx_write("w.xlsx", [["a"], [9]], sheet="Mine")
        self.assertEqual(self.vm.approval_log, [])
        self.assertEqual(self.vm.xlsx_read("w.xlsx", sheet="Mine")["rows"], [["a"], [9]])

    def test_fixture_data_stays_protected_even_after_agent_wrote_the_same_path(self):
        self.sheet([["a"], [1]], name="S")
        import base64
        self.vm.harness_put_file("w.xlsx", base64.b64encode((self.vm._p("w.xlsx")).read_bytes()).decode())
        with self.assertRaisesRegex(PermissionError, "DENIED"):
            self.vm.xlsx_write("w.xlsx", [["z"]], sheet="S")

    def test_group_summary_computes_writes_and_sorts(self):
        self.sheet([["Region", "Revenue"], ["North", 120.5], ["South", 80], ["East", 200.25], ["North", 99.5],
                    ["West", 40], ["South", 150], ["East", 10.75], ["North", 30]], name="Sales")
        out = self.vm.xlsx_group_summary("w.xlsx", group_by="Region", column="Revenue", source_sheet="Sales",
                                         target_sheet="Totals")
        self.assertIn("wrote 5 rows", out)
        rows = self.vm.xlsx_read("w.xlsx", sheet="Totals")["rows"]
        self.assertEqual(rows, [["Region", "Revenue"], ["East", 211], ["North", 250], ["South", 230], ["West", 40]])
        self.assertEqual(self.vm.xlsx_info("w.xlsx")["sheets"][0]["name"], "Sales")
        self.vm.xlsx_group_summary("w.xlsx", group_by="Region", column="Revenue", source_sheet="Sales",
                                   target_sheet="Totals", op="count")      # own sheet: rebuilt without a prompt
        self.assertEqual(self.vm.approval_log, [])

    def test_creating_new_sheet_or_workbook_needs_no_approval(self):
        self.sheet([["a"]], name="One")
        self.sheet([["b"]], name="Two")                          # new sheet, existing workbook
        self.assertEqual(self.vm.approval_log, [])
        self.assertEqual([s["name"] for s in self.vm.xlsx_info("w.xlsx")["sheets"]], ["One", "Two"])

    def test_replace_keeps_sheet_position(self):
        self.sheet([["a"]], name="First")
        self.sheet([["b"]], name="Second")
        self.vm.approver = lambda req: True
        self.vm.xlsx_write("w.xlsx", [["new"]], sheet="First")
        self.assertEqual([s["name"] for s in self.vm.xlsx_info("w.xlsx")["sheets"]], ["First", "Second"])

    def test_invalid_sheet_name_and_bad_rows_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid sheet name"):
            self.vm.xlsx_write("w.xlsx", [["a"]], sheet="bad/name")
        with self.assertRaisesRegex(ValueError, "list of lists"):
            self.vm.xlsx_write("w.xlsx", ["a", "b"], sheet="S")

    def test_append_rows_keeps_existing_and_skips_blank_tail(self):
        self.sheet([["a", "b"], [1, 2]])
        out = self.vm.xlsx_append_rows("w.xlsx", "S", [[3, 4], [5, 6]])
        self.assertIn("rows 3-4", out)
        self.assertEqual(self.vm.xlsx_read("w.xlsx")["rows"], [["a", "b"], [1, 2], [3, 4], [5, 6]])
        self.assertEqual(self.vm.approval_log, [])
        with self.assertRaisesRegex(FileNotFoundError, "xlsx_write"):
            self.vm.xlsx_append_rows("none.xlsx", "S", [[1]])

    def test_set_cells_reports_old_and_new_and_validates_addresses(self):
        self.sheet([["a", "b"], [1, 2]])
        msg = self.vm.xlsx_set_cells("w.xlsx", "S", {"B2": 99, "c1": "=A2+B2"})
        self.assertIn("B2: 2 -> 99", msg)
        self.assertEqual(self.vm.xlsx_read("w.xlsx")["rows"], [["a", "b", 100], [1, 99, None]])
        with self.assertRaisesRegex(ValueError, "invalid cell"):
            self.vm.xlsx_set_cells("w.xlsx", "S", {"BAD": 1})

    def test_aggregate_where_and_group_by(self):
        self.sheet([["Region", "Revenue", "Note"], ["N", 10.5, "x"], ["S", 20, "y"], ["N", 4.5, "z"], ["E", None, "w"]])
        agg = lambda **k: self.vm.xlsx_aggregate("w.xlsx", **k)
        self.assertEqual(agg(column="Revenue", op="sum")["value"], 35)
        self.assertEqual(agg(column="revenue", op="avg")["value"], round(35 / 3, 10))
        self.assertEqual(agg(column="B", op="max")["value"], 20)
        self.assertEqual(agg(op="count")["value"], 4)
        self.assertEqual(agg(column="Revenue", op="sum", group_by="Region")["groups"], {"N": 15, "S": 20, "E": None})
        self.assertEqual(agg(column="Revenue", op="sum", where=[{"column": "Region", "op": "==", "value": "n"}])["value"], 15)
        with self.assertRaisesRegex(KeyError, "unknown column"):
            agg(column="Profit", op="sum")

    def test_query_filters_sorts_and_projects(self):
        self.sheet([["Item", "Amount"], ["a", 5], ["b", 150], ["c", 100], ["d", 120.5], ["e", None]])
        out = self.vm.xlsx_query("w.xlsx", where=[{"column": "Amount", "op": ">", "value": 100}],
                                 order_by="Amount", descending=True)
        self.assertEqual(out["rows"], [["b", 150], ["d", 120.5]])
        self.assertEqual(out["matched"], 2)
        asc = self.vm.xlsx_query("w.xlsx", order_by="Amount", columns=["Item"], limit=3)
        self.assertEqual(asc["header"], ["Item"])
        self.assertEqual(asc["rows"], [["a"], ["c"], ["d"]])
        contains = self.vm.xlsx_query("w.xlsx", where=[{"column": "Item", "op": "contains", "value": "B"}])
        self.assertEqual(contains["rows"], [["b", 150]])

    def test_sandbox_still_applies(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            self.vm.xlsx_write("../escape.xlsx", [["a"]], sheet="S")


class WiringTests(unittest.TestCase):
    def test_sheet_worker_is_least_privilege(self):
        tools = set(WORKER_TOOLS["sheet"])
        for forbidden in ("write_text", "move_path", "move_files", "copy_path", "delete_path", "make_dirs"):
            self.assertNotIn(forbidden, tools)
        self.assertFalse({t for t in tools if t.startswith("harness_")})
        vm = asyncio.run(LocalVM(Path(tempfile.mkdtemp()) / "ws").connect())
        self.assertEqual({t.name for t in vm.for_worker("sheet")}, tools)

    def test_each_worker_has_its_own_step_cap(self):
        self.assertEqual(max_steps("files"), 15)
        self.assertEqual(max_steps("sheet"), 12)

    def test_vm_copy_of_engine_is_identical(self):
        here = Path(__file__).resolve().parent.parent
        self.assertEqual((here / "agent" / "sheet_tools.py").read_bytes(),
                         (here.parent / "vm" / "sheet_tools.py").read_bytes(),
                         "vm/sheet_tools.py must be a byte-identical copy of host/agent/sheet_tools.py")


if __name__ == "__main__":
    unittest.main()
