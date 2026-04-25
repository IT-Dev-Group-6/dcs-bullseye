"""
Schedule routes.

GET    /agent/v1/instances/{instanceId}/schedule  → current schedule or {}
PUT    /agent/v1/instances/{instanceId}/schedule  → replace schedule
DELETE /agent/v1/instances/{instanceId}/schedule  → clear schedule
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ...scheduler import delete_schedule, get_schedule, save_schedule

router = APIRouter()


def _get_instance_name(request: Request, instance_id: str) -> str:
    config = request.app.state.config
    for inst in config.instances:
        if inst.service_name == instance_id:
            return inst.name
    raise HTTPException(status_code=404, detail=f"Instance {instance_id!r} not found")


@router.get("/instances/{instanceId}/schedule")
async def get_instance_schedule(instanceId: str, request: Request) -> dict:
    name = _get_instance_name(request, instanceId)
    return get_schedule(name) or {}


@router.put("/instances/{instanceId}/schedule")
async def set_instance_schedule(instanceId: str, request: Request) -> dict:
    name = _get_instance_name(request, instanceId)
    body = await request.json()
    save_schedule(name, body)
    return body


@router.delete("/instances/{instanceId}/schedule", status_code=204)
async def delete_instance_schedule(instanceId: str, request: Request) -> None:
    name = _get_instance_name(request, instanceId)
    if not delete_schedule(name):
        raise HTTPException(status_code=404, detail=f"No schedule set for {name!r}")
