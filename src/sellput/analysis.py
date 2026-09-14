"""指标与归因（Spec §6 / §15.5；M1-C 实施步骤 1）。

设计原则
--------
1. **纯函数**：输入引擎产出（`PortfolioState` 序列 + `Trade` 清单），输出 `Metrics`；
   不做 I/O、不含随机性、不依赖引擎内部状态 —— 研究层可任意重复调用（Spec §15 N2/N3）。
2. **未定义就是未定义**：分母为 0 的比率返回 `None`，不用 0 或 inf 冒充（避免排序与报告误读）。
3. **口径写进 docstring 并逐条测试**（Spec §15 AC-3/AC-4）。

口径约定（M1-C）
----------------
- 日收益：``r0 = equity[0] / starting_cash - 1``，``ri = equity[i] / equity[i-1] - 1``
  （首个状态日之前发生的成交成本与浮动盈亏计入 r0，不丢失）。
- 年化折算统一按 252 交易日：``years = n_sessions / 252``。
- **CAGR**：``(equity[-1] / starting_cash) ** (252 / n_sessions) - 1``。
- **年化波动率**：``std(daily_returns, ddof=1) * sqrt(252)``（样本标准差；日收益 < 2 个 → None）。
- **Sharpe**：``(mean(r) - rf/252) / std(r) * sqrt(252)``（rf 年化可配、默认 0；std = 0 → None）。
- **Sortino**：``(mean(r) - rf/252) / downside * sqrt(252)``，
  ``downside = sqrt(mean(min(r - rf/252, 0) ** 2))``（对全部观测的下行均方偏差；无下行 → None）。
- **Calmar**：``cagr / |max_dd|``；无回撤（max_dd = 0）或 cagr 未定义 → None。
- **最大回撤**：``min(equity / cummax(equity) - 1)``（与既有 M1-A/M1-B 报告口径一致）。
- **回撤持续天数**：最长水下期的**日历天数** —— 某峰值日到该峰值被收复（equity ≥ 峰值）之日；
  期末仍未收复则以最后一个状态日计。
- **胜率**：``pnl > 0`` 的交易占比（与既有报告口径一致）。
- **Profit Factor**：``Σ正pnl / |Σ负pnl|``；无亏损交易 → None（不返回 inf）。
- **P5 / 最差单笔**：交易 PnL 的 5% 分位（``numpy.percentile`` 默认 linear 插值）与最小值。
- **资本效率**：``Σ(期权开仓权利金) / mean(保证金占用)``；无期权成交或平均保证金 ≤ 0 → None。
- **基准**：价格型 = ``benchmark_price[-1] / benchmark_price[0] - 1``（现有口径，Spec §6.4）；
  总回报型（分红再投）由 `total_return_index` 计算并经 `benchmark_total_index` 传入
  （Spec §6.4 最小设计：除息日分红按次一交易日开盘价再投、无摩擦无税、不动引擎与 B&H 策略）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import date
from itertools import pairwise

import numpy as np

from .portfolio import PortfolioState, Trade

TRADING_DAYS_PER_YEAR = 252.0


@dataclass(frozen=True, slots=True)
class Metrics:
    """回测指标（数据模型见 Spec §15.5）。

    `start` / `end` / `n_sessions` 为**溯源字段**（非金融指标），供 manifest 与复现使用
    （Spec §15 N2 / AC-8）；其余字段与 Spec §15.5 一一对应。
    """

    # ---- 收益 ----
    total_return: float
    cagr: float | None
    ann_vol: float | None
    sharpe: float | None
    sortino: float | None
    calmar: float | None
    # ---- 回撤 ----
    max_dd: float
    dd_duration_days: int
    # ---- 交易 ----
    n_trades: int
    win_rate: float | None
    profit_factor: float | None
    avg_win: float | None
    avg_loss: float | None
    trades_per_year: float | None
    p5_trade_pnl: float | None
    worst_trade_pnl: float | None
    # ---- 基准（§6.4：总回报型按 §15.7 第 8 步交付）----
    benchmark_price_return: float | None
    benchmark_total_return: float | None
    excess_vs_price: float | None
    excess_vs_total: float | None
    # ---- 效率 ----
    capital_efficiency: float | None
    # ---- 溯源 ----
    start: date
    end: date
    n_sessions: int

    def to_dict(self) -> dict[str, float | int | str | None]:
        """扁平 dict（date → ISO 字符串），供 metrics.json / sweep CSV 落盘。"""
        out: dict[str, float | int | str | None] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = value.isoformat() if isinstance(value, date) else value
        return out


# ---------------------------------------------------------------------------
# 序列工具（纯函数，供报告层复用）
# ---------------------------------------------------------------------------


def equity_series(states: Sequence[PortfolioState]) -> list[float]:
    """逐日账户净值序列。"""
    return [float(s.equity) for s in states]


def daily_returns(states: Sequence[PortfolioState], *, starting_cash: float) -> list[float]:
    """日收益序列（首个观测相对起始资金；Spec §15 口径）。"""
    eq = equity_series(states)
    if not eq:
        return []
    out = [eq[0] / starting_cash - 1.0]
    for prev, cur in pairwise(eq):
        out.append(cur / prev - 1.0)
    return out


def drawdown_series(equity: Sequence[float]) -> list[float]:
    """回撤序列（相对历史峰值，≤ 0）。"""
    out: list[float] = []
    peak = float("-inf")
    for value in equity:
        peak = max(peak, value)
        out.append(value / peak - 1.0 if peak > 0 else 0.0)
    return out


def trade_pnls(trades: Sequence[Trade]) -> list[float]:
    """逐笔已实现 PnL（开平配对单位，含手续费）。"""
    return [float(t.pnl) for t in trades]


# ---------------------------------------------------------------------------
# 基准（Spec §6.4）
# ---------------------------------------------------------------------------


def total_return_index(
    *,
    dates: Sequence[date],
    opens: Sequence[float],
    closes: Sequence[float],
    dividends: Mapping[date, float] | None = None,
    base: float = 1.0,
) -> list[float]:
    """总回报指数（分红再投）—— Spec §6.4 的最小设计，纯函数、不动引擎。

    口径：
    - 价格项按**收盘价**估值；
    - 除息日 d 的每股现金分红在**次一交易日开盘价**全额再投：``份额 × (1 + D / open[d+1])``；
    - 全程无摩擦、无税；最后一个交易日发生除息时无可再投的开盘价 → 该笔不再投（并在报告中声明）；
    - `dividends` 缺失（合成数据无分红记录）时退化为价格指数（调用方需在报告中显式声明降级）。
    """
    if len(dates) != len(opens) or len(dates) != len(closes):
        raise ValueError("dates / opens / closes 长度必须一致")
    if not dates:
        return []
    if closes[0] <= 0:
        raise ValueError("closes must be positive")
    divs = dividends or {}
    shares = base / float(closes[0])
    out: list[float] = []
    for i, _day in enumerate(dates):
        if i > 0:
            amount = divs.get(dates[i - 1])
            if amount:
                open_today = float(opens[i])
                if open_today <= 0:
                    raise ValueError("opens must be positive")
                shares *= 1.0 + float(amount) / open_today
        out.append(shares * float(closes[i]))
    return out


# ---------------------------------------------------------------------------
# 分桶统计（RV 桶见 `vol` 模块；此处提供入场 DTE 分桶，Spec §15 F3 / AC-10）
# ---------------------------------------------------------------------------

#: 实际入场 DTE 分桶（Spec §15 F3/AC-10：按实际入场 DTE 解释参数敏感性）
DEFAULT_DTE_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("≤14", 0, 14),
    ("15–21", 15, 21),
    ("22–28", 22, 28),
    ("29–35", 29, 35),
    ("36–45", 36, 45),
    ("≥46", 46, 10**6),
)


@dataclass(frozen=True, slots=True)
class BucketStats:
    """单桶统计（Spec §15 AC-9/AC-10：笔数 / 胜率 / 平均 PnL）。"""

    bucket: str
    n_trades: int
    win_rate: float | None
    avg_pnl: float | None

    def to_dict(self) -> dict[str, float | int | str | None]:
        return {
            "bucket": self.bucket,
            "n_trades": self.n_trades,
            "win_rate": self.win_rate,
            "avg_pnl": self.avg_pnl,
        }


def bucket_stats_from_pnls(bucket: str, pnls: Sequence[float]) -> BucketStats:
    """由一个桶内的 PnL 列表生成统计行（笔数 / 胜率 / 平均 PnL）。"""
    values = [float(p) for p in pnls]
    n = len(values)
    wins = sum(1 for p in values if p > 0)
    return BucketStats(
        bucket=bucket,
        n_trades=n,
        win_rate=(wins / n) if n else None,
        avg_pnl=(sum(values) / n) if n else None,
    )


def entry_dte_bucket_stats(
    trades: Sequence[Trade],
    *,
    buckets: Sequence[tuple[str, int, int]] = DEFAULT_DTE_BUCKETS,
) -> tuple[BucketStats, ...]:
    """按**实际入场 DTE** 汇总交易表现（Spec §15 F3 / AC-10）。

    合成链是月度到期结构，目标 DTE 与实际入场 DTE 会相差约 ±10 个交易日，
    因此参数敏感性必须按实际入场 DTE 解释（`Trade.dte_at_entry`）。
    无 `dte_at_entry` 的交易（股票）被跳过；返回固定行数（含零样本桶）。
    """
    grouped: dict[str, list[float]] = {label: [] for label, _lo, _hi in buckets}
    for trade in trades:
        dte = trade.dte_at_entry
        if dte is None or trade.asset_kind != "option":
            continue
        for label, lo, hi in buckets:
            if lo <= dte <= hi:
                grouped[label].append(float(trade.pnl))
                break
    return tuple(bucket_stats_from_pnls(label, grouped[label]) for label, _lo, _hi in buckets)


def _max_drawdown_duration_days(states: Sequence[PortfolioState]) -> int:
    """最长水下期（日历天数）：峰值 → 收复；期末未收复计至最后一个状态日。

    只有真正跌破过峰值（严格小于）才算一段水下期，因此全程无回撤时返回 0。
    """
    eq = equity_series(states)
    dates = [s.date for s in states]
    best = 0
    peak_idx = 0
    peak = eq[0]
    underwater = False
    for i in range(1, len(eq)):
        if eq[i] >= peak:
            if underwater:
                best = max(best, (dates[i] - dates[peak_idx]).days)
            peak = eq[i]
            peak_idx = i
            underwater = False
        else:
            underwater = True
    if underwater:
        best = max(best, (dates[-1] - dates[peak_idx]).days)
    return best


def _capital_efficiency(states: Sequence[PortfolioState], trades: Sequence[Trade]) -> float | None:
    """资本效率 = 累计期权开仓权利金 / 平均保证金占用（Spec §6.1）。"""
    premium = 0.0
    for trade in trades:
        if getattr(trade, "asset_kind", "option") != "option":
            continue
        multiplier = float(getattr(trade.spec, "multiplier", 100))
        premium += abs(float(trade.qty)) * float(trade.entry_price) * multiplier
    if premium <= 0:
        return None
    margins = [float(s.margin_used) for s in states]
    mean_margin = float(np.mean(margins)) if margins else 0.0
    if mean_margin <= 0:
        return None
    return premium / mean_margin


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def compute_metrics(
    states: Sequence[PortfolioState],
    trades: Sequence[Trade],
    *,
    starting_cash: float,
    risk_free_rate: float = 0.0,
    benchmark_total_index: Sequence[float] | None = None,
    benchmark_total_base: float | None = None,
) -> Metrics:
    """从回测产出计算 Spec §15.5 全套指标（纯函数，确定性）。

    Args:
        states: 逐日 `PortfolioState`（引擎产出，按期升序）。
        trades: 已实现交易清单（开平配对）。
        starting_cash: 期初资金（日收益与总收益的基准）。
        risk_free_rate: 年化无风险利率（仅影响 Sharpe/Sortino；默认 0）。
        benchmark_total_index: 与 `states` 等长的**总回报基准指数**（`total_return_index` 输出，
            Spec §6.4）；未提供时 `benchmark_total_return` / `excess_vs_total` 为 `None`。
        benchmark_total_base: 基准起点值（默认取 ``benchmark_total_index[0]``）。分段计算时传
            "上一段末值"，使基准与净值口径一致（首日 P&L 计入本段，train×test 复合 == full）。
    """
    if not states:
        raise ValueError("compute_metrics requires at least one portfolio state")
    if starting_cash <= 0:
        raise ValueError("starting_cash must be positive")

    eq = equity_series(states)
    n = len(eq)
    final = eq[-1]
    total_return = final / starting_cash - 1.0

    cagr = (final / starting_cash) ** (TRADING_DAYS_PER_YEAR / n) - 1.0 if final > 0 else None

    rets = np.asarray(daily_returns(states, starting_cash=starting_cash), dtype=float)
    rf_daily = risk_free_rate / TRADING_DAYS_PER_YEAR
    ann_vol: float | None = None
    sharpe: float | None = None
    if rets.size >= 2:
        sd = float(np.std(rets, ddof=1))
        ann_vol = sd * math.sqrt(TRADING_DAYS_PER_YEAR)
        if sd > 0:
            sharpe = (float(np.mean(rets)) - rf_daily) / sd * math.sqrt(TRADING_DAYS_PER_YEAR)

    sortino: float | None = None
    if rets.size >= 1:
        downside = float(math.sqrt(float(np.mean(np.minimum(rets - rf_daily, 0.0) ** 2))))
        if downside > 0:
            sortino = (float(np.mean(rets)) - rf_daily) / downside * math.sqrt(
                TRADING_DAYS_PER_YEAR
            )

    max_dd = min(drawdown_series(eq))
    dd_duration_days = _max_drawdown_duration_days(states)
    calmar = cagr / abs(max_dd) if (cagr is not None and max_dd < 0) else None

    pnls = trade_pnls(trades)
    n_trades = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    years = n / TRADING_DAYS_PER_YEAR
    win_rate = len(wins) / n_trades if n_trades else None
    profit_factor = sum(wins) / abs(sum(losses)) if losses else None
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    trades_per_year = n_trades / years if years > 0 else None
    p5_trade_pnl = float(np.percentile(pnls, 5)) if n_trades else None
    worst_trade_pnl = min(pnls) if n_trades else None

    bench_first = float(states[0].benchmark_price)
    bench_last = float(states[-1].benchmark_price)
    benchmark_price_return = bench_last / bench_first - 1.0 if bench_first > 0 else None
    excess_vs_price = (
        total_return - benchmark_price_return if benchmark_price_return is not None else None
    )

    benchmark_total_return: float | None = None
    if benchmark_total_index is not None:
        if len(benchmark_total_index) != n:
            raise ValueError("benchmark_total_index 必须与 states 等长")
        base_value = (
            float(benchmark_total_index[0])
            if benchmark_total_base is None
            else float(benchmark_total_base)
        )
        if base_value > 0:
            benchmark_total_return = float(benchmark_total_index[-1]) / base_value - 1.0
    excess_vs_total = (
        total_return - benchmark_total_return if benchmark_total_return is not None else None
    )

    return Metrics(
        total_return=total_return,
        cagr=cagr,
        ann_vol=ann_vol,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        max_dd=max_dd,
        dd_duration_days=dd_duration_days,
        n_trades=n_trades,
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_win=avg_win,
        avg_loss=avg_loss,
        trades_per_year=trades_per_year,
        p5_trade_pnl=p5_trade_pnl,
        worst_trade_pnl=worst_trade_pnl,
        benchmark_price_return=benchmark_price_return,
        benchmark_total_return=benchmark_total_return,
        excess_vs_price=excess_vs_price,
        excess_vs_total=excess_vs_total,
        capital_efficiency=_capital_efficiency(states, trades),
        start=states[0].date,
        end=states[-1].date,
        n_sessions=n,
    )
