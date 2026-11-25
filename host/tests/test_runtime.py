"""Tests for OpenAI-compatible tool-result adaptation."""
import json
import unittest

from agent.tool_output import decode_tool_result, serialize_tool_result


class ToolOutputTests(unittest.TestCase):
    def test_structured_worker_tool_outputs_are_plain_json_text(self):
        value = [{"path": "Incoming/a.txt", "type": "file", "size": 4}]
        encoded = serialize_tool_result(value)
        self.assertIsInstance(encoded, str)
        self.assertEqual(json.loads(encoded), value)

    def test_structured_mcp_result_is_decoded_for_checkers(self):
        expected = [{"path": "Incoming/a.txt", "type": "file", "size": 4}]
        self.assertEqual(decode_tool_result(json.dumps(expected)), expected)

    def test_regular_text_result_is_preserved(self):
        self.assertEqual(serialize_tool_result("ok"), "ok")
        self.assertEqual(decode_tool_result("ok"), "ok")

    def test_none_result_becomes_plain_text(self):
        self.assertEqual(serialize_tool_result(None), "null")


if __name__ == "__main__":
    unittest.main()
