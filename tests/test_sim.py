"""金标准场景与生命周期（Spec §12-4 / §12-5 / §12-6 / §12-3 确定性）。"""

from __future__ import annotations

from datetime import date

import pytest

from sellput.config import (
    AccountConfig,
    BacktestConfig,
    DataConfig,
    SellPutParams,
    SimulationConfig,
)
from sellput.execution import OrderAction, OrderIntent, OrderReason
from sellput.instruments import NyseCalendar, OptionRight, OptionSpec, OptionStyle, Settlement
from sellput.market_data import synthetic_provider_from_config
from sellput.pricing import BlackScholesEngine
from sellput.sim import SimulationEngine
from tests.conftest import (
    OnceStrategy,
    WeekdayCalendar,
    constant_world,
    make_config,
    make_put_quotes,
)
from tests.conftest import (
    Session as _S,
)

EXPIRY = date(2025, 3, 21)  # 金标准场景用（OnceStrategy，无选期窗口约束）
LOOP_EXPIRY = date(2025, 2, 21)  # Sell Put 全链路用：01-08 决策时 dte = 30
STRIKES = [90.0, 92.0, 94.0, 95.0, 96.0, 98.0, 100.0, 102.0, 104.0]


def put_premium(*, today: date, S: float, K: float, expiry: date = EXPIRY) -> float:
    return BlackScholesEngine().price(
        S=S, K=K, T=(expiry - today).days / 365.0, r=0.04, q=0.0,
        sigma=0.20, right=OptionRight.PUT,
    ).price


# ---- 金标准：OTM 到期（权利金全收） ----

def test_golden_otm_expire(monkeypatch):
    cal = WeekdayCalendar()
    sessions = cal.sessions(date(2025, 1, 6), EXPIRY)
    prov = constant_world(cal, sessions, expiry=EXPIRY, strikes=[95.0], S=100.0)
    cfg = make_config(start=sessions[0], end=sessions[-1])
    spec_ = OptionSpec("SPY", EXPIRY, 95.0, OptionRight.PUT, OptionStyle.AMERICAN)
    monkeypatch.setattr(
        "sellput.sim.build_strategy",
        lambda c: OnceStrategy([OrderIntent(OrderAction.OPEN, spec_, 1, reason=OrderReason.ENTRY)]),
    )
    result = SimulationEngine(cfg).run(prov, cal)

    p0 = put_premium(today=sessions[0], S=100.0, K=95.0)
    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.exit_reason == "expire"
    assert t.pnl == pytest.approx(p0 * 100.0)
    assert result.states[-1].cash == pytest.approx(100_000.0 + p0 * 100.0)
    assert result.states[-1].equity == pytest.approx(result.states[-1].cash)


# ---- 金标准：ITM 实物指派 → 次日开盘卖出 ----

def test_golden_itm_assign_sell_next_open(monkeypatch):
    cal = WeekdayCalendar()
    sessions = cal.sessions(date(2025, 3, 10), date(2025, 3, 24))
    prov = constant_world(cal, sessions, expiry=EXPIRY, strikes=[95.0], S=100.0)
    # 到期日收盘 92 → 指派；次日开盘 91 → 卖出
    prov.set(EXPIRY, _S.CLOSE, 92.0, quotes={}, prev_close=100.0)
    next_day = cal.next_session(EXPIRY)
    prov.set(
        next_day, _S.OPEN, 91.0,
        quotes=make_put_quotes(today=next_day, S=91.0, expiry=EXPIRY, strikes=[95.0], iv=0.2),
        prev_close=92.0,
    )
    prov.set(next_day, _S.CLOSE, 91.0, quotes={}, prev_close=92.0)

    cfg = make_config(start=sessions[0], end=sessions[-1])
    spec_ = OptionSpec("SPY", EXPIRY, 95.0, OptionRight.PUT, OptionStyle.AMERICAN)
    monkeypatch.setattr(
        "sellput.sim.build_strategy",
        lambda c: OnceStrategy([OrderIntent(OrderAction.OPEN, spec_, 1, reason=OrderReason.ENTRY)]),
    )
    result = SimulationEngine(cfg).run(prov, cal)

    p0 = put_premium(today=sessions[0], S=100.0, K=95.0)
    t = result.trades[0]
    assert t.exit_reason == "assign"
    assert t.pnl == pytest.approx(p0 * 100.0)  # 权利金全收；股票腿承担 K−卖出价 之差
    # 股票腿：95 接货 → 91 卖出 → realized = (91−95)×100 = −400
    assert result.states[-1].cash == pytest.approx(100_000.0 + p0 * 100.0 - 400.0)
    # 到期日状态：cash = 100000 + p0*100 − 9500，持 100 股（92 元）
    expiry_state = next(s for s in result.states if s.date == EXPIRY)
    assert expiry_state.cash == pytest.approx(100_000.0 + p0 * 100.0 - 9_500.0)
    assert expiry_state.positions_value == pytest.approx(92.0 * 100.0)


# ---- 金标准：现金结算（SPX 类，M0 简化：按收盘价） ----

def test_golden_cash_settled_itm(monkeypatch):
    cal = WeekdayCalendar()
    sessions = cal.sessions(date(2025, 3, 10), EXPIRY)
    prov = constant_world(cal, sessions, expiry=EXPIRY, strikes=[95.0], S=100.0)
    prov.set(EXPIRY, _S.CLOSE, 92.0, quotes={}, prev_close=100.0)

    cfg = make_config(symbol="SPX", start=sessions[0], end=sessions[-1])
    spec_ = OptionSpec(
        "SPX", EXPIRY, 95.0, OptionRight.PUT, OptionStyle.EUROPEAN, settlement=Settlement.CASH
    )
    monkeypatch.setattr(
        "sellput.sim.build_strategy",
        lambda c: OnceStrategy([OrderIntent(OrderAction.OPEN, spec_, 1, reason=OrderReason.ENTRY)]),
    )
    result = SimulationEngine(cfg).run(prov, cal)

    p0 = put_premium(today=sessions[0], S=100.0, K=95.0)
    t = result.trades[0]
    assert t.exit_reason == "assign_cash"
    # 期权腿以内在价值 3.0 退出 → pnl = (3 − p0)×(−1)×100
    assert t.pnl == pytest.approx((3.0 - p0) * (-1) * 100.0)
    # 现金轧差：+p0×100 − 3×100
    assert result.states[-1].cash == pytest.approx(100_000.0 + p0 * 100.0 - 300.0)


# ---- 全链路：Sell Put 四规则（真实策略 + 常量世界） ----

def test_sellput_full_loop_dte_exit():
    cal = WeekdayCalendar()
    sessions = cal.sessions(date(2025, 1, 8), LOOP_EXPIRY)
    prov = constant_world(cal, sessions, expiry=LOOP_EXPIRY, strikes=STRIKES, S=100.0)
    cfg = make_config(start=sessions[0], end=sessions[-1])
    cfg.strategy.params = SellPutParams(profit_target_pct=0.0)  # 关闭止盈 → 只测 DTE 强退
    result = SimulationEngine(cfg).run(prov, cal)

    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.exit_reason == "dte_exit"
    # 开仓参数：DTE 32（WeekdayCalendar 无假日；01-08 → 02-21）、delta 独立核验
    assert t.dte_at_entry == 32
    bs = BlackScholesEngine()
    T = (LOOP_EXPIRY - sessions[0]).days / 365.0
    expected_strike = min(
        STRIKES,
        key=lambda K: abs(
            bs.price(S=100.0, K=K, T=T, r=0.04, q=0.0, sigma=0.20,
                     right=OptionRight.PUT).greeks.delta + 0.20
        ),
    )
    assert t.spec.strike == expected_strike
    # 平仓盈亏手算：买回价 = 平仓日 BS mid（dte=2），theta 衰减带来正收益
    assert t.pnl > 0


def test_sellput_take_profit_full_loop():
    cal = WeekdayCalendar()
    sessions = cal.sessions(date(2025, 1, 8), date(2025, 2, 7))
    prov = constant_world(cal, sessions, expiry=LOOP_EXPIRY, strikes=STRIKES, S=100.0)
    cfg = make_config(start=sessions[0], end=sessions[-1])

    # 先算策略会选的行权价（同 test_entry_selects_dte_and_delta）
    bs = BlackScholesEngine()
    T = (LOOP_EXPIRY - sessions[0]).days / 365.0
    best_k = min(
        STRIKES,
        key=lambda K: abs(
            bs.price(S=100.0, K=K, T=T, r=0.04, q=0.0, sigma=0.20,
                     right=OptionRight.PUT).greeks.delta + 0.20
        ),
    )
    entry_mid = bs.price(
        S=100.0, K=best_k, T=T, r=0.04, q=0.0, sigma=0.20, right=OptionRight.PUT
    ).price
    # 把 sessions[3] 收盘与 sessions[4] 开盘的该腿 mid 压到 50%（早于自然衰减触发）
    oid = (LOOP_EXPIRY, best_k, OptionRight.PUT)
    for d, sess in ((sessions[3], _S.CLOSE), (sessions[4], _S.OPEN)):
        snap = prov.get(d, sess)
        q = snap.option_quotes[oid]
        new_q = type(q)(
            0.5 * entry_mid, 0.5 * entry_mid, 0.5 * entry_mid, q.volume, q.open_interest
        )
        prov.set(
            d, sess, snap.underlier.price,
            {**{k: (v.bid, v.ask) for k, v in snap.option_quotes.items() if k != oid},
             oid: (new_q.bid, new_q.ask)},
            prev_close=snap.underlier.prev_close,
        )

    result = SimulationEngine(cfg).run(prov, cal)
    assert result.trades
    t = result.trades[0]
    assert t.exit_reason == "take_profit"
    assert t.exit_date == sessions[4]  # 决策于 sessions[3] 收盘 → sessions[4] 开盘平仓
    assert t.spec.strike == best_k
    assert t.dte_at_entry == 32  # WeekdayCalendar 无假日：01-08 → 02-21
    # 张数：按 50% 资金分配，req = (15 + 决策日权利金)×100；决策日用 close(01-03) 链
    premium_est = bs.price(
        S=100.0, K=best_k, T=(LOOP_EXPIRY - cal.prev_session(sessions[0])).days / 365.0,
        r=0.04, q=0.0, sigma=0.20, right=OptionRight.PUT,
    ).price
    expected_contracts = int(100_000.0 * 0.5 / ((15.0 + premium_est) * 100.0))
    assert abs(t.qty) == expected_contracts
    assert t.pnl == pytest.approx(0.5 * entry_mid * abs(t.qty) * 100.0)


# ---- 保证金不足 → 拒绝开仓 ----

def test_margin_reject(monkeypatch):
    cal = WeekdayCalendar()
    sessions = cal.sessions(date(2025, 1, 6), date(2025, 1, 10))
    prov = constant_world(cal, sessions, expiry=EXPIRY, strikes=[95.0], S=100.0)
    cfg = BacktestConfig(
        data=DataConfig(symbol="SPY", start=sessions[0], end=sessions[-1]),
        simulation=SimulationConfig(
            slippage_bps=0.0, commission_per_contract=0.0, commission_per_order=0.0
        ),
        account=AccountConfig(starting_cash=1_000.0),
    )
    spec_ = OptionSpec("SPY", EXPIRY, 95.0, OptionRight.PUT, OptionStyle.AMERICAN)
    monkeypatch.setattr(
        "sellput.sim.build_strategy",
        lambda c: OnceStrategy([OrderIntent(OrderAction.OPEN, spec_, 3, reason=OrderReason.ENTRY)]),
    )
    result = SimulationEngine(cfg).run(prov, cal)
    assert result.orders[0].status == "rejected"
    assert result.orders[0].reject_reason == "insufficient_margin"
    assert result.trades == []
    assert all(not s.positions for s in result.states)


# ---- 确定性（Spec §12-3 之一）：同 config + seed 逐位一致 ----

def test_determinism():
    cfg = make_config(start=date(2024, 1, 1), end=date(2024, 6, 30))
    cal = NyseCalendar()
    p1 = synthetic_provider_from_config(cfg.data, cfg.market.rate)
    p2 = synthetic_provider_from_config(cfg.data, cfg.market.rate)
    r1 = SimulationEngine(cfg).run(p1, cal)
    r2 = SimulationEngine(cfg).run(p2, cal)
    assert [s.equity for s in r1.states] == [s.equity for s in r2.states]
    assert [(t.spec.option_id, t.pnl, t.exit_reason) for t in r1.trades] == [
        (t.spec.option_id, t.pnl, t.exit_reason) for t in r2.trades
    ]


# ---- 资金守恒（全链路） ----

def test_cash_conservation_full_loop(monkeypatch):
    cal = WeekdayCalendar()
    sessions = cal.sessions(date(2025, 3, 10), date(2025, 3, 24))
    prov = constant_world(cal, sessions, expiry=EXPIRY, strikes=[95.0], S=100.0)
    prov.set(EXPIRY, _S.CLOSE, 92.0, quotes={}, prev_close=100.0)
    next_day = cal.next_session(EXPIRY)
    prov.set(
        next_day, _S.OPEN, 91.0,
        quotes=make_put_quotes(today=next_day, S=91.0, expiry=EXPIRY, strikes=[95.0], iv=0.2),
        prev_close=92.0,
    )
    prov.set(next_day, _S.CLOSE, 91.0, quotes={}, prev_close=92.0)
    cfg = make_config(start=sessions[0], end=sessions[-1])
    spec_ = OptionSpec("SPY", EXPIRY, 95.0, OptionRight.PUT, OptionStyle.AMERICAN)
    monkeypatch.setattr(
        "sellput.sim.build_strategy",
        lambda c: OnceStrategy([OrderIntent(OrderAction.OPEN, spec_, 1, reason=OrderReason.ENTRY)]),
    )
    result = SimulationEngine(cfg).run(prov, cal)
    total = sum(t.pnl for t in result.trades)  # 期权腿已实现 PnL
    state = result.states[-1]
    # 守恒：equity = 起点 + Σ期权腿 PnL + Σ股票腿 realized（95 接货 → 91 卖出 = −400）
    assert state.equity == pytest.approx(100_000.0 + total - 400.0)
    assert state.equity == pytest.approx(state.cash + state.positions_value)
