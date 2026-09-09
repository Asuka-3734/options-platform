"""真实标的价格数据接入（M1-A，Spec §13）。

三级获取顺序（与用户确认的设计）：
1. 本地缓存 data/<symbol>_daily.csv —— 命中则完全离线、逐位可复现
2. yfinance —— 抓 SPY 日线（OHLCV，未复权）+ 分红记录，成功后写入缓存
3. 手动 CSV 兜底 —— 网络不可达时读取用户放置的 CSV（兼容 Yahoo 网页导出的列）

代理：读取环境变量 HTTPS_PROXY / HTTP_PROXY（yfinance 底层的 curl_cffi 会自动
遵循）。Windows 上 Python 默认不走系统代理，数据中心出口 IP 直连会被 Yahoo 429 限流。
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

_COVER_TOLERANCE_DAYS = 7


@dataclass(slots=True)
class PriceSeries:
    """真实标的日线序列（未复权口径；期权行权价按未复权价定义）。

    dates 严格升序、无重复；open/close/high/low/volume 为等长 numpy 数组；
    dividends 为 {除息日: 每股现金分红}。
    """

    symbol: str
    dates: list[date]
    open: np.ndarray
    close: np.ndarray
    high: np.ndarray
    low: np.ndarray
    volume: np.ndarray
    dividends: dict[date, float]
    source: str  # "yfinance" | "csv" | "cache"


def load_price_series(
    *,
    symbol: str,
    start: date,
    end: date,
    cache_dir: str = "data",
    csv_path: str | None = None,
    buffer_days: int = 70,
    allow_network: bool = True,
) -> PriceSeries:
    """加载 [start - buffer_days, end] 区间的真实日线（缓冲用于滚动波动率与前收）。"""
    if end <= start:
        raise ValueError("end 必须晚于 start")
    fetch_start = start - timedelta(days=buffer_days)
    cache_dir_path = Path(cache_dir)
    cache_file = cache_dir_path / f"{symbol.lower()}_daily.csv"

    # 1) 本地缓存（离线可复现的基石）
    if cache_file.exists():
        cached = _read_csv(cache_file, symbol)
        if _covers(cached, fetch_start, end):
            return _trim(dataclasses.replace(cached, source="cache"), fetch_start, end)

    yf_failure: Exception | None = None
    # 2) yfinance（需网络；代理走 HTTPS_PROXY/HTTP_PROXY 环境变量）
    if allow_network:
        try:
            series = _fetch_yfinance(symbol, fetch_start, end)
            _save_cache(series, cache_file)
            return series
        except Exception as err:  # noqa: BLE001 任何失败都落到 CSV 兜底
            yf_failure = err

    # 3) 手动 CSV 兜底
    manual = _find_manual_csv(cache_dir_path, csv_path, symbol, fetch_start, end)
    if manual is not None:
        _save_cache(manual, cache_file)
        return manual

    raise RuntimeError(_no_data_message(symbol, cache_dir_path, yf_failure))


# ---------------------------------------------------------------------------
# 三级来源的具体实现
# ---------------------------------------------------------------------------


def _fetch_yfinance(symbol: str, fetch_start: date, end: date) -> PriceSeries:
    """yfinance 抓取（懒加载依赖）。失败抛出异常，由上层兜底。"""
    import yfinance as yf  # 懒加载：无网络环境也能导入核心库

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            df = yf.download(
                symbol,
                start=fetch_start,
                end=end + timedelta(days=1),
                auto_adjust=False,
                progress=False,
                threads=False,
            )
            if df is None or len(df) == 0:
                raise RuntimeError("yfinance 返回空数据")
            if isinstance(df.columns, pd.MultiIndex):
                df = df.droplevel(1, axis=1)  # 去掉 (Close, SPY) 中的标的层级
            dividends: dict[date, float] = {}
            try:
                div_series = yf.Ticker(symbol).dividends
                if div_series is not None and len(div_series):
                    for ts, v in div_series.items():
                        d = pd.Timestamp(ts).date()
                        if fetch_start <= d <= end and float(v) > 0:
                            dividends[d] = float(v)
            except Exception:  # noqa: BLE001 分红拿不到不算致命（q 回退 0）
                pass
            return _build_series(
                symbol=symbol,
                dates=[pd.Timestamp(ts).date() for ts in df.index],
                opens=pd.to_numeric(df["Open"], errors="coerce"),
                closes=pd.to_numeric(df["Close"], errors="coerce"),
                highs=pd.to_numeric(df["High"], errors="coerce") if "High" in df else None,
                lows=pd.to_numeric(df["Low"], errors="coerce") if "Low" in df else None,
                volumes=pd.to_numeric(df["Volume"], errors="coerce") if "Volume" in df else None,
                dividends=dividends,
                source="yfinance",
            )
        except Exception as err:  # noqa: BLE001
            last_err = err
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"yfinance 抓取失败（重试 3 次）：{last_err}") from last_err


def _find_manual_csv(
    cache_dir_path: Path, csv_path: str | None, symbol: str, fetch_start: date, end: date
) -> PriceSeries | None:
    if csv_path:
        candidates = [Path(csv_path)]
    else:
        candidates = sorted(cache_dir_path.glob("*.csv")) if cache_dir_path.is_dir() else []
    for path in candidates:
        try:
            series = _read_csv(path, symbol)
        except Exception:  # noqa: BLE001 候选文件格式不对则试下一个
            continue
        if _covers(series, fetch_start, end):
            return _trim(series, fetch_start, end)
    return None


def _covers(series: PriceSeries, fetch_start: date, end: date) -> bool:
    if not series.dates:
        return False
    first_ok = series.dates[0] <= fetch_start + timedelta(days=_COVER_TOLERANCE_DAYS)
    last_ok = series.dates[-1] >= end - timedelta(days=_COVER_TOLERANCE_DAYS)
    return first_ok and last_ok


def _read_csv(path: Path, symbol: str) -> PriceSeries:
    """读取 CSV（兼容 Yahoo 网页导出的标准列；Dividends 列可选）。"""
    raw = pd.read_csv(path)
    cols = {str(c).strip().lower(): c for c in raw.columns}
    if "date" not in cols:
        raise ValueError(f"{path} 缺少 Date 列")
    dates_raw = pd.to_datetime(raw[cols["date"]], errors="coerce")
    opens = pd.to_numeric(raw[cols["open"]], errors="coerce") if "open" in cols else None
    closes = pd.to_numeric(raw[cols["close"]], errors="coerce") if "close" in cols else None
    if opens is None or closes is None:
        raise ValueError(f"{path} 缺少必需列 Open/Close")
    dividends: dict[date, float] = {}
    if "dividends" in cols:
        divs = pd.to_numeric(raw[cols["dividends"]], errors="coerce").fillna(0.0)
        for d, v in zip(dates_raw, divs, strict=True):
            if pd.notna(d) and float(v) > 0:
                dividends[pd.Timestamp(d).date()] = float(v)
    return _build_series(
        symbol=symbol,
        dates=[pd.Timestamp(d).date() for d in dates_raw],
        opens=opens,
        closes=closes,
        highs=pd.to_numeric(raw[cols["high"]], errors="coerce") if "high" in cols else None,
        lows=pd.to_numeric(raw[cols["low"]], errors="coerce") if "low" in cols else None,
        volumes=pd.to_numeric(raw[cols["volume"]], errors="coerce") if "volume" in cols else None,
        dividends=dividends,
        source="csv",
    )


def _build_series(
    *,
    symbol: str,
    dates: list[date],
    opens: pd.Series | None,
    closes: pd.Series | None,
    highs: pd.Series | None,
    lows: pd.Series | None,
    volumes: pd.Series | None,
    dividends: dict[date, float],
    source: str,
) -> PriceSeries:
    """校验、去重（保留最后一条）、升序后构造 PriceSeries。"""
    if opens is None or closes is None:
        raise ValueError("缺少 Open/Close 数据")
    n = len(dates)
    if n == 0:
        raise ValueError("没有数据行")

    def _arr(s: pd.Series | None, fallback: float) -> np.ndarray:
        if s is None:
            return np.full(n, fallback)
        return s.to_numpy(dtype=float)

    opens_arr = _arr(opens, float("nan"))
    closes_arr = _arr(closes, float("nan"))
    highs_arr = _arr(highs, float("nan"))
    lows_arr = _arr(lows, float("nan"))
    volumes_arr = _arr(volumes, 0.0)

    # 去重（同日期保留最后一条）并升序
    keep: dict[date, int] = {}
    for i, d in enumerate(dates):
        if pd.isna(opens_arr[i]) or pd.isna(closes_arr[i]):
            continue  # 价格缺失的行整行剔除
        keep[d] = i
    ordered = sorted(keep.items())
    if not ordered:
        raise ValueError("没有有效的价格行")
    pos = [i for _, i in ordered]
    return PriceSeries(
        symbol=symbol.upper(),
        dates=[d for d, _ in ordered],
        open=opens_arr[pos],
        close=closes_arr[pos],
        high=highs_arr[pos],
        low=lows_arr[pos],
        volume=volumes_arr[pos],
        dividends={d: v for d, v in dividends.items() if d in keep},
        source=source,
    )


def _trim(series: PriceSeries, fetch_start: date, end: date) -> PriceSeries:
    idx = [i for i, d in enumerate(series.dates) if fetch_start <= d <= end]
    return dataclasses.replace(
        series,
        dates=[series.dates[i] for i in idx],
        open=series.open[idx],
        close=series.close[idx],
        high=series.high[idx],
        low=series.low[idx],
        volume=series.volume[idx],
        dividends={d: v for d, v in series.dividends.items() if fetch_start <= d <= end},
    )


def _save_cache(series: PriceSeries, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(
        {
            "Date": series.dates,
            "Open": series.open,
            "High": series.high,
            "Low": series.low,
            "Close": series.close,
            "Volume": series.volume,
            "Dividends": [series.dividends.get(d, 0.0) for d in series.dates],
        }
    )
    df.to_csv(path, index=False)


def _no_data_message(symbol: str, cache_dir: Path, yf_failure: Exception | None) -> str:
    return (
        f"无法获取 {symbol} 历史日线。\n"
        f"  yfinance 失败原因：{yf_failure}\n"
        "  请二选一：\n"
        "  1) 配置代理后重试（Windows 上 Python 默认不走系统代理）：\n"
        '     setx HTTPS_PROXY "http://127.0.0.1:<port>"\n'
        '     setx HTTP_PROXY "http://127.0.0.1:<port>"（主机与端口换成你代理软件的实际值，重开终端）\n'
        f"  2) 手动放置 CSV 到 {cache_dir} 目录（任意文件名，如 {symbol.lower()}_daily.csv），\n"
        "     列：Date,Open,High,Low,Close,Volume[,Dividends]（Yahoo 网页导出的格式可直接用）。\n"
    )
