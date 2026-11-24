"""Local MCP transport: equivalence with LocalVM, approval over elicitation, worker tool surface. No LLM."""
import asyncio
import base64
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import openpyxl

from agent.local_runtime import LocalVM, _safe_tool_callable
from agent.mcp_local import McpLocalBackend
from agent.runtime import WORKER_TOOLS

STEPS = [
    ("make_dirs", {"path": "d"}),
    ("write_text", {"path": "d/a.txt", "content": "hello"}),
    ("write_text", {"path": "d/b.csv", "content": "Item,Amount\nA,5\nB,7\n"}),
    ("write_text", {"path": "n.txt", "content": "123"}),
    ("read_text", {"path": "n.txt"}),                       # must stay a str, not become int 123
    ("read_text", {"path": "d/b.csv"}),
    ("list_dir", {"path": "d"}),
    ("path_info", {"path": "d/a.txt"}),
    ("copy_files", {"src_dir": "d", "filenames": ["a.txt"], "dst_dir": "e"}),
    ("move_files", {"src_dir": "d", "filenames": ["a.txt"], "dst_dir": "f"}),
    ("move_path", {"src": "n.txt", "dst": "n2.txt"}),
    ("copy_path", {"src": "n2.txt", "dst": "n3.txt"}),
    ("harness_put_file", {"path": "h.bin", "b64": "aGk="}),
    ("harness_get_file", {"path": "h.bin"}),
    ("harness_remove_path", {"path": "h.bin"}),
    ("xlsx_import_csv", {"csv_path": "d/b.csv", "path": "d/t.xlsx"}),
    ("xlsx_info", {"path": "d/t.xlsx"}),
    ("xlsx_read", {"path": "d/t.xlsx"}),
    ("xlsx_query", {"path": "d/t.xlsx", "where": [{"column": "Amount", "op": ">", "value": 5}]}),
    ("xlsx_aggregate", {"path": "d/t.xlsx", "column": "Amount", "op": "sum"}),
    ("xlsx_append_rows", {"path": "d/t.xlsx", "sheet": "Sheet1", "rows": [["C", 9]]}),
    ("xlsx_set_cells", {"path": "d/t.xlsx", "sheet": "Sheet1", "cells": {"B2": 6}}),
    ("xlsx_add_formula_column", {"path": "d/t.xlsx", "sheet": "Sheet1", "column": "C",
                                 "header": "Double", "formula_template": "=B{row}*2"}),
    ("xlsx_add_column", {"path": "d/t.xlsx", "sheet": "Sheet1", "column": "D", "header": "Flag", "value": "x"}),
    ("xlsx_update_row", {"path": "d/t.xlsx", "sheet": "Sheet1", "match_column": "Item",
                         "match_value": "A", "updates": {"Amount": 8}}),
    ("xlsx_group_summary", {"path": "d/t.xlsx", "group_by": "Item", "column": "Amount"}),
    ("xlsx_filter_to_sheet", {"path": "d/t.xlsx", "target_sheet": "Big",
                              "where": [{"column": "Amount", "op": ">", "value": 5}]}),
    ("xlsx_write", {"path": "d/w.xlsx", "rows": [["a", "b"], [1, 2]], "sheet": "Sheet1"}),
    ("xlsx_read", {"path": "d/t.xlsx", "formulas": True}),
    ("list_dir", {"path": ".", "recursive": True}),
]


def _norm(value):
    return value if isinstance(value, str) else json.loads(json.dumps(value, default=str))


class McpLocalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "ws"
        (self.root / "d").mkdir(parents=True)
        (self.root / "d" / "a.tmp").write_text("a")
        (self.root / "d" / "keep.txt").write_text("k")

    def tearDown(self):
        self.temp.cleanup()

    def run_vm(self, fn):
        async def go():
            async with McpLocalBackend(self.root) as vm:
                return await fn(vm)
        return asyncio.run(go())

    # ---------- equivalence ----------
    def test_every_tool_matches_the_direct_backend(self):
        root = Path(self.temp.name) / "eq"

        async def collect(vm):
            out = []
            for name, args in STEPS:
                try:
                    res = await vm.call(name, **args)
                    if name == "list_dir":   # xlsx zips embed timestamps, so their hash/size differ per run
                        res = [{**i, "sha256": None, "size": None} if i["path"].endswith(".xlsx") else i
                               for i in res]
                    out.append(("ok", _norm(res)))
                except Exception:
                    out.append(("err", None))
            return out

        direct = asyncio.run(collect(LocalVM(root)))
        shutil.rmtree(root)

        async def via_mcp():
            async with McpLocalBackend(root) as vm:
                return await collect(vm)

        mcp = asyncio.run(via_mcp())
        errors = [STEPS[i][0] for i, r in enumerate(direct) if r[0] == "err"]
        self.assertEqual(errors, [], f"steps failing on BOTH paths (fix the step): {errors}")
        for (name, _), a, b in zip(STEPS, direct, mcp):
            self.assertEqual(a, b, name)

    def test_tool_surface_matches_direct_tools(self):
        async def go(vm):
            direct = await LocalVM(self.root).connect()
            self.assertEqual(set(vm.tools), set(direct.tools))
            for cap, names in WORKER_TOOLS.items():
                got = [t.name for t in vm.for_worker(cap)]
                self.assertEqual(got, names)
                self.assertFalse([n for n in got if n.startswith("harness_")])
            for name, t in direct.tools.items():
                self.assertEqual(vm.tools[name].description, t.description, name)
                self.assertEqual(set(vm.tools[name].args), set(t.args), name)
                want = t.get_input_schema().model_json_schema().get("required", [])
                self.assertEqual(sorted(vm.tools[name].args_schema.get("required", [])), sorted(want), name)
        self.run_vm(go)

    def test_worker_error_text_is_identical(self):
        ref = _safe_tool_callable(LocalVM(self.root).read_text, "read_text")("nope.txt")

        async def go(vm):
            return await vm.tools["read_text"].ainvoke({"path": "nope.txt"})
        self.assertEqual(self.run_vm(go), ref)
        self.assertTrue(ref.startswith("TOOL_ERROR [read_text]: FileNotFoundError"))

    # ---------- approval gate over elicitation ----------
    def test_fails_closed_without_approver(self):
        async def go(vm):
            with self.assertRaisesRegex(RuntimeError, "DENIED"):
                await vm.call("delete_path", path="d/a.tmp")
            out = await vm.tools["delete_path"].ainvoke({"path": "d/a.tmp"})
            self.assertIn("DENIED", out)
            self.assertNotIn("alternative", out)
        self.run_vm(go)
        self.assertTrue((self.root / "d" / "a.tmp").exists())

    def test_approved_delete_executes_and_is_logged(self):
        seen, events = [], []

        async def go(vm):
            vm.approver = lambda req: seen.append(req) or True
            vm.event_sink = lambda event, **data: events.append((event, data))
            self.assertEqual(await vm.call("delete_path", path="d/a.tmp"), "ok")
            self.assertTrue(vm.approval_log[0]["approved"])
        self.run_vm(go)
        self.assertFalse((self.root / "d" / "a.tmp").exists())
        self.assertEqual(seen[0]["tool"], "delete_path")
        self.assertIn("DELETE file d/a.tmp", seen[0]["summary"])
        self.assertEqual(events[0][0], "approval")

    def test_denied_and_crashing_approver_do_not_execute(self):
        def boom(req):
            raise RuntimeError("no tty")

        async def go(vm):
            for approver in (lambda req: False, boom):
                vm.approver = approver
                with self.assertRaisesRegex(RuntimeError, "DENIED"):
                    await vm.call("delete_path", path="d/a.tmp")
        self.run_vm(go)
        self.assertTrue((self.root / "d" / "a.tmp").exists())

    def test_guards_run_before_asking(self):
        async def go(vm):
            vm.approver = lambda req: True
            for bad in ("d/*.tmp", ".", "d", "../outside", "nope.txt"):
                with self.assertRaises(RuntimeError, msg=bad):
                    await vm.call("delete_path", path=bad)
            self.assertEqual(vm.approval_log, [])
        self.run_vm(go)

    def test_overwrite_is_gated_but_plain_move_is_not(self):
        async def go(vm):
            await vm.call("write_text", path="d/b.txt", content="b")
            await vm.call("move_path", src="d/b.txt", dst="d/c.txt")
            self.assertEqual(vm.approval_log, [])
            with self.assertRaisesRegex(RuntimeError, "DENIED"):
                await vm.call("move_path", src="d/c.txt", dst="d/keep.txt", overwrite=True)
        self.run_vm(go)
        self.assertEqual((self.root / "d" / "keep.txt").read_text(), "k")

    def test_replacing_preexisting_sheet_needs_approval(self):
        wb = openpyxl.Workbook()
        wb.active.append(["h"])
        wb.active.append([1])
        buf = io.BytesIO()
        wb.save(buf)
        b64 = base64.b64encode(buf.getvalue()).decode()
        seen = []

        async def go(vm):
            await vm.call("harness_put_file", path="t.xlsx", b64=b64)    # fixture = user data
            with self.assertRaisesRegex(RuntimeError, "DENIED"):
                await vm.call("xlsx_write", path="t.xlsx", rows=[["x"]], sheet="Sheet")
            vm.approver = lambda req: seen.append(req) or True
            await vm.call("xlsx_write", path="t.xlsx", rows=[["x"]], sheet="Sheet")
        self.run_vm(go)
        self.assertEqual(seen[0]["tool"], "xlsx_write")
        self.assertIn("REPLACE sheet 'Sheet'", seen[0]["summary"])


if __name__ == "__main__":
    unittest.main()