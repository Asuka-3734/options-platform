"""M1-A 一键回测：真实 SPY 日线 + 合成期权链 → 报告 + CSV + 收益曲线 PNG。

用法（在 options-platform 目录下）：
    uv run python scripts/run_backtest.py                    # 默认 2005-01-01 ~ 2024-12-31
    uv run python scripts/run_backtest.py --start 2015-01-01 --end 2024-12-31
    uv run python scripts/run_backtest.py --offline          # 只用本地缓存/手动 CSV，不联网
    uv run python scripts/run_backtest.py --csv data/my.csv  # 指定手动数据文件
    uv run python scripts/run_backtest.py --strategy buy_hold  # 买入持有策略（M1-B 多策略）
    uv run python scripts/run_backtest.py --dte 45 --delta 0.15 --tp 0.25  # M1-C 策略参数配置化
    uv run python scripts/run_backtest.py --dte-exit 5 --entry-frequency when_free

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
from pydantic import ValidationError  # noqa: E402

from sellput.config import (  # noqa: E402
    BacktestConfig,
    BuyHoldParams,
    DataConfig,
    HybridConfig,
    SellPutParams,
    StrategyConfig,
)
from sellput.instruments import NyseCalendar, OptionSpec  # noqa: E402
from sellput.market_data import build_provider  # noqa: E402
from sellput.sim import SimulationEngine  # noqa: E402

# Windows 控制台默认 GBK，强制 UTF-8 输出中文报告与中文报错
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
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
    "hold_end": "持有到期清仓",
}

STRATEGY_LABEL = {"sell_put": "Sell Put", "buy_hold": "Buy & Hold"}


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
    ap.add_argument(
        "--strategy",
        choices=["sell_put", "buy_hold"],
        default="sell_put",
        help="策略类型（M1-B 多策略：sell_put / buy_hold）",
    )
    sp = ap.add_argument_group("Sell Put 策略参数（M1-C 参数配置化，Spec §15 F1）")
    sp.add_argument("--dte", type=int, default=None, help="开仓目标 DTE（默认 30）")
    sp.add_argument(
        "--delta", type=float, default=None, help="目标 delta 绝对值（默认 0.20；须 0<delta<0.5）"
    )
    sp.add_argument("--tp", type=float, default=None, help="止盈比例（默认 0.50；0 = 关闭止盈）")
    sp.add_argument("--dte-exit", type=int, default=None, help="剩余 DTE ≤ 该值强制退出（默认 3）")
    sp.add_argument(
        "--entry-frequency",
        choices=["weekly", "when_free"],
        default=None,
        help="开仓频率（默认 weekly）",
    )
    sp.add_argument(
        "--max-open-positions", type=int, default=None, help="同时持有的空头 Put 腿数上限（默认 1）"
    )
    return ap.parse_args(argv)


#: CLI flag → SellPutParams 字段（Spec §15 F1 / §15.4）
SELL_PUT_PARAM_FLAGS: dict[str, str] = {
    "dte": "dte_target",
    "delta": "delta_target",
    "tp": "profit_target_pct",
    "dte_exit": "dte_exit",
    "entry_frequency": "entry_frequency",
    "max_open_positions": "max_open_positions",
}


def build_strategy_config(args: argparse.Namespace) -> StrategyConfig:
    """按 CLI 参数构建策略配置（Spec §15 F1）；校验沿用 Pydantic 的 SellPutParams（Spec §5）。"""
    overrides = {
        field: getattr(args, flag)
        for flag, field in SELL_PUT_PARAM_FLAGS.items()
        if getattr(args, flag) is not None
    }
    if args.strategy == "buy_hold":
        if overrides:
            raise SystemExit(
                "参数错误：--dte / --delta / --tp / --dte-exit / --entry-frequency / "
                "--max-open-positions 仅适用于 --strategy sell_put"
            )
        return StrategyConfig(type="buy_hold", params=BuyHoldParams())
    try:
        params = SellPutParams(**overrides)
    except ValidationError as err:
        raise SystemExit(f"参数校验失败（Spec §5 Sell Put 参数约束）：{err}") from err
    return StrategyConfig(type="sell_put", params=params)


def describe_strategy_params(strategy: StrategyConfig) -> str:
    """策略参数的人类可读摘要（写进 stdout 报告头，便于确认"这次跑的是哪组参数"）。"""
    params = strategy.params
    if isinstance(params, SellPutParams):
        return (
            f"DTE {params.dte_target} / delta {params.delta_target:.2f} / "
            f"止盈 {params.profit_target_pct:.0%} / DTE<={params.dte_exit} 强退 / "
            f"{params.entry_frequency} / 最多 {params.max_open_positions} 腿"
        )
    if isinstance(params, BuyHoldParams):
        return f"allocation {params.allocation:.0%}（首日全仓买入、期末清仓、分红不付现）"
    return str(params)


def pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def plot_equity(
    states, symbol: str, start: date, end: date, start_cash: float, path: Path,
    strategy_label: str = "Sell Put",
) -> None:
    dates = pd.to_datetime([s.date for s in states])
    eq = np.array([s.equity for s in states]) / start_cash
    bench = np.array([s.benchmark_price for s in states]) / states[0].benchmark_price
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    ax1.plot(dates, eq, label=f"策略净值（{strategy_label}）", lw=1.6, color="#1f77b4")
    ax1.plot(dates, bench, label="买并持有 SPY", lw=1.2, alpha=0.75, color="#ff7f0e")
    ax1.axhline(1.0, color="gray", lw=0.8, ls="--")
    ax1.set_ylabel("净值（起始 = 1.0）")
    ax1.set_title(
        f"{strategy_label} 回测收益曲线（{symbol} {start} ~ {end}，标的真实 / 期权合成报价）"
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
    strat_label = STRATEGY_LABEL[args.strategy]
    strategy = build_strategy_config(args)
    cfg = BacktestConfig(
        run={"name": "m1b" if args.strategy == "buy_hold" else "m1a", "seed": args.seed},
        data=DataConfig(
            provider="hybrid",
            symbol=args.symbol,
            start=args.start,
            end=args.end,
            hybrid=HybridConfig(csv_path=args.csv, offline=args.offline),
        ),
        strategy=strategy,
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
    print(f"{strat_label} 回测报告（M1-B · 标的 {args.symbol} 真实日线 / 期权合成报价）")
    print(f"策略参数: {describe_strategy_params(strategy)}")
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
                "标的": t.spec.symbol if t.asset_kind == "equity" else t.spec.underlying,
                "到期": t.spec.expiry if isinstance(t.spec, OptionSpec) else "—",
                "行权价": t.spec.strike if isinstance(t.spec, OptionSpec) else "—",
                "张数": t.qty if isinstance(t.spec, OptionSpec) else "—",
                "股数": t.qty if t.asset_kind == "equity" else "—",
                "开仓价": round(t.entry_price, 2),
                "平仓价": round(t.exit_price, 2),
                "PnL": round(t.pnl, 2),
                "离场": REASON_ZH.get(t.exit_reason, t.exit_reason),
                "入场DTE": t.dte_at_entry if t.dte_at_entry is not None else "—",
                "入场Δ": round(t.delta_at_entry, 3) if t.delta_at_entry is not None else "—",
                "入场IV": round(t.entry_iv, 4) if t.entry_iv is not None else "—",
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
                "asset_kind": t.asset_kind,
                "symbol": t.spec.symbol if t.asset_kind == "equity" else t.spec.underlying,
                "strike": t.spec.strike if isinstance(t.spec, OptionSpec) else None,
                "expiry": t.spec.expiry if isinstance(t.spec, OptionSpec) else None,
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
    plot_equity(
        states, args.symbol, args.start, args.end, start_cash, out / "equity_curve.png",
        strategy_label=strat_label,
    )
    (out / "summary.txt").write_text(
        f"symbol={args.symbol}\nstart={args.start}\nend={args.end}\nseed={args.seed}\n"
        f"strategy={args.strategy}\nsessions={len(states)}\ntrades={len(trades)}\n"
        f"duration_s={result.duration_seconds:.2f}\n"
        f"final_equity={final_equity:.2f}\ntotal_return={total_return:.6f}\n"
        f"benchmark_return={bench:.6f}\nmax_drawdown={max_dd:.6f}\n",
        encoding="utf-8",
    )
    print(f"\n已导出: {out}/equity_curve.png, states.csv, trades.csv, prices.csv, summary.txt")
    print("提示: 期权报价仍为合成（波动率=SPY 20 日已实现波动率），数字为研究近似。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
