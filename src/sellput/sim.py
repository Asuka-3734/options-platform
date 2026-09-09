"""日频事件引擎（Spec §4.2）。

确定性顺序：开盘决策（Close(t−1)）→ 成交（Open(t)）→ 到期/指派（Close(t)）→
盯市 + 归因 → 利息 → 保证金检查 → 快照。
引擎本身无随机性（随机性全部在数据层与 MC 层，Spec §12-7）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .config import BacktestConfig
from .dividend import ContinuousYieldDividendModel, DividendModel, PerDayDividendModel
from .execution import (
    EquitySaleIntent,
    Fill,
    FillModel,
    Order,
    OrderAction,
    OrderIntent,
    OrderReason,
)
from .instruments import (
    OptionId,
    OptionRight,
    OptionStyle,
    Settlement,
    TradingCalendar,
    defaults_for_symbol,
    underlier_kind,
)
from .margin import MarginModel, build_margin_model
from .market_data import MarketDataProvider, Session
from .portfolio import Portfolio, PortfolioState, PositionSnapshot, Trade
from .pricing import BlackScholesEngine, CRRBinomialEngine, PricingEngine, implied_vol
from .strategy import SellPutStrategy, Strategy, StrategyContext


@dataclass(slots=True)
class SimResult:
    config: BacktestConfig
    states: list[PortfolioState]
    trades: list[Trade]
    orders: list[Order]
    duration_seconds: float


def build_pricing_engine(cfg: BacktestConfig) -> PricingEngine:
    style, _settlement = defaults_for_symbol(cfg.data.symbol)
    if cfg.pricing.engine == "auto":
        if style is OptionStyle.AMERICAN:
            return CRRBinomialEngine(cfg.pricing.binomial_steps)
        return BlackScholesEngine()
    if cfg.pricing.engine == "crr":
        return CRRBinomialEngine(cfg.pricing.binomial_steps)
    return BlackScholesEngine()


def build_dividend_model(cfg: BacktestConfig, provider: object) -> DividendModel:
    src = cfg.pricing.dividend.yield_source
    if isinstance(src, float):
        return ContinuousYieldDividendModel(src)
    # auto：Provider 提供按日查询（M1-A 真实分红折算）时用之，否则退化为常数 q
    if hasattr(provider, "dividend_yield_at"):
        return PerDayDividendModel(provider.dividend_yield_at)
    return ContinuousYieldDividendModel(getattr(provider, "q", 0.0))


def build_strategy(cfg: BacktestConfig) -> Strategy:
    if cfg.strategy.type == "sell_put":
        return SellPutStrategy(cfg.strategy.params)
    raise ValueError(f"unknown strategy type: {cfg.strategy.type}")


class SimulationEngine:
    """M0 日频事件引擎（历史回测与未来 MC 共用同一事件语义）。"""

    def __init__(self, config: BacktestConfig) -> None:
        self.cfg = config

    def run(self, provider: MarketDataProvider, calendar: TradingCalendar) -> SimResult:
        cfg = self.cfg
        t_start = time.perf_counter()
        engine = build_pricing_engine(cfg)
        dividend = build_dividend_model(cfg, provider)
        margin = build_margin_model(cfg.simulation.margin_model)
        fill_model = FillModel(
            slippage_bps=cfg.simulation.slippage_bps,
            commission_per_contract=cfg.simulation.commission_per_contract,
            commission_per_order=cfg.simulation.commission_per_order,
        )
        strategy = build_strategy(cfg)
        kind = underlier_kind(cfg.data.symbol)
        portfolio = Portfolio(cfg.account.starting_cash)

        sessions = provider.sessions()
        if not sessions:
            raise ValueError("no sessions to simulate")
        states: list[PortfolioState] = []
        orders: list[Order] = []
        pending_equity_sales: list[EquitySaleIntent] = []
        pending_force: list[OrderIntent] = []
        order_seq = 0
        prev_close = provider.close_snapshot(calendar.prev_session(sessions[0]))
        prev_iv: dict[OptionId, float] = {}
        margin_blocked = False

        for t in sessions:
            # (a) 昨日挂起的股票卖出（指派 sell_next_open）—— 优先于策略。
            # 开盘链懒获取：仅在需要成交价（股票卖出/期权下单）时构建，节约无交易日的成本。
            pending_sales = list(pending_equity_sales)
            if pending_sales:
                open_snap = provider.open_snapshot(t)
                for sale in pending_sales:
                    price = fill_model.equity_price(open_snap, is_buy=False)
                    portfolio.sell_equity(
                        symbol=sale.symbol,
                        shares=sale.shares,
                        price=price,
                        day=t,
                        commission=fill_model.order_commission(),
                    )
                pending_equity_sales.clear()

            # (b) 信号：只读 prev_close（= t−1 收盘）；强平日跳过策略防立刻重新开仓
            intents: list[OrderIntent] = []
            if not (cfg.simulation.margin_policy == "liquidate" and pending_force):
                if not margin_blocked:
                    ctx = StrategyContext(
                        prev_close=prev_close,
                        portfolio=portfolio,
                        pricing=engine,
                        dividend_model=dividend,
                        calendar=calendar,
                        params=cfg.strategy.params,
                        sizing=cfg.account.sizing,
                        margin_model=margin,
                        underlier_kind=kind,
                    )
                    intents = strategy.on_open(ctx)
            intents = pending_force + intents
            pending_force.clear()

            # (c) 成交（Open(t)）；成交前补全快照：持仓与下单行权价必须可报价
            if intents:
                open_extras = {(p.spec.expiry, p.spec.strike) for p in portfolio.options.values()}
                open_extras |= {(i.option.expiry, i.option.strike) for i in intents}
                open_snap = provider.open_snapshot(t, extra_strikes=open_extras)
            for intent in intents:
                order_seq += 1
                order = Order(id=order_seq, intent=intent, ts=t)
                spec = intent.option
                if intent.action is OrderAction.OPEN and not self._margin_ok(
                    intent, portfolio, margin, kind, open_snap
                ):
                    order.status = "rejected"
                    order.reject_reason = "insufficient_margin"
                    orders.append(order)
                    continue
                is_buy = intent.action is OrderAction.CLOSE  # M0：开=卖、平=买（仅空头 Put）
                price = fill_model.option_price(open_snap, spec, is_buy)
                commission = fill_model.commission(intent.contracts)
                fill = Fill(
                    id=order_seq,
                    order_id=order.id,
                    ts=t,
                    price=price,
                    contracts=intent.contracts,
                    commission=commission,
                    slippage_bps=fill_model.slippage_bps,
                    session=Session.OPEN,
                    kind="option",
                )
                q = open_snap.dividend_yield  # 报价自身的定价参数（反解必须同参数，防下界误报）
                T = (spec.expiry - t).days / 365.0
                iv_entry = implied_vol(
                    price=open_snap.mid(spec.option_id),
                    S=open_snap.underlier.price,
                    K=spec.strike,
                    T=T,
                    r=open_snap.rate,
                    q=q,
                    right=spec.right,
                )
                if intent.action is OrderAction.OPEN:
                    delta_entry = engine.price(
                        S=open_snap.underlier.price,
                        K=spec.strike,
                        T=T,
                        r=open_snap.rate,
                        q=q,
                        sigma=iv_entry,
                        right=spec.right,
                        style=spec.style,
                    ).greeks.delta
                    dte = calendar.dte(t, spec.expiry)
                    open_qty = -intent.contracts if not is_buy else intent.contracts
                    portfolio.open_option(
                        spec=spec,
                        qty=open_qty,
                        price=price,
                        day=t,
                        commission=commission,
                        iv=iv_entry,
                        dte=dte,
                        delta=delta_entry,
                    )
                else:
                    portfolio.close_option(
                        spec=spec,
                        qty=intent.contracts,
                        price=price,
                        day=t,
                        commission=commission,
                        iv=iv_entry,
                        reason=intent.reason.value,
                    )
                order.status = "filled"
                order.fill = fill
                orders.append(order)

            # (d) 到期 / 指派（Close(t)）
            close_snap = provider.close_snapshot(
                t,
                extra_strikes={
                    (p.spec.expiry, p.spec.strike) for p in portfolio.options.values()
                },
            )
            S_close = close_snap.underlier.price
            for oid, pos in list(portfolio.options.items()):
                if pos.spec.expiry != t:
                    continue
                spec = pos.spec
                if spec.right is not OptionRight.PUT:
                    raise NotImplementedError("M0 仅支持空头 Put 的到期处理")
                exit_iv = prev_iv.get(oid, float("nan"))
                if S_close < spec.strike:  # ITM
                    if spec.settlement is Settlement.CASH:
                        portfolio.settle_cash_option(
                            spec=spec, day=t, S_close=S_close, exit_iv=exit_iv
                        )
                    else:
                        assigned_shares = abs(pos.qty) * spec.multiplier
                        portfolio.assign_option(spec=spec, day=t, exit_iv=exit_iv)
                        if cfg.simulation.assignment_policy == "sell_next_open":
                            pending_equity_sales.append(
                                EquitySaleIntent(
                                    symbol=spec.underlying,
                                    shares=assigned_shares,
                                    reason=OrderReason.ASSIGNMENT_LIQUIDATION,
                                )
                            )
                else:
                    portfolio.expire_option(spec=spec, day=t, exit_iv=exit_iv)

            # (e) 盯市 + 归因 + 利息（盯市结果复用于归因与 prev_iv，避免重复定价）
            q_now = dividend.yield_rate(t)
            mark = portfolio.mark_to_market(close_snap, engine, q_now)
            attribution = self._attribution(portfolio, prev_close, close_snap, mark, prev_iv)
            prev_iv = {oid: pm.iv for oid, pm in mark.per_position.items()}
            if cfg.simulation.interest_on_cash > 0.0 and portfolio.cash > 0:
                portfolio.cash += portfolio.cash * cfg.simulation.interest_on_cash / 252.0

            # (f) 保证金检查
            margin_used = portfolio.margin_used(margin, kind, close_snap.underlier.price)
            if cfg.simulation.margin_policy == "reject":
                margin_blocked = margin_used > portfolio.cash
            elif margin_used > portfolio.cash and not pending_force:
                for pos2 in portfolio.options.values():
                    pending_force.append(
                        OrderIntent(
                            OrderAction.CLOSE,
                            pos2.spec,
                            abs(pos2.qty),
                            reason=OrderReason.FORCE_LIQUIDATION,
                        )
                    )

            # (g) 快照
            snap_positions = tuple(
                PositionSnapshot(
                    spec=p.spec,
                    qty=p.qty,
                    avg_price=p.avg_price,
                    mark_price=close_snap.mid(oid),
                    unrealized_pnl=p.unrealized_pnl,
                )
                for oid, p in portfolio.options.items()
            )
            states.append(
                PortfolioState(
                    date=t,
                    cash=portfolio.cash,
                    positions=snap_positions,
                    positions_value=portfolio.positions_value(),
                    margin_used=margin_used,
                    equity=portfolio.equity(),
                    greeks=dict(mark.greeks),
                    attribution=attribution,
                    benchmark_price=close_snap.underlier.price,
                )
            )
            prev_close = close_snap

        return SimResult(
            config=cfg,
            states=states,
            trades=list(portfolio.trades),
            orders=orders,
            duration_seconds=time.perf_counter() - t_start,
        )

    # ---- 内部 ----

    def _margin_ok(
        self,
        intent: OrderIntent,
        portfolio: Portfolio,
        margin: MarginModel,
        kind: str,
        snap,
    ) -> bool:
        req_per = margin.option_requirement(
            kind=kind,
            S=snap.underlier.price,
            K=intent.option.strike,
            premium=snap.mid(intent.option.option_id),
        )
        used = portfolio.margin_used(margin, kind, snap.underlier.price)
        return portfolio.cash - used >= req_per * intent.contracts

    def _attribution(
        self,
        portfolio: Portfolio,
        prev,
        close,
        mark,
        prev_iv: dict[OptionId, float],
    ) -> dict[str, float]:
        """逐腿近似归因（Spec 附录 B）。当日新开仓不参与分解。

        复用 mark_to_market 的每腿 IV 与 Greeks，不重复定价。
        """
        out = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0,
               "residual": 0.0, "total": 0.0}
        S_now, S_prev = close.underlier.price, prev.underlier.price
        dS = S_now - S_prev
        dt = 1.0 / 252.0
        for oid, pos in portfolio.options.items():
            prev_quote = prev.option_quotes.get(oid)
            if prev_quote is None:
                continue  # 当日新开仓：不参与逐日归因（其 PnL 从明日开始分解）
            units = pos.qty * pos.spec.multiplier
            actual = (close.mid(oid) - prev_quote.mid) * units
            pm = mark.per_position.get(oid)
            iv_prev = prev_iv.get(oid)
            if pm is None or iv_prev is None:
                out["residual"] += actual
                out["total"] += actual
                continue
            g = pm.greeks
            d_iv = pm.iv - iv_prev
            approx = (g.delta * dS + 0.5 * g.gamma * dS**2 + g.theta * dt + g.vega * d_iv) * units
            out["delta"] += g.delta * dS * units
            out["gamma"] += 0.5 * g.gamma * dS**2 * units
            out["theta"] += g.theta * dt * units
            out["vega"] += g.vega * d_iv * units
            out["residual"] += actual - approx
            out["total"] += actual
        return out
