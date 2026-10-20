import asyncio
import logging
import os
import platform
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app.config import APP_VERSION, mask_secret, settings
from app.core.account_pool import account_pool, AccountStatus
from app.core import agent_loop, argon_breaker, regeneration_budget
from app.core.debug_metrics import metrics as debug_metrics
from app.core.gemini_client import GeminiWebClient, count_argon_salvaged_results
from app.utils.tools import is_malformed_tool_result, parse_tool_response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["Admin"])

_start_time = time.time()


def _masked_status() -> dict:
    """Mask Google authentication tokens (psid, psidts) at response boundary without mutating internal pool state."""
    status = account_pool.get_status()
    for acc in status.get("accounts", []):
        if acc.get("psid"):
            acc["psid"] = mask_secret(acc["psid"])
        if acc.get("psidts"):
            acc["psidts"] = mask_secret(acc["psidts"])
    return status


class ReloadCookiesRequest(BaseModel):
    psid: Optional[str] = None
    psidts: Optional[str] = None


class AddAccountRequest(BaseModel):
    psid: str
    psidts: str = ""
    label: str = ""


class ToolJsonTestRequest(BaseModel):
    text: str


@router.post("/reload-cookies")
async def reload_cookies(req: ReloadCookiesRequest = None):
    # 修复：result 仅在 `if account.client:` 内赋值，若账号池为空或所有账号无 client，
    # 循环体不执行、result 始终未绑定，后续 result.get(...) 会抛 UnboundLocalError
    # （显式 cookie 分支直接 500，.env 分支被误判为 "Failed to read .env"），
    # 且末尾 "No accounts available" 成为死代码。初始化 result=None 并在循环后兜底返回 503。
    if req and (req.psid or req.psidts):
        result = None
        for account in account_pool.accounts:
            if account.client:
                result = await account.client.reload_cookies(psid=req.psid, psidts=req.psidts)
                if result.get("success"):
                    return {"status": "ok", "message": "Cookies reloaded successfully", "healthy": True}
        if result is None:
            return JSONResponse(
                status_code=503,
                content={"error": {"message": "No accounts available", "type": "reload_error"}},
            )
        return JSONResponse(
            status_code=503,
            content={"error": {"message": result.get("error", "Cookie reload failed"), "type": "reload_error"}},
        )
    else:
        from app.config import Settings
        try:
            fresh = Settings()
            result = None
            for account in account_pool.accounts:
                if account.client:
                    result = await account.client.reload_cookies(
                        psid=fresh.gemini_psid,
                        psidts=fresh.gemini_psidts,
                    )
                    if result.get("success"):
                        return {"status": "ok", "message": "Cookies reloaded successfully", "healthy": True}
            if result is None:
                return JSONResponse(
                    status_code=503,
                    content={"error": {"message": "No accounts available", "type": "reload_error"}},
                )
            return JSONResponse(
                status_code=503,
                content={"error": {"message": result.get("error", "Cookie reload failed"), "type": "reload_error"}},
            )
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"error": {"message": f"Failed to read .env: {e}", "type": "config_error"}},
            )


@router.get("/status")
async def admin_status():
    """Account pool + the Argon/agent-loop state an operator needs to explain "it got slow" or "it answered
    nothing": which model Argon requests will be served by right now, how often the loop needed rescuing, and
    how many Argon replies came back with frames but no answer at all."""
    status = _masked_status()
    status["argon"] = argon_breaker.snapshot()
    status["agent_loop"] = agent_loop.stats()
    status["agent_loop"]["tool_loop_reminder"] = bool(getattr(settings, "tool_loop_reminder", True))
    status["agent_loop"]["stall_guard"] = bool(getattr(settings, "tool_loop_stall_guard", True))
    status["agent_loop"]["salvaged_empty_streams"] = count_argon_salvaged_results()
    status["regenerations"] = regeneration_budget.stats()
    status["debug_metrics"] = debug_metrics.summary()
    status["agent_loop"]["fallback_model"] = settings.argon_fallback_model
    status["slo"] = _slo_view()
    return status


def _slo_view() -> dict:
    """Operational view (ok / degraded / unknown) built from the bounded diagnostics.

    Kept separate from ``admin_status`` so the same view can be reused by the
    metrics exporter and by tests without going through HTTP.
    """
    from app.core.slo import build_slo

    counters = debug_metrics.counters()
    return build_slo(
        windows=debug_metrics.window_summary(),
        breaker=argon_breaker.snapshot(),
        pool=account_pool.health_summary(),
        budget=regeneration_budget.stats(),
        lifetime_requests=int(counters.get("requests") or 0),
    )


@router.get("/debug-metrics")
async def debug_metrics_status():
    """Return bounded request diagnostics without exposing prompts, cookies or account IDs."""
    return {
        "summary": debug_metrics.summary(),
        "records": debug_metrics.records(limit=200),
    }


@router.get("/config")
async def admin_config():
    """Effective runtime configuration, its sources and its drift.

    Secret material is never rendered: for API keys and cookies only "configured"
    and the source are reported. The values themselves come from the existing
    settings schema, not from a second configuration system.
    """
    from app.core.config_diagnostics import build_config_report

    return build_config_report(settings)


@router.get("/debug-metrics/{request_id}")
async def debug_metrics_request(request_id: str):
    """Drill-down for one request ID: a correlated, redacted timeline.

    Only bounded diagnostics are exposed -- never a request or response body, a
    prompt, a cookie, an Authorization header or an account identifier.
    """
    from app.core.request_timeline import build_timeline

    timeline = build_timeline(debug_metrics.record_by_id(request_id))
    if timeline is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "message": "Request not found in the retained diagnostics buffer",
                    "type": "not_found",
                }
            },
        )
    return {"request": timeline}


@router.get("/metrics")
async def prometheus_metrics():
    """Prometheus exposition of the same bounded, redacted diagnostics.

    The admin JSON endpoint stays the source of truth for the web console; this is a
    scrape-friendly rendering of it. It lives under ``/admin`` so it is protected by
    the admin key (the global API key would otherwise expose operational internals to
    every client of the business API).
    """
    from app.core.prometheus_metrics import CONTENT_TYPE, render_metrics

    snapshot = {
        "uptime_seconds": max(0.0, time.time() - _start_time),
        "counters": debug_metrics.counters(),
        "records": debug_metrics.records(limit=2000),
        "windows": debug_metrics.window_summary(),
        "breaker": argon_breaker.snapshot(),
        "pool": account_pool.health_summary(),
        "budget": regeneration_budget.stats(),
    }
    return Response(content=render_metrics(snapshot), media_type=CONTENT_TYPE)


@router.post("/debug/tool-json")
async def validate_tool_json(req: ToolJsonTestRequest):
    """Exercise the same conservative tool parser used by API responses without regenerating."""
    diagnostics = {}
    parsed = parse_tool_response(req.text, diagnostics=diagnostics)
    calls = parsed.get("tool_calls", []) if isinstance(parsed, dict) else []
    return {
        "valid": not is_malformed_tool_result(parsed),
        "result_type": parsed.get("type") if isinstance(parsed, dict) else None,
        "tool_call_count": len(calls) if isinstance(calls, list) else 0,
        "tool_names": [str(c.get("name", ""))[:128] for c in calls if isinstance(c, dict)][:32],
        "repaired": bool(diagnostics.get("repaired")),
        "failure_class": diagnostics.get("failure_class", ""),
        "response_length": diagnostics.get("response_length", len(req.text)),
        "truncated_suspected": bool(diagnostics.get("truncated_suspected")),
    }


@router.post("/argon/reset")
async def reset_argon_breaker():
    """Close the Argon stall breaker by hand (e.g. right after a Google-side incident is over) so Argon is
    probed again on the next request instead of waiting out ARGON_STALL_BREAKER_SECONDS."""
    was_open = argon_breaker.is_open()
    argon_breaker.reset()
    logger.warning(f"Argon stall breaker manually reset (was_open={was_open})")
    return {"success": True, "was_open": was_open, "argon": argon_breaker.snapshot()}


@router.get("/system-info")
async def system_info():
    import psutil
    proc = psutil.Process(os.getpid())
    mem = proc.memory_info()
    total_mem = psutil.virtual_memory().total
    uptime_seconds = int(time.time() - _start_time)

    return {
        "version": APP_VERSION,
        "python_version": platform.python_version(),
        "server_time": datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
        "os": f"{platform.system()} {platform.release()}",
        "memory_usage": mem.rss // (1024 * 1024),
        "memory_total": total_mem // (1024 * 1024),
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "pid": os.getpid(),
        "run_mode": "Docker" if os.path.exists("/.dockerenv") else "Standalone",
        "uptime_seconds": uptime_seconds,
        # The UI hides the restart button when this is False (restart answers 501 there).
        "restart_supported": _restart_supported(),
    }


@router.get("/check-account")
async def check_all_accounts():
    results = await account_pool.check_all()
    return {"accounts": results}


@router.get("/health-history")
async def health_history():
    history = []
    for account in account_pool.accounts:
        if account.client:
            history.extend(
                {**r, "account_id": account.id} for r in account.client.check_history
            )
    history.sort(key=lambda x: x.get("checked_at", ""), reverse=True)
    return {"total": len(history), "records": history[:50]}


@router.get("/accounts")
async def list_accounts():
    return _masked_status()


@router.post("/accounts")
async def add_account(req: AddAccountRequest):
    try:
        account = await account_pool.add_account(
            psid=req.psid,
            psidts=req.psidts,
            label=req.label,
        )
        return {
            "status": "ok",
            "account": {
                "id": account.id,
                "label": account.label,
                "status": account.status.value,
            },
        }
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": {"message": str(e), "type": "add_account_error"}},
        )


@router.delete("/accounts/{account_id}")
async def remove_account(account_id: str):
    removed = await account_pool.remove_account(account_id)
    if removed:
        return {"status": "ok", "message": f"Account {account_id} removed"}
    return JSONResponse(
        status_code=404,
        content={"error": {"message": f"Account {account_id} not found", "type": "not_found"}},
    )


@router.get("/accounts/{account_id}/check")
async def check_single_account(account_id: str):
    try:
        result = await account_pool.check_account(account_id)
        return result
    except ValueError as e:
        return JSONResponse(
            status_code=404,
            content={"error": {"message": str(e), "type": "not_found"}},
        )


class AccountStatusRequest(BaseModel):
    enabled: bool


@router.post("/accounts/{account_id}/status")
async def set_account_status(account_id: str, req: AccountStatusRequest):
    """Enable or disable one account.

    The change is runtime-only (the credentials stay untouched) and is applied
    immediately: a disabled account is no longer picked by the rotation.
    """
    try:
        result = account_pool.set_account_enabled(account_id, req.enabled)
    except ValueError as e:
        return JSONResponse(
            status_code=404,
            content={"error": {"message": str(e), "type": "not_found"}},
        )
    return {"status": "ok", "account": result}


class UpdateCookiesRequest(BaseModel):
    psid: str
    psidts: str = ""
    persist: bool = True


def _normalize_cookie(value: str) -> str:
    """Same normalisation GeminiWebClient.reload_cookies applies before it stores a value."""
    return (value or "").strip().strip('"').strip("'").rstrip(";")


@asynccontextmanager
async def _serialized_with_self_heal(client):
    """Hold the client's heal lock for the duration of a cookie reload.

    generate()/generate_stream() and the account pool already reload an unhealthy client under
    ``_heal_lock``. A push that reloads without it overlaps that self-heal: reload_cookies marks the
    client unhealthy while it re-fetches the session token, a request arriving in that window starts
    its own second reload, and the two trample each other's HTTP session and token (live symptom: a
    chat 502 a few milliseconds after the PUT finished). Taking the same lock makes the request wait
    for the push instead.
    """
    lock = getattr(client, "_heal_lock", None)
    if lock is None:
        yield
        return
    async with lock:
        yield


def _push_is_noop(client, req: "UpdateCookiesRequest") -> bool:
    """True when the client is healthy and already uses exactly the pair being pushed.

    The refresher re-pushes every account each cycle, whether or not Google rotated anything.
    A reload for an unchanged pair would only open an unhealthy window (and a Google round-trip)
    for nothing, so it is skipped. An unhealthy client is always reloaded.
    """
    if getattr(client, "is_healthy", None) is not True:
        return False
    current = getattr(client, "current_cookie_pair", None)
    if current is None:
        return False
    try:
        pair = current()
    except Exception:
        return False
    return pair == (_normalize_cookie(req.psid), _normalize_cookie(req.psidts))


@router.put("/accounts/{account_id}/cookies")
async def update_account_cookies(account_id: str, req: UpdateCookiesRequest):
    for account in account_pool.accounts:
        if account.id == account_id:
            if account.client is None:
                account.client = GeminiWebClient(psid=req.psid, psidts=req.psidts)
            client = account.client
            async with _serialized_with_self_heal(client):
                if _push_is_noop(client, req):
                    logger.info("Cookie push for %s skipped: the client already uses this pair", account_id)
                    return {
                        "status": "ok",
                        "message": f"Account {account_id} cookies unchanged (no reload needed)",
                        "changed": False,
                    }
                result = await client.reload_cookies(psid=req.psid, psidts=req.psidts)
            if result.get("success"):
                account.status = AccountStatus.ACTIVE
                account.psid = req.psid
                account.psidts = req.psidts
                account.consecutive_failures = 0
                account.last_error = ""
                account_pool._save_to_file()
                return {"status": "ok", "message": f"Account {account_id} cookies updated"}

            error = result.get("error", "Cookie reload failed")
            if account.client.is_healthy:
                # reload_cookies rolled back to the previous working session: only the *submitted*
                # pair was rejected, the stored one still works. Overwriting the stored cookies or
                # last_error here would break a good account at the next restart and show a dead
                # session on the dashboard.
                error = f"{error} (submitted cookies rejected; the current working session was kept)"
            elif req.persist:
                account.psid = req.psid
                account.psidts = req.psidts
                account.last_error = error
                account_pool._save_to_file()

            return JSONResponse(
                status_code=503,
                content={"error": {"message": error, "type": "reload_error"}},
            )
    return JSONResponse(
        status_code=404,
        content={"error": {"message": f"Account {account_id} not found", "type": "not_found"}},
    )


@router.get("/verify")
async def verify_token():
    return {"status": "ok"}


# Delay between answering the request and sending SIGTERM, so the response gets out first.
_RESTART_DELAY_SECONDS = 0.5


def _restart_supported() -> bool:
    """SIGTERM means "graceful stop" only on POSIX; on Windows os.kill is a hard TerminateProcess."""
    return sys.platform != "win32"


def _terminate_self() -> None:
    import signal

    os.kill(os.getpid(), signal.SIGTERM)


@router.post("/restart")
async def restart_server(request: Request, confirm: bool = False):
    """重启服务（已受 admin 鉴权保护）。需 confirm=true 防误触，
    避免无意/单击直接触发进程级 SIGTERM 造成可用性中断（VULN-009）。"""
    if not confirm:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "Restart requires secondary confirmation, please supply query param ?confirm=true", "type": "confirmation_required"}},
        )
    if not _restart_supported():
        return JSONResponse(
            status_code=501,
            content={"error": {
                "message": (
                    "Restart is not supported on Windows: stopping the process there is a hard kill "
                    "(no graceful shutdown) and nothing starts it again. "
                    "Restart the service manually or through your service manager."
                ),
                "type": "not_implemented",
            }},
        )
    # Persist buffered logs before signalling. Flushing is synchronous file I/O, so run it in a
    # worker thread; a failing flush must not block the restart.
    log_store = getattr(request.app.state, "log_store", None)
    if log_store is not None:
        try:
            await asyncio.to_thread(log_store.flush)
        except Exception as e:
            logger.warning("Log flush before restart failed: %s", type(e).__name__)
    import threading

    def _restart():
        time.sleep(_RESTART_DELAY_SECONDS)
        _terminate_self()

    threading.Thread(target=_restart, daemon=True).start()
    return {"status": "ok", "message": "Server restarting..."}


@router.get("/check-update")
async def check_update():
    """Check if a new version is available via GitHub Releases"""
    import httpx

    current = APP_VERSION
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.github.com/repos/badmarsh/gemini2api/releases/latest",
                headers={"Accept": "application/vnd.github.v3+json"}
            )
            if resp.status_code == 200:
                data = resp.json()
                latest = data.get("tag_name", "").lstrip("v")
                return {
                    "current": current,
                    "latest": latest,
                    "has_update": latest != current and latest != "",
                    "update_url": data.get("html_url", "https://github.com/badmarsh/gemini2api/releases"),
                    "release_notes": data.get("body", "")
                }
    except Exception as e:
        logger.error(f"Failed to check update: {e}")

    return {"current": current, "latest": current, "has_update": False, "update_url": "https://github.com/badmarsh/gemini2api/releases"}


@router.post("/update")
async def perform_update():
    """Return update instructions"""
    return {
        "status": "ok",
        "message": "Please run the following command on your server to update:",
        "command": "cd /home/ubuntu/gemini2api && git pull origin main && docker compose up -d --build"
    }


class CleanupWebChatsRequest(BaseModel):
    keep_hours: float = 24.0
    skip_pinned: bool = True


@router.get("/web-chats")
async def list_web_chats(recent: int = 300):
    """列出账号在 Gemini 网页端的会话（只读，用于排查/确认清理范围）。"""
    try:
        return {"accounts": await account_pool.list_web_chats(recent=recent)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


_cleanup_bg_tasks: set = set()


@router.post("/cleanup-web-chats")
async def cleanup_web_chats(req: CleanupWebChatsRequest = None):
    """清理超过 keep_hours 的网页端会话（置顶可保留）。手动触发。
    后台异步执行立即返回：清理重度堆积账号可能耗时数分钟（每删一个间隔 0.3s），
    同步等待会让 HTTP 请求超时。结果在服务端日志可见。
    """
    req = req or CleanupWebChatsRequest()
    import asyncio

    async def _run():
        try:
            await account_pool.cleanup_web_chats(
                keep_hours=req.keep_hours, skip_pinned=req.skip_pinned
            )
        except Exception as e:
            logger.warning(f"[cleanup-web-chats] Background cleanup error: {e}")

    task = asyncio.create_task(_run())
    _cleanup_bg_tasks.add(task)
    task.add_done_callback(_cleanup_bg_tasks.discard)
    return {"status": "started", "message": "Cleanup started in background; check server logs for results"}


# ---------------------------------------------------------------------------
# Babysit Experiment Monitoring Endpoints
# ---------------------------------------------------------------------------

def _get_runs_root() -> Path:
    candidate = Path("/app/runs")
    if candidate.exists() and candidate.is_dir():
        return candidate
    local_candidate = Path(os.getcwd()) / "runs"
    local_candidate.mkdir(parents=True, exist_ok=True)
    return local_candidate


def _safe_run_dir(run_id: str) -> Optional[Path]:
    import re
    if not re.match(r"^[A-Za-z0-9_.-]+$", run_id):
        return None
    root = _get_runs_root()
    target = (root / run_id).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target


class KillSwitchRequest(BaseModel):
    run_id: Optional[str] = None
    action: str = "stop"


@router.get("/babysit/runs")
async def list_babysit_runs():
    """List all available babysit experiment runs with summary status."""
    import json
    from pathlib import Path
    root = _get_runs_root()
    runs = []
    if not root.exists():
        return {"runs": [], "global_stop": False}

    global_stop = (Path("STOP_BABYSIT").exists() or (root / "STOP_BABYSIT").exists())

    for item in root.iterdir():
        if not item.is_dir() or item.name.startswith("test_") or item.name.startswith("temp_"):
            continue
        run_id = item.name
        tasks_file = item / "tasks.jsonl"
        findings_file = item / "findings.jsonl"
        trace_file = item / "argon_trace.jsonl"
        metrics_file = item / "metrics.jsonl"
        summary_file = item / "summary.json"
        completed_file = item / "completed_tasks.json"
        run_stop = (item / "STOP_BABYSIT").exists() or (item / "STOP").exists()

        completed_ids = set()
        if completed_file.exists():
            try:
                c_data = json.loads(completed_file.read_text(encoding="utf-8", errors="replace"))
                if isinstance(c_data, list):
                    completed_ids = set(c_data)
            except Exception:
                pass

        task_count = 0
        task_completed = 0
        task_failed = 0
        if tasks_file.exists():
            try:
                for line in tasks_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.strip():
                        task_count += 1
                        try:
                            obj = json.loads(line)
                            tid = obj.get("task_id") or obj.get("id") or ""
                            status = obj.get("status", "")
                            if status == "completed" or tid in completed_ids:
                                task_completed += 1
                            elif status == "failed":
                                task_failed += 1
                        except Exception:
                            pass
            except Exception:
                pass
        if completed_ids:
            task_completed = max(task_completed, len(completed_ids))
            task_count = max(task_count, task_completed)

        findings_count = 0
        crit_count = 0
        if findings_file.exists():
            try:
                for line in findings_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.strip():
                        findings_count += 1
                        try:
                            f_obj = json.loads(line)
                            if str(f_obj.get("severity", "")).upper() in ("CRITICAL", "HIGH", "ERROR"):
                                crit_count += 1
                        except Exception:
                            pass
            except Exception:
                pass

        trace_count = 0
        if trace_file.exists():
            try:
                trace_count = sum(1 for line in trace_file.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())
            except Exception:
                pass

        summary_data = None
        if summary_file.exists():
            try:
                summary_data = json.loads(summary_file.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                pass

        is_empty = bool(task_count == 0 and trace_count == 0 and findings_count == 0)
        if is_empty:
            display_label = f"{run_id} • (prázdny test)"
            status_tag = "empty"
        elif run_id == "smoke-run-01":
            display_label = f"{run_id} • (Smoke Test zo 17.10.)"
            status_tag = "smoke_test"
        elif task_completed > 0 and crit_count == 0:
            display_label = f"{run_id} • {task_completed}/{task_count} úloh • 0 chýb (Overený run)"
            status_tag = "verified"
        elif crit_count > 0:
            display_label = f"{run_id} • {task_completed}/{task_count} úloh • {crit_count} findings (Baseline run)"
            status_tag = "baseline"
        else:
            display_label = f"{run_id} • {task_completed}/{task_count} úloh"
            status_tag = "standard"

        mtime = item.stat().st_mtime
        runs.append({
            "run_id": run_id,
            "display_label": display_label,
            "status_tag": status_tag,
            "is_empty": is_empty,
            "mtime": mtime,
            "mtime_iso": datetime.fromtimestamp(mtime).isoformat(),
            "task_count": task_count,
            "task_completed": task_completed,
            "task_failed": task_failed,
            "findings_count": findings_count,
            "critical_findings": crit_count,
            "trace_events": trace_count,
            "stopped": bool(global_stop or run_stop),
            "summary": summary_data,
        })

    runs.sort(key=lambda r: r["mtime"], reverse=True)
    return {"runs": runs, "global_stop": global_stop}


@router.get("/babysit/runs/{run_id}")
async def get_babysit_run(run_id: str):
    """Get full details, tasks, findings, and traces for a specific run."""
    import json
    from pathlib import Path
    target = _safe_run_dir(run_id)
    if not target or not target.exists() or not target.is_dir():
        return JSONResponse(status_code=404, content={"error": f"Run '{run_id}' not found"})

    def read_jsonl(p: Path, max_lines: int = 1000):
        if not p.exists():
            return []
        lines = []
        try:
            for l in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if l.strip():
                    try:
                        lines.append(json.loads(l))
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"Error reading {p}: {e}")
        if len(lines) > max_lines:
            return lines[-max_lines:]
        return lines

    tasks = read_jsonl(target / "tasks.jsonl")
    findings = read_jsonl(target / "findings.jsonl")
    trace = read_jsonl(target / "argon_trace.jsonl", max_lines=200)
    metrics = read_jsonl(target / "metrics.jsonl", max_lines=100)

    completed_file = target / "completed_tasks.json"
    completed_ids = []
    if completed_file.exists():
        try:
            c_data = json.loads(completed_file.read_text(encoding="utf-8", errors="replace"))
            if isinstance(c_data, list):
                completed_ids = [str(x) for x in c_data if isinstance(x, (str, int))]
        except Exception:
            pass

    completed_set = set(completed_ids)
    if not tasks and completed_ids:
        tasks = [{"task_id": cid, "status": "completed"} for cid in completed_ids]
    else:
        for t in tasks:
            tid = t.get("task_id") or t.get("id") or ""
            if tid in completed_set:
                t["status"] = "completed"
            elif not t.get("status"):
                t["status"] = "in_progress"

    summary = None
    summary_file = target / "summary.json"
    if summary_file.exists():
        try:
            summary = json.loads(summary_file.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            pass

    root = _get_runs_root()
    is_stopped = (
        (target / "STOP_BABYSIT").exists()
        or (target / "STOP").exists()
        or (root / "STOP_BABYSIT").exists()
        or Path("STOP_BABYSIT").exists()
    )

    is_empty = bool(len(tasks) == 0 and len(trace) == 0 and len(findings) == 0 and len(completed_ids) == 0)
    if is_empty:
        display_label = f"{run_id} • (prázdny test)"
        status_tag = "empty"
    elif run_id == "smoke-run-01":
        display_label = f"{run_id} • (Smoke Test zo 17.10.)"
        status_tag = "smoke_test"
    elif len(completed_ids) > 0 and not any(str(f.get("severity", "")).upper() in ("CRITICAL", "ERROR", "HIGH") for f in findings):
        display_label = f"{run_id} • {len(completed_ids)}/{len(tasks)} úloh • 0 chýb (Overený run)"
        status_tag = "verified"
    elif any(str(f.get("severity", "")).upper() in ("CRITICAL", "ERROR", "HIGH") for f in findings):
        display_label = f"{run_id} • {len(completed_ids)}/{len(tasks)} úloh • Baseline run"
        status_tag = "baseline"
    else:
        display_label = run_id
        status_tag = "standard"

    return {
        "run_id": run_id,
        "display_label": display_label,
        "status_tag": status_tag,
        "is_empty": is_empty,
        "stopped": is_stopped,
        "tasks": tasks,
        "completed_task_ids": list(completed_ids),
        "findings": findings,
        "trace": trace,
        "metrics": metrics,
        "summary": summary,
    }


@router.post("/babysit/kill-switch")
async def toggle_kill_switch(req: KillSwitchRequest):
    """Trigger Emergency Stop or Resume for babysit execution."""
    from pathlib import Path
    root = _get_runs_root()
    action = req.action.lower()

    if action == "stop":
        try:
            Path("STOP_BABYSIT").write_text("STOPPED_BY_ADMIN_UI\n", encoding="utf-8")
            (root / "STOP_BABYSIT").write_text("STOPPED_BY_ADMIN_UI\n", encoding="utf-8")
            if req.run_id:
                target = _safe_run_dir(req.run_id)
                if target and target.exists():
                    (target / "STOP_BABYSIT").write_text("STOPPED_BY_ADMIN_UI\n", encoding="utf-8")
            return {"status": "ok", "action": "stop", "message": "Kill-switch activated: STOP_BABYSIT created."}
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})

    elif action == "resume":
        removed = []
        for p in [Path("STOP_BABYSIT"), root / "STOP_BABYSIT"]:
            if p.exists():
                try:
                    p.unlink()
                    removed.append(str(p))
                except Exception:
                    pass
        if req.run_id:
            target = _safe_run_dir(req.run_id)
            if target and target.exists():
                for p in [target / "STOP_BABYSIT", target / "STOP"]:
                    if p.exists():
                        try:
                            p.unlink()
                            removed.append(str(p))
                        except Exception:
                            pass
        return {"status": "ok", "action": "resume", "message": "Kill-switch released.", "removed": removed}
    else:
        return JSONResponse(status_code=400, content={"error": "Invalid action. Use 'stop' or 'resume'."})

