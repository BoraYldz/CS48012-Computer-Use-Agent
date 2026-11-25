"""Unit tests for the generic Files-worker guards (no LLM, no task-specific rules)."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.worker_guard import (LoopDetected, LoopGuard, deletion_intended, drop_in_place_move_checks,
                                drop_unrequested_empty_checks, policy_blocked, prune_files_assertions,
                                select_worker_tools, wrap_untrusted)

JUNK = {"text": "x", "sheet": "x", "values": [123.5, 456.7], "rows": [[1.0]], "cell": "B7", "value": 123.5,
        "tolerance": 0.005, "contains": "SUM(B2:C2)", "count": 5, "column": "Revenue", "group_by": "Region",
        "op": "sum", "source_sheet": "optional", "preserved_sheets": None}


def sg(cap="files", checks=None, sid=0):
    return {"id": sid, "capability": cap, "instruction": "i", "expected_output": "o",
            "check_spec": {"checks": checks or [], "rationale": ""}}


class PruneTests(unittest.TestCase):
    def test_t02_style_junk_is_removed_from_path_exists(self):
        a = {"kind": "path_exists", "path": "Docs/report.txt", "is_dir": False,
             "source_path": "Docs/report.txt", "source_dir": "Docs", "snapshot_error": "bad", **JUNK}
        subs = [sg(checks=[a])]
        changes = prune_files_assertions(subs)
        self.assertEqual(subs[0]["check_spec"]["checks"][0],
                         {"kind": "path_exists", "path": "Docs/report.txt", "is_dir": False})
        self.assertTrue(changes)

    def test_orchestrator_fields_are_reset_not_trusted(self):
        a = {"kind": "files_moved_by_extension", "source_dir": "A", "destination_dir": "B",
             "extension": ".txt", "expected_files": [{"name": "fake.txt"}], "snapshot_error": "x"}
        subs = [sg(checks=[a])]
        prune_files_assertions(subs)
        out = subs[0]["check_spec"]["checks"][0]
        self.assertIsNone(out["expected_files"])
        self.assertIsNone(out["snapshot_error"])
        self.assertEqual(out["extension"], ".txt")

    def test_sheet_subgoals_are_never_touched(self):
        a = {"kind": "path_exists", "path": "x.xlsx", **JUNK}
        subs = [sg(cap="sheet", checks=[copy.deepcopy(a)])]
        self.assertEqual(prune_files_assertions(subs), [])
        self.assertEqual(subs[0]["check_spec"]["checks"][0], a)

    def test_unknown_kind_left_alone(self):
        a = {"kind": "xlsx_cell_equals", "path": "x.xlsx", "cell": "B2", "value": 1}
        subs = [sg(checks=[copy.deepcopy(a)])]
        prune_files_assertions(subs)
        self.assertEqual(subs[0]["check_spec"]["checks"][0], a)


class ProvenanceTests(unittest.TestCase):
    def test_directory_empty_dropped_when_user_never_mentioned_empty(self):
        subs = [sg(checks=[{"kind": "directory_empty", "path": "_eval/x/Incoming"},
                           {"kind": "path_exists", "path": "_eval/x/Text"}])]
        ch = drop_unrequested_empty_checks("Move all .txt files from _eval/x/Incoming into _eval/x/Text.", subs)
        self.assertEqual(len(ch), 1)
        self.assertEqual([a["kind"] for a in subs[0]["check_spec"]["checks"]], ["path_exists"])

    def test_directory_empty_kept_when_requested_for_that_folder(self):
        subs = [sg(checks=[{"kind": "directory_empty", "path": "_eval/x/Incoming"}])]
        goal = "Sort files in _eval/x/Incoming into Organized. Leave the Incoming folder empty."
        self.assertEqual(drop_unrequested_empty_checks(goal, subs), [])
        self.assertEqual(len(subs[0]["check_spec"]["checks"]), 1)

    def test_directory_empty_dropped_when_empty_refers_to_another_folder(self):
        subs = [sg(checks=[{"kind": "directory_empty", "path": "Reports"}])]
        self.assertEqual(len(drop_unrequested_empty_checks("Rename files in Reports; keep Trash empty", subs)), 1)

    def test_empty_phrase_variants(self):
        from agent.worker_guard import _requires_empty
        for goal in ("Leave the Incoming folder empty.", "Incoming must be empty afterwards", "Empty the Incoming folder"):
            self.assertTrue(_requires_empty(goal, "Incoming"), goal)
        self.assertFalse(_requires_empty("Move files from Incoming to Text", "Incoming"))

    def test_in_place_move_contract_dropped(self):
        subs = [sg(checks=[{"kind": "files_moved_by_extension", "source_dir": "R", "destination_dir": "R/",
                            "extension": ".txt"},
                           {"kind": "files_moved_by_extension", "source_dir": "A", "destination_dir": "B",
                            "extension": ".txt"}])]
        self.assertEqual(len(drop_in_place_move_checks(subs)), 1)
        self.assertEqual(subs[0]["check_spec"]["checks"][0]["source_dir"], "A")


class LeastPrivilegeTests(unittest.TestCase):
    TOOLS = [SimpleNamespace(name=n) for n in ("list_dir", "move_files", "delete_path", "read_text")]

    def names(self, tools):
        return [t.name for t in tools]

    def test_delete_hidden_without_intent(self):
        tools, hidden = select_worker_tools(self.TOOLS, "Read notes.txt and write a summary", "Summarize", [])
        self.assertTrue(hidden)
        self.assertNotIn("delete_path", self.names(tools))

    def test_delete_kept_when_goal_asks(self):
        tools, hidden = select_worker_tools(self.TOOLS, "Delete all .tmp files in Work", "x", [])
        self.assertFalse(hidden)
        self.assertIn("delete_path", self.names(tools))

    def test_delete_kept_with_contract(self):
        tools, hidden = select_worker_tools(self.TOOLS, "tidy Work", "x", [{"kind": "no_files_by_extension"}])
        self.assertIn("delete_path", self.names(tools))

    def test_intent_words(self):
        self.assertTrue(deletion_intended("Please remove the temp folder", ""))
        self.assertFalse(deletion_intended("Move report.txt into Archive", "Move the file"))


class BreakerTests(unittest.TestCase):
    def test_third_identical_failure_raises(self):
        g = LoopGuard(limit=3)
        for _ in range(2):
            g.observe("move_path", {"src": "a", "dst": "../b"}, "TOOL_ERROR [move_path]: outside")
        with self.assertRaises(LoopDetected):
            g.observe("move_path", {"dst": "../b", "src": "a"}, "TOOL_ERROR [move_path]: outside")

    def test_successes_and_varied_calls_do_not_trip(self):
        g = LoopGuard(limit=3)
        for _ in range(10):
            g.observe("list_dir", {"path": "a"}, '[{"path": "a/x"}]')
        for i in range(10):
            g.observe("move_path", {"src": f"f{i}"}, "TOOL_ERROR [move_path]: nope")

    def test_policy_blocked_is_per_subgoal(self):
        hist = [{"subgoal_id": 0, "observation": "TOOL_ERROR: ValueError: path outside configured allowed root: /x"},
                {"subgoal_id": 1, "observation": "ok"}]
        self.assertTrue(policy_blocked(hist, 0))
        self.assertFalse(policy_blocked(hist, 1))

    def test_wrap_untrusted_marks_boundaries(self):
        out = wrap_untrusted("hello")
        self.assertIn("untrusted", out.splitlines()[0])
        self.assertIn("hello", out)


class CopyFilesTests(unittest.TestCase):
    def setUp(self):
        from agent.local_runtime import LocalVM
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.vm = LocalVM(self.root)
        (self.root / "Docs").mkdir()
        (self.root / "Docs" / "a.txt").write_text("A")
        (self.root / "Docs" / "b.csv").write_text("B")

    def tearDown(self):
        self.tmp.cleanup()

    def test_copy_keeps_originals_and_content(self):
        r = self.vm.copy_files("Docs", ["a.txt", "b.csv"], "Backup")
        self.assertEqual(r["count"], 2)
        self.assertEqual((self.root / "Docs" / "a.txt").read_text(), "A")
        self.assertEqual((self.root / "Backup" / "b.csv").read_text(), "B")

    def test_refuses_overwrite_and_is_atomic(self):
        (self.root / "Backup").mkdir()
        (self.root / "Backup" / "b.csv").write_text("OLD")
        with self.assertRaises(FileExistsError):
            self.vm.copy_files("Docs", ["a.txt", "b.csv"], "Backup")
        self.assertFalse((self.root / "Backup" / "a.txt").exists())   # nothing copied
        self.assertEqual((self.root / "Backup" / "b.csv").read_text(), "OLD")

    def test_rejects_paths_wildcards_and_escape(self):
        for bad in (["../a.txt"], ["*.txt"], ["sub/a.txt"], []):
            with self.assertRaises((ValueError, FileNotFoundError)):
                self.vm.copy_files("Docs", bad, "Backup")
        with self.assertRaises(ValueError):
            self.vm.copy_files("Docs", ["a.txt"], "../outside")

    def test_registered_for_files_worker_only(self):
        try:
            from agent.runtime import WORKER_TOOLS
        except ImportError:
            self.skipTest("needs host dependencies")
        self.assertIn("copy_files", WORKER_TOOLS["files"])
        self.assertNotIn("copy_files", WORKER_TOOLS["sheet"])


try:
    from agent.graph import _WorkerTraceHandler
    _HAVE_GRAPH = True
except Exception:                                  # langchain not installed in this environment
    _HAVE_GRAPH = False


@unittest.skipUnless(_HAVE_GRAPH, "needs host dependencies")
class TraceHandlerGuardTests(unittest.TestCase):
    class _Log:
        def __init__(self):
            self.records = []

        def write(self, event, **data):
            self.records.append((event, data))

    def test_nested_wrapper_call_is_traced_once(self):
        h = _WorkerTraceHandler(self._Log(), 0, "files")
        h.on_tool_start({"name": "delete_path"}, "{}", run_id="outer", inputs={"path": "a.tmp"})
        h.on_tool_start({"name": "delete_path"}, "{}", run_id="inner", parent_run_id="outer", inputs={"path": "a.tmp"})
        h.on_tool_end("ok", run_id="inner", parent_run_id="outer")
        h.on_tool_end("ok", run_id="outer")
        self.assertEqual(len(h.steps), 1)

    def test_repeated_failure_aborts_worker(self):
        h = _WorkerTraceHandler(self._Log(), 0, "files")
        with self.assertRaises(LoopDetected):
            for i in range(3):
                h.on_tool_start({"name": "move_path"}, "{}", run_id=f"r{i}", inputs={"src": "a"})
                h.on_tool_end("TOOL_ERROR [move_path]: nope", run_id=f"r{i}")

    def test_read_text_observation_keeps_2000_chars(self):
        h = _WorkerTraceHandler(self._Log(), 0, "files")
        h.on_tool_start({"name": "read_text"}, "{}", run_id="r", inputs={"path": "n.txt"})
        h.on_tool_end("x" * 3000, run_id="r")
        self.assertEqual(len(h.steps[0]["observation"]), 2000)


if __name__ == "__main__":
    unittest.main()
