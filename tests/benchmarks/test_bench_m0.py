"""M0 性能 benchmark（Spec §12-9）。

benchmark 是 benchmark target，不是 correctness 门槛（Spec 已确认）：
默认 pytest 排除（-m "not benchmark"）；显式运行：uv run pytest -m benchmark。
慢机上不会导致功能测试失败（此处阈值 60s，仅为防 O(n³) 级退化）。
"""

from __future__ import annotations

from datetime import date

import pytest

from sellput.config import BacktestConfig, DataConfig
from sellput.instruments import NyseCalendar
from sellput.market_data import synthetic_provider_from_config
from sellput.sim import SimulationEngine


@pytest.mark.benchmark
def test_ten_year_backtest_duration():
    cfg = BacktestConfig(
        data=DataConfig(symbol="SPY", start=date(2015, 1, 1), end=date(2024, 12, 31))
    )
    cal = NyseCalendar()
    prov = synthetic_provider_from_config(cfg.data, cfg.market.rate)
    result = SimulationEngine(cfg).run(prov, cal)
    print(f"\n[bench] 10y backtest: {result.duration_seconds:.2f}s "
          f"({len(result.states)} sessions, {len(result.trades)} trades)")
    assert result.duration_seconds < 60.0  # 目标 <10s（Spec §8）；60s 为防退化阈值
