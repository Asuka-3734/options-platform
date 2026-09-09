"""类型化配置（Pydantic v2，Spec §2.3 / §4.4）。

M0 最小可用模型：只包含 M0 实际使用的字段；
sweep / split / monte_carlo 随 M1 / M3 功能引入（Spec §4.1 最小可用原则）。
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RunConfig(BaseModel):
    name: str = "m0-run"
    seed: int = 42


class MarketConfig(BaseModel):
    rate: float = Field(default=0.04, description="无风险利率（年化）")


class DividendConfig(BaseModel):
    model: Literal["continuous_yield"] = "continuous_yield"
    # auto：由 Provider/合成参数提供 q；float：显式给定
    yield_source: float | Literal["auto"] = "auto"


class PricingConfig(BaseModel):
    # auto：SPX→bs / SPY→crr；bs 对美式标的为近似（报告标注）
    engine: Literal["auto", "bs", "crr"] = "auto"
    binomial_steps: int = Field(default=200, ge=2)
    dividend: DividendConfig = DividendConfig()


class SyntheticConfig(BaseModel):
    seed: int = 42
    s0: float = 500.0
    sigma: float = Field(default=0.20, gt=0.0)
    drift: float = 0.0
    iv_atm: float = Field(default=0.20, gt=0.0)
    skew: float = 0.0
    spread_bps: float = 5.0
    q: float = 0.0
    strike_span: float = 0.15
    strikes_per_side: int = 8


class HybridConfig(BaseModel):
    """M1-A：真实标的价格 + 合成期权链。

    - vol_window：已实现波动率的滚动窗口（交易日数），驱动合成链 iv_atm
    - cache_dir / csv_path：本地缓存与手动 CSV 兜底（yfinance 不可达时）
    - offline：True 时不联网（只用缓存/手动 CSV）
    """

    vol_window: int = Field(default=20, ge=5)
    cache_dir: str = "data"
    csv_path: str | None = None  # 手动数据文件；None 时自动扫描 cache_dir 下 *.csv
    buffer_days: int = Field(default=70, ge=25)  # 起点前补数据（滚动波动率/前收）
    offline: bool = False


class DataConfig(BaseModel):
    # M1: yfinance（真实标的日线）；M2: csv / thetadata（真实期权链）
    provider: Literal["synthetic", "hybrid"] = "synthetic"
    symbol: str = "SPY"
    start: date
    end: date
    synthetic: SyntheticConfig = SyntheticConfig()
    hybrid: HybridConfig = HybridConfig()


class SimulationConfig(BaseModel):
    fill: Literal["next_open_mid", "close_mid"] = "next_open_mid"
    slippage_bps: float = 5.0
    commission_per_contract: float = 0.65
    commission_per_order: float = 1.0
    interest_on_cash: float = 0.0
    margin_model: Literal["simplified_regt", "csp"] = "simplified_regt"
    margin_policy: Literal["reject", "liquidate"] = "reject"
    assignment_policy: Literal["sell_next_open", "hold"] = "sell_next_open"
    early_assignment: bool = False  # M0 简化 Bernoulli 模型（默认关，Spec D12）


class SizingConfig(BaseModel):
    mode: Literal["pct_allocated", "fixed_contracts"] = "pct_allocated"
    pct_allocated: float = Field(default=0.5, gt=0.0, le=1.0)
    fixed_contracts: int | None = None
    max_contracts: int | None = None


class AccountConfig(BaseModel):
    starting_cash: float = 100_000.0
    sizing: SizingConfig = SizingConfig()


class SellPutParams(BaseModel):
    """M0 四规则（Spec §5）：DTE 30 / delta 0.20 / 止盈 50% / DTE≤3 强制退出。

    SL / Roll 属 M2，此处刻意不含。
    """

    dte_target: int = 30
    delta_target: float = Field(default=0.20, gt=0.0, lt=0.5)
    # 0 表示关闭止盈（mid 恒 > 0，永不触发）
    profit_target_pct: float = Field(default=0.50, ge=0.0, le=1.0)
    dte_exit: int = 3
    entry_frequency: Literal["weekly", "when_free"] = "weekly"
    max_open_positions: int = 1


class StrategyConfig(BaseModel):
    type: Literal["sell_put"] = "sell_put"
    params: SellPutParams = SellPutParams()


class BacktestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    run: RunConfig = RunConfig()
    market: MarketConfig = MarketConfig()
    pricing: PricingConfig = PricingConfig()
    data: DataConfig
    simulation: SimulationConfig = SimulationConfig()
    account: AccountConfig = AccountConfig()
    strategy: StrategyConfig = StrategyConfig()
