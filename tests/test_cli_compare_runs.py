"""M1-C 步骤 5：`compare_runs.py` CLI 测试（Spec v0.7 §15 F8 / AC-8 / D14）。

用三个真实运行的 manifest 验证：合并对比表、train 段默认按 cagr 降序、
**test 段禁止排序（显式传 --sort-by 直接报错）**、test 段打印 D14 警告。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from sellput.research import run_once, write_manifest
from tests.test_research import make_config

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "compare_runs.py"


@pytest.fixture(scope="module")
def run_dirs(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    base = tmp_path_factory.mktemp("m1c_compare")
    directories: list[Path] = []
    for index, dte in enumerate((20, 30, 45), start=1):
        run = run_once(make_config({"dte_target": dte, "delta_target": 0.20}))
        directory = base / f"run-{index}"
        write_manifest(run, directory, run_id=f"exp-{index:03d}")
        directories.append(directory)
    return directories


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )


def test_compare_cli_prints_merged_table(run_dirs: list[Path]):
    result = run_cli("--runs", *[str(d) for d in run_dirs])
    assert result.returncode == 0
    out = result.stdout
    assert "实验对比" in out and "3 个实验" in out
    for run_id in ("exp-001", "exp-002", "exp-003"):
        assert run_id in out
    assert "指标段：train" in out
    assert "sha256:" in out  # 数据指纹列，用于确认是否同一份数据
    assert "test_eval_count" in out


def test_compare_cli_rejects_sorting_on_test(run_dirs: list[Path]):
    """D14：test 段不得排序（显式请求 → 报错退出，不静默忽略）。"""
    result = run_cli(
        "--runs", *[str(d) for d in run_dirs], "--segment", "test", "--sort-by", "cagr"
    )
    assert result.returncode != 0
    assert "test 段禁止排序" in (result.stdout + result.stderr)


def test_compare_cli_test_segment_without_sort_prints_warning(run_dirs: list[Path]):
    result = run_cli("--runs", *[str(d) for d in run_dirs], "--segment", "test")
    assert result.returncode == 0
    assert "样本外" in result.stdout
    assert "禁止排序" in result.stdout


def test_compare_cli_reports_when_no_manifest_found(tmp_path: Path):
    result = run_cli("--runs", str(tmp_path / "does-not-exist"))
    assert result.returncode != 0
    assert "没有可对比的实验" in result.stderr
