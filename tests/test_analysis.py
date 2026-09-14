"""M1-C 步骤 1：`analysis` 指标模块测试（Spec v0.7 §15 AC-3 / AC-4）。

覆盖：
1. AC-4 手算锚点 —— 252 交易日构造序列 + 5 笔交易，逐项对照手算值；
2. 独立实现交叉验证 —— 用 pandas 独立重算 Sharpe/Sortino/波动率/最大回撤/Profit Factor；
3. AC-3 金标准 —— 从 experiments/m1a、experiments/m1b 的既有产物重算指标，对照冻结数字
   （产物目录被 gitignore，缺失时跳过而非失败）；
4. 未定义即 None、纯函数确定性、Spec §15.5 数据模型字段一致性。
"""

from __future__ import annotations

import csv
import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sellput.analysis import (
    TRADING_DAYS_PER_YEAR,
    Metrics,
    compute_metrics,
    daily_returns,
    drawdown_series,
    equity_series,
    trade_pnls,
)
from sellput.instruments import (
    EquitySpec,
    OptionRight,
    OptionSpec,
    OptionStyle,
    Settlement,
)
from sellput.portfolio import PortfolioState, Trade

_ROOT = Path(__file__).resolve().parents[1]
M1A_DIR = _ROOT / "experiments" / "m1a"
M1B_DIR = _ROOT / "experiments" / "m1b"

# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------


def make_states(
    equity: list[float],
    *,
    start: date = date(2020, 1, 1),
    margin_used: float = 0.0,
    benchmark: list[float] | None = None,
) -> list[PortfolioState]:
    """由净值序列构造逐日 PortfolioState（日期逐日递增）。"""
    out: list[PortfolioState] = []
    for i, value in enumerate(equity):
        out.append(
            PortfolioState(
                date=start + timedelta(days=i),
                cash=value,
                positions=(),
                positions_value=0.0,
                margin_used=margin_used,
                equity=value,
                greeks={},
                attribution={},
                benchmark_price=benchmark[i] if benchmark else 100.0,
            )
        )
    return out


def make_option_trade(
    trade_id: int,
    pnl: float,
    *,
    entry_price: float = 2.0,
    qty: int = -1,
    entry_day: date = date(2020, 1, 2),
) -> Trade:
    return Trade(
        id=trade_id,
        asset_kind="option",
        spec=OptionSpec(
            underlying="SPY",
            expiry=date(2020, 2, 21),
            strike=100.0,
            right=OptionRight.PUT,
            style=OptionStyle.AMERICAN,
            settlement=Settlement.PHYSICAL,
        ),
        qty=qty,
        entry_date=entry_day,
        exit_date=entry_day + timedelta(days=5),
        entry_price=entry_price,
        exit_price=0.0,
        pnl=pnl,
        commissions=0.0,
        exit_reason="expire",
    )


def make_equity_trade(trade_id: int, pnl: float) -> Trade:
    return Trade(
        id=trade_id,
        asset_kind="equity",
        spec=EquitySpec(symbol="SPY"),
        qty=100,
        entry_date=date(2020, 1, 2),
        exit_date=date(2020, 12, 31),
        entry_price=100.0,
        exit_price=110.0,
        pnl=pnl,
        commissions=0.0,
        exit_reason="hold_end",
    )


# ---------------------------------------------------------------------------
# 1. AC-4 手算锚点
# ---------------------------------------------------------------------------


def _alternating_path() -> list[float]:
    """252 个交易日：首日 +1%，其后交替 −0.5% / +1%（各 126 个收益观测）。"""
    equity = [100_000.0 * 1.01]
    for i in range(1, 252):
        equity.append(equity[-1] * (0.995 if i % 2 else 1.01))
    return equity


def test_hand_computed_metric_anchors():
    """AC-4：全部 §15.5 指标对照手算值。"""
    equity = _alternating_path()
    states = make_states(
        equity, margin_used=1_000.0, benchmark=[100.0, *([100.0] * 250), 150.0]
    )
    trades = [
        make_option_trade(i + 1, p)
        for i, p in enumerate([200.0, 150.0, -100.0, 300.0, -250.0])
    ]

    m = compute_metrics(states, trades, starting_cash=100_000.0)

    # 收益：日收益 = 126×(+1%) + 126×(−0.5%) ⇒ 均值 0.0025；样本标准差 0.0075×√(252/251)
    sd = 0.0075 * math.sqrt(252.0 / 251.0)
    assert m.total_return == pytest.approx(equity[-1] / 100_000.0 - 1.0, rel=1e-12)
    assert m.cagr == pytest.approx(equity[-1] / 100_000.0 - 1.0, rel=1e-12)  # N=252 ⇒ 指数为 1
    assert m.ann_vol == pytest.approx(sd * math.sqrt(TRADING_DAYS_PER_YEAR), rel=1e-12)
    assert m.sharpe == pytest.approx(0.0025 / sd * math.sqrt(TRADING_DAYS_PER_YEAR), rel=1e-12)
    # Sortino：下行均方偏差 = √(0.005²/2)
    downside = 0.005 / math.sqrt(2.0)
    assert m.sortino == pytest.approx(
        0.0025 / downside * math.sqrt(TRADING_DAYS_PER_YEAR), rel=1e-12
    )
    # 回撤：每次 +1% 后 −0.5% ⇒ 谷值/峰值 = 0.995 ⇒ −0.005；水下期 2 天后收复
    assert m.max_dd == pytest.approx(-0.005, rel=1e-12)
    assert m.dd_duration_days == 2
    assert m.calmar == pytest.approx(m.cagr / 0.005, rel=1e-12)

    # 交易：5 笔，3 胜 2 负
    assert m.n_trades == 5
    assert m.win_rate == pytest.approx(0.6, rel=1e-12)
    assert m.profit_factor == pytest.approx(650.0 / 350.0, rel=1e-12)
    assert m.avg_win == pytest.approx(650.0 / 3.0, rel=1e-12)
    assert m.avg_loss == pytest.approx(-175.0, rel=1e-12)
    assert m.trades_per_year == pytest.approx(5.0, rel=1e-12)
    assert m.p5_trade_pnl == pytest.approx(-220.0, rel=1e-12)  # linear 插值：−250 + 0.2×150
    assert m.worst_trade_pnl == pytest.approx(-250.0, rel=1e-12)

    # 基准：价格型 = 150/100 − 1
    assert m.benchmark_price_return == pytest.approx(0.5, rel=1e-12)
    assert m.excess_vs_price == pytest.approx(m.total_return - 0.5, rel=1e-12)
    # 总回报型基准按 §15.7 第 8 步交付
    assert m.benchmark_total_return is None
    assert m.excess_vs_total is None
    # 效率：Σ权利金 = 5×2.0×1×100 = 1000；平均保证金 1000 ⇒ 1.0
    assert m.capital_efficiency == pytest.approx(1.0, rel=1e-12)
    # 溯源
    assert m.n_sessions == 252
    assert m.start == date(2020, 1, 1)
    assert m.end == date(2020, 1, 1) + timedelta(days=251)


def test_risk_free_rate_is_configurable():
    """rf 影响 Sharpe/Sortino，但不影响收益与回撤类指标（Spec §15 F6：rf 可配）。"""
    equity = _alternating_path()
    states = make_states(equity, margin_used=1_000.0)
    base = compute_metrics(states, [], starting_cash=100_000.0)
    with_rf = compute_metrics(states, [], starting_cash=100_000.0, risk_free_rate=0.0252)

    sd = 0.0075 * math.sqrt(252.0 / 251.0)
    rf_daily = 0.0252 / TRADING_DAYS_PER_YEAR
    assert with_rf.sharpe == pytest.approx(
        (0.0025 - rf_daily) / sd * math.sqrt(TRADING_DAYS_PER_YEAR), rel=1e-12
    )
    assert with_rf.total_return == pytest.approx(base.total_return, rel=1e-15)
    assert with_rf.max_dd == pytest.approx(base.max_dd, rel=1e-15)


# ---------------------------------------------------------------------------
# 2. 独立实现交叉验证（pandas）
# ---------------------------------------------------------------------------


def test_ratios_match_independent_pandas_implementation():
    """用 pandas 独立重算波动率/Sharpe/最大回撤/Profit Factor，与本模块一致。"""
    steps = [math.sin(i / 7.0) * 0.004 + 0.0003 for i in range(400)]
    equity = list(100_000.0 * np.cumprod([1.0 + s for s in steps]))
    benchmark = list(np.linspace(100.0, 160.0, 400))
    states = make_states(equity, margin_used=5_000.0, benchmark=benchmark)
    trades = [make_option_trade(i + 1, p) for i, p in enumerate([120.0, -80.0, 45.0, -15.0, 200.0])]

    m = compute_metrics(states, trades, starting_cash=100_000.0)

    eq = pd.Series(equity)
    rets = pd.Series(daily_returns(states, starting_cash=100_000.0))
    assert m.ann_vol == pytest.approx(float(rets.std(ddof=1) * math.sqrt(252.0)), rel=1e-12)
    assert m.sharpe == pytest.approx(
        float(rets.mean() / rets.std(ddof=1) * math.sqrt(252.0)), rel=1e-12
    )
    assert m.max_dd == pytest.approx(float((eq / eq.cummax() - 1.0).min()), rel=1e-12)
    pnl = pd.Series(trade_pnls(trades))
    assert m.profit_factor == pytest.approx(
        float(pnl[pnl > 0].sum() / abs(pnl[pnl < 0].sum())), rel=1e-12
    )
    assert m.benchmark_price_return == pytest.approx(60.0 / 100.0, rel=1e-12)


def test_drawdown_series_matches_manual_path():
    equity = [100.0, 90.0, 95.0, 105.0, 100.0, 80.0, 110.0]
    dd = drawdown_series(equity)
    expected = [0.0, -0.10, -0.05, 0.0, 100.0 / 105.0 - 1.0, 80.0 / 105.0 - 1.0, 0.0]
    assert dd == pytest.approx(expected, rel=1e-12)
    states = make_states(equity)
    m = compute_metrics(states, [], starting_cash=100.0)
    assert m.max_dd == pytest.approx(80.0 / 105.0 - 1.0, rel=1e-12)
    # 水下期：峰值 idx0 → 收复 idx3（3 天）；峰值 idx3 → 收复 idx6（3 天）
    assert m.dd_duration_days == 3


def test_equity_curve_and_pnl_accessors():
    states = make_states([100.0, 110.0])
    assert equity_series(states) == [100.0, 110.0]
    assert daily_returns(states, starting_cash=100.0) == pytest.approx([0.0, 0.1], rel=1e-12)
    assert trade_pnls([make_option_trade(1, -12.5)]) == [-12.5]


# ---------------------------------------------------------------------------
# 3. 未定义即 None / 输入校验 / 确定性
# ---------------------------------------------------------------------------


def test_undefined_ratios_return_none_not_zero():
    """无波动、无交易、无回撤时，比率返回 None（不用 0 冒充）。"""
    states = make_states([100_000.0] * 10, benchmark=[100.0] * 10)
    m = compute_metrics(states, [], starting_cash=100_000.0)
    assert m.total_return == 0.0
    assert m.ann_vol == 0.0  # 波动率本身有定义（= 0）
    assert m.sharpe is None  # 分母为 0
    assert m.sortino is None
    assert m.max_dd == 0.0
    assert m.dd_duration_days == 0
    assert m.calmar is None
    assert m.n_trades == 0
    assert m.win_rate is None
    assert m.profit_factor is None
    assert m.avg_win is None
    assert m.avg_loss is None
    assert m.p5_trade_pnl is None
    assert m.worst_trade_pnl is None
    assert m.trades_per_year == 0.0
    assert m.capital_efficiency is None
    assert m.benchmark_price_return == pytest.approx(0.0, rel=1e-12)


def test_single_session_has_no_volatility_sample():
    m = compute_metrics(make_states([101_000.0]), [], starting_cash=100_000.0)
    assert m.n_sessions == 1
    assert m.ann_vol is None
    assert m.sharpe is None
    # CAGR 按文档公式年化（n=1 时指数为 252，值本身不代表真实年化意义）
    assert m.cagr == pytest.approx(1.01**252 - 1.0, rel=1e-12)


def test_no_loss_trades_profit_factor_is_none():
    """全部盈利时 Profit Factor 未定义（不返回 inf）。"""
    m = compute_metrics(
        make_states([100_000.0] * 5), [make_option_trade(1, 10.0)], starting_cash=100_000.0
    )
    assert m.win_rate == pytest.approx(1.0)
    assert m.profit_factor is None
    assert m.avg_win == pytest.approx(10.0)
    assert m.avg_loss is None


def test_equity_only_trades_have_no_capital_efficiency():
    """买入持有（无期权成交）时资本效率未定义。"""
    m = compute_metrics(
        make_states([100_000.0] * 5, margin_used=50_000.0),
        [make_equity_trade(1, 1_000.0)],
        starting_cash=100_000.0,
    )
    assert m.n_trades == 1
    assert m.capital_efficiency is None


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        compute_metrics([], [], starting_cash=100_000.0)
    with pytest.raises(ValueError):
        compute_metrics(make_states([100.0]), [], starting_cash=0.0)


def test_compute_metrics_is_deterministic_and_side_effect_free():
    equity = _alternating_path()
    states = make_states(equity, margin_used=1_000.0, benchmark=[100.0] * 252)
    trades = [make_option_trade(1, 200.0), make_option_trade(2, -250.0)]
    before = [s.equity for s in states]
    a = compute_metrics(states, trades, starting_cash=100_000.0)
    b = compute_metrics(states, trades, starting_cash=100_000.0)
    assert a == b
    assert [s.equity for s in states] == before  # 输入不被修改


# ---------------------------------------------------------------------------
# 4. Spec §15.5 数据模型一致性
# ---------------------------------------------------------------------------

SPEC_V07_METRIC_FIELDS = {
    # Spec v0.7 §15.5 Metrics（顺序与命名以 Spec 为准）
    "total_return",
    "cagr",
    "ann_vol",
    "sharpe",
    "sortino",
    "calmar",
    "max_dd",
    "dd_duration_days",
    "win_rate",
    "profit_factor",
    "avg_win",
    "avg_loss",
    "n_trades",
    "trades_per_year",
    "p5_trade_pnl",
    "worst_trade_pnl",
    "benchmark_price_return",
    "benchmark_total_return",
    "excess_vs_price",
    "excess_vs_total",
    "capital_efficiency",
}
PROVENANCE_FIELDS = {"start", "end", "n_sessions"}


def test_metrics_keys_match_spec_data_model():
    m = compute_metrics(make_states([100_000.0, 101_000.0]), [], starting_cash=100_000.0)
    assert isinstance(m, Metrics)
    assert set(m.to_dict()) == SPEC_V07_METRIC_FIELDS | PROVENANCE_FIELDS


def test_to_dict_serializes_dates_as_iso_strings():
    m = compute_metrics(make_states([100_000.0, 101_000.0]), [], starting_cash=100_000.0)
    d = m.to_dict()
    assert d["start"] == "2020-01-01"
    assert d["end"] == "2020-01-02"
    assert d["n_sessions"] == 2


# ---------------------------------------------------------------------------
# 5. AC-3 金标准：从既有 M1-A / M1-B 产物重算
# ---------------------------------------------------------------------------


def _load_states(path: Path) -> list[PortfolioState]:
    out: list[PortfolioState] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out.append(
                PortfolioState(
                    date=date.fromisoformat(row["date"]),
                    cash=float(row["cash"]),
                    positions=(),
                    positions_value=float(row["positions_value"]),
                    margin_used=float(row["margin_used"]),
                    equity=float(row["equity"]),
                    greeks={},
                    attribution={},
                    benchmark_price=float(row["benchmark_close"]),
                )
            )
    return out


def _load_trades(path: Path) -> list[Trade]:
    out: list[Trade] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            kind = row.get("asset_kind") or "option"
            if kind == "equity":
                spec: OptionSpec | EquitySpec = EquitySpec(symbol=row.get("symbol") or "SPY")
            else:
                spec = OptionSpec(
                    underlying="SPY",
                    expiry=date.fromisoformat(row["expiry"]),
                    strike=float(row["strike"]),
                    right=OptionRight.PUT,
                    style=OptionStyle.AMERICAN,
                    settlement=Settlement.PHYSICAL,
                )
            out.append(
                Trade(
                    id=int(row["id"]),
                    asset_kind=kind,
                    spec=spec,
                    qty=int(float(row["qty"])),
                    entry_date=date.fromisoformat(row["entry_date"]),
                    exit_date=date.fromisoformat(row["exit_date"]),
                    entry_price=float(row["entry_price"]),
                    exit_price=float(row["exit_price"]),
                    pnl=float(row["pnl"]),
                    commissions=float(row["commissions"]),
                    exit_reason=row["exit_reason"],
                )
            )
    return out


@pytest.mark.skipif(
    not (M1A_DIR / "states.csv").exists(), reason="experiments/m1a 产物缺失（gitignore）"
)
def test_golden_m1a_baseline_matches_frozen_numbers():
    """AC-3：默认组合（30/0.20/0.50，2005–2024）重算 == 冻结金标准。"""
    states = _load_states(M1A_DIR / "states.csv")
    trades = _load_trades(M1A_DIR / "trades.csv")
    m = compute_metrics(states, trades, starting_cash=100_000.0)

    assert m.n_sessions == 5033
    assert float(states[-1].equity) == pytest.approx(173_061.18, abs=0.01)
    assert m.total_return == pytest.approx(0.730612, abs=1e-6)
    assert m.max_dd == pytest.approx(-0.850484, abs=1e-6)
    assert m.n_trades == 529
    assert round(m.win_rate, 3) == 0.932
    assert m.benchmark_price_return == pytest.approx(3.871820, abs=1e-6)
    assert m.excess_vs_price == pytest.approx(0.730612 - 3.871820, abs=1e-5)
    assert m.capital_efficiency is not None and m.capital_efficiency > 0
    assert m.cagr is not None and 0.0 < m.cagr < m.total_return
    assert m.worst_trade_pnl == pytest.approx(-174_275.7380514072, rel=1e-9)


@pytest.mark.skipif(
    not (M1B_DIR / "states.csv").exists(), reason="experiments/m1b 产物缺失（gitignore）"
)
def test_golden_m1b_baseline_matches_frozen_numbers():
    """M1-B 买入持有回归：指标层复算与 summary.txt 一致（无期权成交 ⇒ 资本效率 None）。"""
    states = _load_states(M1B_DIR / "states.csv")
    trades = _load_trades(M1B_DIR / "trades.csv")
    m = compute_metrics(states, trades, starting_cash=100_000.0)

    assert m.n_sessions == 5033
    assert float(states[-1].equity) == pytest.approx(484_689.27, abs=0.01)
    assert m.total_return == pytest.approx(3.846893, abs=1e-6)
    assert m.max_dd == pytest.approx(-0.564619, abs=1e-6)
    assert m.n_trades == 1
    assert m.capital_efficiency is None
    assert m.benchmark_price_return == pytest.approx(3.871820, abs=1e-6)
