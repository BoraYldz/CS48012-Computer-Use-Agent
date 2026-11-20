"""Plain-text normalization for tool outputs sent back into chat models."""
import json
from typing import Any


def serialize_tool_result(value: Any) -> str:
    """Return a tool observation as plain text, JSON-encoding structured values."""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if value is None:
        return "null"
    return value if isinstance(value, str) else str(value)


def decode_tool_result(value: Any) -> Any:
    """Decode JSON text returned by an MCP tool, preserving ordinary strings."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value
