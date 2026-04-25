"""
Bench scheduler — runs queued bench jobs during the configured UTC time window.

Picks the oldest pending item from bench_queue, orchestrates the full run:
  1. Upload .miz to agent active_missions_dir
  2. mission_load on the target instance
  3. Wait 60s for DCS to settle + duration_s for the bench run
  4. Stop DCS
  5. Call agent POST /bench/collect (runs afterburner record + push)
  6. Delete the .miz from the agent
  7. Mark the queue item done (or failed)

The manual trigger endpoint (POST /bench/queue/{id}/run) calls run_item() directly,
bypassing the window check.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import FastAPI

from .agent_client import AgentClient, AgentError
from .config import OrchestratorConfig

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 60.0
_SETTLE_DELAY = 60.0  # seconds between mission_load and starting the bench timer


def _in_window(start_utc: str, end_utc: str) -> bool:
    now = datetime.now(timezone.utc)
    now_hm = now.hour * 60 + now.minute
    sh, sm = (int(x) for x in start_utc.split(":"))
    eh, em = (int(x) for x in end_utc.split(":"))
    return (sh * 60 + sm) <= now_hm < (eh * 60 + em)


class BenchScheduler:
    def __init__(self, app: FastAPI, config: OrchestratorConfig) -> None:
        self._app = app
        self._config = config
        self._running: bool = False

    async def run(self) -> None:
        logger.info(
            "[bench/sched] started — window %s–%s UTC",
            self._config.bench_window_start_utc,
            self._config.bench_window_end_utc,
        )
        while True:
            await asyncio.sleep(_POLL_INTERVAL)
            if not _in_window(
                self._config.bench_window_start_utc,
                self._config.bench_window_end_utc,
            ):
                continue
            if self._running:
                continue
            db = self._app.state.db
            item = await db.dequeue_next_pending()
            if item is None:
                continue
            asyncio.create_task(self.run_item(item["id"]))

    async def run_item(self, item_id: str) -> None:
        if self._running:
            logger.warning("[bench/sched] run_item called while already running — skipping %s", item_id)
            return

        self._running = True
        db = self._app.state.db

        item = await db.get_queue_item(item_id)
        if item is None or item["status"] != "pending":
            logger.warning("[bench/sched] item %s not found or not pending", item_id)
            self._running = False
            return

        logger.info("[bench/sched] starting run for %s (%s)", item["miz_filename"], item_id)
        now = datetime.now(timezone.utc).isoformat()
        await db.update_queue_status(item_id, "running", started_at=now)

        try:
            run_id = await self._execute(item)
            completed = datetime.now(timezone.utc).isoformat()
            await db.update_queue_status(item_id, "done", completed_at=completed, run_id=run_id)
            logger.info("[bench/sched] %s complete — run_id=%s", item_id, run_id)
        except Exception as exc:
            completed = datetime.now(timezone.utc).isoformat()
            await db.update_queue_status(item_id, "failed", completed_at=completed, error=str(exc))
            logger.error("[bench/sched] %s failed: %s", item_id, exc)
        finally:
            self._running = False

    async def _execute(self, item: dict) -> str:
        db = self._app.state.db
        host = await db.get_host(item["host_id"])
        if not host:
            raise RuntimeError(f"Host not found: {item['host_id']}")

        agent_base = host["agent_url"].rstrip("/") + "/agent/v1"
        miz_filename = item["miz_filename"]
        service_name = item["instance_id"]
        duration_s = item["duration_s"]

        # Re-read miz_data from DB (stripped from item dict by _queue_row_to_dict)
        raw = await db._get_row(
            "SELECT miz_data FROM bench_queue WHERE id = ?", (item["id"],)
        )
        if raw is None:
            raise RuntimeError("Queue item disappeared")
        miz_data: bytes = raw["miz_data"]

        async with AgentClient(agent_base, host["agent_api_key"]) as client:
            # 1. Upload .miz to active_missions_dir
            logger.info("[bench/sched] uploading %s to agent", miz_filename)
            await client.upload_active_mission(miz_filename, miz_data, timeout=120.0)

            # 2. Start CPU monitor (non-fatal if not configured)
            monitor_started = False
            try:
                await client.bench_monitor_start(service_name)
                monitor_started = True
                logger.info("[bench/sched] CPU monitor started for %s", service_name)
            except AgentError as exc:
                logger.warning("[bench/sched] monitor start skipped (%s) — no CPU data", exc)

            # 3. Load mission (stops DCS, loads, starts)
            logger.info("[bench/sched] loading mission on %s", service_name)
            await client.trigger_action(service_name, "mission_load", {"mission": miz_filename})

            # 4. Wait for DCS to settle, then run for duration
            logger.info("[bench/sched] waiting %ds settle + %ds bench", int(_SETTLE_DELAY), duration_s)
            await asyncio.sleep(_SETTLE_DELAY)
            await asyncio.sleep(duration_s)

            # 5. Stop DCS
            logger.info("[bench/sched] stopping %s", service_name)
            await client.trigger_action(service_name, "stop")
            await asyncio.sleep(10)  # brief pause so log flush completes

            # 6. Stop CPU monitor
            if monitor_started:
                try:
                    await client.bench_monitor_stop()
                    logger.info("[bench/sched] CPU monitor stopped")
                except AgentError as exc:
                    logger.warning("[bench/sched] monitor stop failed (non-fatal): %s", exc)

            # 7. Collect bench data via afterburner
            logger.info("[bench/sched] collecting bench data")
            result = await client.bench_collect(miz_filename, service_name, timeout=120.0)
            run_id: str = result.get("run_id", "")

            # 8. Clean up the .miz
            logger.info("[bench/sched] deleting %s from agent", miz_filename)
            try:
                await client.delete_active_mission(miz_filename)
            except AgentError as exc:
                logger.warning("[bench/sched] delete failed (non-fatal): %s", exc)

        return run_id
