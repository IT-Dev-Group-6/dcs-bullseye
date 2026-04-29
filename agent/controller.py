"""
High-level DCS instance operations built on top of nssm.py.

Each method maps to a single DCS server instance. install() wires up all NSSM
parameters in the correct order so the service runs as the current Windows user
(required for Saved Games path resolution).
"""

from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import subprocess
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from .config import AgentConfig, InstanceConfig
from . import nssm as _nssm
from .nssm import NssmError


# ------------------------------------------------------------------
# Log redaction
# ------------------------------------------------------------------

_SECRET_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*\S+"),
    re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9\-_.]+"),
    re.compile(r"(?i)x-api-key:\s*\S+"),
]


def _redact_match(m: re.Match) -> str:
    full = m.group(0)
    sep_idx = max(full.find(":"), full.find("="))
    if sep_idx == -1:
        return "[REDACTED]"
    return full[: sep_idx + 1] + " [REDACTED]"


def redact_line(line: str) -> str:
    """Replace known secret patterns in a log line with [REDACTED]."""
    out = line
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(_redact_match, out)
    return out


# ------------------------------------------------------------------
# DCS log error parsing
# ------------------------------------------------------------------

# DCS log timestamp prefix: "YYYY-MM-DD HH:MM:SS.mmm"
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}")

# How many lines after an error line to include as context (stack traces, etc.)
_CONTEXT_LINES = 6

# Patterns that indicate a real scripting/Lua error
_SCRIPTING_ERROR_RE = re.compile(
    r"ERROR\s+SCRIPTING"  # ERROR level SCRIPTING category
    r"|WARNING\s+SCRIPTING.*[Ee]rror"  # SCRIPTING warning that contains "error"
    r"|LuaError"  # explicit Lua error tag
    r"|stack traceback:"  # Lua stack trace header
    r"|\bError in '[^']+'"  # Error in 'callback_name'
    r"|\bMission script error\b"
)

# Patterns that indicate a real DCS engine error (non-scripting)
_DCS_ERROR_RE = re.compile(
    r"ERROR\s+(?!SCRIPTING)"  # ERROR level, any category except SCRIPTING
    r"|ALERT\s+"  # any ALERT level line
    r"|\bCRITICAL\b"
)

# Known-harmless noise: matches → skip the line entirely
_NOISE_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bINFO\b"),  # all INFO lines
    re.compile(r"\bDEBUG\b"),  # all DEBUG lines
    re.compile(
        r"WARNING\s+SCRIPTING(?!.*[Ee]rror)"
    ),  # SCRIPTING warnings without "error"
    re.compile(r"WARNING\s+Net\b"),  # network hiccups
    re.compile(r"WARNING\s+EDCORE"),  # EDCORE perf warnings
    re.compile(r"Cannot find.*liveri", re.I),  # missing livery files
    re.compile(r"Cannot find.*sound", re.I),  # missing sound files
    re.compile(r"Cannot find.*texture", re.I),  # missing textures
    re.compile(r"Error running DCS GUI", re.I),  # normal shutdown message
    re.compile(r"OptionsBackground.*[Ee]rror", re.I),  # GUI option errors
    re.compile(r"W:EDCORE.*fps", re.I),  # FPS performance warnings
    re.compile(r"VoiceChat.*disconnect", re.I),  # SRS voice disconnects
    re.compile(r"Config.*\.lua.*not found", re.I),  # optional config files
    # Engine asset / driver loading (EDCORE — not actionable)
    re.compile(r"No suitable driver found to mount", re.I),
    re.compile(r"ZipDriver.*Failed to open zip archive.*\.gitkeep", re.I),
    re.compile(r"Drivers errors while mounting.*\.gitkeep", re.I),
    re.compile(
        r"Failed to load.*\.dll.*\(\d+\)"
    ),  # DLL load failures with OS error codes
    # Weapon/avionics configuration (ED module data bugs — not actionable)
    re.compile(r"Scheme doesn't have an input lead named", re.I),
    re.compile(r"negative (?:drag|weight) of payload\b", re.I),
    # Duplicate object/shape declarations (mod conflicts — not actionable)
    re.compile(r"already declared in (?:shapes\.txt|table\b)", re.I),
    # Unit and model data errors (incomplete/corrupt ED data)
    re.compile(r"Corrupt damage model\b", re.I),
    re.compile(r"Can't open model\b", re.I),
    re.compile(r"invalid source_params\b", re.I),
    re.compile(r"Effect presets records are defined but empty", re.I),
    re.compile(r"Cell .+ references an unknown cell", re.I),
    re.compile(r"Asymmetric(?:al)? wing configuration", re.I),
    re.compile(r"No cell for property records\b", re.I),
    re.compile(r"No property record for cell\b", re.I),
    re.compile(r"fires_pos index\b", re.I),
    # ATC and nav-aid config noise
    re.compile(r"\bINVALID ATC\b"),
    re.compile(r"Call error findGSByLOC\b", re.I),
    re.compile(r"Invalid localizer beacon frequency", re.I),
    # Network / service noise (DCS dedicated-server-mode artefacts)
    re.compile(r"HTTP request dcs:checksession failed", re.I),
    re.compile(r"Failed to init WebGui\b", re.I),
    re.compile(r"Possible problem binding to port", re.I),
]


def _is_noise(line: str) -> bool:
    return any(p.search(line) for p in _NOISE_PATTERNS)


def _classify_error(line: str) -> str | None:
    """Return 'scripting', 'dcs', or None."""
    if _SCRIPTING_ERROR_RE.search(line):
        return "scripting"
    if _DCS_ERROR_RE.search(line):
        return "dcs"
    return None


def _is_context_line(line: str) -> bool:
    """Return True for lines that are context/continuation (stack trace lines, etc.)."""
    return bool(
        re.match(r"\s+", line)  # indented (stack trace body)
        or "stack traceback" in line
        or re.match(r"\s*\[", line)  # Lua source reference [file]:line
    )


def _dedup_key(line: str) -> str:
    """Normalize an error line for deduplication — strip line numbers and timestamps."""
    # Remove Lua source line numbers: ]:123: → ]:N:
    out = re.sub(r"\]:\d+:", "]:N:", line)
    # Remove leading timestamp so identical messages at different times are grouped
    out = re.sub(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} ", "", out)
    return out


def _dedup_blocks(blocks: list[list[str]]) -> list[str]:
    """
    Collapse consecutive identical error blocks into a single block with a repeat count.
    blocks: list of error blocks, each block is a list of lines (error + context).
    """
    if not blocks:
        return []

    result: list[str] = []
    i = 0
    while i < len(blocks):
        block = blocks[i]
        key = _dedup_key(block[0]) if block else ""
        count = 1
        last_ts = ""
        j = i + 1
        while j < len(blocks):
            other = blocks[j]
            if other and _dedup_key(other[0]) == key:
                # Extract timestamp from duplicate for "last seen" reporting
                m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})", other[0])
                if m:
                    last_ts = m.group(1)
                count += 1
                j += 1
            else:
                break
        result.extend(block)
        if count > 1:
            result.append(
                f"    ... (repeated {count - 1} more time{'s' if count > 2 else ''}, last at {last_ts})"
            )
        result.append("")
        i = j

    while result and not result[-1]:
        result.pop()
    return result


# ------------------------------------------------------------------
# Task Scheduler helpers (for instances with manager="task")
# ------------------------------------------------------------------


def _kill_any_dcs_server() -> None:
    """Kill DCS_server.exe processes that were NOT started with a -w flag.

    Prevents port conflicts when a user has DCS running via a desktop shortcut
    (without -w flag) at the same time the agent tries to start it via Task Scheduler.
    Managed instances always include -w <saved_games_key> in their command line,
    so this only removes unmanaged orphans.
    """
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"name='DCS_server.exe'\" "
            "| Where-Object { $_.CommandLine -notlike '*-w *' } "
            "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }",
        ],
        capture_output=True,
        text=True,
    )


def _task_start(task_name: str) -> None:
    """Start a Windows Task Scheduler task by name."""
    _kill_any_dcs_server()
    result = subprocess.run(
        ["schtasks", "/run", "/tn", task_name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"schtasks /run failed: {(result.stdout + result.stderr).strip()}"
        )


def _task_stop(saved_games_key: str, task_name: str = "") -> None:
    """Kill the DCS_server.exe instance whose command line contains the saved_games_key.

    Also ends the Task Scheduler task so its status clears from 'Running', which
    allows schtasks /run to start a new instance immediately after.
    No-op if the process is not running.
    """
    ps = (
        f"$p = Get-CimInstance Win32_Process -Filter \"name='DCS_server.exe'\" "
        f"| Where-Object {{ $_.CommandLine -like '*{saved_games_key}*' }}; "
        f"if ($p) {{ Stop-Process -Id $p.ProcessId -Force }}"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
    )
    # Clear the Task Scheduler "Running" state so the next /run isn't ignored
    if task_name:
        subprocess.run(
            ["schtasks", "/end", "/tn", task_name],
            capture_output=True,
            text=True,
        )


def _task_status(saved_games_key: str) -> str:
    """Return SERVICE_RUNNING or SERVICE_STOPPED based on whether the process is alive."""
    ps = (
        f"$p = Get-CimInstance Win32_Process -Filter \"name='DCS_server.exe'\" "
        f"| Where-Object {{ $_.CommandLine -like '*{saved_games_key}*' }}; "
        f"if ($p) {{ 'SERVICE_RUNNING' }} else {{ 'SERVICE_STOPPED' }}"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or "SERVICE_STOPPED"


def _task_runtime(saved_games_key: str) -> dict:
    """Return {Status, Pid, CreationDate} for the Task Scheduler DCS process."""
    ps = (
        f"$p = Get-CimInstance Win32_Process -Filter \"name='DCS_server.exe'\" "
        f"| Where-Object {{ $_.CommandLine -like '*{saved_games_key}*' }}; "
        f"if ($p) {{ "
        f"[PSCustomObject]@{{ Status='SERVICE_RUNNING'; Pid=[int]$p.ProcessId; "
        f"CreationDate=$p.CreationDate.ToUniversalTime().ToString('o') }} | ConvertTo-Json "
        f"}} else {{ "
        f"[PSCustomObject]@{{ Status='SERVICE_STOPPED'; Pid=$null; CreationDate=$null }} | ConvertTo-Json "
        f"}}"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return {"Status": "SERVICE_STOPPED", "Pid": None, "CreationDate": None}
    except ValueError:
        return {"Status": "SERVICE_STOPPED", "Pid": None, "CreationDate": None}


_MISSION_DONE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?).*loadMission Done"
)
_MISSION_LOAD_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?.*loadMission\s+(.+\.miz)",
    re.IGNORECASE,
)
_THEATRE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?.*Terrain theatre\s+(\S+)",
    re.IGNORECASE,
)


_HOOK_STALE_SECONDS = 300  # hook writes every 5 s; discard if silent for 5 min


def _read_hook_status(log_path: str) -> dict:
    """Read the dcs_agent_status.json written by the Lua hook, or return {}.

    Returns {} if the file is missing or unreadable.  If stale, returns the
    last known mission data with player fields zeroed so callers show the last
    known mission time rather than --- while still showing 0 players.
    """
    status_file = Path(log_path).parent / "dcs_agent_status.json"
    if not status_file.exists():
        return {}
    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except json.JSONDecodeError:
        return {}
    updated_at_str = data.get("updated_at")
    if updated_at_str:
        try:
            updated_at = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
            if (
                datetime.now(timezone.utc) - updated_at
            ).total_seconds() > _HOOK_STALE_SECONDS:
                data["player_count"] = 0
                data["players"] = []
                data["_stale"] = True
                return data
        except ValueError:
            pass
        except TypeError:
            pass
    return data


def _get_mission_info_from_log(
    log_path: str,
) -> tuple[str | None, str | None, str | None]:
    """Return (started_at_iso, mission_name, theatre) from the last completed loadMission in the DCS log.

    Scans the full log file so it works even for servers that have been running for weeks.
    mission_name is the stem of the .miz filename (e.g. 'Nevada Proving Groundss_5').
    theatre comes from 'Terrain theatre <name>' log lines (e.g. 'Nevada', 'Caucasus').
    """
    log = Path(log_path)
    if not log.exists():
        return None, None, None
    last_ts: str | None = None
    last_name: str | None = None
    last_theatre: str | None = None
    pending_name: str | None = None
    pending_theatre: str | None = None
    try:
        with log.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                ml = _MISSION_LOAD_RE.match(line)
                if ml:
                    pending_name = Path(ml.group(1).strip()).stem
                    pending_theatre = None
                    continue
                th = _THEATRE_RE.match(line)
                if th:
                    pending_theatre = th.group(1)
                    continue
                md = _MISSION_DONE_RE.match(line)
                if md:
                    last_ts = md.group(1)
                    last_name = pending_name
                    last_theatre = pending_theatre
                    pending_name = None
                    pending_theatre = None
    except OSError:
        return None, None, None
    if last_ts is None:
        return None, None, None
    try:
        dt = datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        try:
            dt = datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None, None, None
    # DCS logs use local time; convert to UTC correctly
    return (
        datetime.fromtimestamp(dt.timestamp(), tz=timezone.utc).isoformat(),
        last_name,
        last_theatre,
    )


def _patch_mission_list(content: str, lua_path: str) -> str:
    """Replace the missionList block in serverSettings.lua with a single-entry list."""
    replacement = f'["missionList"] =\n\t{{\n\t\t[1] = "{lua_path}",\n\t}}'
    # Use a lambda so re.sub doesn't interpret backslashes in the replacement string
    patched, n = re.subn(
        r'\["missionList"\]\s*=\s*\{[^}]*\}',
        lambda _: replacement,
        content,
        flags=re.DOTALL,
    )
    if n == 0:
        raise RuntimeError('Could not locate ["missionList"] in serverSettings.lua')
    return patched


class DcsController:
    def __init__(self, config: AgentConfig) -> None:
        self._config = config

    # ------------------------------------------------------------------
    # Service lifecycle
    # ------------------------------------------------------------------

    def install(self, instance: InstanceConfig) -> None:
        """Install the DCS instance as an NSSM service and configure all parameters."""
        cfg = self._config
        n = cfg.nssm_path
        svc = instance.service_name
        exe = instance.exe_path
        args = f"-w {instance.saved_games_key}"

        _nssm.install(n, svc, exe, args)

        # Working directory = DCS bin dir (parent of DCS_server.exe)
        app_dir = str(Path(exe).parent)
        _nssm.set_param(n, svc, "AppDirectory", app_dir)

        _nssm.set_param(n, svc, "Description", f"DCS World — {instance.name}")

        start_type = (
            "SERVICE_AUTO_START" if instance.auto_start else "SERVICE_DEMAND_START"
        )
        _nssm.set_param(n, svc, "Start", start_type)

        # Redirect stdout/stderr to a per-service log file
        log_dir = Path(cfg.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = str(log_dir / f"{svc}.log")
        _nssm.set_param(n, svc, "AppStdout", log_file)
        _nssm.set_param(n, svc, "AppStderr", log_file)

        # Daily log rotation
        _nssm.set_param(n, svc, "AppRotateFiles", "1")
        _nssm.set_param(n, svc, "AppRotateSeconds", "86400")

        # Run as the current Windows user so Saved Games paths resolve correctly.
        # The installer must be run as that same user (or admin with credentials).
        username = getpass.getuser()
        domain = os.environ.get("USERDOMAIN", ".")
        _nssm.set_param(n, svc, "ObjectName", f"{domain}\\{username}")

    def remove(self, instance: InstanceConfig) -> None:
        """Remove the NSSM service for this instance."""
        _nssm.remove(self._config.nssm_path, instance.service_name)

    def start(self, instance: InstanceConfig) -> None:
        """Start the instance (NSSM service or Task Scheduler task)."""
        if instance.manager == "task":
            _task_start(instance.service_name)
        else:
            _nssm.start(self._config.nssm_path, instance.service_name)

    def stop(self, instance: InstanceConfig) -> None:
        """Stop the instance."""
        if instance.manager == "task":
            _task_stop(instance.saved_games_key, task_name=instance.service_name)
        else:
            _nssm.stop(self._config.nssm_path, instance.service_name)

    def restart(self, instance: InstanceConfig) -> None:
        """Restart the instance."""
        if instance.manager == "task":
            _task_stop(instance.saved_games_key, task_name=instance.service_name)
            import time

            time.sleep(2)  # let Task Scheduler clear running state before re-triggering
            _task_start(instance.service_name)
        else:
            _nssm.restart(self._config.nssm_path, instance.service_name)

    def status(self, instance: InstanceConfig) -> str:
        """Return a status string (SERVICE_RUNNING / SERVICE_STOPPED)."""
        if instance.manager == "task":
            return _task_status(instance.saved_games_key)
        return _nssm.status(self._config.nssm_path, instance.service_name)

    def runtime_info(self, instance: InstanceConfig) -> dict:
        """Return full runtime dict: status, pid, started_at, mission_started_at."""
        if instance.manager == "task":
            data = _task_runtime(instance.saved_games_key)
            status_raw = data.get("Status", "SERVICE_STOPPED")
            pid = data.get("Pid")
            started_at = data.get("CreationDate")
        else:
            status_raw = _nssm.status(self._config.nssm_path, instance.service_name)
            pid = None
            started_at = None

        mission_started_at: str | None = None
        log_mission_name: str | None = None
        log_theatre: str | None = None
        hook: dict = {}
        if status_raw == "SERVICE_RUNNING":
            mission_started_at, log_mission_name, log_theatre = (
                _get_mission_info_from_log(instance.log_path)
            )
            hook = _read_hook_status(instance.log_path)
            if hook.get("_stale"):
                status_raw = "SERVICE_DEGRADED"

        return {
            "status": status_raw,
            "pid": pid,
            "started_at": started_at,
            "mission_started_at": mission_started_at,
            # Prefer hook data (live); fall back to log-parsed name (no restart needed)
            "mission_name": hook.get("mission_name") or log_mission_name or None,
            # Prefer hook map; fall back to log-parsed theatre
            "map": hook.get("map") or log_theatre or None,
            "player_count": hook.get("player_count"),
            "players": hook.get("players") or [],
            # Only expose in-game time when mission is actually loaded (avoids showing 0:00:00)
            "mission_time_seconds": hook.get("mission_time_seconds")
            if hook.get("mission_loaded")
            else None,
        }

    def minimize_windows(self) -> None:
        """Minimize all DCS_server.exe windows via the DCS-MinimizeWindows Task Scheduler task."""
        result = subprocess.run(
            ["schtasks", "/run", "/tn", "DCS-MinimizeWindows"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"minimize task failed: {(result.stdout + result.stderr).strip()}"
            )

    # ------------------------------------------------------------------
    # Bulk helpers
    # ------------------------------------------------------------------

    def all_statuses(self) -> list[dict]:
        """Return a list of {name, service, status} dicts for all instances."""
        results = []
        for inst in self._config.instances:
            try:
                svc_status = self.status(inst)
            except NssmError as exc:
                svc_status = f"ERROR: {exc}"
            results.append(
                {"name": inst.name, "service": inst.service_name, "status": svc_status}
            )
        return results

    # ------------------------------------------------------------------
    # Mission loading
    # ------------------------------------------------------------------

    def mission_load(self, instance: InstanceConfig, mission_file: str) -> str:
        """
        Update serverSettings.lua to load *mission_file*, then restart the service.

        mission_file may be:
          - A bare filename resolved against instance.missions_dir, then active_missions_dir
          - An absolute path (e.g. from active_missions_dir)

        Returns the absolute path of the mission that was set.
        """
        missions_dir = Path(instance.missions_dir)
        candidate = Path(mission_file)
        if candidate.is_absolute():
            mission_path = candidate
        else:
            mission_path = missions_dir / mission_file
            # Also check active_missions_dir for bare filenames
            if not mission_path.exists() and self._config.active_missions_dir:
                active_path = Path(self._config.active_missions_dir) / mission_file
                if active_path.exists():
                    mission_path = active_path

        if mission_path.suffix.lower() != ".miz":
            raise ValueError(f"Not a .miz file: {mission_file!r}")
        if not mission_path.exists():
            raise FileNotFoundError(f"Mission not found: {mission_path}")

        settings_path = missions_dir.parent / "Config" / "serverSettings.lua"
        if not settings_path.exists():
            raise FileNotFoundError(f"serverSettings.lua not found: {settings_path}")

        lua_path = str(mission_path).replace("\\", "\\\\")
        content = settings_path.read_text(encoding="utf-8")
        settings_path.write_text(
            _patch_mission_list(content, lua_path), encoding="utf-8"
        )

        self.restart(instance)
        return str(mission_path)

    # ------------------------------------------------------------------
    # Log tailing
    # ------------------------------------------------------------------

    def tail_logs(self, instance: InstanceConfig, lines: int = 50) -> list[str]:
        """Return the last *lines* lines from the DCS log for this instance."""
        log_path = Path(instance.log_path)
        if not log_path.exists():
            raise FileNotFoundError(f"Log file not found: {log_path}")
        buffer: deque[str] = deque(maxlen=lines)
        with log_path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                buffer.append(line)
        return list(buffer)

    def parse_errors(
        self, instance: InstanceConfig, search_lines: int = 5000
    ) -> dict[str, list[str]]:
        """
        Parse the DCS log and return two lists of real errors, filtered for noise.

        Returns:
            {
                "scripting_errors": [...],  # Lua/script errors with context
                "dcs_errors":       [...],  # DCS engine errors with context
            }

        Noise filtering:
        - INFO/DEBUG level lines are never errors
        - Known-harmless patterns (missing liveries, shutdown messages, etc.) are excluded

        Context capture:
        - Up to _CONTEXT_LINES lines after each error are included so stack traces
          and follow-on messages aren't lost.
        """
        log_path = Path(instance.log_path)
        if not log_path.exists():
            return {"scripting_errors": [], "dcs_errors": []}

        buf: deque[str] = deque(maxlen=search_lines)
        try:
            with log_path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    buf.append(line)
        except OSError:
            return {"scripting_errors": [], "dcs_errors": []}

        lines = list(buf)
        scripting_blocks: list[list[str]] = []
        dcs_blocks: list[list[str]] = []

        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.rstrip()

            if _is_noise(stripped):
                i += 1
                continue

            kind = _classify_error(stripped)
            if kind:
                # Collect this line + up to _CONTEXT_LINES of follow-on context
                block = [stripped]
                j = i + 1
                while j < len(lines) and j <= i + _CONTEXT_LINES:
                    ctx = lines[j].rstrip()
                    # Stop context if we hit a new timestamped non-context line
                    if _TIMESTAMP_RE.match(ctx) and not _is_context_line(ctx):
                        break
                    block.append(ctx)
                    j += 1
                if kind == "scripting":
                    scripting_blocks.append(block)
                else:
                    dcs_blocks.append(block)
                i = j
                continue

            i += 1

        return {
            "scripting_errors": _dedup_blocks(scripting_blocks),
            "dcs_errors": _dedup_blocks(dcs_blocks),
        }

    def scripting_errors(
        self, instance: InstanceConfig, search_lines: int = 5000
    ) -> list[str]:
        """Legacy wrapper — returns scripting_errors from parse_errors."""
        return self.parse_errors(instance, search_lines)["scripting_errors"]

    # ------------------------------------------------------------------
    # Mission management
    # ------------------------------------------------------------------

    def delete_mission(self, instance: InstanceConfig, filename: str) -> None:
        """Delete a .miz file from the instance's missions directory."""
        if not filename.lower().endswith(".miz"):
            raise ValueError(f"Not a .miz file: {filename!r}")
        mission_path = Path(instance.missions_dir) / filename
        if not mission_path.exists():
            raise FileNotFoundError(f"Mission not found: {mission_path}")
        mission_path.unlink()

    def copy_mission_to_active(self, instance: InstanceConfig, filename: str) -> dict:
        """Copy a .miz from the instance's missions_dir into active_missions_dir."""
        active_dir = self._config.active_missions_dir
        if not active_dir:
            raise ValueError("active_missions_dir not configured")
        src = Path(instance.missions_dir) / filename
        if not src.exists():
            raise FileNotFoundError(f"Mission not found: {src}")
        dest = Path(active_dir) / filename
        import shutil

        shutil.copy2(src, dest)
        return {
            "filename": filename,
            "path": str(dest),
            "size_bytes": dest.stat().st_size,
        }

    def upload_active_mission(self, filename: str, data: bytes) -> dict:
        """Save uploaded bytes to active_missions_dir root, overwriting any existing file."""
        active_dir = self._config.active_missions_dir
        if not active_dir:
            raise ValueError("active_missions_dir not configured")
        dest_dir = Path(active_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        dest.write_bytes(data)
        return {"path": str(dest), "filename": filename, "size": len(data)}

    def delete_active_mission(self, filename: str) -> dict:
        """Move a .miz from active_missions_dir root to Backup_Missions/ subfolder."""
        active_dir = self._config.active_missions_dir
        if not active_dir:
            raise ValueError("active_missions_dir not configured")
        src = Path(active_dir) / filename
        if not src.exists():
            raise FileNotFoundError(f"Mission not found: {src}")
        backup_dir = Path(active_dir) / "Backup_Missions"
        backup_dir.mkdir(parents=True, exist_ok=True)
        dest = backup_dir / filename
        shutil.move(str(src), str(dest))
        return {"backed_up_to": f"Backup_Missions/{filename}"}

    # ------------------------------------------------------------------
    # Server settings
    # ------------------------------------------------------------------

    def set_password(self, instance: InstanceConfig, password: str) -> None:
        """Patch ["password"] in serverSettings.lua, then restart the server."""
        settings_path = (
            Path(instance.missions_dir).parent / "Config" / "serverSettings.lua"
        )
        if not settings_path.exists():
            raise FileNotFoundError(f"serverSettings.lua not found: {settings_path}")
        content = settings_path.read_text(encoding="utf-8")
        escaped = password.replace("\\", "\\\\").replace('"', '\\"')
        patched, n = re.subn(
            r'\["password"\]\s*=\s*"[^"]*"',
            lambda _: f'["password"] = "{escaped}"',
            content,
        )
        if n == 0:
            raise RuntimeError('Could not locate ["password"] in serverSettings.lua')
        settings_path.write_text(patched, encoding="utf-8")
        self.restart(instance)

    # ------------------------------------------------------------------
    # Persistence reset
    # ------------------------------------------------------------------

    def reset_persist(self, instance: InstanceConfig) -> dict:
        """Move .json/.lua/.csv from Missions/Saves/ to a timestamped backup subfolder.

        Returns {"backed_up": N, "backup_dir": "Backup_YYYYMMDD_HHMMSS"}.
        """
        saves_dir = Path(instance.missions_dir) / "Saves"
        if not saves_dir.exists():
            return {"backed_up": 0, "backup_dir": None}
        backup_name = f"Backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        backup_dir = saves_dir / backup_name
        backup_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for pattern in ("*.json", "*.lua", "*.csv"):
            for f in saves_dir.glob(pattern):
                shutil.move(str(f), str(backup_dir / f.name))
                count += 1
        return {"backed_up": count, "backup_dir": backup_name}

    # ------------------------------------------------------------------
    # Host reboot
    # ------------------------------------------------------------------

    def reboot_host(self) -> dict:
        """Schedule a Windows reboot with a 30-second grace period."""
        subprocess.run(["shutdown", "/r", "/t", "30"], capture_output=True, text=True)
        return {"rebooting": True, "delay_seconds": 30}

    # ------------------------------------------------------------------
    # DCS update
    # ------------------------------------------------------------------

    _UPDATE_STATUS_FILE = Path(r"C:\ProgramData\DCSAgent\update_status.json")

    def trigger_dcs_update(self) -> dict:
        """Start the DCS update process in a background thread.

        Returns immediately. Poll get_update_status() for progress.
        The agent process already runs as the correct Windows user so
        DCS_updater.exe can authenticate without a Task Scheduler task.
        """
        import threading

        if self._UPDATE_STATUS_FILE.exists():
            try:
                self._UPDATE_STATUS_FILE.unlink()
            except OSError:
                pass
        thread = threading.Thread(target=self._run_update, daemon=True)
        thread.start()
        return {"triggered": True}

    def _write_update_status(self, phase: str, running: bool, message: str) -> None:
        self._UPDATE_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "phase": phase,
            "running": running,
            "message": message,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._UPDATE_STATUS_FILE.write_text(
            json.dumps(data, indent=4), encoding="utf-8"
        )

    def _run_update(self) -> None:
        """Background thread: stop instances → update DCS → restart instances."""
        import time
        import re
        import urllib.request

        try:
            self._write_update_status("starting", True, "Reading agent config...")

            instances = self._config.instances
            if not instances:
                self._write_update_status(
                    "failed", False, "No instances found in config"
                )
                return

            updater_path = Path(instances[0].exe_path).parent / "DCS_updater.exe"
            if not updater_path.exists():
                self._write_update_status(
                    "failed", False, f"DCS_updater.exe not found at: {updater_path}"
                )
                return

            # Read current version before updating
            dcs_root = updater_path.parent.parent
            cfg_path = dcs_root / "autoupdate.cfg"
            version_before = "unknown"
            if cfg_path.exists():
                try:
                    version_before = json.loads(
                        cfg_path.read_text(encoding="utf-8")
                    ).get("version", "unknown")
                except Exception:
                    pass

            # Stop all instances
            self._write_update_status("stopping", True, "Stopping all DCS servers...")
            for inst in instances:
                subprocess.run(
                    ["schtasks", "/end", "/tn", inst.service_name], capture_output=True
                )
                ps = (
                    f"$p = Get-CimInstance Win32_Process -Filter \"name='DCS_server.exe'\" "
                    f"| Where-Object {{ $_.CommandLine -like '*{inst.saved_games_key}*' }}; "
                    f"if ($p) {{ Stop-Process -Id $p.ProcessId -Force }}"
                )
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps], capture_output=True
                )
            time.sleep(10)

            # Fetch latest version from DCS changelog
            target_version: str | None = None
            try:
                with urllib.request.urlopen(
                    "https://www.digitalcombatsimulator.com/en/news/changelog/release/",
                    timeout=10,
                ) as resp:
                    content = resp.read().decode("utf-8", errors="ignore")
                m = re.search(
                    r"/en/news/changelog/release/(\d+\.\d+\.\d+\.\d+)/", content
                )
                if m:
                    target_version = m.group(1)
            except Exception:
                pass

            msg = (
                f"Running DCS updater — updating to {target_version}..."
                if target_version
                else "Running DCS updater — this may take 10-60 minutes..."
            )
            self._write_update_status("updating", True, msg)

            # Run DCS_updater.exe directly (inherits agent user credentials)
            cmd = (
                [str(updater_path), "update", target_version]
                if target_version
                else [str(updater_path), "--quiet", "update"]
            )
            result = subprocess.run(
                cmd, capture_output=True, text=True, cwd=str(updater_path.parent)
            )
            if result.returncode != 0:
                self._write_update_status(
                    "failed",
                    False,
                    f"DCS_updater.exe failed with exit code {result.returncode}",
                )
                return

            # Restart all instances
            self._write_update_status("restarting", True, "Restarting DCS servers...")
            for inst in instances:
                subprocess.run(
                    ["schtasks", "/run", "/tn", inst.service_name], capture_output=True
                )
                time.sleep(5)

            # Read version after and report
            version_after = "unknown"
            if cfg_path.exists():
                try:
                    version_after = json.loads(
                        cfg_path.read_text(encoding="utf-8")
                    ).get("version", "unknown")
                except Exception:
                    pass

            if version_after != version_before and version_after != "unknown":
                complete_msg = f"Updated {version_before} \u2192 {version_after}. Servers are coming back online."
            else:
                complete_msg = f"Already up to date ({version_after}). Servers are coming back online."
            self._write_update_status("complete", False, complete_msg)

        except Exception as exc:
            self._write_update_status("failed", False, f"Unexpected error: {exc}")

    def get_update_status(self) -> dict:
        """Read the current DCS update status from the status JSON written by the update script."""
        if not self._UPDATE_STATUS_FILE.exists():
            return {
                "phase": "idle",
                "running": False,
                "message": "No update in progress",
            }
        try:
            # utf-8-sig strips BOM if present (e.g. written by PowerShell Set-Content)
            return json.loads(self._UPDATE_STATUS_FILE.read_text(encoding="utf-8-sig"))
        except OSError:
            return {
                "phase": "unknown",
                "running": False,
                "message": "Could not read status",
            }
        except json.JSONDecodeError:
            return {
                "phase": "unknown",
                "running": False,
                "message": "Could not read status",
            }
