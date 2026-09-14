"""M1-C 步骤 6：网格 sweep 测试（Spec v0.7 §15 F2 / N4 / AC-1 / AC-7）。

覆盖：笛卡尔积展开与顺序、9 组合统一格式明细表（AC-1）、**metrics CSV 逐字节复现（AC-7）**、
**结果与进程数无关（AC-7）**、搜索模式不计算 test（F5）、metrics-first 与 `--save-all`（N4）、
组合失败即停并报出上下文。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from sellput.config import (
    BacktestConfig,
    BuyHoldParams,
    DataConfig,
    HybridConfig,
    SellPutParams,
    SplitConfig,
    StrategyConfig,
    SweepConfig,
    SyntheticConfig,
)
from sellput.research import (
    expand_sweep,
    load_manifest,
    sweep,
)

SWEEP_START = date(2020, 1, 1)
SWEEP_END = date(2020, 6, 30)
SWEEP_TRAIN_END = date(2020, 4, 1)


def sweep_config(
    *,
    dte: list[int] | None = None,
    delta: list[float] | None = None,
    tp: list[float] | None = None,
    save_all: bool = False,  # noqa: ARG001 保留参数便于将来扩展
) -> BacktestConfig:
    """小窗口合成数据（SPX + BS 定价）配置：让 sweep 测试保持秒级。"""
    return BacktestConfig(
        data=DataConfig(
            provider="synthetic",
            symbol="SPX",
            start=SWEEP_START,
            end=SWEEP_END,
            synthetic=SyntheticConfig(
                seed=11, s0=3000.0, sigma=0.20, iv_atm=0.20, strikes_per_side=3, strike_span=0.08
            ),
        ),
        strategy=StrategyConfig(type="sell_put", params=SellPutParams()),
        split=SplitConfig(train_end=SWEEP_TRAIN_END),
        sweep=SweepConfig(
            dte_target=dte or [], delta_target=delta or [], profit_target_pct=tp or []
        ),
    )


# ---------------------------------------------------------------------------
# 1. 网格展开
# ---------------------------------------------------------------------------


def test_expand_sweep_is_cartesian_in_declared_order():
    cfg = sweep_config(dte=[20, 30, 45], delta=[0.10, 0.20], tp=[0.50, 1.00])
    configs = expand_sweep(cfg)

    assert len(configs) == 3 * 2 * 2
    combos = [
        (c.strategy.params.dte_target, c.strategy.params.delta_target,
         c.strategy.params.profit_target_pct)
        for c in configs
    ]
    assert combos == [
        (20, 0.10, 0.50), (20, 0.10, 1.00), (20, 0.20, 0.50), (20, 0.20, 1.00),
        (30, 0.10, 0.50), (30, 0.10, 1.00), (30, 0.20, 0.50), (30, 0.20, 1.00),
        (45, 0.10, 0.50), (45, 0.10, 1.00), (45, 0.20, 0.50), (45, 0.20, 1.00),
    ]
    # 基础配置不被修改；单点配置不再带网格
    assert cfg.sweep.dte_target == [20, 30, 45]
    assert all(not c.sweep.dte_target for c in configs)
    # 未扫描的参数沿用基础值
    assert all(c.strategy.params.dte_exit == cfg.strategy.params.dte_exit for c in configs)
    assert all(c.split.train_end == SWEEP_TRAIN_END for c in configs)


def test_expand_sweep_without_grid_returns_single_config():
    configs = expand_sweep(sweep_config())
    assert len(configs) == 1
    assert configs[0].strategy.params.dte_target == 30


def test_expand_sweep_rejects_grid_for_buy_hold():
    cfg = BacktestConfig(
        data=DataConfig(symbol="SPY", start=SWEEP_START, end=SWEEP_END),
        strategy=StrategyConfig(type="buy_hold", params=BuyHoldParams()),
        sweep=SweepConfig(dte_target=[20, 30]),
    )
    with pytest.raises(ValueError, match="仅适用于 sell_put"):
        expand_sweep(cfg)
    # 空网格的 buy_hold 正常运行
    cfg_plain = cfg.model_copy(update={"sweep": SweepConfig()}, deep=True)
    assert len(expand_sweep(cfg_plain)) == 1


# ---------------------------------------------------------------------------
# 2. AC-1：9 组合统一格式明细表
# ---------------------------------------------------------------------------


def test_sweep_nine_combos_produce_uniform_metrics_table(tmp_path: Path):
    out = tmp_path / "sweep-ac1"
    run = sweep(sweep_config(dte=[20, 30, 45], delta=[0.10, 0.20, 0.30], tp=[0.50]),
                out_dir=out, workers=1)

    assert run.n_combos == 9
    assert {r.params["dte_target"] for r in run.results} == {20, 30, 45}
    for result in run.results:
        train = result.segments.train
        assert train is not None
        assert train.n_sessions > 0
        # 统一格式：Return / CAGR / MDD / 胜率 必须在
        assert isinstance(train.total_return, float)
        assert train.cagr is not None and train.max_dd <= 0.0
        assert train.win_rate is None or 0.0 <= train.win_rate <= 1.0

    assert run.metrics_csv is not None and run.metrics_csv.exists()
    lines = run.metrics_csv.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 10  # 表头 + 9 行
    header = lines[0].split(",")
    for column in ("combo", "dte_target", "delta_target", "profit_target_pct",
                   "train_total_return", "train_cagr", "train_max_dd", "train_win_rate",
                   "dataset_fingerprint"):
        assert column in header, column
    # 搜索阶段不得输出任何含样本外的列（test 段 / full 窗口，Spec §15 F5 / D14）
    assert not [c for c in header if c.startswith(("test_", "full_"))]

    # 基准行数据可用（各组合相同）
    benchmark = run.benchmark_returns()
    assert benchmark["full"] is not None and benchmark["train"] is not None
    assert benchmark["test"] is None  # 搜索模式不计算 test


# ---------------------------------------------------------------------------
# 3. AC-7：复现性（逐字节一致 + 与进程数无关）
# ---------------------------------------------------------------------------


def test_sweep_metrics_csv_is_byte_identical_across_runs(tmp_path: Path):
    cfg = sweep_config(dte=[20, 30], delta=[0.10, 0.20], tp=[0.50])
    first = sweep(cfg, out_dir=tmp_path / "run-a", workers=1)
    second = sweep(cfg, out_dir=tmp_path / "run-b", workers=1)

    assert first.metrics_csv is not None and second.metrics_csv is not None
    assert first.metrics_csv.read_bytes() == second.metrics_csv.read_bytes()

    # 每组合 manifest 的确定性字段也一致（created_at 允许不同，Spec §15 N2）
    for result_a, result_b in zip(first.results, second.results, strict=True):
        manifest_a = load_manifest(tmp_path / "run-a" / str(result_a.run_dir))
        manifest_b = load_manifest(tmp_path / "run-b" / str(result_b.run_dir))
        for key in ("config", "metrics", "dataset_fingerprint", "params", "git_commit",
                    "test_eval_count"):
            assert manifest_a[key] == manifest_b[key], key


def test_sweep_results_do_not_depend_on_worker_count(tmp_path: Path):
    cfg = sweep_config(dte=[20, 45], delta=[0.10, 0.25], tp=[0.50])
    serial = sweep(cfg, out_dir=tmp_path / "serial", workers=1)
    parallel = sweep(cfg, out_dir=tmp_path / "parallel", workers=2)

    assert serial.metrics_csv is not None and parallel.metrics_csv is not None
    assert serial.metrics_csv.read_bytes() == parallel.metrics_csv.read_bytes()
    assert [r.params for r in serial.results] == [r.params for r in parallel.results]


# ---------------------------------------------------------------------------
# 4. 搜索纪律与保存粒度
# ---------------------------------------------------------------------------


def test_sweep_never_computes_test_metrics(tmp_path: Path):
    out = tmp_path / "search-only"
    run = sweep(sweep_config(dte=[20, 30], delta=[0.10, 0.20], tp=[0.50]), out_dir=out, workers=1)

    for result in run.results:
        assert result.segments.test is None
        assert result.segments.train is not None
        manifest = load_manifest(out / str(result.run_dir))
        assert manifest["test_eval_count"] == 0
        assert manifest["metrics"]["test"] is None
        # full 窗口含 test 段 → 搜索阶段的清单里也不得出现（Spec §15 F5 / D14）
        assert manifest["metrics"]["full"] is None
        assert manifest["metrics"]["train"]["total_return"] is not None

    sweep_manifest = json.loads((out / "sweep_manifest.json").read_text(encoding="utf-8"))
    assert sweep_manifest["search_mode"] is True
    assert sweep_manifest["test_eval_count_total"] == 0
    assert sweep_manifest["n_combos"] == 4
    assert sweep_manifest["grid"]["dte_target"] == [20, 30]
    assert len(sweep_manifest["combos"]) == 4
    # 指标 CSV 不含任何 test_* 列（搜索阶段不产出样本外数字）
    header = run.metrics_csv.read_text(encoding="utf-8").splitlines()[0]
    assert "test_" not in header


def test_sweep_default_is_metrics_first_and_save_all_adds_artifacts(tmp_path: Path):
    cfg = sweep_config(dte=[20], delta=[0.20], tp=[0.50])
    light = sweep(cfg, out_dir=tmp_path / "light", workers=1)
    heavy = sweep(cfg, out_dir=tmp_path / "heavy", workers=1, save_all=True)

    light_dir = tmp_path / "light" / str(light.results[0].run_dir)
    heavy_dir = tmp_path / "heavy" / str(heavy.results[0].run_dir)

    assert (light_dir / "manifest.json").exists()
    assert (light_dir / "metrics.json").exists()
    assert not (light_dir / "states.csv").exists()  # 默认不写全量制品

    for name in ("states.csv", "trades.csv"):
        assert (heavy_dir / name).exists(), name
    states_header = (heavy_dir / "states.csv").read_text(encoding="utf-8").splitlines()[0]
    assert "equity" in states_header and "benchmark_close" in states_header


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / "data" / "spy_daily.csv").exists(),
    reason="data/spy_daily.csv 缺失（gitignore）",
)
def test_sweep_save_all_exports_prices_when_provider_supports_it(tmp_path: Path):
    """`prices.csv` 仅在 Provider 提供 `prices_frame()` 时写出（hybrid 有、synthetic 无）。"""
    cfg = BacktestConfig(
        data=DataConfig(
            provider="hybrid",
            symbol="SPY",
            start=date(2020, 1, 1),
            end=date(2020, 6, 30),
            hybrid=HybridConfig(offline=True),
        ),
        strategy=StrategyConfig(type="sell_put", params=SellPutParams()),
        split=SplitConfig(train_end=date(2020, 4, 1)),
        sweep=SweepConfig(dte_target=[30], delta_target=[0.20], profit_target_pct=[0.50]),
    )
    run = sweep(cfg, out_dir=tmp_path / "hybrid", workers=1, save_all=True)
    combo_dir = tmp_path / "hybrid" / str(run.results[0].run_dir)
    prices = combo_dir / "prices.csv"
    assert prices.exists()
    assert "Close" in prices.read_text(encoding="utf-8").splitlines()[0]


def test_sweep_fails_fast_with_combo_context(tmp_path: Path):
    """组合执行失败必须中断并报出组合编号与参数（不做静默的部分 sweep）。"""
    cfg = sweep_config(dte=[20, 30], delta=[0.10, 0.20], tp=[0.50])
    bad_data = DataConfig(
        provider="hybrid",
        symbol="SPY",
        start=date(1990, 1, 1),
        end=date(1990, 6, 30),
        hybrid=HybridConfig(offline=True),
    )
    broken = cfg.model_copy(update={"data": bad_data}, deep=True)
    with pytest.raises(RuntimeError, match=r"组合 #0（DTE 20"):
        sweep(broken, out_dir=tmp_path / "broken", workers=1)
