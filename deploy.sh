#!/usr/bin/env bash
# One-shot deploy of the IBKR notifier to its host.
# Pushes the code, syncs deps, optionally switches channel, restarts services.
#
#   ./deploy.sh          push code, keep current channel (prod or test)
#   ./deploy.sh prod     push code + switch to PRODUCTION channel
#   ./deploy.sh test     push code + switch to OPS/TEST channel
#
# Host coordinates come from deploy/deploy.env (see deploy/deploy.env.example).
# Secrets (.env, .env.test, IBC config.ini) live only on the host and are never
# touched. systemd units + IBC config are NOT pushed here (they rarely change);
# re-run deploy/setup.sh on the host after editing deploy/*.service.
set -euo pipefail
cd "$(dirname "$0")"

CONF=deploy/deploy.env
if [ ! -f "$CONF" ]; then
  echo "error: $CONF not found." >&2
  echo "  cp deploy/deploy.env.example $CONF   # then edit it" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$CONF"

MODE="${1:-keep}"
case "$MODE" in prod|test|keep) ;; *)
  echo "usage: $0 [prod|test|keep]" >&2; exit 1 ;;
esac

: "${REMOTE_DIR:?set REMOTE_DIR in $CONF}"
: "${TRANSPORT:?set TRANSPORT in $CONF}"

FILES=(ibkr_telegram_notifier.py ibkr_ops_bot.py ibkr_report.py pyproject.toml
       deploy/remote-deploy.sh)

case "$TRANSPORT" in
  gcloud)
    : "${GCE_INSTANCE:?set GCE_INSTANCE in $CONF}"
    : "${GCE_ZONE:?set GCE_ZONE in $CONF}"
    GC_ARGS=(--zone="$GCE_ZONE")
    if [ -n "${GCP_PROJECT:-}" ]; then GC_ARGS+=(--project="$GCP_PROJECT"); fi
    push() { gcloud compute scp "${FILES[@]}" "$GCE_INSTANCE:$REMOTE_DIR/" "${GC_ARGS[@]}" >/dev/null; }
    run()  { gcloud compute ssh "$GCE_INSTANCE" "${GC_ARGS[@]}" --command="$1"; }
    TARGET_DESC="$GCE_INSTANCE ($GCE_ZONE)"
    ;;
  ssh)
    : "${SSH_TARGET:?set SSH_TARGET in $CONF}"
    push() { scp -q "${FILES[@]}" "$SSH_TARGET:$REMOTE_DIR/"; }
    run()  { ssh "$SSH_TARGET" "$1"; }
    TARGET_DESC="$SSH_TARGET"
    ;;
  *) echo "unknown TRANSPORT: $TRANSPORT (use ssh|gcloud)" >&2; exit 1 ;;
esac

echo "→ pushing code to $TARGET_DESC ($MODE)…"
push
run "chmod +x $REMOTE_DIR/remote-deploy.sh && $REMOTE_DIR/remote-deploy.sh $MODE" 2>&1 | tail -3
