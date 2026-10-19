"""Structured log REST API endpoints."""

from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

from app.core.log_store import LogStore

router = APIRouter(prefix="/admin/logs", tags=["Logs"])


def get_store(request: Request) -> LogStore:
    return request.app.state.log_store


class LogStateUpdate(BaseModel):
    enabled: Optional[bool] = None
    paused: Optional[bool] = None


@router.get("")
async def list_logs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    direction: str = Query(default="all"),
    search: str = Query(default=""),
    traffic_type: str = Query(default="all"),
):
    store = get_store(request)
    return store.query(
        limit=limit,
        offset=offset,
        direction=direction,
        search=search,
        traffic_type=traffic_type,
    )


@router.get("/state")
async def get_log_state(request: Request):
    store = get_store(request)
    return store.get_state()


@router.post("/state")
async def set_log_state(request: Request, body: LogStateUpdate):
    store = get_store(request)
    return store.set_state(enabled=body.enabled, paused=body.paused)


@router.post("/clear")
async def clear_logs(request: Request):
    store = get_store(request)
    store.clear()
    return {"ok": True}


@router.delete("")
async def delete_logs(request: Request):
    store = get_store(request)
    store.clear()
    return {"ok": True}


@router.get("/{record_id}")
async def get_log_record(request: Request, record_id: str):
    store = get_store(request)
    record = store.get(record_id)
    if record is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"message": "Record not found", "type": "not_found"}},
        )
    return record
