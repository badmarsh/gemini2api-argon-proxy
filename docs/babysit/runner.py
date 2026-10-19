"""Automated benchmark runner for Gemini 4 Argon babysitting experiment.

Orchestrates the 3-party loop:
1. Orchestrator (gemini-flash): Generates synthetic task matching task.schema.json
2. Agent (gemini-4-argon): Attempts to solve the task using tools in isolated sandbox
3. Monitor (gemini-flash): Inspects proxy telemetry, logs, and trace to produce findings

Enforces:
- Strict concurrency = 1 (D3)
- Task ceiling (D5, default 5, max 20)
- Kill-switch monitoring (STOP, STOP_BABYSIT)
- Safe sandbox isolation
"""

from __future__ import annotations

import argparse
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
from typing import Any, Dict, List, Optional

# Add docs/babysit to sys.path so we can import sandbox
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
from sandbox import AgentSandbox as BabysitSandbox

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
            cleaned = re.sub(r'[\x00-\x1f]', lambda ch: f'\\u{ord(ch.group()):04x}' if ch.group() not in '\r\n\t' else ch.group(), candidate)
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

        self.max_tasks = min(max_tasks, 20)  # Enforce D5 ceiling
        self.orchestrator_model = orchestrator_model
        self.argon_model = argon_model
        self.monitor_model = monitor_model

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

    def _call_api(
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

    def run_task(self, task_idx: int) -> bool:
        if self.is_stopped():
            return False

        logger.info(f"--- Task {task_idx}/{self.max_tasks} Starting ---")

        # Step 1: Orchestrator generates task
        categories = ["tool_use", "filesystem", "multistep_planning", "tool_recovery", "format_constraints"]
        cat = categories[(task_idx - 1) % len(categories)]
        orch_query = (
            f"Generate exactly 1 synthetic benchmark task for Argon 4 in category '{cat}'. "
            "Output must be a JSON object with top-level key 'task' conforming strictly to the task schema.\n"
            "Requirements:\n"
            "- task_id (pattern '^task_[0-9a-zA-Z_-]+$')\n"
            "- category ('tool_use'|'multistep_planning'|'long_context'|'faulty_ambiguous'|'tool_recovery'|'filesystem'|'stall_resistance'|'format_constraints')\n"
            "- difficulty ('low'|'medium'|'high'|'extreme')\n"
            "- title, prompt\n"
            "- tools: array of {type:'function', function:{name, description, parameters}}\n"
            "- expected_behavior: {success_criteria, timeout_seconds>=360}"
        )

        task = None
        for attempt in range(3):
            logger.info(f"Orchestrator ({self.orchestrator_model}) generating task for category: {cat} (attempt {attempt + 1}/3)...")
            try:
                orch_resp = self._call_api(
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
            logger.error(f"Failed to generate valid task for category {cat}")
            return False

        task_id = task.get("task_id", f"task_{task_idx}_{int(time.time())}")
        task["task_id"] = task_id
        logger.info(f"Generated task: {task_id} - '{task.get('title')}'")

        # Save task
        with open(self.tasks_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(task) + "\n")

        # Step 2: Initialize Sandbox
        sandbox_dir = self.run_dir / "sandbox" / task_id
        sandbox = BabysitSandbox(sandbox_dir)

        # Standard safe tools supported in sandbox
        available_tools = task.get("tools") or [
            {
                "type": "function",
                "function": {
                    "name": "execute_command",
                    "description": "Execute a shell command inside the isolated task sandbox",
                    "parameters": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                },
            }
        ]

        # Step 3: Run Argon with tools (single Argon task at a time - D3)
        logger.info(f"Running Argon ({self.argon_model}) on task {task_id}...")
        messages = [{"role": "user", "content": task.get("prompt", "Perform the assigned task.")}]
        task_timeout = float(task.get("expected_behavior", {}).get("timeout_seconds", 360))
        max_turns = int(task.get("expected_behavior", {}).get("max_turns", 3))

        turns_history = []
        turn = 0
        argon_start = time.time()

        while turn < max_turns and not self.is_stopped():
            turn += 1
            logger.info(f"Argon turn {turn}/{max_turns}...")
            t0 = time.time()
            try:
                argon_resp = self._call_api(
                    messages=messages,
                    model=self.argon_model,
                    tools=available_tools,
                    tool_choice="auto",
                    role="argon",
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

            logger.info(f"Argon response in {latency:.2f}s, finish_reason: {finish_reason}, tool_calls: {len(tool_calls)}")

            # Telemetry from proxy log
            proxy_rec = self._get_latest_proxy_log(self.run_id, "argon")

            turn_trace = {
                "turn": turn,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "request_messages": list(messages),
                "response": argon_resp,
                "latency_seconds": latency,
                "proxy_telemetry": proxy_rec or {},
            }
            turns_history.append(turn_trace)

            messages.append(msg)

            if finish_reason == "tool_calls" and tool_calls:
                for tc in tool_calls:
                    fn_name = tc.get("function", {}).get("name", "")
                    fn_args_raw = tc.get("function", {}).get("arguments", "{}")
                    call_id = tc.get("id", f"call_{turn}")
                    logger.info(f"Executing tool call: {fn_name}({fn_args_raw})")
                    try:
                        args = json.loads(fn_args_raw)
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
                        res = sandbox.run_command(f'python3 -c {json.dumps(args["code"])}')
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

        # Save trace
        full_trace = {
            "task_id": task_id,
            "run_id": self.run_id,
            "turns_count": len(turns_history),
            "total_time_seconds": total_argon_time,
            "turns": turns_history,
        }
        with open(self.trace_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(full_trace) + "\n")

        # Step 4: Monitor evaluates and produces finding
        logger.info(f"Calling Monitor ({self.monitor_model}) to review task {task_id} execution...")
        mon_input = f"""
EVALUATE ARGON EXECUTION TELEMETRY:
Run ID: {self.run_id}
Task ID: {task_id}
Task Category: {task.get('category')}
Task Title: {task.get('title')}
Turns: {len(turns_history)}
Total Duration: {total_argon_time:.2f}s
Latest Turn Telemetry: {json.dumps(turns_history[-1].get('proxy_telemetry') if turns_history else {})}

INSTRUCTIONS:
Produce a JSON object with top-level key 'finding' conforming strictly to finding.schema.json:
- finding_id (pattern '^finding_[0-9a-zA-Z_-]+$')
- category ('argon'|'proxy'|'pool_cookies'|'refresher_noise'|'orchestrator')
- severity ('info'|'warning'|'error'|'critical')
- confidence (0.0 to 1.0)
- summary (string <= 256)
- evidence ({{request_id, timestamp, quoted_log, metric_name, metric_value, task_id}})
- proposed_fix (actionable engineering recommendation)
"""
        finding = None
        for mon_attempt in range(2):
            try:
                mon_resp = self._call_api(
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


def main():
    parser = argparse.ArgumentParser(description="Argon 4 Babysitting Runner")
    parser.add_argument("--base-url", default="http://127.0.0.1:5918")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--max-tasks", type=int, default=5, help="Maximum tasks to execute (ceiling 20)")
    parser.add_argument("--orchestrator-model", default="gemini-flash")
    parser.add_argument("--argon-model", default="gemini-4-argon")
    parser.add_argument("--monitor-model", default="gemini-flash")
    args = parser.parse_args()

    runner = BabysitRunner(
        base_url=args.base_url,
        api_key=args.api_key,
        run_id=args.run_id,
        max_tasks=args.max_tasks,
        orchestrator_model=args.orchestrator_model,
        argon_model=args.argon_model,
        monitor_model=args.monitor_model,
    )
    runner.run()


if __name__ == "__main__":
    main()
