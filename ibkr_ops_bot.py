#!/usr/bin/env python3
"""Telegram ops-control bot for the IBKR notifier.

Long-polls the OPS bot for commands and runs a few supervised systemctl actions
— chiefly /resume, which restarts IB Gateway and thus triggers the IBKR 2FA
push, so the operator can bring the notifier back after using the mobile app
without SSH.

Authorization has two paths (see SECURITY.md):
  - channel posts: accepted only from OPS_CHAT_ID (channel posts carry no user
    identity, so this trusts everyone who can post in that channel);
  - direct messages: accepted only from user ids in OPS_ALLOWED_USER_IDS.

It never touches trading; it only starts/stops the gateway service and reports
status. Runs as its own service so /resume still works when the gateway and
notifier are down.
"""
import logging
import os
import socket
import subprocess
import time

import httpx
from dotenv import load_dotenv

load_dotenv(os.environ.get("ENV_FILE") or None)

TOKEN = os.getenv("OPS_BOT_TOKEN") or os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = str(os.getenv("OPS_CHAT_ID") or os.environ["TELEGRAM_CHAT_ID"])
# Allowlisted Telegram user IDs that may DM the bot commands. Channel posts
# carry no user identity, so DM commands are the per-user-secured path.
ALLOWED_USERS = {int(x) for x in os.getenv("OPS_ALLOWED_USER_IDS", "").split(",") if x.strip()}
API = f"https://api.telegram.org/bot{TOKEN}"

# Gateway connection — same defaults as the notifier, overridable per host.
IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "4001"))
# Distinct from the notifier's IB_CLIENT_ID: /positions opens its own brief
# read-only connection, and two clients may not share an id.
OPS_CLIENT_ID = int(os.getenv("OPS_CLIENT_ID", "12"))
TZ_LABEL = os.getenv("TZ_LABEL", "")

# systemd unit names, so a fork can rename the services without editing code.
GATEWAY_UNIT = os.getenv("GATEWAY_UNIT", "ibc-gateway")
NOTIFIER_UNIT = os.getenv("NOTIFIER_UNIT", "ibkr-notifier")

SYSTEMCTL = "/usr/bin/systemctl"
SUDO = "/usr/bin/sudo"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
# httpx logs the request URL (with the bot token) at INFO — keep it out of journald.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("ibkr-ops")

HELP = ("<b>IBKR Ops</b>\n"
        "/positions — current open positions snapshot\n"
        "/resume — restart IB Gateway (sends a 2FA push; approve on your phone)\n"
        "/pause — stop IB Gateway (release the session to use the mobile app)\n"
        "/status — gateway / notifier / API status\n"
        "/help — this message")


def send(text: str, chat=None) -> None:
    try:
        httpx.post(f"{API}/sendMessage",
                   json={"chat_id": CHAT_ID if chat is None else chat,
                         "text": text, "parse_mode": "HTML"},
                   timeout=15)
    except Exception as e:
        log.warning("send failed: %s", e)


def _is_active(unit: str) -> str:
    try:
        return subprocess.run([SYSTEMCTL, "is-active", unit],
                              capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception as e:
        return f"err({e})"


def _api_up() -> bool:
    try:
        with socket.create_connection((IB_HOST, IB_PORT), timeout=2):
            return True
    except OSError:
        return False


def _sudo(*args) -> None:
    try:
        subprocess.run([SUDO, "-n", SYSTEMCTL, *args],
                       capture_output=True, text=True, timeout=60)
    except Exception as e:
        log.warning("sudo systemctl %s failed: %s", args, e)


def _status_text() -> str:
    return (f"gateway: <b>{_is_active(GATEWAY_UNIT)}</b>\n"
            f"notifier: <b>{_is_active(NOTIFIER_UNIT)}</b>\n"
            f"API {IB_PORT}: <b>{'up' if _api_up() else 'down'}</b>")


def _positions_text() -> str:
    """Open a brief read-only IB connection (spare client id) and render the same
    open-positions snapshot the daily summary uses. Independent of the notifier."""
    import asyncio

    from ib_async import IB

    from ibkr_report import build_summary_message

    async def go() -> str:
        ib = IB()
        await ib.connectAsync(IB_HOST, IB_PORT, clientId=OPS_CLIENT_ID, timeout=10)
        try:
            await asyncio.sleep(3)       # let portfolio + account values populate
            return build_summary_message(ib, TZ_LABEL)
        finally:
            ib.disconnect()

    return asyncio.run(go())


def handle(cmd: str, reply=None) -> None:
    cmd = cmd.split()[0].split("@")[0].lower()   # first token, strip @botname
    if cmd in ("/positions", "/pos"):
        try:
            send(_positions_text(), reply)
        except Exception as e:
            send("⚠️ Couldn't read positions — the gateway may be down. Try /resume.", reply)
            log.warning("positions failed: %s", e)
    elif cmd in ("/resume", "/login"):
        send("🔄 Restarting IB Gateway — <b>approve the 2FA push on your phone</b>. "
             "I'll confirm when the API is back.", reply)
        _sudo("reset-failed", GATEWAY_UNIT)
        _sudo("restart", GATEWAY_UNIT)
        for _ in range(30):           # ~2.5 min window for the 2FA approval
            time.sleep(5)
            if _api_up():
                send(f"✅ Gateway API is back up on {IB_PORT} — "
                     "the notifier will reconnect shortly.", reply)
                return
        send("⏳ Still waiting on the 2FA approval. Tap the push, or send /resume again if it expired.", reply)
    elif cmd == "/pause":
        _sudo("stop", GATEWAY_UNIT)
        send("⏸ IB Gateway stopped — session released. Use the mobile app freely; "
             "send /resume when you're done.", reply)
    elif cmd == "/status":
        send(_status_text(), reply)
    elif cmd in ("/help", "/start"):
        send(HELP, reply)
    # anything else: ignore


def _setup() -> None:
    # Ensure getUpdates works (clear any leftover webhook) and publish the menu.
    try:
        httpx.get(f"{API}/deleteWebhook", timeout=15)
        httpx.post(f"{API}/setMyCommands", json={"commands": [
            {"command": "positions", "description": "Current open positions"},
            {"command": "resume", "description": "Restart IB Gateway (2FA push)"},
            {"command": "pause", "description": "Stop IB Gateway (for mobile use)"},
            {"command": "status", "description": "Service / API status"},
            {"command": "help", "description": "Show commands"},
        ]}, timeout=15)
    except Exception as e:
        log.warning("setup: %s", e)


def main() -> None:
    _setup()
    log.info("Ops bot listening for commands in chat %s", CHAT_ID)
    offset = None
    # Skip any backlog so old commands aren't replayed on (re)start.
    try:
        r = httpx.get(f"{API}/getUpdates", params={"timeout": 0}, timeout=20).json()
        if r.get("result"):
            offset = r["result"][-1]["update_id"] + 1
    except Exception as e:
        log.warning("init getUpdates: %s", e)
    while True:
        try:
            r = httpx.get(f"{API}/getUpdates",
                          params={"timeout": 50, "offset": offset}, timeout=70).json()
        except Exception as e:
            log.warning("getUpdates: %s", e)
            time.sleep(5)
            continue
        for upd in r.get("result", []):
            offset = upd["update_id"] + 1
            post, dm = upd.get("channel_post"), upd.get("message")
            if post:                                    # ops channel (private)
                text = (post.get("text") or "").strip()
                if str((post.get("chat") or {}).get("id", "")) != CHAT_ID:
                    continue
                target = (post.get("chat") or {}).get("id")
            elif dm:                                    # DM/group: allowlist by user id
                text = (dm.get("text") or "").strip()
                uid = (dm.get("from") or {}).get("id")
                if uid not in ALLOWED_USERS:
                    if text.startswith("/"):
                        log.warning("ignoring command from unauthorized user %s", uid)
                    continue
                target = (dm.get("chat") or {}).get("id")
            else:
                continue
            if not text.startswith("/"):
                continue
            log.info("command: %s (-> %s)", text, target)
            # Commands run inline, so a long one (/resume waits up to ~2.5 min
            # for the 2FA tap) blocks this loop. That is deliberate: it serialises
            # systemctl actions on the gateway. Telegram retains the backlog.
            try:
                handle(text, target)
            except Exception as e:
                log.warning("handle %r failed: %s", text, e)


if __name__ == "__main__":
    main()
