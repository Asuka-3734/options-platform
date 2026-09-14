"""M1-C 步骤 6：`run_sweep.py` CLI 测试（Spec v0.7 §15 F2 / N4）。

用真实缓存数据的小窗口跑 2 组合，验证 CLI 端到端可用：指标表、基准行（**搜索阶段不显示
test/full**）、落盘产物与提示语。数据缓存缺失时跳过（gitignore）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_sweep.py"
CACHE = ROOT / "data" / "spy_daily.csv"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        check=False,
    )


@pytest.mark.skipif(not CACHE.exists(), reason="data/spy_daily.csv 缺失（gitignore）")
def test_sweep_cli_runs_small_grid(tmp_path: Path):
    out = tmp_path / "sweep-cli"
    result = run_cli(
        "--dte", "20,30",
        "--delta", "0.20",
        "--tp", "0.50",
        "--start", "2020-01-01",
        "--end", "2020-06-30",
        "--train-end", "2020-04-01",
        "--offline",
        "--workers", "1",
        "--out", str(out),
    )
    assert result.returncode == 0, result.stderr
    stdout = result.stdout
    assert "M1-C 参数 sweep" in stdout
    assert "2 个组合" in stdout
    # 指标表：两行（combo 0/1）
    assert "train_cagr" in stdout
    # 基准行只给 train；test/full 在搜索阶段不产出
    assert "基准行" in stdout
    assert "test / full 未在此阶段计算" in stdout
    # 落盘：sweep 级清单 + 指标 CSV + 两个组合目录
    assert (out / "sweep_manifest.json").exists()
    assert (out / "sweep_metrics.csv").exists()
    combo_dirs = sorted(p.name for p in out.glob("combo-*"))
    assert len(combo_dirs) == 2
    assert (out / combo_dirs[0] / "manifest.json").exists()


@pytest.mark.skipif(not CACHE.exists(), reason="data/spy_daily.csv 缺失（gitignore）")
def test_sweep_cli_help_lists_m1c_flags(tmp_path: Path):
    result = run_cli("--help")
    assert result.returncode == 0
    for flag in ("--dte", "--delta", "--tp", "--train-end", "--workers", "--save-all",
                 "--max-mdd", "--sort-by"):
        assert flag in result.stdout, flag


def _small_grid_args(tmp_path: Path, *extra: str) -> tuple[str, ...]:
    return (
        "--dte", "20,30",
        "--delta", "0.20",
        "--tp", "0.50",
        "--start", "2020-01-01",
        "--end", "2020-06-30",
        "--train-end", "2020-04-01",
        "--offline",
        "--workers", "1",
        "--out", str(tmp_path / "constrained"),
        *extra,
    )


@pytest.mark.skipif(not CACHE.exists(), reason="data/spy_daily.csv 缺失（gitignore）")
def test_sweep_cli_reports_no_solution_explicitly(tmp_path: Path):
    """AC-6：阈值无法满足时显式输出"无可行解"，不打印任何"最佳参数"。"""
    result = run_cli(*_small_grid_args(tmp_path, "--max-mdd", "0.0001"))
    assert result.returncode == 0, result.stderr
    stdout = result.stdout
    assert "无可行解" in stdout
    assert "不会自动放宽阈值" in stdout
    assert "最接近约束 ≠ 满足约束" in stdout
    assert "不等于未来最优参数" in stdout  # §17.2 无预测原则
    # 无可行解时不得出现"可行组合："这一行
    assert "可行组合：" not in stdout


@pytest.mark.skipif(not CACHE.exists(), reason="data/spy_daily.csv 缺失（gitignore）")
def test_sweep_cli_ranks_feasible_combos_under_constraint(tmp_path: Path):
    result = run_cli(*_small_grid_args(tmp_path, "--max-mdd", "0.99", "--sort-by", "cagr"))
    assert result.returncode == 0, result.stderr
    stdout = result.stdout
    assert "约束筛选（train 段）" in stdout
    assert "train 最大回撤 ≥ −99.00%" in stdout
    assert "可行组合：" in stdout
    assert "不等于未来最优参数" in stdout


@pytest.mark.skipif(not CACHE.exists(), reason="data/spy_daily.csv 缺失（gitignore）")
def test_sweep_cli_rejects_percent_style_threshold(tmp_path: Path):
    """`--max-mdd 30` 这类百分数写法必须报错（避免 30 被当成 3000% 的静默误解）。"""
    result = run_cli(*_small_grid_args(tmp_path, "--max-mdd", "30"))
    assert result.returncode != 0
    assert "小数" in (result.stdout + result.stderr)
