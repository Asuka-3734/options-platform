"""M1-C 步骤 3：`run_backtest.py` 参数配置化 CLI 测试（Spec v0.7 §15 F1 / §15.4）。

只覆盖"快速失败"路径（不跑回测）：CLI 暴露了 M1-C 参数 flag、错误组合给出明确报错、
越界参数被 Pydantic 校验拦下。真实回测由 AC 手工验证脚本承担。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_backtest.py"

M1C_FLAGS = (
    "--dte",
    "--delta",
    "--tp",
    "--dte-exit",
    "--entry-frequency",
    "--max-open-positions",
)


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


def test_help_exposes_m1c_parameter_flags():
    result = run_cli("--help")
    assert result.returncode == 0
    for flag in M1C_FLAGS:
        assert flag in result.stdout, flag


def test_buy_hold_rejects_sell_put_only_params():
    result = run_cli("--strategy", "buy_hold", "--dte", "45", "--offline")
    assert result.returncode != 0
    assert "仅适用于 --strategy sell_put" in (result.stdout + result.stderr)


@pytest.mark.parametrize("bad_arg", [["--delta", "0.6"], ["--tp", "1.5"]])
def test_out_of_range_params_are_rejected_before_running(bad_arg):
    result = run_cli(*bad_arg, "--offline")
    assert result.returncode != 0
    assert "参数校验失败" in (result.stdout + result.stderr)
