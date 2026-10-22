"""Semantic invariant checkers for Gemini 4 Argon babysitting framework.

Deterministic, evidence-grounded checks for:
1. Google Drive / Workspace soft refusal phrases and UI leaks (GOOGLE_WEB_ERROR_PHRASES, ElicitationsGroup).
2. Internal cloud code interpreter leakage (code_reference, code_stdout, code_stderr, python?code_reference).
3. Prose refusal / conversational text emitted when tool calls were expected.
4. Repetitive tool calling loops (identical arguments, repeating failures).
5. Streaming anomalies (excessive TTFT, inter-chunk stalls, early refusal leaks).
6. Missing or malformed sandbox artifacts.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple


# Known Google Drive / Workspace / Gemini Web soft error and refusal phrases
REFUSAL_PHRASES = (
    "sorry, something went wrong. please try your request again",
    "i encountered an error doing what you asked",
    "something went wrong. please try again",
    "an error occurred. please try again",
    "i seem to be encountering an error",
    "i'm having a hard time fulfilling your request",
    "i am having a hard time fulfilling your request",
    "first, i'll need your permission to turn on the workspace app",
    "i'll need your permission to turn on the workspace app",
    "first, i'll need your permission to turn on the google drive app",
    "i'll need your permission to turn on the google drive app",
    "first, i'll need your permission to turn on the google docs app",
    "i'll need your permission to turn on the google docs app",
    "first, i'll need your permission to turn on the",
    "i'll need your permission to turn on the",
    "i can't use google docs",
    "i can't use google drive",
    "i can't use gmail",
    "i can't use google workspace",
    "i can't use the workspace app",
    "required gmail settings are off",
    "turn on these settings to continue",
    "i do not have access to the local sandbox filesystem",
    "i do not have access to the local filesystem",
    "i do not have access to the specified execution tools",
    "in this environment, so i cannot directly read",
    "<elicitationsgroup",
    "<elicitation",
)

INTERNAL_CODE_INTERPRETER_INDICATORS = (
    "python?code_reference",
    "code_reference",
    "code_stdout",
    "code_stderr",
    "code_event_index",
)

_CODE_REFERENCE_RE = re.compile(r"python\?code_reference(?:\&code_event_index=\d+)?", re.IGNORECASE)


def _normalize_quotes(text: str) -> str:
    """Normalize typographic quotes and apostrophes to standard ASCII."""
    if not text:
        return ""
    return (
        text.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u0060", "'")
        .replace("\u00b4", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )


def check_refusal_leak(text: str) -> Optional[Dict[str, Any]]:
    """Check if text contains Google Drive / Workspace refusal phrases or web UI leaks."""
    if not text:
        return None
    clean_text = _normalize_quotes(text)
    lower_text = clean_text.lower()
    for phrase in REFUSAL_PHRASES:
        if phrase in lower_text:
            idx = lower_text.find(phrase)
            start = max(0, idx - 40)
            end = min(len(text), idx + len(phrase) + 40)
            snippet = text[start:end].strip()
            return {
                "matched_phrase": phrase,
                "snippet": snippet,
                "description": f"Model output leaked Google Drive/Workspace refusal or UI marker: '{phrase}'",
            }
    return None


def check_code_interpreter_leak(text_or_calls: Any) -> Optional[Dict[str, Any]]:
    """Check if response text or tool arguments leak Google internal code interpreter tokens."""
    texts_to_check = []
    if isinstance(text_or_calls, str):
        texts_to_check.append(text_or_calls)
    elif isinstance(text_or_calls, dict):
        fn = text_or_calls.get("function") or {}
        texts_to_check.append(fn.get("name", ""))
        texts_to_check.append(str(fn.get("arguments", "")))
        texts_to_check.append(str(text_or_calls.get("content", "")))
    elif isinstance(text_or_calls, list):
        for item in text_or_calls:
            if isinstance(item, str):
                texts_to_check.append(item)
            elif isinstance(item, dict):
                fn = item.get("function") or {}
                texts_to_check.append(fn.get("name", ""))
                texts_to_check.append(str(fn.get("arguments", "")))
                texts_to_check.append(str(item.get("content", "")))

    for t in texts_to_check:
        if not t:
            continue
        lower_t = t.lower()
        for ind in INTERNAL_CODE_INTERPRETER_INDICATORS:
            if ind in lower_t:
                idx = lower_t.find(ind)
                start = max(0, idx - 40)
                end = min(len(t), idx + len(ind) + 40)
                return {
                    "matched_indicator": ind,
                    "snippet": t[start:end].strip(),
                    "description": f"Model attempted internal cloud code interpreter execution or leaked indicator: '{ind}'",
                }
    return None


def check_prose_refusal_instead_of_tool(
    message: Dict[str, Any],
    tools_available: bool,
    category: str = "",
) -> Optional[Dict[str, Any]]:
    """Check if the agent emitted conversational prose instead of calling tools when tools were expected."""
    if not tools_available:
        return None
    
    tool_calls = message.get("tool_calls") or []
    content = message.get("content") or ""

    if not tool_calls and content:
        refusal = check_refusal_leak(content)
        lower_content = _normalize_quotes(content).lower()
        is_refusal_prose = (
            refusal is not None
            or "i cannot" in lower_content
            or "i am unable" in lower_content
            or "i don't have access" in lower_content
            or "i do not have access" in lower_content
            or "please provide" in lower_content
            or "paste" in lower_content
        )
        if is_refusal_prose:
            return {
                "content_snippet": content[:200],
                "description": "Model emitted conversational refusal prose instead of invoking available tools",
                "refusal_details": refusal,
            }
    return None


_SIMULATED_CODE_PATTERNS = (
    r"import\s+subprocess\b",
    r"from\s+subprocess\s+import",
    r"\bsubprocess\.(?:run|Popen|check_output|call)\b",
    r"\bos\.(?:system|popen|listdir|walk)\b",
    r"\b(?:Get-ChildItem|Select-Object|Where-Object)\b",
    r"\bgit\s+(?:log|status|diff|checkout|branch)\b",
    r"\bpython(?:\.exe)?\s+scripts[\\/]",
    r"\bopen\(.+?,\s*[\"']r[\"']\)",
)
_SIMULATED_CODE_RE = re.compile("|".join(_SIMULATED_CODE_PATTERNS), re.IGNORECASE)


def check_simulated_code_execution(
    message: Dict[str, Any],
    tools_available: bool,
) -> Optional[Dict[str, Any]]:
    """Check if the agent emitted a code block simulating execution instead of calling available tools."""
    if not tools_available:
        return None

    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        return None

    content = message.get("content") or ""
    if not content or "```" not in content:
        return None

    code_blocks = re.findall(r"```(?:[a-zA-Z0-9_-]+)?\s*([\s\S]*?)```", content)
    for block in code_blocks:
        match = _SIMULATED_CODE_RE.search(block)
        if match:
            return {
                "matched_pattern": match.group(0),
                "snippet": block[:300].strip(),
                "description": f"Model emitted code block simulating command/script execution ('{match.group(0)}') instead of invoking available tools",
            }
    return None


def check_unverified_investigation_prose(
    task: Dict[str, Any],
    turns_history: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Check if the agent produced a final structured investigation report with zero tool calls."""
    if not task.get("tools") or not turns_history:
        return None

    total_tool_calls = 0
    for turn in turns_history:
        resp = turn.get("response") or {}
        choices = resp.get("choices") or []
        msg = choices[0].get("message") if choices else {}
        calls = msg.get("tool_calls") or []
        total_tool_calls += len(calls)

    if total_tool_calls > 0:
        return None

    last_turn = turns_history[-1]
    resp = last_turn.get("response") or {}
    choices = resp.get("choices") or []
    msg = choices[0].get("message") if choices else {}
    content = msg.get("content") or ""

    if len(content) < 150:
        return None

    prompt = task.get("prompt", "").lower()
    cat = task.get("category", "")
    is_investigation = (
        cat in ("investigation", "tool_use", "filesystem", "multistep_planning")
        or any(kw in prompt for kw in ("investigation", "inspect", "audit", "run:", "check if", "list all open", "read evals/"))
    )

    if not is_investigation:
        return None

    has_report_headings = bool(re.search(r"(?:###?\s*(?:BLOCKERS|REDTEAM|STATUS|REPORT|GAPS|FINDINGS|AUDIT)|#\s+.+Report)", content, re.IGNORECASE))
    if has_report_headings:
        return {
            "content_snippet": content[:300].strip(),
            "description": "Agent produced structured investigation report with zero tool executions (hallucinated verification)",
        }
    return None


def _normalize_tool_args(args_val: Any) -> str:
    """Normalize tool arguments for semantic loop comparison."""
    if isinstance(args_val, dict):
        try:
            return json.dumps(args_val, sort_keys=True)
        except Exception:
            return str(args_val)
    if isinstance(args_val, str):
        try:
            parsed = json.loads(args_val)
            if isinstance(parsed, dict):
                return json.dumps(parsed, sort_keys=True)
            return json.dumps(parsed)
        except Exception:
            return args_val.strip()
    return str(args_val)


def check_tool_repetition_loop(turns: List[Dict[str, Any]], threshold: int = 2) -> Optional[Dict[str, Any]]:
    """Detect if the agent repeated the exact same tool call with identical arguments multiple times in a row."""
    if len(turns) < threshold:
        return None

    last_signatures: List[str] = []
    for turn in turns:
        resp = turn.get("response") or {}
        choices = resp.get("choices") or []
        msg = choices[0].get("message") if choices else {}
        calls = msg.get("tool_calls") or []
        sig = []
        for c in calls:
            fn = c.get("function") or {}
            name = fn.get("name", "")
            raw_args = fn.get("arguments", "")
            norm_args = _normalize_tool_args(raw_args)
            sig.append(f"{name}:{norm_args}")
        if sig:
            last_signatures.append(";".join(sig))

    if len(last_signatures) >= threshold:
        target_sig = last_signatures[-1]
        reps = 0
        for s in reversed(last_signatures):
            if s == target_sig:
                reps += 1
            else:
                break
        if reps >= threshold and target_sig:
            return {
                "repeated_call": target_sig,
                "repetitions": reps,
                "description": f"Model is stuck in a repetitive loop, executing '{target_sig}' {reps} times consecutively",
            }
    return None


def check_streaming_invariants(
    chunks: List[str],
    ttft_ms: float,
    max_gap_ms: float,
    stall_threshold_ms: float = 15_000.0,
) -> List[Dict[str, Any]]:
    """Check streaming-specific invariants: chunk stalls and refusal phrase leakage in early chunks."""
    issues = []

    # 1. Early chunk refusal check (first 30 chunks or first 500 characters)
    early_text = "".join(chunks[:30])[:500]
    early_leak = check_refusal_leak(early_text)
    if early_leak:
        issues.append({
            "type": "early_stream_refusal_leak",
            "snippet": early_leak["snippet"],
            "matched_phrase": early_leak["matched_phrase"],
            "description": f"Refusal phrase leaked in initial stream chunks: '{early_leak['matched_phrase']}'",
        })

    # 2. Inter-chunk gap stall
    if max_gap_ms > stall_threshold_ms:
        issues.append({
            "type": "stream_chunk_stall",
            "max_gap_ms": max_gap_ms,
            "description": f"Inter-chunk gap exceeded stall threshold: {max_gap_ms:.1f}ms > {stall_threshold_ms:.1f}ms",
        })

    return issues


def run_all_semantic_invariants(
    task: Dict[str, Any],
    turns_history: List[Dict[str, Any]],
    sandbox: Any = None,
    streaming_chunks: Optional[List[str]] = None,
    ttft_ms: float = 0.0,
    max_gap_ms: float = 0.0,
) -> List[Dict[str, Any]]:
    """Run full deterministic semantic invariant suite and return list of findings."""
    findings = []
    task_id = task.get("task_id", "unknown_task")
    category = task.get("category", "")
    has_tools = bool(task.get("tools"))

    # Turn-level checks
    for turn_idx, turn in enumerate(turns_history, start=1):
        resp = turn.get("response") or {}
        choices = resp.get("choices") or []
        msg = choices[0].get("message") if choices else {}
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []
        req_id = turn.get("proxy_telemetry", {}).get("request_id") or "n/a"
        ts = turn.get("timestamp") or "n/a"

        # 1. Refusal phrase check
        refusal = check_refusal_leak(content)
        if refusal:
            findings.append({
                "invariant_type": "leaked_refusal_phrase",
                "category": "argon",
                "severity": "error",
                "confidence": 0.98,
                "summary": f"Turn {turn_idx}: {refusal['description']}",
                "evidence": {
                    "request_id": req_id,
                    "timestamp": ts,
                    "quoted_log": refusal["snippet"],
                    "metric_name": "turn_index",
                    "metric_value": turn_idx,
                    "task_id": task_id,
                },
                "root_cause_hypothesis": "Argon model confused synthetic path/instructions with Google Drive/Workspace app integration, leaking a refusal prompt.",
                "proposed_fix": "Prefix prompt with local imperative workspace context and ensure proxy InitialPrefixBuffer suppresses this phrase before streaming.",
            })

        # 2. Code interpreter check
        ci_leak = check_code_interpreter_leak(content) or check_code_interpreter_leak(tool_calls)
        if ci_leak:
            findings.append({
                "invariant_type": "internal_code_interpreter_leak",
                "category": "argon",
                "severity": "error",
                "confidence": 0.98,
                "summary": f"Turn {turn_idx}: {ci_leak['description']}",
                "evidence": {
                    "request_id": req_id,
                    "timestamp": ts,
                    "quoted_log": ci_leak["snippet"],
                    "metric_name": "turn_index",
                    "metric_value": turn_idx,
                    "task_id": task_id,
                },
                "root_cause_hypothesis": "Argon invoked internal Google Cloud Python sandbox ('python?code_reference') instead of declared OpenAI function tools.",
                "proposed_fix": "Intercept internal code interpreter tokens in proxy and reinforce tool calling grammar in the prompt.",
            })

        # 3. Prose refusal instead of tool calls
        prose_err = check_prose_refusal_instead_of_tool(msg, has_tools, category)
        if prose_err:
            findings.append({
                "invariant_type": "prose_refusal_instead_of_tool",
                "category": "argon",
                "severity": "error",
                "confidence": 0.95,
                "summary": f"Turn {turn_idx}: {prose_err['description']}",
                "evidence": {
                    "request_id": req_id,
                    "timestamp": ts,
                    "quoted_log": prose_err["content_snippet"],
                    "metric_name": "turn_index",
                    "metric_value": turn_idx,
                    "task_id": task_id,
                },
                "root_cause_hypothesis": "Argon hallucinated environment limitations instead of executing provided tools.",
                "proposed_fix": "Supply realistic imperative prompts and pre-seeded files to prevent agent refusal.",
            })

        # 3b. Simulated code execution check
        sim_code = check_simulated_code_execution(msg, has_tools)
        if sim_code:
            findings.append({
                "invariant_type": "simulated_code_execution",
                "category": "argon",
                "severity": "error",
                "confidence": 0.98,
                "summary": f"Turn {turn_idx}: {sim_code['description']}",
                "evidence": {
                    "request_id": req_id,
                    "timestamp": ts,
                    "quoted_log": sim_code["snippet"],
                    "metric_name": "turn_index",
                    "metric_value": turn_idx,
                    "task_id": task_id,
                },
                "root_cause_hypothesis": "Argon roleplayed Python/shell execution inside a Markdown code block instead of invoking available tools.",
                "proposed_fix": "Intercept simulated execution blocks in proxy and demand JSON tool_call execution.",
            })

        # 4. Turn-level streaming checks
        st = turn.get("streaming_telemetry") or {}
        if st.get("stream"):
            gap = float(st.get("max_inter_chunk_gap_ms") or 0.0)
            if gap > 15_000.0:
                findings.append({
                    "invariant_type": "stream_chunk_stall",
                    "category": "streaming",
                    "severity": "warning",
                    "confidence": 0.95,
                    "summary": f"Turn {turn_idx}: Inter-chunk gap exceeded stall threshold: {gap:.1f}ms > 15000.0ms",
                    "evidence": {
                        "request_id": req_id,
                        "timestamp": ts,
                        "quoted_log": f"max_inter_chunk_gap_ms: {gap:.1f}",
                        "metric_name": "max_inter_chunk_gap_ms",
                        "metric_value": gap,
                        "task_id": task_id,
                    },
                    "root_cause_hypothesis": "Streaming delivery stalled between chunks during turn execution.",
                    "proposed_fix": "Inspect network stability and proxy keepalive ping cadence.",
                })
            if st.get("stream_error"):
                findings.append({
                    "invariant_type": "stream_error",
                    "category": "streaming",
                    "severity": "error",
                    "confidence": 0.99,
                    "summary": f"Turn {turn_idx}: Stream encountered error: {st.get('stream_error')}",
                    "evidence": {
                        "request_id": req_id,
                        "timestamp": ts,
                        "quoted_log": str(st.get("stream_error")),
                        "metric_name": "stream_error",
                        "metric_value": 1,
                        "task_id": task_id,
                    },
                    "root_cause_hypothesis": "Streaming connection aborted or encountered socket error.",
                    "proposed_fix": "Inspect proxy logs and connection keepalive configuration.",
                })
            early_chunks = st.get("early_chunks") or []
            if early_chunks:
                early_leak_turn = check_refusal_leak("".join(early_chunks[:30])[:500])
                if early_leak_turn:
                    findings.append({
                        "invariant_type": "early_stream_refusal_leak",
                        "category": "streaming",
                        "severity": "error",
                        "confidence": 0.98,
                        "summary": f"Turn {turn_idx}: Refusal phrase leaked in stream chunks: '{early_leak_turn['matched_phrase']}'",
                        "evidence": {
                            "request_id": req_id,
                            "timestamp": ts,
                            "quoted_log": early_leak_turn["snippet"],
                            "metric_name": "turn_index",
                            "metric_value": turn_idx,
                            "task_id": task_id,
                        },
                        "root_cause_hypothesis": "Argon leaked a refusal phrase in initial streaming chunks before proxy buffer could suppress it.",
                        "proposed_fix": "Increase proxy InitialPrefixBuffer and verify refusal suppression regexes.",
                    })

    # 4. Tool repetition loop check
    loop = check_tool_repetition_loop(turns_history, threshold=2)
    if loop:
        latest_telemetry = turns_history[-1].get("proxy_telemetry", {}) if turns_history else {}
        findings.append({
            "invariant_type": "tool_repetition_loop",
            "category": "argon",
            "severity": "warning",
            "confidence": 0.95,
            "summary": loop["description"],
            "evidence": {
                "request_id": latest_telemetry.get("request_id") or "n/a",
                "timestamp": turns_history[-1].get("timestamp") if turns_history else "n/a",
                "quoted_log": loop["repeated_call"][:200],
                "metric_name": "repetitions",
                "metric_value": loop["repetitions"],
                "task_id": task_id,
            },
            "root_cause_hypothesis": "Agent failed to inspect tool error feedback and repeatedly attempted the identical failing invocation.",
            "proposed_fix": "Inject error recovery hints or abort repetitive loops early.",
        })

    # 4b. Unverified investigation prose check
    unverified = check_unverified_investigation_prose(task, turns_history)
    if unverified:
        latest_telemetry = turns_history[-1].get("proxy_telemetry", {}) if turns_history else {}
        findings.append({
            "invariant_type": "unverified_investigation_prose",
            "category": "argon",
            "severity": "critical",
            "confidence": 0.99,
            "summary": unverified["description"],
            "evidence": {
                "request_id": latest_telemetry.get("request_id") or "n/a",
                "timestamp": turns_history[-1].get("timestamp") if turns_history else "n/a",
                "quoted_log": unverified["content_snippet"],
                "metric_name": "total_tool_calls",
                "metric_value": 0,
                "task_id": task_id,
            },
            "root_cause_hypothesis": "Argon fabricated investigation conclusions out of pre-training weights without inspecting files or running commands.",
            "proposed_fix": "Reinforce grounding invariants and reject premature reports on zero-tool turns.",
        })

    # 5. Global streaming checks (if streaming chunks passed directly)
    if streaming_chunks:
        stream_issues = check_streaming_invariants(streaming_chunks, ttft_ms, max_gap_ms)
        for issue in stream_issues:
            findings.append({
                "invariant_type": issue["type"],
                "category": "streaming",
                "severity": "error" if "refusal" in issue["type"] else "warning",
                "confidence": 0.95,
                "summary": issue["description"],
                "evidence": {
                    "request_id": "stream_trace",
                    "timestamp": turns_history[-1].get("timestamp") if turns_history else "n/a",
                    "quoted_log": issue.get("snippet", str(issue.get("max_gap_ms", 0))),
                    "metric_name": issue["type"],
                    "metric_value": issue.get("max_gap_ms", 0),
                    "task_id": task_id,
                },
                "root_cause_hypothesis": "Streaming delivery exhibited refusal leakage or excessive inter-chunk delay.",
                "proposed_fix": "Verify proxy stream prefix buffer and keepalive frame mechanism.",
            })

    # 6. Artifact verification in sandbox
    if sandbox is not None:
        expected = task.get("expected_behavior", {}).get("expected_artifacts") or []
        for item in expected:
            if isinstance(item, str):
                art_path = item
                min_size = 1
                must_contain = None
                is_json = False
            elif isinstance(item, dict):
                art_path = item.get("path", "")
                min_size = int(item.get("min_size", 1))
                must_contain = item.get("must_contain")
                is_json = bool(item.get("is_json", False))
            else:
                continue

            if not art_path:
                continue

            v = sandbox.verify_artifact(
                art_path,
                min_size=min_size,
                must_contain=must_contain,
                is_json=is_json,
            )
            latest_ts = turns_history[-1].get("timestamp") if turns_history else "n/a"
            if not v.get("exists"):
                findings.append({
                    "invariant_type": "artifact_missing",
                    "category": "argon",
                    "severity": "error",
                    "confidence": 0.99,
                    "summary": f"Expected artifact '{art_path}' was not created in the sandbox.",
                    "evidence": {
                        "request_id": "sandbox_verification",
                        "timestamp": latest_ts,
                        "quoted_log": f"Artifact missing: {art_path}",
                        "metric_name": "artifact_exists",
                        "metric_value": 0,
                        "task_id": task_id,
                    },
                    "root_cause_hypothesis": "Argon failed to reach final solution or create output file in sandbox.",
                    "proposed_fix": "Increase max_turns or refine prompt guidance on required output files.",
                })
            elif not v.get("valid", True):
                findings.append({
                    "invariant_type": "artifact_invalid",
                    "category": "argon",
                    "severity": "error",
                    "confidence": 0.98,
                    "summary": f"Artifact '{art_path}' failed verification: {v.get('error')}",
                    "evidence": {
                        "request_id": "sandbox_verification",
                        "timestamp": latest_ts,
                        "quoted_log": str(v.get("error", "Artifact verification failed")),
                        "metric_name": "artifact_valid",
                        "metric_value": 0,
                        "task_id": task_id,
                    },
                    "root_cause_hypothesis": "Argon created output file but content was malformed, truncated, or lacked required schema/patterns.",
                    "proposed_fix": "Ensure model adheres to required file formatting instructions.",
                })

    return findings
