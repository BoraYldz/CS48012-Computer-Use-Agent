"""Tests for deterministic normalization of planner acceptance criteria."""
import unittest
from pathlib import Path

from agent.plan_validation import normalize_plan_check_specs


class PlanCheckNormalizationTests(unittest.TestCase):
    def test_creation_folder_converts_absent_to_exists_directory(self):
        plan = [{
            "id": 0,
            "instruction": "Create the DemoProject folder",
            "expected_output": "DemoProject folder created",
            "check_spec": {"checks": [
                {"kind": "path_absent", "path": "DemoProject"},
            ]},
        }]
        changes = normalize_plan_check_specs(plan)
        self.assertEqual(plan[0]["check_spec"]["checks"], [{
            "kind": "path_exists", "path": "DemoProject", "is_dir": True,
        }])
        self.assertEqual(changes[0]["action"], "converted_path_absent_to_path_exists")

    def test_creation_subfolder_converts_absent_to_exists_directory(self):
        plan = [{
            "id": 1,
            "instruction": "Create the Input subfolder inside DemoProject",
            "expected_output": "Input subfolder created",
            "check_spec": {"checks": [
                {"kind": "path_absent", "path": "DemoProject/Input"},
            ]},
        }]
        normalize_plan_check_specs(plan)
        self.assertEqual(plan[0]["check_spec"]["checks"][0], {
            "kind": "path_exists", "path": "DemoProject/Input", "is_dir": True,
        })

    def test_created_file_with_exact_content_removes_contradictory_absent_check(self):
        plan = [{
            "id": 4,
            "instruction": "Create the README.txt file inside DemoProject with the specified content",
            "expected_output": "README.txt file created with exact content",
            "check_spec": {"checks": [
                {"kind": "path_absent", "path": "DemoProject/README.txt"},
                {"kind": "text_equals", "path": "DemoProject/README.txt",
                 "text": "Demo project for quarterly reporting."},
            ]},
        }]
        changes = normalize_plan_check_specs(plan)
        self.assertEqual(plan[0]["check_spec"]["checks"], [{
            "kind": "text_equals", "path": "DemoProject/README.txt",
            "text": "Demo project for quarterly reporting.",
        }])
        self.assertEqual(changes[0]["action"], "removed_contradictory_path_absent")

    def test_created_file_without_content_becomes_file_exists_check(self):
        plan = [{
            "id": 0,
            "instruction": "Create report.csv file",
            "expected_output": "report.csv file created",
            "check_spec": {"checks": [
                {"kind": "path_absent", "path": "report.csv"},
            ]},
        }]
        normalize_plan_check_specs(plan)
        self.assertEqual(plan[0]["check_spec"]["checks"][0], {
            "kind": "path_exists", "path": "report.csv", "is_dir": False,
        })

    def test_delete_subgoal_keeps_path_absent(self):
        plan = [{
            "id": 0,
            "instruction": "Delete the old report folder",
            "expected_output": "old report folder no longer exists",
            "check_spec": {"checks": [
                {"kind": "path_absent", "path": "old report"},
            ]},
        }]
        changes = normalize_plan_check_specs(plan)
        self.assertEqual(changes, [])
        self.assertEqual(plan[0]["check_spec"]["checks"][0]["kind"], "path_absent")

    def test_destination_parent_is_not_mistaken_for_created_target(self):
        plan = [{
            "id": 0,
            "instruction": "Create the README.txt file inside DemoProject",
            "expected_output": "README.txt file created",
            "check_spec": {"checks": [
                {"kind": "path_absent", "path": "DemoProject"},
                {"kind": "path_absent", "path": "DemoProject/README.txt"},
            ]},
        }]
        normalize_plan_check_specs(plan)
        checks = plan[0]["check_spec"]["checks"]
        self.assertEqual(checks[0]["kind"], "path_absent")
        self.assertEqual(checks[1], {
            "kind": "path_exists", "path": "DemoProject/README.txt", "is_dir": False,
        })

    def test_unrelated_absence_check_is_not_rewritten(self):
        plan = [{
            "id": 0,
            "instruction": "Create the Output folder",
            "expected_output": "Output folder created",
            "check_spec": {"checks": [
                {"kind": "path_absent", "path": "Temporary"},
            ]},
        }]
        changes = normalize_plan_check_specs(plan)
        self.assertEqual(changes, [])
        self.assertEqual(plan[0]["check_spec"]["checks"][0]["path"], "Temporary")

    def test_same_path_positive_check_wins_over_contradictory_absence(self):
        plan = [{
            "id": 0,
            "instruction": "Check the README",
            "expected_output": "README content matches",
            "check_spec": {"checks": [
                {"kind": "path_absent", "path": "README.txt"},
                {"kind": "text_equals", "path": "README.txt", "text": "hello"},
            ]},
        }]
        changes = normalize_plan_check_specs(plan)
        self.assertEqual(len(plan[0]["check_spec"]["checks"]), 1)
        self.assertEqual(plan[0]["check_spec"]["checks"][0]["kind"], "text_equals")
        self.assertEqual(changes[0]["action"], "removed_contradictory_path_absent")


if __name__ == "__main__":
    unittest.main()


class FileOrganizationPlanTests(unittest.TestCase):
    def _plan(self):
        return [
            {
                "id": 0,
                "instruction": "Create Organized inside FileSortingDemo/Incoming",
                "expected_output": "Organized folder created",
                "check_spec": {"checks": [
                    {"kind": "path_exists", "path": "FileSortingDemo/Incoming/Organized", "is_dir": True},
                ]},
            },
            {
                "id": 1,
                "instruction": "Create Text, Data, and Markdown inside FileSortingDemo/Incoming/Organized",
                "expected_output": "Destination folders created",
                "check_spec": {"checks": [
                    {"kind": "path_exists", "path": "FileSortingDemo/Incoming/Organized/Text", "is_dir": True},
                    {"kind": "path_exists", "path": "FileSortingDemo/Incoming/Organized/Data", "is_dir": True},
                    {"kind": "path_exists", "path": "FileSortingDemo/Incoming/Organized/Markdown", "is_dir": True},
                ]},
            },
            {
                "id": 2,
                "instruction": "Move all .txt files from FileSortingDemo/Incoming to FileSortingDemo/Incoming/Organized/Text",
                "expected_output": "Text files moved",
                "check_spec": {"checks": [
                    {"kind": "path_absent", "path": "FileSortingDemo/Incoming/*.txt"},
                    {"kind": "path_exists", "path": "FileSortingDemo/Incoming/Organized/Text/*.txt"},
                ]},
            },
            {
                "id": 3,
                "instruction": "Move all .csv files from FileSortingDemo/Incoming to FileSortingDemo/Incoming/Organized/Data",
                "expected_output": "CSV files moved",
                "check_spec": {"checks": [
                    {"kind": "path_absent", "path": "FileSortingDemo/Incoming/*.csv"},
                    {"kind": "path_exists", "path": "FileSortingDemo/Incoming/Organized/Data/*.csv"},
                ]},
            },
            {
                "id": 4,
                "instruction": "Move all .md files from FileSortingDemo/Incoming to FileSortingDemo/Incoming/Organized/Markdown",
                "expected_output": "Markdown files moved",
                "check_spec": {"checks": [
                    {"kind": "path_absent", "path": "FileSortingDemo/Incoming/*.md"},
                    {"kind": "path_exists", "path": "FileSortingDemo/Incoming/Organized/Markdown/*.md"},
                ]},
            },
        ]

    def test_empty_source_rebases_destination_to_sibling(self):
        from agent.plan_validation import normalize_empty_directory_destinations
        plan = self._plan()
        goal = (
            "Organize files inside FileSortingDemo/Incoming. Create an Organized folder "
            "and sort files into it. Leave the Incoming folder empty."
        )
        changes = normalize_empty_directory_destinations(goal, plan)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["new_output_root"], "FileSortingDemo/Organized")
        self.assertEqual(
            plan[0]["check_spec"]["checks"][0]["path"],
            "FileSortingDemo/Organized",
        )
        self.assertIn("at FileSortingDemo/Organized", plan[0]["instruction"])
        self.assertIn("FileSortingDemo/Organized", plan[1]["instruction"])
        # The original source wildcard remains in the source directory.
        self.assertEqual(plan[2]["check_spec"]["checks"][0]["path"], "FileSortingDemo/Incoming/*.txt")

    def test_does_not_rebase_destination_without_explicit_empty_requirement(self):
        from agent.plan_validation import normalize_empty_directory_destinations
        plan = self._plan()
        changes = normalize_empty_directory_destinations(
            "Create Organized inside FileSortingDemo/Incoming and sort the files.", plan
        )
        self.assertEqual(changes, [])
        self.assertEqual(
            plan[0]["check_spec"]["checks"][0]["path"],
            "FileSortingDemo/Incoming/Organized",
        )

    def test_source_wildcard_and_destination_directory_become_snapshot_move_assertion(self):
        from agent.plan_validation import normalize_glob_move_checks
        plan = [{
            "id": 2,
            "instruction": "Move all .txt files from Incoming to Organized/Text",
            "expected_output": "TXT files moved",
            "check_spec": {"checks": [
                {"kind": "path_absent", "path": "Incoming/*.txt"},
                {"kind": "path_exists", "path": "Organized/Text", "is_dir": True},
            ]},
        }]
        changes = normalize_glob_move_checks(plan)
        self.assertEqual(len(changes), 1)
        checks = plan[0]["check_spec"]["checks"]
        self.assertIn({"kind": "path_exists", "path": "Organized/Text", "is_dir": True}, checks)
        self.assertIn({
            "kind": "files_moved_by_extension",
            "source_dir": "Incoming",
            "destination_dir": "Organized/Text",
            "extension": ".txt",
            "expected_files": None,
        }, checks)
        self.assertFalse(any(item.get("kind") == "path_absent" for item in checks))

    def test_paired_wildcard_checks_become_snapshot_move_assertion(self):
        from agent.plan_validation import normalize_glob_move_checks
        plan = self._plan()
        changes = normalize_glob_move_checks(plan)
        self.assertEqual(len(changes), 3)
        txt_checks = plan[2]["check_spec"]["checks"]
        self.assertEqual(len(txt_checks), 1)
        self.assertEqual(txt_checks[0], {
            "kind": "files_moved_by_extension",
            "source_dir": "FileSortingDemo/Incoming",
            "destination_dir": "FileSortingDemo/Incoming/Organized/Text",
            "extension": ".txt",
            "expected_files": None,
        })

    def test_empty_directory_postcondition_is_added_only_to_last_move_subgoal(self):
        from agent.plan_validation import (
            add_empty_directory_postcondition,
            normalize_empty_directory_destinations,
            normalize_glob_move_checks,
        )
        plan = self._plan()
        goal = "Organize FileSortingDemo/Incoming. Leave the Incoming folder empty."
        normalize_empty_directory_destinations(goal, plan)
        normalize_glob_move_checks(plan)
        changes = add_empty_directory_postcondition(goal, plan)
        self.assertEqual(len(changes), 1)
        self.assertFalse(any(
            item.get("kind") == "directory_empty"
            for subgoal in plan[:-1]
            for item in subgoal["check_spec"]["checks"]
        ))
        self.assertIn(
            {"kind": "directory_empty", "path": "FileSortingDemo/Incoming"},
            plan[-1]["check_spec"]["checks"],
        )


class FileMoveSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshots_only_matching_files_before_execution(self):
        from agent.plan_validation import snapshot_move_assertions

        class FakeVM:
            async def call(self, name, **kwargs):
                if name != "list_dir" or kwargs.get("path") != "Incoming":
                    raise AssertionError("unexpected snapshot tool call")
                return [
                    {"path": "Incoming/a.txt", "type": "file", "size": 10},
                    {"path": "Incoming/b.csv", "type": "file", "size": 20},
                    {"path": "Incoming/child", "type": "dir", "size": None},
                    {"path": "Incoming/c.TXT", "type": "file", "size": 30},
                ]

        vm = FakeVM()
        plan = [{"id": 2, "check_spec": {"checks": [{
            "kind": "files_moved_by_extension",
            "source_dir": "Incoming",
            "destination_dir": "Organized/Text",
            "extension": ".txt",
            "expected_files": None,
        }]}}]
        events = await snapshot_move_assertions(vm, plan)
        self.assertEqual(events[0]["files"], [
            {"name": "a.txt", "size": 10},
            {"name": "c.TXT", "size": 30},
        ])
        self.assertEqual(
            plan[0]["check_spec"]["checks"][0]["expected_files"],
            events[0]["files"],
        )

    async def test_snapshot_failure_is_preserved_as_unverified(self):
        from agent.plan_validation import snapshot_move_assertions

        class FakeVM:
            async def call(self, name, **kwargs):
                raise FileNotFoundError("Incoming")

        plan = [{"id": 0, "check_spec": {"checks": [{
            "kind": "files_moved_by_extension",
            "source_dir": "Incoming",
            "destination_dir": "Organized/Text",
            "extension": ".txt",
            "expected_files": None,
        }]}}]
        events = await snapshot_move_assertions(FakeVM(), plan)
        self.assertIn("FileNotFoundError", events[0]["error"])
        self.assertEqual(plan[0]["check_spec"]["checks"][0]["expected_files"], [])
        self.assertIn("snapshot_error", plan[0]["check_spec"]["checks"][0])


class FileOrganizationEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_normalization_snapshot_moves_and_final_empty_check(self):
        import tempfile
        from agent.checkers import check_spec
        from agent.local_runtime import LocalVM
        from agent.plan_validation import (
            add_empty_directory_postcondition,
            normalize_empty_directory_destinations,
            normalize_glob_move_checks,
            snapshot_move_assertions,
        )

        goal = (
            "Organize the files inside FileSortingDemo/Incoming. Create an Organized folder "
            "and sort the files by extension. Leave the Incoming folder empty."
        )
        with tempfile.TemporaryDirectory() as tmp:
            vm = LocalVM(tmp)
            fixtures = {
                "meeting.txt": "meeting content",
                "expenses.csv": "date,amount\n2026-11-01,150",
                "brief.md": "# brief",
                ".DS_Store": "Finder metadata",
            }
            for filename, contents in fixtures.items():
                vm.write_text(f"FileSortingDemo/Incoming/{filename}", contents)

            plan = []
            for index, (extension, folder) in enumerate((
                (".txt", "Text"), (".csv", "Data"), (".md", "Markdown")
            )):
                plan.append({
                    "id": index,
                    "capability": "files",
                    "instruction": (
                        f"Move all {extension} files from FileSortingDemo/Incoming to "
                        f"FileSortingDemo/Incoming/Organized/{folder}"
                    ),
                    "expected_output": f"{extension} files moved",
                    "check_spec": {"checks": [
                        {"kind": "path_absent", "path": f"FileSortingDemo/Incoming/*{extension}"},
                        {"kind": "path_exists", "path": f"FileSortingDemo/Incoming/Organized/{folder}/*{extension}"},
                    ]},
                })

            normalize_empty_directory_destinations(goal, plan)
            normalize_glob_move_checks(plan)
            add_empty_directory_postcondition(goal, plan)
            await snapshot_move_assertions(vm, plan)

            for extension, folder in ((".txt", "Text"), (".csv", "Data"), (".md", "Markdown")):
                names = [
                    Path(entry["path"]).name
                    for entry in vm.list_dir("FileSortingDemo/Incoming")
                    if entry["type"] == "file" and Path(entry["path"]).name.casefold().endswith(extension)
                ]
                if names:
                    vm.move_files(
                        "FileSortingDemo/Incoming",
                        names,
                        f"FileSortingDemo/Organized/{folder}",
                    )

            results = [await check_spec(vm, sg["check_spec"], capability="files") for sg in plan]
            self.assertTrue(all(result["status"] == "pass" for result in results), results)
            empty_result = await check_spec(vm, {"checks": [
                {"kind": "directory_empty", "path": "FileSortingDemo/Incoming"},
            ]}, capability="files")
            self.assertEqual(empty_result["status"], "pass", empty_result)
            self.assertEqual(
                {entry["path"] for entry in vm.list_dir("FileSortingDemo/Organized", recursive=True)
                 if entry["type"] == "file"},
                {
                    "FileSortingDemo/Organized/Text/meeting.txt",
                    "FileSortingDemo/Organized/Data/expenses.csv",
                    "FileSortingDemo/Organized/Markdown/brief.md",
                },
            )


class GenericFileSortNormalizationTests(unittest.TestCase):
    def _plan(self):
        source = "FileSortingDemo/Incoming"
        old_root = f"{source}/Organized"
        return [
            {"id": 0, "capability": "files", "instruction": f"Create Organized inside {source}",
             "expected_output": "Organized folder created",
             "check_spec": {"checks": [{"kind": "path_exists", "path": old_root, "is_dir": True}]}},
            {"id": 1, "capability": "files", "instruction": f"List files inside {source}",
             "expected_output": "Files identified",
             "check_spec": {"checks": [{"kind": "path_exists", "path": source, "is_dir": True}]}},
            {"id": 2, "capability": "files", "instruction": f"Move all .txt files from {source} to {old_root}/Text",
             "expected_output": "TXT files moved",
             "check_spec": {"checks": [{"kind": "path_exists", "path": f"{old_root}/Text", "is_dir": True},
                                          {"kind": "path_absent", "path": f"{source}/*.txt"}]}},
            {"id": 3, "capability": "files", "instruction": f"Move all .csv files from {source} to {old_root}/Data",
             "expected_output": "CSV files moved",
             "check_spec": {"checks": [{"kind": "path_exists", "path": f"{old_root}/Data", "is_dir": True}]}},
            {"id": 4, "capability": "files", "instruction": f"Move all .md files from {source} to {old_root}/Markdown",
             "expected_output": "Markdown files moved",
             "check_spec": {"checks": [{"kind": "path_exists", "path": f"{old_root}/Markdown", "is_dir": True}]}},
            {"id": 5, "capability": "files", "instruction": f"Confirm {source} is empty",
             "expected_output": "Incoming is empty",
             "check_spec": {"checks": [{"kind": "path_absent", "path": f"{source}/*"}]}},
        ]

    def test_generic_request_adds_snapshot_move_assertions_and_strong_inventory_check(self):
        from agent.plan_validation import (
            add_empty_directory_postcondition,
            normalize_bare_empty_globs,
            normalize_empty_directory_destinations,
            normalize_file_sort_assertions,
            strengthen_directory_inventory_checks,
        )
        goal = (
            "Organize the files inside FileSortingDemo/Incoming. Create an Organized folder "
            "and sort the files into three subfolders based on their extensions: put all .txt files in Text, "
            "all .csv files in Data, and all .md files in Markdown. Move the original files rather than copying them. "
            "Do not modify their contents. Leave the Incoming folder empty."
        )
        plan = self._plan()
        normalize_empty_directory_destinations(goal, plan)
        changes = normalize_file_sort_assertions(goal, plan)
        self.assertEqual({item["extension"] for item in changes}, {".txt", ".csv", ".md"})
        inventory = strengthen_directory_inventory_checks(plan, "FileSortingDemo/Incoming")
        self.assertEqual(len(inventory), 1)
        self.assertEqual(plan[1]["check_spec"]["checks"], [{
            "kind": "directory_contains_files", "path": "FileSortingDemo/Incoming"
        }])
        add_empty_directory_postcondition(goal, plan)
        normalize_bare_empty_globs(plan)
        for extension, folder in ((".txt", "Text"), (".csv", "Data"), (".md", "Markdown")):
            matching = [sg for sg in plan if extension in sg["instruction"]]
            self.assertEqual(len(matching), 1)
            self.assertIn(f"FileSortingDemo/Organized/{folder}", matching[0]["instruction"])
            self.assertNotIn("FileSortingDemo/Incoming/Organized", matching[0]["instruction"])
            assertions = matching[0]["check_spec"]["checks"]
            self.assertTrue(any(a.get("kind") == "files_moved_by_extension"
                                and a.get("source_dir") == "FileSortingDemo/Incoming"
                                and a.get("destination_dir") == f"FileSortingDemo/Organized/{folder}"
                                for a in assertions))
        self.assertTrue(any(a.get("kind") == "directory_empty"
                            and a.get("path") == "FileSortingDemo/Incoming"
                            for sg in plan for a in sg["check_spec"]["checks"]))
        self.assertFalse(any(a.get("kind") == "path_absent" and "*" in str(a.get("path"))
                            for sg in plan for a in sg["check_spec"]["checks"]))

    def test_existing_move_subgoal_instruction_is_rewritten_to_canonical_destination(self):
        from agent.plan_validation import normalize_empty_directory_destinations, normalize_file_sort_assertions
        source = "FileSortingDemo/Incoming"
        old_root = f"{source}/Organized"
        plan = [{
            "id": 0,
            "instruction": f"Move all .txt files from {source} to {old_root}/Text",
            "expected_output": f"Files moved to {old_root}/Text",
            "check_spec": {"checks": [
                {"kind": "path_exists", "path": f"{old_root}/Text", "is_dir": True},
                {"kind": "path_absent", "path": f"{source}/*.txt"},
            ]},
        }]
        goal = (
            f"Organize files inside {source}. Create an Organized folder, put .txt files in Text, "
            "and leave the Incoming folder empty."
        )
        normalize_empty_directory_destinations(goal, plan)
        normalize_file_sort_assertions(goal, plan)
        instruction = plan[0]["instruction"]
        expected = plan[0]["expected_output"]
        self.assertIn("move_expected_files", instruction)
        self.assertIn("FileSortingDemo/Organized/Text", instruction)
        self.assertNotIn(old_root, instruction)
        self.assertIn("FileSortingDemo/Organized/Text", expected)

    def test_file_sort_subgoals_are_separated_when_planner_combines_extensions(self):
        from agent.plan_validation import normalize_file_sort_assertions
        source = "Demo/Incoming"
        plan = [{
            "id": 0,
            "instruction": "Move .txt, .csv and .md files into Organized",
            "expected_output": "All files sorted",
            "check_spec": {"checks": [
                {"kind": "path_exists", "path": "Demo/Organized", "is_dir": True},
            ]},
        }]
        goal = (
            f"Organize files inside {source}. Create an Organized folder, put .txt files in Text, "
            "all .csv files in Data, and all .md files in Markdown."
        )
        changes = normalize_file_sort_assertions(goal, plan)
        self.assertEqual({item["extension"] for item in changes}, {".txt", ".csv", ".md"})
        move_subgoals = [
            sg for sg in plan
            if any(a.get("kind") == "files_moved_by_extension" for a in sg["check_spec"]["checks"])
        ]
        self.assertEqual(len(move_subgoals), 3)
        self.assertEqual(len({sg["id"] for sg in move_subgoals}), 3)


class BoundMoveRequestTests(unittest.TestCase):
    def test_move_request_uses_contract_paths_not_model_supplied_paths(self):
        from agent.plan_validation import build_bound_move_request
        contract = {
            "source_dir": "Demo/Incoming",
            "destination_dir": "Demo/Organized/Text",
            "extension": ".txt",
            "expected_files": [{"name": "a.txt"}, {"name": "b.txt"}],
        }
        result = build_bound_move_request(contract, ["a.txt", "b.txt"])
        self.assertEqual(result, {
            "src_dir": "Demo/Incoming",
            "filenames": ["a.txt", "b.txt"],
            "dst_dir": "Demo/Organized/Text",
        })

    def test_move_request_rejects_files_outside_snapshot(self):
        from agent.plan_validation import build_bound_move_request
        contract = {
            "source_dir": "Demo/Incoming", "destination_dir": "Demo/Organized/Text",
            "extension": ".txt", "expected_files": [{"name": "a.txt"}],
        }
        with self.assertRaisesRegex(ValueError, "not in the verified planning-time manifest"):
            build_bound_move_request(contract, ["expenses.csv"])

    def test_move_request_rejects_wrong_extension(self):
        from agent.plan_validation import build_bound_move_request
        contract = {
            "source_dir": "Demo/Incoming", "destination_dir": "Demo/Organized/Text",
            "extension": ".txt", "expected_files": [{"name": "a.txt"}, {"name": "b.csv"}],
        }
        with self.assertRaisesRegex(ValueError, "only .txt files"):
            build_bound_move_request(contract, ["b.csv"])

    def test_move_request_rejects_missing_snapshot(self):
        from agent.plan_validation import build_bound_move_request
        contract = {
            "source_dir": "Demo/Incoming", "destination_dir": "Demo/Organized/Text",
            "extension": ".txt", "expected_files": [],
        }
        with self.assertRaisesRegex(ValueError, "no snapshotted source files"):
            build_bound_move_request(contract, ["a.txt"])

    def test_move_request_rejects_duplicate_filenames(self):
        from agent.plan_validation import build_bound_move_request
        contract = {
            "source_dir": "Demo/Incoming", "destination_dir": "Demo/Organized/Text",
            "extension": ".txt", "expected_files": [{"name": "a.txt"}],
        }
        with self.assertRaisesRegex(ValueError, "duplicate filenames"):
            build_bound_move_request(contract, ["a.txt", "a.txt"])


class DuplicateMoveAssertionTests(unittest.TestCase):
    def test_duplicate_file_move_assertions_are_removed(self):
        from agent.plan_validation import deduplicate_file_move_assertions
        assertion = {
            "kind": "files_moved_by_extension",
            "source_dir": "Demo/Incoming",
            "destination_dir": "Demo/Organized/Text",
            "extension": ".txt",
            "expected_files": None,
        }
        plan = [{
            "id": 1,
            "check_spec": {"checks": [dict(assertion), dict(assertion)]},
        }]
        changes = deduplicate_file_move_assertions(plan)
        self.assertEqual(len(changes), 1)
        self.assertEqual(plan[0]["check_spec"]["checks"], [assertion])


class GenericFileSortIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_bound_move_execution_is_safe_to_retry_after_partial_progress(self):
        import tempfile
        from agent.local_runtime import LocalVM
        from agent.plan_validation import execute_bound_move
        with tempfile.TemporaryDirectory() as tmp:
            vm = LocalVM(tmp)
            vm.write_text("Demo/Incoming/a.txt", "alpha")
            vm.write_text("Demo/Incoming/b.txt", "bravo")
            vm.make_dirs("Demo/Organized/Text")
            # Simulate one file already moved by an earlier interrupted attempt.
            vm.move_files("Demo/Incoming", ["a.txt"], "Demo/Organized/Text")
            assertion = {
                "kind": "files_moved_by_extension",
                "source_dir": "Demo/Incoming",
                "destination_dir": "Demo/Organized/Text",
                "extension": ".txt",
                "expected_files": [
                    {"name": "a.txt", "size": 5, "sha256": vm._sha256(vm.root / "Demo/Organized/Text/a.txt")},
                    {"name": "b.txt", "size": 5, "sha256": vm._sha256(vm.root / "Demo/Incoming/b.txt")},
                ],
            }
            first = await execute_bound_move(vm, assertion, ["a.txt", "b.txt"])
            self.assertEqual(first["moved"], ["b.txt"])
            self.assertEqual(first["already_at_destination"], ["a.txt"])
            second = await execute_bound_move(vm, assertion, ["a.txt", "b.txt"])
            self.assertEqual(second["moved"], [])
            self.assertEqual(set(second["already_at_destination"]), {"a.txt", "b.txt"})
            self.assertEqual(await vm.call("list_dir", path="Demo/Incoming"), [])

    async def test_bound_move_execution_refuses_snapshotted_file_missing_from_both_paths(self):
        import tempfile
        from agent.local_runtime import LocalVM
        from agent.plan_validation import execute_bound_move
        with tempfile.TemporaryDirectory() as tmp:
            vm = LocalVM(tmp)
            vm.make_dirs("Demo/Incoming")
            assertion = {
                "kind": "files_moved_by_extension",
                "source_dir": "Demo/Incoming",
                "destination_dir": "Demo/Organized/Text",
                "extension": ".txt",
                "expected_files": [{"name": "missing.txt", "size": 1}],
            }
            with self.assertRaisesRegex(FileNotFoundError, "missing from both source"):
                await execute_bound_move(vm, assertion, ["missing.txt"])

    async def test_normalized_plan_snapshots_hashes_and_verifies_all_original_files(self):
        import tempfile
        from agent.checkers import check_spec
        from agent.local_runtime import LocalVM
        from agent.plan_validation import (
            add_empty_directory_postcondition,
            normalize_bare_empty_globs,
            normalize_empty_directory_destinations,
            normalize_file_sort_assertions,
            snapshot_move_assertions,
            strengthen_directory_inventory_checks,
        )
        goal = (
            "Organize the files inside FileSortingDemo/Incoming. Create an Organized folder "
            "and sort the files into three subfolders based on their extensions: put all .txt files in Text, "
            "all .csv files in Data, and all .md files in Markdown. Move the original files rather than copying them. "
            "Do not modify their contents. Leave the Incoming folder empty."
        )
        with tempfile.TemporaryDirectory() as tmp:
            vm = LocalVM(tmp)
            contents = {
                "meeting-notes.txt": "Meeting notes for November.\n",
                "weekly-report.txt": "Weekly progress report.\n",
                "expenses.csv": "date,amount\n2026-11-01,150\n",
                "project-brief.md": "# Project Brief\nQuarterly reporting project.\n",
                ".DS_Store": "metadata",
            }
            for filename, value in contents.items():
                vm.write_text(f"FileSortingDemo/Incoming/{filename}", value)
            plan = GenericFileSortNormalizationTests()._plan()
            normalize_empty_directory_destinations(goal, plan)
            normalize_file_sort_assertions(goal, plan)
            from agent.plan_validation import execute_bound_move
            strengthen_directory_inventory_checks(plan, "FileSortingDemo/Incoming")
            add_empty_directory_postcondition(goal, plan)
            normalize_bare_empty_globs(plan)
            await snapshot_move_assertions(vm, plan)

            vm.make_dirs("FileSortingDemo/Organized")
            # Execute in graph order: inventory is checked before any source files move.
            result0 = await check_spec(vm, plan[0]["check_spec"], capability="files")
            result1 = await check_spec(vm, plan[1]["check_spec"], capability="files")
            self.assertEqual(result0["status"], "pass", result0)
            self.assertEqual(result1["status"], "pass", result1)
            move_results = []
            for extension, folder in ((".txt", "Text"), (".csv", "Data"), (".md", "Markdown")):
                matching = next(
                    sg for sg in plan
                    if any(a.get("kind") == "files_moved_by_extension" and a.get("extension") == extension
                           for a in sg["check_spec"]["checks"])
                )
                move_assertion = next(
                    a for a in matching["check_spec"]["checks"]
                    if a.get("kind") == "files_moved_by_extension" and a.get("extension") == extension
                )
                names = [entry["name"] for entry in move_assertion["expected_files"]]
                await execute_bound_move(vm, move_assertion, names)
                move_results.append(await check_spec(vm, matching["check_spec"], capability="files"))
            remaining_results = [
                await check_spec(vm, sg["check_spec"], capability="files")
                for sg in plan
                if sg["id"] not in {plan[0]["id"], plan[1]["id"]}
                and not any(a.get("kind") == "files_moved_by_extension"
                            for a in sg["check_spec"]["checks"])
            ]
            results = [result0, result1, *move_results, *remaining_results]
            self.assertTrue(all(item["status"] == "pass" for item in results), results)
            self.assertEqual(
                {entry["path"] for entry in vm.list_dir("FileSortingDemo/Organized", recursive=True)
                 if entry["type"] == "file"},
                {
                    "FileSortingDemo/Organized/Text/meeting-notes.txt",
                    "FileSortingDemo/Organized/Text/weekly-report.txt",
                    "FileSortingDemo/Organized/Data/expenses.csv",
                    "FileSortingDemo/Organized/Markdown/project-brief.md",
                },
            )

