# DCS Discord Bot

Discord slash-command interface for managing DCS World server instances via the orchestrator API.
All commands are grouped under `/dcs`.

## Commands

### Server Control

| Command | Role | Description |
|---------|------|-------------|
| `/dcs status [instance]` | Everyone | All instances or one detailed status |
| `/dcs start <instance>` | Operator | Start a DCS instance |
| `/dcs stop <instance>` | Operator | Stop a DCS instance |
| `/dcs restart <instance>` | Operator | Restart a DCS instance |
| `/dcs restartall` | Operator | Restart every currently running instance |

### Missions

| Command | Role | Description |
|---------|------|-------------|
| `/dcs mission <instance> <filename>` | Operator | Load a mission and restart the server |
| `/dcs upload <file>` | Operator | Upload a .miz file to the Active Missions folder |
| `/dcs download <filename>` | Operator | Download a .miz file from the Active Missions folder |
| `/dcs delete <filename>` | Operator | Delete a mission (backed up first, requires confirmation) |

### Server Management

| Command | Role | Description |
|---------|------|-------------|
| `/dcs logs <instance>` | Everyone | Fetch a log bundle for an instance |
| `/dcs password <instance> <password>` | Operator | Change the multiplayer password and restart |
| `/dcs resetpersist <instance>` | Operator | Back up and clear persistence save files (requires confirmation) |
| `/dcs minimize` | Operator | Minimize DCS server windows on the host |
| `/dcs reboot <host>` | Admin | Reboot a Windows host machine (requires confirmation) |
| `/dcs update <host>` | Admin | Update DCS World and restart all servers on a host (requires confirmation) |

### Analytics

| Command | Role | Description |
|---------|------|-------------|
| `/dcs stats [instance] [period]` | Everyone | Server-wide analytics: sessions, players, missions, maps, peak hours |
| `/dcs register <dcs_name>` | Everyone | Link your Discord account to your DCS pilot name |
| `/dcs mystats [period]` | Everyone | Your personal stats (ephemeral) |

### Administration

| Command | Role | Description |
|---------|------|-------------|
| `/dcs hosts` | Everyone | List registered host machines and their status |
| `/dcs jobs [status]` | Everyone | Last 10 background jobs with optional status filter |
| `/dcs invite [host_name] [expires_in_hours]` | Admin | Generate a community host invite code |
| `/dcs clear` | Operator | Delete recent bot messages from the channel |

Instance arguments use autocomplete — available instances are fetched live from the orchestrator.

---

## Automatic Features

- **Live status embed** — refreshes every 5 minutes in the configured status channel
- **Daily restart** — servers running longer than 48 hours auto-restart at 5:00 AM Eastern
- **Keepalive auto-start** — stopped instances are started automatically unless their exact instance name is listed in `AUTO_RESTART_EXCLUDE`
- **Crash loop detection** — alerts when an instance crashes 3+ times within 10 minutes
- **Analytics collection** — agent pushes player join/leave and mission events automatically

Test note: on `Goon Test Kitchen`, the host update flow stopped the server but did not actually run `DCS_updater.exe`.

---

## Setup

```bash
cd discord-bot
python -m venv .venv
.venv/Scripts/activate       # Windows
pip install -r requirements.txt

cp .env.example .env
# Fill in DISCORD_TOKEN, GUILD_ID, ORCHESTRATOR_URL, ORCHESTRATOR_API_KEY
```

### Required env vars

| Variable | Description |
|----------|-------------|
| `DISCORD_TOKEN` | Bot token from Discord Developer Portal |
| `GUILD_ID` | Server (guild) ID — commands synced here |
| `ORCHESTRATOR_URL` | Base URL of the orchestrator (e.g. `http://localhost:8888`) |
| `ORCHESTRATOR_API_KEY` | API key set in orchestrator config |
| `BOT_CHANNEL_ID` | *(optional)* Restrict commands to one channel |
| `EVENTS_CHANNEL_ID` | *(optional)* Channel for status change and crash loop alerts |
| `STATUS_CHANNEL_ID` | *(optional)* Channel for the live status embed |
| `OPERATOR_ROLE` | Role required for operator commands (default: `DCS Operator`) |
| `ADMIN_ROLE` | Role required for admin commands (default: `DCS Admin`) |
| `AUTO_RESTART_EXCLUDE` | *(optional)* Comma-separated instance names exempt from keepalive auto-start |
| `DCS_REGISTRATIONS_FILE` | Path to `registrations.json` for `/dcs register` (default: next to bot) |

### Discord bot permissions

- **Scopes:** `bot`, `applications.commands`
- **Bot permissions:** Send Messages, Embed Links, Attach Files, Read Message History, Manage Messages
- **Privileged intents:** Server Members (for role lookup)

---

## Running

```bash
python bot.py
```

The bot registers slash commands to the configured guild on startup.

---

## Architecture

```
bot.py                  — entry point, Bot + event handlers
config.py               — BotConfig dataclass + env loader
orchestrator_client.py  — async httpx wrapper for the orchestrator REST API
registrations.json      — Discord ID → DCS pilot name mappings (auto-created)
cogs/
  dcs.py                — DcsCog: all /dcs slash commands
  events.py             — EventsCog: SSE subscriber, status change + crash loop alerts
```

See `docs/bot-guide.md` for the full user-facing command reference.
See `docs/OpenAPI.yaml` for the orchestrator API spec.
