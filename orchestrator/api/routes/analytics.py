"""
POST /api/v1/analytics/events  — ingest analytics events pushed by agents
GET  /api/v1/analytics/events  — query stored events (admin, requires master key)

Agent auth: X-Host-Id + X-Agent-Key headers (matches host record in DB).
This lets agents push without knowing the orchestrator master API key.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from orchestrator.events import Event

from ..auth import require_api_key

logger = logging.getLogger(__name__)
router = APIRouter()


class AnalyticsEvent(BaseModel):
    instance_id: str | None = None
    event_type: str  # player_join | player_leave | mission_start | mission_end
    player_name: str | None = None
    mission_name: str | None = None
    map: str | None = None
    timestamp: str | None = None  # ISO8601; if omitted, server time is used


class AnalyticsBatch(BaseModel):
    events: list[AnalyticsEvent]


@router.post("/analytics/events", status_code=204)
async def ingest_events(
    batch: AnalyticsBatch,
    request: Request,
    x_host_id: str = Header(..., alias="X-Host-Id"),
    x_agent_key: str = Header(..., alias="X-Agent-Key"),
) -> None:
    db = request.app.state.db
    host = await db.get_host_by_agent_key(x_host_id, x_agent_key)
    if not host:
        raise HTTPException(status_code=401, detail="Invalid host credentials")

    for ev in batch.events:
        await db.write_analytics_event(
            host_id=x_host_id,
            event_type=ev.event_type,
            instance_id=ev.instance_id,
            player_name=ev.player_name,
            mission_name=ev.mission_name,
            map=ev.map,
            timestamp=ev.timestamp,
        )

        # Publish player join/leave to the event bus for real-time notifications
        if ev.event_type in ("player_join", "player_leave"):
            event_type = (
                "player.joined" if ev.event_type == "player_join" else "player.left"
            )
            request.app.state.event_bus.publish(
                Event(
                    type=event_type,
                    data={
                        "playerName": ev.player_name,
                        "instanceId": ev.instance_id,
                        "missionName": ev.mission_name,
                    },
                    instance_id=ev.instance_id,
                    host_id=x_host_id,
                )
            )

    if batch.events:
        logger.debug("[analytics] %s wrote %d event(s)", x_host_id, len(batch.events))


@router.get(
    "/analytics/events",
    response_model=list[dict[str, Any]],
    dependencies=[Depends(require_api_key)],
)
async def query_events(
    request: Request,
    host_id: str | None = None,
    instance_id: str | None = None,
    event_type: str | None = None,
    since: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    db = request.app.state.db
    return await db.list_analytics_events(
        host_id=host_id,
        instance_id=instance_id,
        event_type=event_type,
        since=since,
        limit=min(limit, 2000),
    )
