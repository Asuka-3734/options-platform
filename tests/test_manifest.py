"""M1-C 步骤 5：实验 manifest 与对比表测试（Spec v0.7 §15 F8 / §15.5 / AC-8）。

覆盖：manifest 字段完整性（§15.5 逐字段）、写盘/读回、数据集指纹的稳定性与敏感性、
`test_eval_count` 语义（搜索模式 0 / 最终评估 1）、run_id 与参数标签、对比表合并与
**D14 结构性纪律（test 段禁止排序）**。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from sellput.config import (
    BacktestConfig,
    BuyHoldParams,
    DataConfig,
    StrategyConfig,
)
from sellput.research import (
    MANIFEST_FILENAME,
    METRICS_FILENAME,
    ResearchRun,
    build_manifest,
    comparison_table,
    dataset_fingerprint,
    default_run_id,
    load_manifest,
    params_label,
    run_once,
    write_manifest,
)
from tests.test_research import TRAIN_END, make_config

# Spec v0.7 §15.5 ExperimentManifest 字段（AC-8 要求 manifest 覆盖这些字段）
SPEC_V07_MANIFEST_FIELDS = {
    "config",
    "git_commit",
    "package_version",
    "dataset_fingerprint",
    "train_end",
    "test_eval_count",
    "metrics",
    "created_at",
}


@pytest.fixture(scope="module")
def sample_run() -> ResearchRun:
    """一次合成的 M1-C 运行（SPX + BS 定价，窗口 2020 全年，train_end 2020-07-01）。"""
    return run_once(make_config({"dte_target": 30, "delta_target": 0.20}))


def manifest_stub(run_id: str, *, train_cagr: float | None, test_cagr: float | None = None) -> dict:
    """手工 manifest（只测对比表逻辑时用，避免跑引擎）。"""
    return {
        "run_id": run_id,
        "strategy": "sell_put",
        "params_label": "DTE 30 / delta 0.20 / 止盈 50%",
        "train_end": "2018-12-31",
        "git_commit": "abcdef1234567890",
        "dataset_fingerprint": "sha256:" + "ab12cd34ef56" * 4,
        "created_at": "2026-01-01T00:00:00+00:00",
        "test_eval_count": 1,
        "metrics": {
            "full": {"cagr": train_cagr, "max_dd": -0.5},
            "train": {"cagr": train_cagr, "max_dd": -0.4},
            "test": {"cagr": test_cagr, "max_dd": -0.3} if test_cagr is not None else None,
        },
    }


# ---------------------------------------------------------------------------
# 1. manifest 字段与写盘
# ---------------------------------------------------------------------------


def test_manifest_contains_all_spec_v07_fields(sample_run):
    manifest = build_manifest(sample_run)
    assert SPEC_V07_MANIFEST_FIELDS <= set(manifest)
    assert manifest["train_end"] == TRAIN_END.isoformat()
    assert manifest["metrics"]["train"] is not None
    assert manifest["metrics"]["test"] is not None
    assert manifest["dataset_fingerprint"].startswith("sha256:")
    assert manifest["package_version"]
    assert manifest["created_at"]


def test_manifest_write_and_load_roundtrip(sample_run, tmp_path: Path):
    run_dir = tmp_path / "run-a"
    path = write_manifest(sample_run, run_dir, run_id="exp-001")
    assert path == run_dir / MANIFEST_FILENAME
    assert (run_dir / METRICS_FILENAME).exists()

    from_dir = load_manifest(run_dir)
    from_file = load_manifest(path)
    assert from_dir == from_file
    assert from_dir["run_id"] == "exp-001"
    # metrics.json 与 manifest 内的 metrics 一致
    metrics_json = json.loads((run_dir / METRICS_FILENAME).read_text(encoding="utf-8"))
    assert metrics_json == from_dir["metrics"]


def test_manifest_config_is_fully_expanded(sample_run):
    config = build_manifest(sample_run)["config"]
    # "完整展开配置"：策略参数、simulation、split 等都在里面（可直接重跑）
    assert config["strategy"]["params"]["dte_target"] == 30
    assert config["split"]["train_end"] == TRAIN_END.isoformat()
    assert "slippage_bps" in config["simulation"]
    assert config["data"]["provider"] == "synthetic"
    assert config["account"]["starting_cash"] == 100_000.0


def test_test_eval_count_distinguishes_search_from_final_evaluation(sample_run):
    """搜索模式（不计算 test）→ 0；最终样本外评估 → 1（Spec §15 F5 的审计信号）。"""
    evaluation = build_manifest(sample_run)
    assert evaluation["test_eval_count"] == 1
    assert evaluation["metrics"]["test"] is not None

    search_cfg = make_config({"dte_target": 30, "delta_target": 0.20})
    search_run = run_once(search_cfg, include_test_metrics=False)
    search = build_manifest(search_run)
    assert search["test_eval_count"] == 0
    assert search["metrics"]["test"] is None


def test_dataset_fingerprint_is_stable_and_data_sensitive(sample_run):
    states = sample_run.result.states
    assert dataset_fingerprint(states) == dataset_fingerprint(list(states))  # 稳定

    # 换窗口 → 指纹变化
    other = run_once(make_config({"dte_target": 30, "delta_target": 0.20},
                                 start=date(2020, 1, 1), end=date(2020, 8, 31)))
    assert dataset_fingerprint(other.result.states) != dataset_fingerprint(states)

    # 数据被改动 → 指纹变化
    tampered = list(states)
    tampered[0] = replace(states[0], benchmark_price=states[0].benchmark_price * 1.0000001)
    assert dataset_fingerprint(tampered) != dataset_fingerprint(states)


def test_default_run_id_is_informative_and_filesystem_safe():
    cfg = make_config({"dte_target": 45, "delta_target": 0.15, "profit_target_pct": 0.25})
    run_id = default_run_id(cfg, now=datetime(2026, 1, 2, 3, 4, 5))
    assert run_id == "20260102-030405_spx-dte45-delta15-tp25"
    assert not set(run_id) & set('\\/:*?"<>|')

    bh = BacktestConfig(
        data=DataConfig(symbol="SPY", start=date(2020, 1, 1), end=date(2020, 12, 31)),
        strategy=StrategyConfig(type="buy_hold", params=BuyHoldParams()),
    )
    assert default_run_id(bh, now=datetime(2026, 1, 2, 3, 4, 5)).endswith("_spy-buyhold")


def test_params_label_covers_both_strategies():
    sell_put = make_config({"dte_target": 30, "delta_target": 0.20})
    assert params_label(sell_put) == "DTE 30 / delta 0.20 / 止盈 50%"
    bh = BacktestConfig(
        data=DataConfig(symbol="SPY", start=date(2020, 1, 1), end=date(2020, 12, 31)),
        strategy=StrategyConfig(type="buy_hold", params=BuyHoldParams(allocation=0.5)),
    )
    assert params_label(bh) == "Buy & Hold 50%"


# ---------------------------------------------------------------------------
# 2. 对比表（Spec §15 F8 / AC-8）
# ---------------------------------------------------------------------------


def test_comparison_table_merges_three_runs(sample_run, tmp_path: Path):
    directories = []
    for i, dte in enumerate((20, 30, 45), start=1):
        cfg = make_config({"dte_target": dte, "delta_target": 0.20})
        run = run_once(cfg)
        directory = tmp_path / f"run-{i}"
        write_manifest(run, directory, run_id=f"exp-{i:03d}")
        directories.append(directory)

    manifests = [load_manifest(d) for d in directories]
    rows = comparison_table(manifests, segment="train", sort_by="cagr")

    assert len(rows) == 3
    assert {row["run_id"] for row in rows} == {"exp-001", "exp-002", "exp-003"}
    for row in rows:
        assert row["strategy"] == "sell_put"
        assert row["segment"] == "train"
        assert row["train_end"] == TRAIN_END.isoformat()
        assert row["test_eval_count"] == 1
        assert row["data"].startswith("sha256:")
        assert row["git_commit"] is None or len(row["git_commit"]) == 7
        assert row["cagr"] is not None and row["max_dd"] is not None
    # 排序（cagr 降序）
    cagrs = [row["cagr"] for row in rows]
    assert cagrs == sorted(cagrs, reverse=True)


def test_comparison_table_rejects_sorting_on_test():
    """D14：样本外结果不得用于排序/选参（结构性拒绝，不是文档约定）。"""
    manifests = [manifest_stub("a", train_cagr=0.1, test_cagr=0.2)]
    with pytest.raises(ValueError, match="test 段禁止排序"):
        comparison_table(manifests, segment="test", sort_by="cagr")
    # 不排序时可以看
    rows = comparison_table(manifests, segment="test", sort_by=None)
    assert rows[0]["cagr"] == pytest.approx(0.2)


def test_comparison_table_sorts_desc_with_missing_values_last():
    manifests = [
        manifest_stub("low", train_cagr=0.05),
        manifest_stub("none", train_cagr=None),
        manifest_stub("high", train_cagr=0.10),
    ]
    rows = comparison_table(manifests, segment="train", sort_by="cagr")
    assert [row["run_id"] for row in rows] == ["high", "low", "none"]


def test_comparison_table_handles_search_manifest_without_test_metrics():
    manifest = manifest_stub("search", train_cagr=0.07, test_cagr=None)
    manifest["metrics"]["test"] = None
    manifest["test_eval_count"] = 0
    rows = comparison_table([manifest], segment="test")
    assert len(rows) == 1
    assert rows[0]["cagr"] is None  # 搜索模式没有 test 指标 → 显示为空
    assert rows[0]["test_eval_count"] == 0


def test_comparison_table_validates_arguments():
    with pytest.raises(ValueError, match="unknown segment"):
        comparison_table([], segment="validation")
    with pytest.raises(ValueError, match="sort_by"):
        comparison_table([manifest_stub("a", train_cagr=0.1)], segment="train", sort_by="nope")
