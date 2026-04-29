from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from .conftest import HEADERS


def test_bench_inject_rejects_path_traversal(
    client: TestClient, missions_dir: Path
) -> None:
    client.app.state.config.active_missions_dir = str(missions_dir)

    resp = client.post(
        "/agent/v1/bench/inject",
        headers=HEADERS,
        json={"mission": "../escape.miz"},
    )

    assert resp.status_code == 400
    assert "path separators" in resp.json()["detail"].lower()


def test_bench_collect_rejects_absolute_path(
    client: TestClient, missions_dir: Path
) -> None:
    client.app.state.config.active_missions_dir = str(missions_dir)
    client.app.state.config.orchestrator_url = "https://orchestrator.invalid"
    client.app.state.config.host_id = "host-1"

    resp = client.post(
        "/agent/v1/bench/collect",
        headers=HEADERS,
        json={
            "mission": str(missions_dir / "escape.miz"),
            "service_name": "DCS-test",
        },
    )

    assert resp.status_code == 400
    assert "path separators" in resp.json()["detail"].lower()


def test_bench_inject_accepts_safe_filename(
    client: TestClient, missions_dir: Path, monkeypatch
) -> None:
    mission = missions_dir / "goonfront.miz"
    mission.write_bytes(b"original")
    client.app.state.config.active_missions_dir = str(missions_dir)

    async def fake_run(cmd: list[str], timeout: float = 90.0) -> str:
        output_path = Path(cmd[5])
        output_path.write_bytes(b"patched")
        return "ok"

    monkeypatch.setattr("agent.api.routes.bench._run", fake_run)

    resp = client.post(
        "/agent/v1/bench/inject",
        headers=HEADERS,
        json={"mission": "goonfront.miz"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "injected"}
    assert mission.read_bytes() == b"patched"
