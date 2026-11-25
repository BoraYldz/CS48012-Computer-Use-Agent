"""Validate the eval tasks themselves (no LLM): the ground-truth check must accept the
reference solution, reject a do-nothing baseline where work is required, and reject
destructive/incorrect end states. A bad check would silently corrupt every pass^k number."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from agent.local_runtime import LocalVM
from tasks import files_suite as fs


def run(coro):
    return asyncio.run(coro)


class FilesSuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vm = LocalVM(Path(self.temp.name) / "AgentWorkspace")

    def tearDown(self):
        self.temp.cleanup()

    async def _prepare(self, task):
        base = await fs.reset(self.vm, task)
        self.vm.approval_log.clear()
        self.vm.approver = lambda req, _a=task.approve: _a
        return base

    def _ctx(self):
        return {"approvals": list(self.vm.approval_log)}

    def test_task_ids_unique_and_requests_reference_their_base(self):
        tasks = fs.all_tasks()
        self.assertEqual(len({t.id for t in tasks}), len(tasks))
        for t in tasks:
            self.assertIn(fs.base_of(t), t.request(fs.base_of(t)))

    def test_reference_solution_passes_every_task(self):
        for task in fs.all_tasks():
            with self.subTest(task=task.id):
                async def go():
                    base = await self._prepare(task)
                    await task.solve(self.vm, base)
                    return await task.check(self.vm, base, self._ctx())
                ok, detail = run(go())
                self.assertTrue(ok, detail)

    def test_do_nothing_fails_every_task_that_requires_work(self):
        for task in fs.all_tasks():
            async def go():
                base = await self._prepare(task)
                return await task.check(self.vm, base, self._ctx())
            ok, detail = run(go())
            with self.subTest(task=task.id):
                self.assertEqual(ok, not task.needs_change, f"{task.id}: {detail}")

    def test_reset_wipes_only_the_task_folder(self):
        async def go():
            await fs.put(self.vm, "keep/me.txt", "x")
            task = fs.select(only="t01")[0]
            await fs.reset(self.vm, task)
            await fs.put(self.vm, f"{fs.base_of(task)}/junk.txt", "junk")
            await fs.reset(self.vm, task)
            return await fs.tree(self.vm, "keep"), await fs.tree(self.vm, fs.base_of(task))
        keep, task_tree = run(go())
        self.assertEqual(list(keep), ["me.txt"])
        self.assertNotIn("junk.txt", task_tree)

    def test_collision_check_detects_data_loss(self):
        task = fs.select(only="t06")[0]
        async def go():
            base = await self._prepare(task)
            self.vm.move_path(f"{base}/Incoming/report.txt", f"{base}/Archive/report.txt", overwrite=True)
            return await task.check(self.vm, base, self._ctx())
        # overwrite is approval-gated: with the task's deny policy the move itself is blocked
        with self.assertRaises(PermissionError):
            run(go())
        # ...and an approved overwrite is detected as data loss by the ground-truth check
        async def go2():
            base = await self._prepare(task)
            self.vm.approver = lambda req: True
            self.vm.move_path(f"{base}/Incoming/report.txt", f"{base}/Archive/report.txt", overwrite=True)
            return await task.check(self.vm, base, self._ctx())
        ok, detail = run(go2())
        self.assertFalse(ok)
        self.assertIn("DATA LOSS", detail)

    def test_delete_check_rejects_unapproved_or_excessive_deletion(self):
        task = fs.select(only="t09")[0]
        async def over_delete():
            base = await self._prepare(task)
            for n in ("a.tmp", "b.tmp", "keep.txt"):
                self.vm.delete_path(f"{base}/Work/{n}")
            return await task.check(self.vm, base, self._ctx())
        ok, detail = run(over_delete())
        self.assertFalse(ok)
        self.assertIn("unexpected" if "unexpected" in detail else "missing", detail)

    def test_injection_check_flags_destructive_attempt(self):
        task = fs.select(only="t11")[0]
        async def go():
            base = await self._prepare(task)
            await task.solve(self.vm, base)
            try:
                self.vm.delete_path(f"{base}/Inbox/other.txt")   # denied by policy, but attempted
            except PermissionError:
                pass
            return await task.check(self.vm, base, self._ctx())
        ok, detail = run(go())
        self.assertFalse(ok)
        self.assertIn("destructive", detail)

    def test_escape_check_detects_outside_write(self):
        task = fs.select(only="t12")[0]
        async def go():
            base = await self._prepare(task)
            outside = self.vm.root.parent / "eval_escape_probe"
            outside.mkdir()
            return await task.check(self.vm, base, self._ctx())
        ok, detail = run(go())
        self.assertFalse(ok)
        self.assertIn("ESCAPE", detail)

    def test_traversal_is_refused_by_the_backend(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            self.vm.move_path("_eval/x.txt", "../eval_escape_probe/x.txt")


if __name__ == "__main__":
    unittest.main()
