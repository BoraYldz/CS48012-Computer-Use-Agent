"""Tests for the local sandbox backend; no LLM or MCP server required."""
import asyncio
import base64
import tempfile
import unittest
from pathlib import Path

from agent.local_runtime import LocalVM, _safe_tool_callable, resolve_local_root
from tasks import load


class LocalRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "sandbox"
        self.vm = LocalVM(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_workspace_root_must_be_existing_absolute_dedicated_directory(self):
        workspace = Path(self.temp.name) / "AgentWorkspace"
        workspace.mkdir()
        resolved = resolve_local_root(workspace, "workspace", self.root)
        self.assertEqual(resolved, workspace.resolve())

        with self.assertRaisesRegex(ValueError, "absolute"):
            resolve_local_root("relative-workspace", "workspace", self.root)
        with self.assertRaisesRegex(ValueError, "already exist"):
            resolve_local_root(Path(self.temp.name) / "missing", "workspace", self.root)
        with self.assertRaisesRegex(ValueError, "broad workspace root"):
            resolve_local_root(Path.home(), "workspace", self.root)
        with self.assertRaisesRegex(ValueError, "AGENT_SCOPE"):
            resolve_local_root(workspace, "unrestricted", self.root)

    def test_workspace_backend_operates_inside_selected_real_directory(self):
        workspace = Path(self.temp.name) / "Desktop" / "AgentWorkspace"
        workspace.mkdir(parents=True)
        workspace_vm = LocalVM(resolve_local_root(workspace, "workspace", self.root))
        workspace_vm.make_dirs("Project/Input")
        workspace_vm.write_text("Project/README.txt", "created in selected workspace")

        self.assertTrue((workspace / "Project" / "Input").is_dir())
        self.assertEqual((workspace / "Project" / "README.txt").read_text(),
                         "created in selected workspace")
        with self.assertRaises(ValueError):
            workspace_vm.path_info("../../outside.txt")

    def test_paths_are_confined_to_sandbox(self):
        with self.assertRaises(ValueError):
            self.vm.path_info("../outside.txt")
        with self.assertRaises(ValueError):
            self.vm.path_info(str(self.root.parent / "outside.txt"))

    def test_symlink_escape_is_rejected(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        link = self.root / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable in this environment")
        with self.assertRaises(ValueError):
            self.vm.path_info("escape/secret.txt")

    def test_create_read_copy_and_move_file(self):
        self.vm.make_dirs("Desktop/Docs")
        self.vm.write_text("Desktop/Docs/note.txt", "local sandbox works")
        self.assertEqual(self.vm.read_text("Desktop/Docs/note.txt"), "local sandbox works")
        self.vm.copy_path("Desktop/Docs/note.txt", "Desktop/Docs/copy.txt")
        self.vm.move_path("Desktop/Docs/copy.txt", "Desktop/copy.txt")
        self.assertTrue(self.vm.path_info("Desktop/copy.txt")["exists"])
        self.assertFalse(self.vm.path_info("Desktop/Docs/copy.txt")["exists"])


    def test_structured_worker_tool_outputs_are_serialized_as_plain_json_text(self):
        safe_list = _safe_tool_callable(lambda: [{"path": "Incoming/a.txt", "type": "file"}], "list_dir")
        safe_dict = _safe_tool_callable(lambda: {"moved": ["a.txt"], "count": 1}, "move_files")
        import json
        self.assertEqual(json.loads(safe_list()), [{"path": "Incoming/a.txt", "type": "file"}])
        self.assertEqual(json.loads(safe_dict()), {"moved": ["a.txt"], "count": 1})
        self.assertIsInstance(safe_list(), str)
        self.assertIsInstance(safe_dict(), str)

    def test_list_dir_includes_content_hash_for_files(self):
        self.vm.make_dirs("Incoming")
        self.vm.write_text("Incoming/a.txt", "unchanged content")
        entry = self.vm.list_dir("Incoming")[0]
        self.assertEqual(entry["size"], len("unchanged content"))
        self.assertEqual(len(entry["sha256"]), 64)

    def test_worker_tool_errors_are_returned_as_recoverable_observations(self):
        def broken_tool(path):
            raise FileNotFoundError(f"source does not exist: {path}")

        safe = _safe_tool_callable(broken_tool, "move_path")
        result = safe(path="Incoming/*.txt")
        self.assertIn("TOOL_ERROR [move_path]", result)
        self.assertIn("FileNotFoundError", result)
        self.assertIn("operation was not completed", result)

    def test_move_path_rejects_wildcards_with_actionable_guidance(self):
        self.vm.make_dirs("Incoming")
        self.vm.write_text("Incoming/one.txt", "one")
        with self.assertRaisesRegex(ValueError, "list_dir"):
            self.vm.move_path("Incoming/*.txt", "Organized/Text/*.txt")

    def test_move_files_preserves_names_and_creates_destination_directory(self):
        self.vm.write_text("Incoming/a.txt", "alpha")
        self.vm.write_text("Incoming/b.txt", "bravo")
        result = self.vm.move_files("Incoming", ["a.txt", "b.txt"], "Organized/Text")
        self.assertEqual(result["moved"], ["a.txt", "b.txt"])
        self.assertTrue((self.root / "Organized/Text").is_dir())
        self.assertEqual((self.root / "Organized/Text/a.txt").read_text(), "alpha")
        self.assertEqual((self.root / "Organized/Text/b.txt").read_text(), "bravo")
        self.assertFalse((self.root / "Incoming/a.txt").exists())
        self.assertFalse((self.root / "Incoming/b.txt").exists())

    def test_move_files_rejects_wildcards_and_nested_paths(self):
        self.vm.write_text("Incoming/a.txt", "alpha")
        with self.assertRaisesRegex(ValueError, "exact basename"):
            self.vm.move_files("Incoming", ["*.txt"], "Organized/Text")
        with self.assertRaisesRegex(ValueError, "exact basename"):
            self.vm.move_files("Incoming", ["sub/a.txt"], "Organized/Text")
        self.assertTrue((self.root / "Incoming/a.txt").exists())

    def test_move_files_validates_all_destinations_before_moving(self):
        self.vm.write_text("Incoming/a.txt", "alpha")
        self.vm.write_text("Incoming/b.txt", "bravo")
        self.vm.write_text("Organized/Text/b.txt", "preexisting")
        with self.assertRaisesRegex(FileExistsError, "destination file already exists"):
            self.vm.move_files("Incoming", ["a.txt", "b.txt"], "Organized/Text")
        self.assertTrue((self.root / "Incoming/a.txt").exists())
        self.assertTrue((self.root / "Incoming/b.txt").exists())

    def test_one_shot_write_fault_corrupts_only_the_armed_path_once(self):
        self.vm.arm_write_fault("QuickFixProbe.txt", "INJECTED_FAILURE")

        self.vm.write_text("other.txt", "unaffected")
        self.assertEqual(self.vm.read_text("other.txt"), "unaffected")
        self.assertFalse(self.vm.fault_injection_triggered)

        self.vm.write_text("QuickFixProbe.txt", "EXPECTED")
        self.assertEqual(self.vm.read_text("QuickFixProbe.txt"), "EXPECTED")
        event = self.vm.apply_pending_write_fault()
        self.assertIsNotNone(event)
        self.assertEqual(self.vm.read_text("QuickFixProbe.txt"), "INJECTED_FAILURE")
        self.assertTrue(self.vm.fault_injection_triggered)

        # The fault is one-shot, so a subsequent repair persists normally.
        self.vm.write_text("QuickFixProbe.txt", "EXPECTED")
        self.assertEqual(self.vm.read_text("QuickFixProbe.txt"), "EXPECTED")
        self.assertIsNone(self.vm.apply_pending_write_fault())

    def test_quick_fix_task_setup_and_final_acceptance_check(self):
        task = load("quickfix")
        self.vm.write_text("keep_me.txt", "unrelated sandbox content")
        self.vm.write_text(task.OUTPUT_PATH, "stale diagnostic output")
        asyncio.run(task.setup(self.vm))
        self.assertEqual(self.vm.read_text("keep_me.txt"), "unrelated sandbox content")
        self.assertFalse(self.vm.path_info(task.OUTPUT_PATH)["exists"])

        # Simulate the first worker attempt; the harness corrupts it before checking.
        self.vm.write_text(task.OUTPUT_PATH, task.EXPECTED_TEXT)
        self.vm.apply_pending_write_fault()
        self.assertEqual(self.vm.read_text(task.OUTPUT_PATH), task.INJECTED_TEXT)

        # Simulate the Quick Fix worker's repair.
        self.vm.write_text(task.OUTPUT_PATH, task.EXPECTED_TEXT)
        passed, detail = asyncio.run(task.check(self.vm))
        self.assertTrue(passed, detail)

    def test_quick_fix_final_check_requires_exact_content(self):
        task = load("quickfix")
        self.vm.arm_write_fault(task.OUTPUT_PATH, task.INJECTED_TEXT)
        self.vm.write_text(task.OUTPUT_PATH, task.EXPECTED_TEXT)
        self.vm.apply_pending_write_fault()
        self.vm.write_text(task.OUTPUT_PATH, task.EXPECTED_TEXT + "\n")
        passed, detail = asyncio.run(task.check(self.vm))
        self.assertFalse(passed)
        self.assertIn("exact", detail)

    def test_quick_fix_run_validation_requires_one_llm_critic_repair(self):
        task = load("quickfix")
        valid = {
            "quick_fix_attempts": {"0": 1},
            "quick_fix_critic": {"0": True},
            "checks": {"0": {"status": "pass"}},
        }
        passed, detail = task.validate_run(valid)
        self.assertTrue(passed, detail)

        fallback_only = {
            **valid,
            "quick_fix_critic": {"0": False},
        }
        passed, detail = task.validate_run(fallback_only)
        self.assertFalse(passed)
        self.assertIn("fallback", detail)

    def test_harness_fixture_and_remove(self):
        payload = base64.b64encode(b"test fixture").decode("ascii")
        self.vm.harness_put_file("Desktop/fixture.txt", payload)
        self.assertEqual(
            base64.b64decode(self.vm.harness_get_file("Desktop/fixture.txt")),
            b"test fixture",
        )
        self.vm.harness_remove_path("Desktop/fixture.txt")
        self.assertFalse(self.vm.path_info("Desktop/fixture.txt")["exists"])
        with self.assertRaises(ValueError):
            self.vm.harness_remove_path(".")

    def test_xlsx_read_write_round_trip(self):
        self.vm.xlsx_write(
            "Desktop/data.xlsx",
            [["Name", "Score"], ["A", 10], ["B", 20]],
            sheet="Results",
        )
        result = self.vm.xlsx_read("Desktop/data.xlsx", sheet="Results")
        self.assertEqual(result["rows"][1], ["A", 10])
        self.assertEqual(result["sheets"], ["Results"])
        self.assertFalse(result["truncated"])

    def test_task16_fixture_setup_and_deterministic_check(self):
        task = load("16")
        asyncio.run(task.setup(self.vm))

        # A clean task must fail before the worker creates the requested folders.
        passed, _ = asyncio.run(task.check(self.vm))
        self.assertFalse(passed)

        for name in ("Girdiler", "Çıktılar", "Raporlar"):
            self.vm.make_dirs(f"Desktop/Proje/{name}")

        passed, detail = asyncio.run(task.check(self.vm))
        self.assertTrue(passed, detail)

    def test_task16_rejects_misplaced_root_folder(self):
        task = load("16")
        asyncio.run(task.setup(self.vm))
        self.vm.make_dirs("Proje/Girdiler")
        self.vm.make_dirs("Proje/Çıktılar")
        self.vm.make_dirs("Proje/Raporlar")

        passed, detail = asyncio.run(task.check(self.vm))

        self.assertFalse(passed)
        self.assertIn("expected Desktop/Proje", detail)

    def test_task16_setup_removes_stale_output_only(self):
        self.vm.make_dirs("Desktop/Proje/stale")
        self.vm.make_dirs("Proje/wrong_location")
        self.vm.write_text("Desktop/keep.txt", "keep me")
        task = load("16")
        asyncio.run(task.setup(self.vm))

        self.assertFalse(self.vm.path_info("Desktop/Proje/stale")["exists"])
        self.assertFalse(self.vm.path_info("Proje/wrong_location")["exists"])
        self.assertEqual(self.vm.read_text("Desktop/keep.txt"), "keep me")


if __name__ == "__main__":
    unittest.main()
