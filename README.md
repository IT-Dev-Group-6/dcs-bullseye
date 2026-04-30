<img src="https://raw.githubusercontent.com/wiki/IT-Dev-Group-6/dcs-bullseye/dcs-bullseyelogo.png" alt="DCS Bullseye" width="150">

# dcs-bullseye

> **Pre-production** — actively developed and running in a private environment. Not yet ready for general use.

DCS World server management system — Discord bot + orchestrator + Windows agent.

## Structure

| Directory | Status | Description |
|-----------|--------|-------------|
| `orchestrator/` | Active | FastAPI orchestrator (command router, auth, jobs, audit logging) |
| `agent/` | Active | FastAPI agent (Windows node: process control, logs, missions) |
| `discord-bot/` | Active | DCS management Discord bot |
| `docs/` | Reference | API specs, architecture docs, design notes |
| `configs/` | — | Config file examples |
| `scripts/` | — | Dev/build/test scripts |

## Docs

- `docs/OpenAPI.yaml` — Full REST API contract (source of truth)
- `docs/network-overview.md` — Architecture, tunnel and connectivity diagram
- `docs/bot-guide.md` — Full user command reference

## Deployment

| Step | Script | Platform |
|------|--------|----------|
| 1. Set up VPS | `scripts/install-vps.sh` | Linux (Debian/Ubuntu) |
| 2. Connect Windows DCS host | `scripts/install-agent.ps1` | Windows (via invite code) |

## Quick Reference (development)

### Orchestrator
```bash
cd orchestrator
pip install -r requirements.txt
export DCS_ORCHESTRATOR_CONFIG=../configs/orchestrator.example.json
python -m orchestrator serve
```

### Agent (Windows)
```bash
cd agent
pip install -r requirements.txt
# configure C:\ProgramData\DCSAgent\config.json
python -m agent serve
```

### Discord Bot
```bash
cd discord-bot
pip install -r requirements.txt
cp .env.example .env  # fill in DISCORD_TOKEN, GUILD_ID, ORCHESTRATOR_URL
python bot.py
```

See `docs/bot-guide.md` for the full command reference and `docs/` for architecture and API details.
