"""M1-A 一键回测：真实 SPY 日线 + 合成期权链 → 报告 + CSV + 收益曲线 PNG。

用法（在 options-platform 目录下）：
    uv run python scripts/run_backtest.py                    # 默认 2005-01-01 ~ 2024-12-31
    uv run python scripts/run_backtest.py --start 2015-01-01 --end 2024-12-31
    uv run python scripts/run_backtest.py --offline          # 只用本地缓存/手动 CSV，不联网
    uv run python scripts/run_backtest.py --csv data/my.csv  # 指定手动数据文件

数据获取顺序：本地缓存 data/spy_daily.csv → yfinance（走 HTTPS_PROXY 代理）
→ 手动 CSV 兜底。首次联网成功后自动写缓存，之后完全离线可复现。

产出（--out 目录，默认 experiments/m1a）：
    equity_curve.png  策略净值 vs 买并持有 SPY（+ 回撤副图）
    states.csv        每日账户状态    trades.csv  每笔交易
    prices.csv        实际使用的真实价格序列（审计用）    summary.txt 概要
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 无窗口环境直接出图
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from sellput.config import BacktestConfig, DataConfig, HybridConfig  # noqa: E402
from sellput.instruments import NyseCalendar  # noqa: E402
from sellput.market_data import build_provider  # noqa: E402
from sellput.sim import SimulationEngine  # noqa: E402

# Windows 控制台默认 GBK，强制 UTF-8 输出中文报告
sys.stdout.reconfigure(encoding="utf-8")
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

REASON_ZH = {
    "entry": "新开",
    "take_profit": "止盈50%",
    "dte_exit": "DTE≤3强退",
    "expire": "到期作废",
    "assign": "到期指派",
    "assign_cash": "现金结算",
    "force_liquidation": "强平",
}


def _date(s: str) -> date:
    return date.fromisoformat(s)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="M1-A Sell Put 回测（真实 SPY 日线 + 合成期权链）")
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--start", type=_date, default=date(2005, 1, 1))
    ap.add_argument("--end", type=_date, default=date(2024, 12, 31))
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="experiments/m1a")
    ap.add_argument("--offline", action="store_true", help="禁用网络（只用缓存/手动 CSV）")
    ap.add_argument("--csv", default=None, help="手动数据文件路径（兜底）")
    return ap.parse_args(argv)


def pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def plot_equity(states, symbol: str, start: date, end: date, start_cash: float, path: Path) -> None:
    dates = pd.to_datetime([s.date for s in states])
    eq = np.array([s.equity for s in states]) / start_cash
    bench = np.array([s.benchmark_price for s in states]) / states[0].benchmark_price
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    ax1.plot(dates, eq, label="策略净值（Sell Put）", lw=1.6, color="#1f77b4")
    ax1.plot(dates, bench, label="买并持有 SPY", lw=1.2, alpha=0.75, color="#ff7f0e")
    ax1.axhline(1.0, color="gray", lw=0.8, ls="--")
    ax1.set_ylabel("净值（起始 = 1.0）")
    ax1.set_title(
        f"Sell Put 回测收益曲线（{symbol} {start} ~ {end}，标的真实 / 期权合成报价）"
    )
    ax1.legend(loc="upper left")
    ax1.grid(alpha=0.3)
    dd = eq / np.maximum.accumulate(eq) - 1.0
    ax2.fill_between(dates, dd, 0.0, color="#d62728", alpha=0.35)
    ax2.set_ylabel("策略回撤")
    ax2.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = BacktestConfig(
        run={"name": "m1a", "seed": args.seed},
        data=DataConfig(
            provider="hybrid",
            symbol=args.symbol,
            start=args.start,
            end=args.end,
            hybrid=HybridConfig(csv_path=args.csv, offline=args.offline),
        ),
    )
    provider = build_provider(cfg)
    engine = SimulationEngine(cfg)
    result = engine.run(provider, NyseCalendar())
    states, trades = result.states, result.trades
    start_cash = cfg.account.starting_cash
    final_equity = states[-1].equity
    total_return = final_equity / start_cash - 1.0
    bench = states[-1].benchmark_price / states[0].benchmark_price - 1.0
    eq = pd.Series([s.equity for s in states])
    max_dd = (eq / eq.cummax() - 1.0).min()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print(f"Sell Put 回测报告（M1-A · 标的 {args.symbol} 真实日线 / 期权合成报价）")
    print("=" * 78)
    print(f"区间: {args.start} → {args.end}   交易日: {len(states)}   "
          f"引擎耗时: {result.duration_seconds:.2f}s")
    n_dates = len(provider._dates)
    print(f"数据来源: {provider._dates[0]} ~ {provider._dates[-1]}（{n_dates} 个交易日）")
    print("-" * 78)
    print(f"起始资金:            {start_cash:>14,.2f}")
    print(f"期末净值 (equity):   {final_equity:>14,.2f}")
    print(f"策略总收益:          {pct(total_return):>14}")
    print(f"买并持有基准:        {pct(bench):>14}")
    print(f"超额收益:            {pct(total_return - bench):>14}")
    print(f"最大回撤:            {pct(max_dd):>14}")
    if trades:
        wins = [t for t in trades if t.pnl > 0]
        total_pnl = sum(t.pnl for t in trades)
        hold = [(t.exit_date - t.entry_date).days for t in trades]
        by_reason = pd.Series([t.exit_reason for t in trades]).value_counts()
        print(f"交易笔数:            {len(trades):>14}   (胜率 {len(wins) / len(trades):.1%})")
        print(f"已实现 PnL 合计:     {total_pnl:>14,.2f}")
        print(f"平均持有天数:        {sum(hold) / len(hold):>14.1f}")
        print("离场原因分布: " + ", ".join(
            f"{REASON_ZH.get(k, k)}={v}" for k, v in by_reason.items()
        ))
    else:
        print("（无成交）")
    print("=" * 78)

    if trades:
        print("\n交易明细（前 10 笔）:")
        rows = [
            {
                "id": t.id,
                "开仓日": t.entry_date,
                "平仓日": t.exit_date,
                "到期": t.spec.expiry,
                "行权价": t.spec.strike,
                "张数": t.qty,
                "开仓价": round(t.entry_price, 2),
                "平仓价": round(t.exit_price, 2),
                "PnL": round(t.pnl, 2),
                "离场": REASON_ZH.get(t.exit_reason, t.exit_reason),
                "入场DTE": t.dte_at_entry,
                "入场Δ": round(t.delta_at_entry, 3),
                "入场IV": round(t.entry_iv, 4),
            }
            for t in trades[:10]
        ]
        print(pd.DataFrame(rows).to_string(index=False))

    # ---- 导出 ----
    pd.DataFrame(
        [
            {
                "date": s.date,
                "equity": s.equity,
                "cash": s.cash,
                "positions_value": s.positions_value,
                "margin_used": s.margin_used,
                "benchmark_close": s.benchmark_price,
                "g_delta": s.greeks["delta"],
                "g_theta": s.greeks["theta"],
                "a_total": s.attribution["total"],
                "a_delta": s.attribution["delta"],
                "a_theta": s.attribution["theta"],
                "a_vega": s.attribution["vega"],
                "a_residual": s.attribution["residual"],
            }
            for s in states
        ]
    ).to_csv(out / "states.csv", index=False)
    pd.DataFrame(
        [
            {
                "id": t.id,
                "strike": t.spec.strike,
                "expiry": t.spec.expiry,
                "qty": t.qty,
                "entry_date": t.entry_date,
                "exit_date": t.exit_date,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl": t.pnl,
                "commissions": t.commissions,
                "exit_reason": t.exit_reason,
                "entry_iv": t.entry_iv,
                "exit_iv": t.exit_iv,
                "dte_at_entry": t.dte_at_entry,
                "delta_at_entry": t.delta_at_entry,
            }
            for t in trades
        ]
    ).to_csv(out / "trades.csv", index=False)
    provider.prices_frame().to_csv(out / "prices.csv", index=False)
    plot_equity(states, args.symbol, args.start, args.end, start_cash, out / "equity_curve.png")
    (out / "summary.txt").write_text(
        f"symbol={args.symbol}\nstart={args.start}\nend={args.end}\nseed={args.seed}\n"
        f"sessions={len(states)}\ntrades={len(trades)}\nduration_s={result.duration_seconds:.2f}\n"
        f"final_equity={final_equity:.2f}\ntotal_return={total_return:.6f}\n"
        f"benchmark_return={bench:.6f}\nmax_drawdown={max_dd:.6f}\n",
        encoding="utf-8",
    )
    print(f"\n已导出: {out}/equity_curve.png, states.csv, trades.csv, prices.csv, summary.txt")
    print("提示: 期权报价仍为合成（波动率=SPY 20 日已实现波动率），数字为研究近似。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
