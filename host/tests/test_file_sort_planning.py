"""Tests for deterministic planning of fully specified extension-sorting requests."""
import tempfile
import unittest
from pathlib import Path

from agent.checkers import check_spec
from agent.local_runtime import LocalVM
from agent.plan_validation import (
    add_empty_directory_postcondition,
    build_deterministic_file_sort_plan,
    normalize_file_sort_assertions,
    snapshot_move_assertions,
)


GOAL = (
    "Organize the files inside _eval/t01_sort_ext/Incoming. Create an Organized folder "
    "and sort the files into three subfolders based on their extensions: put all .txt files in Text, "
    "all .csv files in Data, and all .md files in Markdown. Move the original files rather than copying "
    "them. Do not modify their contents. Leave the Incoming folder empty."
)


class DeterministicFileSortPlanTests(unittest.TestCase):
    def test_explicit_extension_sort_with_empty_source_builds_three_file_subgoals(self):
        plan = build_deterministic_file_sort_plan(GOAL)

        self.assertIsNotNone(plan)
        self.assertEqual([sg["capability"] for sg in plan], ["files", "files", "files"])
        self.assertEqual(
            [(c["extension"], c["destination_dir"])
             for sg in plan for c in sg["check_spec"]["checks"]
             if c["kind"] == "files_moved_by_extension"],
            [
                (".txt", "_eval/t01_sort_ext/Organized/Text"),
                (".csv", "_eval/t01_sort_ext/Organized/Data"),
                (".md", "_eval/t01_sort_ext/Organized/Markdown"),
            ],
        )

    def test_ambiguous_destination_or_nonempty_source_requirement_falls_back_to_llm(self):
        self.assertIsNone(build_deterministic_file_sort_plan(
            GOAL.replace("Leave the Incoming folder empty.", "Keep unrelated files in Incoming.")
        ))
        self.assertIsNone(build_deterministic_file_sort_plan(
            "Move files from _eval/demo/Incoming into an archive folder."
        ))


class DeterministicFileSortExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_move_plan_preserves_all_files_and_leaves_source_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            vm = LocalVM(Path(tmp) / "workspace")
            source = "_eval/t01_sort_ext/Incoming"
            contents = {
                "meeting-notes.txt": "Meeting notes: ship on Friday.\n",
                "weekly-report.txt": "Weekly report: all green.\n",
                "expenses.csv": "item,amount\ncoffee,4\n",
                "project-brief.md": "# Brief\nScope and goals.\n",
                ".DS_Store": "macOS metadata",
            }
            for name, content in contents.items():
                vm.write_text(f"{source}/{name}", content)

            plan = build_deterministic_file_sort_plan(GOAL)
            self.assertIsNotNone(plan)
            normalize_file_sort_assertions(GOAL, plan)
            add_empty_directory_postcondition(GOAL, plan)
            snapshots = await snapshot_move_assertions(vm, plan)
            self.assertEqual(len(snapshots), 3)
            self.assertTrue(all("error" not in snapshot for snapshot in snapshots))

            # Simulate the normal Files-worker action using only each verified manifest.
            for subgoal in plan:
                assertion = next(
                    item for item in subgoal["check_spec"]["checks"]
                    if item["kind"] == "files_moved_by_extension"
                )
                names = [item["name"] for item in assertion["expected_files"]]
                self.assertTrue(names)
                vm.move_files(assertion["source_dir"], names, assertion["destination_dir"])

            for subgoal in plan:
                result = await check_spec(vm, subgoal["check_spec"], capability="files")
                self.assertEqual(result["status"], "pass", result)

            source_entries = vm.list_dir(source)
            self.assertEqual(
                [Path(item["path"]).name for item in source_entries
                 if Path(item["path"]).name != ".DS_Store"],
                [],
            )
            self.assertEqual(
                {item["path"] for item in vm.list_dir("_eval/t01_sort_ext/Organized", recursive=True)
                 if item["type"] == "file"},
                {
                    "_eval/t01_sort_ext/Organized/Text/meeting-notes.txt",
                    "_eval/t01_sort_ext/Organized/Text/weekly-report.txt",
                    "_eval/t01_sort_ext/Organized/Data/expenses.csv",
                    "_eval/t01_sort_ext/Organized/Markdown/project-brief.md",
                },
            )


if __name__ == "__main__":
    unittest.main()
