"""Router-level regression tests for account-slot release on SSE/NDJSON disconnects.

The account pool owns leases, but every protocol router wraps its stream at least once.  Closing
that outer generator must propagate all the way down to ``AccountPool.generate_stream``; otherwise
an account remains permanently busy after a browser, SDK, or reverse proxy drops the connection.
"""

import asyncio

from app.core.account_pool import Account, AccountPool, AccountStatus


class _HangingStreamClient:
    """Emit one useful delta, then remain in-flight until the consumer disconnects."""

    is_healthy = True

    def __init__(self):
        self.closed = False

    async def generate_stream(self, prompt, model, conversation_id="", attachments=None,
                              gem_id=None, extended_thinking=False):
        try:
            yield {"type": "delta", "text": "hello"}
            await asyncio.Event().wait()
        finally:
            self.closed = True


def _pool():
    client = _HangingStreamClient()
    pool = AccountPool()
    account = Account(
        id="account-0",
        psid="test-cookie",
        psidts="",
        label="test",
        status=AccountStatus.ACTIVE,
        client=client,
    )
    pool._accounts = [account]
    pool._max_concurrent = 1
    pool._acquire_timeout = 0.5
    return pool, account, client


async def _close_while_slot_is_held(agen, account, client):
    # Protocols emit different numbers of structural frames before the first model delta.
    # Drive until the fake upstream has acquired its slot and emitted that delta.
    for _ in range(12):
        await asyncio.wait_for(agen.__anext__(), timeout=1)
        if account.active_requests == 1:
            break
    assert account.active_requests == 1, "the test never reached an active upstream stream"

    await agen.aclose()  # abrupt downstream disconnect / Starlette cancellation equivalent
    await asyncio.sleep(0)  # let a cancelled keepalive pump run its finalizer

    assert client.closed is True
    assert account.active_requests == 0
    # A user cancelling a request is neutral account health, not an upstream failure.
    assert account.consecutive_failures == 0
    assert account.error_count == 0


def test_openai_midstream_disconnect_releases_account_slot(monkeypatch):
    import app.routers.openai as router

    pool, account, client = _pool()
    monkeypatch.setattr(router, "gemini_client", pool)

    async def run():
        agen = router._stream_response("prompt", "gemini-pro", False, display_model="gemini-pro")
        await _close_while_slot_is_held(agen, account, client)

    asyncio.run(run())


def test_responses_midstream_disconnect_releases_account_slot(monkeypatch):
    import app.routers.responses as router

    pool, account, client = _pool()
    monkeypatch.setattr(router, "gemini_client", pool)

    async def run():
        agen = router._stream_gemini_response(
            None, "prompt", "gemini-pro", False, [], None, None, {}, False,
        )
        await _close_while_slot_is_held(agen, account, client)

    asyncio.run(run())


def test_claude_midstream_disconnect_releases_account_slot(monkeypatch):
    import app.routers.claude as router

    pool, account, client = _pool()
    monkeypatch.setattr(router, "gemini_client", pool)

    async def run():
        agen = router._stream_claude("prompt", "gemini-pro", False)
        await _close_while_slot_is_held(agen, account, client)

    asyncio.run(run())


def test_gemini_midstream_disconnect_releases_account_slot(monkeypatch):
    import app.routers.gemini as router
    from app.models.gemini import GeminiContent, GeminiPart, GeminiRequest

    pool, account, client = _pool()
    monkeypatch.setattr(router, "gemini_client", pool)

    class _State:
        pass

    class _App:
        state = _State()

    class _Request:
        app = _App()

    request_body = GeminiRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="hello")])],
    )

    async def run():
        response = await router.stream_generate_content.__wrapped__(
            "gemini-pro", request_body, _Request(),
        )
        await _close_while_slot_is_held(response.body_iterator, account, client)

    asyncio.run(run())
