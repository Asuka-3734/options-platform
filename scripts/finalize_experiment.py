"""M1-C：最终样本外评估与实验留档（Spec §15.7 第 8/9 步；AC-15）。

对**选定的一组参数**跑一次完整回测，产出 train/test/full 三段指标（含 Price / Total Return
双基准）与全量制品（manifest/metrics/states/trades/prices）。

**纪律**：样本外（test）评估在这条路径上只发生一次（Spec §15 F5 / D14）；
产出目录的 manifest 会记录 `test_eval_count = 1` 作为审计信号。

用法（在 options-platform 目录下）：
    uv run python scripts/finalize_experiment.py --dte 30 --delta 0.20 --tp 0.50 \\
        --start 2005-01-01 --end 2024-12-31 --train-end 2018-12-31 --offline \\
        --out runs/sweep-official/final
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from sellput.config import (
    BacktestConfig,
    DataConfig,
    HybridConfig,
    SellPutParams,
    SplitConfig,
    StrategyConfig,
)
from sellput.research import run_once, write_run_artifacts

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def _date(value: str) -> date:
    return date.fromisoformat(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="M1-C 最终样本外评估（双基准 + 全量制品）")
    ap.add_argument("--dte", type=int, default=30)
    ap.add_argument("--delta", type=float, default=0.20)
    ap.add_argument("--tp", type=float, default=0.50)
    ap.add_argument("--dte-exit", type=int, default=3)
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--start", type=_date, default=date(2005, 1, 1))
    ap.add_argument("--end", type=_date, default=date(2024, 12, 31))
    ap.add_argument("--train-end", type=_date, default=date(2018, 12, 31))
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--out", required=True, help="实验目录（如 runs/sweep-official/final）")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = BacktestConfig(
        run={"name": "m1c-final", "seed": args.seed},
        data=DataConfig(
            provider="hybrid",
            symbol=args.symbol,
            start=args.start,
            end=args.end,
            hybrid=HybridConfig(csv_path=args.csv, offline=args.offline),
        ),
        strategy=StrategyConfig(
            type="sell_put",
            params=SellPutParams(
                dte_target=args.dte,
                delta_target=args.delta,
                profit_target_pct=args.tp,
                dte_exit=args.dte_exit,
            ),
        ),
        split=SplitConfig(train_end=args.train_end),
    )
    print("=" * 96)
    print(f"M1-C 最终样本外评估 · {args.symbol} {args.start} ~ {args.end}")
    print(f"train ≤ {args.train_end}；test 为 2019 起至期末（仅此一次样本外评估）")
    print(
        f"参数：DTE {args.dte} / delta {args.delta:.2f} / 止盈 {args.tp:.0%} / "
        f"DTE≤{args.dte_exit} 强退"
    )
    print("=" * 96)

    run = run_once(cfg, include_total_return_benchmark=True)
    out = Path(args.out)
    manifest_path = write_run_artifacts(run, out)

    seg = run.segments
    rows = [
        ("train", seg.train),
        ("test", seg.test),
        ("full", seg.full),
    ]
    print(f"{'段':<6}{'总收益':>12}{'CAGR':>10}{'年化波动':>12}{'Sharpe':>9}{'最大回撤':>12}"
          f"{'笔数':>7}{'胜率':>9}{'基准(价格)':>13}{'基准(总回报)':>14}")
    for label, metrics in rows:
        if metrics is None:
            print(f"{label:<6}{'—':>12}")
            continue
        cagr = f"{metrics.cagr * 100:+.2f}%" if metrics.cagr is not None else "—"
        vol = f"{metrics.ann_vol * 100:.2f}%" if metrics.ann_vol is not None else "—"
        sharpe = f"{metrics.sharpe:.3f}" if metrics.sharpe is not None else "—"
        win = f"{metrics.win_rate:.1%}" if metrics.win_rate is not None else "—"
        bench_price = (
            f"{metrics.benchmark_price_return * 100:+.2f}%"
            if metrics.benchmark_price_return is not None
            else "—"
        )
        bench_total = (
            f"{metrics.benchmark_total_return * 100:+.2f}%"
            if metrics.benchmark_total_return is not None
            else "—（无分红记录）"
        )
        print(
            f"{label:<6}{metrics.total_return * 100:+11.2f}%{cagr:>10}{vol:>12}{sharpe:>9}"
            f"{metrics.max_dd * 100:+11.2f}%{metrics.n_trades:>7}{win:>9}"
            f"{bench_price:>13}{bench_total:>14}"
        )

    print()
    print(f"实验目录：{out}（manifest.json / metrics.json / states.csv / trades.csv / prices.csv）")
    print(f"manifest：{manifest_path.name}（test_eval_count = 1：本目录包含样本外评估）")
    print("声明：期权报价为合成（Spec §17.2）；总回报基准按除息日分红 ÷ 次一交易日开盘价再投。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
