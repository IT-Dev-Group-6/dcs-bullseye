"""
GET /agent/v1/instances                         — list all instances with status
GET /agent/v1/instances/{instanceId}/status     — runtime status for one instance
GET /agent/v1/instances/{instanceId}/missions   — list .miz files in missions_dir

instanceId matches service_name first, then name (case-insensitive).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

from ...config import InstanceConfig
from ...controller import DcsController
from ...security import safe_join, sanitize_miz_filename
from ..models import (
    InstanceRuntime,
    InstanceStatus,
    InstanceSummary,
    nssm_to_instance_status,
)

router = APIRouter()


def find_instance(config, instance_id: str) -> InstanceConfig:
    """Look up an instance by service_name, then by name (case-insensitive)."""
    key = instance_id.lower()
    for inst in config.instances:
        if inst.service_name.lower() == key or inst.name.lower() == key:
            return inst
    raise HTTPException(
        status_code=404,
        detail=f"No instance with id {instance_id!r}. "
        f"Known: {', '.join(i.service_name for i in config.instances)}",
    )


async def _get_status(ctrl: DcsController, inst: InstanceConfig) -> InstanceStatus:
    loop = asyncio.get_running_loop()
    raw = await loop.run_in_executor(None, ctrl.status, inst)
    return nssm_to_instance_status(raw)


@router.get("/instances", response_model=list[InstanceSummary])
async def list_instances(request: Request) -> list[InstanceSummary]:
    config = request.app.state.config
    ctrl: DcsController = request.app.state.controller

    results = []
    for inst in config.instances:
        status = await _get_status(ctrl, inst)
        results.append(
            InstanceSummary(
                instanceId=inst.service_name,
                name=inst.name,
                serviceName=inst.service_name,
                autoStart=inst.auto_start,
                ports=inst.ports,
                status=status,
            )
        )
    return results


@router.get("/instances/{instanceId}/status", response_model=InstanceRuntime)
async def get_instance_status(instanceId: str, request: Request) -> InstanceRuntime:
    config = request.app.state.config
    ctrl: DcsController = request.app.state.controller

    inst = find_instance(config, instanceId)
    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, ctrl.runtime_info, inst)

    status = nssm_to_instance_status(info.get("status", "SERVICE_STOPPED"))
    now = datetime.now(timezone.utc)

    started_at: datetime | None = None
    uptime_seconds: float | None = None
    if s := info.get("started_at"):
        try:
            started_at = datetime.fromisoformat(s)
            uptime_seconds = (now - started_at).total_seconds()
        except ValueError:
            pass
        except TypeError:
            pass

    mission_started_at: datetime | None = None
    if m := info.get("mission_started_at"):
        try:
            mission_started_at = datetime.fromisoformat(m)
        except ValueError:
            pass
        except TypeError:
            pass

    return InstanceRuntime(
        status=status,
        observedAt=now,
        pid=info.get("pid"),
        startedAt=started_at,
        uptimeSeconds=uptime_seconds,
        missionStartedAt=mission_started_at,
        missionName=info.get("mission_name"),
        map=info.get("map"),
        playerCount=info.get("player_count"),
        players=info.get("players") or [],
        missionTimeSeconds=info.get("mission_time_seconds"),
    )


@router.post("/instances/{instanceId}/upload")
async def upload_mission(instanceId: str, request: Request) -> dict:
    """Save a .miz file to the instance's missions directory."""
    config = request.app.state.config
    inst = find_instance(config, instanceId)

    raw_filename = ""
    cd = request.headers.get("Content-Disposition", "")
    for part in cd.split(";"):
        part = part.strip()
        if part.startswith("filename="):
            raw_filename = part[len("filename=") :].strip().strip('"')
    if not raw_filename:
        raise HTTPException(
            status_code=400, detail="Missing filename in Content-Disposition header"
        )

    try:
        filename = sanitize_miz_filename(raw_filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    data = await request.body()
    if len(data) > config.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {config.max_upload_bytes // (1024 * 1024)} MB)",
        )

    missions_dir = Path(inst.missions_dir)
    missions_dir.mkdir(parents=True, exist_ok=True)
    try:
        dest = safe_join(missions_dir, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if dest.exists():
        raise HTTPException(
            status_code=409, detail=f"A file named {filename!r} already exists"
        )
    dest.write_bytes(data)
    return {"path": str(dest), "filename": filename, "size": len(data)}


@router.get("/instances/{instanceId}/missions")
async def list_missions(instanceId: str, request: Request) -> dict:
    config = request.app.state.config
    inst = find_instance(config, instanceId)
    missions_dir = Path(inst.missions_dir)
    if not missions_dir.exists():
        return {"items": []}
    return {"items": sorted(p.name for p in missions_dir.glob("*.miz"))}


@router.delete("/instances/{instanceId}/missions/{filename}", status_code=204)
async def delete_mission(instanceId: str, filename: str, request: Request) -> Response:
    """Delete a .miz file from the instance's missions directory."""
    config = request.app.state.config
    ctrl: DcsController = request.app.state.controller
    inst = find_instance(config, instanceId)
    try:
        filename = sanitize_miz_filename(filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, ctrl.delete_mission, inst, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(status_code=204)


@router.post("/instances/{instanceId}/missions/{filename}/copy-to-active")
async def copy_mission_to_active(
    instanceId: str, filename: str, request: Request
) -> dict:
    """Copy a .miz from the instance's missions_dir into active_missions_dir."""
    config = request.app.state.config
    ctrl: DcsController = request.app.state.controller
    inst = find_instance(config, instanceId)
    try:
        filename = sanitize_miz_filename(filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None, ctrl.copy_mission_to_active, inst, filename
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


@router.get("/missions")
async def list_active_missions(request: Request) -> dict:
    """List .miz files in the root of active_missions_dir (not subfolders)."""
    config = request.app.state.config
    active_dir = config.active_missions_dir
    if not active_dir:
        raise HTTPException(
            status_code=404, detail="active_missions_dir not configured"
        )
    root = Path(active_dir)
    if not root.exists():
        return {"items": []}
    items = []
    for miz in sorted(root.glob("*.miz")):
        items.append(
            {
                "path": str(miz),
                "name": miz.stem,
                "relative_path": miz.name,
                "size_bytes": miz.stat().st_size,
                "modified_at": datetime.fromtimestamp(
                    miz.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
            }
        )
    return {"items": items}


@router.get("/missions/{filename}")
async def download_active_mission(filename: str, request: Request) -> Response:
    """Download a .miz file from active_missions_dir root."""
    from fastapi.responses import FileResponse

    config = request.app.state.config
    active_dir = config.active_missions_dir
    if not active_dir:
        raise HTTPException(
            status_code=404, detail="active_missions_dir not configured"
        )
    try:
        filename = sanitize_miz_filename(filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    root = Path(active_dir).resolve()
    # Derive the path from the filesystem rather than constructing it from
    # user-supplied data.  iterdir() entries are not tainted by user input.
    path = next((e for e in root.iterdir() if e.is_file() and e.name == filename), None)
    if path is None:
        raise HTTPException(status_code=404, detail=f"{filename!r} not found")
    return FileResponse(path, media_type="application/octet-stream", filename=filename)


@router.post("/missions/upload")
async def upload_active_mission(request: Request) -> dict:
    """Upload a .miz file to active_missions_dir root."""
    config = request.app.state.config
    ctrl: DcsController = request.app.state.controller

    raw_filename = ""
    cd = request.headers.get("Content-Disposition", "")
    for part in cd.split(";"):
        part = part.strip()
        if part.startswith("filename="):
            raw_filename = part[len("filename=") :].strip().strip('"')
    if not raw_filename:
        raise HTTPException(
            status_code=400, detail="Missing filename in Content-Disposition header"
        )

    try:
        filename = sanitize_miz_filename(raw_filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    data = await request.body()
    if len(data) > config.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {config.max_upload_bytes // (1024 * 1024)} MB)",
        )

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None, ctrl.upload_active_mission, filename, data
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


@router.delete("/missions/{filename}", status_code=200)
async def delete_active_mission(filename: str, request: Request) -> dict:
    """Move a .miz from active_missions_dir root to Backup_Missions/ (with backup)."""
    try:
        filename = sanitize_miz_filename(filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    ctrl: DcsController = request.app.state.controller
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, ctrl.delete_active_mission, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
