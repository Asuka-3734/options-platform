"""下单与成交（Spec §4.1 / D9）。

M0：FillModel 以 Open(t) mid ± 滑点成交（买方向 + 滑点，卖方向 − 滑点）；
手续费 = 每单 + 每合约。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from .instruments import OptionSpec
from .market_data import MarketSnapshot, Session


class OrderAction(Enum):
    OPEN = "open"
    CLOSE = "close"


class OrderReason(Enum):
    ENTRY = "entry"
    TAKE_PROFIT = "take_profit"
    DTE_EXIT = "dte_exit"
    ASSIGNMENT_LIQUIDATION = "assignment_liquidation"
    FORCE_LIQUIDATION = "force_liquidation"
    ROLL = "roll"  # M2 预留
    STOP_LOSS = "stop_loss"  # M2 预留


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """策略输出（无副作用）。"""

    action: OrderAction
    option: OptionSpec
    contracts: int
    limit: float | None = None
    reason: OrderReason = OrderReason.ENTRY


@dataclass(frozen=True, slots=True)
class EquitySaleIntent:
    """指派产生的股票次日卖出挂单（Spec §4.2 步骤 1 前优先执行）。"""

    symbol: str
    shares: int
    reason: OrderReason


@dataclass(frozen=True, slots=True)
class Fill:
    id: int
    order_id: int
    ts: date
    price: float
    contracts: int
    commission: float
    slippage_bps: float
    session: Session
    kind: str  # "option" | "equity"


@dataclass(slots=True)
class Order:
    id: int
    intent: OrderIntent
    ts: date
    status: str = "pending"  # pending | filled | rejected
    reject_reason: str | None = None
    fill: Fill | None = None


class FillModel:
    """mid ± 滑点成交 + 手续费（全部可配）。"""

    def __init__(
        self,
        *,
        slippage_bps: float = 5.0,
        commission_per_contract: float = 0.65,
        commission_per_order: float = 1.0,
    ) -> None:
        self.slippage_bps = slippage_bps
        self.commission_per_contract = commission_per_contract
        self.commission_per_order = commission_per_order

    def option_price(self, snapshot: MarketSnapshot, spec: OptionSpec, is_buy: bool) -> float:
        mid = snapshot.mid(spec.option_id)
        s = self.slippage_bps / 1e4
        return mid * (1.0 + s) if is_buy else mid * (1.0 - s)

    def equity_price(self, snapshot: MarketSnapshot, is_buy: bool) -> float:
        s = self.slippage_bps / 1e4
        p = snapshot.underlier.price
        return p * (1.0 + s) if is_buy else p * (1.0 - s)

    def commission(self, contracts: int) -> float:
        return self.commission_per_order + self.commission_per_contract * contracts

    def order_commission(self) -> float:
        return self.commission_per_order
