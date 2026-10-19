"""Regression coverage for defending against Gemini Web internal code interpreter execution."""

import asyncio

from app.core.gemini_client import (
    strip_google_internal_artifacts,
    _stable_internal_artifact_prefix,
)
from app.utils.tools import (
    MALFORMED_TOOL_NOTICE,
    parse_tool_response,
    parse_tool_response_with_retry,
    build_tool_prompt,
)


_RAW_CODE_STDOUT = (
    "```python?code_reference&code_event_index=1\n"
    "import os\n"
    "path = r'C:\\test.html'\n"
    "print(os.path.exists(path))\n"
    "```\n\n"
    "```text?code_stdout&code_event_index=1\n"
    "File exists: False\n"
    "Files in dir: ['input_file_0.png']\n"
    "```\n\n"
    "The file does not exist locally."
)

_RAW_CODE_TRACEBACK = (
    "```text?code_stdout&code_event_index=1\n"
    "Traceback (most recent call last):\n"
    '  File "<xbox-string>", line 3, in <module>\n'
    "    f.write(html_content)\n"
    "NameError: name 'html_content' is not defined\n"
    "```\n"
)


def test_strip_google_internal_artifacts_removes_sandbox_stdout_blocks():
    cleaned = strip_google_internal_artifacts(_RAW_CODE_STDOUT)

    assert "code_stdout" not in cleaned
    assert "File exists: False" not in cleaned
    assert "input_file_0.png" not in cleaned
    assert "code_reference" not in cleaned
    assert "The file does not exist locally." in cleaned


def test_strip_google_internal_artifacts_removes_sandbox_traceback_blocks():
    cleaned = strip_google_internal_artifacts(_RAW_CODE_TRACEBACK)

    assert "code_stdout" not in cleaned
    assert "Traceback" not in cleaned
    assert "xbox-string" not in cleaned
    assert cleaned.strip() == ""


def test_stable_internal_artifact_prefix_holds_unclosed_stdout_fence():
    # Deliberately slice inside the ```text?code_stdout block
    unclosed = "Before\n```text?code_stdout&code_event_index=1\nFile exists: False\n"
    safe = _stable_internal_artifact_prefix(unclosed)

    assert "code_stdout" not in safe
    assert "File exists: False" not in safe
    assert safe.strip() == "Before"


def test_parse_tool_response_detects_internal_code_interpreter_and_flags_malformed():
    diagnostics = {}
    parsed = parse_tool_response(_RAW_CODE_STDOUT, diagnostics=diagnostics, tool_choice="auto")

    assert parsed == {"type": "text", "content": MALFORMED_TOOL_NOTICE}
    assert diagnostics["failure_class"] == "internal_code_interpreter_used"
    assert "internal code interpreter" in diagnostics["error"]


def test_parse_tool_response_allows_plain_text_when_tool_choice_none():
    diagnostics = {}
    parsed = parse_tool_response(_RAW_CODE_STDOUT, diagnostics=diagnostics, tool_choice="none")

    assert parsed["type"] == "text"
    assert parsed["content"] != MALFORMED_TOOL_NOTICE


def test_parse_tool_response_with_retry_sends_internal_interpreter_nudge():
    received_nudge = ""

    async def fake_regenerate(nudge: str = ""):
        nonlocal received_nudge
        received_nudge = nudge
        return (
            '{"status": "tool_use", "tool_calls": [{"name": "exec", '
            '"arguments": {"cmd": "ls -la"}}]}'
        )

    parsed = asyncio.run(parse_tool_response_with_retry(
        _RAW_CODE_STDOUT, fake_regenerate, tool_choice="auto"
    ))

    assert "internal code execution environment" in received_nudge
    assert "MANDATORY CORRECTION" in received_nudge
    assert parsed["type"] == "tool_calls"
    assert parsed["tool_calls"][0]["name"] == "exec"


def test_build_tool_prompt_contains_internal_code_interpreter_ban():
    tools = [{"type": "function", "function": {"name": "exec", "description": "Run shell command"}}]
    prompt = build_tool_prompt("fix all bugs", tools)

    assert "INTERNAL CODE INTERPRETER FORBIDDEN" in prompt
    assert "User files and shell tools exist exclusively on the local machine" in prompt


def test_exact_codex_session_messages_handled_and_stripped():
    # Exact snippet from MSG 5 of codex session 01a15445-54d2-7121-b5f8-af7aa35abc5d
    session_msg5 = (
        "```python?code_reference&code_event_index=1\n"
        "import os\n\n"
        'path = r"C:\\Users\\marek\\Documents\\Robco PhD\\elektroapp\\elektromagnetick_laborat_rium_matfyz.html"\n'
        'print("File exists:", os.path.exists(path))\n'
        "if not os.path.exists(path):\n"
        '    print("Files in dir:", os.listdir(".") if os.path.exists(".") else "None")\n\n'
        "```\n\n"
        "```text?code_stdout&code_event_index=1\n"
        "File exists: False\n"
        "Files in dir: ['input_file_0.png']\n\n"
        "```\n\n"
        "Na základe analýzy vykonám ďalšie kroky."
    )

    # 1. Verification of artifact and sandbox stdout stripping
    cleaned = strip_google_internal_artifacts(session_msg5)
    assert "code_stdout" not in cleaned
    assert "code_reference" not in cleaned
    assert "File exists: False" not in cleaned
    assert "input_file_0.png" not in cleaned
    assert "Na základe analýzy vykonám ďalšie kroky." in cleaned

    # 2. Verification that parse_tool_response flags this as an internal code interpreter attempt
    diag = {}
    parsed = parse_tool_response(session_msg5, diagnostics=diag, tool_choice="auto")
    assert parsed == {"type": "text", "content": MALFORMED_TOOL_NOTICE}
    assert diag["failure_class"] == "internal_code_interpreter_used"
