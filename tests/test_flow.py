"""组合会计正确性（Spec §12-4）：现金守恒、FIFO、手算场景。"""

from __future__ import annotations

from datetime import date

import pytest

from sellput.instruments import OptionRight, OptionSpec, OptionStyle
from sellput.margin import SimplifiedRegTMargin
from sellput.market_data import MarketSnapshot, OptionQuote, Session, UnderlierQuote
from sellput.portfolio import Portfolio
from sellput.pricing import BlackScholesEngine

EXPIRY = date(2025, 2, 21)


def spec(strike: float = 95.0) -> OptionSpec:
    return OptionSpec("SPY", EXPIRY, strike, OptionRight.PUT, OptionStyle.AMERICAN)


def test_open_short_put_cash_and_premium():
    p = Portfolio(100_000.0)
    p.open_option(
        spec=spec(), qty=-1, price=2.0, day=date(2025, 1, 6),
        commission=0.0, iv=0.2, dte=30, delta=-0.2,
    )
    assert p.cash == pytest.approx(100_200.0)
    pos = p.options[spec().option_id]
    assert pos.qty == -1
    assert pos.avg_price == pytest.approx(2.0)


def test_close_short_put_at_higher_price_realizes_loss():
    p = Portfolio(100_000.0)
    p.open_option(
        spec=spec(), qty=-1, price=2.0, day=date(2025, 1, 6),
        commission=0.0, iv=0.2, dte=30, delta=-0.2,
    )
    realized = p.close_option(
        spec=spec(), qty=1, price=3.0, day=date(2025, 1, 7),
        commission=0.0, iv=0.25, reason="close",
    )
    assert realized == pytest.approx(-100.0)
    assert p.cash == pytest.approx(99_900.0)  # +200 权利金 −300 买回
    assert p.options == {}
    t = p.trades[0]
    assert t.exit_reason == "close"
    assert t.pnl == pytest.approx(-100.0)
    assert t.qty == -1


def test_commissions_flow_through():
    p = Portfolio(100_000.0)
    p.open_option(
        spec=spec(), qty=-2, price=2.0, day=date(2025, 1, 6),
        commission=1.0, iv=0.2, dte=30, delta=-0.2,
    )
    assert p.cash == pytest.approx(100_400.0 - 1.0)
    realized = p.close_option(
        spec=spec(), qty=2, price=1.5, day=date(2025, 1, 7),
        commission=1.0, iv=0.2, reason="close",
    )
    # (1.5 − 2.0) × (−2) × 100 − 1.0 = +99.0
    assert realized == pytest.approx(99.0)
    assert p.cash == pytest.approx(100_400.0 - 1.0 - 300.0 - 1.0)
    assert p.trades[0].commissions == pytest.approx(2.0)


def test_fifo_partial_close():
    p = Portfolio(100_000.0)
    s = spec()
    p.open_option(
        spec=s, qty=-1, price=2.0, day=date(2025, 1, 6),
        commission=0.0, iv=0.2, dte=30, delta=-0.2,
    )
    p.open_option(
        spec=s, qty=-1, price=1.5, day=date(2025, 1, 7),
        commission=0.0, iv=0.2, dte=29, delta=-0.2,
    )
    realized = p.close_option(
        spec=s, qty=1, price=1.8, day=date(2025, 1, 8),
        commission=0.0, iv=0.2, reason="close",
    )
    # FIFO：先配对第一笔（entry 2.0）→ (1.8−2.0)×(−1)×100 = +20
    assert realized == pytest.approx(20.0)
    assert p.trades[0].entry_price == pytest.approx(2.0)
    pos = p.options[s.option_id]
    assert pos.qty == -1
    assert pos.avg_price == pytest.approx(1.5)  # 部分平仓后按剩余 lot 重算


def test_mark_to_market_unrealized_and_greeks():
    p = Portfolio(100_000.0)
    s = spec()
    p.open_option(
        spec=s, qty=-1, price=2.0, day=date(2025, 1, 6),
        commission=0.0, iv=0.2, dte=30, delta=-0.2,
    )
    snap = MarketSnapshot(
        date=date(2025, 1, 7),
        session=Session.CLOSE,
        underlier=UnderlierQuote("SPY", 100.0, 100.0),
        option_quotes={s.option_id: OptionQuote(2.4, 2.6, 2.5, 0, 0)},
        rate=0.04,
        dividend_yield=0.0,
    )
    mark = p.mark_to_market(snap, BlackScholesEngine(), 0.0)
    pos = p.options[s.option_id]
    assert pos.unrealized_pnl == pytest.approx((2.5 - 2.0) * (-1) * 100)
    assert p.positions_value() == pytest.approx(2.5 * (-1) * 100)
    assert p.equity() == pytest.approx(p.cash + p.positions_value())
    assert mark.greeks["delta"] > 0  # 空头 Put 组合 delta 为正（−Δ × 空头）


def test_margin_used():
    p = Portfolio(100_000.0)
    p.open_option(
        spec=spec(strike=95.0), qty=-1, price=2.0, day=date(2025, 1, 6),
        commission=0.0, iv=0.2, dte=30, delta=-0.2,
    )
    m = SimplifiedRegTMargin()
    used = p.margin_used(m, "equity", 100.0)
    # max(0.2×100 − 5 + 2, 0.1×95 + 2) = max(17, 11.5) = 17/share → 1700
    assert used == pytest.approx(1700.0)


def test_cash_conservation_over_sequence():
    p = Portfolio(100_000.0)
    s = spec()
    p.open_option(
        spec=s, qty=-1, price=2.0, day=date(2025, 1, 6),
        commission=0.65, iv=0.2, dte=30, delta=-0.2,
    )
    p.close_option(
        spec=s, qty=1, price=1.8, day=date(2025, 1, 7),
        commission=0.65, iv=0.2, reason="close",
    )
    total = sum(t.pnl for t in p.trades) + sum(e.realized_pnl for e in p.equities.values())
    assert p.cash == pytest.approx(100_000.0 + total)
