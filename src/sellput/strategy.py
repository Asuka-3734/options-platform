"""策略层（Spec §5）：Strategy ABC + SellPutStrategy（M0 四规则）。"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import ClassVar

import numpy as np
from pydantic import BaseModel
from scipy.special import erf

from .config import SellPutParams, SizingConfig
from .dividend import DividendModel
from .execution import OrderAction, OrderIntent, OrderReason
from .instruments import OptionRight, OptionSpec, TradingCalendar, defaults_for_symbol
from .margin import MarginModel
from .market_data import MarketSnapshot
from .portfolio import Portfolio
from .pricing import PricingEngine, implied_vol


@dataclass(slots=True)
class StrategyContext:
    """信号上下文（Spec §4.2 步骤 1）：只含 t−1 收盘快照与当前组合，禁止 t 日数据。"""

    prev_close: MarketSnapshot
    portfolio: Portfolio
    pricing: PricingEngine
    dividend_model: DividendModel
    calendar: TradingCalendar
    params: SellPutParams
    sizing: SizingConfig
    margin_model: MarginModel
    underlier_kind: str


class Strategy(ABC):
    params_schema: ClassVar[type[BaseModel]] = SellPutParams

    @abstractmethod
    def on_open(self, ctx: StrategyContext) -> list[OrderIntent]: ...


class SellPutStrategy(Strategy):
    """M0 四规则（Spec §5，已确认）：DTE 30 / delta 0.20 / 止盈 50% / DTE≤3 强制退出。

    优先级：DTE≤3 强退 > 止盈 50% > 新开仓。Stop Loss / Roll 属 M2（接口已预留）。
    """

    params_schema = SellPutParams

    def __init__(self, params: SellPutParams) -> None:
        self.params = params
        self._last_entry_week: tuple[int, int] | None = None

    def on_open(self, ctx: StrategyContext) -> list[OrderIntent]:
        intents: list[OrderIntent] = []
        today = ctx.prev_close.date
        S = ctx.prev_close.underlier.price

        # 1) 离场规则（每个决策日检查；优先级：DTE≤3 > 止盈）
        for oid, pos in list(ctx.portfolio.options.items()):
            spec = pos.spec
            if spec.right is not OptionRight.PUT or pos.qty >= 0:
                continue  # M0 只管理空头 Put
            mid = ctx.prev_close.mid(oid)
            dte = ctx.calendar.dte(today, spec.expiry)
            if dte <= self.params.dte_exit:
                intents.append(
                    OrderIntent(OrderAction.CLOSE, spec, abs(pos.qty), reason=OrderReason.DTE_EXIT)
                )
            elif (
                self.params.profit_target_pct > 0.0  # 0 = 关闭止盈
                and mid <= pos.avg_price * (1.0 - self.params.profit_target_pct)
            ):
                intents.append(
                    OrderIntent(
                        OrderAction.CLOSE, spec, abs(pos.qty), reason=OrderReason.TAKE_PROFIT
                    )
                )

        # 2) 开仓规则
        n_open = sum(
            1
            for p in ctx.portfolio.options.values()
            if p.spec.right is OptionRight.PUT and p.qty < 0
        )
        if n_open >= self.params.max_open_positions:
            return intents
        iso_week = (today.isocalendar().year, today.isocalendar().week)
        if self.params.entry_frequency == "weekly" and self._last_entry_week == iso_week:
            return intents
        spec = self._select_contract(ctx, today, S)
        if spec is None:
            return intents
        contracts = self._size(ctx, spec, S)
        if contracts < 1:
            return intents
        intents.append(OrderIntent(OrderAction.OPEN, spec, contracts, reason=OrderReason.ENTRY))
        self._last_entry_week = iso_week
        return intents

    # ---- 内部 ----

    def _select_contract(
        self, ctx: StrategyContext, today: date, S: float
    ) -> OptionSpec | None:
        expiries = sorted({oid[0] for oid in ctx.prev_close.option_quotes})
        if not expiries:
            return None
        target = self.params.dte_target
        expiry = min(expiries, key=lambda e: abs(ctx.calendar.dte(today, e) - target))
        if abs(ctx.calendar.dte(today, expiry) - target) > 14:
            return None  # 链上没有足够接近目标 DTE 的到期日则本轮不开
        style, settlement = defaults_for_symbol(ctx.prev_close.underlier.symbol)
        q = ctx.dividend_model.yield_rate(today)
        r = ctx.prev_close.rate
        puts = [
            (k, qo)
            for (e, k, rt), qo in ctx.prev_close.option_quotes.items()
            if e == expiry and rt is OptionRight.PUT
        ]
        if not puts:
            return None
        ks = np.array([k for k, _ in puts])
        mids = np.array([q.mid for _, q in puts])
        T = (expiry - today).days / 365.0
        # 选价排序：BS 解析 delta 向量化近似（快）；成交时以引擎计算真实 delta 记录
        ivs = np.array(
            [
                implied_vol(price=float(m), S=S, K=float(k), T=T, r=r, q=q, right=OptionRight.PUT)
                for k, m in zip(ks, mids, strict=True)
            ]
        )
        sqT = math.sqrt(T)
        d1 = (np.log(S / ks) + (r - q + 0.5 * ivs**2) * T) / (ivs * sqT)
        # put delta = e^(−qT)·(N(d1) − 1) = −e^(−qT)·N(−d1)；erf 实现
        deltas = -np.exp(-q * T) * 0.5 * (1.0 + erf(-d1 / math.sqrt(2.0)))
        best_k = float(ks[int(np.argmin(np.abs(deltas - (-self.params.delta_target))))])
        return OptionSpec(
            underlying=ctx.prev_close.underlier.symbol,
            expiry=expiry,
            strike=best_k,
            right=OptionRight.PUT,
            style=style,
            settlement=settlement,
        )

    def _size(self, ctx: StrategyContext, spec: OptionSpec, S: float) -> int:
        if ctx.sizing.mode == "fixed_contracts":
            contracts = ctx.sizing.fixed_contracts or 0
        else:
            premium_est = ctx.prev_close.mid(spec.option_id)
            req = ctx.margin_model.option_requirement(
                kind=ctx.underlier_kind, S=S, K=spec.strike, premium=premium_est
            )
            available = ctx.portfolio.cash - ctx.portfolio.margin_used(
                ctx.margin_model, ctx.underlier_kind, S
            )
            contracts = int(available * ctx.sizing.pct_allocated / req) if req > 0 else 0
        if ctx.sizing.max_contracts is not None:
            contracts = min(contracts, ctx.sizing.max_contracts)
        return max(contracts, 0)
