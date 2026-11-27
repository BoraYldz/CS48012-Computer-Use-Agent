"""Snapshot / rollback harness tools: restore, verification, limits, approval, MCP parity. No LLM."""
import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import rollback
from agent.local_runtime import LocalVM
from agent.mcp_local import McpLocalBackend
from agent.runtime import WORKER_TOOLS

NAMES = {"harness_snapshot", "harness_rollback", "harness_discard_snapshot"}
COUNTS = {"added": 5, "changed": 2, "missing": 2}


def tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() if p.is_file() else None
            for p in root.rglob("*")}


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / "ws"
        (self.root / "d" / "empty").mkdir(parents=True)
        (self.root / "d" / "a.txt").write_text("a")
        (self.root / "d" / "b.txt").write_text("b")
        (self.root / "swap").write_text("file")
        self.vm = LocalVM(self.root)
        self.before = tree(self.root)

    def tearDown(self):
        self.vm.harness_discard_snapshot()
        self.temp.cleanup()

    def mutate(self):
        (self.root / "d" / "a.txt").write_text("changed")
        (self.root / "d" / "b.txt").rename(self.root / "d" / "moved.txt")
        (self.root / "d" / "empty").rmdir()
        (self.root / "new" / "deeper").mkdir(parents=True)
        (self.root / "new" / "deeper" / "n.txt").write_text("n")
        (self.root / "swap").unlink()
        (self.root / "swap").mkdir()
        (self.root / "swap" / "x").write_text("x")

    def snapshot_and_mutate(self) -> dict:
        self.assertTrue(self.vm.harness_snapshot()["ok"])
        self.mutate()
        return tree(self.root)

    def link(self):
        try:
            os.symlink(self.temp.name, self.root / "link", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")

    # ---------- restore ----------
    def test_restores_every_difference_and_can_repeat(self):
        self.assertTrue(self.vm.harness_snapshot()["ok"])
        for _ in range(2):
            self.mutate()
            self.assertEqual(self.vm.harness_rollback(require_approval=False), {"ok": True, **COUNTS})
            self.assertEqual(tree(self.root), self.before)

    def test_read_only_files(self):
        (self.root / "d" / "b.txt").chmod(stat.S_IREAD)
        self.vm.harness_snapshot()
        extra = self.root / "extra.txt"
        extra.write_text("e")
        extra.chmod(stat.S_IREAD)
        self.assertTrue(self.vm.harness_rollback(require_approval=False)["ok"])
        self.assertEqual(tree(self.root), self.before)
        self.assertTrue(self.vm.harness_discard_snapshot()["ok"])

    def test_sheet_ownership_is_restored(self):
        self.vm.harness_snapshot()
        self.vm.xlsx_write("t.xlsx", [["a"], [1]])
        self.assertTrue(self.vm._owned_sheets)
        self.assertTrue(self.vm.harness_rollback(require_approval=False)["ok"])
        self.assertEqual(self.vm._owned_sheets, set())

    def test_copy_is_outside_root_and_discarded(self):
        self.vm.harness_snapshot()
        store = self.vm._snapshot[0].store
        self.assertFalse(store.is_relative_to(self.root))
        self.assertTrue(store.exists())
        self.assertEqual(self.vm.harness_discard_snapshot(), {"ok": True, "discarded": True})
        self.assertFalse(store.exists())
        self.assertEqual(self.vm.harness_discard_snapshot(), {"ok": True, "discarded": False})
        self.assertEqual(self.vm.harness_rollback()["reason"], "no snapshot")

    # ---------- verification ----------
    def test_corrupt_copy_leaves_root_untouched(self):
        mutated = self.snapshot_and_mutate()
        (self.vm._snapshot[0].store / "d" / "a.txt").write_text("bad")
        result = self.vm.harness_rollback(require_approval=False)
        self.assertFalse(result["ok"])
        self.assertIn("corrupt", result["reason"])
        self.assertEqual(tree(self.root), mutated)

    def test_mismatch_after_restore_fails(self):
        self.snapshot_and_mutate()
        with mock.patch.object(rollback.shutil, "copy2", lambda src, dst: Path(dst).write_text("wrong")):
            result = self.vm.harness_rollback(require_approval=False)
        self.assertFalse(result["ok"])
        self.assertIn("mismatch", result["reason"])

    # ---------- limits ----------
    def test_limits_disable_snapshot(self):
        for name, value in (("MAX_FILES", 2), ("MAX_BYTES", 3)):
            with mock.patch.object(rollback, name, value):
                self.assertFalse(self.vm.harness_snapshot()["ok"], name)
            self.assertIsNone(self.vm._snapshot)

    def test_link_disables_snapshot(self):
        self.link()
        result = self.vm.harness_snapshot()
        self.assertFalse(result["ok"])
        self.assertIn("symlink", result["reason"])

    def test_link_added_later_fails_rollback(self):
        self.vm.harness_snapshot()
        self.link()
        self.assertFalse(self.vm.harness_rollback(require_approval=False)["ok"])
        self.assertTrue((self.root / "link").is_symlink())

    # ---------- approval ----------
    def test_unchanged_root_asks_nothing(self):
        self.vm.harness_snapshot()
        self.assertTrue(self.vm.harness_rollback()["ok"])
        self.assertEqual(self.vm.approval_log, [])

    def test_fails_closed_without_approver(self):
        mutated = self.snapshot_and_mutate()
        self.assertEqual(self.vm.harness_rollback(), {"ok": False, "reason": "denied", **COUNTS})
        self.assertEqual(tree(self.root), mutated)

    def test_one_approval_with_counts_and_paths(self):
        self.snapshot_and_mutate()
        seen = []
        self.vm.approver = lambda req: seen.append(req) or True
        self.assertTrue(self.vm.harness_rollback()["ok"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["tool"], "rollback")
        self.assertEqual(seen[0]["args"], COUNTS)
        self.assertIn("remove 5", seen[0]["summary"])
        self.assertIn("new/deeper/n.txt", seen[0]["summary"])

    def test_change_during_approval_aborts(self):
        self.snapshot_and_mutate()
        late = self.root / "late.txt"
        self.vm.approver = lambda req: late.write_text("x") > 0
        result = self.vm.harness_rollback()
        self.assertFalse(result["ok"])
        self.assertIn("changed after", result["reason"])
        self.assertTrue(late.exists())
        self.assertTrue((self.root / "new").exists())

    # ---------- surface ----------
    def test_never_exposed_to_workers(self):
        for names in WORKER_TOOLS.values():
            self.assertFalse(NAMES & set(names))

    def test_mcp_matches_direct(self):
        direct = self.vm.harness_snapshot()
        self.vm.harness_discard_snapshot()

        async def go():
            async with McpLocalBackend(self.root) as vm:
                snap = await vm.call("harness_snapshot")
                self.mutate()
                denied = await vm.call("harness_rollback")
                seen = []
                vm.approver = lambda req: seen.append(req) or True
                done = await vm.call("harness_rollback")
                gone = await vm.call("harness_discard_snapshot")
                return snap, denied, done, gone, seen

        snap, denied, done, gone, seen = asyncio.run(go())
        self.assertEqual(snap, direct)
        self.assertEqual(denied, {"ok": False, "reason": "denied", **COUNTS})
        self.assertEqual(done, {"ok": True, **COUNTS})
        self.assertEqual(gone, {"ok": True, "discarded": True})
        self.assertEqual([r["tool"] for r in seen], ["rollback"])
        self.assertEqual(tree(self.root), self.before)


if __name__ == "__main__":
    unittest.main()