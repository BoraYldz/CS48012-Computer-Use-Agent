"""Unit tests for deterministic subgoal verification (no VM required)."""
import unittest

from agent.checkers import check_spec


class FakeVM:
    def __init__(self):
        self.paths = {
            "Desktop/Proje": {"exists": True, "is_dir": True},
            "Desktop/Proje/Girdiler": {"exists": True, "is_dir": True},
            "Desktop/notlar.txt": {"exists": True, "is_dir": False},
            "Desktop/exact.txt": {"exists": True, "is_dir": False},
            "Desktop/extra.txt": {"exists": True, "is_dir": False},
            "Desktop/data.xlsx": {"exists": True, "is_dir": False},
        }
        self.texts = {
            "Desktop/notlar.txt": "do not touch",
            "Desktop/exact.txt": "QUICK_FIX_RECOVERED",
            "Desktop/extra.txt": "prefix QUICK_FIX_RECOVERED\n",
        }
        self.directories = {
            "FileSortingDemo/Incoming": [],
            "FileSortingDemo/Organized/Text": [
                {"path": "FileSortingDemo/Organized/Text/meeting-notes.txt", "type": "file", "size": 30},
                {"path": "FileSortingDemo/Organized/Text/weekly-report.txt", "type": "file", "size": 24},
            ],
        }
        self.books = {
            "Desktop/data.xlsx": {
                "sheets": ["Sheet1", "Summary"],
                "sheet": "Sheet1",
                "rows": [["Name", "Score"], ["Ada", 95], ["Linus", 91]],
                "total_rows": 3,
                "truncated": False,
            }
        }

    async def call(self, name, **kwargs):
        path = kwargs.get("path")
        if name == "path_info":
            return self.paths.get(path, {"exists": False, "is_dir": False})
        if name == "list_dir":
            if path not in self.directories:
                raise FileNotFoundError(path)
            return self.directories[path]
        if name == "read_text":
            if path not in self.texts:
                raise FileNotFoundError(path)
            return self.texts[path]
        if name == "xlsx_read":
            book = self.books[path]
            if kwargs.get("sheet"):
                if kwargs["sheet"] not in book["sheets"]:
                    raise KeyError(kwargs["sheet"])
                return {**book, "sheet": kwargs["sheet"]}
            return book
        raise AssertionError(f"Unexpected tool: {name}")


class CheckSpecTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.vm = FakeVM()

    async def test_paths_pass(self):
        result = await check_spec(self.vm, {"checks": [
            {"kind": "path_exists", "path": "Desktop/Proje", "is_dir": True},
            {"kind": "path_exists", "path": "Desktop/Proje/Girdiler", "is_dir": True},
        ]})
        self.assertEqual(result["status"], "pass")

    async def test_missing_path_fails_with_actionable_evidence(self):
        result = await check_spec(self.vm, {"checks": [
            {"kind": "path_exists", "path": "Desktop/Proje/Raporlar", "is_dir": True},
        ]})
        self.assertEqual(result["status"], "fail")
        self.assertIn("Desktop/Proje/Raporlar", result["evidence"])

    async def test_directory_empty_ignores_common_os_metadata(self):
        self.vm.directories["Incoming"] = [
            {"path": "Incoming/.DS_Store", "type": "file", "size": 6148},
            {"path": "Incoming/Thumbs.db", "type": "file", "size": 1024},
        ]
        result = await check_spec(self.vm, {"checks": [
            {"kind": "directory_empty", "path": "Incoming"},
        ]}, capability="files")
        self.assertEqual(result["status"], "pass")
        self.assertIn("ignored 2 OS metadata", result["detail"])

    async def test_directory_empty_does_not_ignore_user_files(self):
        self.vm.directories["Incoming"] = [
            {"path": "Incoming/notes.txt", "type": "file", "size": 10},
            {"path": "Incoming/.DS_Store", "type": "file", "size": 100},
        ]
        result = await check_spec(self.vm, {"checks": [
            {"kind": "directory_empty", "path": "Incoming"},
        ]}, capability="files")
        self.assertEqual(result["status"], "fail")
        self.assertIn("1 user item", result["detail"])

    async def test_empty_spec_is_unknown_not_pass(self):
        result = await check_spec(self.vm, {"checks": [], "rationale": "No available postcondition"})
        self.assertEqual(result["status"], "unknown")

    async def test_path_absent(self):
        result = await check_spec(self.vm, {"checks": [
            {"kind": "path_absent", "path": "Desktop/Proje/Raportlar"},
        ]})
        self.assertEqual(result["status"], "pass")

    async def test_missing_text_file_is_a_deterministic_failure(self):
        result = await check_spec(self.vm, {"checks": [
            {"kind": "text_contains", "path": "Desktop/missing.txt", "text": "expected"},
        ]})
        self.assertEqual(result["status"], "fail")

    async def test_text_contains(self):
        result = await check_spec(self.vm, {"checks": [
            {"kind": "text_contains", "path": "Desktop/notlar.txt", "text": "do not touch"},
        ]})
        self.assertEqual(result["status"], "pass")

    async def test_text_equals_passes_only_for_exact_whole_file_content(self):
        result = await check_spec(self.vm, {"checks": [
            {"kind": "text_equals", "path": "Desktop/exact.txt", "text": "QUICK_FIX_RECOVERED"},
        ]}, capability="files")
        self.assertEqual(result["status"], "pass")

    async def test_text_equals_rejects_extra_content_even_when_marker_is_present(self):
        result = await check_spec(self.vm, {"checks": [
            {"kind": "text_equals", "path": "Desktop/extra.txt", "text": "QUICK_FIX_RECOVERED"},
        ]}, capability="files")
        self.assertEqual(result["status"], "fail")
        self.assertIn("exactly match", result["evidence"])

    async def test_text_equals_large_file_is_unknown_not_a_truncated_pass(self):
        path = "Desktop/large.txt"
        self.vm.paths[path] = {"exists": True, "is_dir": False}
        self.vm.texts[path] = "x" * 200_001
        result = await check_spec(self.vm, {"checks": [
            {"kind": "text_equals", "path": path, "text": "x" * 200_001},
        ]}, capability="files")
        self.assertEqual(result["status"], "unknown")
        self.assertIn("verification limit", result["detail"])

    async def test_text_equals_allows_exact_empty_file(self):
        self.vm.texts["Desktop/empty.txt"] = ""
        self.vm.paths["Desktop/empty.txt"] = {"exists": True, "is_dir": False}
        result = await check_spec(self.vm, {"checks": [
            {"kind": "text_equals", "path": "Desktop/empty.txt", "text": ""},
        ]}, capability="files")
        self.assertEqual(result["status"], "pass")

    async def test_spreadsheet_assertions(self):
        result = await check_spec(self.vm, {"checks": [
            {"kind": "xlsx_sheet_exists", "path": "Desktop/data.xlsx", "sheet": "Summary"},
            {"kind": "xlsx_values_present", "path": "Desktop/data.xlsx", "values": ["Ada", 95]},
            {"kind": "xlsx_rows_present", "path": "Desktop/data.xlsx", "rows": [["Linus", 91]]},
        ]})
        self.assertEqual(result["status"], "pass")

    async def test_unsupported_assertion_is_unknown(self):
        result = await check_spec(self.vm, {"checks": [
            {"kind": "window_title_is", "path": "Desktop"},
        ]})
        self.assertEqual(result["status"], "unknown")

    async def test_spreadsheet_empty_value_is_not_used_as_proof(self):
        result = await check_spec(self.vm, {"checks": [
            {"kind": "xlsx_values_present", "path": "Desktop/data.xlsx", "values": [None]},
        ]}, capability="sheet")
        self.assertEqual(result["status"], "unknown")

    async def test_checker_is_scoped_to_worker_capability(self):
        result = await check_spec(self.vm, {"checks": [
            {"kind": "xlsx_sheet_exists", "path": "Desktop/data.xlsx", "sheet": "Sheet1"},
        ]}, capability="files")
        self.assertEqual(result["status"], "unknown")
        self.assertIn("not available to this worker", result["detail"])

    async def test_wildcard_path_assertion_is_unknown_not_false_positive(self):
        result = await check_spec(self.vm, {"checks": [
            {"kind": "path_absent", "path": "FileSortingDemo/Incoming/*.txt"},
        ]}, capability="files")
        self.assertEqual(result["status"], "unknown")
        self.assertIn("wildcard paths are not valid", result["detail"])


    async def test_directory_contains_files_passes_for_user_files(self):
        self.vm.directories["Incoming"] = [
            {"path": "Incoming/a.txt", "type": "file", "size": 1}
        ]
        result = await check_spec(self.vm, {"checks": [
            {"kind": "directory_contains_files", "path": "Incoming"}
        ]}, capability="files")
        self.assertEqual(result["status"], "pass")

    async def test_directory_contains_files_fails_when_only_os_metadata_exists(self):
        self.vm.directories["Incoming"] = [
            {"path": "Incoming/.DS_Store", "type": "file", "size": 1}
        ]
        result = await check_spec(self.vm, {"checks": [
            {"kind": "directory_contains_files", "path": "Incoming"}
        ]}, capability="files")
        self.assertEqual(result["status"], "fail")

    async def test_files_moved_by_extension_detects_hash_change(self):
        self.vm.directories["Incoming"] = []
        self.vm.directories["Organized/Text"] = [
            {"path": "Organized/Text/a.txt", "type": "file", "size": 5, "sha256": "wrong"}
        ]
        result = await check_spec(self.vm, {"checks": [
            {"kind": "files_moved_by_extension", "source_dir": "Incoming",
             "destination_dir": "Organized/Text", "extension": ".txt",
             "expected_files": [{"name": "a.txt", "size": 5, "sha256": "expected"}]}
        ]}, capability="files")
        self.assertEqual(result["status"], "fail")
        self.assertIn("SHA-256", result["evidence"])


    async def test_files_moved_by_extension_is_unknown_when_destination_hash_is_unavailable(self):
        self.vm.directories["Incoming"] = []
        self.vm.directories["Organized/Text"] = [
            {"path": "Organized/Text/a.txt", "type": "file", "size": 5}
        ]
        result = await check_spec(self.vm, {"checks": [
            {"kind": "files_moved_by_extension", "source_dir": "Incoming",
             "destination_dir": "Organized/Text", "extension": ".txt",
             "expected_files": [{"name": "a.txt", "size": 5, "sha256": "expected"}]}
        ]}, capability="files")
        self.assertEqual(result["status"], "unknown")
        self.assertIn("SHA-256 unavailable", result["detail"])

    async def test_directory_empty_passes_only_when_no_entries_remain(self):
        result = await check_spec(self.vm, {"checks": [
            {"kind": "directory_empty", "path": "FileSortingDemo/Incoming"},
        ]}, capability="files")
        self.assertEqual(result["status"], "pass")
        self.vm.directories["FileSortingDemo/Incoming"] = [
            {"path": "FileSortingDemo/Incoming/still-here.txt", "type": "file", "size": 3}
        ]
        result = await check_spec(self.vm, {"checks": [
            {"kind": "directory_empty", "path": "FileSortingDemo/Incoming"},
        ]}, capability="files")
        self.assertEqual(result["status"], "fail")

    async def test_files_moved_by_extension_checks_snapshot_names_and_sizes(self):
        result = await check_spec(self.vm, {"checks": [{
            "kind": "files_moved_by_extension",
            "source_dir": "FileSortingDemo/Incoming",
            "destination_dir": "FileSortingDemo/Organized/Text",
            "extension": ".txt",
            "expected_files": [
                {"name": "meeting-notes.txt", "size": 30},
                {"name": "weekly-report.txt", "size": 24},
            ],
        }]}, capability="files")
        self.assertEqual(result["status"], "pass")

    async def test_files_moved_by_extension_fails_when_original_remains(self):
        self.vm.directories["FileSortingDemo/Incoming"] = [
            {"path": "FileSortingDemo/Incoming/meeting-notes.txt", "type": "file", "size": 30}
        ]
        result = await check_spec(self.vm, {"checks": [{
            "kind": "files_moved_by_extension",
            "source_dir": "FileSortingDemo/Incoming",
            "destination_dir": "FileSortingDemo/Organized/Text",
            "extension": ".txt",
            "expected_files": [{"name": "meeting-notes.txt", "size": 30}],
        }]}, capability="files")
        self.assertEqual(result["status"], "fail")
        self.assertIn("still in source", result["evidence"])

    async def test_missing_destination_file_is_not_reported_as_size_changed(self):
        self.vm.directories["Incoming"] = []
        self.vm.directories["Organized/Text"] = []
        result = await check_spec(self.vm, {"checks": [{
            "kind": "files_moved_by_extension",
            "source_dir": "Incoming",
            "destination_dir": "Organized/Text",
            "extension": ".txt",
            "expected_files": [{"name": "missing.txt", "size": 28, "sha256": "abc"}],
        }]}, capability="files")
        self.assertEqual(result["status"], "fail")
        self.assertIn("missing at destination", result["detail"])
        self.assertNotIn("file sizes changed", result["detail"])
        self.assertNotIn("SHA-256 mismatch", result["detail"])

    async def test_files_moved_by_extension_detects_size_change(self):
        result = await check_spec(self.vm, {"checks": [{
            "kind": "files_moved_by_extension",
            "source_dir": "FileSortingDemo/Incoming",
            "destination_dir": "FileSortingDemo/Organized/Text",
            "extension": ".txt",
            "expected_files": [{"name": "meeting-notes.txt", "size": 999}],
        }]}, capability="files")
        self.assertEqual(result["status"], "fail")
        self.assertIn("file sizes changed", result["evidence"])


if __name__ == "__main__":
    unittest.main()
