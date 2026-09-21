#!/usr/bin/env bash
# Idempotent installer, run ON the host from a clone of this repo:
#
#   git clone <repo> ~/ibkr-src && cd ~/ibkr-src && ./deploy/setup.sh
#
# Installs system deps, the uv project, IB Gateway, IBC, the sudoers rule the
# ops bot needs, and the 5 systemd units. Does NOT start the Gateway (that needs
# your credentials + first 2FA). Never prints secrets.
#
# Everything is derived from the invoking user; override via environment:
#   PROJECT_DIR=...  IBC_PATH=...  DEPLOY_TZ=Asia/Singapore  ./deploy/setup.sh
set -euo pipefail

USER_NAME="$(id -un)"
HOME_DIR="${HOME}"
# The repo this script was run from — no separate scp step needed.
SRC="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_DIR="${PROJECT_DIR:-${HOME_DIR}/ibkr-notifier}"
TWS_PATH="${TWS_PATH:-${HOME_DIR}/Jts}"
IBC_PATH="${IBC_PATH:-/opt/ibc}"
IBC_HOME="${IBC_HOME:-${HOME_DIR}/ibc}"
LOG_PATH="${IBC_HOME}/logs"
# Timezone for DAILY_SUMMARY_TIME and the IBC daily restart. UTC unless set.
DEPLOY_TZ="${DEPLOY_TZ:-UTC}"
UNITS=(xvfb x11vnc ibc-gateway ibkr-notifier ibkr-ops-bot)

log() { echo ">>> $*"; }

if [ "${USER_NAME}" = "root" ]; then
  echo "Run this as the unprivileged user that will own the services, not root." >&2
  exit 1
fi

log "user=${USER_NAME}  project=${PROJECT_DIR}  tz=${DEPLOY_TZ}  src=${SRC}"

# --- 1. System packages ----------------------------------------------------
log "Installing apt packages (xvfb, x11vnc, unzip, curl)..."
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  xvfb x11vnc unzip curl ca-certificates >/dev/null
log "apt packages done."

# --- 2. uv -----------------------------------------------------------------
if [ ! -x "${HOME_DIR}/.local/bin/uv" ]; then
  log "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="${HOME_DIR}/.local/bin/uv"
log "uv: $(${UV} --version)"

# --- 3. Project (uv) -------------------------------------------------------
log "Setting up uv project at ${PROJECT_DIR}..."
mkdir -p "${PROJECT_DIR}"
cp "${SRC}/ibkr_telegram_notifier.py" \
   "${SRC}/ibkr_ops_bot.py" \
   "${SRC}/ibkr_report.py" \
   "${SRC}/pyproject.toml" \
   "${SRC}/env.example" "${PROJECT_DIR}/"
if [ -f "${SRC}/uv.lock" ]; then cp "${SRC}/uv.lock" "${PROJECT_DIR}/"; fi
# README placeholder so pyproject's readme field resolves.
[ -f "${PROJECT_DIR}/README.md" ] || echo "# IBKR Tele Notifier" > "${PROJECT_DIR}/README.md"
cd "${PROJECT_DIR}"
"${UV}" sync --no-dev
log "Verifying imports..."
"${UV}" run --no-dev python -c "import ib_async, httpx, dotenv; print('imports OK')"
# Create .env from example if absent; user fills it in. chmod 600.
if [ ! -f "${PROJECT_DIR}/.env" ]; then
  cp "${PROJECT_DIR}/env.example" "${PROJECT_DIR}/.env"
  log "Created ${PROJECT_DIR}/.env from the template — FILL IT IN before starting."
fi
chmod 600 "${PROJECT_DIR}/.env"

# --- 4. IB Gateway (standalone, unattended) --------------------------------
# IBC expects the OFFLINE Gateway at ${TWS_PATH}/ibgateway/<major>/jars. The
# installer lays files flat into whatever -dir we give, so we install into a
# temp dir, read the version from its "IB Gateway X.YZ.desktop" marker, then
# move it to the versioned path. (The bundled JRE lives in ~/.local/share, not
# inside the install dir, so moving the dir is safe.)
if ! ls "${TWS_PATH}"/ibgateway/*/jars >/dev/null 2>&1; then
  log "Downloading IB Gateway standalone (~335MB)..."
  curl -sSL -o /tmp/ibgw.sh \
    https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh
  chmod +x /tmp/ibgw.sh
  rm -rf /tmp/ibgw_install
  log "Installing IB Gateway (unattended)..."
  /tmp/ibgw.sh -q -dir /tmp/ibgw_install
  rm -f /tmp/ibgw.sh
  VER=$(ls /tmp/ibgw_install | grep -oE '[0-9]+\.[0-9]+' | head -1)   # e.g. 10.45
  TWS_MAJOR_VRSN=$(echo "${VER}" | tr -d '.')                          # e.g. 1045
  mkdir -p "${TWS_PATH}/ibgateway"
  rm -rf "${TWS_PATH}/ibgateway/${TWS_MAJOR_VRSN}"
  mv /tmp/ibgw_install "${TWS_PATH}/ibgateway/${TWS_MAJOR_VRSN}"
else
  VRSN_DIR=$(ls -1 "${TWS_PATH}/ibgateway" | sort -V | tail -1)
  TWS_MAJOR_VRSN="${VRSN_DIR}"
  log "IB Gateway already installed, skipping download."
fi
log "IB Gateway TWS_MAJOR_VRSN=${TWS_MAJOR_VRSN}"

# --- 5. IBC ----------------------------------------------------------------
if [ ! -f "${IBC_PATH}/scripts/ibcstart.sh" ]; then
  log "Fetching latest IBC release URL..."
  IBC_URL=$(curl -sSL https://api.github.com/repos/IbcAlpha/IBC/releases/latest \
    | grep -oE '"browser_download_url": *"[^"]*IBCLinux[^"]*\.zip"' \
    | head -1 | sed -E 's/.*"(https[^"]+)"/\1/')
  log "IBC: ${IBC_URL}"
  curl -sSL -o /tmp/ibc.zip "${IBC_URL}"
  sudo mkdir -p "${IBC_PATH}"
  sudo unzip -o -q /tmp/ibc.zip -d "${IBC_PATH}"
  sudo chmod -R o+rX "${IBC_PATH}"
  sudo chmod +x "${IBC_PATH}"/*.sh "${IBC_PATH}"/scripts/*.sh
  rm -f /tmp/ibc.zip
else
  log "IBC already installed, skipping."
fi

# --- 6. IBC config + env + launcher ----------------------------------------
log "Placing IBC config, env, launcher..."
mkdir -p "${IBC_HOME}" "${LOG_PATH}"
# Don't clobber an existing config that may already hold credentials.
if [ ! -f "${IBC_HOME}/config.ini" ]; then
  sed -e "s#@AUTO_RESTART_TIME@#${AUTO_RESTART_TIME:-12:00}#" \
      "${SRC}/deploy/config.ini" > "${IBC_HOME}/config.ini"
  log "Created ${IBC_HOME}/config.ini — set IbLoginId/IbPassword before starting."
fi
chmod 600 "${IBC_HOME}/config.ini"
sed -e "s#@IBC_PATH@#${IBC_PATH}#g" \
    -e "s#@IBC_HOME@#${IBC_HOME}#g" \
    -e "s#@TWS_PATH@#${TWS_PATH}#g" \
    "${SRC}/deploy/run-gateway.sh.tmpl" > "${IBC_HOME}/run-gateway.sh"
chmod +x "${IBC_HOME}/run-gateway.sh"
cat > "${IBC_HOME}/ibc.env" <<EOF
TWS_MAJOR_VRSN=${TWS_MAJOR_VRSN}
TRADING_MODE=live
IBC_PATH=${IBC_PATH}
TWS_PATH=${TWS_PATH}
TWS_SETTINGS_PATH=${TWS_PATH}
IBC_INI=${IBC_HOME}/config.ini
LOG_PATH=${LOG_PATH}
EOF

# --- 7. VNC password -------------------------------------------------------
# x11vnc.service requires this file; without it the unit fails on first boot.
# VNC is bound to localhost (SSH tunnel only) and used solely for the one-time
# Gateway GUI configuration.
if [ ! -f "${HOME_DIR}/.vnc/passwd" ]; then
  mkdir -p "${HOME_DIR}/.vnc"
  if [ -n "${VNC_PASSWORD:-}" ]; then
    x11vnc -storepasswd "${VNC_PASSWORD}" "${HOME_DIR}/.vnc/passwd" >/dev/null 2>&1
    log "VNC password set from \$VNC_PASSWORD."
  else
    GEN="$(head -c 9 /dev/urandom | base64 | tr -d '/+=' | cut -c1-8)"
    x11vnc -storepasswd "${GEN}" "${HOME_DIR}/.vnc/passwd" >/dev/null 2>&1
    log "Generated a random VNC password. Retrieve it with:"
    log "    x11vnc -showrfbauth ${HOME_DIR}/.vnc/passwd"
  fi
  chmod 600 "${HOME_DIR}/.vnc/passwd"
fi

# --- 8. sudoers rule for the ops bot ---------------------------------------
# /resume and /pause shell out to systemctl. Scope the NOPASSWD grant to exactly
# those verbs on the gateway unit — never a blanket NOPASSWD: ALL.
SUDOERS=/etc/sudoers.d/ibkr-ops-bot
log "Installing ${SUDOERS}..."
sudo tee "${SUDOERS}" >/dev/null <<EOF
# Installed by ibkr-tele-notifier deploy/setup.sh.
# Lets the ops bot bounce IB Gateway (and remote-deploy.sh restart the services)
# without a password. Deliberately narrow: no other units, no other verbs.
${USER_NAME} ALL=(root) NOPASSWD: /usr/bin/systemctl start ibc-gateway, \\
  /usr/bin/systemctl stop ibc-gateway, \\
  /usr/bin/systemctl restart ibc-gateway, \\
  /usr/bin/systemctl reset-failed ibc-gateway, \\
  /usr/bin/systemctl restart ibkr-notifier, \\
  /usr/bin/systemctl restart ibkr-ops-bot, \\
  /usr/bin/systemctl restart ibkr-notifier ibkr-ops-bot
EOF
sudo chmod 440 "${SUDOERS}"
sudo visudo -cf "${SUDOERS}" >/dev/null || { echo "sudoers file invalid — removing"; sudo rm -f "${SUDOERS}"; exit 1; }

# --- 9. systemd units ------------------------------------------------------
log "Rendering + installing systemd units..."
for u in "${UNITS[@]}"; do
  sed -e "s#@USER@#${USER_NAME}#g" \
      -e "s#@HOME@#${HOME_DIR}#g" \
      -e "s#@PROJECT_DIR@#${PROJECT_DIR}#g" \
      -e "s#@IBC_HOME@#${IBC_HOME}#g" \
      -e "s#@UV@#${UV}#g" \
      -e "s#@TZ@#${DEPLOY_TZ}#g" \
      "${SRC}/deploy/${u}.service.tmpl" | sudo tee "/etc/systemd/system/${u}.service" >/dev/null
done
sudo systemctl daemon-reload

# --- Migrate the legacy channel switch -------------------------------------
# Older installs selected the test channel with a systemd drop-in in /etc.
# That is now channel.env in the project dir. Carry the current setting over so
# a host on TEST stays on TEST, then drop the obsolete file.
LEGACY_DROPIN=/etc/systemd/system/ibkr-notifier.service.d/test-channel.conf
if [ -f "${LEGACY_DROPIN}" ]; then
  if grep -q '\.env\.test' "${LEGACY_DROPIN}"; then
    printf 'ENV_FILE=%s/.env.test\n' "${PROJECT_DIR}" > "${PROJECT_DIR}/channel.env"
    log "Migrated legacy TEST-channel drop-in -> ${PROJECT_DIR}/channel.env"
  else
    : > "${PROJECT_DIR}/channel.env"
    log "Migrated legacy drop-in (PROD) -> ${PROJECT_DIR}/channel.env"
  fi
  sudo rm -f "${LEGACY_DROPIN}"
  sudo rmdir /etc/systemd/system/ibkr-notifier.service.d 2>/dev/null || true
  sudo systemctl daemon-reload
fi

# Enable all so they come back on boot. Start only the display + VNC now;
# Gateway/notifier start after you've put in credentials + done first login.
sudo systemctl enable "${UNITS[@]}" >/dev/null 2>&1

# Start the display + VNC, but NEVER restart a display that is already up:
# IB Gateway lives on :99, so bouncing Xvfb on a running host kills the Gateway
# and forces a fresh login (i.e. a 2FA push). Re-running this installer to pick
# up unit-template edits must stay safe on a live host.
if systemctl is-active --quiet xvfb; then
  log "xvfb already running — left alone (restarting it would kill IB Gateway)."
else
  sudo systemctl start xvfb
  log "xvfb started."
fi
if systemctl is-active --quiet x11vnc; then
  log "x11vnc already running — left alone."
else
  sudo systemctl start x11vnc
  log "x11vnc started."
fi

# Units were re-rendered above; a running notifier/ops-bot needs a restart to
# pick them up. The gateway is deliberately NOT touched (that would cost a 2FA).
RESTART=()
for u in ibkr-notifier ibkr-ops-bot; do
  if systemctl is-active --quiet "$u"; then RESTART+=("$u"); fi
done
if [ ${#RESTART[@]} -gt 0 ]; then
  log "Restarting to pick up re-rendered units: ${RESTART[*]}"
  sudo systemctl restart "${RESTART[@]}"
  sleep 3
  for u in "${RESTART[@]}"; do log "  ${u}: $(systemctl is-active "$u")"; done
  log "NOTE: ibc-gateway was NOT restarted (that would trigger a 2FA push)."
  log "SETUP COMPLETE."
  exit 0
fi

log "Gateway/notifier/ops-bot enabled but NOT started yet."
log ""
log "NEXT STEPS:"
log "  1. Fill in ${PROJECT_DIR}/.env          (Telegram tokens, chat ids)"
log "  2. Fill in ${IBC_HOME}/config.ini       (IbLoginId, IbPassword)"
log "  3. Configure the Gateway GUI once over VNC (see deploy/README.md)"
log "  4. sudo systemctl start ibc-gateway ibkr-notifier ibkr-ops-bot"
log "SETUP COMPLETE."
