"""Behavioral XSS regression tests for dynamic admin-console innerHTML renderers."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is required for browser-renderer tests")


def _extract_function(source: str, name: str) -> str:
    head = source.index(f"function {name}(")
    if source[max(0, head - 6):head] == "async ":
        head -= 6
    start = source.index("{", head)
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[head:index + 1]
    raise AssertionError(f"could not extract {name}")


def _run_node(tmp_path: Path, script_text: str, payload: dict) -> dict:
    script = tmp_path / "xss-renderers.mjs"
    script.write_text(script_text, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(script), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
def test_logs_gems_and_babysit_escape_hostile_api_values(tmp_path):
    utils = (ROOT / "static/app/utils.js").read_text(encoding="utf-8")
    logs = (ROOT / "static/app/logs.js").read_text(encoding="utf-8")
    gems = (ROOT / "static/app/gems.js").read_text(encoding="utf-8")
    babysit = (ROOT / "static/app/babysit.js").read_text(encoding="utf-8")

    for name, source in (("logs", logs), ("gems", gems), ("babysit", babysit)):
        assert "function escapeAttr(" in source, f"{name}.js lost attribute-context escaping"

    functions = [
        _extract_function(utils, "escapeHtml"),
        _extract_function(logs, "escapeAttr"),
        _extract_function(logs, "getStatusClass"),
        _extract_function(logs, "truncatePath"),
        _extract_function(logs, "renderTable"),
        # gems.js has its own explicit attribute-context helper.
        _extract_function(gems, "modelNameForGem"),
        _extract_function(gems, "render"),
        _extract_function(babysit, "getToolMeta"),
        _extract_function(babysit, "renderTasks"),
        _extract_function(babysit, "renderTrace"),
        _extract_function(babysit, "renderFindings"),
    ]

    script = "\n".join([
        "const input = JSON.parse(process.argv[2]);",
        "const payload = input.payload;",
        "function t() { return payload; }",
        "let selectedRecordId = null;",
        "let currentAccount = 'account-1';",
        "let currentMappings = {};",
        "function bindRowEvents() {}",
        "const elements = {};",
        "function element(id) { return elements[id] ||= { innerHTML: '', style: {}, querySelectorAll() { return []; } }; }",
        "const document = { getElementById: element, querySelectorAll() { return []; } };",
        *functions,
        "renderTable([input.log]);",
        "render([input.gem], input.mappings);",
        "renderTasks([input.task], []);",
        "renderTrace(input.trace);",
        "renderFindings([input.finding]);",
        "process.stdout.write(JSON.stringify({",
        "  logs: element('logs-tbody').innerHTML,",
        "  gems: element('gem-list-container').innerHTML,",
        "  tasks: element('bb-task-list').innerHTML,",
        "  trace: element('bb-trace-log').innerHTML,",
        "  findings: element('bb-findings-feed').innerHTML",
        "}));",
    ])

    attack = '\"><img src=x onerror="globalThis.pwned=1">'
    data = {
        "payload": attack,
        "log": {
            "id": attack, "ts": attack, "direction": attack, "method": attack,
            "path": attack, "status": attack, "latency_ms": 1, "ttft_ms": 2,
            "served_by": attack, "model": attack,
        },
        "gem": {"id": attack, "name": attack, "description": attack},
        "mappings": {attack: {"gem_id": attack, "account_id": "account-1"}},
        "task": {
            "task_id": attack, "title": attack, "prompt": attack, "category": attack,
            "difficulty": attack, "verification_command": attack,
            "expected_behavior": {"success_criteria": [attack], "timeout_seconds": attack},
            "tools": [attack],
        },
        "trace": [
            {
                "task_id": attack,
                "run_id": attack,
                "turns": [{
                    "turn": attack,
                    "proxy_telemetry": {"status": attack},
                    "response": {
                        "choices": [{
                            "finish_reason": attack,
                            "message": {
                                "content": attack,
                                "tool_calls": [{"function": {"name": attack, "arguments": attack}}],
                            },
                        }],
                        "error": {"message": attack},
                    },
                }],
            },
        ],
        "finding": {
            "severity": attack, "summary": attack, "description": attack,
            "category": attack, "finding_id": attack, "task_id": attack,
            "proposed_fix": attack, "root_cause_hypothesis": attack,
            "evidence": {"quoted_log": attack},
        },
    }

    rendered = _run_node(tmp_path, script, data)
    for renderer, html in rendered.items():
        assert attack not in html, f"{renderer} inserted an API value without escaping"
        assert "<img src=x" not in html, f"{renderer} produced executable attacker markup"
        assert "&lt;img" in html, f"{renderer} test payload did not reach the rendered output"
