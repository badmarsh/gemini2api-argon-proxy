"""Execution-tool prompts must not end the async isolate before work completes."""

from app.utils.tools import build_tool_prompt


_TOOLS = [
    {"type": "function", "function": {
        "name": "run_shell", "description": "Execute shell commands in the JavaScript V8 sandbox.",
        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
    }},
]


def test_execution_tools_require_directly_awaited_promises_and_forbid_unawaited_wrapper():
    prompt = build_tool_prompt("inspect the project", _TOOLS)
    lowered = prompt.lower()

    assert "javascript/v8 and shell execution tools" in lowered
    assert "top-level promises" in lowered
    assert "directly awaited" in lowered
    assert "never use an unawaited" in lowered
    assert "(async () => { ... })()" in prompt
    assert "top-level await" in lowered
