"""M1-C：多实验对比（Spec §15 F8 / AC-8；§15.4）。

把若干实验目录（含 ``manifest.json``）合并成一张对比表，回答"Experiment #001 vs #017 vs #032
有什么不同"。**D14 纪律**：train 段可排序；**test 段禁止排序**（样本外结果不得用于选参数），
违反会直接报错退出。

用法（在 options-platform 目录下）：
    uv run python scripts/compare_runs.py --runs runs/a runs/b runs/c
    uv run python scripts/compare_runs.py --runs "runs/sweep_*"            # 通配符自动展开
    uv run python scripts/compare_runs.py --runs runs/final --segment test # 只看样本外（不排序）
    uv run python scripts/compare_runs.py --runs runs/a --segment full --sort-by max_dd
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from sellput.research import TABLE_METRIC_FIELDS, comparison_table, load_manifest

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

IDENTITY_COLUMNS = ("run_id", "strategy", "params", "train_end", "segment")

#: 显示格式：百分比 / 比率 / 金额 / 整数
_PCT_FIELDS = frozenset(
    {
        "total_return",
        "cagr",
        "ann_vol",
        "win_rate",
        "max_dd",
        "benchmark_price_return",
        "excess_vs_price",
        "excess_vs_total",
    }
)
_MONEY_FIELDS = frozenset({"p5_trade_pnl", "worst_trade_pnl"})
_INT_FIELDS = frozenset({"n_trades", "n_sessions", "dd_duration_days", "test_eval_count"})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="实验对比（Spec §15 F8）：合并多个 manifest 成一张表")
    ap.add_argument(
        "--runs",
        nargs="+",
        required=True,
        help="实验目录（含 manifest.json）或 manifest.json 路径；支持通配符",
    )
    ap.add_argument(
        "--segment",
        choices=["train", "test", "full"],
        default="train",
        help="看哪一段的指标（默认 train；test 段禁止排序，Spec D14）",
    )
    ap.add_argument(
        "--sort-by",
        default=None,
        help=f"按该列降序排序（可选：{', '.join(TABLE_METRIC_FIELDS)}）；test 段会报错",
    )
    return ap.parse_args(argv)


def expand_paths(raw: list[str]) -> list[Path]:
    """展开通配符（Windows 下由本脚本展开，而非依赖 shell）。"""
    out: list[Path] = []
    for item in raw:
        if any(ch in item for ch in "*?["):
            out.extend(sorted(Path().glob(item)))
        else:
            out.append(Path(item))
    return out


def format_cell(field: str, value: object) -> object:
    if value is None:
        return "-"
    if field in _PCT_FIELDS:
        return f"{float(value) * 100:+.2f}%"
    if field in _MONEY_FIELDS:
        return f"{float(value):,.0f}"
    if field in _INT_FIELDS:
        return f"{int(value):,}"
    if isinstance(value, float):
        return f"{value:.3f}"
    return value


def rows_for_display(rows: list[dict]) -> list[dict]:
    return [
        {field: format_cell(field, value) for field, value in row.items()} for row in rows
    ]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = expand_paths(args.runs)
    manifests = []
    for path in paths:
        try:
            manifests.append(load_manifest(path))
        except FileNotFoundError as err:
            print(f"跳过：{err}", file=sys.stderr)
    if not manifests:
        print("没有可对比的实验（未找到 manifest.json）", file=sys.stderr)
        return 2

    # D14：train/full 默认按 cagr 排序；test 段禁止排序（显式传 --sort-by 直接报错）
    sort_by = args.sort_by
    if sort_by is None and args.segment != "test":
        sort_by = "cagr"
    try:
        rows = comparison_table(manifests, segment=args.segment, sort_by=sort_by)
    except ValueError as err:
        print(f"错误：{err}", file=sys.stderr)
        return 2

    print("=" * 100)
    print(f"实验对比（Spec §15 F8）· {len(manifests)} 个实验 · 指标段：{args.segment}")
    if args.segment == "test":
        print(
            "⚠ 样本外（test）段仅用于最终评估：本表禁止排序，不得据 test 结果回头选参数"
            "（Spec §15 F5 / D14）"
        )
    else:
        print(f"排序：{sort_by} 降序（训练段结果，仅表示「在当前模型/数据/区间下训练集内表现」）")
    print("=" * 100)

    columns = [*IDENTITY_COLUMNS, "test_eval_count", "data", "git_commit", *TABLE_METRIC_FIELDS]
    frame = pd.DataFrame(rows_for_display(rows), columns=columns)
    print(frame.to_string(index=False))
    print()
    print("数据指纹列（data）用于确认各实验是否使用同一份数据；完整值见各目录的 manifest.json。")
    if args.segment == "train":
        print("提示：test 段请看 `--segment test`（不排序）；参数选择只能用 train 段。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
