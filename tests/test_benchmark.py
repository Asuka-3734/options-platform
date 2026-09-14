"""M1-C 步骤 8：双基准（Price / Total Return）与分桶测试（Spec v0.7 §6.4 / AC-15 / AC-10）。

覆盖：总回报指数的手算复现（分红按次一交易日开盘价再投）、compute_metrics 的双基准字段、
分段（train/test）基准切片口径、无可投开盘价与无分红数据的降级、实际入场 DTE 分桶。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from sellput.analysis import (
    DEFAULT_DTE_BUCKETS,
    BucketStats,
    compute_metrics,
    entry_dte_bucket_stats,
    total_return_index,
)
from sellput.config import BacktestConfig, DataConfig
from sellput.instruments import EquitySpec, OptionRight, OptionSpec, OptionStyle, Settlement
from sellput.portfolio import PortfolioState, Trade
from sellput.research import run_once, segment_metrics

DAY0 = date(2020, 1, 6)


def make_states(
    closes: list[float], *, start: date = DAY0, benchmark: list[float] | None = None
) -> list[PortfolioState]:
    out: list[PortfolioState] = []
    for i, value in enumerate(closes):
        out.append(
            PortfolioState(
                date=start + timedelta(days=i),
                cash=value,
                positions=(),
                positions_value=0.0,
                margin_used=0.0,
                equity=value,
                greeks={},
                attribution={},
                benchmark_price=(benchmark[i] if benchmark else value),
            )
        )
    return out


def make_option_trade(trade_id: int, day: date, pnl: float, *, dte: int | None) -> Trade:
    return Trade(
        id=trade_id,
        asset_kind="option",
        spec=OptionSpec(
            underlying="SPY",
            expiry=day + timedelta(days=30),
            strike=100.0,
            right=OptionRight.PUT,
            style=OptionStyle.AMERICAN,
            settlement=Settlement.PHYSICAL,
        ),
        qty=-1,
        entry_date=day,
        exit_date=day + timedelta(days=20),
        entry_price=2.0,
        exit_price=0.0,
        pnl=pnl,
        commissions=0.0,
        exit_reason="expire",
        dte_at_entry=dte,
    )


# ---------------------------------------------------------------------------
# 1. 总回报指数（AC-15：与手算再投复现一致）
# ---------------------------------------------------------------------------


def test_total_return_index_hand_computed():
    """3 天、开盘价恒为 10、收盘 10/10.5/11、第 1 天除息 0.5。

    shares: 0.1 → 0.1×(1+0.5/10)=0.105
    index:  1.0 → 0.105×10.5=1.1025 → 0.105×11=1.155
    价格型收益 = 11/10 − 1 = +10%；总回报 = 1.155/1.0 − 1 = +15.5%
    """
    dates = [DAY0, DAY0 + timedelta(days=1), DAY0 + timedelta(days=2)]
    index = total_return_index(
        dates=dates, opens=[10.0, 10.0, 10.0], closes=[10.0, 10.5, 11.0], dividends={DAY0: 0.5}
    )
    assert index == pytest.approx([1.0, 1.1025, 1.155], rel=1e-12)
    assert index[-1] / index[0] - 1 == pytest.approx(0.155, rel=1e-12)
    price_return = 11.0 / 10.0 - 1
    assert index[-1] / index[0] - 1 > price_return  # 分红再投必然不低于价格型


def test_total_return_index_without_dividends_equals_price_index():
    dates = [DAY0 + timedelta(days=i) for i in range(3)]
    index = total_return_index(dates=dates, opens=[10.0] * 3, closes=[10.0, 10.5, 11.0])
    assert index == pytest.approx([1.0, 1.05, 1.1], rel=1e-12)
    assert index[-1] / index[0] - 1 == pytest.approx(11.0 / 10.0 - 1, rel=1e-12)


def test_total_return_index_reinvests_at_next_open_not_close():
    """再投价必须是次一交易日**开盘价**：开盘价越低，再投份额越多。"""
    dates = [DAY0 + timedelta(days=i) for i in range(2)]
    cheap_open = total_return_index(
        dates=dates, opens=[100.0, 5.0], closes=[100.0, 100.0], dividends={DAY0: 5.0}
    )
    rich_open = total_return_index(
        dates=dates, opens=[100.0, 100.0], closes=[100.0, 100.0], dividends={DAY0: 5.0}
    )
    assert cheap_open[-1] > rich_open[-1]  # 5.0 开盘再投 → 份额 ×2；100 开盘再投 → ×1.05


def test_total_return_index_validates_inputs():
    with pytest.raises(ValueError, match="长度必须一致"):
        total_return_index(dates=[DAY0], opens=[1.0, 2.0], closes=[1.0])
    with pytest.raises(ValueError, match="positive"):
        total_return_index(dates=[DAY0], opens=[0.0], closes=[0.0])
    assert total_return_index(dates=[], opens=[], closes=[]) == []


# ---------------------------------------------------------------------------
# 2. 双基准指标（AC-15）
# ---------------------------------------------------------------------------


def test_compute_metrics_reports_both_benchmarks():
    closes = [100_000.0, 101_000.0, 103_000.0]
    states = make_states(closes, benchmark=[100.0, 101.0, 103.0])
    index = [1.0, 1.02, 1.05]

    with_total = compute_metrics(states, [], starting_cash=100_000.0, benchmark_total_index=index)
    without = compute_metrics(states, [], starting_cash=100_000.0)

    assert with_total.benchmark_price_return == pytest.approx(0.03, rel=1e-12)
    assert with_total.benchmark_total_return == pytest.approx(0.05, rel=1e-12)
    assert with_total.excess_vs_price == pytest.approx(0.03 - 0.03, rel=1e-12)
    assert with_total.excess_vs_total == pytest.approx(0.03 - 0.05, rel=1e-12)
    # 未提供总回报指数时字段保持 None（不猜测、不冒充）
    assert without.benchmark_total_return is None and without.excess_vs_total is None


def test_compute_metrics_rejects_misaligned_benchmark_index():
    states = make_states([100_000.0, 101_000.0])
    with pytest.raises(ValueError, match="等长"):
        compute_metrics(states, [], starting_cash=100_000.0, benchmark_total_index=[1.0])


def test_segment_metrics_slices_total_return_benchmark_like_equity():
    """test 段基准以 train 末指数值为起点（与净值口径一致）。"""
    closes = [100_000.0, 110_000.0, 121_000.0, 133_100.0]
    states = make_states(closes)
    index = [1.0, 1.1, 1.21, 1.331]
    seg = segment_metrics(
        states,
        [],
        starting_cash=100_000.0,
        train_end=states[1].date,
        benchmark_total_index=index,
    )
    assert seg.train.benchmark_total_return == pytest.approx(0.10, rel=1e-12)
    assert seg.test.benchmark_total_return == pytest.approx(1.331 / 1.1 - 1, rel=1e-12)
    assert seg.full.benchmark_total_return == pytest.approx(0.331, rel=1e-12)
    # 复合一致性（与净值口径一致）：(1+train)(1+test) − 1 == full
    composite = (1 + seg.train.benchmark_total_return) * (1 + seg.test.benchmark_total_return) - 1
    assert composite == pytest.approx(seg.full.benchmark_total_return, rel=1e-12)


def test_synthetic_provider_has_no_total_return_benchmark():
    """合成数据无分红记录 → 总回报基准不可得（None），报告需显式声明降级。"""
    cfg = BacktestConfig(
        data=DataConfig(symbol="SPX", start=date(2020, 1, 1), end=date(2020, 3, 31)),
    )
    run = run_once(cfg, include_total_return_benchmark=True)
    assert run.segments.full.benchmark_total_return is None
    assert run.segments.full.benchmark_price_return is not None  # 价格型仍然有


# ---------------------------------------------------------------------------
# 3. 实际入场 DTE 分桶（AC-10：按实际入场 DTE 解释，而非假定 target = actual）
# ---------------------------------------------------------------------------


def test_entry_dte_bucket_stats_uses_actual_dte():
    trades = [
        make_option_trade(1, DAY0, 100.0, dte=30),
        make_option_trade(2, DAY0, -50.0, dte=30),
        make_option_trade(3, DAY0, 200.0, dte=21),
        make_option_trade(4, DAY0, -10.0, dte=19),
        make_option_trade(5, DAY0, 300.0, dte=45),
        make_option_trade(6, DAY0, 50.0, dte=None),  # 无 dte → 跳过
        Trade(  # 股票交易 → 跳过
            id=7,
            asset_kind="equity",
            spec=EquitySpec(symbol="SPY"),
            qty=100,
            entry_date=DAY0,
            exit_date=DAY0 + timedelta(days=5),
            entry_price=100.0,
            exit_price=110.0,
            pnl=999.0,
            commissions=0.0,
            exit_reason="hold_end",
        ),
    ]
    stats = entry_dte_bucket_stats(trades)
    assert [s.bucket for s in stats] == [b[0] for b in DEFAULT_DTE_BUCKETS]
    by_bucket = {s.bucket: s for s in stats}
    assert by_bucket["15–21"].n_trades == 2
    assert by_bucket["15–21"].win_rate == pytest.approx(0.5)
    assert by_bucket["15–21"].avg_pnl == pytest.approx(95.0)
    assert by_bucket["29–35"].n_trades == 2
    assert by_bucket["29–35"].avg_pnl == pytest.approx(25.0)
    assert by_bucket["36–45"].n_trades == 1
    assert by_bucket["≤14"].n_trades == 0
    assert by_bucket["≤14"].win_rate is None
    assert sum(s.n_trades for s in stats) == 5  # 2 笔被跳过


def test_entry_dte_bucket_stats_empty_input_returns_zero_rows():
    stats = entry_dte_bucket_stats([])
    assert len(stats) == len(DEFAULT_DTE_BUCKETS)
    assert all(isinstance(s, BucketStats) and s.n_trades == 0 for s in stats)
