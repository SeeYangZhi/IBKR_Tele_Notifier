#!/usr/bin/env python3
"""Message formatting and report building, shared by the notifier and the ops bot.

Everything here is a pure function of its arguments (an `IB` handle is passed in
rather than read from a module global), so both services can render the same
open-positions snapshot from their own connections, and the formatting logic is
unit-testable without an IB Gateway or any environment variables.

Currency is the subtle part. `PortfolioItem` fields (`marketPrice`,
`marketValue`, `averageCost`, `unrealizedPNL`) are denominated in the
*contract's* currency, while account-level tags are in the account's *base*
currency. Anything summed across positions must go through `to_base()` first --
adding an HKD P&L to a USD P&L unconverted silently overstates the total.
"""

import html
from datetime import datetime
from decimal import Decimal

UNSET = 1e307  # values above this are IBKR's "no value" sentinel


# --- Scalar formatting -------------------------------------------------------
def fmt_qty(qty) -> str:
    """Share count without trailing zeros: 100, 12.5."""
    return f"{Decimal(str(qty)).normalize():f}"


def money(x, cur: str = "USD") -> str:
    """Amount in its native currency: $1,234.56 for USD, '1,234.56 SGD' otherwise.
    Negatives (shorts / margin cash) show a leading '-'."""
    sign = "-" if x < 0 else ""
    a = f"{abs(x):,.2f}"
    return f"{sign}${a}" if cur in ("", "USD") else f"{sign}{a} {cur}"


def smoney(x, cur: str = "USD") -> str:
    """Always-signed amount for P&L: +$1,234.56 / -$1,234.56, or '... SGD'."""
    sign = "+" if x >= 0 else "-"
    a = f"{abs(x):,.2f}"
    return f"{sign}${a}" if cur in ("", "USD") else f"{sign}{a} {cur}"


def is_usable(x) -> bool:
    """True if x is a usable number (not None, not NaN, not the IBKR sentinel)."""
    return x is not None and x == x and abs(x) < UNSET


# --- Account values ----------------------------------------------------------
def acct_value(ib, tag: str, cur: str | None = None):
    """First populated value for an account tag, as (float, currency).

    IBKR emits one row PER currency for many tags (e.g. UnrealizedPnL arrives as
    BASE + one row per currency held), so pass `cur` to pin the row you mean --
    "BASE" for the account-level total in the base currency."""
    for v in ib.accountValues():
        if v.tag == tag and v.value and (cur is None or v.currency == cur):
            try:
                return float(v.value), v.currency
            except ValueError:
                pass
    return None


def base_currency(ib) -> str:
    """The account's base currency -- what the account-level tags are reported in.
    NetLiquidation is emitted base-only, so its currency field is the source."""
    nlv = acct_value(ib, "NetLiquidation")
    if nlv and nlv[1] not in ("", "BASE"):
        return nlv[1]
    return "USD"


def fx_rates(ib) -> dict[str, float]:
    """Currency code -> multiplier into the account's base currency, from the
    live account feed (IBKR sends an ExchangeRate row per currency held)."""
    out: dict[str, float] = {}
    for v in ib.accountValues():
        if v.tag == "ExchangeRate" and v.currency not in ("", "BASE") and v.value:
            try:
                out[v.currency] = float(v.value)
            except ValueError:
                pass
    return out


def to_base(x, cur: str, base: str, rates: dict[str, float]):
    """Amount converted into the base currency, or None if no rate is known.

    Portfolio items are reported in the CONTRACT's currency, so anything summed
    across positions (totals) must go through here first."""
    if cur in ("", base):
        return float(x)
    rate = rates.get(cur)
    return None if rate is None else float(x) * rate


# --- Messages ----------------------------------------------------------------
def build_fill_message(fill, report, tz_label: str = "") -> str:
    """One Telegram message for a single fill: OPENED or CLOSED (+ realized P&L)."""
    ex, c = fill.execution, fill.contract
    side = "BUY" if ex.side == "BOT" else "SELL"
    sym = "$" + html.escape(c.localSymbol or c.symbol)
    qty = fmt_qty(ex.shares)
    when = ex.time.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    if tz_label:
        when += f" {tz_label}"
    cur = report.currency or "USD"
    value = float(ex.shares) * float(ex.price)
    realized = report.realizedPNL

    # A fill only realizes P&L when it REDUCES a position. Opening/adding trades
    # come through with realizedPNL == 0.0 in ib_async (not the ~1.8e308 sentinel
    # the API docs imply), so treat only a usable NON-ZERO value as a close.
    if is_usable(realized) and realized != 0:
        head = f"🔴 <b>CLOSED {sym}</b>"
        pnl = f"\nRealized P&amp;L: <b>{smoney(realized, cur)}</b>"
    else:
        head = f"🟢 <b>OPENED {sym}</b>"
        pnl = ""

    return (f"{head}\n"
            f"{side} {qty} @ {money(ex.price, cur)}\n"
            f"Value: {money(value, cur)}{pnl}\n"
            f"Time: {when}")


def build_summary_message(ib, tz_label: str = "", now=None) -> str:
    """Snapshot of every open position plus account-level totals.

    `now` is injectable for tests; it defaults to the local wall clock."""
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    if tz_label:
        stamp += f" {tz_label}"
    header = f"📊 <b>Open Positions</b>\n<i>As at {stamp}</i>"
    items = [p for p in ib.portfolio() if p.position != 0]
    if not items:
        return f"{header}\nNone — flat across the account."

    # Positions are priced in their CONTRACT's currency while account totals are
    # in the base currency, so convert before summing -- and show the base-currency
    # equivalent on foreign positions so the listed lines add up to the totals.
    base = base_currency(ib)
    rates = fx_rates(ib)

    lines = [header]
    total_pnl = 0.0
    total_val = 0.0
    pnl_complete = True   # False if a position had no FX rate and was left out
    for p in sorted(items, key=lambda x: (x.contract.symbol or "")):
        c = p.contract
        cur = c.currency or "USD"
        sym = "$" + html.escape(c.localSymbol or c.symbol)
        side = "LONG" if p.position > 0 else "SHORT"
        lines.append(f"\n<b>{sym}</b>  {side} {fmt_qty(abs(p.position))}")
        avg = f"  Avg {money(p.averageCost, cur)}"
        if is_usable(p.marketPrice) and p.marketPrice:
            avg += f"  →  Mkt {money(p.marketPrice, cur)}"
        lines.append(avg)
        if is_usable(p.marketValue):
            conv = to_base(p.marketValue, cur, base, rates)
            if conv is not None:
                total_val += conv
            approx = f"  (≈ {money(conv, base)})" if conv is not None and cur != base else ""
            lines.append(f"  Market value: {money(p.marketValue, cur)}{approx}")
        if is_usable(p.unrealizedPNL):
            conv = to_base(p.unrealizedPNL, cur, base, rates)
            if conv is None:
                pnl_complete = False   # never add an unconverted foreign amount
            else:
                total_pnl += conv
            approx = f"  (≈ {smoney(conv, base)})" if conv is not None and cur != base else ""
            lines.append(f"  uPnL: <b>{smoney(p.unrealizedPNL, cur)}</b>{approx}")

    # Account-level lines from the live account feed. Cash is negative on margin.
    lines.append("")  # blank separator
    cash = acct_value(ib, "TotalCashValue", base) or acct_value(ib, "TotalCashValue")
    if cash:
        lines.append(f"<b>Cash Positions:</b> {money(cash[0], cash[1])}")
    nlv = acct_value(ib, "NetLiquidation")  # true account value (positions + cash)
    nlv_val = money(nlv[0], nlv[1]) if nlv else money(total_val, base)
    lines.append(f"<b>Total Account Value:</b> {nlv_val}")
    # Prefer IBKR's own base-currency total (matches TWS to the cent); fall back
    # to the converted sum above.
    upnl = acct_value(ib, "UnrealizedPnL", "BASE") or acct_value(ib, "UnrealizedPnL", base)
    if upnl:
        pnl_text = smoney(upnl[0], base)
    else:
        pnl_text = smoney(total_pnl, base)
        if not pnl_complete:
            pnl_text += "  <i>(excl. positions with no FX rate)</i>"
    lines.append(f"<b>Total unrealized P&amp;L:</b> {pnl_text}")
    return "\n".join(lines)
