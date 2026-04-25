"""
Agent configuration — dataclasses and JSON loader.
Config path defaults to env var DCS_AGENT_CONFIG, then C:\\ProgramData\\DCSAgent\\config.json.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path(
    os.environ.get("DCS_AGENT_CONFIG", r"C:\ProgramData\DCSAgent\config.json")
)


class ConfigError(Exception):
    """Raised when the agent configuration cannot be loaded or validated."""


@dataclass
class InstanceConfig:
    name: str  # human label e.g. "Server 1"
    service_name: str  # NSSM service name e.g. "DCS-server1"
    exe_path: str  # full path to DCS_server.exe
    saved_games_key: str  # -w argument e.g. "DCS.server1"
    log_path: str  # dcs.log path for tailing
    missions_dir: str  # missions directory
    auto_start: bool = True
    ports: dict = field(default_factory=dict)  # game/webgui/srs/tacview
    manager: str = "nssm"  # "nssm" or "task" (Windows Task Scheduler)
    bench_csv_path: str = ""  # output path for bench_monitor.py CSV; empty = skip CPU data
    bench_monitor_script: str = ""  # path to bench_monitor.py; empty = no CPU monitoring


@dataclass
class AgentConfig:
    instances: list[InstanceConfig]
    nssm_path: str = "nssm"  # path to nssm.exe or just "nssm" if on PATH
    log_dir: str = r"C:\ProgramData\DCSAgent\logs"
    api_key: str = ""  # X-API-Key; empty = auth disabled (dev mode)
    host: str = "0.0.0.0"
    port: int = 8787
    active_missions_dir: str = ""  # shared mission library folder (walked by /missions)
    max_upload_bytes: int = 100 * 1024 * 1024  # max .miz upload size (default 100 MB)
    orchestrator_url: str = ""  # e.g. https://my-vps:8888 --� set by installer
    host_id: str = ""  # assigned by orchestrator at registration
    afterburner_bin: str = "afterburner"  # path or name of the afterburner CLI


def _parse_instances(raw: list[dict[str, Any]]) -> list[InstanceConfig]:
    parsed: list[InstanceConfig] = []
    for item in raw:
        required = (
            "name",
            "service_name",
            "exe_path",
            "saved_games_key",
            "log_path",
            "missions_dir",
        )
        missing = [f for f in required if not item.get(f)]
        if missing:
            raise ConfigError(
                f"Instance {item.get('name', '?')!r} missing fields: {', '.join(missing)}"
            )
        parsed.append(
            InstanceConfig(
                name=item["name"],
                service_name=item["service_name"],
                exe_path=item["exe_path"],
                saved_games_key=item["saved_games_key"],
                log_path=item["log_path"],
                missions_dir=item["missions_dir"],
                auto_start=bool(item.get("auto_start", True)),
                ports=dict(item.get("ports", {})),
                manager=str(item.get("manager", "nssm")),
                bench_csv_path=str(item.get("bench_csv_path", "")),
                bench_monitor_script=str(item.get("bench_monitor_script", "")),
            )
        )
    return parsed


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> AgentConfig:
    """
    Load and validate the agent configuration JSON.

    The path defaults to %ProgramData%\\DCSAgent\\config.json but can be
    overridden via the DCS_AGENT_CONFIG environment variable or --config flag.
    """
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise ConfigError(f"Config file not found: {cfg_path}")

    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid config JSON: {exc}") from exc

    raw_instances = data.get("instances", [])
    if not isinstance(raw_instances, list) or not raw_instances:
        raise ConfigError("Config must contain a non-empty 'instances' list.")

    instances = _parse_instances(raw_instances)
    return AgentConfig(
        instances=instances,
        nssm_path=str(data.get("nssm_path", "nssm")),
        log_dir=str(data.get("log_dir", r"C:\ProgramData\DCSAgent\logs")),
        api_key=str(data.get("api_key", "")),
        host=str(data.get("host", "0.0.0.0")),
        port=int(data.get("port", 8787)),
        active_missions_dir=str(data.get("active_missions_dir", "")),
        max_upload_bytes=int(data.get("max_upload_bytes", 100 * 1024 * 1024)),
        orchestrator_url=str(data.get("orchestrator_url", "")),
        host_id=str(data.get("host_id", "")),
        afterburner_bin=str(data.get("afterburner_bin", "afterburner")),
    )
