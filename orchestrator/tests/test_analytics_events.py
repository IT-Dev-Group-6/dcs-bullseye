
import pytest
from fastapi.testclient import TestClient

def test_ingest_player_events_publishes_to_bus(client: TestClient, host_id: str, instance_id: str):
    payload = {
        "events": [
            {
                "instance_id": instance_id,
                "event_type": "player_join",
                "player_name": "Test Player",
                "timestamp": "2026-05-01T12:00:00Z"
            },
            {
                "instance_id": instance_id,
                "event_type": "player_leave",
                "player_name": "Test Player",
                "timestamp": "2026-05-01T12:05:00Z"
            }
        ]
    }
    headers = {
        "X-Host-Id": host_id,
        "X-Agent-Key": "agent-key"
    }
    resp = client.post("/api/v1/analytics/events", headers=headers, json=payload)
    assert resp.status_code == 204

    # Verify event bus history
    # The event bus is in app.state.event_bus
    event_bus = client.app.state.event_bus
    recent = event_bus.recent(limit=10)
    
    types = [e.type for e in recent]
    assert "player.joined" in types
    assert "player.left" in types

    join_event = next(e for e in recent if e.type == "player.joined")
    assert join_event.data["playerName"] == "Test Player"
    assert join_event.instance_id == instance_id
    assert join_event.host_id == host_id
