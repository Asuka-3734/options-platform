"""M1-C 步骤 8：研究报告生成测试（Spec v0.7 §15 AC-10 / AC-14 / AC-15）。

用真实缓存数据的小窗口搭建"sweep + 最终评估"，跑 `build_sweep_report.py`，断言报告里
**必须出现**的内容：≥4 个指标热图、约束筛选（含无可行解）、假设清单、效度声明、
月度到期结构声明、实际入场 DTE 分桶、RV 分桶、双基准、test 统计噪声声明。
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from sellput.config import (
    BacktestConfig,
    DataConfig,
    HybridConfig,
    SellPutParams,
    SplitConfig,
    StrategyConfig,
    SweepConfig,
    SyntheticConfig,
)
from sellput.research import run_once, sweep, write_run_artifacts

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_sweep_report.py"
CACHE = ROOT / "data" / "spy_daily.csv"

START, END, TRAIN_END = date(2020, 1, 1), date(2020, 6, 30), date(2020, 4, 1)


def sweep_cfg() -> BacktestConfig:
    return BacktestConfig(
        data=DataConfig(
            provider="synthetic",
            symbol="SPX",
            start=START,
            end=END,
            synthetic=SyntheticConfig(
                seed=11, s0=3000.0, sigma=0.20, iv_atm=0.20, strikes_per_side=3, strike_span=0.08
            ),
        ),
        strategy=StrategyConfig(type="sell_put", params=SellPutParams()),
        split=SplitConfig(train_end=TRAIN_END),
        sweep=SweepConfig(
            dte_target=[20, 30], delta_target=[0.20, 0.30], profit_target_pct=[0.50]
        ),
    )


def final_cfg() -> BacktestConfig:
    """最终评估用 hybrid（真实 SPY 日线 + 分红），从而覆盖 prices.csv 与总回报基准路径。"""
    return BacktestConfig(
        data=DataConfig(
            provider="hybrid",
            symbol="SPY",
            start=START,
            end=END,
            hybrid=HybridConfig(offline=True),
        ),
        strategy=StrategyConfig(
            type="sell_put", params=SellPutParams(dte_target=30, delta_target=0.30)
        ),
        split=SplitConfig(train_end=TRAIN_END),
    )


@pytest.fixture(scope="module")
def report_paths(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    base = tmp_path_factory.mktemp("m1c_report")
    sweep_dir = base / "sweep"
    sweep(sweep_cfg(), out_dir=sweep_dir, workers=1)
    final_dir = sweep_dir / "final"
    run = run_once(final_cfg(), include_total_return_benchmark=True)
    write_run_artifacts(run, final_dir)
    return {
        "sweep": sweep_dir,
        "final": final_dir,
        "html": base / "report.html",
        "md": base / "research_report.md",
        "base": base,
    }


def run_report(paths: dict[str, Path], *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--sweep", str(paths["sweep"]),
            "--final", str(paths["final"]),
            "--out-html", str(paths["html"]),
            "--out-md", str(paths["md"]),
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
    )


@pytest.mark.skipif(not CACHE.exists(), reason="data/spy_daily.csv 缺失（gitignore）")
def test_report_contains_all_ac10_ac14_required_content(report_paths: dict[str, Path]):
    result = run_report(report_paths, "--max-mdd", "0.0001")
    assert result.returncode == 0, result.stderr
    markdown = report_paths["md"].read_text(encoding="utf-8")
    html = report_paths["html"].read_text(encoding="utf-8")

    # AC-10：≥4 个指标的热图（DTE×Delta）
    for metric_title in ("训练段总收益", "训练段 CAGR", "训练段最大回撤", "训练段胜率"):
        assert metric_title in markdown, metric_title
    assert markdown.count("1D 边际") >= 4  # 每个指标都有 DTE/delta 两条边际
    assert "DTE ＼ delta" in markdown
    assert "background:#" in html  # HTML 热图带颜色刻度

    # AC-10：合成链月度到期结构限制声明 + 实际入场 DTE 分桶
    assert "月度到期" in markdown
    assert "实际入场 DTE" in markdown
    assert "RV 分位桶" in markdown and "<25" in markdown and "≥75" in markdown

    # AC-6/AC-10：约束筛选（本 fixture 阈值不可满足 → 必须显式"无可行解"）
    assert "无可行解" in markdown
    assert "最接近约束 ≠ 满足约束" in markdown

    # AC-14：假设清单（§17.1）、窗口、Top-N、敏感性观察、效度声明、test 噪声
    assert "金融假设登记表" in markdown and "synthetic" in markdown
    assert "参数网格" in markdown and "train_end" in markdown
    assert "Top-N" in markdown
    assert "敏感性观察" in markdown
    assert "效度声明" in markdown
    assert "不构成任何未来表现预测" in markdown
    assert "统计噪声" in markdown
    assert "不等于未来最优参数" in markdown  # §17.2 无预测原则

    # AC-15：双基准同时出现
    assert "基准(价格)" in markdown and "基准(总回报)" in markdown
    assert "除息日分红 ÷ 次一交易日开盘价" in markdown


@pytest.mark.skipif(not CACHE.exists(), reason="data/spy_daily.csv 缺失（gitignore）")
def test_report_without_final_run_states_the_gap(report_paths: dict[str, Path], tmp_path: Path):
    """未提供最终评估目录时，报告必须显式说明缺口，而不是静默省略样本外结论。"""
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--sweep", str(report_paths["sweep"]),
            "--out-html", str(tmp_path / "r.html"),
            "--out-md", str(tmp_path / "r.md"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    markdown = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "尚未提供最终评估目录" in markdown
    assert "finalize_experiment.py" in markdown


def test_report_fails_clearly_on_missing_sweep(tmp_path: Path):
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--sweep", str(tmp_path / "nope"),
            "--out-html", str(tmp_path / "r.html"),
            "--out-md", str(tmp_path / "r.md"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    assert result.returncode != 0
    assert "错误" in (result.stdout + result.stderr)
