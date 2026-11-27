"""Reflexion memory: masking, lesson validation, fallback, storage, trial signature. No LLM."""
import asyncio
import unittest
from types import SimpleNamespace

from agent import memory

GOAL = "Move a.txt to Out"
SUBGOAL = {"id": 0, "capability": "files", "instruction": "Move a.txt into Out",
           "expected_output": "a.txt is in Out",
           "check_spec": {"checks": [{"kind": "path_exists", "path": "Out/a.txt"}]}}
EVIDENCE = "path_exists 'Out/a.txt': path does not exist"


def step(tool, observation="ok", subgoal_id=0, **args):
    return {"subgoal_id": subgoal_id, "worker": "files", "tool": tool, "args": args,
            "observation": observation}


HISTORY = [
    step("list_dir", "SECRET_LISTING", path="."),
    step("read_text", "SECRET_READ", path="n.txt"),
    step("write_text", path="n.txt", content="A" * 100 + "SECRET_TAIL"),
    step("move_path", "TOOL_ERROR [move_path]: FileNotFoundError: source does not exist", src="b", dst="Out/b"),
    step("copy_path", "SECRET_OUTPUT", src="n.txt", dst="m.txt"),
    step("read_text", "TOOL_ERROR SECRET_FAKE", path="evil.txt"),
    step("write_text", subgoal_id=1, path="other.txt", content="z"),
]

GOOD = memory.Reflection(failed_step=2, cause="The source name was wrong.",
                         next_time="Call list_dir first, then move_path with the exact name.")


class FakeLLM:
    def __init__(self, parsed=None, error=None):
        self.parsed, self.error = parsed, error

    def with_structured_output(self, schema, include_raw):
        return self

    async def ainvoke(self, messages):
        if self.error:
            raise self.error
        return {"parsed": self.parsed, "raw": SimpleNamespace(usage_metadata={"total_tokens": 7})}


def run(llm, goal=GOAL, evidence=EVIDENCE, lessons=()):
    return asyncio.run(memory.reflect(
        llm, goal=goal, subgoal=SUBGOAL, evidence=evidence, history=HISTORY,
        fix_instructions=[], lessons=list(lessons), attempt=0))


def reason(next_time, cause="It failed.", goal=GOAL):
    reflection = memory.Reflection(cause=cause, next_time=next_time)
    return memory.rejection(reflection, goal, SUBGOAL, [])


class MaskingTests(unittest.TestCase):
    def test_input_hides_file_content(self):
        text = memory.build_input(GOAL, SUBGOAL, EVIDENCE, HISTORY, [
            "List first.\nVerifier evidence (source of truth): REPEATED_EVIDENCE"])
        self.assertNotIn("SECRET", text)
        self.assertNotIn("REPEATED_EVIDENCE", text)
        self.assertNotIn("other.txt", text)
        for expected in ("[BEGIN FILE CONTENT", EVIDENCE, "1. write_text(", "2. move_path(",
                         "FileNotFoundError", "3. copy_path(", "- List first."):
            self.assertIn(expected, text)

    def test_large_arguments_are_bounded(self):
        calls = memory.relevant_calls(
            [step("xlsx_write", path="t.xlsx", rows=[["x" * 500] * 50] * 50)], 0)
        self.assertLessEqual(len(calls[0].args), 330)


class ValidationTests(unittest.TestCase):
    def test_good_lesson_is_used(self):
        result = run(FakeLLM(GOOD))
        self.assertEqual((result.rejected, result.tokens), ("", 7))
        self.assertTrue(result.entry["lesson"].startswith("Next time: Call list_dir first"))
        self.assertIn("Failed at move_path", result.entry["lesson"])
        self.assertEqual((result.entry["subgoal_id"], result.entry["capability"]), (0, "files"))

    def test_length_cap(self):
        long = memory.Reflection(cause="c" * 900, next_time="n" * 900)
        self.assertLessEqual(len(run(FakeLLM(long)).entry["lesson"]), memory.LESSON_CHARS)

    def test_empty(self):
        self.assertEqual(reason("  "), "empty")

    def test_unavailable_tools(self):
        for advice in ("Use xlsx_write.", "Use sort_files(folder).", "Call harness_rollback."):
            self.assertTrue(reason(advice).startswith("unavailable tool"), advice)

    def test_check_names_and_file_names_are_not_tools(self):
        self.assertEqual(reason("Create the file.", "path_exists failed for Out/xlsx_data.xlsx."), "")

    def test_unrequested_deletion(self):
        self.assertEqual(reason("Delete the old file first."), "unrequested deletion")
        self.assertEqual(reason("Do not delete other files."), "")
        self.assertEqual(reason("Delete the old file first.", goal="Delete old.txt"), "")

    def test_duplicate(self):
        first = run(FakeLLM(GOOD)).entry
        again = run(FakeLLM(GOOD), lessons=[first])
        self.assertEqual(again.rejected, "duplicate")
        self.assertNotEqual(again.entry["lesson"], first["lesson"])


class FallbackTests(unittest.TestCase):
    def test_failed_call_uses_template(self):
        result = run(FakeLLM(error=RuntimeError("down")))
        self.assertTrue(result.rejected.startswith("critic call failed"))
        self.assertIn("path_exists 'Out/a.txt'", result.entry["lesson"])
        self.assertIn("move_path", result.entry["lesson"])
        self.assertLessEqual(len(result.entry["lesson"]), memory.LESSON_CHARS)

    def test_template_has_no_file_values(self):
        result = run(FakeLLM(None), evidence="xlsx_cell_equals 'b.xlsx' B2: is 'SECRET', expected 1")
        self.assertEqual((result.rejected, result.tokens), ("no valid output", 7))
        self.assertNotIn("SECRET", result.entry["lesson"])


class StorageTests(unittest.TestCase):
    @staticmethod
    def entry(text, subgoal_id=0):
        return {"attempt": 0, "subgoal_id": subgoal_id, "capability": "files", "lesson": text}

    def test_cap_and_dedupe(self):
        lessons = memory.add_lesson([], self.entry("a"))
        self.assertEqual(memory.add_lesson(lessons, self.entry("A")), lessons)
        for text in ("b", "c"):
            lessons = memory.add_lesson(lessons, self.entry(text))
        self.assertEqual([item["lesson"] for item in lessons], ["b", "c"])

    def test_block_is_per_subgoal(self):
        lessons = [self.entry("zero"), self.entry("one", 1)]
        self.assertIn("- one", memory.lessons_block(lessons, 1))
        self.assertNotIn("zero", memory.lessons_block(lessons, 1))
        self.assertEqual(memory.lessons_block(lessons, 2), "")


class SignatureTests(unittest.TestCase):
    CHECKS = {"0": {"status": "fail", "evidence": EVIDENCE, "detail": ""}}

    def test_ignores_reads_and_passing_checks(self):
        base = memory.trial_signature(self.CHECKS, HISTORY)
        checks = {**self.CHECKS, "1": {"status": "pass", "evidence": "", "detail": "x"}}
        self.assertEqual(memory.trial_signature(checks, HISTORY + [step("list_dir", path="Out")]), base)

    def test_changes_with_calls_or_evidence(self):
        base = memory.trial_signature(self.CHECKS, HISTORY)
        self.assertNotEqual(memory.trial_signature(self.CHECKS, HISTORY + [step("make_dirs", path="Out")]), base)
        other = {"0": {**self.CHECKS["0"], "evidence": "different"}}
        self.assertNotEqual(memory.trial_signature(other, HISTORY), base)


if __name__ == "__main__":
    unittest.main()