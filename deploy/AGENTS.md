# deploy/ — Deployment & service infrastructure

## Purpose

Everything needed to run the notifier 24/7 on a headless Linux host: the systemd unit templates, the IBC config that auto-launches IB Gateway, the launcher, the idempotent installer, and the on-host half of `../deploy.sh`. This directory is the in-repo source of truth for the host's `/etc/systemd/system/*.service` units and `~/ibc/*`.

Operator-facing instructions live in [README.md](README.md); the *why* behind the 2FA and restart behaviour lives in [../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md). This file is the contract for editing what is here.

## Ownership

Governed by the root [AGENTS.md](../AGENTS.md) contracts (read-only, secrets handling, no personal infrastructure in tracked files). Changes here act on a real host running against a live brokerage account — treat them as production changes.

## Local Contracts

- **No host-specific values in tracked files.** No usernames, hostnames, cloud project ids, or absolute home paths. Unit files are `*.service.tmpl` with `@USER@`, `@HOME@`, `@PROJECT_DIR@`, `@IBC_HOME@`, `@UV@`, `@TZ@` placeholders; `run-gateway.sh.tmpl` uses `@IBC_PATH@`, `@IBC_HOME@`, `@TWS_PATH@`. **If you add a placeholder, add its substitution to `setup.sh` in the same change** — an unrendered `@FOO@` reaches the host silently.
- **`config.ini` is a credential-free template.** `IbLoginId`/`IbPassword` stay blank here and are filled only in the deployed copy at `~/ibc/config.ini` (chmod 600). CI fails the build if they are non-blank. `setup.sh` never overwrites an existing deployed config.
- **Pinned IBC values:** `TradingMode=live`, `ReadOnlyApi=yes`, `ReadOnlyLogin=no` (a read-only *login* would hide the account data the summary needs), `OverrideTwsApiPort=4001`, `OverrideTwsMasterClientID=10`, `AcceptIncomingConnectionAction=accept`, `IbAutoClosedown=no`, `AutoRestartTime=@AUTO_RESTART_TIME@`, `ExistingSessionDetectedAction=secondary`, `SecondFactorDevice=IB Key`, `SecondFactorAuthenticationTimeout=300`, `ReloginAfterSecondFactorAuthenticationTimeout=no`.
- **Services** (journald logs, boot-enabled):
  - `xvfb` — virtual display `:99`.
  - `x11vnc` — VNC on `127.0.0.1:5900`, SSH-tunnel only; password at `~/.vnc/passwd`, created by `setup.sh`. One-time GUI config, not normal operation.
  - `ibc-gateway` — IBC launches IB Gateway LIVE via `run-gateway.sh`; `EnvironmentFile=~/ibc/ibc.env` (carries `TWS_MAJOR_VRSN`); `DISPLAY=:99`. **`Restart=on-failure`, never `always`** — see below.
  - `ibkr-notifier` — `uv run` the notifier. Intentionally has NO hard dependency on the gateway: the notifier self-reconnects and must tolerate the gateway being gone. Loads `EnvironmentFile=-<project>/channel.env` for the prod/test channel switch.
  - `ibkr-ops-bot` — `uv run ibkr_ops_bot.py`. Independent of the gateway and notifier so `/resume` works when they are down.
- **`Restart=on-failure` on the gateway is load-bearing.** A yield to a competing mobile session is a clean exit (status 0). With `Restart=always`, systemd relaunches → login → 2FA push → yield → repeat, which spams the operator's phone. Do not "fix" this to `always`.
- **Privilege boundary.** `setup.sh` installs `/etc/sudoers.d/ibkr-ops-bot` granting NOPASSWD for a fixed list of `systemctl` verbs on the project's own units and nothing else. Never widen it to `NOPASSWD: ALL`. Anything an unprivileged path needs must work *without* sudo — this is why the channel switch writes `channel.env` in the project dir instead of a systemd drop-in in `/etc`.
- Secrets are never committed; real credentials live only on the host.

## Work Guidance

- **`setup.sh` must stay idempotent** and safe to re-run. It derives `USER_NAME`, `HOME_DIR`, and `SRC` from the invoking user and the script's own location — no hardcoded paths. Re-running it is the supported way to apply unit-template edits.
- **Redeploy code:** `./deploy.sh [prod|test|keep]` from the repo root. `remote-deploy.sh` is the on-host half and locates the project dir from its own path. Don't hand-run scp/ssh for routine deploys.
- After editing a `*.service.tmpl` or `config.ini`, re-run `./deploy/setup.sh` on the host — `deploy.sh` does not push them.
- IB Gateway must be the **offline standalone** build; IBC does not work with the auto-updating one. The install dir's major-version name (e.g. `1045`) is `TWS_MAJOR_VRSN`.
- **GUI-only host settings** (set once via VNC; not expressible in any text config, because the Gateway's `~/Jts` settings files are IBKR-encrypted `IBGZENC` and cannot be pre-seeded): Master API Client ID = 10, socket port 4001, Read-Only API on, Lock-and-Exit = Auto restart.
- **2FA:** IB Key tap-push needs no VNC once the IBKR Mobile app is enrolled; a QR-code fallback does need VNC.
- **Account type:** must be IBKR **Pro**. Lite returns "API support is not available for accounts that support free trading" and the API never opens.

## Verification

- `bash -n` on every script; CI additionally runs `shellcheck -S warning` over `deploy.sh`, `setup.sh`, `remote-deploy.sh`.
- CI asserts `deploy/config.ini` has blank `IbLoginId`/`IbPassword`.
- On the host: `systemctl is-active xvfb x11vnc ibc-gateway ibkr-notifier ibkr-ops-bot` → all `active`; `is-enabled` → all `enabled`.
- `grep -r '@[A-Z_]*@' /etc/systemd/system/ibkr-*.service` → empty (no unrendered placeholders).
- `ss -ltn | grep 4001` → API listening (only after a completed login).
- `journalctl -u ibkr-notifier` shows `Connected to IB Gateway`.
- Summary path: set `DAILY_SUMMARY_TIME` a minute ahead in the host `.env`, restart the notifier, confirm the Telegram message, then restore it.

## Child DOX Index

None — leaf directory.
