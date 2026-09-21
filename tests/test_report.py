"""Tests for the message/report layer.

These cover the logic that is easy to get wrong and expensive to get wrong in
production: the IBKR "no value" sentinel, the open-vs-close inference, and
multi-currency conversion. Everything runs against lightweight fakes — no IB
Gateway, no network, no environment variables.
"""
import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ibkr_report import (
    UNSET,
    acct_value,
    base_currency,
    build_fill_message,
    build_summary_message,
    fmt_qty,
    fx_rates,
    is_usable,
    money,
    smoney,
    to_base,
)


# --- Fakes -------------------------------------------------------------------
def av(tag, value, currency=""):
    return SimpleNamespace(tag=tag, value=str(value), currency=currency)


def portfolio_item(symbol, position, avg, price, mkt_value, upnl, currency="USD"):
    return SimpleNamespace(
        position=position,
        averageCost=avg,
        marketPrice=price,
        marketValue=mkt_value,
        unrealizedPNL=upnl,
        contract=SimpleNamespace(symbol=symbol, localSymbol=symbol, currency=currency),
    )


class FakeIB:
    def __init__(self, portfolio=(), account_values=()):
        self._portfolio = list(portfolio)
        self._account_values = list(account_values)

    def portfolio(self):
        return self._portfolio

    def accountValues(self):
        return self._account_values


def fill(symbol="AAPL", side="BOT", shares=10, price=100.0):
    return SimpleNamespace(
        execution=SimpleNamespace(
            side=side, shares=shares, price=price, execId="e1",
            time=datetime(2026, 3, 1, 14, 30, tzinfo=UTC)),
        contract=SimpleNamespace(symbol=symbol, localSymbol=symbol, secType="STK"),
    )


def report(realized, currency="USD"):
    return SimpleNamespace(realizedPNL=realized, currency=currency, execId="e1")


# --- Scalar formatting -------------------------------------------------------
@pytest.mark.parametrize("qty,expected", [
    (100, "100"), (100.0, "100"), (12.5, "12.5"), (0.001, "0.001"), (1000, "1000"),
])
def test_fmt_qty_strips_trailing_zeros(qty, expected):
    assert fmt_qty(qty) == expected


@pytest.mark.parametrize("amount,currency,expected", [
    (1234.56, "USD", "$1,234.56"),
    (1234.56, "", "$1,234.56"),          # empty currency is treated as USD
    (1234.56, "HKD", "1,234.56 HKD"),
    (-500.0, "USD", "-$500.00"),         # negative cash on margin
    (-500.0, "SGD", "-500.00 SGD"),
    (0, "USD", "$0.00"),
])
def test_money(amount, currency, expected):
    assert money(amount, currency) == expected


@pytest.mark.parametrize("amount,currency,expected", [
    (1234.56, "USD", "+$1,234.56"),
    (-1234.56, "USD", "-$1,234.56"),
    (0, "USD", "+$0.00"),                # zero reads as non-negative
    (99.5, "HKD", "+99.50 HKD"),
])
def test_smoney_is_always_signed(amount, currency, expected):
    assert smoney(amount, currency) == expected


# --- The IBKR sentinel -------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (0.0, True), (-1.5, True), (1e6, True),
    (None, False),
    (float("nan"), False),
    (1.7976931348623157e308, False),     # the actual IBKR "unset" sentinel
    (UNSET * 10, False),
])
def test_is_usable(value, expected):
    assert is_usable(value) is expected


def test_is_usable_rejects_nan_without_math_isnan():
    assert not is_usable(math.nan)


# --- Open vs close inference (the load-bearing rule) -------------------------
def test_zero_realized_pnl_is_an_opening_trade():
    """ib_async reports 0.0 (not the sentinel) for opening fills. Treating that
    as a close would mislabel every opening trade."""
    msg = build_fill_message(fill(), report(0.0))
    assert "OPENED" in msg
    assert "CLOSED" not in msg
    assert "Realized" not in msg


def test_sentinel_realized_pnl_is_an_opening_trade():
    msg = build_fill_message(fill(), report(1.7976931348623157e308))
    assert "OPENED" in msg


def test_none_realized_pnl_is_an_opening_trade():
    assert "OPENED" in build_fill_message(fill(), report(None))


def test_nonzero_realized_pnl_is_a_close_with_pnl():
    msg = build_fill_message(fill(side="SLD"), report(250.75))
    assert "CLOSED" in msg
    assert "+$250.75" in msg


def test_negative_realized_pnl_is_a_close():
    msg = build_fill_message(fill(side="SLD"), report(-80.25))
    assert "CLOSED" in msg
    assert "-$80.25" in msg


def test_fill_message_renders_side_quantity_and_value():
    msg = build_fill_message(fill(side="BOT", shares=10, price=100.0), report(0.0))
    assert "BUY 10 @ $100.00" in msg
    assert "Value: $1,000.00" in msg


def test_fill_message_sell_side_label():
    assert "SELL " in build_fill_message(fill(side="SLD"), report(0.0))


def test_fill_message_escapes_html_in_symbol():
    f = fill(symbol="<script>")
    assert "<script>" not in build_fill_message(f, report(0.0))
    assert "&lt;script&gt;" in build_fill_message(f, report(0.0))


def test_fill_message_tz_label_is_optional():
    assert "SGT" in build_fill_message(fill(), report(0.0), "SGT")
    assert build_fill_message(fill(), report(0.0), "").rstrip()[-1].isdigit()


def test_fill_message_uses_contract_currency():
    msg = build_fill_message(fill(price=50.0), report(0.0, currency="HKD"))
    assert "50.00 HKD" in msg
    assert "$" not in msg.split("Time:")[0].replace("$AAPL", "")


# --- Currency conversion -----------------------------------------------------
def test_to_base_passes_through_base_currency():
    assert to_base(100.0, "USD", "USD", {}) == 100.0


def test_to_base_treats_empty_currency_as_base():
    assert to_base(100.0, "", "USD", {}) == 100.0


def test_to_base_converts_with_rate():
    assert to_base(100.0, "HKD", "USD", {"HKD": 0.128}) == pytest.approx(12.8)


def test_to_base_returns_none_when_rate_unknown():
    """A missing rate must NOT silently fall through as 1:1 — that would
    overstate the total."""
    assert to_base(100.0, "JPY", "USD", {"HKD": 0.128}) is None


def test_fx_rates_skips_base_and_blank_rows():
    ib = FakeIB(account_values=[
        av("ExchangeRate", 1.0, "BASE"),
        av("ExchangeRate", 0.128, "HKD"),
        av("ExchangeRate", "", "SGD"),
        av("ExchangeRate", "not-a-number", "JPY"),
        av("NetLiquidation", 1000, "USD"),
    ])
    assert fx_rates(ib) == {"HKD": 0.128}


def test_acct_value_pins_by_currency():
    ib = FakeIB(account_values=[
        av("UnrealizedPnL", 10, "HKD"),
        av("UnrealizedPnL", 99, "BASE"),
    ])
    assert acct_value(ib, "UnrealizedPnL", "BASE") == (99.0, "BASE")
    assert acct_value(ib, "UnrealizedPnL", "HKD") == (10.0, "HKD")
    assert acct_value(ib, "UnrealizedPnL") == (10.0, "HKD")   # first populated row


def test_acct_value_missing_tag_returns_none():
    assert acct_value(FakeIB(), "NetLiquidation") is None


def test_base_currency_from_net_liquidation():
    ib = FakeIB(account_values=[av("NetLiquidation", 1000, "SGD")])
    assert base_currency(ib) == "SGD"


def test_base_currency_defaults_to_usd():
    assert base_currency(FakeIB()) == "USD"
    assert base_currency(FakeIB(account_values=[av("NetLiquidation", 1, "BASE")])) == "USD"


# --- Summary -----------------------------------------------------------------
def test_summary_reports_flat_account():
    msg = build_summary_message(FakeIB())
    assert "flat across the account" in msg


def test_summary_ignores_zero_positions():
    ib = FakeIB(portfolio=[portfolio_item("AAPL", 0, 1, 1, 1, 1)])
    assert "flat across the account" in build_summary_message(ib)


def test_summary_renders_long_and_short():
    ib = FakeIB(portfolio=[
        portfolio_item("AAPL", 10, 100.0, 110.0, 1100.0, 100.0),
        portfolio_item("TSLA", -5, 200.0, 190.0, -950.0, 50.0),
    ])
    msg = build_summary_message(ib)
    assert "$AAPL</b>  LONG 10" in msg
    assert "$TSLA</b>  SHORT 5" in msg


def test_summary_shows_base_equivalent_for_foreign_positions():
    ib = FakeIB(
        portfolio=[portfolio_item("0700", 100, 300.0, 400.0, 40000.0, 10000.0, "HKD")],
        account_values=[av("NetLiquidation", 5120, "USD"),
                        av("ExchangeRate", 0.128, "HKD")],
    )
    msg = build_summary_message(ib)
    assert "40,000.00 HKD" in msg
    assert "≈ $5,120.00" in msg          # 40000 * 0.128


def test_summary_prefers_ibkr_base_unrealized_total():
    """IBKR's own BASE figure wins over our converted sum (it matches TWS)."""
    ib = FakeIB(
        portfolio=[portfolio_item("AAPL", 10, 100.0, 110.0, 1100.0, 100.0)],
        account_values=[av("NetLiquidation", 1100, "USD"),
                        av("UnrealizedPnL", 123.45, "BASE")],
    )
    assert "+$123.45" in build_summary_message(ib)


def test_summary_flags_incomplete_total_when_fx_rate_missing():
    """A position in an unconvertible currency must be disclosed, not dropped
    silently into a wrong-looking total."""
    ib = FakeIB(
        portfolio=[portfolio_item("7203", 100, 2000.0, 2100.0, 210000.0, 10000.0, "JPY")],
        account_values=[],               # no ExchangeRate row for JPY
    )
    msg = build_summary_message(ib)
    assert "excl. positions with no FX rate" in msg


def test_summary_does_not_flag_when_all_rates_present():
    ib = FakeIB(
        portfolio=[portfolio_item("AAPL", 10, 100.0, 110.0, 1100.0, 100.0)],
        account_values=[],
    )
    assert "excl. positions with no FX rate" not in build_summary_message(ib)


def test_summary_omits_sentinel_valued_fields():
    sentinel = 1.7976931348623157e308
    ib = FakeIB(portfolio=[
        portfolio_item("AAPL", 10, 100.0, sentinel, sentinel, sentinel)])
    msg = build_summary_message(ib)
    assert "Mkt" not in msg
    assert "Market value" not in msg
    assert "uPnL" not in msg
    assert "$AAPL" in msg                # the position itself still appears


def test_summary_includes_cash_and_account_value():
    ib = FakeIB(
        portfolio=[portfolio_item("AAPL", 10, 100.0, 110.0, 1100.0, 100.0)],
        account_values=[av("NetLiquidation", 12345.67, "USD"),
                        av("TotalCashValue", -500.0, "USD")],
    )
    msg = build_summary_message(ib)
    assert "Cash Positions:</b> -$500.00" in msg
    assert "Total Account Value:</b> $12,345.67" in msg


def test_summary_timestamp_is_injectable():
    msg = build_summary_message(FakeIB(), "SGT", now=datetime(2026, 3, 1, 17, 0, 0))
    assert "2026-03-01 17:00:00 SGT" in msg


def test_summary_escapes_html_in_symbol():
    ib = FakeIB(portfolio=[portfolio_item("<b>", 1, 1.0, 1.0, 1.0, 1.0)])
    assert "&lt;b&gt;" in build_summary_message(ib)


# --- Scheduling --------------------------------------------------------------
def test_seconds_until_later_today():
    from ibkr_telegram_notifier import _seconds_until  # noqa: PLC0415
    now = datetime(2026, 3, 1, 9, 0, 0)
    assert _seconds_until("17:00", now) == 8 * 3600


def test_seconds_until_rolls_to_tomorrow_when_time_has_passed():
    from ibkr_telegram_notifier import _seconds_until  # noqa: PLC0415
    now = datetime(2026, 3, 1, 18, 0, 0)
    assert _seconds_until("17:00", now) == 23 * 3600


def test_seconds_until_exact_match_rolls_forward():
    """At exactly the target minute, schedule the NEXT day — not a zero-length
    sleep that would re-fire in a tight loop."""
    from ibkr_telegram_notifier import _seconds_until  # noqa: PLC0415
    now = datetime(2026, 3, 1, 17, 0, 0)
    assert _seconds_until("17:00", now) == timedelta(days=1).total_seconds()
