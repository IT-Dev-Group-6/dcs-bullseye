"""
POST /agent/v1/bench/monitor/start — start bench_monitor.py for a given instance
POST /agent/v1/bench/monitor/stop  — stop the running monitor
POST /agent/v1/bench/collect       — run afterburner bench record + push after a run

Collect flow (called by orchestrator scheduler):
  1. afterburner bench record <miz_path> --log <log_path> [--cpu <bench_csv_path>]
  2. afterburner bench push <orchestrator_url> --host-id <host_id> --key <api_key>
  Returns {"run_id": "brun_xxx"} parsed from push stdout.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ...config import InstanceConfig

logger = logging.getLogger(__name__)
router = APIRouter()

_RUN_ID_RE = re.compile(r"brun_[0-9a-f]+")


class MonitorRequest(BaseModel):
    service_name: str  # e.g. "DCS-TexasBBQ"


class CollectRequest(BaseModel):
    mission: str  # bare filename, e.g. "mymission.miz"
    service_name: str  # instance service_name, e.g. "DCS-TexasBBQ"


def _find_instance(config, service_name: str) -> InstanceConfig | None:
    for inst in config.instances:
        if inst.service_name.lower() == service_name.lower():
            return inst
    return None


async def _run(cmd: list[str], timeout: float = 90.0) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(cmd)}")
    output = stdout.decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed (exit {proc.returncode}): {output[:500]}")
    return output


@router.post("/bench/monitor/start")
async def start_monitor(payload: MonitorRequest, request: Request) -> dict[str, str]:
    config = request.app.state.config

    inst = _find_instance(config, payload.service_name)
    if inst is None:
        raise HTTPException(
            status_code=404, detail=f"Instance not found: {payload.service_name}"
        )
    if not inst.bench_monitor_script:
        raise HTTPException(
            status_code=422,
            detail="bench_monitor_script not configured for this instance",
        )
    if not inst.bench_csv_path:
        raise HTTPException(
            status_code=422, detail="bench_csv_path not configured for this instance"
        )

    monitor_state = request.app.state.bench_monitor
    if monitor_state.get("proc") is not None:
        raise HTTPException(status_code=409, detail="Monitor already running")

    cmd = [
        sys.executable,
        inst.bench_monitor_script,
        "--server",
        inst.saved_games_key,
        "--out",
        inst.bench_csv_path,
    ]
    logger.info("[bench/monitor] starting: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    monitor_state["proc"] = proc
    monitor_state["service_name"] = payload.service_name
    return {"status": "started", "pid": str(proc.pid)}


@router.post("/bench/monitor/stop")
async def stop_monitor(request: Request) -> dict[str, str]:
    monitor_state = request.app.state.bench_monitor
    proc: asyncio.subprocess.Process | None = monitor_state.get("proc")
    if proc is None:
        return {"status": "not_running"}

    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
    except ProcessLookupError:
        pass

    monitor_state["proc"] = None
    monitor_state["service_name"] = None
    logger.info("[bench/monitor] stopped")
    return {"status": "stopped"}


@router.post("/bench/collect")
async def collect_bench(payload: CollectRequest, request: Request) -> dict[str, str]:
    config = request.app.state.config

    inst = _find_instance(config, payload.service_name)
    if inst is None:
        raise HTTPException(
            status_code=404, detail=f"Instance not found: {payload.service_name}"
        )

    if not config.orchestrator_url:
        raise HTTPException(status_code=503, detail="orchestrator_url not configured")
    if not config.host_id:
        raise HTTPException(status_code=503, detail="host_id not configured")

    active_dir = config.active_missions_dir
    if not active_dir:
        raise HTTPException(
            status_code=503, detail="active_missions_dir not configured"
        )

    miz_path = str(Path(active_dir) / payload.mission)
    afterburner = config.afterburner_bin

    # Build record command
    record_cmd = [afterburner, "bench", "record", miz_path, "--log", inst.log_path]
    if inst.bench_csv_path and Path(inst.bench_csv_path).exists():
        record_cmd += ["--cpu", inst.bench_csv_path]

    logger.info("[bench/collect] recording: %s", " ".join(record_cmd))
    try:
        record_out = await _run(record_cmd)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"bench record failed: {exc}")
    logger.info("[bench/collect] record output: %s", record_out.strip())

    # Build push command
    push_cmd = [
        afterburner,
        "bench",
        "push",
        config.orchestrator_url,
        "--host-id",
        config.host_id,
        "--key",
        config.api_key,
    ]

    logger.info("[bench/collect] pushing to %s", config.orchestrator_url)
    try:
        push_out = await _run(push_cmd)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"bench push failed: {exc}")
    logger.info("[bench/collect] push output: %s", push_out.strip())

    match = _RUN_ID_RE.search(push_out)
    run_id = match.group(0) if match else ""
    return {"run_id": run_id}
