import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, Depends, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.config import settings, APP_VERSION, mask_secret, APPLIED_OVERRIDES, warn_unknown_env_keys
from app.core.settings_overrides import OVERRIDES_PATH
from app.core.account_pool import account_pool
from app.core.auth import verify_api_key, verify_admin_key
from app.core.limiter import limiter
from app.core.fingerprint.version_sync import version_sync_loop
from app.core.usage_stats import UsageStatsStore
from app.core.usage_timer import snapshot_loop
from app.core.log_store import LogStore, create_log_record
from app.core.debug_metrics import metrics as debug_metrics
from app.core.request_context import bind as bind_request_context, reset as reset_request_context
from app.core.security_headers import apply_security_headers
from app.routers import openai, claude, gemini, research
from app.routers import responses as responses_router
from app.routers import admin
from app.routers import logs as logs_router
from app.routers import usage_stats as usage_stats_router
from app.routers import settings as settings_router
from app.routers import api_keys as api_keys_router
from app.routers import model_mapping as model_mapping_router
from app.routers import gems as gems_router
from app.core.api_key_store import ApiKeyPool
from app.core.model_mapping import ModelMapping
from app.core.gem_mapping import GemMapping

STATIC_DIR = Path(__file__).parent.parent / "static"

log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
)
logging.getLogger("app").setLevel(log_level)
logger = logging.getLogger(__name__)


async def _flush_log_store(log_store) -> None:
    """LogStore.flush() 是同步的全量 json.dumps + write_text，即使 issue #10 followup
    F1a 把 body 从落盘内容里剥掉了，2000 条记录的全量重写仍有实打实的 CPU/IO 开销。
    log_flush_loop 每 10 秒调一次、跑在事件循环上，同步调用会让整个代理卡顿一截——
    丢进线程池，不阻塞事件循环（issue #10 followup F1b）。"""
    await asyncio.to_thread(log_store.flush)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up...")
    logger.info(f"API Key: {mask_secret(settings.api_key)}")
    if APPLIED_OVERRIDES:
        # 面板改过的值优先级高于环境变量，运维改了 .env 却"不生效"时这行是唯一线索。
        logger.info(
            "Applied %d panel setting override(s) from %s: %s",
            len(APPLIED_OVERRIDES),
            OVERRIDES_PATH,
            ", ".join(f"{k}={v}" for k, v in sorted(APPLIED_OVERRIDES.items())),
        )
    # Logging is configured by now; flag misspelled keys that the shared .env would hide.
    warn_unknown_env_keys()
    await account_pool.initialize()

    app.state.log_store = LogStore()
    app.state.api_key_pool = ApiKeyPool()
    app.state.model_mapping = ModelMapping()
    app.state.gem_mapping = GemMapping()

    async def log_flush_loop():
        while True:
            await asyncio.sleep(10)
            await _flush_log_store(app.state.log_store)

    log_flush_task = asyncio.create_task(log_flush_loop())

    async def image_cleanup_loop():
        from app.core import image_store
        while True:
            try:
                # cleanup_old 是同步的 listdir/getmtime/remove 全目录扫描，直接调用会阻塞
                # 事件循环、卡住所有并发请求。改为 to_thread 在线程池里跑（修复 #24）。
                await asyncio.to_thread(image_store.cleanup_old)
            except Exception as e:
                logger.warning(f"[image_store] 清理异常: {e}")
            await asyncio.sleep(6 * 3600)  # 每 6 小时清一次过期生成图片

    image_cleanup_task = asyncio.create_task(image_cleanup_loop())

    async def web_chat_cleanup_loop():
        # 定时清理 Gemini 网页端堆积的会话（API 每次对话都会在网页端留记录）。
        # 保留窗口 >> 反代上下文窗口(6h)，不会误删正在续接的活跃对话；置顶可保留。
        interval = max(1, settings.chat_cleanup_interval_hours) * 3600
        while True:
            await asyncio.sleep(interval)
            try:
                results = await account_pool.cleanup_web_chats(
                    keep_hours=settings.chat_cleanup_keep_hours,
                    skip_pinned=settings.chat_cleanup_skip_pinned,
                )
                total_deleted = sum(r.get("deleted", 0) for r in results if isinstance(r, dict))
                if total_deleted:
                    logger.info(f"[web_chat_cleanup] 清理网页会话 {total_deleted} 个")
            except Exception as e:
                logger.warning(f"[web_chat_cleanup] 清理异常: {e}")

    web_chat_cleanup_task = None
    if settings.chat_cleanup_enabled:
        web_chat_cleanup_task = asyncio.create_task(web_chat_cleanup_loop())
        logger.info(
            f"Web chat cleanup loop started "
            f"(keep {settings.chat_cleanup_keep_hours}h, every {settings.chat_cleanup_interval_hours}h)"
        )

    version_task = None
    if settings.version_sync_enabled:
        version_task = asyncio.create_task(version_sync_loop())
        logger.info("Chrome version sync task started")

    # 用量统计 store 始终实例化（即使 usage_stats_enabled=False 或运行时再开启），
    # 这样 /admin/usage-stats/* 端点恒有可读对象，不会因 app.state 缺失而 500（修复 #30/#14）。
    # 仅快照后台循环受开关控制：关闭时不落新快照，端点返回已加载/空数据。
    store = UsageStatsStore(retention_days=settings.usage_stats_retention_days)
    app.state.usage_stats_store = store
    snapshot_task = None
    if settings.usage_stats_enabled:
        snapshot_task = asyncio.create_task(
            snapshot_loop(store, account_pool, interval=settings.usage_stats_interval)
        )
        logger.info("Usage stats snapshot loop started")

    yield

    logger.info("Shutting down...")
    log_flush_task.cancel()
    try:
        await log_flush_task
    except asyncio.CancelledError:
        pass
    image_cleanup_task.cancel()
    try:
        await image_cleanup_task
    except asyncio.CancelledError:
        pass
    if web_chat_cleanup_task:
        web_chat_cleanup_task.cancel()
        try:
            await web_chat_cleanup_task
        except asyncio.CancelledError:
            pass
    app.state.log_store.flush()
    if snapshot_task:
        snapshot_task.cancel()
        try:
            await snapshot_task
        except asyncio.CancelledError:
            pass
    if version_task:
        version_task.cancel()
        try:
            await version_task
        except asyncio.CancelledError:
            pass
    await account_pool.shutdown()


def _fastapi_kwargs(docs_enabled: bool) -> dict:
    """Keyword arguments for FastAPI(...).

    /docs, /redoc and /openapi.json bypass the app-level auth dependency, so unless DOCS_ENABLED is
    set they are removed entirely (404) instead of leaking the full route list to anonymous callers.
    """
    kwargs = dict(
        title="Gemini2API",
        description="Gemini Web to API proxy",
        version=APP_VERSION,
        lifespan=lifespan,
        dependencies=[Depends(verify_api_key)],
    )
    if not docs_enabled:
        kwargs.update(docs_url=None, redoc_url=None, openapi_url=None)
    return kwargs


app = FastAPI(**_fastapi_kwargs(settings.docs_enabled))

# CORS 可配置（VULN-007）。默认 cors_allow_origins="*" + cors_allow_credentials=True → 与原行为完全一致；
# 运维可经环境变量 CORS_ALLOW_ORIGINS（逗号分隔白名单）+ CORS_ALLOW_CREDENTIALS=false 收紧。
_cors_origins = (
    ["*"]
    if settings.cors_allow_origins.strip() == "*"
    else [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
)
# 不改变默认行为（保持零回归），但通配来源 + 允许凭据是不安全组合，启动时提醒运维收紧（#32）。
if _cors_origins == ["*"] and settings.cors_allow_credentials:
    logger.warning(
        "CORS: cors_allow_origins='*' 同时 cors_allow_credentials=True 不安全；"
        "建议设置 CORS_ALLOW_ORIGINS 白名单或 CORS_ALLOW_CREDENTIALS=false 收紧。"
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=settings.cors_allow_credentials,
)

# 限流（P0-2）：始终挂载基础设施；是否真正生效由对话端点装饰器的 exempt_when 按
# settings.rate_limit_enabled 动态决定。默认 rate_limit_enabled=False → 全部旁路，零回归。
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"error": {"message": "Rate limit exceeded", "type": "rate_limit"}},
    )


SKIP_LOG_PREFIXES = ("/static/", "/favicon.ico", "/admin/logs")

import re as _re

# VULN-008：日志脱敏。避免 ?token=sk-xxx / 路径中出现的 key 进入结构化日志。
_TOKEN_SCRUB_RE = _re.compile(r"(token=)[^&\s]+", _re.IGNORECASE)
# The whole key token is masked: letters, digits, "-" and "_" (sk-ant-api03-..., sk-proj-...),
# and the URL-encoded dash (sk%2D...) that shows up in query strings.
_SK_SCRUB_RE = _re.compile(r"sk(?:-|%2[dD])(?:[A-Za-z0-9_-]|%2[dD]){4,}")
# ?key=... / ?api_key=... / ?apikey=... / ?api-key=... values (Gemini-style key query parameter).
_KEY_PARAM_SCRUB_RE = _re.compile(r"(\b(?:api[_-]?)?key=)[^&\s]+", _re.IGNORECASE)


def _scrub_sensitive(text: str) -> str:
    if not text:
        return text
    scrubbed = _TOKEN_SCRUB_RE.sub(r"\1****", text)
    scrubbed = _KEY_PARAM_SCRUB_RE.sub(r"\1****", scrubbed)
    return _SK_SCRUB_RE.sub("sk-****", scrubbed)


class AccessLogScrubFilter(logging.Filter):
    """Scrub secrets (?token=..., ?key=..., sk-... keys) from uvicorn access-log records.

    uvicorn builds the access line from ``record.args`` =
    (client_addr, method, full_path, http_version, status_code); ``full_path`` includes the
    query string, so it must be scrubbed before any handler formats the record. Only str items
    are touched: the int status code has to stay an int because the format string uses ``%d``.
    """

    _g2a_access_scrub = True

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(
                _scrub_sensitive(a) if isinstance(a, str) else a for a in args
            )
        return True


def _install_access_log_scrub() -> None:
    """Attach the scrub filter to ``uvicorn.access`` exactly once.

    Called at import time: uvicorn configures logging (dictConfig) before it imports the app,
    and dictConfig replaces handlers but leaves logger filters alone, so the filter survives.
    Re-running (or reloading this module) replaces the previous instance instead of stacking.
    """
    access_logger = logging.getLogger("uvicorn.access")
    for existing in list(access_logger.filters):
        if getattr(existing, "_g2a_access_scrub", False):
            access_logger.removeFilter(existing)
    access_logger.addFilter(AccessLogScrubFilter())


_install_access_log_scrub()


# ---------------------------------------------------------------------------
# 完整请求/响应体记录（settings.log_bodies_enabled，默认关）
# ---------------------------------------------------------------------------
# 硬约束（勿改）：
#   1. 开关关闭时这段代码一行都不执行，中间件行为与开关引入前逐字节一致。
#   2. **绝不缓冲流式响应体** —— 只记一个短标记。缓冲 SSE/NDJSON 等于把流式变非流式，
#      客户端会一直等到上游结束才收到第一个字节，是比"日志不全"严重得多的事故。
#   3. 只记 body，**不记任何请求头** —— Authorization / Cookie 等凭据永不进日志。
LOG_BODY_MAX_BYTES = 4 * 1024           # 入库上限（原 32KB，issue #10 followup F1c 收窄），超出截断并标注
LOG_BODY_CAPTURE_LIMIT = 1024 * 1024    # 超过这个大小干脆不读（生图 base64 等）
_STREAMING_NOT_CAPTURED = {"_note": "streaming response not captured"}
# gemini 的流式端点用 media_type="application/json"（NDJSON 流），content-type 认不出来，
# 只能靠路径识别；其余流式端点都是 text/event-stream 或带 X-Accel-Buffering: no。
_STREAMING_PATH_HINTS = (":streamgeneratecontent",)


def _body_for_log(raw) -> dict | None:
    """把一段响应/请求字节体转成可入日志的 dict；超限截断并标注。"""
    if not raw:
        return None
    raw = bytes(raw)
    if len(raw) > LOG_BODY_MAX_BYTES:
        return {
            "_truncated": True,
            "_size": len(raw),
            "_preview": raw[:LOG_BODY_MAX_BYTES].decode("utf-8", "replace"),
        }
    import json as _json
    try:
        parsed = _json.loads(raw)
    except Exception:
        return {"_raw": raw.decode("utf-8", "replace")}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


def _looks_like_streaming(response, path: str, stream_flag) -> bool:
    ctype = (response.headers.get("content-type") or "").lower()
    if "text/event-stream" in ctype or "ndjson" in ctype:
        return True
    if (response.headers.get("x-accel-buffering") or "").strip().lower() == "no":
        return True
    if stream_flag is True:
        return True
    low = path.lower()
    return any(hint in low for hint in _STREAMING_PATH_HINTS)


async def _capture_response_body(response):
    """读出**非流式**响应体并重建响应，返回 (response, body_dict)。

    只在已判定为非流式时调用。BaseHTTPMiddleware 的 call_next 一律返回带
    body_iterator 的包装响应（连 JSONResponse 也是），所以必须读迭代器再重建。
    重建时直接搬 raw_headers，保留重复头（CORS 的 vary 等）与 content-length 原样。

    异常安全（issue #10 followup F3）：body_iterator 一旦被排空就回不去了——排空之后
    重建响应必须**无条件**发生，绝不能让"把 body 序列化成日志用 dict"这一步的任何异常
    捎带把重建也跳过，否则客户端会拿到一个 Content-Length 仍在、却零字节的 200。
    所以 `_body_for_log` 单独包一层 try/except，失败只丢一条 log 字段，绝不影响 response。
    """
    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is None:
        try:
            body_dict = _body_for_log(getattr(response, "body", None))
        except Exception as e:
            logger.warning(f"[log_bodies] 记录响应体失败，已跳过（响应不受影响）: {e}")
            body_dict = None
        return response, body_dict
    try:
        content_length = int(response.headers.get("content-length") or 0)
    except (TypeError, ValueError):
        content_length = 0
    if content_length > LOG_BODY_CAPTURE_LIMIT:
        return response, {"_note": "response too large to capture", "_size": content_length}

    chunks = [chunk async for chunk in body_iterator]
    raw = b"".join(bytes(c) for c in chunks)
    # 重建必须先于任何可能失败的步骤完成——见上面 docstring。
    rebuilt = Response(content=raw, status_code=response.status_code)
    rebuilt.raw_headers = list(response.raw_headers)
    rebuilt.background = response.background
    try:
        body_dict = _body_for_log(raw)
    except Exception as e:
        logger.warning(f"[log_bodies] 记录响应体失败，已跳过（响应不受影响）: {e}")
        body_dict = None
    return rebuilt, body_dict


def _debug_error_class(request: Request, status: int, raw: bytes = b"") -> str:
    explicit = getattr(request.state, "debug_error_class", "")
    if explicit:
        return explicit
    diagnostic = getattr(request.state, "parse_diagnostic", {}) or {}
    if diagnostic.get("repaired"):
        return "tool_json_repaired"
    if diagnostic.get("failure_class"):
        return "tool_json_invalid"
    if status == 504:
        return "upstream_timeout"
    if status in (401, 403):
        return "cookie_invalid" if status == 403 else "auth_error"
    if status == 429:
        return "rate_limit"
    if raw and b"protocol drift" in raw.lower():
        return "protocol_drift"
    if status >= 500:
        return "upstream_error"
    if raw and (b'"error": {' in raw or b'"error":{' in raw or b'"type": "response.failed"' in raw or b'"type":"response.failed"' in raw):
        return "upstream_error"
    return ""


def _debug_fallback(response, raw: bytes = b"", requested_model: str | None = None) -> tuple[bool, str, str]:
    headers = getattr(response, "headers", {})
    served_by = (headers.get("X-Gemini2API-Argon-Served-By") or "").strip()
    reason = (headers.get("X-Gemini2API-Argon-Fallback-Reason") or "").strip()
    marker = b"gemini2api argon_fallback served_by="
    if marker in raw:
        fallback_used = True
        try:
            tail = raw.split(marker, 1)[1].splitlines()[0].decode("utf-8", "replace")
            served_by = tail.split("served_by=", 1)[-1].split(" ", 1)[0]
            reason = tail.split("reason=", 1)[-1].split()[0] if "reason=" in tail else reason
        except Exception:
            pass
    else:
        if served_by in {"stall_breaker", "argon_always_fallback"}:
            fallback_used = True
            reason = reason or served_by
            served_by = "gemini-flash-thinking"
        else:
            fallback_used = bool(served_by and served_by != "argon" and "or-" not in served_by)
            if served_by == "argon-or-gemini-flash-thinking":
                served_by = "argon"
    if not served_by and requested_model:
        # Only Argon requests carry a served-by marker. Without one, a non-Argon request was answered
        # by the model it asked for; defaulting to "argon" mislabelled all Flash/Pro traffic and made
        # the requested-vs-served comparison useless.
        from app.core.argon_fallback import is_argon_model

        if not is_argon_model(requested_model):
            served_by = str(requested_model)[:64]
    return fallback_used, reason, served_by or "argon"


def _add_debug_log(request: Request, response, *, path: str, method: str, model: str | None,
                   stream: bool | None, status: int, started: float, ttft_ms: float,
                   max_gap_ms: float, raw: bytes = b"", request_body=None, response_body=None) -> None:
    import time as _time
    total_ms = max(0.0, (_time.perf_counter() - started) * 1000)
    # Wall-clock time the request entered the proxy. The record's own ``ts`` is the
    # time the record was written (after the response), so both together describe
    # the request window for the dashboard's request explorer.
    ingress_ts = datetime.fromtimestamp(
        _time.time() - total_ms / 1000.0, tz=timezone.utc
    ).isoformat()
    fallback_used, fallback_reason, served_by = _debug_fallback(response, raw, model)
    error_class = _debug_error_class(request, status, raw)
    account_hash = getattr(request.state, "account_hash", "") or ""
    regenerations = int(getattr(request.state, "regenerations", 0) or 0)
    finish_reason = getattr(request.state, "finish_reason", "") or ""
    if not finish_reason and raw:
        import re as _re
        match = _re.search(rb'"(?:finish_reason|finishReason|stop_reason|stopReason)"\s*:\s*"([^"\\]+)"', raw)
        if match:
            finish_reason = match.group(1).decode("utf-8", "replace")[:64]
    parse_diagnostic = getattr(request.state, "parse_diagnostic", {}) or {}
    parser_failure = str(parse_diagnostic.get("failure_class") or "")
    parser_response_length = parse_diagnostic.get("response_length")
    parser_repaired = bool(parse_diagnostic.get("repaired"))
    truncated_suspected = bool(parse_diagnostic.get("truncated_suspected"))

    tags = []
    import re as _re_tag
    _tag_pattern = _re_tag.compile(r"^[A-Za-z0-9_.:-]+$")
    for _hdr in ("X-G2A-Run", "X-G2A-Role"):
        _val = request.headers.get(_hdr)
        if _val:
            _val = _val.strip()
            if 0 < len(_val) <= 64 and _tag_pattern.fullmatch(_val):
                tags.append(_val)
                if len(tags) == 2:
                    break

    record = create_log_record(
        method=method,
        path=_scrub_sensitive(path),
        direction="egress" if path.startswith(("/v1beta/", "/gemini/")) else "ingress",
        model=model,
        status=status,
        latency_ms=total_ms,
        stream=stream,
        error=(f"HTTP {status}" if status >= 400 else None),
        request_body=request_body,
        response_body=response_body,
        tags=tags,
        ttft_ms=ttft_ms,
        total_latency_ms=total_ms,
        max_inter_chunk_gap_ms=max_gap_ms,
        error_class=error_class or None,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason or None,
        regenerations=regenerations,
        account_hash=account_hash or None,
        served_by=served_by,
        finish_reason=finish_reason or None,
        parser_failure=parser_failure or None,
        parser_response_length=parser_response_length,
        parser_repaired=parser_repaired,
        truncated_suspected=truncated_suspected,
        ingress_ts=ingress_ts,
    )
    debug = {
        "request_id": record.request_id,
        "ts": record.ts,
        "ingress_ts": ingress_ts,
        "model": model or "",
        "served_by": served_by,
        "account_hash": account_hash,
        "status": status,
        "stream": bool(stream),
        "path": _scrub_sensitive(path),
        "tags": tags,
        "ttft_ms": ttft_ms,
        "total_latency_ms": total_ms,
        "max_inter_chunk_gap_ms": max_gap_ms,
        "error_class": error_class,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "regenerations": regenerations,
        "finish_reason": finish_reason,
        "parser_failure": parser_failure,
        "parser_response_length": parser_response_length,
        "parser_repaired": parser_repaired,
        "truncated_suspected": truncated_suspected,
    }
    debug_metrics.record(debug)
    request.app.state.log_store.add(record)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Attach the response hardening headers (CSP, nosniff, frame options, ...).

    Applied to every response -- the console pages and the API alike. The headers are
    additive: no client contract changes and no status code changes.
    """
    response = await call_next(request)
    apply_security_headers(response)
    return response


@app.middleware("http")
async def log_capture_middleware(request: Request, call_next):
    path = request.url.path
    if any(path.startswith(p) for p in SKIP_LOG_PREFIXES):
        return await call_next(request)

    context_token = bind_request_context(request.state)
    context_reset = False

    def reset_context() -> None:
        nonlocal context_reset
        if not context_reset:
            reset_request_context(context_token)
            context_reset = True

    is_api = path.startswith(("/openai/", "/claude/", "/gemini/", "/v1/", "/v1beta/"))
    if (
        is_api
        and request.method == "POST"
        and request.headers.get("content-type", "").startswith("application/json")
    ):
        try:
            request.state._body_cache = await request.body()
        except Exception:
            pass

    import time
    start = time.perf_counter()
    try:
        response = await call_next(request)
        latency_ms = (time.perf_counter() - start) * 1000
        method = request.method
        status = response.status_code

        if not is_api and not path.startswith("/admin/"):
            reset_context()
            return response

        model = None
        stream = None
        if hasattr(request.state, "_body_cache"):
            import json
            try:
                body = json.loads(request.state._body_cache)
                if isinstance(body, dict):
                    model = body.get("model")
                    stream = body.get("stream")
            except Exception:
                pass
        if not model and "/models/" in path:
            try:
                model = path.split("/models/", 1)[-1].split(":")[0] or None
            except Exception:
                pass

        is_streaming = _looks_like_streaming(response, path, stream)
        request_body = None
        response_body = None
        if settings.log_bodies_enabled and is_api:
            try:
                request_body = _body_for_log(getattr(request.state, "_body_cache", None))
                if is_streaming:
                    response_body = dict(_STREAMING_NOT_CAPTURED)
                else:
                    response, response_body = await _capture_response_body(response)
            except Exception as e:
                logger.warning(f"[log_bodies] 记录请求/响应体失败，已跳过: {e}")

        if is_streaming and getattr(response, "body_iterator", None) is not None:
            original = response.body_iterator

            async def observed_body():
                first_at = None
                previous_at = None
                max_gap = 0.0
                sample = bytearray()
                try:
                    async for chunk in original:
                        now = time.perf_counter()
                        if first_at is None:
                            first_at = now
                        if previous_at is not None:
                            max_gap = max(max_gap, (now - previous_at) * 1000)
                        previous_at = now
                        if len(sample) < 65536:
                            sample.extend(bytes(chunk)[:65536 - len(sample)])
                        yield chunk
                finally:
                    try:
                        _add_debug_log(
                            request, response, path=path, method=method, model=model, stream=True,
                            status=status, started=start,
                            ttft_ms=((first_at - start) * 1000 if first_at is not None else latency_ms),
                            max_gap_ms=max_gap, raw=bytes(sample), request_body=request_body,
                            response_body=response_body,
                        )
                    except Exception as exc:
                        logger.warning("[debug_metrics] stream observation failed: %s", type(exc).__name__)
                    finally:
                        reset_context()

            response.body_iterator = observed_body()
            return response

        raw = b""
        if getattr(response, "body", None):
            try:
                raw = bytes(response.body)
            except Exception:
                raw = b""
        try:
            _add_debug_log(
                request, response, path=path, method=method, model=model, stream=stream,
                status=status, started=start, ttft_ms=latency_ms, max_gap_ms=0.0, raw=raw,
                request_body=request_body, response_body=response_body,
            )
        except Exception as exc:
            logger.warning("[debug_metrics] request observation failed: %s", type(exc).__name__)
        finally:
            reset_context()
        return response
    except BaseException:
        reset_context()
        raise


app.include_router(openai.router, prefix="/openai/v1")
app.include_router(openai.router, prefix="/v1")  # 标准 OpenAI 路径，兼容 OpenClaw 等客户端

app.include_router(responses_router.router, prefix="/openai/v1")
app.include_router(responses_router.router, prefix="/v1")

# Claude：完整端点挂 /claude/v1；裸 /v1 仅暴露对话入口（messages），
# 模型列表 models_router 不挂裸 /v1，避免 /v1/models 与 OpenAI 撞车
app.include_router(claude.models_router, prefix="/claude/v1")
app.include_router(claude.router, prefix="/claude/v1")
app.include_router(claude.router, prefix="/v1")  # 标准 Claude 路径（/v1/messages），兼容 Claude 官方 SDK

# Gemini：/v1beta 与 /v1 不同段，完整端点可同时挂 /gemini/v1beta 和裸 /v1beta
app.include_router(gemini.router, prefix="/gemini/v1beta")
app.include_router(gemini.router, prefix="/v1beta")  # 标准 Gemini 路径，兼容 Gemini 官方 SDK
app.include_router(research.router)
# 管理类路由（/admin/*）改由 verify_admin_key 鉴权（VULN-001 权限分离）。
# 默认 admin_api_key 为空 → 回退 api_key，单 key 仍可用全部管理功能，零回归。
_admin_deps = [Depends(verify_admin_key)]
app.include_router(admin.router, dependencies=_admin_deps)
app.include_router(logs_router.router, dependencies=_admin_deps)
app.include_router(usage_stats_router.router, dependencies=_admin_deps)
app.include_router(settings_router.router, dependencies=_admin_deps)
app.include_router(api_keys_router.router, dependencies=_admin_deps)
app.include_router(model_mapping_router.router, dependencies=_admin_deps)
app.include_router(gems_router.router, dependencies=_admin_deps)


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "gemini2api"}


@app.get("/images/{image_id}")
async def serve_generated_image(image_id: str):
    """提供 AI 生成图片的访问（供对话接口返回可渲染 URL）。"""
    from app.core import image_store
    path = image_store.get_image_path(image_id)
    if not path:
        return JSONResponse(status_code=404, content={"error": "image not found"})
    return FileResponse(path, media_type=image_store.content_type_for(image_id))


# 这两个入口 HTML 必须每次回源校验：里面写着 base.css?v= / app.js?v= 这些版本串，
# 缓存住旧 HTML 就等于把整套前端资源都钉在旧版本上（no-cache 仍走 ETag，命中即 304）。
_HTML_NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/login.html")
async def login_page():
    login_file = STATIC_DIR / "login.html"
    if login_file.exists():
        return FileResponse(login_file, media_type="text/html", headers=_HTML_NO_CACHE)
    return HTMLResponse("<h1>Login page not found</h1>", status_code=404)


@app.get("/index.html")
async def index_page():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file, media_type="text/html", headers=_HTML_NO_CACHE)
    return HTMLResponse("<h1>Panel not found</h1>", status_code=404)


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def root_page():
    """裸地址 ``/`` 是管理员实际打开/收藏的入口，必须和 /index.html 同样每次回源校验。

    没有这条路由时 ``/`` 会落到末尾的 StaticFiles 挂载（html=True 会替我们返回
    index.html），但 starlette 传给 ``get_response`` 的 path 是 ``"."`` —— 不以
    .js/.css/.html/.json 结尾，``RevalidatingStaticFiles`` 因此不会加 Cache-Control，
    浏览器就按启发式新鲜度缓存住这份写着 ``app.js?v=`` 版本串的 HTML，升级后整套前端
    被钉在旧版本上。这正是加 no-cache 想根治的问题，却在最常用的那个 URL 上漏掉了。

    必须显式声明 HEAD：FastAPI 的 APIRoute 只注册声明过的方法（不像 starlette 的
    Route 会自动补 HEAD），只写 ``@app.get("/")`` 的话 ``curl -I http://host/``
    仍会落回 StaticFiles 挂载、拿不到 no-cache —— 而缓存判定用的正是这类条件/探测请求。
    ``/index.html`` 与 ``/login.html`` 没有这个问题：它们的 HEAD 虽然也落回挂载，但
    路径以 .html 结尾，``RevalidatingStaticFiles`` 会照常补上 Cache-Control。
    """
    return await index_page()


API_DIR = Path(__file__).parent.parent / "api"


class RevalidatingStaticFiles(StaticFiles):
    """给面板的 JS/CSS/HTML 加 ``Cache-Control: no-cache``。

    StaticFiles 默认不发 Cache-Control，浏览器就按启发式新鲜度自己缓存。index.html 只给
    base.css/mobile.css/app.js 挂了 ?v= 版本串，而 app.js 里 `import './settings.js'`
    这类裸模块说明符带不上查询串 —— 升级后管理员可能拿到旧的 settings.js/i18n.js，
    甚至出现"只更新了一半"（新 settings.js + 旧 i18n.js → 界面直接显示
    settings.field.logBodiesEnabled 这种原始 i18n 键）。

    no-cache 不是不缓存，是"每次必须回源校验"；StaticFiles 本来就发 ETag/Last-Modified，
    命中即 304 空响应，代价极小，却根治了升级后取到陈旧模块的问题。
    """

    _REVALIDATE_SUFFIXES = (".js", ".css", ".html", ".json")

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if path.endswith(self._REVALIDATE_SUFFIXES):
            response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/api-assets", StaticFiles(directory=str(API_DIR)), name="api-assets")
app.mount("/", RevalidatingStaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, reload=False)
