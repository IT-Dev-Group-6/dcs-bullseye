# Community Host Setup Guide

## Prerequisites

The platform VPS must already be running before community hosts can join. If you are the platform admin and haven't set up the VPS yet, run `scripts/install-vps.sh` first — see the [VPS Setup wiki page](https://github.com/IT-Dev-Group-6/dcs-bullseye/wiki/VPS-Setup).

---

## Before You Start

1. **Install DCS World Dedicated Server**
   Download from the [Eagle Dynamics website](https://www.digitalcombatsimulator.com/en/downloads/world/server/).

2. **Run DCS server at least once**
   Launch it, let it get to the main menu or start a mission, then close it.
   This creates the Saved Games profile the installer needs to find, and generates `serverSettings.lua` which is required for mission loading.

---

## Install the Agent

Get your invite code from the server admin via Discord — they will send you the complete install command. It looks like this:

```powershell
$f="$env:TEMP\install-agent.ps1"; iwr -UseBasicParsing <ORCHESTRATOR_URL>/install/install.ps1 -OutFile $f; powershell -ExecutionPolicy Bypass -File $f -InviteCode GOON-XXXX-XXXX-XXXX -OrchestratorUrl <ORCHESTRATOR_URL>
```

Run the exact command the admin sends you — don't modify the URL or invite code.

The installer will prompt you for:
- A **host name** shown in Discord (e.g. "East Coast BBQ")
- A **server instance name** shown in Discord commands (e.g. "MySquadron Server")
- Your DCS Saved Games profile key if it can't be detected automatically (e.g. `DCS.openbeta_server`)

The installer handles everything else automatically:
- Downloads and installs the DCS Agent and Python environment
- Creates your Active Missions folder inside your DCS Saved Games profile
- Sets up the encrypted tunnel back to the central server (no public IP needed)
- Registers your host so it appears in Discord
- Installs the DCS Lua status hook for live player and mission info
- Creates desktop shortcuts for starting your server and updating the agent
- Removes the default DCS World Server desktop shortcut

---

## Mission Files

Your Active Missions folder is created at:
```
C:\Users\[you]\Saved Games\[DCS profile]\Active Missions\
```

Drop `.miz` files here and they'll show up in `/dcs mission`. You can also use `/dcs upload` from Discord to push files directly, or `/dcs copy-mission` to stage a mission from any server's Missions folder into the Active Missions library.

Mission files are automatically transferred between hosts — if you run `/dcs mission` on your server and pick a mission that lives on a different host, it will be fetched and copied over automatically before loading.

---

## After Install

- Your server will appear in Discord and can be controlled via `/dcs start`, `/dcs stop`, etc.
- The DCSAgent and tunnel services start automatically on Windows boot.
- **Restart DCS after install** so the Lua status hook activates (enables live player/mission info in Discord).

---

## Updating the Agent

If the server admin pushes an update, run this in **PowerShell as Administrator**:

```powershell
$f="$env:TEMP\install-agent.ps1"; iwr -UseBasicParsing <ORCHESTRATOR_URL>/install/install.ps1 -OutFile $f; powershell -ExecutionPolicy Bypass -File $f -Update -OrchestratorUrl <ORCHESTRATOR_URL>
```

Replace `<ORCHESTRATOR_URL>` with the URL from your original install command. This pulls the latest agent code and restarts the service. No re-registration needed.

You can also use the **Update DCS Agent** desktop shortcut created during install — it remembers the URL automatically.

---

## Uninstalling

To remove the agent from your machine, run this in **PowerShell as Administrator**:

```powershell
$f="$env:TEMP\uninstall-agent.ps1"; iwr -UseBasicParsing <ORCHESTRATOR_URL>/install/uninstall.ps1 -OutFile $f; powershell -ExecutionPolicy Bypass -File $f
```

This stops and removes the DCSAgent and tunnel services, deletes the Task Scheduler tasks, removes the Lua hook, and deletes the install directory. Your DCS Saved Games profile and mission files are not touched.

After uninstalling, ask an admin to run `/dcs remove-host` in Discord to remove your host from the platform.
