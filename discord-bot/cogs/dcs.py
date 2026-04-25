from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
from datetime import datetime, time as dt_time, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from orchestrator_client import OrchestratorClient, OrchestratorError

if TYPE_CHECKING:
    from config import BotConfig

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

_EASTERN = ZoneInfo("America/New_York")
_DAILY_RESTART_TIME = dt_time(hour=5, minute=0, tzinfo=_EASTERN)  # 05:00 ET
_MISSION_TIME_RESTART_THRESHOLD = 48 * 3600  # 48 hours in seconds

_STATUS_COLOURS: dict[str, int] = {
    "running": 0x2ECC71,
    "stopped": 0xE74C3C,
    "error": 0xE74C3C,
    "starting": 0xE67E22,
    "stopping": 0xE67E22,
    "degraded": 0xE67E22,
}
_COLOUR_UNKNOWN = 0x95A5A6

_POLL_INTERVAL = 2.0
_POLL_TIMEOUT = 120.0
_TERMINAL_STATES = {"succeeded", "failed"}


def _status_colour(status: str) -> int:
    return _STATUS_COLOURS.get(status.lower(), _COLOUR_UNKNOWN)


# ------------------------------------------------------------------ #
# Player registration (Discord ID → DCS pilot name)                   #
# ------------------------------------------------------------------ #

_REG_FILE = os.environ.get(
    "DCS_REGISTRATIONS_FILE",
    os.path.join(os.path.dirname(__file__), "..", "registrations.json"),
)


def _load_registrations() -> dict[str, str]:
    try:
        with open(_REG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_registrations(regs: dict[str, str]) -> None:
    with open(_REG_FILE, "w", encoding="utf-8") as f:
        json.dump(regs, f, indent=2)


async def _check_port(ip: str, port: int, timeout: float = 3.0) -> bool:
    """Return True if the TCP port accepts a connection."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if h else f"{d}d"


def _pair_sessions(events: list[dict]) -> list[tuple[str, str, float]]:
    """Pair player_join/player_leave events into completed sessions.

    Returns list of (player_name, instance_id, duration_seconds) for each
    matched join→leave pair. Unmatched joins (player still online) are skipped.
    Sessions longer than 24 h are capped and excluded as likely data gaps.
    """
    joins_by_key: dict[tuple, list[datetime]] = {}
    leaves_by_key: dict[tuple, list[datetime]] = {}

    for e in events:
        if not e.get("player_name"):
            continue
        key = (e["player_name"], e.get("instance_id") or "")
        ts_str = e.get("timestamp") or ""
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            continue
        if e["event_type"] == "player_join":
            joins_by_key.setdefault(key, []).append(ts)
        elif e["event_type"] == "player_leave":
            leaves_by_key.setdefault(key, []).append(ts)

    sessions: list[tuple[str, str, float]] = []
    for key, join_times in joins_by_key.items():
        player_name, instance_id = key
        leave_times = sorted(leaves_by_key.get(key, []))
        used: set[int] = set()
        for jt in sorted(join_times):
            for li, lt in enumerate(leave_times):
                if li not in used and lt > jt:
                    duration = (lt - jt).total_seconds()
                    if duration <= 86400:
                        sessions.append((player_name, instance_id, duration))
                    used.add(li)
                    break
    return sessions


def _fmt_game_time(seconds: int) -> str:
    """Format in-game mission clock as H:MM:SS."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _mission_time_str(runtime: dict) -> str | None:
    """Return in-game mission time string, or None if not available from hook."""
    t = runtime.get("missionTimeSeconds")
    if t is None:
        return None
    return _fmt_game_time(t)


def _instances_summary_embed(instances: list[dict]) -> discord.Embed:
    """Single embed showing all instances as compact inline fields."""
    embed = discord.Embed(title="DCS Server Status", colour=0x3498DB)
    for inst in instances:
        runtime = inst.get("runtime") or {}
        status = runtime.get("status", "unknown")
        running = status == "running"
        dot = (
            "🟢"
            if status == "running"
            else "🔴"
            if status in ("stopped", "error")
            else "🟡"
            if status in ("starting", "stopping", "degraded")
            else "⚫"
        )
        lines = [f"{dot} {status.title()}"]

        if uptime := runtime.get("uptimeSeconds"):
            lines.append(f"Server Up: {_fmt_duration(uptime)}")

        mission_time = _mission_time_str(runtime)
        lines.append(f"Mission Time: {mission_time or '—'}")

        mission_name = runtime.get("missionName")
        lines.append(f"Mission: {mission_name or '—'}")

        map_name = runtime.get("map")
        if map_name:
            lines.append(f"Map: {map_name}")

        pc = runtime.get("playerCount")
        if pc is not None:
            players: list[str] = runtime.get("players") or []
            player_str = f"{pc} online"
            if players:
                player_str += f"\n{', '.join(players[:5])}"
            lines.append(f"Players: {player_str}")
        elif running:
            lines.append("Players: —")

        embed.add_field(
            name=inst.get("name", "Unknown"),
            value="\n".join(lines),
            inline=True,
        )
    return embed


def _instance_embed(instance: dict) -> discord.Embed:
    runtime = instance.get("runtime") or {}
    status = runtime.get("status", "unknown")
    running = status == "running"

    embed = discord.Embed(
        title=instance.get("name", instance.get("id", "Unknown")),
        colour=_status_colour(status),
    )
    embed.add_field(name="Status", value=status.title(), inline=True)

    if uptime := runtime.get("uptimeSeconds"):
        embed.add_field(name="Server Up", value=_fmt_duration(uptime), inline=True)

    mission_time = _mission_time_str(runtime)
    embed.add_field(name="Mission Time", value=mission_time or "—", inline=True)

    mission_name = runtime.get("missionName")
    embed.add_field(name="Mission", value=mission_name or "—", inline=False)

    map_name = runtime.get("map")
    if map_name or running:
        embed.add_field(name="Map", value=map_name or "—", inline=True)

    player_count = runtime.get("playerCount")
    if player_count is not None:
        players: list[str] = runtime.get("players") or []
        player_str = f"{player_count} online"
        if players:
            player_str += "\n" + ", ".join(players[:20])
        embed.add_field(name="Players", value=player_str, inline=True)
    elif running:
        embed.add_field(name="Players", value="—", inline=True)

    return embed


def _job_embed(job: dict, title: str = "Job result") -> discord.Embed:
    job_status = job.get("status", "unknown")
    colour = (
        0x2ECC71
        if job_status == "succeeded"
        else 0xE74C3C
        if job_status == "failed"
        else _COLOUR_UNKNOWN
    )
    embed = discord.Embed(title=title, colour=colour)
    embed.add_field(name="Job ID", value=job.get("id", "—"), inline=True)
    embed.add_field(name="Status", value=job_status, inline=True)
    if err := job.get("error"):
        embed.add_field(name="Error", value=err, inline=False)
    return embed


# ------------------------------------------------------------------ #
# Confirm view (two-step button confirmation)                           #
# ------------------------------------------------------------------ #


class _ConfirmView(discord.ui.View):
    """Ephemeral yes/no confirmation. Check `view.confirmed` after `await view.wait()`."""

    def __init__(self, label: str = "Confirm", timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)
        self.confirmed = False
        self._confirm_btn.label = label

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def _confirm_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def _cancel_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.confirmed = False
        self.stop()
        await interaction.response.defer()


# ------------------------------------------------------------------ #
# Cog                                                                  #
# ------------------------------------------------------------------ #

_KEEPALIVE_COOLDOWN = 20 * 60  # seconds between restart attempts per instance
_KEEPALIVE_MAX_ATTEMPTS = 3

_UPDATE_PHASE_COLOURS: dict[str, int] = {
    "starting": 0xE67E22,
    "stopping": 0xE67E22,
    "updating": 0x3498DB,
    "restarting": 0xE67E22,
    "complete": 0x2ECC71,
    "failed": 0xE74C3C,
}
_UPDATE_PHASE_ICONS: dict[str, str] = {
    "starting": "🔄",
    "stopping": "🛑",
    "updating": "⬇️",
    "restarting": "🔁",
    "complete": "✅",
    "failed": "❌",
}


def _update_phase_embed(host: str, phase: str, message: str) -> discord.Embed:
    colour = _UPDATE_PHASE_COLOURS.get(phase, 0x95A5A6)
    icon = _UPDATE_PHASE_ICONS.get(phase, "🔄")
    embed = discord.Embed(
        title=f"{icon} DCS Update — `{host}`",
        description=message,
        colour=colour,
    )
    embed.set_footer(text=f"Phase: {phase}")
    return embed


class DcsCog(commands.Cog):
    def __init__(
        self, config: BotConfig, client: OrchestratorClient, bot: commands.Bot
    ) -> None:
        self.config = config
        self.client = client
        self._bot = bot
        self._status_message_id: int | None = None
        # keepalive cooldown: instance_id → monotonic time of last start attempt
        self._keepalive_last_attempt: dict[str, float] = {}
        # keepalive attempt counter and exhausted set
        self._keepalive_attempts: dict[str, int] = {}
        self._keepalive_exhausted: set[str] = set()
        self.dcs: app_commands.Group = app_commands.Group(
            name="dcs", description="Manage DCS World server instances"
        )
        self._register_commands()

    # ---------------------------------------------------------------- #
    # Lifecycle                                                          #
    # ---------------------------------------------------------------- #

    async def cog_load(self) -> None:
        self._bot.tree.add_command(self.dcs)
        if self.config.status_channel_id:
            self._status_loop.start()
        self._restart_check_loop.start()
        self._keepalive_loop.start()

    async def cog_unload(self) -> None:
        self._bot.tree.remove_command(self.dcs.name)
        if self._status_loop.is_running():
            self._status_loop.cancel()
        if self._restart_check_loop.is_running():
            self._restart_check_loop.cancel()
        if self._keepalive_loop.is_running():
            self._keepalive_loop.cancel()

    # ---------------------------------------------------------------- #
    # Status channel helpers                                            #
    # ---------------------------------------------------------------- #

    def _status_channel(self) -> discord.TextChannel | None:
        if not self.config.status_channel_id:
            return None
        ch = self._bot.get_channel(self.config.status_channel_id)
        return ch if isinstance(ch, discord.TextChannel) else None

    async def _get_or_create_status_message(
        self, channel: discord.TextChannel
    ) -> discord.Message | None:
        # Try cached ID first
        if self._status_message_id:
            try:
                return await channel.fetch_message(self._status_message_id)
            except discord.NotFound:
                self._status_message_id = None

        # Search pinned messages for an existing status embed from this bot
        try:
            for msg in await channel.pins():
                if (
                    msg.author.id == self._bot.user.id
                    and msg.embeds
                    and msg.embeds[0].title == "DCS Server Status"
                ):
                    self._status_message_id = msg.id
                    return msg
        except Exception:
            pass

        # Nothing found — post a new one and pin it
        try:
            embed = discord.Embed(
                title="DCS Server Status", description="Loading…", colour=0x3498DB
            )
            msg = await channel.send(embed=embed)
            log.info("Status message created: id=%s", msg.id)
            await msg.pin()
            log.info("Status message pinned: id=%s", msg.id)
            self._status_message_id = msg.id
            return msg
        except Exception as exc:
            log.warning("Could not create status message: %s", exc)
            return None

    async def _push_status_embed(self) -> None:
        channel = self._status_channel()
        if not channel:
            return
        try:
            instances = await self.client.list_instances()
        except Exception as exc:
            log.warning("Status loop: fetch failed: %s", exc)
            return
        embed = _instances_summary_embed(instances)

        # External servers — TCP port-check only
        if self.config.external_servers:
            results = await asyncio.gather(
                *[
                    _check_port(s["ip"], s["port"])
                    for s in self.config.external_servers
                ],
                return_exceptions=True,
            )
            for srv, online in zip(self.config.external_servers, results):
                if isinstance(online, Exception):
                    online = False
                dot = "🟢" if online else "🔴"
                embed.add_field(
                    name=srv["name"],
                    value=f"{dot} {'Online' if online else 'Offline'}",
                    inline=True,
                )

        embed.set_footer(
            text="Updated "
            + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            + " · auto-restarts daily at 05:00 ET after 48 h mission time"
        )
        msg = await self._get_or_create_status_message(channel)
        if not msg:
            log.warning("Status loop: could not get/create status message")
            return
        try:
            await msg.edit(embed=embed)
            log.info("Status embed updated: msg_id=%s", msg.id)
        except Exception as exc:
            log.warning("Status loop: edit failed: %s", exc)
            self._status_message_id = None  # force re-lookup next time

    async def _run_restart_check(self) -> None:
        """Restart any instance with ≥48 h mission time and 0 players."""
        channel = self._status_channel()
        try:
            instances = await self.client.list_instances()
        except Exception as exc:
            log.warning("Restart check: fetch failed: %s", exc)
            return

        for inst in instances:
            runtime = inst.get("runtime") or {}
            name = inst.get("name", inst.get("id", "?"))
            mission_secs = runtime.get("missionTimeSeconds")
            player_count = runtime.get("playerCount") or 0

            if mission_secs is None or mission_secs < _MISSION_TIME_RESTART_THRESHOLD:
                continue

            time_str = _fmt_game_time(mission_secs)
            if player_count > 0:
                note = (
                    f"⏰ Auto-restart skipped for **{name}** — "
                    f"{time_str} mission time but {player_count} player(s) online."
                )
                log.info(note)
                if channel:
                    await channel.send(note)
                continue

            log.info("Auto-restart: %s (mission time %s, 0 players)", name, time_str)
            if channel:
                await channel.send(
                    f"🔄 Auto-restarting **{name}** — mission time {time_str}, 0 players online."
                )
            try:
                await self.client.trigger_action(
                    inst["id"], "restart", actor_id="system"
                )
            except Exception as exc:
                log.error("Auto-restart failed for %s: %s", name, exc)
                if channel:
                    await channel.send(f"❌ Auto-restart failed for **{name}**: {exc}")

    def keepalive_clear(self, instance_id: str) -> None:
        """Clear the keepalive state for an instance (call when it reaches 'running')."""
        self._keepalive_last_attempt.pop(instance_id, None)
        self._keepalive_attempts.pop(instance_id, None)
        self._keepalive_exhausted.discard(instance_id)

    async def _run_keepalive_check(self) -> None:
        """Start any non-excluded instance that is stopped."""
        exclude = {n.lower() for n in self.config.auto_restart_exclude}
        channel = self._status_channel()
        now = time.monotonic()
        try:
            instances = await self.client.list_instances()
        except Exception as exc:
            log.warning("Keepalive check: fetch failed: %s", exc)
            return

        for inst in instances:
            instance_id = inst.get("id", "")
            name = inst.get("name", instance_id or "?")
            if name.lower() in exclude:
                continue
            runtime = inst.get("runtime") or {}
            if runtime.get("status") != "stopped":
                continue

            if instance_id in self._keepalive_exhausted:
                continue

            last = self._keepalive_last_attempt.get(instance_id)
            if last is not None and now - last < _KEEPALIVE_COOLDOWN:
                remaining = int(_KEEPALIVE_COOLDOWN - (now - last))
                log.debug(
                    "Keepalive: %s in cooldown (%ds remaining), skipping",
                    name,
                    remaining,
                )
                continue

            attempt = self._keepalive_attempts.get(instance_id, 0) + 1
            self._keepalive_attempts[instance_id] = attempt
            self._keepalive_last_attempt[instance_id] = now

            if attempt > _KEEPALIVE_MAX_ATTEMPTS:
                self._keepalive_exhausted.add(instance_id)
                log.warning(
                    "Keepalive: giving up on %s after %d attempts",
                    name,
                    _KEEPALIVE_MAX_ATTEMPTS,
                )
                if channel:
                    await channel.send(
                        f"⛔ Auto-start failed for **{name}** after {_KEEPALIVE_MAX_ATTEMPTS} attempts — "
                        f"manual intervention required."
                    )
                continue

            log.info(
                "Keepalive: starting stopped instance %s (attempt %d/%d)",
                name,
                attempt,
                _KEEPALIVE_MAX_ATTEMPTS,
            )
            if channel:
                await channel.send(
                    f"🔁 Auto-starting **{name}** — instance was stopped (attempt {attempt}/{_KEEPALIVE_MAX_ATTEMPTS})."
                )
            try:
                await self.client.trigger_action(
                    instance_id, "start", actor_id="system"
                )
            except Exception as exc:
                log.error("Keepalive: start failed for %s: %s", name, exc)
                if channel:
                    await channel.send(f"❌ Auto-start failed for **{name}**: {exc}")

    # ---------------------------------------------------------------- #
    # Background tasks                                                  #
    # ---------------------------------------------------------------- #

    @tasks.loop(minutes=5)
    async def _status_loop(self) -> None:
        await self._push_status_embed()

    @_status_loop.before_loop
    async def _before_status_loop(self) -> None:
        await self._bot.wait_until_ready()

    @tasks.loop(time=_DAILY_RESTART_TIME)
    async def _restart_check_loop(self) -> None:
        await self._run_restart_check()

    @_restart_check_loop.before_loop
    async def _before_restart_check(self) -> None:
        await self._bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def _keepalive_loop(self) -> None:
        await self._run_keepalive_check()

    @_keepalive_loop.before_loop
    async def _before_keepalive(self) -> None:
        await self._bot.wait_until_ready()

    # ---------------------------------------------------------------- #
    # Guards & helpers (closures over self)                             #
    # ---------------------------------------------------------------- #

    def _register_commands(self) -> None:
        config = self.config
        client = self.client

        # ---- channel guard ----------------------------------------- #

        async def _check_channel(interaction: discord.Interaction) -> bool:
            if (
                config.bot_channel_id
                and interaction.channel_id != config.bot_channel_id
            ):
                await interaction.response.send_message(
                    f"This command only works in <#{config.bot_channel_id}>.",
                    ephemeral=True,
                )
                return False
            return True

        # ---- operator role guard ------------------------------------ #

        async def _require_operator(interaction: discord.Interaction) -> bool:
            if not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message(
                    "Could not verify your roles. Try again from a server channel.",
                    ephemeral=True,
                )
                return False
            if config.operator_role not in {r.name for r in interaction.user.roles}:
                await interaction.response.send_message(
                    f"You need the **{config.operator_role}** role to use this command.",
                    ephemeral=True,
                )
                return False
            return True

        # ---- admin role guard --------------------------------------- #

        async def _require_admin(interaction: discord.Interaction) -> bool:
            if not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message(
                    "Could not verify your roles. Try again from a server channel.",
                    ephemeral=True,
                )
                return False
            roles = {r.name for r in interaction.user.roles}
            if config.admin_role not in roles:
                await interaction.response.send_message(
                    f"You need the **{config.admin_role}** role to use this command.",
                    ephemeral=True,
                )
                return False
            return True

        # ---- instance autocomplete ---------------------------------- #

        async def _instance_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> list[app_commands.Choice[str]]:
            try:
                instances = await client.list_instances()
            except Exception:
                return []
            low = current.lower()
            return [
                app_commands.Choice(name=inst["name"], value=inst["name"])
                for inst in instances
                if low in inst.get("name", "").lower()
            ][:25]

        # ---- host autocomplete -------------------------------------- #

        async def _host_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> list[app_commands.Choice[str]]:
            try:
                hosts = await client.list_hosts()
            except Exception:
                return []
            low = current.lower()
            return [
                app_commands.Choice(name=h["name"], value=h["name"])
                for h in hosts
                if low in h.get("name", "").lower()
            ][:25]

        # ---- mission file autocomplete (instance missions_dir) ------ #

        async def _mission_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> list[app_commands.Choice[str]]:
            instance_id: str = interaction.namespace.instance or ""
            if not instance_id:
                return []
            try:
                items = await client.list_missions(instance_id)
            except Exception:
                return []
            low = current.lower()
            return [
                app_commands.Choice(name=name, value=name)
                for name in items
                if low in name.lower()
            ][:25]

        # ---- active missions autocomplete (shared library) ---------- #

        async def _active_mission_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> list[app_commands.Choice[str]]:
            """Autocomplete across all hosts' Active Missions folders, sorted by play count.
            Value format: '<host_id>::<filename>'"""
            try:
                hosts = await client.list_hosts()
            except Exception:
                return []

            # Build play-count map from analytics
            play_counts: dict[str, int] = {}
            try:
                events = await client.get_analytics_events(limit=1000)
                for e in events:
                    if e.get("event_type") == "mission_start" and e.get("mission_name"):
                        stem = e["mission_name"]
                        play_counts[stem] = play_counts.get(stem, 0) + 1
            except Exception:
                pass

            low = current.lower()
            seen: set[str] = set()
            candidates: list[tuple[int, str, str]] = []  # (play_count, label, value)
            for host in hosts:
                try:
                    items = await client.list_active_missions(host["id"])
                except Exception:
                    continue
                for m in items:
                    filename = m.get("relative_path", m.get("name", ""))
                    if not filename:
                        continue
                    if low and low not in filename.lower():
                        continue
                    if filename in seen:
                        continue
                    seen.add(filename)
                    stem = (
                        filename[:-4] if filename.lower().endswith(".miz") else filename
                    )
                    count = play_counts.get(stem, 0)
                    label = filename
                    if count:
                        label += f" ({count}\u00d7)"
                    candidates.append((count, label, f"{host['id']}::{filename}"))

            candidates.sort(key=lambda x: -x[0])
            choices = [
                app_commands.Choice(name=c[1][:100], value=c[2])
                for c in candidates[:24]
            ]
            if len(candidates) > 24:
                choices.append(
                    app_commands.Choice(
                        name=f"... {len(candidates) - 24} more — type to search",
                        value="__hint__",
                    )
                )
            return choices

        # ---- job poller -------------------------------------------- #

        async def _poll_job(job_id: str) -> dict:
            elapsed = 0.0
            while elapsed < _POLL_TIMEOUT:
                job = await client.get_job(job_id)
                if job.get("status") in _TERMINAL_STATES:
                    return job
                await asyncio.sleep(_POLL_INTERVAL)
                elapsed += _POLL_INTERVAL
            return await client.get_job(job_id)

        # ---------------------------------------------------------------- #
        # /dcs status [instance]                                            #
        # ---------------------------------------------------------------- #

        @self.dcs.command(
            name="status", description="Show status of one or all instances"
        )
        @app_commands.describe(instance="Instance to check (leave blank for all)")
        @app_commands.autocomplete(instance=_instance_autocomplete)
        async def cmd_status(
            interaction: discord.Interaction, instance: str | None = None
        ) -> None:
            if not await _check_channel(interaction):
                return
            await interaction.response.defer()
            try:
                if instance:
                    inst_ref = await client.get_instance(instance)
                    runtime = await client.get_instance_status(instance)
                    merged = {**inst_ref, "runtime": runtime}
                    await interaction.followup.send(embed=_instance_embed(merged))
                else:
                    instances = await client.list_instances()
                    if not instances:
                        await interaction.followup.send("No instances registered.")
                        return
                    await interaction.followup.send(
                        embed=_instances_summary_embed(instances)
                    )
            except OrchestratorError as exc:
                await interaction.followup.send(
                    f"Orchestrator error: {exc.detail}", ephemeral=True
                )

        # ---------------------------------------------------------------- #
        # /dcs start <instance>                                             #
        # ---------------------------------------------------------------- #

        @self.dcs.command(name="start", description="Start a DCS instance")
        @app_commands.describe(instance="Instance to start")
        @app_commands.autocomplete(instance=_instance_autocomplete)
        async def cmd_start(interaction: discord.Interaction, instance: str) -> None:
            if not await _check_channel(interaction):
                return
            if not await _require_operator(interaction):
                return
            await interaction.response.defer()
            try:
                job_ref = await client.trigger_action(
                    instance, "start", actor_id=str(interaction.user.id)
                )
                job = await _poll_job(job_ref["jobId"])
                if job.get("status") not in _TERMINAL_STATES:
                    await interaction.followup.send(
                        f"Job still running — check `/dcs jobs` (job `{job_ref['jobId']}`)"
                    )
                    return
                await interaction.followup.send(
                    embed=_job_embed(job, f"Start: {instance}")
                )
            except OrchestratorError as exc:
                await interaction.followup.send(
                    f"Orchestrator error: {exc.detail}", ephemeral=True
                )

        # ---------------------------------------------------------------- #
        # /dcs stop <instance>                                              #
        # ---------------------------------------------------------------- #

        @self.dcs.command(name="stop", description="Stop a DCS instance")
        @app_commands.describe(instance="Instance to stop")
        @app_commands.autocomplete(instance=_instance_autocomplete)
        async def cmd_stop(interaction: discord.Interaction, instance: str) -> None:
            if not await _check_channel(interaction):
                return
            if not await _require_operator(interaction):
                return
            await interaction.response.defer()
            try:
                job_ref = await client.trigger_action(
                    instance, "stop", actor_id=str(interaction.user.id)
                )
                job = await _poll_job(job_ref["jobId"])
                if job.get("status") not in _TERMINAL_STATES:
                    await interaction.followup.send(
                        f"Job still running — check `/dcs jobs` (job `{job_ref['jobId']}`)"
                    )
                    return
                await interaction.followup.send(
                    embed=_job_embed(job, f"Stop: {instance}")
                )
            except OrchestratorError as exc:
                await interaction.followup.send(
                    f"Orchestrator error: {exc.detail}", ephemeral=True
                )

        # ---------------------------------------------------------------- #
        # /dcs restart <instance>                                           #
        # ---------------------------------------------------------------- #

        @self.dcs.command(name="restart", description="Restart a DCS instance")
        @app_commands.describe(instance="Instance to restart")
        @app_commands.autocomplete(instance=_instance_autocomplete)
        async def cmd_restart(interaction: discord.Interaction, instance: str) -> None:
            if not await _check_channel(interaction):
                return
            if not await _require_operator(interaction):
                return
            await interaction.response.defer()
            try:
                job_ref = await client.trigger_action(
                    instance, "restart", actor_id=str(interaction.user.id)
                )
                job = await _poll_job(job_ref["jobId"])
                if job.get("status") not in _TERMINAL_STATES:
                    await interaction.followup.send(
                        f"Job still running — check `/dcs jobs` (job `{job_ref['jobId']}`)"
                    )
                    return
                await interaction.followup.send(
                    embed=_job_embed(job, f"Restart: {instance}")
                )
            except OrchestratorError as exc:
                await interaction.followup.send(
                    f"Orchestrator error: {exc.detail}", ephemeral=True
                )

        # ---------------------------------------------------------------- #
        # /dcs logs <instance>                                              #
        # ---------------------------------------------------------------- #

        @self.dcs.command(
            name="logs", description="Fetch a log bundle for a DCS instance"
        )
        @app_commands.describe(instance="Instance to fetch logs from")
        @app_commands.autocomplete(instance=_instance_autocomplete)
        async def cmd_logs(interaction: discord.Interaction, instance: str) -> None:
            if not await _check_channel(interaction):
                return
            await interaction.response.defer()
            try:
                job_ref = await client.trigger_action(
                    instance, "logs_bundle", actor_id=str(interaction.user.id)
                )
                job = await _poll_job(job_ref["jobId"])
                if job.get("status") not in _TERMINAL_STATES:
                    await interaction.followup.send(
                        f"Job still running — check `/dcs jobs` (job `{job_ref['jobId']}`)"
                    )
                    return
                if job.get("status") == "failed":
                    await interaction.followup.send(
                        embed=_job_embed(job, f"Logs: {instance}")
                    )
                    return
                result = job.get("result") or {}
                lines: list[str] = result.get("lines", [])
                scripting: list[str] = result.get("scripting_errors", [])
                dcs_errs: list[str] = result.get("dcs_errors", [])

                # Build output: errors first (most useful), then raw tail
                parts: list[str] = []
                if scripting:
                    parts.append(
                        f"=== SCRIPTING / LUA ERRORS ({len(scripting)} lines) ==="
                    )
                    parts.extend(scripting)
                    parts.append("")
                if dcs_errs:
                    parts.append(f"=== DCS ENGINE ERRORS ({len(dcs_errs)} lines) ===")
                    parts.extend(dcs_errs)
                    parts.append("")
                if not scripting and not dcs_errs:
                    parts.append("=== NO ERRORS DETECTED ===")
                    parts.append("")
                parts.append("=== RECENT LOG (last 200 lines) ===")
                parts.extend(lines)
                content = "\n".join(parts)

                fp = io.BytesIO(content.encode())
                label = f"Logs for `{instance}`"
                flags: list[str] = []
                if scripting:
                    flags.append(f"⚠️ {len(scripting)} scripting line(s)")
                if dcs_errs:
                    flags.append(f"🔴 {len(dcs_errs)} engine error(s)")
                if flags:
                    label += " — " + ", ".join(flags)
                await interaction.followup.send(
                    label + ":",
                    file=discord.File(fp, filename=f"{instance}-logs.txt"),
                )
            except OrchestratorError as exc:
                await interaction.followup.send(
                    f"Orchestrator error: {exc.detail}", ephemeral=True
                )

        # ---------------------------------------------------------------- #
        # /dcs hosts                                                        #
        # ---------------------------------------------------------------- #

        @self.dcs.command(
            name="hosts", description="List registered orchestrator hosts"
        )
        async def cmd_hosts(interaction: discord.Interaction) -> None:
            if not await _check_channel(interaction):
                return
            await interaction.response.defer()
            try:
                host_list = await client.list_hosts()
                if not host_list:
                    await interaction.followup.send("No hosts registered.")
                    return
                embed = discord.Embed(title="Registered Hosts", colour=0x3498DB)
                for host in host_list[:25]:
                    embed.add_field(
                        name=host.get("name", host.get("id", "Unknown")),
                        value=(
                            f"ID: `{host.get('id', '—')}`\n"
                            f"URL: `{host.get('agentUrl', '—')}`\n"
                            f"Online: {host.get('online', '?')}"
                        ),
                        inline=True,
                    )
                await interaction.followup.send(embed=embed)
            except OrchestratorError as exc:
                await interaction.followup.send(
                    f"Orchestrator error: {exc.detail}", ephemeral=True
                )

        # ---------------------------------------------------------------- #
        # /dcs jobs [status]                                                #
        # ---------------------------------------------------------------- #

        # ---------------------------------------------------------------- #
        # /dcs mission <instance> <mission_file> [source]                   #
        # ---------------------------------------------------------------- #

        @self.dcs.command(
            name="mission", description="Load a mission file and restart the server"
        )
        @app_commands.describe(
            instance="Instance to load the mission on",
            mission_file="Mission file (searches Active Missions on all hosts — type to filter)",
        )
        @app_commands.autocomplete(
            instance=_instance_autocomplete, mission_file=_active_mission_autocomplete
        )
        async def cmd_mission(
            interaction: discord.Interaction,
            instance: str,
            mission_file: str,
        ) -> None:
            if not await _check_channel(interaction):
                return
            if not await _require_operator(interaction):
                return

            if mission_file == "__hint__":
                await interaction.response.send_message(
                    "Type part of the mission name to filter, then select from the results.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer()

            try:
                # Parse autocomplete value: "<source_host_id>::<filename>"
                if "::" in mission_file:
                    source_host_id, filename = mission_file.split("::", 1)
                else:
                    # Bare filename typed manually — pass straight through
                    source_host_id = None
                    filename = mission_file

                # Determine target host
                inst_ref = await client.get_instance(instance)
                target_host_id = inst_ref.get("hostId")

                # Cross-host transfer: download from source, upload to target
                if (
                    source_host_id
                    and target_host_id
                    and source_host_id != target_host_id
                ):
                    await interaction.followup.send(
                        f"Fetching `{filename}` from another host — please wait...",
                        ephemeral=True,
                    )
                    data = await client.download_active_mission(
                        source_host_id, filename
                    )
                    await client.upload_active_mission(target_host_id, filename, data)

                job_ref = await client.trigger_action(
                    instance,
                    "mission_load",
                    body={"mission": filename},
                    actor_id=str(interaction.user.id),
                )
                job = await _poll_job(job_ref["jobId"])
                if job.get("status") not in _TERMINAL_STATES:
                    await interaction.followup.send(
                        f"Job still running — check `/dcs jobs` (job `{job_ref['jobId']}`)"
                    )
                    return
                embed = _job_embed(job, f"Mission Load: {instance}")
                if job.get("status") == "succeeded":
                    loaded = (job.get("result") or {}).get("mission", filename)
                    embed.add_field(name="Mission", value=f"`{loaded}`", inline=False)
                await interaction.followup.send(embed=embed)
            except OrchestratorError as exc:
                await interaction.followup.send(
                    f"Orchestrator error: {exc.detail}", ephemeral=True
                )

        # ---------------------------------------------------------------- #
        # /dcs upload <file>                                                 #
        # ---------------------------------------------------------------- #

        @self.dcs.command(
            name="upload",
            description="Upload a .miz file to the Active Missions folder",
        )
        @app_commands.describe(
            file="The .miz file to upload",
            rename="Auto-rename if a file with that name already exists (e.g. mission_2.miz)",
        )
        async def cmd_upload(
            interaction: discord.Interaction,
            file: discord.Attachment,
            rename: bool = False,
        ) -> None:
            if not await _check_channel(interaction):
                return
            if not await _require_operator(interaction):
                return
            if not file.filename.lower().endswith(".miz"):
                await interaction.response.send_message(
                    "Only `.miz` files can be uploaded.", ephemeral=True
                )
                return
            await interaction.response.defer()
            try:
                hosts = await client.list_hosts()
                if not hosts:
                    await interaction.followup.send(
                        "No hosts registered.", ephemeral=True
                    )
                    return
                data = await file.read()
                upload_name = file.filename
                renamed = False
                try:
                    result = await client.upload_active_mission(
                        hosts[0]["id"], upload_name, data
                    )
                except OrchestratorError as exc:
                    if exc.status_code == 409 and rename:
                        # Auto-rename: try filename_2.miz, filename_3.miz, ...
                        stem = file.filename[:-4]  # strip .miz
                        for n in range(2, 11):
                            upload_name = f"{stem}_{n}.miz"
                            try:
                                result = await client.upload_active_mission(
                                    hosts[0]["id"], upload_name, data
                                )
                                renamed = True
                                break
                            except OrchestratorError as inner:
                                if inner.status_code != 409:
                                    raise
                        else:
                            await interaction.followup.send(
                                "Could not find a free filename after 10 attempts.",
                                ephemeral=True,
                            )
                            return
                    elif exc.status_code == 409:
                        await interaction.followup.send(
                            f"`{file.filename}` already exists in Active Missions.\n"
                            f"Use `/dcs upload rename:True` to upload with an auto-generated name instead.",
                            ephemeral=True,
                        )
                        return
                    else:
                        raise
                embed = discord.Embed(title=f"Uploaded: {upload_name}", colour=0x2ECC71)
                if renamed:
                    embed.description = (
                        f"⚠️ Renamed from `{file.filename}` (original already exists)"
                    )
                embed.add_field(name="Size", value=f"{len(data):,} bytes", inline=True)
                if saved_as := result.get("path"):
                    embed.add_field(
                        name="Saved as", value=f"`{saved_as}`", inline=False
                    )
                await interaction.followup.send(embed=embed)
            except OrchestratorError as exc:
                await interaction.followup.send(
                    f"Upload failed: {exc.detail}", ephemeral=True
                )

        # ---------------------------------------------------------------- #
        # /dcs download <filename>                                          #
        # ---------------------------------------------------------------- #

        @self.dcs.command(
            name="download",
            description="Download a .miz file from the Active Missions folder",
        )
        @app_commands.describe(filename="Mission file to download")
        async def cmd_download(
            interaction: discord.Interaction,
            filename: str,
        ) -> None:
            if not await _check_channel(interaction):
                return
            if not await _require_operator(interaction):
                return
            await interaction.response.defer(ephemeral=True)
            try:
                hosts = await client.list_hosts()
                if not hosts:
                    await interaction.followup.send(
                        "No hosts registered.", ephemeral=True
                    )
                    return
                data = await client.download_active_mission(hosts[0]["id"], filename)
            except OrchestratorError as exc:
                await interaction.followup.send(
                    f"Download failed: {exc.detail}", ephemeral=True
                )
                return
            _25MB = 25 * 1024 * 1024
            if len(data) > _25MB:
                await interaction.followup.send(
                    f"`{filename}` is {len(data) / (1024 * 1024):.1f} MB — too large for Discord (25 MB limit).",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                file=discord.File(io.BytesIO(data), filename=filename),
                ephemeral=True,
            )

        @cmd_download.autocomplete("filename")
        async def download_filename_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> list[app_commands.Choice[str]]:
            try:
                hosts = await client.list_hosts()
                if not hosts:
                    return []
                items = await client.list_active_missions(hosts[0]["id"])
            except Exception:
                return []
            low = current.lower()
            return [
                app_commands.Choice(name=m["relative_path"], value=m["relative_path"])
                for m in items
                if low in m.get("relative_path", "").lower()
            ][:25]

        # ---------------------------------------------------------------- #
        # /dcs clear                                                        #
        # ---------------------------------------------------------------- #

        # ---------------------------------------------------------------- #
        # /dcs invite [host_name] [expires_in_hours]                        #
        # ---------------------------------------------------------------- #

        @self.dcs.command(
            name="invite", description="Generate a community host invite code"
        )
        @app_commands.describe(
            host_name="Optional name to pre-assign to the community host",
            expires_in_hours="Hours until the invite expires (1–168, default: 24)",
        )
        async def cmd_invite(
            interaction: discord.Interaction,
            host_name: str = "",
            expires_in_hours: int = 24,
        ) -> None:
            if not await _check_channel(interaction):
                return
            if not await _require_operator(interaction):
                return
            if expires_in_hours < 1 or expires_in_hours > 168:
                await interaction.response.send_message(
                    "expires_in_hours must be between 1 and 168 (max 7 days).",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True)
            try:
                invite = await client.create_invite(
                    host_name=host_name, expires_in_hours=expires_in_hours
                )
                code = invite.get("code", "—")
                base_url = config.installer_base_url
                sha256_arg = (
                    f" -AgentZipSha256 {config.agent_zip_sha256}"
                    if config.agent_zip_sha256
                    else ""
                )
                install_cmd = (
                    f'$f="$env:TEMP\\install-agent.ps1"; '
                    f"iwr -UseBasicParsing {base_url}/install/install.ps1 -OutFile $f; "
                    f"powershell -ExecutionPolicy Bypass -File $f"
                    f" -InviteCode {code}"
                    f" -OrchestratorUrl {base_url}"
                    f"{sha256_arg}"
                )
                embed = discord.Embed(
                    title="Community Host Invite Code",
                    colour=0x9B59B6,
                )
                if host_name:
                    embed.add_field(
                        name="Pre-assigned name", value=host_name, inline=True
                    )
                embed.add_field(
                    name="Expires in", value=f"{expires_in_hours}h", inline=True
                )
                embed.add_field(
                    name="Run this in PowerShell (as Administrator)",
                    value=f"```powershell\n{install_cmd}\n```",
                    inline=False,
                )
                embed.set_footer(
                    text="Single-use. The invite code is embedded in the command above."
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
            except OrchestratorError as exc:
                await interaction.followup.send(f"Error: {exc.detail}", ephemeral=True)

        # ---------------------------------------------------------------- #
        # /dcs clear                                                        #
        # ---------------------------------------------------------------- #

        @self.dcs.command(
            name="clear", description="Delete bot messages from this channel"
        )
        async def cmd_clear(interaction: discord.Interaction) -> None:
            if not await _check_channel(interaction):
                return
            if not await _require_operator(interaction):
                return
            await interaction.response.defer(ephemeral=True)
            channel = interaction.channel
            if not isinstance(channel, discord.TextChannel):
                await interaction.followup.send(
                    "Can only clear text channels.", ephemeral=True
                )
                return
            bot_id = self._bot.user.id
            try:
                deleted = await channel.purge(
                    limit=200, check=lambda m: m.author.id == bot_id
                )
                count = len(deleted)
            except discord.Forbidden:
                count = 0
                async for msg in channel.history(limit=200):
                    if msg.author.id == bot_id:
                        try:
                            await msg.delete()
                            count += 1
                        except discord.HTTPException:
                            pass
            await interaction.followup.send(
                f"Cleared {count} bot message(s).", ephemeral=True
            )

        # ---------------------------------------------------------------- #
        # /dcs delete <mission_file>                                        #
        # ---------------------------------------------------------------- #

        async def _active_mission_delete_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> list[app_commands.Choice[str]]:
            """Autocomplete from Active Missions root — returns filename as value."""
            try:
                hosts = await client.list_hosts()
                if not hosts:
                    return []
                host_id = hosts[0]["id"]
                items = await client.list_active_missions(host_id)
            except Exception:
                return []
            low = current.lower()
            return [
                app_commands.Choice(name=m["relative_path"], value=m["relative_path"])
                for m in items
                if low in m.get("relative_path", "").lower()
            ][:25]

        @self.dcs.command(
            name="delete",
            description="Delete a mission from the Active Missions folder (backed up first)",
        )
        @app_commands.describe(mission_file="Mission filename to delete")
        @app_commands.autocomplete(mission_file=_active_mission_delete_autocomplete)
        async def cmd_delete(
            interaction: discord.Interaction, mission_file: str
        ) -> None:
            if not await _check_channel(interaction):
                return
            if not await _require_operator(interaction):
                return

            view = _ConfirmView()
            await interaction.response.send_message(
                f"Delete **{mission_file}** from Active Missions? It will be moved to `Backup_Missions/`.",
                view=view,
                ephemeral=True,
            )
            await view.wait()
            if not view.confirmed:
                await interaction.edit_original_response(
                    content="Cancelled.", view=None
                )
                return

            await interaction.edit_original_response(content="Deleting…", view=None)
            try:
                hosts = await client.list_hosts()
                if not hosts:
                    await interaction.edit_original_response(
                        content="No hosts registered.", view=None
                    )
                    return
                result = await client.delete_active_mission(
                    hosts[0]["id"], mission_file
                )
                embed = discord.Embed(
                    title="Mission Deleted",
                    description=f"`{mission_file}` moved to `{result.get('backed_up_to', 'Backup_Missions/')}`.",
                    colour=0x2ECC71,
                )
                await interaction.edit_original_response(content=None, embed=embed)
            except OrchestratorError as exc:
                await interaction.edit_original_response(
                    content=f"Delete failed: {exc.detail}", embed=None
                )

        # ---------------------------------------------------------------- #
        # /dcs restartall                                                    #
        # ---------------------------------------------------------------- #

        @self.dcs.command(
            name="restartall", description="Restart every running DCS instance"
        )
        async def cmd_restartall(interaction: discord.Interaction) -> None:
            if not await _check_channel(interaction):
                return
            if not await _require_operator(interaction):
                return
            await interaction.response.defer()
            try:
                instances = await client.list_instances()
            except OrchestratorError as exc:
                await interaction.followup.send(
                    f"Orchestrator error: {exc.detail}", ephemeral=True
                )
                return

            if not instances:
                await interaction.followup.send("No instances registered.")
                return

            running = [
                i
                for i in instances
                if (i.get("runtime") or {}).get("status") == "running"
            ]
            if not running:
                await interaction.followup.send("No instances are currently running.")
                return

            names = ", ".join(i["name"] for i in running)
            await interaction.followup.send(
                f"Restarting {len(running)} instance(s): {names}…"
            )

            async def _do_restart(inst: dict) -> tuple[str, bool, str]:
                name = inst["name"]
                try:
                    job_ref = await client.trigger_action(
                        inst["id"], "restart", actor_id=str(interaction.user.id)
                    )
                    job = await _poll_job(job_ref["jobId"])
                    ok = job.get("status") == "succeeded"
                    msg = (
                        "✅"
                        if ok
                        else f"❌ {(job.get('error') or {}).get('message', 'failed')}"
                    )
                    return name, ok, msg
                except Exception as exc:
                    return name, False, f"❌ {exc}"

            results = await asyncio.gather(*[_do_restart(i) for i in running])
            lines = [f"{msg}  **{name}**" for name, _, msg in results]
            embed = discord.Embed(
                title=f"Restart All — {sum(1 for _, ok, _ in results if ok)}/{len(results)} succeeded",
                description="\n".join(lines),
                colour=0x2ECC71 if all(ok for _, ok, _ in results) else 0xE67E22,
            )
            try:
                await interaction.followup.send(embed=embed)
            except Exception:
                await interaction.channel.send(embed=embed)  # type: ignore[union-attr]

        # ---------------------------------------------------------------- #
        # /dcs password <instance> <new_password>                           #
        # ---------------------------------------------------------------- #

        @self.dcs.command(
            name="password",
            description="Change the multiplayer password for a DCS instance",
        )
        @app_commands.describe(
            instance="Instance to change the password on",
            new_password="New password (leave blank to clear)",
        )
        @app_commands.autocomplete(instance=_instance_autocomplete)
        async def cmd_password(
            interaction: discord.Interaction,
            instance: str,
            new_password: str = "",
        ) -> None:
            if not await _check_channel(interaction):
                return
            if not await _require_operator(interaction):
                return
            # Ephemeral defer so the password isn't shown in the channel
            await interaction.response.defer(ephemeral=True)
            try:
                job_ref = await client.trigger_action(
                    instance,
                    "set_password",
                    body={"password": new_password},
                    actor_id=str(interaction.user.id),
                )
                job = await _poll_job(job_ref["jobId"])
                if job.get("status") not in _TERMINAL_STATES:
                    await interaction.followup.send(
                        f"Job still running — check `/dcs jobs` (job `{job_ref['jobId']}`)",
                        ephemeral=True,
                    )
                    return
                embed = _job_embed(job, "Password Change")
                if job.get("status") == "succeeded":
                    embed.add_field(
                        name="Password",
                        value="(cleared)" if not new_password else "••••••••",
                        inline=True,
                    )
                await interaction.followup.send(embed=embed, ephemeral=True)
            except OrchestratorError as exc:
                await interaction.followup.send(
                    f"Orchestrator error: {exc.detail}", ephemeral=True
                )

        # ---------------------------------------------------------------- #
        # /dcs resetpersist <instance>                                       #
        # ---------------------------------------------------------------- #

        @self.dcs.command(
            name="resetpersist",
            description="Back up and clear persistence save files for an instance",
        )
        @app_commands.describe(instance="Instance to reset persistence for")
        @app_commands.autocomplete(instance=_instance_autocomplete)
        async def cmd_resetpersist(
            interaction: discord.Interaction, instance: str
        ) -> None:
            if not await _check_channel(interaction):
                return
            if not await _require_operator(interaction):
                return

            view = _ConfirmView(label="Confirm Reset Persistence")
            await interaction.response.send_message(
                f"Reset persistence for `{instance}`? Save files will be backed up before deletion.",
                view=view,
                ephemeral=True,
            )
            await view.wait()
            if not view.confirmed:
                await interaction.edit_original_response(
                    content="Cancelled.", view=None
                )
                return
            await interaction.edit_original_response(
                content="Resetting persistence…", view=None
            )

            try:
                job_ref = await client.trigger_action(
                    instance, "reset_persist", actor_id=str(interaction.user.id)
                )
                job = await _poll_job(job_ref["jobId"])
                if job.get("status") not in _TERMINAL_STATES:
                    await interaction.followup.send(
                        f"Job still running — check `/dcs jobs` (job `{job_ref['jobId']}`)"
                    )
                    return
                embed = _job_embed(job, f"Reset Persistence: {instance}")
                if job.get("status") == "succeeded":
                    result = job.get("result") or {}
                    embed.add_field(
                        name="Files backed up",
                        value=str(result.get("backed_up", 0)),
                        inline=True,
                    )
                    if bd := result.get("backup_dir"):
                        embed.add_field(
                            name="Backup folder", value=f"`{bd}`", inline=True
                        )
                await interaction.followup.send(embed=embed)
            except OrchestratorError as exc:
                await interaction.followup.send(
                    f"Orchestrator error: {exc.detail}", ephemeral=True
                )

        # ---------------------------------------------------------------- #
        # /dcs stats [server] [period]                                       #
        # ---------------------------------------------------------------- #

        @self.dcs.command(name="stats", description="Show player and mission analytics")
        @app_commands.describe(
            instance="Server to show stats for (leave blank for all)",
            period="Time period: 7d, 30d, or all time",
        )
        @app_commands.autocomplete(instance=_instance_autocomplete)
        @app_commands.choices(
            period=[
                app_commands.Choice(name="Last 7 days", value="7d"),
                app_commands.Choice(name="Last 30 days", value="30d"),
                app_commands.Choice(name="All time", value="all"),
            ]
        )
        async def cmd_stats(
            interaction: discord.Interaction,
            instance: str | None = None,
            period: str = "7d",
        ) -> None:
            if not await _check_channel(interaction):
                return
            await interaction.response.defer()

            from datetime import datetime, timezone, timedelta
            from collections import Counter

            if period == "7d":
                since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                period_label = "last 7 days"
            elif period == "30d":
                since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
                period_label = "last 30 days"
            else:
                since = None
                period_label = "all time"

            try:
                events = await client.get_analytics_events(
                    instance_id=instance, since=since, limit=2000
                )
            except OrchestratorError as exc:
                await interaction.followup.send(
                    f"Orchestrator error: {exc.detail}", ephemeral=True
                )
                return

            joins = [e for e in events if e["event_type"] == "player_join"]
            missions = [e for e in events if e["event_type"] == "mission_start"]

            player_counts = Counter(
                e["player_name"] for e in joins if e.get("player_name")
            )
            mission_counts = Counter(
                e["mission_name"] for e in joins if e.get("mission_name")
            )
            map_counts = Counter(e["map"] for e in joins if e.get("map"))

            title = "DCS Stats — {} ({})".format(
                instance or "All Servers", period_label
            )
            embed = discord.Embed(title=title, colour=0x3498DB)

            embed.add_field(name="Sessions", value=str(len(joins)), inline=True)
            embed.add_field(
                name="Unique Players", value=str(len(player_counts)), inline=True
            )
            embed.add_field(
                name="Missions Played", value=str(len(mission_counts)), inline=True
            )

            if player_counts:
                top_lines = []
                for i, (name, n) in enumerate(player_counts.most_common(5)):
                    suffix = "s" if n != 1 else ""
                    top_lines.append(
                        "{i}. {name} ({n} session{s})".format(
                            i=i + 1, name=name, n=n, s=suffix
                        )
                    )
                embed.add_field(
                    name="Top Players", value=chr(10).join(top_lines), inline=True
                )

            if mission_counts:
                top_m = []
                for i, (name, n) in enumerate(mission_counts.most_common(5)):
                    top_m.append(
                        "{i}. {name} ({n}x)".format(i=i + 1, name=name[:40], n=n)
                    )
                embed.add_field(
                    name="Top Missions", value=chr(10).join(top_m), inline=True
                )

            if map_counts:
                top_map = []
                for i, (name, n) in enumerate(map_counts.most_common(5)):
                    suffix = "s" if n != 1 else ""
                    top_map.append(
                        "{i}. {name} ({n} session{s})".format(
                            i=i + 1, name=name, n=n, s=suffix
                        )
                    )
                embed.add_field(
                    name="Top Maps", value=chr(10).join(top_map), inline=True
                )

            # Peak hours bar chart
            if joins:
                hour_counts: dict[int, int] = {}
                for e in joins:
                    ts = e.get("timestamp") or ""
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        hour_counts[dt.hour] = hour_counts.get(dt.hour, 0) + 1
                    except Exception:
                        pass
                if hour_counts:

                    def _fmt12(h: int) -> str:
                        suffix = "am" if h < 12 else "pm"
                        return f"{h % 12 or 12}{suffix}"

                    max_count = max(hour_counts.values())
                    bar_len = 8
                    peak_lines = []
                    for h in sorted(hour_counts, key=lambda x: -hour_counts[x])[:8]:
                        n = hour_counts[h]
                        filled = round(n / max_count * bar_len)
                        bar = "\u2588" * filled + "\u2591" * (bar_len - filled)
                        est = _fmt12((h - 5) % 24)
                        pst = _fmt12((h - 8) % 24)
                        peak_lines.append(f"{est} EST / {pst} PST  {bar} {n}")
                    embed.add_field(
                        name="Peak Hours", value=chr(10).join(peak_lines), inline=False
                    )

            # Session duration
            sessions = _pair_sessions(events)
            if sessions:
                durations = [d for _, _, d in sessions]
                avg_dur = sum(durations) / len(durations)
                embed.add_field(
                    name="Avg Session Length",
                    value=f"{_fmt_duration(avg_dur)} ({len(durations)} completed)",
                    inline=False,
                )

            if not joins and not missions:
                embed.description = "No data yet for this period."

            await interaction.followup.send(embed=embed)

        # ---------------------------------------------------------------- #
        # /dcs reboot                                                        #
        # ---------------------------------------------------------------- #

        @self.dcs.command(
            name="reboot", description="Reboot the Windows DCS host machine"
        )
        @app_commands.describe(host="Host machine to reboot")
        @app_commands.autocomplete(host=_host_autocomplete)
        async def cmd_reboot(interaction: discord.Interaction, host: str) -> None:
            if not await _check_channel(interaction):
                return
            if not await _require_admin(interaction):
                return

            view = _ConfirmView(label="Confirm Reboot")
            await interaction.response.send_message(
                f"⚠️ **This will reboot `{host}` and kick all its players.**\n"
                "The servers should come back up automatically after ~2 minutes.",
                view=view,
                ephemeral=True,
            )
            await view.wait()
            if not view.confirmed:
                await interaction.edit_original_response(
                    content="Reboot cancelled.", view=None
                )
                return

            await interaction.edit_original_response(
                content="Sending reboot command…", view=None
            )
            try:
                hosts = await client.list_hosts()
                matched = next((h for h in hosts if h["name"] == host), None)
                if not matched:
                    await interaction.edit_original_response(
                        content=f"Host `{host}` not found.", view=None
                    )
                    return
                await client.reboot_host(matched["id"])
            except OrchestratorError as exc:
                await interaction.edit_original_response(
                    content=f"Reboot failed: {exc.detail}", view=None
                )
                return

            # Announce in status channel
            status_ch = self._status_channel()
            if status_ch:
                await status_ch.send(
                    f"⚠️ `{host}` rebooting in 30 seconds — all its servers going offline. "
                    "They will restart automatically."
                )
            await interaction.edit_original_response(
                content="✅ Reboot command sent. Host will restart in ~30 seconds.",
                view=None,
            )

        # ---------------------------------------------------------------- #
        # /dcs update                                                        #
        # ---------------------------------------------------------------- #

        @self.dcs.command(
            name="update",
            description="Update DCS World on a host and restart its servers",
        )
        @app_commands.describe(host="Host machine to update")
        @app_commands.autocomplete(host=_host_autocomplete)
        async def cmd_update(interaction: discord.Interaction, host: str) -> None:
            if not await _check_channel(interaction):
                return
            if not await _require_admin(interaction):
                return

            view = _ConfirmView(label="Confirm Update")
            await interaction.response.send_message(
                f"⚠️ **This will stop all DCS servers on `{host}`, run the DCS updater, then restart.**\n"
                "The process takes **10–60 minutes**. The status embed will show servers coming back online.\n\n"
                "Proceed?",
                view=view,
                ephemeral=True,
            )
            await view.wait()
            if not view.confirmed:
                await interaction.edit_original_response(
                    content="Update cancelled.", view=None
                )
                return

            await interaction.edit_original_response(
                content="Triggering DCS update…", view=None
            )
            try:
                hosts = await client.list_hosts()
                matched = next((h for h in hosts if h["name"] == host), None)
                if not matched:
                    await interaction.edit_original_response(
                        content=f"Host `{host}` not found.", view=None
                    )
                    return
                await client.trigger_dcs_update(matched["id"])
            except OrchestratorError as exc:
                await interaction.edit_original_response(
                    content=f"Update failed to start: {exc.detail}", view=None
                )
                return

            # Post a live-updating embed in the status channel
            status_ch = self._status_channel()
            status_msg: discord.Message | None = None
            if status_ch:
                status_msg = await status_ch.send(
                    embed=_update_phase_embed(host, "starting", "Starting update...")
                )
            await interaction.edit_original_response(
                content="Update triggered. Watch the status channel for progress.",
                view=None,
            )

            # Background task: poll status and edit the embed until done
            async def _poll_update() -> None:
                _PHASE_LABELS = {
                    "starting": "Starting...",
                    "stopping": "Stopping servers...",
                    "updating": "Downloading update...",
                    "restarting": "Restarting servers...",
                    "complete": "Complete",
                    "failed": "Failed",
                }
                _POLL_INTERVAL = 10  # seconds between polls
                _MAX_POLLS = 360  # 60 minutes max
                for _ in range(_MAX_POLLS):
                    await asyncio.sleep(_POLL_INTERVAL)
                    try:
                        st = await client.get_update_status(matched["id"])
                    except Exception:
                        continue
                    if status_msg:
                        try:
                            await status_msg.edit(
                                embed=_update_phase_embed(
                                    host, st.get("phase", ""), st.get("message", "")
                                )
                            )
                        except Exception:
                            pass
                    if not st.get("running", True):
                        break

                # Final poll — ensures terminal state is always shown even if
                # the last embed edit failed or the loop timed out
                try:
                    st = await client.get_update_status(matched["id"])
                    if status_msg:
                        await status_msg.edit(
                            embed=_update_phase_embed(
                                host, st.get("phase", ""), st.get("message", "")
                            )
                        )
                except Exception:
                    pass

            asyncio.create_task(_poll_update())

        # ---------------------------------------------------------------- #
        # /dcs schedule                                                      #
        # ---------------------------------------------------------------- #

        @self.dcs.command(
            name="schedule-view",
            description="Show the current schedule for a DCS instance",
        )
        @app_commands.describe(instance="Instance to check")
        @app_commands.autocomplete(instance=_instance_autocomplete)
        async def cmd_schedule_view(
            interaction: discord.Interaction, instance: str
        ) -> None:
            if not await _check_channel(interaction):
                return
            await interaction.response.defer(ephemeral=True)
            try:
                sched = await client.get_instance_schedule(instance)
            except OrchestratorError as exc:
                await interaction.followup.send(
                    f"Orchestrator error: {exc.detail}", ephemeral=True
                )
                return

            if not sched:
                await interaction.followup.send(
                    f"No schedule set for `{instance}`.", ephemeral=True
                )
                return

            embed = discord.Embed(title=f"Schedule — `{instance}`", colour=0x3498DB)
            if sched.get("open_time") or sched.get("close_time"):
                tz = sched.get("timezone", "UTC")
                days = ", ".join(sched["days"]) if sched.get("days") else "every day"
                window = f"{sched.get('open_time', '—')} → {sched.get('close_time', '—')}  ({tz}, {days})"
                embed.add_field(name="Hours", value=window, inline=False)
            if sched.get("idle_restart_minutes"):
                embed.add_field(
                    name="Idle restart",
                    value=f"{sched['idle_restart_minutes']} min",
                    inline=True,
                )
            if sched.get("rotate_real_minutes"):
                embed.add_field(
                    name="Mission rotation",
                    value=f"Every {sched['rotate_real_minutes']} min",
                    inline=True,
                )
            if sched.get("mission_playlist"):
                embed.add_field(
                    name="Playlist",
                    value="\n".join(f"• {m}" for m in sched["mission_playlist"]),
                    inline=False,
                )
            await interaction.followup.send(embed=embed, ephemeral=True)

        @self.dcs.command(
            name="schedule-idle",
            description="Restart an instance automatically after N minutes with no players (0 to disable)",
        )
        @app_commands.describe(
            instance="Instance to configure",
            minutes="Minutes of inactivity before restart (0 = off)",
        )
        @app_commands.autocomplete(instance=_instance_autocomplete)
        async def cmd_schedule_idle(
            interaction: discord.Interaction, instance: str, minutes: int
        ) -> None:
            if not await _check_channel(interaction):
                return
            if not await _require_operator(interaction):
                return
            await interaction.response.defer(ephemeral=True)
            try:
                sched = await client.get_instance_schedule(instance) or {}
                if minutes == 0:
                    sched.pop("idle_restart_minutes", None)
                    msg = f"Idle restart **disabled** for `{instance}`."
                else:
                    sched["idle_restart_minutes"] = minutes
                    msg = f"Idle restart set to **{minutes} min** for `{instance}`."
                await client.set_instance_schedule(instance, sched)
                await interaction.followup.send(msg, ephemeral=True)
            except OrchestratorError as exc:
                await interaction.followup.send(
                    f"Orchestrator error: {exc.detail}", ephemeral=True
                )

        # ---- timezone autocomplete ----------------------------------------- #

        async def _timezone_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> list[app_commands.Choice[str]]:
            zones = [
                "UTC",
                "America/New_York",
                "America/Chicago",
                "America/Denver",
                "America/Los_Angeles",
                "America/Anchorage",
                "America/Halifax",
                "America/Sao_Paulo",
                "Europe/London",
                "Europe/Paris",
                "Europe/Berlin",
                "Europe/Helsinki",
                "Europe/Moscow",
                "Asia/Dubai",
                "Asia/Kolkata",
                "Asia/Bangkok",
                "Asia/Singapore",
                "Asia/Tokyo",
                "Asia/Seoul",
                "Australia/Sydney",
                "Australia/Perth",
                "Pacific/Auckland",
                "Pacific/Honolulu",
            ]
            low = current.lower()
            return [
                app_commands.Choice(name=z, value=z) for z in zones if low in z.lower()
            ][:25]

        # ---- days autocomplete --------------------------------------------- #

        async def _days_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> list[app_commands.Choice[str]]:
            presets = [
                ("All days", "mon,tue,wed,thu,fri,sat,sun"),
                ("Weekdays", "mon,tue,wed,thu,fri"),
                ("Weekends", "sat,sun"),
                ("Fri–Sun", "fri,sat,sun"),
                ("Sat–Sun", "sat,sun"),
                ("Thu–Sun", "thu,fri,sat,sun"),
                ("Monday", "mon"),
                ("Tuesday", "tue"),
                ("Wednesday", "wed"),
                ("Thursday", "thu"),
                ("Friday", "fri"),
                ("Saturday", "sat"),
                ("Sunday", "sun"),
            ]
            low = current.lower()
            return [
                app_commands.Choice(name=label, value=value)
                for label, value in presets
                if low in label.lower() or low in value.lower()
            ][:25]

        # ---- playlist mission autocomplete ---------------------------------- #

        async def _playlist_mission_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> list[app_commands.Choice[str]]:
            instance_name = getattr(interaction.namespace, "instance", None)
            if not instance_name:
                return []
            try:
                instances = await client.list_instances()
                inst = next((i for i in instances if i["name"] == instance_name), None)
                if not inst:
                    return []
                items = await client.list_active_missions(inst["hostId"])
            except Exception:
                return []
            ns = interaction.namespace
            already = {getattr(ns, f"mission_{i}", None) for i in range(1, 6)} - {
                None,
                current,
            }
            low = current.lower()
            choices = []
            for m in items:
                filename = m.get("relative_path", m.get("name", ""))
                if not filename or filename in already:
                    continue
                if low and low not in filename.lower():
                    continue
                choices.append(app_commands.Choice(name=filename[:100], value=filename))
            return choices[:25]

        @self.dcs.command(
            name="schedule-hours",
            description="Set open/close times for a DCS instance",
        )
        @app_commands.describe(
            instance="Instance to configure",
            open_time='Time to start the server, e.g. "18:00"',
            close_time='Time to stop the server (only if empty), e.g. "02:00"',
            timezone="IANA timezone (pick from list or type)",
            days="Active days (pick a preset or type comma-separated)",
        )
        @app_commands.autocomplete(
            instance=_instance_autocomplete,
            timezone=_timezone_autocomplete,
            days=_days_autocomplete,
        )
        async def cmd_schedule_hours(
            interaction: discord.Interaction,
            instance: str,
            open_time: str | None = None,
            close_time: str | None = None,
            timezone: str | None = None,
            days: str | None = None,
        ) -> None:
            if not await _check_channel(interaction):
                return
            if not await _require_operator(interaction):
                return
            await interaction.response.defer(ephemeral=True)
            if not open_time and not close_time and timezone is None and not days:
                await interaction.followup.send(
                    "Provide at least one field to update.",
                    ephemeral=True,
                )
                return
            try:
                sched = await client.get_instance_schedule(instance) or {}
                if open_time:
                    sched["open_time"] = open_time
                if close_time:
                    sched["close_time"] = close_time
                if timezone is not None:
                    sched["timezone"] = timezone
                if days:
                    sched["days"] = [d.strip().lower() for d in days.split(",")]
                await client.set_instance_schedule(instance, sched)
                await interaction.followup.send(
                    f"Hours updated for `{instance}`.", ephemeral=True
                )
            except OrchestratorError as exc:
                await interaction.followup.send(
                    f"Orchestrator error: {exc.detail}", ephemeral=True
                )

        @self.dcs.command(
            name="schedule-playlist",
            description="Set a mission rotation playlist for a DCS instance",
        )
        @app_commands.describe(
            instance="Instance to configure",
            mission_1="First mission in the rotation",
            mission_2="Second mission",
            mission_3="Third mission",
            mission_4="Fourth mission",
            mission_5="Fifth mission",
            rotate_minutes="Rotate to next mission every N minutes (0 = manual only)",
        )
        @app_commands.autocomplete(
            instance=_instance_autocomplete,
            mission_1=_playlist_mission_autocomplete,
            mission_2=_playlist_mission_autocomplete,
            mission_3=_playlist_mission_autocomplete,
            mission_4=_playlist_mission_autocomplete,
            mission_5=_playlist_mission_autocomplete,
        )
        async def cmd_schedule_playlist(
            interaction: discord.Interaction,
            instance: str,
            mission_1: str,
            mission_2: str | None = None,
            mission_3: str | None = None,
            mission_4: str | None = None,
            mission_5: str | None = None,
            rotate_minutes: int = 0,
        ) -> None:
            if not await _check_channel(interaction):
                return
            if not await _require_operator(interaction):
                return
            await interaction.response.defer(ephemeral=True)
            try:
                sched = await client.get_instance_schedule(instance) or {}
                playlist = [
                    m
                    for m in [mission_1, mission_2, mission_3, mission_4, mission_5]
                    if m
                ]
                sched["mission_playlist"] = playlist
                if rotate_minutes > 0:
                    sched["rotate_real_minutes"] = rotate_minutes
                else:
                    sched.pop("rotate_real_minutes", None)
                await client.set_instance_schedule(instance, sched)
                lines = "\n".join(f"• {m}" for m in playlist)
                rotation = (
                    f"\nRotation: every **{rotate_minutes} min**"
                    if rotate_minutes > 0
                    else ""
                )
                await interaction.followup.send(
                    f"Playlist updated for `{instance}`:\n{lines}{rotation}",
                    ephemeral=True,
                )
            except OrchestratorError as exc:
                await interaction.followup.send(
                    f"Orchestrator error: {exc.detail}", ephemeral=True
                )

        @self.dcs.command(
            name="schedule-clear",
            description="Remove all scheduling from a DCS instance",
        )
        @app_commands.describe(instance="Instance to clear schedule for")
        @app_commands.autocomplete(instance=_instance_autocomplete)
        async def cmd_schedule_clear(
            interaction: discord.Interaction, instance: str
        ) -> None:
            if not await _check_channel(interaction):
                return
            if not await _require_operator(interaction):
                return
            await interaction.response.defer(ephemeral=True)
            try:
                await client.delete_instance_schedule(instance)
                await interaction.followup.send(
                    f"Schedule cleared for `{instance}`.", ephemeral=True
                )
            except OrchestratorError as exc:
                if exc.status_code == 404:
                    await interaction.followup.send(
                        f"No schedule was set for `{instance}`.", ephemeral=True
                    )
                else:
                    await interaction.followup.send(
                        f"Orchestrator error: {exc.detail}", ephemeral=True
                    )

        # ---------------------------------------------------------------- #
        # /dcs register                                                      #
        # ---------------------------------------------------------------- #

        @self.dcs.command(
            name="register",
            description="Link your Discord account to your DCS pilot name",
        )
        @app_commands.describe(dcs_name="Your exact DCS pilot name (case-sensitive)")
        async def cmd_register(interaction: discord.Interaction, dcs_name: str) -> None:
            regs = _load_registrations()
            old = regs.get(str(interaction.user.id))
            regs[str(interaction.user.id)] = dcs_name
            _save_registrations(regs)
            if old and old != dcs_name:
                msg = f"Updated your DCS name from `{old}` to `{dcs_name}`."
            else:
                msg = f"Registered! Your Discord account is now linked to DCS pilot `{dcs_name}`."
            await interaction.response.send_message(msg, ephemeral=True)

        # ---------------------------------------------------------------- #
        # /dcs mystats                                                       #
        # ---------------------------------------------------------------- #

        @self.dcs.command(name="mystats", description="Show your personal DCS stats")
        @app_commands.describe(period="Time period: 7d, 30d, or all time")
        @app_commands.choices(
            period=[
                app_commands.Choice(name="Last 7 days", value="7d"),
                app_commands.Choice(name="Last 30 days", value="30d"),
                app_commands.Choice(name="All time", value="all"),
            ]
        )
        async def cmd_mystats(
            interaction: discord.Interaction,
            period: str = "7d",
        ) -> None:
            regs = _load_registrations()
            dcs_name = regs.get(str(interaction.user.id))
            if not dcs_name:
                await interaction.response.send_message(
                    "You haven't registered yet. Use `/dcs register <your DCS pilot name>` first.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True)

            from collections import Counter

            from datetime import timedelta

            if period == "7d":
                since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                period_label = "last 7 days"
            elif period == "30d":
                since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
                period_label = "last 30 days"
            else:
                since = None
                period_label = "all time"

            try:
                events = await client.get_analytics_events(since=since, limit=2000)
            except OrchestratorError as exc:
                await interaction.followup.send(
                    f"Orchestrator error: {exc.detail}", ephemeral=True
                )
                return

            my_joins = [
                e
                for e in events
                if e["event_type"] == "player_join" and e.get("player_name") == dcs_name
            ]

            # Pair join/leave for this player to compute session durations
            my_events = [
                e
                for e in events
                if e.get("player_name") == dcs_name
                and e["event_type"] in ("player_join", "player_leave")
            ]
            my_sessions = _pair_sessions(my_events)
            my_durations = [d for _, _, d in my_sessions]

            embed = discord.Embed(
                title=f"Stats for {dcs_name}",
                description=f"Period: {period_label}",
                colour=0x3498DB,
            )

            embed.add_field(name="Sessions", value=str(len(my_joins)), inline=True)

            mission_counts = Counter(
                e["mission_name"] for e in my_joins if e.get("mission_name")
            )
            embed.add_field(
                name="Missions Played", value=str(len(mission_counts)), inline=True
            )

            map_counts = Counter(e["map"] for e in my_joins if e.get("map"))
            embed.add_field(name="Maps Played", value=str(len(map_counts)), inline=True)

            if my_durations:
                avg_dur = sum(my_durations) / len(my_durations)
                total_dur = sum(my_durations)
                embed.add_field(
                    name="Avg Session", value=_fmt_duration(avg_dur), inline=True
                )
                embed.add_field(
                    name="Total Time", value=_fmt_duration(total_dur), inline=True
                )

            if mission_counts:
                fav_mission, fav_count = mission_counts.most_common(1)[0]
                embed.add_field(
                    name="Favourite Mission",
                    value=f"{fav_mission[:50]} ({fav_count}x)",
                    inline=True,
                )

            if map_counts:
                fav_map, fav_map_count = map_counts.most_common(1)[0]
                embed.add_field(
                    name="Favourite Map",
                    value=f"{fav_map} ({fav_map_count}x)",
                    inline=True,
                )

            # Personal peak hours
            if my_joins:

                def _fmt12(h: int) -> str:
                    suffix = "am" if h < 12 else "pm"
                    return f"{h % 12 or 12}{suffix}"

                hour_counts: dict[int, int] = {}
                for e in my_joins:
                    ts = e.get("timestamp") or ""
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        hour_counts[dt.hour] = hour_counts.get(dt.hour, 0) + 1
                    except Exception:
                        pass
                if hour_counts:
                    max_count = max(hour_counts.values())
                    bar_len = 8
                    peak_lines = []
                    for h in sorted(hour_counts, key=lambda x: -hour_counts[x])[:5]:
                        n = hour_counts[h]
                        filled = round(n / max_count * bar_len)
                        bar = "\u2588" * filled + "\u2591" * (bar_len - filled)
                        est = _fmt12((h - 5) % 24)
                        pst = _fmt12((h - 8) % 24)
                        peak_lines.append(f"{est} EST / {pst} PST  {bar} {n}")
                    embed.add_field(
                        name="Your Peak Hours",
                        value=chr(10).join(peak_lines),
                        inline=False,
                    )

            if not my_joins:
                embed.description = (
                    f"No sessions found for `{dcs_name}` in this period."
                )

            await interaction.followup.send(embed=embed, ephemeral=True)
