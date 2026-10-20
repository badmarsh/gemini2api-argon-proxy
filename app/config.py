import re
import string
import secrets
import logging
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator

from app.core.settings_overrides import apply_overrides
from app.utils import atomic_io

logger = logging.getLogger(__name__)

APP_VERSION = "1.7.2"


def _generate_api_key() -> str:
    chars = string.ascii_letters + string.digits
    suffix = "".join(secrets.choice(chars) for _ in range(32))
    return f"sk-{suffix}"


def mask_secret(value: str) -> str:
    """Cryptographic boundary masking: strictly expose first 6 and last 4 characters.
    Short credentials (<= 8 chars) are fully masked. Returns empty string for empty input.
    """
    if not value:
        return ""
    v = str(value).strip()
    if len(v) <= 8:
        return "******"
    if len(v) <= 10:
        return f"{v[:2]}...{v[-2:]}"
    return f"{v[:6]}...{v[-4:]}"


class Settings(BaseSettings):
    gemini_psid: str = ""
    gemini_psidts: str = ""
    api_key: str = ""
    refresh_interval: int = 5
    max_retries: int = 3
    # 遇到 5xx（尤其 Google 数据中心 IP 的 503 "Sorry" 限流）时：
    # - 同账号只快速重试 same_account_5xx_retries 次（应对瞬时抖动），不长退避空耗
    # - 仍失败则换下一个 active 账号重试（failover），单账号无可换则报错
    # - 被 5xx 限流的账号进入 failover_cooldown 秒冷却，期间不优先选它（不标 expired）
    same_account_5xx_retries: int = 1
    failover_cooldown: float = 30.0
    port: int = 5918
    log_level: str = "info"
    rate_limit_enabled: bool = False
    rate_limit_window: int = 60
    rate_limit_max: int = 10
    health_check_enabled: bool = True
    health_check_interval: int = 5
    accounts_file: str = "accounts.json"
    rotation_strategy: str = "round-robin"
    max_concurrent_per_account: int = 8
    # 并发满载时，acquire 排队等待可用槽位的上限（秒）。等不到才报错，
    # 避免 agent 高并发请求直接撞 "No available accounts" 失败。
    acquire_timeout: float = 60.0
    fingerprint_config_path: str = "data/fingerprint.json"
    version_sync_enabled: bool = True
    version_sync_interval: int = 24
    jitter_enabled: bool = True
    usage_stats_enabled: bool = True
    usage_stats_interval: int = 300
    usage_stats_retention_days: int = 30
    model_whitelist: str = ""
    chat_cleanup_enabled: bool = True
    chat_cleanup_keep_hours: float = 24.0
    chat_cleanup_interval_hours: float = 6.0
    chat_cleanup_skip_pinned: bool = True
    # 原生 Gemini 扩展思考（reasoning_effort 触发）。默认开、可一键关；关闭后 reasoning_effort 对原生 Gemini 无效、走普通格式。
    extended_thinking_enabled: bool = True
    # 管理面板/admin 路由的独立访问密钥。默认空 → 回退到 api_key（保持单 key 全功能的原行为，零回归）。
    # 设置非空后，/admin/* 改用该 key 校验，业务 API 仍用 api_key，实现权限分离。
    admin_api_key: str = ""
    # CORS 可配置（默认保持原行为：允许所有来源 + 允许凭据）。多个来源用英文逗号分隔；"*" 表示全部。
    cors_allow_origins: str = "*"
    cors_allow_credentials: bool = True
    # Serve FastAPI's /docs, /redoc and /openapi.json. These routes are NOT protected by the API key
    # and list every route (admin included), so they are off by default; enable for development only.
    docs_enabled: bool = False
    # 生图代下载尺寸后缀。默认 =s2048（画质足够且显著减小体积/耗时）；设 =s0 为全分辨率原图。
    image_download_size_suffix: str = "=s2048"
    # 单次图片下载 HTTP 超时（秒），独立于主 session 的 60s 默认值。
    image_download_timeout: float = 25.0
    # Gemini→第三方 兜底链（默认关闭，零回归）。开启后：任意 Gemini 模型（flash/pro/thinking）
    # 请求报错或返回空响应时，自动改用 API Key 池中的第三方模型原生重试，客户端无感、仍只用一个模型名。
    # 选择策略：默认自动取池中所有“适合聊天”的第三方（排除 image/video/audio/embedding 等非聊天模型），
    # 随机轮询、一个失败换下一个。FALLBACK_MODELS 为可选的精确指定（逗号分隔、按序尝试）；留空即自动。
    fallback_enabled: bool = False
    fallback_models: str = ""
    # 第三方直连「同名多家」故障切换的坏家冷却时长（秒）。某第三方报错/额度耗尽/返回空后，
    # 该时长内新请求优先跳过它，到期自动恢复参与。设 0 关闭冷却（每次都从第一家重试）。
    # 该特性默认生效、无单独开关：仅当某 model 配置了同名多家时才有可见行为变化，单家零回归。
    thirdparty_failover_cooldown: float = 180.0
    # 完整请求/响应体记录（面板日志详情视图）。**默认关**：请求体含用户完整提示词，
    # 属隐私 + 体积敏感，只有运维明确需要排障时才开。开启后也只记 body、绝不记任何
    # 请求头（Authorization 等凭据不进日志），且流式响应只记一个标记、绝不缓冲整个流。
    log_bodies_enabled: bool = False
    # Argon 兜底至 Gemini Flash 3.8 High（带思考链）。
    # 当 Argon 失败/超时/无可用账号/报错/返回空时，自动无缝降级为 Gemini Flash 3.8 High（默认开）。
    argon_fallback_to_flash: bool = True
    # 强制将所有 Argon 请求直接由 Gemini Flash 3.8 High 提供（跳过 Argon 尝试，默认关）。
    argon_always_fallback: bool = False
    # Argon 兜底目标模型名（默认 gemini-flash-thinking 即 Gemini 3.8 Flash High）。
    argon_fallback_model: str = "gemini-flash-thinking"
    # Argon 请求卡死(超时)后：默认(false)不再换下一个账号重试——每个账号都要再等满 360s 超时，
    # 直接交给路由层降级到 Flash；设为 true 恢复“先换账号重试”的旧行为。仅在 argon_fallback_to_flash=true 时生效。
    argon_failover_on_stall: bool = False
    # After an Argon stall (timeout), serve Argon requests from the Flash fallback for this many seconds instead
    # of letting each one wait out the same timeout. 0 disables. Only active when argon_fallback_to_flash=true.
    argon_stall_breaker_seconds: int = 180
    # Argon request timeout in seconds (0 = keep the built-in default). Argon's test-time compute
    # regularly needs 30-75s before the first token; lower it for snappier failures, raise it for very
    # long agent turns. A stuck request is handled by the stall breaker above, not by a shorter timeout.
    argon_timeout: int = 360
    # Does the StreamGenerate payload continue a conversation the server already holds? It does NOT:
    # the 81-element thinking payload has no slot for it (inner[2] is the metadata block, spec §4.3), so
    # a stored conversation_id is ignored upstream. The routers therefore send the CLIENT's full history
    # instead of just the last user message. Set true only after re-verifying the protocol (an ignored
    # conversation_id plus a stripped prompt means the model answers without any context).
    server_side_continuation: bool = False
    # Optional system prompt prepended to every Argon request (before any user/tool content).
    # Set via ARGON_SYSTEM_PROMPT env var. Empty string = disabled. Supports multiline via \n escapes.
    argon_system_prompt: str = ""
    # How ARGON_SYSTEM_PROMPT combines with a system/developer message the caller already sent:
    #   "merge"          - prepend it in front of the caller's own system message (default);
    #   "only_when_none" - the old behaviour: skip it entirely when the caller sent one. Coding harnesses
    #                      (Codex, Cline, OpenCode) always send their own developer prompt, so this silently
    #                      disabled the Argon rules in exactly the setup they were written for;
    #   "replace"        - drop the caller's system message and use ARGON_SYSTEM_PROMPT instead.
    argon_system_prompt_mode: str = "merge"
    # Agent loop guard (coding harnesses): append a hard "act with a tool now, no promises" reminder to the
    # end of every tool-mode prompt, and detect a conversational stall (a reply that only promises future
    # work) and correct it once. Both are inert when the request declares no tools.
    tool_loop_reminder: bool = True
    tool_loop_stall_guard: bool = True

    @field_validator("gemini_psid")
    @classmethod
    def psid_clean(cls, v: str) -> str:
        return v.strip().strip('"').strip("'").rstrip(";")

    @field_validator("gemini_psidts")
    @classmethod
    def clean_psidts(cls, v: str) -> str:
        return v.strip().strip('"').strip("'").rstrip(";")

    @field_validator("api_key")
    @classmethod
    def ensure_api_key(cls, v: str) -> str:
        v = v.strip()
        if not v:
            return _generate_api_key()
        return v

    @field_validator("port")
    @classmethod
    def port_range(cls, v: int) -> int:
        if v < 1 or v > 65535:
            raise ValueError("PORT must be between 1 and 65535")
        return v

    # utf-8-sig: Windows editors (Notepad, PowerShell 5.1 Out-File) prepend a BOM that would
    # otherwise glue itself onto the first key. extra="ignore": the .env file is shared with
    # other components (e.g. the refresher), so unknown keys must not crash the gateway.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8-sig",
        extra="ignore",
    )


settings = Settings()

# 回放管理面板做过的改动（data/settings-overrides.json），优先级**高于**环境变量。
# 必须紧跟在 Settings() 之后：account_pool 等模块在 import 期就会读 settings 的值，
# 放到 lifespan 里回放就晚了。启动日志由 app/main.py 的 lifespan 打印（那时 logging
# 才配置完），运维排查"改了 .env 为什么不生效"时能一眼看到被覆盖的字段。
APPLIED_OVERRIDES = apply_overrides(settings)


_ENV_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*=")


def warn_unknown_env_keys(env_path=".env") -> list[str]:
    """Log one warning naming the .env keys that match no Settings field; return them sorted.

    Only key NAMES are logged, never values. Unknown keys are harmless (extra="ignore"),
    but a misspelled setting would otherwise be dropped silently. Never raises: a missing
    or undecodable file just yields an empty list.
    """
    try:
        text = Path(env_path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return []
    known = {name.lower() for name in Settings.model_fields}
    unknown = set()
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _ENV_KEY_RE.match(line)
        if m and m.group(1).lower() not in known:
            unknown.add(m.group(1))
    result = sorted(unknown)
    if result:
        logger.warning(
            "Ignoring unknown keys in .env: %s (keys used by other components such as "
            "the refresher are fine; otherwise check for typos)",
            ", ".join(result),
        )
    return result


_API_KEY_LINE_RE = re.compile(r"^\s*(?:export\s+)?API_KEY\s*=(.*)$")


def _api_key_line_value(line: str):
    """Return the value of an exact ``API_KEY=`` line in a .env file, or None for any other line.

    Matches only the exact key name (so ADMIN_API_KEY= is not mistaken for it), allows an
    ``export`` prefix and surrounding whitespace, and treats quoted-empty values as empty.
    """
    m = _API_KEY_LINE_RE.match(line)
    if not m:
        return None
    value = m.group(1).strip()
    if value[:1] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end != -1 else value[1:]
    return value.split(" #", 1)[0].strip() if not value.startswith("#") else ""


def _persist_api_key():
    # 仅当 API_KEY 未由真实环境变量显式提供（即本次为自动生成）时才落盘，
    # 避免覆盖运维通过 env 注入的固定 key。
    import os
    if os.environ.get("API_KEY", "").strip():
        return

    env_path = Path(".env")
    if not env_path.exists():
        # 无 .env 时此前直接 return，导致自动生成的 key 从不持久化，每次重启都换新 key，
        # 静默失效所有客户端凭据（修复 #33）。这里创建 .env 并写入，使其跨重启存活。
        try:
            atomic_io.atomic_rewrite_text(env_path, f"API_KEY={settings.api_key}\n")
            logger.info(
                f"API Key generated and saved to new .env: {mask_secret(settings.api_key)}"
            )
        except Exception as e:
            # 只读文件系统等无法落盘时给出醒目告警，避免运维难以排查的"每次重启换 key"。
            logger.warning(
                f"API Key generated but could NOT be persisted (.env unwritable: {e}); "
                f"它会在重启后重新生成。请显式设置 API_KEY 环境变量。"
            )
        return
    try:
        content = env_path.read_text(encoding="utf-8-sig")
        lines = content.splitlines()
        api_key_lines = [i for i, line in enumerate(lines) if _api_key_line_value(line) is not None]
        if any(_api_key_line_value(lines[i]) for i in api_key_lines):
            return  # a real value is already stored
        if api_key_lines:
            for i in api_key_lines:  # only empty API_KEY= lines: fill them in place
                lines[i] = f"API_KEY={settings.api_key}"
            atomic_io.atomic_rewrite_text(env_path, "\n".join(lines) + "\n")
        else:
            atomic_io.atomic_rewrite_text(env_path, content.rstrip("\n") + f"\nAPI_KEY={settings.api_key}\n")
    except Exception as e:
        logger.warning(
            f"API Key generated but could NOT be persisted (.env unwritable: {e}); "
            "it will be regenerated after restart. Set the API_KEY environment variable explicitly."
        )
        return
    logger.info(f"API Key generated and saved to .env: {mask_secret(settings.api_key)}")


_persist_api_key()
