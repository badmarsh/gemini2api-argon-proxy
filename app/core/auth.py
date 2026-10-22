import collections
import logging
import secrets
import time
from pathlib import PurePosixPath
from threading import Lock

from fastapi import Request, HTTPException

from app.config import settings
from app.core.limiter import _client_ip_key

logger = logging.getLogger(__name__)


STATIC_EXTENSIONS = {".html", ".css", ".js", ".ico", ".png", ".jpg", ".svg", ".woff", ".woff2", ".ttf"}

# 所有管理类路由统一挂在 /admin/* 前缀下；这些路径由 verify_admin_key 负责校验，
# 全局 verify_api_key 对其放行以避免双重校验冲突（设置独立 admin_api_key 后两把 key 不同会互相误拒）。
ADMIN_PATH_PREFIX = "/admin"

# Static-asset bypass is only for read-only methods. A mutating request such as
# DELETE /admin/accounts/test.png must never be treated as a static file.
_STATIC_SAFE_METHODS = frozenset({"GET", "HEAD"})

# API / admin namespaces. Paths under these prefixes are never eligible for the
# static-extension bypass, whatever their suffix looks like.
_PROTECTED_PREFIXES = ("/admin", "/v1", "/v1beta", "/openai", "/claude", "/gemini")


class AdminAuthRateLimiter:
    """Tracks failed admin authentication attempts per client IP to prevent brute-forcing.

    Defaults to a 60-second window, max 5 failed attempts, with a 60-second lockout period.
    """

    def __init__(self, max_attempts: int = 5, window_seconds: float = 60.0, lockout_seconds: float = 60.0):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._lock = Lock()
        self._failures: dict[str, list[float]] = collections.defaultdict(list)
        self._lockouts: dict[str, float] = {}

    def is_locked(self, ip: str, now: float | None = None) -> tuple[bool, int]:
        """Returns (is_locked, retry_after_seconds)."""
        now = time.time() if now is None else now
        with self._lock:
            locked_until = self._lockouts.get(ip, 0.0)
            if now < locked_until:
                return True, max(1, int(locked_until - now))
            elif locked_until > 0.0:
                del self._lockouts[ip]

            cutoff = now - self.window_seconds
            timestamps = [t for t in self._failures.get(ip, []) if t > cutoff]
            if timestamps:
                self._failures[ip] = timestamps
            elif ip in self._failures:
                del self._failures[ip]

            if len(timestamps) >= self.max_attempts:
                self._lockouts[ip] = now + self.lockout_seconds
                self._failures.pop(ip, None)
                return True, int(self.lockout_seconds)

            return False, 0

    def record_failure(self, ip: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            cutoff = now - self.window_seconds
            timestamps = [t for t in self._failures.get(ip, []) if t > cutoff]
            timestamps.append(now)
            if len(timestamps) >= self.max_attempts:
                self._lockouts[ip] = now + self.lockout_seconds
                self._failures.pop(ip, None)
            else:
                self._failures[ip] = timestamps

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)
            self._lockouts.pop(ip, None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()
            self._lockouts.clear()


_admin_auth_limiter = AdminAuthRateLimiter()


def reset_admin_rate_limiter() -> None:
    _admin_auth_limiter.reset()


def _has_protected_prefix(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _PROTECTED_PREFIXES)


def _is_public_path(path: str, method: str = "GET") -> bool:
    """无需鉴权即可访问的路径。

    Exact public pages (/health, /, /login.html, /index.html) keep their original
    behaviour. The static-file bypass (extension match, /app/, /components/) applies
    only to GET/HEAD and never to the API/admin prefixes.
    """
    if path == "/health":
        return True
    if path in ("/", "/login.html", "/index.html"):
        return True
    if method.upper() not in _STATIC_SAFE_METHODS:
        return False
    if _has_protected_prefix(path):
        return False
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in STATIC_EXTENSIONS:
        return True
    if path.startswith("/app/") or path.startswith("/components/"):
        return True
    return False


def _extract_request_key(request: Request) -> str:
    """按既有优先级提取调用方提供的 key：Bearer → x-api-key → ?token=。"""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        key = auth_header[7:].strip()
        if key:
            return key

    key = request.headers.get("x-api-key", "").strip()
    if key:
        return key

    return request.query_params.get("token", "").strip()


def _key_matches(provided: str, expected: str) -> bool:
    """恒定时间比较，避免时序侧信道（VULN-011）。非 ASCII 也安全（按 utf-8 字节比较）。"""
    if not provided or not expected:
        return False
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _missing_key_error() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"error": {"message": "Missing API key. Use Authorization: Bearer sk-xxx or x-api-key header.", "type": "auth_error"}},
    )


def _invalid_key_error() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"error": {"message": "Invalid API key.", "type": "auth_error"}},
    )


def _rate_limited_error(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={"error": {"message": f"Too many failed login attempts. Please try again after {retry_after}s.", "type": "rate_limit"}},
        headers={"Retry-After": str(retry_after)},
    )


async def verify_api_key(request: Request):
    """业务 API 全局鉴权。管理类路径(/admin/*)交由 verify_admin_key 校验，此处放行避免双重校验。"""
    path = request.url.path

    if _is_public_path(path, request.method):
        return

    # 管理类路由由 verify_admin_key（在 include_router 时挂载）负责，避免与独立 admin_api_key 冲突
    if path == ADMIN_PATH_PREFIX or path.startswith(ADMIN_PATH_PREFIX + "/"):
        return

    key = _extract_request_key(request)
    if not key:
        raise _missing_key_error()

    if not _key_matches(key, settings.api_key):
        raise _invalid_key_error()


async def verify_admin_key(request: Request):
    """管理面板/admin 路由鉴权。配置了 admin_api_key 则校验它，否则回退 api_key（默认单 key 全功能，零回归）。

    Admin routes are never public: no static-extension or other path-based exemption applies here.
    """
    ip = _client_ip_key(request)
    is_locked, retry_after = _admin_auth_limiter.is_locked(ip)
    if is_locked:
        raise _rate_limited_error(retry_after)

    admin_key = (settings.admin_api_key or "").strip()
    expected = admin_key if admin_key else settings.api_key

    key = _extract_request_key(request)
    if not key:
        raise _missing_key_error()

    if not _key_matches(key, expected):
        _admin_auth_limiter.record_failure(ip)
        raise _invalid_key_error()

    _admin_auth_limiter.record_success(ip)
