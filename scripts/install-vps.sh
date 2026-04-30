#!/usr/bin/env bash
# install-vps.sh — DCS Bullseye VPS installer
#
# Sets up the orchestrator, Discord bot, and frp server on a fresh Debian/Ubuntu VPS.
# Run as root. Re-running with --update pulls the latest code and restarts services.
#
# Usage:
#   bash install-vps.sh                  # fresh install (interactive)
#   bash install-vps.sh --update         # pull latest code, rebuild agent.zip, restart

set -euo pipefail

# ── Constants ─────────────────────────────────────────────────────────────────

REPO_URL="${REPO_URL:-https://github.com/IT-Dev-Group-6/dcs-bullseye.git}"
FRP_VERSION="0.61.0"
FRP_URL="https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_amd64.tar.gz"

PLATFORM_USER="dcs-platform"
INSTALL_DIR="/opt/dcs-platform"
DATA_DIR="/var/lib/dcs-platform"
REPO_DIR="${INSTALL_DIR}/repo"
ORCH_DIR="${INSTALL_DIR}/orchestrator"
BOT_DIR="${INSTALL_DIR}/discord-bot"
INSTALL_STATIC="${INSTALL_DIR}/install"

ORCH_CONFIG="/etc/dcs-orchestrator.json"
BOT_ENV="/etc/dcs-discord-bot.env"
FRP_CONFIG="/etc/frp/frps.toml"

# ── Helpers ───────────────────────────────────────────────────────────────────

step() { echo; echo "==> $*"; }
ok()   { echo "    OK  $*"; }
warn() { echo "    WARN $*"; }
die()  { echo; echo "ERROR: $*" >&2; exit 1; }

gen_secret() { openssl rand -hex 32; }

prompt() {
    local var="$1" msg="$2" default="${3:-}"
    if [[ -n "$default" ]]; then
        read -rp "    ${msg} [${default}]: " val
        echo "${val:-$default}"
    else
        local val=""
        while [[ -z "$val" ]]; do
            read -rp "    ${msg}: " val
        done
        echo "$val"
    fi
}

prompt_optional() {
    local msg="$1" default="${2:-}"
    read -rp "    ${msg} [${default}]: " val
    echo "${val:-$default}"
}

detect_public_ip() {
    curl -s --max-time 5 https://api.ipify.org 2>/dev/null || \
    curl -s --max-time 5 https://ifconfig.me 2>/dev/null || \
    echo ""
}

build_agent_zip() {
    step "Building agent.zip"
    local tmp
    tmp=$(mktemp -d)
    cp -r "${REPO_DIR}/agent" "${tmp}/agent"
    # Strip caches and venv
    rm -rf "${tmp}/agent/.venv" \
           "${tmp}/agent/__pycache__" \
           "${tmp}/agent/tests" \
           "${tmp}/agent/.pytest_cache"
    find "${tmp}/agent" -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    find "${tmp}/agent" -name "*.pyc" -delete 2>/dev/null || true
    mkdir -p "${INSTALL_STATIC}"
    (cd "${tmp}" && zip -qr "${INSTALL_STATIC}/agent.zip" agent/)
    rm -rf "${tmp}"
    # Compute SHA256 and store for bot env
    AGENT_ZIP_SHA256=$(sha256sum "${INSTALL_STATIC}/agent.zip" | awk '{print $1}')
    ok "agent.zip built — SHA256: ${AGENT_ZIP_SHA256}"
}

copy_install_script() {
    # Inject the VPS orchestrator URL as the default parameter value so users
    # don't need to pass -OrchestratorUrl when running the downloaded script.
    sed "s|##ORCHESTRATOR_URL##|${ORCHESTRATOR_URL}|g" \
        "${REPO_DIR}/scripts/install-agent.ps1" > "${INSTALL_STATIC}/install.ps1"
    ok "install.ps1 copied to ${INSTALL_STATIC}/ (URL: ${ORCHESTRATOR_URL})"
}

# ── Argument parsing ──────────────────────────────────────────────────────────

UPDATE=false
# Parse CLI args (supports: --update, --repo-url <url> or --repo-url=<url>)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --update)
      UPDATE=true
      shift
      ;;
    --repo-url)
      if [[ -n "${2-}" ]]; then
        REPO_URL="$2"
        shift 2
      else
        die "--repo-url requires a value"
      fi
      ;;
    --repo-url=*)
      REPO_URL="${1#--repo-url=}"
      shift
      ;;
    *)
      shift
      ;;
  esac
done

# ── Root check ────────────────────────────────────────────────────────────────

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash install-vps.sh"

# ── Distro check ─────────────────────────────────────────────────────────────

if ! command -v apt-get &>/dev/null; then
    die "This installer requires a Debian/Ubuntu system."
fi

# ═════════════════════════════════════════════════════════════════════════════
# UPDATE MODE — pull latest code, rebuild, restart
# ═════════════════════════════════════════════════════════════════════════════

if $UPDATE; then
    echo
    echo "DCS Bullseye — VPS Update"
    echo "========================="

    step "Pulling latest code"
    git -C "${REPO_DIR}" pull --ff-only
    ok "Repo updated"

    step "Updating orchestrator dependencies"
    "${ORCH_DIR}/.venv/bin/pip" install -q --upgrade -r "${REPO_DIR}/orchestrator/requirements.txt"
    # Sync orchestrator package files
    rsync -a --delete \
        --exclude=".venv" --exclude="__pycache__" --exclude="*.pyc" \
        "${REPO_DIR}/orchestrator/" "${ORCH_DIR}/"
    ok "Orchestrator updated"

    step "Updating discord-bot dependencies"
    "${BOT_DIR}/.venv/bin/pip" install -q --upgrade -r "${REPO_DIR}/discord-bot/requirements.txt"
    rsync -a --delete \
        --exclude=".venv" --exclude="__pycache__" --exclude="*.pyc" \
        "${REPO_DIR}/discord-bot/" "${BOT_DIR}/"
    ok "Discord bot updated"

    build_agent_zip
    copy_install_script

    # Update SHA256 in bot env
    if grep -q "AGENT_ZIP_SHA256" "${BOT_ENV}"; then
        sed -i "s|^AGENT_ZIP_SHA256=.*|AGENT_ZIP_SHA256=${AGENT_ZIP_SHA256}|" "${BOT_ENV}"
    else
        echo "AGENT_ZIP_SHA256=${AGENT_ZIP_SHA256}" >> "${BOT_ENV}"
    fi
    ok "AGENT_ZIP_SHA256 updated in ${BOT_ENV}"

    step "Restarting services"
    systemctl restart dcs-orchestrator dcs-discord-bot
    sleep 3
    systemctl is-active dcs-orchestrator && ok "dcs-orchestrator running"
    systemctl is-active dcs-discord-bot  && ok "dcs-discord-bot running"

    echo
    echo "Update complete."
    exit 0
fi

# ═════════════════════════════════════════════════════════════════════════════
# FRESH INSTALL
# ═════════════════════════════════════════════════════════════════════════════

echo
echo "DCS Bullseye — VPS Installer"
echo "============================="
echo
echo "This will install:"
echo "  - DCS Orchestrator    (FastAPI, port 8888)"
echo "  - DCS Discord Bot"
echo "  - frp reverse proxy server (port 7000)"
echo
echo "You will need:"
echo "  - A Discord bot token (from Discord Developer Portal)"
echo "  - Your Discord server (guild) ID"
echo "  - Channel IDs for the bot, status, and events channels"
echo
read -rp "Press Enter to begin, or Ctrl+C to cancel..."

# ── Collect configuration ─────────────────────────────────────────────────────

echo
echo "--- Configuration ---"
echo

PUBLIC_IP=$(detect_public_ip)
PUBLIC_IP=$(prompt "PUBLIC_IP" "VPS public IP or domain (e.g. 1.2.3.4 or yourdomain.com)" "$PUBLIC_IP")

# Determine orchestrator URL
if [[ "$PUBLIC_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    DEFAULT_URL="http://${PUBLIC_IP}:8888"
else
    DEFAULT_URL="https://${PUBLIC_IP}"
fi
ORCHESTRATOR_URL=$(prompt "ORCHESTRATOR_URL" "Orchestrator public URL (used by agents and install links)" "$DEFAULT_URL")
ORCHESTRATOR_URL="${ORCHESTRATOR_URL%/}"

DISCORD_TOKEN=$(prompt "DISCORD_TOKEN" "Discord bot token")
GUILD_ID=$(prompt "GUILD_ID" "Discord guild (server) ID")
BOT_CHANNEL_ID=$(prompt_optional "Bot channel ID (where /dcs commands work)" "")
STATUS_CHANNEL_ID=$(prompt_optional "Status channel ID (live status embed)" "")
EVENTS_CHANNEL_ID=$(prompt_optional "Events channel ID (alerts, crash loops)" "")
OPERATOR_ROLE=$(prompt_optional "Operator role name" "DCS Operator")
ADMIN_ROLE=$(prompt_optional "Admin role name" "DCS Admin")

echo
echo "--- Generating secrets ---"
API_KEY=$(gen_secret)
FRP_TOKEN=$(gen_secret)
ok "Orchestrator API key generated"
ok "frp token generated"

# ── System packages ───────────────────────────────────────────────────────────

step "Installing system packages"
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip git curl unzip zip rsync
ok "System packages installed"

# Python version check
PYTHON_BIN=$(command -v python3)
PY_VER=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [[ $PY_MAJOR -lt 3 ]] || [[ $PY_MAJOR -eq 3 && $PY_MINOR -lt 11 ]]; then
    die "Python 3.11+ required (found ${PY_VER}). Install a newer Python before running this script."
fi
ok "Python ${PY_VER} found"

# ── Create user and directories ───────────────────────────────────────────────

step "Creating platform user and directories"
if ! id "$PLATFORM_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$PLATFORM_USER"
    ok "User ${PLATFORM_USER} created"
else
    ok "User ${PLATFORM_USER} already exists"
fi

mkdir -p "${INSTALL_DIR}" "${DATA_DIR}" "${INSTALL_STATIC}" /etc/frp
chown "${PLATFORM_USER}:${PLATFORM_USER}" "${DATA_DIR}"
ok "Directories created"

# ── Clone repo ────────────────────────────────────────────────────────────────

step "Cloning repository"
if [[ -d "${REPO_DIR}/.git" ]]; then
    git -C "${REPO_DIR}" pull --ff-only
    ok "Repo updated"
else
    git clone --depth=1 "${REPO_URL}" "${REPO_DIR}"
    ok "Repo cloned"
fi

# ── Orchestrator ──────────────────────────────────────────────────────────────

step "Setting up orchestrator"
rsync -a --delete \
    --exclude=".venv" --exclude="__pycache__" --exclude="*.pyc" \
    "${REPO_DIR}/orchestrator/" "${ORCH_DIR}/"

if [[ ! -d "${ORCH_DIR}/.venv" ]]; then
    "$PYTHON_BIN" -m venv "${ORCH_DIR}/.venv"
fi
"${ORCH_DIR}/.venv/bin/pip" install -q --upgrade pip
"${ORCH_DIR}/.venv/bin/pip" install -q -r "${ORCH_DIR}/requirements.txt"
chown -R "${PLATFORM_USER}:${PLATFORM_USER}" "${ORCH_DIR}"
ok "Orchestrator installed"

# ── Discord bot ───────────────────────────────────────────────────────────────

step "Setting up discord bot"
rsync -a --delete \
    --exclude=".venv" --exclude="__pycache__" --exclude="*.pyc" \
    "${REPO_DIR}/discord-bot/" "${BOT_DIR}/"

if [[ ! -d "${BOT_DIR}/.venv" ]]; then
    "$PYTHON_BIN" -m venv "${BOT_DIR}/.venv"
fi
"${BOT_DIR}/.venv/bin/pip" install -q --upgrade pip
"${BOT_DIR}/.venv/bin/pip" install -q -r "${BOT_DIR}/requirements.txt"
chown -R "${PLATFORM_USER}:${PLATFORM_USER}" "${BOT_DIR}"
ok "Discord bot installed"

# ── frp server ────────────────────────────────────────────────────────────────

step "Installing frp server"
if [[ ! -f /usr/local/bin/frps ]]; then
    TMP_FRP=$(mktemp -d)
    curl -sSL "${FRP_URL}" | tar -xz -C "${TMP_FRP}" --strip-components=1
    install -m 755 "${TMP_FRP}/frps" /usr/local/bin/frps
    rm -rf "${TMP_FRP}"
    ok "frps ${FRP_VERSION} installed"
else
    ok "frps already installed ($(frps --version 2>/dev/null | head -1 || echo 'unknown version'))"
fi

# ── agent.zip + install.ps1 ───────────────────────────────────────────────────

build_agent_zip
copy_install_script
chown -R "${PLATFORM_USER}:${PLATFORM_USER}" "${INSTALL_STATIC}"

# ── Write config files ────────────────────────────────────────────────────────

step "Writing configuration files"

# Orchestrator config
cat > "${ORCH_CONFIG}" <<EOF
{
  "api_key": "${API_KEY}",
  "host": "0.0.0.0",
  "port": 8888,
  "db_path": "${DATA_DIR}/orchestrator.db",
  "log_level": "info",
  "public_url": "${ORCHESTRATOR_URL}",
  "frp_server_addr": "${PUBLIC_IP}",
  "frp_server_port": 7000,
  "frp_token": "${FRP_TOKEN}",
  "frp_port_range_start": 8800,
  "frp_port_range_end": 8899,
  "registration_enabled": true
}
EOF
chmod 640 "${ORCH_CONFIG}"
ok "Orchestrator config written to ${ORCH_CONFIG}"

# Discord bot env
cat > "${BOT_ENV}" <<EOF
DISCORD_TOKEN=${DISCORD_TOKEN}
GUILD_ID=${GUILD_ID}
ORCHESTRATOR_URL=http://127.0.0.1:8888
ORCHESTRATOR_API_KEY=${API_KEY}
BOT_CHANNEL_ID=${BOT_CHANNEL_ID}
STATUS_CHANNEL_ID=${STATUS_CHANNEL_ID}
EVENTS_CHANNEL_ID=${EVENTS_CHANNEL_ID}
OPERATOR_ROLE=${OPERATOR_ROLE}
ADMIN_ROLE=${ADMIN_ROLE}
INSTALLER_BASE_URL=${ORCHESTRATOR_URL}
AGENT_ZIP_SHA256=${AGENT_ZIP_SHA256}
DCS_REGISTRATIONS_FILE=${ORCH_DIR}/registrations.json
EXTERNAL_SERVERS=[]
AUTO_RESTART_EXCLUDE=
EOF
chmod 640 "${BOT_ENV}"
ok "Bot env written to ${BOT_ENV}"

# frp server config
cat > "${FRP_CONFIG}" <<EOF
bindPort = 7000
auth.method = "token"
auth.token = "${FRP_TOKEN}"

# Agent tunnels bound to localhost only — not exposed to the internet
proxyBindAddr = "127.0.0.1"

log.to = "/var/log/frps.log"
log.level = "info"
log.maxDays = 7
EOF
ok "frp config written to ${FRP_CONFIG}"

# ── Systemd services ──────────────────────────────────────────────────────────

step "Installing systemd services"

cat > /etc/systemd/system/frps.service <<EOF
[Unit]
Description=frp Reverse Proxy Server
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/frps -c ${FRP_CONFIG}
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/dcs-orchestrator.service <<EOF
[Unit]
Description=DCS Platform Orchestrator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${PLATFORM_USER}
Group=${PLATFORM_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=DCS_ORCHESTRATOR_CONFIG=${ORCH_CONFIG}
ExecStart=${ORCH_DIR}/.venv/bin/python -m orchestrator serve
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${DATA_DIR} ${ORCH_DIR}

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/dcs-discord-bot.service <<EOF
[Unit]
Description=DCS Platform Discord Bot
After=network-online.target dcs-orchestrator.service
Wants=network-online.target

[Service]
Type=simple
User=${PLATFORM_USER}
Group=${PLATFORM_USER}
WorkingDirectory=${BOT_DIR}
EnvironmentFile=${BOT_ENV}
ExecStart=${BOT_DIR}/.venv/bin/python bot.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${ORCH_DIR}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable frps dcs-orchestrator dcs-discord-bot
ok "Services installed and enabled"

# ── Start services ────────────────────────────────────────────────────────────

step "Starting services"
systemctl restart frps
sleep 1
systemctl restart dcs-orchestrator
sleep 2
systemctl restart dcs-discord-bot
sleep 2

FRPS_STATUS=$(systemctl is-active frps           2>/dev/null || echo "failed")
ORCH_STATUS=$(systemctl is-active dcs-orchestrator 2>/dev/null || echo "failed")
BOT_STATUS=$(systemctl is-active dcs-discord-bot  2>/dev/null || echo "failed")

[[ "$FRPS_STATUS" == "active" ]] && ok "frps running"            || warn "frps status: ${FRPS_STATUS}"
[[ "$ORCH_STATUS" == "active" ]] && ok "dcs-orchestrator running" || warn "dcs-orchestrator status: ${ORCH_STATUS}"
[[ "$BOT_STATUS"  == "active" ]] && ok "dcs-discord-bot running"  || warn "dcs-discord-bot status: ${BOT_STATUS}"

# ── Summary ───────────────────────────────────────────────────────────────────

echo
echo "======================================================"
echo " DCS Bullseye VPS Install Complete"
echo "======================================================"
echo
echo " Orchestrator:    ${ORCHESTRATOR_URL}"
echo " API key:         ${API_KEY}"
echo " frp token:       ${FRP_TOKEN}"
echo
echo " Config files:"
echo "   Orchestrator:  ${ORCH_CONFIG}"
echo "   Bot env:       ${BOT_ENV}"
echo "   frp:           ${FRP_CONFIG}"
echo
echo " Install endpoint (for community hosts):"
echo "   ${ORCHESTRATOR_URL}/install/install.ps1"
echo
echo " Next steps:"
echo "   1. Open port 8888 (orchestrator) and 7000 (frp) in your firewall"
echo "   2. Verify the bot is online in Discord"
echo "   3. Use /dcs invite in Discord to generate an invite code"
echo "   4. Send the invite command to your Windows DCS host to install the agent"
echo
if [[ "$ORCHESTRATOR_URL" == http://* ]]; then
    echo " NOTE: The orchestrator is running over plain HTTP."
    echo "       Community host installs require HTTPS."
    echo "       Set up nginx + certbot for a domain, then update:"
    echo "         ${ORCH_CONFIG}  (public_url field)"
    echo "         ${BOT_ENV}      (INSTALLER_BASE_URL field)"
    echo "       Then: systemctl restart dcs-orchestrator dcs-discord-bot"
    echo
fi
echo " To update in future: bash install-vps.sh --update"
echo "======================================================"
