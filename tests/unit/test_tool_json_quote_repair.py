"""Narrow tool JSON quote repair tests.

Only complete JSON objects with unescaped quotes inside an argument string may be repaired. A
truncated object must remain a malformed-tool notice.
"""
import json

from app.utils.tools import MALFORMED_TOOL_NOTICE, parse_tool_response


def test_complete_unescaped_quote_inside_argument_is_escaped_safely():
    raw = '{"status":"tool_use","tool_calls":[{"name":"exec","arguments":{"input":"const label = "hello";\nrun();"}}]}'
    diagnostics = {}

    parsed = parse_tool_response(raw, diagnostics=diagnostics)

    assert parsed["type"] == "tool_calls"
    assert parsed["tool_calls"][0]["arguments"]["input"] == 'const label = "hello";\nrun();'
    assert diagnostics["repaired"] is True
    assert diagnostics["failure_class"] == "unescaped_quote_repaired"
    assert diagnostics["response_length"] == len(raw)


def test_valid_escaped_quotes_are_not_changed():
    raw = json.dumps({
        "status": "tool_use",
        "tool_calls": [{"name": "exec", "arguments": {"input": 'const label = "hello";'}}],
    })
    diagnostics = {}

    parsed = parse_tool_response(raw, diagnostics=diagnostics)

    assert parsed["type"] == "tool_calls"
    assert diagnostics["repaired"] is False


def test_truncated_object_is_never_repaired():
    raw = '{"status":"tool_use","tool_calls":[{"name":"exec","arguments":{"input":"const label = "hello"'
    diagnostics = {}

    parsed = parse_tool_response(raw, diagnostics=diagnostics)

    assert parsed == {"type": "text", "content": MALFORMED_TOOL_NOTICE}
    assert diagnostics["repaired"] is False
    assert diagnostics["truncated_suspected"] is True
    assert diagnostics["failure_class"] == "truncated_json"


def test_plain_text_has_no_failure_class():
    diagnostics = {}
    parsed = parse_tool_response("Hello, this is regular assistant response.", diagnostics=diagnostics)
    assert parsed == {"type": "text", "content": "Hello, this is regular assistant response."}
    assert "failure_class" not in diagnostics
    assert diagnostics["repaired"] is False


def test_markdown_code_fence_json_has_no_failure_class():
    raw = '```json\n{"status":"tool_use","tool_calls":[{"name":"exec","arguments":{"input":"ls"}}]}\n```'
    diagnostics = {}
    parsed = parse_tool_response(raw, diagnostics=diagnostics)
    assert parsed["type"] == "tool_calls"
    assert "failure_class" not in diagnostics
    assert diagnostics["repaired"] is False
