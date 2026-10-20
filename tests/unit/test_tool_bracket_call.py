"""Regression for the Codex incident: the model imitated the prompt-history rendering
``[Tool call: NAME(ARGS_JSON)]`` instead of the JSON envelope. The raw string used to reach the client as a
final answer (finish_reason=stop) and ended the agent turn.

Contract pinned here:
* a reply that is exactly one or more complete ``[Tool call: ...]`` renderings is converted to tool calls;
* anything else that *opens* with that rendering is never forwarded - it takes the malformed path (one
  regeneration with a correction nudge);
* prose that merely mentions the pattern is left alone;
* a truncated call is never repaired (same rule as truncated JSON).
"""

import asyncio
import json

from app.utils.prompt import build_prompt_from_messages
from app.utils.tools import (
    MALFORMED_TOOL_NOTICE,
    is_malformed_tool_result,
    parse_tool_response,
    parse_tool_response_with_retry,
)

VALID_JSON = '{"status":"tool_use","tool_calls":[{"name":"exec","arguments":{"input":"ok"}}]}'

# Shaped like the incident: JS code with parens, brackets, quotes and newlines inside one JSON string.
INCIDENT_ARGS = {
    "input": 'const read1 = await tools.mcp__codex_app__read_thread({threadId: "01a15711", turnLimit: 1});\n'
             'text(JSON.stringify({read1}));'
}
INCIDENT = f"[Tool call: exec({json.dumps(INCIDENT_ARGS)})]"


def _run(coro):
    return asyncio.run(coro)


class _Regen:
    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = 0
        self.nudges = []

    async def __call__(self, nudge=""):
        self.calls += 1
        self.nudges.append(nudge)
        return self.texts.pop(0) if self.texts else ""


def test_incident_reply_is_converted_to_a_tool_call():
    parsed = parse_tool_response(INCIDENT)
    assert parsed == {"type": "tool_calls", "tool_calls": [{"name": "exec", "arguments": INCIDENT_ARGS}]}


def test_parens_and_brackets_inside_string_arguments_do_not_end_the_call_early():
    args = {"input": 'f(a)] g( ")]" ) [Tool call: fake({})]'}
    parsed = parse_tool_response(f"[Tool call: exec({json.dumps(args)})]")
    assert parsed["type"] == "tool_calls"
    assert parsed["tool_calls"] == [{"name": "exec", "arguments": args}]


def test_multiple_calls_are_all_recovered_in_order():
    text = '[Tool call: a({"x": 1})]\n[Tool call: b({})]\n\n[Tool call: c()]'
    parsed = parse_tool_response(text)
    assert [c["name"] for c in parsed["tool_calls"]] == ["a", "b", "c"]
    assert parsed["tool_calls"][0]["arguments"] == {"x": 1}
    assert parsed["tool_calls"][2]["arguments"] == {}


def test_fenced_and_whitespace_padded_call_is_recovered():
    parsed = parse_tool_response('```\n  [Tool call: run( {"cmd": "ls"} ) ]\n```')
    assert parsed == {"type": "tool_calls", "tool_calls": [{"name": "run", "arguments": {"cmd": "ls"}}]}


def test_arguments_that_look_like_an_envelope_do_not_hijack_the_call():
    """Inner {"name","arguments"} keys must stay arguments; the outer name wins (step 0 runs first)."""
    args = {"name": "evil", "arguments": {"a": 1}}
    parsed = parse_tool_response(f"[Tool call: exec({json.dumps(args)})]")
    assert parsed["tool_calls"] == [{"name": "exec", "arguments": args}]


def test_truncated_call_is_malformed_and_never_repaired():
    truncated = '[Tool call: exec({"input":"await tools.secret_tool({'
    diagnostics = {}
    parsed = parse_tool_response(truncated, diagnostics=diagnostics)
    assert parsed == {"type": "text", "content": MALFORMED_TOOL_NOTICE}
    assert is_malformed_tool_result(parsed)
    assert diagnostics["failure_class"] == "bracket_tool_call_unparsed"
    assert diagnostics["truncated_suspected"] is True
    assert "secret_tool" not in json.dumps(parsed)


def test_call_followed_by_prose_is_not_executed_and_not_leaked():
    parsed = parse_tool_response('[Tool call: exec({"input": "rm -rf x"})]\nI will wait for the result.')
    assert parsed == {"type": "text", "content": MALFORMED_TOOL_NOTICE}


def test_non_object_arguments_are_rejected():
    assert parse_tool_response("[Tool call: x([1, 2])]") == {"type": "text", "content": MALFORMED_TOOL_NOTICE}
    assert parse_tool_response('[Tool call: x("str")]') == {"type": "text", "content": MALFORMED_TOOL_NOTICE}


def test_missing_closing_bracket_is_rejected():
    assert is_malformed_tool_result(parse_tool_response('[Tool call: x({"a": 1})'))
    assert is_malformed_tool_result(parse_tool_response('[Tool call: x({"a": 1}]'))


def test_prose_that_only_mentions_the_pattern_is_left_untouched():
    text = "History is rendered as [Tool call: x({})] for past calls; that is just a description."
    assert parse_tool_response(text) == {"type": "text", "content": text}
    quoted = 'Here is the call:\n[Tool call: x({"a": 1})]'
    assert parse_tool_response(quoted) == {"type": "text", "content": quoted}


def test_json_envelope_and_plain_text_are_unchanged():
    assert parse_tool_response(VALID_JSON)["tool_calls"][0]["name"] == "exec"
    assert parse_tool_response("just text") == {"type": "text", "content": "just text"}


def test_valid_bracket_call_satisfies_required_tool_choice_without_retry():
    regen = _Regen(VALID_JSON)
    parsed = _run(parse_tool_response_with_retry(INCIDENT, regen, tool_choice="required"))
    assert parsed["type"] == "tool_calls"
    assert regen.calls == 0


def test_unparseable_bracket_call_retries_once_with_a_format_nudge():
    regen = _Regen(VALID_JSON)
    parsed = _run(parse_tool_response_with_retry('[Tool call: exec({"input": "x"})] trailing', regen))
    assert parsed["type"] == "tool_calls"
    assert regen.calls == 1
    assert "MANDATORY CORRECTION" in regen.nudges[0]
    assert '"status": "tool_use"' in regen.nudges[0]


def test_retry_that_repeats_the_bracket_form_still_never_leaks_it():
    regen = _Regen('[Tool call: exec({"input": "x"})] again')
    parsed = _run(parse_tool_response_with_retry('[Tool call: exec({"input": "x"})] first', regen))
    assert parsed == {"type": "text", "content": MALFORMED_TOOL_NOTICE}
    assert regen.calls == 1


def test_round_trip_with_the_history_renderer():
    """What prompt.py renders for a past call must parse back to the same call."""
    arguments = json.dumps({"input": "text(JSON.stringify({a: [1, 2]}))\n// ) ]"})
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "exec", "arguments": arguments}}]},
    ]
    prompt = build_prompt_from_messages(messages)
    rendered = prompt.split("Assistant: ", 1)[1]
    parsed = parse_tool_response(rendered)
    assert parsed["tool_calls"] == [{"name": "exec", "arguments": json.loads(arguments)}]
