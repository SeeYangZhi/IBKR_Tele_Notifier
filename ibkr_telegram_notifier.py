#!/usr/bin/env python3
"""
IBKR -> Telegram trade notifier + daily position summary.

Read-only. Posts a Telegram message on every fill (open/close) and, once a day
at DAILY_SUMMARY_TIME (local), posts a snapshot of ALL currently-open positions.
Never places or modifies orders.

Open vs Close on fills is inferred from the commission report's realizedPNL:
  - a usable NON-ZERO number  -> reducing / closing -> CLOSED (+P&L)
  - anything else             -> opening / adding   -> OPENED

"Anything else" covers both the IBKR "no value" sentinel (~1.8e308) and a plain
0.0 -- which is what ib_async actually reports for an opening trade, despite the
API docs implying the sentinel. Treating 0.0 as a close would label every
opening fill CLOSED, so the zero check is load-bearing. See ibkr_report.py.

The daily summary reads live account state (ib.portfolio()), so it survives
script/Gateway restarts. If Gateway is unreachable at summary time it says so,
which doubles as a health check.

Captures fills from ALL sources (manual clicks, API orders, TradingView routed
to IBKR) as long as IB_CLIENT_ID matches Gateway's "Master API client ID".
"""

import asyncio
import html
import json
import logging
import os
from datetime import UTC, datetime, timedelta

import httpx
from dotenv import load_dotenv
from ib_async import IB

from ibkr_report import build_fill_message, build_summary_message

# ENV_FILE points at an alternate dotenv (e.g. .env.test for the test bot/channel);
# unset -> the default .env is found as usual.
load_dotenv(os.environ.get("ENV_FILE") or None)

# --- Config -----------------------------------------------------------------
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
# Ops/health-alert channel (e.g. the test bot + ops channel). If unset, ops
# messages fall back to the production channel.
OPS_BOT_TOKEN = os.getenv("OPS_BOT_TOKEN", "")
OPS_CHAT_ID = os.getenv("OPS_CHAT_ID", "")
IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "4001"))           # 4001 = IB Gateway LIVE
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "10"))   # must equal Gateway Master Client ID
DAILY_SUMMARY_TIME = os.getenv("DAILY_SUMMARY_TIME", "17:00")  # local HH:MM, "" disables
TZ_LABEL = os.getenv("TZ_LABEL", "")  # optional label on timestamps, e.g. "SGT"
RETRY_DELAY = 60  # reconnect backoff to the Gateway API; alerts need not be real-time
# Persisted set of already-notified execIds, so a process restart resumes
# catch-up (sends fills missed while down) without re-sending old ones.
STATE_FILE = os.getenv("STATE_FILE",
                       os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "seen_execids.json"))
# Cap on the persisted execId set. IBKR execIds are time-ordered, so keeping the
# newest N is enough to dedupe against anything the Gateway would still replay,
# while stopping the state file from growing without bound on a busy account.
STATE_MAX_IDS = int(os.getenv("STATE_MAX_IDS", "5000"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)s  %(message)s")
# httpx/httpcore log the full request URL (which contains the bot token) at
# INFO — keep secrets out of the journal by raising their level.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("ibkr-tg")

ib = IB()
http = httpx.AsyncClient(timeout=10)


def _load_seen() -> set[str]:
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()
    except Exception as e:
        log.warning("Could not read state file %s: %s", STATE_FILE, e)
        return set()


def _persist_seen() -> None:
    try:
        ids = sorted(seen_exec_ids)
        if len(ids) > STATE_MAX_IDS:       # keep the newest; execIds sort by time
            ids = ids[-STATE_MAX_IDS:]
        tmp = f"{STATE_FILE}.tmp"
        with open(tmp, "w") as f:
            json.dump(ids, f)
        os.replace(tmp, STATE_FILE)   # atomic
    except Exception as e:
        log.warning("Could not write state file %s: %s", STATE_FILE, e)


seen_exec_ids: set[str] = _load_seen()


# --- Telegram ---------------------------------------------------------------
async def _post(token: str, chat: str, text: str) -> bool:
    """Low-level Telegram send; True on HTTP 200. Never logs (so it can't feed
    the ops-alert log handler and loop)."""
    try:
        r = await http.post(f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat, "text": text, "parse_mode": "HTML"})
        return r.status_code == 200
    except Exception:
        return False


async def send_telegram(text: str) -> None:
    """Trade-facing messages -> production channel."""
    if not await _post(BOT_TOKEN, CHAT_ID, text):
        log.error("Telegram send to production channel failed")


async def send_ops(text: str) -> None:
    """Operational/health alerts -> ops channel (falls back to prod if unset)."""
    if not await _post(OPS_BOT_TOKEN or BOT_TOKEN, OPS_CHAT_ID or CHAT_ID, text):
        log.warning("Ops-channel send failed")   # WARNING isn't forwarded -> no loop


class _OpsLogForwarder(logging.Handler):
    """Push genuine problem logs to the ops channel — deduped (15 min), and
    skipping the routine reconnect retries that are normal during mobile use."""
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self._last: dict[str, datetime] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if "Connect failed" in msg or "retry in" in msg:
                return
            now = datetime.now(UTC)
            prev = self._last.get(msg)
            if prev and (now - prev).total_seconds() < 900:
                return
            self._last[msg] = now
            asyncio.ensure_future(
                send_ops(f"🟠 <b>Notifier alert</b>\n<code>{html.escape(msg)}</code>"))
        except Exception:
            pass


log.addHandler(_OpsLogForwarder())


# --- Fill notifications -----------------------------------------------------
def _maybe_notify(fill, report) -> None:
    if fill.contract.secType == "CASH":        # ignore IBKR auto FX conversions, not trades
        return
    ex_id = fill.execution.execId
    if not ex_id or ex_id in seen_exec_ids:   # already notified (this run or a prior one)
        return
    seen_exec_ids.add(ex_id)
    _persist_seen()
    log.info("Notifying fill %s", ex_id)
    asyncio.ensure_future(send_telegram(build_fill_message(fill, report, TZ_LABEL)))


def on_commission(trade, fill, report) -> None:
    _maybe_notify(fill, report)


def sweep_fills() -> None:
    """Alert any fill not yet notified. Runs every connection cycle because
    commissionReportEvent does NOT re-fire for fills received during a (re)connect
    sync — so this is what actually catches fills made while we were disconnected
    (e.g. during mobile use). Deduped by the persisted execId set."""
    for f in ib.fills():
        rep = getattr(f, "commissionReport", None)
        if rep is not None and getattr(rep, "execId", ""):
            _maybe_notify(f, rep)


# --- Daily position summary -------------------------------------------------
async def send_daily_summary() -> None:
    if not ib.isConnected():
        # Health signal -> ops channel, not the trade channel.
        await send_ops("⚠️ Daily summary skipped: not connected to IB Gateway — "
                       "can't read positions. Check the bot/Gateway.")
        return
    await send_telegram(build_summary_message(ib, TZ_LABEL))


def _seconds_until(hhmm: str, now=None) -> float:
    now = now or datetime.now()
    h, m = map(int, hhmm.split(":"))
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def daily_summary_loop() -> None:
    if not DAILY_SUMMARY_TIME:
        return
    log.info("Daily summary scheduled for %s (local).", DAILY_SUMMARY_TIME)
    while True:
        await asyncio.sleep(_seconds_until(DAILY_SUMMARY_TIME))
        log.info("Sending daily position summary.")
        await send_daily_summary()
        await asyncio.sleep(70)  # avoid double-fire within the same minute


# --- Connection loop --------------------------------------------------------
def on_disconnected() -> None:
    log.warning("Disconnected from IB Gateway — will reconnect.")


async def connection_loop() -> None:
    while True:
        if not ib.isConnected():
            try:
                await ib.connectAsync(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, timeout=10)
                log.info("Connected to IB Gateway.")
                # No explicit reqAccountUpdates here: connectAsync already
                # subscribes for a single managed account, which is what fills
                # portfolio() + accountValues() for the summary. (The sync
                # ib.reqAccountUpdates() that used to live here always raised
                # "event loop is already running" and did nothing.)
            except Exception as e:
                log.error("Connect failed: %s (retry in %ss)", e, RETRY_DELAY)
                await asyncio.sleep(RETRY_DELAY)
                continue
        # Sweep every cycle: catches fills the live event missed (incl. those made
        # while disconnected). Cheap (local cache) and deduped.
        if ib.isConnected():
            try:
                sweep_fills()
            except Exception as e:
                log.warning("sweep_fills failed: %s", e)
        await asyncio.sleep(5)


async def main() -> None:
    ib.commissionReportEvent += on_commission
    ib.disconnectedEvent += on_disconnected
    await asyncio.gather(connection_loop(), daily_summary_loop())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down.")
