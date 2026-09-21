# Deployment runbook

An ordered, checkpointed procedure for standing this up on a fresh host —
written to be executed by a person or an AI agent working from a terminal.

**Read this before starting.** Two parts of the procedure cannot be automated by
anyone, for reasons outside this project's control. Knowing where those walls are
up front is the difference between a clean deploy and discovering the wall after
a twenty-minute install.

`deploy/README.md` is the narrative version with more explanation. This file is
the checklist.

---

## Host requirements

These are hard requirements, not preferences. Check all four before provisioning.

| | Requirement | Why it is not negotiable |
|---|---|---|
| **Architecture** | **x86-64 only** | Interactive Brokers publishes no ARM64 build of IB Gateway. Graviton, Ampere, Axion and Raspberry Pi **cannot run this**. Picking an ARM instance is the single most common way to waste an afternoon here. |
| **OS** | Debian 12, or Ubuntu 22.04+ | `deploy/setup.sh` uses `apt-get`. On RHEL/Fedora/Arch, swap the package step. |
| **Init** | systemd | Five unit templates; no OpenRC/runit equivalents ship here. |
| **Account** | IBKR **Pro** | IBKR Lite returns *"API support is not available for accounts that support free trading"* and the API never opens. There is no workaround. Verify this before provisioning anything. |

Sizing and network:

- **2 vCPU, 8 GB RAM.** IB Gateway is a JVM desktop application. 4 GB is tight
  and will swap; 8 GB is comfortable.
- **20 GB disk.** IB Gateway is ~1 GB plus a bundled JRE.
- **No inbound ports required.** The Gateway API (`4001`) and VNC (`5900`) both
  bind to loopback. Do not open either to the internet — `4001` is an
  unauthenticated control channel to a brokerage account. Outbound HTTPS to
  `api.telegram.org` and IBKR is all that is needed.
- A **non-root user with sudo**. `setup.sh` refuses to run as root, because the
  services run as the invoking user.

Also needed before you start: a Telegram bot from [@BotFather](https://t.me/BotFather)
that is an **admin** of the target channel, and the IBKR Mobile app enrolled for
**IB Key** two-factor auth.

---

## Who does what

| # | Step | Who | Automatable? |
|---|---|---|---|
| 1 | Provision the host | agent or human | ✅ yes, given cloud credentials |
| 2 | `git clone` + `./deploy/setup.sh` | **agent** | ✅ yes |
| 3 | Fill `~/ibkr-notifier/.env` | human | ⚠️ credentials only |
| 4 | Fill `~/ibc/config.ini` | human | ⚠️ credentials only |
| 5 | Gateway GUI configuration over VNC | **human** | ❌ **no — see below** |
| 6 | First login + 2FA approval | **human** | ❌ **no — phone tap** |
| 7 | Start services | **agent** | ✅ yes |
| 8 | Verify | **agent** | ✅ yes |

### Why steps 5 and 6 cannot be automated

**Step 5.** Four settings live only in the Gateway's GUI: Master API Client ID,
socket port, Read-Only API, and Lock-and-Exit → Auto restart. The Gateway stores
them under `~/Jts` in an IBKR-encrypted format (`IBGZENC`) that cannot be
generated or pre-seeded. IBC overrides some at launch, but the saved values must
agree or they drift back. Someone has to tunnel VNC and click.

**Step 6.** IBKR sends an IB Key push to a physical phone. There is no API path.

An agent should **stop at step 4**, hand back with the exact instructions below,
and resume at step 7 once the human confirms.

---

## Procedure

### Phase 1 — install (agent)

```bash
git clone https://github.com/SeeYangZhi/IBKR_Tele_Notifier ~/ibkr-src
cd ~/ibkr-src
DEPLOY_TZ=<IANA zone, e.g. Asia/Singapore> \
AUTO_RESTART_TIME=<HH:MM, a time your market is CLOSED> \
./deploy/setup.sh
```

`DEPLOY_TZ` sets the timezone for `DAILY_SUMMARY_TIME` and the Gateway's daily
restart. It defaults to `UTC`; set it deliberately.

`setup.sh` is idempotent and safe to re-run, including on a live host — it will
not restart `xvfb` or `ibc-gateway` if they are already running.

**Checkpoint:** installer prints `SETUP COMPLETE` and a NEXT STEPS block.

### Phase 2 — credentials (human)

```bash
nano ~/ibkr-notifier/.env     # already chmod 600
```

Required: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Set `IB_CLIENT_ID` to match
the Master API Client ID you will set in phase 3 (default `10`). `env.example`
documents every key.

```bash
nano ~/ibc/config.ini         # already chmod 600
```

Set `IbLoginId` and `IbPassword`. These never leave the host; `deploy.sh` does
not push them and `setup.sh` will not overwrite an existing `config.ini`.

**Checkpoint:** both files populated, both still mode `600`.

### Phase 3 — Gateway GUI + first login (human)

From your workstation:

```bash
ssh -L 5900:localhost:5900 <host>
```

Retrieve the VNC password if `setup.sh` generated one:

```bash
x11vnc -showrfbauth ~/.vnc/passwd
```

Start the Gateway and connect a VNC client to `localhost:5900`:

```bash
sudo systemctl start ibc-gateway
```

Complete the login — **approve the IB Key push on your phone** — then set, in the
Gateway window:

- **Configure → Settings → API → Settings**
  - Socket port: `4001`
  - Master API client ID: `10` *(must equal `IB_CLIENT_ID` in `.env`)*
  - Read-Only API: **enabled**
- **Configure → Lock and Exit** → Auto restart

Restart the gateway so IBC's overrides and the saved settings agree:

```bash
sudo systemctl restart ibc-gateway   # one more 2FA tap
```

**Checkpoint:** `ss -ltn | grep 4001` shows the API listening. Until this
succeeds, nothing downstream will work.

### Phase 4 — start and verify (agent)

```bash
sudo systemctl start ibkr-notifier ibkr-ops-bot
```

Verify all of the following:

```bash
systemctl is-active xvfb x11vnc ibc-gateway ibkr-notifier ibkr-ops-bot  # all: active
systemctl is-enabled xvfb x11vnc ibc-gateway ibkr-notifier ibkr-ops-bot # all: enabled
ss -ltn | grep 4001                                    # API listening
journalctl -u ibkr-notifier | grep "Connected to IB Gateway"
grep -L '@[A-Z_]*@' /etc/systemd/system/ibkr-*.service # no unrendered placeholders
```

End-to-end test of the summary path — set `DAILY_SUMMARY_TIME` a minute ahead in
`.env`, restart the notifier, confirm the Telegram message arrives, then restore
the real value.

Finally, send `/status` in the ops channel to confirm the ops bot responds.

---

## Deploying code changes afterwards

On your workstation, once:

```bash
cp deploy/deploy.env.example deploy/deploy.env   # then edit
```

Then `./deploy.sh [prod|test|keep]`. Both plain `ssh` and `gcloud compute ssh`
transports are supported. Secrets are never pushed.

After editing a `*.service.tmpl` or `deploy/config.ini`, re-run
`./deploy/setup.sh` on the host — `deploy.sh` does not push those.

---

## Rules for an agent operating this host

Taken from [deploy/AGENTS.md](../deploy/AGENTS.md); they exist because breaking
them has real consequences.

- **Never restart `ibc-gateway` without warning the human.** It forces a fresh
  login, which sends a 2FA push to their phone. Restarting `xvfb` kills the
  display the Gateway runs on and has the same effect indirectly.
- **Never widen the sudo grant** in `/etc/sudoers.d/ibkr-ops-bot`. It is scoped
  to a fixed list of `systemctl` verbs. The ops bot processes input from the
  internet; a blanket `NOPASSWD: ALL` turns a Telegram message into root.
- **Never commit secrets.** `deploy/config.ini` in the repo is a
  credential-free template and CI fails the build if it is filled in.
- **Never add order-placing code.** This service is read-only by contract.
- **Do not lower the `httpx`/`httpcore` log level** — their `INFO` output
  contains the bot token.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Installer fails fetching IB Gateway | ARM host. Not supported — see requirements. |
| `4001` never listens | Gateway not logged in. Check `journalctl -u ibc-gateway`; usually a pending 2FA push. |
| `"API support is not available…"` | IBKR Lite account. Upgrade to Pro. |
| Fills never reported, positions fine | `IB_CLIENT_ID` ≠ Gateway's Master API Client ID. |
| Repeated 2FA pushes | `ibc-gateway.service` set to `Restart=always`. It must be `on-failure`. |
| `x11vnc` crash-looping | Missing `~/.vnc/passwd`. Re-run `setup.sh`. |
| `./deploy.sh test` doesn't switch channel | Unit predates `channel.env`. Re-run `setup.sh`; it migrates automatically. |
