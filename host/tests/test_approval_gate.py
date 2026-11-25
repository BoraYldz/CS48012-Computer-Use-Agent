"""Human-approval gate for high-blast-radius tools (delete_path, overwrite)."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from agent.checkers import check_spec
from agent.contracts import delete_allowed, snapshot_deletion_contracts
from agent.local_runtime import LocalVM, _safe_tool_callable
from agent.runtime import WORKER_TOOLS


class ApprovalGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "ws"
        self.vm = LocalVM(self.root)
        (self.root / "d").mkdir()
        (self.root / "d" / "a.tmp").write_text("a")
        (self.root / "d" / "keep.txt").write_text("k")
        self.events = []
        self.vm.event_sink = lambda event, **data: self.events.append((event, data))

    def tearDown(self):
        self.temp.cleanup()

    def test_delete_is_in_files_worker_only(self):
        self.assertIn("delete_path", WORKER_TOOLS["files"])
        self.assertNotIn("delete_path", WORKER_TOOLS["sheet"])

    def test_fails_closed_without_approver(self):
        with self.assertRaisesRegex(PermissionError, "DENIED"):
            self.vm.delete_path("d/a.tmp")
        self.assertTrue((self.root / "d" / "a.tmp").exists())

    def test_approved_delete_executes_and_is_logged(self):
        seen = []
        self.vm.approver = lambda req: seen.append(req) or True
        self.assertEqual(self.vm.delete_path("d/a.tmp"), "ok")
        self.assertFalse((self.root / "d" / "a.tmp").exists())
        self.assertIn("DELETE file d/a.tmp", seen[0]["summary"])
        self.assertTrue(self.vm.approval_log[0]["approved"])
        self.assertEqual(self.events[0][0], "approval")

    def test_denied_delete_does_not_execute(self):
        self.vm.approver = lambda req: False
        with self.assertRaises(PermissionError):
            self.vm.delete_path("d/a.tmp")
        self.assertTrue((self.root / "d" / "a.tmp").exists())
        self.assertFalse(self.vm.approval_log[0]["approved"])

    def test_approver_exception_counts_as_denial(self):
        def boom(req):
            raise RuntimeError("no tty")
        self.vm.approver = boom
        with self.assertRaises(PermissionError):
            self.vm.delete_path("d/a.tmp")
        self.assertTrue((self.root / "d" / "a.tmp").exists())

    def test_guards_run_before_asking(self):
        self.vm.approver = lambda req: True
        for bad in ("d/*.tmp", ".", "d", "../outside", "nope.txt"):
            with self.subTest(path=bad):
                with self.assertRaises((ValueError, OSError)):
                    self.vm.delete_path(bad)
        self.assertEqual(self.vm.approval_log, [])  # nothing reached the human

    def test_folder_needs_recursive_and_shows_blast_radius(self):
        seen = []
        self.vm.approver = lambda req: seen.append(req) or True
        self.vm.delete_path("d", recursive=True)
        self.assertIn("2 item(s)", seen[0]["summary"])
        self.assertFalse((self.root / "d").exists())

    def test_overwrite_is_gated_but_plain_move_is_not(self):
        (self.root / "d" / "b.txt").write_text("b")
        self.vm.move_path("d/b.txt", "d/c.txt")                       # no approval needed
        self.assertEqual(self.vm.approval_log, [])
        with self.assertRaises(PermissionError):                       # overwrite blocked
            self.vm.move_path("d/c.txt", "d/keep.txt", overwrite=True)
        self.assertEqual((self.root / "d" / "keep.txt").read_text(), "k")

    def test_tool_wrapper_returns_final_denial_without_inviting_workarounds(self):
        wrapped = _safe_tool_callable(self.vm.delete_path, "delete_path")
        out = wrapped("d/a.tmp")
        self.assertIn("DENIED", out)
        self.assertNotIn("alternative", out)

    def test_no_files_by_extension_checker(self):
        spec = {"checks": [{"kind": "no_files_by_extension", "path": "d", "extension": ".tmp"}]}
        res = asyncio.run(check_spec(self.vm, spec, capability="files"))
        self.assertEqual(res["status"], "fail")
        self.assertIn("a.tmp", res["evidence"])
        self.vm.approver = lambda req: True
        self.vm.delete_path("d/a.tmp")
        res = asyncio.run(check_spec(self.vm, spec, capability="files"))
        self.assertEqual(res["status"], "pass")
        bad = {"checks": [{"kind": "no_files_by_extension", "path": "d", "extension": "tmp"}]}
        self.assertEqual(asyncio.run(check_spec(self.vm, bad, capability="files"))["status"], "unknown")


    def test_collateral_damage_is_detected_even_if_the_tmp_files_are_gone(self):
        """Smoke-test t09: 'no .tmp remain' passed although keep.txt was deleted."""
        spec = {"checks": [{"kind": "no_files_by_extension", "path": "d", "extension": ".tmp"}]}
        asyncio.run(snapshot_deletion_contracts(self.vm, [{"id": 0, "check_spec": spec}]))
        self.assertEqual([f["path"] for f in spec["checks"][0]["preserved_files"]], ["d/keep.txt"])
        self.vm.approver = lambda req: True
        self.vm.delete_path("d/a.tmp")
        self.assertEqual(asyncio.run(check_spec(self.vm, spec, capability="files"))["status"], "pass")
        self.vm.delete_path("d/keep.txt")
        res = asyncio.run(check_spec(self.vm, spec, capability="files"))
        self.assertEqual(res["status"], "fail")
        self.assertIn("COLLATERAL DAMAGE", res["evidence"])

    def test_modified_survivor_is_detected(self):
        spec = {"checks": [{"kind": "no_files_by_extension", "path": "d", "extension": ".tmp"}]}
        asyncio.run(snapshot_deletion_contracts(self.vm, [{"id": 0, "check_spec": spec}]))
        (self.root / "d" / "keep.txt").write_text("changed")
        self.vm.approver = lambda req: True
        self.vm.delete_path("d/a.tmp")
        self.assertIn("modified", asyncio.run(check_spec(self.vm, spec, capability="files"))["evidence"])

    def test_delete_scope(self):
        c = [{"kind": "no_files_by_extension", "path": "Work", "extension": ".tmp"}]
        self.assertTrue(delete_allowed(c, "Work/a.tmp"))
        self.assertTrue(delete_allowed(c, "./Work/A.TMP"))
        for bad in ("Work/keep.txt", "Work/sub/a.tmp", "Other/a.tmp", "Work/../Other/a.tmp", "Work"):
            self.assertFalse(delete_allowed(c, bad), bad)
        self.assertFalse(delete_allowed(c, "Work/a.tmp", recursive=True))
        rec = [{**c[0], "recursive": True}]
        self.assertTrue(delete_allowed(rec, "Work/sub/a.tmp"))


if __name__ == "__main__":
    unittest.main()
