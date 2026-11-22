"""file_ops: state-grounded move / copy / rename contract (no LLM)."""
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from agent.checkers import check_spec
from agent.file_ops import check_file_ops, describe_ops, expand_file_ops, snapshot_file_ops
from agent.local_runtime import LocalVM
from agent.worker_guard import prune_files_assertions


def run(coro):
    return asyncio.run(coro)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.vm = LocalVM(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, rel, text="x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def exists(self, rel):
        return (self.root / rel).exists()

    def assertion(self, **kw):
        return {"kind": "file_ops", **kw}

    def verdict(self, a):
        return run(check_file_ops(self.vm, a))


class RenameTests(Base):
    """t03-style: rename in place; previously UNKNOWN because no check could express it."""

    def setUp(self):
        super().setUp()
        self.put("Reports/q1.txt", "one")
        self.put("Reports/q2.txt", "two")
        self.put("Reports/readme.md", "# r")
        self.a = self.assertion(op="rename", source_dir="Reports", extension=".txt", rename_prefix="2026_")

    def test_expand_builds_exact_ops_from_real_files(self):
        ev = run(expand_file_ops(self.vm, self.a))
        self.assertEqual(ev["count"], 2)
        self.assertEqual([o["dst"] for o in self.a["expected_ops"]], ["Reports/2026_q1.txt", "Reports/2026_q2.txt"])
        self.assertEqual([p["path"] for p in self.a["preserved_files"]], ["Reports/readme.md"])

    def test_pass_when_renamed_correctly(self):
        run(expand_file_ops(self.vm, self.a))
        for n in ("q1", "q2"):
            (self.root / f"Reports/{n}.txt").rename(self.root / f"Reports/2026_{n}.txt")
        self.assertEqual(self.verdict(self.a)[0], "pass")

    def test_fail_when_nothing_done(self):
        run(expand_file_ops(self.vm, self.a))
        status, detail = self.verdict(self.a)
        self.assertEqual(status, "fail")
        self.assertIn("missing", detail)

    def test_fail_when_other_file_touched(self):
        run(expand_file_ops(self.vm, self.a))
        for n in ("q1", "q2"):
            (self.root / f"Reports/{n}.txt").rename(self.root / f"Reports/2026_{n}.txt")
        (self.root / "Reports/readme.md").unlink()
        status, detail = self.verdict(self.a)
        self.assertEqual(status, "fail")
        self.assertIn("COLLATERAL", detail)

    def test_suffix_goes_before_extension(self):
        a = self.assertion(op="rename", source_dir="Reports", extension=".txt", rename_suffix="_v2")
        run(expand_file_ops(self.vm, a))
        self.assertEqual(a["expected_ops"][0]["dst"], "Reports/q1_v2.txt")


class RecursiveCollectTests(Base):
    """t04-style: collect nested files flat; AllCSV lives INSIDE the source tree."""

    def setUp(self):
        super().setUp()
        self.put("Mixed/a.csv", "1")
        self.put("Mixed/sub1/b.csv", "2")
        self.put("Mixed/sub1/deep/c.csv", "3")
        self.put("Mixed/sub2/d.txt", "t")
        self.a = self.assertion(op="move", source_dir="Mixed", destination_dir="Mixed/AllCSV",
                                extension=".csv", recursive=True)

    def test_expansion_sees_nested_files(self):
        run(expand_file_ops(self.vm, self.a))
        self.assertEqual(sorted(o["src"] for o in self.a["expected_ops"]),
                         ["Mixed/a.csv", "Mixed/sub1/b.csv", "Mixed/sub1/deep/c.csv"])

    def test_correct_flat_move_passes(self):
        run(expand_file_ops(self.vm, self.a))
        (self.root / "Mixed/AllCSV").mkdir()
        for rel in ("a.csv", "sub1/b.csv", "sub1/deep/c.csv"):
            shutil.move(str(self.root / "Mixed" / rel), str(self.root / "Mixed/AllCSV" / Path(rel).name))
        self.assertEqual(self.verdict(self.a)[0], "pass")

    def test_moving_whole_folders_is_a_FAIL_not_a_false_success(self):
        run(expand_file_ops(self.vm, self.a))
        (self.root / "Mixed/AllCSV").mkdir()
        shutil.move(str(self.root / "Mixed/a.csv"), str(self.root / "Mixed/AllCSV/a.csv"))
        shutil.move(str(self.root / "Mixed/sub1"), str(self.root / "Mixed/AllCSV/sub1"))
        status, detail = self.verdict(self.a)
        self.assertEqual(status, "fail")
        self.assertIn("missing", detail)
        self.assertIn("unexpected new file", detail)

    def test_only_top_level_move_is_a_FAIL(self):
        run(expand_file_ops(self.vm, self.a))
        (self.root / "Mixed/AllCSV").mkdir()
        shutil.move(str(self.root / "Mixed/a.csv"), str(self.root / "Mixed/AllCSV/a.csv"))
        self.assertEqual(self.verdict(self.a)[0], "fail")

    def test_non_recursive_ignores_nested(self):
        a = self.assertion(op="move", source_dir="Mixed", destination_dir="Out", extension=".csv", recursive=False)
        run(expand_file_ops(self.vm, a))
        self.assertEqual([o["src"] for o in a["expected_ops"]], ["Mixed/a.csv"])


class CopyAndEdgeTests(Base):
    def test_copy_requires_originals_to_survive(self):
        self.put("Docs/report.txt", "R")
        self.put("Docs/data.csv", "D")
        a = self.assertion(op="copy", source_dir="Docs", destination_dir="Backup")
        run(expand_file_ops(self.vm, a))
        # the t02 failure: worker MOVED instead of copying
        (self.root / "Backup").mkdir()
        for n in ("report.txt", "data.csv"):
            shutil.move(str(self.root / "Docs" / n), str(self.root / "Backup" / n))
        status, detail = self.verdict(a)
        self.assertEqual(status, "fail")
        self.assertIn("original(s) of a copy", detail)

    def test_copy_passes_with_copy_files_tool(self):
        self.put("Docs/report.txt", "R")
        a = self.assertion(op="copy", source_dir="Docs", destination_dir="Backup")
        run(expand_file_ops(self.vm, a))
        self.vm.copy_files("Docs", ["report.txt"], "Backup")
        self.assertEqual(self.verdict(a)[0], "pass")

    def test_no_matches_is_unknown_never_pass(self):
        self.put("Incoming/notes.md", "n")
        a = self.assertion(op="move", source_dir="Incoming", destination_dir="Text", extension=".txt")
        ev = run(expand_file_ops(self.vm, a))
        self.assertEqual(ev["count"], 0)
        self.assertEqual(self.verdict(a)[0], "unknown")
        self.assertIn("Change nothing", describe_ops(a))

    def test_moving_other_extension_is_collateral_damage(self):
        """t07 regression: Quick Fix once moved .md files while the task was about .txt."""
        self.put("Incoming/a.txt", "a")
        self.put("Incoming/notes.md", "n")
        a = self.assertion(op="move", source_dir="Incoming", destination_dir="Text", extension=".txt")
        run(expand_file_ops(self.vm, a))
        (self.root / "Text").mkdir()
        shutil.move(str(self.root / "Incoming/a.txt"), str(self.root / "Text/a.txt"))
        shutil.move(str(self.root / "Incoming/notes.md"), str(self.root / "Text/notes.md"))
        status, detail = self.verdict(a)
        self.assertEqual(status, "fail")
        self.assertIn("COLLATERAL", detail)

    def test_name_collision_in_flatten_is_reported_not_overwritten(self):
        self.put("M/a/x.csv", "1")
        self.put("M/b/x.csv", "2")
        a = self.assertion(op="move", source_dir="M", destination_dir="Out", extension=".csv", recursive=True)
        run(expand_file_ops(self.vm, a))
        self.assertIn("collision", a["snapshot_error"])
        self.assertEqual(self.verdict(a)[0], "unknown")

    def test_existing_destination_file_is_a_conflict(self):
        self.put("A/x.txt", "1")
        self.put("B/x.txt", "old")
        a = self.assertion(op="move", source_dir="A", destination_dir="B", extension=".txt")
        run(expand_file_ops(self.vm, a))
        self.assertIn("already exists", a["snapshot_error"])

    def test_bad_parameters_become_unknown(self):
        for bad in (self.assertion(op="explode", source_dir="A"),
                    self.assertion(op="move", source_dir="A"),
                    self.assertion(op="rename", source_dir="A"),
                    self.assertion(op="move", source_dir="A", destination_dir="B", extension="txt")):
            self.put("A/x.txt", "1")
            run(expand_file_ops(self.vm, bad))
            self.assertEqual(self.verdict(bad)[0], "unknown", bad)

    def test_os_metadata_files_are_ignored(self):
        self.put("A/x.txt", "1")
        a = self.assertion(op="move", source_dir="A", destination_dir="B", extension=".txt")
        run(expand_file_ops(self.vm, a))
        self.put("A/.DS_Store", "junk")
        self.vm.move_files("A", ["x.txt"], "B")
        self.assertEqual(self.verdict(a)[0], "pass")

    def test_snapshot_is_taken_once(self):
        self.put("A/x.txt", "1")
        sg = {"id": 0, "check_spec": {"checks": [self.assertion(op="move", source_dir="A", destination_dir="B")]}}
        self.assertEqual(len(run(snapshot_file_ops(self.vm, sg))), 1)
        self.vm.move_files("A", ["x.txt"], "B")
        self.assertEqual(run(snapshot_file_ops(self.vm, sg)), [])        # a Quick Fix retry must not re-snapshot
        self.assertEqual(len(sg["check_spec"]["checks"][0]["expected_ops"]), 1)


class IntegrationTests(Base):
    def test_check_spec_dispatch_for_files_capability(self):
        self.put("A/x.txt", "1")
        a = self.assertion(op="move", source_dir="A", destination_dir="B", extension=".txt")
        run(expand_file_ops(self.vm, a))
        spec = {"checks": [a], "rationale": ""}
        self.assertEqual(run(check_spec(self.vm, spec, capability="files"))["status"], "fail")
        self.vm.move_files("A", ["x.txt"], "B")
        self.assertEqual(run(check_spec(self.vm, spec, capability="files"))["status"], "pass")

    def test_not_available_to_the_sheet_worker(self):
        a = self.assertion(op="move", source_dir="A", destination_dir="B")
        self.assertEqual(run(check_spec(self.vm, {"checks": [a]}, capability="sheet"))["status"], "unknown")

    def test_planner_junk_is_pruned_but_params_and_orchestrator_fields_follow_rules(self):
        a = self.assertion(op="rename", source_dir="R", extension=".txt", rename_prefix="2026_",
                           expected_ops=[{"src": "fake"}], cell="B7", values=[1.5], source_path="R/x")
        sg = {"id": 0, "capability": "files", "check_spec": {"checks": [a]}}
        prune_files_assertions([sg])
        out = sg["check_spec"]["checks"][0]
        self.assertIsNone(out["expected_ops"])
        self.assertNotIn("cell", out)
        self.assertEqual(out["rename_prefix"], "2026_")


if __name__ == "__main__":
    unittest.main()
