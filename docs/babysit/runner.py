"""Automated benchmark runner for Gemini 4 Argon babysitting experiment.

Orchestrates the 3-party loop with full support for:
1. Streaming SSE evaluation (TTFT, inter-chunk gaps, early refusal detection)
2. Multiple protocols (OpenAI Chat Completions & OpenAI Responses /v1/responses)
3. Multimodal / Vision inputs (inline base64 images)
4. Realistic imperative tasks with sandbox fixtures pre-seeding (initial_files)
5. Deterministic semantic invariant checks (refusal leaks, code interpreter leaks, tool loops, artifact checks)
6. Deep telemetry correlation with gemini2api proxy
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
from sandbox import AgentSandbox as BabysitSandbox
from invariants import run_all_semantic_invariants, check_refusal_leak, check_code_interpreter_leak

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("runner")


def extract_json(s: str) -> Any:
    """Robustly extract and parse JSON object or array from LLM response."""
    if not s:
        return {}
    s = s.strip()
    try:
        return json.loads(s, strict=False)
    except Exception:
        pass
    if s.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*\n?", "", s)
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3].strip()
        try:
            return json.loads(cleaned, strict=False)
        except Exception:
            pass
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", s)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip(), strict=False)
        except Exception:
            pass
    m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", s)
    if m:
        candidate = m.group(1)
        try:
            return json.loads(candidate, strict=False)
        except Exception:
            cleaned = re.sub(
                r'[\x00-\x1f]',
                lambda ch: f'\\u{ord(ch.group()):04x}' if ch.group() not in '\r\n\t' else ch.group(),
                candidate,
            )
            try:
                return json.loads(cleaned, strict=False)
            except Exception:
                pass
    raise ValueError(f"Could not extract valid JSON from response: {s[:200]}...")


def resolve_api_key(cli_key: Optional[str] = None) -> str:
    """Resolve API key from CLI arg, environment variables, or local .env file."""
    if cli_key and cli_key.strip():
        return cli_key.strip()
    key = os.environ.get("ADMIN_API_KEY") or os.environ.get("API_KEY")
    if key and key.strip():
        return key.strip()
    candidates = [Path(".env"), Path(__file__).resolve().parents[2] / ".env"]
    for p in candidates:
        if p.is_file():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("ADMIN_API_KEY="):
                        val = line.split("=", 1)[1].strip("\"' ")
                        if val:
                            return val
                    if line.startswith("API_KEY="):
                        val = line.split("=", 1)[1].strip("\"' ")
                        if val:
                            return val
            except Exception:
                pass
    return ""


def encode_image_as_data_url(file_path: Path | str) -> str:
    """Read local image and convert to base64 data URL."""
    p = Path(file_path)
    if not p.is_file():
        return ""
    mime = "image/png"
    if p.suffix.lower() in (".jpg", ".jpeg"):
        mime = "image/jpeg"
    elif p.suffix.lower() == ".webp":
        mime = "image/webp"
    elif p.suffix.lower() == ".gif":
        mime = "image/gif"
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


class BabysitRunner:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:5918",
        api_key: Optional[str] = None,
        run_id: Optional[str] = None,
        runs_dir: str | Path = "runs",
        max_tasks: int = 5,
        orchestrator_model: str = "gemini-flash",
        argon_model: str = "gemini-4-argon",
        monitor_model: str = "gemini-flash",
        default_protocol: str = "chat_completions",
        force_stream: Optional[bool] = None,
        tasks_file: Optional[str | Path] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = resolve_api_key(api_key)
        self.run_id = run_id or f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        self.run_dir = Path(runs_dir) / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

        self.tasks_file = self.run_dir / "tasks.jsonl"
        self.trace_file = self.run_dir / "argon_trace.jsonl"
        self.findings_file = self.run_dir / "findings.jsonl"
        self.completed_file = self.run_dir / "completed_tasks.json"

        self.predefined_tasks: List[Dict[str, Any]] = []
        if tasks_file:
            tf_path = Path(tasks_file)
            if tf_path.is_file():
                for line in tf_path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        self.predefined_tasks.append(json.loads(line))
                if self.predefined_tasks:
                    logger.info(f"Loaded {len(self.predefined_tasks)} predefined tasks from {tf_path}.")

        task_limit = min(max_tasks, 20)  # Enforce D5 ceiling
        if self.predefined_tasks:
            self.max_tasks = min(task_limit, len(self.predefined_tasks))
        else:
            self.max_tasks = task_limit
        self.orchestrator_model = orchestrator_model
        self.argon_model = argon_model
        self.monitor_model = monitor_model
        self.default_protocol = default_protocol
        self.force_stream = force_stream

        self.kill_switch_files = [
            self.run_dir / "STOP",
            self.run_dir / "STOP_BABYSIT",
            Path("STOP_BABYSIT"),
        ]

        self.completed_task_ids: set[str] = set()
        if self.completed_file.exists():
            try:
                self.completed_task_ids = set(json.loads(self.completed_file.read_text(encoding="utf-8")))
            except Exception:
                pass

        self.orch_prompt = (_SCRIPT_DIR / "orchestrator_prompt.txt").read_text(encoding="utf-8")
        self.mon_prompt = (_SCRIPT_DIR / "monitor_prompt.txt").read_text(encoding="utf-8")

    def is_stopped(self) -> bool:
        for ks in self.kill_switch_files:
            if ks.exists():
                logger.warning(f"Kill-switch triggered: {ks.name} found. Stopping runner.")
                return True
        return False

    def _get_latest_proxy_log(self, run_id: str, role: str) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/admin/logs?limit=10"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for rec in data.get("records", []):
                    tags = rec.get("tags") or []
                    if run_id in tags and role in tags:
                        return rec
        except Exception as e:
            logger.warning(f"Failed to fetch proxy log: {e}")
        return None

    def _call_chat_completions_nonstream(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        role: str = "agent",
        timeout: float = 360.0,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "X-G2A-Run": self.run_id,
            "X-G2A-Role": role,
        }
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "max_tokens": 4096,
        }
        if tools:
            body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice

        req = urllib.request.Request(url, headers=headers, data=json.dumps(body).encode("utf-8"))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _call_chat_completions_stream(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        role: str = "agent",
        timeout: float = 360.0,
    ) -> Dict[str, Any]:
        """Execute chat completion with SSE streaming, measuring TTFT and reassembling chunks."""
        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {self.api_key}",
            "X-G2A-Run": self.run_id,
            "X-G2A-Role": role,
        }
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "max_tokens": 4096,
        }
        if tools:
            body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice

        t_start = time.time()
        ttft_ms = 0.0
        last_packet_time = t_start   # updated on ANY valid SSE line (including : ping keepalives)
        last_token_time = t_start    # updated only on substantive data chunks
        max_transport_gap_ms = 0.0   # time between any packet/ping — true transport hang indicator
        max_inter_token_gap_ms = 0.0  # time between substantive tokens — LLM generation speed

        content_parts: List[str] = []
        tool_calls_map: Dict[int, Dict[str, Any]] = {}
        finish_reason = None
        response_id = ""
        model_returned = model
        raw_chunks: List[str] = []
        stream_error = None
        total_data_lines = 0

        req = urllib.request.Request(url, headers=headers, data=json.dumps(body).encode("utf-8"))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                for raw_line in resp:
                    now = time.time()
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    # Track transport liveness on every valid SSE line, including keepalive comments
                    transport_gap = (now - last_packet_time) * 1000.0
                    if transport_gap > max_transport_gap_ms:
                        max_transport_gap_ms = transport_gap
                    last_packet_time = now

                    if line.startswith(":"):  # SSE comment / keepalive — transport alive, skip data processing
                        continue
                    if not line.startswith("data:"):
                        continue

                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break

                    total_data_lines += 1
                    if not ttft_ms:
                        ttft_ms = (now - t_start) * 1000.0

                    # Token-level gap: only between substantive data chunks
                    token_gap_ms = (now - last_token_time) * 1000.0
                    if token_gap_ms > max_inter_token_gap_ms:
                        max_inter_token_gap_ms = token_gap_ms
                    last_token_time = now

                    try:
                        chunk = json.loads(payload)
                    except Exception:
                        continue

                    if "error" in chunk:
                        err_obj = chunk.get("error") or {}
                        stream_error = err_obj.get("message", "SSE stream returned error chunk")
                        finish_reason = "error"
                        break

                    if not response_id and chunk.get("id"):
                        response_id = chunk["id"]
                    if chunk.get("model"):
                        model_returned = chunk["model"]

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    ch = choices[0]
                    if ch.get("finish_reason"):
                        finish_reason = ch["finish_reason"]

                    delta = ch.get("delta") or {}
                    delta_text = delta.get("content") or ""
                    if delta_text:
                        content_parts.append(delta_text)
                        raw_chunks.append(delta_text)

                    delta_tcs = delta.get("tool_calls") or []
                    for dtc in delta_tcs:
                        idx = dtc.get("index") if dtc.get("index") is not None else len(tool_calls_map)
                        if idx not in tool_calls_map:
                            call_id = dtc.get("id") or f"call_{idx}"
                            tool_calls_map[idx] = {
                                "id": call_id,
                                "type": dtc.get("type", "function"),
                                "function": {"name": "", "arguments": ""},
                            }
                        if dtc.get("id"):
                            tool_calls_map[idx]["id"] = dtc["id"]
                        fn_delta = dtc.get("function") or {}
                        if fn_delta.get("name"):
                            tool_calls_map[idx]["function"]["name"] += fn_delta["name"]
                        if fn_delta.get("arguments"):
                            tool_calls_map[idx]["function"]["arguments"] += fn_delta["arguments"]
        except Exception as e:
            logger.error(f"Chat completions stream exception: {e}")
            stream_error = str(e)
            if not finish_reason:
                finish_reason = "error"

        total_latency_ms = (time.time() - t_start) * 1000.0
        final_content = "".join(content_parts) if content_parts else None
        final_tool_calls = [tool_calls_map[i] for i in sorted(tool_calls_map.keys())] or None

        assembled = {
            "id": response_id or f"stream-{int(t_start)}",
            "object": "chat.completion",
            "model": model_returned,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": final_content,
                        "tool_calls": final_tool_calls,
                    },
                    "finish_reason": finish_reason or ("tool_calls" if final_tool_calls else "stop"),
                }
            ],
            "streaming_telemetry": {
                "stream": True,
                "ttft_ms": round(ttft_ms, 2),
                "total_latency_ms": round(total_latency_ms, 2),
                # max_inter_chunk_gap_ms kept for backward-compat with invariants.py (uses transport gap)
                "max_inter_chunk_gap_ms": round(max_transport_gap_ms, 2),
                "max_transport_gap_ms": round(max_transport_gap_ms, 2),
                "max_inter_token_gap_ms": round(max_inter_token_gap_ms, 2),
                "chunk_count": total_data_lines,
                "early_chunks": raw_chunks[:30],
                "stream_error": stream_error,
            },
        }
        return assembled

    def _call_responses_api(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        role: str = "agent",
        stream: bool = False,
        timeout: float = 360.0,
    ) -> Dict[str, Any]:
        """Invoke POST /v1/responses (OpenAI Responses protocol)."""
        url = f"{self.base_url}/v1/responses"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "X-G2A-Run": self.run_id,
            "X-G2A-Role": role,
        }
        if stream:
            headers["Accept"] = "text/event-stream"

        # Map chat messages into responses input format
        input_items = []
        instructions = ""
        for m in messages:
            r = m.get("role")
            c = m.get("content")
            if r == "system":
                instructions = str(c)
            elif r == "user":
                if isinstance(c, list):
                    converted_parts = []
                    for part in c:
                        if not isinstance(part, dict):
                            continue
                        ptype = part.get("type")
                        if ptype == "text":
                            converted_parts.append({"type": "input_text", "text": part.get("text", "")})
                        elif ptype == "image_url":
                            iurl = part.get("image_url", {})
                            url_val = iurl.get("url", "") if isinstance(iurl, dict) else str(iurl)
                            converted_parts.append({"type": "input_image", "image_url": url_val})
                        elif ptype in ("input_text", "input_image"):
                            converted_parts.append(part)
                    input_items.append({"role": "user", "content": converted_parts})
                else:
                    input_items.append({"role": "user", "content": c})
            elif r == "assistant":
                if c:
                    input_items.append({"role": "assistant", "content": c})
                tcs = m.get("tool_calls") or []
                for tc in tcs:
                    fn = tc.get("function") or {}
                    fn_name = fn.get("name", "")
                    fn_args = fn.get("arguments", "")
                    if isinstance(fn_args, dict):
                        fn_args = json.dumps(fn_args)
                    cid = tc.get("id") or f"call_{len(input_items)}"
                    input_items.append({
                        "type": "function_call",
                        "call_id": cid,
                        "name": fn_name,
                        "arguments": fn_args,
                    })
            elif r == "tool":
                cid = m.get("tool_call_id") or "call_0"
                input_items.append({"type": "function_call_output", "call_id": cid, "output": str(c)})

        # Responses tools format mapping
        responses_tools = []
        if tools:
            for t in tools:
                if t.get("type") == "function" and "function" in t:
                    fn = t["function"]
                    responses_tools.append({
                        "type": "function",
                        "name": fn.get("name"),
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {}),
                    })

        body: Dict[str, Any] = {
            "model": model,
            "input": input_items,
            "stream": stream,
        }
        if instructions:
            body["instructions"] = instructions
        if responses_tools:
            body["tools"] = responses_tools

        t_start = time.time()
        req = urllib.request.Request(url, headers=headers, data=json.dumps(body).encode("utf-8"))

        if not stream:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_resp = json.loads(resp.read().decode("utf-8"))
            # Adapt responses output back to common chat completion message structure
            text = ""
            tool_calls = []
            for item in raw_resp.get("output", []) or []:
                if item.get("type") == "message":
                    for c_part in item.get("content", []) or []:
                        text += c_part.get("text", "")
                elif item.get("type") == "function_call":
                    fn_args = item.get("arguments", "")
                    if isinstance(fn_args, dict):
                        fn_args = json.dumps(fn_args)
                    tool_calls.append({
                        "id": item.get("call_id", f"call_{len(tool_calls)}"),
                        "type": "function",
                        "function": {
                            "name": item.get("name"),
                            "arguments": fn_args or "",
                        },
                    })
            return {
                "id": raw_resp.get("id"),
                "model": raw_resp.get("model", model),
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": text or None,
                        "tool_calls": tool_calls or None,
                    },
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }],
                "raw_responses_output": raw_resp,
            }
        else:
            # Streaming responses
            ttft_ms = 0.0
            last_packet_time = t_start   # updated on ANY valid SSE line (including : ping keepalives)
            last_token_time = t_start    # updated only on substantive data chunks
            max_transport_gap_ms = 0.0   # true transport hang indicator
            max_inter_token_gap_ms = 0.0  # LLM generation speed
            text_parts = []
            raw_chunks = []
            tool_calls_dict: Dict[str, Dict[str, Any]] = {}
            item_id_to_call_id: Dict[str, str] = {}
            resp_id = ""
            stream_error = None
            total_data_lines = 0

            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    for raw_line in resp:
                        now = time.time()
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue

                        # Track transport liveness on every valid SSE line, including keepalive comments
                        transport_gap = (now - last_packet_time) * 1000.0
                        if transport_gap > max_transport_gap_ms:
                            max_transport_gap_ms = transport_gap
                        last_packet_time = now

                        if line.startswith(":"):  # SSE comment / keepalive — transport alive, skip data processing
                            continue
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        total_data_lines += 1
                        if not ttft_ms:
                            ttft_ms = (now - t_start) * 1000.0

                        # Token-level gap: only between substantive data chunks
                        token_gap_ms = (now - last_token_time) * 1000.0
                        if token_gap_ms > max_inter_token_gap_ms:
                            max_inter_token_gap_ms = token_gap_ms
                        last_token_time = now

                        try:
                            event_data = json.loads(payload)
                        except Exception:
                            continue

                        etype = event_data.get("type", "")

                        if etype == "response.created":
                            r_obj = event_data.get("response") or {}
                            if r_obj.get("id"):
                                resp_id = r_obj["id"]

                        elif etype == "response.output_item.added":
                            item = event_data.get("item") or {}
                            if item.get("type") == "function_call":
                                iid = item.get("id") or f"fc_{len(tool_calls_dict)}"
                                cid = item.get("call_id") or iid
                                item_id_to_call_id[iid] = cid
                                if cid not in tool_calls_dict:
                                    tool_calls_dict[cid] = {
                                        "id": cid,
                                        "type": "function",
                                        "function": {
                                            "name": item.get("name", ""),
                                            "arguments": item.get("arguments", ""),
                                        },
                                    }

                        elif etype == "response.function_call_arguments.delta":
                            iid = event_data.get("item_id", "")
                            cid = item_id_to_call_id.get(iid) or iid or "call_0"
                            if cid not in tool_calls_dict:
                                tool_calls_dict[cid] = {
                                    "id": cid,
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            delta_arg = event_data.get("delta", "")
                            tool_calls_dict[cid]["function"]["arguments"] += delta_arg

                        elif etype == "response.function_call_arguments.done":
                            iid = event_data.get("item_id", "")
                            cid = item_id_to_call_id.get(iid) or iid
                            if cid in tool_calls_dict and event_data.get("arguments"):
                                tool_calls_dict[cid]["function"]["arguments"] = event_data["arguments"]

                        elif etype == "response.output_item.done":
                            item = event_data.get("item") or {}
                            if item.get("type") == "function_call":
                                cid = item.get("call_id") or item.get("id")
                                if cid and cid in tool_calls_dict and item.get("arguments"):
                                    tool_calls_dict[cid]["function"]["arguments"] = item["arguments"]

                        elif etype == "response.output_text.delta":
                            delta = event_data.get("delta") or ""
                            if delta:
                                text_parts.append(delta)
                                raw_chunks.append(delta)

                        elif etype == "response.completed":
                            r_obj = event_data.get("response") or {}
                            if r_obj.get("id"):
                                resp_id = r_obj["id"]
                            break

                        elif etype == "response.failed":
                            r_obj = event_data.get("response") or {}
                            err_info = r_obj.get("error") or {}
                            stream_error = err_info.get("message", "Responses stream failed")
                            break

                        elif "delta" in event_data and not etype.startswith("response.function_call"):
                            delta = event_data.get("delta") or ""
                            if isinstance(delta, str) and delta:
                                text_parts.append(delta)
                                raw_chunks.append(delta)

            except Exception as e:
                logger.error(f"Responses stream exception: {e}")
                stream_error = str(e)

            final_tcs = list(tool_calls_dict.values()) or None
            final_text = "".join(text_parts) if text_parts else None

            return {
                "id": resp_id or f"resp_stream_{int(t_start)}",
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": final_text,
                        "tool_calls": final_tcs,
                    },
                    "finish_reason": "error" if stream_error else ("tool_calls" if final_tcs else "stop"),
                }],
                "streaming_telemetry": {
                    "stream": True,
                    "protocol": "responses",
                    "ttft_ms": round(ttft_ms, 2),
                    "total_latency_ms": round((time.time() - t_start) * 1000.0, 2),
                    # max_inter_chunk_gap_ms kept for backward-compat with invariants.py (uses transport gap)
                    "max_inter_chunk_gap_ms": round(max_transport_gap_ms, 2),
                    "max_transport_gap_ms": round(max_transport_gap_ms, 2),
                    "max_inter_token_gap_ms": round(max_inter_token_gap_ms, 2),
                    "chunk_count": total_data_lines,
                    "early_chunks": raw_chunks[:30],
                    "stream_error": stream_error,
                },
            }

    def call_agent_api(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        role: str = "agent",
        protocol: str = "chat_completions",
        stream: bool = False,
        timeout: float = 360.0,
    ) -> Dict[str, Any]:
        """Dispatch API call based on protocol and streaming settings."""
        if protocol == "responses":
            return self._call_responses_api(
                messages=messages,
                model=model,
                tools=tools,
                role=role,
                stream=stream,
                timeout=timeout,
            )
        else:
            if stream:
                return self._call_chat_completions_stream(
                    messages=messages,
                    model=model,
                    tools=tools,
                    tool_choice=tool_choice,
                    role=role,
                    timeout=timeout,
                )
            else:
                return self._call_chat_completions_nonstream(
                    messages=messages,
                    model=model,
                    tools=tools,
                    tool_choice=tool_choice,
                    role=role,
                    timeout=timeout,
                )

    def prepare_multimodal_message(
        self,
        prompt: str,
        multimodal_input: Optional[Dict[str, Any]] = None,
        sandbox: Optional[BabysitSandbox] = None,
    ) -> Any:
        """Construct user message content supporting text and images."""
        if not multimodal_input:
            return prompt

        image_url = multimodal_input.get("image_url")
        image_path = multimodal_input.get("image_path")

        if not image_url and image_path and sandbox:
            try:
                target = sandbox._resolve_safe(image_path)
                if target.is_file():
                    image_url = encode_image_as_data_url(target)
            except Exception as e:
                logger.warning(f"Could not resolve image_path {image_path}: {e}")

        if not image_url:
            return prompt

        return [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {
                    "url": image_url,
                    "detail": multimodal_input.get("detail", "auto"),
                },
            },
        ]

    def run_task(self, task_idx: int) -> bool:
        if self.is_stopped():
            return False

        logger.info(f"--- Task {task_idx}/{self.max_tasks} Starting ---")

        task = None
        if self.predefined_tasks and (task_idx - 1) < len(self.predefined_tasks):
            task = dict(self.predefined_tasks[task_idx - 1])
            logger.info(f"Using predefined task {task_idx}: {task.get('task_id')} - '{task.get('title')}'")
        else:
            # Step 1: Orchestrator generates task
            categories = [
                "investigation",
                "tool_use",
                "streaming_imperative",
                "refusal_stress",
                "multistep_planning",
                "responses_protocol",
                "filesystem",
                "tool_recovery",
                "multimodal_vision",
                "format_constraints",
            ]
            cat = categories[(task_idx - 1) % len(categories)]
            orch_query = (
                f"Generate exactly 1 synthetic benchmark task for Argon 4 in category '{cat}'.\n"
                "Output must be a JSON object with top-level key 'task' conforming strictly to task.schema.json.\n"
                "Requirements:\n"
                "- task_id (pattern '^task_[0-9a-zA-Z_-]+$')\n"
                f"- category: '{cat}'\n"
                "- difficulty ('low'|'medium'|'high'|'extreme')\n"
                "- title, prompt (use realistic imperative prompt with relative paths like 'data/input.csv' or 'src/app.py')\n"
                "- initial_files: REQUIRED array of {path, content} for any files referenced in the prompt!\n"
                "- tools: array of {type:'function', function:{name, description, parameters}}\n"
                "- expected_behavior: {success_criteria, timeout_seconds>=360, expected_artifacts}\n"
                "- protocol: 'chat_completions' or 'responses'\n"
                "- stream: true or false"
            )

            for attempt in range(3):
                logger.info(f"Orchestrator ({self.orchestrator_model}) generating task for category: {cat} (attempt {attempt + 1}/3)...")
                try:
                    orch_resp = self._call_chat_completions_nonstream(
                        messages=[
                            {"role": "system", "content": self.orch_prompt},
                            {"role": "user", "content": orch_query},
                        ],
                        model=self.orchestrator_model,
                        role="orchestrator",
                        timeout=120.0,
                    )
                    content = orch_resp["choices"][0]["message"]["content"]
                    task_data = extract_json(content)
                    task = task_data.get("task", task_data)
                    if isinstance(task, dict) and task.get("title"):
                        break
                except Exception as e:
                    logger.warning(f"Orchestrator generation attempt {attempt + 1} failed: {e}")
                    time.sleep(3)

        if not task or not isinstance(task, dict):
            logger.error("Failed to acquire valid task definition")
            return False

        task_id = task.get("task_id", f"task_{task_idx}_{int(time.time())}")
        task["task_id"] = task_id
        logger.info(f"Generated task: {task_id} - '{task.get('title')}'")

        # Save task definition
        with open(self.tasks_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(task) + "\n")

        # Step 2: Initialize Sandbox & Seed Fixtures
        sandbox_dir = self.run_dir / "sandbox" / task_id
        sandbox = BabysitSandbox(sandbox_dir)

        initial_files = task.get("initial_files") or []
        if initial_files:
            seed_res = sandbox.seed_files(initial_files)
            logger.info(f"Seeded {seed_res.get('seeded_count', 0)} fixture files into sandbox for {task_id}.")

        # Determine Protocol and Streaming Mode
        task_protocol = task.get("protocol") or self.default_protocol
        use_stream = self.force_stream if self.force_stream is not None else task.get("stream", False)

        available_tools = task.get("tools") or [
            {
                "type": "function",
                "function": {
                    "name": "run_command",
                    "description": "Execute a shell command inside the task workspace",
                    "parameters": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                },
            }
        ]

        # Step 3: Run Argon with tools (single Argon task at a time - D3)
        logger.info(
            f"Running Argon ({self.argon_model}) on task {task_id} "
            f"[protocol={task_protocol}, stream={use_stream}]..."
        )

        user_content = self.prepare_multimodal_message(
            prompt=task.get("prompt", "Perform the assigned task."),
            multimodal_input=task.get("multimodal_input"),
            sandbox=sandbox,
        )
        messages = [{"role": "user", "content": user_content}]
        task_timeout = float(task.get("expected_behavior", {}).get("timeout_seconds", 360))
        max_turns = int(task.get("expected_behavior", {}).get("max_turns", 4))

        turns_history = []
        all_stream_chunks: List[str] = []
        turn = 0
        argon_start = time.time()

        while turn < max_turns and not self.is_stopped():
            turn += 1
            logger.info(f"Argon turn {turn}/{max_turns}...")
            t0 = time.time()
            try:
                argon_resp = self.call_agent_api(
                    messages=messages,
                    model=self.argon_model,
                    tools=available_tools,
                    tool_choice="auto",
                    role="argon",
                    protocol=task_protocol,
                    stream=use_stream,
                    timeout=max(task_timeout, 360.0),
                )
            except Exception as e:
                logger.error(f"Argon request failed: {e}")
                break

            latency = time.time() - t0
            choice = argon_resp["choices"][0]
            msg = choice.get("message", {})
            finish_reason = choice.get("finish_reason")
            tool_calls = msg.get("tool_calls") or []

            stream_telem = argon_resp.get("streaming_telemetry") or {}
            if stream_telem.get("early_chunks"):
                all_stream_chunks.extend(stream_telem["early_chunks"])

            logger.info(
                f"Argon response in {latency:.2f}s, finish_reason: {finish_reason}, "
                f"tool_calls: {len(tool_calls)}"
                + (f", ttft: {stream_telem.get('ttft_ms')}ms" if stream_telem else "")
            )

            # Telemetry from proxy log
            proxy_rec = self._get_latest_proxy_log(self.run_id, "argon")

            turn_trace = {
                "turn": turn,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "request_messages": list(messages),
                "response": argon_resp,
                "latency_seconds": latency,
                "streaming_telemetry": stream_telem,
                "proxy_telemetry": proxy_rec or {},
            }
            turns_history.append(turn_trace)

            messages.append(msg)

            if tool_calls:
                for tc in tool_calls:
                    fn_name = tc.get("function", {}).get("name", "")
                    fn_args_raw = tc.get("function", {}).get("arguments", "{}")
                    call_id = tc.get("id", f"call_{turn}")
                    logger.info(f"Executing tool call: {fn_name}({fn_args_raw})")
                    if isinstance(fn_args_raw, dict):
                        args = fn_args_raw
                    else:
                        try:
                            args = extract_json(fn_args_raw)
                        except Exception:
                            args = {}

                    if fn_name in ("run_command", "execute_command") and "command" in args:
                        res = sandbox.run_command(args["command"])
                        output = res.get("stdout", "") + (res.get("stderr", "") if res.get("stderr") else "")
                        if not output and res.get("status") == "ok":
                            output = f"Command exited with code {res.get('exit_code', 0)}"
                        elif not output:
                            output = json.dumps(res)
                    elif fn_name == "python_eval" and "code" in args:
                        # Safe python evaluation avoiding Windows shell escaping errors
                        res = sandbox.eval_python(args["code"])
                        output = res.get("stdout", "") + (res.get("stderr", "") if res.get("stderr") else "")
                        if not output and res.get("status") == "ok":
                            output = f"Python completed with code {res.get('exit_code', 0)}"
                        elif not output:
                            output = json.dumps(res)
                    elif fn_name == "read_file" and "path" in args:
                        res = sandbox.read_file(args["path"])
                        output = res.get("content", "") if res.get("status") == "ok" else json.dumps(res)
                    elif fn_name == "write_file" and "path" in args and "content" in args:
                        res = sandbox.write_file(args["path"], args["content"])
                        output = json.dumps(res)
                    elif fn_name == "list_dir":
                        res = sandbox.list_dir(args.get("path", "."))
                        output = json.dumps(res)
                    else:
                        output = f"Tool {fn_name} executed with result: OK"

                    if not isinstance(output, str):
                        output = json.dumps(output)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": output,
                    })
            else:
                # Finished without tool calls
                break

        total_argon_time = time.time() - argon_start

        # Step 4: Run Deterministic Semantic Invariants
        semantic_findings = run_all_semantic_invariants(
            task=task,
            turns_history=turns_history,
            sandbox=sandbox,
            streaming_chunks=all_stream_chunks if use_stream else None,
            ttft_ms=turns_history[-1].get("streaming_telemetry", {}).get("ttft_ms", 0.0) if turns_history else 0.0,
            max_gap_ms=turns_history[-1].get("streaming_telemetry", {}).get("max_inter_chunk_gap_ms", 0.0) if turns_history else 0.0,
        )
        for sf in semantic_findings:
            sf["finding_id"] = f"finding_sem_{task_id}_{uuid_str()[:6]}"
            sf["task_id"] = task_id
            logger.warning(f"Semantic Invariant Alert [{sf.get('invariant_type')}]: {sf.get('summary')}")
            with open(self.findings_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(sf) + "\n")

        # Save trace
        full_trace = {
            "task_id": task_id,
            "run_id": self.run_id,
            "protocol": task_protocol,
            "stream": use_stream,
            "turns_count": len(turns_history),
            "total_time_seconds": total_argon_time,
            "semantic_violations_count": len(semantic_findings),
            "turns": turns_history,
        }
        with open(self.trace_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(full_trace) + "\n")

        # Step 5: Monitor evaluates and produces comprehensive finding
        logger.info(f"Calling Monitor ({self.monitor_model}) to review task {task_id} execution...")

        # Prepare rich context for Monitor
        last_turn_content = ""
        last_turn_calls = []
        if turns_history:
            last_resp = turns_history[-1].get("response", {})
            last_msg = (last_resp.get("choices") or [{}])[0].get("message", {})
            last_turn_content = last_msg.get("content") or ""
            last_turn_calls = last_msg.get("tool_calls") or []

        mon_input = f"""
EVALUATE ARGON EXECUTION TELEMETRY & CONVERSATIONAL TRACE:
Run ID: {self.run_id}
Task ID: {task_id}
Task Category: {task.get('category')}
Task Title: {task.get('title')}
Protocol: {task_protocol} | Stream: {use_stream}
Turns Executed: {len(turns_history)}
Total Duration: {total_argon_time:.2f}s
Deterministic Invariant Violations: {json.dumps([f.get('invariant_type') for f in semantic_findings])}
Latest Agent Content: {json.dumps(last_turn_content[:400])}
Latest Tool Calls: {json.dumps(last_turn_calls)}
Latest Turn Telemetry: {json.dumps(turns_history[-1].get('proxy_telemetry') if turns_history else {})}

INSTRUCTIONS:
Produce a JSON object with top-level key 'finding' conforming strictly to finding.schema.json:
- finding_id (pattern '^finding_[0-9a-zA-Z_-]+$')
- category ('argon'|'proxy'|'pool_cookies'|'refresher_noise'|'orchestrator'|'semantic_invariant'|'streaming')
- invariant_type (e.g. 'leaked_refusal_phrase', 'internal_code_interpreter_leak', 'prose_refusal_instead_of_tool', 'tool_repetition_loop', 'artifact_missing', 'stream_chunk_stall', 'none')
- severity ('info'|'warning'|'error'|'critical')
- confidence (0.0 to 1.0)
- summary (string <= 256)
- evidence ({{request_id, timestamp, quoted_log, metric_name, metric_value, task_id}})
- proposed_fix (actionable engineering recommendation)
"""
        finding = None
        for mon_attempt in range(2):
            try:
                mon_resp = self._call_chat_completions_nonstream(
                    messages=[
                        {"role": "system", "content": self.mon_prompt},
                        {"role": "user", "content": mon_input},
                    ],
                    model=self.monitor_model,
                    role="monitor",
                    timeout=120.0,
                )
                mon_content = mon_resp["choices"][0]["message"]["content"]
                mon_data = extract_json(mon_content)
                finding = mon_data.get("finding", mon_data)
                if isinstance(finding, dict) and finding.get("summary"):
                    finding["task_id"] = task_id
                    break
            except Exception as e:
                logger.warning(f"Monitor attempt {mon_attempt + 1} failed: {e}")
                time.sleep(2)

        if finding and isinstance(finding, dict):
            logger.info(f"Monitor Finding [{finding.get('category')} / {finding.get('severity')}]: {finding.get('summary')}")
            with open(self.findings_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(finding) + "\n")
        else:
            logger.error(f"Monitor failed to produce valid finding for {task_id}")

        self.completed_task_ids.add(task_id)
        self.completed_file.write_text(json.dumps(list(self.completed_task_ids), indent=2), encoding="utf-8")
        return True

    def run(self):
        logger.info(f"Starting BabysitRunner for run_id='{self.run_id}', max_tasks={self.max_tasks}...")
        start_idx = len(self.completed_task_ids) + 1
        if start_idx > self.max_tasks:
            logger.info(f"All {self.max_tasks} tasks already completed for run_id='{self.run_id}'.")
            return

        for i in range(start_idx, self.max_tasks + 1):
            if self.is_stopped():
                break
            success = self.run_task(i)
            if not success:
                break
            if i < self.max_tasks:
                logger.info("Pausing 5s between tasks...")
                time.sleep(5)

        logger.info(f"Runner completed for run_id='{self.run_id}'. Finished {len(self.completed_task_ids)} tasks.")


def uuid_str() -> str:
    import uuid
    return uuid.uuid4().hex


def main():
    parser = argparse.ArgumentParser(description="Argon 4 Babysitting Runner")
    parser.add_argument("--base-url", default="http://127.0.0.1:5918")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--max-tasks", type=int, default=5, help="Maximum tasks to execute (ceiling 20)")
    parser.add_argument("--orchestrator-model", default="gemini-flash")
    parser.add_argument("--argon-model", default="gemini-4-argon")
    parser.add_argument("--monitor-model", default="gemini-flash")
    parser.add_argument(
        "--protocol",
        choices=["chat_completions", "responses", "auto"],
        default="chat_completions",
        help="Protocol to benchmark",
    )
    parser.add_argument("--stream", action="store_true", help="Force SSE streaming mode on all agent requests")
    parser.add_argument("--tasks-file", default=None, help="Path to JSONL file with predefined benchmark tasks to run")
    args = parser.parse_args()

    runner = BabysitRunner(
        base_url=args.base_url,
        api_key=args.api_key,
        run_id=args.run_id,
        max_tasks=args.max_tasks,
        orchestrator_model=args.orchestrator_model,
        argon_model=args.argon_model,
        monitor_model=args.monitor_model,
        default_protocol="chat_completions" if args.protocol == "auto" else args.protocol,
        force_stream=True if args.stream else None,
        tasks_file=args.tasks_file,
    )
    runner.run()


if __name__ == "__main__":
    main()
