"""Regression tests for robust LangChain tool-message tracing."""
import unittest

from langchain_core.messages import ToolMessage

from agent.graph import _WorkerTraceHandler, _text


class _MemoryLog:
    def __init__(self, fail=False):
        self.records = []
        self.fail = fail

    def write(self, event, **data):
        if self.fail:
            raise OSError("simulated log disk failure")
        self.records.append((event, data))


class GraphTraceTests(unittest.TestCase):
    def test_text_extracts_plain_tool_message_content(self):
        message = ToolMessage(
            content='[{"path":"Incoming/a.txt","type":"file"}]',
            name="list_dir",
            tool_call_id="call-1",
        )
        self.assertEqual(_text(message), '[{"path":"Incoming/a.txt","type":"file"}]')

    def test_text_extracts_text_blocks_from_tool_message(self):
        message = ToolMessage(
            content=[{"type": "text", "text": "first"}, {"type": "text", "text": "second"}],
            name="list_dir",
            tool_call_id="call-2",
        )
        self.assertEqual(_text(message), "firstsecond")

    def test_tool_callback_records_tool_message_without_crashing(self):
        log = _MemoryLog()
        handler = _WorkerTraceHandler(log, subgoal_id=2, worker="files")
        handler.on_tool_start(
            {"name": "list_dir"},
            '{"path":"Incoming"}',
            run_id="call-3",
            inputs={"path": "Incoming"},
        )
        handler.on_tool_end(
            ToolMessage(content='[]', name="list_dir", tool_call_id="call-3"),
            run_id="call-3",
        )
        self.assertEqual(len(handler.steps), 1)
        self.assertEqual(handler.steps[0]["observation"], "[]")
        self.assertEqual([event for event, _ in log.records], ["tool_start", "step"])

    def test_callback_logging_failure_does_not_escape_callback(self):
        log = _MemoryLog(fail=True)
        handler = _WorkerTraceHandler(log, subgoal_id=3, worker="files")
        handler.on_tool_start(
            {"name": "list_dir"},
            '{"path":"Incoming"}',
            run_id="call-4",
            inputs={"path": "Incoming"},
        )
        handler.on_tool_end(
            ToolMessage(content='[]', name="list_dir", tool_call_id="call-4"),
            run_id="call-4",
        )
        self.assertEqual(len(handler.steps), 1)
        self.assertEqual(handler.steps[0]["observation"], "[]")
        self.assertEqual(len(handler.callback_errors), 2)


if __name__ == "__main__":
    unittest.main()
