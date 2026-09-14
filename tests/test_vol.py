"""M1-C 步骤 2：`vol` 模块测试（Spec v0.7 §6.2 / §15 F7 / AC-9）。

覆盖：RV 手算锚点与 Provider 同口径一致性、分位/IV Rank 手算锚点、四桶边界（AC-9）、
防前视 shift 语义、分桶统计表（笔数/胜率/平均 PnL）、真实 M1-A 产物的端到端一致性。
"""

from __future__ import annotations

import csv
import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from sellput.data import PriceSeries
from sellput.instruments import EquitySpec, OptionRight, OptionSpec, OptionStyle, Settlement
from sellput.market_data import HybridProvider
from sellput.portfolio import Trade
from sellput.vol import (
    RV_BUCKETS,
    bucket_by_rv,
    iv_percentile,
    iv_rank,
    realized_vol,
    rolling_percentile,
    rv_bucket,
    rv_bucket_stats,
)

_ROOT = Path(__file__).resolve().parents[1]
M1A_DIR = _ROOT / "experiments" / "m1a"


def make_trade(trade_id: int, entry_day: date, pnl: float) -> Trade:
    return Trade(
        id=trade_id,
        asset_kind="option",
        spec=OptionSpec(
            underlying="SPY",
            expiry=entry_day + timedelta(days=30),
            strike=100.0,
            right=OptionRight.PUT,
            style=OptionStyle.AMERICAN,
            settlement=Settlement.PHYSICAL,
        ),
        qty=-1,
        entry_date=entry_day,
        exit_date=entry_day + timedelta(days=10),
        entry_price=2.0,
        exit_price=0.0,
        pnl=pnl,
        commissions=0.0,
        exit_reason="expire",
    )


# ---------------------------------------------------------------------------
# 1. RV 手算锚点与 Provider 口径一致性
# ---------------------------------------------------------------------------


def test_realized_vol_hand_computed_alternating_closes():
    """收盘 100/110 交替：对数收益 ±ln(1.1)，4 个收益的样本标准差 = 2a/√3。"""
    closes = [100.0, 110.0, 100.0, 110.0, 100.0]
    rv = realized_vol(closes, window=4)
    a = math.log(1.1)
    assert math.isnan(rv[0]) and math.isnan(rv[3])
    assert rv[4] == pytest.approx(a * 2.0 / math.sqrt(3.0) * math.sqrt(252.0), rel=1e-12)


def test_realized_vol_warmup_and_validation():
    rv = realized_vol([100.0] * 25, window=20)
    assert np.all(np.isnan(rv[:20]))
    assert rv[20] == pytest.approx(0.0)  # 常数价格 → 零波动（有定义）
    with pytest.raises(ValueError):
        realized_vol([100.0, 0.0, 99.0], window=2)
    with pytest.raises(ValueError):
        realized_vol([100.0, 101.0], window=1)


def _provider_series(closes: list[float]) -> PriceSeries:
    arr = np.asarray(closes, dtype=float)
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(len(closes))]
    return PriceSeries(
        symbol="SPY",
        dates=dates,
        open=arr.copy(),
        close=arr.copy(),
        high=arr.copy(),
        low=arr.copy(),
        volume=np.zeros(len(closes)),
        dividends={},
        source="test",
    )


def test_realized_vol_matches_hybrid_provider_sigma():
    """与驱动合成链 iv_atm 的 HybridProvider.sigma_at 完全同口径（Raw→Derived 可重算）。"""
    steps = [0.01 * math.sin(i / 5.0) for i in range(400)]
    closes = list(100.0 * np.exp(np.cumsum(steps)))
    series = _provider_series(closes)
    provider = HybridProvider(series=series, start=series.dates[0], end=series.dates[-1])

    rv = realized_vol(closes, window=20)
    for i in range(60, len(closes)):  # 前 20 日为窗口预热（Provider 用 0.2 占位，本模块给 nan）
        assert rv[i] == pytest.approx(provider.sigma_at(series.dates[i]), rel=1e-12)


# ---------------------------------------------------------------------------
# 2. 分位 / IV Rank 手算锚点
# ---------------------------------------------------------------------------


def test_rolling_percentile_hand_computed():
    pct = rolling_percentile([1.0, 2.0, 3.0, 4.0, 5.0], window=5, min_obs=2)
    assert np.isnan(pct[0])  # 有效观测 1 个 < min_obs
    assert pct[1] == pytest.approx(50.0)  # [1,2]：1 个低于 2
    assert pct[2] == pytest.approx(200.0 / 3.0)  # [1,2,3]：2/3
    assert pct[3] == pytest.approx(75.0)
    assert pct[4] == pytest.approx(80.0)  # [1..5]：4/5


def test_rolling_percentile_ties_use_strict_less_than():
    pct = rolling_percentile([1.0, 2.0, 2.0, 2.0, 3.0], window=5, min_obs=2)
    assert pct[2] == pytest.approx(100.0 / 3.0)  # 窗口 [1,2,2]：仅 1 个严格小于 2
    assert pct[1] == pytest.approx(50.0)


def test_rolling_percentile_window_is_trailing():
    """窗口只回看最近的 window 个观测：更早的历史不参与（否则 idx3 会是 50%）。"""
    values = [1.0, 2.0, 100.0, 3.0]
    pct = rolling_percentile(values, window=3, min_obs=2)
    assert pct[1] == pytest.approx(50.0)  # 窗口 [1, 2]
    assert pct[3] == pytest.approx(100.0 / 3.0)  # 窗口 [2, 100, 3]：仅 1 个严格小于 3


def test_rolling_percentile_undefined_cases():
    flat = rolling_percentile([0.2] * 30, window=10, min_obs=5)
    assert np.all(np.isnan(flat))  # 全等 → 无区分度
    short = rolling_percentile([0.1, 0.2, 0.3], window=10, min_obs=5)
    assert np.all(np.isnan(short))  # 有效观测不足
    with_nan = rolling_percentile([0.1, float("nan"), 0.3, 0.4], window=10, min_obs=2)
    assert np.isnan(with_nan[1])  # 当前值非有限


def test_iv_rank_hand_computed():
    values = [0.10, 0.20, 0.30, 0.40]
    rank = iv_rank(values, window=4, min_obs=2)
    assert np.isnan(rank[0])
    assert rank[1] == pytest.approx(100.0)
    assert rank[3] == pytest.approx(100.0)
    descending = iv_rank([0.4, 0.3, 0.2, 0.1], window=4, min_obs=2)
    assert descending[3] == pytest.approx(0.0)


def test_iv_rank_constant_series_is_undefined():
    assert np.all(np.isnan(iv_rank([0.2] * 40, window=20, min_obs=5)))


def test_iv_percentile_is_rolling_percentile():
    values = [0.1, 0.2, 0.3, 0.4, 0.5]
    assert np.allclose(
        iv_percentile(values, window=5, min_obs=2),
        rolling_percentile(values, window=5, min_obs=2),
        equal_nan=True,
    )


# ---------------------------------------------------------------------------
# 3. 四桶边界（AC-9：桶边界与手算分位一致）
# ---------------------------------------------------------------------------


def test_bucket_boundaries_match_spec_labels():
    assert RV_BUCKETS == ("<25", "25–50", "50–75", "≥75")
    cases = [
        (0.0, "<25"),
        (24.9999, "<25"),
        (25.0, "25–50"),
        (49.9999, "25–50"),
        (50.0, "50–75"),
        (74.9999, "50–75"),
        (75.0, "≥75"),
        (100.0, "≥75"),
    ]
    for pct, expected in cases:
        assert rv_bucket(pct) == expected, pct
    assert rv_bucket(float("nan")) is None
    assert rv_bucket(None) is None


def test_bucket_by_rv_shift_is_lookahead_safe():
    """shift=1（默认）用 t−1 收盘已知的分位；shift=0 用当日收盘分位。"""
    days = [date(2020, 1, 1) + timedelta(days=i) for i in range(4)]
    pct = np.array([10.0, 90.0, 90.0, 10.0])

    safe = bucket_by_rv(days, pct)  # shift=1
    same_day = bucket_by_rv(days, pct, shift=0)

    assert days[0] not in safe  # 首日无前值
    assert safe[days[1]] == "<25"  # 用 pct[0] = 10
    assert safe[days[2]] == "≥75"  # 用 pct[1] = 90
    assert safe[days[3]] == "≥75"  # 用 pct[2] = 90
    assert same_day[days[1]] == "≥75"  # 用 pct[1] = 90
    assert same_day[days[3]] == "<25"  # 用 pct[3] = 10


def test_bucket_by_rv_validates_inputs():
    days = [date(2020, 1, 1), date(2020, 1, 2)]
    with pytest.raises(ValueError):
        bucket_by_rv(days, [10.0])  # 长度不一致
    with pytest.raises(ValueError):
        bucket_by_rv(days, [10.0, 20.0], shift=-1)


# ---------------------------------------------------------------------------
# 4. 分桶统计表（AC-9：笔数 / 胜率 / 平均 PnL）
# ---------------------------------------------------------------------------


def test_rv_bucket_stats_hand_computed():
    d0, d1, d2, d3, d4 = (date(2020, 1, 1) + timedelta(days=i) for i in range(5))
    buckets = {d0: "<25", d1: "<25", d2: "25–50", d3: "≥75"}
    trades = [
        make_trade(1, d0, 100.0),
        make_trade(2, d1, -50.0),
        make_trade(3, d2, -20.0),
        make_trade(4, d3, 300.0),
        make_trade(5, d4, 999.0),  # 入场日不在 buckets 中 → 跳过
    ]
    stats = rv_bucket_stats(trades, buckets)

    assert [s.bucket for s in stats] == list(RV_BUCKETS)  # 固定四行、顺序稳定
    by_bucket = {s.bucket: s for s in stats}
    assert by_bucket["<25"].n_trades == 2
    assert by_bucket["<25"].win_rate == pytest.approx(0.5)
    assert by_bucket["<25"].avg_pnl == pytest.approx(25.0)
    assert by_bucket["25–50"].n_trades == 1
    assert by_bucket["25–50"].win_rate == pytest.approx(0.0)
    assert by_bucket["25–50"].avg_pnl == pytest.approx(-20.0)
    assert by_bucket["50–75"].n_trades == 0
    assert by_bucket["50–75"].win_rate is None
    assert by_bucket["50–75"].avg_pnl is None
    assert by_bucket["≥75"].n_trades == 1
    assert by_bucket["≥75"].avg_pnl == pytest.approx(300.0)
    assert sum(s.n_trades for s in stats) == 4  # 1 笔因入场日缺失被跳过
    assert by_bucket["<25"].to_dict()["bucket"] == "<25"


def test_rv_bucket_stats_with_no_trades_returns_empty_rows():
    stats = rv_bucket_stats([], {})
    assert len(stats) == 4
    assert all(s.n_trades == 0 and s.win_rate is None and s.avg_pnl is None for s in stats)


def test_rv_bucket_stats_accepts_equity_trades_without_error():
    """归桶只依赖 entry_date；权益类交易由调用方决定是否纳入（本函数不做过滤）。"""
    day = date(2020, 1, 1)
    equity_trade = Trade(
        id=1,
        asset_kind="equity",
        spec=EquitySpec(symbol="SPY"),
        qty=100,
        entry_date=day,
        exit_date=day + timedelta(days=30),
        entry_price=100.0,
        exit_price=110.0,
        pnl=1000.0,
        commissions=0.0,
        exit_reason="hold_end",
    )
    stats = rv_bucket_stats([equity_trade], {day: "≥75"})
    assert {s.bucket: s.n_trades for s in stats}["≥75"] == 1


# ---------------------------------------------------------------------------
# 5. M1-A 真实产物端到端一致性（产物被 gitignore，缺失时跳过）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (M1A_DIR / "prices.csv").exists(), reason="experiments/m1a 产物缺失（gitignore）"
)
def test_m1a_prices_sigma_matches_realized_vol_and_buckets_are_consistent():
    """prices.csv 的 sigma 列 == 本模块 RV；据此归桶后统计自洽（AC-9 端到端）。"""
    days: list[date] = []
    closes: list[float] = []
    sigmas: list[float] = []
    with (M1A_DIR / "prices.csv").open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            days.append(date.fromisoformat(row["Date"]))
            closes.append(float(row["Close"]))
            sigmas.append(float(row["sigma"]))

    rv = realized_vol(closes, window=20)
    for i in range(20, len(closes)):
        assert rv[i] == pytest.approx(sigmas[i], rel=1e-9)

    pct = rolling_percentile(rv, window=252, min_obs=20)
    buckets = bucket_by_rv(days, pct)  # shift=1：入场决策时点已知
    assert set(buckets.values()) <= set(RV_BUCKETS)

    trades = _load_option_trades(M1A_DIR / "trades.csv")
    stats = rv_bucket_stats(trades, buckets)
    assert len(stats) == 4
    assert sum(s.n_trades for s in stats) <= len(trades)
    assert sum(s.n_trades for s in stats) >= len(trades) - 30  # 仅窗口预热期少数交易未归桶
    for s in stats:
        if s.n_trades:
            assert 0.0 <= s.win_rate <= 1.0
            assert s.avg_pnl is not None
    # 真实数据下四桶都应有样本（20 年、约 529 笔交易）
    assert all(s.n_trades > 0 for s in stats)


def _load_option_trades(path: Path) -> list[Trade]:
    out: list[Trade] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out.append(
                Trade(
                    id=int(row["id"]),
                    asset_kind=row.get("asset_kind") or "option",
                    spec=OptionSpec(
                        underlying="SPY",
                        expiry=date.fromisoformat(row["expiry"]),
                        strike=float(row["strike"]),
                        right=OptionRight.PUT,
                        style=OptionStyle.AMERICAN,
                        settlement=Settlement.PHYSICAL,
                    ),
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
