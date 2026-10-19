import asyncio
import contextlib
import json
from typing import Any, AsyncGenerator, AsyncIterator


async def split_into_chunks(text: str, delay: float = 0.03) -> AsyncGenerator[str, None]:
    words = text.split(" ")
    for i, word in enumerate(words):
        chunk = word if i == len(words) - 1 else word + " "
        yield chunk
        await asyncio.sleep(delay)


def format_sse(data: dict | str) -> str:
    if isinstance(data, dict):
        return f"data: {json.dumps(data)}\n\n"
    return f"data: {data}\n\n"


SSE_KEEPALIVE_INTERVAL: float = 3.0
SSE_KEEPALIVE_FRAME: str = ": ping\n\n"


class InitialPrefixBuffer:
    """Hold initial text until it cannot be a sensitive/error prefix.

    Streaming model output is append-only, so a prefix cannot be retracted once sent. This small gate buffers
    only the beginning of a response while it could still match one of ``prefixes``. It releases the buffered
    text as soon as it diverges, or reports a matching prefix without releasing any of it.

    Leading whitespace is ignored for matching (the non-streaming error checks strip before ``startswith``),
    but is preserved if the text is released. ``max_prefix_length`` exposes the required upper bound for
    callers/tests; with a finite prefix set the unresolved non-whitespace buffer cannot grow beyond it.
    """

    def __init__(self, prefixes, *, ignore_leading_whitespace: bool = True):
        self.prefixes = tuple(prefix for prefix in prefixes if isinstance(prefix, str) and prefix)
        self.max_prefix_length = max((len(prefix) for prefix in self.prefixes), default=0)
        self.ignore_leading_whitespace = ignore_leading_whitespace
        self._pending = ""
        self._resolved = False
        self.matched_prefix: str | None = None

    def _candidate(self, text: str) -> str:
        return text.lstrip() if self.ignore_leading_whitespace else text

    def match(self, text: str) -> str | None:
        """Return the matching configured prefix, if ``text`` starts with one."""
        candidate = self._candidate(text)
        return next((prefix for prefix in self.prefixes if candidate.startswith(prefix)), None)

    def feed(self, text: str) -> tuple[str, str | None]:
        """Return ``(safe_text, matched_prefix)`` for the next piece of the stream.

        Once a match is found the pending prefix is discarded and the buffer remains resolved. Once the text
        diverges from every possible prefix, all pending text is safe and is released; subsequent input passes
        straight through.
        """
        if not text:
            return "", self.matched_prefix
        if self._resolved:
            return text, self.matched_prefix

        self._pending += text
        matched = self.match(self._pending)
        if matched:
            self.matched_prefix = matched
            self._pending = ""
            self._resolved = True
            return "", matched

        candidate = self._candidate(self._pending)
        if not candidate or any(prefix.startswith(candidate) for prefix in self.prefixes):
            return "", None

        safe_text = self._pending
        self._pending = ""
        self._resolved = True
        return safe_text, None

    def finish(self) -> tuple[str, str | None]:
        """Resolve a stream that ended before its prefix could be disambiguated."""
        if self._resolved:
            return "", self.matched_prefix
        matched = self.match(self._pending)
        if matched:
            self.matched_prefix = matched
            self._pending = ""
            self._resolved = True
            return "", matched
        safe_text = self._pending
        self._pending = ""
        self._resolved = True
        return safe_text, None


async def iter_with_keepalive(agen, interval: float | None = None) -> AsyncIterator[tuple[str, Any]]:
    """包住 async 生成器 agen：
      - 每个上游事件 → yield ("evt", evt)
      - 上游静默超过 interval → yield ("ping", None)
      - 上游正常结束 → 本生成器正常结束
      - 上游抛异常 → 事件耗尽处原样重抛，交给调用方既有 try/except
    interval 默认 None → 在【调用时】从模块常量 SSE_KEEPALIVE_INTERVAL 解析（可被 monkeypatch）。
    实现要点：后台 task 消费 agen 塞进 Queue；主循环 wait_for(queue.get(), interval)
    超时只取消安全的 queue.get()，绝不取消 anext（避免击穿上游生成器）。"""
    if interval is None:
        interval = SSE_KEEPALIVE_INTERVAL
    queue: asyncio.Queue = asyncio.Queue()

    async def _pump():
        try:
            async for evt in agen:
                await queue.put(("evt", evt))
        except Exception as e:  # noqa: BLE001 上游异常透传给调用方
            await queue.put(("err", e))
        else:
            await queue.put(("done", None))

    task = asyncio.create_task(_pump())
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                yield ("ping", None)
                continue
            if kind == "done":
                return
            if kind == "err":
                raise payload
            yield ("evt", payload)
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task


async def sse_keepalive_during(task: asyncio.Task, interval: float | None = None):
    """在后台 task 完成前周期 yield SSE comment ping，防止网关首字节/读超时。

    关键：task 在某个 interval 窗口内【抛异常】完成时，wait_for 会原样重抛该异常而非
    TimeoutError。绝不能让它逃出本生成器——否则会击穿调用方的 try/except。故非超时的完成
    （成功或异常）一律 return，让控制权落回调用方的 task.result()，由那里统一做错误映射。"""
    if interval is None:
        interval = SSE_KEEPALIVE_INTERVAL
    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=interval)
        except asyncio.TimeoutError:
            yield SSE_KEEPALIVE_FRAME
        except Exception:
            return
