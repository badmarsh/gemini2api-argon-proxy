"""Structured log store with circular buffer, filtering, pagination, and persistence."""

import json
import uuid
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional


@dataclass
class LogRecord:
    id: str
    request_id: str
    direction: str
    ts: str
    method: str
    path: str
    model: Optional[str] = None
    provider: Optional[str] = None
    status: Optional[int] = None
    latency_ms: Optional[float] = None
    stream: Optional[bool] = None
    error: Optional[str] = None
    tags: list = field(default_factory=list)
    ttft_ms: Optional[float] = None
    total_latency_ms: Optional[float] = None
    max_inter_chunk_gap_ms: Optional[float] = None
    error_class: Optional[str] = None
    fallback_used: bool = False
    fallback_reason: Optional[str] = None
    regenerations: int = 0
    account_hash: Optional[str] = None
    served_by: Optional[str] = None
    finish_reason: Optional[str] = None
    parser_failure: Optional[str] = None
    parser_response_length: Optional[int] = None
    parser_repaired: bool = False
    truncated_suspected: bool = False
    # Wall-clock time the request entered the proxy. ``ts`` is the time the record
    # was written (after the response), so the two together describe the request
    # window. Both are ISO-8601 UTC timestamps.
    ingress_ts: Optional[str] = None
    request: Optional[dict] = None
    response: Optional[dict] = None

    def to_dict(self) -> dict:
        return asdict(self)

    def matches_search(self, query: str) -> bool:
        q = query.lower()
        searchable = [
            self.id,
            self.request_id,
            self.direction,
            self.path,
            self.method,
            self.model or "",
            self.error or "",
            self.error_class or "",
            self.fallback_reason or "",
            self.served_by or "",
            self.parser_failure or "",
            str(self.status) if self.status else "",
            *(self.tags or []),
        ]
        return any(q in s.lower() for s in searchable)


@dataclass
class LogState:
    enabled: bool = True
    paused: bool = False


class LogStore:
    def __init__(self, capacity: int = 2000, persist_path: str = "data/logs.json"):
        self._buffer: deque[LogRecord] = deque(maxlen=capacity)
        self._lock = Lock()
        self._state = LogState()
        self._id_index: dict[str, LogRecord] = {}
        self._persist_path = Path(persist_path)
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self._persist_path.exists():
            return
        try:
            raw = json.loads(self._persist_path.read_text(encoding="utf-8"))
            for item in raw:
                record = LogRecord(**item)
                self._buffer.append(record)
                self._id_index[record.id] = record
        except Exception:
            pass

    def flush(self) -> None:
        if not self._dirty:
            return
        with self._lock:
            # 落盘绝不带 request/response body（issue #10 followup F1a）：
            # 1) 隐私——用户完整提示词不该躺在 data/logs.json 里；
            # 2) 体积/性能——2000 条 * 32KB body 能把这次 write_text 从 0.6MB/56ms
            #    拖成 119MB/4s+，而 flush 是每 10s 跑一次的定时任务。
            # 内存里的 buffer（面板详情视图读的就是它）完全不受影响，body 仍在。
            data = []
            for r in self._buffer:
                d = r.to_dict()
                d["request"] = None
                d["response"] = None
                data.append(d)
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            self._persist_path.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
            self._dirty = False
        except Exception:
            pass

    @property
    def state(self) -> LogState:
        return self._state

    def add(self, record: LogRecord) -> None:
        if not self._state.enabled or self._state.paused:
            return
        with self._lock:
            if len(self._buffer) == self._buffer.maxlen:
                evicted = self._buffer[0]
                self._id_index.pop(evicted.id, None)
            self._buffer.append(record)
            self._id_index[record.id] = record
        self._dirty = True

    @staticmethod
    def _is_completions_record(r: LogRecord) -> bool:
        if not r or not r.path:
            return False
        return "/completions" in r.path.lower()

    @staticmethod
    def _is_ai_record(r: LogRecord) -> bool:
        if not r:
            return False
        p = (r.path or "").lower()
        if any(
            x in p
            for x in (
                "/v1beta/",
                "/v1/chat/completions",
                "/v1/messages",
                "/v1/completions",
                "/v1/responses",
                "/v1/embeddings",
            )
        ):
            return True
        if r.model and "/health" not in p and "/admin/" not in p:
            return True
        return False

    def query(
        self,
        limit: int = 50,
        offset: int = 0,
        direction: str = "all",
        search: str = "",
        traffic_type: str = "all",
    ) -> dict:
        with self._lock:
            records = list(reversed(self._buffer))

        if direction != "all":
            records = [r for r in records if r.direction == direction]

        tt = (traffic_type or "all").lower()
        if tt == "completions":
            records = [r for r in records if self._is_completions_record(r)]
        elif tt == "ai":
            records = [r for r in records if self._is_ai_record(r)]

        if search:
            records = [r for r in records if r.matches_search(search)]

        total = len(records)
        page = records[offset : offset + limit]
        return {
            "records": [r.to_dict() for r in page],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get(self, record_id: str) -> Optional[dict]:
        with self._lock:
            record = self._id_index.get(record_id)
        if record:
            return record.to_dict()
        return None

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._id_index.clear()
        self._dirty = True
        self.flush()

    def get_state(self) -> dict:
        return {"enabled": self._state.enabled, "paused": self._state.paused}

    def set_state(self, enabled: Optional[bool] = None, paused: Optional[bool] = None) -> dict:
        if enabled is not None:
            self._state.enabled = enabled
        if paused is not None:
            self._state.paused = paused
        return self.get_state()


def create_log_record(
    method: str,
    path: str,
    direction: str = "ingress",
    model: Optional[str] = None,
    status: Optional[int] = None,
    latency_ms: Optional[float] = None,
    stream: Optional[bool] = None,
    error: Optional[str] = None,
    request_body: Optional[dict] = None,
    response_body: Optional[dict] = None,
    ttft_ms: Optional[float] = None,
    total_latency_ms: Optional[float] = None,
    max_inter_chunk_gap_ms: Optional[float] = None,
    error_class: Optional[str] = None,
    fallback_used: bool = False,
    fallback_reason: Optional[str] = None,
    regenerations: int = 0,
    account_hash: Optional[str] = None,
    served_by: Optional[str] = None,
    finish_reason: Optional[str] = None,
    parser_failure: Optional[str] = None,
    parser_response_length: Optional[int] = None,
    parser_repaired: bool = False,
    truncated_suspected: bool = False,
    ingress_ts: Optional[str] = None,
    tags: Optional[list] = None,
) -> LogRecord:
    now = datetime.now(timezone.utc).isoformat()
    return LogRecord(
        id=uuid.uuid4().hex[:12],
        request_id=uuid.uuid4().hex[:8],
        direction=direction,
        ts=now,
        method=method,
        path=path,
        model=model,
        provider="gemini",
        status=status,
        latency_ms=round(latency_ms, 1) if latency_ms is not None else None,
        stream=stream,
        error=error,
        tags=list(tags) if tags else [],
        ttft_ms=round(ttft_ms, 1) if ttft_ms is not None else None,
        total_latency_ms=round(total_latency_ms, 1) if total_latency_ms is not None else None,
        max_inter_chunk_gap_ms=round(max_inter_chunk_gap_ms, 1) if max_inter_chunk_gap_ms is not None else None,
        error_class=error_class,
        fallback_used=bool(fallback_used),
        fallback_reason=fallback_reason,
        regenerations=int(regenerations or 0),
        account_hash=account_hash,
        served_by=served_by,
        finish_reason=finish_reason,
        parser_failure=parser_failure,
        parser_response_length=int(parser_response_length) if parser_response_length is not None else None,
        parser_repaired=bool(parser_repaired),
        truncated_suspected=bool(truncated_suspected),
        ingress_ts=ingress_ts,
        request=request_body,
        response=response_body,
    )
