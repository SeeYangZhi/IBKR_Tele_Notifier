# Architecture & design notes

Why this code is shaped the way it is. Most of what follows is a fact about the
IBKR API that is either undocumented or documented incorrectly, and each one
cost real debugging time. If you are modifying this project, read this first.

## Components

| File | Role |
|---|---|
| `ibkr_telegram_notifier.py` | The notifier: connection loop, fill notifications, daily summary, ops alerting. Long-running service. |
| `ibkr_ops_bot.py` | Telegram ops-control bot: `/positions`, `/resume`, `/pause`, `/status`. Separate service. |
| `ibkr_report.py` | Pure message formatting and report building, shared by both. No I/O, no environment reads. |

The split matters in two directions. `ibkr_report.py` has no side effects, so
the formatting and currency logic is unit-testable without an IB Gateway or any
credentials. And the ops bot is its own systemd service precisely so `/resume`
still works when the gateway and notifier are down — which is exactly when you
need it.

## Dependencies

`ib_async` (**not** `ib_insync`, which is unmaintained), `httpx`, `python-dotenv`.
Telegram is plain HTTP POSTs rather than `python-telegram-bot`: the bot sends a
handful of message types and long-polls one endpoint, which is not worth a
framework. Dependencies are managed with `uv`.

---

## Fills: open vs close

**The rule:** a usable, *non-zero* `realizedPNL` on the commission report means
the fill reduced a position → `CLOSED`, with P&L. Anything else → `OPENED`.

"Anything else" covers two distinct cases:

- the IBKR "no value" sentinel, approximately `1.8e308` (hence `UNSET = 1e307`
  and `is_usable()`), and
- a plain `0.0`.

**The trap:** the API documentation implies that opening trades carry the
sentinel. In practice `ib_async` reports `0.0` for them. If you treat `0.0` as
"a real number, therefore a close", every opening fill is labelled `CLOSED` with
a realized P&L of zero. The zero check is load-bearing, not defensive padding.

A genuine flat close also produces `0.0` and will be reported as `OPENED`. That
is a known and accepted trade-off: an exactly-zero realized P&L is vanishingly
rare, while opening trades are constant.

### Why there is both an event and a sweep

Fills arrive two ways:

1. **`commissionReportEvent`** — fires live, while connected.
2. **`sweep_fills()`** — runs every connection cycle (5s) over `ib.fills()`.

The sweep is not belt-and-braces. **`commissionReportEvent` does not re-fire for
fills that arrive during a (re)connect sync.** So when the gateway was down —
because you were using the IBKR mobile app, or the weekly re-auth lapsed — the
trades you made in the meantime appear in `ib.fills()` on reconnect but generate
no event. Without the sweep, they would never be reported. The sweep reads a
local cache, so it is cheap.

Both paths funnel through `_maybe_notify()`, deduped by `execId`.

### execId persistence

Sent `execId`s are persisted to `STATE_FILE` (atomically, via a temp file and
`os.replace`). This is what makes restart catch-up safe: on restart the notifier
re-reads all available fills, and the persisted set stops it re-blasting the
channel with trades it already reported.

The set is capped at `STATE_MAX_IDS` (default 5000), keeping the newest —
IBKR `execId`s sort chronologically, so a lexical sort is a time sort.

`secType == "CASH"` fills are skipped: those are IBKR's automatic FX
conversions, not trades you made.

---

## Currency

This is the subtlest part of the codebase and the easiest place to produce a
plausible-looking wrong number.

**The rule:** `PortfolioItem` fields (`marketPrice`, `marketValue`,
`averageCost`, `unrealizedPNL`) are denominated in the **contract's** currency.
Account-level tags are denominated in the account's **base** currency.

So a portfolio holding US and Hong Kong stocks yields market values in USD
*and* HKD from the same list. Adding them produces a number that is wrong but
entirely believable — which is the dangerous kind of wrong.

Every amount summed across positions therefore goes through `to_base()` first,
using live `ExchangeRate` rows from the account feed (`fx_rates()`).

**Missing rates degrade honestly.** If a currency has no rate, `to_base()`
returns `None` and the position is excluded from the total, which is then
labelled *"(excl. positions with no FX rate)"*. It never falls back to 1:1.

**Account tags need pinning by currency.** IBKR emits one row *per currency* for
many tags — `UnrealizedPnL` arrives as a `BASE` row plus one row per currency
held. `acct_value(ib, tag, cur)` takes the currency to pin the row you mean.
Grabbing the first matching row gives you whichever currency IBKR happened to
list first.

**Totals prefer IBKR's own figure.** For total unrealized P&L the code uses
`acct_value(ib, "UnrealizedPnL", "BASE")` when available — it matches TWS to the
cent — and falls back to the converted sum only when absent.

`base_currency()` reads the currency field of `NetLiquidation`, which is emitted
base-only, making it the reliable source for what "base" means on this account.

---

## The daily summary is the health check

At `DAILY_SUMMARY_TIME` the notifier reads `ib.portfolio()` live. Reading live
rather than from cached state is what lets the summary survive notifier and
gateway restarts.

If the gateway is unreachable at that moment, it posts a warning to the ops
channel instead. **That warning is the health signal.** It is deliberately not a
dumb heartbeat: a heartbeat tells you the notifier process is alive, which is
the less interesting fact. This tells you the whole chain — process, gateway,
API, IBKR session — was working at a known time each day. Please do not replace
it with a ping.

## Channel split

Two channels, by design:

- **Production channel** — trades and the healthy daily summary. Nothing else.
  It stays readable as a trade log.
- **Ops channel** (`OPS_BOT_TOKEN` / `OPS_CHAT_ID`) — the disconnected-summary
  warning, Telegram send failures, and forwarded `ERROR` logs.

`_OpsLogForwarder` is a logging handler at `ERROR` level that forwards to the
ops channel, with two filters: routine reconnect noise (`"Connect failed"`,
`"retry in"`) is dropped, since the gateway being down during mobile use is
normal, and identical messages are deduped for 15 minutes.

**`_post()` never logs.** If the low-level sender logged failures at `ERROR`, a
Telegram outage would trigger the forwarder, which would try to send to Telegram,
which would fail and log again. `send_ops()` logs its own failures at `WARNING`,
below the forwarder's threshold, which breaks the cycle.

## Connection handling

The notifier owns its reconnection: a 5-second loop, with a 60-second backoff
after a failed connect. Alerts do not need to be real-time, and the gateway is
routinely unavailable during mobile use, so aggressive retry buys nothing and
costs log noise.

`connectAsync` already subscribes to account updates for a single managed
account, which is what populates `portfolio()` and `accountValues()`. An explicit
`ib.reqAccountUpdates()` is not needed — the synchronous form that once lived
here always raised "event loop is already running" and silently did nothing.

`IB_CLIENT_ID` must equal the Gateway's **Master API Client ID**. That is what
makes the notifier see fills from *all* sources — manual clicks in TWS, API
orders, TradingView-routed orders — rather than only its own.

---

## Mobile coexistence and 2FA

IB Gateway and the IBKR mobile app cannot hold a session simultaneously. The
configuration makes the Gateway lose that contest gracefully:

- **`ExistingSessionDetectedAction=secondary`** — when a competing session
  appears, the Gateway **yields** instead of reclaiming. Logging in on your
  phone no longer kicks the Gateway off in a fight neither side wins.
- **`Restart=on-failure`** on `ibc-gateway.service`, *not* `Restart=always`. A
  yield is a clean exit (status 0), so systemd leaves the Gateway down. With
  `always`, systemd would relaunch it, it would try to log in, that would
  trigger a 2FA push, it would yield again — a cold-restart loop that sends you
  a push every thirty seconds. Real crashes exit non-zero and still auto-recover.
- **`ReloginAfterSecondFactorAuthenticationTimeout=no`** with
  `SecondFactorAuthenticationTimeout=300` — one login attempt sends exactly one
  push, with a five-minute window, and then stops.

**The operating model:** use the mobile app whenever you like; the Gateway
yields, stays down, and sends nothing. When you are done, `/resume` (or
`systemctl restart ibc-gateway`) brings it back with a single 2FA tap, and the
fill sweep reports anything you traded meanwhile.

That one tap is unavoidable — taking the session on mobile invalidates the
Gateway's token, so the next login is a fresh one.

The trade-off: a genuinely missed re-auth (say, the weekly one while you are
away) leaves the Gateway down until you resume it manually. The daily summary
warning is what tells you.

## Read-only enforcement

Two independent layers, described in [SECURITY.md](../SECURITY.md):

1. `ReadOnlyApi=yes` in the IBC config — IB Gateway rejects order-placing calls.
2. The application only ever reads.

`ReadOnlyLogin=no` is deliberate and is *not* a contradiction: a read-only
*login* restricts what account data is readable, which would break the position
and account-value reporting. The read-only *API* setting is the one that blocks
trading.
