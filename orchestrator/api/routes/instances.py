"""
Instance management routes.

GET  /instances                        → list[InstanceSummary]
POST /instances                        → InstanceRef  (201)
GET  /instances/{instanceId}           → InstanceRef | 404
GET  /instances/{instanceId}/status    → InstanceRuntime | 404
GET  /instances/{instanceId}/missions  → {"items": list[str]}
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ...agent_client import AgentClient, AgentError
from ...database import Database
from ..models import InstanceCreate, InstanceRef, InstanceRuntime, InstanceSummary

router = APIRouter()


def _row_to_ref(row: dict) -> InstanceRef:
    return InstanceRef(
        id=row["id"],
        hostId=row["host_id"],
        serviceName=row["service_name"],
        name=row["name"],
        tags=row["tags"],
        createdAt=row["created_at"],
    )


async def _get_instance_or_404(db: Database, instance_id: str) -> dict:
    row = await db.get_instance(instance_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Instance {instance_id!r} not found"
        )
    return row


async def _fetch_runtime(host_row: dict, service_name: str) -> InstanceRuntime | None:
    """Fetch runtime status from agent; returns None on any failure."""
    agent_base = host_row["agent_url"].rstrip("/") + "/agent/v1"
    try:
        async with AgentClient(agent_base, host_row["agent_api_key"]) as client:
            data = await client.get_instance_status(service_name)
        return InstanceRuntime(
            status=data.get("status", "unknown"),
            observedAt=datetime.now(timezone.utc),
            pid=data.get("pid"),
            startedAt=data.get("startedAt"),
            uptimeSeconds=data.get("uptimeSeconds"),
            missionStartedAt=data.get("missionStartedAt"),
            missionName=data.get("missionName"),
            map=data.get("map"),
            playerCount=data.get("playerCount"),
            players=data.get("players"),
            missionTimeSeconds=data.get("missionTimeSeconds"),
            lastExitCode=data.get("lastExitCode"),
            lastError=data.get("lastError"),
        )
    except (AgentError, Exception):
        return InstanceRuntime(status="unknown", observedAt=datetime.now(timezone.utc))


@router.get("/instances", response_model=list[InstanceSummary])
async def list_instances(request: Request) -> list[InstanceSummary]:
    db: Database = request.app.state.db
    rows = await db.list_instances()
    if not rows:
        return []

    # Gather all host rows we need
    host_ids = list({r["host_id"] for r in rows})
    host_map: dict[str, dict] = {}
    for hid in host_ids:
        host_row = await db.get_host(hid)
        if host_row:
            host_map[hid] = host_row

    # Fetch runtime statuses concurrently; failures degrade to unknown (no 500)
    async def _summarize(row: dict) -> InstanceSummary:
        host_row = host_map.get(row["host_id"])
        if host_row:
            runtime = await _fetch_runtime(host_row, row["service_name"])
        else:
            runtime = InstanceRuntime(
                status="unknown", observedAt=datetime.now(timezone.utc)
            )
        return InstanceSummary(
            id=row["id"],
            hostId=row["host_id"],
            serviceName=row["service_name"],
            name=row["name"],
            tags=row["tags"],
            createdAt=row["created_at"],
            runtime=runtime,
        )

    results = await asyncio.gather(
        *[_summarize(r) for r in rows], return_exceptions=True
    )

    summaries: list[InstanceSummary] = []
    for r, row in zip(results, rows):
        if isinstance(r, Exception):
            # Degrade gracefully
            summaries.append(
                InstanceSummary(
                    id=row["id"],
                    hostId=row["host_id"],
                    serviceName=row["service_name"],
                    name=row["name"],
                    tags=row["tags"],
                    createdAt=row["created_at"],
                    runtime=InstanceRuntime(
                        status="unknown", observedAt=datetime.now(timezone.utc)
                    ),
                )
            )
        else:
            summaries.append(r)
    return summaries


@router.post("/instances", response_model=InstanceRef, status_code=201)
async def create_instance(body: InstanceCreate, request: Request) -> JSONResponse:
    db: Database = request.app.state.db

    # Verify host exists
    host_row = await db.get_host(body.hostId)
    if host_row is None:
        raise HTTPException(status_code=404, detail=f"Host {body.hostId!r} not found")

    row = await db.create_instance(
        host_id=body.hostId,
        service_name=body.serviceName,
        name=body.name,
        tags=body.tags,
    )
    return JSONResponse(status_code=201, content=_row_to_ref(row).model_dump())


@router.get("/instances/{instanceId}", response_model=InstanceRef)
async def get_instance(instanceId: str, request: Request) -> InstanceRef:
    db: Database = request.app.state.db
    row = await _get_instance_or_404(db, instanceId)
    return _row_to_ref(row)


@router.get("/instances/{instanceId}/status", response_model=InstanceRuntime)
async def get_instance_status(instanceId: str, request: Request) -> InstanceRuntime:
    db: Database = request.app.state.db
    inst_row = await _get_instance_or_404(db, instanceId)

    host_row = await db.get_host(inst_row["host_id"])
    if host_row is None:
        raise HTTPException(
            status_code=404, detail=f"Host {inst_row['host_id']!r} not found"
        )

    runtime = await _fetch_runtime(host_row, inst_row["service_name"])
    return runtime


@router.post("/instances/{instanceId}/upload")
async def upload_instance_mission(instanceId: str, request: Request) -> dict:
    """Proxy a .miz file upload to the agent's missions directory."""
    db: Database = request.app.state.db
    inst_row = await _get_instance_or_404(db, instanceId)

    host_row = await db.get_host(inst_row["host_id"])
    if host_row is None:
        raise HTTPException(
            status_code=404, detail=f"Host {inst_row['host_id']!r} not found"
        )

    filename = ""
    cd = request.headers.get("Content-Disposition", "")
    for part in cd.split(";"):
        part = part.strip()
        if part.startswith("filename="):
            filename = part[len("filename=") :].strip().strip('"')
    if not filename:
        raise HTTPException(
            status_code=400, detail="Missing filename in Content-Disposition header"
        )
    if not filename.lower().endswith(".miz"):
        raise HTTPException(status_code=400, detail="Only .miz files are accepted")

    data = await request.body()
    agent_base = host_row["agent_url"].rstrip("/") + "/agent/v1"
    try:
        async with AgentClient(agent_base, host_row["agent_api_key"]) as agent:
            result = await agent.upload_mission(
                inst_row["service_name"], filename, data
            )
        return result
    except AgentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/instances/{instanceId}/missions")
async def list_instance_missions(instanceId: str, request: Request) -> dict:
    db: Database = request.app.state.db
    inst_row = await _get_instance_or_404(db, instanceId)

    host_row = await db.get_host(inst_row["host_id"])
    if host_row is None:
        raise HTTPException(
            status_code=404, detail=f"Host {inst_row['host_id']!r} not found"
        )

    agent_base = host_row["agent_url"].rstrip("/") + "/agent/v1"
    try:
        async with AgentClient(agent_base, host_row["agent_api_key"]) as client:
            items = await client.list_missions(inst_row["service_name"])
    except (AgentError, Exception):
        items = []
    return {"items": items}


@router.post("/instances/{instanceId}/missions/{filename}/copy-to-active")
async def copy_instance_mission_to_active(
    instanceId: str, filename: str, request: Request
) -> dict:
    """Copy a .miz from the instance's missions folder into the shared Active Missions folder."""
    db: Database = request.app.state.db
    inst_row = await _get_instance_or_404(db, instanceId)

    host_row = await db.get_host(inst_row["host_id"])
    if host_row is None:
        raise HTTPException(
            status_code=404, detail=f"Host {inst_row['host_id']!r} not found"
        )

    agent_base = host_row["agent_url"].rstrip("/") + "/agent/v1"
    try:
        async with AgentClient(agent_base, host_row["agent_api_key"]) as client:
            return await client.copy_mission_to_active(
                inst_row["service_name"], filename
            )
    except AgentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.delete("/instances/{instanceId}/missions/{filename}", status_code=204)
async def delete_instance_mission(
    instanceId: str, filename: str, request: Request
) -> Response:
    """Proxy a mission delete request to the agent."""
    db: Database = request.app.state.db
    inst_row = await _get_instance_or_404(db, instanceId)

    host_row = await db.get_host(inst_row["host_id"])
    if host_row is None:
        raise HTTPException(
            status_code=404, detail=f"Host {inst_row['host_id']!r} not found"
        )

    agent_base = host_row["agent_url"].rstrip("/") + "/agent/v1"
    try:
        async with AgentClient(agent_base, host_row["agent_api_key"]) as client:
            await client.delete_mission(inst_row["service_name"], filename)
    except AgentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return Response(status_code=204)


async def _agent_for_instance(db: Database, instance_id: str) -> tuple[dict, dict]:
    """Return (inst_row, host_row) or raise 404."""
    inst_row = await _get_instance_or_404(db, instance_id)
    host_row = await db.get_host(inst_row["host_id"])
    if host_row is None:
        raise HTTPException(
            status_code=404, detail=f"Host {inst_row['host_id']!r} not found"
        )
    return inst_row, host_row


@router.get("/instances/{instanceId}/schedule")
async def get_instance_schedule(instanceId: str, request: Request) -> dict:
    db: Database = request.app.state.db
    inst_row, host_row = await _agent_for_instance(db, instanceId)
    agent_base = host_row["agent_url"].rstrip("/") + "/agent/v1"
    try:
        async with AgentClient(agent_base, host_row["agent_api_key"]) as client:
            return await client.get_instance_schedule(inst_row["service_name"])
    except AgentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.put("/instances/{instanceId}/schedule")
async def set_instance_schedule(instanceId: str, request: Request) -> dict:
    db: Database = request.app.state.db
    inst_row, host_row = await _agent_for_instance(db, instanceId)
    body = await request.json()
    agent_base = host_row["agent_url"].rstrip("/") + "/agent/v1"
    try:
        async with AgentClient(agent_base, host_row["agent_api_key"]) as client:
            return await client.set_instance_schedule(inst_row["service_name"], body)
    except AgentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.delete("/instances/{instanceId}/schedule", status_code=204)
async def delete_instance_schedule(instanceId: str, request: Request) -> Response:
    db: Database = request.app.state.db
    inst_row, host_row = await _agent_for_instance(db, instanceId)
    agent_base = host_row["agent_url"].rstrip("/") + "/agent/v1"
    try:
        async with AgentClient(agent_base, host_row["agent_api_key"]) as client:
            await client.delete_instance_schedule(inst_row["service_name"])
    except AgentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return Response(status_code=204)
