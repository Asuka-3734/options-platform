"""M1-C 步骤 3：配置 schema 测试（Spec v0.7 §15 F1/F5/F10、§15.4）。

覆盖：M1-C 新增三段配置的默认值、Spec 目标形态可解析、`extra="forbid"` 保持、
sweep 取值校验复用 `SellPutParams`（Spec §5）、split/benchmark 语义、JSON 往返（供 manifest）。
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from sellput.config import (
    BacktestConfig,
    BenchmarkConfig,
    SellPutParams,
    SplitConfig,
    SweepConfig,
)


def base_payload() -> dict:
    """M1-C 目标形态配置（Spec §4.4 的 v0.7 版本：provider 用实现支持的取值）。"""
    return {
        "data": {
            "provider": "hybrid",
            "symbol": "SPY",
            "start": "2005-01-01",
            "end": "2024-12-31",
        },
        "strategy": {
            "type": "sell_put",
            "params": {"dte_target": 30, "delta_target": 0.20, "profit_target_pct": 0.50},
        },
        "sweep": {
            "dte_target": [20, 30, 45, 60],
            "delta_target": [0.10, 0.15, 0.20, 0.25, 0.30],
            "profit_target_pct": [0.25, 0.50, 0.75, 1.00],
        },
        "split": {"train_end": "2018-12-31"},
        "benchmark": {"price_return": True, "total_return": True},
    }


# ---------------------------------------------------------------------------
# 默认值与目标形态
# ---------------------------------------------------------------------------


def test_m1c_sections_have_safe_defaults():
    cfg = BacktestConfig(data={"symbol": "SPY", "start": "2005-01-01", "end": "2024-12-31"})
    assert cfg.sweep == SweepConfig()
    assert cfg.sweep.dte_target == []
    assert cfg.sweep.delta_target == []
    assert cfg.sweep.profit_target_pct == []
    assert cfg.split == SplitConfig()
    assert cfg.split.train_end is None  # 不切分 = 只出 full 指标
    assert cfg.benchmark == BenchmarkConfig()
    assert cfg.benchmark.price_return is True
    assert cfg.benchmark.total_return is True


def test_spec_v07_config_shape_parses():
    cfg = BacktestConfig(**base_payload())
    assert cfg.sweep.dte_target == [20, 30, 45, 60]
    assert cfg.sweep.delta_target == [0.10, 0.15, 0.20, 0.25, 0.30]
    assert cfg.sweep.profit_target_pct == [0.25, 0.50, 0.75, 1.00]
    assert cfg.split.train_end == date(2018, 12, 31)
    assert cfg.benchmark.price_return and cfg.benchmark.total_return


def test_config_json_roundtrip_keeps_m1c_sections():
    cfg = BacktestConfig(**base_payload())
    restored = BacktestConfig.model_validate_json(cfg.model_dump_json())
    assert restored == cfg


# ---------------------------------------------------------------------------
# sweep 取值校验（复用 SellPutParams，Spec §5）
# ---------------------------------------------------------------------------


def test_sweep_accepts_valid_values_including_boundaries():
    sweep = SweepConfig(
        dte_target=[20, 30],
        delta_target=[0.01, 0.49],
        profit_target_pct=[0.0, 1.0],
    )
    assert sweep.delta_target == [0.01, 0.49]


@pytest.mark.parametrize("bad", [0.0, 0.5, 0.6, -0.1])
def test_sweep_rejects_out_of_range_delta(bad):
    with pytest.raises(ValidationError):
        SweepConfig(delta_target=[0.20, bad])


@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_sweep_rejects_out_of_range_tp(bad):
    with pytest.raises(ValidationError):
        SweepConfig(profit_target_pct=[0.50, bad])


def test_sweep_validation_is_identical_to_strategy_params():
    """一致性保证：sweep 允许的取值 == 单次回测 SellPutParams 允许的取值。"""
    for value in (0.0, 0.5, 0.6):
        params_rejects = False
        try:
            SellPutParams(delta_target=value)
        except ValidationError:
            params_rejects = True
        sweep_rejects = False
        try:
            SweepConfig(delta_target=[value])
        except ValidationError:
            sweep_rejects = True
        assert params_rejects == sweep_rejects, value


# ---------------------------------------------------------------------------
# split / benchmark
# ---------------------------------------------------------------------------


def test_split_train_end_accepts_iso_string_and_none():
    assert SplitConfig(train_end="2018-12-31").train_end == date(2018, 12, 31)
    assert SplitConfig(train_end=None).train_end is None
    with pytest.raises(ValidationError):
        SplitConfig(train_end="not-a-date")


def test_benchmark_flags_can_be_disabled_independently():
    cfg = BenchmarkConfig(price_return=False, total_return=True)
    assert cfg.price_return is False and cfg.total_return is True


# ---------------------------------------------------------------------------
# 未知字段仍被拒绝（extra="forbid"）
# ---------------------------------------------------------------------------


def test_extra_fields_are_still_forbidden():
    payload = base_payload()
    payload["monte_carlo"] = {"n_paths": 100}  # M2 字段，M1-C 不应被接受
    with pytest.raises(ValidationError):
        BacktestConfig(**payload)

    payload = base_payload()
    payload["sweep"] = {"dte_target": [30], "nonsense": 1}
    with pytest.raises(ValidationError):
        BacktestConfig(**payload)
