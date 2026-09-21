# IBKR → Telegram Notifier

A read-only service that watches an Interactive Brokers account and posts to a Telegram channel:

- a message on **every fill** — `🟢 OPENED $TICKER` / `🔴 CLOSED $TICKER` (with realized P&L on closes), and
- a **once-daily snapshot** of all open positions, showing per-position market value and unrealized P&L, plus account-level **Cash Positions**, **Total Account Value** (Net Liquidation), and total unrealized P&L.

Amounts render in their native currency — `$` for USD, otherwise a currency suffix (e.g. `1,234.56 HKD`). Positions priced in a non-base currency also show the base-currency equivalent (`≈ $58,214.16`), and the account totals are converted at the live IBKR exchange rate, so a multi-currency portfolio adds up correctly.

It **never places or modifies orders**. Read-Only API is enforced at both the IBC and application layers.

> [!WARNING]
> **This software connects to a live brokerage account. Use at your own risk.**
>
> It is provided under the Apache License 2.0 **without warranty of any kind** (see [LICENSE](LICENSE)). It is not financial advice and not an IBKR product. You are solely responsible for your account, your credentials, and for verifying that the read-only guarantees hold in your own configuration before pointing it at real money. Test against a paper account first. The read-only property is a design goal enforced at two layers — it is not a guarantee the authors can make on your behalf.

---

## How it works

- Connects to **IB Gateway** on `127.0.0.1:4001` using [`ib_async`](https://github.com/ib-api-reloaded/ib_async), with `IB_CLIENT_ID` matching the Gateway's *Master API Client ID* so it captures fills from **all** sources (manual, API, TradingView-routed).
- Fills arrive via `commissionReportEvent`, backed by a sweep on every connection cycle (the event does not re-fire for fills synced on reconnect). Open vs close is inferred from `realizedPNL`. Fills are de-duped by `execId`, persisted to disk so a restart resumes catch-up without re-sending.
- The daily summary reads `ib.portfolio()` live, so it survives restarts. If the Gateway is unreachable at summary time it posts a warning instead — that warning is the **health signal**.
- Telegram messages are plain `httpx` POSTs to the Bot API.

Operational signal (the "summary skipped: not connected" warning, Telegram-send failures, and error-log alerts) is routed to a **separate ops channel** (`OPS_BOT_TOKEN`/`OPS_CHAT_ID`) so the production channel stays purely trade-facing. Routine reconnect noise is excluded and alerts are de-duped.

For the design rationale — the currency model, the `realizedPNL` semantics, why the fill sweep exists — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Requirements

- An Interactive Brokers **Pro** account. IBKR Lite blocks API access entirely ("API support is not available for accounts that support free trading").
- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).
- A Telegram bot (from [@BotFather](https://t.me/BotFather)) that is an **admin** of your target channel.
- For 24/7 operation: a Linux host to run IB Gateway. See [Deployment](#deployment).

## Configuration

Copy the template and fill it in:

```bash
cp env.example .env && chmod 600 .env
```

| Key | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Target channel id (bot must be admin) |
| `IB_HOST` / `IB_PORT` | `127.0.0.1` / `4001` (Gateway LIVE; `4002` paper) |
| `IB_CLIENT_ID` | Must equal Gateway's Master API Client ID (default `10`) |
| `DAILY_SUMMARY_TIME` | Local `HH:MM` for the daily snapshot (blank disables) |
| `TZ_LABEL` | Optional label on timestamps, e.g. `SGT` (blank for none) |
| `OPS_BOT_TOKEN` / `OPS_CHAT_ID` | Ops channel for health/error alerts (blank → use prod channel) |
| `OPS_ALLOWED_USER_IDS` | Comma-separated Telegram user ids allowed to DM the ops bot |
| `OPS_CLIENT_ID` | Client id for the ops bot's `/positions` connection (default `12`) |
| `STATE_FILE` | Where sent `execId`s persist (default: next to the script) |

`env.example` documents every key, including the less common ones.

## Run locally

```bash
uv sync
uv run python ibkr_telegram_notifier.py
```

To run against a **test bot/channel** instead of production, keep the test credentials in a separate `.env.test` (chmod 600, with its own `IB_CLIENT_ID` and `STATE_FILE`) and point `ENV_FILE` at it:

```bash
ENV_FILE=.env.test uv run python ibkr_telegram_notifier.py
```

## Tests

```bash
uv sync --group dev
uv run pytest        # message formatting, currency conversion, open/close inference
uv run ruff check .
```

The tests use lightweight fakes — no IB Gateway, no network, no credentials.

## Deployment

Runs under systemd on a headless Linux host, with IB Gateway driven by [IBC](https://github.com/IbcAlpha/IBC) under Xvfb. The installer is idempotent and derives every path from the invoking user:

```bash
git clone <this-repo> ~/ibkr-src
cd ~/ibkr-src
./deploy/setup.sh            # or: DEPLOY_TZ=Asia/Singapore ./deploy/setup.sh
```

Then fill in `~/ibkr-notifier/.env` and `~/ibc/config.ini`, do the one-time Gateway GUI configuration over VNC, and start the services. [`deploy/README.md`](deploy/README.md) has the full walkthrough, including the GUI-only settings and the 2FA model.

### Deploying code changes

Point `deploy/deploy.env` at your host once:

```bash
cp deploy/deploy.env.example deploy/deploy.env   # then edit
```

Then:

```bash
./deploy.sh         # push code, restart, keep current channel
./deploy.sh prod    # ... and switch to the production channel
./deploy.sh test    # ... and switch to the ops/test channel
```

Both plain `ssh` and `gcloud compute ssh` transports are supported. Secrets are never pushed — they live only on the host.

Common operations, on the host:

```bash
journalctl -u ibkr-notifier -f          # tail logs
sudo systemctl restart ibkr-notifier    # restart the notifier
sudo systemctl restart ibc-gateway      # restart IB Gateway / IBC
```

## Ops control from Telegram

`ibkr_ops_bot.py` (service `ibkr-ops-bot`) long-polls the **ops bot** for commands, so you can manage the gateway from your phone with no SSH:

- `/positions` — current open-positions snapshot (read-only, on demand)
- `/resume` — restart IB Gateway (sends a 2FA push; approve on your phone) and bring the notifier back, e.g. after using the IBKR mobile app
- `/pause` — stop IB Gateway to release the session for mobile use
- `/status` — gateway / notifier / API status
- `/help` — list commands

It runs as a separate service so `/resume` still works when the gateway and notifier are down.

**Authorization has two paths.** Posts in `OPS_CHAT_ID` are accepted from that channel as a whole (Telegram channel posts carry no user identity), and direct messages are accepted only from user ids in `OPS_ALLOWED_USER_IDS`. Anyone who can post in the ops channel can stop your gateway — read [SECURITY.md](SECURITY.md) before adding members.

Commands run inline, so a long one blocks the poll loop: `/positions` takes a few seconds and `/resume` waits up to ~2.5 minutes for the 2FA tap. This is deliberate — it serialises systemctl actions — and Telegram retains the backlog meanwhile.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues: please follow [SECURITY.md](SECURITY.md) rather than opening a public issue.

## License

[Apache License 2.0](LICENSE).
