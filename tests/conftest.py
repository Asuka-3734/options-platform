"""pytest 共享设施：测试日历、确定性数据源、策略测试替身、配置构造。"""

from __future__ import annotations

from datetime import date, timedelta

from sellput.config import BacktestConfig, DataConfig, SimulationConfig
from sellput.execution import OrderIntent
from sellput.instruments import OptionId, OptionRight, TradingCalendar
from sellput.market_data import MarketSnapshot, OptionQuote, Session, UnderlierQuote
from sellput.pricing import BlackScholesEngine


class WeekdayCalendar(TradingCalendar):
    """周一至周五全部为交易日的测试日历（无假日）。"""

    def sessions(self, start: date, end: date) -> list[date]:
        d = start
        out: list[date] = []
        while d <= end:
            if d.weekday() < 5:
                out.append(d)
            d += timedelta(days=1)
        return out

    def is_session(self, day: date) -> bool:
        return day.weekday() < 5


class StaticProvider:
    """手工构造的确定性数据源（金标准 / 规则 / 防前视测试用）。"""

    def __init__(self, calendar: TradingCalendar, rate: float = 0.04, q: float = 0.0) -> None:
        self.calendar = calendar
        self.rate = rate
        self.q = q
        self._snaps: dict[tuple[date, Session], MarketSnapshot] = {}
        self._sessions: list[date] = []

    def set_sessions(self, days: list[date]) -> None:
        self._sessions = list(days)

    def sessions(self) -> list[date]:
        return list(self._sessions)

    def set(
        self,
        day: date,
        session: Session,
        price: float,
        quotes: dict[OptionId, tuple[float, float]] | None = None,
        prev_close: float | None = None,
    ) -> None:
        option_quotes = {
            oid: OptionQuote(bid=b, ask=a, last=0.5 * (b + a), volume=0, open_interest=0)
            for oid, (b, a) in (quotes or {}).items()
        }
        self._snaps[(day, session)] = MarketSnapshot(
            date=day,
            session=session,
            underlier=UnderlierQuote(
                "SPY", price, price if prev_close is None else prev_close
            ),
            option_quotes=option_quotes,
            rate=self.rate,
            dividend_yield=self.q,
        )

    def get(self, day: date, session: Session) -> MarketSnapshot:
        return self._snaps[(day, session)]

    def close_snapshot(self, day: date, extra_strikes=None) -> MarketSnapshot:
        return self._snaps[(day, Session.CLOSE)]

    def open_snapshot(self, day: date, extra_strikes=None) -> MarketSnapshot:
        return self._snaps[(day, Session.OPEN)]

    def expiries_available(self, day: date) -> list[date]:
        return sorted({oid[0] for oid in self.close_snapshot(day).option_quotes})


def make_put_quotes(
    *,
    today: date,
    S: float,
    expiry: date,
    strikes: list[float],
    iv: float,
    r: float = 0.04,
    q: float = 0.0,
    spread_bps: float = 0.0,
) -> dict[OptionId, tuple[float, float]]:
    """按 BS 定价生成 Put 报价（mid 精确、可选价差）。"""
    bs = BlackScholesEngine()
    T = max((expiry - today).days, 1) / 365.0  # 到期日当天 T=0 时退化为 1 天，避免除零
    out: dict[OptionId, tuple[float, float]] = {}
    for K in strikes:
        mid = bs.price(S=S, K=K, T=T, r=r, q=q, sigma=iv, right=OptionRight.PUT).price
        half = mid * spread_bps / 1e4
        out[(expiry, K, OptionRight.PUT)] = (max(mid - half, 0.01), mid + half)
    return out


def constant_world(
    calendar: TradingCalendar,
    sessions: list[date],
    *,
    expiry: date,
    strikes: list[float],
    iv: float = 0.20,
    S: float = 100.0,
    rate: float = 0.04,
    q: float = 0.0,
) -> StaticProvider:
    """常量世界：每个交易日的 open/close 都是同一价格与同一套 BS 定价链。"""
    prov = StaticProvider(calendar, rate=rate, q=q)
    prov.set_sessions(sessions)
    prev_day = calendar.prev_session(sessions[0])
    for d in [prev_day, *sessions]:
        quotes = make_put_quotes(today=d, S=S, expiry=expiry, strikes=strikes, iv=iv, r=rate, q=q)
        prov.set(d, Session.OPEN, S, quotes, prev_close=S)
        prov.set(d, Session.CLOSE, S, quotes, prev_close=S)
    return prov


class NullStrategy:
    """从不发单。"""

    def on_open(self, ctx) -> list[OrderIntent]:
        return []


class FixedStrategy:
    """固定发单（超额订单 / 强平路径测试用）。"""

    def __init__(self, intents: list[OrderIntent]) -> None:
        self.intents = list(intents)

    def on_open(self, ctx) -> list[OrderIntent]:
        return list(self.intents)


class OnceStrategy:
    """只在首个决策日发单（金标准到期/指派场景用）。"""

    def __init__(self, intents: list[OrderIntent]) -> None:
        self.intents = list(intents)
        self.done = False

    def on_open(self, ctx) -> list[OrderIntent]:
        if self.done:
            return []
        self.done = True
        return list(self.intents)


class RecordingStrategy:
    """记录 (prev_close 日期, OrderIntent 元组) 的代理（防前视测试用）。"""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.log: list[tuple[date, tuple[OrderIntent, ...]]] = []

    def on_open(self, ctx) -> list[OrderIntent]:
        intents = self.inner.on_open(ctx)
        self.log.append((ctx.prev_close.date, tuple(intents)))
        return intents


def make_config(
    *,
    symbol: str = "SPY",
    start: date = date(2025, 1, 1),
    end: date = date(2025, 12, 31),
    **sim_kwargs,
) -> BacktestConfig:
    """零滑点/零手续费的默认配置（金标准手算用；可按需覆盖）。"""
    sim = SimulationConfig(
        slippage_bps=0.0, commission_per_contract=0.0, commission_per_order=0.0, **sim_kwargs
    )
    return BacktestConfig(data=DataConfig(symbol=symbol, start=start, end=end), simulation=sim)
