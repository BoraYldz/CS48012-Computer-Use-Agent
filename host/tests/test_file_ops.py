"""file_ops: state-grounded move / copy / rename contract (no LLM)."""
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from agent.checkers import check_spec
from agent.file_ops import (check_file_ops, contract_pairs, describe_ops, expand_file_ops, snapshot_file_ops,
                            violation)
from agent.local_runtime import LocalVM
from agent.worker_guard import (drop_discovery_subgoals, instruction_op, prune_files_assertions,
                                resolve_file_op_intent)


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


class OpInferenceTests(Base):
    def test_null_op_with_rename_params_means_rename(self):
        self.put("Reports/q1.txt", "1")
        a = self.assertion(op=None, source_dir="Reports", destination_dir="Reports", extension=".txt",
                           rename_prefix="2026_")
        ev = run(expand_file_ops(self.vm, a))
        self.assertEqual((a["op"], ev["count"]), ("rename", 1))

    def test_null_op_is_never_guessed_as_move(self):
        """Regression: t02 (a COPY) was contracted as a move, so the guarded tools forced the worker to move."""
        self.put("A/x.txt", "1")
        a = self.assertion(op=None, source_dir="A", destination_dir="B", extension=".txt")
        run(expand_file_ops(self.vm, a))
        self.assertIn("did not say", a["snapshot_error"])
        self.assertEqual(self.verdict(a)[0], "unknown")


class IntentResolutionTests(unittest.TestCase):
    def sg(self, instruction, **a):
        return {"id": 0, "capability": "files", "instruction": instruction,
                "check_spec": {"checks": [{"kind": "file_ops", **a}]}}

    def op_after(self, sg):
        resolve_file_op_intent([sg])
        return sg["check_spec"]["checks"][0]["op"]

    def test_copy_instruction_overrides_wrong_move(self):
        self.assertEqual(self.op_after(self.sg("Copy report.txt and data.csv from Docs into Backup", op="move")), "copy")

    def test_null_op_takes_instruction_verb(self):
        self.assertEqual(self.op_after(self.sg("Copy files", op=None)), "copy")
        self.assertEqual(self.op_after(self.sg("Back up the Docs folder", op=None)), "copy")
        self.assertEqual(self.op_after(self.sg("Move all .csv files", op=None)), "move")

    def test_rename_params_win_over_everything(self):
        self.assertEqual(self.op_after(self.sg("Move the files", op="move", rename_prefix="x_")), "rename")

    def test_unknown_verb_and_null_op_stays_null(self):
        self.assertIsNone(self.op_after(self.sg("Handle the files", op=None)))

    def test_planner_op_kept_when_instruction_has_no_verb_signal(self):
        self.assertEqual(self.op_after(self.sg("Please tidy the files", op="copy")), "copy")

    def test_instruction_op_first_word_only(self):
        self.assertIsNone(instruction_op("Ensure the files are copied"))
        self.assertEqual(instruction_op("copy x"), "copy")


class SiblingScopeTests(Base):
    """One subgoal, one file_ops per extension, shared destination (a typical sort). Found by code review of t02's log."""

    def setUp(self):
        super().setUp()
        self.put("In/a.txt", "a")
        self.put("In/b.csv", "b")
        self.put("In/keep.md", "k")
        self.sg = {"id": 0, "check_spec": {"checks": [
            self.assertion(op="move", source_dir="In", destination_dir="Out", extension=".txt"),
            self.assertion(op="move", source_dir="In", destination_dir="Out", extension=".csv")]}}
        run(snapshot_file_ops(self.vm, self.sg))

    def test_correct_sort_passes_both_assertions(self):
        self.vm.move_files("In", ["a.txt", "b.csv"], "Out")
        for a in self.sg["check_spec"]["checks"]:
            self.assertEqual(self.verdict(a)[0], "pass", self.verdict(a))

    def test_keep_file_still_protected(self):
        self.vm.move_files("In", ["a.txt", "b.csv", "keep.md"], "Out")
        self.assertEqual(self.verdict(self.sg["check_spec"]["checks"][0])[0], "fail")

    def test_siblings_not_preserved_for_each_other(self):
        first = self.sg["check_spec"]["checks"][0]
        self.assertEqual([k["path"] for k in first["preserved_files"]], ["In/keep.md"])
        self.assertIn("In/b.csv", first["allowed_paths"])


class ContractBoundCallTests(Base):
    """The t04 worker moved the whole `sub1` folder; the contract-bound tools refuse that."""

    def setUp(self):
        super().setUp()
        self.put("Mixed/a.csv", "1")
        self.put("Mixed/sub1/b.csv", "2")
        self.put("Mixed/sub1/deep/c.csv", "3")
        self.a = self.assertion(op="move", source_dir="Mixed", destination_dir="Mixed/AllCSV",
                                extension=".csv", recursive=True)
        run(expand_file_ops(self.vm, self.a))
        self.pairs = contract_pairs([self.a])

    def test_whole_folder_move_is_refused(self):
        self.assertTrue(violation("move_path", {"src": "Mixed/sub1", "dst": "Mixed/AllCSV/sub1"}, self.pairs))

    def test_planned_file_move_is_allowed(self):
        self.assertEqual(violation("move_path", {"src": "Mixed/sub1/b.csv", "dst": "Mixed/AllCSV/b.csv",
                                                 "overwrite": False}, self.pairs), [])

    def test_path_spelling_is_normalized(self):
        self.assertEqual(violation("move_path", {"src": "./Mixed//sub1/b.csv", "dst": "Mixed/AllCSV/b.csv"},
                                   self.pairs), [])

    def test_move_files_batch_checked_per_pair(self):
        ok = violation("move_files", {"src_dir": "Mixed", "filenames": ["a.csv"], "dst_dir": "Mixed/AllCSV"}, self.pairs)
        bad = violation("move_files", {"src_dir": "Mixed", "filenames": ["a.csv", "notes.md"], "dst_dir": "Mixed/AllCSV"},
                        self.pairs)
        self.assertEqual(ok, [])
        self.assertEqual(len(bad), 1)

    def test_copy_tool_cannot_perform_a_move_contract_and_vice_versa(self):
        self.assertTrue(violation("copy_path", {"src": "Mixed/a.csv", "dst": "Mixed/AllCSV/a.csv"}, self.pairs))

    def test_copy_contract_allows_copy_but_refuses_move(self):
        self.put("Docs/r.txt", "r")
        c = self.assertion(op="copy", source_dir="Docs", destination_dir="Backup")
        run(expand_file_ops(self.vm, c))
        pairs = contract_pairs([c])
        self.assertEqual(violation("copy_files", {"src_dir": "Docs", "filenames": ["r.txt"], "dst_dir": "Backup"}, pairs), [])
        self.assertTrue(violation("move_files", {"src_dir": "Docs", "filenames": ["r.txt"], "dst_dir": "Backup"}, pairs))

    def test_non_mutating_tools_and_malformed_calls(self):
        self.assertEqual(violation("list_dir", {"path": "Mixed"}, self.pairs), [])
        self.assertTrue(violation("move_files", {"src_dir": "Mixed", "filenames": [], "dst_dir": "x"}, self.pairs))
        self.assertTrue(violation("move_path", {"src": "Mixed/a.csv"}, self.pairs))


class DiscoveryFilterTests(unittest.TestCase):
    def sg(self, i, instr, checks):
        return {"id": i, "capability": "files", "instruction": instr, "check_spec": {"checks": checks}}

    def test_t04_plan_loses_its_discovery_subgoal_and_ids_are_renumbered(self):
        subs = [self.sg(0, "Discover all .csv files in Mixed and its subfolders",
                        [{"kind": "directory_contains_files", "path": "Mixed", "recursive": True}]),
                self.sg(1, "Move all discovered .csv files into Mixed/AllCSV",
                        [{"kind": "file_ops", "op": "move", "source_dir": "Mixed", "destination_dir": "Mixed/AllCSV"}])]
        self.assertEqual(len(drop_discovery_subgoals(subs)), 1)
        self.assertEqual([s["id"] for s in subs], [0])
        self.assertEqual(subs[0]["check_spec"]["checks"][0]["kind"], "file_ops")

    def test_discovery_subgoal_kept_when_no_file_ops_follows(self):
        subs = [self.sg(0, "List the files in Docs", [{"kind": "directory_contains_files", "path": "Docs"}])]
        self.assertEqual(drop_discovery_subgoals(subs), [])
        self.assertEqual(len(subs), 1)

    def test_real_work_subgoal_is_never_dropped(self):
        subs = [self.sg(0, "Find duplicates and delete them", [{"kind": "no_files_by_extension", "path": "D", "extension": ".tmp"}]),
                self.sg(1, "Move x", [{"kind": "file_ops", "op": "move", "source_dir": "A", "destination_dir": "B"}])]
        self.assertEqual(drop_discovery_subgoals(subs), [])


if __name__ == "__main__":
    unittest.main()
