import asyncio
import pytest
from app.core import stream as stream_mod
from app.core.stream import iter_with_keepalive, SSE_KEEPALIVE_FRAME, SSE_KEEPALIVE_INTERVAL


async def _agen_from(items, gap=0.0):
    for it in items:
        if gap:
            await asyncio.sleep(gap)
        yield it


async def _collect(agen):
    out = []
    async for kv in agen:
        out.append(kv)
    return out


def test_passes_events_in_order_no_ping_when_fast():
    out = asyncio.run(_collect(iter_with_keepalive(_agen_from(["a", "b", "c"]), interval=5.0)))
    assert out == [("evt", "a"), ("evt", "b"), ("evt", "c")]


def test_emits_ping_during_silent_gap():
    # gap (0.15s) > interval (0.05s) → at least one ping, events still in order
    out = asyncio.run(_collect(iter_with_keepalive(_agen_from(["x", "y"], gap=0.15), interval=0.05)))
    kinds = [k for k, _ in out]
    assert ("ping", None) in out
    assert [v for k, v in out if k == "evt"] == ["x", "y"]
    assert kinds.index("ping") < kinds.index("evt")  # ping during the initial silent gap


def test_reraises_upstream_exception_after_draining():
    async def _boom():
        yield "ok"
        raise ValueError("upstream failed")

    holder = {"got": []}

    async def _drive():
        async for kv in iter_with_keepalive(_boom(), interval=5.0):
            holder["got"].append(kv)

    with pytest.raises(ValueError, match="upstream failed"):
        asyncio.run(_drive())
    # 关键断言：抛异常前，"ok" 事件必须先被投递（而不是异常直接吞掉未耗尽的事件）
    assert ("evt", "ok") in holder["got"]


def test_early_break_cancels_pump_no_leak():
    cancelled = {"v": False}

    async def _long():
        try:
            yield "first"
            await asyncio.sleep(10)
            yield "never"
        except asyncio.CancelledError:
            cancelled["v"] = True
            raise

    async def _drive():
        agen = iter_with_keepalive(_long(), interval=5.0)
        async for kv in agen:
            assert kv == ("evt", "first")
            break
        await agen.aclose()          # finally → cancel pump → CancelledError into _long
        await asyncio.sleep(0.05)

    asyncio.run(_drive())
    assert cancelled["v"] is True


def test_interval_none_resolves_module_constant_at_call_time(monkeypatch):
    # interval defaults to None and is resolved from the module global at CALL time,
    # so a monkeypatched interval takes effect (Tasks 2/3 rely on this to test pings fast).
    monkeypatch.setattr(stream_mod, "SSE_KEEPALIVE_INTERVAL", 0.05)
    out = asyncio.run(_collect(iter_with_keepalive(_agen_from(["z"], gap=0.15))))  # no interval arg
    assert ("ping", None) in out and ("evt", "z") in out


def test_constants():
    assert SSE_KEEPALIVE_FRAME == ": ping\n\n"
    assert SSE_KEEPALIVE_INTERVAL == 3.0


def test_default_3s_interval_emits_ping_during_gap(monkeypatch):
    """With SSE_KEEPALIVE_INTERVAL=3.0s (new default), a 4s upstream silence must produce a ping."""
    monkeypatch.setattr(stream_mod, "SSE_KEEPALIVE_INTERVAL", 3.0)
    out = asyncio.run(_collect(iter_with_keepalive(_agen_from(["z"], gap=4.0))))
    assert ("ping", None) in out   # 4s gap > 3s interval → at least one ping
    assert ("evt", "z") in out

