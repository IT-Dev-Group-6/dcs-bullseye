"""
POST /api/v1/bench/runs  — ingest a bench run pushed by afterburner on the agent node
GET  /api/v1/bench/runs  — list runs (public, no auth — read-only)
GET  /api/v1/bench/runs/{run_id} — full run with timeseries (public)

Agent auth for POST: X-Host-Id + X-Agent-Key headers (same as analytics).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

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


class BenchRunPayload(BaseModel):
    mission: str
    started_at: str
    ended_at: str | None = None
    duration_s: int | None = None
    notes: str | None = None
    bench_timeseries: list[BenchTimeseriesRow] = []
    cpu_timeseries: list[BenchCpuRow] = []
    findings: list[BenchFinding] = []


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

    logger.info(
        "[bench] run %s recorded for host %s — mission=%s ts=%d cpu=%d findings=%d",
        run_id,
        x_host_id,
        payload.mission,
        len(payload.bench_timeseries),
        len(payload.cpu_timeseries),
        len(payload.findings),
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
