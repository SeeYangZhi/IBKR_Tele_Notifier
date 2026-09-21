# Deploying the notifier

Running this 24/7 means running IB Gateway 24/7, and IB Gateway is a desktop Java
application. The approach here: a headless Linux host, a virtual X display
(Xvfb), [IBC](https://github.com/IbcAlpha/IBC) to drive the Gateway's GUI through
login, and systemd to supervise the lot.

Any Linux host works. The installer derives every path from the invoking user.

## What gets installed

| Unit | Role |
|---|---|
| `xvfb` | Virtual display `:99` — the headless GUI host. |
| `x11vnc` | VNC on `127.0.0.1:5900`, SSH-tunnel only. One-time GUI config, not normal operation. |
| `ibc-gateway` | IBC launches IB Gateway via `run-gateway.sh` on display `:99`. |
| `ibkr-notifier` | The notifier. No hard dependency on the gateway — it self-reconnects, so it must tolerate the gateway being gone. |
| `ibkr-ops-bot` | The Telegram ops bot. Independent of the gateway and notifier so `/resume` works when they are down. |

All are boot-enabled and log to journald.

Files in this directory:

- `*.service.tmpl` — systemd unit templates. `setup.sh` substitutes `@USER@`,
  `@HOME@`, `@PROJECT_DIR@`, `@IBC_HOME@`, `@UV@`, `@TZ@` and installs the result.
- `config.ini` — the IBC config template. **Credential-free and must stay that way.**
- `run-gateway.sh.tmpl` — the Gateway launcher, rendered to `~/ibc/run-gateway.sh`.
- `setup.sh` — the idempotent installer. Safe to re-run.
- `remote-deploy.sh` — the on-host half of `../deploy.sh`.
- `deploy.env.example` — template for `deploy.env`, which tells `../deploy.sh` where your host is.

## Prerequisites

- A Linux host (Debian 12 is what this is tested on) with ~8 GB RAM. IB Gateway
  is a JVM application and wants room.
- An IBKR **Pro** account. IBKR Lite returns *"API support is not available for
  accounts that support free trading"* and the API never opens. There is no
  workaround.
- The IBKR Mobile app enrolled for **IB Key** two-factor auth. With IB Key,
  logins are a tap on your phone and need no VNC. A QR-code fallback does need VNC.
- `sudo` access for the installing user.

## Install

```bash
git clone <this-repo> ~/ibkr-src
cd ~/ibkr-src
./deploy/setup.sh
```

Useful overrides:

```bash
DEPLOY_TZ=Asia/Singapore \
AUTO_RESTART_TIME=12:00 \
VNC_PASSWORD=something \
./deploy/setup.sh
```

| Variable | Default | Meaning |
|---|---|---|
| `DEPLOY_TZ` | `UTC` | Timezone for `DAILY_SUMMARY_TIME` and the IBC daily restart. |
| `AUTO_RESTART_TIME` | `12:00` | Gateway's daily soft restart, in `DEPLOY_TZ`. Pick a time your market is **closed**. |
| `PROJECT_DIR` | `~/ibkr-notifier` | Where the uv project lives. |
| `IBC_PATH` | `/opt/ibc` | IBC install directory. |
| `VNC_PASSWORD` | random | VNC password. If omitted, one is generated; read it back with `x11vnc -showrfbauth ~/.vnc/passwd`. |

The installer will:

1. install apt deps (`xvfb`, `x11vnc`, `unzip`, `curl`) and `uv`;
2. set up the uv project and verify imports;
3. download and install IB Gateway (**offline standalone** build — IBC does not
   work with the auto-updating one) into `~/Jts/ibgateway/<major>/`;
4. download the latest IBC release into `/opt/ibc`;
5. write `~/ibc/{config.ini,ibc.env,run-gateway.sh}`;
6. create a VNC password;
7. install a **narrowly scoped** sudoers rule for the ops bot (see
   [SECURITY.md](../SECURITY.md));
8. render and install the five units, enabling all and starting only `xvfb` and `x11vnc`.

It deliberately does not start the Gateway — that needs your credentials and a
first interactive login.

## Configure

**1. Telegram and IB settings** — `~/ibkr-notifier/.env` (already `chmod 600`):

```bash
nano ~/ibkr-notifier/.env
```

**2. IBKR credentials** — `~/ibc/config.ini` (already `chmod 600`):

```ini
IbLoginId=your_ibkr_username
IbPassword=your_ibkr_password
```

These never leave the host. `deploy.sh` does not touch them and `setup.sh` will
not overwrite an existing `config.ini`.

**3. One-time Gateway GUI settings.** These cannot be expressed in any text
config — the Gateway's settings files under `~/Jts` are IBKR-encrypted
(`IBGZENC`) and cannot be pre-seeded. Tunnel VNC in:

```bash
ssh -L 5900:localhost:5900 <your-host>
# then connect a VNC client to localhost:5900
```

Start the gateway (`sudo systemctl start ibc-gateway`), complete the first login,
then in the Gateway window set:

- **Configure → Settings → API → Settings**
  - Socket port: `4001`
  - Master API client ID: `10` (must equal `IB_CLIENT_ID` in `.env`)
  - Read-Only API: **enabled**
- **Configure → Lock and Exit** → Auto restart

Restart the gateway afterwards so IBC's overrides and the saved settings agree.

**4. Start everything:**

```bash
sudo systemctl start ibc-gateway ibkr-notifier ibkr-ops-bot
```

## Verify

```bash
systemctl is-active xvfb x11vnc ibc-gateway ibkr-notifier ibkr-ops-bot   # all: active
systemctl is-enabled xvfb x11vnc ibc-gateway ibkr-notifier ibkr-ops-bot  # all: enabled
ss -ltn | grep 4001                    # API listening (only after a completed login)
journalctl -u ibkr-notifier | grep Connected
```

To exercise the summary path end to end, set `DAILY_SUMMARY_TIME` a minute ahead
in `.env`, restart the notifier, confirm the Telegram message arrives, then put
it back.

## Day-to-day

Deploy code changes from your workstation with `../deploy.sh` — see the
[main README](../README.md#deploying-code-changes). Set up `deploy.env` once:

```bash
cp deploy/deploy.env.example deploy/deploy.env   # then edit
```

`deploy.sh` pushes only the Python sources and `pyproject.toml`. It does **not**
push systemd units or `config.ini`. After editing a `*.service.tmpl`, re-run
`./deploy/setup.sh` on the host — it is idempotent and will re-render them.

**Re-running `setup.sh` on a live host is safe.** It will not restart `xvfb` or
`x11vnc` if they are already up — bouncing the display would kill IB Gateway and
cost you a 2FA push — and it never restarts `ibc-gateway` for the same reason. It
does restart `ibkr-notifier` and `ibkr-ops-bot` if they are running, so they pick
up the re-rendered units. If a unit change affects the gateway, restart it
yourself when you are ready to approve the 2FA.

Gateway recovery after mobile use is normally `/resume` in Telegram, not a deploy.

## Upgrading an existing install

Pull the new code onto the host and re-run the installer:

```bash
cd ~/ibkr-src && git pull && ./deploy/setup.sh
```

It re-renders the units, restarts the notifier and ops bot, and leaves the
display and gateway running. It also migrates the legacy channel switch: older
installs selected the test channel with a systemd drop-in at
`/etc/systemd/system/ibkr-notifier.service.d/test-channel.conf`, which is now
`channel.env` in the project directory. The current setting is carried over, so
a host on TEST stays on TEST, and the obsolete drop-in is removed.

## The 2FA and mobile-use model

The short version: use the mobile app whenever you want; the Gateway yields and
stays down, sending no pushes. When you are done, `/resume` brings it back with a
single 2FA tap, and any fills you made meanwhile are caught up and reported.

This behaviour is configured, not accidental, and the reasoning —
`ExistingSessionDetectedAction=secondary`, why the gateway unit uses
`Restart=on-failure` rather than `always`, and why one login sends exactly one
push — is documented in
[docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md#mobile-coexistence-and-2fa).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `ss -ltn \| grep 4001` empty | Gateway not logged in. Check `journalctl -u ibc-gateway`; a pending 2FA push is the usual reason. |
| `x11vnc` failing on boot | Missing `~/.vnc/passwd`. Re-run `setup.sh`, or `x11vnc -storepasswd <pw> ~/.vnc/passwd`. |
| Notifier logs `Connect failed` repeatedly | Gateway down (often intentionally, during mobile use), or `IB_PORT` mismatch. |
| No fills reported, but positions work | `IB_CLIENT_ID` does not match the Gateway's Master API Client ID. |
| `"API support is not available…"` | IBKR Lite account. Upgrade to Pro. |
| Ops bot `/resume` does nothing | Sudoers rule missing or unit renamed. Check `sudo -n /usr/bin/systemctl restart ibc-gateway` as the service user. |
| `./deploy.sh test` doesn't switch channel | Unit predates `channel.env`. Re-run `./deploy/setup.sh` on the host; it migrates the old drop-in automatically. |
| Repeated 2FA pushes | Gateway unit set to `Restart=always`. It must be `on-failure`. |
