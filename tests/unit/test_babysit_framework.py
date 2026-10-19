"""Comprehensive unit tests for the enhanced Babysit framework.

Covers:
1. AgentSandbox: safe isolation, fixture seeding, robust python_eval, artifact verification.
2. Semantic Invariants: refusal leaks, code interpreter leaks, prose vs tools, repetition loops, streaming.
3. Runner utilities: extract_json, multimodal payloads, responses protocol mapping.
4. Schema conformance: task.schema.json and finding.schema.json.
"""

import json
import sys
from pathlib import Path

import pytest

# Ensure docs/babysit is importable
_BABYSIT_DIR = Path(__file__).resolve().parents[2] / "docs" / "babysit"
sys.path.insert(0, str(_BABYSIT_DIR))

from sandbox import AgentSandbox, SandboxViolationError
from invariants import (
    check_refusal_leak,
    check_code_interpreter_leak,
    check_prose_refusal_instead_of_tool,
    check_tool_repetition_loop,
    check_streaming_invariants,
    run_all_semantic_invariants,
)
from runner import extract_json, BabysitRunner


# ============================================================================
# 1. Sandbox Tests
# ============================================================================

def test_sandbox_seed_and_file_ops(tmp_path):
    sandbox = AgentSandbox(tmp_path)
    
    # Pre-seed files
    fixtures = [
        {"path": "data/manifest.json", "content": '{"sku_count": 10}'},
        {"path": "src/script.py", "content": "print('hello from sandbox')"},
    ]
    res = sandbox.seed_files(fixtures)
    assert res["status"] == "ok"
    assert res["seeded_count"] == 2

    # Verify read
    read_res = sandbox.read_file("data/manifest.json")
    assert read_res["status"] == "ok"
    assert '{"sku_count": 10}' in read_res["content"]

    # Verify list_dir
    list_res = sandbox.list_dir("data")
    assert list_res["status"] == "ok"
    assert any(e["name"] == "manifest.json" for e in list_res["entries"])


def test_sandbox_path_traversal_protection(tmp_path):
    sandbox = AgentSandbox(tmp_path)

    # Attempting to escape sandbox must fail
    with pytest.raises(SandboxViolationError):
        sandbox._resolve_safe("../outside.txt")

    with pytest.raises(SandboxViolationError):
        sandbox._resolve_safe("../../etc/passwd")

    # Safe prefix stripping
    p1 = sandbox._resolve_safe("/sandbox/data/test.csv")
    assert p1 == (tmp_path / "data" / "test.csv").resolve()

    p2 = sandbox._resolve_safe("/workspace/src/main.py")
    assert p2 == (tmp_path / "src" / "main.py").resolve()


def test_sandbox_eval_python_clean_execution(tmp_path):
    sandbox = AgentSandbox(tmp_path)
    
    # Clean execution without shell escaping bugs
    code = """
import json
data = [1, 2, 3]
print(json.dumps({'sum': sum(data), 'status': 'ok'}))
"""
    res = sandbox.eval_python(code)
    assert res["status"] == "ok"
    assert res["exit_code"] == 0
    parsed = json.loads(res["stdout"].strip())
    assert parsed["sum"] == 6

    # Syntax error returns stderr and exit code != 0 without python runner crashing
    bad_code = "def bad_syntax(:\n    pass"
    res_err = sandbox.eval_python(bad_code)
    assert res_err["status"] == "ok"
    assert res_err["exit_code"] != 0
    assert "SyntaxError" in res_err["stderr"]


def test_sandbox_verify_artifact(tmp_path):
    sandbox = AgentSandbox(tmp_path)

    # Missing artifact
    v1 = sandbox.verify_artifact("output/report.json")
    assert v1["exists"] is False
    assert v1["valid"] is False

    # Valid artifact creation
    sandbox.write_file("output/report.json", '{"status": "success", "count": 42}')

    v2 = sandbox.verify_artifact(
        "output/report.json",
        min_size=10,
        must_contain=["success", "count"],
        is_json=True,
    )
    assert v2["exists"] is True
    assert v2["valid"] is True

    # Malformed JSON check
    sandbox.write_file("output/bad.json", "not valid json {")
    v3 = sandbox.verify_artifact("output/bad.json", is_json=True)
    assert v3["exists"] is True
    assert v3["valid"] is False
    assert "Invalid JSON" in v3["error"]


# ============================================================================
# 2. Semantic Invariants Tests
# ============================================================================

def test_check_refusal_leak():
    # Google Drive refusal leak
    text_drive = "Sure. First, I'll need your permission to turn on the Google Drive app."
    res_drive = check_refusal_leak(text_drive)
    assert res_drive is not None
    assert "google drive" in res_drive["matched_phrase"]

    # Sandbox filesystem denial
    text_fs = "I do not have access to the local sandbox filesystem (/sandbox/data/) so I cannot read it."
    res_fs = check_refusal_leak(text_fs)
    assert res_fs is not None
    assert "local sandbox filesystem" in res_fs["matched_phrase"]

    # Elicitations UI leak
    text_elicitation = '<ElicitationsGroup message="How would you like to proceed?">'
    res_el = check_refusal_leak(text_elicitation)
    assert res_el is not None
    assert "<elicitationsgroup" in res_el["matched_phrase"]

    # Clean response
    clean_text = "I have read the file and calculated the statistics."
    assert check_refusal_leak(clean_text) is None


def test_check_code_interpreter_leak():
    # Model leaking internal python?code_reference
    leak_text = 'Let us execute python?code_reference&code_event_index=1 to inspect results.'
    res = check_code_interpreter_leak(leak_text)
    assert res is not None
    assert "code_reference" in res["matched_indicator"]

    # Code stdout leak
    leak_stdout = 'Observation from internal sandbox: [code_stdout] 42 [/code_stdout]'
    res_so = check_code_interpreter_leak(leak_stdout)
    assert res_so is not None
    assert "code_stdout" in res_so["matched_indicator"]

    # Clean text
    assert check_code_interpreter_leak("Standard output of command: OK") is None


def test_check_prose_refusal_instead_of_tool():
    msg_refusal = {
        "role": "assistant",
        "content": "I do not have access to the specified execution tools in this environment.",
        "tool_calls": None,
    }
    res = check_prose_refusal_instead_of_tool(msg_refusal, tools_available=True, category="tool_use")
    assert res is not None
    assert "conversational refusal prose" in res["description"]

    # Legitimate tool call should NOT trigger
    msg_tool = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_1", "function": {"name": "read_file", "arguments": "{}"}}],
    }
    assert check_prose_refusal_instead_of_tool(msg_tool, tools_available=True, category="tool_use") is None


def test_check_tool_repetition_loop():
    # 2 identical turns
    turn1 = {
        "response": {
            "choices": [{
                "message": {
                    "tool_calls": [{"function": {"name": "python_eval", "arguments": '{"code": "print(1)"}'}}]
                }
            }]
        }
    }
    turn2 = {
        "response": {
            "choices": [{
                "message": {
                    "tool_calls": [{"function": {"name": "python_eval", "arguments": '{"code": "print(1)"}'}}]
                }
            }]
        }
    }
    loop = check_tool_repetition_loop([turn1, turn2], threshold=2)
    assert loop is not None
    assert loop["repetitions"] == 2
    assert "python_eval" in loop["repeated_call"]

    # Different calls
    turn3 = {
        "response": {
            "choices": [{
                "message": {
                    "tool_calls": [{"function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}]
                }
            }]
        }
    }
    assert check_tool_repetition_loop([turn1, turn3], threshold=2) is None


def test_check_streaming_invariants():
    # Early refusal leak
    chunks = [
        "Sure. First, ",
        "I'll need your permission to turn on the Google Drive app.",
        " How can I help?",
    ]
    issues = check_streaming_invariants(chunks, ttft_ms=500.0, max_gap_ms=100.0)
    assert any(i["type"] == "early_stream_refusal_leak" for i in issues)

    # Inter-chunk stall
    stall_issues = check_streaming_invariants(["chunk1", "chunk2"], ttft_ms=1000.0, max_gap_ms=75_000.0)
    assert any(i["type"] == "stream_chunk_stall" for i in stall_issues)


def test_run_all_semantic_invariants_full_suite(tmp_path):
    sandbox = AgentSandbox(tmp_path)
    task = {
        "task_id": "task_tu_demo_01",
        "category": "tool_use",
        "tools": [{"type": "function", "function": {"name": "read_file"}}],
        "expected_behavior": {
            "expected_artifacts": ["output/result.json"]
        }
    }

    # Trace with a refusal leak in turn 1 and missing artifact
    turn1 = {
        "turn": 1,
        "timestamp": "2026-10-19T20:00:00Z",
        "response": {
            "choices": [{
                "message": {
                    "content": "I do not have access to the local sandbox filesystem.",
                    "tool_calls": None,
                }
            }]
        },
        "proxy_telemetry": {"request_id": "req-123"}
    }

    findings = run_all_semantic_invariants(
        task=task,
        turns_history=[turn1],
        sandbox=sandbox,
    )

    types = [f.get("invariant_type") for f in findings]
    assert "leaked_refusal_phrase" in types
    assert "prose_refusal_instead_of_tool" in types
    assert "artifact_missing" in types


# ============================================================================
# 3. Runner Utilities & Schema Tests
# ============================================================================

def test_extract_json():
    # Raw JSON
    assert extract_json('{"key": "value"}') == {"key": "value"}

    # Markdown code fence
    md = "Here is the plan:\n```json\n{\"task\": {\"title\": \"audit\"}}\n```"
    assert extract_json(md) == {"task": {"title": "audit"}}

    # Embedded object with leading/trailing chatter
    chatter = "Result: {\"status\": 200, \"items\": [1, 2]} thanks!"
    assert extract_json(chatter) == {"status": 200, "items": [1, 2]}


def test_multimodal_message_preparation(tmp_path):
    sandbox = AgentSandbox(tmp_path)
    img_path = tmp_path / "test_chart.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\nfakeimagebytes")

    runner = BabysitRunner(base_url="http://127.0.0.1:5918", run_id="test_run")
    
    # Message without multimodal
    res1 = runner.prepare_multimodal_message("Inspect file", None, sandbox)
    assert res1 == "Inspect file"

    # Message with local image path
    mm_input = {"type": "image", "image_path": "test_chart.png", "detail": "high"}
    res2 = runner.prepare_multimodal_message("Analyze this chart", mm_input, sandbox)
    assert isinstance(res2, list)
    assert res2[0]["text"] == "Analyze this chart"
    assert res2[1]["type"] == "image_url"
    assert res2[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_task_schema_validation():
    import jsonschema

    schema_file = _BABYSIT_DIR / "task.schema.json"
    schema = json.loads(schema_file.read_text(encoding="utf-8"))

    sample_task = {
        "task_id": "task_streaming_reconciliation_01",
        "category": "streaming_imperative",
        "difficulty": "medium",
        "protocol": "responses",
        "stream": True,
        "title": "Streamed Log Processing",
        "prompt": "Process data/logs.csv and generate output/summary.json",
        "initial_files": [
            {"path": "data/logs.csv", "content": "ts,err\n2026-10-19,500\n"}
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "run_command",
                    "description": "Run shell command",
                    "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
                },
            }
        ],
        "expected_behavior": {
            "success_criteria": ["summary.json generated"],
            "timeout_seconds": 360,
            "expected_artifacts": [
                {"path": "output/summary.json", "min_size": 5, "is_json": True}
            ],
        },
    }

    # Must validate cleanly without exception
    jsonschema.validate(instance=sample_task, schema=schema)


def test_finding_schema_validation():
    import jsonschema

    schema_file = _BABYSIT_DIR / "finding.schema.json"
    schema = json.loads(schema_file.read_text(encoding="utf-8"))

    sample_finding = {
        "finding_id": "finding_sem_task_01_a1b2c3",
        "category": "semantic_invariant",
        "invariant_type": "leaked_refusal_phrase",
        "severity": "error",
        "confidence": 0.98,
        "summary": "Turn 1: Model output leaked Google Drive/Workspace refusal",
        "evidence": {
            "request_id": "req-987",
            "timestamp": "2026-10-19T21:00:00Z",
            "quoted_log": "First, I'll need your permission to turn on the Google Drive app",
            "metric_name": "turn_index",
            "metric_value": 1,
            "task_id": "task_01",
        },
        "root_cause_hypothesis": "Model confused local paths with Google Drive app permissions.",
        "proposed_fix": "Prefix prompts with local workspace imperative context.",
        "created_at": "2026-10-19T21:00:05Z",
        "task_id": "task_01",
    }

    # Must validate cleanly without exception
    jsonschema.validate(instance=sample_finding, schema=schema)


# ============================================================================
# 4. Regression Verification on Past Experiment Traces (exp01 & exp02)
# ============================================================================

def test_past_exp01_refusal_detection():
    """Verify that invariants detect the Google Drive/filesystem refusal in exp01 task 1."""
    trace_file = Path(__file__).resolve().parents[2] / "runs" / "20261019-exp01" / "argon_trace.jsonl"
    if not trace_file.is_file():
        pytest.skip("exp01 trace file not found")

    with open(trace_file, encoding="utf-8") as f:
        first_trace = json.loads(f.readline())

    task = {
        "task_id": first_trace["task_id"],
        "category": "tool_use",
        "tools": [{"type": "function", "function": {"name": "read_file"}}],
    }

    findings = run_all_semantic_invariants(task=task, turns_history=first_trace.get("turns", []))
    types = [f.get("invariant_type") for f in findings]

    # Must detect both the refusal leak (Drive/local filesystem phrase) and prose refusal
    assert "leaked_refusal_phrase" in types
    assert "prose_refusal_instead_of_tool" in types


def test_past_exp02_tool_loop_detection():
    """Verify that invariants detect the repetitive python_eval SyntaxError loop in exp02 task 1."""
    trace_file = Path(__file__).resolve().parents[2] / "runs" / "20261019-exp02" / "argon_trace.jsonl"
    if not trace_file.is_file():
        pytest.skip("exp02 trace file not found")

    with open(trace_file, encoding="utf-8") as f:
        first_trace = json.loads(f.readline())

    task = {
        "task_id": first_trace["task_id"],
        "category": "tool_use",
        "tools": [{"type": "function", "function": {"name": "python_eval"}}],
    }

    findings = run_all_semantic_invariants(task=task, turns_history=first_trace.get("turns", []))
    types = [f.get("invariant_type") for f in findings]

    # Must detect the 3-turn repetitive loop that the original monitor missed!
    assert "tool_repetition_loop" in types
    loop_finding = next(f for f in findings if f.get("invariant_type") == "tool_repetition_loop")
    assert loop_finding["evidence"]["metric_value"] == 3


# ============================================================================
# 5. Adversarial Edge Case Verification Tests
# ============================================================================

def test_check_refusal_leak_unicode_curly_quotes():
    """Verify typographic/curly quotes and apostrophes are caught by refusal invariant."""
    text1 = "First, I\u2019ll need your permission to turn on the Google Drive app."
    res1 = check_refusal_leak(text1)
    assert res1 is not None
    assert "google drive" in res1["matched_phrase"]

    text2 = "I\u2019m having a hard time fulfilling your request."
    res2 = check_refusal_leak(text2)
    assert res2 is not None
    assert "having a hard time" in res2["matched_phrase"]


def test_check_streaming_invariants_token_by_token():
    """Verify token-by-token SSE streaming refusal leaks are caught beyond 3 chunks."""
    chunks = [
        "First", ", ", "I'll ", "need ", "your ", "permission ", "to ", "turn ",
        "on ", "the ", "Google ", "Drive ", "app", " to ", "access ", "data.",
    ]
    issues = check_streaming_invariants(chunks, ttft_ms=300.0, max_gap_ms=100.0)
    assert any(i["type"] == "early_stream_refusal_leak" for i in issues)


def test_check_tool_repetition_loop_arg_normalization_and_count():
    """Verify argument formatting differences are normalized and true repetition count is reported."""
    turns = [
        {"response": {"choices": [{"message": {"tool_calls": [{"function": {"name": "run_command", "arguments": '{"command": "cat data.csv"}'}}]}}]}},
        {"response": {"choices": [{"message": {"tool_calls": [{"function": {"name": "run_command", "arguments": '{"command":"cat data.csv"}'}}]}}]}},
        {"response": {"choices": [{"message": {"tool_calls": [{"function": {"name": "run_command", "arguments": '{"command":  "cat data.csv" }'}}]}}]}},
    ]
    loop = check_tool_repetition_loop(turns, threshold=2)
    assert loop is not None
    assert loop["repetitions"] == 3


def test_sandbox_graceful_error_handling(tmp_path):
    """Verify AgentSandbox methods return structured errors rather than crashing on OS edge cases."""
    sandbox = AgentSandbox(tmp_path)
    sandbox.write_file("file.txt", "content")

    # Calling list_dir on a file must not throw NotADirectoryError
    res_list = sandbox.list_dir("file.txt")
    assert res_list["status"] == "error"
    assert "Not a directory" in res_list["message"]

    # Calling write_file on an existing directory must not throw PermissionError
    res_write = sandbox.write_file("file.txt", "overwrite_file_ok")
    assert res_write["status"] == "ok"

    (tmp_path / "subdir").mkdir()
    res_write_dir = sandbox.write_file("subdir", "cannot write to dir")
    assert res_write_dir["status"] == "error"
    assert "directory" in res_write_dir["message"]


def test_artifact_verification_invariants_invalid_and_schema_patterns(tmp_path):
    """Verify run_all_semantic_invariants reports artifact_invalid when content fails schema/checks."""
    sandbox = AgentSandbox(tmp_path)
    sandbox.write_file("output/data.json", "{ malformed json: true ")

    task = {
        "task_id": "task_art_check_01",
        "category": "tool_use",
        "expected_behavior": {
            "expected_artifacts": [
                {"path": "output/data.json", "min_size": 5, "is_json": True}
            ]
        }
    }

    findings = run_all_semantic_invariants(task=task, turns_history=[], sandbox=sandbox)
    types = [f.get("invariant_type") for f in findings]
    assert "artifact_invalid" in types
    invalid_finding = next(f for f in findings if f.get("invariant_type") == "artifact_invalid")
    assert "Invalid JSON" in invalid_finding["summary"]


def test_turn_level_streaming_stall_detected_early():
    """Verify that an inter-chunk stall occurring on turn 1 is detected even if turn 2 is fast."""
    task = {"task_id": "task_stream_01", "category": "streaming_imperative"}
    turn1 = {
        "turn": 1,
        "streaming_telemetry": {
            "stream": True,
            "max_inter_chunk_gap_ms": 22_500.0,
            "ttft_ms": 1200.0,
        },
        "response": {"choices": [{"message": {"content": "Processing data..."}}]},
    }
    turn2 = {
        "turn": 2,
        "streaming_telemetry": {
            "stream": True,
            "max_inter_chunk_gap_ms": 25.0,
            "ttft_ms": 150.0,
        },
        "response": {"choices": [{"message": {"content": "Done."}}]},
    }

    findings = run_all_semantic_invariants(task=task, turns_history=[turn1, turn2])
    types = [f.get("invariant_type") for f in findings]
    assert "stream_chunk_stall" in types
    stall_finding = next(f for f in findings if f.get("invariant_type") == "stream_chunk_stall")
    assert "Turn 1" in stall_finding["summary"]


def test_responses_api_streaming_tool_call_assembly(monkeypatch):
    """Verify _call_responses_api streaming correctly reassembles tool calls and completes cleanly."""
    from app.core.responses_protocol import ResponsesStreamEncoder
    import io

    enc = ResponsesStreamEncoder("resp_wire_01", "gemini-4-argon", {})
    frames = (
        [enc.created(), enc.in_progress()]
        + enc.function_call("fc_wire_01", 0, "call_wire_abc", "run_command", '{"command": "ls -la"}')
        + [enc.completed([], {})]
    )
    raw_payload = "\n".join(frames).encode("utf-8")

    class MockResp(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    def mock_urlopen(req, timeout=360.0):
        return MockResp(raw_payload)

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    runner = BabysitRunner(base_url="http://127.0.0.1:5918", run_id="test_resp_stream")
    result = runner._call_responses_api(
        messages=[{"role": "user", "content": "List files"}],
        model="gemini-4-argon",
        stream=True,
    )

    choice = result["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    tcs = choice["message"]["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["id"] == "call_wire_abc"
    assert tcs[0]["function"]["name"] == "run_command"
    assert tcs[0]["function"]["arguments"] == '{"command": "ls -la"}'
    assert result["streaming_telemetry"]["chunk_count"] > 0


def test_responses_api_multiturn_message_mapping(monkeypatch):
    """Verify _call_responses_api maps previous turn tool calls and results to Responses input."""
    captured_body = {}

    class MockResp:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps({
                "id": "resp_002",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "Task completed"}]}]
            }).encode("utf-8")

    def mock_urlopen(req, timeout=360.0):
        nonlocal captured_body
        captured_body = json.loads(req.data.decode("utf-8"))
        return MockResp()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    runner = BabysitRunner(base_url="http://127.0.0.1:5918", run_id="test_resp_multiturn")
    messages = [
        {"role": "user", "content": "Run calculation"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_calc_99",
                "type": "function",
                "function": {"name": "python_eval", "arguments": '{"code": "2+2"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call_calc_99", "content": "4"},
    ]

    res = runner._call_responses_api(messages=messages, model="gemini-4-argon", stream=False)
    assert res["choices"][0]["message"]["content"] == "Task completed"

    input_items = captured_body.get("input", [])
    fc_item = next(i for i in input_items if i.get("type") == "function_call")
    assert fc_item["call_id"] == "call_calc_99"
    assert fc_item["name"] == "python_eval"
    assert fc_item["arguments"] == '{"code": "2+2"}'

    fco_item = next(i for i in input_items if i.get("type") == "function_call_output")
    assert fco_item["call_id"] == "call_calc_99"
    assert fco_item["output"] == "4"


def test_responses_api_multimodal_translation(monkeypatch):
    """Verify chat-shaped multimodal input is translated to Responses input_text and input_image."""
    captured_body = {}

    class MockResp:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps({
                "id": "resp_mm",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "I see a chart"}]}]
            }).encode("utf-8")

    def mock_urlopen(req, timeout=360.0):
        nonlocal captured_body
        captured_body = json.loads(req.data.decode("utf-8"))
        return MockResp()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    runner = BabysitRunner(base_url="http://127.0.0.1:5918", run_id="test_resp_mm")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Analyze this chart"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="}},
            ],
        }
    ]

    runner._call_responses_api(messages=messages, model="gemini-4-argon", stream=False)
    input_items = captured_body.get("input", [])
    user_item = input_items[0]
    parts = user_item["content"]
    assert parts[0]["type"] == "input_text"
    assert parts[0]["text"] == "Analyze this chart"
    assert parts[1]["type"] == "input_image"
    assert parts[1]["image_url"].startswith("data:image/png;base64,")


def test_chat_completions_stream_connection_drop_recovery(monkeypatch):
    """Verify chat completions stream handles mid-stream drop without crashing and captures partial telemetry."""
    class DroppingStream:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def __iter__(self):
            yield b'data: {"id": "drop_1", "choices": [{"delta": {"content": "Initial chunk "}}]}\n\n'
            raise ConnectionResetError("Connection lost mid-stream")

    def mock_urlopen(req, timeout=360.0):
        return DroppingStream()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    runner = BabysitRunner(base_url="http://127.0.0.1:5918", run_id="test_drop")
    res = runner._call_chat_completions_stream(
        messages=[{"role": "user", "content": "Stream me"}],
        model="gemini-4-argon",
    )

    assert res["choices"][0]["finish_reason"] == "error"
    assert res["choices"][0]["message"]["content"] == "Initial chunk "
    assert "Connection lost" in res["streaming_telemetry"]["stream_error"]


