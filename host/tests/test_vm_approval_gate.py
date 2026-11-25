"""VM mode asks the human before destroying data, like local mode (no VM or model needed)."""
import asyncio
import unittest

from langchain_core.tools import StructuredTool

from agent.runtime import VM

OLD = [["Name", "Score"], ["Old1", 1], ["Old2", 2]]
NEW = [["Name", "Score"], ["A", 1]]


class FakeVM(VM):
    """VM client whose 'server' is a dict of sheets, so the gate can be tested offline."""

    def __init__(self):
        super().__init__(url="http://unused/mcp")
        self.sheets = {("d.xlsx", "Data"): OLD}

        async def xlsx_write(path: str, rows: list, sheet: str = "Sheet1", replace_sheet: bool = True) -> str:
            self.sheets[(path, sheet)] = rows
            return f"wrote {len(rows)} rows"

        self.tools = {"xlsx_write": StructuredTool.from_function(coroutine=xlsx_write, name="xlsx_write",
                                                                 description="write")}

    async def call(self, name, **args):
        if name == "path_info":
            return {"exists": any(p == args["path"] for p, _ in self.sheets)}
        if name == "xlsx_info":
            return {"sheets": [{"name": s, "used_rows": len(r)} for (p, s), r in self.sheets.items()
                               if p == args["path"]]}
        raise KeyError(name)

    def writer(self):
        return next(t for t in self.for_worker("sheet") if t.name == "xlsx_write")


class VMApprovalGateTests(unittest.TestCase):
    def run_write(self, vm, **args):
        return asyncio.run(vm.writer().ainvoke({"path": "d.xlsx", "rows": NEW, "sheet": "Data", **args}))

    def test_denied_replace_keeps_data(self):
        vm = FakeVM()
        vm.approver = lambda req: False
        out = self.run_write(vm)
        self.assertIn("DENIED", out)
        self.assertEqual(vm.sheets[("d.xlsx", "Data")], OLD)
        self.assertEqual([a["approved"] for a in vm.approval_log], [False])

    def test_approved_replace_is_logged_and_done(self):
        vm = FakeVM()
        vm.approver = lambda req: True
        self.run_write(vm)
        self.assertEqual(vm.sheets[("d.xlsx", "Data")], NEW)
        self.assertIn("3 existing row(s)", vm.approval_log[0]["summary"])

    def test_no_approver_fails_closed(self):
        vm = FakeVM()
        self.assertIn("DENIED", self.run_write(vm))

    def test_new_sheet_and_own_sheet_need_no_approval(self):
        vm = FakeVM()
        vm.approver = lambda req: False
        asyncio.run(vm.writer().ainvoke({"path": "new.xlsx", "rows": NEW, "sheet": "Fresh"}))
        asyncio.run(vm.writer().ainvoke({"path": "new.xlsx", "rows": OLD, "sheet": "Fresh"}))
        self.assertEqual(vm.approval_log, [])
        self.assertEqual(vm.sheets[("new.xlsx", "Fresh")], OLD)


if __name__ == "__main__":
    unittest.main()
