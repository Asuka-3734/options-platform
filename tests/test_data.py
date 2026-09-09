"""data.py 单元测试（M1-A）：CSV 解析 / 缓存 / 兜底 / 报错，全程离线（不触网）。"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from sellput import data as data_mod
from sellput.data import PriceSeries, load_price_series

START = date(2020, 3, 1)
END = date(2020, 3, 20)


def _write_csv(path, first: date, n: int, with_divs: bool = True, yahoo_extra: bool = False):
    dates = [first + timedelta(days=i) for i in range(n)]
    df = pd.DataFrame(
        {
            "Date": dates,
            "Open": np.arange(n, dtype=float) + 100.0,
            "High": np.arange(n, dtype=float) + 101.0,
            "Low": np.arange(n, dtype=float) + 99.0,
            "Close": np.arange(n, dtype=float) + 100.5,
            "Volume": np.full(n, 1000.0),
        }
    )
    if yahoo_extra:
        df["Adj Close"] = df["Close"] * 0.99
    if with_divs:
        df["Dividends"] = [0.5 if i == 30 else 0.0 for i in range(n)]
    df.to_csv(path, index=False)


def _fixture_series(symbol: str = "SPY") -> PriceSeries:
    dates = [START - timedelta(days=20) + timedelta(days=i) for i in range(60)]
    return PriceSeries(
        symbol=symbol,
        dates=dates,
        open=np.arange(60, dtype=float) + 100.0,
        close=np.arange(60, dtype=float) + 100.5,
        high=np.arange(60, dtype=float) + 101.0,
        low=np.arange(60, dtype=float) + 99.0,
        volume=np.full(60, 1000.0),
        dividends={},
        source="yfinance",
    )


def test_csv_fallback_then_cache(tmp_path):
    _write_csv(tmp_path / "manual.csv", first=date(2020, 2, 10), n=90)
    s1 = load_price_series(
        symbol="SPY", start=START, end=END, cache_dir=str(tmp_path),
        buffer_days=8, allow_network=False,
    )
    assert s1.source == "csv"
    assert s1.dates[0] == START - timedelta(days=8)
    assert s1.dates[-1] == END
    # 兜底成功后写缓存；第二次直接命中缓存
    s2 = load_price_series(
        symbol="SPY", start=START, end=END, cache_dir=str(tmp_path),
        buffer_days=8, allow_network=False,
    )
    assert s2.source == "cache"
    assert np.array_equal(s1.close, s2.close)
    assert (tmp_path / "spy_daily.csv").exists()


def test_yfinance_success_writes_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(data_mod, "_fetch_yfinance", lambda *a, **k: _fixture_series())
    s1 = load_price_series(
        symbol="SPY", start=START, end=END, cache_dir=str(tmp_path), buffer_days=8
    )
    assert s1.source == "yfinance"
    # 第二次走缓存（不触网）
    def boom(*a, **k):
        raise AssertionError("不应再调 yfinance")

    monkeypatch.setattr(data_mod, "_fetch_yfinance", boom)
    s2 = load_price_series(
        symbol="SPY", start=START, end=END, cache_dir=str(tmp_path), buffer_days=8
    )
    assert s2.source == "cache"


def test_yfinance_failure_falls_back_to_csv(tmp_path, monkeypatch):
    _write_csv(tmp_path / "manual.csv", first=date(2020, 2, 10), n=90)
    monkeypatch.setattr(data_mod, "_fetch_yfinance", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("network down")))
    s = load_price_series(
        symbol="SPY", start=START, end=END, cache_dir=str(tmp_path), buffer_days=8
    )
    assert s.source == "csv"
    assert (tmp_path / "spy_daily.csv").exists()  # 兜底成功也写缓存


def test_yahoo_export_columns_accepted(tmp_path):
    _write_csv(tmp_path / "manual.csv", first=date(2020, 2, 10), n=90, yahoo_extra=True)
    s = load_price_series(
        symbol="SPY", start=START, end=END, cache_dir=str(tmp_path),
        buffer_days=8, allow_network=False,
    )
    assert s.source == "csv"
    assert s.dividends  # Dividends 列被解析
    assert all(np.isfinite(s.high)) and all(np.isfinite(s.low))


def test_insufficient_coverage_raises(tmp_path):
    _write_csv(tmp_path / "manual.csv", first=START, n=10)  # 覆盖不到 fetch_start
    with pytest.raises(RuntimeError) as ei:
        load_price_series(
            symbol="SPY", start=START, end=END, cache_dir=str(tmp_path),
            buffer_days=8, allow_network=False,
        )
    msg = str(ei.value)
    assert "HTTPS_PROXY" in msg and "CSV" in msg


def test_no_data_clear_error_message(tmp_path):
    with pytest.raises(RuntimeError) as ei:
        load_price_series(
            symbol="SPY", start=START, end=END, cache_dir=str(tmp_path),
            buffer_days=8, allow_network=False,
        )
    msg = str(ei.value)
    assert "无法获取 SPY" in msg
    assert "setx HTTPS_PROXY" in msg
    assert "Date,Open,High,Low,Close,Volume" in msg


def test_build_series_dedupes_sorts_drops_nan():
    dates = [date(2020, 1, 3), date(2020, 1, 2), date(2020, 1, 2), date(2020, 1, 4)]
    opens = pd.Series([100.0, 101.0, 102.0, np.nan])
    closes = pd.Series([100.5, 101.5, 103.0, 104.5])  # 1/2 重复：保留最后一条
    s = data_mod._build_series(
        symbol="spy", dates=dates, opens=opens, closes=closes,
        highs=None, lows=None, volumes=None, dividends={}, source="csv",
    )
    assert s.dates == [date(2020, 1, 2), date(2020, 1, 3)]
    assert list(s.open) == [102.0, 100.0]
    assert list(s.close) == [103.0, 100.5]
