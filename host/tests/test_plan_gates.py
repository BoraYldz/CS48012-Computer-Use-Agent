import asyncio
import unittest

from agent import plan_gates as g


def sg(i, instr, checks):
    return {"id": i, "capability": "files", "instruction": instr,
            "check_spec": {"checks": checks, "rationale": ""}}


class GateATests(unittest.TestCase):
    def test_invented_deletion_check_and_subgoal_dropped(self):
        plan = [sg(0, "Rename", [{"kind": "file_ops"}]), sg(1, "Verify", [{"kind": "no_files_by_extension"}])]
        ch = g.drop_unrequested_deletion_checks("Rename every .txt file by adding _old", plan)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["id"], 0)
        self.assertTrue(ch)

    def test_requested_deletion_kept(self):
        plan = [sg(0, "Delete", [{"kind": "no_files_by_extension"}])]
        self.assertEqual(g.drop_unrequested_deletion_checks("Delete all .tmp files", plan), [])
        self.assertEqual(len(plan[0]["check_spec"]["checks"]), 1)

    def test_single_subgoal_kept_even_if_check_dropped(self):
        plan = [sg(0, "x", [{"kind": "no_files_by_extension"}])]
        g.drop_unrequested_deletion_checks("Rename things", plan)
        self.assertEqual(len(plan), 1)


class GateBTests(unittest.TestCase):
    def test_made_up_text_downgraded(self):
        plan = [sg(0, "w", [{"kind": "text_contains", "path": "a.txt", "text": "The CSV has north and south"}])]
        ch = g.ground_free_text_checks("write one sentence into a.txt", plan)
        self.assertEqual(plan[0]["check_spec"]["checks"][0]["kind"], "path_exists")
        self.assertTrue(ch)

    def test_text_from_request_kept(self):
        plan = [sg(0, "w", [{"kind": "text_equals", "path": "a.txt", "text": "Hello  World"}])]
        ch = g.ground_free_text_checks("Write 'hello world' into a.txt", plan)
        self.assertEqual(plan[0]["check_spec"]["checks"][0]["kind"], "text_equals")
        self.assertEqual(ch, [])


class GateCTests(unittest.TestCase):
    def test_read_only_plan_for_write_request(self):
        plan = [sg(0, "Read the content of invoice.txt.", [{"kind": "path_exists"}])]
        self.assertIsNotNone(g.state_change_gap("Read a.txt and write a note into b.txt", plan))
        g.neutralize_plan(plan, "gap")
        self.assertEqual(plan[0]["check_spec"]["checks"], [])

    def test_plan_with_write_subgoal_ok(self):
        plan = [sg(0, "Write a note into b.txt", [{"kind": "path_exists"}])]
        self.assertIsNone(g.state_change_gap("Read a.txt and write a note into b.txt", plan))

    def test_pure_read_request_ok(self):
        plan = [sg(0, "Read a.txt", [{"kind": "path_exists"}])]
        self.assertIsNone(g.state_change_gap("Tell me what a.txt contains", plan))


class FakeVM:
    def __init__(self, existing):
        self.existing = set(existing)

    async def call(self, name, **kw):
        return {"exists": kw["path"] in self.existing}


class GateDTests(unittest.TestCase):
    def run_gate(self, goal, existing):
        plan = [sg(0, "Copy", [{"kind": "file_ops", "op": "copy", "source_dir": "w/Ledger",
                                "destination_dir": "w/Backup", "extension": ".csv"}])]
        ch = asyncio.run(g.precheck_named_sources(FakeVM(existing), goal, plan))
        return plan, ch

    def test_missing_named_source_makes_subgoal_failing(self):
        plan, ch = self.run_gate("Copy w/Ledger/ledger_2025.csv into w/Backup.", {"w/Ledger/ledger_2024.csv"})
        self.assertTrue(ch)
        self.assertEqual(plan[0]["check_spec"]["checks"][0]["path"], "w/Ledger/ledger_2025.csv")

    def test_existing_named_source_untouched(self):
        plan, ch = self.run_gate("Copy w/Ledger/ledger_2024.csv into w/Backup.", {"w/Ledger/ledger_2024.csv"})
        self.assertEqual(ch, [])
        self.assertEqual(plan[0]["check_spec"]["checks"][0]["kind"], "file_ops")

    def test_example_filenames_without_directory_ignored(self):
        plan, ch = self.run_gate("Copy csv files in w/Ledger (for example x.csv becomes y.csv).", set())
        self.assertEqual(ch, [])


if __name__ == "__main__":
    unittest.main()
