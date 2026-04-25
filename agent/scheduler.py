"""
Mission scheduler for the DCS Agent.

Runs as a background task: reads per-instance schedules every TICK_INTERVAL
seconds and fires start/stop/restart/mission_load actions automatically.

Schedule config is stored in SCHEDULES_FILE (separate from the main agent
config so it can be updated via API without restarting the agent).

Supported schedule options (all optional, mix-and-match):
  timezone            - IANA tz string, default "UTC"
  open_time           - "HH:MM"  — start the server at this time
  close_time          - "HH:MM"  — stop the server at this time (only if 0 players)
  days                - ["mon","tue",...] — days the window applies (default: all)
  idle_restart_minutes - restart after this many minutes with 0 players
  mission_playlist    - ["file1.miz", "file2.miz", ...]
  rotate_real_minutes - rotate to next playlist mission every N wall-clock minutes
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import AgentConfig, InstanceConfig
from .controller import DcsController

logger = logging.getLogger(__name__)

SCHEDULES_FILE = Path(r"C:\ProgramData\DCSAgent\schedules.json")
TICK_INTERVAL = 60.0  # seconds between scheduler ticks
_DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# ---------------------------------------------------------------------------
# Schedule storage helpers
# ---------------------------------------------------------------------------


def _load_schedules() -> dict:
    if SCHEDULES_FILE.exists():
        try:
            return json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[scheduler] failed to load schedules: %s", exc)
    return {}


def save_schedule(instance_name: str, schedule: dict) -> None:
    schedules = _load_schedules()
    schedules[instance_name] = schedule
    SCHEDULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCHEDULES_FILE.write_text(json.dumps(schedules, indent=4), encoding="utf-8")


def delete_schedule(instance_name: str) -> bool:
    schedules = _load_schedules()
    if instance_name not in schedules:
        return False
    del schedules[instance_name]
    SCHEDULES_FILE.write_text(json.dumps(schedules, indent=4), encoding="utf-8")
    return True


def get_schedule(instance_name: str) -> dict | None:
    return _load_schedules().get(instance_name)


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------


async def run_scheduler(config: AgentConfig, ctrl: DcsController) -> None:
    """Main scheduler loop. Runs until cancelled."""
    logger.info("[scheduler] started — %d instance(s)", len(config.instances))

    # Per-instance mutable state (not persisted — resets on agent restart)
    state: dict[str, dict] = {
        inst.name: {
            "last_player_time": None,  # wall-clock timestamp of last non-zero player count
            "mission_idx": 0,  # current position in playlist
            "current_mission": None,  # mission name last seen (for tracking rotations)
            "mission_wall_start": None,  # wall-clock time when current mission was first seen
        }
        for inst in config.instances
    }

    loop = asyncio.get_running_loop()
    await asyncio.sleep(30)  # let DCS instances settle before first check

    while True:
        schedules = _load_schedules()
        for inst in config.instances:
            sched = schedules.get(inst.name)
            if not sched:
                continue
            try:
                await _tick_instance(inst, sched, state[inst.name], ctrl, loop)
            except Exception as exc:
                logger.warning("[scheduler] tick error for %s: %s", inst.name, exc)
        await asyncio.sleep(TICK_INTERVAL)


async def _tick_instance(
    inst: InstanceConfig,
    sched: dict,
    state: dict,
    ctrl: DcsController,
    loop: asyncio.AbstractEventLoop,
) -> None:
    info = await loop.run_in_executor(None, ctrl.runtime_info, inst)

    status = info.get("status", "stopped")
    player_count = info.get("player_count", 0) or 0
    mission_name = info.get("mission_name") or ""
    running = status == "running"

    # Track last time players were present
    if player_count > 0:
        state["last_player_time"] = datetime.now(timezone.utc).timestamp()

    # Track mission start time (wall clock) — used for rotation timer
    if mission_name and mission_name != state["current_mission"]:
        state["current_mission"] = mission_name
        state["mission_wall_start"] = datetime.now(timezone.utc).timestamp()
        # Sync playlist index when mission changes externally
        playlist = sched.get("mission_playlist") or []
        if mission_name in playlist:
            state["mission_idx"] = playlist.index(mission_name)

    # --- Idle restart -------------------------------------------------------
    idle_minutes = sched.get("idle_restart_minutes")
    if idle_minutes and running and player_count == 0:
        last = state["last_player_time"]
        if last is not None:
            idle_secs = datetime.now(timezone.utc).timestamp() - last
            if idle_secs >= idle_minutes * 60:
                logger.info(
                    "[scheduler] idle restart %s (idle %.0f min)",
                    inst.name,
                    idle_secs / 60,
                )
                state["last_player_time"] = None
                state["mission_wall_start"] = None
                await loop.run_in_executor(None, ctrl.restart, inst)
                return

    # --- Mission rotation ---------------------------------------------------
    playlist: list[str] = sched.get("mission_playlist") or []
    rotate_minutes = sched.get("rotate_real_minutes")
    if (
        rotate_minutes
        and playlist
        and running
        and state["mission_wall_start"] is not None
    ):
        elapsed = datetime.now(timezone.utc).timestamp() - state["mission_wall_start"]
        if elapsed >= rotate_minutes * 60:
            next_idx = (state["mission_idx"] + 1) % len(playlist)
            next_mission = playlist[next_idx]
            logger.info(
                "[scheduler] rotating %s → %s (elapsed %.0f min)",
                inst.name,
                next_mission,
                elapsed / 60,
            )
            state["mission_idx"] = next_idx
            state["mission_wall_start"] = (
                None  # reset; will be set when new mission detected
            )
            await loop.run_in_executor(None, ctrl.mission_load, inst, next_mission)
            return

    # --- Open/close time window --------------------------------------------
    open_time_str: str | None = sched.get("open_time")
    close_time_str: str | None = sched.get("close_time")
    if not open_time_str and not close_time_str:
        return

    tz_name = sched.get("timezone", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning(
            "[scheduler] unknown timezone %r for %s — using UTC", tz_name, inst.name
        )
        tz = ZoneInfo("UTC")

    now = datetime.now(tz)
    today = _DAY_NAMES[now.weekday()]
    days = sched.get("days")
    if days and today not in days:
        # Not an active day — close the server if empty
        if running and player_count == 0:
            logger.info("[scheduler] closing %s (inactive day: %s)", inst.name, today)
            await loop.run_in_executor(None, ctrl.stop, inst)
        return

    now_t = now.time().replace(second=0, microsecond=0)

    def _parse(t_str: str) -> dt_time:
        h, m = t_str.split(":")
        return dt_time(int(h), int(m))

    if open_time_str and close_time_str:
        open_t = _parse(open_time_str)
        close_t = _parse(close_time_str)
        # Handles overnight windows (e.g. 18:00 → 02:00)
        if open_t <= close_t:
            in_window = open_t <= now_t < close_t
        else:
            in_window = now_t >= open_t or now_t < close_t

        if in_window and not running:
            logger.info("[scheduler] opening %s at %s", inst.name, now_t)
            await loop.run_in_executor(None, ctrl.start, inst)
        elif not in_window and running and player_count == 0:
            logger.info("[scheduler] closing %s at %s", inst.name, now_t)
            await loop.run_in_executor(None, ctrl.stop, inst)

    elif open_time_str:
        open_t = _parse(open_time_str)
        if not running and now_t >= open_t:
            logger.info("[scheduler] opening %s at %s", inst.name, now_t)
            await loop.run_in_executor(None, ctrl.start, inst)

    elif close_time_str:
        close_t = _parse(close_time_str)
        if running and player_count == 0 and now_t >= close_t:
            logger.info("[scheduler] closing %s at %s", inst.name, now_t)
            await loop.run_in_executor(None, ctrl.stop, inst)
