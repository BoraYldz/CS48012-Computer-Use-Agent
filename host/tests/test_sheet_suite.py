"""Validate the Excel eval tasks themselves (no LLM). A wrong ground-truth check would silently
corrupt every pass^k number, so: it must accept the reference solution, reject a do-nothing baseline
wherever work is required, and reject the typical wrong end states."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from agent.local_runtime import LocalVM
from tasks import sheet_suite as ss


def run(coro):
    return asyncio.run(coro)


class SheetSuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vm = LocalVM(Path(self.temp.name) / "AgentWorkspace")
        self.addCleanup(self.temp.cleanup)

    async def _prepare(self, task):
        base = await ss.reset(self.vm, task)
        self.vm.approval_log.clear()
        self.vm.approver = lambda req, _a=task.approve: _a
        return base

    def _ctx(self):
        return {"approvals": list(self.vm.approval_log)}

    def test_ids_unique_and_requests_reference_their_base(self):
        tasks = ss.all_tasks()
        self.assertEqual(len({t.id for t in tasks}), len(tasks))
        for t in tasks:
            self.assertIn(ss.base_of(t), t.request(ss.base_of(t)))

    def test_core_and_extra_split(self):
        self.assertEqual(len(ss.select(tag="core")), 13)
        self.assertEqual(len(ss.select(tag="all")), 14)
        self.assertEqual([t.id for t in ss.select(only="s06")], ["s06_grouped_totals"])

    def test_reference_solution_passes_every_task(self):
        for task in ss.all_tasks():
            with self.subTest(task=task.id):
                async def go():
                    base = await self._prepare(task)
                    await task.solve(self.vm, base)
                    return await task.check(self.vm, base, self._ctx())
                ok, detail = run(go())
                self.assertTrue(ok, detail)

    def test_do_nothing_fails_every_task_that_requires_work(self):
        for task in ss.all_tasks():
            async def go():
                base = await self._prepare(task)
                return await task.check(self.vm, base, self._ctx())
            ok, detail = run(go())
            with self.subTest(task=task.id):
                self.assertEqual(ok, not task.needs_change, f"{task.id}: {detail}")

    # ---- the checks must catch the characteristic wrong answers
    def _break(self, tid, mutate):
        task = ss.select(only=tid)[0]
        async def go():
            base = await self._prepare(task)
            await task.solve(self.vm, base)
            mutate(self.vm, base)
            return await task.check(self.vm, base, self._ctx())
        return run(go())

    def test_numbers_stored_as_text_are_rejected(self):
        def plant_text(vm, b):                       # tools coerce "120" -> 120, so write text directly
            from openpyxl import load_workbook
            wb = load_workbook(vm._p(f"{b}/inventory.xlsx")); wb["Stock"]["B2"] = "120"; wb.save(vm._p(f"{b}/inventory.xlsx"))
        ok, detail = self._break("s01", plant_text)
        self.assertFalse(ok)
        self.assertIn("row 2", detail)

    def test_stray_default_sheet_is_rejected(self):
        ok, detail = self._break("s01", lambda vm, b: vm.xlsx_write(f"{b}/inventory.xlsx", [["x"]], sheet="Sheet"))
        self.assertFalse(ok)
        self.assertIn("unexpected", detail)

    def test_collateral_damage_to_other_sheet_is_rejected(self):
        ok, detail = self._break("s05", lambda vm, b: vm.xlsx_set_cells(f"{b}/report.xlsx", "Notes", {"A2": "changed"}))
        self.assertFalse(ok)
        self.assertIn("Notes", detail)

    def test_pasted_value_instead_of_formula_is_rejected(self):
        ok, detail = self._break("s08", lambda vm, b: vm.xlsx_set_cells(f"{b}/grades.xlsx", "Grades", {"D3": 135}))
        self.assertFalse(ok)
        self.assertIn("row 3", detail)

    def test_equivalent_formula_spellings_are_accepted(self):
        ok, detail = self._break("s08", lambda vm, b: vm.xlsx_set_cells(
            f"{b}/grades.xlsx", "Grades", {"D2": "=sum($B2:$C2)", "D3": "=C3 + B3"}))
        self.assertTrue(ok, detail)

    def test_wrong_totals_and_wrong_order_are_rejected(self):
        ok, _ = self._break("s06", lambda vm, b: vm.xlsx_set_cells(f"{b}/sales.xlsx", "Totals", {"B2": 999}))
        self.assertFalse(ok)
        ok, _ = self._break("s07", lambda vm, b: vm.xlsx_set_cells(f"{b}/orders.xlsx", "Big", {"A2": 1001, "A3": 1005}))
        self.assertFalse(ok)

    def test_replace_instead_of_append_is_flagged(self):
        task = ss.select(only="s03")[0]
        async def go():
            base = await self._prepare(task)
            self.vm.approver = lambda req: True
            self.vm.xlsx_write(f"{base}/log.xlsx", [["Date", "Event"], ["2026-11-18", "Deploy"], ["2026-11-19", "Rollback"]],
                               sheet="Log", replace_sheet=True)
            return await task.check(self.vm, base, self._ctx())
        ok, _ = run(go())
        self.assertFalse(ok)

    def test_approved_replacement_without_a_request_is_rejected(self):
        task = ss.select(only="s09")[0]
        async def go():
            base = await self._prepare(task)
            await task.solve(self.vm, base)
            return await task.check(self.vm, base, {"approvals": []})     # no approval on record
        ok, detail = run(go())
        self.assertFalse(ok)
        self.assertIn("approval", detail)

    def test_injection_check_flags_destructive_attempt(self):
        task = ss.select(only="s12")[0]
        async def go():
            base = await self._prepare(task)
            await task.solve(self.vm, base)
            try:
                self.vm.xlsx_write(f"{base}/tasks.xlsx", [["HACKED"]], sheet="Secrets")   # denied by policy
            except PermissionError:
                pass
            return await task.check(self.vm, base, self._ctx())
        ok, detail = run(go())
        self.assertFalse(ok)
        self.assertIn("destructive", detail)

    def test_invented_output_for_missing_source_is_flagged(self):
        ok, detail = self._break("s11", lambda vm, b: vm.xlsx_write(f"{b}/out.xlsx", [[0]], sheet="Total"))
        self.assertFalse(ok)
        self.assertIn("invented", detail)

    def test_template_modification_is_flagged(self):
        ok, detail = self._break("s13", lambda vm, b: vm.xlsx_set_cells(f"{b}/template.xlsx", "Budget", {"B2": 5000}))
        self.assertFalse(ok)
        self.assertIn("template changed", detail)


if __name__ == "__main__":
    unittest.main()
