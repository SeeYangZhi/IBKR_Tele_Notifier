#!/usr/bin/env bash
# Runs ON the host (pushed + invoked by ../deploy.sh). Syncs deps, optionally
# switches the notifier between PROD and TEST(ops) channel, restarts services,
# prints a one-line status. Never touches secrets (.env, .env.test, config.ini).
#
# Channel switching writes channel.env in the project dir, which the notifier
# unit loads via `EnvironmentFile=-`. That is a plain file owned by the service
# user, so this script needs sudo for nothing but `systemctl restart` — which is
# all the sudoers rule installed by setup.sh grants. See SECURITY.md.
set -euo pipefail

# This script is deployed INTO the project dir, so its own location is the
# project dir — no path needs hardcoding.
REMOTE="$(cd "$(dirname "$0")" && pwd)"
NOTIFIER_UNIT="${NOTIFIER_UNIT:-ibkr-notifier}"
OPS_UNIT="${OPS_UNIT:-ibkr-ops-bot}"
CHANNEL_ENV="$REMOTE/channel.env"
MODE="${1:-keep}"

UV="${UV:-$HOME/.local/bin/uv}"
command -v "$UV" >/dev/null 2>&1 || UV=uv

cd "$REMOTE"
"$UV" sync --no-dev >/tmp/uvsync.log 2>&1 || { echo "uv sync FAILED:"; tail -5 /tmp/uvsync.log; exit 1; }

case "$MODE" in
  prod)  : > "$CHANNEL_ENV" ;;                       # empty -> notifier loads .env
  test)  printf 'ENV_FILE=%s/.env.test\n' "$REMOTE" > "$CHANNEL_ENV" ;;
  keep)  : ;;
  *) echo "unknown mode: $MODE (use prod|test|keep)"; exit 1 ;;
esac

sudo -n systemctl restart "$NOTIFIER_UNIT" "$OPS_UNIT"
sleep 5

# Report the API port the notifier is actually configured for.
PORT="$(grep -hE '^IB_PORT=' "$REMOTE/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
PORT="${PORT:-4001}"
if [ -s "$CHANNEL_ENV" ]; then CH="TEST (ops channel)"; else CH="PROD"; fi
echo "channel=$CH | notifier=$(systemctl is-active "$NOTIFIER_UNIT") | ops-bot=$(systemctl is-active "$OPS_UNIT") | api${PORT}=$(ss -ltn | grep -q ":${PORT}" && echo up || echo down)"
