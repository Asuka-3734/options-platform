"""M1-B 多策略架构：Buy & Hold 全链路与股票会计（Spec §14-1..6）。"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from sellput.config import (
    AccountConfig,
    BacktestConfig,
    BuyHoldParams,
    SellPutParams,
    StrategyConfig,
)
from sellput.execution import FillModel, OrderReason
from sellput.instruments import EquitySpec, OptionRight, OptionSpec, OptionStyle
from sellput.portfolio import Portfolio
from sellput.sim import SimulationEngine, build_strategy
from sellput.strategy import BuyHoldStrategy, SellPutStrategy
from tests.conftest import Session as _S
from tests.conftest import WeekdayCalendar, constant_world, make_config

CAL = WeekdayCalendar()
SESSIONS = CAL.sessions(date(2025, 1, 6), date(2025, 1, 17))  # 10 个交易日
EXPIRY = date(2025, 2, 21)


def bh_cfg(**params) -> BacktestConfig:
    cfg = make_config(start=SESSIONS[0], end=SESSIONS[-1])
    cfg.strategy = StrategyConfig(type="buy_hold", params=BuyHoldParams(**params))
    return cfg


def bh_world():
    return constant_world(CAL, SESSIONS, expiry=EXPIRY, strikes=[95.0], S=100.0)


def run_bh(prov, **params):
    return SimulationEngine(bh_cfg(**params)).run(prov, CAL)


# ---- 生命周期（§14-2） ----

def test_buy_hold_lifecycle_single_trade():
    result = run_bh(bh_world())
    assert len(result.orders) == 2
    assert [o.status for o in result.orders] == ["filled", "filled"]
    assert result.orders[0].intent.reason.value == "entry"
    assert result.orders[1].intent.reason is OrderReason.HOLD_END
    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.asset_kind == "equity"
    assert isinstance(t.spec, EquitySpec) and t.spec.symbol == "SPY"
    assert t.exit_reason == "hold_end"
    assert t.qty == 1000  # 100% × 100,000 / 100（零滑点零手续费）
    assert t.entry_date == SESSIONS[0]
    assert t.exit_date == SESSIONS[-1]
    assert t.pnl == pytest.approx(0.0)
    assert result.states[-1].cash == pytest.approx(100_000.0)
    assert not result.states[-1].positions


def test_buy_hold_price_change_pnl():
    prov = bh_world()
    prov.set(SESSIONS[-1], _S.OPEN, 110.0, quotes={}, prev_close=100.0)
    prov.set(SESSIONS[-1], _S.CLOSE, 110.0, quotes={}, prev_close=100.0)
    result = run_bh(prov)
    t = result.trades[0]
    assert t.pnl == pytest.approx(10.0 * 1000)
    assert result.states[-1].cash == pytest.approx(110_000.0)


# ---- 参数与钳制（§14-4） ----

def test_buy_hold_allocation_param():
    result = run_bh(bh_world(), allocation=0.5)
    assert result.trades[0].qty == 500


def test_buy_hold_clamps_shares_to_cash():
    cfg = bh_cfg()
    cfg.account = AccountConfig(starting_cash=250.0)
    result = SimulationEngine(cfg).run(bh_world(), CAL)
    assert result.trades[0].qty == 2
    assert result.states[-1].cash == pytest.approx(250.0)


def test_buy_hold_sizing_uses_prev_close_not_open():
    prov = bh_world()
    prov.set(SESSIONS[0], _S.OPEN, 200.0, quotes={}, prev_close=100.0)  # 开盘跳涨
    result = run_bh(prov)
    assert result.orders[0].intent.qty == 1000  # 基于 prev_close(100) 定价（防前视）
    assert result.orders[0].fill.contracts == 500  # 开盘 200 → 现金钳制到 500 股


# ---- 保证金与期末钩子（§14-5） ----

def test_buy_hold_final_liquidation_not_margin_blocked():
    result = run_bh(bh_world())
    mid = result.states[3]
    assert mid.margin_used == pytest.approx(50_000.0)
    assert mid.cash == pytest.approx(0.0)
    assert mid.margin_used > mid.cash  # 100% 现金买入 → 保证金日检会置 blocked
    assert result.trades[0].exit_reason == "hold_end"  # on_final 钩子仍完成清仓
    assert result.states[-1].cash == pytest.approx(100_000.0)


def test_buy_hold_works_under_liquidate_policy():
    cfg = make_config(start=SESSIONS[0], end=SESSIONS[-1], margin_policy="liquidate")
    cfg.strategy = StrategyConfig(type="buy_hold", params=BuyHoldParams())
    result = SimulationEngine(cfg).run(bh_world(), CAL)
    assert result.trades[0].exit_reason == "hold_end"
    assert result.states[-1].cash == pytest.approx(100_000.0)


# ---- 状态快照（§14-3） ----

def test_buy_hold_state_positions_include_equity():
    result = run_bh(bh_world())
    mid = result.states[3]
    eq_snaps = [p for p in mid.positions if isinstance(p.spec, EquitySpec)]
    assert len(eq_snaps) == 1
    s = eq_snaps[0]
    assert s.qty == 1000
    assert s.avg_price == pytest.approx(100.0)
    assert s.mark_price == pytest.approx(100.0)
    assert s.unrealized_pnl == pytest.approx(0.0)
    assert mid.positions_value == pytest.approx(100_000.0)
    assert mid.equity == pytest.approx(100_000.0)


# ---- 配置与工厂（§14-6） ----

def test_strategy_config_mismatch_rejected():
    with pytest.raises(ValidationError):
        StrategyConfig(type="buy_hold", params=SellPutParams())
    with pytest.raises(ValidationError):
        StrategyConfig(type="sell_put", params=BuyHoldParams())


def test_build_strategy_factory():
    assert isinstance(build_strategy(bh_cfg()), BuyHoldStrategy)
    sell_cfg = make_config(start=SESSIONS[0], end=SESSIONS[-1])
    assert isinstance(build_strategy(sell_cfg), SellPutStrategy)


# ---- 股票会计（组合层） ----

def test_fill_model_equity_commission():
    fm = FillModel(commission_per_order=1.0, commission_per_share=0.005)
    assert fm.equity_commission(200) == pytest.approx(2.0)


def test_portfolio_buy_sell_equity_trade_and_conservation():
    p = Portfolio(10_000.0)
    p.buy_equity(symbol="SPY", shares=10, price=100.0, day=date(2025, 1, 6), commission=1.0)
    assert p.cash == pytest.approx(8_999.0)
    realized = p.sell_equity(
        symbol="SPY", shares=10, price=110.0, day=date(2025, 1, 7),
        commission=1.0, reason="hold_end",
    )
    assert realized == pytest.approx(99.0)
    t = p.trades[0]
    assert t.asset_kind == "equity"
    assert isinstance(t.spec, EquitySpec)
    assert t.exit_reason == "hold_end"
    assert t.pnl == pytest.approx(98.0)  # 100 − 双边手续费 2
    assert p.cash == pytest.approx(10_000.0 + sum(tr.pnl for tr in p.trades))


def test_assignment_equity_sale_records_no_trade():
    p = Portfolio(100_000.0)
    s = OptionSpec("SPY", EXPIRY, 95.0, OptionRight.PUT, OptionStyle.AMERICAN)
    p.open_option(
        spec=s, qty=-1, price=2.0, day=date(2025, 1, 6),
        commission=0.0, iv=0.2, dte=30, delta=-0.2,
    )
    p.assign_option(spec=s, day=EXPIRY, exit_iv=0.2)
    p.sell_equity(symbol="SPY", shares=100, price=91.0, day=date(2025, 2, 24), commission=1.0)
    assert len(p.trades) == 1
    assert p.trades[0].asset_kind == "option"
