"""Sell Put 四默认规则（Spec §12-6）：DTE30 / delta0.20 / 止盈50% / DTE≤3 强退。"""

from __future__ import annotations

from datetime import date

from sellput.config import SellPutParams, SizingConfig
from sellput.dividend import ContinuousYieldDividendModel
from sellput.execution import OrderAction, OrderReason
from sellput.instruments import OptionRight, OptionSpec, OptionStyle
from sellput.margin import SimplifiedRegTMargin
from sellput.market_data import MarketSnapshot, OptionQuote, Session, UnderlierQuote
from sellput.portfolio import Portfolio
from sellput.pricing import BlackScholesEngine
from sellput.strategy import SellPutStrategy, StrategyContext
from tests.conftest import WeekdayCalendar, make_put_quotes

EXPIRY = date(2025, 2, 21)      # 第三周五；01-08 决策时 dte = 30
FAR_EXPIRY = date(2025, 3, 21)  # 01-08 决策时 dte = 51
STRIKES = [90.0, 92.0, 94.0, 95.0, 96.0, 98.0, 100.0, 102.0, 104.0]
NO_TP = SellPutParams(profit_target_pct=0.0)  # 关闭止盈：专测 DTE 强退


def build_snapshot(today: date, quotes: dict) -> MarketSnapshot:
    option_quotes = {
        oid: OptionQuote(bid=b, ask=a, last=0.5 * (b + a), volume=0, open_interest=0)
        for oid, (b, a) in quotes.items()
    }
    return MarketSnapshot(
        date=today,
        session=Session.CLOSE,
        underlier=UnderlierQuote("SPY", 100.0, 100.0),
        option_quotes=option_quotes,
        rate=0.04,
        dividend_yield=0.0,
    )


def make_ctx(
    today: date,
    *,
    portfolio: Portfolio | None = None,
    mid_override: dict | None = None,
    params: SellPutParams | None = None,
    quotes: dict | None = None,
) -> tuple[SellPutStrategy, StrategyContext]:
    cal = WeekdayCalendar()
    params = params or SellPutParams()
    if quotes is None:
        quotes = make_put_quotes(today=today, S=100.0, expiry=EXPIRY, strikes=STRIKES, iv=0.20)
    if mid_override:
        quotes = {**quotes, **mid_override}
    strat = SellPutStrategy(params)
    ctx = StrategyContext(
        prev_close=build_snapshot(today, quotes),
        portfolio=portfolio or Portfolio(100_000.0),
        pricing=BlackScholesEngine(),
        dividend_model=ContinuousYieldDividendModel(0.0),
        calendar=cal,
        params=params,
        sizing=SizingConfig(),
        margin_model=SimplifiedRegTMargin(),
        underlier_kind="equity",
        first_session=today,
        last_session=today,
    )
    return strat, ctx


def open_short_put(portfolio: Portfolio, price: float = 2.0) -> OptionSpec:
    s = OptionSpec("SPY", EXPIRY, 95.0, OptionRight.PUT, OptionStyle.AMERICAN)
    portfolio.open_option(
        spec=s, qty=-1, price=price, day=date(2025, 1, 8),
        commission=0.0, iv=0.2, dte=30, delta=-0.2,
    )
    return s


def test_dte_exit_fires_at_threshold():
    # 2025-02-18（周二）→ 02-21：dte = 19,20,21 → 3 → 触发强退（关闭止盈避免先触发）
    p = Portfolio(100_000.0)
    open_short_put(p)
    strat, ctx = make_ctx(date(2025, 2, 18), portfolio=p, params=NO_TP)
    intents = strat.on_open(ctx)
    assert len(intents) == 1
    assert intents[0].action is OrderAction.CLOSE
    assert intents[0].reason is OrderReason.DTE_EXIT


def test_dte_exit_not_fired_above_threshold():
    # 2025-02-17（周一）→ 02-21：dte = 4 → 不触发
    p = Portfolio(100_000.0)
    open_short_put(p)
    strat, ctx = make_ctx(date(2025, 2, 17), portfolio=p, params=NO_TP)
    assert strat.on_open(ctx) == []


def test_take_profit_fires_at_50pct():
    p = Portfolio(100_000.0)
    s = open_short_put(p)
    # 2025-02-14：dte = 4 > 3；mid 压到 0.99（≤ 50% × 2.0）
    strat, ctx = make_ctx(
        date(2025, 2, 14), portfolio=p, mid_override={s.option_id: (0.99, 0.99)}
    )
    intents = strat.on_open(ctx)
    assert len(intents) == 1
    assert intents[0].reason is OrderReason.TAKE_PROFIT


def test_take_profit_not_fired_above_half():
    p = Portfolio(100_000.0)
    s = open_short_put(p)
    strat, ctx = make_ctx(
        date(2025, 2, 14), portfolio=p, mid_override={s.option_id: (1.01, 1.01)}
    )
    assert strat.on_open(ctx) == []


def test_priority_dte_over_take_profit():
    # 同时命中 DTE≤3 与止盈 → 强退优先（代码顺序即优先级）
    p = Portfolio(100_000.0)
    s = open_short_put(p)
    strat, ctx = make_ctx(
        date(2025, 2, 18), portfolio=p, mid_override={s.option_id: (0.5, 0.5)}
    )
    intents = strat.on_open(ctx)
    assert intents[0].reason is OrderReason.DTE_EXIT


def test_entry_selects_dte_and_delta():
    today = date(2025, 1, 8)  # dte(01-08 → 02-21) = 30
    quotes = make_put_quotes(today=today, S=100.0, expiry=EXPIRY, strikes=STRIKES, iv=0.20)
    quotes.update(
        make_put_quotes(today=today, S=100.0, expiry=FAR_EXPIRY, strikes=STRIKES, iv=0.20)
    )
    strat, ctx = make_ctx(today, quotes=quotes)
    intents = strat.on_open(ctx)
    assert len(intents) == 1
    intent = intents[0]
    assert intent.action is OrderAction.OPEN
    assert intent.reason is OrderReason.ENTRY
    # DTE：02-21（dte 30）比 03-21（dte 51）更接近目标
    assert intent.asset.expiry == EXPIRY
    # delta：独立用 BS 计算期望行权价
    bs = BlackScholesEngine()
    T = (EXPIRY - today).days / 365.0
    expected = min(
        STRIKES,
        key=lambda K: abs(
            bs.price(S=100.0, K=K, T=T, r=0.04, q=0.0, sigma=0.20,
                     right=OptionRight.PUT).greeks.delta + 0.20
        ),
    )
    assert intent.asset.strike == expected


def test_weekly_entry_frequency():
    quotes = make_put_quotes(
        today=date(2025, 1, 8), S=100.0, expiry=EXPIRY, strikes=STRIKES, iv=0.20
    )
    strat, ctx1 = make_ctx(date(2025, 1, 8), quotes=quotes)  # 周三，ISO 第 2 周
    assert any(i.reason is OrderReason.ENTRY for i in strat.on_open(ctx1))
    # 同一周再决策：不再开新仓（即使无持仓）
    _, ctx2 = make_ctx(date(2025, 1, 10), quotes=quotes)
    assert not any(i.reason is OrderReason.ENTRY for i in strat.on_open(ctx2))
    # 下一周：允许开仓
    _, ctx3 = make_ctx(date(2025, 1, 13), quotes=quotes)
    assert any(i.reason is OrderReason.ENTRY for i in strat.on_open(ctx3))


def test_max_open_positions():
    p = Portfolio(100_000.0)
    open_short_put(p)
    strat, ctx = make_ctx(date(2025, 1, 10), portfolio=p)
    assert not any(i.reason is OrderReason.ENTRY for i in strat.on_open(ctx))


def test_sizing_pct_allocated_floor():
    s = OptionSpec("SPY", EXPIRY, 95.0, OptionRight.PUT, OptionStyle.AMERICAN)
    quotes = make_put_quotes(
        today=date(2025, 1, 8), S=100.0, expiry=EXPIRY, strikes=[95.0], iv=0.20
    )
    mid = 0.5 * sum(quotes[s.option_id])
    req_share = max(0.2 * 100.0 - (100.0 - 95.0) + mid, 0.1 * 95.0 + mid)
    req_contract = req_share * 100.0

    strat, ctx = make_ctx(date(2025, 1, 8), portfolio=Portfolio(10_000.0), quotes=quotes)
    contracts = strat._size(ctx, s, 100.0)
    assert contracts == int(10_000.0 * 0.5 / req_contract)

    # 资金不足 → 0 张
    _, ctx2 = make_ctx(date(2025, 1, 8), portfolio=Portfolio(1_000.0), quotes=quotes)
    assert strat._size(ctx2, s, 100.0) == 0
