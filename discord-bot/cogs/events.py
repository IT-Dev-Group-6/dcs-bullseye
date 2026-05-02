"""
EventsCog — background SSE subscriber.

Connects to the orchestrator's /api/v1/events/stream endpoint and posts
notifications to Discord when significant events occur:

  instance.status_changed  → posts when an instance goes up or down
  job.failed               → posts when a job fails (useful for externally
                             triggered actions not initiated via bot commands)

The connection reconnects automatically with exponential backoff on failure.
Notifications go to EVENTS_CHANNEL_ID if set, otherwise BOT_CHANNEL_ID.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

import discord
import httpx
from discord.ext import commands

if TYPE_CHECKING:
    from config import BotConfig

log = logging.getLogger(__name__)

_SUBSCRIBE_TYPES = "instance.status_changed,job.failed,player.joined,player.left"
_MIN_BACKOFF = 1.0
_MAX_BACKOFF = 60.0

_JOIN_SPAM_WINDOW = 60.0  # seconds
_JOIN_SPAM_THRESHOLD = 5  # joins per window before consolidating

_CRASH_LOOP_WINDOW = 600.0  # seconds — sliding window for crash detection
_CRASH_LOOP_THRESHOLD = 3  # crashes within the window triggers an alert

_STATUS_COLOURS: dict[str, int] = {
    "running": 0x2ECC71,
    "stopped": 0xE74C3C,
    "error": 0xE74C3C,
    "starting": 0xE67E22,
    "stopping": 0xE67E22,
}
_COLOUR_UNKNOWN = 0x95A5A6


def _status_colour(status: str) -> int:
    return _STATUS_COLOURS.get(status.lower(), _COLOUR_UNKNOWN)


class EventsCog(commands.Cog):
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self._task: asyncio.Task | None = None
        # crash loop detection: instance_id → list of crash timestamps (monotonic)
        self._crash_times: dict[str, list[float]] = {}
        self._crash_loop_alerted: set[str] = set()
        # player join spam tracking: instance_id → list of join timestamps (monotonic)
        self._recent_joins: dict[str, list[float]] = {}

    async def cog_load(self) -> None:
        self._task = asyncio.create_task(self._sse_loop())

    async def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------ #
    # Channel resolution                                                   #
    # ------------------------------------------------------------------ #

    def _notify_channel(self) -> discord.TextChannel | None:
        channel_id = self.config.events_channel_id or self.config.bot_channel_id
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)  # type: ignore[attr-defined]
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    # ------------------------------------------------------------------ #
    # SSE loop with reconnect                                              #
    # ------------------------------------------------------------------ #

    async def _sse_loop(self) -> None:
        backoff = _MIN_BACKOFF
        while True:
            try:
                await self._connect_and_stream()
                backoff = _MIN_BACKOFF  # clean disconnect — reset backoff
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("SSE stream lost (%s), reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _connect_and_stream(self) -> None:
        url = (
            f"{self.config.orchestrator_url}/api/v1/events/stream"
            f"?types={_SUBSCRIBE_TYPES}"
        )
        headers: dict[str, str] = {}
        if self.config.orchestrator_api_key:
            headers["X-API-Key"] = self.config.orchestrator_api_key

        log.info("Connecting to SSE stream: %s", url)
        async with httpx.AsyncClient(timeout=None) as http:
            async with http.stream("GET", url, headers=headers) as resp:
                if not resp.is_success:
                    raise RuntimeError(f"SSE stream returned HTTP {resp.status_code}")
                log.info("SSE stream connected")
                buffer: dict[str, str] = {}
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        buffer["event"] = line[6:].strip()
                    elif line.startswith("data:"):
                        buffer["data"] = line[5:].strip()
                    elif line.startswith("id:"):
                        buffer["id"] = line[3:].strip()
                    elif line == "" and "data" in buffer:
                        await self._handle_event(buffer)
                        buffer = {}
                    # comment lines (": ...") are heartbeats — ignore

    # ------------------------------------------------------------------ #
    # Event handler                                                        #
    # ------------------------------------------------------------------ #

    async def _handle_event(self, raw: dict[str, str]) -> None:
        event_type = raw.get("event", "")
        try:
            data = json.loads(raw.get("data", "{}"))
        except json.JSONDecodeError:
            return

        channel = self._notify_channel()
        if channel is None:
            return

        try:
            if event_type == "instance.status_changed":
                await self._on_status_changed(channel, data)
            elif event_type == "job.failed":
                await self._on_job_failed(channel, data)
            elif event_type == "player.joined":
                await self._on_player_joined(channel, data)
            elif event_type == "player.left":
                await self._on_player_left(channel, data)
        except Exception as exc:
            log.warning("Failed to post event notification: %s", exc)

    async def _on_status_changed(
        self, channel: discord.TextChannel, data: dict
    ) -> None:
        instance_name = data.get("name") or data.get("instanceId", "Unknown")
        instance_id = data.get("instanceId", instance_name)
        status = data.get("status", "unknown")
        prev = data.get("previousStatus", "?")

        # Only notify for meaningful transitions (avoid unknown→unknown noise)
        if status == prev:
            return

        # Suppress stopped→starting: this is just keepalive kicking in and
        # the DcsCog already sends its own "Auto-starting" message for it.
        if prev == "stopped" and status == "starting":
            return

        # Crash loop detection
        if status in ("stopped", "error") and prev in (
            "running",
            "starting",
            "stopping",
        ):
            now = time.monotonic()
            times = self._crash_times.get(instance_id, [])
            times.append(now)
            # prune timestamps outside the sliding window
            times = [t for t in times if now - t <= _CRASH_LOOP_WINDOW]
            self._crash_times[instance_id] = times

            if (
                len(times) >= _CRASH_LOOP_THRESHOLD
                and instance_id not in self._crash_loop_alerted
            ):
                self._crash_loop_alerted.add(instance_id)
                await self._on_crash_loop(channel, instance_name, len(times))

        # When an instance recovers, reset crash/alert state and keepalive cooldown
        if status == "running":
            now = time.monotonic()
            times = self._crash_times.get(instance_id, [])
            times = [t for t in times if now - t <= _CRASH_LOOP_WINDOW]
            self._crash_times[instance_id] = times
            if len(times) < _CRASH_LOOP_THRESHOLD:
                self._crash_loop_alerted.discard(instance_id)
            # Allow keepalive to attempt a restart immediately next time if needed
            for cog in self.bot.cogs.values():  # type: ignore[attr-defined]
                if hasattr(cog, "keepalive_clear"):
                    cog.keepalive_clear(instance_id)
                    break

        embed = discord.Embed(
            title="Instance status changed",
            description=f"**{instance_name}**: `{prev}` → `{status}`",
            colour=_status_colour(status),
        )
        await channel.send(embed=embed)

    async def _on_crash_loop(
        self, channel: discord.TextChannel, instance_name: str, crash_count: int
    ) -> None:
        embed = discord.Embed(
            title="Crash loop detected",
            description=(
                f"**{instance_name}** has crashed **{crash_count} times** "
                f"in the last {int(_CRASH_LOOP_WINDOW // 60)} minutes.\n"
                "The server may be stuck in a restart loop."
            ),
            colour=0xFF0000,
        )
        embed.add_field(
            name="What to do",
            value="Use `/dcs status` to check the server, or `/dcs logs` to see recent output.",
            inline=False,
        )
        await channel.send(embed=embed)

    async def _on_job_failed(self, channel: discord.TextChannel, data: dict) -> None:
        instance_id = data.get("instanceId", "?")
        action = data.get("action", "?")
        error_msg = ""
        if err := data.get("error"):
            error_msg = err.get("message", "") if isinstance(err, dict) else str(err)

        embed = discord.Embed(
            title="Job failed",
            description=f"`{action}` on `{instance_id}` failed",
            colour=0xE74C3C,
        )
        if error_msg:
            embed.add_field(name="Error", value=error_msg, inline=False)
        await channel.send(embed=embed)

    async def _on_player_joined(self, channel: discord.TextChannel, data: dict) -> None:
        player_name = data.get("playerName", "Unknown")
        instance_id = data.get("instanceId", "Unknown")
        instance_name = instance_id

        now = time.monotonic()
        times = self._recent_joins.get(instance_id, [])
        # prune timestamps outside the sliding window
        times = [t for t in times if now - t <= _JOIN_SPAM_WINDOW]
        times.append(now)
        self._recent_joins[instance_id] = times

        count = len(times)
        if count < _JOIN_SPAM_THRESHOLD:
            await channel.send(f"🟢 **{player_name}** joined **{instance_name}**")
        elif count == _JOIN_SPAM_THRESHOLD:
            await channel.send(
                f"🟢 **{count}** players joined **{instance_name}** in the last {int(_JOIN_SPAM_WINDOW)}s"
            )
        # if count > threshold, suppress (already notified)

    async def _on_player_left(self, channel: discord.TextChannel, data: dict) -> None:
        player_name = data.get("playerName", "Unknown")
        instance_id = data.get("instanceId", "Unknown")
        instance_name = instance_id

        await channel.send(f"🔴 **{player_name}** left **{instance_name}**")
