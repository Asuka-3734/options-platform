"""M1-C：网格参数 sweep（Spec §15 F2 / N4；AC-1/AC-2/AC-7/AC-13）。

按 DTE × Delta × Take Profit 的组合逐个跑独立回测，输出统一格式的指标表与实验清单。

**搜索纪律（Spec §15 F5 / D14）**：sweep **只看 train 段**——实现上每个组合都不计算
test 指标（`test_eval_count_total = 0`）；样本外评估只对最终选中的那一组参数单独做。

**保存粒度（Spec §15 N4）**：默认 metrics-first（每组合 manifest/metrics + sweep 级
清单与指标 CSV）；`--save-all` 才写全量 states/trades/prices。

用法（在 options-platform 目录下）：
    # AC-1：3×3 = 9 个组合
    uv run python scripts/run_sweep.py --dte 20,30,45 --delta 0.10,0.20,0.30 --tp 0.50 \\
        --start 2005-01-01 --end 2024-12-31 --train-end 2018-12-31 --offline

    # AC-2：4×5×4 = 80 个组合（8 进程）
    uv run python scripts/run_sweep.py --dte 20,30,45,60 --delta 0.10,0.15,0.20,0.25,0.30 \\
        --tp 0.25,0.50,0.75,1.00 --offline --out runs/sweep-official --workers 8
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

from sellput.config import (
    BacktestConfig,
    DataConfig,
    HybridConfig,
    SellPutParams,
    SplitConfig,
    StrategyConfig,
    SweepConfig,
)
from sellput.research import (
    DEFAULT_MAX_WORKERS,
    SELECTION_DISCLAIMER,
    TABLE_METRIC_FIELDS,
    default_sweep_id,
    select_by_constraint,
    sweep,
)

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

DISPLAY_COLUMNS = ("combo", "dte", "delta", "tp", "train_total", "train_cagr",
                   "train_max_dd", "train_win_rate", "train_trades")


def _date(value: str) -> date:
    return date.fromisoformat(value)


def _int_list(value: str) -> list[int]:
    return [int(item) for item in value.replace(" ", "").split(",") if item]


def _float_list(value: str) -> list[float]:
    return [float(item) for item in value.replace(" ", "").split(",") if item]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="M1-C 参数 sweep（只看 train，Spec §15 F2）")
    ap.add_argument("--dte", type=_int_list, default=[], help="DTE 网格，如 20,30,45,60")
    ap.add_argument("--delta", type=_float_list, default=[], help="|delta| 网格，如 0.10,0.15,0.20")
    ap.add_argument(
        "--tp", type=_float_list, default=[], help="止盈比例网格，如 0.25,0.50,0.75,1.0"
    )
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--start", type=_date, default=date(2005, 1, 1))
    ap.add_argument("--end", type=_date, default=date(2024, 12, 31))
    ap.add_argument("--train-end", type=_date, default=date(2018, 12, 31),
                    help="train/test 切分日（Spec §15 F5：train 2005–2018 / test 2019–2024）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--offline", action="store_true", help="禁用网络（只用缓存/手动 CSV）")
    ap.add_argument("--csv", default=None, help="手动数据文件路径（兜底）")
    ap.add_argument("--out", default=None, help="输出目录（默认 runs/<sweep-id>）")
    ap.add_argument("--workers", type=int, default=min(DEFAULT_MAX_WORKERS, 8), help="并行进程数")
    ap.add_argument("--save-all", action="store_true",
                    help="额外保存每组 states/trades/prices（默认 metrics-first，Spec §15 N4）")
    ap.add_argument(
        "--max-mdd",
        type=float,
        default=None,
        help="MDD 约束（填小数，如 0.30 表示最大回撤不超过 30%%）；满足者按 --sort-by 排序",
    )
    ap.add_argument(
        "--sort-by",
        default=None,
        help=f"排序指标（默认 cagr；可选：{', '.join(TABLE_METRIC_FIELDS)}）",
    )
    return ap.parse_args(argv)


def build_config(args: argparse.Namespace) -> BacktestConfig:
    return BacktestConfig(
        run={"name": "m1c-sweep", "seed": args.seed},
        data=DataConfig(
            provider="hybrid",
            symbol=args.symbol,
            start=args.start,
            end=args.end,
            hybrid=HybridConfig(csv_path=args.csv, offline=args.offline),
        ),
        strategy=StrategyConfig(type="sell_put", params=SellPutParams()),
        split=SplitConfig(train_end=args.train_end),
        sweep=SweepConfig(
            dte_target=args.dte, delta_target=args.delta, profit_target_pct=args.tp
        ),
    )


def display_rows(sweep_run) -> list[dict]:
    return display_rows_for(sweep_run.results)


def display_rows_for(results) -> list[dict]:
    """把（可能是筛选后、已排序的）结果列表渲染成表格行。"""
    rows: list[dict] = []
    for result in results:
        train = result.segments.train
        params = result.params
        rows.append(
            {
                "combo": result.combo,
                "dte": params["dte_target"],
                "delta": f"{float(params['delta_target']):.2f}",
                "tp": f"{float(params['profit_target_pct']):.0%}",
                "train_total": f"{train.total_return * 100:+.2f}%",
                "train_cagr": f"{train.cagr * 100:+.2f}%" if train.cagr is not None else "-",
                "train_max_dd": f"{train.max_dd * 100:+.2f}%",
                "train_win_rate": f"{train.win_rate:.1%}" if train.win_rate is not None else "-",
                "train_trades": train.n_trades,
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = build_config(args)
    out_dir = Path(args.out) if args.out else Path("runs") / default_sweep_id()

    n_dte = len(args.dte) or 1
    n_delta = len(args.delta) or 1
    n_tp = len(args.tp) or 1
    print("=" * 96)
    print(f"M1-C 参数 sweep（Spec §15 F2）· 标的 {args.symbol} · {args.start} ~ {args.end}")
    print(f"网格：DTE {args.dte or '[默认 30]'} × delta {args.delta or '[默认 0.20]'} "
          f"× TP {args.tp or '[默认 50%]'} = {n_dte * n_delta * n_tp} 个组合 · {args.workers} 进程")
    print(f"train/test 切分：train ≤ {args.train_end} < test（搜索只看 train，Spec §15 F5 / D14）")
    print(f"输出：{out_dir}")
    print("=" * 96)

    started = time.perf_counter()

    def progress(done: int, total: int) -> None:
        if done == total or done % max(1, total // 10) == 0:
            print(f"  进度 {done}/{total}", flush=True)

    sweep_run = sweep(
        cfg,
        out_dir=out_dir,
        workers=args.workers,
        save_all=args.save_all,
        progress=progress,
    )
    elapsed = time.perf_counter() - started

    sort_by = args.sort_by or "cagr"
    view = None
    if args.max_mdd is None and args.sort_by is None:
        ordered = list(sweep_run.results)  # 网格顺序：便于核对网格完整性
    else:
        try:
            view = select_by_constraint(sweep_run.results, max_dd=args.max_mdd, sort_by=sort_by)
        except ValueError as err:
            print(f"错误：{err}", file=sys.stderr)
            return 2
        ordered = list(view.feasible)

    if view is not None and args.max_mdd is not None:
        print()
        print("-" * 96)
        print(f"约束筛选（train 段）· 约束：{view.constraint_text()} · 排序：{sort_by} 降序")
        print("-" * 96)
        if not view.has_solution:
            print(view.no_solution_message())
            print()
            print(SELECTION_DISCLAIMER)
            _print_outputs(sweep_run, out_dir, args, elapsed)
            return 0
        print(f"可行组合：{len(view.feasible)}/{view.total}（被过滤 {view.n_filtered_out}）")

    frame = pd.DataFrame(
        display_rows_for(ordered), columns=list(DISPLAY_COLUMNS)
    )
    print()
    print(frame.to_string(index=False))
    benchmark = sweep_run.benchmark_returns()
    if benchmark["train"] is not None:
        print()
        print(
            "基准行（Buy & Hold 价格型，各组合相同）："
            f"train {benchmark['train'] * 100:+.2f}%"
            "；test / full 未在此阶段计算（Spec §15 F5：搜索只读 train）"
        )
    if view is not None:
        print()
        print(SELECTION_DISCLAIMER)
    _print_outputs(sweep_run, out_dir, args, elapsed)
    return 0


def _print_outputs(sweep_run, out_dir: Path, args: argparse.Namespace, elapsed: float) -> None:
    print()
    per_combo = elapsed / max(1, sweep_run.n_combos)
    print(f"完成：{sweep_run.n_combos} 个组合，用时 {elapsed:.1f}s（{per_combo:.1f}s/组合）")
    if sweep_run.metrics_csv is not None:
        print(f"指标 CSV：{sweep_run.metrics_csv}（每组合一行，确定性字段）")
    if sweep_run.manifest_path is not None:
        print(
            f"sweep 清单：{sweep_run.manifest_path}"
            "（含网格、数据指纹、git commit、test_eval_count_total=0）"
        )
    artifacts_note = (
        "；--save-all 已附 states/trades/prices"
        if args.save_all
        else "；未附全量制品（--save-all 可开启）"
    )
    print(
        f"每组合产物：{out_dir}/combo-*"
        f"（manifest.json + metrics.json{artifacts_note}）"
    )
    print("提示：本次搜索未计算任何 test 指标；样本外评估只对最终选中的参数做一次。"
          " 数字为 synthetic 期权报价下的研究近似（Spec §17.2）。")


if __name__ == "__main__":
    raise SystemExit(main())
