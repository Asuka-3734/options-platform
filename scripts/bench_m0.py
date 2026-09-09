"""M0 性能 benchmark（Spec §12-9）：10 年日频合成回测耗时，目标 <10s。

用法：uv run python scripts/bench_m0.py
"""

from __future__ import annotations

import time
from datetime import date

from sellput.config import BacktestConfig, DataConfig
from sellput.instruments import NyseCalendar
from sellput.market_data import synthetic_provider_from_config
from sellput.sim import SimulationEngine


def main() -> None:
    cfg = BacktestConfig(
        data=DataConfig(symbol="SPY", start=date(2015, 1, 1), end=date(2024, 12, 31))
    )
    cal = NyseCalendar()
    prov = synthetic_provider_from_config(cfg.data, cfg.market.rate)
    engine = SimulationEngine(cfg)
    t0 = time.perf_counter()
    result = engine.run(prov, cal)
    wall = time.perf_counter() - t0
    print(f"10y synthetic backtest: {len(result.states)} sessions, {len(result.trades)} trades")
    print(f"engine duration: {result.duration_seconds:.2f}s (wall {wall:.2f}s)")
    final = result.states[-1].equity
    print(f"final equity: {final:,.2f} (start {cfg.account.starting_cash:,.0f})")
    target = 10.0
    print(f"target <{target}s: {'PASS' if result.duration_seconds < target else 'MISS'}")


if __name__ == "__main__":
    main()
