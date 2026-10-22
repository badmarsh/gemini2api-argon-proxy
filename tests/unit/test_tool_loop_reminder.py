"""End-of-prompt tool-loop reminder (app/utils/tools.py).

The community fix for "the agent stalls after 1-2 tool calls" is to restate the rules at the *end* of the
prompt: a long turn pushes a prefix-only instruction out of the model's attention, and the last thing it reads
before answering is what it follows.
"""
from app.config import settings
from app.utils.tools import TOOL_LOOP_REMINDER, build_tool_prompt

_TOOL = [{"type": "function", "function": {"name": "run", "description": "runs",
                                           "parameters": {"type": "object"}}}]


def test_reminder_is_appended_at_the_very_end():
    prompt = build_tool_prompt("Human: do the thing", _TOOL)
    assert TOOL_LOOP_REMINDER in prompt
    assert prompt.endswith(TOOL_LOOP_REMINDER), "the reminder must be the last thing the model reads"
    assert prompt.index("User message:") < prompt.index(TOOL_LOOP_REMINDER)


def test_reminder_forbids_promises_and_demands_a_tool_call():
    prompt = build_tool_prompt("Human: do the thing", _TOOL)
    low = prompt.lower()
    assert "tool_call" in low
    assert "promise" in low and "progress report" in low


def test_reminder_forbids_refusals_and_simulation():
    prompt = build_tool_prompt("Human: do the thing", _TOOL)
    low = prompt.lower()
    assert "simulate code execution" in low
    assert "lack filesystem access" in low or "claim you cannot" in low
    assert "question you truly cannot answer" not in low, "loophole must be removed"


def test_reminder_is_inert_without_tools():
    assert build_tool_prompt("Human: hi", []) == "Human: hi"


def test_reminder_respects_tool_choice_none():
    """"none" means the caller explicitly wants text only — a tool reminder would contradict it."""
    prompt = build_tool_prompt("Human: hi", _TOOL, tool_choice="none")
    assert TOOL_LOOP_REMINDER not in prompt
    assert "Do NOT use any tools" in prompt


def test_reminder_can_be_disabled_by_setting(monkeypatch):
    monkeypatch.setattr(settings, "tool_loop_reminder", False)
    prompt = build_tool_prompt("Human: hi", _TOOL)
    assert TOOL_LOOP_REMINDER not in prompt
