"""Regression coverage for tool calls embedded in conversational Markdown output."""

import asyncio

from app.utils.tools import MALFORMED_TOOL_NOTICE, parse_tool_response, parse_tool_response_with_retry


_TOOL_JSON = (
    '{"status":"tool_use","tool_calls":[{"name":"run_shell",'
    '"arguments":{"cmd":"ls -la"}}]}'
)


def test_embedded_fenced_json_tool_call_is_extracted_from_prose():
    response = (
        "I will inspect the files now.\n\n"
        "```json\n"
        f"{_TOOL_JSON}\n"
        "```\n\n"
        "This is the command I plan to run."
    )

    parsed = parse_tool_response(response)

    assert parsed == {
        "type": "tool_calls",
        "tool_calls": [{"name": "run_shell", "arguments": {"cmd": "ls -la"}}],
    }


def test_embedded_fenced_tool_call_beats_unrelated_braces_in_prose():
    response = (
        'The documentation example is {"not": "a tool call"}.\n'
        "```json\n"
        f"{_TOOL_JSON}\n"
        "```\n"
    )

    parsed = parse_tool_response(response)

    assert parsed["type"] == "tool_calls"
    assert parsed["tool_calls"][0]["name"] == "run_shell"


def test_required_tool_choice_rejects_prose_and_retries_for_json():
    calls = 0

    async def regenerate():
        nonlocal calls
        calls += 1
        return _TOOL_JSON

    parsed = asyncio.run(parse_tool_response_with_retry(
        "I will run the command now.", regenerate, tool_choice="required"))

    assert calls == 1
    assert parsed["type"] == "tool_calls"
    assert parsed["tool_calls"][0]["name"] == "run_shell"


def test_required_tool_choice_rejects_prose_when_retry_stays_invalid():
    calls = 0

    async def regenerate():
        nonlocal calls
        calls += 1
        return "The command is running."

    parsed = asyncio.run(parse_tool_response_with_retry(
        "I will run the command now.", regenerate, tool_choice="required"))

    assert calls == 1
    assert parsed == {"type": "text", "content": MALFORMED_TOOL_NOTICE}
