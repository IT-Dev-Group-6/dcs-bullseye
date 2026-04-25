# DCS Platform Bot Guide

All commands are under `/dcs` and must be used in the designated bot channel.

---

## Permissions

| Role | Access |
|------|--------|
| Everyone | `status`, `hosts`, `stats`, `mystats`, `register` |
| **DCS Operator** | All commands except `reboot` and `update` |
| **DCS Admin** | All commands including `reboot` and `update` |

The Operator and Admin role names are configured by the server admin (`OPERATOR_ROLE` and `ADMIN_ROLE` env vars).

---

## Server Control

### `/dcs status [instance]`
Shows the current status of one or all DCS instances — running/stopped, current mission, map, player count, and uptime. Leave the instance blank to see all at once.

### `/dcs start <instance>`
Starts a DCS server instance.

### `/dcs stop <instance>`
Stops a DCS server instance.

### `/dcs restart <instance>`
Restarts a DCS server instance.

### `/dcs restartall`
Restarts every instance that is currently running. Useful after uploading a new mission or pushing a config change across all servers.

---

## Missions

### `/dcs mission <instance> <filename>`
Loads a mission file and restarts the server. The filename autocompletes from the Active Missions folders across **all** hosts, sorted by most-played. If the selected mission lives on a different host than the target instance, it is automatically downloaded and transferred before the load — no manual copying needed.

### `/dcs upload <file>`
Uploads a `.miz` file from your computer directly to the Active Missions folder on the host. Drag and drop the file into Discord when prompted.

### `/dcs download <filename>`
Downloads a `.miz` file from the Active Missions folder to your computer. Useful for pulling a mission to edit it and re-upload. The file is sent as an ephemeral attachment (only you see it). 25 MB Discord limit applies.

### `/dcs delete <filename>`
Removes a mission from the Active Missions folder. The file is backed up to a `Backup_Missions` subfolder on the host before deletion — it is not permanently lost.

---

## Server Management

### `/dcs logs <instance>`
Fetches a log bundle from the DCS server and uploads it as a file. Useful for diagnosing crashes or mission errors.

### `/dcs password <instance> <password>`
Changes the multiplayer password for a DCS instance and restarts it. The response is ephemeral so the password is never shown in the channel.

### `/dcs resetpersist <instance>`
Backs up and clears the persistence save files for an instance (carrier positions, warehouse states, etc.). Use this to reset a campaign to its initial state. Persistence files live in `{Missions folder}\Saves\`. Requires a confirmation button click before executing.

### `/dcs reboot <host>`
Reboots a specific Windows host machine. Requires **DCS Admin** role and confirmation before executing. The machine will come back online automatically and the DCS Agent and tunnel services start on boot.

### `/dcs update <host>`
Stops all DCS servers on a specific host, runs the DCS World updater, then restarts them automatically. Requires **DCS Admin** role and confirmation before executing. The full process takes 10–60 minutes depending on patch size. Progress is reported in the status channel. If DCS is already up to date the updater exits immediately and servers restart normally.

---

## Scheduling

Scheduling lets the bot automatically manage server hours, idle restarts, and mission rotation without manual intervention. Each setting is independent — you can set just idle restart, just a time window, or combine all three. Settings persist across agent restarts.

### `/dcs schedule-view <instance>`
Shows the current schedule for an instance — open/close hours, idle restart threshold, and mission playlist if set.

### `/dcs schedule-idle <instance> <minutes>`
Automatically restarts the server after it has had zero players for the specified number of minutes. Set to `0` to disable.

**Example:** `/dcs schedule-idle MyServer 30` — restart after 30 minutes with no players.

### `/dcs schedule-hours <instance> [open_time] [close_time] [timezone] [days]`
Sets automatic open and close times for a server. The server starts at `open_time` if stopped and shuts down at `close_time` if empty. Any field can be set independently — you can update just the timezone without re-entering times. Overnight windows (e.g. `18:00` → `02:00`) are handled correctly.

- `timezone` — pick from the autocomplete list or type any IANA timezone (default `UTC`)
- `days` — pick a preset from the autocomplete list or type comma-separated day abbreviations, e.g. `fri,sat,sun` (default: every day)

**Example:** `/dcs schedule-hours MyServer 18:00 02:00 America/New_York fri,sat,sun`

### `/dcs schedule-playlist <instance> <mission_1> [mission_2] ... [mission_5] [rotate_minutes]`
Sets a mission rotation playlist with up to 5 missions. Each slot has its own autocomplete pulling from the instance's Active Missions folder — already-selected missions are filtered out of subsequent slots. If `rotate_minutes` is set, the server automatically loads the next mission after that many wall-clock minutes. Set `rotate_minutes` to `0` to keep the playlist without auto-rotation.

**Example:** Select `mission_1`, then `mission_2`, etc. from the autocomplete dropdowns.

### `/dcs schedule-clear <instance>`
Removes all scheduling from an instance. The server will no longer auto-start, auto-stop, idle-restart, or rotate missions.

---

## Analytics

### `/dcs stats [instance] [period]`
Shows server-wide player analytics. Optional filters: a specific instance name, and a time period (`7d`, `30d`, or all time — default is 7 days).

Displays:
- **Sessions** — total player join events
- **Unique Players** — distinct pilot names seen
- **Missions Played** — distinct missions people actually played on
- **Top Players** — top 5 by session count
- **Top Missions** — top 5 missions by sessions
- **Top Maps** — top 5 maps by sessions
- **Peak Hours** — top 8 active hours shown as a bar chart in EST and PST

### `/dcs register <dcs_name>`
Links your Discord account to your DCS pilot name. Only needs to be done once. Your DCS name is case-sensitive — use it exactly as it appears in-game. Required before using `/dcs mystats`.

### `/dcs mystats [period]`
Shows your personal stats based on your registered DCS pilot name. Response is ephemeral (only you see it). Displays your sessions, missions and maps played, favourite mission and map, and your personal peak hours.

---

## Administration

### `/dcs hosts`
Lists all registered host machines, their agent connection status, and last seen time.

### `/dcs invite [host_name] [expires_in_hours]`
Generates a one-time invite code for a new community host to join the platform. The response is ephemeral and includes the full PowerShell install command ready to send. Codes expire in 24 hours by default; set `expires_in_hours` to any value between 1 and 168 (7 days max).

### `/dcs clear`
Deletes recent bot messages from the channel. Useful for cleaning up after a busy session.

---

## Automatic Features

### Live Status Embed
A status embed is pinned in the designated status channel and refreshes automatically every 5 minutes. It shows all managed instances plus any external servers configured by the admin (TCP ping only — no agent required for those).

### Daily Restart
If a mission has been running for more than 48 hours, the server will automatically restart at **5:00 AM Eastern** to clear memory and apply any pending changes. This only triggers if the mission time threshold is met.

### Crash Loop Detection
If an instance crashes and restarts **3 or more times within 10 minutes**, the bot will post a **Crash loop detected** alert in the notification channel. The alert fires once per loop and resets automatically when the instance recovers.

---

## Notes

- All server control commands (start, stop, restart, mission load, etc.) run as **background jobs**. The bot will report when they complete or fail.
- `/dcs reboot`, `/dcs delete`, and `/dcs resetpersist` require a confirmation button click before executing.
- The bot will only respond in the configured bot channel. Commands used elsewhere are silently ignored.
- Analytics data is collected automatically by the agent on each managed host. Stats will be empty until the agent has been running for at least one poll cycle (60 seconds).
