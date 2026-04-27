"""
POST /api/v1/bench/runs         — ingest a bench run (agent-key auth)
GET  /api/v1/bench/runs         — list runs (public)
GET  /api/v1/bench/runs/{id}    — full run with timeseries (public)

POST /api/v1/bench/queue        — add a mission to the bench queue (master key)
GET  /api/v1/bench/queue        — list queue (master key)
DELETE /api/v1/bench/queue/{id} — remove pending item (master key)
POST /api/v1/bench/queue/{id}/run — trigger a queued item immediately (master key)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from pydantic import BaseModel

from ..auth import require_api_key

logger = logging.getLogger(__name__)
router = APIRouter()


class BenchTimeseriesRow(BaseModel):
    elapsed_s: float
    drift_s: float
    groups: int
    units: int


class BenchCpuRow(BaseModel):
    elapsed_s: float
    cpu_pct: float
    mem_mb: float
    threads: int


class BenchFinding(BaseModel):
    rule_id: str
    severity: str
    detail: str | None = None


class BenchLogIssue(BaseModel):
    issue_type: str
    signature: str
    count: int
    first_line: str | None = None
    last_line: str | None = None
    detail: str | None = None


class BenchRunPayload(BaseModel):
    mission: str
    started_at: str
    ended_at: str | None = None
    duration_s: int | None = None
    intended_duration_s: int | None = None
    bench_elapsed_s: int | None = None
    run_quality: str = "unknown"
    injection_status: str | None = None
    hard_stop_error: str | None = None
    notes: str | None = None
    bench_timeseries: list[BenchTimeseriesRow] = []
    cpu_timeseries: list[BenchCpuRow] = []
    findings: list[BenchFinding] = []
    log_issues: list[BenchLogIssue] = []


@router.post("/bench/runs", status_code=201)
async def ingest_bench_run(
    payload: BenchRunPayload,
    request: Request,
    x_host_id: str = Header(..., alias="X-Host-Id"),
    x_agent_key: str = Header(..., alias="X-Agent-Key"),
) -> dict[str, str]:
    db = request.app.state.db
    host = await db.get_host_by_agent_key(x_host_id, x_agent_key)
    if not host:
        raise HTTPException(status_code=401, detail="Invalid host credentials")

    run_id = await db.create_bench_run(
        host_id=x_host_id,
        mission=payload.mission,
        started_at=payload.started_at,
        ended_at=payload.ended_at,
        duration_s=payload.duration_s,
        intended_duration_s=payload.intended_duration_s,
        bench_elapsed_s=payload.bench_elapsed_s,
        run_quality=payload.run_quality,
        injection_status=payload.injection_status,
        hard_stop_error=payload.hard_stop_error,
        notes=payload.notes,
    )

    if payload.bench_timeseries:
        await db.insert_bench_timeseries(
            run_id, [r.model_dump() for r in payload.bench_timeseries]
        )
    if payload.cpu_timeseries:
        await db.insert_bench_cpu(
            run_id, [r.model_dump() for r in payload.cpu_timeseries]
        )
    if payload.findings:
        await db.insert_bench_findings(
            run_id, [r.model_dump() for r in payload.findings]
        )
    if payload.log_issues:
        await db.insert_bench_log_issues(
            run_id, [r.model_dump() for r in payload.log_issues]
        )

    logger.info(
        "[bench] run %s recorded for host %s — mission=%s ts=%d cpu=%d findings=%d issues=%d quality=%s",
        run_id,
        x_host_id,
        payload.mission,
        len(payload.bench_timeseries),
        len(payload.cpu_timeseries),
        len(payload.findings),
        len(payload.log_issues),
        payload.run_quality,
    )
    return {"id": run_id}


@router.get("/bench/runs", response_model=list[dict[str, Any]])
async def list_bench_runs(
    request: Request,
    host_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    db = request.app.state.db
    return await db.list_bench_runs(host_id=host_id, limit=min(limit, 200))


@router.get("/bench/runs/{run_id}", response_model=dict[str, Any])
async def get_bench_run(run_id: str, request: Request) -> dict[str, Any]:
    db = request.app.state.db
    run = await db.get_bench_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return run


# ------------------------------------------------------------------
# Bench queue (master API key required)
# ------------------------------------------------------------------


@router.post("/bench/queue", status_code=201, dependencies=[Depends(require_api_key)])
async def enqueue_bench(
    request: Request,
    miz: UploadFile = File(...),
    host_id: str = Form(""),
    instance_id: str = Form(""),
    duration_s: int = Form(1800),
) -> dict[str, Any]:
    config = request.app.state.config
    db = request.app.state.db

    effective_host = host_id or config.bench_host_id
    effective_instance = instance_id or config.bench_instance_id
    if not effective_host or not effective_instance:
        raise HTTPException(
            status_code=422,
            detail="host_id and instance_id required (or set bench_host_id/bench_instance_id in config)",
        )

    if not miz.filename or not miz.filename.lower().endswith(".miz"):
        raise HTTPException(status_code=422, detail="Uploaded file must be a .miz")

    data = await miz.read()
    item = await db.enqueue_bench(
        miz_filename=miz.filename,
        miz_data=data,
        host_id=effective_host,
        instance_id=effective_instance,
        duration_s=duration_s,
    )
    logger.info(
        "[bench/queue] queued %s for %s/%s duration=%ds → %s",
        miz.filename,
        effective_host,
        effective_instance,
        duration_s,
        item["id"],
    )
    return item


@router.get(
    "/bench/queue",
    response_model=list[dict[str, Any]],
    dependencies=[Depends(require_api_key)],
)
async def list_queue(request: Request) -> list[dict[str, Any]]:
    return await request.app.state.db.list_queue()


@router.delete(
    "/bench/queue/{item_id}", status_code=204, dependencies=[Depends(require_api_key)]
)
async def delete_queue_item(item_id: str, request: Request) -> None:
    deleted = await request.app.state.db.delete_queue_item(item_id)
    if not deleted:
        raise HTTPException(
            status_code=404, detail=f"Item not found or not pending: {item_id}"
        )


@router.post("/bench/queue/{item_id}/run", dependencies=[Depends(require_api_key)])
async def trigger_queue_item(item_id: str, request: Request) -> dict[str, str]:
    db = request.app.state.db
    item = await db.get_queue_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Queue item not found: {item_id}")
    if item["status"] != "pending":
        raise HTTPException(
            status_code=409, detail=f"Item is not pending (status={item['status']})"
        )

    scheduler = getattr(request.app.state, "bench_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Bench scheduler not running")

    asyncio.create_task(scheduler.run_item(item_id))
    return {"status": "started", "id": item_id}
