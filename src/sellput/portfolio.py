"""账户组合与逐日会计（Spec §4.1）。

记账约定（M0，均写入测试）：
- 期权数量 qty 为有符号合约数：多头为正、空头为负。
- avg_price 为每股权利金（正数）。任何成交的现金变动 = −Δqty × price × multiplier。
- unrealized = (mark − avg_price) × qty × multiplier（空头自动为负债方向）。
- FIFO：每笔开仓记录为 LotEntry，平仓/到期/指派从最早 lot 开始配对。
- 实物指派：期权腿以 exit_price=0 退出（权利金全收），行权价与卖出价之差
  记入股票腿 realized（M0 约定，避免重复记账）。
- 现金结算：期权腿以内在价值退出，现金轧差单独扣减。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from .instruments import EquitySpec, OptionId, OptionSpec
from .market_data import MarketSnapshot
from .pricing import Greeks, PricingEngine, implied_vol


@dataclass(slots=True)
class PositionMark:
    """单个期权腿的盯市结果（每股口径 Greeks + IV + mid）。"""

    iv: float
    greeks: Greeks
    mid: float


@dataclass(slots=True)
class MarkResult:
    """组合盯市结果：聚合 Greeks + 每腿明细（供归因复用，避免重复定价）。"""

    greeks: dict[str, float]
    per_position: dict[OptionId, PositionMark]


@dataclass(slots=True)
class LotEntry:
    """开仓批次（FIFO 配对单元）。"""

    qty: int
    price: float
    entry_date: date
    commission: float
    entry_iv: float
    dte_at_entry: int
    delta_at_entry: float


@dataclass(slots=True)
class OptionPosition:
    spec: OptionSpec
    qty: int = 0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    lots: list[LotEntry] = field(default_factory=list)


@dataclass(slots=True)
class EquityPosition:
    symbol: str
    shares: int
    avg_cost: float
    realized_pnl: float = 0.0
    lots: list[EquityLot] = field(default_factory=list)


@dataclass(slots=True)
class EquityLot:
    """股票开仓批次（M1-B：主动买入才有 lot；指派接货不建 lot，卖出不记 Trade）。"""

    shares: int
    price: float
    entry_date: date
    commission: float


@dataclass(frozen=True, slots=True)
class Trade:
    """开平配对（分析单元）。qty 有符号（期权空头为负；股票为股数）。

    M1-B 多策略：asset_kind 标识资产类别（option/equity）；spec 为期权合约或股票；
    期权专属字段（entry_iv/exit_iv/dte_at_entry/delta_at_entry）对股票为 None。
    """

    id: int
    asset_kind: str  # "option" | "equity"
    spec: OptionSpec | EquitySpec
    qty: int
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    pnl: float
    commissions: float
    exit_reason: str  # close | expire | assign | assign_cash | hold_end | ...
    entry_iv: float | None = None
    exit_iv: float | None = None
    dte_at_entry: int | None = None
    delta_at_entry: float | None = None
    model: str = ""
    iv_rank_at_entry: float | None = None  # M1 由 vol 模块填充（Spec §6）


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """持仓快照（M1-B：期权合约或股票；股票 mark=收盘价，avg_price=平均成本）。"""

    spec: OptionSpec | EquitySpec
    qty: int
    avg_price: float
    mark_price: float
    unrealized_pnl: float


@dataclass(frozen=True, slots=True)
class PortfolioState:
    """每日收盘快照（Spec §4.1，不可变）。"""

    date: date
    cash: float
    positions: tuple[PositionSnapshot, ...]
    positions_value: float
    margin_used: float
    equity: float
    greeks: dict[str, float]
    attribution: dict[str, float]
    benchmark_price: float


class Portfolio:
    """账户组合：现金 + 期权仓位（FIFO lots）+ 股票仓位（指派或主动买入，M1-B）。"""

    def __init__(self, starting_cash: float) -> None:
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.options: dict[OptionId, OptionPosition] = {}
        self.equities: dict[str, EquityPosition] = {}
        self.trades: list[Trade] = []
        self._positions_value = 0.0
        self._trade_seq = 0

    # ---- 期权成交 ----

    def open_option(
        self,
        *,
        spec: OptionSpec,
        qty: int,
        price: float,
        day: date,
        commission: float,
        iv: float,
        dte: int,
        delta: float,
    ) -> None:
        """开仓：qty 为有符号数量（空头为负）。现金变动 = −qty × price × mult − commission。"""
        pos = self.options.get(spec.option_id)
        if pos is None:
            pos = OptionPosition(spec=spec)
            self.options[spec.option_id] = pos
        if pos.qty != 0 and (pos.qty > 0) != (qty > 0):
            raise ValueError("M0: 同腿多空双向仓位不支持，请先平仓")
        self.cash -= qty * price * spec.multiplier
        self.cash -= commission
        old_qty = pos.qty
        pos.qty += qty
        pos.avg_price = (pos.avg_price * old_qty + price * qty) / pos.qty
        pos.lots.append(
            LotEntry(
                qty=qty,
                price=price,
                entry_date=day,
                commission=commission,
                entry_iv=iv,
                dte_at_entry=dte,
                delta_at_entry=delta,
            )
        )

    def close_option(
        self,
        *,
        spec: OptionSpec,
        qty: int,
        price: float,
        day: date,
        commission: float,
        iv: float,
        reason: str = "close",
    ) -> float:
        """平仓：qty 为正整数（平仓合约数）。返回实现盈亏（FIFO）。"""
        sign = self._position_sign(spec)  # 必须先于 _exit_lots（满仓平掉后仓位被删除）
        realized = self._exit_lots(
            spec=spec,
            exit_price=price,
            day=day,
            iv=iv,
            reason=reason,
            commission=commission,
            close_qty=qty,
        )
        # 平仓现金流：买入平空头（付钱）/ 卖出平多头（收钱）
        signed = math.copysign(qty, sign)
        self.cash -= -signed * price * spec.multiplier
        self.cash -= commission
        return realized

    # ---- 到期 / 指派 ----

    def expire_option(self, *, spec: OptionSpec, day: date, exit_iv: float) -> float:
        """OTM 到期作废：期权腿以 exit_price=0 退出（权利金已收）。"""
        return self._exit_lots(
            spec=spec,
            exit_price=0.0,
            day=day,
            iv=exit_iv,
            reason="expire",
            commission=0.0,
            close_qty=None,
        )

    def assign_option(self, *, spec: OptionSpec, day: date, exit_iv: float) -> float:
        """空头 Put 实物指派：按行权价接货（产生股票仓位）。

        期权腿以 exit_price=0 退出（权利金全收，M0 记账约定）；
        行权价与卖出价之差记入股票腿 realized。
        """
        pos = self.options.get(spec.option_id)
        if pos is None:
            raise ValueError("no position to assign")
        shares = abs(pos.qty) * spec.multiplier  # 必须先于 _exit_lots（其后仓位被删除）
        realized = self._exit_lots(
            spec=spec,
            exit_price=0.0,
            day=day,
            iv=exit_iv,
            reason="assign",
            commission=0.0,
            close_qty=None,
        )
        self.cash -= shares * spec.strike
        eq = self.equities.get(spec.underlying)
        if eq is None:
            eq = EquityPosition(symbol=spec.underlying, shares=shares, avg_cost=spec.strike)
            self.equities[spec.underlying] = eq
        else:
            total_cost = eq.avg_cost * eq.shares + spec.strike * shares
            eq.shares += shares
            eq.avg_cost = total_cost / eq.shares
        return realized

    def settle_cash_option(
        self, *, spec: OptionSpec, day: date, S_close: float, exit_iv: float
    ) -> float:
        """现金结算（SPX 类，M0 简化：按收盘价，Spec D12）：ITM 空头 Put 支付 K−S。"""
        pos = self.options.get(spec.option_id)
        if pos is None:
            raise ValueError("no position to settle")
        intrinsic = max(spec.strike - S_close, 0.0)
        payment = abs(pos.qty) * intrinsic * spec.multiplier  # 必须先于 _exit_lots
        realized = self._exit_lots(
            spec=spec,
            exit_price=intrinsic,
            day=day,
            iv=exit_iv,
            reason="assign_cash",
            commission=0.0,
            close_qty=None,
        )
        self.cash -= payment
        return realized

    # ---- 股票 ----

    def buy_equity(
        self, *, symbol: str, shares: int, price: float, day: date, commission: float
    ) -> None:
        """买入股票（M1-B）：现金减少，合并 avg_cost，记录买入 lot 供 FIFO 配对。"""
        self.cash -= shares * price
        self.cash -= commission
        eq = self.equities.get(symbol)
        if eq is None:
            eq = EquityPosition(symbol=symbol, shares=shares, avg_cost=price)
            self.equities[symbol] = eq
        else:
            total_cost = eq.avg_cost * eq.shares + price * shares
            eq.shares += shares
            eq.avg_cost = total_cost / eq.shares
        eq.lots.append(
            EquityLot(shares=shares, price=price, entry_date=day, commission=commission)
        )

    def sell_equity(
        self,
        *,
        symbol: str,
        shares: int,
        price: float,
        day: date,
        commission: float,
        reason: str = "close",
    ) -> float:
        """卖出股票：FIFO 配对主动买入的 lot 并记 Trade；无 lot 部分（指派接货）不记。"""
        eq = self.equities.get(symbol)
        if eq is None or eq.shares < shares:
            raise ValueError("insufficient shares")
        remaining = shares
        while remaining > 0 and eq.lots:
            lot = eq.lots[0]
            consumed = min(remaining, lot.shares)
            frac = consumed / shares if shares else 0.0
            lot_commission = commission * frac
            self._record_equity_trade(
                symbol=symbol,
                lot=lot,
                close_shares=consumed,
                exit_price=price,
                exit_date=day,
                exit_reason=reason,
                commission=lot_commission,
            )
            lot.shares -= consumed
            remaining -= consumed
            if lot.shares == 0:
                eq.lots.pop(0)
        realized = (price - eq.avg_cost) * shares - commission
        self.cash += price * shares - commission
        eq.shares -= shares
        eq.realized_pnl += realized
        if eq.shares == 0:
            del self.equities[symbol]
        return realized

    # ---- 盯市 / 保证金 / 不变量 ----

    def mark_to_market(
        self, snapshot: MarketSnapshot, engine: PricingEngine, q: float
    ) -> MarkResult:
        """按收盘快照盯市：更新 unrealized、positions_value；返回组合 Greeks 聚合与每腿明细。"""
        S = snapshot.underlier.price
        greeks = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
        per_position: dict[OptionId, PositionMark] = {}
        total_value = 0.0
        for oid, pos in self.options.items():
            quote = snapshot.option_quotes.get(oid)
            if quote is None:
                raise ValueError(f"missing quote for {oid} on {snapshot.date}")
            T = (pos.spec.expiry - snapshot.date).days / 365.0
            iv = implied_vol(
                price=quote.mid,
                S=S,
                K=pos.spec.strike,
                T=T,
                r=snapshot.rate,
                q=q,
                right=pos.spec.right,
            )
            res = engine.price(
                S=S,
                K=pos.spec.strike,
                T=T,
                r=snapshot.rate,
                q=q,
                sigma=iv,
                right=pos.spec.right,
                style=pos.spec.style,
            )
            pos.unrealized_pnl = (quote.mid - pos.avg_price) * pos.qty * pos.spec.multiplier
            total_value += quote.mid * pos.qty * pos.spec.multiplier
            for g in greeks:
                greeks[g] += getattr(res.greeks, g) * pos.qty * pos.spec.multiplier
            per_position[oid] = PositionMark(iv=iv, greeks=res.greeks, mid=quote.mid)
        for eq in self.equities.values():
            total_value += eq.shares * S
        self._positions_value = total_value
        return MarkResult(greeks=greeks, per_position=per_position)

    def margin_used(self, margin_model, kind: str, S: float) -> float:
        total = 0.0
        for pos in self.options.values():
            if pos.qty < 0:  # 仅空头需要保证金（M0）
                total += margin_model.option_requirement(
                    kind=kind, S=S, K=pos.spec.strike, premium=pos.avg_price
                )
        for eq in self.equities.values():
            total += margin_model.equity_requirement(value=eq.shares * S)
        return total

    def positions_value(self) -> float:
        """自最近一次 mark_to_market 以来的持仓市值。"""
        return self._positions_value

    def equity(self) -> float:
        return self.cash + self._positions_value

    # ---- 内部工具 ----

    def _position_sign(self, spec: OptionSpec) -> int:
        pos = self.options.get(spec.option_id)
        return 1 if (pos is not None and pos.qty > 0) else -1

    def _exit_lots(
        self,
        *,
        spec: OptionSpec,
        exit_price: float,
        day: date,
        iv: float,
        reason: str,
        commission: float,
        close_qty: int | None,
    ) -> float:
        """按 FIFO 配对退出（不改现金；现金变动由调用方按事件语义处理）。"""
        pos = self.options.get(spec.option_id)
        if pos is None:
            raise ValueError(f"no position for {spec.option_id}")
        total = close_qty if close_qty is not None else abs(pos.qty)
        remaining = total
        realized = 0.0
        while remaining > 0 and pos.lots:
            lot = pos.lots[0]
            consumed = min(remaining, abs(lot.qty))
            take = math.copysign(consumed, lot.qty)
            frac = consumed / total if total else 0.0
            lot_commission = commission * frac
            lot_pnl = (exit_price - lot.price) * take * spec.multiplier - lot_commission
            realized += lot_pnl
            self._record_trade(
                spec=spec,
                lot=lot,
                close_qty=take,
                exit_price=exit_price,
                exit_date=day,
                exit_iv=iv,
                exit_reason=reason,
                commission=lot_commission,
            )
            lot.qty -= take
            remaining -= consumed
            if lot.qty == 0:
                pos.lots.pop(0)
        pos.qty -= math.copysign(total, pos.qty)
        pos.realized_pnl += realized
        if pos.qty == 0:
            del self.options[spec.option_id]
        else:
            # 部分平仓后按剩余 lots 重算平均成本（FIFO 语义）
            pos.avg_price = sum(lot.price * lot.qty for lot in pos.lots) / pos.qty
        return realized

    def _record_trade(
        self,
        *,
        spec: OptionSpec,
        lot: LotEntry,
        close_qty: int,
        exit_price: float,
        exit_date: date,
        exit_iv: float,
        exit_reason: str,
        commission: float,
    ) -> None:
        self._trade_seq += 1
        self.trades.append(
            Trade(
                id=self._trade_seq,
                asset_kind="option",
                spec=spec,
                qty=close_qty,
                entry_date=lot.entry_date,
                exit_date=exit_date,
                entry_price=lot.price,
                exit_price=exit_price,
                # 已实现 PnL 扣减开仓与平仓两部分手续费（与现金账守恒：cash = start + Σ pnl）
                pnl=(
                    (exit_price - lot.price) * close_qty * spec.multiplier
                    - lot.commission
                    - commission
                ),
                commissions=lot.commission + commission,
                exit_reason=exit_reason,
                entry_iv=lot.entry_iv,
                exit_iv=exit_iv,
                dte_at_entry=lot.dte_at_entry,
                delta_at_entry=lot.delta_at_entry,
            )
        )

    def _record_equity_trade(
        self,
        *,
        symbol: str,
        lot: EquityLot,
        close_shares: int,
        exit_price: float,
        exit_date: date,
        exit_reason: str,
        commission: float,
    ) -> None:
        """记录股票开平配对（M1-B）：PnL 扣减开仓与平仓两部分手续费（与现金账守恒）。"""
        self._trade_seq += 1
        self.trades.append(
            Trade(
                id=self._trade_seq,
                asset_kind="equity",
                spec=EquitySpec(symbol=symbol),
                qty=close_shares,
                entry_date=lot.entry_date,
                exit_date=exit_date,
                entry_price=lot.price,
                exit_price=exit_price,
                pnl=(exit_price - lot.price) * close_shares - lot.commission - commission,
                commissions=lot.commission + commission,
                exit_reason=exit_reason,
            )
        )
