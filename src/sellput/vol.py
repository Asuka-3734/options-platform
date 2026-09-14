"""波动率统计（Spec §6.2 / §15 F7；M1-C 实施步骤 2）。

M1-C 的用途：把每笔 Trade 按**入场时点已知的已实现波动率（RV）分位**归入四桶，回答
"低波动 vs 高波动环境下 Sell Put 表现是否不同"（Spec §15 F7 / AC-9）。IV Rank / IV Percentile
按 Spec §6.2 一并提供（供报告与后续研究使用），但 M1-C 的归桶维度是 RV 分位。

口径（写入报告）
----------------
- **RV（已实现波动率）**：``RV_t = std(log_returns[t-window+1 .. t], ddof=1) * sqrt(252)``——
  截至 t 收盘的最近 ``window`` 个日对数收益的样本标准差、年化（Spec §6.2：window 默认 20）。
  与 ``HybridProvider.sigma_at()``（驱动合成链 iv_atm 的值）**完全同口径**；
  有效收益不足 ``window`` 个 → ``nan``。
- **分位（percentile rank）**：当前值在**截至当前**的最近 ``window`` 个有效观测中的分位，
  ``100 * #{v < current} / #有效观测``（窗口含当前观测；相等不算"低于"）。
  有效观测不足 ``min_obs``，或窗口内全部相等（无区分度）→ ``nan``
  （Spec §6.2 的 IV Percentile 定义同口径；"未定义即 nan" 与 `analysis` 模块一致）。
- **IV Rank**：``100 * (v - min) / (max - min)``（Spec §6.2 原始定义）；全部相等 → ``nan``。
- **四桶**（Spec §6.2）：``<25`` / ``25–50`` / ``50–75`` / ``≥75``（下界含、上界不含）。
- **防前视**：``bucket_by_rv(..., shift=1)``（默认）把交易日 t 归到 **t−1 收盘即可知**的分位，
  与引擎「Close(t−1) 信号 → Open(t) 成交」一致（Spec D9 / §15 N3）；
  ``shift=0`` 使用当日收盘 RV，仅适用于事后描述性分析。

本模块为纯函数（输入价格/IV 序列，输出数组或统计表），不做 I/O、不依赖 Provider。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date

import numpy as np

from .analysis import BucketStats, bucket_stats_from_pnls
from .portfolio import Trade

TRADING_DAYS_PER_YEAR = 252.0

#: 四桶标签（Spec §6.2 原文），顺序即报告展示顺序
RV_BUCKETS: tuple[str, ...] = ("<25", "25–50", "50–75", "≥75")


def realized_vol(
    closes: Sequence[float] | np.ndarray,
    *,
    window: int = 20,
    annualization: float = TRADING_DAYS_PER_YEAR,
) -> np.ndarray:
    """逐日已实现波动率（年化）；截至当前收盘的最近 ``window`` 个日对数收益、样本标准差。

    返回与 ``closes`` 等长的数组，前 ``window`` 个为 ``nan``（窗口不足）。
    """
    if window < 2:
        raise ValueError("window must be >= 2")
    arr = np.asarray(closes, dtype=float)
    out = np.full(arr.shape, np.nan)
    if arr.size <= window:
        return out
    if np.any(arr <= 0.0):
        raise ValueError("closes must be strictly positive")
    log_rets = np.diff(np.log(arr))
    for i in range(window, arr.size):
        seg = log_rets[i - window : i]
        out[i] = float(np.std(seg, ddof=1) * math.sqrt(annualization))
    return out


def rolling_percentile(
    values: Sequence[float] | np.ndarray,
    *,
    window: int = 252,
    min_obs: int = 20,
) -> np.ndarray:
    """当前值在最近 ``window`` 个有效观测中的分位（0–100，含当前观测、严格小于计数）。

    有效观测不足 ``min_obs``、当前值非有限、或窗口内全部相等 → ``nan``。
    """
    if window < 2:
        raise ValueError("window must be >= 2")
    if min_obs < 2:
        raise ValueError("min_obs must be >= 2")
    arr = np.asarray(values, dtype=float)
    out = np.full(arr.shape, np.nan)
    for i in range(arr.size):
        current = arr[i]
        if not math.isfinite(float(current)):
            continue
        seg = arr[max(0, i - window + 1) : i + 1]
        seg = seg[np.isfinite(seg)]
        if seg.size < min_obs:
            continue
        if float(seg.max()) == float(seg.min()):
            continue  # 无区分度 → 无法判断高/低
        out[i] = 100.0 * float(np.count_nonzero(seg < current)) / float(seg.size)
    return out


def iv_rank(
    values: Sequence[float] | np.ndarray,
    *,
    window: int = 252,
    min_obs: int = 20,
) -> np.ndarray:
    """IV Rank（Spec §6.2）：``100 * (v - min) / (max - min)``，窗口为最近 ``window`` 个有效观测。

    全部相等（无区分度）或有效观测不足 ``min_obs`` → ``nan``。
    """
    if window < 2:
        raise ValueError("window must be >= 2")
    if min_obs < 2:
        raise ValueError("min_obs must be >= 2")
    arr = np.asarray(values, dtype=float)
    out = np.full(arr.shape, np.nan)
    for i in range(arr.size):
        current = arr[i]
        if not math.isfinite(float(current)):
            continue
        seg = arr[max(0, i - window + 1) : i + 1]
        seg = seg[np.isfinite(seg)]
        if seg.size < min_obs:
            continue
        lo, hi = float(seg.min()), float(seg.max())
        if hi == lo:
            continue
        out[i] = 100.0 * (float(current) - lo) / (hi - lo)
    return out


def iv_percentile(
    values: Sequence[float] | np.ndarray,
    *,
    window: int = 252,
    min_obs: int = 20,
) -> np.ndarray:
    """IV Percentile（Spec §6.2：``P(IV_252d < IV_t)``）——`rolling_percentile` 的同义入口。"""
    return rolling_percentile(values, window=window, min_obs=min_obs)


def rv_bucket(percentile: float | None) -> str | None:
    """分位 → 四桶标签（下界含、上界不含）；``nan``/``None`` → ``None``。"""
    if percentile is None:
        return None
    value = float(percentile)
    if not math.isfinite(value):
        return None
    if value < 25.0:
        return RV_BUCKETS[0]
    if value < 50.0:
        return RV_BUCKETS[1]
    if value < 75.0:
        return RV_BUCKETS[2]
    return RV_BUCKETS[3]


def bucket_by_rv(
    sessions: Sequence[date],
    rv_percentiles: Sequence[float] | np.ndarray,
    *,
    shift: int = 1,
) -> dict[date, str]:
    """交易日 → RV 分位桶（Spec §15 F7 / AC-9）。

    Args:
        sessions: 交易日序列（与 ``rv_percentiles`` 等长、同序）。
        rv_percentiles: 各交易日的 RV 分位（``rolling_percentile`` 输出，可含 ``nan``）。
        shift: ``1``（默认）表示交易日 t 使用 **t−shift** 的分位（入场决策时点已知，防前视）；
            ``0`` 表示使用当日收盘分位（事后描述性分析）。
    """
    if shift < 0:
        raise ValueError("shift must be >= 0")
    pct = np.asarray(rv_percentiles, dtype=float)
    if pct.shape[0] != len(sessions):
        raise ValueError("sessions and rv_percentiles must have the same length")
    out: dict[date, str] = {}
    for i, day in enumerate(sessions):
        j = i - shift
        if j < 0:
            continue
        bucket = rv_bucket(float(pct[j]))
        if bucket is not None:
            out[day] = bucket
    return out


def rv_bucket_stats(
    trades: Sequence[Trade],
    buckets_by_date: Mapping[date, str],
) -> tuple[BucketStats, ...]:
    """按 RV 桶汇总交易表现，返回**固定四行**（含零样本桶，便于报告表格稳定）。

    归桶键为 ``trade.entry_date``；无法归桶的交易（窗口不足 / 入场日不在 buckets 中）被跳过，
    因此各行笔数合计可能少于交易总数（报告需明示）。
    """
    grouped: dict[str, list[float]] = {bucket: [] for bucket in RV_BUCKETS}
    for trade in trades:
        bucket = buckets_by_date.get(trade.entry_date)
        if bucket is None:
            continue
        grouped[bucket].append(float(trade.pnl))
    return tuple(bucket_stats_from_pnls(bucket, grouped[bucket]) for bucket in RV_BUCKETS)
