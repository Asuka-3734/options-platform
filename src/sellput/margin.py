"""保证金模型（Spec D11 / 附录 A）。

命名与声明（Spec D11 已确认）：Simplified Reg-T-style Margin Model 是研究用简化近似
（ETF 20% / 宽基指数 15%），**不是对任何券商 Reg-T / broker-specific margin 的完整复现**；
代码文档与报告必须标注 "research approximation"。
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class MarginModel(ABC):
    name: str

    @abstractmethod
    def option_requirement(self, *, kind: str, S: float, K: float, premium: float) -> float:
        """单张合约的保证金需求（M0 只覆盖空头 Put；多头无需保证金）。"""

    def equity_requirement(self, *, value: float) -> float:
        """股票持仓保证金（简化初始保证金 50%）。"""
        return 0.50 * value


class SimplifiedRegTMargin(MarginModel):
    """Simplified Reg-T-style（Spec 附录 A.1）—— research approximation。

    Requirement/share = max(base×S − OTM + Premium, 0.10×K + Premium)
    base：宽基指数 15%，股票/ETF 20%。
    """

    name = "simplified_regt"

    def __init__(
        self,
        *,
        index_base: float = 0.15,
        equity_base: float = 0.20,
        min_strike_frac: float = 0.10,
    ) -> None:
        self.index_base = index_base
        self.equity_base = equity_base
        self.min_strike_frac = min_strike_frac

    def option_requirement(self, *, kind: str, S: float, K: float, premium: float) -> float:
        base = self.index_base if kind == "index" else self.equity_base
        otm = max(S - K, 0.0)  # 空头 Put 的价外金额：S > K 时为正（Spec 附录 A.1）
        per_share = max(base * S - otm + premium, self.min_strike_frac * K + premium)
        return per_share * 100.0


class CashSecuredMargin(MarginModel):
    """Cash-Secured Put（Spec 附录 A.2）：max(K − Premium, 0) × multiplier。"""

    name = "csp"

    def option_requirement(self, *, kind: str, S: float, K: float, premium: float) -> float:
        return max(K - premium, 0.0) * 100.0

    def equity_requirement(self, *, value: float) -> float:
        return 0.0  # 全额现金担保持仓


def build_margin_model(name: str) -> MarginModel:
    if name == "simplified_regt":
        return SimplifiedRegTMargin()
    if name == "csp":
        return CashSecuredMargin()
    raise ValueError(f"unknown margin model: {name}")
