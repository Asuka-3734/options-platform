"""HybridProvider 测试（M1-A）：真实价格快照 / 滚动波动率 / 防前视 / 股息率 / e2e。"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from sellput.config import BacktestConfig, DataConfig, HybridConfig
from sellput.data import PriceSeries
from sellput.instruments import NyseCalendar, OptionRight, monthly_expiries
from sellput.market_data import HybridProvider, build_provider
from sellput.sim import SimulationEngine

CAL = NyseCalendar()
PUT = OptionRight.PUT
N_SESSIONS = 160
SWITCH = 120  # 波动率切换点（sigma_lo → sigma_hi）
SIGMA_LO, SIGMA_HI = 0.10, 0.60
S0 = 100.0


def make_series(
    start: date = date(2020, 1, 2),
    n: int = N_SESSIONS,
    seed: int = 7,
    dividends: dict[date, float] | None = None,
) -> PriceSeries:
    dates = CAL.sessions(start, start + timedelta(days=3 * n))[:n]
    rng = np.random.default_rng(seed)
    closes = np.empty(n)
    s = S0
    for i in range(n):
        sig = SIGMA_HI if i >= SWITCH else SIGMA_LO
        s = s * math.exp(-0.5 * sig**2 / 252.0 + sig * math.sqrt(1.0 / 252.0) * rng.normal())
        closes[i] = s
    opens = np.empty(n)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]  # 开盘=前收（隔离波动率影响，便于防前视断言）
    return PriceSeries(
        symbol="SPY",
        dates=dates,
        open=opens,
        close=closes,
        high=closes,
        low=closes,
        volume=np.full(n, 1e6),
        dividends=dividends or {},
        source="fixture",
    )


@pytest.fixture
def provider() -> HybridProvider:
    series = make_series()
    return HybridProvider(
        series=series, start=series.dates[20], end=series.dates[140], vol_window=20
    )


def test_sessions_and_snapshot_prices(provider):
    series = make_series()
    expect = series.dates[20:141]
    assert provider.sessions() == expect
    d = expect[50]
    prev = expect[49]
    cs = provider.close_snapshot(d)
    os_ = provider.open_snapshot(d)
    assert cs.underlier.price == pytest.approx(float(series.close[50 + 20]))
    assert os_.underlier.price == pytest.approx(float(series.open[50 + 20]))
    assert cs.underlier.prev_close == pytest.approx(float(series.close[49 + 20]))
    assert os_.underlier.prev_close == pytest.approx(float(series.close[49 + 20]))
    assert provider.close_snapshot(prev).underlier.price == pytest.approx(
        float(series.close[49 + 20])
    )


def test_rolling_sigma_matches_hand_calc(provider):
    series = make_series()
    closes = series.close
    rets = np.diff(np.log(closes))
    for i in (40, 80, 139):
        d = series.dates[i]
        expected = float(np.std(rets[max(0, i - 20) : i], ddof=1) * math.sqrt(252.0))
        assert provider.sigma_at(d) == pytest.approx(expected, rel=1e-12)


def test_open_snapshot_no_lookahead(provider):
    """防前视对照实验：抹掉切换日的收盘信息后，开盘快照必须逐位不变。

    构造 series2：close[D] := close[D−1]（即"未来被删除"的世界）。
    开盘快照只用开盘价与前收 → 两个世界的开盘快照应完全一致；
    而收盘快照的波动率包含今日跳变 → 两者不同。
    """
    import dataclasses

    series = make_series()
    d_switch = series.dates[SWITCH]
    prev = series.dates[SWITCH - 1]

    series2 = dataclasses.replace(series, close=series.close.copy())
    series2.close[SWITCH] = series2.close[SWITCH - 1]
    prov2 = HybridProvider(
        series=series2, start=series.dates[20], end=series.dates[140], vol_window=20
    )

    open1 = provider.open_snapshot(d_switch)
    open2 = prov2.open_snapshot(d_switch)
    assert open1.underlier == open2.underlier
    assert set(open1.option_quotes) == set(open2.option_quotes)
    for oid in open1.option_quotes:
        assert open1.mid(oid) == pytest.approx(open2.mid(oid), abs=1e-12)

    # 收盘快照确实使用了今日信息：波动率随今日跳变而不同
    assert provider.sigma_at(d_switch) != pytest.approx(provider.sigma_at(prev), rel=1e-6)


def test_dividend_yield_at_trailing_12m():
    series = make_series()
    d = series.dates[80]
    prev_close = float(series.close[79])
    divs = {
        d - timedelta(days=100): 1.0,
        d - timedelta(days=10): 2.0,
        d - timedelta(days=400): 9.0,  # 超窗忽略
    }
    series.dividends = divs
    prov = HybridProvider(series=series, start=series.dates[20], end=series.dates[140])
    assert prov.dividend_yield_at(d) == pytest.approx(3.0 / prev_close)
    assert prov.dividend_yield_at(series.dates[81]) == pytest.approx(3.0 / float(series.close[80]))


def test_extra_strikes_quotable(provider):
    series = make_series()
    d = series.dates[60]
    S = provider.close_snapshot(d).underlier.price
    expiries = [e for e in monthly_expiries(d, d + timedelta(days=120)) if 7 <= CAL.dte(d, e) <= 90]
    e = expiries[len(expiries) // 2]
    k = round(S * 0.7 / 0.5) * 0.5  # 网格外的深虚值
    snap = provider.close_snapshot(d, extra_strikes={(e, k)})
    assert (e, k, PUT) in snap.option_quotes


def test_end_to_end_engine_run():
    series = make_series()
    start, end = series.dates[30], series.dates[-1]
    provider = HybridProvider(series=series, start=start, end=end, vol_window=20)
    cfg = BacktestConfig(data=DataConfig(symbol="SPY", start=start, end=end))
    result = SimulationEngine(cfg).run(provider, CAL)
    assert len(result.states) == len(provider.sessions())
    assert result.states[-1].benchmark_price == pytest.approx(float(series.close[-1]))
    assert math.isfinite(result.states[-1].equity)
    assert result.trades, "半年的 weekly Sell Put 应产生交易"


def test_build_provider_hybrid_from_csv(tmp_path):
    series = make_series()
    start, end = series.dates[30], series.dates[120]
    import pandas as pd

    csv_path = tmp_path / "manual.csv"
    pd.DataFrame(
        {
            "Date": series.dates,
            "Open": series.open,
            "High": series.high,
            "Low": series.low,
            "Close": series.close,
            "Volume": series.volume,
        }
    ).to_csv(csv_path, index=False)
    cfg = BacktestConfig(
        data=DataConfig(
            provider="hybrid",
            symbol="SPY",
            start=start,
            end=end,
            hybrid=HybridConfig(
                cache_dir=str(tmp_path), csv_path=str(csv_path), offline=True, buffer_days=30
            ),
        )
    )
    provider = build_provider(cfg)
    assert isinstance(provider, HybridProvider)
    assert provider.sessions()[0] == start and provider.sessions()[-1] == end
