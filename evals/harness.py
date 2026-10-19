"""In-process system under test for the offline (T0) tier.

Boots the real FastAPI app on an ephemeral loopback port (real HTTP, real streaming, real middleware) with
the singleton ``account_pool`` populated by ``GeminiWebClient`` instances whose HTTP layer is a
``FakeSession``. Nothing touches Google, the developer's ``data/`` directory or ``.env``.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from pathlib import Path

EVAL_API_KEY = "sk-eval-key"


def ensure_env() -> Path:
    """Deterministic, side-effect-free settings. Must run before the first ``import app...``."""
    os.environ.setdefault("API_KEY", EVAL_API_KEY)
    os.environ.setdefault("LOG_LEVEL", "warning")
    os.environ.setdefault("VERSION_SYNC_ENABLED", "false")
    os.environ.setdefault("USAGE_STATS_ENABLED", "false")
    os.environ.setdefault("CHAT_CLEANUP_ENABLED", "false")
    os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
    os.environ["ARGON_FALLBACK_TO_FLASH"] = "true"
    os.environ["ARGON_STALL_BREAKER_SECONDS"] = "180"
    data = Path(tempfile.mkdtemp(prefix="g2a-evals-data-"))
    os.environ["DATA_DIR"] = str(data)
    import app.core.settings_overrides as so  # noqa: PLC0415

    so.OVERRIDES_PATH = data / "settings-overrides.json"
    return data


class _ScaledAsyncio:
    """Stand-in for the ``asyncio`` module inside gemini_client: timeouts and sleeps run at ``scale``."""

    def __init__(self, real, scale: float):
        self._real = real
        self._scale = scale

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def wait_for(self, aw, timeout=None):
        return await self._real.wait_for(aw, None if timeout is None else timeout * self._scale)

    async def sleep(self, delay, *a, **k):
        return await self._real.sleep(delay * self._scale, *a, **k)


class InProcessSUT:
    def __init__(self, *, accounts: int = 2, time_scale: float = 0.005, max_concurrent: int = 2,
                 keepalive_virtual: float = 10.0, acquire_timeout_virtual: float = 60.0):
        self.n_accounts = accounts
        self.scale = time_scale
        self.max_concurrent = max_concurrent
        self.keepalive_virtual = keepalive_virtual
        self.acquire_timeout_virtual = acquire_timeout_virtual
        self.data_dir = ensure_env()
        from evals.fake_upstream.session import FakeUpstream  # noqa: PLC0415

        self.upstream = FakeUpstream(time_scale=time_scale)
        self.base_url = ""
        self.api_key = os.environ["API_KEY"]
        self._undo: list = []
        self._server = None
        self._task = None

    # ---- lifecycle -------------------------------------------------------------------------------
    async def start(self) -> "InProcessSUT":
        import uvicorn  # noqa: PLC0415

        import app.core.argon_breaker as argon_breaker  # noqa: PLC0415
        import app.core.gemini_client as gc  # noqa: PLC0415
        import app.core.stream as stream_mod  # noqa: PLC0415
        import app.main as app_main  # noqa: PLC0415
        import app.routers.openai as openai_router  # noqa: PLC0415
        from app.config import settings  # noqa: PLC0415
        from app.core.account_pool import Account, account_pool  # noqa: PLC0415
        from app.core.gem_mapping import GemMapping  # noqa: PLC0415
        from app.core.log_store import LogStore  # noqa: PLC0415
        from app.core.model_mapping import ModelMapping  # noqa: PLC0415
        from evals.fake_upstream.session import FakeJar  # noqa: PLC0415

        self._settings = settings
        self._pool = account_pool
        self._breaker = argon_breaker
        argon_breaker.reset()  # process-global state: never inherit a tripped breaker from an earlier run

        def patch(obj, name, value):
            old = getattr(obj, name)
            setattr(obj, name, value)
            self._undo.append((obj, name, old))

        async def _no_jitter(*a, **k):
            return None

        patch(gc, "apply_jitter", _no_jitter)
        patch(gc, "asyncio", _ScaledAsyncio(asyncio, self.scale))
        patch(stream_mod, "SSE_KEEPALIVE_INTERVAL", self.keepalive_virtual * self.scale)
        patch(openai_router, "_SSE_KEEPALIVE_INTERVAL", self.keepalive_virtual * self.scale)
        patch(settings, "acquire_timeout", self.acquire_timeout_virtual * self.scale)

        accounts = []
        for i in range(self.n_accounts):
            client = gc.GeminiWebClient(psid=f"fake-psid-{i}", psidts="fake-psidts")
            client._cookie_jar = FakeJar()
            client._http = self.upstream.session()
            client._session_token = "TOKEN"
            client._healthy = True
            client._current_target = gc.header_builder.get_impersonate_target()

            async def _noop(*a, **k):
                return None

            client._send_heartbeat = _noop

            async def _reload_cookies(*a, **k):
                # the real reload_cookies swaps in a live AsyncSession and talks to Google (RotateCookies etc.);
                # keep the harness hermetic: report failure and leave the fake session in place
                return {"success": False, "error": "fake harness: no network"}

            client.reload_cookies = _reload_cookies
            client._ensure_refresh_task = lambda: None
            accounts.append(Account(id=f"acct{i}", psid=f"fake-psid-{i}", psidts="fake", label=f"fake{i}",
                                    client=client))
        patch(account_pool, "_accounts", accounts)
        patch(account_pool, "_max_concurrent", self.max_concurrent)
        patch(account_pool, "_acquire_timeout", self.acquire_timeout_virtual * self.scale)
        patch(account_pool, "_robin_index", 0)

        state = app_main.app.state
        state.log_store = LogStore(persist_path=str(self.data_dir / "logs.json"))
        state.model_mapping = ModelMapping(path=str(self.data_dir / "mm.json"))
        state.gem_mapping = GemMapping(path=str(self.data_dir / "gm.json"))
        with contextlib.suppress(Exception):
            from app.core.usage_stats import UsageStatsStore  # noqa: PLC0415

            state.usage_stats_store = UsageStatsStore(retention_days=1)

        config = uvicorn.Config(app_main.app, host="127.0.0.1", port=0, log_level="warning",
                                lifespan="off", access_log=False)
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            if self._task.done():
                raise RuntimeError("uvicorn failed to start")
            await asyncio.sleep(0.01)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=10)
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo.clear()
        if getattr(self, "_breaker", None) is not None:
            self._breaker.reset()

    async def __aenter__(self) -> "InProcessSUT":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    # ---- helpers ---------------------------------------------------------------------------------
    @property
    def pool(self):
        return self._pool

    def active_slots(self) -> int:
        return sum(a.active_requests for a in self._pool.accounts)

    @contextlib.contextmanager
    def settings(self, **overrides):
        """Temporarily override ``app.config.settings`` attributes (restored on exit)."""
        old = {k: getattr(self._settings, k) for k in overrides}
        for k, v in overrides.items():
            setattr(self._settings, k, v)
        try:
            yield
        finally:
            for k, v in old.items():
                setattr(self._settings, k, v)

    def auth(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}
