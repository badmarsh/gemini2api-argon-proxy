"""Telemetry and timeline collector for the Argon 4 babysitting experiment.

Merges:
1. Proxy debug metrics (/admin/debug-metrics)
2. Proxy structured logs (/admin/logs)
3. Refresher docker container logs (PUT / skipped events)
Into a correlated `runs/<run_id>/timeline.jsonl`.
Computes TTFT statistics, stall rates, and substitution flags (model != served_by).
Enforces kill-switch and auto-abort fuses.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("collector")


def resolve_api_key(cli_key: str | None = None) -> str:
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


class BabysitCollector:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:5918",
        api_key: str | None = None,
        run_id: str | None = None,
        runs_dir: str | Path = "runs",
        poll_interval: float = 4.0,
        consecutive_5xx_limit: int = 5,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = resolve_api_key(api_key)
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.run_dir = Path(runs_dir) / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.timeline_file = self.run_dir / "timeline.jsonl"
        self.metrics_file = self.run_dir / "metrics.jsonl"
        self.kill_switch_files = [
            self.run_dir / "STOP",
            self.run_dir / "STOP_BABYSIT",
            Path("STOP_BABYSIT"),
        ]
        self.poll_interval = poll_interval
        self.consecutive_5xx_limit = consecutive_5xx_limit

        self.seen_request_ids: Set[str] = set()
        self.seen_log_ids: Set[str] = set()
        self.consecutive_5xx = 0
        self.last_refresher_ts: str = ""

    def _http_get(self, path: str) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            logger.error("HTTP GET %s failed with %d: %s", path, e.code, e.reason)
            return {"error": e.code, "message": str(e.reason)}
        except Exception as e:
            logger.error("HTTP GET %s error: %s", path, e)
            return {"error": "exception", "message": str(e)}

    def check_kill_switches(self) -> bool:
        """Return True if any kill-switch file is found."""
        for kf in self.kill_switch_files:
            if kf.exists():
                logger.warning("Kill switch file detected: %s. Aborting.", kf)
                return True
        return False

    def check_pool_health(self) -> Dict[str, Any]:
        """Query /admin/status and verify active accounts."""
        status = self._http_get("/admin/status")
        if status.get("error"):
            return {
                "healthy": False,
                "error": status.get("error"),
                "message": status.get("message"),
                "active": 0,
                "total": 0,
                "raw": status,
            }
        active = status.get("active", 0)
        total = status.get("total", 0)
        return {
            "healthy": active > 0,
            "active": active,
            "total": total,
            "raw": status,
        }

    def poll_refresher_events(self) -> List[Dict[str, Any]]:
        """Read recent gemini2api-refresher container logs."""
        events = []
        try:
            cmd = ["docker", "logs", "--tail", "50", "--timestamps", "gemini2api-refresher"]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if proc.returncode == 0:
                lines = proc.stdout.splitlines()
                for line in lines:
                    if not line.strip():
                        continue
                    parts = line.split(" ", 1)
                    ts = parts[0]
                    msg = parts[1] if len(parts) > 1 else ""
                    if self.last_refresher_ts and ts <= self.last_refresher_ts:
                        continue
                    if any(term in msg for term in ["skipped source", "Opening browser", "OK (source", "skipped:", "Summary:"]):
                        events.append({
                            "type": "refresher_event",
                            "ts": ts,
                            "message": msg.strip(),
                        })
                if lines:
                    self.last_refresher_ts = lines[-1].split(" ", 1)[0]
        except Exception as e:
            logger.warning("Could not poll refresher container logs: %s", e)
        return events

    def poll_debug_metrics(self) -> List[Dict[str, Any]]:
        data = self._http_get("/admin/debug-metrics")
        records = data.get("records", [])
        new_records = []
        for r in records:
            req_id = r.get("request_id")
            if req_id and req_id not in self.seen_request_ids:
                self.seen_request_ids.add(req_id)
                # Compute and annotate
                req_model = r.get("model", "")
                served_by = r.get("served_by", "")
                r["substitution_suspected"] = bool(req_model and served_by and req_model != served_by)
                new_records.append(r)
        return new_records

    def poll_structured_logs(self) -> List[Dict[str, Any]]:
        data = self._http_get("/admin/logs?limit=50")
        records = data.get("records", [])
        new_logs = []
        for log in records:
            log_id = log.get("id")
            if log_id and log_id not in self.seen_log_ids:
                self.seen_log_ids.add(log_id)
                new_logs.append(log)
        return new_logs

    def collect_once(self) -> Dict[str, Any]:
        """Perform a single collection cycle and return summary."""
        # 1. Kill switch check
        if self.check_kill_switches():
            return {"abort": True, "reason": "kill_switch"}

        # 2. Pool health check
        pool_status = self.check_pool_health()
        if not pool_status["healthy"]:
            if pool_status.get("error") == 401:
                logger.error("HTTP 401 Unauthorized: API key invalid or missing! Set $env:API_KEY, pass --api-key, or ensure API_KEY is in .env.")
                return {"abort": True, "reason": "unauthorized"}
            logger.error("Pool has 0 active accounts! Auto-stop triggered.")
            return {"abort": True, "reason": "pool_depleted"}

        # 3. Pull telemetry
        new_debug = self.poll_debug_metrics()
        new_logs = self.poll_structured_logs()
        new_refresher = self.poll_refresher_events()

        # 4. Check consecutive 5xx errors
        for r in new_debug:
            status = r.get("status", 200)
            if status >= 500:
                self.consecutive_5xx += 1
                if self.consecutive_5xx >= self.consecutive_5xx_limit:
                    logger.error("Consecutive 5xx limit (%d) exceeded! Auto-stop triggered.", self.consecutive_5xx_limit)
                    return {"abort": True, "reason": "consecutive_5xx_limit"}
            else:
                self.consecutive_5xx = 0

        # 5. Append to timeline.jsonl
        timeline_entries = []
        for r in new_debug:
            timeline_entries.append({"source": "debug_metrics", "ts": r.get("ts"), "data": r})
        for log in new_logs:
            timeline_entries.append({"source": "admin_logs", "ts": log.get("ts"), "data": log})
        for ref in new_refresher:
            timeline_entries.append({"source": "refresher", "ts": ref.get("ts"), "data": ref})

        # Sort chronologically
        timeline_entries.sort(key=lambda x: str(x.get("ts") or ""))

        if timeline_entries:
            with open(self.timeline_file, "a", encoding="utf-8") as f:
                for entry in timeline_entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # 6. Compute metrics
        all_ttft = [float(r.get("ttft_ms") or 0) for r in new_debug if r.get("ttft_ms")]
        stalls = [r for r in new_debug if float(r.get("ttft_ms") or 0) > 120_000.0 or r.get("error_class") == "upstream_timeout"]
        substitutions = [r for r in new_debug if r.get("substitution_suspected")]
        streaming_requests = [r for r in new_debug if r.get("stream")]
        responses_requests = [r for r in new_debug if "/responses" in str(r.get("path", ""))]
        stream_gap_stalls = [r for r in new_debug if float(r.get("max_inter_chunk_gap_ms") or 0) > 15_000.0]
        parser_issues = [r for r in new_debug if r.get("error_class") in ("tool_json_invalid", "web_error_text")]

        stats = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "collected_debug_count": len(new_debug),
            "collected_logs_count": len(new_logs),
            "collected_refresher_count": len(new_refresher),
            "pool_active": pool_status["active"],
            "pool_total": pool_status["total"],
            "new_stalls": len(stalls),
            "new_substitutions": len(substitutions),
            "avg_ttft_ms": round(sum(all_ttft) / len(all_ttft), 2) if all_ttft else 0.0,
            "max_ttft_ms": round(max(all_ttft), 2) if all_ttft else 0.0,
            "streaming_requests_count": len(streaming_requests),
            "responses_requests_count": len(responses_requests),
            "stream_gap_stalls_count": len(stream_gap_stalls),
            "parser_issues_count": len(parser_issues),
        }

        with open(self.metrics_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(stats) + "\n")

        return {"abort": False, "stats": stats}

    def run_daemon(self, max_iterations: int | None = None) -> None:
        logger.info("Collector daemon started for run '%s' -> %s", self.run_id, self.run_dir)
        iters = 0
        try:
            while True:
                res = self.collect_once()
                if res.get("abort"):
                    logger.warning("Collector stopping due to: %s", res.get("reason"))
                    break
                iters += 1
                if max_iterations and iters >= max_iterations:
                    break
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            logger.info("Collector daemon stopped by user.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Babysit Telemetry Collector")
    parser.add_argument("--url", default="http://127.0.0.1:5918", help="gemini2api base URL")
    parser.add_argument("--key", default=None, help="API key")
    parser.add_argument("--run-id", default=None, help="Experiment run ID")
    parser.add_argument("--interval", type=float, default=4.0, help="Polling interval in seconds")
    parser.add_argument("--once", action="store_true", help="Execute single collection pass")
    args = parser.parse_args()

    collector = BabysitCollector(
        base_url=args.url,
        api_key=args.key,
        run_id=args.run_id,
        poll_interval=args.interval,
    )
    if args.once:
        res = collector.collect_once()
        print(json.dumps(res, indent=2))
    else:
        collector.run_daemon()
