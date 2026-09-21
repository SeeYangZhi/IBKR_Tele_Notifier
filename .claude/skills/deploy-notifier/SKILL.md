---
name: deploy-notifier
description: Deploy code changes to the IBKR→Telegram notifier running on its host. Use whenever you edit ibkr_telegram_notifier.py, ibkr_ops_bot.py, or ibkr_report.py and need to push to the host and restart, or switch the notifier between the production and ops/test Telegram channels. Prefer ./deploy.sh over hand-running scp/ssh (fewer round-trips, fewer tokens).
---

# Deploy the IBKR notifier

The notifier runs 24/7 under systemd on a Linux host. To deploy code changes, **always run `./deploy.sh`** from the repo root — don't hand-run `scp`/`ssh` (it wastes round-trips and tokens).

Host coordinates live in `deploy/deploy.env` (gitignored, created from `deploy/deploy.env.example`). If it is missing, `deploy.sh` says so and exits; ask the user for the host rather than guessing.

## Usage

```bash
./deploy.sh         # push code, keep current channel mode, restart
./deploy.sh prod    # push code + switch notifier to the PRODUCTION channel
./deploy.sh test    # push code + switch notifier to the OPS/TEST channel
```

It pushes `ibkr_telegram_notifier.py`, `ibkr_ops_bot.py`, `ibkr_report.py`, `pyproject.toml`, and `deploy/remote-deploy.sh`; runs `uv sync`; applies the channel mode; restarts `ibkr-notifier` + `ibkr-ops-bot`; and prints one status line (channel / service states / API port).

## Before deploying

Run the checks — CI runs the same ones:

```bash
uv run pytest && uv run ruff check .
```

## Channel modes

- **prod** — notifier loads `.env` → production channel.
- **test** — `channel.env` in the project dir sets `ENV_FILE=.env.test` → ops/test channel (separate bot, own `IB_CLIENT_ID`, own `seen_execids*.json`). Use for testing without hitting prod.

Each mode has its own on-disk `seen_execids*.json`, so switching back to prod does not re-blast fills prod already saw.

## What deploy.sh does NOT do

- Does **not** push secrets (`.env`, `.env.test`, IBC `config.ini`) — those live only on the host (chmod 600).
- Does **not** push systemd units or IBC `config.ini`. After editing `deploy/*.service.tmpl` or `deploy/config.ini`, re-run the installer on the host — it is idempotent and re-renders the units:
  ```bash
  ssh <host> 'cd ~/ibkr-src && git pull && ./deploy/setup.sh'
  ```
  Never overwrite the deployed `~/ibc/config.ini` — it holds the IBKR password. `setup.sh` already refuses to clobber it.

## Ops control (no SSH needed)

The `ibkr-ops-bot` service takes Telegram commands from the ops channel or an allowlisted DM: `/positions`, `/resume` (restart gateway → 2FA push), `/pause`, `/status`, `/help`. Gateway recovery after mobile use is normally `/resume`, not `deploy.sh`.

## Verify after deploy

The status line should show the expected channel, `notifier=active`, `ops-bot=active`, and `api4001=up`. `api4001=down` usually means the gateway needs a `/resume` (pending 2FA), not that the deploy failed.
